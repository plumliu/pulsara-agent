import type {
  AgentTask,
  CapabilityOperation,
  CapabilitySnapshot,
  McpCreateInput,
  McpServerStatus,
  Message,
  PermissionMode,
  ProjectCapabilityAdoption,
  ProjectCapabilityMutationResult,
  ReasoningBlock,
  SessionSummary,
  SessionWorkspaceSelection,
  SkillInstallResult,
  SubagentRun,
  TodoRun,
  ToolTrace,
  UserCapabilitySnapshot,
  Workspace,
} from './pulsara-types';
import { protocolPermissionModes } from './pulsara-types';

export interface RuntimeBootstrap {
  application: { name: string; version: string; transport: string };
  workspace: Workspace;
  provider: {
    provider: string;
    endpoint_origin: string;
    pro_model: string;
    flash_model: string;
    api_key_set: boolean;
  };
  protocol: { major: number; minor: number };
  runtime: { status: string; origin: string };
}

export interface RuntimeProjection {
  messages: Message[];
  contextCompaction?: ContextCompactionBoundary;
  isRunning: boolean;
  queuedCount: number;
  planMode: boolean;
  activeTurnId?: string;
  control: ProtocolCanonicalControl;
  liveControl: ProtocolLiveControlSnapshot;
  agentTasks: AgentTask[];
  todo?: TodoRun;
  eventSequence: number;
  liveOwnerEpoch: number;
  liveRevision: number;
  liveControlOwnerEpoch: number;
  liveControlRevision: number;
  interaction?: RuntimeInteractionSummary;
}

export interface ContextCompactionBoundary {
  contextBindingRevisionId: string;
  turnId: string;
  sourceThroughSequence: number;
  adoptedAfterEntrySequence: number;
  acceptedAt: string;
}

export type RuntimeInteractionSummary =
  | {
    id: string;
    kind: 'tool-confirmation';
    prompt: string;
    options: string[];
  }
  | {
    id: string;
    kind: 'plan-question' | 'plan-draft';
    workflowId: string;
    workflowRevision: number;
  };

export type RuntimeInteractionContent =
  | {
    kind: 'tool-confirmation';
    prompt: string;
    options: string[];
  }
  | {
    kind: 'plan-question';
    question: string;
    options: Array<{
      ordinal: number;
      label: string;
      description: string;
      recommended: boolean;
    }>;
    allowFreeText: boolean;
  }
  | {
    kind: 'plan-draft';
    body: string;
  };

export type RuntimeInteractionResolution =
  | { kind: 'tool'; decision: 'allow' | 'deny' }
  | { kind: 'plan-question-option'; optionOrdinal: number }
  | { kind: 'plan-question-text'; text: string }
  | {
    kind: 'plan-draft';
    decision: 'approve' | 'revise' | 'cancel';
    feedback?: string;
  };

/** Browser boundary for the local Pulsara application. */
export interface RuntimeAdapter {
  bootstrap(): Promise<RuntimeBootstrap>;
  connect(sessionId: string, takeover?: boolean): Promise<RuntimeConnection>;
  createSession(selection: SessionWorkspaceSelection): Promise<SessionSummary>;
  listSessions(): Promise<SessionSummary[]>;
  listSessionTasks(sessionId: string, cursor?: string): Promise<AgentTaskPage>;
  inspectCapabilities(sessionId: string): Promise<CapabilitySnapshot>;
  reconnectMcpServer(sessionId: string, serverId: string): Promise<CapabilitySnapshot>;
  installSkill(
    sessionId: string,
    sourcePath: string,
  ): Promise<{
    installation: SkillInstallResult;
    adoption: ProjectCapabilityAdoption;
    capabilities: CapabilitySnapshot;
  }>;
  setProjectSkillEnabled(
    sessionId: string,
    skillId: string,
    enabled: boolean,
  ): Promise<ProjectCapabilityMutationResult>;
  createProjectMcp(
    sessionId: string,
    input: McpCreateInput,
  ): Promise<ProjectCapabilityMutationResult>;
  setProjectMcpEnabled(
    sessionId: string,
    serverId: string,
    configIdentity: string,
    enabled: boolean,
  ): Promise<ProjectCapabilityMutationResult>;
  removeProjectMcp(
    sessionId: string,
    serverId: string,
    configIdentity: string,
  ): Promise<ProjectCapabilityMutationResult>;
  inspectUserCapabilities(activeSessionId?: string): Promise<UserCapabilitySnapshot>;
  refreshUserCapabilities(activeSessionId?: string): Promise<UserCapabilitySnapshot>;
  installUserSkill(sourcePath: string, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  setUserSkillEnabled(skillPath: string, enabled: boolean, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  createUserMcp(input: McpCreateInput, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  setUserMcpEnabled(serverId: string, enabled: boolean, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  installUserPlugin(sourcePath: string, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  setUserPluginEnabled(
    pluginId: string,
    packageInstallId: string,
    enabled: boolean,
    activeSessionId?: string,
  ): Promise<{ operation: CapabilityOperation; capabilities: UserCapabilitySnapshot }>;
  removeUserPlugin(pluginId: string, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  openCapabilityRoot(root: 'agents' | 'pulsara'): Promise<void>;
}

export interface AgentTaskPage {
  tasks: AgentTask[];
  totalCount: number;
  remainingCount: number;
  nextCursor?: string;
}

export interface RuntimeConnection {
  readonly sessionId: string;
  readonly role: 'observer' | 'controller';
  readonly generation: number;
  current(): RuntimeProjection;
  snapshot(): Promise<RuntimeProjection>;
  observe(signal?: AbortSignal): Promise<RuntimeProjection>;
  submitPrompt(text: string, permission: PermissionMode): Promise<CommandReceipt>;
  steerActiveTurn(text: string, targetTurnId: string): Promise<CommandReceipt>;
  stopActiveTurn(): Promise<CommandReceipt>;
  compactContext(targetTurnId?: string): Promise<CommandReceipt>;
  acceptSubagentCompletion(taskId: string, permission: PermissionMode): Promise<CommandReceipt>;
  enterPlan(reason: string, permission: PermissionMode): Promise<CommandReceipt>;
  readInteraction(interaction: RuntimeInteractionSummary): Promise<RuntimeInteractionContent>;
  resolveInteraction(
    interaction: RuntimeInteractionSummary,
    resolution: RuntimeInteractionResolution,
  ): Promise<CommandReceipt>;
  queryCommand(commandId: string): Promise<CommandReceipt | undefined>;
  close(): Promise<void>;
}

export interface CommandReceipt {
  commandId: string;
  status: 'succeeded' | 'rejected' | 'pending';
  targetId?: string;
  publicCode?: string;
  publicMessage?: string;
}

export type RuntimeCommandKind =
  | 'SUBMIT_PROMPT'
  | 'STEER_ACTIVE_TURN'
  | 'STOP_ACTIVE_TURN'
  | 'COMPACT_CONTEXT'
  | 'ACCEPT_SUBAGENT_COMPLETION'
  | 'ENTER_PLAN';

export class RuntimeApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly retryable: boolean,
  ) {
    super(message);
  }
}

export class RuntimeGapError extends RuntimeApiError {
  constructor(readonly gapKind: string, reason: string) {
    super('PROTOCOL_GAP', reason || '连接状态需要刷新。', true);
  }
}

interface ProtocolContent {
  kind?: string;
  inline_content?: string;
  digest?: string;
  size?: string | number;
  media_type?: string;
  codec?: string;
}

interface ProtocolAssistantBlock {
  block_id: string;
  block_kind: string;
  tool_call_id?: string;
  tool_name?: string;
  tool_arguments_preview?: string;
  content?: ProtocolContent;
}

interface ProtocolReasoningBlock {
  block_id: string;
  ordinal?: string | number;
  presentation_kind?: string;
  content?: ProtocolContent;
}

interface ProtocolEntry {
  entry_id: string;
  turn_id: string;
  entry_sequence: string | number;
  entry_kind: string;
  scope_kind?: string;
  scope_subagent_task_id?: string;
  content?: ProtocolContent;
  blocks?: ProtocolAssistantBlock[];
  reasoning_blocks?: ProtocolReasoningBlock[];
  accepted_at_utc?: string;
  source_subagent_task_id?: string;
}

interface ProtocolActiveTurn {
  turn_id: string;
  status: string;
  scope_kind?: string;
}

interface ProtocolSubagentTask {
  task_id: string;
  parent_turn_id?: string;
  batch_id?: string;
  task_key?: string;
  status: string;
  objective: string;
  label?: string;
  profile?: string;
  display_role?: string;
  context_mode?: string;
  context_last_n_turns?: string | number;
  pending_reason?: string;
  terminal_reason?: string;
  terminal_public_detail?: string;
  result_id?: string;
  completion_delivered?: boolean;
  result_summary?: string;
  dependency_task_ids?: string[];
}

interface ProtocolTaskInventoryRecord {
  id: string;
  parent_turn_id?: string;
  batch_id?: string;
  task_key?: string;
  label?: string;
  profile?: string;
  display_role?: string;
  context?: { mode?: string; last_n_turns?: string | number | null };
  objective?: string;
  status?: string;
  pending_reason?: string | null;
  terminal_reason?: string | null;
  terminal_public_detail?: string | null;
  completion_delivered?: boolean;
  accepted_at?: string;
  terminal_at?: string | null;
  dependencies?: Array<{
    task_id?: string;
    task_key?: string | null;
    label?: string | null;
    status?: string;
    result_summary?: string | null;
  }>;
  result?: {
    id?: string;
    entry_id?: string | null;
    source?: string | null;
    summary?: string | null;
    output_preview?: string | null;
    diagnostics?: Array<Record<string, unknown>>;
  } | null;
}

interface ProtocolTaskInventoryPage {
  tasks?: ProtocolTaskInventoryRecord[];
  total_count?: string | number;
  remaining_count?: string | number;
  next_cursor?: string | null;
}

interface ProtocolToolAttempt {
  attempt_id?: string;
  assistant_entry_id?: string;
  tool_call_id?: string;
  result_state?: string;
  result_entry_id?: string;
}

export interface ProtocolCanonicalControl {
  session_lifecycle?: string;
  active_turns?: ProtocolActiveTurn[];
  prompt_queue?: Array<Record<string, unknown>>;
  prompt_queue_total_count?: string | number;
  tool_attempts?: ProtocolToolAttempt[];
  subagent_tasks?: ProtocolSubagentTask[];
  active_plan_workflow?: Record<string, unknown>;
  open_plan_interaction?: Record<string, unknown>;
  latest_context_compaction?: {
    turn_id?: string;
    context_binding_revision_id?: string;
    source_through_sequence?: string | number;
    adopted_after_entry_sequence?: string | number;
    accepted_at_utc?: string;
  };
}

interface ProtocolLiveEvent {
  live_revision?: string | number;
  event_type: string;
  turn_id?: string;
  scope_kind?: string;
  scope_subagent_task_id?: string;
  draft_identity?: string;
  generation_id?: string;
  block_id?: string;
  payload?: Record<string, Record<string, unknown>>;
}

interface ProtocolSettlement {
  kind: string;
  draft_identity?: string;
  generation_id?: string;
}

interface ProtocolLiveSnapshot {
  owner_epoch?: string | number;
  through_revision?: string | number;
  events?: ProtocolLiveEvent[];
  settlements?: ProtocolSettlement[];
}

export interface ProtocolLiveControlSnapshot {
  owner_epoch?: string | number;
  live_revision?: string | number;
  current_interaction?: Record<string, unknown>;
  current_todos?: ProtocolTodoRunSnapshot[];
  compaction_in_progress?: boolean;
  input_admission_deferred?: boolean;
}

interface ProtocolTodoRunSnapshot {
  todo_run_id: string;
  todo_revision?: string | number;
  scope_kind?: string;
  scope_subagent_task_id?: string;
  disposition?: string;
  ordered_items?: Array<{ ordinal?: number; text: string; status: string }>;
  pending_count?: string | number;
  in_progress_count?: string | number;
  completed_count?: string | number;
}

interface ProtocolCanonicalSnapshot {
  session_id: string;
  writer_generation?: string | number;
  event_sequence_cut?: string | number;
  entries?: ProtocolEntry[];
  older_history_cursor?: ProtocolHistoryCursor;
  control?: ProtocolCanonicalControl;
}

interface ProtocolHistoryCursor {
  session_id: string;
  cut_sequence: string | number;
  entry_sequence: string | number;
}

interface ProtocolHistoryPage {
  entries?: ProtocolEntry[];
  older_history_cursor?: ProtocolHistoryCursor;
  has_more?: boolean;
}

interface ConnectPayload {
  connection_id: string;
  connection_generation: number;
  session_id: string;
  role: 'observer' | 'controller';
  live_hello: {
    live_owner_epoch?: string | number;
    live_revision?: string | number;
    live_snapshot?: ProtocolLiveSnapshot;
  };
  snapshot: { snapshot: ProtocolCanonicalSnapshot };
  live_control_snapshot: { snapshot: ProtocolLiveControlSnapshot };
}

interface ObservationPayload {
  through_event_sequence?: string | number;
  committed?: Array<{
    projection_kind: string;
    entry?: ProtocolEntry;
    current_control?: ProtocolCanonicalControl;
  }>;
  live_owner_epoch?: string | number;
  through_live_revision?: string | number;
  live?: ProtocolLiveEvent[];
  settlements?: ProtocolSettlement[];
  live_control_owner_epoch?: string | number;
  through_live_control_revision?: string | number;
  live_control?: Array<{
    kind: string;
    interaction?: Record<string, unknown>;
  }>;
  gap?: { kind: string; reason?: string };
}

interface LiveDraft {
  id: string;
  turnId: string;
  scopeKind: string;
  taskId: string;
  body: string;
  reasoning: ReasoningBlock[];
  traces: ToolTrace[];
}

interface LiveTaskProgress {
  status: string;
  summary: string;
}

interface ProtocolError {
  stable_code: string;
  public_message?: string;
}

interface ProtocolCommandOutcome {
  command_id: string;
  status: string;
  target_id?: string;
  public_code?: string;
  public_message?: string;
}

export function selectPromptCommand(isTurnActive: boolean, steer: boolean): RuntimeCommandKind {
  if (isTurnActive && steer) return 'STEER_ACTIVE_TURN';
  return 'SUBMIT_PROMPT';
}

const browserInstanceStorageKey = 'pulsara-browser-instance-id-v1';
const canonicalUuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function resolveBrowserInstanceId(): string {
  const next = () => crypto.randomUUID();
  if (typeof window === 'undefined') return next();
  let existing: string | null = null;
  try {
    existing = window.sessionStorage.getItem(browserInstanceStorageKey);
  } catch {
    return next();
  }
  const navigation = typeof window.performance?.getEntriesByType === 'function'
    ? window.performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
    : undefined;
  if (navigation?.type === 'reload' && existing && canonicalUuidPattern.test(existing)) {
    return existing;
  }
  const created = next();
  try {
    window.sessionStorage.setItem(browserInstanceStorageKey, created);
  } catch {
    // The process-local value still identifies this page while storage is unavailable.
  }
  return created;
}

export class LocalHttpRuntimeAdapter implements RuntimeAdapter {
  private readonly browserInstanceId: string;

  constructor(browserInstanceId = resolveBrowserInstanceId()) {
    this.browserInstanceId = browserInstanceId;
  }

  async bootstrap(): Promise<RuntimeBootstrap> {
    const value = await apiRequest<Omit<RuntimeBootstrap, 'workspace'> & {
      workspace: { id: string; name: string; path: string; kind: string };
    }>('/api/app/bootstrap');
    return {
      ...value,
      workspace: {
        id: value.workspace.id,
        name: value.workspace.name,
        path: value.workspace.path,
        kind: value.workspace.kind === 'transient' || value.workspace.kind === 'quick'
          ? 'quick'
          : 'project',
      },
    };
  }

  async listSessions(): Promise<SessionSummary[]> {
    const payload = await apiRequest<{ sessions: Array<Record<string, unknown>> }>('/api/sessions');
    return payload.sessions.map(projectSessionSummary);
  }

  async listSessionTasks(sessionId: string, cursor?: string): Promise<AgentTaskPage> {
    const query = new URLSearchParams({ limit: '50' });
    if (cursor) query.set('cursor', cursor);
    const payload = await apiRequest<ProtocolTaskInventoryPage>(
      `/api/sessions/${encodeURIComponent(sessionId)}/tasks?${query.toString()}`,
    );
    return {
      tasks: (payload.tasks ?? []).map(projectTaskInventoryRecord),
      totalCount: numeric(payload.total_count),
      remainingCount: numeric(payload.remaining_count),
      nextCursor: payload.next_cursor || undefined,
    };
  }

  async inspectCapabilities(sessionId: string): Promise<CapabilitySnapshot> {
    const payload = await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities`,
    );
    return projectCapabilitySnapshot(payload);
  }

  async reconnectMcpServer(
    sessionId: string,
    serverId: string,
  ): Promise<CapabilitySnapshot> {
    const payload = await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/${encodeURIComponent(serverId)}/reconnect`,
      { method: 'POST' },
    );
    return projectCapabilitySnapshot(payload);
  }

  async installSkill(
    sessionId: string,
    sourcePath: string,
  ): Promise<{
    installation: SkillInstallResult;
    adoption: ProjectCapabilityAdoption;
    capabilities: CapabilitySnapshot;
  }> {
    const payload = await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/skills/install`,
      {
        method: 'POST',
        body: JSON.stringify({ source_path: sourcePath }),
      },
    );
    return {
      installation: projectSkillInstallResult(asRecord(payload.installation)),
      adoption: projectCapabilityAdoption(asRecord(payload.adoption)),
      capabilities: projectCapabilitySnapshot(asRecord(payload.capabilities)),
    };
  }

  async setProjectSkillEnabled(
    sessionId: string,
    skillId: string,
    enabled: boolean,
  ): Promise<ProjectCapabilityMutationResult> {
    const payload = await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/skills/enabled`,
      {
        method: 'POST',
        body: JSON.stringify({ skill_id: skillId, enabled }),
      },
    );
    return projectCapabilityMutation(payload);
  }

  async createProjectMcp(
    sessionId: string,
    input: McpCreateInput,
  ): Promise<ProjectCapabilityMutationResult> {
    const payload = await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp`,
      {
        method: 'POST',
        body: JSON.stringify({
          server_id: input.serverId,
          display_name: input.displayName,
          transport: input.transport,
          endpoint: input.endpoint,
          command: input.command,
          args: input.args,
          available_to_subagents: input.availableToSubagents,
        }),
      },
    );
    return projectCapabilityMutation(payload);
  }

  async setProjectMcpEnabled(
    sessionId: string,
    serverId: string,
    configIdentity: string,
    enabled: boolean,
  ): Promise<ProjectCapabilityMutationResult> {
    const payload = await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/${encodeURIComponent(serverId)}/enabled`,
      {
        method: 'POST',
        body: JSON.stringify({ enabled, config_identity: configIdentity }),
      },
    );
    return projectCapabilityMutation(payload);
  }

  async removeProjectMcp(
    sessionId: string,
    serverId: string,
    configIdentity: string,
  ): Promise<ProjectCapabilityMutationResult> {
    const payload = await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/${encodeURIComponent(serverId)}`,
      {
        method: 'DELETE',
        body: JSON.stringify({ config_identity: configIdentity }),
      },
    );
    return projectCapabilityMutation(payload);
  }

  async inspectUserCapabilities(activeSessionId?: string): Promise<UserCapabilitySnapshot> {
    const query = activeSessionId
      ? `?${new URLSearchParams({ active_session_id: activeSessionId }).toString()}`
      : '';
    return projectUserCapabilitySnapshot(await apiRequest<Record<string, unknown>>(
      `/api/capabilities${query}`,
    ));
  }

  async refreshUserCapabilities(activeSessionId?: string): Promise<UserCapabilitySnapshot> {
    return projectUserCapabilitySnapshot(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/refresh',
      { method: 'POST', body: JSON.stringify(activeSessionId ? { active_session_id: activeSessionId } : {}) },
    ));
  }

  async installUserSkill(sourcePath: string, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/skills/install',
      {
        method: 'POST',
        body: JSON.stringify({ source_path: sourcePath, ...(activeSessionId ? { active_session_id: activeSessionId } : {}) }),
      },
    ));
  }

  async setUserSkillEnabled(skillPath: string, enabled: boolean, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/skills/enabled',
      {
        method: 'POST',
        body: JSON.stringify({
          path: skillPath,
          enabled,
          ...(activeSessionId ? { active_session_id: activeSessionId } : {}),
        }),
      },
    ));
  }

  async createUserMcp(input: McpCreateInput, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/mcp',
      {
        method: 'POST',
        body: JSON.stringify({
          server_id: input.serverId,
          display_name: input.displayName,
          transport: input.transport,
          endpoint: input.endpoint,
          command: input.command,
          args: input.args,
          available_to_subagents: input.availableToSubagents,
          ...(activeSessionId ? { active_session_id: activeSessionId } : {}),
        }),
      },
    ));
  }

  async setUserMcpEnabled(serverId: string, enabled: boolean, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/mcp/${encodeURIComponent(serverId)}/enabled`,
      {
        method: 'POST',
        body: JSON.stringify({ enabled, ...(activeSessionId ? { active_session_id: activeSessionId } : {}) }),
      },
    ));
  }

  async installUserPlugin(sourcePath: string, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/plugins/install',
      {
        method: 'POST',
        body: JSON.stringify({ source_path: sourcePath, ...(activeSessionId ? { active_session_id: activeSessionId } : {}) }),
      },
    ));
  }

  async setUserPluginEnabled(
    pluginId: string,
    packageInstallId: string,
    enabled: boolean,
    activeSessionId?: string,
  ) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/plugins/${encodeURIComponent(pluginId)}/enabled`,
      {
        method: 'POST',
        body: JSON.stringify({
          enabled,
          package_install_id: packageInstallId,
          ...(activeSessionId ? { active_session_id: activeSessionId } : {}),
        }),
      },
    ));
  }

  async removeUserPlugin(pluginId: string, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/plugins/${encodeURIComponent(pluginId)}`,
      {
        method: 'DELETE',
        body: JSON.stringify(activeSessionId ? { active_session_id: activeSessionId } : {}),
      },
    ));
  }

  async openCapabilityRoot(root: 'agents' | 'pulsara'): Promise<void> {
    await apiRequest(`/api/capabilities/roots/${root}/open`, { method: 'POST' });
  }

  async createSession(selection: SessionWorkspaceSelection): Promise<SessionSummary> {
    const payload = await apiRequest<{ session: Record<string, unknown> }>('/api/sessions', {
      method: 'POST',
      body: JSON.stringify(selection.kind === 'quick'
        ? { workspace_kind: 'quick' }
        : { workspace_kind: 'project', workspace_path: selection.path }),
    });
    return projectSessionSummary(payload.session);
  }

  async connect(sessionId: string, takeover = false): Promise<RuntimeConnection> {
    const payload = await apiRequest<ConnectPayload>(
      `/api/sessions/${encodeURIComponent(sessionId)}/connections`,
      {
        method: 'POST',
        body: JSON.stringify({
          browser_instance_id: this.browserInstanceId,
          ...(takeover ? { takeover: true } : {}),
        }),
      },
    );
    const connection = new LocalRuntimeConnection(payload);
    await connection.initialize();
    return connection;
  }
}

class LocalRuntimeConnection implements RuntimeConnection {
  readonly sessionId: string;
  readonly role: 'observer' | 'controller';
  readonly generation: number;
  private readonly connectionId: string;
  private entries = new Map<string, ProtocolEntry>();
  private drafts = new Map<string, LiveDraft>();
  private taskProgress = new Map<string, LiveTaskProgress>();
  private control: ProtocolCanonicalControl = {};
  private liveControl: ProtocolLiveControlSnapshot = {};
  private eventSequence = 0;
  private liveOwnerEpoch = 0;
  private liveRevision = 0;
  private liveControlOwnerEpoch = 0;
  private liveControlRevision = 0;
  private writerGeneration = 0;
  private olderHistoryCursor?: ProtocolHistoryCursor;
  private closed = false;

  constructor(payload: ConnectPayload) {
    this.connectionId = payload.connection_id;
    this.generation = payload.connection_generation;
    this.sessionId = payload.session_id;
    this.role = payload.role;
    this.replaceSnapshot(payload.snapshot.snapshot);
    this.liveControl = payload.live_control_snapshot.snapshot ?? {};
    this.liveControlOwnerEpoch = numeric(this.liveControl.owner_epoch);
    this.liveControlRevision = numeric(this.liveControl.live_revision);
    const live = payload.live_hello.live_snapshot ?? {};
    this.liveOwnerEpoch = numeric(payload.live_hello.live_owner_epoch ?? live.owner_epoch);
    this.liveRevision = numeric(payload.live_hello.live_revision ?? live.through_revision);
    this.applyLive(live.events ?? [], live.settlements ?? []);
  }

  async initialize(): Promise<void> {
    await this.backfillOlderHistory();
    await this.hydrateReasoningContent();
  }

  current(): RuntimeProjection {
    return this.project();
  }

  async snapshot(): Promise<RuntimeProjection> {
    const frame = await this.post<{
      snapshot?: { snapshot?: ProtocolCanonicalSnapshot };
      error?: ProtocolError;
    }>('snapshot', {});
    assertProtocolFrame(frame);
    if (!frame.snapshot?.snapshot) {
      throw new RuntimeApiError('PROTOCOL_RESPONSE_INVALID', '本地服务返回的数据不完整。', true);
    }
    this.replaceSnapshot(frame.snapshot.snapshot);
    await this.backfillOlderHistory();
    await this.hydrateReasoningContent();
    return this.project();
  }

  async observe(signal?: AbortSignal): Promise<RuntimeProjection> {
    const frame = await this.post<{ observation?: ObservationPayload; error?: ProtocolError }>('observe', {
      after_event_sequence: this.eventSequence,
      live_owner_epoch: this.liveOwnerEpoch,
      after_live_revision: this.liveRevision,
      live_control_owner_epoch: this.liveControlOwnerEpoch,
      after_live_control_revision: this.liveControlRevision,
      maximum_events: 128,
      maximum_bytes: 2 << 20,
      wait_ms: 2000,
    }, signal);
    assertProtocolFrame(frame);
    if (!frame.observation) {
      throw new RuntimeApiError('PROTOCOL_RESPONSE_INVALID', '本地服务暂时没有返回更新。', true);
    }
    const observation = frame.observation;
    if (observation.gap?.kind) {
      throw new RuntimeGapError(observation.gap.kind, '连接状态需要刷新。');
    }
    for (const committed of observation.committed ?? []) {
      if (committed.projection_kind === 'IMMUTABLE_ENTRY' && committed.entry) {
        this.entries.set(committed.entry.entry_id, committed.entry);
      } else if (committed.projection_kind === 'CURRENT_CONTROL' && committed.current_control) {
        this.control = committed.current_control;
      }
    }
    this.applyLive(observation.live ?? [], observation.settlements ?? []);
    for (const event of observation.live_control ?? []) {
      if (event.kind === 'LIVE_INTERACTION_CLOSED') delete this.liveControl.current_interaction;
      else if (event.interaction) this.liveControl.current_interaction = event.interaction;
    }
    this.eventSequence = numeric(observation.through_event_sequence ?? this.eventSequence);
    this.liveOwnerEpoch = numeric(observation.live_owner_epoch ?? this.liveOwnerEpoch);
    this.liveRevision = numeric(observation.through_live_revision ?? this.liveRevision);
    this.liveControlOwnerEpoch = numeric(observation.live_control_owner_epoch ?? this.liveControlOwnerEpoch);
    this.liveControlRevision = numeric(observation.through_live_control_revision ?? this.liveControlRevision);
    await this.hydrateReasoningContent();
    return this.project();
  }

  submitPrompt(text: string, permission: PermissionMode): Promise<CommandReceipt> {
    return this.command('SUBMIT_PROMPT', { text, requested_permission_mode: protocolPermissionModes[permission] });
  }

  steerActiveTurn(text: string, targetTurnId: string): Promise<CommandReceipt> {
    return this.command('STEER_ACTIVE_TURN', { text, target_turn_id: targetTurnId });
  }

  stopActiveTurn(): Promise<CommandReceipt> {
    return this.command('STOP_ACTIVE_TURN');
  }

  compactContext(targetTurnId?: string): Promise<CommandReceipt> {
    return this.command('COMPACT_CONTEXT', {
      ...(targetTurnId ? { target_turn_id: targetTurnId } : {}),
      force: true,
    });
  }

  acceptSubagentCompletion(taskId: string, permission: PermissionMode): Promise<CommandReceipt> {
    return this.command('ACCEPT_SUBAGENT_COMPLETION', {
      subagent_task_id: taskId,
      requested_permission_mode: protocolPermissionModes[permission],
    });
  }

  enterPlan(reason: string, permission: PermissionMode): Promise<CommandReceipt> {
    return this.command('ENTER_PLAN', { text: reason, requested_permission_mode: protocolPermissionModes[permission] });
  }

  async readInteraction(
    interaction: RuntimeInteractionSummary,
  ): Promise<RuntimeInteractionContent> {
    if (interaction.kind === 'tool-confirmation') {
      return {
        kind: 'tool-confirmation',
        prompt: interaction.prompt,
        options: interaction.options,
      };
    }
    if (interaction.kind === 'plan-question') {
      const frame = await this.post<{
        plan_question?: {
          interaction_id?: string;
          question?: string;
          options?: Array<{
            ordinal?: string | number;
            label?: string;
            description?: string;
            recommended?: boolean;
          }>;
          allow_free_text?: boolean;
        };
        error?: ProtocolError;
      }>('read-plan-question', { interaction_id: interaction.id });
      assertProtocolFrame(frame);
      const question = frame.plan_question;
      if (!question || question.interaction_id !== interaction.id) {
        throw new RuntimeApiError('INTERACTION_STALE', '这个问题已经更新，请查看最新内容。', true);
      }
      return {
        kind: 'plan-question',
        question: productVisibleText(question.question ?? ''),
        options: (question.options ?? []).map((option) => ({
          ordinal: numeric(option.ordinal),
          label: productVisibleText(option.label ?? ''),
          description: productVisibleText(option.description ?? ''),
          recommended: Boolean(option.recommended),
        })),
        allowFreeText: Boolean(question.allow_free_text),
      };
    }

    let offset = 0;
    let digest: string | undefined;
    let body = '';
    while (true) {
      const frame = await this.post<{
        plan_draft?: {
          interaction_id?: string;
          plan_utf8_digest?: string;
          offset_utf8_bytes?: string | number;
          body?: string;
          next_offset_utf8_bytes?: string | number;
          eof?: boolean;
        };
        error?: ProtocolError;
      }>('read-plan-draft', {
        interaction_id: interaction.id,
        offset_utf8_bytes: offset,
        limit_bytes: 64 << 10,
        ...(digest ? { expected_plan_utf8_digest: digest } : {}),
      });
      assertProtocolFrame(frame);
      const chunk = frame.plan_draft;
      if (!chunk || chunk.interaction_id !== interaction.id || numeric(chunk.offset_utf8_bytes) !== offset) {
        throw new RuntimeApiError('INTERACTION_STALE', '这份方案已经更新，请查看最新版本。', true);
      }
      digest = chunk.plan_utf8_digest || digest;
      body += chunk.body ?? '';
      if (chunk.eof) break;
      const nextOffset = numeric(chunk.next_offset_utf8_bytes);
      if (nextOffset <= offset) {
        throw new RuntimeApiError('PLAN_DRAFT_INVALID', '方案内容暂时无法完整读取。', true);
      }
      offset = nextOffset;
    }
    return { kind: 'plan-draft', body: productVisibleText(body) };
  }

  async resolveInteraction(
    interaction: RuntimeInteractionSummary,
    resolution: RuntimeInteractionResolution,
  ): Promise<CommandReceipt> {
    const current = this.project().interaction;
    if (!current || current.id !== interaction.id || current.kind !== interaction.kind) {
      throw new RuntimeApiError('INTERACTION_STALE', '这项确认已经更新，请查看最新内容。', true);
    }
    const commandId = `command:web:${crypto.randomUUID()}`;
    if (interaction.kind === 'tool-confirmation') {
      if (resolution.kind !== 'tool') {
        throw new RuntimeApiError('INTERACTION_INVALID', '请选择是否允许这次操作。', false);
      }
      const frame = await this.post<{
        command_outcome?: ProtocolCommandOutcome;
        error?: ProtocolError;
      }>('resolve-interaction', {
        command_id: commandId,
        expected_writer_generation: this.writerGeneration,
        expected_owner_epoch: this.liveControlOwnerEpoch,
        expected_live_revision: this.liveControlRevision,
        interaction_id: interaction.id,
        decision: resolution.decision === 'allow' ? 'INTERACTION_ALLOW' : 'INTERACTION_DENY',
      });
      assertProtocolFrame(frame);
      if (!frame.command_outcome) {
        throw new RuntimeApiError('INTERACTION_RESPONSE_INVALID', '本地服务没有返回确认结果。', true);
      }
      return projectCommand(frame.command_outcome);
    }
    if (resolution.kind === 'tool') {
      throw new RuntimeApiError('INTERACTION_INVALID', '请选择一个规划操作。', false);
    }
    const resolutionFields = resolution.kind === 'plan-question-option'
      ? { resolution_kind: 'question_option', option_ordinal: resolution.optionOrdinal }
      : resolution.kind === 'plan-question-text'
        ? { resolution_kind: 'question_text', free_text: resolution.text }
        : {
          resolution_kind: 'draft',
          draft_decision: {
            approve: 'PLAN_DRAFT_APPROVE',
            revise: 'PLAN_DRAFT_REVISE',
            cancel: 'PLAN_DRAFT_CANCEL',
          }[resolution.decision],
          ...(resolution.decision === 'revise' && resolution.feedback?.trim()
            ? { feedback: resolution.feedback.trim() }
            : {}),
        };
    const frame = await this.post<{
      resolve_plan_interaction?: {
        command_id?: string;
        interaction_status?: string;
        continuation_turn_id?: string;
      };
      error?: ProtocolError;
    }>('resolve-plan-interaction', {
      command_id: commandId,
      attempt_expected_writer_generation: this.writerGeneration,
      interaction_id: interaction.id,
      workflow_id: interaction.workflowId,
      expected_workflow_revision: interaction.workflowRevision,
      ...resolutionFields,
    });
    assertProtocolFrame(frame);
    const outcome = frame.resolve_plan_interaction;
    if (!outcome || outcome.command_id !== commandId) {
      throw new RuntimeApiError('INTERACTION_RESPONSE_INVALID', '本地服务没有返回规划结果。', true);
    }
    return {
      commandId,
      status: 'succeeded',
      targetId: outcome.continuation_turn_id || interaction.workflowId,
      publicMessage: '你的选择已接受。',
    };
  }

  async queryCommand(commandId: string): Promise<CommandReceipt | undefined> {
    const frame = await this.post<{
      query_command?: { found?: boolean; outcome?: ProtocolCommandOutcome };
      error?: ProtocolError;
    }>('query-command', { command_id: commandId });
    assertProtocolFrame(frame);
    if (!frame.query_command?.found || !frame.query_command.outcome) return undefined;
    return projectCommand(frame.query_command.outcome);
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    try {
      await apiRequest(`/api/connections/${encodeURIComponent(this.connectionId)}`, {
        method: 'DELETE',
        keepalive: true,
      });
    } catch {
      // Physical transport loss also releases the gateway attachment.
    }
  }

  private async command(
    kind: RuntimeCommandKind,
    fields: Record<string, unknown> = {},
  ): Promise<CommandReceipt> {
    const commandId = `command:web:${crypto.randomUUID()}`;
    const frame = await this.post<{
      command_outcome?: ProtocolCommandOutcome;
      error?: ProtocolError;
    }>('command', {
      command_id: commandId,
      client_submission_id: commandId,
      command_kind: kind,
      ...fields,
    });
    assertProtocolFrame(frame);
    if (!frame.command_outcome) {
      throw new RuntimeApiError('PROTOCOL_RESPONSE_INVALID', '本地服务没有返回操作结果。', true);
    }
    return projectCommand(frame.command_outcome);
  }

  private async post<T>(
    operation: string,
    body: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<T> {
    if (this.closed) {
      throw new RuntimeApiError('CONNECTION_CLOSED', '本地连接已经关闭。', true);
    }
    return apiRequest<T>(
      `/api/connections/${encodeURIComponent(this.connectionId)}/${operation}`,
      { method: 'POST', body: JSON.stringify(body), signal },
    );
  }

  private replaceSnapshot(snapshot: ProtocolCanonicalSnapshot) {
    this.entries = new Map((snapshot.entries ?? []).map((entry) => [entry.entry_id, entry]));
    this.olderHistoryCursor = snapshot.older_history_cursor;
    this.control = snapshot.control ?? {};
    this.eventSequence = numeric(snapshot.event_sequence_cut);
    this.writerGeneration = numeric(snapshot.writer_generation);
  }

  private async backfillOlderHistory(): Promise<void> {
    let cursor = this.olderHistoryCursor;
    while (cursor) {
      const frame = await this.post<{
        history_page?: ProtocolHistoryPage;
        error?: ProtocolError;
      }>('history', {
        cursor,
        maximum_entries: 256,
        maximum_serialized_bytes: 2 << 20,
      });
      assertProtocolFrame(frame);
      const page = frame.history_page;
      if (!page) {
        throw new RuntimeApiError('HISTORY_RESPONSE_INVALID', '较早的会话内容暂时无法加载。', true);
      }
      for (const entry of page.entries ?? []) this.entries.set(entry.entry_id, entry);
      if (!page.has_more) {
        this.olderHistoryCursor = undefined;
        return;
      }

      const next = page.older_history_cursor;
      const currentSequence = numeric(cursor.entry_sequence);
      const nextSequence = numeric(next?.entry_sequence);
      if (
        !next
        || next.session_id !== cursor.session_id
        || numeric(next.cut_sequence) !== numeric(cursor.cut_sequence)
        || nextSequence >= currentSequence
      ) {
        throw new RuntimeApiError('HISTORY_CURSOR_INVALID', '较早的会话内容无法继续加载。', true);
      }
      cursor = next;
      this.olderHistoryCursor = next;
    }
  }

  private async hydrateReasoningContent(): Promise<void> {
    for (const entry of this.entries.values()) {
      for (const block of entry.reasoning_blocks ?? []) {
        const reference = block.content;
        if (!reference || reference.inline_content || numeric(reference.size) === 0) continue;
        const expectedSize = numeric(reference.size);
        const expectedDigest = reference.digest ?? '';
        const chunks: Uint8Array[] = [];
        let offset = 0;
        while (true) {
          const frame = await this.post<{
            content?: {
              digest?: string;
              complete_size?: string | number;
              offset_bytes?: string | number;
              content?: string;
              complete?: boolean;
            };
            error?: ProtocolError;
          }>('read-content', {
            entry_id: entry.entry_id,
            block_id: block.block_id,
            offset_bytes: offset,
            limit_bytes: 1 << 20,
          });
          assertProtocolFrame(frame);
          const chunk = frame.content;
          if (
            !chunk
            || chunk.digest !== expectedDigest
            || numeric(chunk.complete_size) !== expectedSize
            || numeric(chunk.offset_bytes) !== offset
          ) {
            throw new RuntimeApiError(
              'CONTENT_REFERENCE_INVALID',
              '这段思考内容暂时无法完整读取。',
              true,
            );
          }
          const bytes = decodeBase64Bytes(chunk.content ?? '');
          chunks.push(bytes);
          offset += bytes.length;
          if (chunk.complete) break;
          if (bytes.length === 0 || offset >= expectedSize) {
            throw new RuntimeApiError(
              'CONTENT_REFERENCE_INVALID',
              '这段思考内容暂时无法完整读取。',
              true,
            );
          }
        }
        if (offset !== expectedSize) {
          throw new RuntimeApiError(
            'CONTENT_REFERENCE_INVALID',
            '这段思考内容暂时无法完整读取。',
            true,
          );
        }
        const complete = new Uint8Array(expectedSize);
        let cursor = 0;
        for (const chunk of chunks) {
          complete.set(chunk, cursor);
          cursor += chunk.length;
        }
        reference.inline_content = encodeBase64Bytes(complete);
      }
    }
  }

  private applyLive(events: ProtocolLiveEvent[], settlements: ProtocolSettlement[]) {
    for (const event of events) {
      const payload = event.payload ?? {};
      if (event.event_type === 'TODO_SNAPSHOT_UPDATED') {
        this.applyTodoSnapshot(event, payload.todo_snapshot_updated ?? {});
        continue;
      }
      if (event.event_type === 'SUBAGENT_PROGRESS') {
        const progress = payload.subagent_progress ?? {};
        const taskId = String(progress.task_id ?? event.scope_subagent_task_id ?? '');
        if (taskId) {
          this.taskProgress.set(taskId, {
            status: String(progress.status ?? ''),
            summary: productVisibleText(String(progress.public_summary ?? '')),
          });
        }
        continue;
      }
      const identity = event.draft_identity || event.generation_id || event.turn_id || `revision:${event.live_revision}`;
      const current = this.drafts.get(identity) ?? {
        id: identity,
        turnId: event.turn_id ?? '',
        scopeKind: event.scope_kind ?? '',
        taskId: event.scope_subagent_task_id ?? '',
        body: '',
        reasoning: [],
        traces: [],
      };
      if (event.scope_kind) current.scopeKind = event.scope_kind;
      if (event.scope_subagent_task_id) current.taskId = event.scope_subagent_task_id;
      if (event.event_type === 'TEXT_DELTA') current.body += stringField(payload.text_delta, 'delta');
      if (event.event_type === 'TEXT_END') current.body = stringField(payload.text_end, 'final_text') || current.body;
      if (event.event_type === 'THINKING_START') {
        const item = payload.thinking_start ?? {};
        const blockId = String(item.block_identity ?? event.block_id ?? `reasoning:${current.reasoning.length}`);
        if (!current.reasoning.some((block) => block.id === blockId)) {
          current.reasoning.push({
            id: blockId,
            kind: reasoningKind(String(item.presentation_kind ?? '')),
            body: '',
            active: true,
          });
        }
      }
      if (event.event_type === 'THINKING_DELTA') {
        const item = payload.thinking_delta ?? {};
        const blockId = String(item.block_identity ?? event.block_id ?? '');
        let block = current.reasoning.find((candidate) => candidate.id === blockId);
        if (!block) {
          block = { id: blockId || `reasoning:${current.reasoning.length}`, kind: 'full', body: '', active: true };
          current.reasoning.push(block);
        }
        block.active = true;
        block.body += String(item.delta ?? '');
      }
      if (event.event_type === 'THINKING_END') {
        const item = payload.thinking_end ?? {};
        const blockId = String(item.block_identity ?? event.block_id ?? '');
        let block = current.reasoning.find((candidate) => candidate.id === blockId);
        if (!block) {
          block = { id: blockId || `reasoning:${current.reasoning.length}`, kind: 'full', body: '' };
          current.reasoning.push(block);
        }
        block.body = String(item.final_text ?? '') || block.body;
        block.active = false;
      }
      if (event.event_type === 'TOOL_CALL_START') {
        const item = payload.tool_call_start ?? {};
        const toolName = String(item.tool_name ?? 'tool');
        current.traces.push({
          id: String(item.tool_call_id ?? event.block_id ?? crypto.randomUUID()),
          kind: toolKind(toolName),
          toolName,
          title: toolDisplayName(toolName),
          subtitle: '等待参数与执行结果',
          status: 'running',
        });
      }
      if (event.event_type === 'TOOL_CALL_END') {
        const item = payload.tool_call_end ?? {};
        const toolCallId = String(item.tool_call_id ?? '');
        const trace = current.traces.find((candidate) => candidate.id === toolCallId)
          ?? current.traces.at(-1);
        if (trace) {
          const toolName = String(item.tool_name ?? trace.toolName ?? 'tool');
          const argumentsJson = String(item.arguments_json ?? '');
          trace.toolName = toolName;
          trace.kind = toolKind(toolName);
          trace.title = toolDisplayName(toolName);
          trace.subtitle = toolArgumentSummary(toolName, argumentsJson);
          trace.argumentsJson = argumentsJson;
          trace.command = terminalCommand(toolName, argumentsJson);
        }
      }
      if (event.event_type === 'TOOL_RESULT_DELTA') {
        const text = stringField(payload.tool_result_delta, 'text');
        const trace = current.traces.at(-1);
        if (trace && text) trace.output = [...(trace.output ?? []), text];
      }
      if (event.event_type === 'TOOL_RESULT_END') {
        const item = payload.tool_result_end ?? {};
        const trace = current.traces.at(-1);
        if (trace) {
          const resultState = String(item.result_state ?? '').toUpperCase();
          trace.status = resultState === 'SUCCESS'
            ? 'completed'
            : resultState === 'CANCELLED' || resultState === 'CANCELLED_BEFORE_DISPATCH'
              ? 'cancelled'
              : 'failed';
          trace.subtitle = trace.status === 'completed' ? '已完成' : toolFailureLabel(resultState);
          const finalText = String(item.final_text ?? '');
          if (finalText) {
            trace.resultText = finalText;
            trace.output = [formatToolResult(finalText)];
          }
        }
      }
      if (
        event.event_type.startsWith('TEXT_')
        || event.event_type.startsWith('THINKING_')
        || event.event_type.startsWith('TOOL_')
      ) {
        this.drafts.set(identity, current);
      }
    }
    for (const settlement of settlements) {
      const identity = settlement.draft_identity || settlement.generation_id;
      if (identity) this.drafts.delete(identity);
    }
  }

  private applyTodoSnapshot(
    event: ProtocolLiveEvent,
    payload: Record<string, unknown>,
  ) {
    const todoRunId = String(payload.todo_run_id ?? '');
    if (!todoRunId) return;
    const current = [...(this.liveControl.current_todos ?? [])];
    const disposition = String(payload.disposition ?? '');
    if (disposition === 'CLOSED') {
      this.liveControl.current_todos = current.filter((item) => item.todo_run_id !== todoRunId);
      return;
    }

    const snapshot: ProtocolTodoRunSnapshot = {
      todo_run_id: todoRunId,
      todo_revision: payload.todo_revision as string | number | undefined,
      scope_kind: event.scope_kind,
      scope_subagent_task_id: event.scope_subagent_task_id,
      disposition,
      ordered_items: Array.isArray(payload.ordered_items)
        ? payload.ordered_items.map((item) => ({
          ordinal: numeric((item as Record<string, unknown>).ordinal),
          text: String((item as Record<string, unknown>).text ?? ''),
          status: String((item as Record<string, unknown>).status ?? ''),
        }))
        : [],
      pending_count: payload.pending_count as string | number | undefined,
      in_progress_count: payload.in_progress_count as string | number | undefined,
      completed_count: payload.completed_count as string | number | undefined,
    };
    const sameScope = current.findIndex((item) => (
      item.scope_kind === snapshot.scope_kind
      && (item.scope_subagent_task_id ?? '') === (snapshot.scope_subagent_task_id ?? '')
    ));
    if (sameScope >= 0) current[sameScope] = snapshot;
    else current.push(snapshot);
    current.sort((left, right) => {
      if (left.scope_kind === 'ROOT') return right.scope_kind === 'ROOT' ? 0 : -1;
      if (right.scope_kind === 'ROOT') return 1;
      return (left.scope_subagent_task_id ?? '').localeCompare(right.scope_subagent_task_id ?? '');
    });
    this.liveControl.current_todos = current;
  }

  private project(): RuntimeProjection {
    const canonical = [...this.entries.values()].sort(
      (a, b) => numeric(a.entry_sequence) - numeric(b.entry_sequence),
    );
    const agentTasks = projectAgentTasks(
      this.control.subagent_tasks ?? [],
      this.taskProgress,
    );
    const activeTaskIds = new Set(
      agentTasks
        .filter((task) => !isTerminalTaskStatus(task.status))
        .map((task) => task.id),
    );
    const activeTurnIds = new Set(
      (this.control.active_turns ?? []).map((turn) => turn.turn_id),
    );
    const visibleDrafts = [...this.drafts.values()].filter((draft) => {
      if (draft.scopeKind === 'SUBAGENT_TASK' || draft.taskId) {
        return Boolean(draft.taskId) && activeTaskIds.has(draft.taskId);
      }
      return Boolean(draft.turnId) && activeTurnIds.has(draft.turnId);
    });
    const messages = projectEntries(
      canonical,
      this.control.tool_attempts ?? [],
      activeTurnIds,
    );
    for (const draft of visibleDrafts) {
      if (draft.scopeKind === 'SUBAGENT_TASK' || draft.taskId) continue;
      messages.push({
        id: `live:${draft.id}`,
        turnId: draft.turnId,
        role: 'assistant',
        assistantKind: 'live',
        time: '现在',
        body: draft.body,
        reasoning: draft.reasoning.filter((block) => block.body).length
          ? draft.reasoning.filter((block) => block.body)
          : undefined,
        status: 'running',
        traces: draft.traces.length ? draft.traces : undefined,
      });
    }
    attachSubagentRuns(
      messages,
      projectSubagentRuns(
        canonical,
        this.control.tool_attempts ?? [],
        agentTasks,
        visibleDrafts,
      ),
    );
    const hasActiveDraft = visibleDrafts.length > 0;
    const activeTurn = (this.control.active_turns ?? []).find(
      (turn) => turn.scope_kind !== 'SUBAGENT_TASK',
    );
    const todo = projectTodo(this.liveControl);
    const interaction = projectInteraction(this.liveControl, this.control);
    return {
      messages: messages.map(productVisibleMessage),
      contextCompaction: projectContextCompaction(this.control),
      isRunning: Boolean(activeTurn) || hasActiveDraft,
      queuedCount: numeric(this.control.prompt_queue_total_count),
      planMode: Boolean(
        this.control.active_plan_workflow
        && Object.keys(this.control.active_plan_workflow).length,
      ),
      activeTurnId: activeTurn?.turn_id,
      control: this.control,
      liveControl: this.liveControl,
      agentTasks,
      todo,
      eventSequence: this.eventSequence,
      liveOwnerEpoch: this.liveOwnerEpoch,
      liveRevision: this.liveRevision,
      liveControlOwnerEpoch: this.liveControlOwnerEpoch,
      liveControlRevision: this.liveControlRevision,
      interaction,
    };
  }
}

function assertProtocolFrame(frame: { error?: ProtocolError }) {
  if (frame.error) {
    const retryable = new Set([
      'DEADLINE_EXCEEDED',
      'SERVER_OPERATION_FAILED',
      'STALE_ATTACHMENT',
    ]).has(frame.error.stable_code);
    throw new RuntimeApiError(
      frame.error.stable_code,
      frame.error.public_message ?? '本地服务拒绝了这次请求。',
      retryable,
    );
  }
}

async function apiRequest<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      headers: init.body
        ? { 'Content-Type': 'application/json', ...init.headers }
        : init.headers,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new RuntimeApiError(
      'LOCAL_TRANSPORT_UNAVAILABLE',
      '无法连接本地 Pulsara 服务。',
      true,
    );
  }
  const payload = await response.json().catch(() => ({})) as {
    error?: { code?: string; message?: string; retryable?: boolean };
  };
  if (!response.ok) {
    throw new RuntimeApiError(
      payload.error?.code ?? `HTTP_${response.status}`,
      payload.error?.message ?? '本地 Pulsara 请求失败。',
      Boolean(payload.error?.retryable),
    );
  }
  return payload as T;
}

const mcpStatusByProtocol: Record<string, McpServerStatus> = {
  DISABLED: 'disabled',
  CONFIGURED: 'configured',
  CONNECTING: 'connecting',
  DISCOVERING: 'discovering',
  READY: 'ready',
  FAILED_RETRYABLE: 'failed-retryable',
  FAILED_TERMINAL: 'failed',
  RETIRING: 'updating',
  CLOSED: 'closed',
};

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function recordArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.map(asRecord) : [];
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function projectCapabilitySnapshot(value: Record<string, unknown>): CapabilitySnapshot {
  const skills = asRecord(value.skills);
  const mcp = asRecord(value.mcp);
  const adoption = asRecord(value.adoption);
  return {
    sessionId: String(value.session_id ?? ''),
    workspacePath: String(value.workspace_path ?? ''),
    workspaceKind: value.workspace_kind === 'quick' ? 'quick' : 'project',
    adoption: {
      scope: 'workspace',
      pending: Boolean(adoption.pending),
      attention: adoption.attention === 'PROJECT_CAPABILITY_ADOPTION_FAILED'
        || adoption.attention === 'PROJECT_MCP_ADOPTION_INCOMPLETE'
        ? adoption.attention
        : undefined,
      when: 'next-user-turn',
    },
    skills: {
      status: skills.status === 'attention' ? 'attention' : 'ready',
      configPath: String(skills.config_path ?? ''),
      items: recordArray(skills.items).map((item) => ({
        id: String(item.id ?? ''),
        name: String(item.name ?? ''),
        description: String(item.description ?? ''),
        location: String(item.location ?? ''),
        path: String(item.path ?? ''),
        source: (
          item.source === 'workspace'
          || item.source === 'user'
          || item.source === 'plugin'
          || item.source === 'bundled'
            ? item.source
            : 'bundled'
        ),
        editable: Boolean(item.editable),
        enabled: item.enabled !== false,
        effective: item.effective !== false,
        configured: Boolean(item.configured),
        authoringNotes: stringArray(item.authoring_notes),
      })),
      issues: recordArray(skills.issues).map((item) => ({
        kind: item.kind === 'shadowed'
          ? 'shadowed'
          : item.kind === 'conflict'
            ? 'conflict'
            : 'invalid',
        title: String(item.title ?? '技能需要留意'),
        path: typeof item.path === 'string' ? item.path : undefined,
        details: stringArray(item.details),
      })),
      details: stringArray(skills.details),
      roots: recordArray(skills.roots).map((item) => ({
        path: String(item.path ?? ''),
        scope: item.scope === 'workspace' ? 'workspace' : 'user',
      })),
    },
    mcp: {
      configPath: String(mcp.config_path ?? ''),
      servers: recordArray(mcp.servers).map((server) => {
        const rawTransport = asRecord(server.transport);
        const transport = Object.keys(rawTransport).length ? {
          kind: rawTransport.kind === 'stdio' ? 'stdio' as const : 'http' as const,
          summary: String(rawTransport.summary ?? ''),
          detail: String(rawTransport.detail ?? rawTransport.summary ?? ''),
        } : undefined;
        return {
          id: String(server.id ?? ''),
          name: String(server.name ?? server.id ?? 'MCP 服务'),
          source: server.source === 'workspace'
            ? 'workspace' as const
            : server.source === 'user'
              ? 'user' as const
              : server.source === 'plugin'
                ? 'plugin' as const
                : 'host' as const,
          editable: Boolean(server.editable),
          configIdentity: typeof server.config_identity === 'string' && server.config_identity
            ? server.config_identity
            : undefined,
          enabled: Boolean(server.enabled),
          configuredEnabled: Boolean(server.configured_enabled),
          needsApproval: Boolean(server.needs_approval),
          effective: Boolean(server.effective),
          status: mcpStatusByProtocol[String(server.status ?? '')] ?? 'failed',
          required: Boolean(server.required),
          availableToSubagents: Boolean(server.available_to_subagents),
          toolCount: numeric(server.tool_count),
          discoveredToolCount: numeric(server.discovered_tool_count),
          resourceCount: numeric(server.resource_count),
          resourceTemplateCount: numeric(server.resource_template_count),
          promptCount: numeric(server.prompt_count),
          instructions: String(server.instructions ?? ''),
          hasFailure: Boolean(server.has_failure),
          failureCategory: typeof server.failure_category === 'string'
            ? server.failure_category
            : undefined,
          transport,
          tools: recordArray(server.tools).map((tool) => ({
            name: String(tool.name ?? ''),
            remoteName: String(tool.remote_name ?? ''),
            description: String(tool.description ?? ''),
            effect: tool.effect === 'READ_ONLY' ? 'read-only' as const : 'external-effect' as const,
            availableToSubagents: Boolean(tool.available_to_subagents),
            parallelSafe: Boolean(tool.parallel_safe),
          })),
        };
      }),
      collisions: recordArray(mcp.collisions).map((collision) => ({
        name: String(collision.name ?? ''),
        members: recordArray(collision.members).map((member) => ({
          serverId: String(member.server_id ?? ''),
          toolName: String(member.tool_name ?? ''),
        })),
      })),
    },
  };
}

function projectCapabilityAdoption(value: Record<string, unknown>): ProjectCapabilityAdoption {
  return {
    scope: 'workspace',
    pendingSessions: numeric(value.pending_sessions),
    when: 'next-user-turn',
  };
}

function projectCapabilityMutation(value: Record<string, unknown>): ProjectCapabilityMutationResult {
  const operation = asRecord(value.operation);
  return {
    operation: {
      status: String(operation.status ?? ''),
      success: Boolean(operation.success),
      message: String(operation.message ?? '项目能力已经更新。'),
      details: stringArray(operation.details),
    },
    adoption: projectCapabilityAdoption(asRecord(value.adoption)),
    capabilities: projectCapabilitySnapshot(asRecord(value.capabilities)),
  };
}

function projectUserCapabilitySnapshot(value: Record<string, unknown>): UserCapabilitySnapshot {
  const skills = asRecord(value.skills);
  const mcp = asRecord(value.mcp);
  const plugins = asRecord(value.plugins);
  const adoption = asRecord(value.adoption);
  const roots = recordArray(value.roots).map((item) => ({
    kind: item.kind === 'agents' ? 'agents' as const : 'pulsara' as const,
    path: String(item.path ?? ''),
  }));
  return {
    roots,
    skills: {
      status: skills.status === 'attention' ? 'attention' : 'ready',
      configPath: String(skills.config_path ?? ''),
      items: recordArray(skills.items).map((item) => ({
        name: String(item.name ?? ''),
        description: String(item.description ?? ''),
        location: String(item.location ?? ''),
        path: String(item.path ?? ''),
        enabled: item.enabled !== false,
        root: item.root === 'agents' ? 'agents' : 'pulsara',
        authoringNotes: stringArray(item.authoring_notes),
      })),
      issues: recordArray(skills.issues).map((item) => ({
        kind: item.kind === 'shadowed'
          ? 'shadowed'
          : item.kind === 'conflict'
            ? 'conflict'
            : 'invalid',
        title: String(item.title ?? '技能需要留意'),
        path: typeof item.path === 'string' ? item.path : undefined,
        details: stringArray(item.details),
      })),
      details: stringArray(skills.details),
      roots: recordArray(skills.roots).map((item) => ({
        kind: item.kind === 'agents' ? 'agents' : 'pulsara',
        path: String(item.path ?? ''),
      })),
    },
    mcp: {
      configPath: String(mcp.config_path ?? ''),
      servers: recordArray(mcp.servers).map((server) => {
        const transport = asRecord(server.transport);
        return {
          id: String(server.id ?? ''),
          name: String(server.name ?? server.id ?? 'MCP 服务'),
          enabled: Boolean(server.enabled),
          status: mcpStatusByProtocol[String(server.status ?? '')] ?? 'failed',
          required: Boolean(server.required),
          availableToSubagents: Boolean(server.available_to_subagents),
          toolCount: numeric(server.tool_count),
          resourceCount: numeric(server.resource_count),
          resourceTemplateCount: numeric(server.resource_template_count),
          promptCount: numeric(server.prompt_count),
          instructions: String(server.instructions ?? ''),
          hasFailure: Boolean(server.has_failure),
          failureCategory: typeof server.failure_category === 'string'
            ? server.failure_category
            : undefined,
          transport: {
            kind: transport.kind === 'stdio' ? 'stdio' as const : 'http' as const,
            summary: String(transport.summary ?? ''),
          },
          tools: recordArray(server.tools).map((tool) => ({
            name: String(tool.name ?? ''),
            remoteName: String(tool.remote_name ?? ''),
            description: String(tool.description ?? ''),
            effect: tool.effect === 'READ_ONLY' ? 'read-only' as const : 'external-effect' as const,
            availableToSubagents: Boolean(tool.available_to_subagents),
            parallelSafe: Boolean(tool.parallel_safe),
          })),
        };
      }),
    },
    plugins: {
      status: plugins.status === 'attention' ? 'attention' : 'ready',
      items: recordArray(plugins.items).map((item) => ({
        id: String(item.id ?? ''),
        name: String(item.name ?? item.id ?? '插件'),
        description: String(item.description ?? ''),
        version: typeof item.version === 'string' ? item.version : undefined,
        author: typeof item.author === 'string' ? item.author : undefined,
        enabled: Boolean(item.enabled),
        packageInstallId: String(item.package_install_id ?? ''),
        packageRoot: String(item.package_root ?? ''),
        skillCount: numeric(item.skill_count),
        mcpCount: numeric(item.mcp_count),
        effectiveSkillNames: stringArray(item.effective_skill_names),
        effectiveMcpServerIds: stringArray(item.effective_mcp_server_ids),
        details: stringArray(item.details),
      })),
      details: stringArray(plugins.details),
    },
    adoption: Object.keys(adoption).length > 0 ? {
      updatedSessions: numeric(adoption.updated_sessions),
      attentionSessions: numeric(adoption.attention_sessions),
    } : undefined,
  };
}

function projectUserCapabilityOperation(value: Record<string, unknown>): {
  operation: CapabilityOperation;
  capabilities: UserCapabilitySnapshot;
} {
  const operation = asRecord(value.operation);
  return {
    operation: {
      status: String(operation.status ?? ''),
      success: Boolean(operation.success ?? operation.installed),
      message: String(operation.message ?? '能力已经更新。'),
      details: stringArray(operation.details),
      pluginId: typeof operation.plugin_id === 'string' ? operation.plugin_id : undefined,
    },
    capabilities: projectUserCapabilitySnapshot(asRecord(value.capabilities)),
  };
}

function projectSkillInstallResult(value: Record<string, unknown>): SkillInstallResult {
  return {
    status: String(value.status ?? ''),
    installed: Boolean(value.installed),
    message: String(value.message ?? '技能安装没有完成。'),
    sourcePath: String(value.source_path ?? ''),
    destinationPath: typeof value.destination_path === 'string'
      ? value.destination_path
      : undefined,
    details: stringArray(value.details),
  };
}

function projectSessionSummary(value: Record<string, unknown>): SessionSummary {
  const lifecycle = String(value.lifecycle ?? 'OPEN');
  const rawWorkspace = value.workspace as Record<string, unknown> | undefined;
  const rawTaskCounts = value.task_counts as Record<string, unknown> | undefined;
  const workspace = rawWorkspace
    ? {
      id: String(rawWorkspace.id ?? ''),
      name: String(rawWorkspace.name ?? '工作目录'),
      path: String(rawWorkspace.path ?? ''),
      kind: rawWorkspace.kind === 'quick' ? 'quick' as const : 'project' as const,
    }
    : undefined;
  return {
    id: String(value.id),
    title: String(value.title ?? 'Pulsara 会话'),
    subtitle: String(value.subtitle ?? lifecycle),
    status: Boolean(value.live) ? 'waiting' : 'completed',
    updatedAt: formatRelativeTime(String(value.updated_at ?? '')),
    live: Boolean(value.live),
    taskCounts: rawTaskCounts ? {
      total: numeric(rawTaskCounts.total),
      active: numeric(rawTaskCounts.active),
      waiting: numeric(rawTaskCounts.waiting),
      attention: numeric(rawTaskCounts.attention),
    } : undefined,
    workspace,
  };
}

function projectEntries(
  entries: ProtocolEntry[],
  attempts: ProtocolToolAttempt[],
  activeTurnIds: ReadonlySet<string>,
): Message[] {
  const messages: Message[] = [];
  const unresolvedTraces: ToolTrace[] = [];
  for (const entry of entries) {
    if (entry.scope_kind === 'SUBAGENT_TASK') continue;
    if (
      entry.entry_kind === 'INTER_AGENT_MESSAGE'
      && entry.source_subagent_task_id
    ) {
      messages.push({
        id: entry.entry_id,
        turnId: entry.turn_id,
        entrySequence: numeric(entry.entry_sequence),
        role: 'user',
        userKind: 'subagent-completion',
        time: formatTime(entry.accepted_at_utc),
        body: '',
        sourceSubagentTaskId: entry.source_subagent_task_id,
      });
      continue;
    }
    if (
      entry.entry_kind === 'USER_MESSAGE'
      || entry.entry_kind === 'USER_STEER'
      || entry.entry_kind === 'PLAN_CONTINUATION'
    ) {
      const userKind: NonNullable<Message['userKind']> = entry.entry_kind === 'USER_STEER'
        ? 'steer'
        : entry.entry_kind === 'PLAN_CONTINUATION'
          ? 'plan-continuation'
          : 'prompt';
      messages.push({
        id: entry.entry_id,
        turnId: entry.turn_id,
        entrySequence: numeric(entry.entry_sequence),
        role: 'user',
        userKind,
        time: formatTime(entry.accepted_at_utc),
        body: entry.entry_kind === 'PLAN_CONTINUATION'
          ? projectPlanContinuation(decodeContent(entry.content))
          : decodeContent(entry.content),
      });
      continue;
    }
    if (
      entry.entry_kind === 'ASSISTANT_MESSAGE'
      || entry.entry_kind === 'ASSISTANT_TOOL_REQUEST'
    ) {
      const text = (entry.blocks ?? [])
        .filter((block) => block.block_kind === 'TEXT')
        .map((block) => decodeContent(block.content))
        .filter(Boolean)
        .join('\n\n');
      const traces = (entry.blocks ?? [])
        .filter((block) => block.block_kind === 'TOOL_CALL')
        .map(projectToolBlock);
      const reasoning = projectReasoningBlocks(entry);
      unresolvedTraces.push(...traces);
      messages.push({
        id: entry.entry_id,
        turnId: entry.turn_id,
        entrySequence: numeric(entry.entry_sequence),
        role: 'assistant',
        assistantKind: entry.entry_kind === 'ASSISTANT_MESSAGE' ? 'terminal' : 'tool-request',
        time: formatTime(entry.accepted_at_utc),
        body: text || (entry.entry_kind === 'ASSISTANT_MESSAGE' ? decodeContent(entry.content) : ''),
        reasoning: reasoning.length ? reasoning : undefined,
        status: 'completed',
        traces: traces.length ? traces : undefined,
      });
      continue;
    }
    if (entry.entry_kind === 'TOOL_RESULT') {
      const attempt = attempts.find((item) => item.result_entry_id === entry.entry_id);
      const target = attempt
        ? messages.find((message) => message.id === attempt.assistant_entry_id)
        : undefined;
      const trace = target?.traces?.find((item) => item.id === attempt?.tool_call_id);
      const pendingTrace = trace ?? unresolvedTraces.shift();
      if (pendingTrace) {
        const pendingIndex = unresolvedTraces.indexOf(pendingTrace);
        if (pendingIndex >= 0) unresolvedTraces.splice(pendingIndex, 1);
        const result = decodeContent(entry.content);
        const resultState = attempt?.result_state || inferToolResultState(result);
        const succeeded = resultState === 'SUCCESS';
        const cancelled = resultState === 'CANCELLED'
          || resultState === 'CANCELLED_BEFORE_DISPATCH';
        pendingTrace.status = succeeded ? 'completed' : cancelled ? 'cancelled' : 'failed';
        pendingTrace.subtitle = succeeded ? '已完成' : toolFailureLabel(resultState);
        if (result) {
          pendingTrace.resultText = result;
          pendingTrace.output = [formatToolResult(result)];
        }
        pendingTrace.meta = succeeded ? '操作完成' : cancelled ? '操作已取消' : '操作未完成';
        continue;
      }
      const fallbackTarget = [...messages].reverse().find((message) => message.role === 'assistant');
      const fallbackTrace: ToolTrace = {
        id: entry.entry_id,
        kind: 'artifact',
        title: '操作结果',
        subtitle: '已记录',
        status: 'completed',
        output: [formatToolResult(decodeContent(entry.content))].filter(Boolean),
      };
      if (fallbackTarget) fallbackTarget.traces = [...(fallbackTarget.traces ?? []), fallbackTrace];
      else messages.push({
        id: entry.entry_id,
        turnId: entry.turn_id,
        entrySequence: numeric(entry.entry_sequence),
        role: 'assistant',
        assistantKind: 'tool-request',
        time: formatTime(entry.accepted_at_utc),
        body: '',
        traces: [fallbackTrace],
        status: 'completed',
      });
      continue;
    }
    if (entry.entry_kind === 'TERMINAL_OBSERVATION') {
      const target = [...messages].reverse().find((message) => message.role === 'assistant');
      const trace: ToolTrace = {
        id: entry.entry_id,
        kind: 'terminal',
        title: '命令进展',
        subtitle: '已记录',
        status: 'completed',
        output: [decodeContent(entry.content)].filter(Boolean),
      };
      if (target) target.traces = [...(target.traces ?? []), trace];
      else {
        messages.push({
          id: entry.entry_id,
          turnId: entry.turn_id,
          entrySequence: numeric(entry.entry_sequence),
          role: 'assistant',
          assistantKind: 'tool-request',
          time: formatTime(entry.accepted_at_utc),
          body: '',
          traces: [trace],
          status: 'completed',
        });
      }
    }
  }
  for (const message of messages) {
    if (message.turnId && activeTurnIds.has(message.turnId)) continue;
    for (const trace of message.traces ?? []) {
      settleHistoricalTrace(trace);
    }
  }
  return messages;
}

function projectSubagentRuns(
  entries: ProtocolEntry[],
  attempts: ProtocolToolAttempt[],
  tasks: AgentTask[],
  drafts: LiveDraft[],
): SubagentRun[] {
  const taskById = new Map(tasks.map((task) => [task.id, task]));
  const runs = new Map<string, SubagentRun>();
  const order = new Map<string, number>();
  const unresolvedByTask = new Map<string, ToolTrace[]>();
  let fallbackIndex = 0;

  const ensureRun = (taskId: string, sequence = Number.MAX_SAFE_INTEGER): SubagentRun => {
    const existing = runs.get(taskId);
    if (existing) {
      order.set(taskId, Math.min(order.get(taskId) ?? sequence, sequence));
      return existing;
    }
    const task = taskById.get(taskId);
    const index = task ? Math.max(0, tasks.indexOf(task)) : fallbackIndex++;
    const run: SubagentRun = {
      id: taskId,
      label: task?.label || `子任务 ${index + 1}`,
      role: task?.role || '子任务',
      objective: task?.objective || '',
      // Active and queued tasks are always present in canonical control. A
      // transcript-only task has already left that control surface; until a
      // successful final reply below proves completion, present the neutral
      // terminal state instead of falsely leaving it running forever.
      status: task?.status || 'ended',
      parentId: task?.parentId,
      summary: task?.summary,
      color: task?.color ?? (['blue', 'amber', 'violet', 'green'] as const)[index % 4],
      activities: [],
    };
    runs.set(taskId, run);
    order.set(taskId, sequence);
    unresolvedByTask.set(taskId, []);
    return run;
  };

  // A task exists for the user as soon as the Host accepts it, even when it is
  // still waiting for a worker slot and therefore has no transcript or live
  // draft yet. Seed every canonical task before enriching active ones below.
  tasks.forEach((task, index) => ensureRun(task.id, index));

  for (const entry of entries) {
    if (entry.scope_kind !== 'SUBAGENT_TASK' || !entry.scope_subagent_task_id) continue;
    const taskId = entry.scope_subagent_task_id;
    const run = ensureRun(taskId, numeric(entry.entry_sequence));
    const content = decodeContent(entry.content);
    if (entry.entry_kind === 'USER_MESSAGE') {
      if (!run.objective) run.objective = content;
      continue;
    }
    if (entry.entry_kind === 'INTER_AGENT_MESSAGE') {
      run.activities.push({
        id: entry.entry_id,
        time: formatTime(entry.accepted_at_utc),
        body: content,
        kind: 'guidance',
        status: 'completed',
      });
      continue;
    }
    if (
      entry.entry_kind === 'ASSISTANT_MESSAGE'
      || entry.entry_kind === 'ASSISTANT_TOOL_REQUEST'
    ) {
      const body = (entry.blocks ?? [])
        .filter((block) => block.block_kind === 'TEXT')
        .map((block) => decodeContent(block.content))
        .filter(Boolean)
        .join('\n\n') || (entry.entry_kind === 'ASSISTANT_MESSAGE' ? content : '');
      const traces = (entry.blocks ?? [])
        .filter((block) => block.block_kind === 'TOOL_CALL')
        .map(projectToolBlock);
      const reasoning = projectReasoningBlocks(entry);
      unresolvedByTask.get(taskId)?.push(...traces);
      run.activities.push({
        id: entry.entry_id,
        time: formatTime(entry.accepted_at_utc),
        body,
        reasoning: reasoning.length ? reasoning : undefined,
        status: 'completed',
        traces: traces.length ? traces : undefined,
      });
      if (entry.entry_kind === 'ASSISTANT_MESSAGE' && body) {
        run.summary ??= body;
        if (!taskById.has(taskId)) run.status = 'completed';
      }
      continue;
    }
    if (entry.entry_kind === 'TOOL_RESULT') {
      const attempt = attempts.find((item) => item.result_entry_id === entry.entry_id);
      const target = attempt
        ? run.activities.find((activity) => activity.id === attempt.assistant_entry_id)
        : undefined;
      const trace = target?.traces?.find((item) => item.id === attempt?.tool_call_id);
      const unresolved = unresolvedByTask.get(taskId) ?? [];
      const pendingTrace = trace ?? unresolved.shift();
      if (pendingTrace) {
        const pendingIndex = unresolved.indexOf(pendingTrace);
        if (pendingIndex >= 0) unresolved.splice(pendingIndex, 1);
        const resultState = attempt?.result_state || inferToolResultState(content);
        const succeeded = resultState === 'SUCCESS';
        const cancelled = resultState === 'CANCELLED'
          || resultState === 'CANCELLED_BEFORE_DISPATCH';
        pendingTrace.status = succeeded ? 'completed' : cancelled ? 'cancelled' : 'failed';
        pendingTrace.subtitle = succeeded ? '已完成' : toolFailureLabel(resultState);
        if (content) {
          pendingTrace.resultText = content;
          pendingTrace.output = [formatToolResult(content)];
        }
        pendingTrace.meta = succeeded ? '操作完成' : cancelled ? '操作已取消' : '操作未完成';
      }
      continue;
    }
    if (entry.entry_kind === 'TERMINAL_OBSERVATION') {
      const target = run.activities.at(-1);
      const trace: ToolTrace = {
        id: entry.entry_id,
        kind: 'terminal',
        title: '命令进展',
        subtitle: '已记录',
        status: 'completed',
        output: [content].filter(Boolean),
      };
      if (target) target.traces = [...(target.traces ?? []), trace];
      else run.activities.push({
        id: entry.entry_id,
        time: formatTime(entry.accepted_at_utc),
        body: '',
        status: 'completed',
        traces: [trace],
      });
    }
  }

  for (const draft of drafts) {
    if (draft.scopeKind !== 'SUBAGENT_TASK' && !draft.taskId) continue;
    if (!draft.taskId) continue;
    const canonicalTask = taskById.get(draft.taskId);
    if (
      canonicalTask
      && isTerminalTaskStatus(canonicalTask.status)
    ) continue;
    const run = ensureRun(draft.taskId);
    run.status = 'running';
    run.activities.push({
      id: `live:${draft.id}`,
      time: '现在',
      body: draft.body || '正在处理…',
      reasoning: draft.reasoning.filter((block) => block.body).length
        ? draft.reasoning.filter((block) => block.body)
        : undefined,
      status: 'running',
      traces: draft.traces.length ? draft.traces : undefined,
    });
  }

  for (const [taskId, traces] of unresolvedByTask) {
    const run = runs.get(taskId);
    if (!run || ['pending', 'running', 'waiting'].includes(run.status)) continue;
    for (const trace of traces) settleHistoricalTrace(trace);
  }

  return [...runs.values()].sort(
    (left, right) => (order.get(left.id) ?? 0) - (order.get(right.id) ?? 0),
  );
}

function settleHistoricalTrace(trace: ToolTrace): void {
  if (trace.status !== 'running') return;
  trace.status = 'cancelled';
  trace.subtitle = '已结束';
  trace.meta = '操作未完成';
}

function createsSubagentTasks(trace: ToolTrace): boolean {
  const name = trace.toolName?.toLowerCase() ?? '';
  return name.includes('create_agent_tasks') || name.includes('spawn_agent');
}

function attachSubagentRuns(messages: Message[], runs: SubagentRun[]): void {
  if (!runs.length) return;
  const groups = new Map<string, SubagentRun[]>();
  for (const run of runs) {
    const key = run.parentId || '';
    groups.set(key, [...(groups.get(key) ?? []), run]);
  }
  for (const [parentId, group] of groups) {
    if (!parentId) continue;
    const creator = messages.find((message) => (
      message.role === 'assistant'
      && message.turnId === parentId
      && message.traces?.some(createsSubagentTasks)
    ));
    const target = creator ?? messages.find((message) => (
      message.role === 'assistant' && message.turnId === parentId
    ));
    if (!target) continue;
    target.subagentRuns = [...(target.subagentRuns ?? []), ...group];
  }
}

function inferToolResultState(content: string): string {
  const normalizedContent = content.trim().toLowerCase();
  if (
    normalizedContent.includes('permission denied')
    || normalizedContent.includes('tool execution denied by user')
    || normalizedContent.includes('requires bypass-permissions mode')
    || normalizedContent.includes('not permitted')
  ) return 'PERMISSION_DENIED';
  try {
    const parsed = JSON.parse(content) as Record<string, unknown>;
    const status = String(parsed.status ?? '').toLowerCase();
    if (['cancelled', 'canceled', 'killed', 'interrupted'].includes(status)) return 'CANCELLED';
    if (['denied', 'permission_denied', 'forbidden'].includes(status)) return 'PERMISSION_DENIED';
    if (['error', 'failed', 'failure', 'invalid'].includes(status)) return 'APPLICATION_ERROR';
    if (parsed.ok === false || parsed.success === false) return 'APPLICATION_ERROR';
    if (typeof parsed.error === 'string' && parsed.error.trim()) return 'APPLICATION_ERROR';
    if (typeof parsed.exit_code === 'number' && parsed.exit_code !== 0) return 'APPLICATION_ERROR';
    return 'SUCCESS';
  } catch {
    return normalizedContent ? 'SUCCESS' : 'APPLICATION_ERROR';
  }
}

function formatToolResult(content: string): string {
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    const planControl = String(value.plan_control ?? '');
    if (planControl === 'QUESTION_ANSWERED') return '已记录你的选择。';
    if (planControl === 'DRAFT_SUBMITTED_FOR_REVIEW') return '方案已提交，等待你的确认。';
    const path = typeof value.path === 'string' ? value.path : '';
    if (path && typeof value.bytes_written === 'number') {
      return `已写入 ${path} · ${value.bytes_written} 字节`;
    }
    if (path && typeof value.total_lines === 'number') {
      return `已读取 ${path} · ${value.total_lines} 行${value.truncated ? ' · 内容已截断' : ''}`;
    }
    const message = value.message ?? value.error;
    if (typeof message === 'string' && message) return productVisibleText(message);
    for (const key of ['output', 'stdout', 'stderr', 'content', 'result', 'text', 'summary']) {
      const field = value[key];
      if (typeof field === 'string' && field) return field;
    }
    if (typeof value.exit_code === 'number') return `命令已结束，退出码 ${value.exit_code}。`;
    if (String(value.status ?? '').toLowerCase() === 'success') return '操作已完成。';
    return '操作已完成。';
  } catch {
    return productVisibleText(content);
  }
}

function projectPlanContinuation(content: string): string {
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    const feedback = value.feedback;
    if (typeof feedback === 'string' && feedback.trim()) {
      return `请按此修改方案：${feedback.trim()}`;
    }
    const transition = String(value.transition ?? value.handoff_kind ?? '').toUpperCase();
    if (transition.includes('APPROV')) return '方案已批准，请按方案继续。';
    if (transition.includes('CANCEL')) return '已取消这次规划。';
    return '继续处理这次规划。';
  } catch {
    return content;
  }
}

function toolFailureLabel(state?: string): string {
  if (state === 'PERMISSION_DENIED') return '已拒绝';
  if (state === 'CANCELLED' || state === 'CANCELLED_BEFORE_DISPATCH') return '已取消';
  if (state === 'INVALID_ARGUMENTS') return '参数有误';
  if (state === 'TOOL_UNAVAILABLE') return '未能执行';
  return '执行失败';
}

function projectToolBlock(block: ProtocolAssistantBlock): ToolTrace {
  const name = block.tool_name || 'Tool';
  const argumentsPreview = decodeBase64(block.tool_arguments_preview ?? '');
  return {
    id: block.tool_call_id || block.block_id,
    kind: toolKind(name),
    toolName: name,
    title: toolDisplayName(name),
    subtitle: toolArgumentSummary(name, argumentsPreview),
    status: 'running',
    argumentsJson: argumentsPreview,
    command: terminalCommand(name, argumentsPreview),
  };
}

function projectReasoningBlocks(entry: ProtocolEntry): ReasoningBlock[] {
  return [...(entry.reasoning_blocks ?? [])]
    .sort((left, right) => numeric(left.ordinal) - numeric(right.ordinal))
    .map((block) => ({
      id: block.block_id,
      kind: reasoningKind(block.presentation_kind ?? ''),
      body: decodeContent(block.content),
    }))
    .filter((block) => Boolean(block.body));
}

function reasoningKind(value: string): ReasoningBlock['kind'] {
  return value.includes('SUMMARY') ? 'summary' : 'full';
}

function toolDisplayName(name: string): string {
  const normalized = name.toLowerCase();
  if (normalized === 'reload_capabilities') return '刷新能力';
  if (normalized === 'list_mcp_servers') return '浏览 MCP 服务';
  if (normalized === 'inspect_new_mcp_tool') return '检查 MCP 工具';
  if (normalized === 'use_new_mcp_tool') return '调用 MCP 工具';
  if (normalized.includes('report_agent_result')) return '提交子任务结果';
  if (normalized.includes('create_agent_tasks') || normalized.includes('spawn_agent')) return '创建子任务';
  if (normalized.includes('wait_agent')) return '等待子任务';
  if (normalized.includes('send_agent_message')) return '发送子任务消息';
  if (normalized.includes('stop_agent')) return '停止子任务';
  if (normalized.includes('list_agents')) return '查看子任务';
  if (normalized === 'todo') return '更新 TODO';
  if (normalized.includes('ask_plan_question')) return '提出规划问题';
  if (normalized.includes('exit_plan')) return '提交规划方案';
  if (normalized.includes('artifact_read')) return '读取保留内容';
  if (normalized.includes('write_file')) return '写入文件';
  if (normalized.includes('read_file')) return '读取文件';
  if (normalized.includes('apply_patch') || normalized.includes('edit')) return '更新文件';
  if (normalized.includes('search') || normalized.includes('grep')) return '搜索内容';
  if (normalized.includes('list')) return '浏览文件';
  if (normalized.includes('terminal_monitor')) return '等待命令';
  if (normalized.includes('terminal') || normalized.includes('shell') || normalized.includes('exec')) return '运行命令';
  if (normalized.includes('browser')) return '操作浏览器';
  return '使用工具';
}

function toolArgumentSummary(name: string, content: string): string {
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    const normalized = name.toLowerCase();
    if (normalized === 'reload_capabilities') return '重新载入 Skill、MCP 与 Hook';
    if (normalized === 'list_mcp_servers') {
      return typeof value.server_id === 'string' && value.server_id
        ? `查看 ${value.server_id} 的工具`
        : '读取当前 MCP 服务列表';
    }
    if (normalized === 'inspect_new_mcp_tool') {
      const server = typeof value.server_id === 'string' ? value.server_id : '';
      const tool = typeof value.tool_name === 'string' ? value.tool_name : '';
      return [server, tool].filter(Boolean).join(' · ') || '检查一个 MCP 工具';
    }
    if (normalized === 'use_new_mcp_tool') return '调用已经检查的 MCP 工具';
    if (normalized.includes('report_agent_result')) return '提交最终结果';
    if (normalized.includes('create_agent_tasks')) {
      return `创建 ${Array.isArray(value.tasks) ? value.tasks.length : 0} 个子任务`;
    }
    if (normalized.includes('spawn_agent')) return String(value.label ?? value.task ?? '创建一个子任务');
    if (normalized.includes('wait_agent')) return '等待指定子任务出现新进展';
    if (normalized.includes('send_agent_message')) return '向子任务发送补充信息';
    if (normalized.includes('stop_agent')) return '停止指定子任务';
    if (normalized.includes('list_agents')) return '读取当前子任务状态';
    if (normalized === 'todo') return '更新当前工作清单';
    if (name.toLowerCase().includes('ask_plan_question')) return String(value.question ?? '等待你的选择');
    if (name.toLowerCase().includes('exit_plan')) return String(value.summary ?? '等待你确认方案');
    if (terminalCommand(name, content)) return '等待执行结果';
    if (typeof value.path === 'string') return value.path;
    return '正在准备操作';
  } catch {
    // Keep a short provider-visible preview when it is not structured JSON.
  }
  return content.slice(0, 160) || '正在准备操作';
}

function terminalCommand(name: string, content: string): string | undefined {
  const normalized = name.toLowerCase();
  if (
    !normalized.includes('terminal')
    && !normalized.includes('shell')
    && !normalized.includes('exec')
  ) return undefined;
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    for (const key of ['command', 'cmd', 'script']) {
      const command = value[key];
      if (typeof command === 'string' && command.trim()) return command.trim();
    }
    if (Array.isArray(value.argv) && value.argv.every((item) => typeof item === 'string')) {
      const command = value.argv.join(' ').trim();
      if (command) return command;
    }
  } catch {
    // Only complete, structured terminal arguments can be rendered as a command.
  }
  return undefined;
}

function projectTodo(control: ProtocolLiveControlSnapshot): TodoRun | undefined {
  const run = control.current_todos?.find((item) => (
    item.scope_kind === 'ROOT' && item.disposition !== 'CLOSED'
  ));
  if (!run?.ordered_items?.length) return undefined;
  return {
    id: run.todo_run_id,
    items: run.ordered_items.map((item, index) => {
      const status = item.status.toUpperCase();
      return {
        id: `${run.todo_run_id}:${item.ordinal ?? index}`,
        label: productVisibleText(item.text),
        status: status === 'COMPLETED'
          ? 'completed'
          : status === 'IN_PROGRESS'
            ? 'in-progress'
            : 'pending',
      };
    }),
  };
}

function projectContextCompaction(
  control: ProtocolCanonicalControl,
): ContextCompactionBoundary | undefined {
  const value = control.latest_context_compaction;
  const contextBindingRevisionId = String(value?.context_binding_revision_id ?? '');
  const turnId = String(value?.turn_id ?? '');
  const acceptedAt = String(value?.accepted_at_utc ?? '');
  if (!contextBindingRevisionId || !turnId || !acceptedAt) return undefined;
  return {
    contextBindingRevisionId,
    turnId,
    sourceThroughSequence: numeric(value?.source_through_sequence),
    adoptedAfterEntrySequence: numeric(value?.adopted_after_entry_sequence),
    acceptedAt,
  };
}

function projectInteraction(
  liveControl: ProtocolLiveControlSnapshot,
  control: ProtocolCanonicalControl,
): RuntimeInteractionSummary | undefined {
  const live = liveControl.current_interaction;
  const liveId = String(live?.interaction_id ?? '');
  if (liveId) {
    return {
      id: liveId,
      kind: 'tool-confirmation',
      prompt: String(live?.public_prompt ?? ''),
      options: Array.isArray(live?.public_options)
        ? live.public_options.map(String)
        : [],
    };
  }

  const open = control.open_plan_interaction;
  const active = control.active_plan_workflow;
  const id = String(open?.interaction_id ?? '');
  const workflowId = String(open?.workflow_id ?? active?.workflow_id ?? '');
  const workflowRevision = numeric(active?.workflow_revision);
  const kind = String(open?.kind ?? '');
  if (!id || !workflowId || workflowRevision < 1) return undefined;
  if (kind === 'QUESTION') {
    return { id, kind: 'plan-question', workflowId, workflowRevision };
  }
  if (kind === 'DRAFT_REVIEW') {
    return { id, kind: 'plan-draft', workflowId, workflowRevision };
  }
  return undefined;
}

function projectAgentTasks(
  tasks: ProtocolSubagentTask[],
  progress: ReadonlyMap<string, LiveTaskProgress>,
): AgentTask[] {
  return tasks.map((task, index) => {
    const live = progress.get(task.task_id);
    const status = taskStatus(task.status);
    return {
      id: task.task_id,
      label: productVisibleText(task.label || `子任务 ${index + 1}`),
      role: productVisibleText(task.display_role || profileLabel(task.profile)),
      profile: task.profile,
      objective: productVisibleText(task.objective),
      status,
      parentId: task.parent_turn_id,
      batchId: task.batch_id,
      taskKey: task.task_key,
      context: projectTaskContext(task.context_mode, task.context_last_n_turns),
      pendingReason: task.pending_reason || undefined,
      terminalReason: task.terminal_reason || undefined,
      terminalPublicDetail: task.terminal_public_detail
        ? productVisibleText(task.terminal_public_detail)
        : undefined,
      completionDelivered: Boolean(task.completion_delivered),
      dependencyIds: task.dependency_task_ids ?? [],
      summary: task.result_summary ? productVisibleText(task.result_summary) : undefined,
      progress: !isTerminalTaskStatus(status) && live?.summary
        ? live.summary
        : undefined,
      result: task.result_id ? {
        id: task.result_id,
        summary: productVisibleText(task.result_summary ?? ''),
        diagnostics: [],
      } : undefined,
      color: taskColor(task.task_id),
    };
  });
}

function projectTaskInventoryRecord(task: ProtocolTaskInventoryRecord): AgentTask {
  const dependencies = (task.dependencies ?? []).map((dependency) => ({
    id: String(dependency.task_id ?? ''),
    taskKey: dependency.task_key || undefined,
    label: dependency.label ? productVisibleText(dependency.label) : undefined,
    status: taskStatus(String(dependency.status ?? '')),
    resultSummary: dependency.result_summary
      ? productVisibleText(dependency.result_summary)
      : undefined,
  }));
  const result = task.result?.id ? {
    id: task.result.id,
    entryId: task.result.entry_id || undefined,
    summary: productVisibleText(task.result.summary ?? ''),
    outputPreview: task.result.output_preview
      ? productVisibleText(task.result.output_preview)
      : undefined,
    diagnostics: Array.isArray(task.result.diagnostics)
      ? task.result.diagnostics
      : [],
  } : undefined;
  return {
    id: task.id,
    label: productVisibleText(task.label || task.task_key || '子任务'),
    role: productVisibleText(task.display_role || profileLabel(task.profile)),
    profile: task.profile,
    objective: productVisibleText(task.objective ?? ''),
    status: taskStatus(String(task.status ?? '')),
    parentId: task.parent_turn_id,
    batchId: task.batch_id,
    taskKey: task.task_key,
    context: projectTaskContext(task.context?.mode, task.context?.last_n_turns),
    pendingReason: task.pending_reason || undefined,
    terminalReason: task.terminal_reason || undefined,
    terminalPublicDetail: task.terminal_public_detail
      ? productVisibleText(task.terminal_public_detail)
      : undefined,
    completionDelivered: Boolean(task.completion_delivered),
    acceptedAt: task.accepted_at,
    terminalAt: task.terminal_at || undefined,
    dependencyIds: dependencies.map((dependency) => dependency.id),
    dependencies,
    summary: result?.summary || undefined,
    result,
    color: taskColor(task.id),
  };
}

function projectTaskContext(
  mode: string | undefined,
  turns: string | number | null | undefined,
): AgentTask['context'] {
  return String(mode ?? '').toUpperCase() === 'LAST_N'
    ? { mode: 'last-n', lastNTurns: numeric(turns) || undefined }
    : { mode: 'none' };
}

function profileLabel(profile?: string): string {
  const labels: Record<string, string> = {
    general_worker: '通用协作',
    research_worker: '研究',
    review_worker: '审阅',
    verification_worker: '验证',
    synthesizer: '整合',
  };
  return labels[profile ?? ''] ?? '子任务';
}

function taskColor(taskId: string): AgentTask['color'] {
  let hash = 0;
  for (const character of taskId) hash = ((hash * 31) + character.charCodeAt(0)) >>> 0;
  return (['blue', 'amber', 'violet', 'green'] as const)[hash % 4];
}

export function mergeRuntimeTaskInventory(
  projection: RuntimeProjection,
  inventory: AgentTask[],
): RuntimeProjection {
  const liveById = new Map(projection.agentTasks.map((task) => [task.id, task]));
  const merged = inventory.map((durable) => {
    const live = liveById.get(durable.id);
    liveById.delete(durable.id);
    if (!live) return durable;
    const durableTerminal = isTerminalTaskStatus(durable.status);
    return {
      ...live,
      ...durable,
      status: durableTerminal ? durable.status : live.status,
      progress: durableTerminal ? undefined : live.progress ?? durable.progress,
      summary: durable.summary ?? live.summary,
      dependencies: durable.dependencies?.length
        ? durable.dependencies
        : live.dependencies,
      dependencyIds: durable.dependencyIds.length
        ? durable.dependencyIds
        : live.dependencyIds,
      result: durable.result ?? live.result,
    };
  });
  merged.push(...liveById.values());

  const messages = projection.messages.map((message) => ({
    ...message,
    subagentRuns: message.subagentRuns?.map((run) => ({
      ...run,
      activities: [...run.activities],
    })),
  }));
  const runById = new Map<string, SubagentRun>();
  for (const message of messages) {
    for (const run of message.subagentRuns ?? []) runById.set(run.id, run);
  }
  const missing: SubagentRun[] = [];
  for (const task of merged) {
    const run = runById.get(task.id);
    if (run) {
      run.label = task.label;
      run.role = task.role;
      run.objective = task.objective;
      run.status = task.status;
      run.parentId = task.parentId;
      run.summary = task.summary;
      run.color = task.color;
      continue;
    }
    missing.push({
      id: task.id,
      label: task.label,
      role: task.role,
      objective: task.objective,
      status: task.status,
      parentId: task.parentId,
      summary: task.summary,
      color: task.color,
      activities: [],
    });
  }
  attachSubagentRuns(messages, missing);
  return { ...projection, messages, agentTasks: merged };
}

function projectCommand(value: ProtocolCommandOutcome): CommandReceipt {
  const status = value.status === 'SUCCEEDED'
    ? 'succeeded'
    : value.status === 'PENDING'
      ? 'pending'
      : 'rejected';
  return {
    commandId: value.command_id,
    status,
    targetId: value.target_id || undefined,
    publicCode: value.public_code || undefined,
    publicMessage: value.public_message || undefined,
  };
}

function decodeContent(content?: ProtocolContent): string {
  if (!content?.inline_content) return '';
  return decodeBase64(content.inline_content);
}

function decodeBase64(value: string): string {
  if (!value) return '';
  try {
    return new TextDecoder().decode(decodeBase64Bytes(value));
  } catch {
    return '';
  }
}

function decodeBase64Bytes(value: string): Uint8Array {
  const decoded = atob(value);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

function encodeBase64Bytes(value: Uint8Array): string {
  const pieces: string[] = [];
  for (let offset = 0; offset < value.length; offset += 0x8000) {
    pieces.push(String.fromCharCode(...value.subarray(offset, offset + 0x8000)));
  }
  return btoa(pieces.join(''));
}

const PRODUCT_TEXT_REPLACEMENTS: ReadonlyArray<readonly [RegExp, string]> = [
  [/ROOT subagent orchestration requires bypass-permissions mode/gi, '创建子任务需要在本轮选择“完全访问”权限'],
  [/\bPERMISSION_MODE_BYPASS_PERMISSIONS\b/g, '完全访问'],
  [/\bbypass-permissions\b/gi, '完全访问'],
  [/\bPERMISSION_MODE_ACCEPT_EDITS\b/g, '接受编辑'],
  [/\baccept-edits\b/gi, '接受编辑'],
  [/\bPERMISSION_MODE_READ_ONLY\b/g, '只读'],
  [/\bread-only\b/gi, '只读'],
  [/\bPERMISSION_MODE_ASK_PERMISSIONS\b/g, '每次询问'],
  [/\bask-permissions\b/gi, '每次询问'],
  [/\bcreate_agent_tasks\b|\bspawn_agent\b/g, '创建子任务'],
  [/\bwait_agent\b/g, '等待子任务'],
  [/\breport_agent_result\b/g, '提交子任务结果'],
  [/\bsend_agent_message\b/g, '发送子任务消息'],
  [/\bstop_agent\b/g, '停止子任务'],
  [/\blist_agents\b/g, '查看子任务'],
  [/\bSUBAGENT_TASK\b/g, '子任务'],
  [/使用\s+read_file\s+工具读取文件/gi, '读取'],
  [/\bread_file\b/g, '读取文件'],
  [/\bwrite_file\b/g, '写入文件'],
  [/\bedit_file\b/g, '更新文件'],
  [/使用\s+terminal\s+tool\b/gi, '使用终端'],
  [/使用\s+terminal\s+工具/gi, '使用终端'],
  [/\bterminal\s+tool\b/gi, '终端'],
  [/\bterminal\s+工具/gi, '终端'],
  [/处于\s+active\s+状态/gi, '正在运行'],
  [/处于\s+active(?=[，,。\s])/gi, '正在运行'],
  [/\bwas rejected\b/gi, '已被拒绝'],
  [/\bpermission denied\b/gi, '权限已拒绝'],
  [/\bexit_code\s*=\s*0\b/gi, '退出码为 0'],
  [/\bexit\s+code\s*0\b/gi, '退出码为 0'],
  [/\byielded_to_background\s*=\s*false\b/gi, '保持前台运行'],
  [/\btimed_out\s*=\s*false\b/gi, '未超时'],
  [/\bsource_coverage\s*=\s*COMPLETE\b/g, '输出完整'],
  [/\bTerminal Protocol(?: v?\d+)?\b/gi, '本地服务'],
  [/\bHostSession\b/g, '本地会话'],
  [/\bKernel\b/g, '本地服务'],
  [/\bROOT\b/g, '主任务'],
];

export function productVisibleText(value: string): string {
  return PRODUCT_TEXT_REPLACEMENTS.reduce(
    (current, [pattern, replacement]) => current.replace(pattern, replacement),
    value,
  );
}

function productVisibleMessage(message: Message): Message {
  if (message.role === 'user') return message;
  return {
    ...message,
    body: productVisibleText(message.body),
    reasoning: message.reasoning?.map((block) => ({
      ...block,
      body: productVisibleText(block.body),
    })),
    subagentRuns: message.subagentRuns?.map((run) => ({
      ...run,
      label: productVisibleText(run.label),
      role: productVisibleText(run.role),
      objective: productVisibleText(run.objective),
      summary: run.summary ? productVisibleText(run.summary) : undefined,
      activities: run.activities.map((activity) => ({
        ...activity,
        body: productVisibleText(activity.body),
        reasoning: activity.reasoning?.map((block) => ({
          ...block,
          body: productVisibleText(block.body),
        })),
      })),
    })),
  };
}

function stringField(
  value: Record<string, unknown> | undefined,
  field: string,
): string {
  return value && typeof value[field] === 'string' ? value[field] as string : '';
}

function numeric(value: unknown): number {
  const parsed = Number(value ?? 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatTime(value?: string): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ''
    : new Intl.DateTimeFormat('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
    }).format(date);
}

function formatRelativeTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '最近';
  const minutes = Math.max(0, Math.round((Date.now() - date.getTime()) / 60_000));
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'short',
    day: 'numeric',
  }).format(date);
}

function toolKind(name: string): ToolTrace['kind'] {
  const lower = name.toLowerCase();
  if (lower.includes('terminal') || lower.includes('exec')) return 'terminal';
  if (lower.includes('read')) return 'read';
  if (lower.includes('edit') || lower.includes('write')) return 'edit';
  if (lower.includes('search')) return 'search';
  if (lower.includes('mcp')) return 'mcp';
  return 'artifact';
}

function taskStatus(status: string): AgentTask['status'] {
  if (status === 'ACTIVE') return 'running';
  if (status === 'COMPLETED') return 'completed';
  if (status === 'CANCELLED') return 'cancelled';
  if (status === 'FAILED') return 'failed';
  if (status === 'WAITING_DEPENDENCY') return 'waiting';
  if (status === 'INTERRUPTED') return 'interrupted';
  if (status === 'BLOCKED_DEPENDENCY_FAILED') return 'blocked';
  if (status === 'PENDING_START') return 'pending';
  return 'failed';
}

function isTerminalTaskStatus(status: AgentTask['status'] | SubagentRun['status']): boolean {
  return status === 'completed'
    || status === 'cancelled'
    || status === 'failed'
    || status === 'interrupted'
    || status === 'blocked'
    || status === 'ended';
}
