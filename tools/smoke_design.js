// @ts-check
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawnSync } = require('node:child_process');

async function child() {
  const electron = require('electron');
  const root = process.env.CAFE_DESIGN_SMOKE_APP;
  const out = process.env.CAFE_DESIGN_SMOKE_OUT;
  if (!root || !out || !path.isAbsolute(root) || !path.isAbsolute(out)) throw new Error('SMOKE_PATHS_REJECTED');
  const userData = path.join(root, 'user-data'); fs.mkdirSync(userData);
  electron.app.setPath('userData', userData);
  electron.app.disableHardwareAcceleration();
  // index boots itself in an actual Electron main process: observe that one boot.
  const created = new Promise(resolve => electron.app.once('browser-window-created', (_event, win) => resolve(win)));
  require('../src/main/index');
  const win = await created;
  await new Promise((resolve, reject) => {
    win.webContents.once('did-finish-load', resolve);
    win.webContents.once('did-fail-load', () => reject(new Error('SMOKE_LOAD_FAILED')));
    if (win.webContents.getURL() === 'cafe://app/bootstrap' && !win.webContents.isLoadingMainFrame()) resolve();
  });
  win.setSize(1280, 900); win.show();
  const settle = "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))";
  await win.webContents.executeJavaScript(settle);
  const inspect = `(() => ({
    theme: document.documentElement.getAttribute('data-cafe-theme') || 'dark',
    text: document.body.innerText,
    font: getComputedStyle(document.body).fontFamily,
    background: getComputedStyle(document.body).backgroundColor,
    enabled: [...document.querySelectorAll('button')].filter(b => !b.disabled && b.getClientRects().length > 0).map(b => b.textContent.trim()),
    disabled: [...document.querySelectorAll('button')].filter(b => ['New capsule','Send','Start recording','Connect output'].includes(b.textContent.trim())).map(b => b.disabled),
    bridge: Object.keys(window.cafe || {}).sort()
  }))()`;
  fs.mkdirSync(out, { recursive: true });
  const records = [];
  for (const theme of ['dark', 'light']) {
    if (theme === 'light') {
      await win.webContents.executeJavaScript("[...document.querySelectorAll('button')].find(b => b.textContent.includes('Switch to')).click()");
      await win.webContents.executeJavaScript(settle);
    }
    const actual = await win.webContents.executeJavaScript(inspect);
    fs.writeFileSync(path.join(out, 'observation-' + theme + '.json'), JSON.stringify(actual, null, 2) + '\n');
    if (actual.theme !== theme || !actual.text.includes('Café workspace') || !actual.text.includes('Runtime unavailable') || /OpenAI|MetaViewer|\bTerminal\b/.test(actual.text) || /S[oö]hne|Signifier|\bInter\b/i.test(actual.font) || actual.disabled.length !== 4 || actual.disabled.some(value => !value) || actual.enabled.length !== 1 || !actual.enabled[0].startsWith('Switch to') || actual.bridge.join(',') !== 'getSnapshot,onSnapshot,submitIntent') throw new Error('SMOKE_RENDERER_CONTRACT_REJECTED');
    const image = await win.webContents.capturePage();
    if (image.isEmpty()) throw new Error('SMOKE_EMPTY_SCREENSHOT');
    fs.writeFileSync(path.join(out, theme + '.png'), image.toPNG());
    records.push({ theme, background: actual.background, font: actual.font, dimensions: image.getSize() });
  }
  if (records[0].background === records[1].background) throw new Error('SMOKE_THEME_NOT_APPLIED');
  fs.writeFileSync(path.join(out, 'smoke.json'), JSON.stringify({ status: 'pass', records }, null, 2) + '\n');
  win.close(); electron.app.exit(0);
}

function parent() {
  const args = process.argv.slice(2);
  if (args.length !== 4 || args[0] !== '--electron' || args[2] !== '--out') throw new Error('SMOKE_USAGE');
  const executable = path.resolve(args[1]), out = path.resolve(args[3]);
  if (!fs.statSync(executable).isFile()) throw new Error('SMOKE_ELECTRON_UNAVAILABLE');
  const root = path.resolve(__dirname, '..');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'cafe-design-'));
  try {
    fs.mkdirSync(path.join(temporary, 'dist'));
    for (const name of ['renderer', 'preload']) fs.cpSync(path.join(root, 'dist', name), path.join(temporary, 'dist', name), { recursive: true });
    fs.writeFileSync(path.join(temporary, 'package.json'), JSON.stringify({ name: 'cafe-design-smoke', version: '0.1.0-rc.1', main: __filename }));
    const env = { ...process.env, CAFE_DESIGN_SMOKE_APP: temporary, CAFE_DESIGN_SMOKE_OUT: out };
    delete env.ELECTRON_RUN_AS_NODE; delete env.NODE_OPTIONS;
    const result = spawnSync('xvfb-run', ['-a', executable, temporary], { env, encoding: 'utf8', timeout: 120000, maxBuffer: 1024 * 1024 });
    fs.mkdirSync(out, { recursive: true });
    fs.writeFileSync(path.join(out, 'smoke-process.json'), JSON.stringify({status:result.status,signal:result.signal,error:result.error?.code || null}) + '\n');
    fs.writeFileSync(path.join(out, 'smoke.stderr.log'), result.stderr || '');
    fs.writeFileSync(path.join(out, 'smoke.stdout.log'), result.stdout || '');
    if (result.status !== 0) throw new Error('SMOKE_ELECTRON_FAILED');
    const receipt = JSON.parse(fs.readFileSync(path.join(out, 'smoke.json'), 'utf8'));
    if (receipt.status !== 'pass') throw new Error('SMOKE_RECEIPT_REJECTED');
    console.log(JSON.stringify(receipt));
  } finally { fs.rmSync(temporary, { recursive: true, force: true }); }
}

if (process.versions.electron) {
  child().catch(error => { console.error('SMOKE_RENDERER_FAILED', error instanceof Error ? error.name + ': ' + error.message.split('\n')[0] : 'UNKNOWN'); require('electron').app.exit(1); });
} else {
  try { parent(); } catch (error) { console.error(error instanceof Error ? error.message : 'SMOKE_FAILED'); process.exitCode = 1; }
}
