const byId = (id) => document.getElementById(id);
let plan;
let options = {};
let candidate;
let base;
let busy = false;

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || `请求失败 (${response.status})`);
  return result;
}

function status(message, isError = false) {
  byId('status').textContent = message;
  byId('status').classList.toggle('error', isError);
}

function lock(value) {
  busy = value;
  document.querySelectorAll('input, button, select, textarea').forEach((element) => {
    element.disabled = value;
  });
}

function invalidate() {
  if (candidate) status('草图参数已修改，请重新生成预览。');
  candidate = undefined;
  byId('confirm').hidden = true;
  byId('downloads').hidden = true;
  byId('preview').hidden = true;
  byId('full-preview').hidden = true;
  byId('empty').hidden = false;
}

function field(parent, name, element) {
  const label = document.createElement('label');
  label.append(name, element);
  parent.append(label);
}

function editFields() {
  const screen = plan.screens.find((entry) => entry.id === byId('edit-screen').value);
  const region = byId('edit-fields');
  region.replaceChildren();
  const option = options[screen.id] ||= {};
  if (screen.kind === 'visual') {
    const variant = document.createElement('select');
    variant.add(new Option('暖色', 'warm'));
    variant.add(new Option('柔光', 'soft'));
    variant.value = option.variant || 'warm';
    variant.addEventListener('change', () => { option.variant = variant.value; invalidate(); });
    field(region, '本地场景', variant);
    const offsets = document.createElement('div');
    offsets.className = 'offsets';
    const axes = [['text_x', '文字横移'], ['text_y', '文字纵移']];
    if (screen.use_bottle !== false) axes.push(['product_x', '瓶图横移'], ['product_y', '瓶图纵移']);
    for (const [key, label] of axes) {
      const input = document.createElement('input');
      Object.assign(input, {type: 'number', min: -36, max: 36, step: 1, value: option.offsets?.[key] || 0});
      input.addEventListener('input', () => {
        (option.offsets ||= {})[key] = input.value === '' ? 0 : Number(input.value);
        invalidate();
      });
      field(offsets, `${label}（px）`, input);
    }
    region.append(offsets);
  }
  const labels = {kicker: '引题', title: '标题', subtitle: '副标题', body: '正文', footnote: '脚注'};
  for (const item of screen.items) {
    const input = document.createElement('textarea');
    input.maxLength = 160;
    input.value = option.text?.[item.key] ?? item.text;
    input.addEventListener('input', () => {
      (option.text ||= {})[item.key] = input.value;
      invalidate();
    });
    field(region, item.label || labels[item.role] || item.key, input);
  }
}

function showPlan() {
  byId('product').textContent = plan.name;
  byId('old-page').href = `/api/products/${plan.code}/old-page`;
  const layouts = new Map((plan.layout_templates ?? []).map((template) => [template.screen_id, template]));
  const storeAvailable = layouts.has('hero') && layouts.has('selling');
  byId('layout-choice').hidden = !storeAvailable;
  const shopMode = storeAvailable && byId('layout-mode').value === 'store';
  byId('screens').replaceChildren();
  byId('edit-screen').replaceChildren();
  const visibleScreens = shopMode ? plan.screens.filter((screen) => layouts.has(screen.id)) : plan.screens;
  for (const screen of visibleScreens) {
    const input = document.createElement('input');
    Object.assign(input, {type: 'checkbox', value: screen.id, checked: shopMode || ['hero', 'info'].includes(screen.id)});
    input.addEventListener('change', invalidate);
    const label = document.createElement('label');
    label.append(input, screen.name);
    byId('screens').append(label);
    byId('edit-screen').add(new Option(screen.name, screen.id));
  }
  for (const [screen, layout] of layouts) {
    const option = options[screen] ||= {};
    if (shopMode) option.template = layout.id;
    else delete option.template;
  }
  byId('approvals').replaceChildren(...plan.approvals.map((item) => {
    const line = document.createElement('li');
    line.textContent = `${item.status === 'approved' ? '已核准' : '待核准'} · ${item.note}`;
    return line;
  }));
  editFields();
  byId('work').hidden = false;
}

async function lookup() {
  if (busy) return;
  const code = byId('code').value.trim();
  lock(true);
  byId('work').hidden = true;
  try {
    plan = await api(`/api/products/${encodeURIComponent(code)}`);
    options = {};
    base = undefined;
    byId('layout-mode').value = 'store';
    invalidate();
    showPlan();
    status('已读取旧页和模块计划。');
  } catch (error) {
    status(error.message, true);
  } finally {
    lock(false);
  }
}

byId('lookup').addEventListener('submit', (event) => { event.preventDefault(); lookup(); });
byId('layout-mode').addEventListener('change', () => { invalidate(); showPlan(); });
byId('edit-screen').addEventListener('change', editFields);
byId('preview-button').addEventListener('click', async () => {
  if (busy) return;
  const screens = [...byId('screens').querySelectorAll(':checked')].map((input) => input.value);
  if (!screens.length) { status('至少选择一屏。', true); return; }
  invalidate();
  lock(true);
  status('正在合成草图…');
  try {
    candidate = await api('/api/previews', {
      code: plan.code, screens, options: Object.fromEntries(screens.map((id) => [id, options[id] || {}])),
      ...(base ? {base_job_id: base} : {}),
    });
    base = candidate.job_id;
    byId('empty').hidden = true;
    byId('preview').src = candidate.preview;
    byId('preview').hidden = false;
    byId('full-preview').href = candidate.preview;
    byId('full-preview').hidden = false;
    byId('confirm').hidden = false;
    status('草图已生成。确认仅用于本次内部排版。');
  } catch (error) {
    status(error.message, true);
  } finally {
    lock(false);
  }
});

byId('confirm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy || !candidate) return;
  lock(true);
  status('正在生成并回读分层 PSD…');
  try {
    const result = await api(`/api/jobs/${candidate.job_id}/confirm`, {
      revision: candidate.revision, reviewer: byId('reviewer').value.trim(),
    });
    const downloads = byId('downloads');
    const labels = {'detail.psd': '下载 PSD', 'detail.png': '下载 PNG', 'checks.json': '导出检查', 'confirmation.json': '确认记录'};
    downloads.replaceChildren(...Object.entries(result.files).map(([name, path]) => {
      const link = document.createElement('a');
      Object.assign(link, {href: path, download: name, textContent: labels[name]});
      return link;
    }));
    downloads.hidden = false;
    status('已导出内部样稿。成稿与确认草图一致。');
  } catch (error) {
    status(error.message, true);
  } finally {
    lock(false);
  }
});

lookup();
