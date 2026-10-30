import { describe, expect, it, vi } from 'vitest';
import { BrowserTabs, browserUrl, browserAddressUrl, type BrowserTabRecord, type BrowserTabSink } from '../../frontend/lib/browser-tabs';
import type { BrowserEvent, BrowserState, DesktopBrowser } from '../../frontend/lib/desktop-host';
const state = (tab: BrowserTabRecord, instanceId = crypto.randomUUID()): BrowserState => ({ instanceId, tabId: tab.id, title: '', url: '', pendingUrl: null, loading: false, canGoBack: false, canGoForward: false, error: null, crashed: false });
function setup() {
  let event!: (event: BrowserEvent) => void;
  const sink: BrowserTabSink = { visible: () => true, activeId: () => undefined, insert: vi.fn(), changed: vi.fn(), activate: vi.fn(), shortcut: vi.fn() };
  const host: DesktopBrowser = { create: vi.fn(), command: vi.fn(async () => {}), close: vi.fn(async () => {}), clear: vi.fn(async () => {}), activate: vi.fn(async () => {}), layout: vi.fn(async () => {}), hide: vi.fn(async () => {}), onEvent: listener => { event = listener; return () => {}; } };
  vi.mocked(host.create).mockImplementation(async ({tabId}) => state({id: tabId}));
  const tabs = new BrowserTabs(host, sink); tabs.connect();
  return {tabs, host, sink, event: (value: BrowserEvent) => event(value)};
}
const settle = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
it.each([
  [' example.com/path?q=one#two ', 'https://example.com/path?q=one#two'],
  ['example.com:8080', 'https://example.com:8080/'],
  ['localhost:3000/path', 'http://localhost:3000/path'],
  ['localhost:443', 'http://localhost:443/'],
  ['demo.localhost:3000', 'http://demo.localhost:3000/'],
  ['127.0.0.1:8080', 'http://127.0.0.1:8080/'],
  ['[::1]:8080', 'http://[::1]:8080/'],
  ['http://example.com', 'http://example.com/'],
  ['https://localhost:3000', 'https://localhost:3000/'],
])('normalizes address input %s without changing explicit protocols or ports', (input, expected) => {
  expect(browserAddressUrl(input)).toBe(expected);
});
it.each(['', '/Users/me/index.html', 'file:///tmp/a', 'javascript:alert(1)', 'javascript:123', 'tel:123', 'data:text/html,hello', 'https://me:secret@example.com', 'not a hostname'])('rejects invalid or privileged address input %s', input => {
  expect(() => browserAddressUrl(input)).toThrow();
});
describe('browser resource ownership', () => {
  it('matches committed URLs in the user browser, preferring the active exact match', async () => {
    const {tabs, host, sink, event} = setup();
    const first = tabs.open('https://example.com')!; await settle();
    event({kind: 'state', state: {...first.state!, url:'https://example.com/'}});
    expect(tabs.open(' https://example.com/ ')).toBe(first);
    const second = tabs.open('https://example.com/', {newTab:true})!; await settle();
    event({kind:'state',state:{...second.state!,url:'https://example.com/'}});
    sink.activeId = () => second.id;
    expect(tabs.open('https://example.com')).toBe(second);
    event({kind:'state',state:{...second.state!,pendingUrl:'https://other.test/',loading:true}});
    expect(tabs.open('https://example.com')).toBe(first);
    expect(host.create).toHaveBeenCalledTimes(2);
  });
  it('closes an exact late allocation after close, without navigating or recreating the tab', async () => {
    const {tabs, host, sink} = setup();
    let resolve!: (state: BrowserState) => void;
    vi.mocked(host.create).mockImplementation(() => new Promise(done => {resolve=done;}));
    const record = tabs.open('https://example.com')!;
    tabs.close(record.id); resolve(state(record,'late')); await settle();
    expect(host.close).toHaveBeenCalledWith('late'); expect(host.command).not.toHaveBeenCalled();
    expect(tabs.records.size).toBe(0); expect(sink.insert).toHaveBeenCalledTimes(1);
  });
  it('uses the latest address during allocation, and ignores an older rejected navigation', async () => {
    const {tabs,host} = setup(); let resolve!: (state:BrowserState)=>void;
    vi.mocked(host.create).mockImplementation(() => new Promise(done=>{resolve=done;}));
    const record=tabs.open('https://first.test')!;
    await tabs.navigate(record,'https://second.test'); resolve(state(record)); await settle();
    expect(host.create).toHaveBeenCalledTimes(1);
    expect(host.command).toHaveBeenLastCalledWith(expect.objectContaining({url:'https://second.test/'}));
    let reject!: (error:Error)=>void;
    vi.mocked(host.command).mockImplementationOnce(()=>new Promise((_done,fail)=>{reject=fail;}));
    const previous=tabs.navigate(record,'https://previous.test'); await tabs.navigate(record,'https://current.test');
    reject(new Error('stale')); await previous; expect(record.error).toBeUndefined();
  });
  it('keeps hidden popup requests on their source and background requests do not activate', async () => {
    const {tabs,host,sink,event}=setup(); const record=tabs.open('https://source.test')!; await settle();
    sink.visible=()=>false; event({kind:'popup',instanceId:record.state!.instanceId,url:'https://child.test/',disposition:'foreground-tab'});
    expect(record.popup).toBe('https://child.test/'); expect(host.create).toHaveBeenCalledTimes(1);
    sink.visible=()=>true; event({kind:'popup',instanceId:record.state!.instanceId,url:'https://child.test/',disposition:'background-tab'});
    expect(sink.insert).toHaveBeenLastCalledWith(expect.objectContaining({id:expect.any(String)}),false);
    tabs.close(record.id); event({kind:'popup',instanceId:record.state!.instanceId,url:'https://stale.test/',disposition:'foreground-tab'});
    expect(host.create).toHaveBeenCalledTimes(2);
  });
  it('clears the user browser even after every visible tab was already closed', async () => {
    const {tabs,host}=setup(); const record=tabs.open()!; await settle(); tabs.close(record.id);
    tabs.clear(); expect(host.clear).toHaveBeenCalledOnce();
  });
  it('does not infer schemes, credentials, or local-file navigation', () => {
    for (const url of ['example.com','file:///tmp/test','javascript:alert(1)','https://user:password@example.com']) expect(()=>browserUrl(url)).toThrow();
    expect(browserUrl('http://localhost:3000/path#part')).toBe('http://localhost:3000/path#part');
  });
});
