import { McpAuthorizationContext, mcpAuthorizationKey, useMcpAuthorization } from '../components/mcp-authorization';
import { MessageSquarePlus, Sparkles } from 'lucide-react';
import { flushSync } from 'react-dom';
import { uiPreferences } from '../lib/ui-preferences';
import { isCanonicalIdentity } from '../lib/identity';

import { DesktopTitlebar } from '../components/desktop-titlebar';
import { desktopHost } from '../lib/desktop-host';
import { UserAvatarProvider } from '../components/user-avatar';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityRail } from '../components/activity-rail';
import { CapabilityView, type CapabilityTab } from '../components/capability-view';
import { DatabaseSetupGuide } from '../components/database-setup-guide';
import { LocalScheduledTasksApi } from '../lib/scheduled-tasks-api';
import { ScheduledTasksView } from '../components/scheduled-tasks-view';
import { MemoryView } from '../components/memory-view';
import { NewSessionDialog, ToastStack } from '../components/overlays';
import { SessionSidebar } from '../components/session-sidebar';
import { SessionOpeningView } from '../components/session-opening-view';
import { SessionSearchDialog } from '../components/session-search-dialog';
import { SessionRenameDialog } from '../components/session-rename-dialog';
import { SessionDeletionDialog } from '../components/session-deletion-dialog';
import { SettingsView, type SettingsSection } from '../components/settings-view';
import { ToolResultDisplayContext, readSavedToolResultDisplay } from '../lib/tool-result-display';
import { PromptDraftStore } from '../lib/prompt-draft';
import { SessionSuggestionCache, SessionToolDecisionStore } from '../lib/session-ephemeral-results';
import {
  LocalHttpRuntimeAdapter,
  RuntimeApiError,
  type RuntimeAdapter,
  type RuntimeBootstrap,
  type RuntimeConnection,
  type SessionTabCloseConfirmation,
  type SessionTabCloseOutcome,
  type ModelCallBindingPayload,
} from '../lib/runtime-adapter';

import type {
  PluginImportOptions,
  ArchivedSessionDeletionItem,
  AppView,
  CapabilityOperation,
  McpEditInput,
  McpImportSelection,
  RuntimeStatus,
  SessionSummary,
  SessionWorkspaceSelection,
  SkillImportInput,
  ToastMessage,
  UserCapabilitySnapshot,
  UserMcpServerCapability,
  UserPluginCapability,
  PluginMcpConnection,
  UserSkillCapability,
} from '../lib/pulsara-types';

import { WelcomeTypewriter } from '../components/welcome-typewriter';
import { SessionCanvas, useSessionCanvas, useSessionCanvasWindow, type SessionCanvasPlacement } from '../components/session-canvas';
import { SessionWorkbench, emptySession, emptyWorkspace, productMessage, type SessionWorkbenchObservation } from './session-workbench';

const defaultAdapter = new LocalHttpRuntimeAdapter();

function pendingTabUnloadIds(sessions: SessionSummary[]): string {
  return sessions.filter(session => session.tabUnloadPending).map(session => session.id).sort().join(',');
}

type PendingOpen = {
  placement: SessionCanvasPlacement;
  result: Promise<boolean>;
  finish: (opened: boolean) => void;
};

type CloseRequest = {
  id: string;
  connection: RuntimeConnection;
  confirmation?: Extract<SessionTabCloseOutcome, { status: 'confirmation_required' }>;
  completion?: Promise<boolean>;
  error?: string;
};

function capabilityAdoptionMessages(adoption: UserCapabilitySnapshot['adoption']): string[] {
  const messages: string[] = [];
  if (adoption?.attentionSessions) messages.push(`${adoption.attentionSessions} 个已打开会话需要稍后重试。`);
  if (adoption?.pendingSessions) messages.push(`${adoption.pendingSessions} 个会话将在下次模型请求前的安全时机载入。`);
  return messages;
}

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

interface PulsaraAppProps {
  adapter?: RuntimeAdapter;
  onStartupSettled?: () => void;
}
export default function PulsaraApp({ adapter = defaultAdapter, onStartupSettled }: PulsaraAppProps) {
  const canvas = useSessionCanvas();
  const { place: placeSession, close: removePlacement, clear: clearCanvas } = canvas;
  const [canvasTabHost, setCanvasTabHost] = useState<HTMLElement | null>(null);
  const [mountedSessionIds, setMountedSessionIds] = useState<string[]>([]);
  const mountedSessionIdsRef = useRef(mountedSessionIds);
  const pendingOpens = useRef(new Map<string, PendingOpen>());
  const sessionConnections = useRef(new Map<string, RuntimeConnection>());
  const [sessionObservations, setSessionObservations] = useState<Record<string, SessionWorkbenchObservation>>({});
  const observationsRef = useRef(sessionObservations);
  const [openingSessionId, setOpeningSessionId] = useState('');
  const [closeRequest, setCloseRequest] = useState<CloseRequest>();
  const closeRequestRef = useRef<CloseRequest>(undefined);
  const publishCloseRequest = useCallback((request?: CloseRequest) => {
    closeRequestRef.current = request;
    setCloseRequest(request);
  }, []);
  const pendingTabClose = useCallback((connection: RuntimeConnection) => {
    const request = closeRequestRef.current;
    return request?.connection === connection ? request.completion : undefined;
  }, []);
  const dismissTabClose = useCallback(() => {
    if (!closeRequestRef.current?.completion) publishCloseRequest();
  }, [publishCloseRequest]);
  const [startupSettled, setStartupSettled] = useState(false);
  useEffect(() => {
    if (startupSettled) onStartupSettled?.();
  }, [startupSettled, onStartupSettled]);
  const [promptDraftStore] = useState(() => new PromptDraftStore());
  const [suggestionCache] = useState(() => new SessionSuggestionCache());
  const [toolDecisionStore] = useState(() => new SessionToolDecisionStore(id => mountedSessionIdsRef.current.includes(id)));
  const scheduledApi = useMemo(() => new LocalScheduledTasksApi(), []);
  const [activeView, setActiveView] = useState<AppView>('workbench');
  const activeViewRef = useRef(activeView);
  activeViewRef.current = activeView;
  useEffect(() => {
    if (activeView !== 'workbench') dismissTabClose();
  }, [activeView, dismissTabClose]);
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
  const [focusScheduledTaskId, setFocusScheduledTaskId] = useState<string>();
  const [userCapabilities, setUserCapabilities] = useState<UserCapabilitySnapshot>();
  const [userCapabilityMutation, setUserCapabilityMutation] = useState<UserCapabilitySnapshot>();
  const [userCapabilityLoading, setUserCapabilityLoading] = useState(false);
  const [userCapabilityError, setUserCapabilityError] = useState<string>();
  const userCapabilityAttempt = useRef(0);
  const userCapabilitiesRef = useRef<UserCapabilitySnapshot | undefined>(undefined);
  const completeUserPlugins = useRef<UserCapabilitySnapshot['plugins'] | undefined>(undefined);
  const userCapabilityLoads = useRef<Promise<void>>(Promise.resolve());
  const refreshVisibleUserCapabilities = useRef<(() => void) | undefined>(undefined);
  const userCapabilityReadAbort = useRef<AbortController | undefined>(undefined);
  const userCapabilityPageAbort = useRef<AbortController | undefined>(undefined);
  const [userServiceStatus, setUserServiceStatus] = useState<RuntimeStatus>('starting');
  const [userServiceError, setUserServiceError] = useState<string>();
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
  useEffect(() => {
    promptDraftStore.setImporter((id, files, signal) => {
      const connection = sessionConnections.current.get(id);
      if (!connection) throw new Error('文件所属会话未连接，请重新连接后重试。');
      return connection.importFiles(files, signal);
    });
    return () => promptDraftStore.destroy();
  }, [promptDraftStore]);
  const [theme, setTheme] = useState<'light' | 'dark'>(readSavedTheme);
  const [showBuiltinToolResults, setShowBuiltinToolResults] = useState(readSavedToolResultDisplay);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const databaseState = bootstrap?.database_state;
  const databaseBlocked = databaseState !== undefined && databaseState !== 'ready';
  const canCreateSession = userServiceStatus === 'online' && databaseState === 'ready';
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
  const acceptUserCapabilities = useCallback((next: UserCapabilitySnapshot) => {
    const inventory = { ...next };
    delete inventory.adoption;
    userCapabilitiesRef.current = inventory;
    // An unavailable inspection cannot prove that a saved owner disappeared.
    // Retain the last complete values so recovery can still detect replacements.
    if (next.plugins.status === 'ready') {
      for (const plugin of completeUserPlugins.current?.items ?? []) {
        if (plugin.packageInstallId.visibility !== 'VISIBLE') continue;
        const packageId = plugin.packageInstallId.value;
        const current = next.plugins.items.find(item => item.id === plugin.id && item.packageInstallId.visibility === 'VISIBLE' && item.packageInstallId.value === packageId);
        for (const connection of plugin.mcpConnections ?? []) {
          const replacement = current?.mcpConnections?.find(item => item.serverId === connection.serverId);
          // Compare complete existing values, not a new identity/fingerprint. An
          // external overlay change or package removal also retires old UI auth.
          if (!replacement || JSON.stringify([connection.config, connection.overlay]) !== JSON.stringify([replacement.config, replacement.overlay])) {
            invalidateMcpAuthorization(mcpAuthorizationKey('plugin', packageId, connection.serverId));
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
    setUserCapabilityMutation(next);
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

  const onSessionConnection = useCallback((id: string, previous?: RuntimeConnection, current?: RuntimeConnection) => {
    if (previous && sessionConnections.current.get(id) === previous) sessionConnections.current.delete(id);
    if (current) sessionConnections.current.set(id, current);
  }, []);

  const publishSessionStatus = useCallback((id: string, status: SessionSummary['status']) => {
    setSessionList(current => {
      const previous = current.find(session => session.id === id);
      if (!previous || (previous.status === status && previous.updatedAt === '刚刚')) return current;
      return current.map(session => session === previous ? { ...session, status, updatedAt: '刚刚' } : session);
    });
  }, []);

  const removeMountedSession = useCallback((id: string) => {
    if (closeRequestRef.current?.id === id) publishCloseRequest();
    pendingOpens.current.get(id)?.finish(false);
    pendingOpens.current.delete(id);
    setOpeningSessionId(current => current === id ? '' : current);
    if (!mountedSessionIdsRef.current.includes(id)) return;
    const next = mountedSessionIdsRef.current.filter(item => item !== id);
    mountedSessionIdsRef.current = next;
    setMountedSessionIds(next);
    toolDecisionStore.releaseSessionView(id);
    const observations = { ...observationsRef.current };
    delete observations[id];
    observationsRef.current = observations;
    setSessionObservations(observations);
    suggestionCache.forget(id);
    if (readSavedSessionId() === id) saveSessionId('');
    removePlacement(id);
  }, [publishCloseRequest, removePlacement, suggestionCache, toolDecisionStore]);

  const forgetSession = useCallback((id: string) => {
    removeMountedSession(id);
    promptDraftStore.removeSession(id);
    toolDecisionStore.forget(id);
    changeSessionList(current => current.filter(session => session.id !== id));
  }, [changeSessionList, promptDraftStore, removeMountedSession, toolDecisionStore]);

  const removeAllSessions = useCallback(() => {
    for (const id of mountedSessionIdsRef.current) removeMountedSession(id);
    clearCanvas();
  }, [clearCanvas, removeMountedSession]);

  const onSessionObservation = useCallback((id: string, observation: SessionWorkbenchObservation) => {
    if (!mountedSessionIdsRef.current.includes(id)) return;
    const previous = observationsRef.current[id];
    if (previous?.status === observation.status && previous.role === observation.role
      && previous.error === observation.error && previous.failure === observation.failure
      && previous.ready === observation.ready && previous.historyOnly === observation.historyOnly
      && previous.approvalPending === observation.approvalPending
      && previous.running === observation.running && previous.retry === observation.retry) return;
    const observations = { ...observationsRef.current, [id]: observation };
    observationsRef.current = observations;
    setSessionObservations(observations);
    const pending = pendingOpens.current.get(id);
    if (!pending) return;
    if (observation.ready) {
      pendingOpens.current.delete(id);
      setOpeningSessionId(current => current === id ? '' : current);
      placeSession(id, pending.placement);
      pending.finish(true);
    } else if (observation.error && observation.error !== previous?.error) {
      pending.finish(false);
      notify('会话未能打开', observation.error, 'warning');
    }
  }, [notify, placeSession]);

  const requestOpenSession = useCallback((
    id: string, placement: SessionCanvasPlacement = { type: 'open' },
  ): Promise<boolean> => {
    if (deletingSession.current === id || archivingSession.current === id) return Promise.resolve(false);
    const close = closeRequestRef.current;
    if (close?.id === id && close.completion) {
      return close.completion.then(() => requestOpenSession(id, placement));
    }
    if (placement.type === 'open') {
      // A newer sidebar selection supersedes an unfinished selection without a tab.
      // The current placement stays in view until a replacement is ready.
      for (const [other, pending] of pendingOpens.current) {
        if (other !== id && pending.placement.type === 'open') removeMountedSession(other);
      }
    }
    const pending = pendingOpens.current.get(id);
    if (mountedSessionIdsRef.current.includes(id) && !pending) {
      placeSession(id, placement);
      return Promise.resolve(true);
    }
    const observation = observationsRef.current[id];
    if (pending && !observation?.error) {
      pending.placement = placement;
      return pending.result;
    }
    let finish!: (opened: boolean) => void;
    const result = new Promise<boolean>(resolve => { finish = resolve; });
    pendingOpens.current.set(id, { placement, result, finish });
    setOpeningSessionId(id);
    if (!mountedSessionIdsRef.current.includes(id)) {
      const next = [...mountedSessionIdsRef.current, id];
      mountedSessionIdsRef.current = next;
      setMountedSessionIds(next);
    } else observation?.retry();
    return result;
  }, [placeSession, removeMountedSession]);

  useEffect(() => () => {
    for (const pending of pendingOpens.current.values()) pending.finish(false);
    pendingOpens.current.clear();
    closeRequestRef.current = undefined;
  }, [adapter]);

  useEffect(() => {
    if (canvas.layout.focusedId) saveSessionId(canvas.layout.focusedId);
  }, [canvas.layout.focusedId]);

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
      if (!sessionConnections.current.has(session.id)) return session;
      const projected = current.find(item => item.id === session.id);
      return projected ? { ...session, status: projected.status, updatedAt: projected.updatedAt,
        live: true } : session;
    }));
    for (const id of mountedSessionIdsRef.current) {
      if (!pendingOpens.current.has(id) && deletingSession.current !== id && !sessions.some(session => session.id === id)) {
        forgetSession(id);
      }
    }
    if (globalReads.configuration.databaseReady) setUserServiceStatus('online');
    return sessions;
  }, [forgetSession, globalReads]);

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


  const pendingTabUnloads = useMemo(() => pendingTabUnloadIds(sessionList), [sessionList]);
  useEffect(() => {
    let observation = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const visible = () => databaseState === 'ready' && document.visibilityState === 'visible';
    const refreshLater = () => { timer = setTimeout(refreshSessions, 30_000); };
    function observe(signal: AbortSignal) {
      if (signal.aborted || !visible()) return;
      if (!pendingTabUnloads) { refreshLater(); return; }
      // One wait covers every pending close, including sessions on another page.
      void adapter.waitForTabUnload(pendingTabUnloads.split(','), signal).then(
        () => { if (!signal.aborted) refreshSessions(); },
        () => { if (!signal.aborted) refreshLater(); },
      );
    }
    function refreshSessions() {
      clearTimeout(timer);
      observation.abort();
      observation = new AbortController();
      if (!visible()) return;
      const { signal } = observation;
      void readSessionList(() => !signal.aborted).then(sessions => {
        if (signal.aborted) return;
        if (!sessions) refreshLater();
        else if (pendingTabUnloadIds(sessions) === pendingTabUnloads) observe(signal);
        // A different set starts its own observation when React publishes the list.
      }, () => { if (!signal.aborted) refreshLater(); });
    }
    observe(observation.signal);
    window.addEventListener('focus', refreshSessions);
    document.addEventListener('visibilitychange', refreshSessions);
    return () => {
      observation.abort();
      clearTimeout(timer);
      window.removeEventListener('focus', refreshSessions);
      document.removeEventListener('visibilitychange', refreshSessions);
    };
  }, [adapter, databaseState, pendingTabUnloads, readSessionList]);

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
        setUserServiceError(undefined);
        removeAllSessions();
        changeSessionList([]);
        return;
      }
      if (!background) await readSessionList(ownsRead);
      if (!ownsRead()) return;
      setUserServiceStatus('online');
      setUserServiceError(undefined);
    } catch (error) {
      if (!ownsRead()) return;
      setUserServiceStatus(error instanceof RuntimeApiError && error.retryable ? 'offline' : 'failed');
      setUserServiceError(productMessage(error instanceof Error ? error.message : undefined, '无法连接本地服务。'));
      throw error;
    } finally {
      if (ownsRead() && !background) setStartupSettled(true);
    }
  }, [adapter, globalReads, invalidateSessionList, changeSessionList, readSessionList, removeAllSessions]);

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
          return;
        }
        const sessions = await readSessionList(ownsRead);
        if (!ownsRead()) return;
        setUserServiceStatus('online');
        const savedSessionId = readSavedSessionId();
        const initialSession = sessions?.find((session) => session.id === savedSessionId) ?? sessions?.[0];
        // Present the selected session's loading state before releasing the
        // startup cover. Restoration never takes over later user navigation.
        if (initialSession && !mountedSessionIdsRef.current.length) void requestOpenSession(initialSession.id);
      } catch (error) {
        if (!ownsRead()) return;
        setUserServiceStatus(error instanceof RuntimeApiError && error.retryable ? 'offline' : 'failed');
        setUserServiceError(productMessage(error instanceof Error ? error.message : undefined, 'Pulsara 启动失败。'));
      } finally {
        // Global initialization owns the cover; selected-session restoration
        // continues under its own workbench connection state.
        if (ownsRead()) setStartupSettled(true);
      }
    })();
    return () => { disposed = true; };
  }, [adapter, requestOpenSession, globalReads, invalidateSessionList, readSessionList]);

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

  const openSession = useCallback((id: string, placement?: SessionCanvasPlacement): Promise<boolean> => {
    const view = activeViewRef.current;
    const opened = requestOpenSession(id, placement);
    void opened.then(ok => {
      if (!ok || activeViewRef.current !== view) return;
      setActiveView('workbench');
      if (!window.matchMedia?.('(min-width: 1151px)').matches) setSidebarOpen(false);
    });
    return opened;
  }, [requestOpenSession]);

  const createSession = async (selection: SessionWorkspaceSelection): Promise<boolean> => {
    if (!canCreateSession) return false;
    invalidateSessionList();
    try {
      const created = await adapter.createSession(selection);
      if (!globalReads.active) return false;
      changeSessionList((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      if (!(await openSession(created.id))) return false;
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

  const installDeviceSkill = useCallback(async (input: SkillImportInput): Promise<boolean> => {
    try {
      const result = await adapter.installUserSkill(input);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(result.operation.success ? '技能已安装' : '技能没有安装', result);
      return result.operation.success;
    } catch (error) {
      notify('技能安装失败', productMessage(error instanceof Error ? error.message : undefined, '请检查本地目录后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, notify, notifyUserCapabilityResult]);

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
      return result.operation.success;
    } catch (error) {
      notify('MCP 服务添加失败', productMessage(error instanceof Error ? error.message : undefined, '请检查连接信息后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, notify, notifyUserCapabilityResult]);

  const importDeviceMcp = useCallback(async (input: McpImportSelection): Promise<boolean> => {
    try {
      const result = await adapter.importUserMcp(input);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(result.operation.success ? 'MCP 已导入' : 'MCP 没有导入', result);
      return result.operation.success;
    } catch (error) {
      notify('MCP 导入失败', productMessage(error instanceof Error ? error.message : undefined, '请处理预览中的字段后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, notify, notifyUserCapabilityResult]);

  const toggleDeviceMcp = useCallback(async (
    server: UserMcpServerCapability,
    enabled: boolean,
  ): Promise<boolean> => {
    try {
      const result = await adapter.updateUserMcp({serverId: server.id, config: {...server.config, enabled}, secretChanges: []}, server.currentIdentity);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(enabled ? 'MCP 服务已开启' : 'MCP 服务已关闭', result);
      return result.operation.success;
    } catch (error) {
      notify('MCP 状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, notify, notifyUserCapabilityResult]);

  const editDeviceMcp = useCallback(async (server: UserMcpServerCapability, input: McpEditInput): Promise<boolean> => {
    try {
      const result = await adapter.updateUserMcp(input, server.currentIdentity);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult('MCP 配置已更新', result);
      return result.operation.success;
    } catch (error) {
      notify('MCP 配置未更新', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重新打开编辑。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, notify, notifyUserCapabilityResult]);

  const removeDeviceMcp = useCallback(async (server: UserMcpServerCapability): Promise<boolean> => {
    try {
      const result = await adapter.removeUserMcp(server.id, server.currentIdentity);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult('MCP 服务已移除', result);
      return result.operation.success;
    } catch (error) {
      notify('MCP 服务未移除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, notify, notifyUserCapabilityResult]);

  const deviceMcpAuthorization = useCallback(async (server: UserMcpServerCapability, action: 'login' | 'status' | 'cancel' | 'logout') => {
    await runMcpAuthorization(mcpAuthorizationKey('device', server.id, server.currentIdentity), action,
      next => adapter.userMcpAuthorization(server.id, next));
  }, [adapter, runMcpAuthorization]);

  const removeDeviceSkill = useCallback(async (skill: UserSkillCapability): Promise<boolean> => {
    try {
      const result = await adapter.removeUserSkill(skill);
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(result.operation.success ? '技能已删除' : '技能未删除', result);
      return result.operation.success;
    } catch (error) {
      notify('技能未删除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, notify, notifyUserCapabilityResult]);

  const pluginMcpAuthorization = useCallback(async (plugin: UserPluginCapability, connection: PluginMcpConnection, action: 'login' | 'status' | 'cancel' | 'logout') => {
    if (plugin.packageInstallId.visibility !== 'VISIBLE') {
      notify('授权状态未更新', '插件安装身份不可用，请刷新后重试。', 'warning');
      return;
    }
    await runMcpAuthorization(mcpAuthorizationKey('plugin', plugin.packageInstallId.value, connection.serverId), action,
      next => adapter.pluginMcpAuthorization(plugin, connection, next));
  }, [adapter, notify, runMcpAuthorization]);

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
      return result.operation.success;
    } catch (error) {
      notify('技能状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, notify, notifyUserCapabilityResult]);

  const toggleDevicePlugin = useCallback(async (
    plugin: UserPluginCapability,
    enabled: boolean,
  ): Promise<boolean> => {
    try {
      if (plugin.packageInstallId.visibility !== 'VISIBLE') throw new Error('插件安装身份不可用，请刷新后重试。');
      const result = await adapter.setUserPluginEnabled(
        plugin.id,
        plugin.packageInstallId.value,
        enabled,
        plugin.connectionReview ?? [],
      );
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(enabled ? '插件已开启' : '插件已关闭', result);
      return result.operation.success;
    } catch (error) {
      notify('插件状态没有改变', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, notify, notifyUserCapabilityResult]);

  const removeDevicePlugin = useCallback(async (plugin: UserPluginCapability): Promise<boolean> => {
    try {
      if (plugin.packageInstallId.visibility !== 'VISIBLE') throw new Error('插件安装身份不可用，请刷新后重试。');
      const result = await adapter.removeUserPlugin(plugin.id, plugin.packageInstallId.value);
      if (result.operation.success) {
        for (const connection of plugin.mcpConnections ?? []) {
          invalidateMcpAuthorization(mcpAuthorizationKey('plugin', plugin.packageInstallId.value, connection.serverId));
        }
      }
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult(result.operation.success ? '插件已移除' : '插件没有移除', result);
      return result.operation.success;
    } catch (error) {
      notify('插件没有移除', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, invalidateMcpAuthorization, notify, notifyUserCapabilityResult]);

  const editPluginConnection = useCallback(async (
    plugin: UserPluginCapability,
    connection: import('../lib/pulsara-types').PluginMcpConnection,
    input: import('../lib/pulsara-types').PluginMcpEditInput,
  ): Promise<boolean> => {
    try {
      if (plugin.packageInstallId.visibility !== 'VISIBLE') throw new Error('插件安装身份不可用，请刷新后重试。');
      const result = await adapter.updatePluginConnection(plugin, connection, input);
      if (result.operation.success) {
        invalidateMcpAuthorization(mcpAuthorizationKey('plugin', plugin.packageInstallId.value, connection.serverId));
      }
      adoptUserCapabilityMutation(result.capabilities);
      notifyUserCapabilityResult('插件连接', result);
      return result.operation.success;
    } catch (error) {
      notify('插件连接未保存', productMessage(error instanceof Error ? error.message : undefined, '请刷新后重试。'), 'warning');
      return false;
    }
  }, [adapter, adoptUserCapabilityMutation, invalidateMcpAuthorization, notify, notifyUserCapabilityResult]);

  const openCapabilityRoot = useCallback(async (root: 'agents' | 'pulsara'): Promise<void> => {
    try {
      await adapter.openCapabilityRoot(root);
    } catch (error) {
      notify('无法打开目录', productMessage(error instanceof Error ? error.message : undefined, '请稍后重试。'), 'warning');
    }
  }, [adapter, notify]);


  const showWorkbench = activeView === 'workbench' && !databaseBlocked;
  const activeSessionId = canvas.layout.focusedId ?? '';
  const openingSession = sessionList.find(session => session.id === openingSessionId);
  const openingObservation = sessionObservations[openingSessionId];
  const releaseSession = (id: string) => {
    // A reopen queued on the close acknowledgement must mount a new reader,
    // even when React would batch both placements into the same render.
    flushSync(() => removeMountedSession(id));
    void readSessionList().catch(() => {});
  };
  const submitTabClose = (request: CloseRequest, confirmation: SessionTabCloseConfirmation) => {
    const pending: CloseRequest = { ...request, error: undefined };
    const ownsRequest = () => closeRequestRef.current === pending;
    pending.completion = Promise.resolve().then(async () => {
      if (!ownsRequest()) return false;
      try {
        if (sessionConnections.current.get(pending.id) !== pending.connection) {
          throw new Error('会话连接已改变，请重新关闭标签。');
        }
        const outcome = await adapter.closeSessionTab(pending.connection, confirmation);
        if (!ownsRequest()) return false;
        if (sessionConnections.current.get(pending.id) !== pending.connection) {
          throw new Error('会话连接已改变，请重新关闭标签。');
        }
        if (outcome.status === 'closed') {
          releaseSession(pending.id);
          return true;
        }
        publishCloseRequest(activeViewRef.current === 'workbench'
          ? { id: pending.id, connection: pending.connection, confirmation: outcome }
          : undefined);
      } catch (error) {
        if (!ownsRequest()) return false;
        const message = productMessage(error instanceof Error ? error.message : undefined, '无法确认这次标签关闭，请稍后重试。');
        if (pending.confirmation && activeViewRef.current === 'workbench') publishCloseRequest({ ...pending, completion: undefined, error: message });
        else {
          publishCloseRequest();
          notify('暂时无法关闭标签', message, 'warning');
        }
      }
      return false;
    });
    publishCloseRequest(pending);
  };
  const closeSession = (id: string) => {
    if (!mountedSessionIdsRef.current.includes(id)) return;
    const current = closeRequestRef.current;
    if (current) {
      if (!current.completion && current.id === id) dismissTabClose();
      return;
    }
    const connection = sessionConnections.current.get(id);
    if (!connection) {
      if (observationsRef.current[id]?.historyOnly) releaseSession(id);
      else notify('暂时无法关闭标签', '会话正在重新连接。请在连接恢复后重试。', 'warning');
      return;
    }
    submitTabClose({ id, connection }, { formIds: [], otherClients: false });
  };
  const confirmTabClose = () => {
    const request = closeRequestRef.current;
    if (request?.confirmation && !request.completion) {
      const { formIds, otherClients } = request.confirmation;
      submitTabClose(request, { formIds, otherClients });
    }
  };
  const onBrowserShortcut = (command: 'search' | 'new-session') => {
    flushSync(() => {
      if (command === 'search') setSearchOpen(value => !value);
      else if (canCreateSession) setNewSessionOpen(true);
    });
  };
  const renderWorkbench = (session: SessionSummary) => <CanvasWorkbench
    adapter={adapter}
    session={session}
    loaded={canvas.layout.windows.has(session.id)}
    bootstrap={bootstrap}
    userServiceStatus={userServiceStatus}
    userServiceError={userServiceError}
    userCapabilityMutation={userCapabilityMutation}
    scheduledApi={scheduledApi}
    promptDraftStore={promptDraftStore}
    suggestionCache={suggestionCache}
    toolDecisionStore={toolDecisionStore}
    pendingTabClose={pendingTabClose}
    changeSessionList={changeSessionList}
    invalidateSessionList={invalidateSessionList}
    readSessionList={readSessionList}
    notify={notify}
    runMcpAuthorization={runMcpAuthorization}
    onObservation={onSessionObservation}
    onConnection={onSessionConnection}
    onPublishStatus={publishSessionStatus}
    onSessionMissing={forgetSession}
    onClose={closeSession}
    onOpenSession={openSession}
    onRenameSession={setRenameTarget}
    onOpenScheduledTask={id => {
      navigate('scheduled');
      setFocusScheduledTaskId(id);
    }}
    onOpenUserCapabilities={kind => {
      navigate('capabilities');
      setCapabilityTab(kind ?? 'plugins');
    }}
    onOpenSettings={(section, highlightHome) => navigate('settings', section, highlightHome)}
    onBrowserShortcut={onBrowserShortcut}
  />;
  const canvasSessions = mountedSessionIds.map(id => {
    const session = sessionList.find(item => item.id === id) ?? { ...emptySession, id, title: '会话' };
    const observation = sessionObservations[id];
    return { id, title: session.title, pendingApproval: observation?.approvalPending,
      running: observation?.running, content: renderWorkbench(session) };
  });
  const sessionOpenErrors = Object.fromEntries(
    [...pendingOpens.current.keys()].map(id => [id, sessionObservations[id]?.error]),
  );
  const serviceUnavailable = userServiceStatus === 'offline' || userServiceStatus === 'failed';
  const emptyContent = openingSession ? <SessionOpeningView
    session={openingSession}
    workspace={openingSession.workspace ?? emptyWorkspace}
    error={openingObservation?.error}
    failure={openingObservation?.failure}
    onRetry={() => void requestOpenSession(openingSession.id, pendingOpens.current.get(openingSession.id)?.placement)}
    onRestart={desktopHost() ? () => {
      void desktopHost()!.restartWorkbench().catch(error => notify('工作台未能重启',
        error instanceof Error ? error.message : '请重新打开 Pulsara。', 'warning'));
    } : undefined}
  /> : mountedSessionIds.length ? undefined
    : serviceUnavailable || userServiceStatus === 'reconnecting' ? (
    <section className="session-opening__body" aria-label="会话连接状态">
      <div className="session-opening__content">
        <div>
          <h3 role="status">{serviceUnavailable ? '本地服务连接失败' : '正在重新连接本地服务…'}</h3>
          {serviceUnavailable && userServiceError && <p>{userServiceError}</p>}
        </div>
        {serviceUnavailable && <button className="ghost-button" type="button" onClick={reconnectUserService}>重新连接</button>}
      </div>
    </section>
  ) : (
    <section className="session-canvas__welcome is-welcome" aria-label="会话欢迎页">
      <div className="composer-wrap">
        <div className="composer-frame">
          <div className="welcome-heading"><WelcomeTypewriter active={showWorkbench} /></div>
          <div className="composer session-required-composer">
            <span className="session-required-composer__icon" aria-hidden="true"><Sparkles size={15} /></span>
            <span className="session-required-composer__copy">
              <strong>创建或选择会话后开始</strong>
              <small>提问、写代码，或一起探索一个想法。</small>
            </span>
            <button className="secondary-action" type="button" disabled={!canCreateSession} onClick={openNewSession}>
              <MessageSquarePlus size={13} aria-hidden="true" /> 创建会话
            </button>
          </div>
        </div>
      </div>
    </section>
  );

  return (
    <UserAvatarProvider adapter={adapter}>
    <McpAuthorizationContext.Provider value={mcpAuthorizationStatuses}>
    <ToolResultDisplayContext.Provider value={{ showBuiltinToolResults, onChange: setShowBuiltinToolResults }}>
    <main data-platform={desktopHost()?.platform} className={`pulsara-shell desktop-shell${sidebarOpen ? " sidebar-visible" : ""}${activeView === 'workbench' ? ' is-workbench' : ' is-surface'}`}>
      <DesktopTitlebar conversation={activeView === 'workbench'} onTabsHost={setCanvasTabHost}
        onToggleSidebar={showWorkbench ? () => setSidebarOpen(value => !value) : undefined} />
      <div className="desktop-navigation"><ActivityRail activeView={activeView} onNavigate={navigate} />

      {activeView === 'workbench' && !databaseBlocked && (
        <SessionSidebar
          workspace={emptyWorkspace}
          sessions={sessionList}
          activeSessionId={activeSessionId}
          openingSessionId={openingObservation?.error ? undefined : openingSessionId}
          sessionOpenErrors={sessionOpenErrors}
          runtimeStatus={userServiceStatus}
          isOpen={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
          onSelectSession={id => void openSession(id)}
          onSessionPointerDown={canvas.beginSidebarDrag}
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
      <SessionCanvas controller={canvas} visible={showWorkbench} sessions={canvasSessions} tabHost={canvasTabHost}
        onOpenSession={openSession} onCloseSession={closeSession} emptyContent={emptyContent}
        closingTab={closeRequest ? { id: closeRequest.id, confirmation: closeRequest.confirmation,
          busy: Boolean(closeRequest.completion), error: closeRequest.error, onKeep: dismissTabClose, onConfirm: confirmTabClose } : undefined} />
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
          onDatabaseReset={() => { globalReads.configuration = { databaseReady: false }; invalidateSessionList(); removeAllSessions(); for (const session of sessionList) forgetSession(session.id); }}
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
    </main>
    </ToolResultDisplayContext.Provider>
    </McpAuthorizationContext.Provider>
    </UserAvatarProvider>
  );
}

function CanvasWorkbench(props: Omit<Parameters<typeof SessionWorkbench>[0], 'visible' | 'focused'>) {
  const { visible, focused } = useSessionCanvasWindow();
  return <SessionWorkbench {...props} visible={visible} focused={focused} />;
}
