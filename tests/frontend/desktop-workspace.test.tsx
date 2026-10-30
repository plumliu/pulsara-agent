import { uiPreferences } from '../../frontend/lib/ui-preferences';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { DesktopWorkspace, DesktopWorkspaceProvider, useDesktopWorkspace } from '../../frontend/components/desktop-workspace';
import { DesktopTitlebar, DesktopWindowControls } from '../../frontend/components/desktop-titlebar';
import { PromptDraftStore } from '../../frontend/lib/prompt-draft';
import { rejectDirectoryDrop } from '../../frontend/lib/file-drop';
import type { PulsaraDesktop } from '../../frontend/lib/desktop-host';

afterEach(() => { cleanup(); delete window.pulsaraDesktop; vi.restoreAllMocks(); vi.unstubAllGlobals(); });
let workspace: NonNullable<ReturnType<typeof useDesktopWorkspace>>;
function Content({ onClose = () => {} }: { onClose?: () => void }) { workspace = useDesktopWorkspace()!; return <DesktopWorkspace isOpen onClose={onClose} toolbar={null} renderTab={tab => <p>{tab.sessionId}:{tab.entityId}</p>} />; }
function Host({ sessionId, onClose }: { sessionId: string; onClose?: () => void }) { return <DesktopWorkspaceProvider sessionId={sessionId} onShow={() => {}}><Content onClose={onClose} /></DesktopWorkspaceProvider>; }
it('does not render session-owned file or visualization content while the session loads', () => {
  const renderFile = vi.fn(() => <p>file body</p>);
  const renderVisualization = vi.fn(() => <p>visualization body</p>);
  function Pane({ available }: { available: boolean }) {
    workspace = useDesktopWorkspace()!;
    return <DesktopWorkspace isOpen onClose={() => {}} toolbar={null} sessionContentAvailable={available} renderTab={() => <p>other</p>} />;
  }
  const view = render(<DesktopWorkspaceProvider sessionId="a" onShow={() => {}}><Pane available /></DesktopWorkspaceProvider>);
  act(() => workspace.open({ id: 'file', sessionId: 'a', title: 'file', kind: 'file', render: renderFile }));
  expect(screen.getByText('file body')).toBeTruthy();
  view.rerender(<DesktopWorkspaceProvider sessionId="a" onShow={() => {}}><Pane available={false} /></DesktopWorkspaceProvider>);
  const previousFileCalls = renderFile.mock.calls.length;
  expect(screen.queryByText('file body')).toBeNull();
  expect(screen.getByText('正在加载会话…')).toBeTruthy();
  act(() => workspace.open({ id: 'visualization', sessionId: 'a', title: 'visualization', kind: 'visualization', render: renderVisualization }));
  expect(renderFile).toHaveBeenCalledTimes(previousFileCalls);
  expect(renderVisualization).not.toHaveBeenCalled();
  view.rerender(<DesktopWorkspaceProvider sessionId="a" onShow={() => {}}><Pane available /></DesktopWorkspaceProvider>);
  expect(screen.getByText('visualization body')).toBeTruthy();
});
it('updates overflow arrows on scroll and resize without changing the selected tab', () => {
  let resize = () => {};
  vi.stubGlobal('ResizeObserver', class { constructor(callback: () => void) { resize = callback; } observe() {} disconnect() {} });
  render(<Host sessionId="a" />);
  act(() => workspace.open({ id: 'file', sessionId: 'a', title: 'guide.md', kind: 'file' }));
  const list = screen.getByRole('tablist');
  Object.defineProperties(list, { clientWidth: { configurable: true, value: 300 }, scrollWidth: { configurable: true, value: 900 } });
  list.scrollBy = vi.fn();
  act(() => resize());
  const left = screen.getByRole('button', { name: '向左滚动标签页' }) as HTMLButtonElement;
  const right = screen.getByRole('button', { name: '向右滚动标签页' }) as HTMLButtonElement;
  expect(left.disabled).toBe(true);
  expect(right.disabled).toBe(false);
  fireEvent.click(right);
  expect(list.scrollBy).toHaveBeenCalledWith({ left: 210, behavior: 'smooth' });
  list.scrollLeft = 600;
  fireEvent.scroll(list);
  expect(left.disabled).toBe(false);
  expect(right.disabled).toBe(true);
  expect(workspace.activeId).toBe('file');
  Object.defineProperty(list, 'clientWidth', { value: 900 });
  list.scrollLeft = 0;
  act(() => resize());
  expect(left.disabled).toBe(true);
  expect(right.disabled).toBe(true);
});
it('hides after a deliberate rightward drag and reopens the same tab at a usable width', () => {
  vi.stubGlobal('PointerEvent', MouseEvent);
  const closed = vi.fn();
  function Resizable() {
    workspace = useDesktopWorkspace()!;
    const [open, setOpen] = useState(true);
    return <><button onClick={() => setOpen(true)}>Reopen</button><DesktopWorkspace isOpen={open} onClose={() => { closed(); setOpen(false); }} toolbar={null} renderTab={tab => <p>{tab.title}</p>} /></>;
  }
  render(<DesktopWorkspaceProvider sessionId="a" onShow={() => {}}><Resizable /></DesktopWorkspaceProvider>);
  const dispose = vi.fn();
  act(() => workspace.open({ id: 'file', sessionId: 'a', title: 'guide.md', kind: 'file', onClose: dispose }));
  const pane = screen.getByLabelText('会话标签工作区');
  const separator = screen.getByRole('separator');
  separator.setPointerCapture = vi.fn();
  separator.releasePointerCapture = vi.fn();
  vi.spyOn(pane, 'getBoundingClientRect').mockReturnValue({ width: 400 } as DOMRect);
  fireEvent.pointerDown(separator, { clientX: 1000, button: 0 });
  expect(pane.classList.contains('is-resizing')).toBe(true);
  fireEvent.pointerMove(separator, { clientX: 1110 });
  expect(pane.style.getPropertyValue('--workspace-width')).toBe('350px');
  expect(closed).not.toHaveBeenCalled();
  fireEvent.pointerMove(separator, { clientX: 1240 });
  fireEvent.pointerMove(separator, { clientX: 1300 });
  expect(closed).toHaveBeenCalledOnce();
  expect(pane.classList.contains('is-open')).toBe(false);
  expect(pane.classList.contains('is-resizing')).toBe(false);
  expect(pane.hasAttribute('inert')).toBe(true);
  expect(dispose).not.toHaveBeenCalled();
  expect(separator.releasePointerCapture).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByText('Reopen'));
  expect(pane.classList.contains('is-open')).toBe(true);
  expect(pane.hasAttribute('inert')).toBe(false);
  expect(pane.style.getPropertyValue('--workspace-width')).toBe('400px');
  expect(screen.getByRole('tab', { name: 'guide.md' }).getAttribute('aria-selected')).toBe('true');
  fireEvent.pointerDown(separator, { clientX: 1000, button: 0 });
  fireEvent.lostPointerCapture(separator);
  fireEvent.pointerMove(separator, { clientX: 1300 });
  expect(closed).toHaveBeenCalledOnce();
});
it('focuses exact instances and preserves independent session tabs without retargeting', () => {
  const view = render(<Host sessionId="a" />);
  act(() => workspace.open({ id: 'a-agent', sessionId: 'a', kind: 'agent', title: 'Review', entityId: 'agent-1' }));
  act(() => workspace.open({ id: 'a-agent', sessionId: 'a', kind: 'agent', title: 'Review', entityId: 'agent-1' }));
  expect(screen.getAllByRole('tab')).toHaveLength(1);
  view.rerender(<Host sessionId="b" />);
  expect(screen.queryByRole('tab')).toBeNull();
  act(() => workspace.open({ id: 'b-agent', sessionId: 'b', kind: 'agent', title: 'Other', entityId: 'agent-2' }));
  expect(screen.getByText('b:agent-2')).toBeTruthy();
  view.rerender(<Host sessionId="a" />);
  expect(screen.getByText('a:agent-1')).toBeTruthy();
  expect(screen.queryByRole('tab', { name: 'Other' })).toBeNull();
});
it('closes only the view and retains uncertain controls under the original Host', () => {
  const closeRead = vi.fn();
  render(<Host sessionId="a" />);
  const original = new Map();
  workspace.backgroundControls.set('["a","host-a"]', original);
  act(() => workspace.open({ id: 'process-1', sessionId: 'a', kind: 'process', hostSessionId: 'host-a', entityId: 'p1', title: 'build', onClose: closeRead }));
  fireEvent.click(screen.getByRole('button', { name: '关闭 build' }));
  expect(closeRead).toHaveBeenCalledOnce();
  expect(workspace.backgroundControls.get('["a","host-a"]')).toBe(original);
  expect(screen.queryByRole('tab')).toBeNull();
});
it('accepts native files as original paths and does not read or import their bytes', async () => {
  const file = new File(['secret bytes'], 'photo.png', { type: 'image/png' });
  const read = vi.fn(); Object.defineProperty(file, 'arrayBuffer', { value: read });
  window.pulsaraDesktop = { pathForFile: candidate => candidate === file ? '/tmp/original/photo.png' : '' } as PulsaraDesktop;
  const store = new PromptDraftStore();
  act(() => { store.getEditor('a'); store.insertFiles('a', [file], 'end'); });
  const result = await store.capture('a');
  expect(result.content.parts).toEqual([{ type: 'text', text: '【本地文件："/tmp/original/photo.png"】' }]);
  expect(read).not.toHaveBeenCalled();
  store.destroy();
});
it('references a native directory itself without recursively importing its children', async () => {
  const file = new File([], 'project');
  window.pulsaraDesktop = { pathForFile: () => '/tmp/project' } as PulsaraDesktop;
  const transfer = { files: [file], items: [{kind: 'file', getAsFile: () => file, webkitGetAsEntry: () => ({ isDirectory: true })}] } as unknown as DataTransfer;
  expect(rejectDirectoryDrop(transfer, vi.fn())).toBe(false);
  const store = new PromptDraftStore();
  act(() => { store.getEditor('a'); store.insertFiles('a', [file], 'end'); });
  const result = await store.capture('a');
  expect(result.content.parts).toEqual([{ type: 'text', text: '【本地文件夹："/tmp/project"】' }]);
  store.destroy();
});
it('clears only a confirmed deleted session and disposes its file resources', () => {
  const control = { current: null as import('../../frontend/components/desktop-workspace').WorkspaceControl | null };
  const release = vi.fn();
  render(<DesktopWorkspaceProvider sessionId="a" onShow={() => {}} controlRef={control}><Content /></DesktopWorkspaceProvider>);
  act(() => workspace.open({ id: 'a-file', sessionId: 'a', title: 'notes.md', kind: 'file', onClose: release }));
  act(() => workspace.open({ id: 'b-agent', sessionId: 'b', title: 'Work', kind: 'agent' }));
  workspace.backgroundControls.set('["a","host-a"]', new Map());
  workspace.backgroundControls.set('["b","host-b"]', new Map());
  act(() => control.current!.forgetSession('a'));
  expect(release).toHaveBeenCalledOnce();
  expect(workspace.tabs).toHaveLength(0);
  expect(workspace.backgroundControls.has('["a","host-a"]')).toBe(false);
  expect(workspace.backgroundControls.has('["b","host-b"]')).toBe(true);
});

it('keeps category controls in the top header and instance tabs below it', () => {
  const view = render(<Host sessionId="a" />);
  act(() => workspace.open({id:'a-agent',sessionId:'a',kind:'agent',title:'Review',entityId:'agent-1'}));
  const topbar = view.container.querySelector('.workspace-topbar')!;
  expect(topbar.querySelector('[role="tablist"]')).toBeNull();
  expect(topbar.nextElementSibling?.querySelector('[role="tablist"]')).toBe(screen.getByRole('tablist'));
  expect(topbar.querySelector('.workspace-toolbar')).toBeTruthy();
  expect(view.container.querySelectorAll('header')).toHaveLength(1);
  expect(screen.getByRole('button',{name:'收起工作区'})).toBeTruthy();
});
it('leaves session title and workspace switch with the session header and keeps native controls', () => {
  const sidebar=vi.fn(), maximize=vi.fn();
  window.pulsaraDesktop={platform:'darwin',toggleMaximize:maximize} as unknown as PulsaraDesktop;
  const view=render(<DesktopTitlebar workbench title="Session name" onToggleSidebar={sidebar} onToggleWorkspace={vi.fn()}/>);
  expect(screen.queryByText('Session name')).toBeNull();
  expect(screen.queryByRole('button',{name:'切换标签工作区'})).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'切换会话侧栏'}));
  expect(sidebar).toHaveBeenCalledOnce();
  fireEvent.doubleClick(screen.getByRole('button',{name:'切换会话侧栏'}));
  expect(maximize).not.toHaveBeenCalled();
  fireEvent.doubleClick(view.container.querySelector('header')!);
  expect(maximize).toHaveBeenCalledOnce();
});

it('routes Linux window controls to the desktop host without maximizing on a button double-click', () => {
  const minimize = vi.fn(), maximize = vi.fn(), close = vi.fn();
  window.pulsaraDesktop = { platform: 'linux', minimize, toggleMaximize: maximize, closeWindow: close } as unknown as PulsaraDesktop;
  const view = render(<main><DesktopTitlebar workbench title="Session" onToggleSidebar={vi.fn()} onToggleWorkspace={vi.fn()} /><DesktopWindowControls /></main>);
  expect(screen.getByRole('group', { name: '窗口控制' }).parentElement).toBe(view.container.querySelector('main'));
  fireEvent.click(screen.getByRole('button', { name: '最小化' }));
  fireEvent.click(screen.getByRole('button', { name: '最大化或还原' }));
  fireEvent.click(screen.getByRole('button', { name: '关闭窗口' }));
  fireEvent.doubleClick(screen.getByRole('button', { name: '关闭窗口' }));
  expect(minimize).toHaveBeenCalledOnce();
  expect(maximize).toHaveBeenCalledOnce();
  expect(close).toHaveBeenCalledOnce();
});

it.each(['linux', 'darwin'] as const)('places workspace categories below the top row only on Linux (%s)', platform => {
  window.pulsaraDesktop = { platform } as PulsaraDesktop;
  const toolbar = <nav aria-label="工作区入口"><button>能力</button><button>任务</button><button>后台进程</button><button>新建网页</button></nav>;
  const collapse = vi.fn();
  function WithCategories() {
    workspace = useDesktopWorkspace()!;
    return <DesktopWorkspace isOpen onClose={collapse} toolbar={toolbar} renderTab={() => null} />;
  }
  const view = render(<DesktopWorkspaceProvider sessionId="a" onShow={vi.fn()}>
    <WithCategories />
  </DesktopWorkspaceProvider>);
  act(() => workspace.open({ id: 'a-file', sessionId: 'a', title: 'guide.md', kind: 'file' }));
  const topbar = view.container.querySelector('.workspace-topbar')!;
  const categories = screen.getByRole('navigation', { name: '工作区入口' });
  expect(topbar.contains(categories)).toBe(platform === 'darwin');
  if (platform === 'linux') {
    expect(categories.parentElement).toBe(topbar.nextElementSibling);
    expect(categories.parentElement!.nextElementSibling).toBe(view.container.querySelector('.workspace-tab-strip'));
  }
  expect(topbar.contains(screen.getByRole('button', { name: '收起工作区' }))).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '收起工作区' }));
  expect(collapse).toHaveBeenCalledOnce();
});

it('collapses only after closing the current session last tab and retains other sessions', () => {
  const collapse = vi.fn(), release = vi.fn();
  const view = render(<Host sessionId="a" onClose={collapse} />);
  act(() => workspace.open({ id: 'a-file', sessionId: 'a', kind: 'file', title: 'guide.md', onClose: release }));
  act(() => workspace.open({ id: 'a-agent', sessionId: 'a', kind: 'agent', title: 'Review' }));
  act(() => workspace.open({ id: 'b-file', sessionId: 'b', kind: 'file', title: 'Other' }));
  fireEvent.click(screen.getByRole('button', { name: '关闭 Review' }));
  expect(collapse).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '关闭 guide.md' }));
  expect(collapse).toHaveBeenCalledOnce();
  expect(release).toHaveBeenCalledOnce();
  view.rerender(<Host sessionId="b" onClose={collapse} />);
  expect(screen.getByRole('tab', { name: 'Other' })).toBeTruthy();
});

it('remembers widths per session across switching and remounting, saving after drag completion', () => {
  vi.stubGlobal('PointerEvent', MouseEvent);
  let view = render(<Host sessionId="a" />);
  const pane = () => screen.getByLabelText('会话标签工作区');
  const separator = screen.getByRole('separator');
  separator.setPointerCapture = vi.fn();
  vi.spyOn(pane(), 'getBoundingClientRect').mockReturnValue({width:350} as DOMRect);
  fireEvent.pointerDown(separator, {clientX:800,button:0});
  fireEvent.pointerMove(separator, {clientX:700});
  expect(uiPreferences.getItem('pulsara-workspace-width:a')).toBeNull();
  fireEvent.pointerUp(separator);
  expect(uiPreferences.getItem('pulsara-workspace-width:a')).toBe('450');
  view.rerender(<Host sessionId="b" />);
  expect(pane().style.getPropertyValue('--workspace-width')).toBe('350px');
  vi.spyOn(pane(), 'getBoundingClientRect').mockReturnValue({width:350} as DOMRect);
  fireEvent.keyDown(screen.getByRole('separator'), {key:'ArrowLeft'});
  expect(uiPreferences.getItem('pulsara-workspace-width:b')).toBe('382');
  view.rerender(<Host sessionId="a" />);
  expect(pane().style.getPropertyValue('--workspace-width')).toBe('450px');
  view.unmount();
  view = render(<Host sessionId="b" />);
  expect(pane().style.getPropertyValue('--workspace-width')).toBe('382px');
});

function userBrowserHost() {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  const create = vi.fn(async ({tabId}: {tabId:string}) => ({
    instanceId: `native:${tabId}`, tabId, url: '', pendingUrl: null, title: '', loading: false,
    canGoBack: false, canGoForward: false, error: null, crashed: false,
  }));
  const close = vi.fn(async () => {}), clear = vi.fn(async () => {});
  window.pulsaraDesktop = { browser: {create, close, clear, activate: vi.fn(async () => {}), layout: vi.fn(async () => {}), hide: vi.fn(async () => {}), command: vi.fn(async () => {}), onEvent: () => () => {}} } as unknown as PulsaraDesktop;
  return {create, close, clear};
}

it('opens user webpages without a session and preserves their identity through session switches and deletion', async () => {
  const browser = userBrowserHost();
  const control = {current:null as import('../../frontend/components/desktop-workspace').WorkspaceControl | null};
  function UserHost({sessionId}: {sessionId:string}) {
    return <DesktopWorkspaceProvider sessionId={sessionId} onShow={() => {}} controlRef={control}><Content /></DesktopWorkspaceProvider>;
  }
  const view = render(<UserHost sessionId="" />);
  await act(async () => { workspace.browserTabs.open(); });
  const id = workspace.tabs[0].id;
  expect(workspace.tabs[0].sessionId).toBeUndefined();
  expect(browser.create).toHaveBeenCalledWith({tabId:id});
  expect(workspace.activeId).toBe(id);
  view.rerender(<UserHost sessionId="a" />);
  act(() => workspace.open({id:'a-file',sessionId:'a',kind:'file',title:'A file'}));
  act(() => workspace.activate(id));
  view.rerender(<UserHost sessionId="b" />);
  expect(workspace.tabs.map(tab => tab.id)).toEqual([id]); expect(workspace.activeId).toBe(id);
  act(() => control.current!.forgetSession('a'));
  act(() => control.current!.clear());
  view.rerender(<UserHost sessionId="" />);
  expect(workspace.tabs.map(tab => tab.id)).toEqual([id]); expect(workspace.activeId).toBe(id);
  expect(browser.close).not.toHaveBeenCalled(); expect(browser.clear).not.toHaveBeenCalled();
  act(() => workspace.close(id));
  expect(browser.close).toHaveBeenCalledWith(`native:${id}`);
  expect(workspace.tabs).toHaveLength(0);
  view.unmount(); expect(browser.clear).toHaveBeenCalledOnce();
});

it('deduplicates against the effective selected browser after falling back from another session page', async () => {
  userBrowserHost();
  const view = render(<Host sessionId="a" />);
  let first!: import('../../frontend/lib/browser-tabs').BrowserTabRecord;
  let second!: import('../../frontend/lib/browser-tabs').BrowserTabRecord;
  await act(async () => {
    first = workspace.browserTabs.open(); second = workspace.browserTabs.open(undefined, {newTab:true});
  });
  first.state!.url = second.state!.url = 'https://example.test/';
  act(() => workspace.open({id:'a-file',sessionId:'a',kind:'file',title:'A file'}));
  view.rerender(<Host sessionId="empty" />);
  expect(workspace.activeId).toBe(second.id);
  let found;
  act(() => { found = workspace.browserTabs.open('https://example.test'); });
  expect(found).toBe(second); expect(workspace.tabs).toHaveLength(2);
});

it('releases a late user webpage allocation when the workspace owner is destroyed', async () => {
  const browser = userBrowserHost();
  let resolve!: (state: Awaited<ReturnType<typeof browser.create>>) => void;
  browser.create.mockImplementation(() => new Promise(done => {resolve=done;}));
  const view = render(<Host sessionId="" />);
  act(() => { workspace.browserTabs.open(); });
  const id = workspace.tabs[0].id;
  view.unmount();
  await act(async () => resolve({instanceId:'late',tabId:id,url:'',pendingUrl:null,title:'',loading:false,canGoBack:false,canGoForward:false,error:null,crashed:false}));
  expect(browser.clear).toHaveBeenCalledOnce(); expect(browser.close).toHaveBeenCalledWith('late');
});
