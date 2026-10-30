import { app, BrowserWindow, clipboard, dialog, ipcMain, Menu, nativeImage, shell, Tray, type IpcMainInvokeEvent } from 'electron';
import { stat } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { BundledPostgres } from './postgres';
import { PythonRuntime } from './runtime';
import { UiPreferences } from './ui-preferences';
import { cliLauncherScript, cliStatus, registerCli, type CliRegistration } from './cli-install';
import { BrowserOwner } from './browser-owner';
import { StartupCover } from './startup-cover';
import { isOAuthReturn } from './oauth-return';
import { desktopResourcePaths } from './resource-paths';
import { posixHostOperations } from './host-posix';
import { createPosixPostgresOperations } from './postgres-posix';
import type { HostOperations } from './host-operations';
import { createWslHostOperations, type WslTarget, type WslSelection } from './host-wsl';
import { WslSetup, loadWslPayload } from './wsl-setup';
import { createWslPostgresOperations } from './postgres-wsl';
import { toBackendPath, toWindowsPath } from './backend-paths';
import { createBackendSession } from './backend-session';
import { checkInstalledWslMaintenance, uninstallWsl } from './wsl-maintenance';

const windowsHost = process.platform === 'win32';
// Avoid Windows occlusion/capture compositor artifacts while retaining accelerated WebGL and video.
if (windowsHost) app.commandLine.appendSwitch('disable-gpu-compositing');
const hostResourcesRoot = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..', 'resources');
let resources = desktopResourcePaths(hostResourcesRoot);
let host: HostOperations = posixHostOperations;
function resolveHome(): string {
  try { return host.resolveHome(resources.pythonExecutable); }
  catch { dialog.showErrorBox('Pulsara 无法启动','无法解析应用数据目录或缺少随包 Python。请检查 PULSARA_HOME 与安装资源。'); app.exit(1); throw new Error('Desktop home unavailable'); }
}
let home = windowsHost ? '' : resolveHome();
const startupFile = path.resolve(__dirname, '..', 'assets', 'startup.html');
const desktopIcon = path.resolve(__dirname, '..', 'assets', 'pulsara.png');
app.setName('Pulsara');
if (!windowsHost) app.setPath('userData', path.join(home, 'desktop', 'electron'));
const uiPreferences = new UiPreferences(path.join(app.getPath('userData'), 'ui-preferences.json'));
const wslSetup = windowsHost ? new WslSetup(path.join(app.getPath('userData'), 'wsl-selection.json')) : undefined;
let wslTarget: WslTarget | undefined;
let setupPending: Promise<void> | undefined;
let startupCancellation: AbortController | undefined;
let backendSession: ReturnType<typeof createBackendSession> | undefined;
let windowTransition: Promise<void> = Promise.resolve();
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
let startupFailure: unknown;
let postgres: BundledPostgres;
let runtime: PythonRuntime;
function configureRuntime(interopHelper?: string) {
  postgres = new BundledPostgres(wslTarget
    ? createWslPostgresOperations(resources, home, host, () => startupCancellation?.signal)
    : createPosixPostgresOperations(resources.postgresBin, home));
  runtime = new PythonRuntime(resources, home, event => {
    if ((event.type === 'failed' || event.type === 'exited') && !starting && !quitting && !resetting) {
      startupError = event.error ?? '内核不可用';
      void showStartup(startupError).catch(error => console.error('Could not display startup status', error));
    }
  }, host, wslTarget ? {
    distribution:wslTarget.distro, user:wslTarget.user, interopHelper:interopHelper!,
    async openOAuthBrowser(url) {
      if (!runtime.running || !origin || quitting || resetting) throw new Error('工作台正在关闭，未打开授权页面。');
      await shell.openExternal(url);
    },
  } : undefined);
}
if (!windowsHost) configureRuntime();

async function prepareWindows(selection?: WslSelection) {
  if (runtime || !wslSetup) return;
  if (setupPending) return setupPending;
  setupPending = (async () => {
    const saved = selection ?? (await wslSetup.discover()).saved;
    if (!saved) { await showStartup('', 'setup'); return; }
    await showStartup('正在检查 WSL 目标与 Linux 应用资源…', 'starting');
    const target = selection ? await wslSetup.select(selection) : await wslSetup.inspect(saved);
    const payload = await loadWslPayload(hostResourcesRoot);
    if (payload.version !== app.getVersion()) throw new Error('随包 Linux 资源版本不匹配，请重新安装 Pulsara。');
    if (quitting) return;
    startupCancellation = new AbortController();
    const prepared = await wslSetup.prepare(target, payload, {signal:startupCancellation.signal});
    if (quitting) return;
    wslTarget = prepared.target; home = prepared.home; resources = prepared.resources;
    host = createWslHostOperations(wslTarget, home, path.join(app.getPath('userData'), 'logs'));
    configureRuntime(prepared.hostHelperPath);
  })().finally(() => { setupPending = undefined; });
  return setupPending;
}

function focusWindow() { if (window) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); } }
function transitionWindow<T>(action: () => Promise<T>): Promise<T> {
  const pending = windowTransition.then(action);
  windowTransition = pending.then(() => {}, () => {});
  return pending;
}
async function showStartup(error = '', phase = error ? 'failed' : 'starting'): Promise<void> {
  const {showing} = await transitionWindow(async () => {
    if (backendSession) {
      const previousSession = backendSession;
      origin = undefined;
      previousSession.seal();
      await previousSession.retire(async () => {
        await destroyWindow();
      });
      // Keep the original failed retirement reachable on retry and Quit.
      backendSession = undefined;
      createWindow();
    }
    const retiring = browser?.retireHost();
    origin = undefined;
    await retiring;
    return {showing:startupCover?.show(error, phase)};
  });
  await showing;
  focusWindow();
}

async function replaceWindow(session: Electron.Session): Promise<void> {
  const {showing} = await transitionWindow(async () => {
    const previous = window;
    const bounds = previous?.getBounds();
    const maximized = previous?.isMaximized();
    await destroyWindow();
    createWindow(session);
    if (bounds) window!.setBounds(bounds);
    if (maximized) window!.maximize();
    return {showing:startupCover!.show()};
  });
  await showing;
}

async function destroyWindow() {
  await browser?.retireHost();
  await startupCover?.retire();
  if (!window || window.webContents.isDestroyed()) return;
  const contents = window.webContents;
  const destroyed = new Promise<void>(resolve => contents.once('destroyed', () => resolve()));
  window.destroy();
  await destroyed;
}

function windowsControlsOverlay() {
  const dark = uiPreferences.read()['pulsara-theme'] === 'dark';
  return {height:44, color:dark ? '#202228' : '#faf8f2', symbolColor:dark ? '#dad5cb' : '#32343d'};
}
function createWindow(session?: Electron.Session) {
  window = new BrowserWindow({width:1440, height:920, minWidth:760, minHeight:520, backgroundColor:'#f3f0e7', title:'Pulsara',
    ...(process.platform !== 'darwin' ? {icon:desktopIcon} : {}),
    titleBarStyle:process.platform === 'darwin' ? 'hiddenInset' : 'hidden', trafficLightPosition:{x:16,y:16},
    ...(windowsHost ? {titleBarOverlay:windowsControlsOverlay(), autoHideMenuBar:true} : {}),
    webPreferences:{preload:path.join(__dirname,'preload.js'), sandbox:true, contextIsolation:true, nodeIntegration:false,
      webviewTag:false, navigateOnDragDrop:false, ...(session ? {session} : {})}});
  browser = new BrowserOwner(window, () => origin);
  startupCover = new StartupCover(window, startupFile, path.join(__dirname,'preload.js'));
  window.webContents.setWindowOpenHandler(({url}) => {
    if (/^https?:\/\//i.test(url)) void dialog.showMessageBox(window!, {message:'在默认浏览器中打开链接？', detail:url, buttons:['取消','打开'], defaultId:0,cancelId:0}).then(choice => {if(choice.response === 1) void shell.openExternal(url);});
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
}
async function boot(fromReset = false) {
  if (quitting || (resetting && !fromReset)) return;
  if (starting) return starting;
  starting = (async () => {
    if (windowsHost && !runtime) { await prepareWindows(); if (!runtime || quitting) return; }
    if (runtime.running) throw new Error('之前的内核仍在收尾，请等待它退出后重试。');
    startupCancellation = new AbortController();
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
    }, () => postgres.initialized);
    const [cover, kernel] = await Promise.allSettled([showing, preparing]);
    if (quitting) return;
    if (cover.status === 'rejected') throw cover.reason;
    if (kernel.status === 'rejected') throw kernel.reason;
    const url = kernel.value;
    const parsed = new URL(url);
    if (parsed.protocol !== (windowsHost ? 'https:' : 'http:') || parsed.hostname !== '127.0.0.1' || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) throw new Error('内核返回了无效的本地工作台地址。');
    if (windowsHost) {
      const pinned = createBackendSession({origin:parsed.origin, certificatePem:runtime.certificatePem!});
      backendSession = pinned;
      await replaceWindow(pinned.session);
      // Linux listening precedes WSL's Windows localhost forwarding. Keep the
      // startup owner and pinned Session until the host can reach that endpoint.
      try { await pinned.waitUntilReachable(startupCancellation?.signal); }
      catch (error) { if (quitting) return; throw error; }
    }
    if (quitting) return;
    origin = parsed.origin;
    startupError = '';
    startupFailure = undefined;
    try { await window!.loadURL(url); }
    catch (error) { if (!quitting) throw error; }
  })().catch(async error => {
    startupFailure = error;
    startupError = error instanceof Error ? error.message : String(error);
    if (!quitting) await showStartup(startupError, windowsHost && !runtime ? 'setup' : 'failed')
      .catch(retirement => dialog.showErrorBox('Pulsara 无法完成界面清理', `${startupError}\n${String(retirement)}`));
    // Leave an existing database untouched on failure; another old Python owner may still be closing.
  }).finally(() => { starting = undefined; startupCancellation = undefined; });
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
  const packageTarget = process.platform === 'linux' && resources.root.startsWith('/opt/')
    ? resources.packageCli : undefined;
  return {
    destination: packageTarget ? '/usr/bin/pulsara' : path.join(os.homedir(), '.local', 'bin', 'pulsara'),
    script: cliLauncherScript(resources.pythonExecutable), packageTarget, searchPath: process.env.PATH ?? '',
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
  if (!runtime) throw new Error('请先完成 WSL 接入。');
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
    if (!runtime) {
      quitting = true;
      startupCancellation?.abort();
      await setupPending?.catch(() => {});
      app.exit(0);
      return;
    }
    const work = await runtime.inspectQuit();
    if (work.error || work.activeTasks || work.scheduledTasks) {
      focusWindow();
      const choice = await dialog.showMessageBox(window!, {type:'question', message:'退出 Pulsara？', detail: work.error || '退出会停止当前工作及后台调度。关闭窗口可以继续在后台运行。', buttons:['继续后台运行','退出 Pulsara'], defaultId:0, cancelId:0});
      if (choice.response !== 1) return;
    }
    quitting = true;
    startupCancellation?.abort();
    await showStartup('正在停止并结算当前工作，等待内核退出…', 'stopping');
    await runtime.stop();
    if (starting) await starting;
    if ((startupFailure as {postgresExecution?: string} | undefined)?.postgresExecution === 'unknown') throw startupFailure;
    // If startup never owned a ready kernel and reused a database, don't stop another owner's instance.
    if (postgres.startedHere || (databaseReady && !startupError)) await postgres.stop();
    app.exit(0);
  } catch (error) {
    quitting = false;
    const message = `退出尚未完成：${error instanceof Error ? error.message : String(error)}`;
    await showStartup(message)
      .catch(retirement => dialog.showErrorBox('Pulsara 退出尚未完成', `${message}\n${String(retirement)}`));
  } finally { quittingRequest = false; }
}

const uninstallArgument = process.argv?.find(argument => argument.startsWith('--pulsara-uninstall='));
if (windowsHost && uninstallArgument) {
  void app.whenReady().then(() => uninstallWsl(app.getPath('userData'), app.getVersion(),
    uninstallArgument.slice('--pulsara-uninstall='.length), path.resolve(__dirname, '..', 'assets', 'wsl-uninstall.py'), process.resourcesPath))
    .then(() => app.exit(0), error => { console.error('WSL uninstall failed:', error instanceof Error ? error.message : String(error)); app.exit(1); });
} else if (windowsHost && process.argv?.includes('--pulsara-maintenance-check')) {
  void app.whenReady().then(() => checkInstalledWslMaintenance(app.getPath('userData'), app.getVersion()))
    .then(() => app.exit(0), error => { console.error('WSL maintenance admission refused:', error instanceof Error ? error.message : String(error)); app.exit(1); });
} else if (!app.requestSingleInstanceLock()) app.quit();
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
    createWindow();
    if (process.platform === 'linux' || windowsHost) {
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
    handle('retry',()=>boot(),true); handle('logs',()=>shell.openPath(windowsHost ? path.join(app.getPath('userData'),'logs') : path.join(home,'desktop')),true);
    if (windowsHost) {
      handle('wsl-discover', () => wslSetup!.discover(), true);
      handle('wsl-inspect', value => {
        if (typeof value !== 'string') throw new Error('请选择已有的 WSL 发行版。');
        return wslSetup!.inspect({distro:value});
      }, true);
      handle('wsl-select', async value => {
        if (runtime || setupPending || quitting || resetting) throw new Error('当前不能更换 WSL 目标。');
        if (!value || typeof value.distro !== 'string' || typeof value.user !== 'string') throw new Error('无效的 WSL 目标。');
        try { await prepareWindows({distro:value.distro, user:value.user}); await boot(); }
        catch (error) { await showStartup(error instanceof Error ? error.message : String(error), runtime ? 'failed' : 'setup'); throw error; }
      }, true);
      ipcMain.on('desktop:backend-path', (event, value) => {
        event.returnValue = '';
        try { trusted(event); if (wslTarget) event.returnValue = toBackendPath(value, wslTarget.distro); }
        catch { /* A Windows File remains an upload; no host path read is exposed. */ }
      });
    }
    handle('reset-database',()=>resetDatabase(),true);
    handle('read-ui-preferences', () => uiPreferences.read());
    handle('startup-settled', () => startupCover!.finish());
    handle('focus', focusWindow);
    handle('browser-create', value => browser!.create(value));
    handle('browser-command', value => browser!.command(value));
    handle('browser-close', value => browser!.close(value));
    handle('browser-clear', () => browser!.clear());
    handle('browser-activate', value => browser!.activate(value));
    handle('browser-layout', value => browser!.layout(value));
    handle('browser-hide', value => browser!.hide(value));
    handle('write-ui-preference', value => {
      uiPreferences.write(value);
      if (windowsHost && value.key === 'pulsara-theme') window!.setTitleBarOverlay(windowsControlsOverlay());
    });
    handle('cli-status',async()=>{
      if (windowsHost) {
        try { await host.execFile('/usr/bin/test', ['-x',resources.packageCli], {env:{PULSARA_HOME:home}}); return {state:'ready', path:resources.packageCli, managedBy:'wsl'}; }
        catch { return {state:'missing', path:resources.packageCli, managedBy:'wsl', message:'随包 WSL 命令入口不可用，请检查应用资源。'}; }
      }
      await cliRegistrationPending; return cliStatus(cliRegistration());
    });
    handle('repair-cli',value=>{ if (windowsHost) throw new Error('命令行入口位于 WSL 内，不修改 Windows PATH。'); if (typeof value !== 'boolean') throw new Error('无效的命令行入口操作。'); return ensureCli(value); });
    handle('write-clipboard-text', value => {
      if (typeof value !== 'string') throw new Error('剪贴板内容必须是文本。');
      return clipboard.writeText(value);
    });
    handle('choose-files',async()=>{const result=await dialog.showOpenDialog(window!,{properties:['openFile','multiSelections']});return result.canceled?[]:result.filePaths.map(value => windowsHost ? toBackendPath(value, wslTarget!.distro) : value);});
    handle('choose-workspace-directory', async () => {
      if (!windowsHost || !wslTarget) throw new Error('请先连接 WSL 环境。');
      const result = await dialog.showOpenDialog(window!, {
        defaultPath: toWindowsPath(wslTarget.home, wslTarget.distro), properties: ['openDirectory'],
      });
      if (result.canceled) return null;
      const selected = result.filePaths[0];
      // A foreign selection is display-only; Linux remains the admission owner.
      let linuxPath: string | null;
      try { linuxPath = toBackendPath(selected, wslTarget.distro); }
      catch { linuxPath = null; }
      return {path: selected, linuxPath};
    });
    handle('choose-directory', async () => {
      // Electron owns platform-specific desktop lookup (including Linux XDG).
      // A desktop folder is optional; never create one just to open the picker.
      let defaultPath = app.getPath('home');
      if (windowsHost) {
        const selected = await dialog.showOpenDialog(window!, {defaultPath:toWindowsPath(wslTarget!.home,wslTarget!.distro), properties:['openDirectory']});
        return selected.canceled ? null : toBackendPath(selected.filePaths[0],wslTarget!.distro);
      }
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
      if (windowsHost) {
        if (!wslTarget || !value || typeof value.path !== 'string' || typeof value.reveal !== 'boolean') throw new Error('无效的 Linux 文件操作。');
        const target = toWindowsPath(value.path, wslTarget.distro);
        if (value.reveal) { shell.showItemInFolder(target); return; }
        if (!(await stat(target)).isDirectory() && !/\.(?:png|jpe?g|gif|webp|avif|bmp|tiff?|pdf|docx?|xlsx?|pptx?|odt|ods|odp)$/i.test(value.path)) throw new Error('此文件类型不能通过预览直接启动。');
        const error = await shell.openPath(target); if (error) throw new Error(error);
        return;
      }
      if (!value || typeof value.path!=='string' || !path.isAbsolute(value.path) || typeof value.reveal!=='boolean') throw new Error('无效的本地文件操作。');
      if (value.reveal) {shell.showItemInFolder(value.path);return;}
      if (!(await stat(value.path)).isDirectory() && !/\.(?:png|jpe?g|gif|webp|avif|bmp|tiff?|pdf|docx?|xlsx?|pptx?|odt|ods|odp)$/i.test(value.path)) throw new Error('此文件类型不能通过预览直接启动。');
      const error=await shell.openPath(value.path);if(error)throw new Error(error);
    });
    handle('open-external',async value=>{if(typeof value!=='string')throw new Error('链接无效。');const target=new URL(value);if(!['https:','http:'].includes(target.protocol)||target.username||target.password)throw new Error('不支持此链接协议。');await shell.openExternal(target.href);},true);
    if (app.isPackaged && !windowsHost) void ensureCli().catch(error => console.error('CLI registration failed', error));
    await boot();
  });
}
