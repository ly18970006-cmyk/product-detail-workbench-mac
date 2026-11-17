'use strict';

const byId = (id) => document.getElementById(id);
const code = byId('code');
const lookup = byId('lookup');
const editor = byId('layer-text');
const confirm = byId('confirm');
const status = byId('status');
const locateError = byId('locate-error');
const retry = byId('retry');
const panes = ['after', 'before'].map((side) => byId(`${side}-scroll`));
const unified = document.documentElement.hasAttribute('data-workbench');
let draft;
let draftLookup;
let lookupSequence = 0;
let composing = false;
let queueCapability;

function draftMessage(state, value, error = false) {
  state.notice = {value, error};
  if (state === draft) message(value, error);
}

function liveDraft(state) {
  return unified || state === draft;
}

async function api(path, body, signal) {
  const options = {signal};
  if (body !== undefined) {
    Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || '服务请求失败，请重试。');
    error.status = response.status;
    error.fieldId = data.field_id == null ? undefined : String(data.field_id);
    throw error;
  }
  return data;
}

function message(text, error = false) {
  if (status.textContent !== text) status.textContent = text;
  status.classList.toggle('error', error);
}

function supportsRenderQueue() {
  queueCapability ??= api('/api/health').then((data) => data.render_queue === true).catch((error) => {
    queueCapability = undefined;
    throw error;
  });
  return queueCapability;
}

function renderOperation(payload) {
  return {payload: {...payload, request_id: crypto.randomUUID()}};
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function followRender(operation, current, showStatus) {
  if (operation.result) return operation.result;
  while (current()) {
    let task;
    try {
      const signal = AbortSignal.timeout(15000);
      task = operation.statusUrl ? await api(operation.statusUrl, undefined, signal)
        : await api('/api/render-tasks', operation.payload, signal);
    } catch (error) {
      if ((error.status === 404 && operation.statusUrl) || error.status === 409) operation.failed = true;
      if (!error.status) throw new Error('暂时无法连接排版服务，点击重试可继续查询当前任务。');
      throw error;
    }
    if (!current()) return;
    operation.statusUrl = task.status_url ?? operation.statusUrl;
    if (task.state === 'complete') {
      operation.result = task.result;
      return task.result;
    }
    if (task.state === 'failed' || task.state === 'superseded') {
      operation.failed = true;
      const error = new Error(task.error || '排版未完成，请重试。');
      error.status = task.status_code;
      error.fieldId = task.field_id == null ? undefined : String(task.field_id);
      throw error;
    }
    if (!operation.statusUrl || !['queued', 'running'].includes(task.state)) {
      throw new Error('暂时无法读取排版状态，请重试。');
    }
    showStatus(task.state);
    await wait(650);
  }
}

function clearApproval(state) {
  state.job = undefined;
  confirm.disabled = true;
  confirm.hidden = false;
  retry.hidden = true;
  for (const kind of ['psd', 'png']) {
    const link = byId(`download-${kind}`);
    link.hidden = true;
    link.removeAttribute('href');
  }
}

function clearDraft() {
  if (draft) {
    clearTimeout(draft.timer);
    clearApproval(draft);
  }
  draft = undefined;
  composing = false;
  retry.hidden = true;
  locateError.hidden = true;
  byId('actions').hidden = true;
  byId('product').hidden = true;
  byId('product-note').hidden = true;
  byId('layer-editor').hidden = true;
  byId('edit-hint').hidden = true;
  byId('hotspots').replaceChildren();
  for (const side of ['after', 'before']) {
    byId(`${side}-sheet`).hidden = true;
    byId(`${side}-empty`).hidden = false;
    byId(side).removeAttribute('src');
    byId(`${side}-scroll`).scrollTop = 0;
  }
}

function setProduct(product) {
  byId('product-name').textContent = product.name;
  byId('template').textContent = `${product.width} px · ${product.template}`;
  byId('bottle-name').textContent = product.bottle;
  byId('bottle').src = product.bottle_url;
  byId('bottle').alt = `${product.name}瓶图`;
  byId('product-note').textContent = product.note;
  byId('product-note').hidden = !product.note;
  byId('product').hidden = false;
}

async function previewImage(side, url) {
  const previous = byId(side);
  const image = new Image();
  image.id = previous.id;
  image.alt = previous.alt;
  image.draggable = false;
  image.src = url;
  try {
    await image.decode();
  } catch {
    throw new Error('整页预览加载失败，请重试。');
  }
  return image;
}

function showImage(image) {
  const side = image.id;
  byId(side).replaceWith(image);
  byId(`${side}-sheet`).hidden = false;
  byId(`${side}-empty`).hidden = true;
}

function plainText(text) {
  return text.replace(/\r\n?/g, '\n');
}

function updateSelection(state) {
  for (const [id, button] of state.buttons) {
    button.setAttribute('aria-pressed', String(id === state.selected));
    button.classList.toggle('changed', state.applied.has(id));
    button.classList.toggle('error', id === state.invalidField);
  }
  editor.setAttribute('aria-invalid', String(state.selected === state.invalidField && !!state.selected));
  locateError.hidden = !state.buttons.has(state.invalidField);
}

function selectField(state, id) {
  if (state !== draft) return;
  const field = state.baseFields.get(id);
  if (id !== state.selected) {
    state.selected = id;
    byId('layer-name').textContent = field.name;
    byId('layer-screen').textContent = field.screen;
    editor.value = state.edits.get(id) ?? field.text;
    byId('layer-editor').hidden = false;
    byId('edit-hint').hidden = true;
    updateSelection(state);
  }
  editor.focus({preventScroll: !matchMedia('(max-width: 760px)').matches});
}

function placeHotspots(state, fields) {
  for (const {id: fieldId, bounds, text} of fields) {
    const id = String(fieldId);
    const button = state.buttons.get(id);
    if (!button) continue;
    const [left, top, right, bottom] = bounds;
    button.style.left = `${100 * left / state.width}%`;
    button.style.top = `${100 * top / state.height}%`;
    button.style.width = `${100 * (right - left) / state.width}%`;
    button.style.height = `${100 * (bottom - top) / state.height}%`;
    button.setAttribute('aria-label', `${button.title} · ${plainText(text).replace(/\n/g, '／')}`);
  }
  updateSelection(state);
}

function makeHotspots(state) {
  const elements = document.createDocumentFragment();
  for (const [id, field] of state.baseFields) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'hotspot';
    button.dataset.fieldId = id;
    button.style.zIndex = state.baseFields.size - state.buttons.size;
    button.title = `${field.screen} · ${field.name}`;
    button.addEventListener('click', () => selectField(state, id));
    state.buttons.set(id, button);
    elements.append(button);
  }
  byId('hotspots').replaceChildren(elements);
  placeHotspots(state, state.fields ?? state.baseFields.values());
}

function editableDraft(result, requested, usesQueue) {
  return {
    id: result.id, code: requested, width: result.width, height: result.height, usesQueue,
    baseline: result.before_url, revision: result.revision ?? 0, source: result.source,
    baseFields: new Map(result.fields.map((field) => [String(field.id), {...field, text: plainText(field.text)}])),
    edits: new Map(), applied: new Set(), buttons: new Map(),
  };
}

async function lookupProduct() {
  if (lookup.disabled || !code.reportValidity()) return;
  const requested = code.value.trim();
  if (draft?.code === requested) return;
  const sequence = ++lookupSequence;
  if (draftLookup?.code !== requested) {
    clearDraft();
    draftLookup = {code: requested};
  }
  const lookupState = draftLookup;
  const current = () => sequence === lookupSequence;
  lookup.disabled = true;
  retry.hidden = true;
  message('正在查找商品并加载整页。');
  try {
    const [product, usesQueue] = await Promise.all([
      api(`/api/products/${encodeURIComponent(requested)}`), supportsRenderQueue(),
    ]);
    if (!current()) return;
    setProduct(product);
    if (usesQueue && (!lookupState.operation || lookupState.operation.failed)) {
      lookupState.operation = renderOperation({kind: 'draft', code: requested});
    }
    const result = usesQueue ? await followRender(lookupState.operation, current, (state) => {
      message(state === 'queued' ? '排队中，轮到后将自动加载整页。' : '正在排版，准备改前基准。');
    }) : await api('/api/drafts', {code: requested});
    if (!current()) return;
    const images = await Promise.all(['after', 'before'].map((side) => previewImage(side, result.before_url)));
    if (!current()) return;
    draft = editableDraft(result, requested, usesQueue);
    draftLookup = undefined;
    for (const side of ['after', 'before']) {
      byId(`${side}-sheet`).style.setProperty('--sheet-ratio', `${draft.width} / ${draft.height}`);
    }
    images.forEach(showImage);
    makeHotspots(draft);
    byId('actions').hidden = false;
    byId('edit-hint').hidden = !draft.baseFields.size;
    message(draft.baseFields.size ? '' : '这版母版没有可点选的独立文字层。');
  } catch (error) {
    if (current()) {
      message(error.message, true);
      retry.textContent = '重试加载';
      retry.hidden = false;
    }
  } finally {
    if (current()) lookup.disabled = false;
  }
}

function schedulePreview(state, delay = 350) {
  clearTimeout(state.timer);
  state.timer = setTimeout(() => {
    state.timer = undefined;
    if ((state.usesQueue || !state.active) && (state !== draft || !composing)) renderPreview(state);
  }, delay);
}

function requestUpdate() {
  const state = draft;
  if (!state?.selected) return;
  const field = state.baseFields.get(state.selected);
  const value = plainText(editor.value);
  const previous = state.edits.get(state.selected) ?? field.text;
  if (previous === value) return;
  if (value === field.text) state.edits.delete(state.selected);
  else state.edits.set(state.selected, value);
  const invalidate = Boolean(state.job || state.active || state.operation);
  clearApproval(state);
  state.invalidField = undefined;
  state.pending = true;
  state.operation = undefined;
  state.directRetries = 0;
  const revision = ++state.revision;
  if (invalidate) {
    state.reservation = (state.reservation ?? Promise.resolve()).then(() => {
      if (liveDraft(state) && revision === state.revision) {
        return api(`/api/drafts/${state.id}/revision`, {revision}, AbortSignal.timeout(4000));
      }
    }).catch(() => {});
  }
  updateSelection(state);
  state.delivered = undefined;
  state.retryVisible = false;
  draftMessage(state, '正在更新预览…');
  schedulePreview(state);
}

async function renderPreview(state) {
  if (!liveDraft(state) || !state.pending) return;
  state.pending = false;
  const revision = state.revision;
  const edits = Object.fromEntries(state.edits);
  const operation = state.operation ?? (state.usesQueue
    ? renderOperation({kind: 'preview', draft_id: state.id, revision, edits}) : {});
  state.operation = operation;
  state.active = operation;
  const current = () => liveDraft(state) && state.revision === revision;
  try {
    await state.reservation;
    if (!current()) return;
    const result = state.usesQueue ? await followRender(operation, current, (status) => {
      draftMessage(state, status === 'queued' ? '排队中，可继续修改文案。' : '正在排版，可继续修改文案。');
    }) : await api(`/api/drafts/${state.id}/preview`, {revision, edits});
    if (!current()) return;
    const image = await previewImage('after', result.preview_url || state.baseline);
    if (!current()) return;
    if (state === draft) showImage(image);
    state.afterImage = image;
    state.fields = result.fields;
    state.applied = new Set(Object.keys(edits));
    if (state === draft) placeHotspots(state, state.fields ?? state.baseFields.values());
    if (result.state === 'preview' && result.id && result.receipt) {
      state.job = result;
      if (state === draft) confirm.disabled = composing;
      draftMessage(state, state === draft && composing ? '输入完成后将自动更新。' : `已更新 ${state.applied.size} 处，可确认导出。`);
    } else {
      draftMessage(state, '已恢复原稿。');
    }
  } catch (error) {
    if (!current()) return;
    if (!state.usesQueue && error.status === 429 && state.directRetries < 3) {
      state.pending = true;
      ++state.revision;
      schedulePreview(state, 600 * ++state.directRetries);
      draftMessage(state, '排版服务正在处理其他任务，稍后自动重试。');
      return;
    }
    state.invalidField = error.fieldId;
    state.retryVisible = error.status !== 400 && !error.fieldId;
    if (state === draft) {
      updateSelection(state);
      retry.textContent = '重试更新';
      retry.hidden = !state.retryVisible;
    }
    draftMessage(state, error.message, true);
  } finally {
    if (state.active === operation) state.active = undefined;
    if (liveDraft(state) && state.pending && state.timer === undefined && (state !== draft || !composing)) {
      schedulePreview(state, 0);
    }
  }
}

panes.forEach((pane, index) => {
  const opposite = panes[1 - index];
  let programmaticTop;
  pane.setSyncedTop = (top) => {
    if (Math.abs(pane.scrollTop - top) < 0.5) return;
    pane.scrollTop = top;
    programmaticTop = pane.scrollTop;
  };
  pane.addEventListener('scroll', () => {
    if (programmaticTop === pane.scrollTop) {
      programmaticTop = undefined;
      return;
    }
    programmaticTop = undefined;
    if (unified && workMode === 'refresh') syncWorkbenchScroll(index);
    else opposite.setSyncedTop(pane.scrollTop);
  }, {passive: true});
});

locateError.addEventListener('click', () => {
  const field = draft?.buttons.get(draft.invalidField);
  if (!field) return;
  selectField(draft, draft.invalidField);
  const [after, before] = panes;
  after.setSyncedTop(field.offsetTop + (field.offsetHeight - after.clientHeight) / 2);
  before.setSyncedTop(after.scrollTop);
});

if (!unified) {
  code.addEventListener('input', () => {
    ++lookupSequence;
    draftLookup = undefined;
    clearDraft();
    lookup.disabled = false;
    message('');
  });
  lookup.addEventListener('click', lookupProduct);
  byId('edit-form').addEventListener('submit', (event) => {
    event.preventDefault();
    lookupProduct();
  });
}

editor.addEventListener('compositionstart', () => {
  composing = true;
  confirm.disabled = true;
});
editor.addEventListener('input', (event) => {
  if (!composing && !event.isComposing) requestUpdate();
});
editor.addEventListener('compositionend', () => {
  composing = false;
  requestUpdate();
  if (draft?.pending && draft.timer === undefined) schedulePreview(draft);
  if (draft?.job && !draft.pending && !draft.active && !confirm.hidden) {
    confirm.disabled = false;
    message(`已更新 ${draft.applied.size} 处，可确认导出。`);
  }
});

retry.addEventListener('click', () => {
  if (!draft) {
    if (!unified) lookupProduct();
    return;
  }
  if (draft.active) return;
  retry.hidden = true;
  draft.retryVisible = false;
  draft.invalidField = undefined;
  updateSelection(draft);
  draft.pending = true;
  draft.directRetries = 0;
  if (!draft.usesQueue || !draft.operation || draft.operation.failed) {
    draft.operation = undefined;
    ++draft.revision;
  }
  draftMessage(draft, '正在更新预览…');
  schedulePreview(draft, 0);
});

confirm.addEventListener('click', async () => {
  const state = draft;
  const job = state?.job;
  if (!job || composing || state.active || state.pending || state.confirming) return;
  state.confirming = true;
  confirm.disabled = true;
  editor.readOnly = true;
  if (!unified) {
    code.disabled = true;
    lookup.disabled = true;
  }
  draftMessage(state, '正在确认并导出当前版本。');
  try {
    const result = await api(`/api/jobs/${job.id}/confirm`, {receipt: job.receipt});
    state.delivered = result;
    if (state !== draft) return;
    for (const kind of ['psd', 'png']) {
      const link = byId(`download-${kind}`);
      link.href = result[`${kind}_url`];
      link.hidden = false;
    }
    confirm.hidden = true;
    draftMessage(state, '已导出，可下载 PSD 和整页 PNG。');
  } catch (error) {
    draftMessage(state, error.message, true);
    if (error.status === 409) {
      if (state === draft) clearApproval(state);
      else state.job = undefined;
      state.operation = undefined;
      state.retryVisible = true;
      if (state === draft) {
        retry.textContent = '重试更新';
        retry.hidden = false;
      }
    } else if (state === draft) {
      confirm.disabled = false;
    }
  } finally {
    state.confirming = false;
    if (!unified || state === draft) editor.readOnly = false;
    if (!unified) {
      code.disabled = false;
      lookup.disabled = false;
    }
  }
});

if (!unified) {
  api('/api/products').then(({products}) => {
    for (const item of products) {
      const option = document.createElement('option');
      option.value = item.code;
      option.label = item.name;
      byId('codes').append(option);
    }
  }).catch(() => {});
  lookupProduct();
}
