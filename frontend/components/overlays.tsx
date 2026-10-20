import {
  Check,
  ChevronRight,
  FolderOpen,
  FolderPlus,
  X,
} from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { createPortal } from 'react-dom';
import type { SessionWorkspaceSelection, ToastMessage } from '../lib/pulsara-types';

interface NewSessionDialogProps {
  open: boolean;
  canCreateSession: boolean;
  onClose: () => void;
  onCreate: (selection: SessionWorkspaceSelection) => Promise<boolean>;
  onPickDirectory: (initialPath: string, signal: AbortSignal) => Promise<string | null>;
}

export function NewSessionDialog({ open, canCreateSession, onClose, onCreate, onPickDirectory }: NewSessionDialogProps) {
  const [workspaceKind, setWorkspaceKind] = useState<'quick' | 'project'>('quick');
  const [workspacePath, setWorkspacePath] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [picking, setPicking] = useState(false);
  const [pickerError, setPickerError] = useState('');
  const pickerRequest = useRef<AbortController | null>(null);
  useEffect(() => () => { pickerRequest.current?.abort(); }, []);

  const pickDirectory = async () => {
    if (pickerRequest.current || submitting) return;
    const request = new AbortController();
    pickerRequest.current = request;
    setPicking(true);
    setPickerError('');
    try {
      const path = await onPickDirectory(workspacePath, request.signal);
      if (!request.signal.aborted && path !== null) setWorkspacePath(path);
    } catch (error) {
      if (!request.signal.aborted) setPickerError(error instanceof Error ? error.message : '无法打开目录选择窗口，请重试。');
    } finally {
      if (!request.signal.aborted) {
        pickerRequest.current = null;
        setPicking(false);
      }
    }
  };

  if (!open) return null;

  const submit = async () => {
    if (!canCreateSession || submitting || picking || (workspaceKind === 'project' && !workspacePath)) return;
    setSubmitting(true);
    const created = await onCreate(
      workspaceKind === 'quick'
        ? { kind: 'quick' }
        : { kind: 'project', path: workspacePath },
    );
    setSubmitting(false);
    if (created) {
      setWorkspaceKind('quick');
      setWorkspacePath('');
      onClose();
    }
  };

  return (
    <div className="overlay-root" role="dialog" aria-modal="true" aria-labelledby="new-session-title">
      <button className="overlay-scrim" onClick={onClose} aria-label="关闭新建会话" />
      <form className="new-session-dialog" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <header><div><span className="page-kicker">本地会话</span><h2 id="new-session-title">新建会话</h2><p>选择这次会话使用的工作目录</p></div><button type="button" onClick={onClose} aria-label="关闭"><X size={16} /></button></header>
        <div className="workspace-kind-grid" role="radiogroup" aria-label="工作目录来源">
          <button type="button" role="radio" aria-checked={workspaceKind === 'quick'} className={`workspace-kind-card${workspaceKind === 'quick' ? ' is-selected' : ''}`} onClick={() => setWorkspaceKind('quick')}>
            <span className="workspace-kind-icon"><FolderPlus size={18} /></span>
            <span><strong>快速开始</strong><small>Pulsara 创建并管理一个持久目录</small></span>
            <i>{workspaceKind === 'quick' && <Check size={11} />}</i>
          </button>
          <button type="button" role="radio" aria-checked={workspaceKind === 'project'} className={`workspace-kind-card${workspaceKind === 'project' ? ' is-selected' : ''}`} onClick={() => setWorkspaceKind('project')}>
            <span className="workspace-kind-icon"><FolderOpen size={18} /></span>
            <span><strong>指定目录</strong><small>使用一个已有的本地工作目录</small></span>
            <i>{workspaceKind === 'project' && <Check size={11} />}</i>
          </button>
        </div>
        {workspaceKind === 'project' && (
          <div className="workspace-path-field">
            <label htmlFor="workspace-path-preview">目录路径</label>
            <div className="workspace-path-selection">
              <input id="workspace-path-preview" readOnly value={workspacePath} title={workspacePath} placeholder="尚未选择目录" />
              <button type="button" onClick={() => void pickDirectory()} disabled={picking || submitting}>
                <FolderOpen size={15} />{picking ? '正在选择…' : '选择目录'}
              </button>
            </div>
            {pickerError && <small role="alert">{pickerError}</small>}
          </div>
        )}
        <footer><button className="primary-action" type="submit" disabled={!canCreateSession || submitting || picking || (workspaceKind === 'project' && !workspacePath)}>{submitting ? '正在创建…' : '创建会话'} <span>↗</span></button></footer>
      </form>
    </div>
  );
}

function activeModal(): HTMLDialogElement | null {
  // Inline galleries use non-modal open dialogs; only a real modal owns toasts.
  return typeof document === 'undefined' ? null : Array.from(document.querySelectorAll<HTMLDialogElement>('dialog[open]')).find(dialog => dialog.matches(':modal')) ?? null;
}

function subscribeToModalChange(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.body, { attributes: true, attributeFilter: ['open'], childList: true, subtree: true });
  return () => observer.disconnect();
}

const noModalSubscription = () => () => {};

function Toast({ toast, exiting, onDismiss, onExitComplete }: {
  toast: ToastMessage; exiting: boolean;
  onDismiss: (id: number) => void; onExitComplete: (id: number) => void;
}) {
  const button = useRef<HTMLButtonElement>(null);
  const entrance = useRef<Animation | undefined>(undefined);
  const drag = useRef<{ pointerId: number; x: number; y: number; width: number } | undefined>(undefined);
  const suppressClick = useRef(false);
  const [offset, setOffset] = useState(0);
  const cancelEntrance = useCallback(() => { entrance.current?.cancel(); entrance.current = undefined; }, []);
  const releaseDrag = useCallback((target: HTMLButtonElement) => {
    const current = drag.current;
    drag.current = undefined;
    if (current && target.hasPointerCapture(current.pointerId)) target.releasePointerCapture(current.pointerId);
  }, []);
  const startDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (exiting || event.button !== 0 || event.isPrimary === false || drag.current) return;
    suppressClick.current = false;
    drag.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY,
      width: event.currentTarget.getBoundingClientRect().width };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const moveDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = drag.current;
    if (exiting || !current || current.pointerId !== event.pointerId) return;
    const x = event.clientX - current.x;
    const y = event.clientY - current.y;
    if (Math.hypot(x, y) < 6) { setOffset(0); return; }
    suppressClick.current = true;
    cancelEntrance();
    setOffset(x > Math.abs(y) ? x : 0);
    if (x >= Math.min(96, current.width * .3) && x > Math.abs(y)) {
      releaseDrag(event.currentTarget);
      onDismiss(toast.id);
    }
  };
  const finishDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const current = drag.current;
    if (exiting || !current || current.pointerId !== event.pointerId) return;
    const x = event.clientX - current.x;
    const y = event.clientY - current.y;
    if (Math.hypot(x, y) >= 6) suppressClick.current = true;
    releaseDrag(event.currentTarget);
    if (x >= Math.min(96, current.width * .3) && x > Math.abs(y)) {
      setOffset(x);
      onDismiss(toast.id);
    } else setOffset(0);
  };
  const cancelDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (drag.current?.pointerId !== event.pointerId) return;
    releaseDrag(event.currentTarget);
    if (!exiting) setOffset(0);
  };
  useEffect(() => {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    // Moving the portal between modals preserves this element's lifetime.
    entrance.current = button.current?.animate?.([
      { opacity: 0, transform: 'translateY(8px) scale(.985)' },
      { opacity: 1, transform: 'none' },
    ], { duration: 220, easing: 'ease-out' });
    return cancelEntrance;
  }, [cancelEntrance]);
  useLayoutEffect(() => {
    const target = button.current;
    if (!exiting || !target) return;
    cancelEntrance();
    releaseDrag(target);
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches || !target.animate) {
      onExitComplete(toast.id);
      return;
    }
    const style = window.getComputedStyle(target);
    const distance = offset + window.innerWidth - target.getBoundingClientRect().left + 24;
    const animation = target.animate([
      { translate: style.translate, opacity: style.opacity },
      { translate: `${distance}px 0`, opacity: 0 },
    ], { duration: 220, easing: 'cubic-bezier(.2,.7,.2,1)', fill: 'forwards' });
    let current = true;
    void animation.finished.then(() => { if (current) onExitComplete(toast.id); }, () => {});
    return () => { current = false; animation.cancel(); };
  }, [exiting, offset, onExitComplete, toast.id, cancelEntrance, releaseDrag]);
  return <button ref={button} className={`toast toast--${toast.tone ?? 'neutral'}${offset > 0 && !exiting ? ' is-dragging' : ''}${exiting ? ' is-exiting' : ''}`}
    style={{ translate: `${offset}px 0` }} disabled={exiting}
    onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={finishDrag}
    onPointerCancel={cancelDrag} onLostPointerCapture={cancelDrag}
    onClick={event => {
      // A captured drag can still produce a click. Keyboard activation remains.
      if (suppressClick.current && event.detail !== 0) { event.preventDefault(); return; }
      (toast.onClick ?? (() => onDismiss(toast.id)))();
    }}><span className="toast-mark">{toast.tone === 'success' ? <Check size={12} /> : '✦'}</span><span><strong>{toast.title}</strong>{toast.detail && <small>{toast.detail}</small>}</span>{toast.onClick ? <ChevronRight size={11} aria-hidden="true" /> : <X size={11} />}</button>;
}

export function ToastStack({ toasts, onDismiss }: { toasts: ToastMessage[]; onDismiss: (id: number) => void }) {
  const stackRef = useRef<HTMLDivElement>(null);
  const items = useRef(new Map<number, HTMLDivElement>());
  const positions = useRef(new Map<number, { element: HTMLDivElement; top: number; animation?: Animation }>());
  const [presentToasts, setPresentToasts] = useState(toasts);
  useLayoutEffect(() => {
    // Retain removed nodes only until their shared exit animation completes.
    setPresentToasts(current => [
      ...current.map(toast => toasts.find(next => next.id === toast.id) ?? toast),
      ...toasts.filter(toast => !current.some(existing => existing.id === toast.id)),
    ]);
  }, [toasts]);
  const finishExit = useCallback((id: number) => {
    setPresentToasts(current => current.filter(toast => toast.id !== id));
  }, []);
  const [portalHost] = useState<HTMLDivElement | null>(() => (
    typeof window === 'undefined' ? null : window.document.createElement('div')
  ));
  const hasToasts = presentToasts.length > 0;
  const modal = useSyncExternalStore(hasToasts ? subscribeToModalChange : noModalSubscription, activeModal, () => null);

  useLayoutEffect(() => () => {
    for (const position of positions.current.values()) position.animation?.cancel();
    portalHost?.remove();
  }, [portalHost]);

  useLayoutEffect(() => {
    // Wait for the modal observer if this commit has just detached its target.
    if (!portalHost || (modal && !modal.isConnected)) return;
    // Keep the React portal target stable so rehoming preserves toast nodes,
    // their entrance animations and their existing dismissal lifetime.
    const parent = modal ?? window.document.body;
    parent.appendChild(portalHost);
    const stack = stackRef.current;
    if (!stack?.isConnected || !hasToasts || typeof stack.showPopover !== 'function') return;
    stack.showPopover();
    return () => { if (stack.isConnected && stack.matches(':popover-open')) stack.hidePopover(); };
  }, [modal, hasToasts, portalHost]);

  useLayoutEffect(() => {
    const next = new Map<number, { element: HTMLDivElement; top: number; animation?: Animation }>();
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    for (const [id, element] of items.current) {
      const previous = positions.current.get(id);
      const transform = window.getComputedStyle(element).transform;
      const offset = transform && transform !== 'none' ? new DOMMatrixReadOnly(transform).m42 : 0;
      const top = element.getBoundingClientRect().top - offset;
      const distance = previous?.element === element ? previous.top - top + offset : 0;
      previous?.animation?.cancel();
      const animation = !reducedMotion && Math.abs(distance) > .5 ? element.animate?.([
        { transform: `translateY(${distance}px)` }, { transform: 'none' },
      ], { duration: 220, easing: 'cubic-bezier(.2,.7,.2,1)' }) : undefined;
      next.set(id, { element, top, animation });
    }
    for (const [id, previous] of positions.current) if (!next.has(id)) previous.animation?.cancel();
    positions.current = next;
  }, [presentToasts]);

  const content = (
    <div ref={stackRef} className="toast-stack" popover="manual" aria-live="polite">
      {presentToasts.map((toast) => <div className="toast-stack__item" key={toast.id}
        ref={element => { if (element) items.current.set(toast.id, element); else items.current.delete(toast.id); }}>
        <Toast toast={toast} exiting={!toasts.some(current => current.id === toast.id)} onDismiss={onDismiss} onExitComplete={finishExit} />
      </div>)}
    </div>
  );
  return portalHost && hasToasts ? createPortal(content, portalHost) : null;
}
