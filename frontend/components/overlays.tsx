'use client';

import {
  Check,
  FolderOpen,
  FolderPlus,
  X,
} from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';
import type { SessionWorkspaceSelection, ToastMessage } from '../lib/pulsara-types';

interface NewSessionDialogProps {
  open: boolean;
  canCreateSession: boolean;
  defaultWorkspacePath: string;
  onClose: () => void;
  onCreate: (selection: SessionWorkspaceSelection) => Promise<boolean>;
  onPickDirectory: (initialPath: string, signal: AbortSignal) => Promise<string | null>;
}

export function NewSessionDialog({ open, canCreateSession, defaultWorkspacePath, onClose, onCreate, onPickDirectory }: NewSessionDialogProps) {
  const [workspaceKind, setWorkspaceKind] = useState<'quick' | 'project'>('quick');
  const [workspacePath, setWorkspacePath] = useState(defaultWorkspacePath);
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
      setWorkspacePath(defaultWorkspacePath);
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
          <button type="button" role="radio" aria-checked={workspaceKind === 'project'} className={`workspace-kind-card${workspaceKind === 'project' ? ' is-selected' : ''}`} onClick={() => { setWorkspaceKind('project'); if (!workspacePath) setWorkspacePath(defaultWorkspacePath); }}>
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
  return typeof document === 'undefined' ? null : document.querySelector<HTMLDialogElement>('dialog[open]');
}

function subscribeToModalChange(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.body, { attributes: true, attributeFilter: ['open'], childList: true, subtree: true });
  return () => observer.disconnect();
}

const noModalSubscription = () => () => {};

export function ToastStack({ toasts, onDismiss }: { toasts: ToastMessage[]; onDismiss: (id: number) => void }) {
  const stackRef = useRef<HTMLDivElement>(null);
  const hasToasts = toasts.length > 0;
  const modal = useSyncExternalStore(hasToasts ? subscribeToModalChange : noModalSubscription, activeModal, () => null);

  useLayoutEffect(() => {
    const stack = stackRef.current;
    if (!stack || !hasToasts || typeof stack.showPopover !== 'function') return;
    stack.showPopover();
    return () => { if (stack.matches(':popover-open')) stack.hidePopover(); };
  }, [modal, hasToasts]);

  const content = (
    <div ref={stackRef} className="toast-stack" popover="manual" aria-live="polite">
      {toasts.map((toast) => <button className={`toast toast--${toast.tone ?? 'neutral'}`} key={toast.id} onClick={() => onDismiss(toast.id)}><span className="toast-mark">{toast.tone === 'success' ? <Check size={12} /> : '✦'}</span><span><strong>{toast.title}</strong>{toast.detail && <small>{toast.detail}</small>}</span><X size={11} /></button>)}
    </div>
  );
  return modal ? createPortal(content, modal) : content;
}
