import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Archive, Check, ChevronDown, LoaderCircle, Search, X } from 'lucide-react';
import type { RuntimeAdapter, SessionSearchItem, SessionSearchPage } from '../lib/runtime-adapter';
import type { SessionSummary } from '../lib/pulsara-types';

function searchPattern(query: string) {
  const words = [...new Set(query.trim().split(/\s+/).filter(Boolean))];
  if (!words.length) return null;
  const escaped = words.sort((a, b) => b.length - a.length).map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  return new RegExp(`(${escaped.join('|')})`, 'giu');
}

function Highlight({ text, query }: { text: string; query: string }) {
  const pattern = searchPattern(query);
  if (!pattern) return <>{text}</>;
  return <>{text.split(pattern).map((part, index) => index % 2 ? <mark key={index}>{part}</mark> : part)}</>;
}

function focusedSnippet(text: string, query: string) {
  const pattern = searchPattern(query);
  const normalized = text.replace(/\s+/gu, ' ');
  const hit = pattern ? normalized.search(pattern) : -1;
  if (hit < 0) return normalized;
  const before = Array.from(normalized.slice(0, hit));
  // A short leading context keeps the hit visible in the compact one-line row.
  return (before.length > 12 ? '…' : '') + before.slice(-12).join('') + normalized.slice(hit);
}

const scopeOptions = [
  { value: 'ALL', label: '全部' },
  { value: 'OPEN', label: '活跃会话' },
  { value: 'ARCHIVED', label: '已归档' },
] as const;
type SearchScope = typeof scopeOptions[number]['value'];

function SessionScopePicker({ value, disabled, onChange }: {
  value: SearchScope; disabled: boolean; onChange: (value: SearchScope) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  useEffect(() => {
    if (!open) return;
    root.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus();
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  const close = () => { setOpen(false); trigger.current?.focus(); };
  return <div ref={root} className="popover-anchor session-search-scope"
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}
    onKeyDown={event => {
      if (event.nativeEvent.isComposing) return;
      if (open && event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
      else if (open && event.key === 'Tab') { event.stopPropagation(); close(); }
      else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault(); event.stopPropagation();
        if (!open) { setOpen(true); return; }
        const options = [...(root.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]') ?? [])];
        const current = options.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
          : (current + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
        options[next]?.focus();
      }
    }}>
    <button ref={trigger} type="button" className={`mode-chip${open ? ' is-active' : ''}`}
      aria-label="会话搜索范围" aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined}
      disabled={disabled} onClick={() => setOpen(current => !current)}>
      <span>{scopeOptions.find(option => option.value === value)?.label}</span><ChevronDown size={10} />
    </button>
    {open && <div id={menuId} className="menu-popover session-search-scope-menu" role="menu" aria-label="会话搜索范围">
      {scopeOptions.map(option => <button key={option.value} type="button" role="menuitemradio"
        aria-checked={value === option.value} className={value === option.value ? 'is-selected' : ''}
        onClick={() => { onChange(option.value); close(); }}>
        <span>{option.label}</span>{value === option.value && <Check size={13} />}
      </button>)}
    </div>}
  </div>;
}

export function SessionSearchDialog({ adapter, available, onClose, onOpen }: {
  adapter: RuntimeAdapter; available: boolean; onClose: () => void;
  onOpen: (session: SessionSummary) => void;
}) {
  const [query, setQuery] = useState('');
  const [lifecycle, setLifecycle] = useState<'ALL' | 'OPEN' | 'ARCHIVED'>('ALL');
  const [retry, setRetry] = useState(0);
  const key = JSON.stringify([query, lifecycle, retry]);
  const [page, setPage] = useState<(SessionSearchPage & { key: string; error?: string; loadingMore?: boolean })>();
  const [selected, setSelected] = useState(-1);
  const [restore, setRestore] = useState<SessionSummary>();
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState('');
  const operation = useRef(false);
  const mounted = useRef(true);
  const composing = useRef(false);
  const request = useRef<AbortController | null>(null);
  const dialog = useRef<HTMLElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const results = useRef<HTMLDivElement>(null);
  const current = page?.key === key ? page : undefined;
  const recent = !query.trim();
  const items = recent ? (current?.items ?? []).slice(0, 5) : current?.items ?? [];
  const loading = available && !current;

  useEffect(() => {
    mounted.current = true;
    const previous = document.activeElement as HTMLElement | null;
    const app = document.querySelector<HTMLElement>('main.pulsara-shell');
    const inert = app?.inert ?? false;
    if (app) app.inert = true;
    input.current?.focus();
    return () => { mounted.current = false; if (app) app.inert = inert; if (previous?.isConnected) previous.focus(); };
  }, []);

  useEffect(() => {
    if (!available) return;
    const controller = new AbortController(); request.current = controller;
    const timer = window.setTimeout(() => {
      void adapter.searchSessions(query, lifecycle, undefined, controller.signal).then(result => {
        if (!controller.signal.aborted) setPage({ ...result, key });
      }).catch(error => {
        if (!controller.signal.aborted) setPage({ key, items: [], nextCursor: null,
          error: error instanceof Error ? error.message : '暂时无法搜索，请重试。' });
      });
    }, query.trim() ? 200 : 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [adapter, available, key, query, lifecycle]);

  useEffect(() => {
    results.current?.querySelector<HTMLElement>(`[data-result-index="${selected}"]`)?.scrollIntoView?.({ block: 'nearest' });
  }, [selected]);

  const loadMore = async () => {
    const signal = request.current?.signal;
    if (!current?.nextCursor || current.loadingMore || !signal || signal.aborted) return;
    setPage({ ...current, loadingMore: true, error: undefined });
    try {
      const next = await adapter.searchSessions(query, lifecycle, current.nextCursor, signal);
      if (!signal.aborted) setPage({ key, items: [...current.items, ...next.items.filter(item => !current.items.some(prior => prior.session.id === item.session.id))], nextCursor: next.nextCursor });
    } catch (error) {
      if (!signal.aborted) setPage({ ...current, error: error instanceof Error ? error.message : '加载失败，请重试。' });
    }
  };
  const open = async (session: SessionSummary, explicitlyRestore = false) => {
    if (operation.current) return;
    if (session.lifecycle === 'ARCHIVED' && !explicitlyRestore) { setRestore(session); setOpenError(''); return; }
    operation.current = true; setOpening(true); setOpenError('');
    try {
      if (explicitlyRestore) {
        const result = await adapter.unarchiveSession(session.id);
        if (result.session_id !== session.id || result.status !== 'OPEN') throw new Error('尚未确认取消归档，请刷新查看。');
      }
      const fresh = await adapter.readSession(session.id);
      if (!fresh || fresh.lifecycle === 'ARCHIVED') throw new Error('这条会话已归档或已删除，请刷新搜索结果。');
      if (mounted.current) { onOpen(fresh); onClose(); }
    } catch (error) {
      if (mounted.current) setOpenError(error instanceof Error ? error.message : '无法打开会话，请重试。');
    } finally {
      operation.current = false;
      if (mounted.current) setOpening(false);
    }
  };
  const choose = (item: SessionSearchItem) => { void open(item.session); };
  return createPortal(<div className="session-search-overlay" onClick={event => { if (event.target === event.currentTarget && !operation.current) onClose(); }}>
    <section ref={dialog} className="session-search-dialog" role="dialog" aria-modal="true" aria-label="搜索会话" onKeyDown={event => {
      event.stopPropagation();
      if (event.nativeEvent.isComposing || composing.current || event.keyCode === 229) return;
      if (event.key === 'Escape' || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k')) {
        event.preventDefault(); if (!operation.current) onClose(); return;
      }
      if (!restore && event.target === input.current && ['ArrowDown', 'ArrowUp', 'Enter'].includes(event.key)) {
        event.preventDefault();
        if (event.key === 'Enter') { if (items[selected]) choose(items[selected]); }
        else setSelected(value => {
          if (!items.length) return -1;
          if (value < 0) return event.key === 'ArrowDown' ? 0 : items.length - 1;
          return Math.max(0, Math.min(items.length - 1, value + (event.key === 'ArrowDown' ? 1 : -1)));
        });
      }
      if (event.key === 'Tab') {
        const controls = [...(dialog.current?.querySelectorAll<HTMLElement>('input:not(:disabled),select:not(:disabled),button:not(:disabled)') ?? [])];
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }}>
      <header><Search size={18} /><input ref={input} placeholder="搜索会话…" aria-label="搜索关键词" value={query} disabled={opening}
        onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}
        onChange={event => { setQuery(event.target.value); setSelected(-1); setRestore(undefined); setOpenError(''); }} />
        <SessionScopePicker value={lifecycle} disabled={opening} onChange={value => {
          setLifecycle(value); setSelected(-1); setRestore(undefined); setOpenError('');
        }} />
        <button type="button" aria-label="关闭会话搜索" disabled={opening} onClick={onClose}><X size={17} /></button></header>
      {!available ? <p className="session-search-state">连接本地服务并配置数据库后即可搜索会话。</p>
      : restore ? <div className="session-search-restore"><Archive size={20} /><strong>{restore.title}</strong><p>此会话已归档，取消归档后才能打开。</p>
          <div><button disabled={opening} onClick={() => { setRestore(undefined); setOpenError(''); }}>返回结果</button>
          <button disabled={opening} onClick={() => void open(restore, true)}>{opening ? '正在恢复…' : '取消归档并打开'}</button></div></div>
      : <div className="session-search-results" ref={results} aria-label="会话搜索结果" aria-busy={loading || opening}>
        {recent && <p className="session-search-recent">最近会话</p>}
        {loading && <p className="session-search-state" role="status"><LoaderCircle size={15} className="is-spinning" />正在搜索…</p>}
        {!loading && !current?.error && items.length === 0 && <p className="session-search-state">{query.trim() ? '没有找到相关会话' : '还没有会话'}</p>}
        {items.map((item, index) => <button type="button" key={item.session.id} data-result-index={index} disabled={opening}
          className={`session-search-result${index === selected ? ' is-selected' : ''}`} onFocus={() => setSelected(index)} onClick={() => choose(item)}>
          <div className="session-search-result-heading">
            {item.session.lifecycle === 'ARCHIVED' && <span className="session-search-archive-badge">已归档</span>}
            <strong><Highlight text={item.session.title} query={query} /></strong>
            <small><span title={item.session.workspace?.path}>{item.session.workspace?.kind === 'project' ? item.session.workspace.name : '快速开始'}</span><span>{item.session.updatedAt}</span></small>
          </div>
          {item.snippet && item.matchKind !== 'title' && <p><Highlight text={focusedSnippet(item.snippet, query)} query={query} /></p>}
        </button>)}
        {current?.error && <div className="session-search-state" role="alert">{current.error}<button onClick={() => current.nextCursor ? void loadMore() : setRetry(value => value + 1)}>重试</button></div>}
        {!recent && current?.nextCursor && !current.error && <button className="session-search-more" disabled={current.loadingMore || opening} onClick={() => void loadMore()}>{current.loadingMore ? '正在加载…' : '加载更多'}</button>}
      </div>}
      {openError && <p className="session-search-state" role="alert">{openError}</p>}
    </section>
  </div>, document.body);
}
