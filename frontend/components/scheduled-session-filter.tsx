import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronRight, ListFilter, MessageSquare, Search } from 'lucide-react';
import { LocalScheduledTasksApi, type ScheduledSession } from '../lib/scheduled-tasks-api';

export function ScheduledSessionFilter({ api, selected, onSelect, disabled, refresh }: {
  api: LocalScheduledTasksApi;
  selected: ScheduledSession | null;
  onSelect: (session: ScheduledSession | null) => void;
  disabled: boolean;
  refresh: number;
}) {
  const [query, setQuery] = useState('');
  const [items, setItems] = useState<ScheduledSession[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const revision = useRef(0);
  const invalidateRequests = useCallback(() => { revision.current++; }, []);

  useEffect(() => {
    const captured = ++revision.current;
    const controller = new AbortController();
    setLoading(true); setError(''); setItems([]); setCursor(null);
    const timer = setTimeout(() => {
      void api.listSessions(query, undefined, controller.signal).then(page => {
        if (revision.current !== captured) return;
        setItems(page.sessions); setCursor(page.next_cursor);
      }).catch(e => {
        if (!controller.signal.aborted && revision.current === captured) setError((e as Error).message);
      }).finally(() => { if (revision.current === captured) setLoading(false); });
    }, query ? 180 : 0);
    return () => { clearTimeout(timer); controller.abort(); invalidateRequests(); };
  }, [api, query, refresh, retry, invalidateRequests]);

  const more = async () => {
    if (!cursor || loading) return;
    const captured = ++revision.current;
    setLoading(true); setError('');
    try {
      const page = await api.listSessions(query, cursor);
      if (revision.current !== captured) return;
      setItems(old => [...old.filter(item => !page.sessions.some(next => next.id === item.id)), ...page.sessions]);
      setCursor(page.next_cursor);
    } catch (e) { if (revision.current === captured) setError((e as Error).message); }
    finally { if (revision.current === captured) setLoading(false); }
  };

  return <nav className="scheduled-sessions" aria-label="按会话筛选">
    <label className="memory-search scheduled-session-search"><Search size={16} aria-hidden="true" />
      <input type="search" aria-label="搜索任务会话" placeholder="搜索会话" value={query} disabled={disabled}
        onChange={e => { revision.current++; setQuery(e.target.value); }} />
    </label>
    <div className="scheduled-session-options" aria-busy={loading}>
      <button className="scheduled-session-option" aria-pressed={!selected} disabled={disabled} onClick={() => onSelect(null)}><ListFilter size={15} aria-hidden="true" /><span>全部会话</span><ChevronRight className="scheduled-session-chevron" size={12} aria-hidden="true" /></button>
      {items.map(session => <button key={session.id} className="scheduled-session-option" title={session.title}
        aria-pressed={selected?.id === session.id} disabled={disabled} onClick={() => onSelect(session)}><MessageSquare size={15} aria-hidden="true" /><span>{session.title}</span><ChevronRight className="scheduled-session-chevron" size={12} aria-hidden="true" /></button>)}
    </div>
    {loading && !items.length && <p className="scheduled-session-message" role="status">加载中…</p>}
    {!loading && !error && !items.length && <p className="scheduled-session-message">{query ? '没有匹配的会话' : '暂无会话'}</p>}
    {error && <div className="scheduled-session-message" role="alert">{error}<button className="scheduled-session-option" onClick={() => setRetry(value => value + 1)}>重试</button></div>}
    {cursor && <button className="scheduled-session-option" disabled={disabled || loading} onClick={() => void more()}>加载更多会话</button>}
  </nav>;
}
