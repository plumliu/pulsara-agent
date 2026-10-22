import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, nativeImage, shell, Tray, type IpcMainInvokeEvent } from 'electron';
import { stat } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import os from 'node:os';
import { BundledPostgres } from './postgres';
import { PythonRuntime } from './runtime';
import { UiPreferences } from './ui-preferences';
import { cliLauncherScript, cliStatus, registerCli, type CliRegistration } from './cli-install';
import { BrowserOwner } from './browser-owner';
import { StartupCover } from './startup-cover';
import { isOAuthReturn } from './oauth-return';

const resources = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..', 'resources');
function resolveHome(): string {
  try { return execFileSync(path.join(resources,'python/bin/python3'), ['-c','from pulsara_agent.capability.pulsara_home import require_pulsara_home; print(require_pulsara_home())'], {encoding:'utf8'}).trim(); }
  catch { dialog.showErrorBox('Pulsara 无法启动','无法解析应用数据目录或缺少随包 Python。请检查 PULSARA_HOME 与安装资源。'); app.exit(1); throw new Error('Desktop home unavailable'); }
}
const home = resolveHome();
const startupFile = path.resolve(__dirname, '..', 'assets', 'startup.html');
const desktopIcon = path.resolve(__dirname, '..', 'assets', 'pulsara.png');
app.setName('Pulsara');
app.setPath('userData', path.join(home, 'desktop', 'electron'));
const uiPreferences = new UiPreferences(path.join(app.getPath('userData'), 'ui-preferences.json'));
let window: BrowserWindow | undefined;
let tray: Tray | undefined;
let origin: string | undefined;
let browser: BrowserOwner | undefined;
let startupCover: StartupCover | undefined;
let starting: Promise<void> | undefined;
let quitting = false;
let quittingRequest = false;
let resetting = false;
let databaseReady = false;
let startupError = '';
const postgres = new BundledPostgres(resources, home);
const runtime = new PythonRuntime(resources, home, event => {
  if ((event.type === 'failed' || event.type === 'exited') && !starting && !quitting && !resetting) {
    startupError = event.error ?? '内核不可用';
    void showStartup(startupError).catch(error => console.error('Could not display startup status', error));
  }
});

function focusWindow() { if (window) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); } }
async function showStartup(error = '', phase = error ? 'failed' : 'starting') {
  const retiring = browser?.retireHost();
  origin = undefined;
  await retiring;
  await startupCover?.show(error, phase);
  focusWindow();
}
async function boot(fromReset = false) {
  if (quitting || (resetting && !fromReset)) return;
  if (starting) return starting;
  starting = (async () => {
    if (runtime.running) throw new Error('之前的内核仍在收尾，请等待它退出后重试。');
    // Loading the cover and waiting for the old owner need not delay Python
    // imports. The old owner still gates PostgreSQL, and both preparations gate
    // the start frame. Join every owner before navigation or displaying failure.
    const showing = showStartup();
    const preparing = runtime.start(async () => {
      await runtime.waitForPreviousOwner();
      if (quitting) throw new Error('Pulsara 正在退出。');
      const [cover, database] = await Promise.allSettled([showing, postgres.start()]);
      if (cover.status === 'rejected') throw cover.reason;
      if (database.status === 'rejected') throw database.reason;
      databaseReady = true;
      return postgres.settings();
    }, true);
    const [cover, kernel] = await Promise.allSettled([showing, preparing]);
    if (quitting) return;
    if (cover.status === 'rejected') throw cover.reason;
    if (kernel.status === 'rejected') throw kernel.reason;
    const url = kernel.value;
    const parsed = new URL(url);
    if (parsed.protocol !== 'http:' || parsed.hostname !== '127.0.0.1' || parsed.username || parsed.password || parsed.pathname !== '/') throw new Error('内核返回了无效的本地工作台地址。');
    origin = parsed.origin;
    startupError = '';
    await window!.loadURL(url);
  })().catch(async error => {
    startupError = error instanceof Error ? error.message : String(error);
    await showStartup(startupError);
    // Leave an existing database untouched on failure; another old Python owner may still be closing.
  }).finally(() => { starting = undefined; });
  return starting;
}

function trusted(event: IpcMainInvokeEvent, startupAllowed = false) {
  if (startupAllowed && startupCover?.accepts(event)) return;
  if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) throw new Error('无效的桌面请求来源。');
  const url = event.senderFrame.url;
  const isWorkbench = origin && new URL(url).origin === origin && new URL(url).pathname === '/';
  if (!isWorkbench) throw new Error('此页面不能调用桌面接口。');
}
function handle(name: string, action: (value?: any) => unknown, startupAllowed = false) {
  ipcMain.handle(`desktop:${name}`, (event, value) => { trusted(event, startupAllowed); return action(value); });
}
function cliRegistration(): CliRegistration {
  const packageTarget = process.platform === 'linux' && resources.startsWith('/opt/')
    ? path.join(resources, 'bin', 'pulsara') : undefined;
  return {
    destination: packageTarget ? '/usr/bin/pulsara' : path.join(os.homedir(), '.local', 'bin', 'pulsara'),
    script: cliLauncherScript(resources), packageTarget, searchPath: process.env.PATH ?? '',
  };
}
// Registration is best effort and independent of kernel/database startup.
let cliRegistrationPending: Promise<unknown> | undefined;
async function ensureCli(replaceConflict = false) {
  const pending = (cliRegistrationPending ?? Promise.resolve()).then(() => registerCli(cliRegistration(), replaceConflict));
  cliRegistrationPending = pending;
  try { return await pending; } finally { if (cliRegistrationPending === pending) cliRegistrationPending = undefined; }
}
async function resetDatabase() {
  if (starting || quitting || quittingRequest || resetting) throw new Error('请等待当前启动或关闭操作完成。');
  resetting = true;
  try {
    const choice = await dialog.showMessageBox(window!, {type:'warning',message:'重置 Pulsara 专用数据库？',detail:`将永久删除会话、记忆和定时任务，并重建当前版本数据库。工作目录文件和模型配置保留。\n\n${postgres.directory}`,buttons:['取消','重置数据库'],defaultId:0,cancelId:0});
    if(choice.response!==1)return;
    await showStartup('正在停止内核并重置专用数据库…', 'resetting');
    await runtime.stop();
    await runtime.waitForPreviousOwner();
    await postgres.resetConfirmed();
    databaseReady=false;
    await boot(true);
  } catch(error) {await showStartup(`数据库重置未完成：${error instanceof Error?error.message:String(error)}`);}
  finally {resetting=false;}
}
async function requestQuit() {
  if (quittingRequest || quitting || resetting) return;
  quittingRequest = true;
  try {
    const work = await runtime.inspectQuit();
    if (work.error || work.activeTasks || work.scheduledTasks) {
      focusWindow();
      const choice = await dialog.showMessageBox(window!, {type:'question', message:'退出 Pulsara？', detail: work.error || '退出会停止当前工作及后台调度。关闭窗口可以继续在后台运行。', buttons:['继续后台运行','退出 Pulsara'], defaultId:0, cancelId:0});
      if (choice.response !== 1) return;
    }
    quitting = true;
    await showStartup('正在停止并结算当前工作，等待内核退出…', 'stopping');
    await runtime.stop();
    if (starting) await starting;
    // If startup never owned a ready kernel and reused a database, don't stop another owner's instance.
    if (postgres.startedHere || (databaseReady && !startupError)) await postgres.stop();
    app.exit(0);
  } catch (error) {
    quitting = false;
    await showStartup(`退出尚未完成：${error instanceof Error ? error.message : String(error)}`);
  } finally { quittingRequest = false; }
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', focusWindow);
  app.on('open-url', (event, url) => {
    event.preventDefault();
    if (isOAuthReturn(url)) focusWindow();
  });
  app.on('activate', focusWindow);
  app.on('before-quit', event => { event.preventDefault(); void requestQuit(); });
  app.on('window-all-closed', () => { /* The explicit Quit action owns process shutdown. */ });
  void app.whenReady().then(async () => {
    if (app.isPackaged) app.setAsDefaultProtocolClient('pulsara');
    window = new BrowserWindow({width: 1440, height: 920, minWidth: 760, minHeight: 520, backgroundColor:'#f3f0e7', title:'Pulsara',
      ...(process.platform === 'linux' ? {icon: desktopIcon} : {}),
      titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'hidden',
      trafficLightPosition: {x: 16, y: 16},
      webPreferences: {preload:path.join(__dirname,'preload.js'), sandbox:true, contextIsolation:true, nodeIntegration:false, webviewTag:false, navigateOnDragDrop:false}});
    browser = new BrowserOwner(window, () => origin);
    startupCover = new StartupCover(window, startupFile, path.join(__dirname, 'preload.js'));
    window.webContents.setWindowOpenHandler(({url}) => {
      if (/^https?:\/\//i.test(url)) void dialog.showMessageBox(window!, {message:'在默认浏览器中打开链接？', detail:url, buttons:['取消','打开'], defaultId:0,cancelId:0}).then(choice => {if(choice.response===1) void shell.openExternal(url);});
      return {action:'deny'};
    });
    window.webContents.on('will-navigate', event => event.preventDefault());
    window.webContents.on('will-attach-webview', event => event.preventDefault());
    window.webContents.session.setPermissionRequestHandler((_contents, _permission, done) => done(false));
    window.on('close', event => {
      if (quitting) return;
      event.preventDefault();
      if (process.platform === 'darwin' || tray) window!.hide();
      else void dialog.showMessageBox(window!, {message:'关闭 Pulsara', buttons:['最小化','退出'], defaultId:0,cancelId:0}).then(result => result.response ? void requestQuit() : window!.minimize());
    });
    if (process.platform === 'linux') {
      try {
        tray = new Tray(nativeImage.createFromPath(desktopIcon).resize({width:32,height:32}));
        tray.setToolTip('Pulsara'); tray.setContextMenu(Menu.buildFromTemplate([{label:'打开 Pulsara',click:focusWindow},{label:'退出 Pulsara',click:()=>void requestQuit()}]));
        tray.on('click',focusWindow);
      } catch { tray = undefined; }
    }
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      {label:'Pulsara',submenu:[{label:'关于 Pulsara',role:'about'},{label:'重置专用数据库…',click:()=>void resetDatabase().catch(error=>dialog.showErrorBox('重置失败',String(error)))},{type:'separator'},{label:'退出 Pulsara',accelerator:'CommandOrControl+Q',click:()=>void requestQuit()}]},
      {label:'文件',submenu:[{label:'关闭窗口',role:'close',accelerator:'CommandOrControl+W'}]},
      {role:'editMenu'}, {label:'显示',submenu:[{role:'reload'},{role:'togglefullscreen'},{role:'zoomIn'},{role:'zoomOut'},{role:'resetZoom'},{role:'toggleDevTools'}]}, {role:'windowMenu'},
    ]));
    handle('minimize',()=>window!.minimize(),true); handle('maximize',()=>window!.isMaximized()?window!.unmaximize():window!.maximize(),true);
    handle('close',()=>window!.close(),true); handle('quit',()=>requestQuit(),true);
    handle('retry',()=>boot(),true); handle('logs',()=>shell.openPath(path.join(home,'desktop')),true);
    handle('reset-database',()=>resetDatabase(),true);
    handle('read-ui-preferences', () => uiPreferences.read());
    handle('startup-settled', () => startupCover!.finish());
    handle('focus', focusWindow);
    handle('browser-create', value => browser!.create(value));
    handle('browser-command', value => browser!.command(value));
    handle('browser-close', value => browser!.close(value));
    handle('browser-forget-session', value => browser!.forgetSession(value));
    handle('browser-clear', () => browser!.clear());
    handle('browser-activate', value => browser!.activate(value));
    handle('browser-layout', value => browser!.layout(value));
    handle('browser-hide', value => browser!.hide(value));
    handle('write-ui-preference', value => uiPreferences.write(value));
    handle('cli-status',async()=>{ await cliRegistrationPending; return cliStatus(cliRegistration()); });
    handle('repair-cli',value=>{ if (typeof value !== 'boolean') throw new Error('无效的命令行入口操作。'); return ensureCli(value); });
    handle('write-clipboard-text', value => {
      if (typeof value !== 'string') throw new Error('剪贴板内容必须是文本。');
      return clipboard.writeText(value);
    });
    handle('choose-files',async()=>{const result=await dialog.showOpenDialog(window!,{properties:['openFile','multiSelections']});return result.canceled?[]:result.filePaths;});
    handle('choose-directory', async () => {
      // Electron owns platform-specific desktop lookup (including Linux XDG).
      // A desktop folder is optional; never create one just to open the picker.
      let defaultPath = app.getPath('home');
      try {
        const desktop = app.getPath('desktop');
        if ((await stat(desktop)).isDirectory()) defaultPath = desktop;
      } catch { /* Missing/unavailable desktop: start at the user's home. */ }
      const result = await dialog.showOpenDialog(window!, {
        defaultPath, properties: ['openDirectory', 'createDirectory'],
      });
      return result.canceled ? null : result.filePaths[0];
    });
    handle('open-path',async value=>{
      if (!value || typeof value.path!=='string' || !path.isAbsolute(value.path) || typeof value.reveal!=='boolean') throw new Error('无效的本地文件操作。');
      if (value.reveal) {shell.showItemInFolder(value.path);return;}
      if (!(await stat(value.path)).isDirectory() && !/\.(?:png|jpe?g|gif|webp|avif|bmp|tiff?|pdf|docx?|xlsx?|pptx?|odt|ods|odp)$/i.test(value.path)) throw new Error('此文件类型不能通过预览直接启动。');
      const error=await shell.openPath(value.path);if(error)throw new Error(error);
    });
    handle('open-external',async value=>{if(typeof value!=='string')throw new Error('链接无效。');const target=new URL(value);if(!['https:','http:'].includes(target.protocol)||target.username||target.password)throw new Error('不支持此链接协议。');await shell.openExternal(target.href);});
    if (app.isPackaged) void ensureCli().catch(error => console.error('CLI registration failed', error));
    await boot();
  });
}
