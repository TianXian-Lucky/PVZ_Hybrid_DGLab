'use strict';

const $ = (id) => document.getElementById(id);
const node = (tag, text, className) => {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = String(text);
  if (className) el.className = className;
  return el;
};
const svgNS = 'http://www.w3.org/2000/svg';
const svgNode = (tag, attrs) => {
  const el = document.createElementNS(svgNS, tag);
  Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, String(value)));
  return el;
};

const pageLabels = {plugin:'反馈设置',dashboard:'运行面板',presets:'波形预设',device:'设备连接',game:'游戏指南',logs:'运行日志'};
const pageIds = new Set(Object.keys(pageLabels));
const here = new URL(location.href);
const fragment = new URLSearchParams(here.hash.slice(1));
const launchSession = here.searchParams.get('session') || fragment.get('session');
const desktopFlag = here.searchParams.get('desktop') ?? fragment.get('desktop');
const desktopAgent = /(?:Electron\/|PVZHybridDesktop\/)/i.test(navigator.userAgent);
let desktopWindow = desktopAgent || desktopFlag === '1';
let session = launchSession || '';
try {
  if (launchSession) {
    sessionStorage.setItem('pvz-standalone-session', launchSession);
    sessionStorage.setItem('pvz-standalone-desktop', desktopWindow ? '1' : '0');
    here.searchParams.delete('session');
    here.searchParams.delete('desktop');
    if (fragment.has('session') || fragment.has('desktop')) here.hash = '#plugin';
    history.replaceState(null, '', here.href);
  } else {
    session = sessionStorage.getItem('pvz-standalone-session') || '';
    if (desktopFlag !== null) sessionStorage.setItem('pvz-standalone-desktop', desktopWindow ? '1' : '0');
    else desktopWindow ||= sessionStorage.getItem('pvz-standalone-desktop') === '1';
  }
} catch (_) { /* The current URL still supplies authorization when storage is unavailable. */ }
document.documentElement.classList.toggle('desktop-window', desktopWindow);

const channels = [
  {value:'both', label:'A + B 通道'},
  {value:'a', label:'A 通道'},
  {value:'b', label:'B 通道'},
];
const deviceProtocol = {v3:'郊狼 3.0', v4:'DG-LAB App 4.x · 郊狼 3.0'};
const outputReady = (s) => {
  const device = s?.device;
  if (!device?.connected || !(device.ready ?? true) || s.config?.show_damage === false) return false;
  const channel = s.config?.channel, available = device.channels;
  return Boolean(!available || (channel === 'both' ? available.a && available.b : available[channel]));
};
// 通道没就绪时，告诉用户具体是哪一项不满足、该去手机上做什么。
// 后端要求「当前强度 0 < level ≤ 上限」才算可用，所以强度为 0 是最常见的原因。
const channelHint = (device) => {
  const channel = state?.config?.channel;
  const levels = device?.levels || {}, limits = device?.limits || {};
  const available = device?.channels || {};
  const label = {both:'A+B', a:'A', b:'B'}[channel] || '所选';
  const names = channel === 'both' ? ['a','b'] : [channel === 'b' ? 'b' : 'a'];
  const blocked = names.filter((c) => !available[c]);
  if (!blocked.length) return '设备反馈需单独授权';
  const zero = [], unknown = [], other = [];
  for (const c of blocked) {
    const name = c === 'b' ? 'B 通道' : 'A 通道';
    const level = levels[c], limit = limits[c];
    if (limit === null || limit === undefined) unknown.push(name);
    else if (!(typeof level === 'number' && level > 0)) zero.push(name);
    else other.push(name);
  }
  const parts = [];
  if (zero.length) parts.push(`把${zero.join('、')}强度调到 1 以上`);
  if (unknown.length) parts.push(`确认${unknown.join('、')}的上限`);
  if (other.length) parts.push(`检查${other.join('、')}是否静音或过热`);
  return `请先在手机 App ${parts.join('；')}（当前输出 ${label}）`;
};
const badges = {ok:'通过', pending:'等待', warn:'注意', fail:'未通过'};
const tabSequence = ['模式与输出','强度反馈','触发事件','游戏 Mod'];
const renderCache = new Map();
let state = null;
let tab = 0;
let currentModal = '';
let toastTimer;
let pollTimer;
let pending = false;
let reachable = false;
let stopping = false;
let quitting = false;
let exitRequested = false;
let previousOutput = false;
let presence = null;
let presenceTimer = null;
let presenceFailures = 0;
let pageActive = true;
let saveMessage = '已自动保存';
let selectedPairIp = 'auto';
let selectedPairModel = null;

function cached(key, value, render) {
  const signature = JSON.stringify(value);
  if (renderCache.get(key) !== signature) {
    renderCache.set(key, signature);
    render(value);
  }
}
function tell(message, ms = 4400) {
  const toast = $('toast');
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, ms);
}
function reportNetwork(message) {
  const bar = $('offline');
  bar.textContent = message || '';
  bar.hidden = !message;
}
function localizedError(err) {
  return String(err && err.message || err || '未知错误');
}
function statusFields() {
  const raw = state && state.status || {};
  return raw.fields && typeof raw.fields === 'object' ? raw.fields : raw;
}
function schema() {
  const fields = state && state.manifest && state.manifest.config_schema;
  return Array.isArray(fields) ? fields : [];
}
function valueOf(field) {
  const config = state && state.config || {};
  return Object.hasOwn(config, field.key) ? config[field.key] : field.default;
}
function view(name, setHash = true) {
  if (!pageIds.has(name)) name = 'plugin';
  $('page-location').textContent = pageLabels[name];
  if (setHash && location.hash !== '#' + name) {
    if (fragment.has('session') && !state) history.replaceState(null, '', location.href);
    else location.hash = name;
  }
  pageIds.forEach((id) => { $('page-' + id).hidden = id !== name; });
  document.querySelectorAll('.nav-tile[data-page],.nav-link[data-page]').forEach((el) => {
    const selected = el.dataset.page === name;
    el.classList.toggle('is-current', selected);
    if (selected) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
}
function chooseTab(index, focus = false) {
  const count = schema().length;
  if (index < 0 || index >= count) return;
  tab = index;
  renderTabs();
  if (focus) $('config-tab-' + index)?.focus();
}
function assignOptions(select, options, value) {
  const selected = value == null ? '' : String(value);
  const items = options.slice();
  if (selected && !items.some((opt) => String(opt.value) === selected)) {
    items.push({value:selected, label:selected + '（当前配置）'});
  }
  select.replaceChildren(...items.map((opt) => {
    const el = node('option', opt.label);
    el.value = String(opt.value);
    return el;
  }));
  select.value = selected;
}
function makeField(field) {
  const wrapper = node('div', undefined, 'field');
  const label = node('label', field.label || field.key);
  const id = 'setting-' + field.key;
  label.htmlFor = id;
  if (field.description) label.title = field.description;
  wrapper.append(label);
  let control;
  const value = valueOf(field);
  if (field.type === 'bool') {
    const switchWrap = node('div', undefined, 'switch-wrap');
    control = node('input', undefined, 'switch');
    control.type = 'checkbox';
    control.setAttribute('role', 'switch');
    control.checked = value === true;
    switchWrap.append(control); wrapper.append(switchWrap);
  } else if (field.type === 'select' || field.type === 'preset' || field.type === 'channel') {
    control = node('select');
    const options = field.type === 'preset' ?
      (state.presets || []).map((name) => ({label:name,value:name})) :
      field.type === 'channel' ? channels : (field.options || []);
    assignOptions(control, options, value);
    wrapper.append(control);
  } else if (field.type === 'number' || field.type === 'duration') {
    control = node('input');
    control.type = 'number';
    control.min = field.min ?? (field.type === 'duration' ? 0.1 : -100);
    control.max = field.max ?? (field.type === 'duration' ? 300 : 100);
    control.step = field.step ?? (field.type === 'duration' ? 0.1 : 1);
    control.value = value;
    wrapper.append(control);
  } else {
    return wrapper;
  }
  control.id = id;
  control.dataset.config = field.key;
  if (field.description) control.title = field.description;
  control.addEventListener('change', () => {
    let next = control.value;
    if (field.type === 'bool') next = control.checked;
    else if (control.type === 'number') {
      if (control.value === '' || !control.checkValidity()) {
        control.value = valueOf(field);
        tell('请输入允许范围内的数值。');
        return;
      }
      next = control.valueAsNumber;
    }
    if (next !== valueOf(field)) act('config', {key:field.key,value:next});
  });
  return wrapper;
}
function renderTabs() {
  const sections = schema();
  if (!sections.length) {
    if (!renderCache.has('no-schema')) {
      const empty = node('p', '等待本机服务加载插件配置。', 'muted');
      $('tab-content').replaceChildren(empty);
      renderCache.set('no-schema', true);
    }
    $('inline-preview').hidden = true;
    return;
  }
  tab = Math.min(tab, sections.length - 1);
  cached('tabs', [sections.map((section) => section.section), tab], ([names, active]) => {
    const tabs = $('tabs');
    const previous = document.activeElement?.id;
    tabs.replaceChildren(...names.map((name, index) => {
      const button = node('button', name || tabSequence[index] || '设置');
      button.type = 'button';
      button.id = 'config-tab-' + index;
      button.setAttribute('role','tab');
      button.setAttribute('aria-controls','config-panel');
      button.setAttribute('aria-selected',String(index === active));
      button.tabIndex = index === active ? 0 : -1;
      button.addEventListener('click', () => chooseTab(index, true));
      button.addEventListener('keydown', (ev) => {
        const deltas = {ArrowLeft:-1,ArrowRight:1};
        if (Object.hasOwn(deltas,ev.key)) {
          ev.preventDefault(); chooseTab((index + deltas[ev.key] + names.length) % names.length,true);
        } else if (ev.key === 'Home' || ev.key === 'End') {
          ev.preventDefault(); chooseTab(ev.key === 'Home' ? 0 : names.length - 1,true);
        }
      });
      return button;
    }));
    if (previous && previous.startsWith('config-tab-')) $('config-tab-' + active)?.focus();
  });
  const section = sections[tab];
  const descriptors = section.fields || [];
  // Do not replace live form controls on a state poll: syncing values separately preserves drafts and focus.
  cached('form-layout', [tab,section,state.presets], () => {
    const previous = document.activeElement?.id;
    const panel = node('section',undefined,'tab-panel');
    panel.id = 'config-panel';
    panel.setAttribute('role','tabpanel');
    panel.setAttribute('aria-labelledby','config-tab-' + tab);
    const fields = node('div',undefined,'fields');
    fields.append(...descriptors.map(makeField));
    panel.append(node('h2',section.section), fields);
    if (descriptors.some((field) => field.key === 'strength_enable')) {
      panel.append(node('p','独立版按波形幅度叠加强度，不改手机手动档位；满幅处不会再增强。','settings-note muted'));
      const reset = node('button','重置插件累积','outline reset-baseline');
      reset.id = 'reset-baseline'; reset.type = 'button'; reset.dataset.action = 'reset_baseline'; reset.hidden = true;
      panel.append(reset);
    }
    $('tab-content').replaceChildren(panel);
    if (previous && previous.startsWith('setting-')) $(previous)?.focus();
  });
  for (const field of descriptors) {
    const control = $('setting-' + field.key);
    if (!control) continue;
    control.disabled = pending || !reachable || quitting;
    // Preserving edited numeric text while a field is focused must not preserve rejected writes.
    if (control === document.activeElement && control.type === 'number' && !pending) continue;
    const value = valueOf(field);
    if (control.type === 'checkbox') control.checked = value === true;
    else if (field.type === 'preset' && !Array.from(control.options).some((item) => item.value === String(value))) {
      assignOptions(control,(state.presets || []).map((name) => ({label:name,value:name})),value);
    } else if (String(control.value) !== String(value)) control.value = String(value);
  }
  const reset = $('reset-baseline');
  if (reset) {
    const level = Number(state.strength_baseline);
    reset.hidden = !(level > 0);
    reset.disabled = pending || !reachable || quitting;
    reset.title = level > 0 ? '当前插件累积：' + level + '%。不会修改手机手动档位。' : '';
  }
  $('inline-preview').hidden = !descriptors.some((field) => field.key === 'preset');
}
function graph(target, values, label) {
  const data = Array.isArray(values) && values.length > 1
    ? values.map(Number).filter(Number.isFinite).map((x) => Math.max(0,Math.min(100,x))).slice(0,2048) : [];
  if (data.length < 2) {
    target.replaceChildren(node('p','没有可显示的波形预览。','no-graph'));
    return;
  }
  const left = 35, right = 695, top = 8, bottom = 103;
  const svg = svgNode('svg',{viewBox:'0 0 702 115',role:'img','aria-label':label});
  const id = 'fill-' + target.id;
  const defs = svgNode('defs',{});
  const gradient = svgNode('linearGradient',{id,x1:0,x2:0,y1:0,y2:1});
  gradient.append(svgNode('stop',{offset:'0%','stop-color':'#d5578d','stop-opacity':.22}),
    svgNode('stop',{offset:'100%','stop-color':'#d5578d','stop-opacity':.015}));
  defs.append(gradient); svg.append(defs);
  [100,75,50,25,0].forEach((level) => {
    const y = bottom - level * (bottom - top) / 100;
    const line = svgNode('line',{x1:left,x2:right,y1:y,y2:y,stroke:'#e9dce7','stroke-width':1});
    const text = svgNode('text',{x:0,y:y+4,fill:'#6f626f','font-size':13});
    text.textContent = String(level);
    svg.append(line,text);
  });
  const points = data.map((x,index) => [
    left + index * (right-left) / (data.length-1),
    bottom - x * (bottom-top) / 100,
  ].map((n) => n.toFixed(2)));
  const p = points.map((xy) => xy.join(',')).join(' ');
  svg.append(svgNode('polygon',{points:left+','+bottom+' '+p+' '+right+','+bottom,fill:'url(#'+id+')'}),
    svgNode('polyline',{points:p,fill:'none',stroke:'#a93764','stroke-width':2.4,'stroke-linejoin':'round','stroke-linecap':'round'}),
    svgNode('circle',{cx:right,cy:points.at(-1)[1],r:3.2,fill:'#a93764'}));
  target.replaceChildren(svg);
}
function renderCards(target, values) {
  const cards = Array.isArray(values) ? values.slice(0,24) : [];
  if (!cards.length) {
    target.replaceChildren(node('p','暂无新的受伤提示。进入游戏触发一次实际伤害后再查看。','muted'));
    return;
  }
  target.replaceChildren(...cards.map((card) => {
    const el = node('article',undefined,'event-card');
    if (typeof card === 'string') { el.append(node('p',card)); return el; }
    if (!card || typeof card !== 'object') { el.append(node('p','受伤事件')); return el; }
    const data = card.data || card.fields || card;
    el.append(node('h3',data.title || data.label || data.name || '受伤事件'));
    const details = data.body || data.detail || data.message || data.text || (data.label ? data.name : null);
    if (details) el.append(node('p',details));
    return el;
  }));
}
function renderDashboard(data) {
  const {status,cards} = data;
  $('dashboard-status').textContent = status.display_status || (state.enabled ? '插件已启动，等待运行状态。' : '插件尚未启用。');
  const labels = [
    ['received','收到事件'],['eligible','命中筛选'],['shown','显示提示'],['session_requests','反馈请求'],
  ];
  $('stats').replaceChildren(...labels.map(([key,name]) => {
    const stat = node('div',undefined,'stat');
    stat.append(node('small',name),node('strong',Number.isFinite(status[key]) ? status[key] : '—'));
    return stat;
  }));
  renderCards($('dashboard-cards'),cards);
}
function renderPresets() {
  const names = Array.isArray(state.presets) ? state.presets : [];
  const selected = state.config?.preset;
  cached('preset-buttons',[names,selected],() => {
    const parent = $('preset-list');
    if (!names.length) {
      parent.replaceChildren(node('p','本机暂无可用预设。','muted'));
      return;
    }
    const oldText = parent.contains(document.activeElement) ? document.activeElement.textContent : null;
    parent.replaceChildren(...names.map((name) => {
      const button = node('button',name,'preset-entry');
      button.type = 'button';
      button.setAttribute('aria-pressed',String(name === selected));
      button.addEventListener('click',() => {
        if (name !== selected) act('config',{key:'preset',value:name});
      });
      return button;
    }));
    if (oldText) Array.from(parent.children).find((child) => child.textContent === oldText)?.focus();
  });
  $('preset-list').querySelectorAll('button').forEach((button) => { button.disabled = pending || !reachable || quitting; });
  $('import-presets').disabled = pending || !reachable || quitting;
  $('preset-title').textContent = selected ? '强度预览 · '+selected : '强度预览';
  cached('preview',[state.preview,selected],([values,name]) => {
    const title = (name || '当前预设') + ' 的强度曲线（范围 0 至 100）';
    graph($('graph-inline'),values,title); graph($('graph-library'),values,title);
  });
}
function isIpv4(value) {
  return typeof value === 'string' && /^\d{1,3}(\.\d{1,3}){3}$/.test(value) &&
    value.split('.').every((part) => Number(part) <= 255 && String(Number(part)) === part);
}
function renderDevice(device, pair) {
  const connected = reachable && device.connected === true;
  const pairActive = reachable && pair.active === true;
  const modelKey = String(device.model || pair.model || 'v3');
  const deviceReady = connected && (device.ready ?? connected) === true;
  const model = deviceProtocol[modelKey] || modelKey;
  const channel = state.config?.channel;
  $('rail-model').textContent = model;
  $('mini-model').textContent = model;
  $('mini-status').textContent = !reachable ? '状态未确认' : deviceReady ? '已连接' : connected ? 'App 已配对' : (pairActive ? '等待扫码' : '未连接');
  $('mini-indicator').classList.toggle('online',reachable && connected);
  $('rail-connection').textContent = !reachable ? '无法确认设备状态' : deviceReady ? '设备已连接' : connected ? 'App 已配对，等待设备就绪' : (pairActive ? '等待扫码连接' : '等待设备连接');
  $('rail-channel').textContent = channel === 'a' ? '仅 A 通道' : channel === 'b' ? '仅 B 通道' : channel === 'both' ? 'A + B 通道' : '未选择通道';
  const ips = Array.isArray(pair.ips) ? Array.from(new Set(pair.ips.filter(isIpv4))) : [];
  const manual = selectedPairIp !== 'auto';
  const missingChoice = manual && !ips.includes(selectedPairIp);
  const ipOptions = [{value:'auto',label:'自动选择'}, ...ips.map((ip) => ({value:ip,label:ip}))];
  if (missingChoice) ipOptions.push({value:selectedPairIp,label:selectedPairIp + '（当前未检测到）'});
  cached('pair-addresses',[ipOptions, selectedPairIp], () => assignOptions($('pair-ip'),ipOptions,selectedPairIp));
  const networkNote = !ips.length
    ? '没有检测到局域网 IPv4。请将电脑与手机接入同一局域网，再重新生成配对码。'
    : missingChoice ? '所选地址当前不可用。请改用自动选择，或选择列表中的其他地址。'
    : pairActive && !connected ? '现有配对码不会随选项改变；改选后请重新生成。'
    : manual ? '生成时会使用手动选中的地址。请确认手机与该地址位于同一局域网。'
    : '生成时会自动使用可用的局域网地址，也可手动选择。';
  if ($('pair-ip-note').textContent !== networkNote) $('pair-ip-note').textContent = networkNote;
  const boundIp = (pairActive || connected) && isIpv4(pair.ip) ? pair.ip : '';
  const bound = $('pair-bound-ip');
  bound.hidden = !boundIp;
  bound.textContent = boundIp ? '本次配对地址：' + boundIp : '';
  $('pair-model').value = selectedPairModel || pair.model || (Object.hasOwn(deviceProtocol,modelKey) ? modelKey : 'v3');
  $('pair-model').disabled = pending || !reachable || quitting || connected;
  $('pair-ip').disabled = pending || !reachable || quitting || connected;
  $('pair-start').disabled = pending || !reachable || quitting || connected;
  $('pair-start').textContent = pairActive ? '重新生成配对码' : '生成配对码';
  $('pair-stop').disabled = pending || !reachable || quitting || !(connected || pairActive);
  $('pair-stop').textContent = connected ? '结束连接' : '取消配对';
  $('pair-selection-note').textContent = $('pair-model').value === 'v4'
    ? '4.x 指 DG-LAB App 的 Socket 协议，支持选择 App 中已连接的郊狼 3.0。' : '通过 DG-LAB App 3.x 的 Socket 功能连接郊狼 3.0。';
  const detailText = !reachable ? '连接本机服务失败，设备状态不可用。' : device.detail || (connected
    ? '设备已连接；设备反馈由本次会话单独授权。'
    : pairActive ? '请使用郊狼 App 扫描本次配对码。' : '未配对');
  if ($('pair-state').textContent !== detailText) $('pair-state').textContent = detailText;
  const qr = $('pair-qr');
  const data = typeof pair.qr === 'string' && /^data:image\/(?:png|svg\+xml|webp);(?:base64,|charset=utf-8,|utf8,|,)/i.test(pair.qr) ? pair.qr : null;
  const hasQr = pairActive && !connected && Boolean(data);
  if (hasQr) {
    if (qr.getAttribute('src') !== data) qr.src = data;
    qr.hidden = false;
  } else {
    if (qr.hasAttribute('src')) qr.removeAttribute('src');
    qr.hidden = true;
  }
  const check = reachable && hasQr && boundIp && Number.isInteger(pair.port) && pair.port > 0 && pair.port <= 65535;
  $('pair-check').hidden = !check;
  $('pair-check-url').textContent = check ? 'http://' + boundIp + ':' + pair.port + '/check' : '';
  const diagnostic = reachable && typeof pair.diagnostic === 'string' ? pair.diagnostic.trim() : '';
  const diagnosticLine = $('pair-diagnostic');
  const detail = diagnostic ? '连接诊断：' + diagnostic : '';
  diagnosticLine.hidden = !detail;
  if (diagnosticLine.textContent !== detail) diagnosticLine.textContent = detail;
  const slots = connected && Array.isArray(device.slots) ? device.slots.filter((slot) => slot && typeof slot.id === 'string') : [];
  $('pair-result').hidden = !hasQr && !slots.length;
  $('pair-result-title').textContent = hasQr ? '扫描配对码' : '目标设备';
  $('device-target').hidden = !slots.length;
  $('device-slot').disabled = pending || !reachable || quitting || !slots.some((slot) => slot.connected);
  cached('device-slots',[slots,device.slot], () => {
    const select = $('device-slot');
    const empty = node('option','选择目标设备'); empty.value = ''; empty.disabled = true;
    select.replaceChildren(empty,...slots.map((slot) => {
      const option = node('option',(slot.name || '未命名设备') + (slot.connected ? '' : '（未连接）'));
      option.value = slot.id;
      option.disabled = slot.connected !== true;
      return option;
    }));
  });
  $('device-slot').value = slots.some((slot) => slot.id === device.slot) ? device.slot : '';
  cached('meters',[connected,device.limits,device.levels], () => {
    const meters = $('device-meters');
    meters.hidden = !connected;
    if (!connected) { meters.replaceChildren(); return; }
    const letters = ['a','b'];
    meters.replaceChildren(...letters.map((letter) => {
      const name = letter.toUpperCase() + ' 通道';
      const limit = device.limits && device.limits[letter];
      const level = device.levels && device.levels[letter];
      const row = node('div',undefined,'meter-row');
      const scale = node('meter');
      scale.setAttribute('aria-label',name);
      scale.min = 0; scale.max = Number.isFinite(limit) && limit > 0 ? limit : 1;
      scale.value = Number.isFinite(level) ? Math.min(scale.max,Math.max(0,level)) : 0;
      row.append(node('span',name),scale,node('span',(Number.isFinite(level)?level:'—')+' / '+(Number.isFinite(limit)?limit:'—'),'muted'));
      return row;
    }));
  });
}

function renderModal() {
  if (!currentModal) return;
  const content = $('modal-body');
  if (currentModal === 'check') {
    $('modal-title').textContent = '启动检查';
    const details = statusFields().startup_check;
    const steps = details && Array.isArray(details.steps) ? details.steps : [];
    cached('modal-check',steps,() => {
      if (!steps.length) {
        content.replaceChildren(node('p',state?.enabled
          ? '本机尚未收到启动检查结果。' : '启用插件后会显示来自实际运行的启动检查。','muted'));
        return;
      }
      const items = node('div',undefined,'check-list');
      steps.forEach((step) => {
        const row = node('article',undefined,'check-item');
        const stateName = Object.hasOwn(badges,step.state) ? step.state : 'pending';
        const copy = node('div');
        copy.append(node('h3',step.title || '检查项'),node('p',step.detail || '—'));
        if (step.hint) copy.append(node('small',step.hint));
        row.append(node('span',badges[stateName],'check-badge '+stateName),copy);
        items.append(row);
      });
      content.replaceChildren(items);
    });
  } else if (currentModal === 'cards') {
    $('modal-title').textContent = '实时受伤提示';
    cached('modal-cards',state?.cards || [], (cards) => renderCards(content,cards));
  } else if (currentModal === 'about') {
    $('modal-title').textContent = '关于本工具';
    cached('modal-about',[state?.manifest, reachable, pending], () => {
      const manifest = state?.manifest || {};
      const description = node('div',undefined,'about-text');
      description.append(node('p',manifest.name || '杂交版 · 植物与僵尸受伤反馈'));
      const credits = [manifest.version && ('插件版本 '+manifest.version), manifest.author && ('原作作者：'+manifest.author)].filter(Boolean);
      if (credits.length) description.append(node('p',credits.join(' · '),'muted'));
      description.append(node('p',desktopWindow ? '本机独立版 · 无需打开浏览器。图形预览不会驱动设备；需要结束会话时可直接停止反馈。' : '这是本机运行的独立版。图形预览不会驱动设备；需要结束会话时可直接停止反馈。','muted'));
      const buttons = node('div',undefined,'button-row');
      const exit = node('button','退出应用','outline danger');
      exit.type = 'button';
      exit.dataset.action = 'quit';
      exit.disabled = !reachable || pending || quitting;
      buttons.append(exit);
      content.replaceChildren(description,buttons);
    });
  }
}
function openModal(name) {
  currentModal = name;
  for (const key of ['modal-check','modal-about','modal-cards']) renderCache.delete(key);
  renderModal();
  if (!$('modal').open) $('modal').showModal();
  $('modal-body').scrollTop = 0;
}
function render() {
  if (!state) { renderTabs(); return; }
  const status = statusFields(), device = state.device || {}, pair = state.pair || {};
  const enabled = state.enabled === true;
  const arm = state.output_enabled === true;
  renderTabs();
  $('plugin-title').textContent = state.manifest?.name || '杂交版 · 植物与僵尸受伤反馈（V3）';
  let text = enabled ? '仅观察游戏事件' : '本机反馈尚未启用';
  if (enabled && arm) text = status.armed ? '设备反馈已就绪' : '等待反馈条件';
  if (enabled && !arm && device.connected) {
    // 区分「没授权」和「通道还没就绪」——两者原因完全不同，
    // 后者需要用户去手机 App 把通道强度调到 1 以上，光点按钮没用。
    text = outputReady(state)
      ? '设备反馈需单独授权'
      : channelHint(device);
  }
  if (!enabled && outputReady(state)) text = '启用会允许本次设备反馈';
  $('run-status').textContent = reachable ? text : '与本机服务断开 · 运行状态未确认';
  const badge = $('active-summary');
  badge.textContent = !reachable ? '本机连接中断' : enabled ? '插件运行中' : '插件未启用';
  badge.classList.toggle('is-running', enabled && reachable);
  const button = $('toggle-plugin');
  button.disabled = pending || !reachable || quitting;
  button.textContent = enabled ? '停用插件' : '启用插件';
  button.classList.toggle('primary',!enabled);
  button.classList.toggle('outline',enabled);
  const armButton = $('arm-output');
  const showArm = enabled && device.connected === true && !arm;
  const ready = showArm && outputReady(state);
  armButton.hidden = !showArm;
  armButton.disabled = !ready || pending || !reachable || quitting;
  armButton.title = ready
    ? '仅授权当前连接与通道的本次设备反馈'
    : state.config?.show_damage === false
      ? '需要先打开触发事件总开关'
      : channelHint(device);
  const quitButton = document.querySelector('.quit-control');
  if (quitButton) quitButton.disabled = !reachable || pending || quitting;
  document.querySelectorAll('[data-action="stop"]').forEach((b) => {
    b.disabled = !(enabled || arm || device.connected) || !reachable || pending || quitting;
  });
  $('save-status').textContent = reachable ? saveMessage : '连接中断，未保存的更改不会生效';
  if (typeof state.error === 'string' && state.error.trim() && reachable) reportNetwork(state.error);
  else if (reachable) reportNetwork('');
  renderDevice(device,pair);
  renderPresets();
  cached('dashboard',[status,state.cards || [],enabled],([fields,cards]) => renderDashboard({status:fields,cards}));
  cached('logs',state.logs || [],(logs) => {
    const lines = Array.isArray(logs) ? logs.map(String) : [];
    $('logs-list').textContent = lines.join('\n') || '暂无运行日志。';
    $('copy-logs').disabled = lines.length === 0;
  });
  renderModal();
}
function connectPresence() {
  if (!session || !state || quitting || exitRequested || !pageActive || presence || presenceTimer !== null) return;
  const retry = () => {
    if (quitting || exitRequested || !pageActive || presenceTimer !== null) return;
    // A native browser socket answers websocket pings even in a background tab.
    // Back off on repeated failures; the existing authenticated HTTP poll still works.
    const delay = Math.min(12000, 900 * (2 ** Math.min(5, presenceFailures++)));
    presenceTimer = setTimeout(() => { presenceTimer = null; connectPresence(); }, delay);
  };
  let socket;
  try {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(scheme + '//' + location.host + '/api/presence', [
      'pvz-presence-v1', 'session.' + session,
    ]);
  } catch (_) {
    retry();
    return;
  }
  presence = socket;
  let openedAt = 0;
  socket.addEventListener('open', () => { openedAt = Date.now(); }, {once:true});
  socket.addEventListener('close', () => {
    if (presence !== socket) return;
    presence = null;
    if (openedAt && Date.now() - openedAt > 4000) presenceFailures = 0;
    retry();
  }, {once:true});
}
function closePresence() {
  clearTimeout(presenceTimer);
  presenceTimer = null;
  const socket = presence;
  presence = null;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
}
function api(path, method, body) {
  let controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), method === 'GET' ? 3500 : 9000);
  const headers = {'X-PVZ-Session':session};
  if (body) headers['Content-Type'] = 'application/json';
  return fetch(path,{
    method, headers, body:body && JSON.stringify(body), signal:controller.signal,
    cache:'no-store', credentials:'same-origin', redirect:'error',
  }).then(async (res) => {
    let data = null;
    try { data = await res.json(); } catch (_) { /* No JSON is valid for completed stop/quit calls. */ }
    if (!res.ok || data?.ok === false) {
      if (res.status === 401 || res.status === 403) throw new Error(desktopWindow ? '本机授权已失效。请关闭窗口，再重新打开独立版。' : '此页面的本机授权已失效。请从启动器重新打开独立版。');
      if (res.status === 409) throw new Error(data?.error || data?.message || '当前连接状态不允许此操作。');
      throw new Error(data?.error || data?.message || ('本机服务返回错误 ' + res.status));
    }
    return data;
  }).finally(() => clearTimeout(timer));
}
function accept(data) {
  if (!data || typeof data !== 'object' || !data.manifest) throw new Error('本机服务没有返回有效状态。');
  const now = data.output_enabled === true;
  if (previousOutput && !now && !stopping && !quitting) tell('本次设备反馈已结束；如需继续，请重新授权。');
  previousOutput = now;
  state = data;
  reachable = true;
  connectPresence();
  render();
}
async function refresh() {
  const data = await api('/api/state','GET');
  accept(data?.state || data);
}
function lose(err) {
  reachable = false;
  reportNetwork(err?.name === 'AbortError' || err instanceof TypeError
    ? (desktopWindow ? '连接本机服务失败。请关闭窗口，再重新打开独立版。' : '连接本机服务失败。请确认独立版仍在运行。') : localizedError(err));
  render();
}
function schedule(delay = 950) {
  clearTimeout(pollTimer);
  if (!quitting && session) pollTimer = setTimeout(poll,delay);
}
async function poll() {
  if (pending || quitting) return;
  try { await refresh(); }
  catch (err) { if (!quitting) lose(err); }
  finally { if (!pending) schedule(document.hidden ? 2200 : 950); }
}
let pollInFlight;
const rawPoll = poll;
poll = function runPoll() {
  if (!pollInFlight) {
    pollInFlight = rawPoll().finally(() => { pollInFlight = null; });
  }
  return pollInFlight;
};
async function act(action, fields = {}) {
  if (!reachable || pending || quitting) return;
  clearTimeout(pollTimer);
  pending = true;
  if (action === 'quit') { exitRequested = true; closePresence(); }
  stopping = ['stop','disable','unpair'].includes(action);
  if (action === 'config') saveMessage = '正在保存…';
  render();
  try {
    if (pollInFlight) await pollInFlight;
    const data = await api('/api/action','POST',{action,...fields});
    if (action === 'quit') {
      quitting = true;
      reachable = false;
      closePresence();
      if ($('modal').open) $('modal').close();
      reportNetwork(desktopWindow ? '正在关闭独立版…' : '已请求关闭独立版。本页面现在可以关闭。');
      return;
    }
    if (action === 'config') saveMessage = '已自动保存';
    const next = data?.state || data;
    if (next?.manifest) accept(next);
    else await refresh();
    if (action === 'stop') tell('已停止本次反馈。');
    if (action === 'unpair') tell('设备连接已结束。');
    if (action === 'reset_baseline') tell('插件累积已重置。');
    if (action === 'import_presets') tell('波形预设已导入。');
  } catch (err) {
    if (action === 'quit') exitRequested = false;
    if (action === 'config') saveMessage = '未保存：' + localizedError(err);
    tell(localizedError(err));
    try { await refresh(); } catch (refreshError) { lose(refreshError); }
  } finally {
    pending = false;
    stopping = false;
    render();
    if (!quitting) schedule();
  }
}
$('toggle-plugin').addEventListener('click',() => {
  if (!state) return;
  if (!state.enabled && outputReady(state) && !confirm('设备已就绪。启用插件会允许本次游戏触发设备反馈，确定启用？')) return;
  act(state.enabled ? 'disable' : 'enable');
});
$('open-mod-tab').addEventListener('click',() => {
  view('plugin');
  chooseTab(schema().findIndex((section) => (section.fields || []).some((field) => field.key === 'auto_install_mod')),true);
});
$('import-presets').addEventListener('click',() => $('import-preset-file').click());
$('import-preset-file').addEventListener('change',async (ev) => {
  const file = ev.target.files?.[0];
  ev.target.value = '';
  if (!file) return;
  try {
    if (!/\.json$/i.test(file.name) || file.size > 256 * 1024) throw new Error('请选择不超过 256 KiB 的 JSON 文件。');
    const data = JSON.parse(await file.text());
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('预设文件必须是“名称 → 帧列表”的 JSON 对象。');
    const entries = Object.entries(data);
    if (entries.length < 1 || entries.length > 16) throw new Error('每次导入 1 至 16 个预设。');
    if (entries.some(([name, frames]) => !name.trim() || !Array.isArray(frames) || frames.length < 1 || frames.length > 512 || frames.some((frame) => typeof frame !== 'string' || !/^[a-f0-9]{16}$/i.test(frame)))) {
      throw new Error('每个预设须有 1 至 512 帧，每帧恰好 16 个十六进制字符。');
    }
    await act('import_presets',{value:data});
  } catch (err) {
    tell(err instanceof SyntaxError ? '文件不是有效的 JSON。' : localizedError(err));
  }
});
$('pair-ip').addEventListener('change',(ev) => {
  selectedPairIp = ev.target.value || 'auto';
  if (state) renderDevice(state.device || {},state.pair || {});
});
$('device-slot').addEventListener('change',(ev) => { if (ev.target.value) act('slot',{value:ev.target.value}); });
$('pair-model').addEventListener('change',(ev) => {
  selectedPairModel = ev.target.value;
  if (state) renderDevice(state.device || {},state.pair || {});
});
$('fullscreen').addEventListener('click',async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen();
    else tell('当前浏览器不支持全屏切换。');
  } catch (_) { tell('无法切换全屏。'); }
});
$('copy-logs').addEventListener('click',async () => {
  try {
    await navigator.clipboard.writeText($('logs-list').textContent);
    tell('日志已复制。');
  } catch (_) { tell('未能访问剪贴板；可选中日志后手动复制。'); }
});
$('close-modal').addEventListener('click',() => $('modal').close());
$('modal').addEventListener('close',() => { currentModal = ''; });
document.addEventListener('click',(ev) => {
  const el = ev.target.closest('button,[data-page]');
  if (!el || el.disabled) return;
  if (el.dataset.page) view(el.dataset.page);
  if (el.dataset.modal) openModal(el.dataset.modal);
  const action = el.dataset.action;
  if (action === 'pair') act('pair',{ip:selectedPairIp,model:$('pair-model').value});
  else if (action === 'reset_baseline') {
    if (confirm('清零本插件的永久累积强度？不会修改手机手动档位。')) act('reset_baseline');
  } else if (action === 'quit') {
    if (confirm('退出将结束本次连接和设备反馈。确定退出？')) act('quit');
  } else if (action) act(action);
});
const pageHash = () => location.hash.slice(1).split('=')[0];
window.addEventListener('hashchange',() => view(pageHash(),false));
window.addEventListener('pagehide',() => { pageActive = false; closePresence(); });
window.addEventListener('pageshow',() => { pageActive = true; connectPresence(); });
document.addEventListener('visibilitychange',() => { if (!document.hidden && !pending) schedule(20); });
const compactSidebar = matchMedia('(max-width:630px)');
function placeDevicePanel() {
  const panel = $('live-device-panel');
  const slot = $(compactSidebar.matches ? 'device-status-narrow' : 'sidebar-device-status');
  if (panel.parentElement !== slot) slot.append(panel);
}
compactSidebar.addEventListener('change',placeDevicePanel);
placeDevicePanel();
view(pageHash(),false);
render();
if (session) schedule(0);
else {
  reportNetwork(desktopWindow ? '未能取得本机授权。请关闭窗口，再重新打开独立版。' : '没有本机授权。请从启动器打开独立版，而不是直接访问此页面。');
  $('save-status').textContent = '未连接本机服务';
}

