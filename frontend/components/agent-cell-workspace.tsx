import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Bot, ChevronRight, GitFork, LoaderCircle } from 'lucide-react';
import { flushSync } from 'react-dom';
import { agentCellApi, AgentCellError, type AgentRow, type CellGraphPage, type CellRow, type Participant, type WorkFact } from '../lib/agent-cell-api';
import { FileCode } from './file-code';

export function transitionWorkspace(source: HTMLElement, open: () => void) {
  if (!document.startViewTransition || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) { open(); return; }
  source.style.viewTransitionName = 'agent-cell-item';
  try {
    const effect = document.startViewTransition({ types: ['agent-cell-detail'], update: () => flushSync(open) });
    void effect.ready.catch(() => undefined);
    void effect.finished.catch(() => undefined).finally(() => { source.style.viewTransitionName = ''; });
  } catch { source.style.viewTransitionName = ''; open(); }
}
function animateChange(update: () => void) {
  if (!document.startViewTransition || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) { update(); return; }
  try {
    const effect = document.startViewTransition({ types: ['agent-cell-update'], update: () => flushSync(update) });
    void effect.ready.catch(() => undefined);
    void effect.finished.catch(() => undefined);
  } catch { update(); }
}
function errorText(error: unknown) { return error instanceof Error ? error.message : '暂时无法读取。'; }
function retired(error: unknown) { return error instanceof AgentCellError && error.status === 404; }
export function mergeParticipants(...pages: Participant[][]): Participant[] {
  const combined = new Map<string, Participant>();
  for (const participant of pages.flat()) {
    const key = JSON.stringify([participant.agent_id, participant.work_id]);
    const previous = combined.get(key);
    combined.set(key, { ...participant, kinds: [...new Set([...(previous?.kinds ?? []), ...participant.kinds])] });
  }
  return [...combined.values()];
}

function usePages<T>(fetchPage: (cursor: string | undefined, signal: AbortSignal) => Promise<{ rows: T[]; next: string | null }>, key: (row: T) => string, readOne: (id: string, signal: AbortSignal) => Promise<T>) {
  const [rows, setRows] = useState<T[]>([]); const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState(''); const [loading, setLoading] = useState(true); const [unavailable, setUnavailable] = useState(false); const pages = useRef(1);
  const revision = useRef(0); const active = useRef<AbortController | null>(null); const known = useRef(new Map<string, T>());
  const refresh = useCallback(async (signal: AbortSignal) => {
    const request = ++revision.current;
    try {
      let cursor: string | undefined; let result: T[] = []; let nextCursor: string | null = null;
      for (let i = 0; i < pages.current; i++) {
        const page = await fetchPage(cursor, signal); result = [...result, ...page.rows]; nextCursor = page.next;
        if (!nextCursor) break; cursor = nextCursor;
      }
      const seen = new Map(result.map(row => [key(row), row]));
      const missing = [...known.current.keys()].filter(id => !seen.has(id));
      let partial = false;
      for (let i = 0; i < missing.length; i += 8) {
        const checks = await Promise.allSettled(missing.slice(i, i + 8).map(id => readOne(id, signal)));
        if (signal.aborted || request !== revision.current) return;
        checks.forEach((check, index) => { const id = missing[i + index]; if (check.status === 'fulfilled') seen.set(id, check.value); else if (!retired(check.reason)) { const old = known.current.get(id); if (old) seen.set(id, old); partial = true; } });
      }
      if (!signal.aborted && request === revision.current) {
        const changed = known.current.size > 0 && (known.current.size !== seen.size || [...known.current.keys()].some(id => !seen.has(id)));
        const commit = () => { if (signal.aborted || request !== revision.current) return; known.current = seen; setRows([...seen.values()]); setNext(nextCursor); setError(partial ? '部分对象暂时无法刷新。' : ''); };
        if (changed) animateChange(commit); else commit();
      }
    } catch (failure) {
      if (!signal.aborted && request === revision.current) {
        if (retired(failure)) {
          revision.current++; active.current?.abort(); known.current.clear(); setRows([]); setNext(null); setError(''); setLoading(false); setUnavailable(true);
        } else setError(errorText(failure));
      }
    }
    finally { if (!signal.aborted && request === revision.current) setLoading(false); }
  }, [fetchPage, key, readOne]);
  useEffect(() => {
    pages.current = 1; known.current = new Map(); setRows([]); setLoading(true); setUnavailable(false);
    const controller = new AbortController(); active.current = controller;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => { await refresh(controller.signal); if (!controller.signal.aborted) timer = setTimeout(() => void tick(), 3000); };
    void tick();
    return () => { controller.abort(); active.current = null; clearTimeout(timer); };
  }, [refresh]);
  const more = () => { pages.current += 1; if (active.current) void refresh(active.current.signal); };
  return { rows, next, error, loading, unavailable, more };
}
export function AgentList({ root, onOpen }: { root: string; onOpen: (agent: AgentRow, source: HTMLElement) => void }) {
  const fetchPage = useCallback(async (cursor: string | undefined, signal: AbortSignal) => { const page = await agentCellApi.agents(root, cursor, signal); return { rows: page.agents, next: page.next_cursor }; }, [root]);
  const key = useCallback((row: AgentRow) => row.agent_id, []);
  const readOne = useCallback(async (id: string, signal: AbortSignal) => (await agentCellApi.agent(root, id, signal)).agent, [root]);
  const page = usePages(fetchPage, key, readOne);
  if (page.unavailable) return <div className="inspector-empty" role="status">对象已不可用</div>;
  return <section className="agent-cell-list"><div className="agent-cell-heading"><Bot size={17}/><strong>子代理</strong></div>
    {page.loading && <div className="agent-cell-empty"><LoaderCircle size={16} className="spin" /></div>}
    {page.error && <div role="status" className="agent-cell-error">{page.error}</div>}
    {!page.loading && !page.error && !page.rows.length && <div className="agent-cell-empty">暂无子代理</div>}
    {page.rows.map(agent => <button key={agent.agent_id} type="button" className="agent-cell-row" onClick={event => onOpen(agent, event.currentTarget)}>
      <span className="agent-cell-avatar"><Bot size={15}/></span><span className="agent-cell-row-name">{agent.title || '子代理'}</span><span className={`agent-cell-indicator${agent.current_work_id ? ' is-active' : ''}`} title={agent.current_work_id ? '工作中' : '空闲'} />{agent.queued_count > 0 && <span className="agent-cell-count">{agent.queued_count}</span>}<ChevronRight size={14}/>
    </button>)}
    {page.next && <button className="agent-cell-more" type="button" onClick={page.more}>加载更多</button>}
  </section>;
}
export function CellList({ root, host, onOpen }: { root: string; host: string; onOpen: (cell: CellRow, host: string, source: HTMLElement) => void }) {
  const fetchPage = useCallback(async (cursor: string | undefined, signal: AbortSignal) => { const page = await agentCellApi.cells(root, host, cursor, signal); return { rows: page.cells, next: page.next_cursor }; }, [root, host]);
  const key = useCallback((row: CellRow) => row.target.cell_id, []);
  const readOne = useCallback(async (id: string, signal: AbortSignal) => (await agentCellApi.cell(root, host, id, signal)).cell, [root, host]);
  const page = usePages(fetchPage, key, readOne);
  if (page.unavailable) return <div className="inspector-empty" role="status">对象已不可用</div>;
  return <section className="agent-cell-list"><div className="agent-cell-heading"><GitFork size={17}/><strong>cell</strong></div>
    {page.loading && <div className="agent-cell-empty"><LoaderCircle size={16} className="spin" /></div>}
    {page.error && <div role="status" className="agent-cell-error">{page.error}</div>}
    {!page.loading && !page.error && !page.rows.length && <div className="agent-cell-empty">暂无 Cell</div>}
    {page.rows.map(cell => <button key={cell.target.cell_id} type="button" className="agent-cell-row" onClick={event => onOpen(cell, host, event.currentTarget)}>
      <span className="agent-cell-avatar agent-cell-avatar--cell"><GitFork size={15}/></span><span className="agent-cell-row-name">{cell.target.cell_id}</span><span className={`agent-cell-indicator${['running','queued'].includes(cell.state) ? ' is-active' : ''}`} title={cell.state}/><ChevronRight size={14}/>
    </button>)}
    {page.next && <button className="agent-cell-more" type="button" onClick={page.more}>加载更多</button>}
  </section>;
}
export function CellSource({ root, attempt, host, cell, onGraph }: { root: string; attempt: string; host?: string; cell?: string; onGraph?: (source: HTMLElement) => void }) {
  const [text, setText] = useState(''); const [next, setNext] = useState<number | null>(null); const [loading, setLoading] = useState(true);
  const [error, setError] = useState(''); const [graph, setGraph] = useState(false);
  const owner = useRef<AbortController | null>(null); const morePending = useRef(false);
  useEffect(() => {
    const controller = new AbortController(); owner.current = controller;
    setText(''); setNext(null); setLoading(true); setGraph(false); setError('');
    void agentCellApi.source(root, attempt, 0, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      if (result.source.status !== 'AVAILABLE') setError(result.source.reason);
      else { setText(result.source.text); setNext(result.source.next_offset_chars); }
    }).catch(failure => { if (!controller.signal.aborted) setError(errorText(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); if (owner.current === controller) owner.current = null; };
  }, [root, attempt]);
  const graphEnabled = Boolean(onGraph);
  useEffect(() => {
    if (!host || !cell || !graphEnabled) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const result = await agentCellApi.graph(root, host, cell, 'works', undefined, controller.signal);
        if (controller.signal.aborted) return;
        if ('status' in result) { setGraph(false); return; }
        setGraph(true);
      } catch { /* A temporary graph read failure does not invalidate canonical source. */ }
      if (!controller.signal.aborted) timer = setTimeout(() => void refresh(), 3000);
    };
    void refresh(); return () => { controller.abort(); clearTimeout(timer); };
  }, [root, host, cell, graphEnabled]);
  const more = async () => {
    const controller = owner.current;
    if (next === null || !controller || controller.signal.aborted || morePending.current) return;
    morePending.current = true;
    try {
      const result = await agentCellApi.source(root, attempt, next, controller.signal);
      if (controller.signal.aborted || owner.current !== controller) return;
      if (result.source.status !== 'AVAILABLE') { setError(result.source.reason); return; }
      const page = result.source; setText(current => current + page.text); setNext(page.next_offset_chars); setError('');
    } catch (failure) { if (!controller.signal.aborted) setError(errorText(failure)); }
    finally { morePending.current = false; }
  };
  return <section className="agent-cell-source">
    {graph && onGraph && <div className="agent-cell-source-toolbar"><button type="button" onClick={event => onGraph(event.currentTarget)}><GitFork size={14}/>查看工作流图</button></div>}
    {error && <div className="agent-cell-error" role="status">{error}</div>}
    {loading ? <div className="agent-cell-empty"><LoaderCircle size={16} className="spin"/></div> : <FileCode source={text} filename="cell.js"/>}
    {next !== null && <button type="button" className="agent-cell-more" onClick={() => void more()}>加载更多</button>}
  </section>;
}

class GraphClosed extends Error {}

type GraphWork = WorkFact & { dependencies: string[] };
export function CellGraph({ root, host, cell, onAgent, onWork, viewport }: { root: string; host: string; cell: string; onAgent: (agentId: string) => void; onWork: (work: WorkFact) => void; viewport?: { top: number; left: number; zoom?: number; expandedWorkIds?: string[]; expandedParents?: string[]; worksPages?: number; participantsPages?: number } }) {
  const [works, setWorks] = useState<Record<string, GraphWork>>({}); const worksRef = useRef(works); worksRef.current = works;
  const [participants, setParticipants] = useState<Participant[]>([]); const participantsRef = useRef(participants); participantsRef.current = participants;
  const [names, setNames] = useState<Record<string, string>>({}); const [coverage, setCoverage] = useState('complete');
  const [error, setError] = useState(''); const [unavailable, setUnavailable] = useState(false); const [loaded, setLoaded] = useState(false);
  const [next, setNext] = useState<{works: string | null; participants: string | null}>({ works: null, participants: null });
  const pageCounts = useRef({ works: viewport?.worksPages ?? 1, participants: viewport?.participantsPages ?? 1 });
  const reload = useRef<(() => void) | null>(null);
  const seeds = useRef(new Set<string>());
  const [zoom, setZoomState] = useState(viewport?.zoom ?? 1); const canvas = useRef<HTMLDivElement>(null);
  const [expandedParents, setExpandedParents] = useState(() => new Set(viewport?.expandedParents ?? []));
  const scroll = useRef<HTMLDivElement>(null);
  const pan = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const owner = useRef<AbortController | null>(null); const expired = useRef(false);
  const [lines, setLines] = useState<Array<{x1:number;y1:number;x2:number;y2:number}>>([]);
  const setZoom = (value: number) => { const nextZoom = Math.max(.5, Math.min(2, value)); setZoomState(nextZoom); if (viewport) viewport.zoom = nextZoom; };
  const expire = useCallback(() => { expired.current = true; owner.current?.abort(); seeds.current.clear(); if (viewport) { viewport.top = 0; viewport.left = 0; delete viewport.zoom; delete viewport.expandedWorkIds; delete viewport.expandedParents; delete viewport.worksPages; delete viewport.participantsPages; } setWorks({}); setParticipants([]); setNames({}); setExpandedParents(new Set()); setLines([]); setNext({ works: null, participants: null }); setError(''); setUnavailable(true); }, [viewport]);
  useEffect(() => {
    const controller = new AbortController(); owner.current = controller; expired.current = false; let timer: ReturnType<typeof setTimeout>; let busy = false; let requested = false;
    const scan = async (section: 'works' | 'participants') => {
      let cursor: string | undefined; let nextCursor: string | null = null; let coverage: CellGraphPage['participation_coverage'] = 'complete'; let items: CellGraphPage['items'] = [];
      for (let i = 0; i < pageCounts.current[section]; i++) {
        const page = await agentCellApi.graph(root, host, cell, section, cursor, controller.signal);
        if ('status' in page) throw new GraphClosed(page.message);
        items = [...items, ...page.items]; if (page.participation_coverage !== 'complete') coverage = page.participation_coverage;
        nextCursor = page.next_cursor; if (!nextCursor) break; cursor = nextCursor;
      }
      return { items, next_cursor: nextCursor, participation_coverage: coverage };
    };
    const load = async () => {
      if (busy) { requested = true; return; }
      busy = true; clearTimeout(timer);
      try {
      const [ws, ps] = await Promise.all([scan('works'), scan('participants')]);
      if (controller.signal.aborted || expired.current || owner.current !== controller) return;
      const seed = ws.items as WorkFact[]; const extra = ps.items as Participant[];
      for (const work of seed) seeds.current.add(work.work_id);
      // Preserve previously loaded pages and expanded predecessors until exact reads confirm retirement.
      const ids = [...new Set([...Object.keys(worksRef.current), ...seeds.current, ...(viewport?.expandedWorkIds ?? []), ...participantsRef.current.flatMap(p => p.work_id ? [p.work_id] : []), ...extra.flatMap(p => p.work_id ? [p.work_id] : [])])];
      const updated: Record<string, GraphWork> = { ...worksRef.current }; const removedWorks = new Set<string>(); let partialRead = false;
      for (let i = 0; i < ids.length; i += 8) {
        const batch = await Promise.allSettled(ids.slice(i, i + 8).map(id => agentCellApi.work(root, id, controller.signal)));
        batch.forEach((item, index) => { const id = ids[i + index]; if (item.status === 'fulfilled') updated[id] = { ...item.value.work, dependencies: item.value.inputs.filter(input => input.kind === 'dependency').map(input => input.source_work_id) }; else if (retired(item.reason)) { delete updated[id]; seeds.current.delete(id); removedWorks.add(id); } else partialRead = true; });
        if (controller.signal.aborted || expired.current || owner.current !== controller) return;
      }
      const combined = mergeParticipants(participantsRef.current, extra);
      const agents = [...new Set([...Object.values(updated).map(w => w.agent_id), ...combined.map(p => p.agent_id)])];
      const labels: Record<string, string> = {}; const removed = new Set<string>();
      for (let i = 0; i < agents.length; i += 8) {
        const batch = await Promise.allSettled(agents.slice(i, i + 8).map(id => agentCellApi.agent(root, id, controller.signal)));
        batch.forEach((item, index) => { const id = agents[i + index]; if (item.status === 'fulfilled') labels[id] = item.value.agent.title || '子代理'; else if (retired(item.reason)) removed.add(id); else partialRead = true; });
        if (controller.signal.aborted || expired.current || owner.current !== controller) return;
      }
      for (const [id, work] of Object.entries(updated)) if (removed.has(work.agent_id)) { delete updated[id]; removedWorks.add(id); }
      if (viewport?.expandedWorkIds) viewport.expandedWorkIds = viewport.expandedWorkIds.filter(id => !removedWorks.has(id));
      const visibleParticipants = combined.filter(p => !removed.has(p.agent_id) && (!p.work_id || !removedWorks.has(p.work_id)));
      const visibleAgents = new Set([...Object.values(updated).map(work => work.agent_id), ...visibleParticipants.map(participant => participant.agent_id)]);
      const changed = Object.keys(updated).length !== Object.keys(worksRef.current).length || Object.keys(worksRef.current).some(id => !updated[id]) || visibleParticipants.length !== participantsRef.current.length;
      const publish = () => { if (controller.signal.aborted || expired.current || owner.current !== controller) return; setWorks(updated); setParticipants(visibleParticipants); setNames(current => Object.fromEntries([...visibleAgents].flatMap(id => { const name = labels[id] ?? current[id]; return name === undefined ? [] : [[id, name]]; }))); };
      if (changed && (Object.keys(worksRef.current).length || participantsRef.current.length)) animateChange(publish); else publish();
      setCoverage(ws.participation_coverage !== 'complete' ? ws.participation_coverage : ps.participation_coverage);
      setNext({ works: ws.next_cursor, participants: ps.next_cursor });
      setError(partialRead ? '部分对象暂时无法刷新。' : ''); setUnavailable(false); setLoaded(true);
    } catch (failure) { if (!controller.signal.aborted && !expired.current && owner.current === controller) { if (failure instanceof GraphClosed) expire(); else setError(`刷新失败：${errorText(failure)}`); setLoaded(true); } }
      busy = false;
      if (!controller.signal.aborted && !expired.current && owner.current === controller) {
        if (requested) { requested = false; void load(); } else timer = setTimeout(() => void load(), 3000);
      }
    };
    reload.current = () => void load(); void load(); return () => { controller.abort(); if (owner.current === controller) { owner.current = null; reload.current = null; } clearTimeout(timer); };
  }, [root, host, cell, expire, viewport]);
  const expand = async (ids: string[]) => { const controller = owner.current; if (!controller || controller.signal.aborted || expired.current) return; for (const id of ids) { if (worksRef.current[id]) continue; try { const result = await agentCellApi.work(root, id, controller.signal); if (controller.signal.aborted || expired.current || owner.current !== controller) return; if (viewport) viewport.expandedWorkIds = [...new Set([...(viewport.expandedWorkIds ?? []), id])]; animateChange(() => { if (controller.signal.aborted || expired.current || owner.current !== controller) return; setWorks(current => ({ ...current, [id]: { ...result.work, dependencies: result.inputs.filter(i => i.kind === 'dependency').map(i => i.source_work_id) } })); }); } catch (failure) { if (controller.signal.aborted || expired.current || owner.current !== controller) return; if (!retired(failure)) setError(errorText(failure)); } } };
  const more = (section: 'works' | 'participants') => { if (!next[section] || !owner.current || expired.current) return; pageCounts.current[section]++; if (viewport) { if (section === 'works') viewport.worksPages = pageCounts.current.works; else viewport.participantsPages = pageCounts.current.participants; } reload.current?.(); };
  const visibleWorks = useMemo(() => {
    const ids = new Set([...seeds.current, ...participants.flatMap(p => p.work_id ? [p.work_id] : [])]);
    const pending = [...ids];
    for (let i = 0; i < pending.length; i++) {
      const work = works[pending[i]];
      if (!work || !expandedParents.has(work.work_id)) continue;
      for (const source of work.dependencies) if (!ids.has(source) && works[source]) { ids.add(source); pending.push(source); }
    }
    return Object.values(works).filter(work => ids.has(work.work_id));
  }, [works, participants, expandedParents]);
  const groups = useMemo(() => { const ids = new Set([...visibleWorks.map(w => w.agent_id), ...participants.map(p => p.agent_id)]); return [...ids].map(id => ({ id, works: visibleWorks.filter(w => w.agent_id === id), kinds: [...new Set(participants.filter(p => p.agent_id === id).flatMap(p => p.kinds))] })); }, [visibleWorks, participants]);
  useEffect(() => { if (scroll.current && viewport) { scroll.current.scrollTop = viewport.top; scroll.current.scrollLeft = viewport.left; } }, [viewport]);
  useEffect(() => { const element = canvas.current; if (!element) return; const update = () => { const base = element.getBoundingClientRect(); const found: typeof lines = []; const rows = [...element.querySelectorAll<HTMLElement>('[data-work]')]; for (const work of Object.values(works)) for (const source of work.dependencies) { const start = rows.find(row => row.dataset.work === source); const end = rows.find(row => row.dataset.work === work.work_id); if (!start || !end) continue; const a = start.getBoundingClientRect(), b = end.getBoundingClientRect(); found.push({ x1:(a.right-base.left)/zoom, y1:(a.top+a.height/2-base.top)/zoom, x2:(b.left-base.left)/zoom, y2:(b.top+b.height/2-base.top)/zoom }); } setLines(found); }; update(); if (typeof ResizeObserver === 'undefined') return; const observer = new ResizeObserver(update); observer.observe(element); return () => observer.disconnect(); }, [works, groups, zoom]);
  if (unavailable) return <div className="inspector-empty" role="status">实时工作流视图已关闭</div>;
  return <section className="agent-cell-graph"><div className="agent-cell-graph-header"><strong>工作流图</strong><div><button title="缩小" onClick={() => setZoom(zoom-.1)}>−</button><button title="适配" onClick={() => setZoom(scroll.current && canvas.current ? Math.min(1, (scroll.current.clientWidth - 24) / canvas.current.scrollWidth) : 1)}>{Math.round(zoom*100)}%</button><button title="放大" onClick={() => setZoom(zoom+.1)}>＋</button></div></div>
    {error && <div className="agent-cell-error" role="status">{error}</div>}{coverage !== 'complete' && <div className="agent-cell-error" role="status">部分参与信息暂不可用</div>}
    {loaded && !error && !groups.length && coverage === 'complete' && !next.works && !next.participants && <div className="agent-cell-empty">暂无子代理编排</div>}
    <div className="agent-cell-graph-scroll" ref={scroll} onScroll={event => { if (viewport) { viewport.top = event.currentTarget.scrollTop; viewport.left = event.currentTarget.scrollLeft; } }} onPointerDown={event => { if (event.button !== 0 || (event.target as HTMLElement).closest('button')) return; const element = event.currentTarget; pan.current = { x: event.clientX, y: event.clientY, left: element.scrollLeft, top: element.scrollTop }; element.setPointerCapture(event.pointerId); }} onPointerMove={event => { if (!pan.current) return; event.currentTarget.scrollLeft = pan.current.left + pan.current.x - event.clientX; event.currentTarget.scrollTop = pan.current.top + pan.current.y - event.clientY; }} onPointerUp={() => { pan.current = null; }} onPointerCancel={() => { pan.current = null; }}><div className="agent-cell-graph-scale" style={{ transform: `scale(${zoom})`, transformOrigin: 'top left' }} ref={canvas}>
      <svg className="agent-cell-graph-lines" aria-hidden="true"><defs><marker id="agent-cell-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" /></marker></defs>{lines.map((line, index) => <path key={index} d={`M ${line.x1} ${line.y1} C ${line.x1+40} ${line.y1}, ${line.x2-40} ${line.y2}, ${line.x2} ${line.y2}`} markerEnd="url(#agent-cell-arrow)" />)}</svg>
      {groups.map(group => <article className="agent-cell-graph-card" key={group.id}><button type="button" className="agent-cell-graph-agent" onClick={() => onAgent(group.id)}><span className="agent-cell-avatar"><Bot size={14}/></span><strong>{names[group.id] ?? group.id}</strong><ChevronRight size={14}/></button>{group.kinds.length > 0 && <div className="agent-cell-participation">{group.kinds.map(kind => ({ query: '查询', wait: '等待', message: '消息', stop: '停止' }[kind] ?? kind)).join(' · ')}</div>}{group.works.map(work => <div className="agent-cell-graph-work" key={work.work_id} data-work={work.work_id}><button className="agent-cell-work-link" title="定位工作" onClick={() => onWork(work)}>{work.work_id}</button><span className="agent-cell-work-status">{({ running: '运行中', queued: '排队中', completed: '已完成', stopped: '已停止' } as Record<string, string>)[work.status] ?? work.status}</span>{work.dependencies.length > 0 && <button title={expandedParents.has(work.work_id) ? '收起前置工作' : '展开前置工作'} aria-expanded={expandedParents.has(work.work_id)} onClick={() => { const opening = !expandedParents.has(work.work_id); animateChange(() => { if (expired.current || !owner.current || owner.current.signal.aborted) return; setExpandedParents(current => { const value = new Set(current); if (opening) value.add(work.work_id); else value.delete(work.work_id); if (viewport) viewport.expandedParents = [...value]; return value; }); }); if (opening) void expand(work.dependencies); }}>{expandedParents.has(work.work_id) ? '−' : '＋'}</button>}</div>)}</article>)}
    </div></div>{(next.works || next.participants) && <button type="button" className="agent-cell-more" onClick={() => { more('works'); more('participants'); }}>加载更多</button>}
  </section>;
}
