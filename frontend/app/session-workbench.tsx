import type { ToolOutputPage } from '../lib/runtime-adapter';
import { usePendingSteerPages } from '../lib/pending-steer-pages';
import { mcpAuthorizationKey, type useMcpAuthorization, useModelMcpAuthorization } from '../components/mcp-authorization';
import { PresentationProvider } from '../components/presentation-provider';
import { uiPreferences } from '../lib/ui-preferences';
import { randomIdentity } from '../lib/identity';

import { DesktopWorkspaceProvider } from '../components/desktop-workspace';
import { FilePreviewProvider } from '../components/file-browser';

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import type { CapabilityTab } from '../components/capability-view';
import type { LocalScheduledTasksApi } from '../lib/scheduled-tasks-api';
import { InspectorPanel } from '../components/inspector-panel';
import type { SettingsSection } from '../components/settings-view';
import { WorkbenchView } from '../components/workbench-view';
import type { PromptDraftStore } from '../lib/prompt-draft';
import { type SessionSuggestionCache, type SessionToolDecisionStore, type ToolDecisionIntent, useSessionEphemeralResults } from '../lib/session-ephemeral-results';
import {
  CanonicalHistoryView,
  compareSequence,
  projectRootStatus,
  type WorkspaceAvailability,
  type UserControlCommandRef,
  type UserControlTarget,
  RuntimeApiError,
  type RuntimeAdapter,
  type RuntimeBootstrap,
  type RuntimeConnection,
  type ModelCallBindingPayload,
  type RuntimeInteractionResolution,
  type RuntimeInteractionSummary,
  type RuntimeProjection,
  type HistoryResidence,
  type LocalPromptSubmission,
  type QueuedPrompt,
  type QueuedPromptAction,
  type CommandReceipt,
  type EditablePromptContent,
  type CanonicalPromptImagePart,
} from '../lib/runtime-adapter';

type StopObservation = {
  target: UserControlTarget;
  reference?: UserControlCommandRef;
  phase: 'preparing' | 'pending' | 'unknown' | 'terminal';
  receipt?: CommandReceipt;
  error?: string;
  querying?: boolean;
  lastQueryStarted?: number;
};

function stopResultMessage(receipt: CommandReceipt): string {
  const messages: Record<string, string> = {
    CONTROL_REQUEST_EXPIRED: '本次停止请求已过期，请重新请求停止。',
    OWNER_UNAVAILABLE: '原会话运行实例已不可控制；不会改投新的实例。',
    CONTROL_TARGET_UNAVAILABLE: '原目标当前无法停止，尚不能确认其执行已结束。',
    CONTROL_TARGET_NOT_CURRENT: '原目标已不在当前执行中；不会停止其他任务。',
    CONTROL_ALREADY_TERMINAL: '原目标已结束。',
    ROOT_STOPPED: '本轮运行已停止。',
  };
  return messages[receipt.publicCode ?? ''] ?? receipt.publicMessage
    ?? (receipt.status === 'failed' ? '停止结果未能确认。' : receipt.status === 'rejected' ? '这次停止请求未被接受。' : '停止状态已确认。');
}
import type {
  CapabilitySnapshot,
  McpEditInput,
  McpServerCapability,
  PermissionMode,
  RuntimeStatus,
  SessionSummary,
  SkillCapability,
  SkillImportInput,
  ToastMessage,
  UserCapabilitySnapshot,
  Workspace,
} from '../lib/pulsara-types';

export const emptyWorkspace: Workspace = {
  id: 'local',
  name: '尚未选择工作目录',
  path: '',
  kind: 'project',
};

export const emptySession: SessionSummary = {
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
  canonicalInputs: [],
  planMode: false,
  control: {},
  liveControl: {},
  eventSequence: 0,
  liveOwnerEpoch: 0,
  liveRevision: 0,
  liveControlOwnerEpoch: 0,
  liveControlRevision: 0,
};

// The workbench shares the loopback HTTP/1.1 pool with commands, history and
// other reads. Leave two of its six sockets free of blocking session polls.
const maximumWaitingSessionObservations = 4;
const backgroundObservationIntervalMs = 750;
const visibleObservationIntervalMs = 250;
const busyObservationIntervalMs = 75;
let waitingSessionObservations = 0;

const internalLanguage = /terminal(?:\s+protocol)?|protocol\s*v?\d*|kernel|canonical|generation|authority|projection|epoch|hostsession|runtime|attachment|owner|provider\s+prefix/i;

export function productMessage(message: string | undefined, fallback: string): string {
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
    execution: receipt.promptDelivery?.execution,
    outcomeCode: receipt.publicCode,
    detail: receipt.publicMessage,
  };
  if (queueStatus === 'CONSUMED') return { ...shared, status: 'consumed' };
  if (queueStatus === 'CANCELLED') return { ...shared, status: 'cancelled' };
  if (queueStatus === 'REJECTED') return { ...shared, status: 'rejected' };
  if (queueStatus === 'PENDING') return { ...shared, status: 'synchronizing' };
  return { ...shared, status: receipt.status === 'rejected' ? 'rejected' : 'synchronizing' };
}

function requirePromptReceipt(
  commandId: string,
  receipt: CommandReceipt,
  queueItemId?: string,
): void {
  if (receipt.commandId !== commandId
    || (queueItemId && receipt.promptDelivery?.queueItemId !== queueItemId)) {
    throw new RuntimeApiError('PROMPT_RECEIPT_MISMATCH', '输入状态与原始输入身份不一致，请稍后重试核对。', false);
  }
}

function canonicalInputMatches(
  message: RuntimeProjection['canonicalInputs'][number],
  submission: LocalPromptSubmission,
): boolean {
  return message.inputSource?.commandId === submission.commandId
    && (!submission.queueItemId || message.inputSource.queueItemId === submission.queueItemId);
}

function canonicalPromptReady(message: RuntimeProjection['canonicalInputs'][number], projection: RuntimeProjection): boolean {
  // The history host cannot display the canonical bubble until its grouping is
  // ready too. Keep exact submitted content through a visible handoff; an
  // offscreen, context-confirmed input is re-readable and can release its preview.
  const outsideReadingBand = projection.history && !projection.history.windows.some(window => window.entryIds.includes(message.id));
  return (outsideReadingBand || message.contentState === undefined || message.contentState === 'ready')
    && message.historyContextState !== 'loading' && message.historyContextState !== 'error';
}

function promptWasAccepted(receipt: CommandReceipt): boolean {
  const queueStatus = receipt.promptDelivery?.queueStatus.toUpperCase();
  if (queueStatus === 'CONSUMED' || queueStatus === 'PENDING') return true;
  if (queueStatus === 'CANCELLED' || queueStatus === 'REJECTED') return false;
  return receipt.status !== 'rejected';
}

export interface SessionWorkbenchObservation {
  status: RuntimeStatus;
  role?: RuntimeConnection['role'];
  error?: string;
  failure?: { details: string; requiresRestart: boolean };
  ready: boolean;
  historyOnly: boolean;
  approvalPending: boolean;
  running: boolean;
  retry: () => void;
}

interface SessionWorkbenchProps {
  adapter: RuntimeAdapter;
  session: SessionSummary;
  loaded: boolean;
  visible: boolean;
  focused: boolean;
  bootstrap?: RuntimeBootstrap;
  userServiceStatus: RuntimeStatus;
  userServiceError?: string;
  userCapabilityMutation?: UserCapabilitySnapshot;
  scheduledApi: LocalScheduledTasksApi;
  promptDraftStore: PromptDraftStore;
  suggestionCache: SessionSuggestionCache;
  toolDecisionStore: SessionToolDecisionStore;
  pendingTabClose: (connection: RuntimeConnection) => Promise<boolean> | undefined;
  changeSessionList: (update: (sessions: SessionSummary[]) => SessionSummary[]) => void;
  invalidateSessionList: () => void;
  readSessionList: (canPublish?: () => boolean) => Promise<SessionSummary[] | undefined>;
  notify: (title: string, detail?: ToastMessage['detail'], tone?: ToastMessage['tone']) => void;
  runMcpAuthorization: ReturnType<typeof useMcpAuthorization>['run'];
  onObservation: (id: string, observation: SessionWorkbenchObservation) => void;
  onConnection: (id: string, previous?: RuntimeConnection, current?: RuntimeConnection) => void;
  onPublishStatus: (id: string, status: SessionSummary['status']) => void;
  onSessionMissing: (id: string) => void;
  onClose: (id: string) => void;
  onOpenSession: (id: string) => Promise<boolean>;
  onRenameSession: (session: SessionSummary) => void;
  onOpenScheduledTask: (id: string) => void;
  onOpenUserCapabilities: (kind?: CapabilityTab) => void;
  onOpenSettings: (section?: SettingsSection, highlightHome?: boolean) => void;
  onBrowserShortcut: (command: 'search' | 'new-session') => void;
}

export function SessionWorkbench({
  adapter, session: activeSession, loaded, visible, focused, bootstrap, userServiceStatus, userServiceError, userCapabilityMutation,
  scheduledApi, promptDraftStore, suggestionCache, toolDecisionStore, pendingTabClose, changeSessionList, invalidateSessionList, readSessionList,
  notify: onNotify, runMcpAuthorization, onObservation, onConnection, onPublishStatus, onSessionMissing, onClose, onOpenSession, onRenameSession,
  onOpenScheduledTask, onOpenUserCapabilities, onOpenSettings, onBrowserShortcut,
}: SessionWorkbenchProps) {
  const sessionId = activeSession.id;
  const foreground = visible && focused;
  const foregroundRef = useRef(foreground);
  foregroundRef.current = foreground;
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const notify = useCallback<SessionWorkbenchProps['notify']>((...message) => {
    if (foregroundRef.current) onNotify(...message);
  }, [onNotify]);
  const [activeSessionId, setActiveSessionId] = useState('');
  const [scheduledTask, setScheduledTask] = useState<{ sessionId: string; taskId: string }>();
  const [projection, setProjection] = useState<RuntimeProjection>(emptyProjection);
  // These observations own only UI results, never the underlying execution.
  const stopObservations = useRef<StopObservation[]>([]);
  const [stopRevision, setStopRevision] = useState(0);
  const publishStops = useCallback(() => setStopRevision(value => value + 1), []);
  useEffect(() => () => { stopObservations.current = []; }, [adapter]);
  const [queuePages, setQueuePages] = useState<{owner: RuntimeConnection | CanonicalHistoryView; items: QueuedPrompt[]; absent: Set<string>}>();
  const queuePagesRef = useRef(queuePages);
  queuePagesRef.current = queuePages;
  const queuePageRead = useRef<AbortController | undefined>(undefined);
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot>();
  const [capabilityLoading, setCapabilityLoading] = useState(false);
  const [capabilityError, setCapabilityError] = useState<string>();
  const [capabilityBusy, setCapabilityBusy] = useState<string>();
  const capabilityAttempt = useRef(0);
  const [connection, setConnection] = useState<RuntimeConnection>();
  const [connectionRole, setConnectionRole] = useState<RuntimeConnection['role']>();
  const historyRef = useRef<CanonicalHistoryView | undefined>(undefined);
  const historyReadAbort = useRef(new AbortController());
  const [workspaceObservation, setWorkspaceObservation] = useState<{sessionId: string; value: WorkspaceAvailability}>();
  const [workspaceRecoveryBusy, setWorkspaceRecoveryBusy] = useState(false);
  const [workspaceRecoveryError, setWorkspaceRecoveryError] = useState<string>();

  const connectionRef = useRef<RuntimeConnection | undefined>(undefined);
  const activeSessionIdRef = useRef('');
  const connectionAttempt = useRef(0);
  const connectionInFlight = useRef<{
    sessionId: string; reconnecting: boolean; takeover: boolean; reopenTab: boolean; attempt: number;
    promise: Promise<RuntimeConnection | undefined>;
  } | undefined>(undefined);
  const promptReconciliationInFlight = useRef(new Set<string>());
  const [connectionStatus, setRuntimeStatus] = useState<RuntimeStatus>('starting');
  const runtimeStatus = sessionId ? connectionStatus : userServiceStatus;
  const [runtimeError, setRuntimeError] = useState<string>();
  const [runtimeFailure, setRuntimeFailure] = useState<{details: string; requiresRestart: boolean}>();
  const [runtimeReopenBusy, setRuntimeReopenBusy] = useState(false);
  const runtimeReopenOwner = useRef<RuntimeConnection | undefined>(undefined);
  const [localSubmissions, setLocalSubmissions] = useState<LocalPromptSubmission[]>([]);
  const observationUrgent = projection.isRunning || Boolean(projection.manualCompactionCommandId)
    || projection.queuedCount > 0 || localSubmissions.some(item => item.sessionId === sessionId && item.status === 'sending');
  const observationUrgentRef = useRef(observationUrgent);
  observationUrgentRef.current = observationUrgent;
  const wakeObservation = useRef<(() => void) | undefined>(undefined);
  useEffect(() => {
    if (observationUrgent || visible) wakeObservation.current?.();
  }, [observationUrgent, foreground, visible]);
  const [queueActions, setQueueActions] = useState<QueuedPromptAction[]>([]);
  const readToolDecisions = useCallback(() => toolDecisionStore.read(sessionId), [sessionId, toolDecisionStore]);
  const toolDecisions = useSyncExternalStore(toolDecisionStore.subscribe, readToolDecisions, readToolDecisions);
  const toolDecisionQueries = useRef(new Map<string, { cut: string; generation: number; inFlight: boolean }>());
  const saveToolDecision = useCallback((intent: ToolDecisionIntent) => {
    if (['accepted', 'rejected', 'retired'].includes(intent.status)) toolDecisionQueries.current.delete(intent.commandId);
    toolDecisionStore.save(intent);
  }, [toolDecisionStore]);
  const queueActionsInFlight = useRef(new Set<string>());
  const [inspectorOpen, setInspectorOpenState] = useState(() => uiPreferences.getItem('pulsara-workspace-open') === 'true');
  const inspectorOpenRef = useRef(inspectorOpen);
  const setInspectorOpen = useCallback((next: boolean | ((current: boolean) => boolean)) => {
    const value = typeof next === 'function' ? next(inspectorOpenRef.current) : next;
    inspectorOpenRef.current = value;
    setInspectorOpenState(value);
    uiPreferences.setItem('pulsara-workspace-open', String(value));
  }, []);
  const [turnPermission, setTurnPermission] = useState<PermissionMode>('bypass-permissions');
  const databaseState = bootstrap?.database_state;
  useModelMcpAuthorization(connection, adapter, runMcpAuthorization);
  const ownsHistoryReader = useCallback((reader: RuntimeConnection | CanonicalHistoryView): boolean => (
    activeSessionIdRef.current === reader.sessionId
    && (reader instanceof CanonicalHistoryView ? historyRef.current === reader : connectionRef.current === reader)
  ), []);

  const ownsConnection = useCallback((expected: RuntimeConnection): boolean => (
    connectionRef.current === expected
    && activeSessionIdRef.current === expected.sessionId
    && connectionRef.current.generation === expected.generation
  ), []);

  const notifyPromptReceipt = useCallback((
    receipt: CommandReceipt,
    acceptedTitle: string,
    acceptedDetail: string,
  ) => {
    const queueStatus = receipt.promptDelivery?.queueStatus.toUpperCase();
    if (queueStatus === 'CONSUMED' && receipt.promptDelivery?.execution?.status === 'stopped') {
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
    const queuedByCommand = new Map(next.queuedPrompts.map((item) => [item.commandId, item]));
    const transitionByCommand = new Map(next.promptTransitions.map((item) => (
      [item.commandId, item]
    )));
    setLocalSubmissions((current) => {
      const projected: LocalPromptSubmission[] = current.flatMap((item) => {
        if (item.sessionId !== (owner?.sessionId ?? activeSessionIdRef.current)) return [item];
        const canonical = next.canonicalInputs.find(message => canonicalInputMatches(message, item));
        if (canonical && canonicalPromptReady(canonical, next)) return [];
        if (canonical) return [{ ...item, status: 'consumed' as const, consumedEntryId: canonical.id }];
        // A history/body publication can precede the committed queue suffix.
        // It cannot downgrade an exact terminal receipt to older pending state.
        if (['consumed', 'cancelled', 'rejected'].includes(item.status)) return [item];
        const queued = queuedByCommand.get(item.commandId);
        if (queued && (!item.queueItemId || queued.queueItemId === item.queueItemId)) {
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
        if (transition && (!item.queueItemId || transition.queueItemId === item.queueItemId)) {
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
        if (queued.sourceKind !== 'command') continue;
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
    onPublishStatus(owner?.sessionId ?? activeSessionIdRef.current, projectRootStatus(next.control.latest_root_turn));
  }, [onPublishStatus]);

  const activeReader = connection ?? historyRef.current;
  const pendingSteerReading = usePendingSteerPages(activeReader, projection);
  // The preview can also change at the same event cut (e.g. exact queue reads).
  // Streaming text changes neither queue membership nor body availability.
  const queuePreview = JSON.stringify(projection.queuedPrompts.map(item => [item.queueItemId, item.contentUnavailable]));
  useEffect(() => () => {
    queuePageRead.current?.abort();
    queuePageRead.current = undefined;
  }, [activeReader, activeSessionId, projection.eventSequence, projection.control, projection.queuedCount, queuePreview]);
  useEffect(() => {
    const reader = activeReader;
    if (!reader || !activeSessionId || reader.sessionId !== activeSessionId || queuePageRead.current) return;
    const controller = new AbortController();
    queuePageRead.current = controller;
    const previous = queuePagesRef.current?.owner === reader ? queuePagesRef.current : undefined;
    void (async () => {
      const found = new Map<string, QueuedPrompt>();
      let cursor: string | undefined;
      // Use the existing read-only pagination to obtain the actual tail.
      // Rendering a bounded preview must not confuse page 1's end with it.
      for (;;) {
        const page = await reader.readQueuePage(cursor, controller.signal);
        for (const item of page.items) found.set(item.queueItemId, item);
        if (!page.nextCursor) break;
        cursor = page.nextCursor;
      }
      const presentInControl = new Set(projection.queuedPrompts.map(item => item.queueItemId));
      const absent = new Set([...previous?.absent ?? []].filter(id => presentInControl.has(id)));
      for (const id of found.keys()) absent.delete(id);
      const previouslySeen = new Map([...(previous?.items ?? []), ...projection.queuedPrompts.filter(item=>item.deliveryMode==='new-turn')].map(item => [item.queueItemId, item]));
      for (const item of previouslySeen.values()) if (!found.has(item.queueItemId)) {
        const exact = await reader.readQueueItem(item.queueItemId, controller.signal);
        if (exact) found.set(exact.queueItemId, exact); else absent.add(item.queueItemId);
      }
      if (!controller.signal.aborted && ownsHistoryReader(reader)) {
        setQueuePages({owner:reader, items:[...found.values()], absent});
      }
    })().catch(() => {
      // Keep the last confirmed queue and retry on the next publication.
      if (queuePageRead.current === controller) queuePageRead.current = undefined;
    });
  }, [activeReader, activeSessionId, ownsHistoryReader, projection.eventSequence, projection.control, projection.queuedCount, queuePreview, projection.queuedPrompts]);

  useEffect(() => {
    if (!activeReader || !ownsHistoryReader(activeReader)) return;
    const publish = () => {
      if (ownsHistoryReader(activeReader)) {
        publishProjection(activeReader.current(), activeReader instanceof CanonicalHistoryView ? undefined : activeReader);
      }
    };
    const unsubscribe = activeReader.subscribe?.(publish);
    // A context/body read may have completed between connect and subscribing.
    publish();
    return unsubscribe;
  }, [activeReader, ownsHistoryReader, publishProjection]);

  const readConsumedInput = useCallback((owner: RuntimeConnection, receipt: CommandReceipt) => {
    const entryId = receipt.promptDelivery?.consumedEntryId;
    if (!entryId || receipt.promptDelivery?.queueStatus.toUpperCase() !== 'CONSUMED' || !owner.locateEntry) return;
    const signal = historyReadAbort.current.signal;
    // This read never changes the accepted receipt or blocks continued observe.
    void owner.locateEntry(entryId, signal).then(next => {
      if (!signal.aborted && ownsConnection(owner)) publishProjection(next, owner);
    }).catch(() => {
      // Keep the local known content and exact receipt while history is unavailable.
    });
  }, [ownsConnection, publishProjection]);

  const loadBackgroundProcesses = useCallback(async (cursor?: string) => {
    const active = connectionRef.current;
    if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
    const host = active.current().hostSessionId;
    if (!host) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '运行实例尚未就绪。', true);
    const page = await active.listBackgroundProcesses(cursor);
    if (!ownsConnection(active) || active.current().hostSessionId !== host) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '后台命令所属的会话已经改变。', true);
    return page;
  }, [ownsConnection]);

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

  const { suggestion, observeTitle } = useSessionEphemeralResults({
    cache: suggestionCache, adapter, connection, projection,
    suggestionsEnabled: bootstrap?.local_settings.input_suggestions_enabled ?? true,
    binding: activeSession.modelCallBinding,
    visible: visible,
    refreshSessions: readSessionList,
  });
  const openRuntimeSessionAttempt = useCallback(async (
    sessionId: string,
    reconnecting: boolean,
    takeover: boolean,
    reopenTab: boolean,
  ): Promise<RuntimeConnection | undefined> => {
    historyReadAbort.current.abort();
    historyReadAbort.current = new AbortController();
    const attempt = ++connectionAttempt.current;
    capabilityAttempt.current += 1;
    setCapabilities(undefined);
    setCapabilityError(undefined);
    setCapabilityLoading(Boolean(sessionId));
    setRuntimeStatus(reconnecting ? 'reconnecting' : 'starting');
    setRuntimeError(undefined);
    setRuntimeFailure(undefined);
    const previous = connectionRef.current;
    const previousHistory = historyRef.current;
    const retainedHistory = previousHistory?.sessionId === sessionId ? previousHistory : undefined;
    historyRef.current = retainedHistory;
    connectionRef.current = undefined;
    if (previous) onConnection(sessionId, previous);
    setConnection(undefined);
    if (!retainedHistory) setWorkspaceObservation(undefined);
    setWorkspaceRecoveryBusy(false);
    setRuntimeReopenBusy(false);
    setWorkspaceRecoveryError(undefined);
    try {
      if (previousHistory && previousHistory !== retainedHistory) await previousHistory.close();
      if (previous) await previous.close();
      if (attempt !== connectionAttempt.current) return undefined;
      const next = await adapter.connect(sessionId, takeover, reopenTab);
      if (attempt !== connectionAttempt.current) {
        await next.close();
        return undefined;
      }
      if (next instanceof CanonicalHistoryView) {
        if (retainedHistory) await retainedHistory.close();
        if (attempt !== connectionAttempt.current) { await next.close(); return undefined; }
        historyRef.current = next;
        setConnectionRole(undefined);
        activeSessionIdRef.current = sessionId;
        setActiveSessionId(sessionId);
        setWorkspaceObservation({sessionId, value: next.availability});
        publishProjection(next.current());
        setRuntimeStatus('online');
        setCapabilityLoading(false);
        return undefined;
      }
      if (retainedHistory) await retainedHistory.close();
      if (attempt !== connectionAttempt.current) { await next.close(); return undefined; }
      historyRef.current = undefined;
      setWorkspaceObservation({sessionId, value: next.availability});
      connectionRef.current = next;
      setConnectionRole(next.role);
      activeSessionIdRef.current = sessionId;
      setConnection(next);
      setActiveSessionId(sessionId);
      onConnection(sessionId, undefined, next);
      changeSessionList((current) => current.map((session) => (
        session.id === sessionId ? { ...session, live: true } : session
      )));
      publishProjection(next.current(), next);
      setRuntimeStatus('online');
      void readSessionList(() => attempt === connectionAttempt.current).catch(() => {
        // The active connection is authoritative for the current session. A
        // later successful open/reconnect will retry the cold list refresh.
      });
      return next;
    } catch (error) {
      if (attempt !== connectionAttempt.current) return undefined;
      try {
        const current = await adapter.readSession(sessionId);
        if (attempt !== connectionAttempt.current) return undefined;
        if (!current) {
          onSessionMissing(sessionId);
          return undefined;
        }
      } catch { /* An unavailable read cannot confirm that a session was removed. */ }
      if (attempt !== connectionAttempt.current) return undefined;
      const message = productMessage(error instanceof Error ? error.message : undefined, '无法连接本地服务。');
      setRuntimeStatus(error instanceof RuntimeApiError && error.retryable ? 'offline' : 'failed');
      setRuntimeError(message);
      setRuntimeFailure(error instanceof Error ? {
        details: error instanceof RuntimeApiError ? `${error.code}: ${error.message}` : `${error.name}: ${error.message}`,
        requiresRestart: error instanceof RuntimeApiError && error.code === 'SESSION_RECOVERY_CLOSE_FAILED',
      } : undefined);
      if (retainedHistory) {
        setWorkspaceRecoveryError(message);
        void adapter.workspaceAvailability(sessionId).then(value => {
          if (attempt === connectionAttempt.current && historyRef.current === retainedHistory) {
            setWorkspaceObservation({sessionId, value});
          }
        }).catch(() => {});
      }
      setCapabilityLoading(false);
      return undefined;
    }
  }, [adapter, publishProjection, changeSessionList, onConnection, onSessionMissing, readSessionList]);

  const openRuntimeSession = useCallback((sessionId: string, reconnecting = false, takeover = false, reopenTab = !reconnecting): Promise<RuntimeConnection | undefined> => {
    const current = connectionInFlight.current;
    if (current && current.attempt === connectionAttempt.current && current.sessionId === sessionId
      && current.reconnecting === reconnecting && current.takeover === takeover && current.reopenTab === reopenTab) return current.promise;
    // Share the complete original attempt, including publication and stale
    // cleanup. Sharing only adapter.connect would let a second attempt close
    // the same connection as its stale result. Different intents retain the
    // original attempt counter and cancellation boundary.
    const promise = openRuntimeSessionAttempt(sessionId, reconnecting, takeover, reopenTab);
    const owner = {sessionId, reconnecting, takeover, reopenTab, attempt: connectionAttempt.current, promise};
    connectionInFlight.current = owner;
    void promise.finally(() => {
      if (connectionInFlight.current === owner) connectionInFlight.current = undefined;
    }).catch(() => {});
    return promise;
  }, [openRuntimeSessionAttempt]);

  useEffect(() => {
    if (!sessionId) return;
    void openRuntimeSession(sessionId);
    return () => {
      historyReadAbort.current.abort();
      connectionAttempt.current += 1;
      capabilityAttempt.current += 1;
      activeSessionIdRef.current = '';
      const active = connectionRef.current;
      const history = historyRef.current;
      connectionRef.current = undefined;
      historyRef.current = undefined;
      if (history) void history.close();
      if (active) {
        onConnection(sessionId, active);
        void active.close().catch(() => {});
      }
    };
  }, [onConnection, openRuntimeSession, sessionId]);

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
          if (value.outcome === 'AVAILABLE' && historyRef.current?.sessionId === activeSessionId && !historyRef.current.runtimeCloseFailed) {
            await openRuntimeSession(activeSessionId, true);
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
    const closing = pendingTabClose(expectedConnection);
    if (closing && await closing) return undefined;
    if (!ownsConnection(expectedConnection)) return undefined;
    if (!(error instanceof RuntimeApiError) || !error.retryable) return expectedConnection;
    setRuntimeStatus('reconnecting');
    setRuntimeError(productMessage(error.message, '连接已中断，正在重新连接。'));
    const recovered = await openRuntimeSession(expectedConnection.sessionId, true);
    return recovered && ownsConnection(recovered) ? recovered : undefined;
  }, [openRuntimeSession, ownsConnection, pendingTabClose]);
  useEffect(() => {
    if (!connection) return;
    const abort = new AbortController();
    let active = true;
    void (async () => {
      while (active && !abort.signal.aborted) {
        const wasForeground = foregroundRef.current;
        const wasVisible = visibleRef.current;
        const urgent = observationUrgentRef.current;
        const mayWait = urgent && waitingSessionObservations < maximumWaitingSessionObservations;
        try {
          let next: RuntimeProjection | undefined;
          try {
            if (mayWait) waitingSessionObservations += 1;
            next = await connection.observe(abort.signal, mayWait ? 2000 : 0);
          } finally {
            if (mayWait) waitingSessionObservations -= 1;
          }
          if (!active || connectionRef.current !== connection) return;
          if (next) {
            publishProjection(next, connection);
            for (const notice of next.presentationNotices ?? []) notify(notice);
          }
          setRuntimeStatus('online');
          if (mayWait || (!wasForeground && foregroundRef.current) || (!wasVisible && visibleRef.current)) continue;
          const busy = urgent || observationUrgentRef.current || next?.isRunning || next?.manualCompactionCommandId || next?.queuedCount;
          const interval = busy ? busyObservationIntervalMs
            : visibleRef.current ? visibleObservationIntervalMs : backgroundObservationIntervalMs;
          await new Promise<void>(resolve => {
            const finish = () => {
              clearTimeout(timer);
              wakeObservation.current = undefined;
              resolve();
            };
            const timer = window.setTimeout(finish, interval);
            wakeObservation.current = finish;
          });
        } catch (error) {
          const closing = pendingTabClose(connection);
          if (closing && await closing) return;
          if (abort.signal.aborted || !active || !ownsConnection(connection)
            || runtimeReopenOwner.current === connection) return;
          setRuntimeStatus('reconnecting');
          setRuntimeError(productMessage(error instanceof Error ? error.message : undefined, '连接已中断。'));
          setRuntimeFailure(error instanceof Error ? {
            details: error instanceof RuntimeApiError ? `${error.code}: ${error.message}` : `${error.name}: ${error.message}`,
            requiresRestart: error instanceof RuntimeApiError && error.code === 'SESSION_RECOVERY_CLOSE_FAILED',
          } : undefined);
          window.setTimeout(async () => {
            const closing = pendingTabClose(connection);
            if (closing && await closing) return;
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
      wakeObservation.current?.();
    };
  }, [connection, notify, openRuntimeSession, ownsConnection, pendingTabClose, publishProjection]);

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
        requirePromptReceipt(candidate.commandId, receipt, candidate.queueItemId);
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
        readConsumedInput(connection, receipt);
      } catch (error) {
        if (connectionRef.current !== connection) return;
        if (error instanceof RuntimeApiError && error.code === 'PROMPT_RECEIPT_MISMATCH') {
          notify('无法核对输入状态', error.message, 'warning');
        }
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
    notify,
    notifyPromptReceipt,
    projection.eventSequence,
    readConsumedInput,
  ]);

  useEffect(() => {
    if (!activeSessionId || !connection || connection.sessionId !== activeSessionId) return;
    const frame = window.requestAnimationFrame(() => {
      void loadCapabilities(activeSessionId);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeSessionId, connection, loadCapabilities, userCapabilityMutation]);

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

  const activeWorkspace = activeSession.workspace ?? emptyWorkspace;
  // Context reads and observe keep running while the initial tail is prepared.
  // The reader latches initialReady, so later page/context refreshes stay inline.
  const initialHistoryPending = activeReader?.sessionId === activeSessionId && projection.history?.initialReady === false;
  const isObserver = connection?.role === 'observer';
  const workspaceAvailability = workspaceObservation?.sessionId === activeSessionId ? workspaceObservation.value : undefined;
  const canControl = connection?.role === 'controller' && workspaceAvailability?.outcome === 'AVAILABLE' && runtimeStatus === 'online';
  const mergedProjection = projection;
  const renderedMessages = mergedProjection.messages;
  const visibleQueuePages = queuePages?.owner === activeReader ? queuePages : undefined;
  const renderedQueuedPrompts = useMemo(() => {
    const items = new Map(projection.queuedPrompts.map(item => [item.queueItemId, item]));
    for (const item of visibleQueuePages?.items ?? []) items.set(item.queueItemId, item);
    for (const item of pendingSteerReading.items) items.set(item.queueItemId, item);
    for (const id of visibleQueuePages?.absent ?? []) items.delete(id);
    for (const message of projection.canonicalInputs) if (message.inputSource?.queueItemId) items.delete(message.inputSource.queueItemId);
    return [...items.values()].sort((a,b) => compareSequence(a.sequence,b.sequence));
  }, [projection.queuedPrompts, projection.canonicalInputs, visibleQueuePages, pendingSteerReading.items]);
  const renderedSubmissions = useMemo(() => localSubmissions.filter(item => (
    item.sessionId === sessionId
    && !item.handledByQueueAction
    && item.outcomeCode !== 'USER_REDIRECTED_TO_STEER'
    && !queueActions.some(action => action.status === 'accepted' && action.source.commandId === item.commandId)
    && !projection.canonicalInputs.some(message => canonicalInputMatches(message, item) && canonicalPromptReady(message, projection))
  )), [localSubmissions, projection, queueActions, sessionId]);
  const activeSkills = useMemo(() => (
    (capabilities?.skills.items ?? []).filter(skill => skill.enabled && skill.effective)
  ), [capabilities?.skills.items]);

  useEffect(() => {
    if (!visible || !activeSessionId || databaseState !== 'ready' || runtimeStatus !== 'online') return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let reading = false;
    let needsRefresh = false;
    const refresh = async () => {
      if (controller.signal.aborted) return;
      if (reading) { needsRefresh = true; return; }
      clearTimeout(timer);
      if (document.visibilityState === 'visible') {
        reading = true;
        try {
          const page = await scheduledApi.list('', undefined, controller.signal, activeSessionId);
          if (!controller.signal.aborted) {
            const task = page.tasks.find(item => item.status === 'ACTIVE' && item.session_id === activeSessionId)
              ?? page.tasks.find(item => item.session_id === activeSessionId);
            setScheduledTask(task ? { sessionId: activeSessionId, taskId: task.id } : undefined);
          }
        } catch {
          if (!controller.signal.aborted) setScheduledTask(undefined);
        } finally { reading = false; }
      }
      if (needsRefresh) {
        needsRefresh = false;
        return void refresh();
      }
      if (!controller.signal.aborted) timer = setTimeout(refresh, 30_000);
    };
    const stopWatchingTasks = scheduledApi.onTasksChanged(activeSessionId, refresh);
    void refresh();
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      stopWatchingTasks();
      controller.abort(); clearTimeout(timer);
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [activeSessionId, visible, databaseState, projection.isRunning, runtimeStatus, scheduledApi]);
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
      if (!reopened) throw new Error('新的 runtime 已准备，但工作台尚未重新连接。');
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
  const forkConversation = useCallback(async (entryId: string): Promise<void> => {
    const sourceId = activeSessionIdRef.current;
    if (!sourceId || !canControl) return;
    invalidateSessionList();
    let outcome;
    try {
      outcome = await adapter.forkConversation(sourceId, entryId);
    } catch {
      try { await readSessionList(); } catch { /* keep uncertainty */ }
      notify('尚未确认分叉结果', '请刷新会话列表查看；不会自动重复创建。', 'warning');
      return;
    }
    if (outcome.outcome === 'NOT_CREATED') {
      notify('未创建分叉', outcome.public_code ?? '请选择已完成的最终回复。', 'warning');
      return;
    }
    try { await readSessionList(); } catch { /* child remains canonical */ }
    if (outcome.outcome === 'CREATED_OPEN_DEFERRED') {
      notify('分叉已创建，暂未打开', '稍后从会话列表打开即可，无需重新创建。', 'warning');
      return;
    }
    if (await onOpenSession(outcome.child_session_id)) {
      onNotify('分叉已打开', '已保留选定回复处的有效上下文。', 'success');
    }
    else notify('分叉已创建，暂未连接', '可以从会话列表重新打开。', 'warning');
  }, [adapter, canControl, invalidateSessionList, notify, onNotify, onOpenSession, readSessionList]);

  const readContextUsage = useCallback((signal: AbortSignal) => (
    adapter.contextUsage(activeSessionId, signal)
  ), [adapter, activeSessionId]);

  const updateModelCallBinding = async (binding: ModelCallBindingPayload): Promise<void> => {
    const active = connectionRef.current;
    const sessionId = activeSessionIdRef.current;
    if (!sessionId || !active || active.role !== 'controller') {
      throw new Error('当前窗口没有修改这个会话的权限。');
    }
    invalidateSessionList();
    suggestionCache.invalidate(sessionId);
    const accepted = await adapter.updateModelCallBinding(sessionId, binding);
    if (!ownsConnection(active)) return;
    changeSessionList((current) => current.map((session) => session.id === sessionId
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
      ? Boolean((receipt.status === 'succeeded' && receipt.planDraftDecision === 'revise')
        || (delivery && delivery.deliveryMode === 'steer' && ['PENDING', 'CONSUMED'].includes(delivery.queueStatus)))
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
    if (source.deliveryMode !== 'new-turn' || (kind === 'send' && (source.sourceKind !== 'command' || source.inputOrigin === 'SCHEDULED_TASK')) || (kind === 'edit' && source.sourceKind !== 'command' && source.inputOrigin !== 'SCHEDULED_TASK')) return;
    const active = connectionRef.current;
    if (!active || active.role !== 'controller' || !ownsConnection(active)) return;
    // CURRENT_CONTROL advances across FIFO ROOTs; the connection's initial
    // live-control snapshot can still name the ROOT present at reload.
    const current = active.current();
    const reviewInteraction = current.interaction?.kind === 'plan-draft' ? current.interaction : undefined;
    const targetTurnId = current.control.active_turns?.find(
      turn => turn.scope_kind === 'ROOT' && turn.status === 'RUNNING',
    )?.turn_id;
    if (kind === 'send') suggestionCache.invalidate(active.sessionId);
    if (kind === 'send' && !targetTurnId && !reviewInteraction) {
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
      commandId: randomIdentity('command'), source, kind,
      restoredContent,
      targetTurnId: kind === 'send' ? targetTurnId : undefined,
      reviewInteraction: kind === 'send' ? reviewInteraction : undefined,
      submittedAt: new Date().toISOString(), status: 'submitting',
    };
    setQueueActions(current => [...current.filter(item => !(item.sessionId === action.sessionId && item.source.queueItemId === source.queueItemId)), action]);
    try {
      const receipt = kind === 'send'
        ? reviewInteraction
          ? await active.resolveInteraction(reviewInteraction, { kind: 'plan-draft', decision: 'revise', sourceQueueItemId: source.queueItemId }, action.commandId) as CommandReceipt
          : await active.steerQueuedPrompt(action.commandId, source.queueItemId, targetTurnId!)
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
      if (item.kind === 'send' && item.status === 'accepted' && item.receipt?.planDraftDecision === 'revise'
        && projection.canonicalInputs.some(message => message.inputSource?.commandId === item.commandId)) return [];
      if (item.kind === 'send' && delivery) {
        if (projection.canonicalInputs.some(message => message.userKind === 'steer'
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
        && !projection.canonicalInputs.some(message => message.inputSource?.commandId === item.commandId
          && (item.receipt?.planDraftDecision === 'revise'
            || message.inputSource.queueItemId === item.receipt?.promptDelivery?.queueItemId)))
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
        const source = await connection.readQueueStatus(candidate.source.queueItemId);
        if (!ownsConnection(connection)) return;
        if (!source || source.deliveryMode !== 'new-turn' || source.status === 'PENDING') return;
        // The action may have committed between the first command read and
        // the source read. Its receipt wins over interpreting that terminal row.
        const settledReceipt = await connection.queryCommand(candidate.commandId);
        if (!ownsConnection(connection)) return;
        if (settledReceipt) {
          settleQueueAction(candidate, settledReceipt, connection);
          return;
        }
        setQueueActions(current => current.map(item => item.commandId === candidate.commandId && item.sessionId === candidate.sessionId
          ? { ...item, status: 'rejected', connectionGeneration: connection.generation }
          : item));
        queueActionsInFlight.current.delete(`${candidate.sessionId}:${candidate.source.queueItemId}`);
        notify('操作未完成', source.status === 'CONSUMED' ? '这条输入已开始处理，无法再编辑、删除或改为引导。' : source.status === 'CANCELLED' ? '这条输入已被取消，无法继续操作。' : '这条输入已被拒绝，无法继续操作。', 'warning');
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
    const commandId = randomIdentity('command');
    const deliveryMode = 'new-turn' as const;
    try {
      if (requestPlan && projection.isRunning) {
        notify('当前运行结束后才能先规划', '先规划只作用于一条尚未开始的新输入。', 'warning');
        return false;
      }
      if (requestPlan && !projection.planMode) {
        const plan = await active.enterPlan(permission);
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
        displayAsMessage: !projection.isRunning && !projection.interaction && projection.queuedCount === 0
          && !localSubmissions.some(item => item.sessionId === active.sessionId
            && ['sending', 'synchronizing', 'queued', 'unknown'].includes(item.status)),
      }]);
      suggestionCache.invalidate(active.sessionId);
      const receipt = await active.submitPrompt(commandId, content, permission);
      requirePromptReceipt(commandId, receipt);
      const accepted = promptWasAccepted(receipt);
      // The exact receipt can clear its submitted draft after detach. Only
      // the connection that sent it may publish workbench observations.
      if (!ownsConnection(active)) return accepted;
      if (accepted) wakeObservation.current?.();
      const titleHost = active.current().hostSessionId;
      if (accepted && titleHost) observeTitle(active.sessionId, titleHost);
      setLocalSubmissions((current) => current.map((item) => item.commandId === commandId
        ? submissionFromReceipt(item, receipt)
        : item));
      notifyPromptReceipt(
        receipt,
        requestPlan ? '本轮将先制定计划' : projection.isRunning ? '输入将在下一轮处理' : '输入已接受',
        projection.isRunning ? '会在当前轮完成后自动开始。' : '已送达本地服务。',
      );
      readConsumedInput(active, receipt);
      return accepted;
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
          requirePromptReceipt(commandId, recovered);
          setLocalSubmissions((current) => current.map((item) => item.commandId === commandId
            ? submissionFromReceipt(item, recovered)
            : item));
          notifyPromptReceipt(recovered, '输入已接受', '正在同步本地服务中的精确队列状态。');
          readConsumedInput(recoveredConnection, recovered);
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

  const retainStopReceipt = useCallback((item: StopObservation, receipt: CommandReceipt) => {
    if (!stopObservations.current.includes(item) || !item.reference) return;
    if (receipt.commandId !== item.reference.commandId || (receipt.targetId && receipt.targetId !== item.reference.targetId)) {
      throw new Error('停止响应与原操作身份不一致。');
    }
    item.receipt = receipt;
    item.phase = receipt.status === 'pending' ? 'pending' : 'terminal';
    item.error = undefined;
    publishStops();
  }, [publishStops]);

  useEffect(() => {
    const retained = stopObservations.current.filter(item => item.reference || (connection?.sessionId === item.target.sessionId && connection.current().hostSessionId === item.target.hostSessionId));
    if (retained.length !== stopObservations.current.length) {
      stopObservations.current = retained;
      publishStops();
    }
  }, [connection, publishStops]);

  // Present already known results even when this session is now read-only.
  // Controller authority is needed only for new reads and mutations.
  useEffect(() => {
    if (!foreground || !activeSessionId) return;
    for (const item of [...stopObservations.current]) {
      if (item.target.sessionId !== activeSessionId) continue;
      if (item.phase !== 'terminal' && projection.hostSessionId && item.target.hostSessionId !== projection.hostSessionId) {
        item.phase = 'terminal';
        item.error = '原会话运行实例已失效；不会把停止操作改投新的实例。';
      }
      if (item.phase !== 'terminal') continue;
      notify(item.receipt?.status === 'succeeded' ? '停止状态已确认' : '停止未能确认',
        item.error ?? (item.receipt ? stopResultMessage(item.receipt) : '原运行实例已不可查询。'),
        item.receipt?.status === 'succeeded' ? 'success' : 'warning');
      stopObservations.current = stopObservations.current.filter(value => value !== item);
      publishStops();
    }
  }, [activeSessionId, foreground, projection.hostSessionId, stopRevision, notify, publishStops]);

  // Query cadence is UI read scheduling, not a deadline on long-running work.
  useEffect(() => {
    if (!connection || runtimeStatus !== 'online'
      || connection.role !== 'controller'
      || !stopObservations.current.some(item => item.target.sessionId === connection.sessionId)) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => {
      if (disposed || !ownsConnection(connection)) return;
      const host = connection.current().hostSessionId;
      for (const item of [...stopObservations.current]) {
        if (item.target.sessionId !== connection.sessionId) continue;
        if (host !== item.target.hostSessionId || !item.reference || item.querying || item.phase === 'preparing' || item.phase === 'terminal'
          || performance.now() - (item.lastQueryStarted ?? -Infinity) < 1000) continue;
        item.querying = true;
        item.lastQueryStarted = performance.now();
        const reference = item.reference;
        void connection.queryControlCommand(reference).then(query => {
          if (!stopObservations.current.includes(item)) return;
          if (query.status === 'FOUND' && query.receipt) retainStopReceipt(item, query.receipt);
          else {
            item.phase = query.status === 'OWNER_UNAVAILABLE' ? 'terminal' : 'unknown';
            item.error = query.status === 'OWNER_UNAVAILABLE'
              ? '原 Host 已失效；不会把这次操作改投新的 Host。'
              : item.error?.includes('未查到原操作结果') ? item.error
                : `${item.error ? `${item.error} ` : ''}未查到原操作结果；Pulsara 不会自动重发。`;
            publishStops();
          }
        }).catch(error => {
          if (!stopObservations.current.includes(item)) return;
          item.phase = 'unknown';
          item.error = productMessage(error instanceof Error ? error.message : undefined, '停止结果暂时无法查询；不会自动重发。');
          publishStops();
        }).finally(() => { item.querying = false; });
      }
      timer = setTimeout(poll, 1000);
    };
    poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [connection, runtimeStatus, stopRevision, ownsConnection, notify, publishStops, retainStopReceipt]);

  const stopRun = async (explicitNew = false) => {
    const active = connectionRef.current;
    if (!active || active.role !== 'controller' || !ownsConnection(active)
      || active.sessionId !== activeSessionId) return;
    // Freeze the target displayed by this render; a newer adapter observation
    // must not silently redirect a click to a successor the user has not seen.
    const current = projection;
    const targetId = current.activeTurnId ?? current.manualCompactionCommandId;
    if (!targetId || !current.hostSessionId || active.current().hostSessionId !== current.hostSessionId) return;
    const target: UserControlTarget = Object.freeze({
      sessionId: active.sessionId, hostSessionId: current.hostSessionId,
      operation: current.activeTurnId ? 'STOP_ACTIVE_TURN' : 'STOP_CONTEXT_COMPACTION',
      targetKind: current.activeTurnId ? 'ROOT_TURN' : 'MANUAL_COMPACTION', targetId,
    });
    const previous = stopObservations.current.filter(item => item.target.sessionId === target.sessionId
      && item.target.hostSessionId === target.hostSessionId && item.target.targetId === targetId
      && item.target.operation === target.operation).at(-1);
    if (previous && (previous.phase === 'preparing' || previous.phase === 'pending'
      || (previous.phase === 'unknown' && !explicitNew))) { publishStops(); return; }
    const item: StopObservation = { target, phase: 'preparing' };
    stopObservations.current.push(item);
    publishStops();
    try {
      const reference = await active.prepareUserControlCommand(target);
      if (!stopObservations.current.includes(item)) return;
      if (!ownsConnection(active)) {
        stopObservations.current = stopObservations.current.filter(value => value !== item);
        publishStops();
        return;
      }
      item.reference = reference;
      // Submission remains in preparing until its response; no query can race
      // ahead of dispatch and interpret a missing attempt as an outcome.
      const receipt = await (target.operation === 'STOP_ACTIVE_TURN'
        ? active.stopActiveTurn(reference) : active.stopContextCompaction(reference));
      retainStopReceipt(item, receipt);
    } catch (error) {
      if (!stopObservations.current.includes(item)) return;
      const message = productMessage(error instanceof Error ? error.message : undefined, '停止请求暂时无法确认。');
      if (!item.reference) {
        stopObservations.current = stopObservations.current.filter(value => value !== item);
        if (ownsConnection(active)) notify('暂时无法停止', message, 'warning');
      } else {
        item.phase = 'unknown';
        item.error = message;
        void recoverConnectionAfterOperation(error, active);
      }
      publishStops();
    }
  };
  const visibleStopObservations = stopObservations.current.filter(item => item.target.sessionId === activeSessionId
    && item.target.hostSessionId === projection.hostSessionId && item.phase !== 'terminal');
  const currentStop = visibleStopObservations.filter(item => item.target.targetId === (projection.activeTurnId ?? projection.manualCompactionCommandId)).at(-1);

  const compact = async () => {
    const active = connectionRef.current;
    if (!active || active.role !== 'controller') return;
    try {
      suggestionCache.invalidate(active.sessionId);
      const receipt = await active.compactContext();
      if (!ownsConnection(active)) return;
      const alreadyCompact = receipt.status === 'succeeded' && receipt.publicCode === 'NOT_NEEDED';
      if (receipt.status === 'pending') wakeObservation.current?.();
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
          notify('决定结果未确认');
        }
      }).catch(() => { /* Keep this command unknown; other queries still advance. */ }).finally(() => {
        if (toolDecisionQueries.current.get(intent.commandId) !== attempt) return;
        attempt.inFlight = false;
        const latest = readToolDecisions().find(item => item.commandId === intent.commandId);
        if (latest && ownsConnection(connection)
          && (latest.status === 'unknown' || (latest.status === 'submitting'
            && latest.connectionGeneration !== connection.generation))) {
          // Reconsider a newer cut that arrived during this query. The same
          // completed cut remains remembered, so an empty read cannot poll itself.
          saveToolDecision({ ...latest, status: 'unknown' });
        }
      });
    }
  }, [connection, toolDecisions, projection.eventSequence, projection.liveControlRevision, projection.interaction, acceptToolDecision, saveToolDecision, readToolDecisions, ownsConnection, notify]);

  const submitToolDecision = useCallback(async (
    active: RuntimeConnection, interaction: Extract<RuntimeInteractionSummary, { kind: 'tool-confirmation' }>,
    decision: 'allow' | 'deny',
  ): Promise<boolean> => {
    const hostSessionId = active.current().hostSessionId;
    if (!hostSessionId || interaction.decisionInProgress
      || !interaction.expiresAtUtc || Date.parse(interaction.expiresAtUtc) <= Date.now()
      || readToolDecisions().some(item => item.sessionId === active.sessionId
        && item.hostSessionId === hostSessionId && item.interactionId === interaction.id
        && item.status !== 'rejected')) return false;
    const intent: ToolDecisionIntent = {
      sessionId: active.sessionId, hostSessionId, interactionId: interaction.id,
      commandId: randomIdentity('command'), decision,
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
  }, [acceptToolDecision, saveToolDecision, readToolDecisions, ownsConnection, notify, recoverConnectionAfterOperation]);

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
          revise: ['修改意见已提交', 'Pulsara 将继续规划。'],
          cancel: ['本次审批已取消', 'Pulsara 将继续规划。'],
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

  const readToolOutput = useCallback(async (resultEntryId: string, position: ToolOutputPage['position']) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
    const page = await active.readToolOutput(resultEntryId, position);
    if (!ownsHistoryReader(active)) {
      throw new RuntimeApiError('TOOL_OUTPUT_OWNER_CHANGED', '工具输出所属的会话已经改变。', true);
    }
    return page;
  }, [ownsHistoryReader]);

  const locateAnnotationSource = useCallback(async (entryId: string, signal: AbortSignal) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active?.locateAnnotationSource) throw new Error('本地服务未连接。');
    const readSignal = AbortSignal.any([signal, historyReadAbort.current.signal]);
    const next = await active.locateAnnotationSource(entryId, readSignal);
    readSignal.throwIfAborted();
    if (!ownsHistoryReader(active)) throw new Error('会话已经切换。');
    publishProjection(next, active instanceof CanonicalHistoryView ? undefined : active);
  }, [ownsHistoryReader, publishProjection]);

  const loadHistory = useCallback(async (windowId?: string, direction?: 'older' | 'newer') => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active?.readHistory) throw new Error('本地服务未连接。');
    const signal = historyReadAbort.current.signal;
    const next = await active.readHistory(windowId, direction, signal);
    signal.throwIfAborted();
    if (!ownsHistoryReader(active)) throw new Error('会话已经切换。');
    publishProjection(next, active instanceof CanonicalHistoryView ? undefined : active);
  }, [ownsHistoryReader, publishProjection]);

  const readLatestHistory = useCallback(async (signal: AbortSignal) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active?.readLatestHistory) throw new Error('本地服务未连接。');
    const readSignal = AbortSignal.any([historyReadAbort.current.signal, signal]);
    const next = await active.readLatestHistory(readSignal);
    readSignal.throwIfAborted();
    if (!ownsHistoryReader(active)) throw new Error('会话已经切换。');
    publishProjection(next, active instanceof CanonicalHistoryView ? undefined : active);
  }, [ownsHistoryReader, publishProjection]);

  const readProcessEnd = useCallback(async (entryId: string, signal: AbortSignal) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active?.readProcessEnd) throw new Error('本地服务未连接。');
    const readSignal = AbortSignal.any([historyReadAbort.current.signal, signal]);
    const next = await active.readProcessEnd(entryId, readSignal);
    readSignal.throwIfAborted();
    if (!ownsHistoryReader(active)) throw new Error('会话已经切换。');
    publishProjection(next, active instanceof CanonicalHistoryView ? undefined : active);
  }, [ownsHistoryReader, publishProjection]);

  const hydrateEntries = useCallback(async (ids: string[], includeReasoning = false) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active?.hydrateEntries) throw new Error('本地服务未连接。');
    const signal = historyReadAbort.current.signal;
    const next = await active.hydrateEntries(ids, signal, includeReasoning);
    signal.throwIfAborted();
    if (!ownsHistoryReader(active)) throw new Error('会话已经切换。');
    publishProjection(next, active instanceof CanonicalHistoryView ? undefined : active);
  }, [ownsHistoryReader, publishProjection]);

  const retainHistory = useCallback((interest: HistoryResidence) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active || !ownsHistoryReader(active)) return;
    const next = active.retainHistory?.(interest);
    if (next) publishProjection(next, active instanceof CanonicalHistoryView ? undefined : active);
  }, [ownsHistoryReader, publishProjection]);

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
  }, [ownsHistoryReader]);

  const readVisualizationThumbnail = useCallback(async (
    entryId: string, ordinal: number, digest: string, size: number, signal: AbortSignal,
  ) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active) throw new Error('本地服务未连接。');
    const image = await active.readVisualizationThumbnail(entryId, ordinal, digest, size, signal);
    if (!ownsHistoryReader(active)) throw new Error('可视化所属的会话已经改变。');
    return image;
  }, [ownsHistoryReader]);

  const readImageReference = useCallback(async (ref: string, signal: AbortSignal) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active) throw new Error('本地服务未连接。');
    const image = await active.readImageReference(ref, signal);
    if (!ownsHistoryReader(active)) throw new Error('图片所属工作区已经改变。');
    return image;
  }, [ownsHistoryReader]);

  const readVisualization = useCallback(async (
    entryId: string, ordinal: number, digest: string, size: number, item?: import('../lib/pulsara-types').VisualizationOccurrence, signal?: AbortSignal,
  ) => {
    const active = connectionRef.current ?? historyRef.current;
    if (!active) {
      throw new RuntimeApiError(
        'LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true,
      );
    }
    const html = await active.readVisualizationContent(entryId, ordinal, digest, size, item, signal);
    if (!ownsHistoryReader(active)) {
      throw new RuntimeApiError(
        'VISUALIZATION_OWNER_CHANGED', '可视化所属的会话已经改变。', true,
      );
    }
    return html;
  }, [ownsHistoryReader]);


  const retryHistory = useCallback(() => { void loadHistory().catch(() => {}); }, [loadHistory]);
  const retry = useCallback(() => {
    if (initialHistoryPending) retryHistory();
    else void openRuntimeSession(sessionId, true, false, true);
  }, [initialHistoryPending, openRuntimeSession, retryHistory, sessionId]);

  const approvalPending = Boolean(projection.interaction);
  useEffect(() => {
    if (!sessionId) return;
    onObservation(sessionId, {
      status: runtimeStatus,
      role: connectionRole,
      error: runtimeError ?? (initialHistoryPending ? projection.history?.initialError : undefined),
      failure: runtimeFailure,
      ready: Boolean(activeReader && !initialHistoryPending),
      historyOnly: activeReader instanceof CanonicalHistoryView,
      approvalPending,
      running: projection.isRunning || Boolean(projection.manualCompactionCommandId),
      retry,
    });
  }, [activeReader, approvalPending, connectionRole, initialHistoryPending, onObservation, projection.history?.initialError,
    projection.isRunning, projection.manualCompactionCommandId, retry, runtimeError, runtimeFailure, runtimeStatus, sessionId]);

  if (sessionId && !loaded) return null;
  return (
    <DesktopWorkspaceProvider visible={visible} browserVisible={visible && inspectorOpen} onBrowserShortcut={onBrowserShortcut} sessionId={activeSessionId} onShow={() => setInspectorOpen(true)}>
    <FilePreviewProvider mediaRanges={connection ? projection.mediaRanges : []} mediaReadStarts={connection ? projection.mediaReadStarts : []} mediaAnnotationsRevision={projection.mediaAnnotationsRevision} sessionId={activeSessionId} imageOwnerKey={activeSessionId} readImageReference={readImageReference} ownerKey={connection ? `${connection.sessionId}:${connection.generation}` : 'disconnected'} api={connection?.filePreview} onNotify={notify}>
    <PresentationProvider connection={connection} visible={visible && !initialHistoryPending} isOpen={inspectorOpen} onNotify={notify}>
    <div className="session-workbench-body">
        <WorkbenchView
          onCompletePaths={(prefix, cursor, signal) => adapter.completeWorkspacePaths(activeSession.id, prefix, cursor, signal)}
          onListMcpPrompts={async (query, offset, signal) => {
            const connection = connectionRef.current;
            if (!connection || connection.sessionId !== activeSession.id) throw new Error('会话尚未连接。');
            return connection.listMcpPrompts(query, offset, signal);
          }}
          onPreviewMcpPrompt={async (target, args, signal) => {
            const connection = connectionRef.current;
            if (!connection || connection.sessionId !== activeSession.id) throw new Error('会话尚未连接。');
            return connection.previewMcpPrompt(target, args, signal);
          }}
          workspace={activeWorkspace}
          session={activeSession}
          messages={renderedMessages}
          history={projection.history}
          onLoadHistory={loadHistory}
          onReadLatestHistory={readLatestHistory}
          onReadProcessEnd={readProcessEnd}
          onHydrateEntries={hydrateEntries}
          onRetainHistory={retainHistory}
          interruptionNotices={mergedProjection.interruptionNotices}
          contextCompaction={mergedProjection.contextCompaction}
          contextCompactionInProgress={mergedProjection.liveControl.root_compaction_in_progress === true}
          initialContextBase={mergedProjection.initialContextBase}
          onFork={forkConversation}
          todo={projection.todo}
          activePlanMode={projection.planMode}
          providerRetry={projection.providerRetry}
          isRunning={projection.isRunning}
          activeTurnId={projection.activeTurnId}
          manualCompactionCommandId={projection.manualCompactionCommandId}
          inspectorOpen={inspectorOpen}
          queuedCount={projection.queuedCount}
          queuedPrompts={renderedQueuedPrompts}
          steerNextCursor={pendingSteerReading.nextCursor}
          steerLoading={pendingSteerReading.loading}
          steerReadError={pendingSteerReading.error}
          onLoadMoreSteers={pendingSteerReading.more}
          queueActions={queueActions.filter(item => item.sessionId === activeSession.id && item.connectionGeneration === connection?.generation)}
          onQueueAction={actOnQueuedPrompt}
          onQueueActionHandled={(commandId) => setQueueActions(current => current.map(item => item.commandId === commandId ? { ...item, handled: true } : item))}
          localSubmissions={renderedSubmissions}
          runtimeStatus={runtimeStatus}
          runtimeError={sessionId ? runtimeError : userServiceError}
          runtimeErrorDetails={runtimeFailure?.details}
          runtimeRecoveryBlocked={runtimeFailure?.requiresRestart}
          modelConfigurations={bootstrap?.model_configurations ?? []}
          modelCatalogLoading={bootstrap?.model_catalog_loading}
          modelCallBinding={activeSession.modelCallBinding}
          onReadContextUsage={readContextUsage}
          contextUsageRevision={`${projection.hostSessionId ?? ''}:${projection.eventSequence}:${Boolean(projection.liveControl.compaction_in_progress)}:${JSON.stringify(bootstrap?.model_configurations)}`}
          interaction={projection.interaction}
          toolDecisionPending={toolDecisions.some(item => item.sessionId === connection?.sessionId
            && item.hostSessionId === projection.hostSessionId && item.interactionId === projection.interaction?.id
            && item.status !== 'rejected')}
          canControl={canControl}
          historyOnly={historyRef.current?.sessionId === activeSessionId}
          runtimeClosing={historyRef.current?.sessionId === activeSessionId && historyRef.current.runtimeClosing}
          runtimeCloseFailed={historyRef.current?.sessionId === activeSessionId && historyRef.current.runtimeCloseFailed}
          isObserver={isObserver}
          workspaceAvailability={workspaceAvailability}
          workspaceRecoveryBusy={workspaceRecoveryBusy}
          workspaceRecoveryError={workspaceRecoveryError}
          onRestoreWorkspace={restoreWorkspace}
          onRecheckWorkspace={recheckWorkspace}
          skills={activeSkills}
          onReconnect={reconnect}
          onTakeControl={() => activeSessionId && void openRuntimeSession(activeSessionId, true, true)}
          onToggleInspector={() => setInspectorOpen((value) => !value)}
          onOpenModelSettings={() => onOpenSettings('models')}
          suggestion={suggestion}
          onModelCallBindingChange={updateModelCallBinding}
          onSend={sendPrompt}
          onStop={() => void stopRun()}
          stopControl={visibleStopObservations.length ? {
            busy: currentStop?.phase === 'preparing' || currentStop?.phase === 'pending',
            message: currentStop?.phase === 'unknown' ? currentStop.error ?? '停止结果待确认。'
              : currentStop ? '正在停止本轮运行…' : '之前的停止结果仍待确认。',
            onRetry: currentStop?.phase === 'unknown' ? () => void stopRun(true) : undefined,
            onDismiss: () => {
              stopObservations.current = stopObservations.current.filter(item => !visibleStopObservations.includes(item));
              publishStops();
            },
          } : undefined}
          onCompact={compact}
          onRenameSession={() => onRenameSession(activeSession)}
          onOpenScheduledTask={runtimeStatus === 'online' && scheduledTask?.sessionId === activeSession.id
            ? () => onOpenScheduledTask(scheduledTask.taskId) : undefined}
          onReopenRuntime={() => void reopenRuntime()}
          onCloseSession={sessionId ? () => onClose(sessionId) : undefined}
          runtimeReopenBusy={runtimeReopenBusy}
          onReadInteraction={readInteraction}
          onResolveInteraction={resolveInteraction}
          outputOwnerKey={`${connection?.sessionId ?? ''}:${connection?.generation ?? 0}`}
          onReadToolOutput={readToolOutput}
          onReadPromptImage={readPromptImage}
          onLocateAnnotation={locateAnnotationSource}
          onReadVisualization={readVisualization}
          onReadVisualizationThumbnail={readVisualizationThumbnail}
          promptDraftStore={promptDraftStore}
          onNotify={notify}
          permission={turnPermission}
          onPermissionChange={setTurnPermission}
        />
        {sessionId && <InspectorPanel
          adapter={adapter}
          sessionContentAvailable={!initialHistoryPending}
          projectMcpForms={{
            preview: (input) => adapter.previewMcpImport(input),
            test: (input) => adapter.testProjectMcp(activeSessionId, input),
            import: async (input) => {
              const result = await adapter.importProjectMcp(activeSessionId, input);
              adoptCapabilityMutation(activeSessionId, result.capabilities);
              notify(result.operation.success ? '项目 MCP 已导入' : 'MCP 未导入', result.operation.message, result.operation.success ? 'success' : 'warning');
              return result.operation.success;
            },
            authorizationKey: server => mcpAuthorizationKey('project', activeSessionId, server.id, server.configIdentity ?? ''),
            authorize: async (server, action) => {
              await runMcpAuthorization(mcpAuthorizationKey('project', activeSessionId, server.id, server.configIdentity ?? ''), action,
                next => adapter.projectMcpAuthorization(activeSessionId, server.id, next));
            },
          }}
          session={activeSession}
          isOpen={inspectorOpen}
          modelConfigurations={bootstrap?.model_configurations ?? []}
          onReadToolOutput={readToolOutput}
          onReadPromptImage={readPromptImage}
          onReadVisualization={readVisualization} onReadVisualizationThumbnail={readVisualizationThumbnail}
          canControl={canControl}
          capabilities={capabilities}
          capabilityLoading={capabilityLoading}
          capabilityError={capabilityError}
          capabilityBusy={capabilityBusy}
          backgroundOwnerKey={`${connection?.sessionId ?? ''}:${connection?.generation ?? 0}:${projection.hostSessionId ?? ''}`}
          backgroundHostSessionId={projection.hostSessionId}
          onLoadBackgroundProcesses={loadBackgroundProcesses}
          onReadBackgroundProcessLog={async (processId, cursor) => {
            const active = connectionRef.current;
            if (!active) throw new RuntimeApiError('LOCAL_CONNECTION_UNAVAILABLE', '本地服务未连接。', true);
            const host = active.current().hostSessionId;
            if (active.sessionId !== activeSessionId || host !== projection.hostSessionId) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '原进程实例已失效。', false);
            const page = await active.readBackgroundProcessLog(processId, cursor);
            if (!ownsConnection(active) || active.current().hostSessionId !== host) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '后台命令所属的会话已经改变。', true);
            return page;
          }}
          onPrepareBackgroundControl={async (target) => {
            const active = connectionRef.current;
            if (!active || active.role !== 'controller' || active.sessionId !== target.sessionId
              || active.current().hostSessionId !== target.hostSessionId) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '原进程实例已失效。', false);
            const reference = await active.prepareUserControlCommand(target);
            if (!ownsConnection(active) || active.current().hostSessionId !== target.hostSessionId) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '原进程实例已失效。', false);
            return reference;
          }}
          onTerminateBackgroundProcess={async (reference) => {
            const active = connectionRef.current;
            if (!active || active.role !== 'controller') throw new RuntimeApiError('CONTROL_UNAVAILABLE', '当前窗口没有控制权限。', false);
            if (active.sessionId !== reference.sessionId || active.current().hostSessionId !== reference.hostSessionId) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '原进程实例已失效。', false);
            return active.terminateBackgroundProcess(reference);
          }}
          onQueryControl={async (reference) => {
            const active = connectionRef.current;
            if (!active || active.role !== 'controller') {
              throw new RuntimeApiError('CONTROL_UNAVAILABLE', '当前窗口没有控制权限。', false);
            }
            if (active.sessionId !== reference.sessionId || active.current().hostSessionId !== reference.hostSessionId) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '原进程实例已失效。', false);
            // The workspace record accepts an exact late receipt independently
            // from whichever session is now visible.
            return active.queryControlCommand(reference);
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
          onReconnectProjectMcp={async server => {
            const active = connectionRef.current;
            if (!active || active.sessionId !== activeSessionId || active.current().hostSessionId !== projection.hostSessionId) { notify('连接实例已改变', '请重新打开当前连接后操作。', 'warning'); return; }
            await reconnectProjectMcp(server);
          }}
          onPickDirectory={() => adapter.pickWorkspaceDirectory()}
          onOpenUserCapabilities={onOpenUserCapabilities}
          onShowProjectScopeNotice={kind => {
            const label = kind === 'skills' ? '技能' : 'MCP';
            notify(`这里添加的${label}仅会应用到当前目录中`, {
              before: `如果需要添加用户级${label}，请在`, label: '能力页', after: '添加',
              href: `#capabilities/${kind}`,
              onClick: () => onOpenUserCapabilities(kind),
            });
          }}
          onOpenHomeSettings={() => onOpenSettings('general', true)}
          onNotify={notify}
          onClose={() => setInspectorOpen(false)}
        />}

    </div>
    </PresentationProvider>
    </FilePreviewProvider>
    </DesktopWorkspaceProvider>
  );
}
