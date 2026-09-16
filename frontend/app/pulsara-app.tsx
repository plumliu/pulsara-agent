'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityRail } from '../components/activity-rail';
import { CapabilityView } from '../components/capability-view';
import { DatabaseSetupGuide } from '../components/database-setup-guide';
import { MemoryView } from '../components/memory-view';
import { InspectorPanel } from '../components/inspector-panel';
import { CommandPalette, NewSessionDialog, ToastStack } from '../components/overlays';
import { OverviewView } from '../components/overview-view';
import { SessionSidebar } from '../components/session-sidebar';
import { SettingsView } from '../components/settings-view';
import { WorkbenchView } from '../components/workbench-view';
import { ToolResultDisplayContext, readSavedToolResultDisplay } from '../lib/tool-result-display';
import { PromptDraftStore } from '../lib/prompt-draft';
import {
  LocalHttpRuntimeAdapter,
  createUserControlCommandRef,
  mergeRuntimeTaskInventory,
  RuntimeApiError,
  type RuntimeAdapter,
  type RuntimeBootstrap,
  type RuntimeConnection,
  type ModelCallBindingPayload,
  type RuntimeInteractionResolution,
  type RuntimeInteractionSummary,
  type RuntimeProjection,
  type LocalPromptSubmission,
  type QueuedPrompt,
  type QueuedPromptAction,
  type CommandReceipt,
  type EditablePromptContent,
  type CanonicalPromptImagePart,
} from '../lib/runtime-adapter';
import { promptContentTextProjection } from '../lib/prompt-content';
import type {
  PluginImportOptions,
  AgentTask,
  AppView,
  CapabilitySnapshot,
  McpEditInput,
  McpImportSelection,
  McpServerCapability,
  PermissionMode,
  RuntimeStatus,
  SessionSummary,
  SessionWorkspaceSelection,
  SkillCapability,
  SkillImportInput,
  ToastMessage,
  UserCapabilitySnapshot,
  UserMcpServerCapability,
  UserPluginCapability,
  PluginMcpConnection,
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
  queuedPrompts: [],
  promptTransitions: [],
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

function productMessage(message: string | undefined, fallback: string): string {
  if (!message || internalLanguage.test(message) || !/[\u3400-\u9fff]/u.test(message)) return fallback;
  return message;
}

function submissionFromReceipt(
  submission: LocalPromptSubmission,
  receipt: CommandReceipt,
): LocalPromptSubmission {
  const queueStatus = receipt.promptDelivery?.queueStatus.toUpperCase();
  const shared = {
    ...submission,
    queueItemId: receipt.promptDelivery?.queueItemId ?? submission.queueItemId,
    consumedEntryId: receipt.promptDelivery?.consumedEntryId ?? submission.consumedEntryId,
    deliveryMode: receipt.promptDelivery?.deliveryMode ?? submission.deliveryMode,
    outcomeCode: receipt.publicCode,
    detail: receipt.publicMessage,
  };
  if (queueStatus === 'CONSUMED') return { ...shared, status: 'consumed' };
  if (queueStatus === 'CANCELLED') return { ...shared, status: 'cancelled' };
  if (queueStatus === 'REJECTED') return { ...shared, status: 'rejected' };
  if (queueStatus === 'PENDING') return { ...shared, status: 'synchronizing' };
  return { ...shared, status: receipt.status === 'rejected' ? 'rejected' : 'synchronizing' };
}

function promptWasAccepted(receipt: CommandReceipt): boolean {
  const queueStatus = receipt.promptDelivery?.queueStatus.toUpperCase();
  if (queueStatus === 'CONSUMED' || queueStatus === 'PENDING') return true;
  if (queueStatus === 'CANCELLED' || queueStatus === 'REJECTED') return false;
  return receipt.status !== 'rejected';
}

interface PulsaraAppProps {
  adapter?: RuntimeAdapter;
}

type ToolDecisionIntent = {
  sessionId: string; hostSessionId: string; interactionId: string;
  commandId: string; decision: 'allow' | 'deny'; connectionGeneration: number;
  status: 'submitting' | 'unknown' | 'accepted' | 'rejected' | 'retired';
};

export default function PulsaraApp({ adapter = defaultAdapter }: PulsaraAppProps) {
  const [promptDraftStore] = useState(() => new PromptDraftStore());
  const [activeView, setActiveView] = useState<AppView>('workbench');
  const [bootstrap, setBootstrap] = useState<RuntimeBootstrap>();
  const [sessionList, setSessionList] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState('');
  const [focusMemoryEntry, setFocusMemoryEntry] = useState<{ sessionId: string; entryId: string }>();
  const [projection, setProjection] = useState<RuntimeProjection>(emptyProjection);
  const [taskInventory, setTaskInventory] = useState<AgentTask[]>([]);
  const [taskInventorySessionId, setTaskInventorySessionId] = useState('');
  const [taskInventoryLoading, setTaskInventoryLoading] = useState(false);
  const [taskInventoryError, setTaskInventoryError] = useState<string>();
  const taskInventoryAttempt = useRef(0);
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot>();
  const [capabilityLoading, setCapabilityLoading] = useState(false);
  const [capabilityError, setCapabilityError] = useState<string>();
  const [capabilityBusy, setCapabilityBusy] = useState<string>();
  const capabilityAttempt = useRef(0);
  const [userCapabilities, setUserCapabilities] = useState<UserCapabilitySnapshot>();
  const [userCapabilityLoading, setUserCapabilityLoading] = useState(false);
  const [userCapabilityError, setUserCapabilityError] = useState<string>();
  const userCapabilityAttempt = useRef(0);
  const [connection, setConnection] = useState<RuntimeConnection>();
  const connectionRef = useRef<RuntimeConnection | undefined>(undefined);
  const activeSessionIdRef = useRef('');
  const connectionAttempt = useRef(0);
  const promptReconciliationInFlight = useRef(new Set<string>());
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus>('starting');
  const [runtimeError, setRuntimeError] = useState<string>();
  const [localSubmissions, setLocalSubmissions] = useState<LocalPromptSubmission[]>([]);
  const [queueActions, setQueueActions] = useState<QueuedPromptAction[]>([]);
  const [toolDecisions, setToolDecisions] = useState<ToolDecisionIntent[]>([]);
  const toolDecisionsRef = useRef<ToolDecisionIntent[]>([]);
  const toolDecisionQueries = useRef(new Map<string, { cut: string; generation: number; inFlight: boolean }>());
  const saveToolDecision = useCallback((intent: ToolDecisionIntent) => {
    const previous = toolDecisionsRef.current.find(item => item.commandId === intent.commandId);
    // A late empty/error response cannot downgrade an exact known decision or
    // revive a retired query. A newer click has its own command identity.
    if (previous && ['accepted', 'rejected', 'retired'].includes(previous.status)
      && intent.status !== 'accepted') return;
    const next = [...toolDecisionsRef.current.filter(item => item.commandId !== intent.commandId), intent];
    if (['accepted', 'rejected', 'retired'].includes(intent.status)) toolDecisionQueries.current.delete(intent.commandId);
    toolDecisionsRef.current = next;
    setToolDecisions(next);
  }, []);
  const queueActionsInFlight = useRef(new Set<string>());
  const [inspectorOpen, setInspectorOpen] = useState(readInitialInspectorVisibility);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const desktop = window.matchMedia('(min-width: 1221px)');
    const onResize = (event: MediaQueryListEvent) => {
      if (!event.matches) setInspectorOpen(false);
    };
    desktop.addEventListener('change', onResize);
    return () => desktop.removeEventListener('change', onResize);
  }, []);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  useEffect(() => () => promptDraftStore.destroy(), [promptDraftStore]);
  const [theme, setTheme] = useState<'light' | 'dark'>(readSavedTheme);
  const [showBuiltinToolResults, setShowBuiltinToolResults] = useState(readSavedToolResultDisplay);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const [turnPermission, setTurnPermission] = useState<PermissionMode>('bypass-permissions');
  const databaseState = bootstrap?.database_state;
  const databaseBlocked = databaseState !== undefined && databaseState !== 'ready';
  const canCreateSession = runtimeStatus === 'online' && databaseState === 'ready';

  const ownsConnection = useCallback((expected: RuntimeConnection): boolean => (
    connectionRef.current === expected
    && activeSessionIdRef.current === expected.sessionId
    && connectionRef.current.generation === expected.generation
  ), []);

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

  const notifyPromptReceipt = useCallback((
    receipt: CommandReceipt,
    acceptedTitle: string,
    acceptedDetail: string,
  ) => {
    const queueStatus = receipt.promptDelivery?.queueStatus.toUpperCase();
    if (queueStatus === 'CONSUMED' && receipt.publicCode === 'TURN_INTERRUPTED') {
      notify(
        '输入已接收，执行已中断',
        productMessage(receipt.publicMessage, '输入已写入会话，但对应任务已中断。'),
        'warning',
      );
      return;
    }
    if (queueStatus === 'CONSUMED') {
      notify('输入已接受', productMessage(receipt.publicMessage, '输入已写入会话。'), 'success');
      return;
    }
    if (receipt.publicCode === 'USER_REDIRECTED_TO_STEER') {
      notify('已改为引导', '这条输入已改为当前任务的引导。', 'success');
      return;
    }
    if (queueStatus === 'CANCELLED') {
      notify('输入未投递', productMessage(receipt.publicMessage, '等待处理的输入已取消。'), 'warning');
      return;
    }
    if (queueStatus === 'REJECTED' || (!queueStatus && receipt.status === 'rejected')) {
      notify('输入被拒绝', productMessage(receipt.publicMessage, '本地服务没有接受这条输入。'), 'warning');
      return;
    }
    notify(acceptedTitle, acceptedDetail, 'success');
  }, [notify]);

  const publishProjection = useCallback((next: RuntimeProjection, owner?: RuntimeConnection) => {
    setProjection(next);
    const consumedCommandIds = new Set(next.messages.flatMap((message) => (
      message.inputSource?.commandId ? [message.inputSource.commandId] : []
    )));
    const queuedByCommand = new Map(next.queuedPrompts.map((item) => [item.commandId, item]));
    const transitionByCommand = new Map(next.promptTransitions.map((item) => (
      [item.commandId, item]
    )));
    setLocalSubmissions((current) => {
      const projected: LocalPromptSubmission[] = current.flatMap((item) => {
        if (owner && item.sessionId !== owner.sessionId) return [item];
        if (consumedCommandIds.has(item.commandId)) return [];
        const queued = queuedByCommand.get(item.commandId);
        if (queued) {
          return [{
            ...item,
            connectionGeneration: owner?.generation ?? item.connectionGeneration,
            status: 'queued' as const,
            observedPending: true,
            queueItemId: queued.queueItemId,
            deliveryMode: queued.deliveryMode,
            targetTurnId: queued.targetTurnId,
            permission: queued.permission,
          }];
        }
        const transition = transitionByCommand.get(item.commandId);
        if (transition) {
          return [{
            ...item,
            ...transition,
            content: transition.contentUnavailable && item.content
              ? item.content
              : transition.content,
          }];
        }
        if (
          owner
          && item.connectionGeneration !== owner.generation
          && (item.status === 'sending' || item.status === 'synchronizing' || item.status === 'queued')
        ) {
          return [{
            ...item,
            connectionGeneration: owner.generation,
            status: 'unknown' as const,
            lastCheckedEventSequence: undefined,
            lastCheckedConnectionGeneration: undefined,
          }];
        }
        if (item.status === 'queued') return [{ ...item, status: 'synchronizing' as const }];
        return [item];
      });
      if (!owner) return projected;
      for (const queued of next.queuedPrompts) {
        if (projected.some((item) => (
          item.sessionId === owner.sessionId && item.commandId === queued.commandId
        ))) continue;
        projected.push({
          sessionId: owner.sessionId,
          connectionGeneration: owner.generation,
          commandId: queued.commandId,
          queueItemId: queued.queueItemId,
          content: queued.content,
          deliveryMode: queued.deliveryMode,
          targetTurnId: queued.targetTurnId,
          permission: queued.permission,
          observedPending: true,
          status: 'queued',
        });
      }
      for (const transition of next.promptTransitions) {
        if (transition.sessionId !== owner.sessionId || projected.some((item) => (
          item.sessionId === owner.sessionId && item.commandId === transition.commandId
        ))) continue;
        projected.push(transition);
      }
      return projected;
    });
    setSessionList((current) => current.map((session) => (
      session.id === activeSessionIdRef.current
        ? {
          ...session,
          status: next.isRunning ? 'running'
            : next.control.latest_root_turn?.status === 'INTERRUPTED' ? 'interrupted'
            : next.control.latest_root_turn?.status === 'COMPLETED' ? 'completed'
            : 'draft',
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
      const groups: Array<{ id: string; taskCount: number }> = [];
      const seenGroupCursors = new Set<string>();
      let groupCursor: string | undefined;
      do {
        const page = await adapter.listSessionTaskGroups(sessionId, groupCursor);
        if (attempt !== taskInventoryAttempt.current || activeSessionIdRef.current !== sessionId) return;
        groups.push(...page.groups.map((group) => ({ id: group.id, taskCount: group.taskCount })));
        groupCursor = page.nextCursor;
        if (groupCursor) {
          if (seenGroupCursors.has(groupCursor)) {
            throw new RuntimeApiError('TASK_GROUP_PAGE_LOOP', '任务组清单暂时无法完整读取。', true);
          }
          seenGroupCursors.add(groupCursor);
        }
      } while (groupCursor);

      for (const group of groups) {
        const seenTaskCursors = new Set<string>();
        let taskCursor: string | undefined;
        let loaded = 0;
        do {
          const page = await adapter.listSessionTasks(sessionId, taskCursor, group.id);
          if (attempt !== taskInventoryAttempt.current || activeSessionIdRef.current !== sessionId) return;
          tasks.push(...page.tasks);
          loaded += page.tasks.length;
          taskCursor = page.nextCursor;
          if (taskCursor) {
            if (seenTaskCursors.has(taskCursor)) {
              throw new RuntimeApiError('TASK_PAGE_LOOP', '子任务清单暂时无法完整读取。', true);
            }
            seenTaskCursors.add(taskCursor);
          }
        } while (taskCursor);
        if (loaded !== group.taskCount) {
          throw new RuntimeApiError('TASK_GROUP_CHANGED', '任务组在读取期间发生变化，请刷新后重试。', true);
        }
      }

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
    // A mutation may settle after the user has already moved to another
    // session.  An obsolete refresh must not retire the current session's
    // inspection or leave its loading indicator without an owner.
    if (
      activeSessionIdRef.current !== sessionId
      || connectionRef.current?.sessionId !== sessionId
    ) return;
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

  const adoptCapabilityMutation = useCallback((
    sessionId: string,
    next: CapabilitySnapshot,
  ): void => {
    if (activeSessionIdRef.current !== sessionId) return;
    // A mutation response is newer than any inspection that began before it.
    // Retire those reads so a late response cannot paint stale switches back.
    capabilityAttempt.current += 1;
    setCapabilities(next);
    setCapabilityError(undefined);
    setCapabilityLoading(false);
  }, []);

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
      setSessionList((current) => current.map((session) => (
        session.id === sessionId ? { ...session, live: true } : session
      )));
      publishProjection(next.current(), next);
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

  const recoverConnectionAfterOperation = useCallback(async (
    error: unknown,
    expectedConnection: RuntimeConnection,
  ) => {
    if (!ownsConnection(expectedConnection)) return undefined;
    if (!(error instanceof RuntimeApiError) || !error.retryable) return expectedConnection;
    setRuntimeStatus('reconnecting');
    setRuntimeError(productMessage(error.message, '连接已中断，正在重新连接。'));
    const recovered = await openRuntimeSession(expectedConnection.sessionId, true);
    return recovered && ownsConnection(recovered) ? recovered : undefined;
  }, [openRuntimeSession, ownsConnection]);

  const refreshConfiguration = useCallback(async () => {
    const boot = await adapter.bootstrap();
    setBootstrap(boot);
    if (boot.database_state !== 'ready') {
      if (boot.database_state === 'database_restart_required') {
        const previous = connectionRef.current;
        connectionAttempt.current += 1;
        connectionRef.current = undefined;
        activeSessionIdRef.current = '';
        setConnection(undefined);
        setRuntimeStatus('online');
        setRuntimeError(undefined);
        if (previous) await previous.close();
      }
      if (!connectionRef.current) {
        setSessionList([]);
        setActiveSessionId('');
      }
      return;
    }
    setSessionList(await adapter.listSessions());
  }, [adapter]);

  useEffect(() => {
    let disposed = false;
    void (async () => {
      try {
        const boot = await adapter.bootstrap();
        if (disposed) return;
        setBootstrap(boot);
        if (boot.database_state !== 'ready') {
          setRuntimeStatus('online');
          return;
        }
        const sessions = await adapter.listSessions();
        if (disposed) return;
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
          const next = await connection.observe(abort.signal);
          if (!active || connectionRef.current !== connection) return;
          publishProjection(next, connection);
          for (const notice of next.presentationNotices ?? []) {
            notify(notice);
          }
          setRuntimeStatus('online');
        } catch (error) {
          if (abort.signal.aborted || !active || !ownsConnection(connection)) return;
          setRuntimeStatus('reconnecting');
          setRuntimeError(productMessage(error instanceof Error ? error.message : undefined, '连接已中断。'));
          window.setTimeout(() => {
            if (active && ownsConnection(connection)) void openRuntimeSession(connection.sessionId, true);
          }, 450);
          return;
        }
      }
    })();
    return () => {
      active = false;
      abort.abort();
    };
  }, [connection, notify, openRuntimeSession, ownsConnection, publishProjection]);

  useEffect(() => {
    if (!connection || connection.sessionId !== activeSessionId) return;
    const candidate = localSubmissions.find((item) => (
      item.sessionId === connection.sessionId
      && (
        (item.status === 'synchronizing' && item.observedPending)
        || item.status === 'unknown'
      )
      && (
        item.lastCheckedEventSequence !== projection.eventSequence
        || item.lastCheckedConnectionGeneration !== connection.generation
      )
      && !promptReconciliationInFlight.current.has(
        `${item.sessionId}:${connection.generation}:${item.commandId}:${projection.eventSequence}`,
      )
    ));
    if (!candidate) return;
    const reconciliationKey = `${candidate.sessionId}:${connection.generation}:${candidate.commandId}:${projection.eventSequence}`;
    promptReconciliationInFlight.current.add(reconciliationKey);
    void (async () => {
      try {
        const receipt = await connection.queryCommand(candidate.commandId);
        if (connectionRef.current !== connection) return;
        if (!receipt) {
          setLocalSubmissions((current) => current.map((item) => (
            item.sessionId === candidate.sessionId
            && item.connectionGeneration === candidate.connectionGeneration
            && item.commandId === candidate.commandId
              ? {
                ...item,
                status: 'unknown',
                lastCheckedEventSequence: projection.eventSequence,
                lastCheckedConnectionGeneration: connection.generation,
                detail: '本地服务尚未返回这条输入的最终状态。',
              }
              : item
          )));
          return;
        }
        setLocalSubmissions((current) => current.map((item) => (
          item.sessionId === candidate.sessionId
          && item.connectionGeneration === candidate.connectionGeneration
          && item.commandId === candidate.commandId
            ? submissionFromReceipt(
              {
                ...item,
                lastCheckedEventSequence: projection.eventSequence,
                lastCheckedConnectionGeneration: connection.generation,
              },
              receipt,
            )
            : item
        )));
        if (receipt.promptDelivery?.queueStatus.toUpperCase() !== 'PENDING') {
          notifyPromptReceipt(receipt, '输入状态已核对', '已按原始输入身份同步本地服务状态。');
        }
        const next = await connection.snapshot();
        if (connectionRef.current === connection) publishProjection(next, connection);
      } catch (error) {
        if (connectionRef.current !== connection) return;
        setLocalSubmissions((current) => current.map((item) => (
          item.sessionId === candidate.sessionId
          && item.connectionGeneration === candidate.connectionGeneration
          && item.commandId === candidate.commandId
            ? {
              ...item,
              status: 'unknown',
              lastCheckedEventSequence: projection.eventSequence,
              lastCheckedConnectionGeneration: connection.generation,
              detail: productMessage(
                error instanceof Error ? error.message : undefined,
                '暂时无法核对这条输入；Pulsara 不会自动重发。',
              ),
            }
            : item
        )));
      } finally {
        promptReconciliationInFlight.current.delete(reconciliationKey);
      }
    })();
  }, [
    activeSessionId,
    connection,
    localSubmissions,
    notifyPromptReceipt,
    projection.eventSequence,
    publishProjection,
  ]);

  const taskRefreshKey = useMemo(() => projection.agentTasks.map((task) => (
    `${task.id}:${task.status}:${task.result?.id ?? ''}:${task.completionAccepted ? '1' : '0'}`
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
    if (
      !activeSessionId
      || !connection
      || connection.sessionId !== activeSessionId
      || !capabilities?.adoption.pending
      || projection.isRunning
    ) return;
    const frame = window.requestAnimationFrame(() => {
      void loadCapabilities(activeSessionId);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    activeSessionId,
    capabilities?.adoption.pending,
    connection,
    loadCapabilities,
    projection.isRunning,
    projection.messages.length,
  ]);

  const mcpConnectionIsSettling = capabilities?.mcp.servers.some((server) => (
    server.status === 'connecting'
    || server.status === 'discovering'
    || server.status === 'updating'
  )) ?? false;

  useEffect(() => {
    if (
      !mcpConnectionIsSettling
      || !activeSessionId
      || !connection
      || connection.sessionId !== activeSessionId
    ) return;
    const timer = window.setTimeout(() => {
      void loadCapabilities(activeSessionId);
    }, 650);
    return () => window.clearTimeout(timer);
  }, [
    activeSessionId,
    capabilities,
    connection,
    loadCapabilities,
    mcpConnectionIsSettling,
  ]);

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
    try {
      window.localStorage?.setItem('pulsara-show-builtin-tool-results', String(showBuiltinToolResults));
    } catch {
      // Browser preference storage may be unavailable in private contexts.
    }
  }, [showBuiltinToolResults]);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setCommandOpen((value) => !value);
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'n') {
        event.preventDefault();
        if (canCreateSession) {
          setNewSessionOpen(true);
        }
      }
      if (event.key === 'Escape') {
        setCommandOpen(false);
        setNewSessionOpen(false);
        setSidebarOpen(false);
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [canCreateSession]);

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
  const taskActivities = useMemo(() => {
    const activities = new Map<string, NonNullable<(typeof mergedProjection.messages)[number]['subagentRuns']>[number]['activities']>();
    for (const message of mergedProjection.messages) {
      for (const run of message.subagentRuns ?? []) {
        const byId = new Map((activities.get(run.id) ?? []).map((item) => [item.id, item]));
        for (const item of run.activities) byId.set(item.id, item);
        activities.set(run.id, [...byId.values()]);
      }
    }
    return activities;
  }, [mergedProjection]);
  const renderedMessages = mergedProjection.messages;

  const navigate = (view: AppView) => {
    setActiveView(view);
    setSidebarOpen(false);
  };

  const openNewSession = () => {
    if (!canCreateSession) return;
    setNewSessionOpen(true);
  };

  const reconnect = () => {
    if (activeSessionId) void openRuntimeSession(activeSessionId, true);
    else window.location.reload();
  };

  const openSession = (id: string) => {
    setActiveView('workbench');
    setSidebarOpen(false);
    void openRuntimeSession(id);
  };

  const createSession = async (selection: SessionWorkspaceSelection): Promise<boolean> => {
    if (!canCreateSession) return false;
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

  const forkConversation = async (entryId: string): Promise<void> => {
    const sourceId = activeSessionIdRef.current;
    if (!sourceId) return;
    const childId = `session:${crypto.randomUUID().replaceAll('-', '')}`;
    let outcome;
    try {
      outcome = await adapter.forkConversation(sourceId, entryId, childId);
    } catch {
      // Resolve the preselected identity; never replay an uncertain creation.
      try {
        const child = await adapter.readSession(childId);
        if (!child) {
          notify('尚未确认分叉结果', `请刷新会话列表后确认。新会话 ID：${childId}`, 'warning');
          return;
        }
        outcome = { outcome: 'CREATED_OPEN_DEFERRED' as const };
      } catch {
        notify('尚未确认分叉结果', `请恢复连接后查询会话 ${childId}；不会自动重复创建。`, 'warning');
        return;
      }
    }
    if (outcome.outcome === 'NOT_CREATED') {
      notify('未创建分叉', outcome.public_code ?? '请选择已完成的最终回复。', 'warning');
      return;
    }
    try { setSessionList(await adapter.listSessions()); } catch { /* child remains canonical */ }
    if (outcome.outcome === 'CREATED_OPEN_DEFERRED') {
      notify('分叉已创建，暂未打开', '稍后从会话列表打开即可，无需重新创建。', 'warning');
      return;
    }
    setActiveView('workbench');
    if (await openRuntimeSession(childId)) {
      setTurnPermission('bypass-permissions');
      notify('分叉已打开', '已保留选定回复处的有效上下文。', 'success');
    }
    else notify('分叉已创建，暂未连接', '可以从会话列表重新打开。', 'warning');
  };

  const updateModelCallBinding = async (binding: ModelCallBindingPayload): Promise<void> => {
    const active = connectionRef.current;
    const sessionId = activeSessionIdRef.current;
    if (!sessionId || !active || active.role !== 'controller') {
      throw new Error('当前窗口没有修改这个会话的权限。');
    }
    const accepted = await adapter.updateModelCallBinding(sessionId, binding);
    if (!ownsConnection(active)) return;
    setSessionList((current) => current.map((session) => session.id === sessionId
      ? { ...session, modelCallBinding: accepted.modelCallBinding }
      : session));
    notify(
      accepted.reasoningPreferenceReset ? '推理选项已更新' : '会话模型已更新',
      accepted.reasoningPreferenceReset
        ? '原选择已不再适用于该模型，已改用这个模型当前的默认选项。'
        : '新选择只作用于下一条尚未接纳的新输入。',
      accepted.reasoningPreferenceReset ? 'warning' : 'success',
    );
  };

  const settleQueueAction = useCallback((action: QueuedPromptAction, receipt: CommandReceipt, owner: RuntimeConnection) => {
    if (!ownsConnection(owner) || owner.sessionId !== action.sessionId) return;
    const delivery = receipt.promptDelivery;
    const accepted = action.kind === 'send'
      ? Boolean(delivery && delivery.deliveryMode === 'steer' && ['PENDING', 'CONSUMED'].includes(delivery.queueStatus))
      : receipt.status === 'succeeded' && receipt.publicCode === 'PROMPT_CANCELLED'
        && delivery?.queueItemId === action.source.queueItemId;
    if (receipt.commandId !== action.commandId) throw new Error('队列操作返回了不一致的身份。');
    setQueueActions(current => current.map(item => item.commandId === action.commandId && item.sessionId === action.sessionId
      ? { ...item, connectionGeneration: owner.generation, status: accepted ? 'accepted' : 'rejected', receipt }
      : item));
    if (accepted) setLocalSubmissions(current => current.map(item => (
      item.sessionId === action.sessionId && item.commandId === action.source.commandId
        && item.queueItemId === action.source.queueItemId
        ? { ...item, handledByQueueAction: true }
        : item
    )));
    queueActionsInFlight.current.delete(`${action.sessionId}:${action.source.queueItemId}`);
    if (!accepted) notify('操作未完成', productMessage(receipt.publicMessage, receipt.publicCode ?? '请刷新后查看这条输入的状态。'), 'warning');
  }, [notify, ownsConnection]);

  const actOnQueuedPrompt = async (source: QueuedPrompt, kind: QueuedPromptAction['kind']) => {
    const active = connectionRef.current;
    if (!active || active.role !== 'controller' || !ownsConnection(active)) return;
    // CURRENT_CONTROL advances across FIFO ROOTs; the connection's initial
    // live-control snapshot can still name the ROOT present at reload.
    const targetTurnId = active.current().control.active_turns?.find(
      turn => turn.scope_kind === 'ROOT' && turn.status === 'RUNNING',
    )?.turn_id;
    if (kind === 'send' && !targetTurnId) {
      notify('当前任务已结束', '这条输入会按队列顺序自动处理。', 'warning');
      return;
    }
    const key = `${active.sessionId}:${source.queueItemId}`;
    if (queueActionsInFlight.current.has(key)) return;
    queueActionsInFlight.current.add(key);
    let restoredContent: EditablePromptContent | undefined;
    if (kind === 'edit') {
      try {
        restoredContent = await active.readPromptForEdit(source.content);
      } catch (error) {
        queueActionsInFlight.current.delete(key);
        if (ownsConnection(active)) notify(
          '暂时无法编辑这条输入',
          productMessage(
            error instanceof Error ? error.message : undefined,
            '原排队输入仍会保留，请稍后重试。',
          ),
          'warning',
        );
        return;
      }
      if (!ownsConnection(active)) {
        queueActionsInFlight.current.delete(key);
        return;
      }
    }
    const action: QueuedPromptAction = {
      sessionId: active.sessionId, connectionGeneration: active.generation,
      commandId: `command:web:${crypto.randomUUID()}`, source, kind,
      restoredContent,
      targetTurnId: kind === 'send' ? targetTurnId : undefined,
      submittedAt: new Date().toISOString(), status: 'submitting',
    };
    setQueueActions(current => [...current.filter(item => !(item.sessionId === action.sessionId && item.source.queueItemId === source.queueItemId)), action]);
    try {
      const receipt = kind === 'send'
        ? await active.steerQueuedPrompt(action.commandId, source.queueItemId, targetTurnId!)
        : await active.cancelQueuedPrompt(action.commandId, source.queueItemId);
      if (!ownsConnection(active)) return;
      settleQueueAction(action, receipt, active);
    } catch (error) {
      if (!ownsConnection(active)) return;
      const recovered = await recoverConnectionAfterOperation(error, active);
      if (!recovered || !ownsConnection(recovered)) return;
      // The reconciliation effect owns queries, including recovery on a new
      // connection. Do not race a second query against it after reconnect.
      setQueueActions(current => current.map(item => item.commandId === action.commandId
        ? { ...item, status: 'unknown', connectionGeneration: recovered.generation,
          lastCheckedEventSequence: undefined, lastCheckedConnectionGeneration: undefined }
        : item));
      notify('正在核对操作状态', '将查询这次操作的原始身份，不会自动重发。', 'warning');
    }
  };

  useEffect(() => {
    if (!connection || !ownsConnection(connection)) return;
    setQueueActions(current => current.flatMap(item => {
      if (item.sessionId !== connection.sessionId) return [item];
      if (item.status === 'submitting' && item.connectionGeneration !== connection.generation) return [{
        ...item, status: 'unknown' as const, connectionGeneration: connection.generation,
        lastCheckedEventSequence: undefined, lastCheckedConnectionGeneration: undefined,
      }];
      if (item.status === 'accepted' && item.connectionGeneration !== connection.generation) {
        // An accepted edit's original text is still needed until the composer
        // restores it. Reconnecting only discards disposable steer previews.
        if (item.kind === 'edit' && !item.handled) return [{ ...item, connectionGeneration: connection.generation }];
        return [];
      }
      const delivery = item.receipt?.promptDelivery;
      if (item.kind === 'send' && delivery) {
        if (projection.messages.some(message => message.userKind === 'steer'
          && message.inputSource?.commandId === item.commandId
          && message.inputSource.queueItemId === delivery.queueItemId)) return [];
      }
      if (item.kind !== 'send' && item.status === 'accepted' && item.handled
        && !projection.queuedPrompts.some(prompt => prompt.queueItemId === item.source.queueItemId)) return [];
      return [item];
    }));
  }, [connection, ownsConnection, projection]);

  useEffect(() => {
    if (!connection || !ownsConnection(connection)) return;
    const queryKey = (item: QueuedPromptAction) => `queue-action:${item.sessionId}:${connection.generation}:${item.commandId}`;
    const candidate = queueActions.find(item => item.sessionId === connection.sessionId && (
      item.status === 'unknown' || (item.kind === 'send' && item.status === 'accepted'
        && !projection.queuedPrompts.some(prompt => prompt.commandId === item.commandId
          && prompt.queueItemId === item.receipt?.promptDelivery?.queueItemId)
        && !projection.messages.some(message => message.userKind === 'steer'
          && message.inputSource?.commandId === item.commandId
          && message.inputSource.queueItemId === item.receipt?.promptDelivery?.queueItemId))
    ) && !promptReconciliationInFlight.current.has(queryKey(item))
      && (item.lastCheckedEventSequence !== projection.eventSequence || item.lastCheckedConnectionGeneration !== connection.generation));
    if (!candidate) return;
    const key = queryKey(candidate);
    promptReconciliationInFlight.current.add(key);
    setQueueActions(current => current.map(item => item === candidate ? {
      ...item, lastCheckedEventSequence: projection.eventSequence, lastCheckedConnectionGeneration: connection.generation,
    } : item));
    void (async () => {
      try {
        const receipt = await connection.queryCommand(candidate.commandId);
        if (!ownsConnection(connection)) return;
        if (receipt) {
          settleQueueAction(candidate, receipt, connection);
          return;
        }
        // Rejections happen before the action command is inserted. A missing
        // command alone is not proof, but a consumed exact NEW_TURN source can
        // never subsequently be cancelled/redirected by this action's CAS.
        const sourceConsumed = connection.current().messages.some(message => (
          message.userKind === 'prompt'
          && message.inputSource?.commandId === candidate.source.commandId
          && message.inputSource.queueItemId === candidate.source.queueItemId
          && message.inputSource.deliveryMode === 'new-turn'
        ));
        const sourceReceipt = sourceConsumed ? undefined : await connection.queryCommand(candidate.source.commandId);
        if (!ownsConnection(connection)) return;
        const delivery = sourceReceipt?.promptDelivery;
        if (!sourceConsumed && !(sourceReceipt?.commandId === candidate.source.commandId
          && delivery?.queueItemId === candidate.source.queueItemId
          && delivery.deliveryMode === 'new-turn' && delivery.queueStatus === 'CONSUMED')) return;
        setQueueActions(current => current.map(item => item.commandId === candidate.commandId && item.sessionId === candidate.sessionId
          ? { ...item, status: 'rejected', connectionGeneration: connection.generation }
          : item));
        queueActionsInFlight.current.delete(`${candidate.sessionId}:${candidate.source.queueItemId}`);
        notify('操作未完成', '这条输入已开始处理，无法再编辑、删除或改为引导。', 'warning');
      } catch {
        // Keep unknown facts unknown; only query again on a new cut/connection.
      } finally {
        promptReconciliationInFlight.current.delete(key);
        if (ownsConnection(connection)) {
          // A newer cut may have arrived while this exact query was in flight.
          // Reconsider it without concurrent queries or mutating resubmission.
          setQueueActions(current => [...current]);
        }
      }
    })();
  }, [connection, notify, ownsConnection, projection, queueActions, settleQueueAction]);

  const sendPrompt = async (
    content: EditablePromptContent,
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
    const commandId = `command:web:${crypto.randomUUID()}`;
    const deliveryMode = 'new-turn' as const;
    try {
      if (requestPlan && projection.isRunning) {
        notify('当前运行结束后才能先规划', '先规划只作用于一条尚未开始的新输入。', 'warning');
        return false;
      }
      if (requestPlan && !projection.planMode) {
        const text = promptContentTextProjection(content);
        const plan = await active.enterPlan(
          text || '用户提交了一条图片输入。',
          permission,
        );
        if (!ownsConnection(active)) return false;
        if (plan.status === 'rejected') {
          notify('无法为本轮启用规划', productMessage(plan.publicMessage, '请稍后重试。'), 'warning');
          return false;
        }
      }
      setLocalSubmissions((current) => [...current, {
        sessionId: active.sessionId,
        connectionGeneration: active.generation,
        commandId,
        content,
        deliveryMode,
        permission,
        status: 'sending',
        displayAsMessage: !projection.isRunning && projection.queuedCount === 0
          && !localSubmissions.some(item => item.sessionId === active.sessionId
            && ['sending', 'synchronizing', 'queued', 'unknown'].includes(item.status)),
      }]);
      const receipt = await active.submitPrompt(commandId, content, permission);
      if (!ownsConnection(active)) return false;
      setLocalSubmissions((current) => current.map((item) => item.commandId === commandId
        ? submissionFromReceipt(item, receipt)
        : item));
      notifyPromptReceipt(
        receipt,
        requestPlan ? '本轮将先制定计划' : projection.isRunning ? '输入将在下一轮处理' : '输入已接受',
        projection.isRunning ? '会在当前轮完成后自动开始。' : '已送达本地服务。',
      );
      return promptWasAccepted(receipt);
    } catch (error) {
      if (!ownsConnection(active)) return false;
      if (error instanceof RuntimeApiError && (
        error.code === 'HTTP_413'
        || error.code === 'PROTOCOL_FRAME_OUT_OF_BOUNDS'
      )) {
        setLocalSubmissions((current) => current.map((item) => item.commandId === commandId
          ? {
            ...item,
            status: 'rejected',
            outcomeCode: error.code,
            detail: error.message,
          }
          : item));
        notify(
          '输入超过上传容量',
          productMessage(
            error.message,
            '图片和文字编码后的完整请求超过当前 8 MiB 上传容量。',
          ),
          'warning',
        );
        return false;
      }
      const recoveredConnection = await recoverConnectionAfterOperation(error, active);
      if (!recoveredConnection || !ownsConnection(recoveredConnection)) return false;
      try {
        const recovered = await recoveredConnection.queryCommand(commandId);
        if (!ownsConnection(recoveredConnection)) return false;
        if (recovered) {
          setLocalSubmissions((current) => current.map((item) => item.commandId === commandId
            ? submissionFromReceipt(item, recovered)
            : item));
          notifyPromptReceipt(recovered, '输入已接受', '正在同步本地服务中的精确队列状态。');
          const next = await recoveredConnection.snapshot();
          if (!ownsConnection(recoveredConnection)) return false;
          publishProjection(next, recoveredConnection);
          return promptWasAccepted(recovered);
        }
      } catch {
        if (!ownsConnection(recoveredConnection)) return false;
        // The original command identity remains visible as unknown; it is never resent.
      }
      setLocalSubmissions((current) => current.map((item) => item.commandId === commandId
        ? {
          ...item,
          status: 'unknown',
          lastCheckedEventSequence: projection.eventSequence,
          lastCheckedConnectionGeneration: recoveredConnection?.generation,
        }
        : item));
      notify('提交状态未知', productMessage(error instanceof Error ? error.message : undefined, '可重新连接查看，Pulsara 不会自动重发。'), 'warning');
      return false;
    }
  };

  const stopRun = async () => {
    const active = connectionRef.current;
    const targetTurnId = projection.activeTurnId;
    if (!active || active.role !== 'controller' || !targetTurnId) return;
    if (!projection.hostSessionId || !projection.controlAdmissionDeadlineMs) {
      notify('暂时无法停止', '当前 Host 的控制身份尚未确认，请等待刷新后重试。', 'warning');
      return;
    }
    const reference = createUserControlCommandRef(
      projection,
      active.sessionId,
      'STOP_ACTIVE_TURN',
      'ROOT_TURN',
      targetTurnId,
    );
    try {
      const receipt = await active.stopActiveTurn(reference);
      if (!ownsConnection(active)) return;
      notify(
        receipt.status === 'rejected' ? '没有可停止的运行' : '停止请求已送达',
        receipt.status === 'rejected' ? '当前没有可停止的任务。' : '正在停止当前任务。',
        receipt.status === 'rejected' ? 'warning' : 'success',
      );
    } catch (error) {
      const recovered = await recoverConnectionAfterOperation(error, active);
      if (!recovered || !ownsConnection(recovered)) return;
      try {
        const query = await recovered.queryControlCommand(reference);
        if (!ownsConnection(recovered)) return;
        const receipt = query.receipt;
        if (query.status === 'FOUND' && receipt) {
          notify(
            receipt.status === 'pending' ? '正在停止本轮运行' : '停止状态已确认',
            receipt.publicMessage || '已按原操作身份恢复精确状态。',
            receipt.status === 'failed' || receipt.status === 'rejected' ? 'warning' : 'success',
          );
          return;
        }
        notify(
          query.status === 'OWNER_UNAVAILABLE' ? '原会话已不可控制' : '停止结果不可确认',
          query.status === 'OWNER_UNAVAILABLE'
            ? '原 Host 已失效；Pulsara 不会把这次操作改投新的 Host。'
            : '原操作结果已不在当前 Host 的保留窗口内；Pulsara 不会自动重发。',
          'warning',
        );
        return;
      } catch {
        if (!ownsConnection(recovered)) return;
      }
      notify('停止结果待确认', productMessage(error instanceof Error ? error.message : undefined, '重新连接后可按原操作身份查询；Pulsara 不会自动重发。'), 'warning');
    }
  };

  const compact = async () => {
    const active = connectionRef.current;
    if (!active || active.role !== 'controller') return;
    try {
      const receipt = await active.compactContext(projection.activeTurnId);
      if (!ownsConnection(active)) return;
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
      const recovered = await recoverConnectionAfterOperation(error, active);
      if (!recovered || !ownsConnection(recovered)) return;
      notify('上下文整理失败', productMessage(error instanceof Error ? error.message : undefined, '请稍后重试。'), 'warning');
    }
  };

  const cancelTask = async (task: AgentTask): Promise<void> => {
    const active = connectionRef.current;
    if (!active || active.role !== 'controller') return;
    if (!projection.hostSessionId || !projection.controlAdmissionDeadlineMs) {
      notify('暂时无法取消', '当前 Host 的控制身份尚未确认，请等待刷新后重试。', 'warning');
      return;
    }
    const reference = createUserControlCommandRef(
      projection,
      active.sessionId,
      'CANCEL_SUBAGENT_TASK',
      'SUBAGENT_TASK',
      task.id,
    );
    try {
      const receipt = await active.cancelSubagentTask(reference);
      if (!ownsConnection(active)) return;
      if (receipt.status === 'rejected') {
        notify(
          '无法取消这项任务',
          productMessage(receipt.publicMessage, '任务身份或当前 Host 已经改变。'),
          'warning',
        );
        return;
      }
      notify(
        receipt.status === 'pending' ? '正在取消任务' : '任务状态已更新',
        receipt.publicMessage || (receipt.status === 'pending'
          ? '已由当前 Host 接管；任务仍可能正在完成已启动操作的收尾。'
          : '已保留任务的真实终态和依赖结果。'),
        receipt.status === 'failed' ? 'warning' : 'success',
      );
      void loadSessionTasks(active.sessionId);
    } catch (error) {
      const recovered = await recoverConnectionAfterOperation(error, active);
      if (!recovered || !ownsConnection(recovered)) return;
      try {
        const query = await recovered.queryControlCommand(reference);
        if (!ownsConnection(recovered)) return;
        const receipt = query.receipt;
        if (query.status === 'FOUND' && receipt) {
          notify(
            receipt.status === 'pending' ? '正在取消任务' : '任务状态已确认',
            receipt.publicMessage || '已按原操作身份恢复精确状态。',
            receipt.status === 'failed' || receipt.status === 'rejected' ? 'warning' : 'success',
          );
          void loadSessionTasks(recovered.sessionId);
          return;
        }
        notify(
          query.status === 'OWNER_UNAVAILABLE' ? '原会话已不可控制' : '取消结果不可确认',
          query.status === 'OWNER_UNAVAILABLE'
            ? '原 Host 已失效；Pulsara 不会把这次取消改投新的 Host。'
            : '原操作结果已不在当前 Host 的保留窗口内；Pulsara 不会自动重发。',
          'warning',
        );
        return;
      } catch {
        if (!ownsConnection(recovered)) return;
      }
      notify(
        '取消结果待确认',
        productMessage(error instanceof Error ? error.message : undefined, '重新连接后可按原操作身份查询；Pulsara 不会自动重发。'),
        'warning',
      );
    }
  };

  const readInteraction = useCallback(async (interaction: RuntimeInteractionSummary) => {
    const active = connectionRef.current;
    if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
    const content = await active.readInteraction(interaction);
    if (!ownsConnection(active)) {
      throw new RuntimeApiError('INTERACTION_OWNER_CHANGED', '这项确认所属的会话已经改变。', true);
    }
    return content;
  }, [ownsConnection]);

  const acceptToolDecision = useCallback((intent: ToolDecisionIntent, receipt: CommandReceipt): boolean => {
    if (receipt.commandId !== intent.commandId
      || receipt.publicCode !== `INTERACTION_${intent.decision.toUpperCase()}`
      || receipt.status !== 'succeeded') return false;
    saveToolDecision({ ...intent, status: 'accepted' });
    return true;
  }, [saveToolDecision]);

  // Only read the original command on a new connection/canonical/live cut.
  // A null query or a false busy projection cannot erase a local unknown intent.
  useEffect(() => {
    if (!connection) return;
    const intents = toolDecisions.filter(item => item.sessionId === connection.sessionId
      && (item.status === 'unknown' || (item.status === 'submitting'
        && item.connectionGeneration !== connection.generation)));
    const cut = `${connection.generation}:${projection.eventSequence}:${projection.liveControlRevision}:${projection.interaction?.id}:${projection.interaction?.kind === 'tool-confirmation' ? projection.interaction.decisionInProgress : ''}`;
    for (const intent of intents) {
      const previous = toolDecisionQueries.current.get(intent.commandId);
      if (previous?.cut === cut || (previous?.generation === connection.generation && previous.inFlight)) continue;
      const attempt = { cut, generation: connection.generation, inFlight: true };
      toolDecisionQueries.current.set(intent.commandId, attempt);
      void connection.queryCommand(intent.commandId).then(receipt => {
        if (receipt) {
          if (acceptToolDecision(intent, receipt) && ownsConnection(connection)) {
            notify(intent.decision === 'allow' ? '已允许本次操作' : '已拒绝本次操作', '决定已接纳；操作结果以实际执行记录为准。', 'success');
          }
          return;
        }
        if (!ownsConnection(connection)) return;
        const current = connection.current();
        if (current.hostSessionId && current.liveControlRevision !== undefined
          && (current.hostSessionId !== intent.hostSessionId || current.interaction?.id !== intent.interactionId)) {
          // The original live confirmation has ended. Retire its empty query,
          // without calling it rejected or reopening any confirmation button.
          saveToolDecision({ ...intent, status: 'retired' });
        }
      }).catch(() => { /* Keep this command unknown; other queries still advance. */ }).finally(() => {
        if (toolDecisionQueries.current.get(intent.commandId) !== attempt) return;
        attempt.inFlight = false;
        const latest = toolDecisionsRef.current.find(item => item.commandId === intent.commandId);
        if (latest && ownsConnection(connection)
          && (latest.status === 'unknown' || (latest.status === 'submitting'
            && latest.connectionGeneration !== connection.generation))) {
          // Reconsider a newer cut that arrived during this query. The same
          // completed cut remains remembered, so an empty read cannot poll itself.
          saveToolDecision({ ...latest, status: 'unknown' });
        }
      });
    }
  }, [connection, toolDecisions, projection.eventSequence, projection.liveControlRevision, projection.interaction, acceptToolDecision, saveToolDecision, ownsConnection, notify]);

  const submitToolDecision = useCallback(async (
    active: RuntimeConnection, interaction: Extract<RuntimeInteractionSummary, { kind: 'tool-confirmation' }>,
    decision: 'allow' | 'deny',
  ): Promise<boolean> => {
    const hostSessionId = active.current().hostSessionId;
    if (!hostSessionId || interaction.decisionInProgress
      || !interaction.expiresAtUtc || Date.parse(interaction.expiresAtUtc) <= Date.now()
      || toolDecisionsRef.current.some(item => item.sessionId === active.sessionId
        && item.hostSessionId === hostSessionId && item.interactionId === interaction.id
        && item.status !== 'rejected')) return false;
    const intent: ToolDecisionIntent = {
      sessionId: active.sessionId, hostSessionId, interactionId: interaction.id,
      commandId: `command:web:${crypto.randomUUID()}`, decision,
      connectionGeneration: active.generation, status: 'submitting',
    };
    saveToolDecision(intent);
    try {
      const receipt = await active.resolveInteraction(interaction, { kind: 'tool', decision, commandId: intent.commandId });
      if ('submitted' in receipt || !acceptToolDecision(intent, receipt)) {
        saveToolDecision({ ...intent, status: 'unknown' });
        return false;
      }
      if (ownsConnection(active)) notify(decision === 'allow' ? '已允许本次操作' : '已拒绝本次操作', '决定已接纳；操作结果以实际执行记录为准。', 'success');
      return true;
    } catch (error) {
      const rejected = error instanceof RuntimeApiError && ['INTERACTION_NOT_ACCEPTED', 'INTERACTION_STALE', 'CONTROLLER_REQUIRED', 'INTERACTION_INVALID'].includes(error.code);
      // A lost HTTP ACK does not prove that the attached Host connection died.
      // Read the saved command before transport cleanup can delay reconciliation.
      if (!rejected && ownsConnection(active)) {
        try {
          const receipt = await active.queryCommand(intent.commandId);
          if (receipt && acceptToolDecision(intent, receipt)) {
            if (ownsConnection(active)) notify(decision === 'allow' ? '已允许本次操作' : '已拒绝本次操作', '决定已接纳；操作结果以实际执行记录为准。', 'success');
            return true;
          }
        } catch { /* The original intent remains blocked through reconnect. */ }
      }
      saveToolDecision({ ...intent, status: rejected ? 'rejected' : 'unknown' });
      const recovered = await recoverConnectionAfterOperation(error, active);
      if (recovered && ownsConnection(recovered)) notify(rejected ? '这项选择没有被接受' : '正在核实这项决定',
        rejected ? '请查看当前确认后重新选择。' : '正在查询原决定，操作结果尚待核实。', 'warning');
      return false;
    }
  }, [acceptToolDecision, saveToolDecision, ownsConnection, notify, recoverConnectionAfterOperation]);

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
    if (resolution.kind === 'tool') {
      return interaction.kind === 'tool-confirmation'
        ? submitToolDecision(active, interaction, resolution.decision) : false;
    }
    try {
      const receipt = await active.resolveInteraction(interaction, resolution);
      if (!ownsConnection(active)) return false;
      if ('submitted' in receipt) {
        if (!receipt.submitted) return false;
        notify(resolution.kind === 'capability' && resolution.decision === 'CANCEL' ? '已取消本次配置' : '配置已提交', 'Pulsara 将继续处理。', 'success');
        return true;
      }
      if (receipt.status === 'rejected') {
        notify('这项选择没有被接受', productMessage(receipt.publicMessage, '内容可能已经更新，请查看最新状态。'), 'warning');
        return false;
      }
      if (resolution.kind === 'plan-draft') {
        if (receipt.planDraftDecision !== resolution.decision) {
          notify('无法确认规划结果', '本地服务返回的决策身份不一致，请刷新后重试。', 'warning');
          return false;
        }
        const copy = {
          approve: ['方案已批准', '已创建按批准方案继续处理的任务。'],
          revise: ['修改意见已提交', '已创建继续修订方案的任务。'],
          cancel: ['规划已取消', '未因本次取消启动这份方案的实施。你可以发送新任务。'],
        } as const;
        notify(copy[receipt.planDraftDecision][0], copy[receipt.planDraftDecision][1], 'success');
        return true;
      }
      const title = '回答已提交';
      notify(title, 'Pulsara 将继续处理。', 'success');
      return true;
    } catch (error) {
      const recovered = await recoverConnectionAfterOperation(error, active);
      if (!recovered || !ownsConnection(recovered)) return false;
      notify('无法完成这项选择', productMessage(error instanceof Error ? error.message : undefined, '内容可能已经更新，请稍后重试。'), 'warning');
      return false;
    }
  }, [notify, ownsConnection, recoverConnectionAfterOperation, submitToolDecision]);

  const installDeviceSkill = useCallback(async (input: SkillImportInput): Promise<boolean> => {
    try {
      const result = await adapter.installUserSkill(input, activeSessionIdRef.current || undefined);
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

  const installDevicePlugin = useCallback(async (sourcePath: string, options?: PluginImportOptions): Promise<boolean> => {
    try {
      const result = await adapter.installUserPlugin(sourcePath, activeSessionIdRef.current || undefined, options);
      setUserCapabilities(result.capabilities);
      setUserCapabilityError(undefined);
      notify(result.operation.success ? '插件已安装' : '插件没有安装', result.operation.message, result.operation.success ? 'success' : 'warning');
      return result.operation.success;
    } catch (error) {
      notify('插件安装失败', productMessage(error instanceof Error ? error.message : undefined, '请检查本地目录后重试。'), 'warning');
      return false;
    }
  }, [adapter, notify]);

  const createDeviceMcp = useCallback(async (input: McpEditInput): Promise<boolean> => {
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

  const importDeviceMcp = useCallback(async (input: McpImportSelection): Promise<boolean> => {
    try {
      const result = await adapter.importUserMcp(input, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      notify(result.operation.success ? 'MCP 已导入' : 'MCP 没有导入', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 导入失败', productMessage(error instanceof Error ? error.message : undefined, '请处理预览中的字段后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const toggleDeviceMcp = useCallback(async (
    server: UserMcpServerCapability,
    enabled: boolean,
  ): Promise<boolean> => {
    try {
      const result = await adapter.updateUserMcp({serverId: server.id, config: {...server.config, enabled}, secretChanges: []}, server.currentIdentity, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      notify(enabled ? 'MCP 服务已开启' : 'MCP 服务已关闭', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const editDeviceMcp = useCallback(async (server: UserMcpServerCapability, input: McpEditInput): Promise<boolean> => {
    try {
      const result = await adapter.updateUserMcp(input, server.currentIdentity, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      notify('MCP 配置已更新', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 配置未更新', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重新打开编辑。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const removeDeviceMcp = useCallback(async (server: UserMcpServerCapability): Promise<boolean> => {
    try {
      const result = await adapter.removeUserMcp(server.id, server.currentIdentity, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      notify('MCP 服务已移除', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 服务未移除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const deviceMcpAuthorization = useCallback(async (server: UserMcpServerCapability, action: 'login' | 'status' | 'cancel' | 'logout') => {
    try {
      const result = await adapter.userMcpAuthorization(server.id, action);
      const labels: Record<string, string> = { idle: '当前没有进行中的登录。', connecting: '正在准备登录，请在浏览器中继续。', awaiting_user: '请在浏览器中完成授权。', authorized: '已完成授权，可以测试连接。', cancelled: '登录已取消。', failed: '登录未完成，请检查客户端配置。' };
      notify('MCP 授权', action === 'logout' ? '本机授权已清除，远端令牌未吊销。' : result.error || labels[result.state] || '登录状态已更新。', result.error ? 'warning' : 'neutral');
    } catch (error) {
      notify('授权操作未完成', productMessage(error instanceof Error ? error.message : undefined, '请检查连接是否已保存。'), 'warning');
    }
  }, [adapter, notify]);

  const removeDeviceSkill = useCallback(async (skill: UserSkillCapability): Promise<boolean> => {
    try {
      const result = await adapter.removeUserSkill(skill, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      notify(result.operation.success ? '技能已删除' : '技能未删除', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('技能未删除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const pluginMcpAuthorization = useCallback(async (plugin: UserPluginCapability, connection: PluginMcpConnection, action: 'login' | 'status' | 'cancel' | 'logout') => {
    try {
      const result = await adapter.pluginMcpAuthorization(plugin, connection, action);
      const labels: Record<string, string> = {idle: '当前没有进行中的登录。', connecting: '正在准备登录，请在浏览器中继续。', awaiting_user: '请在浏览器中完成授权。', authorized: '授权已完成。', cancelled: '登录已取消。', failed: '登录未完成。'};
      notify('插件 MCP 授权', action === 'logout' ? '本机授权已清除，远端令牌未吊销。' : result.error || labels[result.state] || '授权状态已更新。', result.error ? 'warning' : 'neutral');
    } catch (error) { notify('授权操作未完成', productMessage(error instanceof Error ? error.message : undefined, '请刷新连接后重试。'), 'warning'); }
  }, [adapter, notify]);

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
        plugin.connectionReview ?? [],
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
      const result = await adapter.removeUserPlugin(plugin.id, plugin.packageInstallId, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      notify(result.operation.success ? '插件已移除' : '插件没有移除', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('插件没有移除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, loadCapabilities, notify]);

  const editPluginConnection = useCallback(async (
    plugin: UserPluginCapability,
    connection: import('../lib/pulsara-types').PluginMcpConnection,
    input: import('../lib/pulsara-types').PluginMcpEditInput,
  ): Promise<boolean> => {
    try {
      const result = await adapter.updatePluginConnection(plugin, connection, input, activeSessionIdRef.current || undefined);
      setUserCapabilities(result.capabilities);
      notify('插件连接', result.operation.message, result.operation.success ? 'success' : 'warning');
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('插件连接未保存', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
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

  const installProjectSkill = useCallback(async (input: SkillImportInput): Promise<void> => {
    const sessionId = activeSessionIdRef.current;
    if (!sessionId) throw new Error('请先打开一个会话。');
    setCapabilityBusy('正在添加技能…');
    try {
      const result = await adapter.installSkill(sessionId, input);
      adoptCapabilityMutation(sessionId, result.capabilities);
      if (!result.installation.installed) throw new Error(result.installation.message);
      notify('项目技能已添加', '同一目录中的会话会在下次模型请求前的安全时机载入。', 'success');
    } finally {
      setCapabilityBusy(undefined);
    }
  }, [adapter, adoptCapabilityMutation, notify]);

  const toggleProjectSkill = useCallback(async (
    skill: SkillCapability,
    enabled: boolean,
  ): Promise<void> => {
    const sessionId = activeSessionIdRef.current;
    if (!sessionId) return;
    setCapabilityBusy(enabled ? '正在开启技能…' : '正在关闭技能…');
    try {
      const result = await adapter.setProjectSkillEnabled(sessionId, skill.id, enabled);
      adoptCapabilityMutation(sessionId, result.capabilities);
      notify(enabled ? '项目技能已开启' : '项目技能已关闭', result.operation.message, 'success');
    } catch (error) {
      notify('技能状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
    } finally {
      setCapabilityBusy(undefined);
    }
  }, [adapter, adoptCapabilityMutation, notify]);

  const removeProjectSkill = useCallback(async (skill: SkillCapability): Promise<void> => {
    const sessionId = activeSessionIdRef.current;
    if (!sessionId || !skill.removalIdentity) return;
    setCapabilityBusy('正在删除项目技能…');
    try {
      const result = await adapter.removeProjectSkill(sessionId, skill);
      adoptCapabilityMutation(sessionId, result.capabilities);
      notify(result.operation.success ? '技能已删除' : '技能未删除', result.operation.message, result.operation.success ? 'success' : 'warning');
    } catch (error) {
      notify('技能未删除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      throw error;
    } finally { setCapabilityBusy(undefined); }
  }, [adapter, adoptCapabilityMutation, notify]);

  const createProjectMcp = useCallback(async (input: McpEditInput): Promise<void> => {
    const sessionId = activeSessionIdRef.current;
    if (!sessionId) throw new Error('请先打开一个会话。');
    setCapabilityBusy('正在添加 MCP…');
    try {
      const result = await adapter.createProjectMcp(sessionId, input);
      adoptCapabilityMutation(sessionId, result.capabilities);
      if (!result.operation.success) throw new Error(result.operation.message);
      notify('项目 MCP 已添加', result.operation.message, 'success');
    } finally {
      setCapabilityBusy(undefined);
    }
  }, [adapter, adoptCapabilityMutation, notify]);

  const editProjectMcp = useCallback(async (server: McpServerCapability, input: McpEditInput): Promise<void> => {
    const sessionId = activeSessionIdRef.current;
    if (!sessionId || !server.configIdentity) throw new Error('请重新打开项目连接。');
    setCapabilityBusy('正在保存 MCP…');
    try {
      const result = await adapter.updateProjectMcp(sessionId, input, server.configIdentity);
      adoptCapabilityMutation(sessionId, result.capabilities);
      if (!result.operation.success) throw new Error(result.operation.message);
      notify('项目 MCP 已保存', result.operation.message, 'success');
    } finally { setCapabilityBusy(undefined); }
  }, [adapter, adoptCapabilityMutation, notify]);

  const toggleProjectMcp = useCallback(async (
    server: McpServerCapability,
    enabled: boolean,
  ): Promise<void> => {
    const sessionId = activeSessionIdRef.current;
    if (!sessionId) return;
    if (!server.configIdentity) {
      notify('MCP 状态没有改变', '能力信息已经更新，请刷新后重试。', 'warning');
      void loadCapabilities(sessionId);
      return;
    }
    setCapabilityBusy(enabled ? '正在开启 MCP…' : '正在关闭 MCP…');
    try {
      const result = await adapter.setProjectMcpEnabled(
        sessionId,
        server.id,
        server.configIdentity,
        enabled,
      );
      adoptCapabilityMutation(sessionId, result.capabilities);
      notify(enabled ? '项目 MCP 已开启' : '项目 MCP 已关闭', result.operation.message, 'success');
    } catch (error) {
      notify('MCP 状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      await loadCapabilities(sessionId);
    } finally {
      setCapabilityBusy(undefined);
    }
  }, [adapter, adoptCapabilityMutation, loadCapabilities, notify]);

  const removeProjectMcp = useCallback(async (server: McpServerCapability): Promise<void> => {
    if (!window.confirm(`从这个目录移除“${server.name}”？`)) return;
    const sessionId = activeSessionIdRef.current;
    if (!sessionId) return;
    if (!server.configIdentity) {
      notify('MCP 没有移除', '能力信息已经更新，请刷新后重试。', 'warning');
      void loadCapabilities(sessionId);
      return;
    }
    setCapabilityBusy('正在移除 MCP…');
    try {
      const result = await adapter.removeProjectMcp(
        sessionId,
        server.id,
        server.configIdentity,
      );
      adoptCapabilityMutation(sessionId, result.capabilities);
      notify('项目 MCP 已移除', result.operation.message, 'success');
    } catch (error) {
      notify('MCP 没有移除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      await loadCapabilities(sessionId);
    } finally {
      setCapabilityBusy(undefined);
    }
  }, [adapter, adoptCapabilityMutation, loadCapabilities, notify]);

  const reconnectProjectMcp = useCallback(async (server: McpServerCapability): Promise<void> => {
    const sessionId = activeSessionIdRef.current;
    if (!sessionId) return;
    setCapabilityBusy('正在重新连接…');
    try {
      const next = await adapter.reconnectMcpServer(sessionId, server.id);
      adoptCapabilityMutation(sessionId, next);
      notify('已经请求重新连接', '连接状态会在发现完成后更新。', 'success');
    } catch (error) {
      notify('没有重新连接', productMessage(error instanceof Error ? error.message : undefined, '请稍后重试。'), 'warning');
    } finally {
      setCapabilityBusy(undefined);
    }
  }, [adapter, adoptCapabilityMutation, notify]);

  const readToolArtifact = useCallback(async (resultEntryId: string, offsetChars: number) => {
    const active = connectionRef.current;
    if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
    const page = await active.readToolArtifact(resultEntryId, offsetChars);
    if (!ownsConnection(active)) {
      throw new RuntimeApiError('TOOL_ARTIFACT_OWNER_CHANGED', '工具输出所属的会话已经改变。', true);
    }
    return page;
  }, [ownsConnection]);

  const readPromptImage = useCallback(async (image: CanonicalPromptImagePart) => {
    const active = connectionRef.current;
    if (!active) {
      throw new RuntimeApiError(
        'LOCAL_CONNECTION_UNAVAILABLE',
        '本地服务未连接。',
        true,
      );
    }
    const bytes = await active.readPromptImage(image);
    if (!ownsConnection(active)) {
      throw new RuntimeApiError(
        'PROMPT_IMAGE_OWNER_CHANGED',
        '图片所属的会话已经改变。',
        true,
      );
    }
    return bytes;
  }, [ownsConnection]);

  return (
    <ToolResultDisplayContext.Provider value={{ showBuiltinToolResults, onChange: setShowBuiltinToolResults }}>
    <main className={`pulsara-shell${activeView === 'workbench' ? ' is-workbench' : ' is-surface'}${inspectorOpen && !databaseBlocked ? ' has-inspector' : ''}`}>
      <ActivityRail activeView={activeView} onNavigate={navigate} onOpenCommand={() => setCommandOpen(true)} />

      {activeView === 'workbench' && !databaseBlocked && (
        <SessionSidebar
          workspace={activeWorkspace}
          sessions={sessionList}
          activeSessionId={activeSessionId}
          runtimeStatus={runtimeStatus}
          connectionRole={connection?.role}
          isOpen={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
          onSelectSession={openSession}
          onNewSession={openNewSession}
          canCreateSession={canCreateSession}
          onOpenCommand={() => setCommandOpen(true)}
          onTakeControl={() => activeSessionId && void openRuntimeSession(activeSessionId, true, true)}
        />
      )}

      {activeView === 'overview' && (
        <OverviewView
          sessions={sessionList}
          activeSessionId={activeSessionId}
          runtimeStatus={runtimeStatus}
          databaseState={databaseState}
          agentTasks={mergedProjection.agentTasks}
          localSettings={bootstrap?.local_settings}
          modelConfigurations={bootstrap?.model_configurations ?? []}
          onNavigate={navigate}
          onOpenSession={openSession}
          onNewSession={openNewSession}
          canCreateSession={canCreateSession}
        />
      )}
      {activeView === 'workbench' && !databaseBlocked && (
        <WorkbenchView
          workspace={activeWorkspace}
          session={activeSession}
          messages={renderedMessages}
          contextCompaction={mergedProjection.contextCompaction}
          initialContextBase={mergedProjection.initialContextBase}
          onFork={forkConversation}
          todo={projection.todo}
          activePlanMode={projection.planMode}
          isRunning={projection.isRunning}
          inspectorOpen={inspectorOpen}
          queuedCount={projection.queuedCount}
          queuedPrompts={projection.queuedPrompts}
          queueActions={queueActions.filter(item => item.sessionId === activeSession.id && item.connectionGeneration === connection?.generation)}
          onQueueAction={actOnQueuedPrompt}
          onQueueActionHandled={(commandId) => setQueueActions(current => current.map(item => item.commandId === commandId ? { ...item, handled: true } : item))}
          localSubmissions={localSubmissions.filter((item) => (
            item.sessionId === activeSession.id
            && !item.handledByQueueAction
            && item.outcomeCode !== 'USER_REDIRECTED_TO_STEER'
            && !queueActions.some(action => action.status === 'accepted' && action.source.commandId === item.commandId)
            && (item.displayAsMessage || !projection.queuedPrompts.some((queued) => queued.commandId === item.commandId))
            && !projection.messages.some((message) => message.inputSource?.commandId === item.commandId)
          ))}
          runtimeStatus={runtimeStatus}
          runtimeError={runtimeError}
          modelConfigurations={bootstrap?.model_configurations ?? []}
          modelCallBinding={activeSession.modelCallBinding}
          interaction={projection.interaction}
          toolDecisionPending={toolDecisions.some(item => item.sessionId === connection?.sessionId
            && item.hostSessionId === projection.hostSessionId && item.interactionId === projection.interaction?.id
            && item.status !== 'rejected')}
          canControl={canControl}
          isObserver={isObserver}
          skills={(capabilities?.skills.items ?? []).filter(
            (skill) => skill.enabled && skill.effective,
          )}
          focusTaskId={undefined}
          focusMemoryEntry={focusMemoryEntry}
          focusTaskRevision={0}
          focusTaskHighlighted={false}
          onReconnect={reconnect}
          onTakeControl={() => activeSessionId && void openRuntimeSession(activeSessionId, true, true)}
          onOpenSidebar={() => setSidebarOpen(true)}
          onNewSession={openNewSession}
          canCreateSession={canCreateSession}
          onToggleInspector={() => setInspectorOpen((value) => !value)}
          onOpenModelSettings={() => setActiveView('settings')}
          onModelCallBindingChange={updateModelCallBinding}
          onSend={sendPrompt}
          onStop={() => void stopRun()}
          onCompact={compact}
          onReadInteraction={readInteraction}
          onResolveInteraction={resolveInteraction}
          artifactOwnerKey={`${connection?.sessionId ?? ''}:${connection?.generation ?? 0}`}
          onReadToolArtifact={readToolArtifact}
          onReadPromptImage={readPromptImage}
          promptDraftStore={promptDraftStore}
          onNotify={notify}
          permission={turnPermission}
          onPermissionChange={setTurnPermission}
        />
      )}
      {activeView === 'workbench' && !databaseBlocked && inspectorOpen && <button className="inspector-scrim" aria-label="收起详情侧栏" onClick={() => setInspectorOpen(false)} />}
      {activeView === 'workbench' && !databaseBlocked && (
        <InspectorPanel
          projectMcpForms={{
            preview: (input) => adapter.previewMcpImport(input),
            test: (input) => adapter.testProjectMcp(activeSessionId, input),
            import: async (input) => {
              const result = await adapter.importProjectMcp(activeSessionId, input);
              adoptCapabilityMutation(activeSessionId, result.capabilities);
              notify(result.operation.success ? '项目 MCP 已导入' : 'MCP 未导入', result.operation.message, result.operation.success ? 'success' : 'warning');
              return result.operation.success;
            },
            authorize: async (server, action) => {
              try {
                const result = await adapter.projectMcpAuthorization(activeSessionId, server.id, action);
                const labels: Record<string, string> = {idle: '当前没有进行中的登录。', connecting: '正在准备登录，请在浏览器中继续。', awaiting_user: '请在浏览器中完成授权。', authorized: '已完成授权，可以测试连接。', cancelled: '登录已取消。', failed: '登录未完成，请检查客户端配置。'};
                notify('项目 MCP 授权', action === 'logout' ? '本机授权已清除，远端令牌未吊销。' : result.error || labels[result.state] || '登录状态已更新。', result.error ? 'warning' : 'neutral');
              } catch (error) { notify('授权操作未完成', productMessage(error instanceof Error ? error.message : undefined, '请检查连接是否已保存。'), 'warning'); }
            },
          }}
          session={activeSession}
          isOpen={inspectorOpen}
          agentTasks={mergedProjection.agentTasks}
          taskActivities={taskActivities}
          taskArtifactOwnerKey={`${connection?.sessionId ?? ''}:${connection?.generation ?? 0}`}
          onReadToolArtifact={readToolArtifact}
          onLoadTaskActivities={async (taskId, cursor) => {
            const sessionId = activeSession.id;
            const page = await adapter.listSessionTaskActivities(sessionId, taskId, cursor);
            if (activeSessionIdRef.current !== sessionId) {
              throw new RuntimeApiError('TASK_ACTIVITY_OWNER_CHANGED', '任务活动所属的会话已经改变。', true);
            }
            const active = connectionRef.current;
            if (!active || active.sessionId !== sessionId) {
              throw new RuntimeApiError('TASK_ACTIVITY_OWNER_CHANGED', '任务活动所属的会话已经改变。', true);
            }
            const activities = [];
            for (const activity of page.activities) {
              if (activity.body !== undefined || activity.contentSize === 0) {
                activities.push({ ...activity, body: activity.body ?? '' });
                continue;
              }
              const body = await active.readCanonicalEntryContent(
                activity.entryId,
                activity.contentDigest,
                activity.contentSize,
              );
              if (!ownsConnection(active)) {
                throw new RuntimeApiError('TASK_ACTIVITY_OWNER_CHANGED', '任务活动所属的会话已经改变。', true);
              }
              activities.push({ ...activity, body });
            }
            return { ...page, activities };
          }}
          loading={taskInventoryLoading}
          canControl={canControl}
          capabilities={capabilities}
          capabilityLoading={capabilityLoading}
          capabilityError={capabilityError}
          capabilityBusy={capabilityBusy}
          error={taskInventoryError}
          onRetry={() => activeSessionId && void loadSessionTasks(activeSessionId)}
          onCancelTask={cancelTask}
          backgroundOwnerKey={`${connection?.sessionId ?? ''}:${connection?.generation ?? 0}:${projection.hostSessionId ?? ''}`}
          backgroundHostSessionId={projection.hostSessionId}
          backgroundControlAdmissionDeadlineMs={projection.controlAdmissionDeadlineMs}
          onLoadBackgroundProcesses={async (cursor) => {
            const active = connectionRef.current;
            if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
            const page = await active.listBackgroundProcesses(cursor);
            if (!ownsConnection(active)) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '后台命令所属的会话已经改变。', true);
            return page;
          }}
          onReadBackgroundProcessLog={async (processId, cursor) => {
            const active = connectionRef.current;
            if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
            const page = await active.readBackgroundProcessLog(processId, cursor);
            if (!ownsConnection(active)) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '后台命令所属的会话已经改变。', true);
            return page;
          }}
          onTerminateBackgroundProcess={async (reference) => {
            const active = connectionRef.current;
            if (!active || active.role !== 'controller') throw new RuntimeApiError('CONTROL_UNAVAILABLE', '当前窗口没有控制权限。', false);
            const receipt = await active.terminateBackgroundProcess(reference);
            if (!ownsConnection(active)) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '后台命令所属的会话已经改变。', true);
            return receipt;
          }}
          onQueryControl={async (reference) => {
            const active = connectionRef.current;
            if (!active || active.role !== 'controller') {
              throw new RuntimeApiError('CONTROL_UNAVAILABLE', '当前窗口没有控制权限。', false);
            }
            const query = await active.queryControlCommand(reference);
            if (!ownsConnection(active)) {
              throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '后台命令所属的会话已经改变。', true);
            }
            return query;
          }}
          onRetryCapabilities={() => activeSessionId && void loadCapabilities(activeSessionId)}
          onToggleProjectSkill={toggleProjectSkill}
          onInstallProjectSkill={installProjectSkill}
          onPreviewProjectSkills={(source) => adapter.previewSkillImport(source)}
          onRemoveProjectSkill={removeProjectSkill}
          onCreateProjectMcp={createProjectMcp}
          onEditProjectMcp={editProjectMcp}
          onToggleProjectMcp={toggleProjectMcp}
          onRemoveProjectMcp={removeProjectMcp}
          onReconnectProjectMcp={reconnectProjectMcp}
          onOpenUserCapabilities={() => setActiveView('capabilities')}
          onNotify={notify}
          onClose={() => setInspectorOpen(false)}
        />
      )}
      {activeView === 'workbench' && databaseBlocked && databaseState && (
        <div className="database-workbench-mask">
          <DatabaseSetupGuide
            state={databaseState}
            variant="overlay"
            onOpenSettings={() => navigate('settings')}
          />
        </div>
      )}
      {activeView === 'capabilities' && (
        <CapabilityView
          snapshot={userCapabilities}
          loading={userCapabilityLoading}
          error={userCapabilityError}
          onRefresh={() => loadUserCapabilities(true)}
          onOpenRoot={openCapabilityRoot}
          onInstallSkill={installDeviceSkill}
          onPreviewSkills={(sourcePath) => adapter.previewSkillImport(sourcePath)}
          onInstallPlugin={installDevicePlugin}
          onPreviewPlugin={(path) => adapter.previewPluginImport(path)}
          onCreateMcp={createDeviceMcp}
          onTestMcp={(input) => adapter.testUserMcp(input)}
          onPreviewMcpImport={(input) => adapter.previewMcpImport(input)}
          onImportMcp={importDeviceMcp}
          onEditMcp={editDeviceMcp}
          onRemoveMcp={removeDeviceMcp}
          onMcpAuthorization={deviceMcpAuthorization}
          onToggleSkill={toggleDeviceSkill}
          onRemoveSkill={removeDeviceSkill}
          onToggleMcp={toggleDeviceMcp}
          onTogglePlugin={toggleDevicePlugin}
          onRemovePlugin={removeDevicePlugin}
          onEditPluginConnection={editPluginConnection}
          onPluginMcpAuthorization={pluginMcpAuthorization}
        />
      )}
      {activeView === 'settings' && (
        <SettingsView
          theme={theme}
          bootstrap={bootstrap}
          runtimeStatus={runtimeStatus}
          adapter={adapter}
          onThemeChange={setTheme}
          onConfigurationChanged={refreshConfiguration}
          onNotify={notify}
        />
      )}

      {activeView === 'memory' && <MemoryView api={adapter.memory} databaseState={databaseState} runtimeStatus={runtimeStatus} onReconnect={reconnect} onOpenSettings={() => navigate('settings')} onOpenSource={source => { setFocusMemoryEntry({ sessionId: source.session_id, entryId: source.entry_id }); openSession(source.session_id); }} />}

      <CommandPalette
        open={commandOpen}
        theme={theme}
        onClose={() => setCommandOpen(false)}
        onNavigate={navigate}
        onNewSession={openNewSession}
        canCreateSession={canCreateSession}
        onThemeChange={setTheme}
      />
      <NewSessionDialog
        open={newSessionOpen}
        canCreateSession={canCreateSession}
        defaultWorkspacePath={workspace.path === '正在连接…' ? '' : workspace.path}
        onClose={() => setNewSessionOpen(false)}
        onCreate={createSession}
      />
      <ToastStack
        toasts={toasts}
        onDismiss={(id) => setToasts((current) => current.filter((toast) => toast.id !== id))}
      />
    </main>
    </ToolResultDisplayContext.Provider>
  );
}
