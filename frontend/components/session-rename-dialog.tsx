import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Pencil, X } from 'lucide-react';
import type { SessionSummary } from '../lib/pulsara-types';

export function SessionRenameDialog({ session, onSave, onClose }: {
  session: SessionSummary;
  onSave: (title: string) => Promise<void>;
  onClose: () => void;
}) {
  const [title, setTitle] = useState(session.title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submitting = useRef(false);
  const composing = useRef(false);
  const dialog = useRef<HTMLElement>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const app = document.querySelector<HTMLElement>('main.pulsara-shell');
    const inert = app?.inert ?? false;
    if (app) app.inert = true;
    input.current?.focus(); input.current?.select();
    return () => {
      if (app) app.inert = inert;
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  const save = async () => {
    if (submitting.current || composing.current) return;
    const value = title.trim();
    if (!value || /[\p{Cc}\u2028\u2029]/u.test(value)) {
      setError('标题不能为空，且不能包含换行或控制字符。'); return;
    }
    submitting.current = true; setBusy(true); setError('');
    try { await onSave(value); onClose(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '未能确认保存结果，请刷新确认或重试。'); }
    finally { submitting.current = false; setBusy(false); }
  };
  return createPortal(<div className="memory-delete-overlay">
    <section ref={dialog} className="memory-delete-dialog session-rename-dialog" role="dialog" aria-modal="true"
      aria-labelledby="session-rename-title" onKeyDown={event => {
        event.stopPropagation();
        if (event.key === 'Escape' && !event.nativeEvent.isComposing && !composing.current) {
          event.preventDefault(); if (!submitting.current) onClose();
        }
        if (event.key !== 'Tab') return;
        const controls = [...(dialog.current?.querySelectorAll<HTMLElement>('input:not(:disabled),button:not(:disabled)') ?? [])];
        const first = controls[0]; const last = controls.at(-1);
        if (!first || !last) { event.preventDefault(); return; }
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }}>
      <header><Pencil size={19} /><h2 id="session-rename-title">重命名会话</h2>
        <button type="button" aria-label="关闭重命名" disabled={busy} onClick={onClose}><X size={17} /></button>
      </header>
      <form onSubmit={event => { event.preventDefault(); void save(); }}>
        <div className="memory-delete-body">
          <input ref={input} id="session-rename-input" aria-label="会话标题" value={title} disabled={busy} autoComplete="off"
            onChange={event => { setTitle(event.target.value); setError(''); }}
            onCompositionStart={() => { composing.current = true; }}
            onCompositionEnd={() => { composing.current = false; }}
            onKeyDown={event => {
              if (event.key === 'Enter' && (composing.current || event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault();
            }} />
          {error && <p className="memory-delete-alert" role="alert">{error}</p>}
        </div>
        <footer><button type="button" disabled={busy} onClick={onClose}>取消</button>
          <button type="submit" className="session-rename-save" disabled={busy || !title.trim()}>{busy ? '正在保存…' : '保存'}</button>
        </footer>
      </form>
    </section>
  </div>, document.body);
}
