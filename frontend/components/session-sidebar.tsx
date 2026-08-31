import { Eye, FolderGit2, GitFork, Plus, Search } from 'lucide-react';
import type { RuntimeStatus, SessionSummary, Workspace } from '../lib/pulsara-types';
import { BrandMark } from './brand-mark';

const connectionLabels: Record<RuntimeStatus, string> = {
  starting: '正在连接',
  online: '已连接本地服务',
  reconnecting: '正在重新连接',
  offline: '连接已中断',
  failed: '连接失败',
};

interface SessionSidebarProps {
  workspace: Workspace;
  sessions: SessionSummary[];
  activeSessionId: string;
  runtimeStatus: RuntimeStatus;
  connectionRole?: 'observer' | 'controller';
  isOpen: boolean;
  onClose: () => void;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
  onOpenCommand: () => void;
  onTakeControl: () => void;
}

function StatusGlyph({ status }: { status: SessionSummary['status'] }) {
  return <span className={`session-status session-status--${status}`} aria-hidden="true" />;
}

function taskCountSummary(session: SessionSummary): string | undefined {
  const counts = session.taskCounts;
  if (!counts?.total) return undefined;
  const parts: string[] = [];
  if (counts.active + counts.waiting > 0) parts.push(`${counts.active + counts.waiting} 个进行中`);
  if (counts.attention > 0) parts.push(`${counts.attention} 项需留意`);
  if (!parts.length) parts.push(`${counts.total} 个子任务`);
  return parts.join(' · ');
}

export function SessionSidebar({
  workspace,
  sessions,
  activeSessionId,
  runtimeStatus,
  connectionRole,
  isOpen,
  onClose,
  onSelectSession,
  onNewSession,
  onOpenCommand,
  onTakeControl,
}: SessionSidebarProps) {
  return (
    <>
      <button
        className={`mobile-scrim${isOpen ? ' is-visible' : ''}`}
        onClick={onClose}
        aria-label="关闭会话侧栏"
        tabIndex={isOpen ? 0 : -1}
      />
      <aside className={`session-sidebar${isOpen ? ' is-mobile-open' : ''}`}>
        <header className="brand-row">
          <div className="brand-lockup">
            <BrandMark compact />
            <div>
              <p className="eyebrow">本地智能工作台</p>
              <h1>Pulsara</h1>
            </div>
          </div>
          <span className={`runtime-dot runtime-dot--${runtimeStatus}`} title={connectionLabels[runtimeStatus]} />
        </header>

        <button className="sidebar-search" onClick={onOpenCommand}>
          <Search size={14} />
          <span>搜索与命令</span>
          <kbd>⌘ K</kbd>
        </button>

        <button className="new-session" onClick={onNewSession}>
          <Plus size={14} />
          <span>新建会话</span>
          <kbd>⌘ N</kbd>
        </button>

        <div className="workspace-heading">
          <span className="workspace-emblem"><FolderGit2 size={14} /></span>
          <span className="workspace-copy">
            <strong>{workspace.name}</strong>
            <small>{workspace.kind === 'quick' ? '快速开始' : '指定目录'}</small>
          </span>
        </div>

        <section className="session-list" aria-label="最近会话">
          <div className="section-label">
            <span>最近会话</span>
          </div>
          <div className="session-list__scroll">
            {sessions.length === 0 && (
              <div className="session-empty">
                <strong>还没有会话</strong>
                <span>创建一个任务后，会话记录会出现在这里。</span>
              </div>
            )}
            {sessions.map((session) => (
              <button
                className={`session-item${session.id === activeSessionId ? ' is-active' : ''}`}
                key={session.id}
                onClick={() => onSelectSession(session.id)}
              >
                <StatusGlyph status={session.status} />
                <span className="session-item__copy">
                  <strong>{session.title}</strong>
                  <small>
                    {session.subtitle} · {session.id === activeSessionId
                      ? '当前会话'
                      : session.live ? '已载入' : '可恢复'}
                  </small>
                  <span className="session-item__meta">
                    <span>{session.updatedAt}</span>
                    <span>{session.workspace?.kind === 'quick' ? '快速开始' : '指定目录'}</span>
                  </span>
                  {taskCountSummary(session) && (
                    <span className={`session-item__tasks${session.taskCounts?.attention ? ' has-attention' : ''}`}>
                      <GitFork size={10} /> {taskCountSummary(session)}
                    </span>
                  )}
                </span>
                {session.unread && <span className="unread-dot" aria-label="有新活动" />}
              </button>
            ))}
          </div>
        </section>

        <div className="sidebar-footer">
          <div className="connection-line">
            <span className="status-pulse" />
            <span>{runtimeStatus === 'online' && connectionRole === 'observer' ? '已连接 · 旁观中' : connectionLabels[runtimeStatus]}</span>
            <code>本机</code>
          </div>
          <div className={`connection-detail${connectionRole === 'observer' ? ' is-observer' : ''}`}>
            <span>{connectionRole === 'observer' ? '此会话正在另一个窗口中操作' : '数据保存在这台设备上'}</span>
            {runtimeStatus === 'online' && connectionRole === 'observer' && (
              <button type="button" onClick={onTakeControl}><Eye size={11} /> 在此窗口继续</button>
            )}
          </div>
        </div>
      </aside>
    </>
  );
}
