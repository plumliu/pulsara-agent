/** Renderer-visible resource references; main still authenticates every caller. */
export interface BrowserState {
  instanceId: string;
  sessionId: string;
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
export interface BrowserPresentation { instanceId: string | null; presentationId: string }
export interface BrowserLayout {
  instanceId: string;
  presentationId: string;
  bounds: { x: number; y: number; width: number; height: number };
  visible: boolean;
}
export interface BrowserHide { instanceId: string; presentationId: string }
export type BrowserCommand = { instanceId: string; kind: 'navigate'; url: string }
  | { instanceId: string; kind: 'back' | 'forward' | 'reload' | 'stop' | 'focus' };
export type BrowserEvent =
  | { kind: 'state'; state: BrowserState }
  | { kind: 'popup'; instanceId: string; sessionId: string; url: string; disposition: string }
  | { kind: 'notice'; instanceId: string; message: string }
  | { kind: 'shortcut'; command: 'search' | 'new-session' };
export interface DesktopBrowser {
  create(input: { sessionId: string; tabId: string }): Promise<BrowserState>;
  command(input: BrowserCommand): Promise<void>;
  close(instanceId: string): Promise<void>;
  forgetSession(sessionId: string): Promise<void>;
  clear(): Promise<void>;
  activate(input: BrowserPresentation): Promise<void>;
  layout(input: BrowserLayout): Promise<void>;
  hide(input: BrowserHide): Promise<void>;
  onEvent(listener: (event: BrowserEvent) => void): () => void;
}
