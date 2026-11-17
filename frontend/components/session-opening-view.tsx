import { LoaderCircle, RotateCcw } from 'lucide-react';
import type { SessionSummary, Workspace } from '../lib/pulsara-types';
import { BrandMark } from './brand-mark';

export function SessionOpeningView({ session, workspace, error, failure, onRetry, onRestart }: {
  session: SessionSummary;
  workspace: Workspace;
  error?: string;
  failure?: { details: string; requiresRestart: boolean };
  onRetry: () => void;
  onRestart?: () => void;
}) {
  return (
    <section className="workbench session-opening" aria-label="会话加载页">
      <header className="topbar">
        <div className="session-title">
          <div><h2>{session.title}</h2><p>{workspace.kind === 'quick' ? '快速开始' : '指定目录'} · {workspace.path}</p></div>
        </div>
      </header>
      <div className="session-opening__body">
        <div className="session-opening__content">
          <BrandMark />
          <div role={error ? 'alert' : 'status'} aria-live="polite">
            <h3>{error ? '会话未能打开' : session.live ? '正在打开会话' : '正在恢复会话'}</h3>
            <p>{error || '正在载入消息与工作环境…'}</p>
            {error && failure?.requiresRestart && <p>任务仍在停止。</p>}
          </div>
          {error
            ? failure?.requiresRestart
              ? onRestart && <button className="ghost-button" onClick={onRestart}><RotateCcw size={14} />重启工作台</button>
              : <button className="ghost-button" onClick={onRetry}><RotateCcw size={14} />重试</button>
            : <LoaderCircle className="session-opening__spinner" size={18} aria-hidden="true" />}
          {error && failure?.details && <details className="runtime-error-details"><summary>技术详情</summary><pre>{failure.details}</pre></details>}
        </div>
      </div>
    </section>
  );
}
