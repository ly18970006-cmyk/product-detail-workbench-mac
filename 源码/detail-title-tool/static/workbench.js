'use strict';

const productsByCode = new Map();
const modeEdit = byId('mode-edit');
const modeRefresh = byId('mode-refresh');
const materialForm = byId('refresh-materials');
const editSource = byId('edit-source');
const refreshScope = byId('refresh-scope');
const refreshPlanMode = byId('refresh-plan-mode');
const refreshPreviewMode = byId('refresh-preview-mode');
const openEdited = byId('refresh-open-edit');
const protectedSunburst = 'protected_sunburst';
const protectedSkuKey = 'detail-workbench-protected-sku';
const protectedSubmissionKey = 'detail-workbench-protected-submission';
let activeProduct;
let workMode = 'edit';
let requestSequence = 0;
let viewSequence = 0;
let refreshHealth;

function selectedEdit(product) {
  return product?.edits.get(product.editSource);
}

function visibleMode(product, mode) {
  return activeProduct === product && workMode === mode;
}

function rememberView() {
  if (!activeProduct) return;
  const state = workMode === 'edit' ? draft : activeProduct.refresh;
  if (state) state.scroll = panes[0].scrollTop;
}

function resetWorkspace() {
  ++viewSequence;
  draft = undefined;
  composing = false;
  locateError.hidden = true;
  retry.hidden = true;
  byId('layer-editor').hidden = true;
  byId('edit-hint').hidden = true;
  byId('hotspots').replaceChildren();
  for (const side of ['after', 'before']) {
    byId(`${side}-sheet`).hidden = true;
    byId(`${side}-empty`).hidden = false;
    const previous = byId(side);
    const empty = previous.cloneNode(false);
    empty.removeAttribute('src');
    previous.replaceWith(empty);
    byId(`${side}-scroll`).scrollTop = 0;
  }
  setDelivery();
  for (const id of ['refresh-full-old', 'refresh-full-new', 'refresh-full-comparison', 'refresh-baseline-comparison',
    'refresh-scene-candidate']) byId(id).hidden = true;
  message('');
}

function setDelivery(files, canConfirm = false, canOpen = false) {
  confirm.hidden = Boolean(files);
  confirm.disabled = !canConfirm;
  byId('actions').hidden = !(canConfirm || files);
  openEdited.hidden = !canOpen;
  for (const kind of ['psd', 'png']) {
    const link = byId(`download-${kind}`);
    const url = files?.[`detail.${kind}`] ?? files?.[`${kind}_url`];
    link.hidden = !url;
    if (url) link.href = url;
    else link.removeAttribute('href');
  }
}

function productDetails(data) {
  byId('product').hidden = false;
  byId('product-name').textContent = data.name || `商品 ${data.code}`;
  byId('template').textContent = [data.width ? `${data.width} px` : '', data.template].filter(Boolean).join(' · ');
  byId('bottle-name').textContent = data.bottle_is_candidate && data.bottle ? `候选瓶图 · ${data.bottle}` : data.bottle || '';
  byId('bottle-frame').hidden = !data.bottle_url;
  if (data.bottle_url) {
    byId('bottle').src = data.bottle_url;
    byId('bottle').alt = `${data.name}瓶图`;
  } else byId('bottle').removeAttribute('src');
  byId('product-note').textContent = data.note || '';
  byId('product-note').hidden = !data.note;
  const capabilities = data.capabilities;
  const enabled = ['edit', 'refresh'].filter(mode => capabilities[mode].available);
  byId('capability-summary').textContent = enabled.length === 2 ? '可改字 · 可翻新'
    : enabled.length ? `可${enabled[0] === 'edit' ? '改字' : '翻新'}` : '当前素材待补齐';
  byId('edit-capability').textContent = capabilities.edit.reason || '';
  byId('refresh-capability').textContent = capabilities.refresh.reason || '';
}

async function lookupWorkbench() {
  if (!code.reportValidity()) return;
  const value = code.value.trim();
  const sequence = ++requestSequence;
  lookup.disabled = true;
  byId('lookup-status').textContent = '正在查找商品…';
  try {
    const data = await api(`/api/workbench/products/${encodeURIComponent(value)}`);
    if (sequence !== requestSequence) return;
    if (protectedSku() !== value) protectedSku('');
    rememberView();
    const product = productsByCode.get(value) ?? {
      code: value, editSource: 'registered', edits: new Map([['registered', {label: '已登记母版'}]]), refresh: {selected: 'all'},
    };
    product.data = data;
    productsByCode.set(value, product);
    activeProduct = product;
    productDetails(data);
    byId('lookup-status').textContent = '';
    const mode = (workMode === 'edit' && !data.capabilities.edit.available && !selectedEdit(product)?.state
      && data.capabilities.refresh.available) ? 'refresh'
      : workMode === 'refresh' && !data.capabilities.refresh.available && data.capabilities.edit.available ? 'edit' : workMode;
    activateMode(mode, false);
  } catch (error) {
    if (sequence !== requestSequence) return;
    rememberView();
    activeProduct = undefined;
    resetWorkspace();
    byId('product').hidden = true;
    byId('capability-summary').textContent = '';
    byId('edit-capability').textContent = '';
    byId('refresh-capability').textContent = '';
    syncRefresh();
    byId('lookup-status').textContent = error.message;
  } finally {
    if (sequence === requestSequence) lookup.disabled = false;
  }
}

function activateMode(mode, saveScroll = true) {
  if (saveScroll) rememberView();
  workMode = mode;
  resetWorkspace();
  const isEdit = mode === 'edit';
  modeEdit.setAttribute('aria-pressed', String(isEdit));
  modeRefresh.setAttribute('aria-pressed', String(!isEdit));
  byId('edit-panel').hidden = !isEdit;
  byId('refresh-panel').hidden = isEdit;
  byId('after-label').textContent = isEdit ? '改后 · 点击文字编辑' : '翻新预览';
  byId('before-label').textContent = isEdit ? '改前' : '旧详情页';
  byId('after-empty').textContent = isEdit ? '查找商品后显示整页' : '生成后显示翻新预览';
  byId('before-empty').textContent = isEdit ? '查找商品后显示整页' : '登记素材后显示旧详情页';
  if (!activeProduct) return;
  if (isEdit) activateEdit(activeProduct);
  else {
    syncRefresh();
    showRefreshImages(activeProduct);
    loadRefresh(activeProduct);
  }
}

function draftSourceLabel(state) {
  return state.source ? '来源为已确认翻新稿，保留原场景和文字图层。' : '';
}

function paintEdit(product, state) {
  if (!visibleMode(product, 'edit') || selectedEdit(product).state !== state) return;
  draft = state;
  draft.buttons.clear();
  for (const side of ['after', 'before']) {
    byId(`${side}-sheet`).style.setProperty('--sheet-ratio', `${state.width} / ${state.height}`);
  }
  showImage(state.beforeImage);
  showImage(state.afterImage);
  makeHotspots(state);
  if (state.selected) {
    const field = state.baseFields.get(state.selected);
    byId('layer-name').textContent = field.name;
    byId('layer-screen').textContent = field.screen;
    editor.value = state.edits.get(state.selected) ?? field.text;
    byId('layer-editor').hidden = false;
  } else {
    byId('edit-hint').hidden = !state.baseFields.size;
  }
  editor.readOnly = Boolean(state.confirming);
  updateSelection(state);
  retry.hidden = !state.retryVisible;
  retry.textContent = '重试更新';
  const source = byId('edit-provenance');
  source.textContent = draftSourceLabel(state);
  source.hidden = !state.source;
  setDelivery(state.delivered, Boolean(state.job && !state.confirming && !state.active && !state.pending));
  if (!state.delivered) byId('actions').hidden = false;
  panes[0].setSyncedTop(state.scroll || 0);
  panes[1].setSyncedTop(state.scroll || 0);
  message(state.notice?.value ?? '', state.notice?.error);
}

function activateEdit(product) {
  editSource.replaceChildren();
  for (const [id, entry] of product.edits) editSource.add(new Option(entry.label, id));
  editSource.value = product.editSource;
  byId('edit-source-row').hidden = product.edits.size < 2;
  byId('edit-provenance').hidden = true;
  const entry = selectedEdit(product);
  if (entry.state) paintEdit(product, entry.state);
  else if (product.data.capabilities.edit.available || entry.payload) createEdit(product, entry);
}

async function createEdit(product, entry) {
  if (entry.loading) {
    message(entry.notice || '正在加载改字稿…');
    return;
  }
  entry.loading = true;
  if (!entry.operation || entry.operation.failed) {
    entry.operation = renderOperation(entry.payload ?? {kind: 'draft', code: product.code});
  }
  const visible = () => visibleMode(product, 'edit') && selectedEdit(product) === entry;
  const show = (value, error = false) => {
    entry.notice = value;
    if (visible()) message(value, error);
  };
  show('正在加载改字稿…');
  try {
    const result = await followRender(entry.operation, () => true, state => {
      show(state === 'queued' ? '改字稿排队中…' : '正在准备改字稿…');
    });
    const [beforeImage, afterImage] = await Promise.all(['before', 'after'].map(side => previewImage(side, result.before_url)));
    entry.state = Object.assign(editableDraft(result, product.code, true), {beforeImage, afterImage});
    paintEdit(product, entry.state);
  } catch (error) {
    show(error.message, true);
    if (visible()) {
      retry.textContent = '重试加载';
      retry.hidden = false;
    }
  } finally {
    entry.loading = false;
  }
}

async function refreshRequest(path, body, key) {
  const options = {signal: AbortSignal.timeout(15000)};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json'};
    if (key) options.headers['Idempotency-Key'] = key;
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.message || data.error || '翻新服务暂时不可用，请重试。');
    error.status = response.status;
    throw error;
  }
  return data;
}

const refreshProductPath = product => `/api/refresh/products/${product.code}`;
const candidateKinds = {old_page: 'old_pages', bottle: 'bottles', operations: 'operations', logo: 'logos'};
const materialNames = {old_page: '旧详情页', bottle: '可用透明瓶图', operations: '运营资料', logo: '品牌标志',
  official_logo: '官方标志', packaging: '核准包装信息', channel: '核准渠道信息', brand_rule: '核准品牌规范', approved_copy: '核准文案'};

function setRefreshNotice(product, value, error = false) {
  product.refresh.notice = {value, error};
  if (visibleMode(product, 'refresh')) message(value, error);
}

function updateCandidates(state) {
  const {candidates, registration} = state;
  for (const [field, kind] of Object.entries(candidateKinds)) {
    const select = materialForm.elements.namedItem(field);
    const rows = (candidates?.[kind] ?? []).filter(candidate => field !== 'bottle' || candidate.transparent);
    const registered = registration?.selection?.[field]?.id;
    const original = state.material?.[field] ?? registered ?? (registration?.reference_profile_stale ? '' : rows[0]?.id);
    select.replaceChildren(new Option(field === 'old_page' ? '请选择' : '未选择', ''));
    for (const row of rows) select.add(new Option(row.name, row.id));
    select.value = rows.some(row => row.id === original) ? original : '';
  }
}

function companyAccess(product, step) {
  return Boolean(refreshHealth?.company?.configured && refreshHealth.pilot_limit > 0
    && product?.data.capabilities.refresh.company_scope?.[step]);
}

function protectedSku(value) {
  try {
    if (value === undefined) return sessionStorage.getItem(protectedSkuKey);
    if (value) sessionStorage.setItem(protectedSkuKey, value);
    else sessionStorage.removeItem(protectedSkuKey);
  } catch {}
}

function protectedSubmission(value) {
  try {
    if (value === undefined) return JSON.parse(sessionStorage.getItem(protectedSubmissionKey));
    if (value) sessionStorage.setItem(protectedSubmissionKey, JSON.stringify(value));
    else sessionStorage.removeItem(protectedSubmissionKey);
  } catch {}
}

function protectedOperation(state, key) {
  return {endpoint: '/api/refresh/previews', key, payload: {plan_job_id: state.plan.id,
    screens: [state.plan.result.plan.protected_scene_screen], mode: protectedSunburst}};
}

function protectedScope(state) {
  const screen = state?.plan?.result.plan.protected_scene_screen;
  return Boolean(screen && screen === state.selected);
}

function screenPreview(state) {
  if (protectedScope(state) && state.protectedPreview) return state.protectedPreview;
  if (state?.plan?.result.plan.whole_review && state.selected !== state.previewScope) return undefined;
  return state?.preview;
}

function protectedRun(task, state) {
  return task?.result?.background_runs?.[state.plan.result.plan.protected_scene_screen];
}

const protectedStatuses = {
  reference_edit_not_authorized: '商品未获准调用公司模型',
  unavailable: '公司网关不可用',
  invalid_model: '网关未配置指定的 Sunburst 模型',
  pilot_limit: '公司模型试点额度已用完',
  lookup_failed: '未通过模型可用性检查',
  failed: '公司网关返回生成失败',
  unknown: '公司网关结果待核对',
  pending: '公司网关尚未返回结果',
  invalid_image: '网关返回的图片无法使用',
  invalid_scene_geometry: '网关返回图与首屏比例不符',
  protection_failed: '画面保护校验未通过',
  no_lighting_change: '未检测到可保留的光影变化',
  prior_request_unresolved: '已有首屏生成任务待核对',
};

function protectedStatus(task, state) {
  const run = protectedRun(task, state);
  if (run?.effect_applied && run.candidate) {
    return {value: `Sunburst 首屏候选待审${run.model ? `。实际模型：${run.model}` : ''}。`, error: false};
  }
  const detail = task.error?.message || protectedStatuses[run?.status] || run?.reason || run?.error;
  const failure = detail ? `（${detail}）` : '';
  return {value: task.result?.preview
    ? `未生成可用的 Sunburst 候选${failure}，当前显示冻结首屏。`
    : `Sunburst 首屏${task.state === 'unknown' ? '结果待核对' : '生成失败'}${failure}。`, error: true};
}

function setProtectedNotice(product, value, error = false) {
  product.refresh.protectedNotice = {value, error};
  if (protectedScope(product.refresh)) setRefreshNotice(product, value, error);
}

function finishProtectedPreview(product, task) {
  const state = product.refresh;
  state.protectedTaskId = task.id;
  state.protectedPreview = task.result?.preview ? task : undefined;
  state.protectedUnresolved = task.state === 'unknown'
    || ['unknown', 'pending', 'prior_request_unresolved'].includes(protectedRun(task, state)?.status);
  const notice = protectedStatus(task, state);
  setProtectedNotice(product, notice.value, notice.error);
}

function refreshMode(product, step) {
  if (step === 'preview' && product?.refresh?.plan?.result.plan.whole_review) return 'whole_review';
  return companyAccess(product, step) ? product.refresh[`${step}Mode`] ?? 'local_review' : 'local_review';
}

function selectedScreens(state) {
  return (state?.plan?.result.plan.screens ?? []).filter(screen => state.selected === 'all' || state.selected === screen.id);
}

function refreshModeNote(product) {
  if (!product?.data.capabilities.refresh.available) return '';
  const {plan, preview} = product.refresh;
  if (product.refresh.registration?.review_plan?.whole_review) {
    return '对照栏已对齐实际品牌首屏，完整旧页保留双包装公告。';
  }
  const layoutModel = plan?.result.call?.model;
  const sceneModels = preview ? companySceneModels(preview) : [];
  if (layoutModel || preview) {
    return [layoutModel && `分屏 · ${layoutModel}`, preview && (sceneModels.length
      ? `场景 · ${sceneModels.join(' / ')}` : '场景 · 本地试排')].filter(Boolean).join('。');
  }
  if (!refreshHealth?.company?.configured) return '本地版式验证 · 未调用模型。';
  if (!refreshHealth.pilot_limit) return '公司网关已配置，模型试点额度为 0。本地版式验证可用。';
  const companyPlan = companyAccess(product, 'plan');
  const companyPreview = companyAccess(product, 'preview');
  if (!companyPlan && !companyPreview) return '当前商品未开通公司模型素材范围。本地版式验证可用。';
  const models = [companyPlan && refreshHealth.company.layout_model,
    companyPreview && refreshHealth.company.image_model].filter(Boolean);
  return `本地版式验证可用。公司模型${models.length ? ` · ${models.join(' / ')}` : '已开通试点'}`;
}

function companySceneModels(task) {
  const runs = Object.values(task.result.background_runs ?? {});
  return [...new Set(runs.filter(run => ['company_scene_prompt_only', 'company_scene_edit'].includes(run.source) && run.model).map(run => run.model))];
}

function previewNotice(task) {
  if (task.result.review_only) return '整页草稿已就绪，待审稿。';
  const models = companySceneModels(task);
  return models.length ? `公司模型场景预览已就绪 · ${models.join(' / ')}。可确认内部版式。`
    : '本地场景预览已就绪，可确认内部版式。';
}

function canConfirmRefresh(product) {
  const state = product?.refresh;
  return Boolean(!protectedScope(state) && state?.preview && !state.preview.result.review_only
    && !state.busy && !state.unknown && state.selected === state.previewScope
    && refreshMode(product, 'preview') === state.previewModeAtRun);
}

function syncRefresh() {
  const product = activeProduct;
  const state = product?.refresh;
  materialForm.hidden = !state?.candidates;
  const planButton = byId('refresh-plan');
  planButton.hidden = !state?.registration;
  planButton.textContent = state?.registration?.protected_scene_screen && !state.plan ? '准备品牌首屏' : '分析分屏';
  byId('refresh-screens').hidden = !state?.plan;
  byId('refresh-model').textContent = refreshModeNote(product);
  const missing = [...new Set(state?.registration?.missing ?? state?.candidates?.missing ?? [])];
  byId('refresh-missing').hidden = !missing.length;
  byId('refresh-missing').textContent = missing.length ? `待补齐：${missing.map(item => materialNames[item] ?? item).join('、')}` : '';
  if (state?.candidates) updateCandidates(state);
  if (state?.plan) {
    refreshScope.replaceChildren(new Option('整页', 'all'));
    for (const screen of state.plan.result.plan.screens) {
      refreshScope.add(new Option(screen.name, screen.id));
    }
    refreshScope.value = state.selected;
  }
  const busy = Boolean(state?.busy);
  const unresolved = Boolean(state?.unknown);
  const protectedSelected = protectedScope(state);
  const companyPlan = companyAccess(product, 'plan');
  const companyPreview = companyAccess(product, 'preview');
  byId('refresh-plan-mode-row').hidden = !state?.registration || state.registration.protected_scene_screen || !companyPlan;
  refreshPlanMode.value = refreshMode(product, 'plan');
  refreshPlanMode.disabled = busy;
  byId('refresh-preview-mode-row').hidden = protectedSelected || !companyPreview;
  refreshPreviewMode.value = refreshMode(product, 'preview');
  refreshPreviewMode.disabled = busy;
  const companySelected = refreshPreviewMode.value === 'company';
  const validCompanyScope = selectedScreens(state).filter(screen => screen.kind === 'visual').length === 1;
  const scopeNote = byId('refresh-preview-scope-note');
  scopeNote.hidden = !protectedSelected && !companySelected;
  scopeNote.textContent = protectedSelected ? '保持文字、标志和瓶身，生成后可比较光影变化。'
    : '公司场景试点每次处理一个视觉屏，请按需选择。';
  const registerButton = materialForm.querySelector('button');
  registerButton.textContent = state?.registration?.reference_profile_stale ? '重新登记并准备品牌首屏' : '登记素材';
  registerButton.disabled = busy;
  for (const select of materialForm.querySelectorAll('select')) select.disabled = busy;
  planButton.disabled = busy || unresolved;
  const previewButton = byId('refresh-preview');
  previewButton.textContent = protectedSelected ? '生成 1 张 Sunburst 首屏候选' : '生成翻新预览';
  previewButton.disabled = busy || unresolved || (protectedSelected && state.protectedUnresolved)
    || (companySelected && !validCompanyScope);
  refreshScope.disabled = busy;
  byId('refresh-query').hidden = !state?.unknown;
  byId('refresh-retry').hidden = !state?.retry;
  const preview = screenPreview(state);
  const run = protectedSelected ? protectedRun(state.protectedPreview, state) : undefined;
  const candidate = protectedSelected ? (run?.effect_applied ? run.candidate : undefined)
    : Object.values(state?.sceneCandidateTask?.result.background_runs ?? {}).find(run => run.candidate)?.candidate;
  byId('refresh-scene-candidate').textContent = protectedSelected ? '打开首屏候选 PNG' : '查看场景候选';
  byId('refresh-full-comparison').textContent = protectedSelected ? '与上线旧页对照' : '打开同宽对照';
  byId('after-label').textContent = protectedSelected ? (candidate ? '首屏候选 · 待审' : preview ? '冻结首屏' : '首屏候选') : '翻新预览';
  byId('before-label').textContent = protectedSelected ? '上线旧页 · 品牌首屏' : '旧详情页';
  for (const [id, url] of [
    ['refresh-full-old', state?.registration?.full_old_page_url],
    ['refresh-full-new', !protectedSelected && state?.preview?.result.review_only && state.preview.result.preview],
    ['refresh-full-comparison', protectedSelected ? state.protectedPreview?.result.comparison : state?.preview?.result.comparison],
    ['refresh-baseline-comparison', protectedSelected && state.protectedPreview?.result.baseline_comparison],
    ['refresh-scene-candidate', candidate],
  ]) {
    const link = byId(id);
    link.hidden = !url;
    if (url) link.href = url;
    else link.removeAttribute('href');
  }
  openEdited.disabled = Boolean(state?.opening);
  const delivered = protectedSelected ? undefined : state?.delivered;
  setDelivery(delivered?.result.files, canConfirmRefresh(product), Boolean(delivered));
  if (!protectedSelected && state?.preview && !state.preview.result.review_only && !delivered) byId('actions').hidden = false;
  if (state?.notice) message(state.notice.value, state.notice.error);
}

async function showRefreshImages(product) {
  const state = product.refresh;
  const sequence = ++viewSequence;
  let oldPage = state.registration?.old_page_url;
  const wholeReview = state.plan?.result.plan.whole_review;
  if (oldPage && wholeReview && state.selected !== 'all') oldPage += `&screen=${encodeURIComponent(state.selected)}`;
  const newPage = screenPreview(state)?.result.preview;
  try {
    const images = await Promise.all([
      oldPage ? previewImage('before', oldPage) : null,
      newPage ? previewImage('after', newPage) : null,
    ]);
    if (!visibleMode(product, 'refresh') || sequence !== viewSequence) return;
    for (const image of images.filter(Boolean)) {
      byId(`${image.id}-sheet`).style.setProperty('--sheet-ratio', `${image.naturalWidth} / ${image.naturalHeight}`);
      showImage(image);
    }
    if (wholeReview && !newPage) {
      byId('after-sheet').hidden = true;
      byId('after-empty').hidden = false;
    }
    panes[0].setSyncedTop(state.scroll || 0);
    if (!newPage) panes[1].setSyncedTop(state.scroll || 0);
    if (newPage) syncWorkbenchScroll(0);
  } catch (error) {
    if (visibleMode(product, 'refresh') && sequence === viewSequence) setRefreshNotice(product, error.message, true);
  }
}

function syncWorkbenchScroll(index) {
  const pane = panes[index];
  const opposite = panes[1 - index];
  const from = pane.scrollHeight - pane.clientHeight;
  const to = opposite.scrollHeight - opposite.clientHeight;
  if (from > 0) opposite.setSyncedTop(pane.scrollTop / from * Math.max(0, to));
}

async function loadRefresh(product) {
  const state = product.refresh;
  if (state.candidates || state.loading || !product.data.capabilities.refresh.available) return;
  state.loading = true;
  state.retry = undefined;
  setRefreshNotice(product, '正在读取可登记的素材…');
  let resumeProtected;
  try {
    const path = refreshProductPath(product);
    const [candidates, existing, health] = await Promise.all([
      refreshRequest(path + '/candidates'),
      refreshRequest(path).catch(error => {
        if (error.status !== 404) throw error;
      }),
      refreshRequest('/api/refresh/health').catch(() => undefined),
    ]);
    if (health) refreshHealth = health;
    const history = existing ? await refreshRequest(path + '/jobs').catch(error => {
      if (existing.protected_scene_screen) throw error;
      return {jobs: []};
    }) : {jobs: []};
    state.candidates = candidates;
    state.registration = existing;
    if (existing) {
      state.plan = history.jobs.find(task => task.action === 'plan' && task.state === 'succeeded'
        && task.result?.registration === existing.version);
      if (state.plan?.result.plan.whole_review) {
        const screens = state.plan.result.plan.screens;
        state.sceneCandidateTask = history.jobs.find(task => task.action === 'preview' && task.state === 'succeeded'
          && task.review_kind !== protectedSunburst
          && task.result?.plan_job_id === state.plan.id
          && Object.values(task.result.background_runs ?? {}).some(run => run.candidate_pending_visual_review));
        const latest = history.jobs.find(task => task.action === 'preview' && task.state === 'succeeded'
          && task.review_kind !== protectedSunburst
          && task.result?.review_only && task.result.plan_job_id === state.plan.id
          && (task.result.screens.length === screens.length || task.result.screens.length === 1));
        if (latest) {
          state.preview = latest;
          state.previewScope = latest.result.screens.length === screens.length ? 'all' : latest.result.screens[0];
          state.selected = state.previewScope;
          state.previewModeAtRun = 'whole_review';
        }
        const protectedScreen = state.plan.result.plan.protected_scene_screen;
        const protectedJob = protectedScreen && history.jobs.find(task => task.action === 'preview'
          && task.review_kind === protectedSunburst && task.plan_job_id === state.plan.id);
        const submission = protectedSubmission();
        const awaitingSubmission = protectedScreen && submission?.plan === state.plan.id && submission.prior === (protectedJob?.id ?? null);
        if (protectedJob || awaitingSubmission || (protectedScreen && protectedSku() === product.code)) {
          state.selected = protectedScreen;
          if (awaitingSubmission) {
            state.protectedTaskId = submission.prior;
            state.protectedUnresolved = true;
            state.retry = () => runRefresh(product, 'protected', protectedOperation(state, submission.key));
            setProtectedNotice(product, '上次提交尚未返回任务编号。点击重试可沿用同一提交编号。', true);
          } else if (protectedJob?.state === 'queued' || protectedJob?.state === 'running') {
            resumeProtected = protectedJob;
          } else if (protectedJob) {
            if (submission?.plan === state.plan.id) protectedSubmission(null);
            finishProtectedPreview(product, protectedJob);
          }
        }
      }
    }
    const protectedNotice = protectedScope(state) && state.protectedNotice;
    if (protectedNotice) setRefreshNotice(product, protectedNotice.value, protectedNotice.error);
    else setRefreshNotice(product, resumeProtected ? '正在恢复 Sunburst 首屏任务…'
      : existing?.reference_profile_stale ? '品牌首屏参考已更新。可沿用当前素材选择，重新登记并准备品牌首屏。'
      : state.preview ? previewNotice(state.preview)
      : state.plan ? '已恢复当前登记的分屏计划，可直接生成预览。'
      : existing ? '已加载当前登记。' : '选择旧页及可用素材后登记。');
    if (visibleMode(product, 'refresh')) {
      syncRefresh();
      showRefreshImages(product);
    }
  } catch (error) {
    setRefreshNotice(product, error.message, true);
    state.retry = () => loadRefresh(product);
    if (visibleMode(product, 'refresh')) syncRefresh();
  } finally {
    state.loading = false;
  }
  if (resumeProtected) {
    await runRefresh(product, 'protected', {url: resumeProtected.url});
  }
}

function invalidateRefreshPreview(state) {
  state.preview = undefined;
  state.delivered = undefined;
  state.unknown = undefined;
  state.previewScope = undefined;
  state.previewModeAtRun = undefined;
}

function resetProtectedPreview(state) {
  state.protectedTaskId = undefined;
  state.protectedPreview = undefined;
  state.protectedNotice = undefined;
  state.protectedUnresolved = false;
}

async function registerMaterials(product) {
  const state = product.refresh;
  const selection = Object.fromEntries([...new FormData(materialForm)].filter(([, value]) => value));
  state.material = selection;
  state.busy = true;
  state.retry = undefined;
  syncRefresh();
  setRefreshNotice(product, '正在登记素材…');
  let prepareProtected;
  try {
    state.registration = await refreshRequest(refreshProductPath(product) + '/registrations', selection);
    state.plan = undefined;
    state.selected = 'all';
    invalidateRefreshPreview(state);
    resetProtectedPreview(state);
    prepareProtected = state.registration.protected_scene_screen;
    setRefreshNotice(product, '登记完成，可以分析分屏。');
    if (visibleMode(product, 'refresh')) {
      activateMode('refresh', false);
    }
  } catch (error) {
    setRefreshNotice(product, error.message, true);
  } finally {
    state.busy = false;
    if (visibleMode(product, 'refresh')) syncRefresh();
  }
  if (prepareProtected) await startRefresh(product, 'plan');
}

async function runRefresh(product, step, operation) {
  const state = product.refresh;
  if (state.busy) return;
  state.busy = true;
  state.retry = undefined;
  state.unknown = undefined;
  if (visibleMode(product, 'refresh')) syncRefresh();
  try {
    let task = operation.url
      ? await refreshRequest(operation.url)
      : await refreshRequest(operation.endpoint, operation.payload, operation.key);
    operation.url = task.url;
    if (step === 'protected' && protectedSubmission()?.plan === state.plan.id) protectedSubmission(null);
    while (task.state === 'queued' || task.state === 'running') {
      const label = {plan: '分析分屏', preview: '生成预览', confirm: '导出确认稿', protected: '生成 Sunburst 首屏'}[step];
      const notice = `${label}${task.state === 'queued' ? '排队中…' : '进行中…'}`;
      if (step === 'protected') setProtectedNotice(product, notice);
      else setRefreshNotice(product, notice);
      await wait(650);
      task = await refreshRequest(operation.url);
    }
    if (step === 'protected') {
      finishProtectedPreview(product, task);
      if (visibleMode(product, 'refresh')) showRefreshImages(product);
      return;
    }
    if (task.state === 'unknown') {
      state.unknown = {step, operation};
      setRefreshNotice(product, task.error?.message || '任务结果待查询，请先查询原任务。', true);
      return;
    }
    if (task.state !== 'succeeded') {
      operation.url = undefined;
      operation.key = crypto.randomUUID();
      const detail = task.error?.message;
      throw new Error(detail?.includes('type.create') ? '文字图层生成失败，当前预览未完成。请重试。' : detail || '翻新任务失败。');
    }
    if (step === 'plan') {
      state.plan = task;
      state.selected = task.result.plan.protected_scene_screen ?? 'all';
      const model = task.result.call?.model;
      setRefreshNotice(product, protectedScope(state) ? '品牌首屏已准备，可生成 Sunburst 候选。'
        : model ? `公司模型分屏已完成 · ${model}。请选择预览范围。` : '本地分屏已完成。请选择预览范围。');
    } else if (step === 'preview') {
      state.preview = task;
      state.previewScope = operation.scope;
      state.previewModeAtRun = operation.payload.mode;
      if (Object.values(task.result.background_runs ?? {}).some(run => run.candidate_pending_visual_review)) {
        state.sceneCandidateTask = task;
      }
      setRefreshNotice(product, previewNotice(task));
    } else {
      state.delivered = task;
      setRefreshNotice(product, '版式已确认，可以下载或打开确认稿改字。');
    }
    if (visibleMode(product, 'refresh')) showRefreshImages(product);
  } catch (error) {
    const detail = error.message || '翻新任务暂时无法查询，请重试。';
    if (step === 'protected') {
      if (operation.url || !error.status || error.status === 408 || error.status >= 500) {
        state.protectedUnresolved = true;
        state.retry = () => runRefresh(product, step, operation);
      } else if (protectedSubmission()?.plan === state.plan.id) {
        protectedSubmission(null);
        state.protectedUnresolved = false;
      }
      setProtectedNotice(product, detail, true);
    } else {
      state.retry = () => runRefresh(product, step, operation);
      setRefreshNotice(product, detail, true);
    }
  } finally {
    state.busy = false;
    if (visibleMode(product, 'refresh')) syncRefresh();
  }
}

function startRefresh(product, step) {
  const state = product.refresh;
  if (state.busy || state.unknown || (step === 'plan' && !state.registration) ||
      (step === 'preview' && !state.plan) ||
      (step === 'confirm' && !canConfirmRefresh(product))) return;
  let endpoint;
  let payload;
  if (step === 'plan') {
    invalidateRefreshPreview(state);
    resetProtectedPreview(state);
    state.plan = undefined;
    endpoint = refreshProductPath(product) + '/plans';
    payload = {registration: state.registration.version,
      mode: state.registration.protected_scene_screen ? 'local_review' : refreshMode(product, 'plan')};
  } else if (step === 'preview') {
    const screens = selectedScreens(state);
    if (protectedScope(state)) {
      if (state.protectedUnresolved) return;
      protectedSku(product.code);
      const prior = state.protectedTaskId ?? null;
      resetProtectedPreview(state);
      setProtectedNotice(product, '正在提交 Sunburst 首屏任务…');
      showRefreshImages(product);
      const key = crypto.randomUUID();
      protectedSubmission({plan: state.plan.id, prior, key});
      return runRefresh(product, 'protected', protectedOperation(state, key));
    }
    const mode = refreshMode(product, 'preview');
    if (mode === 'company' && screens.filter(screen => screen.kind === 'visual').length !== 1) return;
    invalidateRefreshPreview(state);
    endpoint = '/api/refresh/previews';
    payload = {plan_job_id: state.plan.id, screens: screens.map(screen => screen.id), mode};
    const prior = state.sceneCandidateTask;
    if (mode === 'whole_review' && state.selected === 'all' && prior?.result.plan_job_id === state.plan.id) {
      payload.mode = 'reuse';
      payload.base_job_id = prior.id;
    }
  } else {
    endpoint = state.preview.url + '/confirm';
    payload = {revision: state.preview.result.revision};
  }
  if (step !== 'confirm') {
    byId('after-sheet').hidden = true;
    byId('after-empty').hidden = false;
  }
  return runRefresh(product, step, {endpoint, payload, key: crypto.randomUUID(), scope: state.selected});
}

async function queryRefresh(product) {
  const pending = product.refresh.unknown;
  if (!pending) return;
  try {
    await refreshRequest(pending.operation.url + '/query', {}, crypto.randomUUID());
    await runRefresh(product, pending.step, pending.operation);
  } catch (error) {
    setRefreshNotice(product, error.message, true);
  }
}

function openConfirmed(product) {
  const delivered = product.refresh.delivered;
  if (!delivered) return;
  const key = delivered.id;
  if (!product.edits.has(key)) {
    product.edits.set(key, {label: '已确认翻新稿', payload: {kind: 'refresh-edit', code: product.code,
      job_id: key, revision: delivered.result.revision}});
  }
  product.editSource = key;
  activateMode('edit');
}

modeEdit.addEventListener('click', () => {
  if (workMode !== 'edit') {
    protectedSku('');
    activateMode('edit');
  }
});
modeRefresh.addEventListener('click', () => { if (workMode !== 'refresh') activateMode('refresh'); });
byId('edit-form').addEventListener('submit', event => { event.preventDefault(); lookupWorkbench(); });
code.addEventListener('input', () => {
  ++requestSequence;
  lookup.disabled = false;
  byId('lookup-status').textContent = activeProduct && code.value !== activeProduct.code ? '点击查找切换商品。' : '';
});
editSource.addEventListener('change', () => {
  rememberView();
  activeProduct.editSource = editSource.value;
  activateMode('edit', false);
});
retry.addEventListener('click', () => {
  if (!draft && activeProduct && workMode === 'edit') createEdit(activeProduct, selectedEdit(activeProduct));
});
materialForm.addEventListener('change', () => {
  if (activeProduct) activeProduct.refresh.material = Object.fromEntries(new FormData(materialForm));
});
materialForm.addEventListener('submit', event => {
  event.preventDefault();
  if (activeProduct) registerMaterials(activeProduct);
});
byId('refresh-plan').addEventListener('click', () => { if (activeProduct) startRefresh(activeProduct, 'plan'); });
byId('refresh-preview').addEventListener('click', () => { if (activeProduct) startRefresh(activeProduct, 'preview'); });
refreshPlanMode.addEventListener('change', () => {
  if (activeProduct) activeProduct.refresh.planMode = refreshPlanMode.value;
});
refreshPreviewMode.addEventListener('change', () => {
  if (!activeProduct) return;
  const state = activeProduct.refresh;
  state.previewMode = refreshPreviewMode.value;
  if (state.preview && !state.delivered) {
    setRefreshNotice(activeProduct, canConfirmRefresh(activeProduct) ? previewNotice(state.preview) : '背景来源已改变，请生成新预览。');
  }
  syncRefresh();
});
refreshScope.addEventListener('change', () => {
  const state = activeProduct.refresh;
  state.selected = refreshScope.value;
  if (protectedScope(state)) {
    protectedSku(activeProduct.code);
    const {value, error} = state.protectedNotice ?? {value: '可生成 Sunburst 首屏候选以供审阅。'};
    setRefreshNotice(activeProduct, value, error);
  } else if (state.preview && !state.delivered) {
    setRefreshNotice(activeProduct, state.preview.result.review_only
      ? state.selected === state.previewScope ? previewNotice(state.preview) : '已切换对应旧屏，可生成该屏预览。'
      : canConfirmRefresh(activeProduct)
      ? previewNotice(state.preview) : '翻新设置已改变，生成新预览后可确认。');
  } else if (state.plan?.result.plan.protected_scene_screen) {
    setRefreshNotice(activeProduct, '已切换预览范围，可生成该范围预览。');
  }
  syncRefresh();
  if (state.plan?.result.plan.whole_review) showRefreshImages(activeProduct);
});
byId('refresh-query').addEventListener('click', () => { if (activeProduct) queryRefresh(activeProduct); });
byId('refresh-retry').addEventListener('click', () => { activeProduct?.refresh.retry?.(); });
openEdited.addEventListener('click', () => { if (activeProduct) openConfirmed(activeProduct); });
confirm.addEventListener('click', () => {
  if (activeProduct && workMode === 'refresh') startRefresh(activeProduct, 'confirm');
});

api('/api/workbench/products').then(({products}) => {
  const list = byId('codes');
  for (const product of products) list.append(new Option(product.name, product.code));
}).catch(() => {});
const rememberedProtectedSku = protectedSku();
if (/^\d{8,16}$/.test(rememberedProtectedSku ?? '')) {
  code.value = rememberedProtectedSku;
  workMode = 'refresh';
}
lookupWorkbench();
