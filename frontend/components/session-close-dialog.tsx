import { useEffect, useId, useRef } from 'react';

interface SessionCloseDialogProps {
  title: string;
  hasTerminalProcesses: boolean;
  stopping: boolean;
  error?: string;
  onKeep: () => void;
  onStop: () => void;
}

export function SessionCloseDialog({
  title, hasTerminalProcesses, stopping, error, onKeep, onStop,
}: SessionCloseDialogProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element?.showModal();
    return () => {
      element?.close?.();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);
  return (
    <dialog ref={dialog} className="session-dialog session-close-dialog"
      aria-labelledby={titleId} aria-describedby={descriptionId} onCancel={event => {
        event.preventDefault();
        event.stopPropagation();
        if (!stopping) onKeep();
      }}>
      <header><h2 id={titleId}>关闭「{title}」？</h2></header>
      <div className="session-dialog-body">
        <p id={descriptionId}>这个会话还有任务未完成。{hasTerminalProcesses && '停止任务后，后台终端会继续运行。'}</p>
        {error && <p role="alert" className="session-dialog-alert">{error}</p>}
        {stopping && <p role="status">正在等待会话任务停止…</p>}
      </div>
      <footer>
        <button type="button" autoFocus disabled={stopping} onClick={onKeep}>{error ? '保留会话' : '继续运行'}</button>
        <button type="button" className="is-danger" disabled={stopping || Boolean(error)} onClick={onStop}>停止并关闭</button>
      </footer>
    </dialog>
  );
}
