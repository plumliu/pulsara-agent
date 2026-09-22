import { ChevronRight, Ellipsis, Eye, Folder, FolderOpen, GitFork, Plus, Search, Trash2 } from 'lucide-react';
import { useMemo, useState } from 'react';
import type { RuntimeStatus, SessionSummary, Workspace } from '../lib/pulsara-types';
import { BrandMark } from './brand-mark';
import {
  getSessionPresence,
  SessionPresenceGlyph,
  sessionPresenceLabels,
} from './session-presence';

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
  onDeleteSession: (session: SessionSummary) => void;
  onNewSession: () => void;
  canCreateSession: boolean;
  onOpenCommand: () => void;
  onTakeControl: () => void;
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

interface ProjectSessionGroup {
  key: string;
  label: string;
  path: string;
  sessions: SessionSummary[];
}

function groupSessionsByWorkspace(
  sessions: SessionSummary[],
  fallbackWorkspace: Workspace,
): { projects: ProjectSessionGroup[]; quick: SessionSummary[] } {
  const projects = new Map<string, ProjectSessionGroup>();
  const quick: SessionSummary[] = [];

  for (const session of sessions) {
    const sessionWorkspace = session.workspace ?? fallbackWorkspace;
    if (sessionWorkspace.kind === 'quick') {
      quick.push(session);
      continue;
    }
    const identity = sessionWorkspace.path || sessionWorkspace.id || sessionWorkspace.name;
    const key = `project:${identity}`;
    const existing = projects.get(key);
    if (existing) {
      existing.sessions.push(session);
      continue;
    }
    projects.set(key, {
      key,
      label: sessionWorkspace.name || '工作目录',
      path: sessionWorkspace.path,
      sessions: [session],
    });
  }

  return { projects: [...projects.values()], quick };
}

function SessionItem({
  session,
  activeSessionId,
  onSelectSession,
  onDeleteSession,
}: {
  session: SessionSummary;
  activeSessionId: string;
  onSelectSession: (id: string) => void;
  onDeleteSession: (session: SessionSummary) => void;
}) {
  const presence = getSessionPresence(session, activeSessionId);
  return (
    <div className="session-row">
    <button
      className={`session-item${presence === 'current' ? ' is-active' : ''}`}
      onClick={() => onSelectSession(session.id)}
    >
      <SessionPresenceGlyph presence={presence} />
      <span className="session-item__copy">
        <strong>{session.title}</strong>
        <small>{session.subtitle} · {sessionPresenceLabels[presence]}</small>
        {taskCountSummary(session) && (
          <span className={`session-item__tasks${session.taskCounts?.attention ? ' has-attention' : ''}`}>
            <GitFork size={10} /> {taskCountSummary(session)}
          </span>
        )}
      </span>
      {session.unread && <span className="unread-dot" aria-label="有新活动" />}
    </button>
    <details className="session-row__menu">
      <summary aria-label={`${session.title} 更多操作`}><Ellipsis size={15} /></summary>
      <button type="button" onClick={event => {
        event.currentTarget.closest('details')?.removeAttribute('open');
        onDeleteSession(session);
      }}><Trash2 size={13} />删除会话…</button>
    </details>
    </div>
  );
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
  onDeleteSession,
  onNewSession,
  canCreateSession,
  onOpenCommand,
  onTakeControl,
}: SessionSidebarProps) {
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(() => new Set());
  const groupedSessions = useMemo(
    () => groupSessionsByWorkspace(sessions, workspace),
    [sessions, workspace],
  );
  const toggleGroup = (key: string) => {
    setCollapsedGroups((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };
  const projectsCollapsed = collapsedGroups.has('section:projects');
  const quickCollapsed = collapsedGroups.has('section:quick');

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

        <button className="new-session" disabled={!canCreateSession} onClick={onNewSession}>
          <Plus size={14} />
          <span>新建会话</span>
          <kbd>⌘ N</kbd>
        </button>

        <section className="session-list" aria-label="会话目录">
          <div className="session-list__scroll">
            <section className="session-section" aria-label="从目录中打开">
              <button
                className={`session-section__toggle${projectsCollapsed ? '' : ' is-expanded'}`}
                type="button"
                aria-expanded={!projectsCollapsed}
                onClick={() => toggleGroup('section:projects')}
              >
                <strong>从目录中打开</strong><ChevronRight size={12} />
              </button>
              {!projectsCollapsed && (
                <div className="session-projects">
                  {groupedSessions.projects.length === 0 && <span className="session-group-empty">还没有从目录中打开的会话</span>}
                  {groupedSessions.projects.map((project, index) => {
                    const collapsed = collapsedGroups.has(project.key);
                    const sessionListId = `project-session-list-${index}`;
                    const ProjectIcon = collapsed ? Folder : FolderOpen;
                    return (
                      <section className="session-project" key={project.key} aria-label={`${project.label} 会话`}>
                        <button
                          className={`session-project__toggle${collapsed ? '' : ' is-expanded'}`}
                          type="button"
                          title={project.path || project.label}
                          aria-expanded={!collapsed}
                          aria-controls={sessionListId}
                          onClick={() => toggleGroup(project.key)}
                        >
                          <ProjectIcon size={13} /><strong>{project.label}</strong><ChevronRight size={12} />
                        </button>
                        {!collapsed && (
                          <div className="session-project__sessions" id={sessionListId}>
                            {project.sessions.map((session) => (
                              <SessionItem
                                key={session.id}
                                session={session}
                                activeSessionId={activeSessionId}
                                onSelectSession={onSelectSession}
                                onDeleteSession={onDeleteSession}
                              />
                            ))}
                          </div>
                        )}
                      </section>
                    );
                  })}
                </div>
              )}
            </section>

            <section className="session-section session-section--quick" aria-label="快速开始">
              <button
                className={`session-section__toggle${quickCollapsed ? '' : ' is-expanded'}`}
                type="button"
                aria-expanded={!quickCollapsed}
                onClick={() => toggleGroup('section:quick')}
              >
                <strong>快速开始</strong><ChevronRight size={12} />
              </button>
              {!quickCollapsed && (
                <div className="session-section__sessions">
                  {groupedSessions.quick.length === 0 && <span className="session-group-empty">还没有快速开始会话</span>}
                  {groupedSessions.quick.map((session) => (
                    <SessionItem
                      key={session.id}
                      session={session}
                      activeSessionId={activeSessionId}
                      onSelectSession={onSelectSession}
                      onDeleteSession={onDeleteSession}
                    />
                  ))}
                </div>
              )}
            </section>
          </div>
        </section>

        <div className="sidebar-footer">
          <div className="connection-line">
            <span className={`runtime-dot runtime-dot--${runtimeStatus}`} />
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
