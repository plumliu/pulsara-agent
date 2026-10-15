import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArchiveRestore, Check, CheckCircle2, ChevronLeft, ChevronRight, CircleHelp, CircleMinus, CirclePause, Clock3, FolderOpen, ListChecks, LoaderCircle, MessageSquare, RotateCcw, TriangleAlert, Trash2, X } from 'lucide-react';
import type { ArchivedSessionDeletionItem, SessionSummary } from '../lib/pulsara-types';
import { usePromptHover } from '../lib/prompt-hover';

const deletionStates = {
  pending: { label: '等待处理', tone: 'neutral', icon: Clock3 },
  running: { label: '正在处理', tone: 'blue', icon: LoaderCircle },
  DELETED: { label: '已删除', tone: 'green', icon: CheckCircle2 },
  ABSENT: { label: '已不存在', tone: 'neutral', icon: CircleMinus },
  NOT_ARCHIVED: { label: '已跳过', tone: 'blue', icon: ArchiveRestore },
  failed: { label: '未完成', tone: 'amber', icon: TriangleAlert },
  unknown: { label: '待确认', tone: 'violet', icon: CircleHelp },
} as const;
const settledStates = ['DELETED', 'ABSENT', 'NOT_ARCHIVED', 'failed', 'unknown'] as const;

function ArchiveProgressSegment({ status, count }: { status: typeof settledStates[number]; count: number }) {
  const { trigger, details, position, show, hide, keep } = usePromptHover(120);
  const id = useId();
  const text = `${deletionStates[status].label} ${count}`;
  return <>
    <button ref={trigger} type="button" className="archive-delete-progress-segment" data-tone={deletionStates[status].tone}
      style={{ flexGrow: count }} aria-label={text} aria-describedby={position ? id : undefined}
      onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide} onClick={show} />
    {position && createPortal(<span ref={details} id={id} role="tooltip" className="archive-delete-progress-tooltip"
      data-tone={deletionStates[status].tone}
      style={{ left: position.left, top: position.top, translate: position.above ? '0 24px' : '0 4px' }}
      onMouseEnter={keep} onMouseLeave={hide}><span className="archive-delete-tooltip-swatch" aria-hidden="true" /><span>{text}</span></span>, document.body)}
  </>;
}

export function SessionDeletionDialog({ session, sessions, results, busy, error, onConfirm, onClose, onRetry }: ({
  session: SessionSummary; sessions?: never; results?: never; onRetry?: never;
} | {
  session?: never; sessions: readonly SessionSummary[]; results?: readonly ArchivedSessionDeletionItem[]; onRetry?: () => void;
}) & {
  busy: boolean; error?: string;
  onConfirm: () => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLElement>(null);
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil((results?.length ?? 0) / 5));
  const currentPage = Math.min(page, pageCount - 1);
  const canClose = Boolean(results) || !busy;
  const counts = { pending: 0, running: 0, DELETED: 0, ABSENT: 0, NOT_ARCHIVED: 0, failed: 0, unknown: 0 };
  for (const item of results ?? []) counts[item.status] += 1;
  const processed = settledStates.reduce((total, status) => total + counts[status], 0);
  const incomplete = counts.failed + counts.unknown;
  const HeaderIcon = results ? ListChecks : Trash2;
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const app = document.querySelector<HTMLElement>('main.pulsara-shell');
    const inert = app?.inert ?? false;
    if (app) app.inert = true;
    dialog.current?.focus();
    return () => {
      if (app) app.inert = inert;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);
  return createPortal(<div className="memory-delete-overlay">
    <section ref={dialog} className={`memory-delete-dialog session-delete-dialog${results ? ' has-archive-results' : ''}`} role="dialog" aria-modal="true"
      aria-labelledby="session-delete-title" aria-describedby="session-delete-description" tabIndex={-1} onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); if (canClose) onClose(); }
        if (event.key !== 'Tab') return;
        const buttons = [...(dialog.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])];
        const first = buttons[0]; const last = buttons.at(-1);
        if (!first || !last) { event.preventDefault(); return; }
        if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {
          event.preventDefault(); last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) {
          event.preventDefault(); first.focus();
        }
      }}>
      <header>
        <span className="session-delete-icon"><HeaderIcon size={20} aria-hidden="true" /></span>
        <div className="session-delete-heading">
          <h2 id="session-delete-title">{results ? '归档会话删除结果' : sessions ? `永久删除本次列表中的 ${sessions.length} 条已归档会话？` : '删除这条会话？'}</h2>
          <p id="session-delete-description">{results ? '逐条查看操作结果；关闭此窗口不会停止正在处理的会话。' : sessions ? '无法撤销。执行时已取消归档的会话将跳过。' : '会话、记录及绑定的定时任务将永久删除，无法撤销。'}</p>
        </div>
        <button aria-label={results ? '关闭删除结果' : '关闭删除确认'} disabled={!canClose} onClick={onClose}><X size={17} /></button>
      </header>
      <div className="memory-delete-body session-delete-body">
        {session && <div className="session-delete-target">
          <MessageSquare size={17} aria-hidden="true" />
          <div><strong>{session.title}</strong>{session.workspace?.path && <span className="session-delete-path">{session.workspace.path}</span>}</div>
        </div>}
        {results && <>
          <section className="archive-delete-overview" aria-label="处理进度">
            <div className="archive-delete-progress-heading">
              <div><strong>{busy ? '正在处理归档会话' : '本次处理已结束'}</strong></div>
              <p aria-live="polite"><span className="archive-delete-processed-label">已处理</span><strong>{processed}</strong><span>/ {results.length}</span></p>
            </div>
            <div className="archive-delete-progress-track">
              <span className="archive-delete-progress-accessible" role="progressbar" aria-label="归档会话处理进度" aria-valuemin={0} aria-valuemax={results.length} aria-valuenow={processed} />
              <div className="archive-delete-progress-fill" style={{ width: `${results.length ? processed / results.length * 100 : 0}%` }}>
                {settledStates.filter(status => counts[status] > 0).map(status => <ArchiveProgressSegment key={status} status={status} count={counts[status]} />)}
              </div>
            </div>
          </section>
          <section className="archive-delete-detail" aria-label="会话明细">
            <div className="archive-delete-list-heading"><h3>会话明细</h3><span>共 {results.length} 项</span></div>
            <ul className="archived-delete-results" aria-label="归档删除结果">
              {results.slice(currentPage * 5, currentPage * 5 + 5).map(item => {
                const state = deletionStates[item.status];
                const StatusIcon = state.icon;
                const workspace = item.session.workspace;
                return <li key={item.session.id} data-tone={state.tone} data-attention={item.status === 'failed' || item.status === 'unknown' || Boolean(item.detail) || undefined}>
                  <div className="archive-delete-row-heading"><strong>{item.session.title}</strong><span className="archive-delete-status"><StatusIcon size={12} aria-hidden="true" className={item.status === 'running' ? 'archive-delete-spinner' : undefined} />{state.label}</span></div>
                  {workspace?.path && <p className="archive-delete-directory" title={workspace.path}><FolderOpen size={12} aria-hidden="true" /><span>{workspace.name || workspace.path.split(/[\\/]/).filter(Boolean).at(-1) || workspace.path}</span></p>}
                  {item.detail ? <p className="archive-delete-note" role="alert"><TriangleAlert size={13} aria-hidden="true" /><span>{item.detail}</span></p> : item.status === 'NOT_ARCHIVED' && <p className="archive-delete-note is-context">已取消归档，保留此会话。</p>}
                </li>;
              })}
            </ul>
            {pageCount > 1 && <nav className="archive-delete-pagination" aria-label="归档删除结果分页"><span>{currentPage * 5 + 1}–{Math.min((currentPage + 1) * 5, results.length)} / {results.length}</span><div><button disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}><ChevronLeft size={14} aria-hidden="true" />上一页</button><span>{currentPage + 1} / {pageCount}</span><button disabled={currentPage === pageCount - 1} onClick={() => setPage(currentPage + 1)}>下一页<ChevronRight size={14} aria-hidden="true" /></button></div></nav>}
          </section>
        </>}
        {!results && <div className="session-delete-preserved">
          <p>以下内容仍会保留</p>
          <ul aria-label="仍会保留的内容">
            {['已保存的记忆', '工作目录'].map(label => <li key={label}><Check size={14} aria-hidden="true" />{label}</li>)}
          </ul>
        </div>}
        {session?.live && <p className="session-delete-runtime"><CirclePause size={15} aria-hidden="true" />将先停止此会话及其后台任务。</p>}
        {error && <p className="memory-delete-alert" role="alert">{error}</p>}
      </div>
      <footer>{results && <span className="archive-delete-footer-note">{incomplete > 0 ? <><TriangleAlert size={13} aria-hidden="true" />{incomplete} 项需要处理</> : busy ? '正在处理，可稍后查看结果' : '本次结果已全部记录'}</span>}<button disabled={!canClose} onClick={onClose}>{results ? '关闭' : '取消'}</button>
        {results ? onRetry && <button className="archive-delete-retry" disabled={busy || incomplete === 0} onClick={onRetry}><RotateCcw size={14} aria-hidden="true" />重试未完成项…</button> : <button className="is-danger" disabled={busy} onClick={onConfirm}>
          {busy ? session?.live ? '正在停止并删除…' : '正在删除…' : error ? '重试确认' : session?.live ? '停止并删除' : '永久删除'}
        </button>}
      </footer>
    </section>
  </div>, document.body);
}
