import { Archive, ArchiveRestore, ChevronLeft, ChevronRight, RefreshCw, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { RuntimeAdapter } from '../lib/runtime-adapter';
import type { SessionSummary } from '../lib/pulsara-types';

// Match the model configuration list's display page size; all sessions remain reachable.
const pageSize = 5;

export function ArchivedSessions({ adapter, revision, onRestored, onDelete }: {
  adapter: RuntimeAdapter; revision: number;
  onRestored: () => Promise<void>; onDelete: (session: SessionSummary) => void;
}) {
  const [list, setList] = useState<{
    adapter: RuntimeAdapter; revision: number; items: SessionSummary[];
    loading: boolean; error?: string;
  }>({ adapter, revision, items: [], loading: true });
  // A changed source is loading immediately, without resetting state in an effect.
  const loading = list.adapter !== adapter || list.revision !== revision || list.loading;
  const items = list.items;
  const error = loading ? undefined : list.error;
  const [busy, setBusy] = useState<string>();
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const pageStart = currentPage * pageSize;
  const visibleItems = items.slice(pageStart, pageStart + pageSize);
  const generation = useRef(0);
  const load = useCallback(async () => {
    const own = ++generation.current;
    try {
      const result = await adapter.listArchivedSessions();
      if (own === generation.current) {
        setList({ adapter, revision, items: result, loading: false });
        setPage(current => Math.min(current, Math.max(0, Math.ceil(result.length / pageSize) - 1)));
      }
    } catch (e) {
      if (own === generation.current) setList(current => ({ ...current, adapter, revision,
        loading: false, error: e instanceof Error ? e.message : '无法读取已归档会话' }));
    }
  }, [adapter, revision]);
  useEffect(() => { void load(); return () => { generation.current += 1; }; }, [load]);
  const refresh = async () => {
    setList(current => ({ ...current, loading: true, error: undefined }));
    await load();
  };
  const restore = async (session: SessionSummary) => {
    if (busy) return;
    setBusy(session.id); setList(current => ({ ...current, error: undefined }));
    try {
      const result = await adapter.unarchiveSession(session.id);
      if (result.status !== 'OPEN' || result.session_id !== session.id) throw new Error('尚未确认取消归档，请刷新查看。');
      await refresh(); await onRestored();
    } catch (e) { setList(current => ({ ...current, error: e instanceof Error ? e.message : '尚未确认操作结果，请刷新查看。' })); }
    finally { setBusy(undefined); }
  };
  return <section className="settings-group archived-sessions">
    <header><Archive size={16} /><div><h2>已归档会话</h2><p>收起暂时不用的会话；取消归档后，可回到会话页继续。</p></div><button aria-label="刷新已归档会话" disabled={loading || Boolean(busy)} onClick={() => void refresh()}><RefreshCw size={14} /></button></header>
    {error && <p className="settings-alert" role="alert">{error}</p>}
    {loading ? <p className="archived-sessions-empty">正在读取…</p> : items.length === 0 ? <p className="archived-sessions-empty">还没有已归档会话</p> : <><ul>
      {visibleItems.map(session => <li key={session.id}><div><div className="archived-session-heading"><strong title={session.title}>{session.title}</strong><small>归档于 {session.updatedAt}</small></div><p title={session.workspace?.path}>{session.workspace?.path}</p></div><div className="archived-session-actions">
        <button disabled={Boolean(busy)} onClick={() => void restore(session)}><ArchiveRestore size={14} />{busy === session.id ? '正在恢复…' : '取消归档'}</button>
        <button className="subtle-danger" disabled={Boolean(busy)} onClick={() => onDelete(session)}><Trash2 size={14} />永久删除…</button>
      </div></li>)}
    </ul>{pageCount > 1 && <nav className="model-pagination" aria-label="已归档会话分页">
      <span aria-live="polite">{pageStart + 1}–{pageStart + visibleItems.length} / 共 {items.length} 项</span>
      <div><button disabled={currentPage === 0 || Boolean(busy)} onClick={() => setPage(currentPage - 1)}><ChevronLeft size={13} />上一页</button><span>{currentPage + 1} / {pageCount}</span><button disabled={currentPage === pageCount - 1 || Boolean(busy)} onClick={() => setPage(currentPage + 1)}>下一页<ChevronRight size={13} /></button></div>
    </nav>}</>}
  </section>;
}
