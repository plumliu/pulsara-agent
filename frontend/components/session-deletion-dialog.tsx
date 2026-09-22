import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { Check, CirclePause, MessageSquare, Trash2, X } from 'lucide-react';
import type { SessionSummary } from '../lib/pulsara-types';

export function SessionDeletionDialog({ session, busy, error, onConfirm, onClose }: {
  session: SessionSummary; busy: boolean; error?: string;
  onConfirm: () => void; onClose: () => void;
}) {
  const dialog = useRef<HTMLElement>(null);
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
    <section ref={dialog} className="memory-delete-dialog session-delete-dialog" role="dialog" aria-modal="true"
      aria-labelledby="session-delete-title" aria-describedby="session-delete-description" tabIndex={-1} onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); if (!busy) onClose(); }
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
        <span className="session-delete-icon"><Trash2 size={20} aria-hidden="true" /></span>
        <div className="session-delete-heading">
          <h2 id="session-delete-title">删除这条会话？</h2>
          <p id="session-delete-description">会话及其记录将永久删除，无法撤销。</p>
        </div>
        <button aria-label="关闭删除确认" disabled={busy} onClick={onClose}><X size={17} /></button>
      </header>
      <div className="memory-delete-body session-delete-body">
        <div className="session-delete-target">
          <MessageSquare size={17} aria-hidden="true" />
          <div><strong>{session.title}</strong>{session.workspace?.path && <span className="session-delete-path">{session.workspace.path}</span>}</div>
        </div>
        <div className="session-delete-preserved">
          <p>以下内容仍会保留</p>
          <ul aria-label="仍会保留的内容">
            {['已保存的记忆', '其他分支会话', '工作目录'].map(label => <li key={label}><Check size={14} aria-hidden="true" />{label}</li>)}
          </ul>
        </div>
        {session.live && <p className="session-delete-runtime"><CirclePause size={15} aria-hidden="true" />将先停止此会话及其后台任务。</p>}
        {error && <p className="memory-delete-alert" role="alert">{error}</p>}
      </div>
      <footer><button disabled={busy} onClick={onClose}>取消</button><button className="is-danger" disabled={busy} onClick={onConfirm}>
        {busy ? session.live ? '正在停止并删除…' : '正在删除…' : error ? '重试确认' : session.live ? '停止并删除' : '永久删除'}
      </button></footer>
    </section>
  </div>, document.body);
}
