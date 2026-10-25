"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.BrowserOwner = void 0;
const electron_1 = require("electron");
const node_crypto_1 = require("node:crypto");
const browser_policy_1 = require("./browser-policy");
function identity(value) {
    if (typeof value !== 'string' || !value.trim())
        throw new Error('缺少网页实例身份。');
}
/** Owns only native browser resources belonging to this trusted workbench. */
class BrowserOwner {
    host;
    origin;
    emit;
    partitions = new Map();
    pages = new Map();
    presentation = null;
    accepting = true;
    constructor(host, origin, emit = event => {
        if (!host.webContents.isDestroyed())
            host.webContents.send('desktop:browser-event', event);
    }) {
        this.host = host;
        this.origin = origin;
        this.emit = emit;
        host.webContents.on('did-start-navigation', (_event, _url, inPlace, main) => { if (main && !inPlace)
            void this.retireHost(); });
        host.webContents.on('did-finish-load', () => {
            try {
                const url = new URL(host.webContents.getURL());
                this.accepting = url.origin === this.origin() && url.pathname === '/';
            }
            catch {
                this.accepting = false;
            }
        });
        host.webContents.on('render-process-gone', () => { void this.retireHost(); });
        host.once('closed', () => { void this.retireHost(); });
    }
    partition(sessionId) {
        const existing = this.partitions.get(sessionId);
        if (existing)
            return existing;
        const origin = this.origin();
        if (!origin)
            throw new Error('工作台尚未就绪。');
        const part = { sessionId, session: electron_1.session.fromPartition(`pulsara-browser-${(0, node_crypto_1.randomUUID)()}`), port: (0, browser_policy_1.networkPort)(new URL(origin)), retired: false };
        const ses = part.session;
        ses.setPermissionRequestHandler((_wc, _permission, done) => done(false));
        ses.setPermissionCheckHandler(() => false);
        ses.setDevicePermissionHandler(() => false);
        ses.setDisplayMediaRequestHandler((_request, done) => done({}));
        ses.setBluetoothPairingHandler((_details, done) => done({ confirmed: false }));
        ses.webRequest.onBeforeRequest((details, done) => done({ cancel: !(0, browser_policy_1.allowBrowserRequest)(details.url, details.resourceType, part.port, part.retired) }));
        ses.on('will-download', (event, _item, wc) => {
            event.preventDefault();
            const page = [...this.pages.values()].find(page => page.live && page.view.webContents === wc);
            if (page)
                this.notice(page, '内嵌网页不支持下载；可在系统浏览器中继续。');
        });
        this.partitions.set(sessionId, part);
        return part;
    }
    create(input) {
        identity(input?.sessionId);
        identity(input?.tabId);
        if (!this.accepting || !this.origin())
            throw new Error('工作台正在切换，不能创建网页。');
        if ([...this.pages.values()].some(page => page.state.sessionId === input.sessionId && page.state.tabId === input.tabId))
            throw new Error('网页标签已有原生实例。');
        const part = this.partition(input.sessionId);
        const view = new electron_1.WebContentsView({ webPreferences: { session: part.session, sandbox: true, contextIsolation: true,
                nodeIntegration: false, nodeIntegrationInWorker: false, nodeIntegrationInSubFrames: false, webSecurity: true,
                webviewTag: false, plugins: false, allowRunningInsecureContent: false, navigateOnDragDrop: false, disableDialogs: true } });
        const page = { partition: part, view, live: true, visible: false, navigation: null, firstCommit: true,
            state: { instanceId: (0, node_crypto_1.randomUUID)(), sessionId: input.sessionId, tabId: input.tabId, url: '', pendingUrl: null,
                title: '', loading: false, canGoBack: false, canGoForward: false, error: null, crashed: false } };
        this.pages.set(page.state.instanceId, page);
        try {
            view.setVisible(false);
            this.listen(page);
            this.host.contentView.addChildView(view);
            return { ...page.state };
        }
        catch (error) {
            this.close(page.state.instanceId);
            throw error;
        }
    }
    active(page) { return page.live && this.pages.get(page.state.instanceId) === page && !page.partition.retired && !page.view.webContents.isDestroyed(); }
    publish(page) {
        if (!this.active(page))
            return;
        const wc = page.view.webContents;
        page.state.canGoBack = wc.navigationHistory.canGoBack();
        page.state.canGoForward = wc.navigationHistory.canGoForward();
        this.emit({ kind: 'state', state: { ...page.state } });
    }
    notice(page, message) { if (this.active(page))
        this.emit({ kind: 'notice', instanceId: page.state.instanceId, message }); }
    begin(page, url, awaitingStart = false) {
        const op = { url, stopped: false, awaitingStart, programmatic: awaitingStart };
        page.navigation = op;
        page.state.pendingUrl = url;
        page.state.loading = true;
        page.state.error = null;
        return op;
    }
    failure(page, op, url, message, code) {
        if (!this.active(page) || page.navigation !== op || op.stopped || code === -3)
            return;
        page.state.error = { url, message };
        page.state.loading = false;
        page.state.pendingUrl = null;
        this.publish(page);
    }
    listen(page) {
        const wc = page.view.webContents;
        wc.setWindowOpenHandler(details => {
            if (!this.active(page))
                return { action: 'deny' };
            try {
                if (details.postBody || (details.frameName && details.frameName !== '_blank'))
                    throw new Error('unsupported popup');
                const url = (0, browser_policy_1.browserURL)(details.url, page.partition.port);
                this.emit({ kind: 'popup', instanceId: page.state.instanceId, sessionId: page.state.sessionId, url, disposition: details.disposition });
            }
            catch {
                this.notice(page, '此新窗口流程暂不支持；可在系统浏览器中继续。');
            }
            return { action: 'deny' };
        });
        wc.on('will-attach-webview', event => event.preventDefault());
        wc.on('will-frame-navigate', event => {
            const allowed = event.isMainFrame ? this.validURL(page, event.url) : (0, browser_policy_1.allowBrowserRequest)(event.url, 'subFrame', page.partition.port, page.partition.retired);
            if (!allowed) {
                event.preventDefault();
                this.notice(page, '此地址不能在内嵌网页中打开。');
            }
        });
        wc.on('will-redirect', (event, url, _inPlace, main) => {
            if (!(main ? this.validURL(page, url) : (0, browser_policy_1.allowBrowserRequest)(url, 'subFrame', page.partition.port, page.partition.retired)))
                event.preventDefault();
        });
        wc.on('login', (event, _details, _auth, done) => { event.preventDefault(); done(); this.notice(page, '内嵌网页不支持 HTTP 身份验证；可在系统浏览器中继续。'); });
        wc.on('before-input-event', (event, input) => {
            if (!this.active(page) || !page.visible || input.type !== 'keyDown' || input.isAutoRepeat || input.isComposing || input.alt || input.shift || !(process.platform === 'darwin' ? input.meta : input.control))
                return;
            const key = input.key.toLowerCase();
            if (key === 'k' || key === 'n') {
                event.preventDefault();
                // Leave the guest before sending the application command: renderer IPC
                // and React rendering must not let immediately following text reach it.
                // Visibility remains presentation-owned, so a rejected/no-op command
                // cannot strand the native page hidden.
                this.host.webContents.focus();
                this.emit({ kind: 'shortcut', command: key === 'k' ? 'search' : 'new-session' });
            }
        });
        wc.on('did-start-navigation', (_event, url, inPlace, main) => {
            if (!main || !this.active(page))
                return;
            if (inPlace)
                return;
            if (page.navigation?.awaitingStart && page.navigation.url === url)
                page.navigation.awaitingStart = false;
            else
                this.begin(page, url);
            this.publish(page);
        });
        wc.on('did-redirect-navigation', (_event, url, _inPlace, main) => {
            if (main && this.active(page) && page.navigation) {
                page.navigation.url = url;
                page.state.pendingUrl = url;
                this.publish(page);
            }
        });
        const commit = (url) => {
            if (!this.active(page) || !this.validURL(page, url))
                return;
            page.state.url = url;
            page.state.pendingUrl = null;
            page.state.error = null;
            if (page.firstCommit) {
                page.firstCommit = false;
                const history = wc.navigationHistory;
                if (history.getActiveIndex() > 0 && history.getEntryAtIndex(0)?.url === 'about:blank')
                    history.removeEntryAtIndex(0);
            }
            this.publish(page);
        };
        wc.on('did-navigate', (_event, url) => commit(url));
        wc.on('did-navigate-in-page', (_event, url, main) => { if (main)
            commit(url); });
        wc.on('page-title-updated', (_event, title) => { if (this.active(page)) {
            page.state.title = title;
            this.publish(page);
        } });
        wc.on('did-start-loading', () => { if (this.active(page)) {
            page.state.loading = true;
            this.publish(page);
        } });
        wc.on('did-stop-loading', () => { if (this.active(page)) {
            page.state.loading = false;
            page.state.pendingUrl = null;
            this.publish(page);
        } });
        wc.on('did-fail-load', (_event, code, description, url, main) => {
            const op = page.navigation;
            // loadURL has an operation-bound Promise. Its older failure event can arrive
            // during a newer same-URL request, so only native navigations use this event.
            if (main && op && !op.programmatic && op.url === url)
                this.failure(page, op, url, description, code);
        });
        wc.on('render-process-gone', (_event, details) => {
            if (!this.active(page))
                return;
            const state = { ...page.state, crashed: true, loading: false, pendingUrl: null, error: { url: page.state.pendingUrl || page.state.url, message: `网页进程已退出：${details.reason}` } };
            this.close(page.state.instanceId);
            this.emit({ kind: 'state', state });
        });
        wc.once('destroyed', () => { if (page.live)
            this.close(page.state.instanceId); });
    }
    validURL(page, url) { try {
        (0, browser_policy_1.browserURL)(url, page.partition.port);
        return !page.partition.retired;
    }
    catch {
        return false;
    } }
    command(input) {
        identity(input?.instanceId);
        const page = this.pages.get(input.instanceId);
        if (!page || !this.active(page))
            return;
        const wc = page.view.webContents;
        if (input.kind === 'focus') {
            if (page.visible && this.presentation?.instanceId === input.instanceId)
                wc.focus();
            return;
        }
        if (input.kind === 'stop') {
            if (page.navigation)
                page.navigation.stopped = true;
            wc.stop();
            page.state.loading = false;
            page.state.pendingUrl = null;
            this.publish(page);
            return;
        }
        if (input.kind === 'navigate') {
            const url = (0, browser_policy_1.browserURL)(input.url, page.partition.port);
            const op = this.begin(page, url, true);
            this.publish(page);
            void wc.loadURL(url).catch((error) => this.failure(page, op, op.url, error.message, error.errno));
            return;
        }
        if (input.kind === 'back' || input.kind === 'forward') {
            const history = wc.navigationHistory;
            const offset = input.kind === 'back' ? -1 : 1;
            if (!history.canGoToOffset(offset))
                return;
            const target = history.getEntryAtIndex(history.getActiveIndex() + offset);
            if (target)
                (0, browser_policy_1.browserURL)(target.url, page.partition.port);
            if (offset < 0)
                history.goBack();
            else
                history.goForward();
            return;
        }
        if (input.kind === 'reload') {
            if (page.state.url) {
                (0, browser_policy_1.browserURL)(wc.getURL(), page.partition.port);
                wc.reload();
            }
            return;
        }
        throw new Error('不支持的网页命令。');
    }
    activate(input) {
        identity(input?.presentationId);
        if (input.instanceId !== null)
            identity(input.instanceId);
        for (const page of this.pages.values())
            this.hidePage(page);
        this.presentation = { ...input };
    }
    matches(input) {
        if (this.presentation?.instanceId !== input.instanceId || this.presentation.presentationId !== input.presentationId)
            return;
        const page = this.pages.get(input.instanceId);
        return page && this.active(page) ? page : undefined;
    }
    layout(input) {
        const page = this.matches(input);
        if (!page)
            return;
        if (typeof input.visible !== 'boolean')
            throw new Error('无效的网页可见性。');
        if (!input.visible) {
            this.hidePage(page);
            return;
        }
        const bounds = (0, browser_policy_1.browserBounds)(input.bounds, this.host.webContents.getZoomFactor());
        page.view.setBounds(bounds);
        page.view.setVisible(true);
        page.visible = true;
    }
    hide(input) {
        const page = this.matches(input);
        if (!page)
            return;
        this.hidePage(page);
    }
    hidePage(page) {
        page.visible = false;
        if (!page.view.webContents.isDestroyed()) {
            if (page.view.webContents.isFocused() && !this.host.webContents.isDestroyed())
                this.host.webContents.focus();
            page.view.setVisible(false);
        }
    }
    close(instanceId) {
        identity(instanceId);
        const page = this.pages.get(instanceId);
        if (!page)
            return;
        page.live = false;
        page.navigation = null;
        this.hidePage(page);
        this.pages.delete(instanceId);
        if (this.presentation?.instanceId === instanceId)
            this.presentation = null;
        if (!this.host.isDestroyed())
            this.host.contentView.removeChildView(page.view);
        const wc = page.view.webContents;
        if (!wc.isDestroyed()) {
            wc.once('destroyed', () => wc.removeAllListeners());
            wc.close({ waitForBeforeUnload: false });
        }
        else
            wc.removeAllListeners();
    }
    async forgetSession(sessionId) {
        identity(sessionId);
        const part = this.partitions.get(sessionId);
        if (!part)
            return;
        part.retired = true;
        this.partitions.delete(sessionId);
        for (const page of [...this.pages.values()])
            if (page.partition === part)
                this.close(page.state.instanceId);
        try {
            await part.session.clearData();
        }
        catch (error) {
            console.error('Could not clear retired browser partition', error);
        }
    }
    async clear() {
        this.presentation = null;
        const parts = [...this.partitions.values()];
        for (const part of parts)
            part.retired = true;
        await Promise.all(parts.map(part => this.forgetSession(part.sessionId)));
    }
    retireHost() {
        this.accepting = false;
        return this.clear();
    }
}
exports.BrowserOwner = BrowserOwner;
