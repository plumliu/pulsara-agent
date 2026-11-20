import { Archive, ChevronRight, Ellipsis, Pencil, Folder, FolderOpen, LoaderCircle, Plus, Search, SquarePen, Trash2 } from 'lucide-react';
import {
  useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent,
} from 'react';
import type { RuntimeStatus, SessionSummary, SessionWorkspaceSelection, Workspace } from '../lib/pulsara-types';
import type { LocalScheduledTasksApi, ScheduledTask } from '../lib/scheduled-tasks-api';
import { desktopHost } from '../lib/desktop-host';
import { BrandMark } from './brand-mark';
import { isContextMenuKey, useContextMenu, type MenuItem } from './context-menu';
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
  openingSessionId?: string;
  sessionOpenErrors?: Readonly<Record<string, string | undefined>>;
  onSessionPointerDown?: (event: ReactPointerEvent, session: SessionSummary) => void;
  runtimeStatus: RuntimeStatus;
  isOpen: boolean;
  onClose: () => void;
  onSelectSession: (id: string) => void;
  onDeleteSession: (session: SessionSummary) => void;
  onArchiveSession: (session: SessionSummary) => void;
  onRenameSession: (session: SessionSummary) => void;
  onRefreshSessions: () => Promise<SessionSummary[] | undefined>;
  scheduledApi: LocalScheduledTasksApi;
  onTaskNotice: (title: string, detail?: string, tone?: 'success' | 'warning') => void;
  onNewSession: () => void;
  onCreateSession: (selection: SessionWorkspaceSelection) => Promise<boolean>;
  onPickDirectory: (signal: AbortSignal) => Promise<string | null>;
  onNotify: (title: string, detail: string) => void;
  canCreateSession: boolean;
  onOpenSearch: () => void;
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
  opening,
  openError,
  onSessionPointerDown,
  onSelectSession,
  onDeleteSession,
  onArchiveSession,
  onRenameSession,
  onRefreshSessions,
  scheduledApi,
  onTaskNotice,
}: {
  session: SessionSummary;
  activeSessionId: string;
  opening: boolean;
  openError?: string;
  onSessionPointerDown?: SessionSidebarProps['onSessionPointerDown'];
  onSelectSession: (id: string) => void;
  onDeleteSession: (session: SessionSummary) => void;
  onArchiveSession: (session: SessionSummary) => void;
  onRenameSession: (session: SessionSummary) => void;
  onRefreshSessions: SessionSidebarProps['onRefreshSessions'];
  scheduledApi: LocalScheduledTasksApi;
  onTaskNotice: SessionSidebarProps['onTaskNotice'];
}) {
  const presence = getSessionPresence(session, activeSessionId);
  const { open } = useContextMenu();
  const menuItems = (target: SessionSummary, tasks: ScheduledTask[] = []): MenuItem[] => [
    { label: '重命名', icon: Pencil, run: () => onRenameSession(target) },
    {
      label: '归档会话', icon: Archive, disabled: !target.canArchive,
      title: target.canArchive ? '归档会删除绑定的定时任务，取消归档后需重新创建' : '会话仍有任务或待处理事项，暂时无法归档',
      run: () => onArchiveSession(target),
    },
    ...tasks.map(task => ({
      key: task.id,
      label: tasks.length > 1 ? `删除定时任务 · ${task.name}` : '删除定时任务',
      icon: Trash2,
      danger: true,
      run: () => {
        void scheduledApi.action(task, 'delete').then(() => {
          void onRefreshSessions();
          onTaskNotice('定时任务已删除', task.name, 'success');
        }, error => onTaskNotice('定时任务未删除', error instanceof Error ? error.message : '请重试。', 'warning'));
      },
    })),
    { label: '删除会话…', icon: Trash2, danger: true, run: () => onDeleteSession(target) },
  ];
  const readTasks = async (signal: AbortSignal) => {
    const tasks: ScheduledTask[] = [];
    let cursor: string | undefined;
    do {
      const page = await scheduledApi.list('', cursor, signal, session.id);
      tasks.push(...page.tasks.filter(task => task.session_id === session.id));
      cursor = page.next_cursor ?? undefined;
    } while (cursor && !signal.aborted);
    return tasks;
  };
  const showMenu = (event: ReactMouseEvent<HTMLElement> | ReactKeyboardEvent<HTMLElement>) => {
    if (event.defaultPrevented) return;
    open(event, `${session.title} 更多操作`, menuItems(session), async signal => {
      const [sessions, tasks] = await Promise.all([onRefreshSessions(), readTasks(signal).catch(() => [])]);
      const refreshed = sessions?.find(item => item.id === session.id);
      return sessions && !refreshed ? [] : menuItems(refreshed ?? session, tasks);
    });
  };
  return (
    <div className="session-row" onContextMenu={showMenu}
      onKeyDown={event => { if (isContextMenuKey(event)) showMenu(event); }}>
      <button
        className={`session-item${presence === 'current' ? ' is-active' : ''}`}
        aria-current={presence === 'current' ? 'page' : undefined}
        onClick={event => { if (event.detail < 2) onSelectSession(session.id); }}
        onDoubleClick={() => onRenameSession(session)}
        onPointerDown={event => onSessionPointerDown?.(event, session)}
      >
        {opening ? <LoaderCircle size={11} className="session-item__loading" aria-hidden="true" /> : <SessionPresenceGlyph presence={presence} />}
        <span className="session-item__copy">
          <strong title={session.title}>{session.title}</strong>
          <small title={openError}>{opening ? '正在载入' : openError || sessionPresenceLabels[presence]}</small>
        </span>
        {session.unread && <span className="unread-dot" aria-label="有新活动" />}
      </button>
      {session.status === 'running' && <span className="session-row__activity" role="img" aria-label={`${session.title} 正在处理`} title="正在处理">
        <LoaderCircle size={14} className="is-spinning" aria-hidden="true" />
      </span>}
      <div className="session-row__menu">
        <button type="button" aria-label={`${session.title} 更多操作`} aria-haspopup="menu" onClick={showMenu}><Ellipsis size={15} /></button>
      </div>
    </div>
  );
}

export function SessionSidebar({
  workspace,
  sessions,
  activeSessionId,
  openingSessionId,
  sessionOpenErrors,
  onSessionPointerDown,
  runtimeStatus,
  isOpen,
  onClose,
  onSelectSession,
  onDeleteSession,
  onArchiveSession,
  onRenameSession,
  onRefreshSessions,
  scheduledApi,
  onTaskNotice,
  onNewSession,
  onCreateSession,
  onPickDirectory,
  onNotify,
  canCreateSession,
  onOpenSearch,
}: SessionSidebarProps) {
  const shortcutModifier = desktopHost()?.platform === 'darwin' ? '⌘' : 'Ctrl';
  const [scrollbarNearby, setScrollbarNearby] = useState(false);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(() => new Set());
  const [creatingGroup, setCreatingGroup] = useState<string | null>(null);
  const [pickingDirectory, setPickingDirectory] = useState(false);
  const createRequest = useRef<AbortController | null>(null);
  // A picker result must not create a session after this sidebar disappears or
  // creation becomes unavailable. Already submitted creates keep their owner.
  useEffect(() => () => { createRequest.current?.abort(); }, [canCreateSession]);

  const createFromShortcut = async (key: string, selection?: SessionWorkspaceSelection) => {
    if (!canCreateSession || createRequest.current) return;
    const request = new AbortController();
    createRequest.current = request;
    setCreatingGroup(key);
    setPickingDirectory(!selection);
    try {
      if (!selection) {
        const path = await onPickDirectory(request.signal);
        if (request.signal.aborted || path === null) return;
        selection = { kind: 'project', path };
      }
      setPickingDirectory(false);
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
        setPickingDirectory(false);
      }
    }
  };
  const addButton = (key: string, label: string, selection?: SessionWorkspaceSelection) => (
    <button
      className="session-group-add"
      type="button"
      aria-label={label}
      title={creatingGroup === key && pickingDirectory ? '等待选择目录' : label}
      aria-busy={creatingGroup === key && !pickingDirectory}
      disabled={!canCreateSession || creatingGroup !== null || (selection?.kind === 'project' && !selection.path)}
      onClick={() => void createFromShortcut(key, selection)}
    >
      {creatingGroup === key && !pickingDirectory
        ? <LoaderCircle size={13} className="is-spinning" />
        : selection ? <SquarePen size={13} /> : <FolderOpen size={13} />}
    </button>
  );
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
      <aside className="session-sidebar" inert={!isOpen}>
        <header className="brand-row">
          <div className="brand-lockup">
            <BrandMark compact />
            <h1>Pulsara</h1>
          </div>
          <span className={`runtime-dot runtime-dot--${runtimeStatus}`} title={connectionLabels[runtimeStatus]} />
        </header>

        <button className="sidebar-search" onClick={onOpenSearch}>
          <Search size={14} />
          <span>搜索会话</span>
          <kbd>{shortcutModifier} K</kbd>
        </button>

        <button className="new-session" disabled={!canCreateSession || creatingGroup !== null} onClick={onNewSession}>
          <Plus size={14} />
          <span>新建会话</span>
          <kbd>{shortcutModifier} N</kbd>
        </button>

        <section className="session-list" aria-label="会话目录">
          <div className="session-list__scroll" data-scrollbar-visible={scrollbarNearby || undefined}
            onPointerMove={event => {
              if (event.pointerType === 'touch') return;
              const edge = event.currentTarget.getBoundingClientRect().right;
              setScrollbarNearby(event.clientX >= edge - 24);
            }}
            onPointerLeave={() => setScrollbarNearby(false)}>
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
                                opening={openingSessionId === session.id}
                                openError={sessionOpenErrors?.[session.id]}
                                onSessionPointerDown={onSessionPointerDown}
                                onSelectSession={onSelectSession}
                                onDeleteSession={onDeleteSession} onArchiveSession={onArchiveSession} onRenameSession={onRenameSession} onRefreshSessions={onRefreshSessions}
                                scheduledApi={scheduledApi} onTaskNotice={onTaskNotice}
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
                      opening={openingSessionId === session.id}
                      openError={sessionOpenErrors?.[session.id]}
                      onSessionPointerDown={onSessionPointerDown}
                      onSelectSession={onSelectSession}
                      onDeleteSession={onDeleteSession} onArchiveSession={onArchiveSession} onRenameSession={onRenameSession} onRefreshSessions={onRefreshSessions}
                      scheduledApi={scheduledApi} onTaskNotice={onTaskNotice}
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
            <span>{connectionLabels[runtimeStatus]}</span>
          </div>

        </div>
      </aside>
    </>
  );
}
