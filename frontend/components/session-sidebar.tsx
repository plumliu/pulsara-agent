import { Archive, ChevronRight, Ellipsis, Eye, Folder, FolderOpen, GitFork, LoaderCircle, Plus, Search, SquarePen, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { RuntimeStatus, SessionSummary, SessionWorkspaceSelection, Workspace } from '../lib/pulsara-types';
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
  onArchiveSession: (session: SessionSummary) => void;
  onRefreshSessions: () => void;
  onNewSession: () => void;
  onCreateSession: (selection: SessionWorkspaceSelection) => Promise<boolean>;
  onPickDirectory: (initialPath: string, signal: AbortSignal) => Promise<string | null>;
  onNotify: (title: string, detail: string) => void;
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
  onArchiveSession,
  onRefreshSessions,
}: {
  session: SessionSummary;
  activeSessionId: string;
  onSelectSession: (id: string) => void;
  onDeleteSession: (session: SessionSummary) => void;
  onArchiveSession: (session: SessionSummary) => void;
  onRefreshSessions: () => void;
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
    <details className="session-row__menu" onToggle={event => { if (event.currentTarget.open) onRefreshSessions(); }}>
      <summary aria-label={`${session.title} 更多操作`}><Ellipsis size={15} /></summary>
      <div className="session-row__menu-actions">
      <button type="button" disabled={!session.canArchive} title={session.canArchive ? undefined : '会话仍有任务或待处理事项，暂时无法归档'} onClick={event => {
        event.currentTarget.closest('details')?.removeAttribute('open');
        onArchiveSession(session);
      }}><Archive size={13} />归档会话</button>
      <button type="button" onClick={event => {
        event.currentTarget.closest('details')?.removeAttribute('open');
        onDeleteSession(session);
      }}><Trash2 size={13} />删除会话…</button>
      </div>
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
  onArchiveSession,
  onRefreshSessions,
  onNewSession,
  onCreateSession,
  onPickDirectory,
  onNotify,
  canCreateSession,
  onOpenCommand,
  onTakeControl,
}: SessionSidebarProps) {
  const sidebarRef = useRef<HTMLElement>(null);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(() => new Set());
  const [creatingGroup, setCreatingGroup] = useState<string | null>(null);
  const createRequest = useRef<AbortController | null>(null);
  // A picker result must not create a session after this sidebar disappears or
  // creation becomes unavailable. Already submitted creates keep their owner.
  useEffect(() => () => { createRequest.current?.abort(); }, [canCreateSession]);

  const createFromShortcut = async (key: string, selection?: SessionWorkspaceSelection) => {
    if (!canCreateSession || createRequest.current) return;
    const request = new AbortController();
    createRequest.current = request;
    setCreatingGroup(key);
    try {
      if (!selection) {
        const path = await onPickDirectory(workspace.path, request.signal);
        if (request.signal.aborted || path === null) return;
        selection = { kind: 'project', path };
      }
      const selected = selection;
      setCollapsedGroups(current => {
        const next = new Set(current);
        next.delete(selected.kind === 'quick' ? 'section:quick' : 'section:projects');
        // Picker and session summaries both carry the host's canonical path.
        if (selected.kind === 'project') next.delete(`project:${selected.path}`);
        return next;
      });
      await onCreateSession(selected);
    } catch (error) {
      if (!request.signal.aborted) {
        onNotify('无法打开目录', error instanceof Error ? error.message : '请重试。');
      }
    } finally {
      if (createRequest.current === request) {
        createRequest.current = null;
        setCreatingGroup(null);
      }
    }
  };
  const addButton = (key: string, label: string, selection?: SessionWorkspaceSelection) => (
    <button
      className="session-group-add"
      type="button"
      aria-label={label}
      title={label}
      aria-busy={creatingGroup === key}
      disabled={!canCreateSession || creatingGroup !== null || (selection?.kind === 'project' && !selection.path)}
      onClick={() => void createFromShortcut(key, selection)}
    >
      {creatingGroup === key
        ? <LoaderCircle size={13} className="is-spinning" />
        : selection ? <SquarePen size={13} /> : <FolderOpen size={13} />}
    </button>
  );
  useEffect(() => {
    const openMenus = () => sidebarRef.current?.querySelectorAll<HTMLDetailsElement>('.session-row__menu[open]');
    const dismissOutside = (event: MouseEvent) => {
      if (!(event.target instanceof Node)) return;
      openMenus()?.forEach(menu => {
        if (!menu.contains(event.target as Node)) menu.open = false;
      });
    };
    const dismissOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      openMenus()?.forEach(menu => {
        menu.open = false;
        menu.querySelector('summary')?.focus();
      });
    };
    document.addEventListener('click', dismissOutside, true);
    document.addEventListener('keydown', dismissOnEscape);
    return () => {
      document.removeEventListener('click', dismissOutside, true);
      document.removeEventListener('keydown', dismissOnEscape);
    };
  }, []);
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
      <aside ref={sidebarRef} className={`session-sidebar${isOpen ? ' is-mobile-open' : ''}`}>
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

        <button className="new-session" disabled={!canCreateSession || creatingGroup !== null} onClick={onNewSession}>
          <Plus size={14} />
          <span>新建会话</span>
          <kbd>⌘ N</kbd>
        </button>

        <section className="session-list" aria-label="会话目录">
          <div className="session-list__scroll">
            <section className="session-section" aria-label="从目录中打开">
              <div className="session-group-heading">
              <button
                className={`session-section__toggle${projectsCollapsed ? '' : ' is-expanded'}`}
                type="button"
                aria-expanded={!projectsCollapsed}
                onClick={() => toggleGroup('section:projects')}
              >
                <strong>从目录中打开</strong><ChevronRight size={12} />
              </button>
              {addButton('section:projects', '选择目录并创建会话')}
              </div>
              {!projectsCollapsed && (
                <div className="session-projects">
                  {groupedSessions.projects.length === 0 && <span className="session-group-empty">还没有从目录中打开的会话</span>}
                  {groupedSessions.projects.map((project, index) => {
                    const collapsed = collapsedGroups.has(project.key);
                    const sessionListId = `project-session-list-${index}`;
                    const ProjectIcon = collapsed ? Folder : FolderOpen;
                    return (
                      <section className="session-project" key={project.key} aria-label={`${project.label} 会话`}>
                        <div className="session-group-heading">
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
                        {addButton(project.key, `在 ${project.label} 中创建会话`, { kind: 'project', path: project.path })}
                        </div>
                        {!collapsed && (
                          <div className="session-project__sessions" id={sessionListId}>
                            {project.sessions.map((session) => (
                              <SessionItem
                                key={session.id}
                                session={session}
                                activeSessionId={activeSessionId}
                                onSelectSession={onSelectSession}
                                onDeleteSession={onDeleteSession} onArchiveSession={onArchiveSession} onRefreshSessions={onRefreshSessions}
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
              <div className="session-group-heading">
              <button
                className={`session-section__toggle${quickCollapsed ? '' : ' is-expanded'}`}
                type="button"
                aria-expanded={!quickCollapsed}
                onClick={() => toggleGroup('section:quick')}
              >
                <strong>快速开始</strong><ChevronRight size={12} />
              </button>
              {addButton('section:quick', '创建快速开始会话', { kind: 'quick' })}
              </div>
              {!quickCollapsed && (
                <div className="session-section__sessions">
                  {groupedSessions.quick.length === 0 && <span className="session-group-empty">还没有快速开始会话</span>}
                  {groupedSessions.quick.map((session) => (
                    <SessionItem
                      key={session.id}
                      session={session}
                      activeSessionId={activeSessionId}
                      onSelectSession={onSelectSession}
                      onDeleteSession={onDeleteSession} onArchiveSession={onArchiveSession} onRefreshSessions={onRefreshSessions}
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
