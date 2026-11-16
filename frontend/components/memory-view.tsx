import { useEffect, useRef, useState } from 'react';
import { Brain, ChevronDown, ChevronRight, CircleAlert, FileText, FolderOpen, Globe2, MessageSquare, Pencil, Plus, Search, Trash2, UserRound, X } from 'lucide-react';
import type { DatabaseDataPlaneState } from '../lib/runtime-adapter';
import type { RuntimeStatus } from '../lib/pulsara-types';
import { MemoryApiError, type LocalMemoryApi, type MemoryKind, type MemoryProject, type MemoryRecord, type MemorySelection } from '../lib/memory-api';
import { DatabaseSetupGuide } from './database-setup-guide';
import type { MarkdownNotify } from './markdown-body';

export const memoryKindLabels: Record<MemoryKind, string> = { FACT: '事实', USER_PROFILE: '关于你', RESPONSE_PREFERENCE: '回答偏好' };
const kindIcons = { FACT: FileText, USER_PROFILE: UserRound, RESPONSE_PREFERENCE: MessageSquare };
const normalize = (value: string) => value.replace(/\r\n?/gu, '\n').normalize('NFC').trim();
const errorText = (error: unknown) => error instanceof Error ? error.message : '操作未能确认完成，请刷新核对后再决定';
interface Props {
  api: LocalMemoryApi; databaseState: DatabaseDataPlaneState | undefined;
  runtimeStatus: RuntimeStatus; onReconnect: () => void; onOpenSettings: () => void; onNotify: MarkdownNotify;
}
export function MemoryView(props: Props) {
  const connecting = props.runtimeStatus === 'starting' || props.runtimeStatus === 'reconnecting';
  const connectionLabel = props.runtimeStatus === 'failed' ? '本地服务连接失败' : props.runtimeStatus === 'offline' ? '本地服务连接已中断' : props.runtimeStatus === 'reconnecting' ? '正在重新连接本地服务…' : '正在连接本地服务…';
  return <section className="memory-view"><header className="page-header"><div><span className="page-kicker">个人记忆</span><h1>记忆</h1><p>保存值得记住的背景、事实与回答偏好。</p></div></header>
    {props.databaseState && props.databaseState !== 'ready' ? <DatabaseSetupGuide state={props.databaseState} variant="overview" onOpenSettings={props.onOpenSettings} /> : props.databaseState === 'ready' && props.runtimeStatus === 'online' ? <MemoryContent {...props} /> : <div className="memory-layout"><div className="memory-main memory-collection memory-empty" role="status"><span className="memory-empty-icon">{connecting ? <Brain size={25} aria-hidden="true" /> : <CircleAlert size={25} aria-hidden="true" />}</span><h2>{connectionLabel}</h2><p>{connecting ? '连接后即可查看和管理记忆。' : '暂时无法读取记忆，请重新连接本地服务。'}</p>{!connecting && <button className="secondary-action memory-reconnect" onClick={props.onReconnect}>重新连接</button>}</div></div>}
  </section>;
}
interface Editor { record?: MemoryRecord; source: MemorySelection; kind: MemoryKind; title: string; statement: string; scope: string }
function scopeOf(value: string): MemorySelection { return value === 'global' ? { view: 'global', workspace_id: null } : { view: 'project', workspace_id: value }; }
function MemoryContent({ api, onNotify }: Props) {
  const [view, setView] = useState<'global' | 'project'>('global');
  const [projects, setProjects] = useState<MemoryProject[]>([]);
  const [projectCursor, setProjectCursor] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState('');
  const [kind, setKind] = useState(''); const [search, setSearch] = useState('');
  const [items, setItems] = useState<MemoryRecord[]>([]); const [cursor, setCursor] = useState<string | null>(null);
  const [detail, setDetail] = useState<MemoryRecord>(); const [editor, setEditor] = useState<Editor>();
  const [confirmDelete, setConfirmDelete] = useState(false); const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true); const [failed, setFailed] = useState(false); const [revision, setRevision] = useState(0);
  const selection: MemorySelection = { view, workspace_id: view === 'project' ? workspace : null };
  const key = `${view}:${workspace}:${kind}:${search}:${revision}`; const currentKey = useRef(key); currentKey.current = key;
  const detailRequest = useRef(0);
  useEffect(() => {
    let alive = true;
    api.projects().then(page => { if (alive) { setProjects(page.items); setProjectCursor(page.next_cursor); } }).catch(error => { if (alive) onNotify('项目列表读取失败', errorText(error), 'warning'); });
    return () => { alive = false; };
  }, [api, onNotify]);
  useEffect(() => {
    let alive = true;
    setDetail(undefined); setEditor(undefined); setConfirmDelete(false); setItems([]); setCursor(null); setFailed(false);
    if (view === 'project' && !workspace) { setLoading(false); return; }
    setLoading(true);
    api.catalog(selection, { kind, search }).then(page => { if (alive) { setItems(page.items); setCursor(page.next_cursor); } }).catch(error => { if (alive) { setFailed(true); onNotify('记忆读取失败', errorText(error), 'warning'); } }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  // selection is represented by view/workspace; it is deliberately independent of the active session.
  }, [api, key]);
  async function open(record: MemoryRecord) {
    const request = ++detailRequest.current;
    setBusy(true); setEditor(undefined); setConfirmDelete(false);
    try { const value = await api.detail(selection, record.target.memory_id); if (request === detailRequest.current) setDetail(value); }
    catch (error) { onNotify('记忆详情读取失败', errorText(error), 'warning'); }
    finally { if (request === detailRequest.current) setBusy(false); }
  }
  function beginCreate() { setDetail(undefined); setConfirmDelete(false); setEditor({ source: selection, kind: 'FACT', title: '', statement: '', scope: view === 'global' ? 'global' : workspace }); }
  function beginEdit(record: MemoryRecord) { setEditor({ record, source: selection, kind: record.kind, title: record.title, statement: record.statement, scope: view === 'global' ? 'global' : workspace }); }
  async function save() {
    if (!editor || busy) return;
    const title = normalize(editor.title); const statement = editor.kind === 'RESPONSE_PREFERENCE' ? '' : normalize(editor.statement);
    if (!title || title.includes('\n') || Array.from(title).length > 128 || (editor.kind !== 'RESPONSE_PREFERENCE' && (!statement || Array.from(statement).length > 8000))) {
      onNotify('请检查记忆内容', '标题须为非空单行且最多128个字符；普通正文最多8000个字符。', 'warning'); return;
    }
    const destination = scopeOf(editor.scope); setBusy(true);
    try {
      const value = editor.record ? (await api.edit(editor.source, editor.record, { ...(editor.kind === 'RESPONSE_PREFERENCE' ? {} : { statement }), destination })).record : await api.create(destination, { kind: editor.kind, title, statement });
      setEditor(undefined); setDetail(value); setView(destination.view); setWorkspace(destination.workspace_id ?? ''); setRevision(value => value + 1);
      onNotify(editor.record ? '记忆已保存' : '已新增记忆', '', 'success');
    } catch (error) { onNotify('记忆未能保存', error instanceof MemoryApiError ? errorText(error) : '提交结果暂时无法确认。请刷新核对后再决定；草稿仍保留。', 'warning'); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!detail || busy) return;
    setBusy(true);
    try { await api.delete(selection, detail); setDetail(undefined); setConfirmDelete(false); setRevision(value => value + 1); onNotify('已删除记忆', '', 'success'); }
    catch (error) { onNotify('记忆未能删除', errorText(error), 'warning'); }
    finally { setBusy(false); }
  }
  const canUseScope = view === 'global' || Boolean(workspace);
  const scopeLabel = view === 'global' ? '全局记忆' : projects.find(item => item.workspace_id === workspace)?.label ?? '项目记忆';
  const closeDetail = () => { detailRequest.current += 1; setDetail(undefined); setEditor(undefined); setConfirmDelete(false); };
  return <div className={`memory-layout${detail || editor ? ' has-detail' : ''}`}><div className="memory-main">
    <div className="memory-scope-header"><div className="memory-tabs sliding-tabs" data-active={view} role="tablist" aria-label="记忆范围">{(['global', 'project'] as const).map(value => <button key={value} role="tab" aria-selected={view === value} disabled={busy} onClick={() => setView(value)}>{value === 'global' ? <Globe2 size={15} /> : <FolderOpen size={15} />}{value === 'global' ? '全局记忆' : '项目记忆'}</button>)}</div><div className="memory-project-slot">{view === 'project' && <label className="memory-project"><FolderOpen size={16} /><select aria-label="选择项目" value={workspace} disabled={busy} onChange={event => setWorkspace(event.target.value)}><option value="">选择项目</option>{projects.map(project => <option key={project.workspace_id} value={project.workspace_id}>{project.label} · {project.root}</option>)}</select><ChevronDown size={14} /></label>}</div></div>
    {view === 'project' && projectCursor && <button className="memory-more" disabled={busy} onClick={async () => { setBusy(true); try { const page = await api.projects(projectCursor); setProjects(old => [...old, ...page.items]); setProjectCursor(page.next_cursor); } catch (error) { onNotify('项目列表读取失败', errorText(error), 'warning'); } finally { setBusy(false); } }}>更多项目</button>}
    <div className="memory-collection"><div className="memory-toolbar"><div className="memory-controls"><label className="memory-search"><Search size={16} /><input aria-label="搜索记忆" placeholder="搜索标题或正文…" value={search} disabled={busy} onChange={event => setSearch(event.target.value)} /></label><button className="memory-edit memory-create" disabled={busy || !canUseScope} onClick={beginCreate}><Plus size={15} />新增记忆</button></div><div className="memory-filters sliding-tabs" data-kind={kind} role="group" aria-label="记忆类别">{[['', '全部'], ...Object.entries(memoryKindLabels)].map(([value, label]) => <button key={value} disabled={busy} aria-pressed={kind === value} onClick={() => setKind(value)}>{label}</button>)}</div></div>
      {loading ? <div className="memory-loading" role="status"><Brain size={25} /><p>正在读取记忆…</p></div> : !items.length ? <div className="memory-empty"><span className="memory-empty-icon"><Brain size={25} /></span><h2>{failed ? '暂时无法读取记忆' : !canUseScope ? '选择一个项目' : search || kind ? '没有找到匹配的记忆' : '这里还没有记忆'}</h2><p>{failed ? '重新读取后即可继续管理。' : !canUseScope ? '请选择你要查看或保存记忆的项目。' : '可以新增记忆，也可以在对话中告诉Pulsara值得记住的内容。'}</p>{failed && <button className="memory-more" onClick={() => setRevision(value => value + 1)}>重新读取</button>}</div> : <div className="memory-list">{items.map(record => { const Icon = kindIcons[record.kind]; return <button className={`memory-row${detail?.target.memory_id === record.target.memory_id ? ' is-selected' : ''}`} key={record.target.memory_id} disabled={busy} onClick={() => void open(record)}><span className="memory-row-icon"><Icon size={17} /></span><div className="memory-row-copy"><div className="memory-row-heading"><h3 className="memory-title">{record.title}</h3></div>{record.statement && <p className="memory-list-excerpt">{record.statement}</p>}<span className="memory-metadata"><span className="memory-kind">{memoryKindLabels[record.kind]}</span><span>{scopeLabel}</span></span></div><ChevronRight className="memory-row-chevron" size={16} /></button>; })}</div>}
    </div>{cursor && <button className="memory-more" disabled={busy} onClick={async () => { const captured = key; setBusy(true); try { const page = await api.catalog(selection, { kind, search, cursor }); if (currentKey.current === captured) { setItems(old => [...old, ...page.items]); setCursor(page.next_cursor); } } catch (error) { onNotify('无法加载更多记忆', errorText(error), 'warning'); } finally { setBusy(false); } }}>加载更多记忆</button>}
  </div>{(detail || editor) && <aside className="memory-detail" aria-label={editor && !editor.record ? '新增记忆' : '记忆详情'}><header><span className="memory-detail-heading"><Brain size={17} /><h2>{editor && !editor.record ? '新增记忆' : '记忆详情'}</h2></span><button aria-label="关闭记忆详情" disabled={busy} onClick={closeDetail}><X size={17} /></button></header><div className="memory-detail-body">
    {editor ? <form className="memory-editor" onSubmit={event => { event.preventDefault(); void save(); }}>
      {editor.record ? <><h3 className="memory-title">{editor.title}</h3><span className="memory-kind">{memoryKindLabels[editor.kind]}</span><p>标题和类别在创建后固定。</p></> : <><label htmlFor="memory-create-kind">类别</label><select id="memory-create-kind" value={editor.kind} disabled={busy} onChange={event => setEditor({ ...editor, kind: event.target.value as MemoryKind })}>{Object.entries(memoryKindLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><label htmlFor="memory-create-title">{editor.kind === 'RESPONSE_PREFERENCE' ? '完整回答偏好' : '标题'}</label><input id="memory-create-title" value={editor.title} disabled={busy} onChange={event => setEditor({ ...editor, title: event.target.value })} /><p>{Array.from(normalize(editor.title)).length} / 128 字符</p></>}
      {editor.kind !== 'RESPONSE_PREFERENCE' && <><label htmlFor="memory-statement-edit">正文</label><textarea id="memory-statement-edit" value={editor.statement} disabled={busy} onChange={event => setEditor({ ...editor, statement: event.target.value })} /><p>{Array.from(normalize(editor.statement)).length} / 8000 字符</p></>}
      <label htmlFor="memory-save-scope">保存范围</label><select id="memory-save-scope" value={editor.scope} disabled={busy} onChange={event => setEditor({ ...editor, scope: event.target.value })}><option value="global">全局记忆</option>{projects.map(project => <option key={project.workspace_id} value={project.workspace_id}>{project.label} · {project.root}</option>)}</select>
      <div className="memory-editor-actions"><button type="button" disabled={busy} onClick={() => setEditor(undefined)}>取消</button><button type="submit" disabled={busy}>{busy ? '正在保存…' : '保存记忆'}</button></div>
    </form> : detail && <><h3 className="memory-title memory-detail-title">{detail.title}</h3><span className="memory-metadata"><span className="memory-kind">{memoryKindLabels[detail.kind]}</span><span>{scopeLabel}</span></span>{detail.statement && <p className="memory-statement">{detail.statement}</p>}{confirmDelete ? <div className="memory-delete-confirmation" role="group" aria-label="确认删除记忆"><p>确认删除“{detail.title}”？</p><div className="memory-editor-actions"><button disabled={busy} onClick={() => setConfirmDelete(false)}>取消</button><button className="memory-delete" disabled={busy} onClick={() => void remove()}>{busy ? '正在删除…' : '确认删除'}</button></div></div> : <div className="memory-detail-actions"><button className="memory-edit" disabled={busy} onClick={() => beginEdit(detail)}><Pencil size={14} />编辑记忆</button><button className="memory-delete" disabled={busy} onClick={() => setConfirmDelete(true)}><Trash2 size={14} />删除记忆</button></div>}</>}
  </div></aside>}</div>;
}
