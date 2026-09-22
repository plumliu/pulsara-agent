'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowRight, ArrowUpRight, Brain, Check, ChevronDown, ChevronRight, CircleAlert, FileText, FolderOpen, Globe2, History, Layers3, MessageSquare, Pencil, Search, SlidersHorizontal, Trash2, UserRound, X } from 'lucide-react';
import type { DatabaseDataPlaneState } from '../lib/runtime-adapter';
import type { RuntimeStatus } from '../lib/pulsara-types';
import { MemoryApiError, type LocalMemoryApi, type MemoryDetail, type MemoryFact, type MemoryKind, type MemoryProject, type MemoryRecord, type MemorySelection, type MemorySource } from '../lib/memory-api';
import { DatabaseSetupGuide } from './database-setup-guide';

export const memoryKindLabels: Record<MemoryKind, string> = { FACT: '事实', USER_PROFILE: '关于你', RESPONSE_PREFERENCE: '回答偏好', DECISION: '决策' };
const memoryKindIcons = { FACT: FileText, USER_PROFILE: UserRound, RESPONSE_PREFERENCE: MessageSquare, DECISION: Layers3 };
const relationLabels = { BASED_ON: '依据关系', BASIS_FOR: '依据关系', UPDATES: '取代关系', UPDATED_BY: '取代关系', CONFLICTS_WITH: '冲突标记' };
const relationEnds = { BASED_ON: ['依赖的记忆', '依据记忆'], BASIS_FOR: ['依据记忆', '依赖的记忆'], UPDATES: ['新记忆', '旧记忆'], UPDATED_BY: ['旧记忆', '新记忆'], CONFLICTS_WITH: ['记忆一', '记忆二'] } as const;
const relationPhrases = { BASED_ON: 'is based on', BASIS_FOR: 'is the basis of', UPDATES: 'supersedes', UPDATED_BY: 'is superseded by', CONFLICTS_WITH: 'contradicts' } as const;
const conflictLabels: Record<string, string> = {
  ACTIVE_SEMANTIC_COLLISION: '已有相同内容正在使用', RESTORATION_SEMANTIC_COLLISION: '这些旧记忆内容重复',
  RESPONSE_PREFERENCE_CAPACITY: '恢复后回答偏好过多', SURVIVING_SUPERSEDE_ANCESTRY: '仍有较新的记忆保留，不能自动恢复这条旧记忆',
};
const date = (value: string) => new Date(value).toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });
function errorText(error: unknown) { return error instanceof Error ? error.message : '记忆操作失败，请重试'; }

interface Props {
  api: LocalMemoryApi; databaseState: DatabaseDataPlaneState | undefined;
  runtimeStatus: RuntimeStatus; onReconnect: () => void;
  onOpenSettings: () => void; onOpenSource: (source: MemorySource) => void;
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
  const [jumpTarget, setJumpTarget] = useState<MemoryFact | null>(null);
  const [jumpSerial, setJumpSerial] = useState(0);
  const [confirmation, setConfirmation] = useState<MemoryRecord[] | null>(null);
  const [editDraft, setEditDraft] = useState<string | null>(null);
  const [editError, setEditError] = useState('');
  const [previewCurrent, setPreviewCurrent] = useState(true);
  const [additional, setAdditional] = useState<string[]>([]);
  const [busy, setBusy] = useState(false); const [loading, setLoading] = useState(true);
  const [error, setError] = useState(''); const [notice, setNotice] = useState(''); const [revision, setRevision] = useState(0);
  const detailRequest = useRef(0);
  const jumpRequest = useRef<MemoryFact | null>(null);
  const jumpRow = useRef<HTMLButtonElement>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const selection: MemorySelection = { view, workspace_id: view === 'project' ? workspace : null };
  const key = `${view}:${workspace}:${kind}:${lifecycle}:${search}:${revision}`;
  const [previousQuery, setPreviousQuery] = useState({ api, key });
  const currentKey = useRef(key);
  const jumpView = jumpTarget?.context_id === 'ctx:global' ? 'global' : 'project';
  const activeJump = jumpTarget && view === jumpView && (view === 'global' || workspace === jumpTarget.context_id)
    && kind === '' && search === '' && lifecycle === (jumpTarget.lifecycle === 'SUPERSEDED' ? 'updated' : 'active') ? jumpTarget : null;
  const visibleItems = activeJump && !items.some(item => item.fact_id === activeJump.fact_id) ? [activeJump, ...items] : items;
  const jumpId = activeJump?.fact_id;
  useEffect(() => { currentKey.current = key; }, [key]);
  useEffect(() => () => { currentKey.current = ''; detailRequest.current++; }, []);
  useEffect(() => { if (jumpId) jumpRow.current?.scrollIntoView?.({ block: 'nearest' }); }, [jumpId, items]);

  // Reset only when the query changes, before committing its new view. A
  // deferred reset can erase errors already returned by the projects request.
  if (previousQuery.api !== api || previousQuery.key !== key) {
    setPreviousQuery({ api, key });
    setItems([]);
    if (!activeJump) { setDetail(null); setPendingDetail(null); setEditDraft(null); }
    setConfirmation(null);
    setAdditional([]); setCursor(null); setError('');
    setLoading(view !== 'project' || Boolean(workspace));
  }

  useEffect(() => {
    let active = true;
    void (async () => {
      let next: string | undefined;
      const loaded: MemoryProject[] = [];
      do {
        const p = await api.projects(next);
        if (!active) return;
        loaded.push(...p.items);
        const first = next === undefined;
        setProjects(old => first ? p.items : [...old, ...p.items]);
        next = p.next_cursor ?? undefined;
      } while (next);
      setWorkspace(current => current && !loaded.some(p => p.workspace_id === current) ? '' : current);
    })().catch(e => { if (active) setError(errorText(e)); });
    return () => { active = false; };
  }, [api, revision]);
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

  const openFact = useCallback(async (fact: MemoryFact, append = false) => {
    if (busy || pendingDetail?.fact_id === fact.fact_id || (!append && !pendingDetail && detail?.fact.fact_id === fact.fact_id)) return;
    const requestId = ++detailRequest.current;
    // Keep the mounted panel and its content while reading, so selecting a
    // different memory does not collapse/re-expand the entire page layout.
    setError(''); setEditDraft(null); setEditError(''); setPendingDetail(fact);
    try {
      const selected: MemorySelection = fact.context_id === 'ctx:global' ? { view: 'global', workspace_id: null } : { view: 'project', workspace_id: fact.context_id };
      const next = await api.detail(selected, fact.fact_id, append ? detail?.next_cursor ?? undefined : undefined);
      if (detailRequest.current !== requestId) return;
      setDetail(previous => append && previous ? { ...next, relations: [...previous.relations, ...next.relations] } : next);
      if (!append) { setConfirmation(null); setAdditional([]); }
    } catch (e) { if (detailRequest.current === requestId) { setError(errorText(e)); setJumpTarget(current => current?.fact_id === fact.fact_id ? null : current); } }
    finally { if (detailRequest.current === requestId) setPendingDetail(null); }
  }, [api, busy, pendingDetail, detail]);
  useEffect(() => {
    const target = jumpRequest.current;
    if (!target || !activeJump || target.fact_id !== activeJump.fact_id) return;
    jumpRequest.current = null;
    void openFact(target);
  }, [jumpSerial, activeJump, openFact]);
  function clearJump() { jumpRequest.current = null; setJumpTarget(null); }
  function jumpToFact(fact: MemoryFact) {
    setEditDraft(null); setEditError('');
    jumpRequest.current = fact;
    setJumpTarget(fact);
    setJumpSerial(value => value + 1);
    setView(fact.context_id === 'ctx:global' ? 'global' : 'project');
    setWorkspace(fact.context_id === 'ctx:global' ? '' : fact.context_id);
    setKind(''); setSearch('');
    setLifecycle(fact.lifecycle === 'SUPERSEDED' ? 'updated' : 'active');
  }
  async function openSource(relationId?: string) {
    if (!detail || busy || pendingDetail) return;
    const requestId = ++detailRequest.current;
    const fact = detail.fact;
    const selected: MemorySelection = { view: fact.context_id === 'ctx:global' ? 'global' : 'project', workspace_id: fact.context_id === 'ctx:global' ? null : fact.context_id };
    setBusy(true); setError('');
    try {
      let fresh = await api.detail(selected, fact.fact_id);
      while (relationId && !fresh.relations.some(r => r.relation_id === relationId) && fresh.next_cursor) {
        const page = await api.detail(selected, fact.fact_id, fresh.next_cursor);
        fresh = { ...page, relations: [...fresh.relations, ...page.relations] };
      }
      if (detailRequest.current !== requestId) return;
      setDetail(fresh);
      const source = relationId ? fresh.relations.find(r => r.relation_id === relationId)?.owner.source : fresh.source;
      if (source?.locator) onOpenSource(source.locator);
      else setNotice(source?.availability === 'DELETED' ? '最初保存位置已删除，记忆仍然保留。' : '来源已不可打开，详情已刷新。');
    } catch (e) { if (detailRequest.current === requestId) setError(errorText(e)); }
    finally { setBusy(false); }
  }
  async function preview(roots = additional) {
    if (!detail || pendingDetail) return;
    const requestId = ++detailRequest.current;
    setBusy(true); setError(''); setPreviewCurrent(false);
    try {
      const fact = detail.fact;
      const selected: MemorySelection = { view: fact.context_id === 'ctx:global' ? 'global' : 'project', workspace_id: fact.context_id === 'ctx:global' ? null : fact.context_id };
      const result = await api.preview(selected, fact.fact_id, roots);
      if (detailRequest.current === requestId) { setConfirmation(result); setPreviewCurrent(true); }
    } catch (e) { if (detailRequest.current === requestId) setError(errorText(e)); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!detail || !confirmation || !previewCurrent || busy || pendingDetail) return;
    setBusy(true); setError('');
    try {
      await api.delete(detail.fact.fact_id, confirmation);
      setDetail(null); setConfirmation(null); setAdditional([]); clearJump(); setRevision(v => v + 1);
      setNotice('记忆已删除。正在进行的回复不会被改写，之后的新一轮将使用更新后的记忆。');
    } catch (e) {
      setError(errorText(e));
      setConfirmation(e instanceof MemoryApiError && e.preview ? e.preview : null);
      setPreviewCurrent(Boolean(e instanceof MemoryApiError && e.preview));
      if (e instanceof MemoryApiError && e.status === 404) { setDetail(null); clearJump(); setRevision(v => v + 1); setNotice('这条记忆已不存在，列表已更新。'); }
    } finally { setBusy(false); }
  }
  async function saveEdit() {
    if (!detail || editDraft === null || busy || pendingDetail) return;
    const normalized = editDraft.replace(/\r\n?/gu, '\n').normalize('NFC').trim();
    const maxBytes = detail.fact.kind === 'RESPONSE_PREFERENCE' ? 2048 : 8192;
    if (!normalized || new TextEncoder().encode(normalized).length > maxBytes) {
      setEditError(`记忆正文必须在 1 至 ${maxBytes} UTF-8 字节之间`); return;
    }
    setBusy(true); setEditError('');
    try {
      const selection: MemorySelection = detail.fact.context_id === 'ctx:global'
        ? { view: 'global', workspace_id: null }
        : { view: 'project', workspace_id: detail.fact.context_id };
      const result = await api.editStatement(selection, detail.fact, normalized);
      const updated = { ...detail.fact, ...result.fact };
      setDetail(old => old?.fact.fact_id === updated.fact_id
        ? { ...old, fact: updated, user_edited_at: result.user_edited_at } : old);
      setItems(old => old.map(item => item.fact_id === updated.fact_id ? updated : item));
      if (result.changed) {
        jumpRequest.current = null; setJumpTarget(updated);
        setKind(''); setSearch('');
        setLifecycle(updated.lifecycle === 'SUPERSEDED' ? 'updated' : 'active');
        setNotice('记忆正文已保存；已有关系未自动更改。');
      }
      setEditDraft(null);
    } catch (e) {
      setEditError(e instanceof MemoryApiError
        ? errorText(e)
        : '提交结果暂时无法确认。请重新打开详情核对正文，再决定是否重试；草稿仍保留。');
    }
    finally { setBusy(false); }
  }
  const label = (fact: MemoryFact) => fact.context_id === 'ctx:global' ? '全局记忆' : fact.context_label ?? projects.find(p => p.workspace_id === fact.context_id)?.label ?? '项目记忆';
  const card = (fact: MemoryFact) => <><p className="memory-statement">{fact.statement}</p><span className="memory-metadata"><span className="memory-kind">{memoryKindLabels[fact.kind]}</span><span>{label(fact)}</span><time dateTime={fact.updated_at}>{date(fact.updated_at)}</time></span></>;
  const filtered = Boolean(search || kind);
  const emptyTitle = view === 'project' && !workspace ? '选择一个项目' : filtered ? '没有找到匹配的记忆' : lifecycle === 'updated' ? '暂无已被替代的记忆' : view === 'global' ? '还没有全局记忆' : '这个项目还没有记忆';
  const emptyCopy = view === 'project' && !workspace ? '同一个目录下的会话，共享这里的项目记忆。' : filtered ? '试试其他关键词，或切换记忆类别。' : lifecycle === 'updated' ? '被新内容替代的记忆，会保留在这里供你查看。' : '在对话中告诉 Pulsara 值得记住的背景或偏好，保存后会出现在这里。';
  const detailBusy = busy || Boolean(pendingDetail);
  return <div className={`memory-layout${detail || pendingDetail ? ' has-detail' : ''}`}>
    <div className="memory-main">
      <div className="memory-scope-header"><div className="memory-tabs" role="tablist" aria-label="记忆范围">{(['global', 'project'] as const).map(v => <button role="tab" aria-selected={view === v} disabled={busy} key={v} onClick={() => { clearJump(); setView(v); }}>{v === 'global' ? <Globe2 size={15} aria-hidden="true" /> : <FolderOpen size={15} aria-hidden="true" />}{v === 'global' ? '全局记忆' : '项目记忆'}</button>)}</div><span className="memory-scope-note">{view === 'global' ? '在不同会话间延续的背景与偏好' : '仅在所选目录中共享'}</span></div>
      <div className="memory-collection">
      <div className="memory-toolbar">
      {view === 'project' && <div className="memory-project"><FolderOpen size={16} aria-hidden="true" /><select aria-label="选择项目" value={workspace} disabled={busy} onChange={e => { clearJump(); setWorkspace(e.target.value); }}><option value="">选择有记忆的项目</option>{projects.map(p => <option key={p.workspace_id} value={p.workspace_id}>{p.label} · {p.root}</option>)}</select><ChevronDown size={14} aria-hidden="true" /></div>}
      <div className="memory-controls"><label className="memory-search"><Search size={16} aria-hidden="true" /><input aria-label="搜索记忆" placeholder="搜索记忆正文…" value={search} disabled={busy} onChange={e => { clearJump(); setSearch(e.target.value); }} /></label><div className="memory-state-select"><History size={15} aria-hidden="true" /><select aria-label="记忆状态" value={lifecycle} disabled={busy} onChange={e => { clearJump(); setLifecycle(e.target.value); }}><option value="active">正在使用</option><option value="updated">已被替代</option></select><ChevronDown size={13} aria-hidden="true" /></div></div>
      <div className="memory-filters" aria-label="记忆类别"><SlidersHorizontal size={14} aria-hidden="true" />{[['', '全部'], ...Object.entries(memoryKindLabels)].map(([value, title]) => <button key={value} disabled={busy} aria-pressed={kind === value} onClick={() => { clearJump(); setKind(value); }}>{title}</button>)}</div>
      </div>
      {error && !confirmation && <p role="alert" className="memory-error memory-feedback"><CircleAlert size={16} aria-hidden="true" />{error}</p>}{notice && <p role="status" className="memory-notice memory-feedback"><Check size={16} aria-hidden="true" />{notice}</p>}
      {loading && !activeJump ? <div className="memory-loading" role="status"><span className="memory-empty-icon"><Brain size={24} aria-hidden="true" /></span><p>正在读取记忆…</p></div> : !visibleItems.length ? <div className="memory-empty"><span className="memory-empty-icon">{view === 'project' ? <FolderOpen size={25} aria-hidden="true" /> : <Brain size={25} aria-hidden="true" />}</span><h2>{emptyTitle}</h2><p>{emptyCopy}</p></div> : <div className="memory-list">{visibleItems.map(f => {
        const Icon = memoryKindIcons[f.kind];
        return <button ref={jumpId === f.fact_id ? jumpRow : undefined} className={`memory-row${(pendingDetail ?? detail?.fact ?? activeJump)?.fact_id === f.fact_id ? ' is-selected' : ''}`} key={f.fact_id} disabled={busy} onClick={() => { clearJump(); void openFact(f); }}><span className="memory-row-icon"><Icon size={17} aria-hidden="true" /></span><div className="memory-row-copy">{card(f)}{(f.lifecycle === 'SUPERSEDED' || f.needs_confirmation || f.kind === 'RESPONSE_PREFERENCE') && <span className={`memory-usage${f.needs_confirmation && f.lifecycle === 'ACTIVE' ? ' is-conflicted' : ''}`}>{f.lifecycle === 'SUPERSEDED' ? <History size={12} aria-hidden="true" /> : f.needs_confirmation ? <CircleAlert size={12} aria-hidden="true" /> : null}{f.lifecycle === 'SUPERSEDED' ? '已被替代' : f.needs_confirmation ? '需要确认' : '通常随新一轮提供'}</span>}</div><ChevronRight className="memory-row-chevron" size={16} aria-hidden="true" /></button>;
      })}</div>}
      </div>
      {cursor && <button className="memory-more" disabled={busy} onClick={async () => { const captured = key; setBusy(true); try { const p = await api.catalog(selection, { kind, lifecycle, search, cursor }); if (currentKey.current === captured) { setItems(old => [...old, ...p.items]); setCursor(p.next_cursor); } } catch (e) { setError(errorText(e)); } finally { setBusy(false); } }}>加载更多记忆</button>}
    </div>
    {(detail || pendingDetail) && <aside className="memory-detail" aria-label="记忆详情" aria-busy={Boolean(pendingDetail)}><header><span className="memory-detail-heading"><Brain size={17} aria-hidden="true" /><h2>记忆详情</h2></span>{pendingDetail && <span className="memory-detail-loading" role="status">正在读取…</span>}<button aria-label="关闭记忆详情" disabled={busy} onClick={() => { detailRequest.current++; clearJump(); setEditDraft(null); setDetail(null); setPendingDetail(null); setConfirmation(null); setAdditional([]); }}><X size={17} /></button></header>{detail && <div className="memory-detail-body">
      {editDraft === null ? card(detail.fact) : <div className="memory-editor"><label htmlFor="memory-statement-edit">记忆正文</label><textarea id="memory-statement-edit" value={editDraft} disabled={busy} onChange={e => { setEditDraft(e.target.value); setEditError(''); }} /><span className="memory-metadata"><span className="memory-kind">{memoryKindLabels[detail.fact.kind]}</span><span>{label(detail.fact)}</span></span><p>仅修改这条记忆的文字。已有依据、取代和冲突关系不会自动重判；请自行核对。</p>{editError && <p role="alert" className="memory-error">{editError}</p>}<div className="memory-editor-actions"><button disabled={busy} onClick={() => { setEditDraft(null); setEditError(''); }}>取消</button><button disabled={busy} onClick={() => void saveEdit()}>{busy ? '正在保存…' : '保存正文'}</button></div></div>}
      {detail.user_edited_at && editDraft === null && <p className="memory-edited-note">用户已编辑正文；来源对话只记录最初保存的位置。</p>}
      {detail.fact.needs_confirmation && detail.fact.kind === 'RESPONSE_PREFERENCE' && <p>需要确认：冲突解决前暂不作为回答偏好使用。</p>}
      <div className="memory-detail-actions">
        {detail.source.locator ? <button className="memory-source" disabled={detailBusy} onClick={() => void openSource()}>在对话中查看<ArrowUpRight size={14} aria-hidden="true" /></button> : <span className="memory-detail-unavailable">{detail.source.availability === 'DELETED' ? '最初保存位置已删除' : '来源对话已关闭'}</span>}
        <button className="memory-edit" disabled={detailBusy || editDraft !== null || Boolean(confirmation)} onClick={() => { setEditDraft(detail.fact.statement); setEditError(''); }}><Pencil size={14} aria-hidden="true" />编辑记忆</button>
        <button ref={deleteButton} className="memory-delete" disabled={detailBusy || editDraft !== null || Boolean(confirmation)} onClick={() => void preview()}><Trash2 size={14} />{busy ? '正在读取…' : '删除记忆…'}</button>
      </div>
      <section className="memory-graph" aria-label="记忆关系图">
        <h3>关系</h3>
        <div className="memory-graph-current"><span className="memory-graph-caption">当前记忆</span><span className="memory-graph-statement">{detail.fact.statement}</span></div>
        {detail.relations.length ? <div className="memory-graph-branches">{detail.relations.map(r => <div className="memory-graph-branch" key={r.relation_id}>
            <div className="memory-graph-edge"><ArrowRight size={15} aria-hidden="true" /><span>{relationPhrases[r.relative_role]}</span></div>
            <button className="memory-graph-node" disabled={detailBusy} onClick={() => jumpToFact(r.companion)} aria-label={`查看记忆：${r.companion.statement}`}><span className="memory-graph-statement">{r.companion.statement}</span><span className="memory-graph-caption">{memoryKindLabels[r.companion.kind]} · {label(r.companion)}{r.companion.lifecycle === 'SUPERSEDED' ? ' · 已被替代' : ''}</span></button>
            <div className="memory-graph-origin"><span>{r.owner.write_tool === 'remember' ? '保存时建立' : '后续标定'}</span>{r.owner.source.locator ? <button disabled={detailBusy} onClick={() => void openSource(r.relation_id)}>查看建立处<ArrowUpRight size={12} aria-hidden="true" /></button> : <span>· {r.owner.source.availability === 'DELETED' ? '最初保存位置已删除' : '来源对话已关闭'}</span>}</div>
          </div>)}</div> : <p className="memory-graph-empty">暂无关联记忆</p>}
        {detail.next_cursor && <button className="memory-graph-more" disabled={detailBusy} onClick={() => void openFact(detail.fact, true)}>更多关系</button>}
      </section>
      </div>}
    </aside>}
    {confirmation && detail && <MemoryDeletionDialog
      fact={detail.fact} records={confirmation} additional={additional} busy={detailBusy} error={error} previewCurrent={previewCurrent}
      returnFocusRef={deleteButton} scopeLabel={label}
      onToggleAdditional={(factId, checked) => setAdditional(old => checked ? [...old, factId] : old.filter(id => id !== factId))}
      onRepreview={() => void preview()} onConfirm={() => void remove()}
      onClose={() => { if (detailBusy) return; setConfirmation(null); setAdditional([]); setError(''); }}
    />}
  </div>;
}

interface MemoryDeletionDialogProps {
  fact: MemoryFact; records: MemoryRecord[]; additional: string[]; busy: boolean; error: string; previewCurrent: boolean;
  returnFocusRef: React.RefObject<HTMLButtonElement | null>;
  scopeLabel: (fact: MemoryFact) => string;
  onToggleAdditional: (factId: string, checked: boolean) => void;
  onRepreview: () => void; onConfirm: () => void; onClose: () => void;
}

function MemoryDeletionDialog({ fact, records, additional, busy, error, previewCurrent, returnFocusRef, scopeLabel, onToggleAdditional, onRepreview, onConfirm, onClose }: MemoryDeletionDialogProps) {
  const dialog = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  useEffect(() => { close.current = onClose; }, [onClose]);
  useEffect(() => {
    const application = document.querySelector<HTMLElement>('main.pulsara-shell');
    const previousInert = application?.inert ?? false;
    const returnFocus = returnFocusRef.current;
    if (application) application.inert = true;
    dialog.current?.focus();
    return () => {
      if (application) application.inert = previousInert;
      window.requestAnimationFrame(() => returnFocus?.isConnected && returnFocus.focus());
    };
  }, [returnFocusRef]);
  const header = records[0];
  const ready = previewCurrent && header?.type === 'HEADER' && header.disposition === 'READY';
  const deleted = records.filter((r): r is Extract<MemoryRecord, { type: 'FACT_DELETE' }> => r.type === 'FACT_DELETE');
  const companions = deleted.filter(r => r.fact.fact_id !== fact.fact_id);
  const restores = records.filter((r): r is Extract<MemoryRecord, { type: 'FACT_RESTORE' }> => r.type === 'FACT_RESTORE');
  const relations = records.filter((r): r is Extract<MemoryRecord, { type: 'RELATION_EFFECT' }> => r.type === 'RELATION_EFFECT');
  const conflicts = records.filter((r): r is Extract<MemoryRecord, { type: 'RESTORATION_CONFLICT' }> => r.type === 'RESTORATION_CONFLICT');
  const factLine = (item: MemoryFact) => <span className="memory-delete-fact"><span>{item.statement}</span><small>{memoryKindLabels[item.kind]} · {scopeLabel(item)}{item.lifecycle === 'SUPERSEDED' ? ' · 已被替代' : ''}</small></span>;
  const onKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); if (!busy) close.current(); return; }
    if (event.key !== 'Tab') return;
    const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), summary') ?? [])];
    if (!focusable.length) { event.preventDefault(); return; }
    const first = focusable[0]!; const last = focusable[focusable.length - 1]!;
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) { event.preventDefault(); first.focus(); }
  };
  return createPortal(<div className="memory-delete-overlay" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <section ref={dialog} className="memory-delete-dialog" role="dialog" aria-modal="true" aria-labelledby="memory-delete-title" tabIndex={-1} onKeyDown={onKeyDown}>
      <header><div><span className="memory-delete-kicker">确认操作</span><h2 id="memory-delete-title">删除这条记忆？</h2></div><button type="button" aria-label="关闭删除确认" disabled={busy} onClick={onClose}><X size={17} /></button></header>
      <div className="memory-delete-body">
        <div className="memory-delete-target"><strong>你选择删除</strong>{factLine(fact)}</div>
        <div className="memory-delete-totals" aria-label="删除影响概览"><span>删除记忆 <strong>{deleted.length}</strong> 条</span>{restores.length > 0 && <span>重新生效 <strong>{restores.length}</strong> 条</span>}{relations.length > 0 && <span>关系变化 <strong>{relations.length}</strong> 项</span>}</div>
        {error && <p className="memory-delete-alert" role="alert"><CircleAlert size={16} aria-hidden="true" />{error}</p>}
        {!ready && <div className="memory-delete-blocked" role="status"><CircleAlert size={18} aria-hidden="true" /><span><strong>目前不能直接删除</strong><small>{!previewCurrent ? busy ? '正在重新计算影响，请稍候。' : '影响未能重新计算，请重试或取消。' : '有旧记忆可能重新生效并产生冲突。请检查下方内容，选择还要一并删除的旧记忆，再重新计算影响。'}</small></span></div>}
        {companions.length > 0 && <section className="memory-delete-group"><h3>还会一并删除 <span>{companions.length} 条</span></h3><p>这些记忆依赖本次删除的内容，或是你另外选择删除的记忆。</p><ul>{companions.map(r => <li key={r.fact.fact_id}>{factLine(r.fact)}</li>)}</ul></section>}
        {restores.length > 0 && <section className="memory-delete-group"><h3>{ready ? '旧记忆将重新生效' : '以下旧记忆可能重新生效'} <span>{restores.length} 条</span></h3><ul>{restores.map(r => <li key={r.fact.fact_id}>{factLine(r.fact)}</li>)}</ul></section>}
        {conflicts.length > 0 && <section className="memory-delete-group memory-delete-conflicts"><h3>需要处理的冲突 <span>{conflicts.length} 项</span></h3><ul>{conflicts.map((r, i) => <li key={`${r.group}:${i}`}><strong>{conflictLabels[r.reason] ?? '这条旧记忆不能自动恢复'}</strong>{factLine(r.subject)}{r.companion && <small>相关记忆：{r.companion.statement}</small>}</li>)}</ul></section>}
        {!ready && restores.length > 0 && <fieldset className="memory-delete-choices" disabled={busy}><legend>还要一起删除哪些旧记忆？</legend><p>勾选后先重新计算影响；此时不会立即删除。</p>{restores.map(r => <label key={r.fact.fact_id}><input type="checkbox" checked={additional.includes(r.fact.fact_id)} onChange={e => onToggleAdditional(r.fact.fact_id, e.target.checked)} /><span>{r.fact.statement}</span></label>)}<button type="button" disabled={previewCurrent && !additional.length} onClick={onRepreview}>重新计算影响</button></fieldset>}
        {!ready && restores.length === 0 && (previewCurrent ? <p className="memory-delete-explainer">没有可在此选择的旧记忆。请取消，先到记忆页检查相关内容。</p> : <button type="button" className="memory-delete-retry" disabled={busy} onClick={onRepreview}>重新计算影响</button>)}
        {relations.length > 0 && <details className="memory-delete-relations"><summary>查看 {relations.length} 项关系变化</summary><ul>{relations.map(r => <li key={r.relation_id}><strong>{r.effect === 'REMOVED' ? `${relationLabels[r.relative_role]}将移除` : '恢复后两条记忆会同时生效，冲突标记仍保留'}</strong><div className="memory-delete-relation-facts"><p><small>{relationEnds[r.relative_role][0]}</small>{r.subject.statement}</p><p><small>{relationEnds[r.relative_role][1]}</small>{r.companion.statement}</p></div></li>)}</ul></details>}
      </div>
      <footer><button type="button" disabled={busy} onClick={onClose}>取消</button><button type="button" className="memory-delete-confirm" disabled={busy || !ready} onClick={onConfirm}>{busy ? previewCurrent ? '正在删除…' : '正在计算…' : '确认删除'}</button></footer>
    </section>
  </div>, document.body);
}
