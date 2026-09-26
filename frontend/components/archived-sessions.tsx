import { Archive, ArchiveRestore, RefreshCw, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { RuntimeAdapter } from '../lib/runtime-adapter';
import type { SessionSummary } from '../lib/pulsara-types';

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
  const generation = useRef(0);
  const load = useCallback(async () => {
    const own = ++generation.current;
    try {
      const result = await adapter.listArchivedSessions();
      if (own === generation.current) setList({ adapter, revision, items: result, loading: false });
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
    {loading ? <p className="archived-sessions-empty">正在读取…</p> : items.length === 0 ? <p className="archived-sessions-empty">还没有已归档会话</p> : <ul>
      {items.map(session => <li key={session.id}><div><strong>{session.title}</strong><p>{session.workspace?.path}</p><small>归档于 {session.updatedAt}</small></div><div className="archived-session-actions">
        <button disabled={Boolean(busy)} onClick={() => void restore(session)}><ArchiveRestore size={14} />{busy === session.id ? '正在恢复…' : '取消归档'}</button>
        <button className="subtle-danger" disabled={Boolean(busy)} onClick={() => onDelete(session)}><Trash2 size={14} />永久删除…</button>
      </div></li>)}
    </ul>}
  </section>;
}
