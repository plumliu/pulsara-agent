import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { select } from 'd3-selection';
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from 'd3-zoom';
import { createCellGraphLayoutEngine, layoutCellGraph, type GraphLayout, type GraphLayoutInput } from './cell-graph-layout';

export interface CellGraphReadingState {
  top: number; left: number; zoom?: number;
  worksPages?: number; participantsPages?: number;
  layout?: { input: GraphLayoutInput; geometry: GraphLayout };
}
const edgePath = (points: GraphLayout['lines'][number]['controlPoints']) => `M ${points[0].x} ${points[0].y} C ${points.slice(1).map(point => `${point.x} ${point.y}`).join(' ')}`;

/** D3 owns gestures and camera transforms; the existing graph owner supplies facts and cards. */
export function CellGraphMap({ works, count, viewport, children }: {
  works: Record<string, { dependencies: string[] }>;
  count: number;
  viewport?: CellGraphReadingState;
  children: ReactNode;
}) {
  const surface = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLDivElement>(null);
  const behavior = useRef<ZoomBehavior<HTMLDivElement, unknown> | null>(null);
  const arrowId = useId();
  const [camera, setCamera] = useState(() => {
    const scale = Math.max(.5, Math.min(2, viewport?.zoom ?? 1));
    return zoomIdentity.translate(-(viewport?.left ?? 0) * scale, -(viewport?.top ?? 0) * scale).scale(scale);
  });
  const cameraRef = useRef(camera); cameraRef.current = camera;
  // An adopted camera already has zoom; a fresh tab only has zero-valued offsets.
  const positioned = useRef(viewport?.zoom !== undefined);
  const [dragging, setDragging] = useState(false);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [geometry, setGeometry] = useState<GraphLayout>({ width: 0, height: 0, nodes: [], lines: [] });
  const [input, setInput] = useState<GraphLayoutInput>();
  const engine = useRef<ReturnType<typeof createCellGraphLayoutEngine> | undefined>(undefined);
  const [layoutError, setLayoutError] = useState('');

  useEffect(() => () => { engine.current?.terminateWorker(); engine.current = undefined; }, []);

  useEffect(() => {
    const element = surface.current;
    if (!element) return;
    const measure = () => setSize({ width: element.clientWidth, height: element.clientHeight });
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(measure);
    observer?.observe(element);
    return () => observer?.disconnect();
  }, []);

  useEffect(() => {
    const element = surface.current;
    if (!element) return;
    let active = true;
    const controller = zoom<HTMLDivElement, unknown>().scaleExtent([.5, 2]).clickDistance(3)
      .filter(event => event.type === 'wheel' || ((!event.ctrlKey && !event.button)
        && !(event.target instanceof Element && event.target.closest('.agent-cell-graph-card'))))
      .on('start.map', event => { if (active && ['mousedown', 'touchstart'].includes(event.sourceEvent?.type)) setDragging(true); })
      .on('zoom.map', (event: { transform: ZoomTransform; sourceEvent?: Event }) => {
        if (!active) return;
        if (event.sourceEvent) positioned.current = true;
        cameraRef.current = event.transform;
        setCamera(event.transform);
        if (viewport && positioned.current) { viewport.zoom = event.transform.k; viewport.left = -event.transform.x / event.transform.k; viewport.top = -event.transform.y / event.transform.k; }
      })
      .on('end.map', () => { if (active) setDragging(false); });
    behavior.current = controller;
    const selection = select<HTMLDivElement, unknown>(element);
    selection.call(controller).call(controller.transform, cameraRef.current)
      .on('dblclick.zoom', null)
      // Keep the wheel inside the map even when D3 reaches its scale extent.
      .on('wheel.map', event => event.preventDefault(), { passive: false });
    return () => { active = false; behavior.current = null; selection.on('.zoom', null).on('.map', null); };
  }, [viewport]);

  useLayoutEffect(() => {
    const world = canvas.current;
    if (!world) return;
    const cards = [...world.querySelectorAll<HTMLElement>('[data-agent]')];
    const measure = () => {
      // Hidden panes have no physical card measurements yet; wait for ResizeObserver.
      if (cards.some(card => !card.offsetWidth || !card.offsetHeight)) return;
      const measured = cards.map(card => ({ id: card.dataset.agent!, width: card.offsetWidth, height: card.offsetHeight,
        works: [...card.querySelectorAll<HTMLElement>('[data-work]')].map(row => ({ id: row.dataset.work!, y: row.offsetTop + row.offsetHeight / 2 })) }));
      const visibleWorks = new Set(measured.flatMap(card => card.works.map(work => work.id)));
      const dependencies = Object.entries(works).flatMap(([target, work]) => visibleWorks.has(target)
        ? work.dependencies.filter(source => visibleWorks.has(source)).map(source => ({ source, target })) : []);
      const next = { cards: measured, dependencies };
      // Compare complete measured values; polling identities and viewport size do not lay out the map.
      setInput(current => JSON.stringify(current) === JSON.stringify(next) ? current : next);
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(measure);
    for (const card of cards) observer?.observe(card);
    return () => observer?.disconnect();
  }, [works, children]);

  useEffect(() => {
    if (!input) return;
    if (!input.cards.length) { setGeometry({ width: 0, height: 0, nodes: [], lines: [] }); return; }
    if (viewport?.layout && JSON.stringify(viewport.layout.input) === JSON.stringify(input)) {
      setGeometry(viewport.layout.geometry);
      setLayoutError('');
      return;
    }
    let current = true;
    try {
      engine.current ??= createCellGraphLayoutEngine();
      void layoutCellGraph(engine.current, input).then(result => {
        if (current) {
          if (viewport) viewport.layout = { input, geometry: result };
          setGeometry(result); setLayoutError('');
        }
      }).catch(error => { if (current) setLayoutError(error instanceof Error ? error.message : '暂时无法更新协作图布局。'); });
    } catch (error) { setLayoutError(error instanceof Error ? error.message : '暂时无法启动协作图布局。'); }
    return () => { current = false; };
  }, [input, viewport]);

  useLayoutEffect(() => {
    const positions = new Map(geometry.nodes.map(node => [node.id, node]));
    for (const card of canvas.current?.querySelectorAll<HTMLElement>('[data-agent]') ?? []) {
      const position = positions.get(card.dataset.agent!);
      card.style.visibility = position ? 'visible' : 'hidden';
      if (position) { card.style.left = `${position.x}px`; card.style.top = `${position.y}px`; }
    }
  }, [geometry, children]);

  useLayoutEffect(() => {
    const element = surface.current, controller = behavior.current;
    if (positioned.current || !element || !controller || !size.width || !size.height || !geometry.nodes.length) return;
    const { left, top, right, bottom } = geometry.nodes.reduce((bounds, node) => ({
      left: Math.min(bounds.left, node.x), top: Math.min(bounds.top, node.y),
      right: Math.max(bounds.right, node.x + node.width), bottom: Math.max(bounds.bottom, node.y + node.height),
    }), { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity });
    positioned.current = true;
    select<HTMLDivElement, unknown>(element).call(controller.translateTo, (left + right) / 2, (top + bottom) / 2);
  }, [geometry, size]);

  const visible = { x: -camera.x / camera.k, y: -camera.y / camera.k, width: size.width / camera.k, height: size.height / camera.k };
  return <div className="agent-cell-graph-map">
    <div ref={surface} className={`agent-cell-graph-viewport${dragging ? ' is-dragging' : ''}`} role="region" tabIndex={0}
      aria-label="协作地图：滚轮缩放，左键拖拽空白处" style={{ backgroundSize: `${20 * camera.k}px ${20 * camera.k}px`, backgroundPosition: `${camera.x}px ${camera.y}px` }}
      onKeyDown={event => {
        if (event.target !== event.currentTarget || event.ctrlKey || event.metaKey || event.altKey || !surface.current || !behavior.current) return;
        const selection = select<HTMLDivElement, unknown>(surface.current);
        const offsets: Record<string, [number, number]> = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] };
        if (offsets[event.key]) { event.preventDefault(); positioned.current = true; const [x, y] = offsets[event.key]; selection.call(behavior.current.translateBy, x / camera.k, y / camera.k); }
        else if (['+', '=', '-'].includes(event.key)) { event.preventDefault(); positioned.current = true; selection.call(behavior.current.scaleBy, event.key === '-' ? 1 / 1.2 : 1.2); }
      }}>
      <div className="agent-cell-graph-scale" ref={canvas} style={{ transform: `translate(${camera.x}px, ${camera.y}px) scale(${camera.k})`, width: geometry.width, height: geometry.height }}>
        <svg className="agent-cell-graph-lines" aria-hidden="true"><defs><marker id={arrowId} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" /></marker></defs>{geometry.lines.map(line => <path key={line.id} d={edgePath(line.controlPoints)} markerEnd={`url(#${arrowId})`} />)}</svg>
        {children}
      </div>
    </div>
    {count > 0 && <svg className="agent-cell-graph-minimap" role="img" aria-label="协作图小地图，蓝框表示当前视窗"
      viewBox={`0 0 ${Math.max(1, geometry.width)} ${Math.max(1, geometry.height)}`}>
      <g className="agent-cell-graph-minimap__lines">{geometry.lines.map(line => <path key={line.id} d={edgePath(line.controlPoints)} vectorEffect="non-scaling-stroke" />)}</g>
      {geometry.nodes.map(node => <rect className="agent-cell-graph-minimap__node" key={node.id} x={node.x} y={node.y} width={node.width} height={node.height} rx={8} />)}
      <rect className="agent-cell-graph-minimap__viewport" {...visible} vectorEffect="non-scaling-stroke" />
    </svg>}
    {layoutError && <div className="agent-cell-graph-layout-error" role="status">布局暂时无法更新：{layoutError}</div>}
  </div>;
}
