import { app, session, type BrowserWindow, type Session, type WebContents } from 'electron';
import { isCanonicalIdentity, randomIdentity, type IdentityKind } from './identity';
import { browserGuestSource, type BrowserCommand, type BrowserEvent, type BrowserState } from './browser-contract';
import { allowBrowserRequest, browserURL, networkPort } from './browser-policy';

interface Partition { session: Session; port: string; retired: boolean; contents: Map<WebContents, Promise<void>>; retiring?: Promise<void> }
interface Navigation { url: string; stopped: boolean; awaitingStart: boolean; programmatic: boolean }
interface Page {
  state: BrowserState;
  partition: Partition;
  contents?: WebContents;
  attaching: boolean;
  attached: boolean;
  navigation: Navigation | null;
  firstCommit: boolean;
}
function identity(value: unknown, kind: IdentityKind): asserts value is string {
  if (!isCanonicalIdentity(value, kind)) throw new Error('无效的网页身份。');
}

/** Owns only native browser resources belonging to this trusted workbench. */
export class BrowserOwner {
  private partitionState?: Partition;
  private pages = new Map<string, Page>();
  private accepting: boolean;

  constructor(host: BrowserWindow, private origin: () => string | undefined,
    private emit: (event: BrowserEvent) => void = event => {
      if (!host.webContents.isDestroyed()) host.webContents.send('desktop:browser-event', event);
    }) {
    const contents = host.webContents;
    this.accepting = this.isWorkbench(contents.getURL());
    contents.on('did-start-navigation', (_event, _url, inPlace, main) => { if (main && !inPlace) void this.retireHost().catch(error => console.error('Could not retire browser resources', error)); });
    contents.on('did-navigate', (_event, url) => { this.accepting = this.isWorkbench(url); });
    contents.on('will-attach-webview', (event, preferences, params) => {
      const marker = typeof params.src === 'string' && params.src.slice('about:blank#'.length);
      const page = marker && this.pages.get(marker);
      // Electron's will-attach event identifies the embedder, not its calling frame.
      // Only the trusted main-frame IPC can reserve the one-use, unguessable source.
      if (!page || page.attaching || page.partition.retired || params.src !== browserGuestSource(page.state.instanceId)
        || !this.accepting || !this.isWorkbench(contents.mainFrame.url)) {
        event.preventDefault();
        return;
      }
      page.attaching = true;
      // Drop every attribute-derived preference. In particular, no preload or
      // renderer-selected partition survives; the owner has already secured this Session.
      for (const key of Object.keys(preferences)) delete preferences[key as keyof Electron.WebPreferences];
      Object.assign(preferences, { session: page.partition.session, zoomFactor: contents.getZoomFactor(), sandbox: true,
        contextIsolation: true, nodeIntegration: false, nodeIntegrationInWorker: false, nodeIntegrationInSubFrames: false,
        webSecurity: true, webviewTag: false, plugins: false, allowRunningInsecureContent: false,
        navigateOnDragDrop: false, disableDialogs: true });
      // The source attribute starts Electron's attach. Only the owner loads a document.
      params.src = '';
      const created = (_event: Electron.Event, guest: WebContents) => {
        if (guest.getType() !== 'webview' || guest.hostWebContents !== contents) return;
        app.off('web-contents-created', created);
        this.bind(page, guest);
      };
      app.on('web-contents-created', created);
      // Electron creates the guest synchronously after will-attach returns.
      queueMicrotask(() => app.off('web-contents-created', created));
    });
    contents.on('did-attach-webview', (_event, guest) => {
      const page = this.pageFor(guest);
      if (!page || !this.live(page)) { if (!guest.isDestroyed()) guest.close({ waitForBeforeUnload: false }); return; }
      page.attached = true;
      const op = page.navigation;
      if (op && !op.stopped) this.load(page, guest, op);
    });
    host.webContents.on('render-process-gone', () => { void this.retireHost().catch(error => console.error('Could not retire browser resources', error)); });
    host.once('closed', () => { void this.retireHost().catch(error => console.error('Could not retire browser resources', error)); });
  }

  private isWorkbench(value: string): boolean {
    try { const url = new URL(value); return url.origin === this.origin() && url.pathname === '/'; }
    catch { return false; }
  }

  private pageFor(contents: WebContents): Page | undefined {
    for (const page of this.pages.values()) if (page.contents === contents) return page;
  }

  private partition(): Partition {
    const existing = this.partitionState;
    if (existing) {
      if (existing.retired) throw new Error('网页会话仍在关闭，请等待其结算。');
      return existing;
    }
    const origin = this.origin();
    if (!origin) throw new Error('工作台尚未就绪。');
    const part: Partition = { session: session.fromPartition(randomIdentity('browser-partition')), port: networkPort(new URL(origin)), retired: false, contents: new Map() };
    const ses = part.session;
    ses.setPermissionRequestHandler((_wc, _permission, done) => done(false));
    ses.setPermissionCheckHandler(() => false);
    ses.setDevicePermissionHandler(() => false);
    ses.setDisplayMediaRequestHandler((_request, done) => done({}));
    ses.setBluetoothPairingHandler((_details, done) => done({ confirmed: false }));
    ses.webRequest.onBeforeRequest((details, done) => done({ cancel: !allowBrowserRequest(details.url, details.resourceType, part.port, part.retired) }));
    ses.on('will-download', (event, _item, wc) => {
      event.preventDefault();
      const page = this.pageFor(wc);
      if (page) this.notice(page, '内嵌网页不支持下载；可在系统浏览器中继续。');
    });
    this.partitionState = part;
    return part;
  }

  create(input: { tabId: string }): BrowserState {
    identity(input?.tabId, 'tab');
    if (!this.accepting || !this.origin()) throw new Error('工作台正在切换，不能创建网页。');
    if ([...this.pages.values()].some(page => page.state.tabId === input.tabId)) throw new Error('网页标签已有原生实例。');
    const part = this.partition();
    let instanceId: string;
    do { instanceId = randomIdentity('browser-instance'); } while (this.pages.has(instanceId));
    const page: Page = { partition: part, attaching: false, attached: false, navigation: null, firstCommit: true,
      state: { instanceId, tabId: input.tabId, url: '', pendingUrl: null,
        title: '', loading: false, canGoBack: false, canGoForward: false, error: null, crashed: false } };
    this.pages.set(page.state.instanceId, page);
    return { ...page.state };
  }

  private bind(page: Page, wc: WebContents): void {
    page.contents = wc;
    page.partition.contents.set(wc, new Promise(resolve => wc.once('destroyed', () => {
      page.partition.contents.delete(wc);
      resolve();
    })));
    this.listen(page, wc);
  }

  private live(page: Page): boolean { return this.pages.get(page.state.instanceId) === page && !page.partition.retired; }
  private active(page: Page): boolean { return this.live(page) && page.contents?.isDestroyed() === false; }
  private publish(page: Page) {
    if (!this.live(page)) return;
    const wc = page.contents;
    if (wc && !wc.isDestroyed()) {
      page.state.canGoBack = wc.navigationHistory.canGoBack();
      page.state.canGoForward = wc.navigationHistory.canGoForward();
    }
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
  private listen(page: Page, wc: WebContents) {
    wc.setWindowOpenHandler(details => {
      if (!this.active(page)) return { action: 'deny' };
      try {
        if (details.postBody || (details.frameName && details.frameName !== '_blank')) throw new Error('unsupported popup');
        const url = browserURL(details.url, page.partition.port);
        this.emit({ kind: 'popup', instanceId: page.state.instanceId, url, disposition: details.disposition });
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
      if (!this.active(page) || !wc.isFocused() || input.type !== 'keyDown' || input.isAutoRepeat || input.isComposing || input.alt || input.shift || !(process.platform === 'darwin' ? input.meta : input.control)) return;
      const key = input.key.toLowerCase();
      if (key === 'k' || key === 'n') {
        event.preventDefault();
        // The host dialog takes DOM focus in its layout effect.
        this.emit({ kind: 'shortcut', instanceId: page.state.instanceId, command: key === 'k' ? 'search' : 'new-session' });
      }
    });
    wc.on('did-start-navigation', (_event, url, inPlace, main) => {
      if (!main || !this.active(page)) return;
      if (inPlace || !this.validURL(page, url)) return;
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
    wc.on('render-process-gone', (_event, details) => this.guestLost(page, `网页进程已退出：${details.reason}`));
    wc.once('destroyed', () => this.guestLost(page, '网页已关闭。'));
  }
  private validURL(page: Page, url: string): boolean { try { browserURL(url, page.partition.port); return !page.partition.retired; } catch { return false; } }

  private guestLost(page: Page, message: string) {
    if (!this.live(page)) return;
    const state = { ...page.state, crashed: true, loading: false, pendingUrl: null, error: { url: page.state.pendingUrl || page.state.url, message } };
    this.close(page.state.instanceId);
    this.emit({ kind: 'state', state });
  }

  private load(page: Page, wc: WebContents, op: Navigation) {
    void wc.loadURL(op.url).catch((error: Error & { errno?: number }) => this.failure(page, op, op.url, error.message, error.errno));
  }

  command(input: BrowserCommand): void {
    identity(input?.instanceId, 'browser-instance');
    const page = this.pages.get(input.instanceId);
    if (!page || !this.live(page)) return;
    const wc = page.attached && this.active(page) ? page.contents : undefined;
    if (input.kind === 'stop') {
      if (page.navigation) page.navigation.stopped = true;
      wc?.stop(); page.state.loading = false; page.state.pendingUrl = null; this.publish(page); return;
    }
    if (input.kind === 'navigate') {
      const url = browserURL(input.url, page.partition.port);
      const op = this.begin(page, url, true); this.publish(page);
      if (wc) this.load(page, wc, op);
      return;
    }
    if (!wc) return;
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

  close(instanceId: string): void {
    identity(instanceId, 'browser-instance');
    const page = this.pages.get(instanceId);
    if (!page) return;
    this.pages.delete(instanceId);
    const wc = page.contents;
    if (wc && !wc.isDestroyed()) wc.close({ waitForBeforeUnload: false });
  }
  async clear(): Promise<void> {
    const part = this.partitionState;
    if (!part) return;
    part.retired = true;
    part.retiring ??= Promise.resolve().then(async () => {
      for (const page of [...this.pages.values()]) this.close(page.state.instanceId);
      // Join the user partition's exact guests, including a tab already closing.
      await Promise.all(part.contents.values());
      await part.session.closeAllConnections();
      await part.session.clearData();
      if (this.partitionState === part) this.partitionState = undefined;
    });
    await part.retiring;
  }
  retireHost(): Promise<void> {
    this.accepting = false;
    return this.clear();
  }
}
