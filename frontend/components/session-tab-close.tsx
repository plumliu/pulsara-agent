import { autoUpdate, computePosition, offset, shift } from '@floating-ui/dom';
import { LoaderCircle, X } from 'lucide-react';
import { useEffect, useId, useLayoutEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import type { SessionTabCloseOutcome } from '../lib/runtime-adapter';

export interface SessionTabCloseRequest {
  confirmation?: Extract<SessionTabCloseOutcome, { status: 'confirmation_required' }>;
  busy: boolean;
  error?: string;
  onKeep: () => void;
  onConfirm: () => void;
}

export function SessionTabClose({ title, request, onClose, onReturnToForm }: {
  title: string;
  request?: SessionTabCloseRequest;
  onClose: () => void;
  onReturnToForm: () => void;
}) {
  const anchor = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const keep = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  const descriptionId = useId();
  const confirmation = request?.confirmation;
  const open = Boolean(confirmation);
  const busy = request?.busy ?? false;
  const onKeep = request?.onKeep;

  useLayoutEffect(() => {
    const trigger = anchor.current;
    const popup = menu.current;
    if (!open || !trigger || !popup) return;
    let position = 0;
    const stop = autoUpdate(trigger, popup, () => {
      const request = ++position;
      void computePosition(trigger, popup, {
        placement: 'bottom-end',
        strategy: 'fixed',
        middleware: [offset(6), shift({ padding: 8 })],
      }).then(({ x, y }) => {
        if (request !== position) return;
        Object.assign(popup.style, { left: `${x}px`, top: `${y}px` });
      });
    });
    return () => { position += 1; stop(); };
  }, [open]);

  useEffect(() => {
    if (!confirmation || busy || !onKeep) return;
    keep.current?.focus({ preventScroll: true });
    const pointerDown = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || menu.current?.contains(event.target)
        || anchor.current?.contains(event.target)) return;
      onKeep();
    };
    const keyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing) return;
      event.preventDefault();
      event.stopPropagation();
      anchor.current?.focus({ preventScroll: true });
      onKeep();
    };
    document.addEventListener('pointerdown', pointerDown, true);
    document.addEventListener('keydown', keyDown, true);
    return () => {
      document.removeEventListener('pointerdown', pointerDown, true);
      document.removeEventListener('keydown', keyDown, true);
    };
  }, [busy, confirmation, onKeep]);

  const hasForm = Boolean(confirmation?.formIds.length);
  const otherClients = confirmation?.otherClients ?? false;
  const returnToForm = confirmation?.visibleForm ?? false;
  const message = hasForm
    ? (otherClients
      ? '关闭此处标签会取消尚未完成的能力配置表单。另一处仍可查看会话。'
      : '关闭标签会取消尚未完成的能力配置表单。')
    : '此会话也在另一处打开。关闭此处标签后，另一处仍可查看会话。';
  return <>
    <button ref={anchor} type="button" className="session-canvas__tab-close"
      aria-label={`关闭会话 ${title}`} title="关闭标签" disabled={busy} aria-busy={busy}
      aria-haspopup={open ? 'dialog' : undefined} aria-expanded={open ? true : undefined}
      aria-controls={open ? menuId : undefined} onClick={onClose}>
      {busy ? <LoaderCircle className="is-spinning" size={13} /> : <X size={13} />}
    </button>
    {request && confirmation && createPortal(
      <div ref={menu} id={menuId} className="menu-popover session-tab-close-menu"
        role="dialog" aria-label={`关闭「${title}」？`} aria-describedby={descriptionId} aria-busy={busy}>
        <p id={descriptionId}>{message}</p>
        {request.error && <p role="alert">{request.error}</p>}
        <div className="session-tab-close-menu__actions">
          <button ref={keep} type="button" disabled={busy} onClick={() => {
            if (returnToForm) onReturnToForm();
            else anchor.current?.focus({ preventScroll: true });
            request.onKeep();
          }}>{returnToForm ? '返回表单' : '保留标签'}</button>
          <button type="button" disabled={busy} onClick={request.onConfirm}>
            {otherClients ? '关闭此处标签' : '关闭标签'}
          </button>
        </div>
      </div>, document.body,
    )}
  </>;
}
