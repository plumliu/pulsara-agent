import { WebContentsView, type BrowserWindow, type IpcMainInvokeEvent, type WebContents } from 'electron';
import { pathToFileURL } from 'node:url';

type Cover = {
  view: WebContentsView;
  contents: WebContents;
  finishing?: Promise<void>;
  endFade?: () => void;
};

/** Keep the original startup document above the workbench across navigation. */
export class StartupCover {
  private current?: Cover;

  constructor(private host: BrowserWindow, private file: string, private preload: string) {
    host.on('resize', () => this.resize());
    // Hidden renderers may not deliver animation completion until shown again.
    const finishHidden = () => { if (this.current?.finishing) this.dispose(); };
    host.on('hide', finishHidden);
    host.on('minimize', finishHidden);
    host.once('closed', () => this.dispose());
  }

  private resize() {
    const [width, height] = this.host.getContentSize();
    this.current?.view.setBounds({ x: 0, y: 0, width, height });
  }

  async show(error = '', phase = error ? 'failed' : 'starting') {
    this.dispose();
    const view = new WebContentsView({ webPreferences: {
      preload: this.preload, sandbox: true, contextIsolation: true,
      nodeIntegration: false, webviewTag: false, navigateOnDragDrop: false,
    } });
    const contents = view.webContents;
    const cover: Cover = { view, contents };
    this.current = cover;
    contents.once('destroyed', () => { if (this.current === cover) this.dispose(); });
    view.setBackgroundColor('#00000000');
    contents.setWindowOpenHandler(() => ({ action: 'deny' }));
    contents.on('will-navigate', event => event.preventDefault());
    contents.on('will-attach-webview', event => event.preventDefault());
    this.resize();
    this.host.contentView.addChildView(view);
    await contents.loadFile(this.file, { query: { error, phase } });
  }

  accepts(event: IpcMainInvokeEvent): boolean {
    const contents = this.current?.contents;
    return Boolean(contents && !contents.isDestroyed() && event.sender === contents && event.senderFrame === contents.mainFrame
      && event.senderFrame.url.split('?')[0] === pathToFileURL(this.file).href);
  }

  async finish() {
    const cover = this.current;
    if (!cover) return;
    if (cover.finishing) return cover.finishing;
    if (cover.contents.isDestroyed() || !this.host.isVisible() || this.host.isMinimized()) {
      this.dispose();
      return;
    }
    const retired = new Promise<void>(resolve => { cover.endFade = resolve; });
    cover.finishing = (async () => {
      try {
        await Promise.race([retired, cover.contents.executeJavaScript(`(() => {
          if (document.hidden) return;
          return new Promise(resolve => {
            const finish = () => {
              document.removeEventListener('visibilitychange', hidden);
              resolve();
            };
            const hidden = () => { if (document.hidden) finish(); };
            document.addEventListener('visibilitychange', hidden);
            document.body.animate(
              [{ opacity: 1 }, { opacity: 0 }],
              { duration: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 200, fill: 'forwards' }
            ).finished.then(finish, finish);
          });
        })()`)]);
      } catch (error) {
        if (this.current === cover && !cover.contents.isDestroyed()) throw error;
      } finally {
        // An error/reset screen may have replaced the cover while it was fading.
        if (this.current === cover) this.dispose();
      }
    })();
    return cover.finishing;
  }

  private dispose() {
    const cover = this.current;
    this.current = undefined;
    if (!cover) return;
    cover.endFade?.();
    if (!this.host.isDestroyed()) this.host.contentView.removeChildView(cover.view);
    // Electron clears view.webContents when its native WebContents is destroyed.
    if (!cover.contents.isDestroyed()) cover.contents.close();
  }
}
