import { Copy, LoaderCircle, Minus, Square, X } from 'lucide-react';
import {
  createContext, useCallback, useContext, useLayoutEffect, useMemo, useRef, useState,
  type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import {
  applyDockResize, clamp, closeWindow, defaultFloat, dividerSpec, dockConflicts, dockDividers, dockPixels, emptyLayout,
  fillFreeDock, focusWindow, guideDockResize, maximizedWindow, minimizeWindow, placeWindow, reachableFloat, recallWindow,
  releaseDockSide, reorderTabs, resizeDockLine, resizeEdges, resizeFloat, snapRegion, toggleMaximized, wholeCanvas,
  type CanvasRect, type CanvasSize, type DockDivider, type ResizeEdge, type SessionCanvasLayout, type SessionCanvasPlacement, type WindowLayout,
} from './session-canvas-layout';
import '../app/styles/session-canvas.css';

export type { SessionCanvasPlacement } from './session-canvas-layout';

interface SessionTitle { id: string; title: string }
export interface SessionCanvasSession extends SessionTitle {
  pendingApproval?: boolean;
  running?: boolean;
  content: ReactNode;
}

interface WindowContextValue {
  inCanvas: boolean;
  visible: boolean;
  focused: boolean;
  activate?: () => void;
  identityHost?: HTMLElement | null;
  actionsHost?: HTMLElement | null;
  showIdentity?: (show: boolean) => void;
}

const WindowContext = createContext<WindowContextValue>({ inCanvas: false, visible: true, focused: true });
export function useSessionCanvasWindow() { return useContext(WindowContext); }

export function SessionCanvasTitleIdentity({ children }: { children: ReactNode }) {
  const { identityHost, showIdentity } = useSessionCanvasWindow();
  useLayoutEffect(() => {
    showIdentity?.(true);
    return () => showIdentity?.(false);
  }, [showIdentity]);
  return identityHost ? createPortal(children, identityHost) : null;
}

export function SessionCanvasTitleActions({ children }: { children: ReactNode }) {
  const { actionsHost } = useSessionCanvasWindow();
  return actionsHost ? createPortal(children, actionsHost) : null;
}

type LayoutChange = (layout: SessionCanvasLayout) => SessionCanvasLayout;
type Handoff = { id: string; rect: CanvasRect };
type Capture = (handoff?: Handoff) => void;

export function useSessionCanvas() {
  const [layout, setLayout] = useState(emptyLayout);
  const current = useRef(emptyLayout);
  const area = useRef<CanvasSize>({ w: 0, h: 0 });
  const capture = useRef<Capture | null>(null);
  const cancelGesture = useRef<(() => void) | null>(null);
  const pendingHandoffs = useRef(new Map<SessionCanvasPlacement, Handoff>());
  const sidebarDrag = useRef<((event: ReactPointerEvent, session: SessionTitle) => void) | null>(null);
  const update = useCallback((change: LayoutChange, handoff?: Handoff) => {
    const next = change(current.current);
    if (next === current.current) return;
    capture.current?.(handoff);
    current.current = next;
    setLayout(next);
  }, []);
  const place = useCallback((id: string, placement: SessionCanvasPlacement = { type: 'maximized' }) => {
    const handoff = pendingHandoffs.current.get(placement);
    pendingHandoffs.current.delete(placement);
    update(state => placeWindow(state, id, area.current, placement), handoff);
  }, [update]);
  const focus = useCallback((id: string) => update(state => focusWindow(state, id)), [update]);
  const minimize = useCallback((id: string) => update(state => minimizeWindow(state, id)), [update]);
  const close = useCallback((id: string) => update(state => closeWindow(state, id)), [update]);
  const clear = useCallback(() => {
    cancelGesture.current?.();
    pendingHandoffs.current.clear();
    update(() => emptyLayout);
  }, [update]);
  const beginSidebarDrag = useCallback((event: ReactPointerEvent, session: SessionTitle) => {
    sidebarDrag.current?.(event, session);
  }, []);
  return { layout, place, focus, minimize, close, clear, beginSidebarDrag, current, area, update, sidebarDrag, capture, cancelGesture, pendingHandoffs };
}

interface SessionCanvasProps {
  controller: ReturnType<typeof useSessionCanvas>;
  sessions: readonly SessionCanvasSession[];
  visible?: boolean;
  tabHost: HTMLElement | null;
  onOpenSession: (id: string, placement: SessionCanvasPlacement) => unknown;
  onCloseSession: (id: string) => unknown;
  emptyContent?: ReactNode;
}

interface GestureBase { pointerId: number; x: number; y: number; started: boolean }
type MoveGesture = GestureBase & {
  kind: 'move';
  source: 'sidebar' | 'tab' | 'title';
  session: SessionTitle;
  origin?: CanvasRect;
};
type Gesture = MoveGesture
  | (GestureBase & { kind: 'float-resize'; id: string; origin: CanvasRect; edge: ResizeEdge })
  | (GestureBase & { kind: 'dock-resize'; divider: DockDivider; guide: ReturnType<typeof guideDockResize> });

type MovePreview = {
  kind: 'move';
  session: SessionTitle;
  source: MoveGesture['source'];
  where: 'tabs' | 'canvas' | null;
  x: number;
  y: number;
  rect?: CanvasRect;
  dock?: CanvasRect;
  tabIndex?: number;
  markerX?: number;
};
type Preview = MovePreview
  | { kind: 'float-resize'; id: string; rect: CanvasRect }
  | { kind: 'dock-resize'; regions: ReadonlyMap<string, CanvasRect>; divider: string };

const pixels = (box: CanvasRect): CSSProperties => ({ left: box.x, top: box.y, width: box.w, height: box.h });
const percentages = (box: CanvasRect): CSSProperties => ({
  left: `${box.x * 100}%`, top: `${box.y * 100}%`, width: `${box.w * 100}%`, height: `${box.h * 100}%`,
});
const fromDom = ({ left, top, width, height }: DOMRect): CanvasRect => ({ x: left, y: top, w: width, h: height });
const contains = (box: DOMRect, x: number, y: number) => x >= box.left && x < box.right && y >= box.top && y < box.bottom;
const interactive = 'button, input, textarea, select, a, summary, [contenteditable], [data-canvas-no-drag]';
const isTitleGrip = (target: EventTarget) => target instanceof Element
  && target.closest('.session-canvas-window__title') !== null && target.closest(interactive) === null;

function windowBox(item: WindowLayout, view: CanvasSize) {
  if (item.mode === 'maximized') return { x: 0, y: 0, ...view };
  if (item.mode === 'docked' && item.dock) return dockPixels(item.dock, view);
  return reachableFloat(item.float, view);
}

function SessionCanvasWindow({
  session, mode, focused, shown, rect, z, conflict, canvasVisible, activateWindow, onMinimize, onMaximize, onTitle,
  onDoubleTitle, onResize, windowRef, rememberFocus,
}: {
  session: SessionCanvasSession;
  mode: WindowLayout['mode'] | undefined;
  focused: boolean;
  shown: boolean;
  rect: CSSProperties;
  z: number;
  conflict: boolean;
  canvasVisible: boolean;
  activateWindow: (id: string) => void;
  onMinimize: () => void;
  onMaximize: () => void;
  onTitle: (event: ReactPointerEvent) => void;
  onDoubleTitle: () => void;
  onResize: (event: ReactPointerEvent, edge: ResizeEdge) => void;
  windowRef: (node: HTMLElement | null) => void;
  rememberFocus: (id: string, element: HTMLElement) => void;
}) {
  const [identityHost, setIdentityHost] = useState<HTMLDivElement | null>(null);
  const [actionsHost, setActionsHost] = useState<HTMLDivElement | null>(null);
  const [hasIdentity, setHasIdentity] = useState(false);
  const activate = useCallback(() => { if (shown && canvasVisible) activateWindow(session.id); }, [activateWindow, canvasVisible, session.id, shown]);
  const context = useMemo<WindowContextValue>(() => ({
    inCanvas: true, visible: shown && canvasVisible, focused: focused && canvasVisible, activate,
    identityHost, actionsHost, showIdentity: setHasIdentity,
  }), [shown, canvasVisible, focused, activate, identityHost, actionsHost]);
  const maximized = mode === 'maximized';
  return (
    <WindowContext.Provider value={context}>
      <section
        ref={windowRef}
        id={session.id}
        aria-label={session.title}
        className="session-canvas-window"
        data-mode={mode}
        data-focused={focused || undefined}
        data-dock-conflict={conflict || undefined}
        hidden={!shown}
        inert={!shown || !canvasVisible}
        tabIndex={-1}
        style={{ ...rect, zIndex: z }}
        onFocusCapture={event => {
          if (event.target instanceof HTMLElement && event.target.closest('.session-canvas-window__body')) {
            rememberFocus(session.id, event.target);
          }
          activate();
        }}
        onPointerDownCapture={event => {
          if (event.button !== 0) return;
          // Title identity is rendered from the workbench through a portal.
          // Capture at the window so the same gesture handles both DOM paths.
          if (isTitleGrip(event.target)) onTitle(event);
          else activate();
        }}
        onClickCapture={event => { if (isTitleGrip(event.target)) activate(); }}
        onDoubleClickCapture={event => { if (isTitleGrip(event.target)) onDoubleTitle(); }}
      >
        <header className="session-canvas-window__title">
          <div className="session-canvas-window__identity">
            {!hasIdentity && <strong title={session.title}>{session.title}</strong>}
            <div className="session-canvas-window__identity-host" ref={setIdentityHost} />
          </div>
          <div className="session-canvas-window__actions" data-canvas-no-drag>
            <div className="session-canvas-window__actions-host" ref={setActionsHost} />
            <button type="button" aria-label={maximized ? '还原会话窗口' : '铺满会话窗口'} onClick={onMaximize}>
              {maximized ? <Copy size={13} /> : <Square size={13} />}
            </button>
            <button type="button" aria-label="收起会话窗口" onClick={onMinimize}>
              <Minus size={15} />
            </button>
          </div>
        </header>
        <div className="session-canvas-window__body">{session.content}</div>
        {mode === 'floating' && resizeEdges.map(edge => (
          <div key={edge} className="session-canvas-window__resize" data-edge={edge}
            aria-hidden="true" onPointerDown={event => onResize(event, edge)} />
        ))}
      </section>
    </WindowContext.Provider>
  );
}

const duration = { tab: 185, lift: 150, snap: 205, place: 250, open: 305, tuck: 270 };
const easing = 'cubic-bezier(.18,.78,.24,1)';
const prefersReducedMotion = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
const transform = (from: CanvasRect, to: CanvasRect) =>
  `translate(${from.x - to.x}px, ${from.y - to.y}px) scale(${from.w / to.w}, ${from.h / to.h})`;

function animateRect(node: HTMLElement, from: CanvasRect, to: CanvasRect, ms: number, initial: Keyframe = {}) {
  if (!from.w || !from.h || !to.w || !to.h || !node.animate) return;
  const difference = Math.abs(from.x - to.x) + Math.abs(from.y - to.y) + Math.abs(from.w - to.w) + Math.abs(from.h - to.h);
  if (difference < 1) return;
  return node.animate([
    { transformOrigin: '0 0', transform: transform(from, to), ...initial },
    { transformOrigin: '0 0', transform: 'none' },
  ], { duration: ms, easing });
}

type MotionSnapshot = {
  windows: Map<string, CanvasRect>;
  tabs: Map<string, CanvasRect>;
  flightRects: Map<string, CanvasRect>;
  preview?: CanvasRect;
  handoff?: Handoff;
};

export function SessionCanvas({ controller, sessions, visible = true, tabHost, onOpenSession, onCloseSession, emptyContent }: SessionCanvasProps) {
  const { layout, area, current, update, focus, minimize, capture, sidebarDrag, cancelGesture, pendingHandoffs } = controller;
  const rootRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const tabStrip = useRef<HTMLDivElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const flightLayer = useRef<HTMLDivElement>(null);
  const windows = useRef(new Map<string, HTMLElement>());
  const tabs = useRef(new Map<string, HTMLElement>());
  const [view, setView] = useState<CanvasSize>({ w: 0, h: 0 });
  const [preview, setPreview] = useState<Preview | null>(null);
  const gesture = useRef<Gesture | null>(null);
  const previewState = useRef<Preview | null>(null);
  const suppressClick = useRef(false);
  const snapshot = useRef<MotionSnapshot | null>(null);
  const motions = useRef(new Map<HTMLElement, Animation>());
  const flights = useRef(new Map<string, { node: HTMLElement; animation: Animation }>());
  const lastFocus = useRef<string | null>(null);
  const focusedElements = useRef(new Map<string, HTMLElement>());
  const rememberFocus = useCallback((id: string, element: HTMLElement) => { focusedElements.current.set(id, element); }, []);
  const openSession = useRef(onOpenSession);
  useLayoutEffect(() => { openSession.current = onOpenSession; }, [onOpenSession]);

  useLayoutEffect(() => {
    const node = canvasRef.current;
    if (!node || !visible) return;
    const measure = () => {
      const { width: w, height: h } = node.getBoundingClientRect();
      if ((w || h) && (w !== area.current.w || h !== area.current.h)) {
        area.current = { w, h };
        setView(area.current);
      }
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [area, visible]);

  const recordMotion = useCallback<Capture>((handoff) => {
    if (!rootRef.current || rootRef.current.hidden || prefersReducedMotion()) return;
    if (snapshot.current) {
      if (handoff) snapshot.current.handoff = handoff;
      return;
    }
    const measure = (nodes: Map<string, HTMLElement>) => new Map([...nodes].flatMap(([id, node]) =>
      node.hidden ? [] : [[id, fromDom(node.getBoundingClientRect())]]));
    const previewNode = previewRef.current ?? cardRef.current;
    snapshot.current = {
      windows: measure(windows.current), tabs: measure(tabs.current),
      flightRects: new Map([...flights.current].map(([id, { node }]) => [id, fromDom(node.getBoundingClientRect())])),
      preview: previewNode ? fromDom(previewNode.getBoundingClientRect()) : undefined,
      handoff,
    };
  }, []);

  useLayoutEffect(() => {
    capture.current = recordMotion;
    const activeMotions = motions.current;
    const activeFlights = flights.current;
    return () => {
      capture.current = null;
      for (const animation of activeMotions.values()) animation.cancel();
      for (const flight of activeFlights.values()) flight.animation.cancel();
    };
  }, [capture, recordMotion]);

  useLayoutEffect(() => {
    const before = snapshot.current;
    snapshot.current = null;
    if (!before || !visible || prefersReducedMotion()) return;
    const play = (node: HTMLElement, from: CanvasRect, ms: number, initial?: Keyframe) => {
      const previous = motions.current.get(node);
      previous?.cancel();
      const animation = animateRect(node, from, fromDom(node.getBoundingClientRect()), ms, initial);
      if (!animation) return;
      motions.current.set(node, animation);
      const clean = () => { if (motions.current.get(node) === animation) motions.current.delete(node); };
      void animation.finished.then(clean, clean);
    };
    const tabPositions = new Map<string, CanvasRect>();
    for (const [id, node] of tabs.current) {
      motions.current.get(node)?.cancel();
      tabPositions.set(id, fromDom(node.getBoundingClientRect()));
      const old = before.tabs.get(id);
      if (old) play(node, old, duration.tab);
    }
    for (const [id, node] of windows.current) {
      if (node.hidden) continue;
      if (preview?.kind === 'move' && preview.where === 'canvas' && preview.session.id === id && node.dataset.mode === 'floating') {
        motions.current.get(node)?.cancel();
        continue;
      }
      const flight = flights.current.get(id);
      flight?.animation.cancel();
      const existing = before.windows.get(id);
      const supplied = before.handoff?.id === id ? before.handoff.rect : undefined;
      const from = supplied ?? existing ?? before.flightRects.get(id) ?? before.tabs.get(id) ?? tabPositions.get(id);
      if (from) play(node, from, existing || supplied ? duration.place : duration.open, existing || supplied ? {} : { opacity: .6 });
    }
    for (const [id, from] of before.windows) {
      const tab = tabs.current.get(id);
      const to = tabPositions.get(id);
      if (!tab || !to || !windows.current.get(id)?.hidden || !flightLayer.current || !from.w || !from.h || !to.w || !to.h || !tab.animate) continue;
      flights.current.get(id)?.animation.cancel();
      const ghost = document.createElement('div');
      ghost.className = 'session-canvas__flight';
      ghost.textContent = tab.querySelector('.session-canvas__tab-title')?.textContent ?? '';
      Object.assign(ghost.style, { left: `${from.x}px`, top: `${from.y}px`, width: `${from.w}px`, height: `${from.h}px` });
      flightLayer.current.append(ghost);
      const animation = ghost.animate([
        { transformOrigin: '0 0', transform: 'none', opacity: .94 },
        { transformOrigin: '0 0', transform: transform(to, from), opacity: .22 },
      ], { duration: duration.tuck, easing });
      flights.current.set(id, { node: ghost, animation });
      const clean = () => {
        ghost.remove();
        if (flights.current.get(id)?.animation === animation) flights.current.delete(id);
      };
      void animation.finished.then(clean, clean);
      tab.animate([{ boxShadow: 'none' }, { boxShadow: 'inset 0 0 0 1px var(--blue)', offset: .35 }, { boxShadow: 'none' }],
        { duration: 215, delay: duration.tuck - 70, easing });
    }
    const target = previewRef.current;
    if (target && preview?.kind === 'move') {
      const from = before.preview ?? before.windows.get(preview.session.id) ?? before.tabs.get(preview.session.id);
      if (from) play(target, from, preview.dock ? duration.snap : duration.lift);
    }
  }, [layout, preview, visible]);

  useLayoutEffect(() => {
    const old = lastFocus.current;
    lastFocus.current = layout.focusedId;
    if (!visible || old === layout.focusedId) return;
    const node = layout.focusedId ? windows.current.get(layout.focusedId) : undefined;
    const previousElement = layout.focusedId ? focusedElements.current.get(layout.focusedId) : undefined;
    const active = document.activeElement;
    const prior = old ? windows.current.get(old) : undefined;
    if (node && !node.contains(active)
      && (active === document.body || prior?.contains(active) || tabStrip.current?.contains(active) || active?.closest('.session-item'))) {
      const target = previousElement && node.contains(previousElement) && !previousElement.closest('[hidden], [inert]')
        ? previousElement : node;
      target.focus({ preventScroll: true });
    }
  }, [layout.focusedId, visible]);

  useLayoutEffect(() => {
    const live = new Set(sessions.map(session => session.id));
    for (const id of focusedElements.current.keys()) if (!live.has(id)) focusedElements.current.delete(id);
  }, [sessions]);

  const begin = useCallback((event: ReactPointerEvent, next: Omit<MoveGesture, keyof GestureBase>
    | Omit<Extract<Gesture, { kind: 'float-resize' }>, keyof GestureBase>
    | Omit<Extract<Gesture, { kind: 'dock-resize' }>, keyof GestureBase>) => {
    if (event.button !== 0 || gesture.current || event.pointerType === 'touch') return;
    if (next.kind === 'move' && (next.source === 'sidebar' || next.source === 'title')) event.preventDefault();
    suppressClick.current = false;
    gesture.current = { ...next, pointerId: event.pointerId, x: event.clientX, y: event.clientY, started: false };
  }, []);
  useLayoutEffect(() => {
    sidebarDrag.current = visible ? (event, session) => begin(event, { kind: 'move', source: 'sidebar', session }) : null;
    return () => { sidebarDrag.current = null; };
  }, [begin, sidebarDrag, visible]);

  useLayoutEffect(() => {
    if (!visible) return;
    let frame = 0;
    let pending: PointerEvent | undefined;
    const target = canvasRef.current;
    const changePreview = (next: Preview | null) => {
      previewState.current = next;
      setPreview(next);
    };
    const reset = () => {
      const drag = gesture.current;
      gesture.current = null;
      cancelAnimationFrame(frame);
      frame = 0;
      pending = undefined;
      if (drag?.started && target?.hasPointerCapture?.(drag.pointerId)) target.releasePointerCapture(drag.pointerId);
      changePreview(null);
    };
    const cancel = () => {
      if (!gesture.current) return;
      if (gesture.current.started) {
        suppressClick.current = true;
        recordMotion();
      }
      reset();
    };
    cancelGesture.current = cancel;
    const paint = (event: PointerEvent) => {
      const drag = gesture.current;
      if (!drag?.started || !target || event.pointerId !== drag.pointerId) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (drag.kind === 'float-resize') {
        changePreview({ kind: drag.kind, id: drag.id, rect: resizeFloat(drag.origin, drag.edge, dx, dy, area.current) });
        return;
      }
      if (drag.kind === 'dock-resize') {
        const { coordinate, span, low, high, collapseAt } = drag.guide;
        const delta = coordinate === 'x' ? dx : dy;
        const raw = drag.divider.line + delta / span;
        if ((delta < 0 && raw <= collapseAt) || (delta > 0 && raw >= 1 - collapseAt)) {
          suppressClick.current = true;
          update(state => releaseDockSide(state, drag.divider, delta < 0, area.current));
          reset();
        } else {
          changePreview({ kind: drag.kind, divider: drag.divider.key,
            regions: resizeDockLine(drag.divider, clamp(raw, low, high)) });
        }
        return;
      }
      const canvasBounds = target.getBoundingClientRect();
      const stripBounds = tabStrip.current?.getBoundingClientRect();
      const view = area.current;
      const state = current.current;
      const previous = previewState.current;
      const canvasPoint = { x: event.clientX - canvasBounds.left, y: event.clientY - canvasBounds.top };
      const overNavigation = document.elementFromPoint(event.clientX, event.clientY)?.closest('.session-sidebar, .activity-rail');
      const inCanvas = !overNavigation && contains(canvasBounds, event.clientX, event.clientY);
      const inTabs = !overNavigation && drag.source !== 'title' && stripBounds && contains(stripBounds, event.clientX, event.clientY);
      const next: MovePreview = {
        kind: 'move', session: drag.session, source: drag.source, where: inTabs ? 'tabs' : inCanvas ? 'canvas' : null,
        x: event.clientX, y: event.clientY,
      };
      if (inTabs) {
        const candidates = state.tabs.filter(id => id !== drag.session.id)
          .flatMap(id => { const node = tabs.current.get(id); return node ? [node.getBoundingClientRect()] : []; });
        const nextIndex = candidates.findIndex(box => event.clientX < box.left + box.width / 2);
        next.tabIndex = nextIndex < 0 ? candidates.length : nextIndex;
        const adjacent = nextIndex < 0 ? candidates.at(-1)?.right : candidates[nextIndex].left;
        next.markerX = clamp(adjacent ?? stripBounds.left + 8, stripBounds.left + 2, stripBounds.right - 2) - stripBounds.left;
      } else if (inCanvas) {
        const item = state.windows.get(drag.session.id);
        const float = item?.float ?? defaultFloat(view, state.tabs.length);
        const grab = drag.source === 'title' && drag.origin
          ? clamp(drag.x - canvasBounds.left - drag.origin.x, 16, float.w - 30)
          : Math.min(float.w * .36, 145);
        const origin = drag.source === 'title' && item?.mode === 'floating' ? drag.origin : undefined;
        next.rect = reachableFloat(origin ? { ...origin, x: origin.x + dx, y: origin.y + dy }
          : { ...float, x: canvasPoint.x - grab, y: canvasPoint.y - 16 }, view);
        next.dock = snapRegion(canvasPoint, view, previous?.kind === 'move' ? previous.dock : undefined);
      }
      const priorMove = previous?.kind === 'move' ? previous : undefined;
      const oldDock = priorMove?.dock;
      if (next.where !== priorMove?.where || oldDock?.x !== next.dock?.x || oldDock?.y !== next.dock?.y
        || oldDock?.w !== next.dock?.w || oldDock?.h !== next.dock?.h) recordMotion();
      changePreview(next);
    };
    const flush = () => {
      frame = 0;
      if (pending) paint(pending);
      pending = undefined;
    };
    const move = (event: PointerEvent) => {
      const drag = gesture.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      if (!drag.started) {
        if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 5) return;
        drag.started = true;
        target?.setPointerCapture?.(event.pointerId);
      }
      if (event.cancelable) event.preventDefault();
      pending = event;
      if (!frame) frame = requestAnimationFrame(flush);
    };
    const finish = (event: PointerEvent) => {
      const drag = gesture.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      if (!drag.started) { reset(); return; }
      paint(event);
      const next = previewState.current;
      if (!gesture.current) return;
      suppressClick.current = true;
      if (next?.kind === 'float-resize') {
        update(state => placeWindow(state, next.id, area.current, { type: 'floating', rect: next.rect }));
      } else if (next?.kind === 'dock-resize') {
        update(state => applyDockResize(state, next.regions));
      } else if (next?.kind === 'move' && next.where === 'tabs' && next.tabIndex !== undefined) {
        const id = next.session.id;
        const index = next.tabIndex;
        if (current.current.windows.has(id)) update(state => reorderTabs(state, id, index));
        else void openSession.current(id, { type: 'maximized', tabIndex: index });
      } else if (next?.kind === 'move' && next.where === 'canvas' && next.rect) {
        const id = next.session.id;
        const placement: SessionCanvasPlacement = next.dock ? { type: 'docked', rect: next.dock }
          : { type: 'floating', rect: next.rect };
        const source = previewRef.current ?? windows.current.get(id);
        const handoff = source ? { id, rect: fromDom(source.getBoundingClientRect()) } : undefined;
        if (current.current.windows.has(id)) {
          update(state => placeWindow(state, id, area.current, placement), handoff);
        } else {
          if (handoff) pendingHandoffs.current.set(placement, handoff);
          const removeHandoff = () => { pendingHandoffs.current.delete(placement); };
          void Promise.resolve(openSession.current(id, placement)).then(removeHandoff, removeHandoff);
        }
      } else {
        recordMotion();
      }
      reset();
    };
    const keydown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !gesture.current?.started) return;
      event.preventDefault();
      event.stopPropagation();
      cancel();
    };
    const pointerCancel = (event: PointerEvent) => {
      if (event.pointerId === gesture.current?.pointerId) cancel();
    };
    const click = (event: MouseEvent) => {
      if (!suppressClick.current || !event.detail) return;
      suppressClick.current = false;
      event.preventDefault();
      event.stopPropagation();
    };
    const pointerDown = () => { suppressClick.current = false; };
    document.addEventListener('pointerdown', pointerDown, true);
    document.addEventListener('pointermove', move, { passive: false });
    document.addEventListener('pointerup', finish);
    document.addEventListener('pointercancel', pointerCancel);
    document.addEventListener('keydown', keydown, true);
    document.addEventListener('click', click, true);
    window.addEventListener('blur', cancel);
    return () => {
      cancelGesture.current = null;
      reset();
      document.removeEventListener('pointerdown', pointerDown, true);
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', finish);
      document.removeEventListener('pointercancel', pointerCancel);
      document.removeEventListener('keydown', keydown, true);
      document.removeEventListener('click', click, true);
      window.removeEventListener('blur', cancel);
    };
  }, [area, cancelGesture, current, pendingHandoffs, recordMotion, update, visible]);

  const byId = new Map(sessions.map(session => [session.id, session]));
  const moving = preview?.kind === 'move' && preview.where === 'canvas' ? preview : null;
  const movingId = moving?.session.id;
  const movingShown = movingId !== undefined && layout.admitted.includes(movingId);
  const full = maximizedWindow(layout);
  const conflicts = moving?.dock ? dockConflicts(layout, moving.session.id, moving.dock) : [];
  const dividers = dockDividers(layout);
  const dockChanges = preview?.kind === 'dock-resize' ? preview.regions : undefined;

  const tabBar = visible && tabHost ? createPortal(
      <div className="session-canvas__tabs" data-dragging={preview ? true : undefined}>
        <div ref={tabStrip} className="session-canvas__tab-strip" role="tablist" aria-label="已载入会话">
          {layout.tabs.map(id => {
            const session = byId.get(id);
            if (!session) return null;
            const selected = layout.focusedId === id;
            return (
              <div key={id} className="session-canvas__tab" data-selected={selected || undefined}
                data-placed={layout.admitted.includes(id) || undefined}
                ref={node => { if (node) tabs.current.set(id, node); else tabs.current.delete(id); }}>
                <button type="button" className="session-canvas__tab-select" role="tab" aria-selected={selected}
                  aria-controls={id} title={session.title}
                  onClick={() => update(state => recallWindow(state, id, area.current))}
                  onPointerDown={event => begin(event, { kind: 'move', source: 'tab', session })}>
                  {session.pendingApproval ? <span className="session-canvas__approval" title="等待审批" aria-label="等待审批" />
                    : session.running ? <LoaderCircle size={12} className="is-spinning" aria-label="正在处理" />
                      : <span className="session-canvas__tab-dot" aria-hidden="true" />}
                  <span className="session-canvas__tab-title">{session.title}</span>
                </button>
                <button type="button" className="session-canvas__tab-close" aria-label={`关闭会话 ${session.title}`} title="关闭会话"
                  onClick={() => void onCloseSession(id)}><X size={13} /></button>
              </div>
            );
          })}
        </div>
        {preview?.kind === 'move' && preview.markerX !== undefined && <div className="session-canvas__insertion" style={{ left: preview.markerX }} />}
      </div>,
      tabHost,
  ) : null;

  return (
    <div ref={rootRef} className="session-canvas" hidden={!visible} data-dragging={preview ? true : undefined}>
      {tabBar}
      <div className="session-canvas__surface" ref={canvasRef}>
        {layout.admitted.length === 0 && emptyContent && <div className="session-canvas__empty">{emptyContent}</div>}
        {sessions.map(session => {
          const { id } = session;
          const item = layout.windows.get(id);
          const isMoving = movingId === id && movingShown;
          const shrunk = !!moving && full === id && !isMoving;
          const floatSize = preview?.kind === 'float-resize' && preview.id === id ? preview.rect : undefined;
          const floating = isMoving ? moving?.rect : floatSize ?? (shrunk && item ? reachableFloat(item.float, view) : undefined);
          const dock = dockChanges?.get(id) ?? item?.dock;
          const rect = floating ? pixels(floating) : item?.mode === 'maximized' ? percentages(wholeCanvas)
            : item?.mode === 'docked' && dock ? percentages(dock) : item ? pixels(reachableFloat(item.float, view)) : {};
          return <SessionCanvasWindow key={id} session={session} mode={item?.mode}
            shown={layout.admitted.includes(id)} focused={layout.focusedId === id} canvasVisible={visible}
            rect={rect} z={isMoving ? 10 : (layout.stack.indexOf(id) + 1) * 2}
            conflict={conflicts.includes(id)}
            windowRef={node => { if (node) windows.current.set(id, node); else windows.current.delete(id); }}
            activateWindow={focus} rememberFocus={rememberFocus} onMinimize={() => minimize(id)}
            onMaximize={() => update(state => toggleMaximized(state, id, area.current))}
            onDoubleTitle={() => update(state => state.windows.get(id)?.mode === 'floating'
              ? fillFreeDock(state, id, area.current) : toggleMaximized(state, id, area.current))}
            onTitle={event => { if (item) begin(event, { kind: 'move', source: 'title', session, origin: windowBox(item, view) }); }}
            onResize={(event, edge) => { if (item) begin(event, { kind: 'float-resize', id, edge, origin: windowBox(item, view) }); }}
          />;
        })}
        {dividers.filter(divider => !divider.participants.some(part => part.id === movingId)).map(divider => {
          const { axis, rect } = dividerSpec(divider, view, dockChanges);
          const z = Math.max(...divider.participants.map(part => layout.stack.indexOf(part.id) + 1)) * 2 + 1;
          return <div key={divider.key} role="separator" aria-label="调整停靠窗口大小"
            aria-orientation={axis === 'x' ? 'vertical' : 'horizontal'} data-axis={axis}
            className="session-canvas__divider"
            data-active={preview?.kind === 'dock-resize' && preview.divider === divider.key || undefined}
            style={{ ...pixels(rect), zIndex: z }}
            onPointerDown={event => begin(event, { kind: 'dock-resize', divider, guide: guideDockResize(layout, divider, view) })} />;
        })}
        {moving && (moving.dock || !movingShown) && (
          <div className="session-canvas__preview" ref={previewRef} aria-hidden="true" data-snap={!!moving.dock || undefined}
            style={moving.dock ? percentages(moving.dock) : moving.rect ? pixels(moving.rect) : undefined}>
            <strong>{moving.session.title}</strong>
          </div>
        )}
      </div>
      {preview?.kind === 'move' && preview.source !== 'title' && preview.where !== 'canvas' && (
        <div className="session-canvas__drag-card" ref={cardRef} aria-hidden="true" style={{ left: preview.x + 12, top: preview.y + 15 }}>
          {preview.session.title}
        </div>
      )}
      <div ref={flightLayer} className="session-canvas__flights" aria-hidden="true" />
    </div>
  );
}
