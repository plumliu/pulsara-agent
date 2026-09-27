import { LoaderCircle, Menu, RotateCcw } from 'lucide-react';
import type { SessionSummary, Workspace } from '../lib/pulsara-types';
import { BrandMark } from './brand-mark';

export function SessionOpeningView({ session, workspace, error, onRetry, onOpenSidebar }: {
  session: SessionSummary;
  workspace: Workspace;
  error?: string;
  onRetry: () => void;
  onOpenSidebar: () => void;
}) {
  return (
    <section className="workbench session-opening" aria-label="会话加载页">
      <header className="topbar">
        <div className="session-title">
          <button className="mobile-menu-button" onClick={onOpenSidebar} aria-label="打开会话侧栏"><Menu size={17} /></button>
          <div><h2>{session.title}</h2><p>{workspace.kind === 'quick' ? '快速开始' : '指定目录'} · {workspace.path}</p></div>
        </div>
      </header>
      <div className="session-opening__body">
        <div className="session-opening__content">
          <BrandMark />
          <div role={error ? 'alert' : 'status'} aria-live="polite">
            <h3>{error ? '会话未能打开' : session.live ? '正在打开会话' : '正在恢复会话'}</h3>
            <p>{error || '正在载入消息与工作环境…'}</p>
          </div>
          {error
            ? <button className="ghost-button" onClick={onRetry}><RotateCcw size={14} />重试</button>
            : <LoaderCircle className="session-opening__spinner" size={18} aria-hidden="true" />}
        </div>
      </div>
    </section>
  );
}
