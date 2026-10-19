import { WebContentsView, type BrowserWindow, type IpcMainInvokeEvent } from 'electron';
import { pathToFileURL } from 'node:url';

/** Keep the original startup document above the workbench across navigation. */
export class StartupCover {
  private view?: WebContentsView;

  constructor(private host: BrowserWindow, private file: string, private preload: string) {
    host.on('resize', () => this.resize());
    host.once('closed', () => this.dispose());
  }

  private resize() {
    const [width, height] = this.host.getContentSize();
    this.view?.setBounds({ x: 0, y: 0, width, height });
  }

  async show(error = '', phase = error ? 'failed' : 'starting') {
    this.dispose();
    const view = new WebContentsView({ webPreferences: {
      preload: this.preload, sandbox: true, contextIsolation: true,
      nodeIntegration: false, webviewTag: false, navigateOnDragDrop: false,
    } });
    this.view = view;
    view.setBackgroundColor('#00000000');
    view.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    view.webContents.on('will-navigate', event => event.preventDefault());
    view.webContents.on('will-attach-webview', event => event.preventDefault());
    this.resize();
    this.host.contentView.addChildView(view);
    await view.webContents.loadFile(this.file, { query: { error, phase } });
  }

  accepts(event: IpcMainInvokeEvent): boolean {
    const contents = this.view?.webContents;
    return Boolean(contents && event.sender === contents && event.senderFrame === contents.mainFrame
      && event.senderFrame.url.split('?')[0] === pathToFileURL(this.file).href);
  }

  async finish() {
    const view = this.view;
    if (!view || view.webContents.isDestroyed()) return;
    if (!this.host.isVisible() || this.host.isMinimized()) {
      this.dispose();
      return;
    }
    try {
      await view.webContents.executeJavaScript(`document.body.animate(
        [{ opacity: 1 }, { opacity: 0 }],
        { duration: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 200, fill: 'forwards' }
      ).finished.then(() => undefined)`);
    } finally {
      // An error/reset screen may have replaced the cover while it was fading.
      if (this.view === view) this.dispose();
    }
  }

  private dispose() {
    const view = this.view;
    this.view = undefined;
    if (!view) return;
    if (!this.host.isDestroyed()) this.host.contentView.removeChildView(view);
    if (!view.webContents.isDestroyed()) view.webContents.close();
  }
}
