'use client';

import { FilePreviewProvider } from '../components/file-preview-dialog';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityRail } from '../components/activity-rail';
import { CapabilityView } from '../components/capability-view';
import { DatabaseSetupGuide } from '../components/database-setup-guide';
import { LocalScheduledTasksApi } from '../lib/scheduled-tasks-api';
import { ScheduledTasksView } from '../components/scheduled-tasks-view';
import { MemoryView } from '../components/memory-view';
import { InspectorPanel } from '../components/inspector-panel';
import { NewSessionDialog, ToastStack } from '../components/overlays';
import { OverviewView } from '../components/overview-view';
import { SessionSidebar } from '../components/session-sidebar';
import { SessionOpeningView } from '../components/session-opening-view';
import { SessionSearchDialog } from '../components/session-search-dialog';
import { SessionRenameDialog } from '../components/session-rename-dialog';
import { SessionDeletionDialog } from '../components/session-deletion-dialog';
import { SettingsView, type SettingsSection } from '../components/settings-view';
import { WorkbenchView } from '../components/workbench-view';
import { ToolResultDisplayContext, readSavedToolResultDisplay } from '../lib/tool-result-display';
import { PromptDraftStore } from '../lib/prompt-draft';
import {
  LocalHttpRuntimeAdapter,
  CanonicalHistoryView,
  projectRootStatus,
  type WorkspaceAvailability,
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
  type AgentTaskGroup,
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
  name: '尚未选择工作目录',
  path: '',
  kind: 'project',
};

const emptySession: SessionSummary = {
  id: '',
  title: '尚未选择会话',
  subtitle: '创建一个任务，或从左侧恢复已有会话',
  status: null,
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
  const scheduledApi = useMemo(() => new LocalScheduledTasksApi(), []);
  const [activeView, setActiveView] = useState<AppView>('workbench');
  const [settingsInitialSection, setSettingsInitialSection] = useState<SettingsSection>();
  const [settingsHighlightHome, setSettingsHighlightHome] = useState(false);
  const [bootstrap, setBootstrap] = useState<RuntimeBootstrap>();
  const [sessionList, setSessionList] = useState<SessionSummary[]>([]);
  const [sessionRevision, setSessionRevision] = useState(0);
  const archivingSession = useRef<string | undefined>(undefined);
  const [renameTarget, setRenameTarget] = useState<SessionSummary>();
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary>();
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string>();
  const deletingSession = useRef<string | undefined>(undefined);
  const requestedSession = useRef('');
  const [activeSessionId, setActiveSessionId] = useState('');
  // Selection feedback is local UI state; only a connected runtime becomes active.
  const [openingSessionId, setOpeningSessionId] = useState('');
  const [focusSourceEntry, setFocusSourceEntry] = useState<{ sessionId: string; entryId: string }>();
  const [projection, setProjection] = useState<RuntimeProjection>(emptyProjection);
  const [taskInventory, setTaskInventory] = useState<AgentTask[]>([]);
  const [taskGroups, setTaskGroups] = useState<AgentTaskGroup[]>([]);
  const [taskGroupCursor, setTaskGroupCursor] = useState<string>();
  const [taskGroupTotal, setTaskGroupTotal] = useState(0);
  const [loadedTaskGroups, setLoadedTaskGroups] = useState<ReadonlySet<string>>(new Set());
  const taskRowWatermarks = useRef(new Map<string, number>());
  const taskGroupWatermarks = useRef(new Map<string, number>());
  const taskTotalWatermark = useRef(-1);
  const taskPageWatermark = useRef(-1);
  const taskInventoryRef = useRef(taskInventory);
  taskInventoryRef.current = taskInventory;
  const [taskInventorySessionId, setTaskInventorySessionId] = useState('');
  const [taskInventoryLoading, setTaskInventoryLoading] = useState(false);
  const [taskInventoryError, setTaskInventoryError] = useState<string>();
  const taskInventoryAttempt = useRef(0);
  const taskReadOwner = useRef({});
  const taskLastPageCursor = useRef<string | undefined>(undefined);
  const taskPageRequest = useRef<object | undefined>(undefined);
  const openTaskGroup = useRef<string | undefined>(undefined);
  const taskGroupsRef = useRef(taskGroups);
  taskGroupsRef.current = taskGroups;
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
  const historyRef = useRef<CanonicalHistoryView | undefined>(undefined);
  const [workspaceObservation, setWorkspaceObservation] = useState<{sessionId: string; value: WorkspaceAvailability}>();
  const [workspaceRecoveryBusy, setWorkspaceRecoveryBusy] = useState(false);
  const [workspaceRecoveryError, setWorkspaceRecoveryError] = useState<string>();

  const connectionRef = useRef<RuntimeConnection | undefined>(undefined);
  useEffect(() => {
    promptDraftStore.setImporter(async (sessionId, files, directory, signal) => {
      const active = connectionRef.current;
      if (!active || active.sessionId !== sessionId) throw new Error('文件所属会话未连接，请切回该会话后重试。');
      return active.importFiles(files, directory, signal);
    });
  }, [promptDraftStore]);
  const activeSessionIdRef = useRef('');
  const connectionAttempt = useRef(0);
  const promptReconciliationInFlight = useRef(new Set<string>());
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus>('starting');
  const [runtimeError, setRuntimeError] = useState<string>();
  const [runtimeReopenBusy, setRuntimeReopenBusy] = useState(false);
  const runtimeReopenOwner = useRef<RuntimeConnection | undefined>(undefined);
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
  const [searchOpen, setSearchOpen] = useState(false);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  useEffect(() => () => promptDraftStore.destroy(), [promptDraftStore]);
  const [theme, setTheme] = useState<'light' | 'dark'>(readSavedTheme);
  const [showBuiltinToolResults, setShowBuiltinToolResults] = useState(readSavedToolResultDisplay);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const [turnPermission, setTurnPermission] = useState<PermissionMode>('bypass-permissions');
  const databaseState = bootstrap?.database_state;
  const databaseBlocked = databaseState !== undefined && databaseState !== 'ready';
  const canCreateSession = runtimeStatus === 'online' && databaseState === 'ready';

  const ownsHistoryReader = (reader: RuntimeConnection | CanonicalHistoryView): boolean => (
    activeSessionIdRef.current === reader.sessionId
    && (reader instanceof CanonicalHistoryView ? historyRef.current === reader : connectionRef.current === reader)
  );

  const ownsConnection = useCallback((expected: RuntimeConnection): boolean => (
    connectionRef.current === expected
    && activeSessionIdRef.current === expected.sessionId
    && connectionRef.current.generation === expected.generation
  ), []);

  const readSubagentCapacity = useCallback(async () => {
    const active = connectionRef.current;
    if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
    const value = await active.readSubagentCapacity();
    if (!ownsConnection(active)) throw new RuntimeApiError('OWNER_CHANGED', '运行时连接已经改变。', true);
    return value;
  }, [ownsConnection]);

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
          status: projectRootStatus(next.control.latest_root_turn),
          updatedAt: '刚刚',
        }
        : session
    )));
  }, []);

  const mergeTaskTotal = useCallback((total: number, sequence: number) => {
    if (sequence < taskTotalWatermark.current) return;
    taskTotalWatermark.current = sequence;
    setTaskGroupTotal(total);
  }, []);

  const loadSessionTasks = useCallback(async (sessionId: string) => {
    const attempt = ++taskInventoryAttempt.current;
    const owner = taskReadOwner.current;
    setTaskInventoryLoading(true);
    setTaskInventoryError(undefined);
    try {
      const cursor = taskLastPageCursor.current;
      const page = await adapter.listSessionTaskGroups(sessionId, cursor);
      if (owner !== taskReadOwner.current || attempt !== taskInventoryAttempt.current || activeSessionIdRef.current !== sessionId) return;
      setTaskGroups((current) => {
        const byId = new Map(current.map((group) => [group.id, group]));
        for (const group of page.groups) {
          if (page.readEventSequence < (taskGroupWatermarks.current.get(group.id) ?? -1)) continue;
          taskGroupWatermarks.current.set(group.id, page.readEventSequence);
          byId.set(group.id, group);
        }
        return [...byId.values()];
      });
      if (cursor === taskLastPageCursor.current && page.readEventSequence >= taskPageWatermark.current) {
        taskPageWatermark.current = page.readEventSequence;
        setTaskGroupCursor(page.nextCursor);
      }
      mergeTaskTotal(page.totalCount, page.readEventSequence);
      setTaskInventorySessionId(sessionId);
      setTaskInventoryLoading(false);
    } catch (error) {
      if (owner !== taskReadOwner.current || attempt !== taskInventoryAttempt.current || activeSessionIdRef.current !== sessionId) return;
      setTaskInventoryError(productMessage(
        error instanceof Error ? error.message : undefined,
        '任务清单暂时无法读取。',
      ));
      setTaskInventoryLoading(false);
    }
  }, [adapter, mergeTaskTotal]);

  const mergeTaskRows = useCallback((tasks: AgentTask[], readEventSequence: number) => {
    setTaskInventory((current) => {
      const byId = new Map(current.map((task) => [task.id, task]));
      for (const task of tasks) {
        if (readEventSequence < (taskRowWatermarks.current.get(task.id) ?? -1)) continue;
        taskRowWatermarks.current.set(task.id, readEventSequence);
        byId.set(task.id, task);
      }
      return [...byId.values()];
    });
  }, []);

  const loadTask = useCallback(async (taskId: string) => {
    const sessionId = activeSessionIdRef.current;
    const owner = taskReadOwner.current;
    if (!sessionId) throw new RuntimeApiError('TASK_OWNER_CHANGED', '当前没有活动会话。', true);
    const { task } = await adapter.readSessionTask(sessionId, taskId);
    if (activeSessionIdRef.current !== sessionId || taskReadOwner.current !== owner) {
      throw new RuntimeApiError('TASK_OWNER_CHANGED', '任务所属的会话已经改变。', true);
    }
    return task;
  }, [adapter]);

  const loadTaskActivities = useCallback(async (taskId: string, cursor?: string) => {
    const sessionId = activeSessionIdRef.current;
    const active = connectionRef.current ?? historyRef.current;
    if (!sessionId || !active || active.sessionId !== sessionId) throw new RuntimeApiError('TASK_ACTIVITY_OWNER_CHANGED', '任务活动所属的会话已经改变。', true);
    const page = await adapter.listSessionTaskActivities(sessionId, taskId, cursor);
    if (activeSessionIdRef.current !== sessionId || !ownsHistoryReader(active)) {
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
      if (!ownsHistoryReader(active)) {
        throw new RuntimeApiError('TASK_ACTIVITY_OWNER_CHANGED', '任务活动所属的会话已经改变。', true);
      }
      activities.push({ ...activity, body });
    }
    return { ...page, activities };
  }, [adapter, ownsConnection]);

  const loadTaskBackgroundProcesses = useCallback(async (cursor?: string) => {
    const active = connectionRef.current;
    if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
    const page = await active.listBackgroundProcesses(cursor);
    if (!ownsConnection(active)) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '后台命令所属的会话已经改变。', true);
    return page;
  }, [ownsConnection]);

  const loadTaskGroup = useCallback(async (groupId: string) => {
    const sessionId = activeSessionIdRef.current;
    if (!sessionId) return;
    if (openTaskGroup.current !== groupId) {
      setTaskInventory([]);
      setLoadedTaskGroups(new Set());
      taskRowWatermarks.current.clear();
    }
    openTaskGroup.current = groupId;
    const owner = taskReadOwner.current;
    const page = await adapter.listSessionTasks(sessionId, undefined, groupId);
    if (owner !== taskReadOwner.current || openTaskGroup.current !== groupId || activeSessionIdRef.current !== sessionId) return;
    if (page.nextCursor) throw new RuntimeApiError('TASK_GROUP_PAGE_INCOMPLETE', '任务组超过单次接纳边界。', true);
    mergeTaskRows(page.tasks, page.readEventSequence);
    setLoadedTaskGroups((current) => new Set(current).add(groupId));
  }, [adapter, mergeTaskRows]);

  const unloadTaskGroup = useCallback((groupId: string) => {
    if (openTaskGroup.current !== groupId) return;
    openTaskGroup.current = undefined;
    setTaskInventory([]);
    setLoadedTaskGroups(new Set());
    taskRowWatermarks.current.clear();
  }, []);

  const loadMoreTaskGroups = useCallback(async () => {
    const sessionId = activeSessionIdRef.current;
    const cursor = taskGroupCursor;
    if (!sessionId || !cursor || taskPageRequest.current) return;
    const request = {};
    taskPageRequest.current = request;
    const owner = taskReadOwner.current;
    try {
    const page = await adapter.listSessionTaskGroups(sessionId, cursor);
    if (owner !== taskReadOwner.current || activeSessionIdRef.current !== sessionId) return;
    taskLastPageCursor.current = cursor;
    taskPageWatermark.current = page.readEventSequence;
    taskInventoryAttempt.current += 1;
    setTaskInventoryLoading(false);
    setTaskGroups((current) => {
      const byId = new Map(current.map((group) => [group.id, group]));
      for (const group of page.groups) {
        if (page.readEventSequence < (taskGroupWatermarks.current.get(group.id) ?? -1)) continue;
        taskGroupWatermarks.current.set(group.id, page.readEventSequence);
        byId.set(group.id, group);
      }
      return [...byId.values()];
    });
    setTaskGroupCursor(page.nextCursor);
    mergeTaskTotal(page.totalCount, page.readEventSequence);
    } finally {
      if (taskPageRequest.current === request) taskPageRequest.current = undefined;
    }
  }, [adapter, taskGroupCursor, mergeTaskTotal]);

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

  const forgetSession = useCallback((sessionId: string) => {
    setSessionList(current => current.filter(s => s.id !== sessionId));
    promptDraftStore.removeSession(sessionId);
    setLocalSubmissions(current => current.filter(item => item.sessionId !== sessionId));
    setQueueActions(current => current.filter(item => item.sessionId !== sessionId));
    for (const item of toolDecisionsRef.current) {
      if (item.sessionId === sessionId) toolDecisionQueries.current.delete(item.commandId);
    }
    for (const key of promptReconciliationInFlight.current) {
      if (key.startsWith(`${sessionId}:`)) promptReconciliationInFlight.current.delete(key);
    }
    toolDecisionsRef.current = toolDecisionsRef.current.filter(item => item.sessionId !== sessionId);
    setToolDecisions(toolDecisionsRef.current);
    setFocusSourceEntry(current => current?.sessionId === sessionId ? undefined : current);
    if (readSavedSessionId() === sessionId) saveSessionId('');
    if (requestedSession.current !== sessionId) return;
    const previous = connectionRef.current;
    const previousHistory = historyRef.current;
    historyRef.current = undefined;
    if (previousHistory) void previousHistory.close();
    connectionAttempt.current += 1;
    taskInventoryAttempt.current += 1;
    taskReadOwner.current = {};
    taskPageRequest.current = undefined;
    taskLastPageCursor.current = undefined;
    openTaskGroup.current = undefined;
    capabilityAttempt.current += 1;
    connectionRef.current = undefined;
    activeSessionIdRef.current = '';
    requestedSession.current = '';
    setConnection(undefined);
    setActiveSessionId('');
    setOpeningSessionId('');
    setProjection(emptyProjection);
    setTaskInventory([]);
    setTaskGroups([]);
    setTaskGroupCursor(undefined);
    setTaskGroupTotal(0);
    setLoadedTaskGroups(new Set());
    taskRowWatermarks.current.clear();
    taskGroupWatermarks.current.clear();
    taskTotalWatermark.current = -1;
    taskPageWatermark.current = -1;
    setTaskInventorySessionId('');
    setTaskInventoryLoading(false);
    setTaskInventoryError(undefined);
    setCapabilities(undefined);
    setCapabilityLoading(false);
    setCapabilityError(undefined);
    setRuntimeStatus('online');
    setRuntimeError(undefined);
    if (previous) void previous.close().catch(() => {});
  }, [promptDraftStore]);

  const confirmSessionDelete = async () => {
    if (!deleteTarget || deletingSession.current) return;
    const target = deleteTarget;
    deletingSession.current = target.id;
    setDeleteBusy(true);
    setDeleteError(undefined);
    try {
      const outcome = await adapter.deleteSession(target.id);
      if (outcome.session_id !== target.id || !['DELETED', 'ABSENT'].includes(outcome.status)) {
        throw new Error('服务器尚未确认删除结果，请重试确认。');
      }
      forgetSession(target.id);
      setSessionRevision(value => value + 1);
      setDeleteTarget(undefined);
      notify('会话已删除', '记忆、其他分支会话和工作目录已保留。', 'success');
      try { setSessionList(await adapter.listSessions()); } catch { /* deletion is already confirmed */ }
    } catch (error) {
      setDeleteError(productMessage(error instanceof Error ? error.message : undefined, '暂时无法确认删除结果，请重试确认。'));
    } finally {
      deletingSession.current = undefined;
      setDeleteBusy(false);
    }
  };

  const archiveSession = async (target: SessionSummary) => {
    if (archivingSession.current) return;
    archivingSession.current = target.id;
    try {
      const result = await adapter.archiveSession(target.id);
      if (result.status !== 'ARCHIVED' || result.session_id !== target.id) throw new Error('尚未确认归档结果，请刷新查看。');
      forgetSession(target.id);
      setSessionRevision(value => value + 1);
      notify('会话已归档', '可在设置中取消归档；绑定的定时任务已删除，需要重新创建。', 'success');
      try { setSessionList(await adapter.listSessions()); }
      catch { notify('会话已归档', '列表暂时未能刷新，请稍后刷新页面。', 'warning'); }
    } catch (error) {
      notify('会话归档未完成', error instanceof Error ? error.message : '请刷新后重试。', 'warning');
    } finally { archivingSession.current = undefined; }
  };

  const openRuntimeSession = useCallback(async (
    sessionId: string,
    reconnecting = false,
    takeover = false,
  ): Promise<RuntimeConnection | undefined> => {
    if (deletingSession.current === sessionId || archivingSession.current === sessionId) return undefined;
    requestedSession.current = sessionId;
    const attempt = ++connectionAttempt.current;
    setOpeningSessionId(current => current === sessionId ? current : '');
    taskInventoryAttempt.current += 1;
    taskReadOwner.current = {};
    taskPageRequest.current = undefined;
    taskLastPageCursor.current = undefined;
    openTaskGroup.current = undefined;
    capabilityAttempt.current += 1;
    setTaskInventory([]);
    setTaskGroups([]);
    setTaskGroupCursor(undefined);
    setTaskGroupTotal(0);
    setLoadedTaskGroups(new Set());
    taskRowWatermarks.current.clear();
    taskGroupWatermarks.current.clear();
    taskTotalWatermark.current = -1;
    taskPageWatermark.current = -1;
    setTaskInventorySessionId('');
    setTaskInventoryError(undefined);
    setTaskInventoryLoading(Boolean(sessionId));
    setCapabilities(undefined);
    setCapabilityError(undefined);
    setCapabilityLoading(Boolean(sessionId));
    setRuntimeStatus(reconnecting ? 'reconnecting' : 'starting');
    setRuntimeError(undefined);
    const previous = connectionRef.current;
    const previousHistory = historyRef.current;
    const retainedHistory = previousHistory?.sessionId === sessionId ? previousHistory : undefined;
    historyRef.current = retainedHistory;
    connectionRef.current = undefined;
    setConnection(undefined);
    if (!retainedHistory) setWorkspaceObservation(undefined);
    setWorkspaceRecoveryBusy(false);
    setRuntimeReopenBusy(false);
    setWorkspaceRecoveryError(undefined);
    try {
      if (previousHistory && previousHistory !== retainedHistory) await previousHistory.close();
      if (previous) await previous.close();
      if (attempt !== connectionAttempt.current) return undefined;
      const next = await adapter.connect(sessionId, takeover);
      if (attempt !== connectionAttempt.current) {
        await next.close();
        return undefined;
      }
      if (next instanceof CanonicalHistoryView) {
        if (retainedHistory) await retainedHistory.close();
        historyRef.current = next;
        activeSessionIdRef.current = sessionId;
        setActiveSessionId(sessionId);
        setOpeningSessionId('');
        saveSessionId(sessionId);
        setWorkspaceObservation({sessionId, value: next.availability});
        publishProjection(next.current());
        setRuntimeStatus('online');
        setTaskInventoryLoading(false);
        setCapabilityLoading(false);
        return undefined;
      }
      const availability = await adapter.workspaceAvailability(sessionId);
      if (attempt !== connectionAttempt.current) { await next.close(); return undefined; }
      if (retainedHistory) await retainedHistory.close();
      historyRef.current = undefined;
      setWorkspaceObservation({sessionId, value: availability});
      connectionRef.current = next;
      activeSessionIdRef.current = sessionId;
      setConnection(next);
      setActiveSessionId(sessionId);
      setOpeningSessionId('');
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
      try {
        if (!(await adapter.readSession(sessionId)) && attempt === connectionAttempt.current) {
          forgetSession(sessionId);
          setRuntimeStatus('online');
          setRuntimeError(undefined);
          return undefined;
        }
      } catch { /* an unavailable server is not evidence of a missing session */ }
      if (attempt !== connectionAttempt.current) return undefined;
      const message = productMessage(error instanceof Error ? error.message : undefined, '无法连接本地服务。');
      setRuntimeStatus(error instanceof RuntimeApiError && error.retryable ? 'offline' : 'failed');
      setRuntimeError(message);
      if (retainedHistory) {
        setWorkspaceRecoveryError(message);
        void adapter.workspaceAvailability(sessionId).then(value => {
          if (attempt === connectionAttempt.current && historyRef.current === retainedHistory) {
            setWorkspaceObservation({sessionId, value});
          }
        }).catch(() => {});
      }
      setTaskInventoryLoading(false);
      setCapabilityLoading(false);
      return undefined;
    }
  }, [adapter, publishProjection, forgetSession]);

  const recheckWorkspace = useCallback(async () => {
    const sessionId = activeSessionIdRef.current;
    const attempt = connectionAttempt.current;
    if (!sessionId) return;
    setWorkspaceRecoveryError(undefined);
    try {
      const value = await adapter.workspaceAvailability(sessionId);
      if (attempt !== connectionAttempt.current || activeSessionIdRef.current !== sessionId) return;
      setWorkspaceObservation({sessionId, value});
      if (value.outcome === 'AVAILABLE' && !connectionRef.current) await openRuntimeSession(sessionId);
    } catch (error) {
      if (attempt === connectionAttempt.current && activeSessionIdRef.current === sessionId) {
        setWorkspaceRecoveryError(productMessage(error instanceof Error ? error.message : undefined, '无法检查工作目录。'));
      }
    }
  }, [adapter, openRuntimeSession]);

  const restoreWorkspace = useCallback(async () => {
    const sessionId = activeSessionIdRef.current;
    const attempt = connectionAttempt.current;
    if (!sessionId || workspaceRecoveryBusy) return;
    setWorkspaceRecoveryBusy(true);
    setWorkspaceRecoveryError(undefined);
    try {
      const value = await adapter.restoreWorkspace(sessionId);
      if (attempt !== connectionAttempt.current || activeSessionIdRef.current !== sessionId) return;
      setWorkspaceObservation({sessionId, value});
      await openRuntimeSession(sessionId);
    } catch (error) {
      if (attempt === connectionAttempt.current && activeSessionIdRef.current === sessionId) {
        setWorkspaceRecoveryError(productMessage(error instanceof Error ? error.message : undefined, '无法创建工作目录。'));
      }
    } finally {
      if (activeSessionIdRef.current === sessionId) setWorkspaceRecoveryBusy(false);
    }
  }, [adapter, openRuntimeSession, workspaceRecoveryBusy]);

  useEffect(() => {
    if (!activeSessionId) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const check = async () => {
      try {
        const value = await adapter.workspaceAvailability(activeSessionId, controller.signal);
        if (!disposed && activeSessionIdRef.current === activeSessionId) {
          setWorkspaceObservation({sessionId: activeSessionId, value});
          if (value.outcome === 'AVAILABLE' && historyRef.current?.sessionId === activeSessionId) {
            await openRuntimeSession(activeSessionId);
          }
        }
      } catch { /* A read error cannot establish missing or available. */ }
      if (!disposed) timer = setTimeout(check, 30_000);
    };
    void check();
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
  }, [activeSessionId, adapter, openRuntimeSession]);

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

  useEffect(() => {
    const refreshSessions = () => {
      if (document.visibilityState !== 'visible' || databaseState !== 'ready') return;
      void adapter.listSessions().then(sessions => {
        setSessionList(sessions);
        const selected = activeSessionIdRef.current;
        if (selected && deletingSession.current !== selected && !sessions.some(s => s.id === selected)) {
          forgetSession(selected);
        }
      }).catch(() => {});
    };
    window.addEventListener('focus', refreshSessions);
    document.addEventListener('visibilitychange', refreshSessions);
    return () => {
      window.removeEventListener('focus', refreshSessions);
      document.removeEventListener('visibilitychange', refreshSessions);
    };
  }, [adapter, databaseState, forgetSession]);

  const refreshConfiguration = useCallback(async () => {
    const boot = await adapter.bootstrap();
    setBootstrap(boot);
    if (boot.database_state !== 'ready') {
      if (boot.database_state === 'database_restart_required') {
        const previous = connectionRef.current;
        const previousHistory = historyRef.current;
        historyRef.current = undefined;
        if (previousHistory) await previousHistory.close();
        connectionAttempt.current += 1;
        connectionRef.current = undefined;
        activeSessionIdRef.current = '';
        setConnection(undefined);
        setOpeningSessionId('');
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
      const history = historyRef.current;
      historyRef.current = undefined;
      if (history) void history.close();
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
          if (abort.signal.aborted || !active || !ownsConnection(connection)
            || runtimeReopenOwner.current === connection) return;
          setRuntimeStatus('reconnecting');
          setRuntimeError(productMessage(error instanceof Error ? error.message : undefined, '连接已中断。'));
          window.setTimeout(() => {
            if (active && ownsConnection(connection) && runtimeReopenOwner.current !== connection) {
              void openRuntimeSession(connection.sessionId, true);
            }
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

  const refreshTaskGroups = useCallback(async (sessionId: string, ids: readonly string[]) => {
    const owner = taskReadOwner.current;
    const visibleIds = new Set(taskGroupsRef.current.map(group => group.id));
    const visible = [...new Set(ids)].filter(id => visibleIds.has(id));
    const rows = await Promise.all(visible.map(id => adapter.readSessionTaskGroup(sessionId, id)));
    if (owner !== taskReadOwner.current || activeSessionIdRef.current !== sessionId) return;
    for (const row of rows) {
      mergeTaskTotal(row.totalCount, row.readEventSequence);
      setTaskGroups(current => {
        if (row.readEventSequence < (taskGroupWatermarks.current.get(row.group.id) ?? -1)) return current;
        taskGroupWatermarks.current.set(row.group.id, row.readEventSequence);
        return current.map(group => group.id === row.group.id ? row.group : group);
      });
    }
    // Unknown batches affect the overview count, not the full task cache.
    if (ids.some(id => !visibleIds.has(id))) await loadSessionTasks(sessionId);
  }, [adapter, loadSessionTasks, mergeTaskTotal]);

  useEffect(() => {
    const reader = connection ?? historyRef.current;
    if (!activeSessionId || !reader || reader.sessionId !== activeSessionId) return;
    taskReadOwner.current = {};
    taskPageRequest.current = undefined;
    taskRowWatermarks.current.clear();
    taskGroupWatermarks.current.clear();
    taskTotalWatermark.current = -1;
    taskPageWatermark.current = -1;
    const groupId = openTaskGroup.current;
    const visibleIds = taskGroupsRef.current.map(group => group.id);
    const frame = window.requestAnimationFrame(() => {
      void Promise.all([
        loadSessionTasks(activeSessionId),
        refreshTaskGroups(activeSessionId, visibleIds),
        ...(groupId ? [loadTaskGroup(groupId)] : []),
      ]).catch(error => setTaskInventoryError(productMessage(error instanceof Error ? error.message : undefined, '任务清单暂时无法读取。')));
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeSessionId, connection, runtimeStatus, projection.taskSnapshotRevision, loadSessionTasks, refreshTaskGroups, loadTaskGroup]);

  const taskGroupInvalidationKey = (projection.taskGroupInvalidations ?? []).join('|');
  const taskInvalidationKey = (projection.taskInvalidations ?? []).join('|');
  useEffect(() => {
    if (!activeSessionId || !connection || connection.sessionId !== activeSessionId || !taskGroupInvalidationKey) return;
    const owner = taskReadOwner.current;
    const ids = [...new Set(taskGroupInvalidationKey.split('|'))];
    const groupId = openTaskGroup.current;
    const changedTasks = new Set(taskInvalidationKey.split('|'));
    const dependencyChanged = taskInventoryRef.current.some(task => task.dependencyIds.some(id => changedTasks.has(id)));
    void Promise.all([
      refreshTaskGroups(activeSessionId, ids),
      ...(groupId && (ids.includes(groupId) || dependencyChanged) ? [loadTaskGroup(groupId)] : []),
    ]).catch(error => {
      if (owner === taskReadOwner.current && activeSessionIdRef.current === activeSessionId) setTaskInventoryError(productMessage(
        error instanceof Error ? error.message : undefined, '任务变化暂时无法读取。',
      ));
    });
  }, [activeSessionId, connection, refreshTaskGroups, loadTaskGroup, taskGroupInvalidationKey, taskInvalidationKey, projection.eventSequence]);

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
      || projection.isRunning
    ) return;
    const frame = window.requestAnimationFrame(() => {
      void loadCapabilities(activeSessionId);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    activeSessionId,
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
        setSearchOpen((value) => !value);
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'n') {
        event.preventDefault();
        if (canCreateSession) {
          setNewSessionOpen(true);
        }
      }
      if (event.key === 'Escape') {
        setSearchOpen(false);
        setNewSessionOpen(false);
        setSidebarOpen(false);
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [canCreateSession]);

  const activeSession = useMemo(
    () => sessionList.find((session) => session.id === activeSessionId) ?? emptySession,
    [activeSessionId, sessionList],
  );
  const activeWorkspace = activeSession.workspace ?? emptyWorkspace;
  const openingSession = sessionList.find(session => session.id === openingSessionId);
  const isObserver = connection?.role === 'observer';
  const workspaceAvailability = workspaceObservation?.sessionId === activeSessionId ? workspaceObservation.value : undefined;
  const canControl = connection?.role === 'controller' && workspaceAvailability?.outcome === 'AVAILABLE' && runtimeStatus === 'online';
  const mergedProjection = useMemo(() => mergeRuntimeTaskInventory(
    projection,
    taskInventorySessionId === activeSessionId ? taskInventory : [],
  ), [activeSessionId, projection, taskInventory, taskInventorySessionId]);
  const taskActivities = useMemo(() => {
    const activities = new Map<string, NonNullable<(typeof mergedProjection.messages)[number]['subagentRuns']>[number]['activities']>();
    // Exact task-ID observations also serve a dependency opened on demand.
    // Its canonical task row governs terminal filtering in TaskGraphDialog.
    for (const run of projection.subagentRuns ?? []) activities.set(run.id, run.activities);
    for (const message of mergedProjection.messages) {
      for (const run of message.subagentRuns ?? []) {
        const byId = new Map((activities.get(run.id) ?? []).map((item) => [item.id, item]));
        for (const item of run.activities) byId.set(item.id, item);
        activities.set(run.id, [...byId.values()]);
      }
    }
    return activities;
  }, [mergedProjection, projection.subagentRuns]);
  const renderedMessages = mergedProjection.messages;

  const navigate = (view: AppView, settingsSection?: SettingsSection, highlightHome = false) => {
    if (view === 'settings') {
      setSettingsInitialSection(settingsSection);
      setSettingsHighlightHome(highlightHome);
    }
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

  const reopenRuntime = useCallback(async () => {
    const sessionId = activeSessionIdRef.current;
    const previous = connectionRef.current;
    if (!sessionId || !previous || !canControl || runtimeReopenBusy) return;
    let attempt = connectionAttempt.current;
    runtimeReopenOwner.current = previous;
    setRuntimeReopenBusy(true);
    setRuntimeStatus('reconnecting');
    setRuntimeError(undefined);
    try {
      // Submit with the still-valid controller. The server's original reopen
      // operation owns detach; closing the browser first would revoke admission.
      const outcome = await adapter.reopenRuntime(sessionId);
      if (attempt !== connectionAttempt.current || !ownsConnection(previous)) return;
      const opening = openRuntimeSession(sessionId, true);
      attempt = connectionAttempt.current;
      const reopened = await opening;
      if (attempt !== connectionAttempt.current || activeSessionIdRef.current !== sessionId) return;
      if (!reopened) throw new Error('新的 runtime 已准备，但浏览器尚未重新连接。');
      notify(outcome.status === 'reopened' ? 'Runtime 已安全重启' : 'Runtime 已重新连接',
        '上下文已从 canonical 数据重新投影；本地运行态不会回放。', 'success');
    } catch (error) {
      if (attempt !== connectionAttempt.current || activeSessionIdRef.current !== sessionId) return;
      const detail = productMessage(error instanceof Error ? error.message : undefined,
        '安全重启没有完成；如果提示隔离状态，请完整重启 Pulsara。');
      setRuntimeStatus('failed');
      setRuntimeError(detail);
      notify('Runtime 没有重启', detail, 'warning');
    } finally {
      if (runtimeReopenOwner.current === previous) runtimeReopenOwner.current = undefined;
      if (attempt === connectionAttempt.current && activeSessionIdRef.current === sessionId) setRuntimeReopenBusy(false);
    }
  }, [adapter, canControl, notify, openRuntimeSession, ownsConnection, runtimeReopenBusy]);

  const openSession = (id: string) => {
    if (deletingSession.current === id || archivingSession.current === id) return;
    setActiveView('workbench');
    setSidebarOpen(false);
    if (connectionRef.current?.sessionId === id || (openingSessionId === id && !runtimeError)) return;
    setOpeningSessionId(id);
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
      setSidebarOpen(false);
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

  const createScheduledSession = async (binding: ModelCallBindingPayload): Promise<string> => {
    if (!canCreateSession) throw new Error('请先连接本地服务和数据库。');
    const created = await adapter.createSession({kind:'quick'}, binding);
    setSessionList(current => [created, ...current.filter(item=>item.id!==created.id)]);
    return created.id;
  };

  const forkConversation = async (entryId: string): Promise<void> => {
    const sourceId = activeSessionIdRef.current;
    if (!sourceId || !canControl) return;
    let outcome;
    try {
      outcome = await adapter.forkConversation(sourceId, entryId);
    } catch {
      try { setSessionList(await adapter.listSessions()); } catch { /* keep uncertainty */ }
      notify('尚未确认分叉结果', '请刷新会话列表查看；不会自动重复创建。', 'warning');
      return;
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
    if (await openRuntimeSession(outcome.child_session_id)) {
      setTurnPermission('bypass-permissions');
      notify('分叉已打开', '已保留选定回复处的有效上下文。', 'success');
    }
    else notify('分叉已创建，暂未连接', '可以从会话列表重新打开。', 'warning');
  };

  const readContextUsage = useCallback((signal: AbortSignal) => (
    adapter.contextUsage(activeSessionId, signal)
  ), [adapter, activeSessionId]);

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
    const active = connectionRef.current ?? historyRef.current;
    if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
    const page = await active.readToolArtifact(resultEntryId, offsetChars);
    if (!ownsHistoryReader(active)) {
      throw new RuntimeApiError('TOOL_ARTIFACT_OWNER_CHANGED', '工具输出所属的会话已经改变。', true);
    }
    return page;
  }, [ownsConnection]);

  const locateAnnotationSource = useCallback(async (entryId: string, signal: AbortSignal) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active?.locateAnnotationSource) throw new Error('本地服务未连接。');
    const next = await active.locateAnnotationSource(entryId, signal);
    signal.throwIfAborted();
    if (!ownsHistoryReader(active)) throw new Error('会话已经切换。');
    publishProjection(next, active instanceof CanonicalHistoryView ? undefined : active);
    setFocusSourceEntry({ sessionId: active.sessionId, entryId });
  }, [ownsConnection, publishProjection]);

  const readPromptImage = useCallback(async (image: CanonicalPromptImagePart) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active) {
      throw new RuntimeApiError(
        'LOCAL_CONNECTION_UNAVAILABLE',
        '本地服务未连接。',
        true,
      );
    }
    const bytes = await active.readPromptImage(image);
    if (!ownsHistoryReader(active)) {
      throw new RuntimeApiError(
        'PROMPT_IMAGE_OWNER_CHANGED',
        '图片所属的会话已经改变。',
        true,
      );
    }
    return bytes;
  }, [ownsConnection]);

  const readVisualizationThumbnail = useCallback(async (
    entryId: string, ordinal: number, digest: string, size: number, signal: AbortSignal,
  ) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active) throw new Error('本地服务未连接。');
    const image = await active.readVisualizationThumbnail(entryId, ordinal, digest, size, signal);
    if (!ownsHistoryReader(active)) throw new Error('可视化所属的会话已经改变。');
    return image;
  }, [ownsConnection]);

  const readVisualization = useCallback(async (
    entryId: string, ordinal: number, digest: string, size: number,
  ) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active) {
      throw new RuntimeApiError(
        'LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true,
      );
    }
    const html = await active.readVisualizationHtml(entryId, ordinal, digest, size);
    if (!ownsHistoryReader(active)) {
      throw new RuntimeApiError(
        'VISUALIZATION_OWNER_CHANGED', '可视化所属的会话已经改变。', true,
      );
    }
    return html;
  }, [ownsConnection]);

  return (
    <ToolResultDisplayContext.Provider value={{ showBuiltinToolResults, onChange: setShowBuiltinToolResults }}>
    <main className={`pulsara-shell${activeView === 'workbench' ? ' is-workbench' : ' is-surface'}${inspectorOpen && !databaseBlocked && !openingSession ? ' has-inspector' : ''}`}>
      <ActivityRail activeView={activeView} onNavigate={navigate} />

      {activeView === 'workbench' && !databaseBlocked && (
        <SessionSidebar
          workspace={activeWorkspace}
          sessions={sessionList}
          activeSessionId={openingSessionId || activeSessionId}
          openingSessionId={runtimeError ? undefined : openingSessionId}
          runtimeStatus={runtimeStatus}
          isOpen={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
          onSelectSession={openSession}
          onDeleteSession={session => { setDeleteTarget(session); setDeleteError(undefined); }}
          onArchiveSession={session => void archiveSession(session)}
          onRenameSession={setRenameTarget}
          onRefreshSessions={() => { void adapter.listSessions().then(setSessionList).catch(() => {}); }}
          onNewSession={openNewSession}
          onCreateSession={createSession}
          onPickDirectory={(initialPath, signal) => adapter.pickWorkspaceDirectory(initialPath, signal)}
          onNotify={(title, detail) => notify(title, detail, 'warning')}
          canCreateSession={canCreateSession}
          onOpenSearch={() => setSearchOpen(true)}
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
      {activeView === 'workbench' && !databaseBlocked && openingSession && (
        <SessionOpeningView
          session={openingSession}
          workspace={openingSession.workspace ?? emptyWorkspace}
          error={runtimeError}
          onRetry={() => openSession(openingSession.id)}
          onOpenSidebar={() => setSidebarOpen(true)}
        />
      )}
      {activeView === 'workbench' && !databaseBlocked && (
        <div className="session-connected-views" hidden={Boolean(openingSession)}>
        <FilePreviewProvider ownerKey={connection ? `${connection.sessionId}:${connection.generation}` : 'disconnected'} api={connection?.filePreview} onNotify={notify}>
        <WorkbenchView
          onCompletePaths={(prefix, cursor, signal) => adapter.completeWorkspacePaths(activeSession.id, prefix, cursor, signal)}
          workspace={activeWorkspace}
          session={activeSession}
          messages={renderedMessages}
          interruptionNotices={mergedProjection.interruptionNotices}
          contextCompaction={mergedProjection.contextCompaction}
          initialContextBase={mergedProjection.initialContextBase}
          onFork={forkConversation}
          todo={projection.todo}
          activePlanMode={projection.planMode}
          isRunning={projection.isRunning}
          activeTurnId={projection.activeTurnId}
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
          onReadContextUsage={readContextUsage}
          contextUsageRevision={`${projection.hostSessionId ?? ''}:${projection.eventSequence}:${Boolean(projection.liveControl.compaction_in_progress)}:${JSON.stringify(bootstrap?.model_configurations)}`}
          interaction={projection.interaction}
          toolDecisionPending={toolDecisions.some(item => item.sessionId === connection?.sessionId
            && item.hostSessionId === projection.hostSessionId && item.interactionId === projection.interaction?.id
            && item.status !== 'rejected')}
          canControl={canControl}
          historyOnly={historyRef.current?.sessionId === activeSessionId}
          isObserver={isObserver}
          workspaceAvailability={workspaceAvailability}
          workspaceRecoveryBusy={workspaceRecoveryBusy}
          workspaceRecoveryError={workspaceRecoveryError}
          onRestoreWorkspace={restoreWorkspace}
          onRecheckWorkspace={recheckWorkspace}
          skills={(capabilities?.skills.items ?? []).filter(
            (skill) => skill.enabled && skill.effective,
          )}
          focusTaskId={undefined}
          focusSourceEntry={focusSourceEntry}
          focusTaskRevision={0}
          focusTaskHighlighted={false}
          onReconnect={reconnect}
          onTakeControl={() => activeSessionId && void openRuntimeSession(activeSessionId, true, true)}
          onOpenSidebar={() => setSidebarOpen(true)}
          onNewSession={openNewSession}
          canCreateSession={canCreateSession}
          onToggleInspector={() => setInspectorOpen((value) => !value)}
          onOpenModelSettings={() => navigate('settings', 'models')}
          onModelCallBindingChange={updateModelCallBinding}
          onSend={sendPrompt}
          onStop={() => void stopRun()}
          onCompact={compact}
          onReopenRuntime={() => void reopenRuntime()}
          runtimeReopenBusy={runtimeReopenBusy}
          onReadInteraction={readInteraction}
          onResolveInteraction={resolveInteraction}
          artifactOwnerKey={`${connection?.sessionId ?? ''}:${connection?.generation ?? 0}`}
          onReadToolArtifact={readToolArtifact}
          onReadPromptImage={readPromptImage}
          onLocateAnnotation={locateAnnotationSource}
          onReadVisualization={readVisualization}
          onReadVisualizationThumbnail={readVisualizationThumbnail}
          promptDraftStore={promptDraftStore}
          onNotify={notify}
          permission={turnPermission}
          onPermissionChange={setTurnPermission}
        />
        {inspectorOpen && <button className="inspector-scrim" aria-label="收起详情侧栏" onClick={() => setInspectorOpen(false)} />}
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
          modelConfigurations={bootstrap?.model_configurations ?? []}
          taskGroups={taskGroups}
          loadedTaskGroups={loadedTaskGroups}
          taskGroupTotal={taskGroupTotal}
          onLoadTask={loadTask}
          onLoadTaskGroup={loadTaskGroup}
          onUnloadTaskGroup={unloadTaskGroup}
          onLoadMoreTaskGroups={loadMoreTaskGroups}
          onReadSubagentCapacity={readSubagentCapacity}
          taskActivities={taskActivities}
          taskArtifactOwnerKey={`${connection?.sessionId ?? ''}:${connection?.generation ?? 0}`}
          onReadToolArtifact={readToolArtifact}
          onLoadTaskActivities={loadTaskActivities}
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
          onLoadBackgroundProcesses={loadTaskBackgroundProcesses}
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
          onOpenHomeSettings={() => navigate('settings', 'service', true)}
          onNotify={notify}
          onClose={() => setInspectorOpen(false)}
        />
        </FilePreviewProvider>
        </div>
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
          onNotify={notify}
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
          initialSection={settingsInitialSection}
          highlightHome={settingsHighlightHome}
          sessionRevision={sessionRevision}
          onSessionsChanged={async () => { setSessionList(await adapter.listSessions()); }}
          onDeleteSession={session => { setDeleteTarget(session); setDeleteError(undefined); }}
          theme={theme}
          bootstrap={bootstrap}
          runtimeStatus={runtimeStatus}
          adapter={adapter}
          onThemeChange={setTheme}
          onConfigurationChanged={refreshConfiguration}
          onNotify={notify}
        />
      )}

      {activeView === 'scheduled' && <ScheduledTasksView modelConfigurations={bootstrap?.model_configurations ?? []} onCreateSession={createScheduledSession} api={scheduledApi} sessions={sessionList} ready={!databaseBlocked && runtimeStatus === 'online'} onOpenSettings={() => navigate('settings')} onOpenSession={(id, entryId) => { if (entryId) setFocusSourceEntry({ sessionId: id, entryId }); openSession(id); }} />}
      {activeView === 'memory' && <MemoryView api={adapter.memory} databaseState={databaseState} runtimeStatus={runtimeStatus} onReconnect={reconnect} onOpenSettings={() => navigate('settings')} onOpenSource={source => { setFocusSourceEntry({ sessionId: source.session_id, entryId: source.entry_id }); openSession(source.session_id); }} />}

      {searchOpen && <SessionSearchDialog adapter={adapter} available={databaseState === 'ready' && Boolean(bootstrap)}
        onClose={() => setSearchOpen(false)} onOpen={session => {
          setSessionList(current => current.some(item => item.id === session.id)
            ? current.map(item => item.id === session.id ? session : item) : [...current, session]);
          setSessionRevision(value => value + 1);
          openSession(session.id);
        }} />}
      {newSessionOpen && <NewSessionDialog
        open={newSessionOpen}
        canCreateSession={canCreateSession}
        onClose={() => setNewSessionOpen(false)}
        onCreate={createSession}
        onPickDirectory={(initialPath, signal) => adapter.pickWorkspaceDirectory(initialPath, signal)}
      />}
      <ToastStack
        toasts={toasts}
        onDismiss={(id) => setToasts((current) => current.filter((toast) => toast.id !== id))}
      />
      {renameTarget && <SessionRenameDialog key={renameTarget.id} session={renameTarget}
        onClose={() => setRenameTarget(undefined)} onSave={async title => {
          const result = await adapter.renameSession(renameTarget.id, title);
          if (result.session_id !== renameTarget.id || result.title !== title) throw new Error('未能确认保存结果，请刷新确认或重试。');
          setSessionList(current => current.map(item => item.id === result.session_id ? { ...item, title: result.title } : item));
          setSessionRevision(value => value + 1);
        }} />}
      {deleteTarget && <SessionDeletionDialog session={deleteTarget} busy={deleteBusy} error={deleteError}
        onConfirm={() => void confirmSessionDelete()} onClose={() => { if (!deleteBusy) setDeleteTarget(undefined); }} />}
    </main>
    </ToolResultDisplayContext.Provider>
  );
}
