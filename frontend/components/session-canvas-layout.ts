export interface CanvasRect { x: number; y: number; w: number; h: number }
export interface CanvasSize { w: number; h: number }
type WindowPlacement =
  | { type: 'floating'; rect: CanvasRect }
  | { type: 'docked'; rect: CanvasRect }
  | { type: 'maximized' };
export type SessionCanvasPlacement = WindowPlacement | { type: 'open'; tabIndex?: number };

export interface WindowLayout {
  mode: WindowPlacement['type'];
  float: CanvasRect;
  dock?: CanvasRect;
}

export interface SessionCanvasLayout {
  tabs: readonly string[];
  windows: ReadonlyMap<string, WindowLayout>;
  admitted: readonly string[];
  stack: readonly string[];
  focusedId: string | null;
}

// Floating bounds are the conversation minimum in section 1 of the canvas contract.
export const floatMinimum = { w: 480, h: 420 };
// The canvas prototype's usable compact conversation bounds, required by section 3.
export const dockMinimum = { w: 260, h: 190 };
const maximumPlaced = 4;
const epsilon = 1e-8;
export const wholeCanvas = { x: 0, y: 0, w: 1, h: 1 };

export const emptyLayout: SessionCanvasLayout = {
  tabs: [], windows: new Map(), admitted: [], stack: [], focusedId: null,
};

export const right = (box: CanvasRect) => box.x + box.w;
export const bottom = (box: CanvasRect) => box.y + box.h;
export const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value));
const sameLine = (a: number, b: number) => Math.abs(a - b) < epsilon;

export function dockPixels(box: CanvasRect, view: CanvasSize): CanvasRect {
  return { x: box.x * view.w, y: box.y * view.h, w: box.w * view.w, h: box.h * view.h };
}

export function defaultFloat(view: CanvasSize, index: number): CanvasRect {
  const offset = index % 7;
  return {
    x: 35 + 25 * offset,
    y: 37 + 23 * offset,
    w: clamp(view.w * .49, floatMinimum.w, 638),
    h: clamp(view.h * .71, floatMinimum.h, 572),
  };
}

export function reachableFloat(box: CanvasRect, view: CanvasSize): CanvasRect {
  const grip = Math.min(96, view.w / 2);
  return {
    ...box,
    x: clamp(box.x, grip + Math.min(132, view.w / 2) - box.w, view.w - grip),
    y: clamp(box.y, 0, Math.max(0, view.h - 36)),
  };
}

const overlap = (a: CanvasRect, b: CanvasRect, key: 'x' | 'y', size: 'w' | 'h') =>
  Math.min(a[key] + a[size], b[key] + b[size]) - Math.max(a[key], b[key]);

const intersects = (a: CanvasRect, b: CanvasRect) =>
  overlap(a, b, 'x', 'w') > epsilon && overlap(a, b, 'y', 'h') > epsilon;

export function dockConflicts(layout: SessionCanvasLayout, id: string, region: CanvasRect) {
  return layout.admitted.filter(other => {
    const item = layout.windows.get(other);
    return other !== id && item?.mode === 'docked' && !!item.dock && intersects(region, item.dock);
  });
}

export function reorderTabs(layout: SessionCanvasLayout, id: string, index: number) {
  if (!layout.windows.has(id)) return layout;
  const tabs = layout.tabs.filter(tab => tab !== id);
  tabs.splice(index, 0, id);
  return tabs.every((tab, at) => tab === layout.tabs[at]) ? layout : { ...layout, tabs };
}

export function focusWindow(layout: SessionCanvasLayout, id: string) {
  if (layout.focusedId === id || !layout.stack.includes(id)) return layout;
  return { ...layout, stack: [...layout.stack.filter(tab => tab !== id), id], focusedId: id };
}

function removeWindows(layout: SessionCanvasLayout, ids: ReadonlySet<string>, close: boolean): SessionCanvasLayout {
  if (!ids.size) return layout;
  const windows = close ? new Map([...layout.windows].filter(([id]) => !ids.has(id))) : layout.windows;
  const stack = layout.stack.filter(id => !ids.has(id));
  return {
    ...layout, windows, stack, focusedId: stack.at(-1) ?? null,
    admitted: layout.admitted.filter(id => !ids.has(id)),
    tabs: close ? layout.tabs.filter(id => !ids.has(id)) : layout.tabs,
  };
}

export function minimizeWindow(layout: SessionCanvasLayout, id: string) {
  return layout.admitted.includes(id) ? removeWindows(layout, new Set([id]), false) : layout;
}

export function closeWindow(layout: SessionCanvasLayout, id: string) {
  return layout.windows.has(id) ? removeWindows(layout, new Set([id]), true) : layout;
}

function restoredPlacement(layout: SessionCanvasLayout, id: string, float: CanvasRect, dock?: CanvasRect): WindowPlacement {
  return dock && !dockConflicts(layout, id, dock).length ? { type: 'docked', rect: dock } : { type: 'floating', rect: float };
}

export function placeWindow(
  layout: SessionCanvasLayout,
  id: string,
  view: CanvasSize,
  placement: SessionCanvasPlacement = { type: 'open' },
): SessionCanvasLayout {
  const prior = layout.windows.get(id);
  const alreadyPlaced = layout.admitted.includes(id);
  if (placement.type === 'open' && alreadyPlaced) {
    const focused = focusWindow(layout, id);
    return placement.tabIndex === undefined ? focused : reorderTabs(focused, id, placement.tabIndex);
  }
  const float = prior?.float ?? defaultFloat(view, layout.tabs.length);
  let target: WindowPlacement;
  if (placement.type === 'open') {
    target = prior?.mode === 'maximized' ? { type: 'maximized' } : restoredPlacement(layout, id, float, prior?.dock);
  } else {
    target = placement;
  }
  const removed = new Set(target.type === 'docked' ? dockConflicts(layout, id, target.rect) : []);
  if (!alreadyPlaced && layout.admitted.length - removed.size >= maximumPlaced) {
    const oldest = layout.admitted.find(other => !removed.has(other));
    if (oldest) removed.add(oldest);
  }
  const retained = removeWindows(layout, removed, false);
  const windows = new Map(retained.windows);
  windows.set(id, {
    float: target.type === 'floating' ? target.rect : float,
    mode: target.type,
    dock: target.type === 'docked' ? target.rect : target.type === 'maximized' ? prior?.dock : undefined,
  });
  const next = {
    tabs: prior ? retained.tabs : [...retained.tabs, id],
    windows,
    admitted: alreadyPlaced ? retained.admitted : [...retained.admitted, id],
    stack: [...retained.stack.filter(other => other !== id), id],
    focusedId: id,
  };
  return placement.type === 'open' && placement.tabIndex !== undefined
    ? reorderTabs(next, id, placement.tabIndex) : next;
}

export function toggleMaximized(layout: SessionCanvasLayout, id: string, view: CanvasSize) {
  const item = layout.windows.get(id);
  if (!item) return layout;
  return placeWindow(layout, id, view, item.mode === 'maximized'
    ? restoredPlacement(layout, id, item.float, item.dock) : { type: 'maximized' });
}

export function snapRegion(point: { x: number; y: number }, view: CanvasSize, previous?: CanvasRect) {
  const near = (distance: number, sticky: boolean) => distance <= (sticky ? 39 : 30);
  const left = near(point.x, previous?.w === .5 && previous.x === 0);
  const rightEdge = !left && near(view.w - point.x, previous?.w === .5 && previous.x === .5);
  const top = near(point.y, previous?.h === .5 && previous.y === 0);
  const bottomEdge = !top && near(view.h - point.y, previous?.h === .5 && previous.y === .5);
  if (!left && !rightEdge && !top && !bottomEdge) return;
  const halfX = left || rightEdge || point.x < 69 || view.w - point.x < 69;
  const halfY = top || bottomEdge || point.y < 69 || view.h - point.y < 69;
  return {
    x: halfX && (rightEdge || (!left && view.w - point.x < point.x)) ? .5 : 0,
    y: halfY && (bottomEdge || (!top && view.h - point.y < point.y)) ? .5 : 0,
    w: halfX ? .5 : 1,
    h: halfY ? .5 : 1,
  };
}

export function fillFreeDock(layout: SessionCanvasLayout, id: string, view: CanvasSize) {
  const item = layout.windows.get(id);
  if (item?.mode !== 'floating' || !view.w || !view.h) return layout;
  const origin = reachableFloat(item.float, view);
  const centerX = origin.x + origin.w / 2;
  const centerY = origin.y + origin.h / 2;
  const docks = layout.admitted.flatMap(other => {
    const dock = layout.windows.get(other);
    return dock?.mode === 'docked' && dock.dock ? [dock.dock] : [];
  });
  const fits = (box: CanvasRect) => box.w * view.w >= dockMinimum.w && box.h * view.h >= dockMinimum.h
    && !docks.some(dock => intersects(box, dock));
  const distance = (box: CanvasRect) => ((box.x + box.w / 2) * view.w - centerX) ** 2
    + ((box.y + box.h / 2) * view.h - centerY) ** 2;
  const halves = [
    { x: 0, y: 0, w: .5, h: 1 }, { x: .5, y: 0, w: .5, h: 1 },
    { x: 0, y: 0, w: 1, h: .5 }, { x: 0, y: .5, w: 1, h: .5 },
  ].filter(fits).sort((a, b) => distance(a) - distance(b));
  let best = halves[0];
  if (!best) {
    const xs = [...new Set([0, .5, 1, ...docks.flatMap(box => [box.x, right(box)])])].sort((a, b) => a - b);
    const ys = [...new Set([0, .5, 1, ...docks.flatMap(box => [box.y, bottom(box)])])].sort((a, b) => a - b);
    let bestArea = 0;
    for (const [i, x] of xs.entries()) {
      for (const endX of xs.slice(i + 1)) {
        for (const [j, y] of ys.entries()) {
          for (const endY of ys.slice(j + 1)) {
            const box = { x, y, w: endX - x, h: endY - y };
            const area = box.w * box.h;
            if (area > .5 + epsilon || area < bestArea - epsilon || !fits(box)) continue;
            if (!best || area > bestArea + epsilon || distance(box) < distance(best)) {
              best = box;
              bestArea = area;
            }
          }
        }
      }
    }
  }
  return best ? placeWindow(layout, id, view, { type: 'docked', rect: best }) : layout;
}

export const edges = ['n', 'e', 's', 'w'] as const;
type Edge = typeof edges[number];
export const resizeEdges = [...edges, 'nw', 'ne', 'sw', 'se'] as const;
export type ResizeEdge = typeof resizeEdges[number];

const edgeInfo = {
  n: { coordinate: 'y', size: 'h', cross: 'x', crossSize: 'w', opposite: 's', leading: true },
  e: { coordinate: 'x', size: 'w', cross: 'y', crossSize: 'h', opposite: 'w', leading: false },
  s: { coordinate: 'y', size: 'h', cross: 'x', crossSize: 'w', opposite: 'n', leading: false },
  w: { coordinate: 'x', size: 'w', cross: 'y', crossSize: 'h', opposite: 'e', leading: true },
} as const;
const atEdge = (box: CanvasRect, edge: Edge) => {
  const { coordinate, size, leading } = edgeInfo[edge];
  return box[coordinate] + (leading ? 0 : box[size]);
};

function moveEdge(box: CanvasRect, edge: Edge, line: number): CanvasRect {
  const { coordinate, size, leading } = edgeInfo[edge];
  return leading ? { ...box, [coordinate]: line, [size]: box[coordinate] + box[size] - line }
    : { ...box, [size]: line - box[coordinate] };
}

interface DockParticipant { id: string; edge: Edge; box: CanvasRect }
export interface DockDivider {
  key: string;
  edge: Edge;
  line: number;
  participants: DockParticipant[];
}

export function dockDividers(layout: SessionCanvasLayout): DockDivider[] {
  const docks = layout.admitted.flatMap(id => {
    const item = layout.windows.get(id);
    return item?.mode === 'docked' && item.dock ? [{ id, box: item.dock }] : [];
  });
  const seen = new Set<string>();
  const result: DockDivider[] = [];
  for (const dock of docks) {
    for (const edge of edges) {
      const key = `${dock.id}/${edge}`;
      const line = atEdge(dock.box, edge);
      if (seen.has(key) || sameLine(line, 0) || sameLine(line, 1)) continue;
      const { cross, crossSize } = edgeInfo[edge];
      const participants: DockParticipant[] = [{ ...dock, edge }];
      for (const part of participants) {
        const facing = edgeInfo[part.edge].opposite;
        for (const candidate of docks) {
          if (participants.some(other => other.id === candidate.id)) continue;
          if (sameLine(atEdge(candidate.box, facing), line) && overlap(part.box, candidate.box, cross, crossSize) > epsilon) {
            participants.push({ ...candidate, edge: facing });
          }
        }
      }
      for (const part of participants) seen.add(`${part.id}/${part.edge}`);
      result.push({ key, edge, line, participants });
    }
  }
  return result;
}

export function dividerSpec(divider: DockDivider, view: CanvasSize, changes?: ReadonlyMap<string, CanvasRect>) {
  const { coordinate, size, cross, crossSize } = edgeInfo[divider.edge];
  const boxes = divider.participants.map(part => changes?.get(part.id) ?? part.box);
  const across = view[crossSize];
  const coordinatePx = atEdge(boxes[0], divider.edge) * view[size];
  const start = Math.min(...boxes.map(box => box[cross])) * across;
  const end = Math.max(...boxes.map(box => box[cross] + box[crossSize])) * across;
  const inset = Math.min(6, (end - start) / 3);
  const from = start > epsilon ? start + inset : start;
  const to = end < across - epsilon ? end - inset : end;
  const rect = coordinate === 'x'
    ? { x: coordinatePx - 5, y: from, w: 10, h: to - from }
    : { x: from, y: coordinatePx - 5, w: to - from, h: 10 };
  return { rect, axis: coordinate };
}

export function guideDockResize(layout: SessionCanvasLayout, divider: DockDivider, view: CanvasSize) {
  const { coordinate, size, cross, crossSize } = edgeInfo[divider.edge];
  const span = view[size];
  const others = layout.admitted.flatMap(id => {
    if (divider.participants.some(part => part.id === id)) return [];
    const item = layout.windows.get(id);
    return item?.mode === 'docked' && item.dock ? [item.dock] : [];
  });
  let low = 0;
  let high = 1;
  for (const part of divider.participants) {
    const { leading, opposite } = edgeInfo[part.edge];
    const fixed = atEdge(part.box, opposite);
    const minimum = Math.min(part.box[size], dockMinimum[size] / span);
    if (leading) high = Math.min(high, fixed - minimum);
    else low = Math.max(low, fixed + minimum);
    for (const other of others) {
      if (overlap(part.box, other, cross, crossSize) <= epsilon) continue;
      const boundary = atEdge(other, opposite);
      if (leading && boundary <= divider.line) low = Math.max(low, boundary);
      if (!leading && boundary >= divider.line) high = Math.min(high, boundary);
    }
  }
  return { coordinate, span, low, high, collapseAt: Math.min(dockMinimum[size] / 2, span / 4) / span };
}

export function resizeDockLine(divider: DockDivider, line: number) {
  return new Map(divider.participants.map(part => [part.id, moveEdge(part.box, part.edge, line)]));
}

export function applyDockResize(layout: SessionCanvasLayout, regions: ReadonlyMap<string, CanvasRect>): SessionCanvasLayout {
  const windows = new Map(layout.windows);
  for (const [id, dock] of regions) {
    const item = windows.get(id);
    if (item?.mode === 'docked') windows.set(id, { ...item, dock });
  }
  return { ...layout, windows };
}

export function releaseDockSide(layout: SessionCanvasLayout, divider: DockDivider, start: boolean, view: CanvasSize) {
  const expanding = new Map<string, CanvasRect>();
  const removed = new Set<string>();
  for (const part of divider.participants) {
    if (edgeInfo[part.edge].leading === start) expanding.set(part.id, moveEdge(part.box, part.edge, start ? 0 : 1));
    else removed.add(part.id);
  }
  for (const [id, region] of expanding) {
    if (sameLine(region.w, 1) && sameLine(region.h, 1)) return placeWindow(removeWindows(layout, removed, false), id, view, { type: 'maximized' });
    for (const other of dockConflicts(layout, id, region)) {
      if (!expanding.has(other)) removed.add(other);
    }
  }
  return applyDockResize(removeWindows(layout, removed, false), expanding);
}

export function resizeFloat(origin: CanvasRect, edge: ResizeEdge, dx: number, dy: number, view: CanvasSize) {
  const x = edge.includes('w') ? Math.min(origin.x + dx, right(origin) - floatMinimum.w) : origin.x;
  const y = edge.includes('n') ? clamp(origin.y + dy, 0, bottom(origin) - floatMinimum.h) : origin.y;
  const endX = edge.includes('e') ? Math.max(right(origin) + dx, x + floatMinimum.w) : right(origin);
  const endY = edge.includes('s') ? Math.max(bottom(origin) + dy, y + floatMinimum.h) : bottom(origin);
  return reachableFloat({ x, y, w: endX - x, h: endY - y }, view);
}
