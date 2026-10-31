import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { CellGraphMap, type CellGraphReadingState } from '../../frontend/components/cell-graph-map';

const layoutRuns = vi.hoisted(() => ({ count: 0 }));
// Use ELK's supported in-process entry in jsdom; production uses its real Web Worker.
vi.mock('../../frontend/components/cell-graph-layout', async importOriginal => {
  const actual = await importOriginal<typeof import('../../frontend/components/cell-graph-layout')>();
  const { default: ELK } = await import('elkjs/lib/elk.bundled.js');
  return { ...actual, createCellGraphLayoutEngine: () => {
    const engine = new ELK({ algorithms: ['layered'] });
    return { layout: (...args: Parameters<typeof engine.layout>) => { layoutRuns.count++; return engine.layout(...args); }, terminateWorker: () => {} };
  } };
});
beforeEach(() => {
  layoutRuns.count = 0;
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(function() { return this.classList.contains('agent-cell-graph-card') ? 240 : 0; });
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function() { return this.classList.contains('agent-cell-graph-card') ? 140 : this.hasAttribute('data-work') ? 32 : 0; });
  vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get').mockImplementation(function() { return this.hasAttribute('data-work') ? 80 : 0; });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function dimensions(element: HTMLElement) {
  Object.defineProperties(element, { clientWidth: { configurable: true, value: 500 }, clientHeight: { configurable: true, value: 400 } });
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, right: 500, bottom: 400, width: 500, height: 400 } as DOMRect);
}

it('keeps the pointed world coordinate fixed during wheel zoom and restores the saved camera', () => {
  const viewport: CellGraphReadingState = { left: 10, top: 20, zoom: 1 };
  const content = <article className="agent-cell-graph-card" data-agent="agent:a">研究员</article>;
  const view = render(<CellGraphMap works={{}} count={1} viewport={viewport}>{content}</CellGraphMap>);
  const surface = screen.getByRole('region', { name: /协作地图/ });
  dimensions(surface);
  const wheel = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: -100, clientX: 100, clientY: 80 });
  fireEvent(surface, wheel);
  expect(wheel.defaultPrevented).toBe(true);
  expect(viewport.zoom).toBeGreaterThan(1);
  expect(viewport.left + 100 / viewport.zoom!).toBeCloseTo(110);
  expect(viewport.top + 80 / viewport.zoom!).toBeCloseTo(100);
  expect(screen.getByRole('img', { name: /协作图小地图/ })).toBeTruthy();
  const saved = { ...viewport };
  view.unmount();
  const restored = render(<CellGraphMap works={{}} count={1} viewport={viewport}>{content}</CellGraphMap>);
  expect(viewport.left).toBeCloseTo(saved.left);
  expect(viewport.top).toBeCloseTo(saved.top);
  expect(viewport.zoom).toBeCloseTo(saved.zoom!);
  expect(restored.container.querySelector('.agent-cell-graph-scale')?.getAttribute('style')).toContain(`scale(${saved.zoom})`);
});

it('pans with the left button on empty space and leaves card clicks and the right button alone', async () => {
  const viewport: CellGraphReadingState = { left: 10, top: 20, zoom: 1 };
  const open = vi.fn();
  render(<CellGraphMap works={{}} count={1} viewport={viewport}><article className="agent-cell-graph-card" data-agent="agent:a"><button onClick={open}>研究员</button></article></CellGraphMap>);
  const surface = screen.getByRole('region', { name: /协作地图/ });
  dimensions(surface);
  const mouse = (target: Element | Window, type: string, init: MouseEventInit) => {
    const event = new MouseEvent(type, { bubbles: true, cancelable: true, ...init });
    // Vitest's Window proxy cannot pass jsdom's UIEvent constructor brand check.
    Object.defineProperty(event, 'view', { value: surface.ownerDocument.defaultView });
    fireEvent(target, event);
  };
  mouse(surface, 'mousedown', { button: 0, clientX: 100, clientY: 80 });
  mouse(window, 'mousemove', { buttons: 1, clientX: 150, clientY: 105 });
  mouse(window, 'mouseup', { button: 0, clientX: 150, clientY: 105 });
  expect(viewport.left).toBeCloseTo(-40);
  expect(viewport.top).toBeCloseTo(-5);
  // D3 suppresses only the click immediately following a completed drag.
  await new Promise(resolve => setTimeout(resolve, 0));
  const saved = { ...viewport };
  const button = await screen.findByRole('button', { name: '研究员' });
  mouse(button, 'mousedown', { button: 0, clientX: 150, clientY: 105 });
  mouse(window, 'mousemove', { buttons: 1, clientX: 200, clientY: 150 });
  mouse(window, 'mouseup', { button: 0, clientX: 200, clientY: 150 });
  fireEvent.click(button);
  expect(open).toHaveBeenCalledOnce();
  mouse(surface, 'mousedown', { button: 2, clientX: 100, clientY: 80 });
  mouse(window, 'mousemove', { buttons: 2, clientX: 200, clientY: 180 });
  mouse(window, 'mouseup', { button: 2, clientX: 200, clientY: 180 });
  const contextMenu = new MouseEvent('contextmenu', { bubbles: true, cancelable: true, button: 2 });
  fireEvent(surface, contextMenu);
  expect(contextMenu.defaultPrevented).toBe(false);
  expect({ left: viewport.left, top: viewport.top, zoom: viewport.zoom }).toEqual({ left: saved.left, top: saved.top, zoom: saved.zoom });
});

it('changes only the viewport when the sidebar is resized, preserving node positions and camera', async () => {
  const observers = new Map<Element, () => void>();
  vi.stubGlobal('ResizeObserver', class {
    constructor(private callback: () => void) {}
    observe(element: Element) { observers.set(element, this.callback); }
    disconnect() {}
  });
  const viewport: CellGraphReadingState = { left: 30, top: 40, zoom: 1 };
  const view = render(<CellGraphMap count={3} viewport={viewport} works={{ 'work:a': { dependencies: [] }, 'work:b': { dependencies: [] }, 'work:c': { dependencies: ['work:a', 'work:b'] } }}>
    {['a', 'b', 'c'].map(id => <article key={id} className="agent-cell-graph-card" data-agent={id}><div data-work={`work:${id}`}>{id}</div></article>)}
  </CellGraphMap>);
  const surface = screen.getByRole('region', { name: /协作地图/ });
  dimensions(surface);
  act(() => observers.get(surface)?.());
  await waitFor(() => expect(view.container.querySelectorAll('.agent-cell-graph-lines > path')).toHaveLength(2));
  const cards = [...view.container.querySelectorAll<HTMLElement>('[data-agent]')];
  const positions = cards.map(card => [card.style.left, card.style.top]);
  const paths = [...view.container.querySelectorAll('.agent-cell-graph-lines > path')].map(path => path.getAttribute('d'));
  const mapBounds = view.container.querySelector('.agent-cell-graph-minimap')?.getAttribute('viewBox');
  const layout = viewport.layout;
  Object.defineProperties(surface, { clientWidth: { value: 260 }, clientHeight: { value: 800 } });
  act(() => observers.get(surface)?.());
  expect(cards.map(card => [card.style.left, card.style.top])).toEqual(positions);
  expect([...view.container.querySelectorAll('.agent-cell-graph-lines > path')].map(path => path.getAttribute('d'))).toEqual(paths);
  expect(view.container.querySelector('.agent-cell-graph-minimap__viewport')?.getAttribute('height')).toBe('800');
  expect(viewport.layout).toBe(layout);
  expect(view.container.querySelector('.agent-cell-graph-minimap')?.getAttribute('viewBox')).toBe(mapBounds);
  expect({ left: viewport.left, top: viewport.top, zoom: viewport.zoom }).toEqual({ left: 30, top: 40, zoom: 1 });
});

it('centers a fresh node region once, then reuses layout and camera across resizing, graph changes and remounting', async () => {
  const observers = new Map<Element, () => void>();
  vi.stubGlobal('ResizeObserver', class {
    constructor(private callback: () => void) {}
    observe(element: Element) { observers.set(element, this.callback); }
    disconnect() {}
  });
  const viewport: CellGraphReadingState = { left: 0, top: 0 };
  const graph = (ids: string[]) => <CellGraphMap count={ids.length} viewport={viewport} works={Object.fromEntries(ids.map((id, index) => [`work:${id}`, { dependencies: index ? [`work:${ids[index - 1]}`] : [] }]))}>
    {ids.map(id => <article key={id} className="agent-cell-graph-card" data-agent={id}><div data-work={`work:${id}`}>{id}</div></article>)}
  </CellGraphMap>;
  const camera = () => ({ left: viewport.left, top: viewport.top, zoom: viewport.zoom });
  const view = render(graph(['a', 'b']));
  const surface = screen.getByRole('region', { name: /协作地图/ });
  dimensions(surface);
  act(() => observers.get(surface)?.());
  await waitFor(() => expect(viewport.zoom).toBe(1));
  const nodes = viewport.layout!.geometry.nodes;
  const middleX = (Math.min(...nodes.map(node => node.x)) + Math.max(...nodes.map(node => node.x + node.width))) / 2;
  const middleY = (Math.min(...nodes.map(node => node.y)) + Math.max(...nodes.map(node => node.y + node.height))) / 2;
  expect(viewport.left + 250).toBeCloseTo(middleX);
  expect(viewport.top + 200).toBeCloseTo(middleY);
  const centered = camera();
  Object.defineProperties(surface, { clientWidth: { value: 260 }, clientHeight: { value: 800 } });
  act(() => observers.get(surface)?.());
  expect(camera()).toEqual(centered);
  expect(layoutRuns.count).toBe(1);
  view.rerender(graph(['a', 'b', 'c']));
  await waitFor(() => expect(viewport.layout?.geometry.nodes).toHaveLength(3));
  expect(camera()).toEqual(centered);
  expect(layoutRuns.count).toBe(2);
  const cached = viewport.layout;
  view.unmount();
  const restored = render(graph(['a', 'b', 'c']));
  const restoredSurface = screen.getByRole('region', { name: /协作地图/ });
  dimensions(restoredSurface);
  act(() => observers.get(restoredSurface)?.());
  await waitFor(() => expect(restored.container.querySelectorAll('.agent-cell-graph-minimap__node')).toHaveLength(3));
  expect(viewport.layout).toBe(cached);
  expect(layoutRuns.count).toBe(2);
  expect(camera()).toEqual(centered);
});
