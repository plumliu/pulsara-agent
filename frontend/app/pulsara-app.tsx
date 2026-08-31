'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityRail } from '../components/activity-rail';
import { CapabilityView } from '../components/capability-view';
import { InspectorPanel } from '../components/inspector-panel';
import { CommandPalette, NewSessionDialog, ToastStack } from '../components/overlays';
import { OverviewView } from '../components/overview-view';
import { SessionSidebar } from '../components/session-sidebar';
import { SettingsView } from '../components/settings-view';
import { WorkbenchView } from '../components/workbench-view';
import {
  LocalHttpRuntimeAdapter,
  mergeRuntimeTaskInventory,
  RuntimeApiError,
  type RuntimeAdapter,
  type RuntimeBootstrap,
  type RuntimeConnection,
  type RuntimeInteractionResolution,
  type RuntimeInteractionSummary,
  type RuntimeProjection,
} from '../lib/runtime-adapter';
import type {
  AgentTask,
  AppView,
  CapabilitySnapshot,
  McpCreateInput,
  Message,
  PermissionMode,
  RuntimeStatus,
  SessionSummary,
  SessionWorkspaceSelection,
  ToastMessage,
  UserCapabilitySnapshot,
  UserMcpServerCapability,
  UserPluginCapability,
  UserSkillCapability,
  Workspace,
} from '../lib/pulsara-types';

const defaultAdapter = new LocalHttpRuntimeAdapter();

const emptyWorkspace: Workspace = {
  id: 'local',
  name: '本地工作区',
  path: '正在连接…',
  kind: 'project',
};

const emptySession: SessionSummary = {
  id: '',
  title: '尚未选择会话',
  subtitle: '创建一个任务，或从左侧恢复已有会话',
  status: 'draft',
  updatedAt: '',
  live: false,
};

const emptyProjection: RuntimeProjection = {
  messages: [],
  isRunning: false,
  queuedCount: 0,
  planMode: false,
  control: {},
  liveControl: {},
  agentTasks: [],
  eventSequence: 0,
  liveOwnerEpoch: 0,
  liveRevision: 0,
  liveControlOwnerEpoch: 0,
  liveControlRevision: 0,
};

function readSavedTheme(): 'light' | 'dark' {
  try {
    return typeof window !== 'undefined'
      && typeof window.localStorage?.getItem === 'function'
      && window.localStorage.getItem('pulsara-theme') === 'dark'
      ? 'dark'
      : 'light';
  } catch {
    return 'light';
  }
}

function readInitialInspectorVisibility(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(min-width: 1221px)').matches;
}

function readSavedSessionId(): string {
  try {
    return typeof window !== 'undefined'
      ? window.localStorage?.getItem('pulsara-active-session') ?? ''
      : '';
  } catch {
    return '';
  }
}

function saveSessionId(sessionId: string): void {
  try {
    window.localStorage?.setItem('pulsara-active-session', sessionId);
  } catch {
    // The session remains usable when browser preference storage is unavailable.
  }
}

const internalLanguage = /terminal(?:\s+protocol)?|protocol\s*v?\d*|kernel|canonical|generation|authority|projection|epoch|hostsession|runtime|attachment|owner|provider\s+prefix/i;
const TASK_FOCUS_DURATION_MS = 2600;

function productMessage(message: string | undefined, fallback: string): string {
  if (!message || internalLanguage.test(message) || !/[\u3400-\u9fff]/u.test(message)) return fallback;
  return message;
}

interface PulsaraAppProps {
  adapter?: RuntimeAdapter;
}

export default function PulsaraApp({ adapter = defaultAdapter }: PulsaraAppProps) {
  const [activeView, setActiveView] = useState<AppView>('workbench');
  const [bootstrap, setBootstrap] = useState<RuntimeBootstrap>();
  const [sessionList, setSessionList] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState('');
  const [projection, setProjection] = useState<RuntimeProjection>(emptyProjection);
  const [taskInventory, setTaskInventory] = useState<AgentTask[]>([]);
  const [taskInventorySessionId, setTaskInventorySessionId] = useState('');
  const [taskInventoryLoading, setTaskInventoryLoading] = useState(false);
  const [taskInventoryError, setTaskInventoryError] = useState<string>();
  const taskInventoryAttempt = useRef(0);
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot>();
  const [, setCapabilityLoading] = useState(false);
  const [, setCapabilityError] = useState<string>();
  const capabilityAttempt = useRef(0);
  const [userCapabilities, setUserCapabilities] = useState<UserCapabilitySnapshot>();
  const [userCapabilityLoading, setUserCapabilityLoading] = useState(false);
  const [userCapabilityError, setUserCapabilityError] = useState<string>();
  const userCapabilityAttempt = useRef(0);
  const [focusedTask, setFocusedTask] = useState<{ id: string; revision: number; highlighted: boolean }>();
  const focusTaskRevisionRef = useRef(0);
  const focusTaskTimerRef = useRef<number | undefined>(undefined);
  const [connection, setConnection] = useState<RuntimeConnection>();
  const connectionRef = useRef<RuntimeConnection | undefined>(undefined);
  const activeSessionIdRef = useRef('');
  const connectionAttempt = useRef(0);
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus>('starting');
  const [runtimeError, setRuntimeError] = useState<string>();
  const [optimisticMessages, setOptimisticMessages] = useState<Message[]>([]);
  const [inspectorOpen, setInspectorOpen] = useState(readInitialInspectorVisibility);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [theme, setTheme] = useState<'light' | 'dark'>(readSavedTheme);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const [turnPermission, setTurnPermission] = useState<PermissionMode>('bypass-permissions');

  useEffect(() => () => {
    if (focusTaskTimerRef.current !== undefined) window.clearTimeout(focusTaskTimerRef.current);
  }, []);

  const notify = useCallback((
    title: string,
    detail?: string,
    tone: ToastMessage['tone'] = 'neutral',
  ) => {
    const id = Date.now() + Math.floor(Math.random() * 1000);
    setToasts((current) => [...current.slice(-2), { id, title, detail, tone }]);
    window.setTimeout(
      () => setToasts((current) => current.filter((toast) => toast.id !== id)),
      4200,
    );
  }, []);

  const publishProjection = useCallback((next: RuntimeProjection) => {
    setProjection(next);
    setOptimisticMessages((current) => current.filter((optimistic) => (
      !next.messages.some((message) => (
        message.role === optimistic.role && message.body === optimistic.body
      ))
    )));
    setSessionList((current) => current.map((session) => (
      session.id === activeSessionIdRef.current
        ? {
          ...session,
          status: next.isRunning ? 'running' : 'completed',
          updatedAt: '刚刚',
        }
        : session
    )));
  }, []);

  const loadSessionTasks = useCallback(async (sessionId: string) => {
    const attempt = ++taskInventoryAttempt.current;
    setTaskInventoryLoading(true);
    setTaskInventoryError(undefined);
    try {
      const tasks: AgentTask[] = [];
      const seenCursors = new Set<string>();
      let cursor: string | undefined;
      do {
        const page = await adapter.listSessionTasks(sessionId, cursor);
        if (attempt !== taskInventoryAttempt.current || activeSessionIdRef.current !== sessionId) return;
        tasks.push(...page.tasks);
        cursor = page.nextCursor;
        if (cursor) {
          if (seenCursors.has(cursor)) {
            throw new RuntimeApiError('TASK_PAGE_LOOP', '子任务清单暂时无法完整读取。', true);
          }
          seenCursors.add(cursor);
        }
      } while (cursor);

      const uniqueTasks = [...new Map(tasks.map((task) => [task.id, task])).values()];
      setTaskInventory(uniqueTasks);
      setTaskInventorySessionId(sessionId);
      setSessionList((current) => current.map((session) => session.id === sessionId ? {
        ...session,
        taskCounts: {
          total: uniqueTasks.length,
          active: uniqueTasks.filter((task) => task.status === 'running').length,
          waiting: uniqueTasks.filter((task) => task.status === 'pending' || task.status === 'waiting').length,
          attention: uniqueTasks.filter((task) => (
            task.status === 'failed' || task.status === 'interrupted' || task.status === 'blocked'
          )).length,
        },
      } : session));
      setTaskInventoryLoading(false);
    } catch (error) {
      if (attempt !== taskInventoryAttempt.current || activeSessionIdRef.current !== sessionId) return;
      setTaskInventoryError(productMessage(
        error instanceof Error ? error.message : undefined,
        '子任务清单暂时无法读取。',
      ));
      setTaskInventoryLoading(false);
    }
  }, [adapter]);

  const loadCapabilities = useCallback(async (sessionId: string) => {
    const attempt = ++capabilityAttempt.current;
    setCapabilityLoading(true);
    setCapabilityError(undefined);
    try {
      const next = await adapter.inspectCapabilities(sessionId);
      if (attempt !== capabilityAttempt.current || activeSessionIdRef.current !== sessionId) return;
      setCapabilities(next);
      setCapabilityLoading(false);
    } catch (error) {
      if (attempt !== capabilityAttempt.current || activeSessionIdRef.current !== sessionId) return;
      setCapabilityError(productMessage(
        error instanceof Error ? error.message : undefined,
        '暂时无法读取这个会话的能力。',
      ));
      setCapabilityLoading(false);
    }
  }, [adapter]);

  const loadUserCapabilities = useCallback(async (refresh = false) => {
    const attempt = ++userCapabilityAttempt.current;
    setUserCapabilityLoading(true);
    setUserCapabilityError(undefined);
    try {
      const next = refresh
        ? await adapter.refreshUserCapabilities(activeSessionIdRef.current || undefined)
        : await adapter.inspectUserCapabilities(activeSessionIdRef.current || undefined);
      if (attempt !== userCapabilityAttempt.current) return;
      setUserCapabilities(next);
      setUserCapabilityLoading(false);
    } catch (error) {
      if (attempt !== userCapabilityAttempt.current) return;
      setUserCapabilityError(productMessage(
        error instanceof Error ? error.message : undefined,
        '暂时无法读取这台设备上的能力。',
      ));
      setUserCapabilityLoading(false);
    }
  }, [adapter]);

  const openRuntimeSession = useCallback(async (
    sessionId: string,
    reconnecting = false,
    takeover = false,
  ): Promise<RuntimeConnection | undefined> => {
    const attempt = ++connectionAttempt.current;
    taskInventoryAttempt.current += 1;
    capabilityAttempt.current += 1;
    setTaskInventory([]);
    setTaskInventorySessionId('');
    setTaskInventoryError(undefined);
    setTaskInventoryLoading(Boolean(sessionId));
    setCapabilities(undefined);
    setCapabilityError(undefined);
    setCapabilityLoading(Boolean(sessionId));
    if (focusTaskTimerRef.current !== undefined) {
      window.clearTimeout(focusTaskTimerRef.current);
      focusTaskTimerRef.current = undefined;
    }
    setFocusedTask(undefined);
    setRuntimeStatus(reconnecting ? 'reconnecting' : 'starting');
    setRuntimeError(undefined);
    const previous = connectionRef.current;
    connectionRef.current = undefined;
    setConnection(undefined);
    if (previous) await previous.close();
    try {
      const next = await adapter.connect(sessionId, takeover);
      if (attempt !== connectionAttempt.current) {
        await next.close();
        return undefined;
      }
      connectionRef.current = next;
      activeSessionIdRef.current = sessionId;
      setConnection(next);
      setActiveSessionId(sessionId);
      saveSessionId(sessionId);
      setOptimisticMessages([]);
      setSessionList((current) => current.map((session) => (
        session.id === sessionId ? { ...session, live: true } : session
      )));
      publishProjection(next.current());
      setRuntimeStatus('online');
      void adapter.listSessions().then((refreshed) => {
        if (attempt !== connectionAttempt.current) return;
        setSessionList((current) => refreshed.map((session) => {
          if (session.id !== activeSessionIdRef.current) return session;
          const projected = current.find((item) => item.id === session.id);
          return projected
            ? { ...session, status: projected.status, updatedAt: projected.updatedAt }
            : session;
        }));
      }).catch(() => {
        // The active connection is authoritative for the current session. A
        // later successful open/reconnect will retry the cold list refresh.
      });
      return next;
    } catch (error) {
      if (attempt !== connectionAttempt.current) return undefined;
      const message = productMessage(error instanceof Error ? error.message : undefined, '无法连接本地服务。');
      setRuntimeStatus(error instanceof RuntimeApiError && error.retryable ? 'offline' : 'failed');
      setRuntimeError(message);
      setTaskInventoryLoading(false);
      setCapabilityLoading(false);
      return undefined;
    }
  }, [adapter, publishProjection]);

  const recoverConnectionAfterOperation = useCallback((error: unknown) => {
    if (!(error instanceof RuntimeApiError) || !error.retryable) return;
    const active = connectionRef.current;
    if (!active) return;
    setRuntimeStatus('reconnecting');
    setRuntimeError(productMessage(error.message, '连接已中断，正在重新连接。'));
    void openRuntimeSession(active.sessionId, true);
  }, [openRuntimeSession]);

  useEffect(() => {
    let disposed = false;
    void (async () => {
      try {
        const [boot, sessions] = await Promise.all([
          adapter.bootstrap(),
          adapter.listSessions(),
        ]);
        if (disposed) return;
        setBootstrap(boot);
        setSessionList(sessions);
        const savedSessionId = readSavedSessionId();
        const initialSession = sessions.find((session) => session.id === savedSessionId) ?? sessions[0];
        if (initialSession) await openRuntimeSession(initialSession.id);
        else {
          setRuntimeStatus('online');
          setActiveView('overview');
        }
      } catch (error) {
        if (disposed) return;
        setRuntimeStatus(error instanceof RuntimeApiError && error.retryable ? 'offline' : 'failed');
        setRuntimeError(productMessage(error instanceof Error ? error.message : undefined, 'Pulsara 启动失败。'));
      }
    })();
    return () => {
      disposed = true;
      connectionAttempt.current += 1;
      const active = connectionRef.current;
      connectionRef.current = undefined;
      activeSessionIdRef.current = '';
      if (active) void active.close();
    };
  }, [adapter, openRuntimeSession]);

  useEffect(() => {
    if (!connection) return;
    const abort = new AbortController();
    let active = true;
    void (async () => {
      while (active && !abort.signal.aborted) {
        try {
          publishProjection(await connection.observe(abort.signal));
          setRuntimeStatus('online');
        } catch (error) {
          if (abort.signal.aborted || !active) return;
          setRuntimeStatus('reconnecting');
          setRuntimeError(productMessage(error instanceof Error ? error.message : undefined, '连接已中断。'));
          window.setTimeout(() => {
            if (active) void openRuntimeSession(connection.sessionId, true);
          }, 450);
          return;
        }
      }
    })();
    return () => {
      active = false;
      abort.abort();
    };
  }, [connection, openRuntimeSession, publishProjection]);

  const taskRefreshKey = useMemo(() => projection.agentTasks.map((task) => (
    `${task.id}:${task.status}:${task.result?.id ?? ''}:${task.completionDelivered ? '1' : '0'}`
  )).join('|'), [projection.agentTasks]);

  useEffect(() => {
    if (!activeSessionId || !connection || connection.sessionId !== activeSessionId) return;
    const frame = window.requestAnimationFrame(() => {
      void loadSessionTasks(activeSessionId);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeSessionId, connection, loadSessionTasks, taskRefreshKey]);

  useEffect(() => {
    if (!activeSessionId || !connection || connection.sessionId !== activeSessionId) return;
    const frame = window.requestAnimationFrame(() => {
      void loadCapabilities(activeSessionId);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeSessionId, connection, loadCapabilities]);

  useEffect(() => {
    if (activeView !== 'capabilities') return;
    const frame = window.requestAnimationFrame(() => void loadUserCapabilities());
    return () => window.cancelAnimationFrame(frame);
  }, [activeSessionId, activeView, loadUserCapabilities]);

  useEffect(() => {
    const refreshVisibleCapabilities = () => {
      if (document.visibilityState === 'visible' && activeView === 'capabilities') {
        void loadUserCapabilities();
      }
    };
    window.addEventListener('focus', refreshVisibleCapabilities);
    document.addEventListener('visibilitychange', refreshVisibleCapabilities);
    return () => {
      window.removeEventListener('focus', refreshVisibleCapabilities);
      document.removeEventListener('visibilitychange', refreshVisibleCapabilities);
    };
  }, [activeView, loadUserCapabilities]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage?.setItem('pulsara-theme', theme);
    } catch {
      // Browser preference storage may be unavailable in private contexts.
    }
  }, [theme]);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setCommandOpen((value) => !value);
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'n') {
        event.preventDefault();
        setNewSessionOpen(true);
      }
      if (event.key === 'Escape') {
        setCommandOpen(false);
        setNewSessionOpen(false);
        setSidebarOpen(false);
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, []);

  const workspace = bootstrap?.workspace ?? emptyWorkspace;
  const activeSession = useMemo(
    () => sessionList.find((session) => session.id === activeSessionId) ?? emptySession,
    [activeSessionId, sessionList],
  );
  const activeWorkspace = activeSession.workspace ?? workspace;
  const isObserver = connection?.role === 'observer';
  const canControl = connection?.role === 'controller';
  const mergedProjection = useMemo(() => mergeRuntimeTaskInventory(
    projection,
    taskInventorySessionId === activeSessionId ? taskInventory : [],
  ), [activeSessionId, projection, taskInventory, taskInventorySessionId]);
  const renderedMessages = useMemo(
    () => [...mergedProjection.messages, ...optimisticMessages],
    [mergedProjection.messages, optimisticMessages],
  );

  const navigate = (view: AppView) => {
    setActiveView(view);
    setSidebarOpen(false);
  };

  const openSession = (id: string) => {
    setActiveView('workbench');
    setSidebarOpen(false);
    void openRuntimeSession(id);
  };

  const createSession = async (selection: SessionWorkspaceSelection): Promise<boolean> => {
    try {
      const created = await adapter.createSession(selection);
      setSessionList((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setActiveView('workbench');
      const next = await openRuntimeSession(created.id);
      if (!next) return false;
      notify(
        '会话已创建',
        selection.kind === 'quick' ? '工作目录已由 Pulsara 准备好。' : '已连接到指定目录。',
        'success',
      );
      return true;
    } catch (error) {
      notify('无法创建会话', productMessage(error instanceof Error ? error.message : undefined, '请检查工作目录后重试。'), 'warning');
      return false;
    }
  };

  const sendPrompt = async (
    text: string,
    steer: boolean,
    permission: PermissionMode,
    requestPlan: boolean,
  ): Promise<boolean> => {
    const active = connectionRef.current;
    if (!active) {
      notify('本地服务未连接', runtimeError, 'warning');
      return false;
    }
    if (active.role !== 'controller') {
      notify('这个会话正在另一个窗口中操作', '选择“在此窗口继续”后即可发送新指令。', 'warning');
      return false;
    }
    try {
      if (requestPlan && projection.isRunning) {
        notify('当前运行结束后才能先规划', '先规划只作用于一条尚未开始的新输入。', 'warning');
        return false;
      }
      if (requestPlan && !projection.planMode) {
        const plan = await active.enterPlan(text, permission);
        if (plan.status === 'rejected') {
          notify('无法为本轮启用规划', productMessage(plan.publicMessage, '请稍后重试。'), 'warning');
          return false;
        }
      }
      const receipt = projection.isRunning && steer
        ? projection.activeTurnId
          ? await active.steerActiveTurn(text, projection.activeTurnId)
          : undefined
        : await active.submitPrompt(text, permission);
      if (!receipt) {
        notify('暂时无法引导当前任务', '当前没有可以接收补充指令的任务。', 'warning');
        return false;
      }
      if (receipt.status === 'rejected') {
        notify('输入被拒绝', productMessage(receipt.publicMessage, '本地服务没有接受这条输入。'), 'warning');
        return false;
      }
      setOptimisticMessages((current) => [...current, {
        id: `optimistic:${receipt.commandId}`,
        role: 'user',
        userKind: projection.isRunning && steer ? 'steer' : 'prompt',
        time: '现在',
        body: text,
        status: 'waiting',
      }]);
      notify(
        requestPlan ? '本轮将先制定计划' : projection.isRunning && !steer ? '输入将在下一轮处理' : steer ? '补充指令已接受' : '输入已接受',
        projection.isRunning && !steer
          ? '会在当前轮完成后自动开始。'
          : steer
            ? '当前任务会在安全位置接收这条补充指令。'
            : '已送达本地服务。',
        'success',
      );
      return true;
    } catch (error) {
      recoverConnectionAfterOperation(error);
      notify('提交失败', productMessage(error instanceof Error ? error.message : undefined, '请稍后重试。'), 'warning');
      return false;
    }
  };

  const stopRun = async () => {
    const active = connectionRef.current;
    if (!active || active.role !== 'controller') return;
    try {
      const receipt = await active.stopActiveTurn();
      notify(
        receipt.status === 'rejected' ? '没有可停止的运行' : '停止请求已送达',
        receipt.status === 'rejected' ? '当前没有可停止的任务。' : '正在停止当前任务。',
        receipt.status === 'rejected' ? 'warning' : 'success',
      );
    } catch (error) {
      recoverConnectionAfterOperation(error);
      notify('停止失败', productMessage(error instanceof Error ? error.message : undefined, '请稍后重试。'), 'warning');
    }
  };

  const compact = async () => {
    const active = connectionRef.current;
    if (!active || active.role !== 'controller') return;
    try {
      const receipt = await active.compactContext(projection.activeTurnId);
      const alreadyCompact = receipt.status === 'succeeded' && receipt.publicCode === 'NOT_NEEDED';
      notify(
        alreadyCompact ? '无需整理上下文' : receipt.status === 'rejected' ? '暂时无法整理上下文' : '上下文整理请求已提交',
        alreadyCompact
          ? '当前上下文已经较紧凑，本次整理无法进一步缩小。'
          : receipt.status === 'rejected'
            ? '当前状态不允许整理上下文。'
            : 'Pulsara 会在安全时机完成整理。',
        receipt.status === 'rejected' ? 'warning' : 'success',
      );
    } catch (error) {
      recoverConnectionAfterOperation(error);
      notify('上下文整理失败', productMessage(error instanceof Error ? error.message : undefined, '请稍后重试。'), 'warning');
    }
  };

  const acceptTaskCompletion = async (task: AgentTask): Promise<void> => {
    const active = connectionRef.current;
    if (!active || active.role !== 'controller' || task.completionDelivered) return;
    try {
      const receipt = await active.acceptSubagentCompletion(task.id, turnPermission);
      if (receipt.status === 'rejected') {
        if (receipt.publicCode === 'ROOT_TURN_ALREADY_RUNNING') {
          notify('主任务已经开始处理', '这项工作会在合适的时机自动交给 Pulsara。', 'success');
          void loadSessionTasks(active.sessionId);
          return;
        }
        notify('暂时无法继续处理', productMessage(receipt.publicMessage, '请刷新任务状态后重试。'), 'warning');
        return;
      }
      setTaskInventory((current) => current.map((item) => item.id === task.id ? {
        ...item,
        completionDelivered: true,
      } : item));
      setTurnPermission('accept-edits');
      notify(
        task.status === 'completed' ? 'Pulsara 已收到结果' : 'Pulsara 已收到这项问题',
        '已经开始新一轮处理；子任务不会重新运行。',
        'success',
      );
      void loadSessionTasks(active.sessionId);
    } catch (error) {
      recoverConnectionAfterOperation(error);
      notify('暂时无法继续处理', productMessage(error instanceof Error ? error.message : undefined, '请稍后重试。'), 'warning');
    }
  };

  const locateTask = (taskId: string) => {
    const revision = ++focusTaskRevisionRef.current;
    if (focusTaskTimerRef.current !== undefined) window.clearTimeout(focusTaskTimerRef.current);
    setActiveView('workbench');
    setFocusedTask({ id: taskId, revision, highlighted: true });
    focusTaskTimerRef.current = window.setTimeout(() => {
      setFocusedTask((current) => (
        current?.id === taskId && current.revision === revision
          ? { ...current, highlighted: false }
          : current
      ));
      focusTaskTimerRef.current = undefined;
    }, TASK_FOCUS_DURATION_MS);
  };

  const readInteraction = useCallback(async (interaction: RuntimeInteractionSummary) => {
    const active = connectionRef.current;
    if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
    return active.readInteraction(interaction);
  }, []);

  const resolveInteraction = useCallback(async (
    interaction: RuntimeInteractionSummary,
    resolution: RuntimeInteractionResolution,
  ): Promise<boolean> => {
    const active = connectionRef.current;
    if (!active) {
      notify('本地服务未连接', '重新连接后再完成这项确认。', 'warning');
      return false;
    }
    if (active.role !== 'controller') {
      notify('这个会话正在另一个窗口中操作', '选择“在此窗口继续”后即可完成这项确认。', 'warning');
      return false;
    }
    try {
      const receipt = await active.resolveInteraction(interaction, resolution);
      if (receipt.status === 'rejected') {
        notify('这项选择没有被接受', productMessage(receipt.publicMessage, '内容可能已经更新，请查看最新状态。'), 'warning');
        return false;
      }
      const title = resolution.kind === 'tool'
        ? resolution.decision === 'allow' ? '已允许本次操作' : '已拒绝本次操作'
        : resolution.kind === 'plan-draft'
          ? resolution.decision === 'approve' ? '方案已批准' : resolution.decision === 'revise' ? '修改意见已提交' : '规划已取消'
          : '回答已提交';
      notify(title, 'Pulsara 将继续处理。', 'success');
      return true;
    } catch (error) {
      recoverConnectionAfterOperation(error);
      notify('无法完成这项选择', productMessage(error instanceof Error ? error.message : undefined, '内容可能已经更新，请稍后重试。'), 'warning');
      return false;
    }
  }, [notify, recoverConnectionAfterOperation]);

  const installDeviceSkill = useCallback(async (sourcePath: string): Promise<boolean> => {
    try {
      const result = await adapter.installUserSkill(sourcePath, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      setUserCapabilityError(undefined);
      notify(result.operation.success ? '技能已安装' : '技能没有安装', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('技能安装失败', productMessage(error instanceof Error ? error.message : undefined, '请检查本地目录后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const installDevicePlugin = useCallback(async (sourcePath: string): Promise<boolean> => {
    try {
      const result = await adapter.installUserPlugin(sourcePath, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      setUserCapabilityError(undefined);
      notify(result.operation.success ? '插件已安装' : '插件没有安装', result.operation.message, result.operation.success ? 'success' : 'warning');
      return result.operation.success;
    } catch (error) {
      notify('插件安装失败', productMessage(error instanceof Error ? error.message : undefined, '请检查本地目录后重试。'), 'warning');
      return false;
    }
  }, [adapter, notify]);

  const createDeviceMcp = useCallback(async (input: McpCreateInput): Promise<boolean> => {
    try {
      const result = await adapter.createUserMcp(input, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      setUserCapabilityError(undefined);
      notify(result.operation.success ? 'MCP 服务已添加' : 'MCP 服务没有添加', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 服务添加失败', productMessage(error instanceof Error ? error.message : undefined, '请检查连接信息后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const toggleDeviceMcp = useCallback(async (
    server: UserMcpServerCapability,
    enabled: boolean,
  ): Promise<boolean> => {
    try {
      const result = await adapter.setUserMcpEnabled(server.id, enabled, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      notify(enabled ? 'MCP 服务已开启' : 'MCP 服务已关闭', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const toggleDeviceSkill = useCallback(async (
    skill: UserSkillCapability,
    enabled: boolean,
  ): Promise<boolean> => {
    try {
      const result = await adapter.setUserSkillEnabled(
        skill.path,
        enabled,
        activeSessionIdRef.current || undefined,
      );
      setUserCapabilities(result.capabilities);
      notify(enabled ? '技能已开启' : '技能已关闭', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('技能状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const toggleDevicePlugin = useCallback(async (
    plugin: UserPluginCapability,
    enabled: boolean,
  ): Promise<boolean> => {
    try {
      const result = await adapter.setUserPluginEnabled(
        plugin.id,
        plugin.packageInstallId,
        enabled,
        activeSessionIdRef.current || undefined,
      );
      setUserCapabilities(result.capabilities);
      notify(enabled ? '插件已开启' : '插件已关闭', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('插件状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const removeDevicePlugin = useCallback(async (plugin: UserPluginCapability): Promise<boolean> => {
    try {
      const result = await adapter.removeUserPlugin(plugin.id, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      notify(result.operation.success ? '插件已移除' : '插件没有移除', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('插件没有移除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const openCapabilityRoot = useCallback(async (root: 'agents' | 'pulsara'): Promise<void> => {
    try {
      await adapter.openCapabilityRoot(root);
    } catch (error) {
      notify('无法打开目录', productMessage(error instanceof Error ? error.message : undefined, '请稍后重试。'), 'warning');
    }
  }, [adapter, notify]);

  return (
    <main className={`pulsara-shell${activeView === 'workbench' ? ' is-workbench' : ' is-surface'}${inspectorOpen ? ' has-inspector' : ''}`}>
      <ActivityRail activeView={activeView} onNavigate={navigate} onOpenCommand={() => setCommandOpen(true)} />

      {activeView === 'workbench' && (
        <SessionSidebar
          workspace={activeWorkspace}
          sessions={sessionList}
          activeSessionId={activeSessionId}
          runtimeStatus={runtimeStatus}
          connectionRole={connection?.role}
          isOpen={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
          onSelectSession={openSession}
          onNewSession={() => setNewSessionOpen(true)}
          onOpenCommand={() => setCommandOpen(true)}
          onTakeControl={() => activeSessionId && void openRuntimeSession(activeSessionId, true, true)}
        />
      )}

      {activeView === 'overview' && (
        <OverviewView
          sessions={sessionList}
          activeSessionId={activeSessionId}
          runtimeStatus={runtimeStatus}
          agentTasks={mergedProjection.agentTasks}
          onNavigate={navigate}
          onOpenSession={openSession}
          onNewSession={() => setNewSessionOpen(true)}
        />
      )}
      {activeView === 'workbench' && (
        <WorkbenchView
          workspace={activeWorkspace}
          session={activeSession}
          messages={renderedMessages}
          todo={projection.todo}
          activePlanMode={projection.planMode}
          isRunning={projection.isRunning}
          inspectorOpen={inspectorOpen}
          queuedCount={projection.queuedCount}
          runtimeStatus={runtimeStatus}
          runtimeError={runtimeError}
          modelName={bootstrap?.provider.pro_model}
          interaction={projection.interaction}
          canControl={canControl}
          isObserver={isObserver}
          skills={capabilities?.skills.items ?? []}
          focusTaskId={focusedTask?.id}
          focusTaskRevision={focusedTask?.revision ?? 0}
          focusTaskHighlighted={focusedTask?.highlighted ?? false}
          onReconnect={() => activeSessionId && void openRuntimeSession(activeSessionId, true)}
          onTakeControl={() => activeSessionId && void openRuntimeSession(activeSessionId, true, true)}
          onOpenSidebar={() => setSidebarOpen(true)}
          onToggleInspector={() => setInspectorOpen((value) => !value)}
          onSend={sendPrompt}
          onStop={() => void stopRun()}
          onCompact={compact}
          onReadInteraction={readInteraction}
          onResolveInteraction={resolveInteraction}
          onNotify={notify}
          permission={turnPermission}
          onPermissionChange={setTurnPermission}
        />
      )}
      {activeView === 'workbench' && (
        <InspectorPanel
          session={activeSession}
          isOpen={inspectorOpen}
          agentTasks={mergedProjection.agentTasks}
          todo={projection.todo}
          loading={taskInventoryLoading}
          canControl={canControl}
          isRunning={projection.isRunning}
          permission={turnPermission}
          error={taskInventoryError}
          onRetry={() => activeSessionId && void loadSessionTasks(activeSessionId)}
          onLocate={locateTask}
          onAcceptCompletion={(task) => void acceptTaskCompletion(task)}
          onClose={() => setInspectorOpen(false)}
        />
      )}
      {activeView === 'capabilities' && (
        <CapabilityView
          snapshot={userCapabilities}
          loading={userCapabilityLoading}
          error={userCapabilityError}
          onRefresh={() => loadUserCapabilities(true)}
          onOpenRoot={openCapabilityRoot}
          onInstallSkill={installDeviceSkill}
          onInstallPlugin={installDevicePlugin}
          onCreateMcp={createDeviceMcp}
          onToggleSkill={toggleDeviceSkill}
          onToggleMcp={toggleDeviceMcp}
          onTogglePlugin={toggleDevicePlugin}
          onRemovePlugin={removeDevicePlugin}
        />
      )}
      {activeView === 'settings' && (
        <SettingsView
          theme={theme}
          bootstrap={bootstrap}
          runtimeStatus={runtimeStatus}
          onThemeChange={setTheme}
        />
      )}

      <CommandPalette
        open={commandOpen}
        theme={theme}
        onClose={() => setCommandOpen(false)}
        onNavigate={navigate}
        onNewSession={() => setNewSessionOpen(true)}
        onThemeChange={setTheme}
      />
      <NewSessionDialog
        open={newSessionOpen}
        defaultWorkspacePath={workspace.path === '正在连接…' ? '' : workspace.path}
        onClose={() => setNewSessionOpen(false)}
        onCreate={createSession}
      />
      <ToastStack
        toasts={toasts}
        onDismiss={(id) => setToasts((current) => current.filter((toast) => toast.id !== id))}
      />
    </main>
  );
}
