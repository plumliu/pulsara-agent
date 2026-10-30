import { BrowserTabs, browserTitle, type BrowserTabRecord } from '../lib/browser-tabs';
import { desktopHost } from '../lib/desktop-host';
import { BrowserPanel } from './browser-panel';
import { uiPreferences } from '../lib/ui-preferences';
import type { CommandReceipt, UserControlCommandRef } from '../lib/runtime-adapter';
import type { AgentReadingState } from './agent-conversation-pane';
import { Blocks, Bot, ChevronLeft, ChevronRight, Globe, FileCode2, FileText, FolderKanban, ListChecks, PanelRightClose, Server, TerminalSquare, X } from 'lucide-react';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react';
import { flushSync } from 'react-dom';

export type WorkspaceTabKind = 'browser' | 'file' | 'visualization' | 'capabilities' | 'mcp' | 'background' | 'process' | 'subagents' | 'agent' | 'cells' | 'cells-unavailable' | 'cell' | 'cell-graph';
export interface WorkspaceControl { forgetSession: (sessionId: string) => void; clear: () => void }
interface WorkspaceTabBase {
  browser?: BrowserTabRecord;
  view?: { top: number; left: number; reader?: { top: number; left: number }; graph?: { top: number; left: number; zoom?: number; expandedWorkIds?: string[]; expandedParents?: string[]; worksPages?: number; participantsPages?: number }; agentReadingState?: AgentReadingState };
  id: string;
  title: string;
  workLocation?: { workId: string; requestId: number };
  render?: () => ReactNode;
  onClose?: () => void;
}
type StandardWorkspaceKind = 'file' | 'visualization' | 'capabilities' | 'mcp' | 'background' | 'process';
type StandardWorkspaceTab = { [K in StandardWorkspaceKind]: { kind: K; entityId?: string; hostSessionId?: string } }[StandardWorkspaceKind];
export type WorkspaceTab = WorkspaceTabBase & (
  | { kind: 'browser'; browser: BrowserTabRecord; sessionId?: never; entityId?: never; hostSessionId?: never }
  | ({ sessionId: string; browser?: never } & (StandardWorkspaceTab
  | { kind: 'subagents'; entityId?: never; hostSessionId?: never }
  | { kind: 'cells-unavailable'; entityId?: never; hostSessionId?: never }
  | { kind: 'cells'; entityId?: never; hostSessionId: string }
  | { kind: 'agent'; entityId: string; hostSessionId?: never }
  | { kind: 'cell'; sourceExecAttemptId: string; entityId?: string; hostSessionId?: string }
  | { kind: 'cell-graph'; entityId: string; hostSessionId: string })));
export type BackgroundControlMemory = Map<string, { reference?: UserControlCommandRef; control?: CommandReceipt; error?: string }>;
export interface PreparedWorkspaceTab { adopt: () => WorkspaceTab | undefined; release: () => void; start?: () => void }
interface WorkspaceContext {
  interact: () => void;
  interactionVersion: () => number;
  publish: (tabs: WorkspaceTab[], foreground: boolean) => void;
  sessionId: string;
  backgroundControls: Map<string, BackgroundControlMemory>;
  tabs: WorkspaceTab[];
  activeId?: string;
  open: (tab: WorkspaceTab, options?: { background?: boolean }) => void;
  browserTabs: BrowserTabs;
  visible: boolean;
  update: (id: string, patch: Partial<WorkspaceTabBase>) => void;
  close: (id: string) => void;
  activate: (id: string) => void;
}
function selectedTabId(tabs: WorkspaceTab[], sessionId: string, browserId: string | undefined, sessions: Record<string, string>) {
  const visible = tabs.filter(tab => tab.kind === 'browser' || tab.sessionId === sessionId);
  return visible.find(tab => tab.id === (browserId ?? sessions[sessionId]))?.id ?? visible.at(-1)?.id;
}
const Context = createContext<WorkspaceContext | undefined>(undefined);
export function useDesktopWorkspace() { return useContext(Context); }

/** Disposable view descriptions only. Execution and control references stay with their owners. */
export function DesktopWorkspaceProvider({ sessionId, onShow, children, controlRef, visible = true, onBrowserShortcut }: { visible?: boolean; onBrowserShortcut?: (command: 'search' | 'new-session') => void; sessionId: string; onShow: () => void; children: ReactNode; controlRef?: RefObject<WorkspaceControl | null> }) {
  const backgroundControls = useRef(new Map<string, BackgroundControlMemory>()).current;
  const [tabs, setTabs] = useState<WorkspaceTab[]>([]);
  const [active, setActive] = useState<Record<string, string>>({});
  const [activeBrowserId, setActiveBrowserId] = useState<string>();
  const tabsRef = useRef(tabs); tabsRef.current = tabs;
  const interaction = useRef(0);
  const interact = useCallback(() => { interaction.current++; }, []);
  const interactionVersion = useCallback(() => interaction.current, []);
  const show = useRef(onShow); show.current = onShow;
  const current = useRef({ sessionId, visible, active, activeBrowserId, onBrowserShortcut }); current.current = { sessionId, visible, active, activeBrowserId, onBrowserShortcut };
  const install = useCallback((next: WorkspaceTab[]) => { tabsRef.current = next; setTabs(next); }, []);
  const select = useCallback((tab: WorkspaceTab) => {
    if (tab.kind === 'browser') setActiveBrowserId(tab.id);
    else {
      setActive(value => ({ ...value, [tab.sessionId]: tab.id }));
      if (tab.sessionId === current.current.sessionId) setActiveBrowserId(undefined);
    }
  }, []);
  const activate = useCallback((id: string) => {
    interaction.current++;
    const tab = tabsRef.current.find(tab => tab.id === id);
    if (tab) { select(tab); if ((tab.kind === 'browser' || current.current.sessionId === tab.sessionId) && current.current.visible) show.current(); }
  }, [select]);
  const open = useCallback((tab: WorkspaceTab, options?: { background?: boolean }) => {
    interaction.current++;
    tab = { view: tabsRef.current.find(item => item.id === tab.id)?.view ?? { top: 0, left: 0, graph: { top: 0, left: 0 } }, ...tab };
    install(tabsRef.current.some(item => item.id === tab.id) ? tabsRef.current.map(item => item.id === tab.id ? tab : item) : [...tabsRef.current, tab]);
    if (!options?.background) { select(tab); if ((tab.kind === 'browser' || current.current.sessionId === tab.sessionId) && current.current.visible) show.current(); }
  }, [install, select]);
  const publish = useCallback((batch: WorkspaceTab[], foreground: boolean) => {
    if (!batch.length || batch.some(tab => tab.kind !== 'browser' && tab.sessionId !== current.current.sessionId) || !current.current.visible) return;
    const next = [...tabsRef.current];
    for (const tab of batch) if (!next.some(item => item.id === tab.id)) next.push({ view: { top: 0, left: 0 }, ...tab });
    install(next);
    if (foreground) { select(batch.at(-1)!); show.current(); }
  }, [install, select]);
  const update = useCallback((id: string, patch: Partial<WorkspaceTabBase>) => {
    install(tabsRef.current.map(item => item.id === id ? { ...item, ...patch } as WorkspaceTab : item));
  }, [install]);
  const [browserTabs] = useState(() => new BrowserTabs(desktopHost()?.browser, {
    visible: () => current.current.visible,
    activeId: () => selectedTabId(tabsRef.current, current.current.sessionId, current.current.activeBrowserId, current.current.active),
    insert: (record, foreground) => open({ id: record.id, kind: 'browser', title: browserTitle(record), browser: record, onClose: () => browserTabs.close(record.id) }, { background: !foreground }),
    changed: record => update(record.id, { title: browserTitle(record), browser: record }),
    activate,
    shortcut: command => current.current.onBrowserShortcut?.(command),
  }));
  useEffect(() => browserTabs.connect(), [browserTabs]);
  useEffect(() => () => browserTabs.clear(), [browserTabs]);
  const close = useCallback((id: string) => {
    interaction.current++;
    const tab = tabsRef.current.find(tab => tab.id === id);
    tab?.onClose?.();
    const next = tabsRef.current.filter(item => item.id !== id);
    install(next);
    if (tab?.kind === 'browser') {
      if (current.current.activeBrowserId === id) {
        setActiveBrowserId(undefined);
        const fallback = next.filter(item => item.kind === 'browser' || item.sessionId === current.current.sessionId).at(-1);
        if (fallback) select(fallback);
      }
    } else if (tab) {
      setActive(value => ({ ...value, [tab.sessionId]: value[tab.sessionId] === id ? next.filter(item => item.kind !== 'browser' && item.sessionId === tab.sessionId).at(-1)?.id ?? '' : value[tab.sessionId] }));
    }
  }, [install, select]);
  const forgetSession = useCallback((target: string) => {
    for (const tab of tabsRef.current.filter(tab => tab.kind !== 'browser' && tab.sessionId === target)) tab.onClose?.();
    install(tabsRef.current.filter(tab => tab.kind === 'browser' || tab.sessionId !== target));
    setActive(value => { const next = { ...value }; delete next[target]; return next; });
    for (const key of backgroundControls.keys()) if (JSON.parse(key)[0] === target) backgroundControls.delete(key);
  }, [backgroundControls, install]);
  // Database/session cleanup cannot retire the independent user browser owner.
  if (controlRef) controlRef.current = { forgetSession, clear: () => { for (const target of new Set(tabsRef.current.flatMap(tab => tab.kind === 'browser' ? [] : [tab.sessionId]))) forgetSession(target); backgroundControls.clear(); } };
  const visibleTabs = useMemo(() => tabs.filter(tab => tab.kind === 'browser' || tab.sessionId === sessionId), [tabs, sessionId]);
  const activeId = selectedTabId(tabs, sessionId, activeBrowserId, active);
  const value = useMemo(() => ({ interact, interactionVersion, publish, sessionId, visible, browserTabs, backgroundControls, tabs: visibleTabs, activeId, open, update, close, activate }), [interact, interactionVersion, publish, backgroundControls, browserTabs, visible, visibleTabs, sessionId, activeId, open, update, close, activate]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
const icons = { browser: Globe, file: FileText, visualization: FileText, capabilities: Blocks, mcp: Server, background: TerminalSquare, process: TerminalSquare, subagents: Bot, agent: Bot, cells: FileCode2, 'cells-unavailable': FileCode2, cell: FileText, 'cell-graph': ListChecks };
function WorkspaceTabStrip({ tabs, selectedId, activate, close }: { tabs: WorkspaceTab[]; selectedId?: string; activate: (id: string) => void; close: (id: string) => void }) {
  const list = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });
  const [scrolling, setScrolling] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const measure = useCallback(() => {
    const element = list.current;
    if (!element) return;
    const left = element.scrollLeft > 1;
    const right = element.scrollWidth - element.clientWidth - element.scrollLeft > 1;
    setEdges(current => current.left === left && current.right === right ? current : { left, right });
  }, []);
  useEffect(() => {
    const element = list.current;
    if (!element) return;
    // Native scrolling owns dragging and touchpad input; observe only edge visibility.
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    for (const child of element.children) observer.observe(child);
    return () => observer.disconnect();
  }, [tabs, measure]);
  useEffect(() => {
    const element = list.current;
    const active = element?.querySelector<HTMLElement>('.is-active');
    if (!element || !active || !element.clientWidth) return;
    const viewport = element.getBoundingClientRect();
    const bounds = active.getBoundingClientRect();
    const delta = bounds.left < viewport.left + 24 ? bounds.left - viewport.left - 24 : bounds.right > viewport.right - 24 ? bounds.right - viewport.right + 24 : 0;
    if (delta) element.scrollBy({ left: delta, behavior: 'smooth' });
  }, [selectedId]);
  useEffect(() => () => clearTimeout(timer.current), []);
  const scroll = (direction: number) => {
    const element = list.current;
    if (element) element.scrollBy({ left: direction * element.clientWidth * .7, behavior: 'smooth' });
  };
  if (!tabs.length) return null;
  return <div className="workspace-tab-strip" data-scrolling={scrolling || undefined} data-overflow={edges.left || edges.right || undefined}>
    <div ref={list} className="workspace-tabs" role="tablist" aria-label="打开的内容" onScroll={() => {
      measure(); setScrolling(true); clearTimeout(timer.current);
      timer.current = setTimeout(() => setScrolling(false), 700);
    }}>{tabs.map(tab => { const Icon = icons[tab.kind]; return <div key={tab.id} className={`workspace-tab${selectedId === tab.id ? ' is-active' : ''}`}><button type="button" role="tab" aria-selected={selectedId === tab.id} onClick={() => { if (tab.id === selectedId) return; if (!document.startViewTransition || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) activate(tab.id); else { const transition = document.startViewTransition(() => flushSync(() => activate(tab.id))); void transition.finished.catch(() => undefined); } }} title={tab.title}><Icon size={13} /><span>{tab.title}</span></button><button type="button" aria-label={`关闭 ${tab.title}`} title="关闭视图" onClick={() => close(tab.id)}><X size={12} /></button></div>; })}</div>
    <button type="button" className="workspace-tab-edge is-left" aria-label="向左滚动标签页" title="向左滚动" disabled={!edges.left} onClick={() => scroll(-1)}><ChevronLeft size={14} /></button>
    <button type="button" className="workspace-tab-edge is-right" aria-label="向右滚动标签页" title="向右滚动" disabled={!edges.right} onClick={() => scroll(1)}><ChevronRight size={14} /></button>
  </div>;
}
// Keep content readable at 350px; a deliberate drag past half that width hides the pane.
const workspaceMinWidth = 350;
const workspaceCollapseWidth = workspaceMinWidth / 2;
type WorkspaceProps = { isOpen: boolean; onClose: () => void; toolbar: ReactNode; renderTab: (tab: WorkspaceTab) => ReactNode; sessionContentAvailable?: boolean };
export function DesktopWorkspace(props: WorkspaceProps) {
  const workspace = useDesktopWorkspace();
  return workspace ? <WorkspacePane {...props} /> : null;
}
function WorkspacePane({ isOpen, onClose, toolbar, renderTab, sessionContentAvailable = true }: WorkspaceProps) {
  const workspace = useDesktopWorkspace();
  const sessionId = workspace?.sessionId ?? '';
  const browserSelected = workspace?.tabs.find(tab => tab.id === workspace.activeId)?.kind === 'browser';
  const widthKey = browserSelected || !sessionId ? 'pulsara-workspace-width' : `pulsara-workspace-width:${sessionId}`;
  const readWidth = () => {
    const saved = Number(uiPreferences.getItem(widthKey));
    return Number.isFinite(saved) && saved >= workspaceMinWidth ? saved : workspaceMinWidth;
  };
  const [size, setSize] = useState(() => ({ widthKey, width: readWidth() }));
  if (size.widthKey !== widthKey) setSize({ widthKey, width: readWidth() });
  const width = size.widthKey === widthKey ? size.width : readWidth();
  const setWidth = (value: number) => setSize({ widthKey, width: value });
  const saveWidth = (value: number) => {
    uiPreferences.setItem(widthKey, String(value));
  };
  const [resizing, setResizing] = useState(false);
  const drag = useRef<{ x: number; width: number; nextWidth: number } | undefined>(undefined);
  const finishDrag = () => {
    if (drag.current) saveWidth(drag.current.nextWidth);
    drag.current = undefined;
    setResizing(false);
  };
  useEffect(() => () => {
    if (drag.current) uiPreferences.setItem(widthKey, String(drag.current.nextWidth));
    drag.current = undefined;
    setResizing(false);
  }, [widthKey]);
  if (!workspace) return null;
  const selected = workspace.tabs.find(tab => tab.id === workspace.activeId) ?? workspace.tabs.at(-1);
  const closeTab = (id: string) => {
    workspace.close(id);
    if (workspace.tabs.length === 1) onClose();
  };
  return <aside className={`desktop-workspace${isOpen ? ' is-open' : ''}${resizing ? ' is-resizing' : ''}`} style={{ '--workspace-width': `${width}px` } as React.CSSProperties} aria-label="会话标签工作区" inert={!isOpen}>
    <div className="workspace-resizer" role="separator" aria-label="调整工作区宽度" aria-orientation="vertical" tabIndex={0}
      onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); const next = Math.max(workspaceMinWidth, Math.min(window.innerWidth * .7, event.currentTarget.parentElement!.getBoundingClientRect().width + (event.key === 'ArrowLeft' ? 32 : -32))); setWidth(next); saveWidth(next); } }}
      onPointerDown={event => {
        if (event.button !== 0 || !isOpen) return;
        const currentWidth = event.currentTarget.parentElement!.getBoundingClientRect().width;
        drag.current = { x: event.clientX, width: currentWidth, nextWidth: currentWidth };
        setResizing(true);
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={event => {
        const start = drag.current;
        if (!start || !isOpen) return;
        const nextWidth = start.width + start.x - event.clientX;
        if (nextWidth <= workspaceCollapseWidth) {
          drag.current = undefined;
          setResizing(false);
          setWidth(Math.max(workspaceMinWidth, start.width));
          saveWidth(Math.max(workspaceMinWidth, start.width));
          event.currentTarget.releasePointerCapture(event.pointerId);
          onClose();
          return;
        }
        start.nextWidth = Math.max(workspaceMinWidth, Math.min(window.innerWidth * .7, nextWidth));
        setWidth(start.nextWidth);
      }}
      onPointerUp={finishDrag} onPointerCancel={finishDrag}
      onLostPointerCapture={finishDrag} />
    <div className="workspace-surface">
    <header className="workspace-topbar">
    <div className="workspace-toolbar">{!['linux', 'win32'].includes(desktopHost()?.platform ?? '') && toolbar}<button type="button" title="收起工作区" aria-label="收起工作区" onClick={onClose}><PanelRightClose size={15} /></button></div>
    </header>
    {['linux', 'win32'].includes(desktopHost()?.platform ?? '') && <div className="workspace-toolbar workspace-categories">{toolbar}</div>}
    <WorkspaceTabStrip key={sessionId} tabs={workspace.tabs} selectedId={selected?.id} activate={workspace.activate} close={closeTab} />
    <div className="workspace-content" role="tabpanel" key={selected?.id} ref={element => { if (element && selected?.view) { element.scrollTop = selected.view.top; element.scrollLeft = selected.view.left; } }} onScroll={event => { if (selected?.view) { selected.view.top = event.currentTarget.scrollTop; selected.view.left = event.currentTarget.scrollLeft; } }}>{selected?.browser ? <BrowserPanel record={selected.browser} tabs={workspace.browserTabs} isOpen={isOpen} /> : selected ? (!sessionContentAvailable ? <div className="workspace-empty" role="status"><strong>正在加载会话…</strong></div> : selected.render ? selected.render() : renderTab(selected)) : isOpen ? <div className="workspace-empty"><FolderKanban size={28} /><strong>从上方选择内容，或从对话打开文件。</strong></div> : null}</div>
    </div>
  </aside>;
}

export function inspectorTab(sessionId: string, kind: Exclude<WorkspaceTabKind, 'browser'>, title: string, entityId?: string, hostSessionId?: string): WorkspaceTab {
  const id = JSON.stringify([sessionId, kind, entityId ?? '', hostSessionId ?? '']);
  if (kind === 'agent') {
    if (!entityId || hostSessionId) throw new Error('Agent tab requires its session identity');
    return { id, sessionId, kind, title, entityId };
  }
  if (kind === 'cell') throw new Error('Source tab requires its canonical attempt identity');
  if (kind === 'cell-graph') {
    if (!entityId || !hostSessionId) throw new Error('Cell tab requires its Cell and Host identities');
    return { id, sessionId, kind, title, entityId, hostSessionId };
  }
  if (kind === 'cells') {
    if (entityId || !hostSessionId) throw new Error('Cell list requires its Host identity');
    return { id, sessionId, kind, title, hostSessionId };
  }
  if (kind === 'subagents' || kind === 'cells-unavailable') {
    if (entityId || hostSessionId) throw new Error('List tab is scoped to its ROOT');
    return { id, sessionId, kind, title };
  }
  return { id, sessionId, kind, title, entityId, hostSessionId } as WorkspaceTab;
}
