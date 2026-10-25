import { session, WebContentsView, type BrowserWindow, type Session, type WebContents } from 'electron';
import { randomUUID } from 'node:crypto';
import type { BrowserCommand, BrowserEvent, BrowserHide, BrowserLayout, BrowserPresentation, BrowserState } from './browser-contract';
import { allowBrowserRequest, browserBounds, browserURL, networkPort } from './browser-policy';

interface Partition { sessionId: string; session: Session; port: string; retired: boolean; contents: Map<WebContents, Promise<void>>; retiring?: Promise<void> }
interface Navigation { url: string; stopped: boolean; awaitingStart: boolean; programmatic: boolean }
interface Page {
  state: BrowserState;
  partition: Partition;
  view: WebContentsView;
  live: boolean;
  visible: boolean;
  navigation: Navigation | null;
  firstCommit: boolean;
}
function identity(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !value.trim()) throw new Error('缺少网页实例身份。');
}

/** Owns only native browser resources belonging to this trusted workbench. */
export class BrowserOwner {
  private partitions = new Map<string, Partition>();
  private pages = new Map<string, Page>();
  private presentation: BrowserPresentation | null = null;
  private accepting = true;

  constructor(private host: BrowserWindow, private origin: () => string | undefined,
    private emit: (event: BrowserEvent) => void = event => {
      if (!host.webContents.isDestroyed()) host.webContents.send('desktop:browser-event', event);
    }) {
    host.webContents.on('did-start-navigation', (_event, _url, inPlace, main) => { if (main && !inPlace) void this.retireHost().catch(error => console.error('Could not retire browser resources', error)); });
    host.webContents.on('did-finish-load', () => {
      try { const url = new URL(host.webContents.getURL()); this.accepting = url.origin === this.origin() && url.pathname === '/'; }
      catch { this.accepting = false; }
    });
    host.webContents.on('render-process-gone', () => { void this.retireHost().catch(error => console.error('Could not retire browser resources', error)); });
    host.once('closed', () => { void this.retireHost().catch(error => console.error('Could not retire browser resources', error)); });
  }

  private partition(sessionId: string): Partition {
    const existing = this.partitions.get(sessionId);
    if (existing) {
      if (existing.retired) throw new Error('网页会话仍在关闭，请等待其结算。');
      return existing;
    }
    const origin = this.origin();
    if (!origin) throw new Error('工作台尚未就绪。');
    const part: Partition = { sessionId, session: session.fromPartition(`pulsara-browser-${randomUUID()}`), port: networkPort(new URL(origin)), retired: false, contents: new Map() };
    const ses = part.session;
    ses.setPermissionRequestHandler((_wc, _permission, done) => done(false));
    ses.setPermissionCheckHandler(() => false);
    ses.setDevicePermissionHandler(() => false);
    ses.setDisplayMediaRequestHandler((_request, done) => done({}));
    ses.setBluetoothPairingHandler((_details, done) => done({ confirmed: false }));
    ses.webRequest.onBeforeRequest((details, done) => done({ cancel: !allowBrowserRequest(details.url, details.resourceType, part.port, part.retired) }));
    ses.on('will-download', (event, _item, wc) => {
      event.preventDefault();
      const page = [...this.pages.values()].find(page => page.live && page.view.webContents === wc);
      if (page) this.notice(page, '内嵌网页不支持下载；可在系统浏览器中继续。');
    });
    this.partitions.set(sessionId, part);
    return part;
  }

  create(input: { sessionId: string; tabId: string }): BrowserState {
    identity(input?.sessionId); identity(input?.tabId);
    if (!this.accepting || !this.origin()) throw new Error('工作台正在切换，不能创建网页。');
    if ([...this.pages.values()].some(page => page.state.sessionId === input.sessionId && page.state.tabId === input.tabId)) throw new Error('网页标签已有原生实例。');
    const part = this.partition(input.sessionId);
    const view = new WebContentsView({ webPreferences: { session: part.session, sandbox: true, contextIsolation: true,
      nodeIntegration: false, nodeIntegrationInWorker: false, nodeIntegrationInSubFrames: false, webSecurity: true,
      webviewTag: false, plugins: false, allowRunningInsecureContent: false, navigateOnDragDrop: false, disableDialogs: true } });
    const page: Page = { partition: part, view, live: true, visible: false, navigation: null, firstCommit: true,
      state: { instanceId: randomUUID(), sessionId: input.sessionId, tabId: input.tabId, url: '', pendingUrl: null,
        title: '', loading: false, canGoBack: false, canGoForward: false, error: null, crashed: false } };
    this.pages.set(page.state.instanceId, page);
    const wc = view.webContents;
    part.contents.set(wc, new Promise(resolve => wc.once('destroyed', () => {
      part.contents.delete(wc);
      resolve();
    })));
    try {
      view.setVisible(false);
      this.listen(page);
      this.host.contentView.addChildView(view);
      return { ...page.state };
    } catch (error) { this.close(page.state.instanceId); throw error; }
  }

  private active(page: Page): boolean { return page.live && this.pages.get(page.state.instanceId) === page && !page.partition.retired && !page.view.webContents.isDestroyed(); }
  private publish(page: Page) {
    if (!this.active(page)) return;
    const wc = page.view.webContents;
    page.state.canGoBack = wc.navigationHistory.canGoBack();
    page.state.canGoForward = wc.navigationHistory.canGoForward();
    this.emit({ kind: 'state', state: { ...page.state } });
  }
  private notice(page: Page, message: string) { if (this.active(page)) this.emit({ kind: 'notice', instanceId: page.state.instanceId, message }); }
  private begin(page: Page, url: string, awaitingStart = false): Navigation {
    const op = { url, stopped: false, awaitingStart, programmatic: awaitingStart };
    page.navigation = op; page.state.pendingUrl = url; page.state.loading = true; page.state.error = null;
    return op;
  }
  private failure(page: Page, op: Navigation, url: string, message: string, code?: number) {
    if (!this.active(page) || page.navigation !== op || op.stopped || code === -3) return;
    page.state.error = { url, message }; page.state.loading = false; page.state.pendingUrl = null; this.publish(page);
  }
  private listen(page: Page) {
    const wc = page.view.webContents;
    wc.setWindowOpenHandler(details => {
      if (!this.active(page)) return { action: 'deny' };
      try {
        if (details.postBody || (details.frameName && details.frameName !== '_blank')) throw new Error('unsupported popup');
        const url = browserURL(details.url, page.partition.port);
        this.emit({ kind: 'popup', instanceId: page.state.instanceId, sessionId: page.state.sessionId, url, disposition: details.disposition });
      } catch { this.notice(page, '此新窗口流程暂不支持；可在系统浏览器中继续。'); }
      return { action: 'deny' };
    });
    wc.on('will-attach-webview', event => event.preventDefault());
    wc.on('will-frame-navigate', event => {
      const allowed = event.isMainFrame ? this.validURL(page, event.url) : allowBrowserRequest(event.url, 'subFrame', page.partition.port, page.partition.retired);
      if (!allowed) { event.preventDefault(); this.notice(page, '此地址不能在内嵌网页中打开。'); }
    });
    wc.on('will-redirect', (event, url, _inPlace, main) => {
      if (!(main ? this.validURL(page, url) : allowBrowserRequest(url, 'subFrame', page.partition.port, page.partition.retired))) event.preventDefault();
    });
    wc.on('login', (event, _details, _auth, done) => { event.preventDefault(); done(); this.notice(page, '内嵌网页不支持 HTTP 身份验证；可在系统浏览器中继续。'); });
    wc.on('before-input-event', (event, input) => {
      if (!this.active(page) || !page.visible || input.type !== 'keyDown' || input.isAutoRepeat || input.isComposing || input.alt || input.shift || !(process.platform === 'darwin' ? input.meta : input.control)) return;
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
      if (!main || !this.active(page)) return;
      if (inPlace) return;
      if (page.navigation?.awaitingStart && page.navigation.url === url) page.navigation.awaitingStart = false;
      else this.begin(page, url);
      this.publish(page);
    });
    wc.on('did-redirect-navigation', (_event, url, _inPlace, main) => {
      if (main && this.active(page) && page.navigation) { page.navigation.url = url; page.state.pendingUrl = url; this.publish(page); }
    });
    const commit = (url: string) => {
      if (!this.active(page) || !this.validURL(page, url)) return;
      page.state.url = url; page.state.pendingUrl = null; page.state.error = null;
      if (page.firstCommit) {
        page.firstCommit = false;
        const history = wc.navigationHistory;
        if (history.getActiveIndex() > 0 && history.getEntryAtIndex(0)?.url === 'about:blank') history.removeEntryAtIndex(0);
      }
      this.publish(page);
    };
    wc.on('did-navigate', (_event, url) => commit(url));
    wc.on('did-navigate-in-page', (_event, url, main) => { if (main) commit(url); });
    wc.on('page-title-updated', (_event, title) => { if (this.active(page)) { page.state.title = title; this.publish(page); } });
    wc.on('did-start-loading', () => { if (this.active(page)) { page.state.loading = true; this.publish(page); } });
    wc.on('did-stop-loading', () => { if (this.active(page)) { page.state.loading = false; page.state.pendingUrl = null; this.publish(page); } });
    wc.on('did-fail-load', (_event, code, description, url, main) => {
      const op = page.navigation;
      // loadURL has an operation-bound Promise. Its older failure event can arrive
      // during a newer same-URL request, so only native navigations use this event.
      if (main && op && !op.programmatic && op.url === url) this.failure(page, op, url, description, code);
    });
    wc.on('render-process-gone', (_event, details) => {
      if (!this.active(page)) return;
      const state = { ...page.state, crashed: true, loading: false, pendingUrl: null, error: { url: page.state.pendingUrl || page.state.url, message: `网页进程已退出：${details.reason}` } };
      this.close(page.state.instanceId);
      this.emit({ kind: 'state', state });
    });
    wc.once('destroyed', () => { if (page.live) this.close(page.state.instanceId); });
  }
  private validURL(page: Page, url: string): boolean { try { browserURL(url, page.partition.port); return !page.partition.retired; } catch { return false; } }

  command(input: BrowserCommand): void {
    identity(input?.instanceId);
    const page = this.pages.get(input.instanceId);
    if (!page || !this.active(page)) return;
    const wc = page.view.webContents;
    if (input.kind === 'focus') { if (page.visible && this.presentation?.instanceId === input.instanceId) wc.focus(); return; }
    if (input.kind === 'stop') {
      if (page.navigation) page.navigation.stopped = true;
      wc.stop(); page.state.loading = false; page.state.pendingUrl = null; this.publish(page); return;
    }
    if (input.kind === 'navigate') {
      const url = browserURL(input.url, page.partition.port);
      const op = this.begin(page, url, true); this.publish(page);
      void wc.loadURL(url).catch((error: Error & { errno?: number }) => this.failure(page, op, op.url, error.message, error.errno));
      return;
    }
    if (input.kind === 'back' || input.kind === 'forward') {
      const history = wc.navigationHistory;
      const offset = input.kind === 'back' ? -1 : 1;
      if (!history.canGoToOffset(offset)) return;
      const target = history.getEntryAtIndex(history.getActiveIndex() + offset);
      if (target) browserURL(target.url, page.partition.port);
      if (offset < 0) history.goBack(); else history.goForward();
      return;
    }
    if (input.kind === 'reload') { if (page.state.url) { browserURL(wc.getURL(), page.partition.port); wc.reload(); } return; }
    throw new Error('不支持的网页命令。');
  }

  activate(input: BrowserPresentation): void {
    identity(input?.presentationId);
    if (input.instanceId !== null) identity(input.instanceId);
    for (const page of this.pages.values()) this.hidePage(page);
    this.presentation = { ...input };
  }
  private matches(input: { instanceId: string; presentationId: string }): Page | undefined {
    if (this.presentation?.instanceId !== input.instanceId || this.presentation.presentationId !== input.presentationId) return;
    const page = this.pages.get(input.instanceId);
    return page && this.active(page) ? page : undefined;
  }
  layout(input: BrowserLayout): void {
    const page = this.matches(input);
    if (!page) return;
    if (typeof input.visible !== 'boolean') throw new Error('无效的网页可见性。');
    if (!input.visible) { this.hidePage(page); return; }
    const bounds = browserBounds(input.bounds, this.host.webContents.getZoomFactor());
    page.view.setBounds(bounds); page.view.setVisible(true); page.visible = true;
  }
  hide(input: BrowserHide): void {
    const page = this.matches(input);
    if (!page) return;
    this.hidePage(page);
  }
  private hidePage(page: Page) {
    page.visible = false;
    if (!page.view.webContents.isDestroyed()) {
      if (page.view.webContents.isFocused() && !this.host.webContents.isDestroyed()) this.host.webContents.focus();
      page.view.setVisible(false);
    }
  }
  close(instanceId: string): void {
    identity(instanceId);
    const page = this.pages.get(instanceId);
    if (!page) return;
    page.live = false; page.navigation = null; this.hidePage(page);
    this.pages.delete(instanceId);
    if (this.presentation?.instanceId === instanceId) this.presentation = null;
    if (!this.host.isDestroyed()) this.host.contentView.removeChildView(page.view);
    const wc = page.view.webContents;
    if (!wc.isDestroyed()) {
      wc.once('destroyed', () => wc.removeAllListeners());
      wc.close({ waitForBeforeUnload: false });
    } else if (!page.partition.contents.has(wc)) wc.removeAllListeners();
  }
  async forgetSession(sessionId: string): Promise<void> {
    identity(sessionId);
    const part = this.partitions.get(sessionId);
    if (!part) return;
    part.retired = true;
    part.retiring ??= Promise.resolve().then(async () => {
      for (const page of [...this.pages.values()]) if (page.partition === part) this.close(page.state.instanceId);
      // Closed tabs leave the presentation map before their native destruction.
      // Their partition retains ownership until those exact events have joined.
      await Promise.all(part.contents.values());
      await part.session.closeAllConnections();
      await part.session.clearData();
      this.partitions.delete(sessionId);
    });
    await part.retiring;
  }
  async clear(): Promise<void> {
    this.presentation = null;
    const parts = [...this.partitions.values()];
    for (const part of parts) part.retired = true;
    await Promise.all(parts.map(part => this.forgetSession(part.sessionId)));
  }
  retireHost(): Promise<void> {
    this.accepting = false;
    return this.clear();
  }
}
