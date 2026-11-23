'use strict';

const { app, BrowserWindow, Menu, dialog, nativeTheme, screen, session } = require('electron');
const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const { existsSync } = require('node:fs');
const path = require('node:path');

const APP_NAME = '杂交版 · 受伤反馈';
const SMOKE = process.argv.includes('--desktop-smoke');
const START_MS = 16000;
const GRACE_MS = 3500;
const KILL_MS = 1400;
const MAX_HANDSHAKE = 4096;
const ASSETS = new Set(['/', '/app.js', '/styles.css', '/api/state', '/api/action']);

let mainWindow = null;
let backend = null;
let displayed = false;
let stopping = false;
let exitStarted = false;

const failureMessages = Object.freeze({
  missing: '未找到程序随附的运行组件。请完整解压发行包后重新打开，不要只复制 EXE。',
  occupied: '请先退出旧版控制面板，再打开 EXE。',
  timeout: '本机服务启动超时。请关闭程序后重试；如仍出现问题，可重新解压发行包。',
  protocol: '本机服务未返回有效的启动信息。请检查是否完整解压了发行包。',
  backend: '本机后台服务意外退出，桌面应用已关闭。请重新打开应用。',
  renderer: '应用界面未能正常运行，反馈服务已被停止。请重新打开应用。',
  stuck: '未能确认后台服务已停止。请从任务管理器关闭此应用启动的 Python 进程。',
  smoke: '桌面自检未通过。'
});

class StartupError extends Error {
  constructor(code) {
    super(code);
    this.kind = code;
  }
}

function untilSettled(promise, ms, fallback) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => resolve(fallback), ms);
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (err) => { clearTimeout(timer); reject(err); }
    );
  });
}

function parsePanel(line) {
  let obj;
  try { obj = JSON.parse(line); } catch (_) { throw new StartupError('protocol'); }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj) || Object.keys(obj).length !== 2 ||
      obj.kind !== 'panel' || typeof obj.url !== 'string') {
    throw new StartupError('protocol');
  }
  const match = /^http:\/\/127\.0\.0\.1:([1-9]\d{0,4})\/#session=([A-Za-z0-9_-]{43})$/.exec(obj.url);
  if (!match || Number(match[1]) > 65535) throw new StartupError('protocol');
  const parsed = new URL(obj.url);
  if (!parsed.port || parsed.href !== obj.url) throw new StartupError('protocol');
  return parsed;
}

function spawnBackend() {
  const infra = path.join(process.resourcesPath, 'backend');
  const py = path.join(infra, 'runtime', 'python.exe');
  const script = path.join(infra, 'app', 'app.py');
  if (!existsSync(py) || !existsSync(script)) throw new StartupError('missing');

  const env = { ...process.env, PYTHONUTF8: '1', PYTHONUNBUFFERED: '1' };
  for (const key of ['PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONUSERBASE']) delete env[key];
  const child = spawn(py, ['-X', 'utf8', '-B', script, SMOKE ? '--desktop-verify' : '--desktop'], {
    cwd: path.dirname(script),
    windowsHide: true,
    shell: false,
    detached: false,
    stdio: ['pipe', 'pipe', 'pipe'],
    env
  });

  // Only the framed startup record is consumed. Never forward backend streams to logs.
  child.stderr.resume();
  child.stderr.on('error', () => {});
  child.stdout.on('error', () => {});
  child.stdin.on('error', () => {});
  const status = { child, done: false, killed: false };
  status.end = new Promise((resolve) => {
    child.once('close', (code, signal) => {
      status.done = true;
      resolve({ code, signal });
    });
  });
  status.panel = new Promise((resolve, reject) => {
    let bytes = Buffer.alloc(0);
    let done = false;
    const timer = setTimeout(() => settle(new StartupError('timeout')), START_MS);
    function settle(err, panel) {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child.stdout.off('data', take);
      bytes = Buffer.alloc(0);
      child.stdout.resume();
      if (err) reject(err);
      else resolve(panel);
    }
    function take(chunk) {
      const nl = chunk.indexOf(10);
      const part = nl < 0 ? chunk : chunk.subarray(0, nl);
      if (bytes.length + part.length > MAX_HANDSHAKE) {
        settle(new StartupError('protocol'));
        return;
      }
      bytes = Buffer.concat([bytes, part]);
      if (nl < 0) return;
      try { settle(null, parsePanel(bytes.toString('utf8'))); }
      catch (err) { settle(err); }
    }
    child.stdout.on('data', take);
    child.on('error', () => settle(new StartupError('missing')));
    status.end.then(({ code }) => {
      settle(new StartupError(code === 2 ? 'occupied' : 'backend'));
    });
  });
  return status;
}

function hideWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.hide();
}

async function stopBackend() {
  if (!backend) return { stopped: true, graceful: false };
  const current = backend;
  if (!current.done) {
    // EOF is an independent shutdown signal, even if the command wasn't delivered.
    try { current.child.stdin.end('quit\n', 'utf8'); }
    catch (_) { /* The child may already be exiting. */ }
  }
  let result = await untilSettled(current.end, GRACE_MS, null);
  if (!result) {
    current.killed = true;
    try { current.child.kill(); } catch (_) { /* Retry observation of the existing handle. */ }
    result = await untilSettled(current.end, KILL_MS, null);
  }
  return { stopped: !!result, graceful: !!result && result.code === 0 && !current.killed };
}

function printLine(stream, line) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, 700);
    try {
      stream.write(line + '\n', () => { clearTimeout(timer); resolve(); });
    } catch (_) { clearTimeout(timer); resolve(); }
  });
}

async function shutdown(reason = null, success = false) {
  if (stopping) return;
  stopping = true;
  hideWindow();
  let stopped = { stopped: false, graceful: false };
  try { stopped = await stopBackend(); } catch (_) {}
  if (!stopped.stopped) reason = 'stuck';
  else if (SMOKE && success && !stopped.graceful) reason = 'smoke';
  if (SMOKE) {
    if (success && !reason) await printLine(process.stdout, 'DESKTOP_SMOKE_OK');
    else await printLine(process.stderr, 'DESKTOP_SMOKE_FAILED: ' + (reason || 'interrupted'));
  } else if (reason) {
    try {
      await dialog.showMessageBox({
        type: 'error',
        title: APP_NAME,
        message: '无法继续运行',
        detail: failureMessages[reason] || failureMessages.backend,
        buttons: ['确定'],
        noLink: true
      });
    } catch (_) {}
  }
  exitStarted = true;
  app.exit(reason || (SMOKE && !success) ? 1 : 0);
}

function topDocument(url, panel) {
  try {
    const target = new URL(url);
    return target.origin === panel.origin && target.protocol === 'http:' &&
      !target.username && !target.password && target.pathname === '/' && target.search === '';
  } catch (_) { return false; }
}

function allowRequest(details, panel) {
  try {
    const target = new URL(details.url);
    if (target.protocol === 'data:') {
      return details.resourceType === 'image' &&
        /^data:image\/(?:png|svg\+xml)(?:[;,]|$)/i.test(details.url);
    }
    if (target.username || target.password || target.search || target.host !== panel.host) return false;
    if (target.protocol === 'ws:') {
      return details.resourceType === 'webSocket' && target.pathname === '/api/presence';
    }
    return target.protocol === 'http:' && ASSETS.has(target.pathname) &&
      details.resourceType !== 'subFrame';
  } catch (_) { return false; }
}

async function lockedSession(panel) {
  const local = session.fromPartition('pvz-volatile-' + randomBytes(12).toString('hex'), { cache: false });
  await local.setProxy({ mode: 'direct' });
  local.setPermissionRequestHandler((_wc, _permit, callback) => callback(false));
  local.setPermissionCheckHandler(() => false);
  local.setDevicePermissionHandler(() => false);
  local.setSpellCheckerEnabled(false);
  local.on('will-download', (event) => event.preventDefault());
  local.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !allowRequest(details, panel) }));
  return local;
}

function guardNavigation(contents, panel) {
  function gate(event, value, _inPlace, frame) {
    if (typeof event.isMainFrame === 'boolean' && !event.isMainFrame) return event.preventDefault();
    if (typeof frame === 'boolean' && !frame) return event.preventDefault();
    const url = event.url || (typeof value === 'string' ? value : value?.url);
    if (!topDocument(url, panel)) event.preventDefault();
  }
  contents.on('will-navigate', gate);
  contents.on('will-frame-navigate', gate);
  contents.on('will-redirect', gate);
  contents.on('will-attach-webview', (event) => event.preventDefault());
  contents.setWindowOpenHandler(() => ({ action: 'deny' }));
  contents.on('render-process-gone', () => { void shutdown('renderer'); });
  contents.on('unresponsive', () => { void shutdown('renderer'); });
}

function openWindow(panel, local) {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;
  const icon = path.join(__dirname, 'assets', 'app.ico');
  const window = new BrowserWindow({
    title: APP_NAME,
    width: Math.min(1240, width),
    height: Math.min(810, height),
    minWidth: Math.min(880, width),
    minHeight: Math.min(610, height),
    autoHideMenuBar: true,
    show: false,
    backgroundColor: '#ffffff',
    ...(existsSync(icon) ? { icon } : {}),
    webPreferences: {
      session: local,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
      devTools: false,
      webviewTag: false,
      spellcheck: false,
      backgroundThrottling: !SMOKE
    }
  });
  window.removeMenu();
  window.on('page-title-updated', (event) => event.preventDefault());
  window.on('close', (event) => {
    if (!exitStarted) { event.preventDefault(); void shutdown(); }
  });
  window.on('closed', () => { mainWindow = null; });
  window.once('ready-to-show', () => {
    if (!SMOKE && !stopping && !window.isDestroyed()) window.show();
  });
  guardNavigation(window.webContents, panel);
  return window;
}

async function verifyUI(contents) {
  if (nativeTheme.themeSource !== 'light' || nativeTheme.shouldUseDarkColors) {
    throw new StartupError('smoke');
  }
  const probe = `(async () => {
    const tabs = [...document.querySelectorAll('#tabs [role="tab"]')].map(n => n.textContent.trim());
    const token = sessionStorage.getItem('pvz-standalone-session');
    const root = document.documentElement;
    if (!token || tabs.length !== 4 || root.dataset.theme !== 'light' ||
        getComputedStyle(root).colorScheme !== 'light') return false;
    // Settings tab switches are local render operations; never click an enable or pair control.
    for (let i = 0; i < tabs.length; i += 1) {
      const tab = document.querySelectorAll('#tabs [role="tab"]')[i];
      tab.click();
      if (document.querySelector('#tabs [aria-selected="true"]')?.id !== tab.id) return false;
    }
    document.querySelector('#tabs [role="tab"]')?.click();
    const unauthorized = await fetch('/api/state', {cache:'no-store',credentials:'omit'});
    const response = await fetch('/api/state', {cache:'no-store',credentials:'omit',headers:{'X-PVZ-Session':token}});
    if (unauthorized.status !== 403 || !response.ok) return false;
    const s = await response.json();
    const button = document.querySelector('#toggle-plugin');
    document.querySelector('button[data-page="device"]')?.click();
    const picker = document.querySelector('#pair-ip');
    const autoDefault = !document.querySelector('#page-device')?.hidden &&
      picker?.value === 'auto' && picker.selectedOptions[0]?.textContent.trim() === '自动选择';
    const status = document.querySelector('#pair-state');
    const statusFields = ['pair-state','pair-bound-ip','pair-diagnostic','device-meters'];
    const layoutOk = statusFields.every(id => document.querySelectorAll('#'+id).length === 1) &&
      Boolean(document.querySelector('#page-device #pair-qr') && document.querySelector('#page-device #device-slot'));
    document.querySelector('button[data-page="plugin"]')?.click();
    const sidebarOk = !matchMedia('(min-width: 631px)').matches ||
      (status?.closest('.sidebar') && status.getClientRects().length > 0 &&
       statusFields.every(id => document.querySelector('.sidebar #'+id)));
    return layoutOk && sidebarOk && autoDefault && !location.href.includes('session=') && tabs.join('|') === '模式与输出|强度反馈|触发事件|游戏 Mod' &&
      s.manifest?.config_schema?.length === 4 && s.enabled === false && s.output_enabled === false &&
      s.device?.connected === false && s.device?.ready === false && s.pair?.active === false &&
      button?.textContent?.trim() === '启用插件' && button.disabled === false;
  })().catch(() => false)`;
  const deadline = Date.now() + 10500;
  while (!stopping && Date.now() < deadline) {
    if (await untilSettled(contents.executeJavaScript(probe, false), 1300, false) === true) return;
    await new Promise((resolve) => setTimeout(resolve, 160));
  }
  throw new StartupError('smoke');
}

async function initialize() {
  nativeTheme.themeSource = 'light';
  Menu.setApplicationMenu(null);
  backend = spawnBackend();
  backend.end.then(({ code }) => {
    if (!stopping && displayed) void shutdown(code === 0 && !SMOKE ? null : 'backend');
  });
  const panel = await backend.panel;
  if (stopping) return;
  if (backend.done) throw new StartupError('backend');
  const local = await lockedSession(panel);
  if (stopping) return;
  mainWindow = openWindow(panel, local);
  let loaded = false;
  try {
    loaded = await untilSettled(mainWindow.loadURL(panel.href).then(() => true), START_MS, false);
  } catch (_) {}
  if (stopping) return;
  if (!loaded || backend.done) throw new StartupError(loaded ? 'backend' : 'renderer');
  displayed = true;
  if (SMOKE) {
    await verifyUI(mainWindow.webContents);
    await shutdown(null, true);
  }
}

app.setName('PVZ Feedback');
app.setAppUserModelId('TianXianbaobao.PVZHybridFeedback');
if (!SMOKE && !app.requestSingleInstanceLock()) {
  app.exit(0);
} else {
  if (!SMOKE) app.on('second-instance', () => {
    if (stopping || !mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
  app.on('before-quit', (event) => {
    if (!exitStarted) { event.preventDefault(); void shutdown(); }
  });
  app.on('window-all-closed', () => { if (!exitStarted) void shutdown(); });
  process.once('SIGINT', () => { void shutdown(); });
  process.once('SIGTERM', () => { void shutdown(); });
  app.whenReady().then(initialize).catch((err) => {
    void shutdown(err instanceof StartupError ? err.kind : 'renderer');
  });
}

