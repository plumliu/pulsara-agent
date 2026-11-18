/** Renderer-visible resource references; main still authenticates every caller. */
export interface BrowserState {
  instanceId: string;
  tabId: string;
  url: string;
  pendingUrl: string | null;
  title: string;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  error: { url: string; message: string } | null;
  crashed: boolean;
}
export type BrowserCommand = { instanceId: string; kind: 'navigate'; url: string }
  | { instanceId: string; kind: 'back' | 'forward' | 'reload' | 'stop' };
export type BrowserEvent =
  | { kind: 'state'; state: BrowserState }
  | { kind: 'popup'; instanceId: string; url: string; disposition: string }
  | { kind: 'notice'; instanceId: string; message: string }
  | { kind: 'shortcut'; instanceId: string; command: 'search' | 'new-session' };
export interface DesktopBrowser {
  create(input: { tabId: string }): Promise<BrowserState>;
  command(input: BrowserCommand): Promise<void>;
  close(instanceId: string): Promise<void>;
  onEvent(listener: (event: BrowserEvent) => void): () => void;
}

export function browserGuestSource(instanceId: string): string { return `about:blank#${instanceId}`; }
