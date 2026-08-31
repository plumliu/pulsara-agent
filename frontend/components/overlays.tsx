'use client';

import {
  Check,
  Command,
  Gauge,
  FolderOpen,
  FolderPlus,
  MessageCircle,
  Moon,
  Plus,
  Search,
  Settings,
  Sun,
  X,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import type { AppView, SessionWorkspaceSelection, ToastMessage } from '../lib/pulsara-types';

interface CommandPaletteProps {
  open: boolean;
  theme: 'light' | 'dark';
  onClose: () => void;
  onNavigate: (view: AppView) => void;
  onNewSession: () => void;
  onThemeChange: (theme: 'light' | 'dark') => void;
}

const commandItems = [
  { id: 'overview', label: '打开总览', detail: '查看运行状态与最近活动', icon: Gauge, view: 'overview' as AppView },
  { id: 'workbench', label: '打开会话工作台', detail: '回到当前活动会话', icon: MessageCircle, view: 'workbench' as AppView },
  { id: 'settings', label: '打开设置', detail: '外观、模型与本地服务', icon: Settings, view: 'settings' as AppView },
];

export function CommandPalette({ open, theme, onClose, onNavigate, onNewSession, onThemeChange }: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const filtered = useMemo(() => commandItems.filter((item) => `${item.label} ${item.detail}`.toLowerCase().includes(query.toLowerCase())), [query]);
  if (!open) return null;

  return (
    <div className="overlay-root" role="dialog" aria-modal="true" aria-label="命令面板">
      <button className="overlay-scrim" onClick={onClose} aria-label="关闭命令面板" />
      <section className="command-palette">
        <label><Search size={17} /><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入命令或搜索…" /><kbd>ESC</kbd></label>
        <div className="command-results">
          <span className="menu-label">建议</span>
          <button onClick={() => { onNewSession(); onClose(); }}><span className="command-icon"><Plus size={14} /></span><span><strong>新建会话</strong><small>选择快速开始或指定目录</small></span><kbd>⌘ N</kbd></button>
          {filtered.map(({ id, label, detail, icon: Icon, view }) => <button key={id} onClick={() => { onNavigate(view); onClose(); }}><span className="command-icon"><Icon size={14} /></span><span><strong>{label}</strong><small>{detail}</small></span></button>)}
        </div>
        <footer><span><Command size={11} /> Pulsara 快捷操作</span><button onClick={() => onThemeChange(theme === 'light' ? 'dark' : 'light')}>{theme === 'light' ? <Moon size={11} /> : <Sun size={11} />}{theme === 'light' ? '深色模式' : '浅色模式'}</button></footer>
      </section>
    </div>
  );
}

interface NewSessionDialogProps {
  open: boolean;
  defaultWorkspacePath: string;
  onClose: () => void;
  onCreate: (selection: SessionWorkspaceSelection) => Promise<boolean>;
}

export function NewSessionDialog({ open, defaultWorkspacePath, onClose, onCreate }: NewSessionDialogProps) {
  const [workspaceKind, setWorkspaceKind] = useState<'quick' | 'project'>('quick');
  const [workspacePath, setWorkspacePath] = useState(defaultWorkspacePath);
  const [submitting, setSubmitting] = useState(false);

  if (!open) return null;

  const submit = async () => {
    if (submitting || (workspaceKind === 'project' && !workspacePath.trim())) return;
    setSubmitting(true);
    const created = await onCreate(
      workspaceKind === 'quick'
        ? { kind: 'quick' }
        : { kind: 'project', path: workspacePath.trim() },
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
          <label className="workspace-path-field">
            <span>目录路径</span>
            <input autoFocus value={workspacePath} onChange={(event) => setWorkspacePath(event.target.value)} placeholder="/Users/you/path/to/project" />
            <small>请输入已存在的绝对路径</small>
          </label>
        )}
        <footer><p>创建后，在会话输入框中描述要完成的工作。</p><button className="primary-action" type="submit" disabled={submitting || (workspaceKind === 'project' && !workspacePath.trim())}>{submitting ? '正在创建…' : '创建会话'} <span>↗</span></button></footer>
      </form>
    </div>
  );
}

export function ToastStack({ toasts, onDismiss }: { toasts: ToastMessage[]; onDismiss: (id: number) => void }) {
  return (
    <div className="toast-stack" aria-live="polite">
      {toasts.map((toast) => <button className={`toast toast--${toast.tone ?? 'neutral'}`} key={toast.id} onClick={() => onDismiss(toast.id)}><span className="toast-mark">{toast.tone === 'success' ? <Check size={12} /> : '✦'}</span><span><strong>{toast.title}</strong>{toast.detail && <small>{toast.detail}</small>}</span><X size={11} /></button>)}
    </div>
  );
}
