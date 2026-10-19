import type { BrowserState, DesktopBrowser } from './desktop-host';

export interface BrowserTabRecord {
  id: string;
  focusAddressOnOpen?: boolean;
  sessionId: string;
  state?: BrowserState;
  opening?: object;
  pendingNavigation?: string;
  navigation?: object;
  error?: string;
  notice?: string;
  popup?: string;
}
export interface BrowserTabSink {
  visibleSession(): string;
  activeId(sessionId: string): string | undefined;
  insert(record: BrowserTabRecord, foreground: boolean): void;
  changed(record: BrowserTabRecord): void;
  activate(id: string): void;
  shortcut(command: 'search' | 'new-session'): void;
}
export function browserUrl(input: string): string {
  const url = new URL(input.trim());
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('请输入 http:// 或 https:// 网页地址。');
  return url.href;
}
/** Address-bar convenience only; links and native commands still require URLs. */
export function browserAddressUrl(input: string): string {
  const value = input.trim();
  if (!value || /^[/\\]/.test(value) && !value.startsWith('//')) throw new Error('请输入有效的网页地址。');
  const hasScheme = /^[a-z][a-z\d+.-]*:/i.test(value) && !/^(?:localhost|[^/?#:\s]*\.[^/?#:\s]+):\d+(?:[/?#]|$)/i.test(value);
  try {
    const url = new URL(hasScheme ? value : `https://${value.replace(/^\/\//, '')}`);
    if (!hasScheme && (url.hostname === 'localhost' || url.hostname.endsWith('.localhost') || /^127\./.test(url.hostname) || url.hostname === '[::1]')) return browserUrl(`http://${value.replace(/^\/\//, '')}`);
    return browserUrl(url.href);
  } catch { throw new Error('请输入有效的 HTTP 或 HTTPS 网页地址。'); }
}
export function browserTitle(record: BrowserTabRecord): string {
  if (record.state?.title) return record.state.title;
  try { return new URL(record.state?.url || '').host; } catch { return '新网页'; }
}
/** Only disposable view resources. Stable records outlive React tab descriptions. */
export class BrowserTabs {
  readonly records = new Map<string, BrowserTabRecord>();
  private unsubscribe?: () => void;
  constructor(private host: DesktopBrowser | undefined, private sink: BrowserTabSink) {}
  connect() {
    this.unsubscribe = this.host?.onEvent(event => {
      if (event.kind === 'shortcut') { this.sink.shortcut(event.command); return; }
      const record = [...this.records.values()].find(item => item.state?.instanceId === (event.kind === 'state' ? event.state.instanceId : event.instanceId));
      if (!record) return;
      if (event.kind === 'state') { record.state = event.state; this.sink.changed(record); }
      if (event.kind === 'notice') { record.notice = event.message; this.sink.changed(record); }
      if (event.kind === 'popup') {
        if (this.sink.visibleSession() !== record.sessionId) { record.popup = event.url; this.sink.changed(record); }
        else this.open(record.sessionId, event.url, { newTab: true, background: event.disposition === 'background-tab' });
      }
    });
    return () => { this.unsubscribe?.(); this.unsubscribe = undefined; };
  }
  open(sessionId: string, input?: string, options: { newTab?: boolean; background?: boolean } = {}) {
    if (!sessionId) return;
    const url = input ? browserUrl(input) : undefined;
    if (url && !options.newTab) {
      const candidates = [...this.records.values()].filter(item => item.sessionId === sessionId && item.state?.url === url && !item.state.loading && !item.state.pendingUrl && !item.state.crashed);
      const match = candidates.find(item => item.id === this.sink.activeId(sessionId)) ?? candidates[0];
      if (match) { if (!options.background) this.sink.activate(match.id); return match; }
    }
    const record: BrowserTabRecord = { id: crypto.randomUUID(), sessionId, focusAddressOnOpen: !url };
    this.records.set(record.id, record);
    this.sink.insert(record, !options.background);
    void this.create(record, url);
    return record;
  }
  async create(record: BrowserTabRecord, url?: string) {
    const operation = {}; record.opening = operation; record.pendingNavigation = url; record.error = undefined;
    this.sink.changed(record);
    try {
      if (!this.host) throw new Error('桌面网页服务不可用。');
      const state = await this.host.create({ sessionId: record.sessionId, tabId: record.id });
      if (this.records.get(record.id) !== record || record.opening !== operation) { await this.host.close(state.instanceId); return; }
      record.opening = undefined; record.state = state; this.sink.changed(record);
      const target = record.pendingNavigation; record.pendingNavigation = undefined;
      if (target) await this.navigate(record, target);
    } catch (error) {
      if (this.records.get(record.id) !== record || (record.opening && record.opening !== operation)) return;
      record.opening = undefined; record.error = String(error instanceof Error ? error.message : error); this.sink.changed(record);
    }
  }
  async navigate(record: BrowserTabRecord, input: string) {
    const operation = {}; record.navigation = operation;
    try {
      const url = browserUrl(input);
      if (record.opening) { record.pendingNavigation = url; return; }
      if (record.state?.crashed || !record.state) { await this.create(record, url); return; }
      record.error = undefined; record.notice = undefined; this.sink.changed(record);
      await this.host?.command({ instanceId: record.state.instanceId, kind: 'navigate', url });
    } catch (error) {
      if (this.records.get(record.id) !== record || record.navigation !== operation) return;
      record.error = error instanceof Error ? error.message : String(error); this.sink.changed(record);
    }
  }
  close(id: string) {
    const record = this.records.get(id); if (!record) return;
    this.records.delete(id); record.opening = undefined;
    if (record.state) void this.host?.close(record.state.instanceId).catch(() => {});
  }
  forgetSession(sessionId: string) {
    for (const record of this.records.values()) if (record.sessionId === sessionId) this.close(record.id);
    void this.host?.forgetSession(sessionId).catch(() => {});
  }
  clear() { for (const id of this.records.keys()) this.close(id); void this.host?.clear().catch(() => {}); }
}
