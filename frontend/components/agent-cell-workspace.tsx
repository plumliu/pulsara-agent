import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Bot, ChevronRight, FileCode2, LoaderCircle, Network } from 'lucide-react';
import { flushSync } from 'react-dom';
import { agentCellApi, AgentCellError, agentLabel, agentActivity, agentTaskLabel, workTitle, type AgentRow, type CellGraphPage, type CellRow, type Participant, type WorkFact } from '../lib/agent-cell-api';
import { FilePreviewPanel, FileTextContent } from './file-preview-panel';
import { WorkspaceLoading } from './workspace-loading';
import { CellGraphMap, type CellGraphReadingState } from './cell-graph-map';

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
  const empty = !page.loading && !page.error && !page.rows.length;
  return <section className={empty ? undefined : 'agent-cell-list'}>
    {page.loading && <div className="agent-cell-empty"><LoaderCircle size={16} className="spin" /></div>}
    {page.error && <div role="status" className="agent-cell-error">{page.error}</div>}
    {empty && <div className="inspector-empty"><Bot size={18}/><span>暂无子代理</span></div>}
    {page.rows.map(agent => <button key={agent.agent_id} type="button" className="agent-cell-row" onClick={event => onOpen(agent, event.currentTarget)}>
      <span className="agent-cell-avatar"><Bot size={15}/></span><span className="agent-cell-row-copy"><span className="agent-cell-row-name" title={agent.agent_id}>{agentLabel(agent)}</span><span className="agent-cell-row-task" title={agentTaskLabel(agent)}>{agentTaskLabel(agent)}</span>{agent.queued_work_count > 0 && <small>另有 {agent.queued_work_count} 项工作排队</small>}</span><span className={`agent-cell-indicator${agent.has_running_turn ? ' is-active' : ''}`} title={agentActivity(agent)} /><ChevronRight size={14}/>
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
  const empty = !page.loading && !page.error && !page.rows.length;
  return <section className={empty ? undefined : 'agent-cell-list'}>
    {page.loading && <div className="agent-cell-empty"><LoaderCircle size={16} className="spin" /></div>}
    {page.error && <div role="status" className="agent-cell-error">{page.error}</div>}
    {empty && <div className="inspector-empty"><FileCode2 size={18}/><span>暂无 Cell</span></div>}
    {page.rows.map(cell => <button key={cell.target.cell_id} type="button" className="agent-cell-row" onClick={event => onOpen(cell, host, event.currentTarget)}>
      <span className="agent-cell-avatar agent-cell-avatar--cell"><FileCode2 size={15}/></span><span className="agent-cell-row-name">{cell.target.cell_id}</span><span className={`agent-cell-indicator${['running','queued'].includes(cell.state) ? ' is-active' : ''}`} title={cell.state}/><ChevronRight size={14}/>
    </button>)}
    {page.next && <button className="agent-cell-more" type="button" onClick={page.more}>加载更多</button>}
  </section>;
}
export function CellSource({ root, attempt, host, cell, onGraph, viewport }: { root: string; attempt: string; host?: string; cell?: string; onGraph?: (source: HTMLElement) => void; viewport?: { top: number; left: number } }) {
  const [text, setText] = useState(''); const [next, setNext] = useState<number | null>(null); const [loading, setLoading] = useState(true);
  const [error, setError] = useState(''); const [graph, setGraph] = useState(false);
  const owner = useRef<AbortController | null>(null); const morePending = useRef(false);
  const scroller = useRef<HTMLDivElement>(null);
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
  useLayoutEffect(() => {
    if (loading || !scroller.current) return;
    scroller.current.scrollTop = viewport?.top ?? 0;
    scroller.current.scrollLeft = viewport?.left ?? 0;
  }, [root, attempt, loading, viewport]);
  const graphEnabled = Boolean(onGraph);
  useEffect(() => {
    setGraph(false);
    if (!host || !cell || !graphEnabled) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const presence = async (section: 'works' | 'participants'): Promise<'present' | 'empty' | 'unknown' | 'ended'> => {
      let cursor: string | undefined;
      const seen = new Set<string>();
      let complete = true;
      while (!controller.signal.aborted) {
        const result = await agentCellApi.graph(root, host, cell, section, cursor, controller.signal);
        if (controller.signal.aborted) return 'unknown';
        if ('status' in result || ['completed', 'failed', 'terminated'].includes(result.cell.state)) return 'ended';
        if (result.items.length) return 'present';
        if (result.participation_coverage !== 'complete') complete = false;
        if (!result.next_cursor) return section === 'participants' && !complete ? 'unknown' : 'empty';
        if (seen.has(result.next_cursor)) throw new Error('Cell graph cursor did not advance');
        seen.add(result.next_cursor); cursor = result.next_cursor;
      }
      return 'unknown';
    };
    const refresh = async () => {
      try {
        let result = await presence('works');
        if (result !== 'present' && result !== 'ended') result = await presence('participants');
        if (controller.signal.aborted) return;
        if (result === 'ended') { setGraph(false); return; }
        if (result !== 'unknown') setGraph(result === 'present');
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
  return <FilePreviewPanel className="agent-cell-source" label="Cell 冻结源码"
    toolbar={graph && onGraph && <button type="button" className="file-preview-action" onClick={event => onGraph(event.currentTarget)}><Network size={14}/>查看协作图</button>}
    notice={error && <div className="file-preview-error" role="status">{error}</div>}>
    {loading ? <WorkspaceLoading/> : <div className="file-preview-reader">
      <FileTextContent source={text} filename="cell.js" scrollerRef={scroller} onScroll={event => {
        if (viewport) { viewport.top = event.currentTarget.scrollTop; viewport.left = event.currentTarget.scrollLeft; }
      }}/>
      {next !== null && <footer className="file-preview-pagebar file-preview-pagination"><button type="button" onClick={() => void more()}>加载更多</button></footer>}
    </div>}
  </FilePreviewPanel>;
}

class GraphClosed extends Error {}

type GraphWork = WorkFact & { dependencies: string[] };
export function CellGraph({ root, host, cell, onAgent, onWork, viewport }: { root: string; host: string; cell: string; onAgent: (agentId: string) => void; onWork: (work: WorkFact) => void; viewport?: CellGraphReadingState }) {
  const [works, setWorks] = useState<Record<string, GraphWork>>({}); const worksRef = useRef(works); worksRef.current = works;
  const [participants, setParticipants] = useState<Participant[]>([]); const participantsRef = useRef(participants); participantsRef.current = participants;
  const [names, setNames] = useState<Record<string, string>>({}); const [coverage, setCoverage] = useState('complete');
  const [error, setError] = useState(''); const [unavailable, setUnavailable] = useState(false); const [loaded, setLoaded] = useState(false);
  const [next, setNext] = useState<{works: string | null; participants: string | null}>({ works: null, participants: null });
  const pageCounts = useRef({ works: viewport?.worksPages ?? 1, participants: viewport?.participantsPages ?? 1 });
  const reload = useRef<(() => void) | null>(null);
  const seeds = useRef(new Set<string>());
  const owner = useRef<AbortController | null>(null); const expired = useRef(false);
  const expire = useCallback(() => { expired.current = true; owner.current?.abort(); seeds.current.clear(); if (viewport) { viewport.top = 0; viewport.left = 0; delete viewport.zoom; delete viewport.worksPages; delete viewport.participantsPages; delete viewport.layout; } setWorks({}); setParticipants([]); setNames({}); setNext({ works: null, participants: null }); setError(''); setUnavailable(true); }, [viewport]);
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
      // Preserve previously loaded Cell pages until exact reads confirm retirement.
      const ids = [...new Set([...Object.keys(worksRef.current), ...seeds.current, ...participantsRef.current.flatMap(p => p.work_id ? [p.work_id] : []), ...extra.flatMap(p => p.work_id ? [p.work_id] : [])])];
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
        batch.forEach((item, index) => { const id = agents[i + index]; if (item.status === 'fulfilled') labels[id] = agentLabel(item.value.agent); else if (retired(item.reason)) removed.add(id); else partialRead = true; });
        if (controller.signal.aborted || expired.current || owner.current !== controller) return;
      }
      for (const [id, work] of Object.entries(updated)) if (removed.has(work.agent_id)) { delete updated[id]; removedWorks.add(id); }
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
  const more = (section: 'works' | 'participants') => { if (!next[section] || !owner.current || expired.current) return; pageCounts.current[section]++; if (viewport) { if (section === 'works') viewport.worksPages = pageCounts.current.works; else viewport.participantsPages = pageCounts.current.participants; } reload.current?.(); };
  const groups = useMemo(() => { const visibleWorks = Object.values(works); const ids = new Set([...visibleWorks.map(w => w.agent_id), ...participants.map(p => p.agent_id)]); return [...ids].map(id => ({ id, works: visibleWorks.filter(w => w.agent_id === id) })); }, [works, participants]);
  if (unavailable) return <div className="inspector-empty" role="status">实时工作流视图已关闭</div>;
  return <section className="agent-cell-graph" aria-label="协作图">
    <CellGraphMap key={JSON.stringify([root, host, cell])} works={works} count={groups.length} viewport={viewport}>
      {groups.map(group => <article className="agent-cell-graph-card" key={group.id} data-agent={group.id}>
        <button type="button" className="agent-cell-graph-agent" onClick={() => onAgent(group.id)}><span className="agent-cell-avatar"><Bot size={14}/></span><strong>{names[group.id] ?? group.id}</strong><ChevronRight size={14}/></button>
        {group.works.map(work => <div className="agent-cell-graph-work" key={work.work_id} data-work={work.work_id}>
          <button className="agent-cell-work-link" title={`${workTitle(work.work_title)}\n${work.work_id}`} onClick={() => onWork(work)}>{workTitle(work.work_title)}</button>
          <span className="agent-cell-work-status">{({ running: '运行中', queued: '排队中', completed: '已完成', stopped: '已停止' } as Record<string, string>)[work.status] ?? work.status}</span>
        </div>)}
      </article>)}
    </CellGraphMap>
    {(error || coverage !== 'complete') && <div className="agent-cell-graph-notices">{error && <div className="agent-cell-error" role="status">{error}</div>}{coverage !== 'complete' && <div className="agent-cell-error" role="status">部分参与信息暂不可用</div>}</div>}
    {loaded && !error && !groups.length && coverage === 'complete' && !next.works && !next.participants && <div className="agent-cell-graph-empty">暂无子代理编排</div>}
    {(next.works || next.participants) && <button type="button" className="agent-cell-more agent-cell-graph-more" onClick={() => { more('works'); more('participants'); }}>加载更多</button>}
  </section>;
}
