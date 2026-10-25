"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const electron_1 = require("electron");
const promises_1 = require("node:fs/promises");
const node_path_1 = __importDefault(require("node:path"));
const node_os_1 = __importDefault(require("node:os"));
const postgres_1 = require("./postgres");
const runtime_1 = require("./runtime");
const ui_preferences_1 = require("./ui-preferences");
const cli_install_1 = require("./cli-install");
const browser_owner_1 = require("./browser-owner");
const startup_cover_1 = require("./startup-cover");
const oauth_return_1 = require("./oauth-return");
const resource_paths_1 = require("./resource-paths");
const host_posix_1 = require("./host-posix");
const postgres_posix_1 = require("./postgres-posix");
const host_wsl_1 = require("./host-wsl");
const wsl_setup_1 = require("./wsl-setup");
const postgres_wsl_1 = require("./postgres-wsl");
const backend_paths_1 = require("./backend-paths");
const backend_session_1 = require("./backend-session");
const wsl_maintenance_1 = require("./wsl-maintenance");
const windowsHost = process.platform === 'win32';
const hostResourcesRoot = electron_1.app.isPackaged ? process.resourcesPath : node_path_1.default.resolve(__dirname, '..', 'resources');
let resources = (0, resource_paths_1.desktopResourcePaths)(hostResourcesRoot);
let host = host_posix_1.posixHostOperations;
function resolveHome() {
    try {
        return host.resolveHome(resources.pythonExecutable);
    }
    catch {
        electron_1.dialog.showErrorBox('Pulsara 无法启动', '无法解析应用数据目录或缺少随包 Python。请检查 PULSARA_HOME 与安装资源。');
        electron_1.app.exit(1);
        throw new Error('Desktop home unavailable');
    }
}
let home = windowsHost ? '' : resolveHome();
const startupFile = node_path_1.default.resolve(__dirname, '..', 'assets', 'startup.html');
const desktopIcon = node_path_1.default.resolve(__dirname, '..', 'assets', 'pulsara.png');
electron_1.app.setName('Pulsara');
if (!windowsHost)
    electron_1.app.setPath('userData', node_path_1.default.join(home, 'desktop', 'electron'));
const uiPreferences = new ui_preferences_1.UiPreferences(node_path_1.default.join(electron_1.app.getPath('userData'), 'ui-preferences.json'));
const wslSetup = windowsHost ? new wsl_setup_1.WslSetup(node_path_1.default.join(electron_1.app.getPath('userData'), 'wsl-selection.json')) : undefined;
let wslTarget;
let setupPending;
let startupCancellation;
let backendSession;
let windowTransition = Promise.resolve();
let window;
let tray;
let origin;
let browser;
let startupCover;
let starting;
let quitting = false;
let quittingRequest = false;
let resetting = false;
let databaseReady = false;
let startupError = '';
let startupFailure;
let postgres;
let runtime;
function configureRuntime(interopHelper) {
    postgres = new postgres_1.BundledPostgres(wslTarget
        ? (0, postgres_wsl_1.createWslPostgresOperations)(resources, home, host, () => startupCancellation?.signal)
        : (0, postgres_posix_1.createPosixPostgresOperations)(resources.postgresBin, home));
    runtime = new runtime_1.PythonRuntime(resources, home, event => {
        if ((event.type === 'failed' || event.type === 'exited') && !starting && !quitting && !resetting) {
            startupError = event.error ?? '内核不可用';
            void showStartup(startupError).catch(error => console.error('Could not display startup status', error));
        }
    }, host, wslTarget ? {
        distribution: wslTarget.distro, user: wslTarget.user, interopHelper: interopHelper,
        async openOAuthBrowser(url) {
            if (!runtime.running || !origin || quitting || resetting)
                throw new Error('工作台正在关闭，未打开授权页面。');
            await electron_1.shell.openExternal(url);
        },
    } : undefined);
}
if (!windowsHost)
    configureRuntime();
async function prepareWindows(selection) {
    if (runtime || !wslSetup)
        return;
    if (setupPending)
        return setupPending;
    setupPending = (async () => {
        const saved = selection ?? (await wslSetup.discover()).saved;
        if (!saved) {
            await showStartup('', 'setup');
            return;
        }
        await showStartup('正在检查 WSL 目标与 Linux 应用资源…', 'starting');
        const target = selection ? await wslSetup.select(selection) : await wslSetup.inspect(saved);
        const payload = await (0, wsl_setup_1.loadWslPayload)(hostResourcesRoot);
        if (payload.version !== electron_1.app.getVersion())
            throw new Error('随包 Linux 资源版本不匹配，请重新安装 Pulsara。');
        if (quitting)
            return;
        startupCancellation = new AbortController();
        const prepared = await wslSetup.prepare(target, payload, { signal: startupCancellation.signal });
        if (quitting)
            return;
        wslTarget = prepared.target;
        home = prepared.home;
        resources = prepared.resources;
        host = (0, host_wsl_1.createWslHostOperations)(wslTarget, home, node_path_1.default.join(electron_1.app.getPath('userData'), 'logs'));
        configureRuntime(prepared.hostHelperPath);
    })().finally(() => { setupPending = undefined; });
    return setupPending;
}
function focusWindow() { if (window) {
    if (window.isMinimized())
        window.restore();
    window.show();
    window.focus();
} }
function transitionWindow(action) {
    const pending = windowTransition.then(action);
    windowTransition = pending.then(() => { }, () => { });
    return pending;
}
async function showStartup(error = '', phase = error ? 'failed' : 'starting') {
    const { showing } = await transitionWindow(async () => {
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
        return { showing: startupCover?.show(error, phase) };
    });
    await showing;
    focusWindow();
}
async function replaceWindow(session) {
    const { showing } = await transitionWindow(async () => {
        const previous = window;
        const bounds = previous?.getBounds();
        const maximized = previous?.isMaximized();
        await destroyWindow();
        createWindow(session);
        if (bounds)
            window.setBounds(bounds);
        if (maximized)
            window.maximize();
        return { showing: startupCover.show() };
    });
    await showing;
}
async function destroyWindow() {
    await browser?.retireHost();
    await startupCover?.retire();
    if (!window || window.webContents.isDestroyed())
        return;
    const contents = window.webContents;
    const destroyed = new Promise(resolve => contents.once('destroyed', () => resolve()));
    window.destroy();
    await destroyed;
}
function createWindow(session) {
    window = new electron_1.BrowserWindow({ width: 1440, height: 920, minWidth: 760, minHeight: 520, backgroundColor: '#f3f0e7', title: 'Pulsara',
        ...(process.platform !== 'darwin' ? { icon: desktopIcon } : {}),
        titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'hidden', trafficLightPosition: { x: 16, y: 16 },
        webPreferences: { preload: node_path_1.default.join(__dirname, 'preload.js'), sandbox: true, contextIsolation: true, nodeIntegration: false,
            webviewTag: false, navigateOnDragDrop: false, ...(session ? { session } : {}) } });
    browser = new browser_owner_1.BrowserOwner(window, () => origin);
    startupCover = new startup_cover_1.StartupCover(window, startupFile, node_path_1.default.join(__dirname, 'preload.js'));
    window.webContents.setWindowOpenHandler(({ url }) => {
        if (/^https?:\/\//i.test(url))
            void electron_1.dialog.showMessageBox(window, { message: '在默认浏览器中打开链接？', detail: url, buttons: ['取消', '打开'], defaultId: 0, cancelId: 0 }).then(choice => { if (choice.response === 1)
                void electron_1.shell.openExternal(url); });
        return { action: 'deny' };
    });
    window.webContents.on('will-navigate', event => event.preventDefault());
    window.webContents.on('will-attach-webview', event => event.preventDefault());
    window.webContents.session.setPermissionRequestHandler((_contents, _permission, done) => done(false));
    window.on('close', event => {
        if (quitting)
            return;
        event.preventDefault();
        if (process.platform === 'darwin' || tray)
            window.hide();
        else
            void electron_1.dialog.showMessageBox(window, { message: '关闭 Pulsara', buttons: ['最小化', '退出'], defaultId: 0, cancelId: 0 }).then(result => result.response ? void requestQuit() : window.minimize());
    });
}
async function boot(fromReset = false) {
    if (quitting || (resetting && !fromReset))
        return;
    if (starting)
        return starting;
    starting = (async () => {
        if (windowsHost && !runtime) {
            await prepareWindows();
            if (!runtime || quitting)
                return;
        }
        if (runtime.running)
            throw new Error('之前的内核仍在收尾，请等待它退出后重试。');
        startupCancellation = new AbortController();
        // Loading the cover and waiting for the old owner need not delay Python
        // imports. The old owner still gates PostgreSQL, and both preparations gate
        // the start frame. Join every owner before navigation or displaying failure.
        const showing = showStartup();
        const preparing = runtime.start(async () => {
            await runtime.waitForPreviousOwner();
            if (quitting)
                throw new Error('Pulsara 正在退出。');
            const [cover, database] = await Promise.allSettled([showing, postgres.start()]);
            if (cover.status === 'rejected')
                throw cover.reason;
            if (database.status === 'rejected')
                throw database.reason;
            databaseReady = true;
            return postgres.settings();
        }, () => postgres.initialized);
        const [cover, kernel] = await Promise.allSettled([showing, preparing]);
        if (quitting)
            return;
        if (cover.status === 'rejected')
            throw cover.reason;
        if (kernel.status === 'rejected')
            throw kernel.reason;
        const url = kernel.value;
        const parsed = new URL(url);
        if (parsed.protocol !== (windowsHost ? 'https:' : 'http:') || parsed.hostname !== '127.0.0.1' || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash)
            throw new Error('内核返回了无效的本地工作台地址。');
        if (windowsHost) {
            const pinned = (0, backend_session_1.createBackendSession)({ origin: parsed.origin, certificatePem: runtime.certificatePem });
            backendSession = pinned;
            await replaceWindow(pinned.session);
            // Linux listening precedes WSL's Windows localhost forwarding. Keep the
            // startup owner and pinned Session until the host can reach that endpoint.
            try {
                await pinned.waitUntilReachable(startupCancellation?.signal);
            }
            catch (error) {
                if (quitting)
                    return;
                throw error;
            }
        }
        if (quitting)
            return;
        origin = parsed.origin;
        startupError = '';
        startupFailure = undefined;
        try {
            await window.loadURL(url);
        }
        catch (error) {
            if (!quitting)
                throw error;
        }
    })().catch(async (error) => {
        startupFailure = error;
        startupError = error instanceof Error ? error.message : String(error);
        if (!quitting)
            await showStartup(startupError, windowsHost && !runtime ? 'setup' : 'failed')
                .catch(retirement => electron_1.dialog.showErrorBox('Pulsara 无法完成界面清理', `${startupError}\n${String(retirement)}`));
        // Leave an existing database untouched on failure; another old Python owner may still be closing.
    }).finally(() => { starting = undefined; startupCancellation = undefined; });
    return starting;
}
function trusted(event, startupAllowed = false) {
    if (startupAllowed && startupCover?.accepts(event))
        return;
    if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame)
        throw new Error('无效的桌面请求来源。');
    const url = event.senderFrame.url;
    const isWorkbench = origin && new URL(url).origin === origin && new URL(url).pathname === '/';
    if (!isWorkbench)
        throw new Error('此页面不能调用桌面接口。');
}
function handle(name, action, startupAllowed = false) {
    electron_1.ipcMain.handle(`desktop:${name}`, (event, value) => { trusted(event, startupAllowed); return action(value); });
}
function cliRegistration() {
    const packageTarget = process.platform === 'linux' && resources.root.startsWith('/opt/')
        ? resources.packageCli : undefined;
    return {
        destination: packageTarget ? '/usr/bin/pulsara' : node_path_1.default.join(node_os_1.default.homedir(), '.local', 'bin', 'pulsara'),
        script: (0, cli_install_1.cliLauncherScript)(resources.pythonExecutable), packageTarget, searchPath: process.env.PATH ?? '',
    };
}
// Registration is best effort and independent of kernel/database startup.
let cliRegistrationPending;
async function ensureCli(replaceConflict = false) {
    const pending = (cliRegistrationPending ?? Promise.resolve()).then(() => (0, cli_install_1.registerCli)(cliRegistration(), replaceConflict));
    cliRegistrationPending = pending;
    try {
        return await pending;
    }
    finally {
        if (cliRegistrationPending === pending)
            cliRegistrationPending = undefined;
    }
}
async function resetDatabase() {
    if (!runtime)
        throw new Error('请先完成 WSL 接入。');
    if (starting || quitting || quittingRequest || resetting)
        throw new Error('请等待当前启动或关闭操作完成。');
    resetting = true;
    try {
        const choice = await electron_1.dialog.showMessageBox(window, { type: 'warning', message: '重置 Pulsara 专用数据库？', detail: `将永久删除会话、记忆和定时任务，并重建当前版本数据库。工作目录文件和模型配置保留。\n\n${postgres.directory}`, buttons: ['取消', '重置数据库'], defaultId: 0, cancelId: 0 });
        if (choice.response !== 1)
            return;
        await showStartup('正在停止内核并重置专用数据库…', 'resetting');
        await runtime.stop();
        await runtime.waitForPreviousOwner();
        await postgres.resetConfirmed();
        databaseReady = false;
        await boot(true);
    }
    catch (error) {
        await showStartup(`数据库重置未完成：${error instanceof Error ? error.message : String(error)}`);
    }
    finally {
        resetting = false;
    }
}
async function requestQuit() {
    if (quittingRequest || quitting || resetting)
        return;
    quittingRequest = true;
    try {
        if (!runtime) {
            quitting = true;
            startupCancellation?.abort();
            await setupPending?.catch(() => { });
            electron_1.app.exit(0);
            return;
        }
        const work = await runtime.inspectQuit();
        if (work.error || work.activeTasks || work.scheduledTasks) {
            focusWindow();
            const choice = await electron_1.dialog.showMessageBox(window, { type: 'question', message: '退出 Pulsara？', detail: work.error || '退出会停止当前工作及后台调度。关闭窗口可以继续在后台运行。', buttons: ['继续后台运行', '退出 Pulsara'], defaultId: 0, cancelId: 0 });
            if (choice.response !== 1)
                return;
        }
        quitting = true;
        startupCancellation?.abort();
        await showStartup('正在停止并结算当前工作，等待内核退出…', 'stopping');
        await runtime.stop();
        if (starting)
            await starting;
        if (startupFailure?.postgresExecution === 'unknown')
            throw startupFailure;
        // If startup never owned a ready kernel and reused a database, don't stop another owner's instance.
        if (postgres.startedHere || (databaseReady && !startupError))
            await postgres.stop();
        electron_1.app.exit(0);
    }
    catch (error) {
        quitting = false;
        const message = `退出尚未完成：${error instanceof Error ? error.message : String(error)}`;
        await showStartup(message)
            .catch(retirement => electron_1.dialog.showErrorBox('Pulsara 退出尚未完成', `${message}\n${String(retirement)}`));
    }
    finally {
        quittingRequest = false;
    }
}
if (windowsHost && process.argv?.includes('--pulsara-maintenance-check')) {
    void electron_1.app.whenReady().then(() => (0, wsl_maintenance_1.checkInstalledWslMaintenance)(electron_1.app.getPath('userData'), electron_1.app.getVersion()))
        .then(() => electron_1.app.exit(0), error => { console.error('WSL maintenance admission refused:', error instanceof Error ? error.message : String(error)); electron_1.app.exit(1); });
}
else if (!electron_1.app.requestSingleInstanceLock())
    electron_1.app.quit();
else {
    electron_1.app.on('second-instance', focusWindow);
    electron_1.app.on('open-url', (event, url) => {
        event.preventDefault();
        if ((0, oauth_return_1.isOAuthReturn)(url))
            focusWindow();
    });
    electron_1.app.on('activate', focusWindow);
    electron_1.app.on('before-quit', event => { event.preventDefault(); void requestQuit(); });
    electron_1.app.on('window-all-closed', () => { });
    void electron_1.app.whenReady().then(async () => {
        if (electron_1.app.isPackaged)
            electron_1.app.setAsDefaultProtocolClient('pulsara');
        createWindow();
        if (process.platform === 'linux' || windowsHost) {
            try {
                tray = new electron_1.Tray(electron_1.nativeImage.createFromPath(desktopIcon).resize({ width: 32, height: 32 }));
                tray.setToolTip('Pulsara');
                tray.setContextMenu(electron_1.Menu.buildFromTemplate([{ label: '打开 Pulsara', click: focusWindow }, { label: '退出 Pulsara', click: () => void requestQuit() }]));
                tray.on('click', focusWindow);
            }
            catch {
                tray = undefined;
            }
        }
        electron_1.Menu.setApplicationMenu(electron_1.Menu.buildFromTemplate([
            { label: 'Pulsara', submenu: [{ label: '关于 Pulsara', role: 'about' }, { label: '重置专用数据库…', click: () => void resetDatabase().catch(error => electron_1.dialog.showErrorBox('重置失败', String(error))) }, { type: 'separator' }, { label: '退出 Pulsara', accelerator: 'CommandOrControl+Q', click: () => void requestQuit() }] },
            { label: '文件', submenu: [{ label: '关闭窗口', role: 'close', accelerator: 'CommandOrControl+W' }] },
            { role: 'editMenu' }, { label: '显示', submenu: [{ role: 'reload' }, { role: 'togglefullscreen' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'resetZoom' }, { role: 'toggleDevTools' }] }, { role: 'windowMenu' },
        ]));
        handle('minimize', () => window.minimize(), true);
        handle('maximize', () => window.isMaximized() ? window.unmaximize() : window.maximize(), true);
        handle('close', () => window.close(), true);
        handle('quit', () => requestQuit(), true);
        handle('retry', () => boot(), true);
        handle('logs', () => electron_1.shell.openPath(windowsHost ? node_path_1.default.join(electron_1.app.getPath('userData'), 'logs') : node_path_1.default.join(home, 'desktop')), true);
        if (windowsHost) {
            handle('wsl-discover', () => wslSetup.discover(), true);
            handle('wsl-inspect', value => {
                if (typeof value !== 'string')
                    throw new Error('请选择已有的 WSL 发行版。');
                return wslSetup.inspect({ distro: value });
            }, true);
            handle('wsl-select', async (value) => {
                if (runtime || setupPending || quitting || resetting)
                    throw new Error('当前不能更换 WSL 目标。');
                if (!value || typeof value.distro !== 'string' || typeof value.user !== 'string')
                    throw new Error('无效的 WSL 目标。');
                try {
                    await prepareWindows({ distro: value.distro, user: value.user });
                    await boot();
                }
                catch (error) {
                    await showStartup(error instanceof Error ? error.message : String(error), runtime ? 'failed' : 'setup');
                    throw error;
                }
            }, true);
            electron_1.ipcMain.on('desktop:backend-path', (event, value) => {
                event.returnValue = '';
                try {
                    trusted(event);
                    if (wslTarget)
                        event.returnValue = (0, backend_paths_1.toBackendPath)(value, wslTarget.distro);
                }
                catch { /* A Windows File remains an upload; no host path read is exposed. */ }
            });
        }
        handle('reset-database', () => resetDatabase(), true);
        handle('read-ui-preferences', () => uiPreferences.read());
        handle('startup-settled', () => startupCover.finish());
        handle('focus', focusWindow);
        handle('browser-create', value => browser.create(value));
        handle('browser-command', value => browser.command(value));
        handle('browser-close', value => browser.close(value));
        handle('browser-forget-session', value => browser.forgetSession(value));
        handle('browser-clear', () => browser.clear());
        handle('browser-activate', value => browser.activate(value));
        handle('browser-layout', value => browser.layout(value));
        handle('browser-hide', value => browser.hide(value));
        handle('write-ui-preference', value => uiPreferences.write(value));
        handle('cli-status', async () => {
            if (windowsHost) {
                try {
                    await host.execFile('/usr/bin/test', ['-x', resources.packageCli], { env: { PULSARA_HOME: home } });
                    return { state: 'ready', path: resources.packageCli, managedBy: 'wsl' };
                }
                catch {
                    return { state: 'missing', path: resources.packageCli, managedBy: 'wsl', message: '随包 WSL 命令入口不可用，请检查应用资源。' };
                }
            }
            await cliRegistrationPending;
            return (0, cli_install_1.cliStatus)(cliRegistration());
        });
        handle('repair-cli', value => { if (windowsHost)
            throw new Error('命令行入口位于 WSL 内，不修改 Windows PATH。'); if (typeof value !== 'boolean')
            throw new Error('无效的命令行入口操作。'); return ensureCli(value); });
        handle('write-clipboard-text', value => {
            if (typeof value !== 'string')
                throw new Error('剪贴板内容必须是文本。');
            return electron_1.clipboard.writeText(value);
        });
        handle('choose-files', async () => { const result = await electron_1.dialog.showOpenDialog(window, { properties: ['openFile', 'multiSelections'] }); return result.canceled ? [] : result.filePaths.map(value => windowsHost ? (0, backend_paths_1.toBackendPath)(value, wslTarget.distro) : value); });
        handle('choose-directory', async () => {
            // Electron owns platform-specific desktop lookup (including Linux XDG).
            // A desktop folder is optional; never create one just to open the picker.
            let defaultPath = electron_1.app.getPath('home');
            if (windowsHost) {
                const selected = await electron_1.dialog.showOpenDialog(window, { defaultPath: (0, backend_paths_1.toWindowsPath)(node_path_1.default.posix.join(wslTarget.home, 'workspaces'), wslTarget.distro), properties: ['openDirectory'] });
                return selected.canceled ? null : (0, backend_paths_1.toBackendPath)(selected.filePaths[0], wslTarget.distro);
            }
            try {
                const desktop = electron_1.app.getPath('desktop');
                if ((await (0, promises_1.stat)(desktop)).isDirectory())
                    defaultPath = desktop;
            }
            catch { /* Missing/unavailable desktop: start at the user's home. */ }
            const result = await electron_1.dialog.showOpenDialog(window, {
                defaultPath, properties: ['openDirectory', 'createDirectory'],
            });
            return result.canceled ? null : result.filePaths[0];
        });
        handle('open-path', async (value) => {
            if (windowsHost) {
                if (!wslTarget || !value || typeof value.path !== 'string' || typeof value.reveal !== 'boolean')
                    throw new Error('无效的 Linux 文件操作。');
                const target = (0, backend_paths_1.toWindowsPath)(value.path, wslTarget.distro);
                if (value.reveal) {
                    electron_1.shell.showItemInFolder(target);
                    return;
                }
                if (!(await (0, promises_1.stat)(target)).isDirectory() && !/\.(?:png|jpe?g|gif|webp|avif|bmp|tiff?|pdf|docx?|xlsx?|pptx?|odt|ods|odp)$/i.test(value.path))
                    throw new Error('此文件类型不能通过预览直接启动。');
                const error = await electron_1.shell.openPath(target);
                if (error)
                    throw new Error(error);
                return;
            }
            if (!value || typeof value.path !== 'string' || !node_path_1.default.isAbsolute(value.path) || typeof value.reveal !== 'boolean')
                throw new Error('无效的本地文件操作。');
            if (value.reveal) {
                electron_1.shell.showItemInFolder(value.path);
                return;
            }
            if (!(await (0, promises_1.stat)(value.path)).isDirectory() && !/\.(?:png|jpe?g|gif|webp|avif|bmp|tiff?|pdf|docx?|xlsx?|pptx?|odt|ods|odp)$/i.test(value.path))
                throw new Error('此文件类型不能通过预览直接启动。');
            const error = await electron_1.shell.openPath(value.path);
            if (error)
                throw new Error(error);
        });
        handle('open-external', async (value) => { if (typeof value !== 'string')
            throw new Error('链接无效。'); const target = new URL(value); if (!['https:', 'http:'].includes(target.protocol) || target.username || target.password)
            throw new Error('不支持此链接协议。'); await electron_1.shell.openExternal(target.href); }, true);
        if (electron_1.app.isPackaged && !windowsHost)
            void ensureCli().catch(error => console.error('CLI registration failed', error));
        await boot();
    });
}
