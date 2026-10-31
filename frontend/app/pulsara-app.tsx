import { usePendingSteerPages } from '../lib/pending-steer-pages';
import { McpAuthorizationContext, mcpAuthorizationKey, useMcpAuthorization, useModelMcpAuthorization } from '../components/mcp-authorization';
import { PresentationProvider } from '../components/presentation-provider';
import { flushSync } from 'react-dom';
import { uiPreferences } from '../lib/ui-preferences';
import { randomIdentity, isCanonicalIdentity } from '../lib/identity';

import { DesktopWorkspaceProvider, type WorkspaceControl } from '../components/desktop-workspace';
import { DesktopTitlebar, DesktopWindowControls } from '../components/desktop-titlebar';
import { desktopHost } from '../lib/desktop-host';
import { FilePreviewProvider } from '../components/file-browser';
import { UserAvatarProvider } from '../components/user-avatar';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityRail } from '../components/activity-rail';
import { CapabilityView, type CapabilityTab } from '../components/capability-view';
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
import { SessionSuggestionCache, useSessionEphemeralResults } from '../lib/session-ephemeral-results';
import {
  LocalHttpRuntimeAdapter,
  CanonicalHistoryView,
  compareSequence,
  projectRootStatus,
  type WorkspaceAvailability,
  createUserControlCommandRef,
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
import { promptContentTextProjection } from '../lib/prompt-content';
import type {
  PluginImportOptions,
  ArchivedSessionDeletionItem,
  AppView,
  CapabilitySnapshot,
  CapabilityOperation,
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

function capabilityAdoptionMessages(adoption: UserCapabilitySnapshot['adoption']): string[] {
  const messages: string[] = [];
  if (adoption?.attentionSessions) messages.push(`${adoption.attentionSessions} 个已打开会话需要稍后重试。`);
  if (adoption?.pendingSessions) messages.push(`${adoption.pendingSessions} 个会话将在下次模型请求前的安全时机载入。`);
  return messages;
}

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

function readSavedTheme(): 'light' | 'dark' {
  return uiPreferences.getItem('pulsara-theme') === 'dark' ? 'dark' : 'light';
}

function readSavedSessionId(): string {
  const saved = uiPreferences.getItem('pulsara-active-session') ?? '';
  if (saved && !isCanonicalIdentity(saved, 'session')) {
    uiPreferences.removeItem('pulsara-active-session');
    return '';
  }
  return saved;
}

function saveSessionId(sessionId: string): void {
  uiPreferences.setItem('pulsara-active-session', sessionId);
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

interface PulsaraAppProps {
  adapter?: RuntimeAdapter;
  onStartupSettled?: () => void;
}

type ToolDecisionIntent = {
  sessionId: string; hostSessionId: string; interactionId: string;
  commandId: string; decision: 'allow' | 'deny'; connectionGeneration: number;
  status: 'submitting' | 'unknown' | 'accepted' | 'rejected' | 'retired';
};

export default function PulsaraApp({ adapter = defaultAdapter, onStartupSettled }: PulsaraAppProps) {
  const [startupSettled, setStartupSettled] = useState(false);
  useEffect(() => {
    if (startupSettled) onStartupSettled?.();
  }, [startupSettled, onStartupSettled]);
  const workspaceControl = useRef<WorkspaceControl | null>(null);
  const [promptDraftStore] = useState(() => new PromptDraftStore());
  const [suggestionCache] = useState(() => new SessionSuggestionCache());
  const scheduledApi = useMemo(() => new LocalScheduledTasksApi(), []);
  const [activeView, setActiveView] = useState<AppView>('overview');
  const [capabilityTab, setCapabilityTab] = useState<CapabilityTab>('plugins');
  const [settingsInitialSection, setSettingsInitialSection] = useState<SettingsSection>();
  const [settingsHighlightHome, setSettingsHighlightHome] = useState(false);
  const [bootstrap, setBootstrap] = useState<RuntimeBootstrap>();
  const [sessionList, setSessionList] = useState<SessionSummary[]>([]);
  // These disposable read owners belong to the device API, not a selected chat.
  const globalReads = useMemo(() => ({ adapter, active: true, configuration: { databaseReady: false }, sessions: {} }), [adapter]);
  useEffect(() => {
    globalReads.active = true;
    return () => {
      globalReads.active = false;
      globalReads.configuration = { databaseReady: false };
      globalReads.sessions = {};
    };
  }, [globalReads]);
  const invalidateSessionList = useCallback(() => { globalReads.sessions = {}; }, [globalReads]);
  const changeSessionList = useCallback((next: SessionSummary[] | ((current: SessionSummary[]) => SessionSummary[])) => {
    invalidateSessionList();
    if (globalReads.active) setSessionList(next);
  }, [globalReads, invalidateSessionList]);
  const [sessionRevision, setSessionRevision] = useState(0);
  const archivingSession = useRef<string | undefined>(undefined);
  const [renameTarget, setRenameTarget] = useState<SessionSummary>();
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary>();
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string>();
  const deletingSession = useRef<string | undefined>(undefined);
  const [bulkDeleteTarget, setBulkDeleteTarget] = useState<SessionSummary[]>();
  const [bulkDeletion, setBulkDeletion] = useState<{ items: ArchivedSessionDeletionItem[]; running: boolean; refreshError?: string }>();
  const [bulkResultsOpen, setBulkResultsOpen] = useState(false);
  const [bulkNoticeDismissed, setBulkNoticeDismissed] = useState(false);
  const bulkDeletionOwner = useRef<{ active: boolean; running: boolean; items: ArchivedSessionDeletionItem[] } | undefined>(undefined);
  useEffect(() => () => {
    if (bulkDeletionOwner.current) bulkDeletionOwner.current.active = false;
    bulkDeletionOwner.current = undefined;
  }, [adapter]);
  const requestedSession = useRef('');
  const [activeSessionId, setActiveSessionId] = useState('');
  const [activeSchedule, setActiveSchedule] = useState<{ sessionId: string; taskId: string }>();
  const [focusScheduledTaskId, setFocusScheduledTaskId] = useState<string>();
  // Selection feedback is local UI state; only a connected runtime becomes active.
  const [openingSessionId, setOpeningSessionId] = useState('');
  const [projection, setProjection] = useState<RuntimeProjection>(emptyProjection);
  const [queuePages, setQueuePages] = useState<{owner: RuntimeConnection | CanonicalHistoryView; items: QueuedPrompt[]; absent: Set<string>}>();
  const queuePagesRef = useRef(queuePages);
  queuePagesRef.current = queuePages;
  const queueReadGeneration = useRef(0);
  const [capabilities, setCapabilities] = useState<CapabilitySnapshot>();
  const [capabilityLoading, setCapabilityLoading] = useState(false);
  const [capabilityError, setCapabilityError] = useState<string>();
  const [capabilityBusy, setCapabilityBusy] = useState<string>();
  const capabilityAttempt = useRef(0);
  const [userCapabilities, setUserCapabilities] = useState<UserCapabilitySnapshot>();
  const [userCapabilityLoading, setUserCapabilityLoading] = useState(false);
  const [userCapabilityError, setUserCapabilityError] = useState<string>();
  const userCapabilityAttempt = useRef(0);
  const userCapabilitiesRef = useRef<UserCapabilitySnapshot | undefined>(undefined);
  const completeUserPlugins = useRef<UserCapabilitySnapshot['plugins'] | undefined>(undefined);
  const userCapabilityLoads = useRef<Promise<void>>(Promise.resolve());
  const refreshVisibleUserCapabilities = useRef<(() => void) | undefined>(undefined);
  const userCapabilityReadAbort = useRef<AbortController | undefined>(undefined);
  const userCapabilityPageAbort = useRef<AbortController | undefined>(undefined);
  const [connection, setConnection] = useState<RuntimeConnection>();
  const historyRef = useRef<CanonicalHistoryView | undefined>(undefined);
  const historyReadAbort = useRef(new AbortController());
  const [workspaceObservation, setWorkspaceObservation] = useState<{sessionId: string; value: WorkspaceAvailability}>();
  const [workspaceRecoveryBusy, setWorkspaceRecoveryBusy] = useState(false);
  const [workspaceRecoveryError, setWorkspaceRecoveryError] = useState<string>();

  const connectionRef = useRef<RuntimeConnection | undefined>(undefined);
  useEffect(() => {
    promptDraftStore.setImporter(async (sessionId, files, signal) => {
      const active = connectionRef.current;
      if (!active || active.sessionId !== sessionId) throw new Error('文件所属会话未连接，请切回该会话后重试。');
      return active.importFiles(files, signal);
    });
  }, [promptDraftStore]);
  const activeSessionIdRef = useRef('');
  const connectionAttempt = useRef(0);
  const connectionInFlight = useRef<{
    sessionId: string; reconnecting: boolean; takeover: boolean; attempt: number;
    promise: Promise<RuntimeConnection | undefined>;
  } | undefined>(undefined);
  const promptReconciliationInFlight = useRef(new Set<string>());
  // Global API readiness is independent of an individual conversation transport.
  const [userServiceStatus, setUserServiceStatus] = useState<RuntimeStatus>('starting');
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus>('starting');
  const [runtimeError, setRuntimeError] = useState<string>();
  const [runtimeFailure, setRuntimeFailure] = useState<{details: string; requiresRestart: boolean}>();
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
  const [inspectorOpen, setInspectorOpenState] = useState(() => uiPreferences.getItem('pulsara-workspace-open') === 'true');
  const inspectorOpenRef = useRef(inspectorOpen);
  const setInspectorOpen = useCallback((next: boolean | ((current: boolean) => boolean)) => {
    const value = typeof next === 'function' ? next(inspectorOpenRef.current) : next;
    inspectorOpenRef.current = value;
    setInspectorOpenState(value);
    uiPreferences.setItem('pulsara-workspace-open', String(value));
  }, []);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const desktop = window.matchMedia('(min-width: 1221px)');
    const onResize = (event: MediaQueryListEvent) => {
      if (!event.matches) setInspectorOpen(false);
    };
    desktop.addEventListener('change', onResize);
    return () => desktop.removeEventListener('change', onResize);
  }, [setInspectorOpen]);
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    const saved = uiPreferences.getItem('pulsara-sidebar-open');
    return saved === null ? Boolean(window.matchMedia?.('(min-width: 1151px)').matches) : saved === 'true';
  });
  useEffect(() => { uiPreferences.setItem('pulsara-sidebar-open', String(sidebarOpen)); }, [sidebarOpen]);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const desktop = window.matchMedia('(min-width: 1151px)');
    const onResize = (event: MediaQueryListEvent) => {
      if (!event.matches) setSidebarOpen(false);
    };
    desktop.addEventListener('change', onResize);
    return () => desktop.removeEventListener('change', onResize);
  }, []);
  const [searchOpen, setSearchOpen] = useState(false);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  useEffect(() => () => promptDraftStore.destroy(), [promptDraftStore]);
  const [theme, setTheme] = useState<'light' | 'dark'>(readSavedTheme);
  const [showBuiltinToolResults, setShowBuiltinToolResults] = useState(readSavedToolResultDisplay);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const [turnPermission, setTurnPermission] = useState<PermissionMode>('bypass-permissions');
  const databaseState = bootstrap?.database_state;
  const databaseBlocked = databaseState !== undefined && databaseState !== 'ready';
  const canCreateSession = userServiceStatus === 'online' && databaseState === 'ready';

  const ownsHistoryReader = useCallback((reader: RuntimeConnection | CanonicalHistoryView): boolean => (
    activeSessionIdRef.current === reader.sessionId
    && (reader instanceof CanonicalHistoryView ? historyRef.current === reader : connectionRef.current === reader)
  ), []);

  const ownsConnection = useCallback((expected: RuntimeConnection): boolean => (
    connectionRef.current === expected
    && activeSessionIdRef.current === expected.sessionId
    && connectionRef.current.generation === expected.generation
  ), []);

  const notify = useCallback((
    title: string,
    detail?: ToastMessage['detail'],
    tone: ToastMessage['tone'] = 'neutral',
  ) => {
    const id = Date.now() + Math.floor(Math.random() * 1000);
    setToasts((current) => [...current, { id, title, detail, tone }]);
    window.setTimeout(
      () => setToasts((current) => current.filter((toast) => toast.id !== id)),
      4200,
    );
  }, []);

  const notifyUserCapabilityResult = useCallback((title: string, result: {
    operation: CapabilityOperation; capabilities: UserCapabilitySnapshot;
  }) => {
    const detail = [result.operation.message, ...capabilityAdoptionMessages(result.capabilities.adoption)].filter(Boolean).join(' ');
    notify(title, detail, result.operation.success && !result.capabilities.adoption?.attentionSessions ? 'success' : 'warning');
  }, [notify]);

  const {
    statuses: mcpAuthorizationStatuses, run: runMcpAuthorization,
    invalidate: invalidateMcpAuthorization,
  } = useMcpAuthorization(notify);
  useModelMcpAuthorization(connection, adapter, runMcpAuthorization);

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

  const activeReader = connection ?? historyRef.current;
  const pendingSteerReading = usePendingSteerPages(activeReader, projection);
  useEffect(() => {
    const reader = activeReader;
    if (!reader || !activeSessionId || reader.sessionId !== activeSessionId) return;
    const controller = new AbortController();
    const generation = ++queueReadGeneration.current;
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
      if (!controller.signal.aborted && generation === queueReadGeneration.current && ownsHistoryReader(reader)) {
        setQueuePages({owner:reader, items:[...found.values()], absent});
      }
    })().catch(() => { /* A failed read retains the last confirmed queue. */ });
    return () => controller.abort();
  }, [activeReader, activeSessionId, ownsHistoryReader, projection.eventSequence, projection.queuedPrompts]);

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

  const acceptUserCapabilities = useCallback((next: UserCapabilitySnapshot) => {
    const inventory = { ...next };
    delete inventory.adoption;
    userCapabilitiesRef.current = inventory;
    // An unavailable inspection cannot prove that a saved owner disappeared.
    // Retain the last complete values so recovery can still detect replacements.
    if (next.plugins.status === 'ready') {
      for (const plugin of completeUserPlugins.current?.items ?? []) {
        const current = next.plugins.items.find(item => item.id === plugin.id && item.packageInstallId === plugin.packageInstallId);
        for (const connection of plugin.mcpConnections ?? []) {
          const replacement = current?.mcpConnections?.find(item => item.serverId === connection.serverId);
          // Compare complete existing values, not a new identity/fingerprint. An
          // external overlay change or package removal also retires old UI auth.
          if (!replacement || JSON.stringify([connection.config, connection.overlay]) !== JSON.stringify([replacement.config, replacement.overlay])) {
            invalidateMcpAuthorization(mcpAuthorizationKey('plugin', plugin.packageInstallId, connection.serverId));
          }
        }
      }
      completeUserPlugins.current = next.plugins;
    }
    setUserCapabilities(inventory);
    setUserCapabilityError(undefined);
    setUserCapabilityLoading(false);
  }, [invalidateMcpAuthorization]);

  const adoptUserCapabilityMutation = useCallback((next: UserCapabilitySnapshot) => {
    // Device capabilities belong to the user. Retire earlier reads regardless
    // of the selected session, just as the session mutation owner does locally.
    userCapabilityAttempt.current += 1;
    userCapabilityReadAbort.current?.abort();
    acceptUserCapabilities(next);
    if (next.mcp.servers.some(server => (
      server.status === 'connecting' || server.status === 'discovering' || server.status === 'updating'
    ))) refreshVisibleUserCapabilities.current?.();
  }, [acceptUserCapabilities]);

  const loadUserCapabilities = useCallback(async (signal?: AbortSignal) => {
    if (signal?.aborted) return;
    const load = async () => {
      if (signal?.aborted) return;
      const attempt = ++userCapabilityAttempt.current;
      const controller = new AbortController();
      const readSignal = signal ? AbortSignal.any([controller.signal, signal]) : controller.signal;
      userCapabilityReadAbort.current = controller;
      setUserCapabilityLoading(true);
      setUserCapabilityError(undefined);
      try {
        const next = await adapter.inspectUserCapabilities(readSignal);
        if (!readSignal.aborted && attempt === userCapabilityAttempt.current) {
          acceptUserCapabilities(next);
        }
      } catch (error) {
        if (readSignal.aborted || attempt !== userCapabilityAttempt.current) return;
        const message = productMessage(
          error instanceof Error ? error.message : undefined,
          '暂时无法读取这台设备上的能力。',
        );
        setUserCapabilityError(message);
        setUserCapabilityLoading(false);
      } finally {
        if (userCapabilityReadAbort.current === controller) userCapabilityReadAbort.current = undefined;
        if (readSignal.aborted && attempt === userCapabilityAttempt.current) setUserCapabilityLoading(false);
      }
    };
    // Toolbar and periodic reads share one admission lane.
    const operation = userCapabilityLoads.current.then(load, load);
    userCapabilityLoads.current = operation;
    await operation;
  }, [adapter, acceptUserCapabilities]);

  const forgetSession = useCallback((sessionId: string) => {
    suggestionCache.forget(sessionId);
    workspaceControl.current?.forgetSession(sessionId);
    uiPreferences.removeItem(`pulsara-workspace-width:${sessionId}`);
    changeSessionList(current => current.filter(s => s.id !== sessionId));
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
    if (readSavedSessionId() === sessionId) saveSessionId('');
    if (requestedSession.current !== sessionId) return;
    historyReadAbort.current.abort();
    const previous = connectionRef.current;
    const previousHistory = historyRef.current;
    historyRef.current = undefined;
    if (previousHistory) void previousHistory.close();
    connectionAttempt.current += 1;
    capabilityAttempt.current += 1;
    connectionRef.current = undefined;
    activeSessionIdRef.current = '';
    requestedSession.current = '';
    setConnection(undefined);
    setActiveSessionId('');
    setOpeningSessionId('');
    setProjection(emptyProjection);
    setCapabilities(undefined);
    setCapabilityLoading(false);
    setCapabilityError(undefined);
    setRuntimeStatus('online');
    setRuntimeError(undefined);
    if (previous) void previous.close().catch(() => {});
  }, [changeSessionList, promptDraftStore, suggestionCache]);

  const readSessionList = useCallback(async (canPublish: () => boolean = () => true) => {
    const owner = {};
    globalReads.sessions = owner;
    const ownsRead = () => globalReads.active && globalReads.sessions === owner && canPublish();
    let sessions: SessionSummary[];
    try { sessions = await globalReads.adapter.listSessions(); }
    catch (error) { if (!ownsRead()) return; throw error; }
    if (!ownsRead()) return;
    setSessionList(current => sessions.map(session => {
      // A cold list cannot roll back the current connection's newer live facts.
      if (session.id !== connectionRef.current?.sessionId) return session;
      const projected = current.find(item => item.id === session.id);
      return projected ? { ...session, status: projected.status, updatedAt: projected.updatedAt,
        live: connectionRef.current?.sessionId === session.id ? true : session.live } : session;
    }));
    const selected = activeSessionIdRef.current;
    if (selected && deletingSession.current !== selected && !sessions.some(session => session.id === selected)) {
      forgetSession(selected);
    }
    if (globalReads.configuration.databaseReady) setUserServiceStatus('online');
    return sessions;
  }, [forgetSession, globalReads]);

  const { suggestion, observeTitle } = useSessionEphemeralResults({
    cache: suggestionCache, adapter, connection, projection,
    suggestionsEnabled: bootstrap?.local_settings.input_suggestions_enabled ?? true,
    binding: sessionList.find(session => session.id === activeSessionId)?.modelCallBinding,
    visible: activeView === 'workbench' && connection?.sessionId === activeSessionId,
    refreshSessions: readSessionList,
  });

  const confirmSessionDelete = async () => {
    if (!deleteTarget || deletingSession.current || bulkDeletionOwner.current?.running) return;
    const target = deleteTarget;
    deletingSession.current = target.id;
    setDeleteBusy(true);
    setDeleteError(undefined);
    invalidateSessionList();
    try {
      const outcome = await adapter.deleteSession(target.id);
      if (outcome.session_id !== target.id || !['DELETED', 'ABSENT'].includes(outcome.status)) {
        throw new Error('服务器尚未确认删除结果，请重试确认。');
      }
      forgetSession(target.id);
      setSessionRevision(value => value + 1);
      setDeleteTarget(undefined);
      notify('会话已删除', '记忆和工作目录已保留。', 'success');
      try { await readSessionList(); } catch { /* deletion is already confirmed */ }
    } catch (error) {
      setDeleteError(productMessage(error instanceof Error ? error.message : undefined, '暂时无法确认删除结果，请重试确认。'));
    } finally {
      deletingSession.current = undefined;
      setDeleteBusy(false);
    }
  };

  const requestSessionDelete = (session: SessionSummary) => {
    if (deletingSession.current || bulkDeletionOwner.current?.running || bulkDeleteTarget) return;
    setDeleteTarget(session); setDeleteError(undefined);
  };
  const requestBulkDelete = (sessions: readonly SessionSummary[]) => {
    if (deletingSession.current || bulkDeletionOwner.current?.running || deleteTarget) return;
    const targets = [...new Map(sessions.map(session => [session.id, { ...session }])).values()];
    if (targets.length) { setBulkResultsOpen(false); setBulkDeleteTarget(targets); }
  };
  const confirmBulkDelete = async () => {
    if (!bulkDeleteTarget?.length || deletingSession.current || bulkDeletionOwner.current?.running) return;
    const owner: NonNullable<typeof bulkDeletionOwner.current> = { active: true, running: true, items: bulkDeleteTarget.map(session => ({ session, status: 'pending' })) };
    bulkDeletionOwner.current = owner;
    invalidateSessionList();
    setBulkDeleteTarget(undefined);
    setBulkNoticeDismissed(false);
    const ownsBatch = () => owner.active && bulkDeletionOwner.current === owner;
    const publish = () => { if (ownsBatch()) setBulkDeletion({ items: [...owner.items], running: owner.running }); };
    const update = (index: number, status: ArchivedSessionDeletionItem['status'], detail?: string) => {
      owner.items[index] = { ...owner.items[index], status, detail }; publish();
    };
    publish();
    let next = 0;
    // This request concurrency is the physical admission policy in spec §7.2,
    // not a limit on logical targets, batch duration or session history.
    await Promise.all(Array.from({ length: Math.min(3, owner.items.length) }, async () => {
      while (ownsBatch() && next < owner.items.length) {
        const index = next++; const session = owner.items[index].session;
        update(index, 'running');
        try {
          const outcome = await adapter.deleteArchivedSession(session.id);
          if (!ownsBatch()) return;
          if (outcome.session_id !== session.id || !['DELETED', 'ABSENT', 'NOT_ARCHIVED'].includes(outcome.status)
              || (outcome.cleanup_warning !== null && (outcome.cleanup_warning?.code !== 'SESSION_DELETE_QUARANTINED' || typeof outcome.cleanup_warning?.message !== 'string'))) {
            throw new RuntimeApiError('SESSION_DELETE_UNCONFIRMED', '服务器尚未确认本条结果，请重试确认。', true);
          }
          update(index, outcome.status, outcome.cleanup_warning?.message);
          if (outcome.status !== 'NOT_ARCHIVED') forgetSession(session.id);
        } catch (error) {
          if (!ownsBatch()) return;
          const unknown = !(error instanceof RuntimeApiError) || ['LOCAL_TRANSPORT_UNAVAILABLE', 'SESSION_DELETE_UNCONFIRMED'].includes(error.code) || error.code.startsWith('HTTP_');
          update(index, unknown ? 'unknown' : 'failed', productMessage(error instanceof Error ? error.message : undefined, '尚未确认删除结果，请重试确认。'));
        }
      }
    }));
    if (!ownsBatch()) return;
    let refreshError: string | undefined;
    try { await readSessionList(ownsBatch); }
    catch { refreshError = '操作结果已保留，但会话列表暂时无法刷新，请稍后刷新查看。'; }
    if (!ownsBatch()) return;
    owner.running = false;
    setSessionRevision(value => value + 1);
    setBulkDeletion({ items: [...owner.items], running: false, refreshError });
  };

  const archiveSession = async (target: SessionSummary) => {
    if (archivingSession.current || bulkDeletionOwner.current?.running || bulkDeleteTarget) return;
    archivingSession.current = target.id;
    invalidateSessionList();
    try {
      const result = await adapter.archiveSession(target.id);
      if (result.status !== 'ARCHIVED' || result.session_id !== target.id) throw new Error('尚未确认归档结果，请刷新查看。');
      forgetSession(target.id);
      setSessionRevision(value => value + 1);
      notify('会话已归档', '可在设置中取消归档；绑定的定时任务已删除，需要重新创建。', 'success');
      try { await readSessionList(); }
      catch { notify('会话已归档', '列表暂时未能刷新，请稍后刷新页面。', 'warning'); }
    } catch (error) {
      notify('会话归档未完成', error instanceof Error ? error.message : '请刷新后重试。', 'warning');
    } finally { archivingSession.current = undefined; }
  };

  const openRuntimeSessionAttempt = useCallback(async (
    sessionId: string,
    reconnecting = false,
    takeover = false,
  ): Promise<RuntimeConnection | undefined> => {
    if (deletingSession.current === sessionId || archivingSession.current === sessionId) return undefined;
    invalidateSessionList();
    requestedSession.current = sessionId;
    historyReadAbort.current.abort();
    historyReadAbort.current = new AbortController();
    const attempt = ++connectionAttempt.current;
    setOpeningSessionId(current => current === sessionId ? current : '');
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
        if (attempt !== connectionAttempt.current) { await next.close(); return undefined; }
        historyRef.current = next;
        activeSessionIdRef.current = sessionId;
        setActiveSessionId(sessionId);
        setOpeningSessionId('');
        saveSessionId(sessionId);
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
      activeSessionIdRef.current = sessionId;
      setConnection(next);
      setActiveSessionId(sessionId);
      setOpeningSessionId('');
      saveSessionId(sessionId);
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
  }, [adapter, publishProjection, forgetSession, invalidateSessionList, changeSessionList, readSessionList]);

  const openRuntimeSession = useCallback((sessionId: string, reconnecting = false, takeover = false): Promise<RuntimeConnection | undefined> => {
    if (deletingSession.current === sessionId || archivingSession.current === sessionId) return Promise.resolve(undefined);
    const current = connectionInFlight.current;
    if (current && current.attempt === connectionAttempt.current && current.sessionId === sessionId
      && current.reconnecting === reconnecting && current.takeover === takeover) return current.promise;
    // Share the complete original attempt, including publication and stale
    // cleanup. Sharing only adapter.connect would let a second attempt close
    // the same connection as its stale result. Different intents retain the
    // original attempt counter and cancellation boundary.
    const promise = openRuntimeSessionAttempt(sessionId, reconnecting, takeover);
    const owner = {sessionId, reconnecting, takeover, attempt: connectionAttempt.current, promise};
    connectionInFlight.current = owner;
    void promise.finally(() => {
      if (connectionInFlight.current === owner) connectionInFlight.current = undefined;
    }).catch(() => {});
    return promise;
  }, [openRuntimeSessionAttempt]);

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
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let latestRead: object;
    const refreshSessions = () => {
      clearTimeout(timer);
      if (document.visibilityState !== 'visible' || databaseState !== 'ready') return;
      const read = {};
      latestRead = read;
      void readSessionList().catch(() => {}).finally(() => {
        if (!disposed && latestRead === read && document.visibilityState === 'visible') {
          timer = setTimeout(refreshSessions, 30_000);
        }
      });
    };
    // User-level metadata can change after a queued input becomes ROOT while
    // another page is open. Use the ordinary desktop observation cadence;
    // this reads session rows only and cannot create ephemeral provider work.
    if (databaseState === 'ready') timer = setTimeout(refreshSessions, 30_000);
    window.addEventListener('focus', refreshSessions);
    document.addEventListener('visibilitychange', refreshSessions);
    return () => {
      disposed = true;
      clearTimeout(timer);
      window.removeEventListener('focus', refreshSessions);
      document.removeEventListener('visibilitychange', refreshSessions);
    };
  }, [databaseState, readSessionList]);

  const refreshConfiguration = useCallback(async (options?: { background?: boolean }) => {
    const background = options?.background === true;
    const owner = background ? globalReads.configuration : { databaseReady: false };
    if (!background) {
      globalReads.configuration = owner;
      invalidateSessionList();
    }
    const ownsRead = () => globalReads.active && globalReads.configuration === owner;
    if (!background) setUserServiceStatus('reconnecting');
    try {
      const boot = await adapter.bootstrap();
      if (!ownsRead()) return;
      setBootstrap(boot);
      owner.databaseReady = boot.database_state === 'ready';
      if (boot.database_state !== 'ready') {
        setUserServiceStatus('online');
        if (boot.database_state === 'database_restart_required') {
          const previous = connectionRef.current;
          const previousHistory = historyRef.current;
          // Detach these exact old owners before awaiting their cleanup. A
          // later global read or session open can then own its own connection.
          historyRef.current = undefined;
          connectionAttempt.current += 1;
          connectionRef.current = undefined;
          activeSessionIdRef.current = '';
          setConnection(undefined);
          setOpeningSessionId('');
          setRuntimeStatus('online');
          setRuntimeError(undefined);
          setActiveSessionId('');
          changeSessionList([]);
          await Promise.all([previousHistory?.close(), previous?.close()]);
          return;
        }
        if (!connectionRef.current) {
          changeSessionList([]);
          setActiveSessionId('');
        }
        return;
      }
      if (!background) await readSessionList(ownsRead);
      if (!ownsRead()) return;
      setUserServiceStatus('online');
    } catch (error) {
      if (!ownsRead()) return;
      setUserServiceStatus(error instanceof RuntimeApiError && error.retryable ? 'offline' : 'failed');
      throw error;
    } finally {
      if (ownsRead() && !background) setStartupSettled(true);
    }
  }, [adapter, globalReads, invalidateSessionList, changeSessionList, readSessionList]);

  const reconnectUserService = useCallback(() => {
    void refreshConfiguration().catch(() => { /* The global read published its failure. */ });
  }, [refreshConfiguration]);

  useEffect(() => {
    if (!startupSettled || !(bootstrap?.model_catalog_loading || bootstrap?.model_catalog_recovering) || userServiceStatus !== 'online') return;
    // The local shell is usable while its one initial remote catalog attempt
    // runs. Observe through the ordinary configuration owner until it settles;
    // a successful service reconnect resumes observation even if loading stayed
    // true. Reconnecting itself owns configuration reads until it is online.
    let disposed = false;
    let timer: number;
    const observe = async () => {
      try {
        await refreshConfiguration({ background: true });
      } catch {
        return; // The ordinary configuration owner published the read failure.
      }
      if (!disposed) timer = window.setTimeout(() => { void observe(); }, bootstrap.model_catalog_loading ? 250 : 2000);
    };
    timer = window.setTimeout(() => { void observe(); }, bootstrap.model_catalog_loading ? 250 : 2000);
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [bootstrap?.model_catalog_loading, bootstrap?.model_catalog_recovering, startupSettled, refreshConfiguration, userServiceStatus]);

  useEffect(() => {
    let disposed = false;
    const owner = { databaseReady: false };
    globalReads.configuration = owner;
    invalidateSessionList();
    const ownsRead = () => !disposed && globalReads.active && globalReads.configuration === owner;
    void (async () => {
      try {
        const boot = await adapter.bootstrap();
        if (!ownsRead()) return;
        setBootstrap(boot);
        owner.databaseReady = boot.database_state === 'ready';
        if (boot.database_state !== 'ready') {
          setUserServiceStatus('online');
          setRuntimeStatus('online');
          return;
        }
        const sessions = await readSessionList(ownsRead);
        if (!ownsRead()) return;
        setUserServiceStatus('online');
        const savedSessionId = readSavedSessionId();
        const initialSession = sessions?.find((session) => session.id === savedSessionId) ?? sessions?.[0];
        // Restore the session in the background; startup never owns navigation.
        if (initialSession) {
          void openRuntimeSession(initialSession.id);
        }
        else {
          setRuntimeStatus('online');
        }
      } catch (error) {
        if (!ownsRead()) return;
        setUserServiceStatus(error instanceof RuntimeApiError && error.retryable ? 'offline' : 'failed');
        setRuntimeStatus(error instanceof RuntimeApiError && error.retryable ? 'offline' : 'failed');
        setRuntimeError(productMessage(error instanceof Error ? error.message : undefined, 'Pulsara 启动失败。'));
      } finally {
        // Global initialization owns the cover; selected-session restoration
        // continues under its own workbench connection state.
        if (ownsRead()) setStartupSettled(true);
      }
    })();
    return () => {
      disposed = true;
      historyReadAbort.current.abort();
      connectionAttempt.current += 1;
      const active = connectionRef.current;
      const history = historyRef.current;
      historyRef.current = undefined;
      if (history) void history.close();
      connectionRef.current = undefined;
      activeSessionIdRef.current = '';
      if (active) void active.close();
    };
  }, [adapter, openRuntimeSession, globalReads, invalidateSessionList, readSessionList]);

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
          setRuntimeFailure(error instanceof Error ? {
            details: error instanceof RuntimeApiError ? `${error.code}: ${error.message}` : `${error.name}: ${error.message}`,
            requiresRestart: error instanceof RuntimeApiError && error.code === 'SESSION_RECOVERY_CLOSE_FAILED',
          } : undefined);
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
    readConsumedInput,
  ]);

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
    let page = new AbortController();
    userCapabilityPageAbort.current = page;
    let disposed = false;
    let reading = false;
    let refreshRequested = false;
    let request: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = async () => {
      clearTimeout(timer);
      if (disposed || document.visibilityState !== 'visible') return;
      if (reading) { refreshRequested = true; return; }
      reading = true;
      refreshRequested = false;
      request = new AbortController();
      try {
        await loadUserCapabilities(request.signal);
      } finally {
        reading = false;
        if (!disposed && document.visibilityState === 'visible') {
          if (refreshRequested) {
            refreshRequested = false;
            void refresh();
          } else {
            const settling = userCapabilitiesRef.current?.mcp.servers.some(server => (
              server.status === 'connecting' || server.status === 'discovering' || server.status === 'updating'
            ));
            // Keep the existing fast settling cadence and the desktop's ordinary
            // 30-second observation cadence. This page observes all user services,
            // independently of whichever conversation happens to be selected.
            timer = setTimeout(() => void refresh(), settling ? 650 : 30_000);
          }
        }
      }
    };
    const observeVisibility = () => {
      if (document.visibilityState !== 'visible') {
        clearTimeout(timer);
        page.abort();
        request?.abort();
        userCapabilityReadAbort.current?.abort();
      } else {
        if (page.signal.aborted) {
          page = new AbortController();
          userCapabilityPageAbort.current = page;
        }
        void refresh();
      }
    };
    refreshVisibleUserCapabilities.current = observeVisibility;
    const frame = window.requestAnimationFrame(observeVisibility);
    window.addEventListener('focus', observeVisibility);
    document.addEventListener('visibilitychange', observeVisibility);
    return () => {
      disposed = true;
      page.abort();
      request?.abort();
      userCapabilityReadAbort.current?.abort();
      if (userCapabilityPageAbort.current === page) userCapabilityPageAbort.current = undefined;
      window.cancelAnimationFrame(frame);
      clearTimeout(timer);
      refreshVisibleUserCapabilities.current = undefined;
      window.removeEventListener('focus', observeVisibility);
      document.removeEventListener('visibilitychange', observeVisibility);
    };
  }, [activeView, loadUserCapabilities]);

  useEffect(() => {
    refreshVisibleUserCapabilities.current?.();
  }, [mcpAuthorizationStatuses]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    uiPreferences.setItem('pulsara-theme', theme);
  }, [theme]);

  useEffect(() => {
    uiPreferences.setItem('pulsara-show-builtin-tool-results', String(showBuiltinToolResults));
  }, [showBuiltinToolResults]);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return;
      const shortcut = (event.metaKey || event.ctrlKey) && !event.altKey && !event.getModifierState('AltGraph');
      if (shortcut && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setSearchOpen((value) => !value);
      }
      if (shortcut && event.key.toLowerCase() === 'n') {
        event.preventDefault();
        if (canCreateSession) {
          setNewSessionOpen(true);
        }
      }
      if (event.key === 'Escape') {
        setSearchOpen(false);
        setNewSessionOpen(false);
        if (!window.matchMedia?.('(min-width: 1151px)').matches) setSidebarOpen(false);
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
  // Context reads and observe keep running while the initial tail is prepared.
  // The reader latches initialReady, so later page/context refreshes stay inline.
  const initialHistoryPending = activeReader?.sessionId === activeSessionId && projection.history?.initialReady === false;
  const loadingSession = openingSession ?? (initialHistoryPending ? activeSession : undefined);
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

  useEffect(() => {
    if (activeView !== 'workbench' || !activeSessionId || databaseState !== 'ready' || runtimeStatus !== 'online') return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let reading = false;
    const refresh = async () => {
      if (reading || controller.signal.aborted) return;
      clearTimeout(timer);
      if (document.visibilityState === 'visible') {
        reading = true;
        try {
          const page = await scheduledApi.list('ACTIVE', undefined, controller.signal, activeSessionId);
          if (!controller.signal.aborted) {
            const task = page.tasks.find(item => item.status === 'ACTIVE' && item.session_id === activeSessionId);
            setActiveSchedule(task ? { sessionId: activeSessionId, taskId: task.id } : undefined);
          }
        } catch {
          if (!controller.signal.aborted) setActiveSchedule(undefined);
        } finally { reading = false; }
      }
      if (!controller.signal.aborted) timer = setTimeout(refresh, 30_000);
    };
    void refresh();
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      controller.abort(); clearTimeout(timer);
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [activeSessionId, activeView, databaseState, projection.isRunning, runtimeStatus, scheduledApi]);

  const navigate = (view: AppView, settingsSection?: SettingsSection, highlightHome = false) => {
    if (view === 'capabilities') setCapabilityTab('plugins');
    if (view === 'settings') {
      setSettingsInitialSection(settingsSection);
      setSettingsHighlightHome(highlightHome);
    }
    setActiveView(view);
    setFocusScheduledTaskId(undefined);
    if (!window.matchMedia?.('(min-width: 1151px)').matches) setSidebarOpen(false);
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

  const openSession = (id: string) => {
    if (deletingSession.current === id || archivingSession.current === id) return;
    setActiveView('workbench');
    if (!window.matchMedia?.('(min-width: 1151px)').matches) setSidebarOpen(false);
    if (connectionRef.current?.sessionId === id || (openingSessionId === id && !runtimeError)) return;
    setOpeningSessionId(id);
    void openRuntimeSession(id);
  };

  const createSession = async (selection: SessionWorkspaceSelection): Promise<boolean> => {
    if (!canCreateSession) return false;
    invalidateSessionList();
    try {
      const created = await adapter.createSession(selection);
      if (!globalReads.active) return false;
      changeSessionList((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setActiveView('workbench');
      const next = await openRuntimeSession(created.id);
      if (!next) return false;
      if (!window.matchMedia?.('(min-width: 1151px)').matches) setSidebarOpen(false);
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
    invalidateSessionList();
    const created = await adapter.createSession({kind:'quick'}, binding);
    changeSessionList(current => [created, ...current.filter(item=>item.id!==created.id)]);
    return created.id;
  };

  const forkConversation = async (entryId: string): Promise<void> => {
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
      ? Boolean((receipt.status === 'succeeded' && receipt.planDraftDecision === 'revise' && receipt.planContinuationTurnId)
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
        displayAsMessage: !projection.isRunning && !projection.interaction && projection.queuedCount === 0
          && !localSubmissions.some(item => item.sessionId === active.sessionId
            && ['sending', 'synchronizing', 'queued', 'unknown'].includes(item.status)),
      }]);
      suggestionCache.invalidate(active.sessionId);
      const receipt = await active.submitPrompt(commandId, content, permission);
      const titleHost = active.current().hostSessionId;
      if (promptWasAccepted(receipt) && titleHost) observeTitle(active.sessionId, titleHost);
      if (!ownsConnection(active)) return false;
      requirePromptReceipt(commandId, receipt);
      setLocalSubmissions((current) => current.map((item) => item.commandId === commandId
        ? submissionFromReceipt(item, receipt)
        : item));
      notifyPromptReceipt(
        receipt,
        requestPlan ? '本轮将先制定计划' : projection.isRunning ? '输入将在下一轮处理' : '输入已接受',
        projection.isRunning ? '会在当前轮完成后自动开始。' : '已送达本地服务。',
      );
      readConsumedInput(active, receipt);
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

  const stopRun = async () => {
    const active = connectionRef.current;
    const targetTurnId = projection.activeTurnId;
    const manualCommandId = projection.manualCompactionCommandId;
    if (!active || active.role !== 'controller' || (!targetTurnId && !manualCommandId)) return;
    if (!projection.hostSessionId || !projection.controlAdmissionDeadlineMs) {
      notify('暂时无法停止', '当前 Host 的控制身份尚未确认，请等待刷新后重试。', 'warning');
      return;
    }
    const reference = createUserControlCommandRef(
      projection,
      active.sessionId,
      targetTurnId ? 'STOP_ACTIVE_TURN' : 'STOP_CONTEXT_COMPACTION',
      targetTurnId ? 'ROOT_TURN' : 'MANUAL_COMPACTION',
      targetTurnId ?? manualCommandId!,
    );
    try {
      const receipt = await (targetTurnId ? active.stopActiveTurn(reference) : active.stopContextCompaction(reference));
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
            : `${productMessage(error instanceof Error ? error.message : undefined, '当前无法取得这次操作的结果。')} 未查到原操作结果；Pulsara 不会自动重发。`,
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
      suggestionCache.invalidate(active.sessionId);
      const receipt = await active.compactContext();
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
      const result = await adapter.installUserSkill(input);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(result.operation.success ? '技能已安装' : '技能没有安装', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('技能安装失败', productMessage(error instanceof Error ? error.message : undefined, '请检查本地目录后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, loadCapabilities, notify, notifyUserCapabilityResult]);

  const installDevicePlugin = useCallback(async (sourcePath: string, options?: PluginImportOptions): Promise<boolean> => {
    try {
      const result = await adapter.installUserPlugin(sourcePath, options);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(result.operation.success ? '插件已安装' : '插件没有安装', result);
      return result.operation.success;
    } catch (error) {
      notify('插件安装失败', productMessage(error instanceof Error ? error.message : undefined, '请检查本地目录后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, notify, notifyUserCapabilityResult]);

  const createDeviceMcp = useCallback(async (input: McpEditInput): Promise<boolean> => {
    try {
      const result = await adapter.createUserMcp(input);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(result.operation.success ? 'MCP 服务已添加' : 'MCP 服务没有添加', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 服务添加失败', productMessage(error instanceof Error ? error.message : undefined, '请检查连接信息后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, loadCapabilities, notify, notifyUserCapabilityResult]);

  const importDeviceMcp = useCallback(async (input: McpImportSelection): Promise<boolean> => {
    try {
      const result = await adapter.importUserMcp(input);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(result.operation.success ? 'MCP 已导入' : 'MCP 没有导入', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 导入失败', productMessage(error instanceof Error ? error.message : undefined, '请处理预览中的字段后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, loadCapabilities, notify, notifyUserCapabilityResult]);

  const toggleDeviceMcp = useCallback(async (
    server: UserMcpServerCapability,
    enabled: boolean,
  ): Promise<boolean> => {
    try {
      const result = await adapter.updateUserMcp({serverId: server.id, config: {...server.config, enabled}, secretChanges: []}, server.currentIdentity);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(enabled ? 'MCP 服务已开启' : 'MCP 服务已关闭', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, loadCapabilities, notify, notifyUserCapabilityResult]);

  const editDeviceMcp = useCallback(async (server: UserMcpServerCapability, input: McpEditInput): Promise<boolean> => {
    try {
      const result = await adapter.updateUserMcp(input, server.currentIdentity);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult('MCP 配置已更新', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 配置未更新', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重新打开编辑。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, loadCapabilities, notify, notifyUserCapabilityResult]);

  const removeDeviceMcp = useCallback(async (server: UserMcpServerCapability): Promise<boolean> => {
    try {
      const result = await adapter.removeUserMcp(server.id, server.currentIdentity);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult('MCP 服务已移除', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('MCP 服务未移除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, loadCapabilities, notify, notifyUserCapabilityResult]);

  const deviceMcpAuthorization = useCallback(async (server: UserMcpServerCapability, action: 'login' | 'status' | 'cancel' | 'logout') => {
    await runMcpAuthorization(mcpAuthorizationKey('device', server.id, server.currentIdentity), action,
      next => adapter.userMcpAuthorization(server.id, next));
  }, [adapter, runMcpAuthorization]);

  const removeDeviceSkill = useCallback(async (skill: UserSkillCapability): Promise<boolean> => {
    try {
      const result = await adapter.removeUserSkill(skill);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(result.operation.success ? '技能已删除' : '技能未删除', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('技能未删除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, loadCapabilities, notify, notifyUserCapabilityResult]);

  const pluginMcpAuthorization = useCallback(async (plugin: UserPluginCapability, connection: PluginMcpConnection, action: 'login' | 'status' | 'cancel' | 'logout') => {
    await runMcpAuthorization(mcpAuthorizationKey('plugin', plugin.packageInstallId, connection.serverId), action,
      next => adapter.pluginMcpAuthorization(plugin, connection, next));
  }, [adapter, runMcpAuthorization]);

  const toggleDeviceSkill = useCallback(async (
    skill: UserSkillCapability,
    enabled: boolean,
  ): Promise<boolean> => {
    try {
      const result = await adapter.setUserSkillEnabled(
        skill.path,
        enabled,
      );
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(enabled ? '技能已开启' : '技能已关闭', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('技能状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, loadCapabilities, notify, notifyUserCapabilityResult]);

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
      );
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(enabled ? '插件已开启' : '插件已关闭', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('插件状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, loadCapabilities, notify, notifyUserCapabilityResult]);

  const removeDevicePlugin = useCallback(async (plugin: UserPluginCapability): Promise<boolean> => {
    try {
      const result = await adapter.removeUserPlugin(plugin.id, plugin.packageInstallId);
      if (result.operation.success) {
        for (const connection of plugin.mcpConnections ?? []) {
          invalidateMcpAuthorization(mcpAuthorizationKey('plugin', plugin.packageInstallId, connection.serverId));
        }
      }
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(result.operation.success ? '插件已移除' : '插件没有移除', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('插件没有移除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, invalidateMcpAuthorization, loadCapabilities, notify, notifyUserCapabilityResult]);

  const editPluginConnection = useCallback(async (
    plugin: UserPluginCapability,
    connection: import('../lib/pulsara-types').PluginMcpConnection,
    input: import('../lib/pulsara-types').PluginMcpEditInput,
  ): Promise<boolean> => {
    try {
      const result = await adapter.updatePluginConnection(plugin, connection, input);
      if (result.operation.success) {
        invalidateMcpAuthorization(mcpAuthorizationKey('plugin', plugin.packageInstallId, connection.serverId));
      }
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult('插件连接', result);
      if (activeSessionIdRef.current) void loadCapabilities(activeSessionIdRef.current);
      return result.operation.success;
    } catch (error) {
      notify('插件连接未保存', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, invalidateMcpAuthorization, loadCapabilities, notify, notifyUserCapabilityResult]);

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

  return (
    <UserAvatarProvider adapter={adapter}>
    <McpAuthorizationContext.Provider value={mcpAuthorizationStatuses}>
    <DesktopWorkspaceProvider visible={activeView === 'workbench' && !databaseBlocked} onBrowserShortcut={command => flushSync(() => { if (command === 'search') setSearchOpen(value => !value); else if (canCreateSession) setNewSessionOpen(true); })} controlRef={workspaceControl} sessionId={activeSessionId} onShow={() => setInspectorOpen(true)}>
    <FilePreviewProvider sessionId={activeSessionId} imageOwnerKey={activeSessionId} readImageReference={readImageReference} ownerKey={connection ? `${connection.sessionId}:${connection.generation}` : 'disconnected'} api={connection?.filePreview} onNotify={notify}>
    <PresentationProvider connection={connection} visible={activeView === "workbench" && !databaseBlocked && !loadingSession} isOpen={inspectorOpen} onNotify={notify}>
    <ToolResultDisplayContext.Provider value={{ showBuiltinToolResults, onChange: setShowBuiltinToolResults }}>
    <main data-platform={desktopHost()?.platform} className={`pulsara-shell desktop-shell${sidebarOpen ? " sidebar-visible" : ""}${activeView === 'workbench' ? ' is-workbench' : ' is-surface'}${inspectorOpen && !databaseBlocked ? ' has-inspector' : ''}`}>
      <DesktopTitlebar workbench={activeView === "workbench"} title={activeView === "workbench" ? activeSession.title : "Pulsara"} onToggleSidebar={() => setSidebarOpen(value => !value)} onToggleWorkspace={() => setInspectorOpen(value => !value)} />
      <div className="desktop-navigation"><ActivityRail activeView={activeView} onNavigate={navigate} />

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
          onDeleteSession={requestSessionDelete}
          onArchiveSession={session => void archiveSession(session)}
          onRenameSession={setRenameTarget}
          onRefreshSessions={() => { void readSessionList().catch(() => {}); }}
          onNewSession={openNewSession}
          onCreateSession={createSession}
          onPickDirectory={signal => adapter.pickWorkspaceDirectory(signal)}
          onNotify={(title, detail) => notify(title, detail, 'warning')}
          canCreateSession={canCreateSession}
          onOpenSearch={() => setSearchOpen(true)}
        />
      )}

      </div>
      {activeView === 'overview' && (
        <OverviewView
          sessions={sessionList}
          runtimeStatus={userServiceStatus}
          databaseState={databaseState}
          localSettings={bootstrap?.local_settings}
          modelConfigurations={bootstrap?.model_configurations ?? []}
          modelCatalogLoading={bootstrap?.model_catalog_loading}
          onNavigate={navigate}
          onOpenSession={openSession}
          onNewSession={openNewSession}
          canCreateSession={canCreateSession}
        />
      )}
      {activeView === 'workbench' && !databaseBlocked && loadingSession && (
        <SessionOpeningView
          session={loadingSession}
          workspace={loadingSession.workspace ?? emptyWorkspace}
          error={openingSession ? runtimeError : projection.history?.initialError}
          onRetry={() => { if (openingSession) openSession(openingSession.id); else void loadHistory().catch(() => {}); }}
        />
      )}
      {activeView === 'workbench' && !databaseBlocked && !initialHistoryPending && (
        <div className="session-connected-views" hidden={Boolean(openingSession)}>
        <WorkbenchView
          onCompletePaths={(prefix, cursor, signal) => adapter.completeWorkspacePaths(activeSession.id, prefix, cursor, signal)}
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
          localSubmissions={localSubmissions.filter((item) => (
            item.sessionId === activeSession.id
            && !item.handledByQueueAction
            && item.outcomeCode !== 'USER_REDIRECTED_TO_STEER'
            && !queueActions.some(action => action.status === 'accepted' && action.source.commandId === item.commandId)
            && !projection.canonicalInputs.some(message => canonicalInputMatches(message, item) && canonicalPromptReady(message, projection))
          ))}
          runtimeStatus={runtimeStatus}
          runtimeError={runtimeError}
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
          isObserver={isObserver}
          workspaceAvailability={workspaceAvailability}
          workspaceRecoveryBusy={workspaceRecoveryBusy}
          workspaceRecoveryError={workspaceRecoveryError}
          onRestoreWorkspace={restoreWorkspace}
          onRecheckWorkspace={recheckWorkspace}
          skills={(capabilities?.skills.items ?? []).filter(
            (skill) => skill.enabled && skill.effective,
          )}
          onReconnect={reconnect}
          onTakeControl={() => activeSessionId && void openRuntimeSession(activeSessionId, true, true)}
          onNewSession={openNewSession}
          canCreateSession={canCreateSession}
          onToggleInspector={() => setInspectorOpen((value) => !value)}
          onOpenModelSettings={() => navigate('settings', 'models')}
          suggestion={suggestion}
          onModelCallBindingChange={updateModelCallBinding}
          onSend={sendPrompt}
          onStop={() => void stopRun()}
          onCompact={compact}
          onRenameSession={() => setRenameTarget(activeSession)}
          onLocateScheduledTask={taskId => { setFocusScheduledTaskId(taskId); setActiveView('scheduled'); if (!window.matchMedia?.('(min-width: 1151px)').matches) setSidebarOpen(false); }}
          onOpenScheduledTask={runtimeStatus === 'online' && activeSchedule?.sessionId === activeSession.id ? () => {
            setFocusScheduledTaskId(activeSchedule.taskId);
            setActiveView('scheduled'); if (!window.matchMedia?.('(min-width: 1151px)').matches) setSidebarOpen(false);
          } : undefined}
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
        </div>
      )}
      {activeView === 'workbench' && !databaseBlocked && (
        <InspectorPanel
          adapter={adapter}
          sessionContentAvailable={!loadingSession}
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
          onReadToolArtifact={readToolArtifact}
          onReadPromptImage={readPromptImage}
          onReadVisualization={readVisualization} onReadVisualizationThumbnail={readVisualizationThumbnail}
          canControl={canControl}
          capabilities={capabilities}
          capabilityLoading={capabilityLoading}
          capabilityError={capabilityError}
          capabilityBusy={capabilityBusy}
          backgroundOwnerKey={`${connection?.sessionId ?? ''}:${connection?.generation ?? 0}:${projection.hostSessionId ?? ''}`}
          backgroundHostSessionId={projection.hostSessionId}
          backgroundControlAdmissionDeadlineMs={projection.controlAdmissionDeadlineMs}
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
          onTerminateBackgroundProcess={async (reference) => {
            const active = connectionRef.current;
            if (!active || active.role !== 'controller') throw new RuntimeApiError('CONTROL_UNAVAILABLE', '当前窗口没有控制权限。', false);
            if (active.sessionId !== activeSessionId || active.current().hostSessionId !== projection.hostSessionId) throw new RuntimeApiError('BACKGROUND_OWNER_CHANGED', '原进程实例已失效。', false);
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
          onReconnectProjectMcp={async server => {
            const active = connectionRef.current;
            if (!active || active.sessionId !== activeSessionId || active.current().hostSessionId !== projection.hostSessionId) { notify('连接实例已改变', '请重新打开当前连接后操作。', 'warning'); return; }
            await reconnectProjectMcp(server);
          }}
          onPickDirectory={() => adapter.pickWorkspaceDirectory()}
          onOpenUserCapabilities={kind => { navigate('capabilities'); setCapabilityTab(kind ?? 'plugins'); }}
          onShowProjectScopeNotice={kind => {
            const label = kind === 'skills' ? '技能' : 'MCP';
            notify(`这里添加的${label}仅会应用到当前目录中`, {
              before: `如果需要添加用户级${label}，请在`, label: '能力页', after: '添加',
              href: `#capabilities/${kind}`,
              onClick: () => { navigate('capabilities'); setCapabilityTab(kind); },
            });
          }}
          onOpenHomeSettings={() => navigate('settings', 'general', true)}
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
          tab={capabilityTab}
          onTabChange={setCapabilityTab}
          onPickDirectory={() => adapter.pickWorkspaceDirectory()}
          hooks={adapter.hooks}
          onNotify={notify}
          snapshot={userCapabilities}
          loading={userCapabilityLoading}
          error={userCapabilityError}
          onRefresh={() => loadUserCapabilities(userCapabilityPageAbort.current?.signal)}
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
          onSessionsChanged={async () => { await readSessionList(); }}
          onDeleteSession={requestSessionDelete}
          onDeleteArchivedSessions={requestBulkDelete}
          sessionDeletionBusy={deleteBusy || Boolean(bulkDeletion?.running)}
          theme={theme}
          bootstrap={bootstrap}
          runtimeStatus={userServiceStatus}
          adapter={adapter}
          onThemeChange={setTheme}
          onConfigurationChanged={refreshConfiguration}
          onDatabaseReset={() => { globalReads.configuration = { databaseReady: false }; invalidateSessionList(); workspaceControl.current?.clear(); for (const session of sessionList) forgetSession(session.id); }}
          onNotify={notify}
        />
      )}

      {activeView === 'scheduled' && <ScheduledTasksView onNotify={notify} focusTaskId={focusScheduledTaskId} modelCatalogLoading={bootstrap?.model_catalog_loading} modelConfigurations={bootstrap?.model_configurations ?? []} onCreateSession={createScheduledSession} api={scheduledApi} sessions={sessionList} ready={databaseState === 'ready' && userServiceStatus === 'online'} onOpenSettings={() => navigate('settings')} onOpenSession={openSession} />}
      {activeView === 'memory' && <MemoryView onNotify={notify} api={adapter.memory} databaseState={databaseState} runtimeStatus={userServiceStatus} onReconnect={reconnectUserService} onOpenSettings={() => navigate('settings')} />}

      {searchOpen && <SessionSearchDialog adapter={adapter} available={databaseState === 'ready' && Boolean(bootstrap)}
        onClose={() => setSearchOpen(false)} onOpen={session => {
          changeSessionList(current => current.some(item => item.id === session.id)
            ? current.map(item => item.id === session.id ? session : item) : [...current, session]);
          setSessionRevision(value => value + 1);
          openSession(session.id);
        }} />}
      {newSessionOpen && <NewSessionDialog
        open={newSessionOpen}
        canCreateSession={canCreateSession}
        onClose={() => setNewSessionOpen(false)}
        onCreate={createSession}
        onPickDirectory={signal => adapter.pickWorkspaceDirectory(signal)}
      />}
      <ToastStack
        toasts={[...toasts, ...(bulkDeletion && !bulkResultsOpen && !bulkDeleteTarget && !deleteTarget && !bulkNoticeDismissed ? [{
          id: -1,
          title: `${bulkDeletion.running ? '正在删除归档会话' : '归档会话删除结果'} · 已处理 ${bulkDeletion.items.filter(item => !['pending', 'running'].includes(item.status)).length} / ${bulkDeletion.items.length}`,
          detail: '点击查看详情',
          onClick: () => {
            setBulkResultsOpen(true);
            if (!bulkDeletion.running) setBulkNoticeDismissed(true);
          },
        }] : [])]}
        onDismiss={(id) => {
          if (id === -1) setBulkNoticeDismissed(true);
          else setToasts(current => current.filter(toast => toast.id !== id));
        }}
      />
      {renameTarget && <SessionRenameDialog key={renameTarget.id} session={renameTarget}
        onClose={() => setRenameTarget(undefined)} onSave={async title => {
          invalidateSessionList();
          const result = await adapter.renameSession(renameTarget.id, title);
          if (result.session_id !== renameTarget.id || result.title !== title) throw new Error('未能确认保存结果，请刷新确认或重试。');
          changeSessionList(current => current.map(item => item.id === result.session_id ? { ...item, title: result.title } : item));
          setSessionRevision(value => value + 1);
        }} />}
      {deleteTarget && <SessionDeletionDialog session={deleteTarget} busy={deleteBusy} error={deleteError}
        onConfirm={() => void confirmSessionDelete()} onClose={() => { if (!deleteBusy) setDeleteTarget(undefined); }} />}
      {bulkDeleteTarget && <SessionDeletionDialog sessions={bulkDeleteTarget} busy={false}
        onConfirm={() => void confirmBulkDelete()} onClose={() => setBulkDeleteTarget(undefined)} />}
      {bulkResultsOpen && bulkDeletion && <SessionDeletionDialog sessions={bulkDeletion.items.map(item => item.session)} results={bulkDeletion.items}
        busy={bulkDeletion.running} error={bulkDeletion.refreshError} onConfirm={() => {}} onClose={() => {
          setBulkResultsOpen(false);
          if (!bulkDeletion.running) setBulkNoticeDismissed(true);
        }}
        onRetry={() => requestBulkDelete(bulkDeletion.items.filter(item => ['failed', 'unknown'].includes(item.status)).map(item => item.session))} />}
      <DesktopWindowControls />
    </main>
    </ToolResultDisplayContext.Provider>
    </PresentationProvider>
    </FilePreviewProvider>
    </DesktopWorkspaceProvider>
    </McpAuthorizationContext.Provider>
    </UserAvatarProvider>
  );
}
