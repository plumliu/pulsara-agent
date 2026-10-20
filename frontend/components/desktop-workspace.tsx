import { BrowserTabs, browserTitle, type BrowserTabRecord } from '../lib/browser-tabs';
import { desktopHost } from '../lib/desktop-host';
import { BrowserPanel } from './browser-panel';
import { uiPreferences } from '../lib/ui-preferences';
import type { CommandReceipt, UserControlCommandRef } from '../lib/runtime-adapter';
import { Blocks, Bot, ChevronLeft, ChevronRight, Globe, FileText, FolderKanban, ListChecks, PanelRightClose, Server, TerminalSquare, X } from 'lucide-react';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react';

export type WorkspaceTabKind = 'browser' | 'file' | 'visualization' | 'tasks' | 'group' | 'task' | 'capabilities' | 'mcp' | 'background' | 'process';
export interface WorkspaceControl { forgetSession: (sessionId: string) => void; clear: () => void }
export interface WorkspaceTab {
  browser?: BrowserTabRecord;
  view?: { top: number; left: number; taskId?: string; reader?: { top: number; left: number }; graph?: { top: number; left: number; zoom?: number } };
  id: string;
  sessionId: string;
  title: string;
  kind: WorkspaceTabKind;
  entityId?: string;
  hostSessionId?: string;
  render?: () => ReactNode;
  onClose?: () => void;
}
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
  update: (id: string, patch: Partial<WorkspaceTab>) => void;
  close: (id: string) => void;
  activate: (id: string) => void;
}
const Context = createContext<WorkspaceContext | undefined>(undefined);
export function useDesktopWorkspace() { return useContext(Context); }

/** Disposable view descriptions only. Execution and control references stay with their owners. */
export function DesktopWorkspaceProvider({ sessionId, onShow, children, controlRef, visible = true, onBrowserShortcut }: { visible?: boolean; onBrowserShortcut?: (command: 'search' | 'new-session') => void; sessionId: string; onShow: () => void; children: ReactNode; controlRef?: RefObject<WorkspaceControl | null> }) {
  const backgroundControls = useRef(new Map<string, BackgroundControlMemory>()).current;
  const [tabs, setTabs] = useState<WorkspaceTab[]>([]);
  const [active, setActive] = useState<Record<string, string>>({});
  const tabsRef = useRef(tabs); tabsRef.current = tabs;
  const interaction = useRef(0);
  const interact = useCallback(() => { interaction.current++; }, []);
  const interactionVersion = useCallback(() => interaction.current, []);
  const show = useRef(onShow); show.current = onShow;
  const current = useRef({ sessionId, visible, active, onBrowserShortcut }); current.current = { sessionId, visible, active, onBrowserShortcut };
  const activate = useCallback((id: string) => {
    interaction.current++;
    const tab = tabsRef.current.find(tab => tab.id === id);
    if (tab) { setActive(current => ({ ...current, [tab.sessionId]: id })); if (current.current.sessionId === tab.sessionId && current.current.visible) show.current(); }
  }, []);
  const open = useCallback((tab: WorkspaceTab, options?: { background?: boolean }) => {
    interaction.current++;
    tab = { view: tabsRef.current.find(item => item.id === tab.id)?.view ?? { top: 0, left: 0, graph: { top: 0, left: 0 } }, ...tab };
    setTabs(current => current.some(item => item.id === tab.id) ? current.map(item => item.id === tab.id ? { ...item, ...tab } : item) : [...current, tab]);
    if (!options?.background) { setActive(current => ({ ...current, [tab.sessionId]: tab.id })); if (current.current.sessionId === tab.sessionId && current.current.visible) show.current(); }
  }, []);
  const publish = useCallback((batch: WorkspaceTab[], foreground: boolean) => {
    if (!batch.length || batch.some(tab => tab.sessionId !== current.current.sessionId) || !current.current.visible) return;
    const next = [...tabsRef.current];
    for (const tab of batch) if (!next.some(item => item.id === tab.id)) next.push({ view: { top: 0, left: 0 }, ...tab });
    tabsRef.current = next;
    setTabs(next);
    if (foreground) { const last = batch.at(-1)!; setActive(value => ({ ...value, [last.sessionId]: last.id })); show.current(); }
  }, []);
  const update = useCallback((id: string, patch: Partial<WorkspaceTab>) => {
    setTabs(current => current.map(item => item.id === id ? { ...item, ...patch } : item));
  }, []);
  const [browserTabs] = useState(() => new BrowserTabs(desktopHost()?.browser, {
    visibleSession: () => current.current.visible ? current.current.sessionId : '',
    activeId: target => current.current.active[target],
    insert: (record, foreground) => open({ id: record.id, sessionId: record.sessionId, kind: 'browser', title: browserTitle(record), browser: record, onClose: () => browserTabs.close(record.id) }, { background: !foreground }),
    changed: record => update(record.id, { title: browserTitle(record), browser: record }),
    activate,
    shortcut: command => current.current.onBrowserShortcut?.(command),
  }));
  useEffect(() => browserTabs.connect(), [browserTabs]);
  useEffect(() => () => browserTabs.clear(), [browserTabs]);
  const close = useCallback((id: string) => {
    interaction.current++;
    const tab = tabsRef.current.find(item => item.id === id);
    tab?.onClose?.();
    setTabs(current => current.filter(item => item.id !== id));
    if (tab) setActive(current => ({ ...current, [tab.sessionId]: current[tab.sessionId] === id ? tabsRef.current.filter(item => item.sessionId === tab.sessionId && item.id !== id).at(-1)?.id ?? '' : current[tab.sessionId] }));
  }, []);
  const forgetSession = useCallback((target: string) => {
    browserTabs.forgetSession(target);
    for (const tab of tabsRef.current.filter(tab => tab.sessionId === target)) tab.onClose?.();
    setTabs(current => current.filter(tab => tab.sessionId !== target));
    setActive(current => { const next = { ...current }; delete next[target]; return next; });
    for (const key of backgroundControls.keys()) if (JSON.parse(key)[0] === target) backgroundControls.delete(key);
  }, [backgroundControls, browserTabs]);
  if (controlRef) controlRef.current = { forgetSession, clear: () => { browserTabs.clear(); for (const target of new Set(tabsRef.current.map(tab => tab.sessionId))) forgetSession(target); backgroundControls.clear(); } };
  const value = useMemo(() => ({ interact, interactionVersion, publish, sessionId, visible, browserTabs, backgroundControls, tabs: tabs.filter(tab => tab.sessionId === sessionId), activeId: active[sessionId], open, update, close, activate }), [interact, interactionVersion, publish, backgroundControls, browserTabs, visible, tabs, sessionId, active, open, update, close, activate]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
const icons = { browser: Globe, file: FileText, visualization: FileText, tasks: ListChecks, group: FolderKanban, task: Bot, capabilities: Blocks, mcp: Server, background: TerminalSquare, process: TerminalSquare };
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
    }}>{tabs.map(tab => { const Icon = icons[tab.kind]; return <div key={tab.id} className={`workspace-tab${selectedId === tab.id ? ' is-active' : ''}`}><button type="button" role="tab" aria-selected={selectedId === tab.id} onClick={() => activate(tab.id)} title={tab.title}><Icon size={13} /><span>{tab.title}</span></button><button type="button" aria-label={`关闭 ${tab.title}`} title="关闭视图" onClick={() => close(tab.id)}><X size={12} /></button></div>; })}</div>
    <button type="button" className="workspace-tab-edge is-left" aria-label="向左滚动标签页" title="向左滚动" disabled={!edges.left} onClick={() => scroll(-1)}><ChevronLeft size={14} /></button>
    <button type="button" className="workspace-tab-edge is-right" aria-label="向右滚动标签页" title="向右滚动" disabled={!edges.right} onClick={() => scroll(1)}><ChevronRight size={14} /></button>
  </div>;
}
// Keep content readable at 320px; a deliberate drag past half that width hides the pane.
const workspaceMinWidth = 320;
const workspaceCollapseWidth = workspaceMinWidth / 2;
type WorkspaceProps = { isOpen: boolean; onClose: () => void; toolbar: ReactNode; renderTab: (tab: WorkspaceTab) => ReactNode };
export function DesktopWorkspace(props: WorkspaceProps) {
  const workspace = useDesktopWorkspace();
  return workspace ? <WorkspacePane {...props} /> : null;
}
function WorkspacePane({ isOpen, onClose, toolbar, renderTab }: WorkspaceProps) {
  const workspace = useDesktopWorkspace();
  const sessionId = workspace?.sessionId ?? '';
  const readWidth = () => {
    const saved = Number(uiPreferences.getItem(`pulsara-workspace-width:${sessionId}`));
    return Number.isFinite(saved) && saved >= workspaceMinWidth ? saved : workspaceMinWidth;
  };
  const [size, setSize] = useState(() => ({ sessionId, width: readWidth() }));
  if (size.sessionId !== sessionId) setSize({ sessionId, width: readWidth() });
  const width = size.sessionId === sessionId ? size.width : readWidth();
  const setWidth = (value: number) => setSize({ sessionId, width: value });
  const saveWidth = (value: number) => {
    if (sessionId) uiPreferences.setItem(`pulsara-workspace-width:${sessionId}`, String(value));
  };
  const [resizing, setResizing] = useState(false);
  const drag = useRef<{ x: number; width: number; nextWidth: number } | undefined>(undefined);
  const finishDrag = () => {
    if (drag.current) saveWidth(drag.current.nextWidth);
    drag.current = undefined;
    setResizing(false);
  };
  useEffect(() => () => {
    if (sessionId && drag.current) uiPreferences.setItem(`pulsara-workspace-width:${sessionId}`, String(drag.current.nextWidth));
    drag.current = undefined;
    setResizing(false);
  }, [sessionId]);
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
    <div className="workspace-toolbar">{desktopHost()?.platform !== 'linux' && toolbar}<button type="button" title="收起工作区" aria-label="收起工作区" onClick={onClose}><PanelRightClose size={15} /></button></div>
    </header>
    {desktopHost()?.platform === 'linux' && <div className="workspace-toolbar workspace-categories">{toolbar}</div>}
    <WorkspaceTabStrip key={sessionId} tabs={workspace.tabs} selectedId={selected?.id} activate={workspace.activate} close={closeTab} />
    <div className="workspace-content" role="tabpanel" key={selected?.id} ref={element => { if (element && selected?.view) { element.scrollTop = selected.view.top; element.scrollLeft = selected.view.left; } }} onScroll={event => { if (selected?.view) { selected.view.top = event.currentTarget.scrollTop; selected.view.left = event.currentTarget.scrollLeft; } }}>{selected?.browser ? <BrowserPanel record={selected.browser} tabs={workspace.browserTabs} isOpen={isOpen} /> : selected ? (selected.render ? selected.render() : renderTab(selected)) : isOpen ? <div className="workspace-empty"><FolderKanban size={28} /><strong>打开能力、任务或后台进程，也可以从对话打开文件。</strong></div> : null}</div>
    </div>
  </aside>;
}

export function inspectorTab(sessionId: string, kind: WorkspaceTabKind, title: string, entityId?: string, hostSessionId?: string): WorkspaceTab {
  return { id: JSON.stringify([sessionId, kind, entityId ?? '', hostSessionId ?? '']), sessionId, kind, title, entityId, hostSessionId };
}
