'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Brain, Check, ChevronDown, ChevronRight, CircleAlert, FileText, FolderOpen, Globe2, History, Layers3, MessageSquare, Search, SlidersHorizontal, Trash2, UserRound, X } from 'lucide-react';
import type { DatabaseDataPlaneState } from '../lib/runtime-adapter';
import type { RuntimeStatus } from '../lib/pulsara-types';
import { MemoryApiError, type LocalMemoryApi, type MemoryDetail, type MemoryFact, type MemoryKind, type MemoryProject, type MemoryRecord, type MemorySelection } from '../lib/memory-api';
import { DatabaseSetupGuide } from './database-setup-guide';

export const memoryKindLabels: Record<MemoryKind, string> = { FACT: '事实', USER_PROFILE: '关于你', RESPONSE_PREFERENCE: '回答偏好', DECISION: '决策' };
const memoryKindIcons = { FACT: FileText, USER_PROFILE: UserRound, RESPONSE_PREFERENCE: MessageSquare, DECISION: Layers3 };
const relationLabels = { BASED_ON: '依据', BASIS_FOR: '作为依据', UPDATES: '更新了', UPDATED_BY: '已被更新为', CONFLICTS_WITH: '存在冲突' };
const conflictLabels: Record<string, string> = {
  ACTIVE_SEMANTIC_COLLISION: '已有相同内容正在使用', RESTORATION_SEMANTIC_COLLISION: '这些旧记忆内容重复',
  RESPONSE_PREFERENCE_CAPACITY: '恢复后回答偏好过多', SURVIVING_SUPERSEDE_ANCESTRY: '仍有较新的记忆保留，不能自动恢复这条旧记忆',
};
const date = (value: string) => new Date(value).toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });
function errorText(error: unknown) { return error instanceof Error ? error.message : '记忆操作失败，请重试'; }

interface Props {
  api: LocalMemoryApi; databaseState: DatabaseDataPlaneState | undefined;
  runtimeStatus: RuntimeStatus; onReconnect: () => void;
  onOpenSettings: () => void; onOpenSource: (source: NonNullable<MemoryDetail['source']>) => void;
}

export function MemoryView(props: Props) {
  const connecting = props.runtimeStatus === 'starting' || props.runtimeStatus === 'reconnecting';
  const connectionLabel = props.runtimeStatus === 'failed' ? '本地服务连接失败' : props.runtimeStatus === 'offline' ? '本地服务连接已中断' : props.runtimeStatus === 'reconnecting' ? '正在重新连接本地服务…' : '正在连接本地服务…';
  return <section className="memory-view"><header className="page-header"><div><span className="page-kicker">个人记忆</span><h1>记忆</h1><p>查看留下的背景与偏好，整理不再需要的内容。</p></div></header>
    {props.databaseState && props.databaseState !== 'ready' ? <DatabaseSetupGuide state={props.databaseState} variant="overview" onOpenSettings={props.onOpenSettings} /> : props.databaseState === 'ready' && props.runtimeStatus === 'online' ? <MemoryContent {...props} /> : (
      <div className="memory-layout"><div className="memory-main memory-collection memory-empty" role="status">
        <span className="memory-empty-icon">{connecting ? <Brain size={25} aria-hidden="true" /> : <CircleAlert size={25} aria-hidden="true" />}</span>
        <h2>{connectionLabel}</h2>
        <p>{connecting ? '连接后即可查看和管理记忆。' : '暂时无法读取记忆，请重新连接本地服务。'}</p>
        {!connecting && <button className="secondary-action memory-reconnect" onClick={props.onReconnect}>重新连接</button>}
      </div></div>
    )}
  </section>;
}

function MemoryContent({ api, onOpenSource }: Props) {
  const [view, setView] = useState<'global' | 'project'>('global');
  const [projects, setProjects] = useState<MemoryProject[]>([]);
  const [workspace, setWorkspace] = useState('');
  const [kind, setKind] = useState(''); const [lifecycle, setLifecycle] = useState('active');
  const [search, setSearch] = useState(''); const [items, setItems] = useState<MemoryFact[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [pendingDetail, setPendingDetail] = useState<MemoryFact | null>(null);
  const [confirmation, setConfirmation] = useState<MemoryRecord[] | null>(null);
  const [additional, setAdditional] = useState<string[]>([]);
  const [busy, setBusy] = useState(false); const [loading, setLoading] = useState(true);
  const [error, setError] = useState(''); const [notice, setNotice] = useState(''); const [revision, setRevision] = useState(0);
  const detailRequest = useRef(0);
  const selection: MemorySelection = { view, workspace_id: view === 'project' ? workspace : null };
  const key = `${view}:${workspace}:${kind}:${lifecycle}:${search}:${revision}`;
  const [previousQuery, setPreviousQuery] = useState({ api, key });
  const currentKey = useRef(key);
  useEffect(() => { currentKey.current = key; }, [key]);
  useEffect(() => () => { currentKey.current = ''; detailRequest.current++; }, []);

  // Reset only when the query changes, before committing its new view. A
  // deferred reset can erase errors already returned by the projects request.
  if (previousQuery.api !== api || previousQuery.key !== key) {
    setPreviousQuery({ api, key });
    setItems([]); setDetail(null); setPendingDetail(null); setConfirmation(null);
    setAdditional([]); setCursor(null); setError('');
    setLoading(view !== 'project' || Boolean(workspace));
  }

  useEffect(() => {
    let active = true;
    void (async () => {
      let next: string | undefined;
      do {
        const p = await api.projects(next);
        if (!active) return;
        const first = next === undefined;
        setProjects(old => first ? p.items : [...old, ...p.items]);
        next = p.next_cursor ?? undefined;
      } while (next);
    })().catch(e => { if (active) setError(errorText(e)); });
    return () => { active = false; };
  }, [api]);
  useEffect(() => {
    let active = true;
    detailRequest.current++;
    if (view === 'project' && !workspace) {
      return () => { active = false; };
    }
    const timer = setTimeout(() => {
      api.catalog({ view, workspace_id: view === 'project' ? workspace : null }, { kind, lifecycle, search }).then(p => { if (active) { setItems(p.items); setCursor(p.next_cursor); } }).catch(e => { if (active) setError(errorText(e)); }).finally(() => { if (active) setLoading(false); });
    }, 180);
    return () => { active = false; clearTimeout(timer); };
  }, [api, view, workspace, kind, lifecycle, search, revision]);

  async function openFact(fact: MemoryFact, append = false) {
    if (busy || pendingDetail?.fact_id === fact.fact_id || (!append && !pendingDetail && detail?.fact.fact_id === fact.fact_id)) return;
    const requestId = ++detailRequest.current;
    // Keep the mounted panel and its content while reading, so selecting a
    // different memory does not collapse/re-expand the entire page layout.
    setError(''); setPendingDetail(fact);
    try {
      const selected: MemorySelection = fact.context_id === 'ctx:global' ? { view: 'global', workspace_id: null } : { view: 'project', workspace_id: fact.context_id };
      const next = await api.detail(selected, fact.fact_id, append ? detail?.next_cursor ?? undefined : undefined);
      if (detailRequest.current !== requestId) return;
      setDetail(previous => append && previous ? { ...next, relations: [...previous.relations, ...next.relations] } : next);
      if (!append) { setConfirmation(null); setAdditional([]); }
    } catch (e) { if (detailRequest.current === requestId) setError(errorText(e)); }
    finally { if (detailRequest.current === requestId) setPendingDetail(null); }
  }
  async function preview(roots = additional) {
    if (!detail || pendingDetail) return;
    const requestId = ++detailRequest.current;
    setBusy(true); setError(''); setConfirmation(null);
    try {
      const fact = detail.fact;
      const selected: MemorySelection = { view: fact.context_id === 'ctx:global' ? 'global' : 'project', workspace_id: fact.context_id === 'ctx:global' ? null : fact.context_id };
      const result = await api.preview(selected, fact.fact_id, roots);
      if (detailRequest.current === requestId) setConfirmation(result);
    } catch (e) { if (detailRequest.current === requestId) setError(errorText(e)); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!detail || !confirmation || busy || pendingDetail) return;
    setBusy(true); setError('');
    try {
      await api.delete(detail.fact.fact_id, confirmation);
      setDetail(null); setConfirmation(null); setAdditional([]); setRevision(v => v + 1);
      setNotice('记忆已删除。正在进行的回复不会被改写，之后的新一轮将使用更新后的记忆。');
    } catch (e) {
      setError(errorText(e));
      setConfirmation(e instanceof MemoryApiError && e.preview ? e.preview : null);
      if (e instanceof MemoryApiError && e.status === 404) { setDetail(null); setRevision(v => v + 1); setNotice('这条记忆已不存在，列表已更新。'); }
    } finally { setBusy(false); }
  }
  const label = (fact: MemoryFact) => fact.context_label ?? (fact.context_id === 'ctx:global' ? '跨对话' : projects.find(p => p.workspace_id === fact.context_id)?.label ?? '项目');
  const card = (fact: MemoryFact) => <><p className="memory-statement">{fact.statement}</p><span className="memory-metadata"><span className="memory-kind">{memoryKindLabels[fact.kind]}</span><span>{label(fact)}</span><time dateTime={fact.updated_at}>{date(fact.updated_at)}</time></span></>;
  const header = confirmation?.[0];
  const ready = header?.type === 'HEADER' && header.disposition === 'READY';
  const restores = confirmation?.filter((r): r is Extract<MemoryRecord, { type: 'FACT_RESTORE' }> => r.type === 'FACT_RESTORE') ?? [];
  const filtered = Boolean(search || kind);
  const emptyTitle = view === 'project' && !workspace ? '选择一个项目' : filtered ? '没有找到匹配的记忆' : lifecycle === 'updated' ? '还没有已更新的记忆' : view === 'global' ? '还没有跨对话记忆' : '这个项目还没有记忆';
  const emptyCopy = view === 'project' && !workspace ? '同一个目录下的会话，共享这里的项目记忆。' : filtered ? '试试其他关键词，或切换记忆类别。' : lifecycle === 'updated' ? '被新内容替代的记忆，会保留在这里供你查看。' : '在对话中告诉 Pulsara 值得记住的背景或偏好，整理后会出现在这里。';
  const detailBusy = busy || Boolean(pendingDetail);
  return <div className={`memory-layout${detail || pendingDetail ? ' has-detail' : ''}`}>
    <div className="memory-main">
      <div className="memory-scope-header"><div className="memory-tabs" role="tablist" aria-label="记忆范围">{(['global', 'project'] as const).map(v => <button role="tab" aria-selected={view === v} disabled={busy} key={v} onClick={() => setView(v)}>{v === 'global' ? <Globe2 size={15} aria-hidden="true" /> : <FolderOpen size={15} aria-hidden="true" />}{v === 'global' ? '跨对话' : '项目'}</button>)}</div><span className="memory-scope-note">{view === 'global' ? '在不同会话间延续的背景与偏好' : '仅在所选目录中共享'}</span></div>
      <div className="memory-collection">
      <div className="memory-toolbar">
      {view === 'project' && <div className="memory-project"><FolderOpen size={16} aria-hidden="true" /><select aria-label="选择项目" value={workspace} disabled={busy} onChange={e => setWorkspace(e.target.value)}><option value="">选择曾打开的项目目录</option>{projects.map(p => <option key={p.workspace_id} value={p.workspace_id}>{p.label} · {p.root}</option>)}</select><ChevronDown size={14} aria-hidden="true" /></div>}
      <div className="memory-controls"><label className="memory-search"><Search size={16} aria-hidden="true" /><input aria-label="搜索记忆" placeholder="搜索记忆正文…" value={search} disabled={busy} onChange={e => setSearch(e.target.value)} /></label><div className="memory-state-select"><History size={15} aria-hidden="true" /><select aria-label="记忆状态" value={lifecycle} disabled={busy} onChange={e => setLifecycle(e.target.value)}><option value="active">正在使用</option><option value="updated">已更新</option></select><ChevronDown size={13} aria-hidden="true" /></div></div>
      <div className="memory-filters" aria-label="记忆类别"><SlidersHorizontal size={14} aria-hidden="true" />{[['', '全部'], ...Object.entries(memoryKindLabels)].map(([value, title]) => <button key={value} disabled={busy} aria-pressed={kind === value} onClick={() => setKind(value)}>{title}</button>)}</div>
      </div>
      {error && <p role="alert" className="memory-error memory-feedback"><CircleAlert size={16} aria-hidden="true" />{error}</p>}{notice && <p role="status" className="memory-notice memory-feedback"><Check size={16} aria-hidden="true" />{notice}</p>}
      {loading ? <div className="memory-loading" role="status"><span className="memory-empty-icon"><Brain size={24} aria-hidden="true" /></span><p>正在读取记忆…</p></div> : !items.length ? <div className="memory-empty"><span className="memory-empty-icon">{view === 'project' ? <FolderOpen size={25} aria-hidden="true" /> : <Brain size={25} aria-hidden="true" />}</span><h2>{emptyTitle}</h2><p>{emptyCopy}</p></div> : <div className="memory-list">{items.map(f => {
        const Icon = memoryKindIcons[f.kind];
        return <button className={`memory-row${(pendingDetail ?? detail?.fact)?.fact_id === f.fact_id ? ' is-selected' : ''}`} key={f.fact_id} disabled={busy} onClick={() => void openFact(f)}><span className="memory-row-icon"><Icon size={17} aria-hidden="true" /></span><div className="memory-row-copy">{card(f)}<span className={`memory-usage${f.needs_confirmation && f.lifecycle === 'ACTIVE' ? ' is-conflicted' : ''}`}>{f.lifecycle === 'SUPERSEDED' ? <History size={12} aria-hidden="true" /> : f.needs_confirmation ? <CircleAlert size={12} aria-hidden="true" /> : null}{f.lifecycle === 'SUPERSEDED' ? '已更新' : f.needs_confirmation ? '需要确认' : f.kind === 'RESPONSE_PREFERENCE' ? '通常随新一轮提供' : '相关时使用'}</span></div><ChevronRight className="memory-row-chevron" size={16} aria-hidden="true" /></button>;
      })}</div>}
      </div>
      {cursor && <button className="memory-more" disabled={busy} onClick={async () => { const captured = key; setBusy(true); try { const p = await api.catalog(selection, { kind, lifecycle, search, cursor }); if (currentKey.current === captured) { setItems(old => [...old, ...p.items]); setCursor(p.next_cursor); } } catch (e) { setError(errorText(e)); } finally { setBusy(false); } }}>加载更多记忆</button>}
    </div>
    {(detail || pendingDetail) && <aside className="memory-detail" aria-label="记忆详情" aria-busy={Boolean(pendingDetail)}><header><span className="memory-detail-heading"><Brain size={17} aria-hidden="true" /><h2>记忆详情</h2></span>{pendingDetail && <span className="memory-detail-loading" role="status">正在读取…</span>}<button aria-label="关闭记忆详情" disabled={busy} onClick={() => { detailRequest.current++; setDetail(null); setPendingDetail(null); setConfirmation(null); setAdditional([]); }}><X size={17} /></button></header>{detail && <div className="memory-detail-body">
      {card(detail.fact)}<p className="memory-usage">记录于 {date(detail.fact.recorded_at)}</p><h3>适用条件与例外</h3><p>以正文中的条件、时间和例外为准；适用于{label(detail.fact)}。</p>
      {detail.fact.needs_confirmation && detail.fact.kind === 'RESPONSE_PREFERENCE' && <p>需要确认：冲突解决前暂不作为回答偏好使用。</p>}
      <h3>形成与整理</h3><p>{detail.formation}</p>{detail.public_summary && <p>{detail.public_summary}</p>}
      {detail.source && <button className="memory-source" disabled={detailBusy} onClick={() => onOpenSource(detail.source!)}>在对话中查看<ArrowUpRight size={14} aria-hidden="true" /></button>}
      {!!detail.relations.length && <h3>依据、更新与冲突</h3>}{detail.relations.map(r => <div className="memory-relation" key={r.relation_id}><strong>{relationLabels[r.relative_role]}</strong><button disabled={detailBusy} onClick={() => void openFact(r.companion)}>{card(r.companion)}</button>{r.public_summary && <p>{r.public_summary}</p>}</div>)}
      {detail.next_cursor && <button disabled={detailBusy} onClick={() => void openFact(detail.fact, true)}>更多关系</button>}
      {!confirmation && <button className="memory-delete" disabled={detailBusy} onClick={() => void preview()}><Trash2 size={14} />{busy ? '正在读取…' : '删除记忆…'}</button>}
      {confirmation && <section className="memory-confirmation" aria-label="删除影响"><h3>确认删除影响</h3><p>删除无法撤销。依赖这些内容的记忆也会一并删除；对话原文不变。</p>
        {confirmation.map((r, i) => {
          if (r.type === 'FACT_DELETE') return <div className="memory-impact" key={i}><strong>将删除</strong>{card(r.fact)}</div>;
          if (r.type === 'FACT_RESTORE') return <div className="memory-impact" key={i}><strong>{ready ? '将恢复为正在使用' : '待确认的恢复项'}</strong>{card(r.fact)}</div>;
          if (r.type === 'RELATION_EFFECT') return <div className="memory-impact" key={i}><strong>{r.effect === 'REMOVED' ? '移除关系' : '恢复后需要确认'} · {relationLabels[r.relative_role]}</strong>{card(r.subject)}<span>与</span>{card(r.companion)}{r.public_summary && <p>{r.public_summary}</p>}</div>;
          if (r.type === 'RESTORATION_CONFLICT') return <div className="memory-impact memory-error" key={i}><strong>{conflictLabels[r.reason] ?? '旧记忆需要你确认'}</strong>{card(r.subject)}{r.companion && card(r.companion)}</div>;
          return null;
        })}
        {!ready && <fieldset disabled={detailBusy}><legend>选择不再需要的旧记忆，一并删除后重新预览</legend>{restores.map(r => <label key={r.fact.fact_id}><input type="checkbox" checked={additional.includes(r.fact.fact_id)} onChange={e => setAdditional(old => e.target.checked ? [...old, r.fact.fact_id] : old.filter(id => id !== r.fact.fact_id))} />{r.fact.statement}</label>)}<button disabled={detailBusy || !additional.length} onClick={() => void preview()}>重新计算影响</button></fieldset>}
        <footer><button disabled={detailBusy} onClick={() => { setConfirmation(null); setAdditional([]); }}>取消</button><button className="memory-delete" disabled={detailBusy || !ready} onClick={() => void remove()}>{busy ? '正在处理…' : '确认删除'}</button></footer>
      </section>}
      </div>}
    </aside>}
  </div>;
}
