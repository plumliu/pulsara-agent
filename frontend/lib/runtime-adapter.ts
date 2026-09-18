import { LocalMemoryApi } from './memory-api';
import type {
  PluginImportOptions,
  PluginImportDiscovery,
  AgentTask,
  CapabilityOperation,
  CapabilitySnapshot,
  PluginMcpConnection,
  PluginMcpEditInput,
  UserPluginCapability,
  McpEditInput,
  McpImportSource,
  McpImportPreview,
  McpImportSelection,
  McpConnectionTestResult,
  McpServerStatus,
  Message,
  PermissionMode,
  ProjectCapabilityAdoption,
  ProjectCapabilityMutationResult,
  ReasoningBlock,
  SessionSummary,
  SessionWorkspaceSelection,
  SkillInstallResult,
  SkillCapability,
  SkillImportInput,
  SkillImportCandidate,
  SubagentActivity,
  SubagentRun,
  TodoRun,
  ToolTrace,
  UserCapabilitySnapshot,
  UserSkillCapability,
  Workspace,
} from './pulsara-types';
import { protocolPermissionModes } from './pulsara-types';
import {
  decodeCanonicalPromptContent,
  editablePromptToTransport,
  promptContentTextProjection,
  PROMPT_BODY_MEDIA_TYPE,
  type CanonicalPromptContent,
  type CanonicalPromptImagePart,
  type EditablePromptContent,
} from './prompt-content';

export type {
  CanonicalPromptContent,
  CanonicalPromptImagePart,
  EditablePromptContent,
} from './prompt-content';

export interface RuntimeBootstrap {
  application: { name: string; version: string; transport: string };
  workspace: Workspace;
  protocol: { major: number; minor: number };
  runtime: { status: string; origin: string; database_state: DatabaseDataPlaneState };
  local_settings: LocalSettingsSummary;
  model_configurations: ModelConfigurationSummary[];
  database_state: DatabaseDataPlaneState;
}

export type DatabaseDataPlaneState =
  | 'database_not_configured'
  | 'database_configured_unverified'
  | 'database_unavailable'
  | 'database_schema_action_required'
  | 'database_reset_required'
  | 'database_resetting'
  | 'database_restart_required'
  | 'ready';

export type ReasoningSelectionPayload =
  | { kind: 'effort'; value: string | null }
  | { kind: 'toggle'; enabled: boolean }
  | { kind: 'budget_tokens'; tokens: number };

export interface ModelCallBindingPayload {
  connection_id: string;
  reasoning: ReasoningSelectionPayload | null;
}

export interface ModelCallBindingUpdate {
  modelCallBinding: ModelCallBindingPayload;
  reasoningPreferenceReset: boolean;
}

export interface ReasoningControlSummary {
  kind: 'selectable' | 'fixed_on' | 'unavailable' | 'provider_default';
  effort?: { values: Array<string | null> } | null;
  toggle?: boolean;
  budget_tokens?: { minimum: number | null; maximum: number | null } | null;
}

export interface ModelConfigurationSummary {
  id: string;
  source: 'models_dev' | 'user_declared';
  route_id: string;
  wire_api: 'openai_chat_completions' | 'openai_responses';
  model_id: string;
  base_url: string;
  status: 'ready' | 'unavailable';
  authentication: 'bearer_api_key' | 'none';
  credential_configured: boolean;
  route_name?: string;
  display_name?: string;
  context_tokens?: number;
  max_output_tokens?: number;
  tool_call?: boolean | null;
  input_modalities?: string[] | null;
  output_modalities?: string[] | null;
  reasoning: ReasoningControlSummary;
  default_reasoning?: ReasoningSelectionPayload | null;
}

export type ModelConfigurationInput =
  | {
    source: 'models_dev';
    route_id: string;
    model_id: string;
    wire_api: 'openai_chat_completions' | 'openai_responses';
    api_key: string;
  }
  | {
    source: 'user_declared';
    configuration_name: string;
    base_url: string;
    model_id: string;
    wire_api: 'openai_chat_completions' | 'openai_responses';
    authentication: 'bearer_api_key' | 'none';
    api_key: string | null;
    context_tokens: number;
    max_output_tokens: number;
    tool_call: boolean;
    input_modalities: string[];
    reasoning:
      | { kind: 'provider_default' }
      | { kind: 'toggle' }
      | { kind: 'effort'; values: string[] };
  };

export interface ModelCatalogWireApi {
  wire_api: 'openai_chat_completions' | 'openai_responses';
  executable: boolean;
  reason: string | null;
  endpoint: string | null;
  reasoning?: ReasoningControlSummary;
  recommended?: boolean;
}

export interface ModelCatalogModel {
  model_id: string;
  display_name: string;
  wire_dialect: 'openai_compatible' | 'provider_native' | 'unknown';
  context_tokens: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  tool_call: boolean | null;
  input_modalities: string[] | null;
  output_modalities: string[] | null;
  wire_shape_hint: 'responses' | 'completions' | null;
  wire_apis: ModelCatalogWireApi[];
}

export interface ModelCatalogRoute {
  route_id: string;
  display_name: string;
  models: ModelCatalogModel[];
}

export interface ModelCatalogReadModel {
  status: 'ready' | 'unavailable';
  routes: ModelCatalogRoute[];
}

export interface LocalSettingsSummary {
  state?: 'ready' | 'unavailable';
  postgres: { runtime_dsn: string; admin_dsn: string | null } | null;
  dashscope_credentials: {
    embedding_configured: boolean;
    rerank_configured: boolean;
  };
}

export interface LocalSettingsReadModel {
  local_settings: LocalSettingsSummary;
  model_configurations: ModelConfigurationSummary[];
  database_state: DatabaseDataPlaneState;
}

export interface RuntimeProjection {
  /** Canonical ROOT admission order, before lossy transcript presentation. */
  canonicalRootTurnIds?: readonly string[];
  messages: Message[];
  presentationNotices?: string[];
  contextCompaction?: ContextCompactionBoundary;
  initialContextBase?: ProtocolCanonicalControl['initial_context_base'];
  isRunning: boolean;
  queuedCount: number;
  queuedPrompts: QueuedPrompt[];
  promptTransitions: LocalPromptSubmission[];
  planMode: boolean;
  activeTurnId?: string;
  hostSessionId?: string;
  controlAdmissionDeadlineMs?: number;
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

export interface QueuedPrompt {
  queueItemId: string;
  commandId: string;
  sequence: number;
  status: 'pending';
  deliveryMode: 'new-turn' | 'steer';
  targetTurnId?: string;
  content: CanonicalPromptContent;
  permission?: PermissionMode;
  submittedAt?: string;
  requestedPermission?: PermissionMode;
}

export interface QueuedPromptAction {
  sessionId: string;
  connectionGeneration: number;
  commandId: string;
  kind: 'send' | 'edit' | 'delete';
  source: QueuedPrompt;
  restoredContent?: EditablePromptContent;
  targetTurnId?: string;
  submittedAt: string;
  status: 'submitting' | 'unknown' | 'accepted' | 'rejected';
  receipt?: CommandReceipt;
  handled?: boolean;
  lastCheckedEventSequence?: number;
  lastCheckedConnectionGeneration?: number;
}

export interface ToolArtifactPage {
  resultEntryId: string;
  text: string;
  offsetChars: number;
  returnedChars: number;
  totalChars: number;
  hasMore: boolean;
  nextOffsetChars?: number;
}

export interface BackgroundProcess {
  processId: string;
  command: string;
  cwd: string;
  status: string;
  exitCode?: number;
  physicalState: string;
  ioMode: string;
  streamId: string;
  outputCursor: string;
  retainedFromCursor: string;
  durationSeconds: number;
  timedOut: boolean;
  originTurnId: string;
  originSubagentTaskId?: string;
}

export interface BackgroundProcessPage {
  processes: BackgroundProcess[];
  nextCursor?: string;
}

export interface BackgroundProcessLog {
  process: BackgroundProcess;
  output: string;
  outputCursor: string;
  retainedFromCursor: string;
  gapBeforeOutput: boolean;
  truncatedByResponseBound: boolean;
  sourceCoverage: string;
}

export interface UserControlCommandRef {
  commandId: string;
  operation: 'STOP_ACTIVE_TURN' | 'CANCEL_SUBAGENT_TASK' | 'TERMINATE_BACKGROUND_PROCESS';
  sessionId: string;
  hostSessionId: string;
  targetKind: 'ROOT_TURN' | 'SUBAGENT_TASK' | 'BACKGROUND_PROCESS';
  targetId: string;
}

export function createUserControlCommandRef(
  projection: Pick<RuntimeProjection, 'hostSessionId' | 'controlAdmissionDeadlineMs'>,
  sessionId: string,
  operation: UserControlCommandRef['operation'],
  targetKind: UserControlCommandRef['targetKind'],
  targetId: string,
): UserControlCommandRef {
  const hostSessionId = projection.hostSessionId ?? '';
  const deadline = projection.controlAdmissionDeadlineMs ?? 0;
  if (!sessionId || !hostSessionId || !targetId || deadline < 1) {
    throw new RuntimeApiError(
      'CONTROL_TARGET_UNAVAILABLE',
      '当前控制身份尚未确认，请刷新后重试。',
      true,
    );
  }
  return {
    commandId: `command:control:${deadline}:${crypto.randomUUID()}`,
    operation,
    sessionId,
    hostSessionId,
    targetKind,
    targetId,
  };
}

export interface UserControlReceipt {
  accepted: boolean;
  execution: 'NOT_STARTED' | 'RUNNING' | 'FINISHED';
  process?: {
    disposition: string;
    status: string;
    exitCode?: number;
    physicalState: string;
    groupAlive?: boolean;
  };
  monitor?: {
    monitorId: string;
    outcome: string;
    detail?: string;
  };
  feedback?: {
    canonicalStatus: string;
    inclusionStatus: string;
    ownerAvailability: string;
    reason?: string;
    targetRootTurnId?: string;
    entryId?: string;
    contextBindingRevisionId?: string;
    modelCallIndex?: number;
    transportInvocationAttempted: boolean;
    transportInvocationSucceeded?: boolean;
    transportDetail?: string;
  };
}

export interface UserControlQueryResult {
  status: 'FOUND' | 'RESULT_UNAVAILABLE' | 'OWNER_UNAVAILABLE';
  receipt?: CommandReceipt;
}

export interface LocalPromptSubmission {
  // Browser-only placement chosen at submission; never changes delivery mode.
  displayAsMessage?: boolean;
  sessionId: string;
  connectionGeneration: number;
  commandId: string;
  content?: EditablePromptContent | CanonicalPromptContent;
  contentUnavailable?: boolean;
  handledByQueueAction?: boolean;
  deliveryMode: 'new-turn' | 'steer';
  targetTurnId?: string;
  permission?: PermissionMode;
  queueItemId?: string;
  consumedEntryId?: string;
  observedPending?: boolean;
  lastCheckedEventSequence?: number;
  lastCheckedConnectionGeneration?: number;
  status: 'sending' | 'synchronizing' | 'queued' | 'unknown' | 'consumed' | 'rejected' | 'cancelled';
  outcomeCode?: string;
  detail?: string;
}

export interface ContextCompactionBoundary {
  contextBindingRevisionId: string;
  turnId: string;
  sourceThroughSequence: number;
  adoptedAfterEntrySequence: number;
  acceptedAt: string;
}

export type ForkOutcome = {
  outcome: 'CREATED_AND_OPENED' | 'CREATED_OPEN_DEFERRED' | 'NOT_CREATED';
  child_session_id: string;
  public_code?: string;
};

export type RuntimeInteractionSummary =
  | {
    id: string;
    kind: 'tool-confirmation';
    expiresAtUtc: string;
    decisionInProgress: boolean;
    prompt: string;
    options: string[];
  }
  | { id: string; kind: 'capability-form'; prompt: string; options: string[] }
  | {
    id: string;
    kind: 'plan-question' | 'plan-draft';
    workflowId: string;
    workflowRevision: number;
  };

export type RuntimeInteractionContent =
  | { kind: 'capability-form'; form: Record<string, unknown> }
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
  | { kind: 'capability'; decision: 'SUBMIT' | 'CANCEL'; submission?: Record<string, unknown> }
  | { kind: 'tool'; decision: 'allow' | 'deny' }
  | { kind: 'plan-question-option'; optionOrdinal: number }
  | { kind: 'plan-question-text'; text: string }
  | {
    kind: 'plan-draft';
    decision: 'approve' | 'revise' | 'cancel';
    feedback?: string;
  };

/** App freezes a tool command before transport; forms/plan keep their own semantics. */
export type RuntimeInteractionSubmission =
  | Exclude<RuntimeInteractionResolution, { kind: 'tool' }>
  | { kind: 'tool'; decision: 'allow' | 'deny'; commandId: string };

/** Browser boundary for the local Pulsara application. */
export interface RuntimeAdapter {
  readonly memory: LocalMemoryApi;
  bootstrap(): Promise<RuntimeBootstrap>;
  modelCatalog(refresh?: boolean): Promise<ModelCatalogReadModel>;
  localSettings(): Promise<LocalSettingsReadModel>;
  addModelConfiguration(input: ModelConfigurationInput): Promise<{ model_configuration: ModelConfigurationSummary; wire_shape_warning: boolean }>;
  deleteModelConfiguration(connectionId: string): Promise<{
    model_configuration_id: string;
    deleted: boolean;
    model_configurations: ModelConfigurationSummary[];
  }>;
  testModelConfiguration(input: ModelConfigurationInput): Promise<{ status: 'ready' }>;
  savePostgres(runtimeDsn: string, adminDsn: string | null): Promise<LocalSettingsReadModel & { restart_required: boolean }>;
  checkPostgres(): Promise<Record<string, unknown>>;
  migratePostgres(): Promise<Record<string, unknown>>;
  resetPostgres(target: NonNullable<LocalSettingsSummary['postgres']>): Promise<{ database_name: string; restart_required: boolean }>;
  putDashScopeCredential(kind: 'embedding' | 'rerank', apiKey: string): Promise<boolean>;
  deleteDashScopeCredential(kind: 'embedding' | 'rerank'): Promise<boolean>;
  updateModelCallBinding(sessionId: string, binding: ModelCallBindingPayload): Promise<ModelCallBindingUpdate>;
  reopenRuntime(sessionId: string): Promise<{
    status: 'reopened' | 'deferred';
    publicCode?: string;
  }>;
  connect(sessionId: string, takeover?: boolean): Promise<RuntimeConnection>;
  createSession(selection: SessionWorkspaceSelection): Promise<SessionSummary>;
  forkConversation(sessionId: string, anchorEntryId: string, childSessionId: string): Promise<ForkOutcome>;
  readSession(sessionId: string): Promise<SessionSummary | null>;
  listSessions(): Promise<SessionSummary[]>;
  listSessionTaskGroups(sessionId: string, cursor?: string): Promise<AgentTaskGroupPage>;
  listSessionTasks(sessionId: string, cursor?: string, batchId?: string): Promise<AgentTaskPage>;
  listSessionTaskActivities(sessionId: string, taskId: string, cursor?: string): Promise<AgentTaskActivityPage>;
  inspectCapabilities(sessionId: string): Promise<CapabilitySnapshot>;
  reconnectMcpServer(sessionId: string, serverId: string): Promise<CapabilitySnapshot>;
  installSkill(
    sessionId: string,
    input: SkillImportInput,
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
  removeProjectSkill(sessionId: string, skill: SkillCapability): Promise<ProjectCapabilityMutationResult>;
  createProjectMcp(
    sessionId: string,
    input: McpEditInput,
  ): Promise<ProjectCapabilityMutationResult>;
  testProjectMcp(sessionId: string, input: McpEditInput): Promise<McpConnectionTestResult>;
  importProjectMcp(sessionId: string, input: McpImportSelection): Promise<ProjectCapabilityMutationResult>;
  projectMcpAuthorization(sessionId: string, serverId: string, action: 'login' | 'status' | 'cancel' | 'logout'): Promise<{state: string; error: string | null}>;
  setProjectMcpEnabled(
    sessionId: string,
    serverId: string,
    configIdentity: string,
    enabled: boolean,
  ): Promise<ProjectCapabilityMutationResult>;
  updateProjectMcp(sessionId: string, input: McpEditInput, expectedIdentity: string): Promise<ProjectCapabilityMutationResult>;
  removeProjectMcp(
    sessionId: string,
    serverId: string,
    configIdentity: string,
  ): Promise<ProjectCapabilityMutationResult>;
  inspectUserCapabilities(activeSessionId?: string): Promise<UserCapabilitySnapshot>;
  refreshUserCapabilities(activeSessionId?: string): Promise<UserCapabilitySnapshot>;
  previewSkillImport(sourcePath: string): Promise<SkillImportCandidate[]>;
  installUserSkill(input: SkillImportInput, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  setUserSkillEnabled(skillPath: string, enabled: boolean, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  removeUserSkill(skill: UserSkillCapability, activeSessionId?: string): Promise<{
    operation: CapabilityOperation; capabilities: UserCapabilitySnapshot;
  }>;
  createUserMcp(input: McpEditInput, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  testUserMcp(input: McpEditInput): Promise<McpConnectionTestResult>;
  previewMcpImport(input: McpImportSource): Promise<McpImportPreview[]>;
  importUserMcp(input: McpImportSelection, activeSessionId?: string): Promise<{operation: CapabilityOperation; capabilities: UserCapabilitySnapshot}>;
  updateUserMcp(input: McpEditInput, expectedIdentity: string, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  removeUserMcp(serverId: string, expectedIdentity: string, activeSessionId?: string): Promise<{
    operation: CapabilityOperation; capabilities: UserCapabilitySnapshot;
  }>;
  userMcpAuthorization(serverId: string, action: 'login' | 'status' | 'cancel' | 'logout'): Promise<{ state: string; error: string | null }>;
  pluginMcpAuthorization(plugin: UserPluginCapability, connection: PluginMcpConnection, action: 'login' | 'status' | 'cancel' | 'logout'): Promise<{ state: string; error: string | null }>;
  previewPluginImport(sourcePath: string): Promise<PluginImportDiscovery>;
  installUserPlugin(sourcePath: string, activeSessionId?: string, options?: PluginImportOptions): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  setUserPluginEnabled(
    pluginId: string,
    packageInstallId: string,
    enabled: boolean,
    connectionReview: NonNullable<UserPluginCapability['connectionReview']>,
    activeSessionId?: string,
  ): Promise<{ operation: CapabilityOperation; capabilities: UserCapabilitySnapshot }>;
  removeUserPlugin(pluginId: string, packageInstallId: string, activeSessionId?: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  updatePluginConnection(plugin: UserPluginCapability, connection: PluginMcpConnection, input: PluginMcpEditInput, activeSessionId?: string): Promise<{ operation: CapabilityOperation; capabilities: UserCapabilitySnapshot }>;
  openCapabilityRoot(root: 'agents' | 'pulsara'): Promise<void>;
}

export interface AgentTaskPage {
  tasks: AgentTask[];
  totalCount: number;
  remainingCount: number;
  nextCursor?: string;
}

export interface AgentTaskGroup {
  id: string;
  parentTurnId: string;
  firstAcceptedAt: string;
  taskCount: number;
  statusCounts: Record<'pending' | 'active' | 'waiting' | 'completed' | 'cancelled' | 'failed' | 'interrupted' | 'blocked', number>;
  singleTaskLabel?: string;
}

export interface AgentTaskGroupPage {
  groups: AgentTaskGroup[];
  totalCount: number;
  remainingCount: number;
  nextCursor?: string;
}

export interface AgentTaskActivityRecord {
  entryId: string;
  turnId: string;
  entrySequence: number;
  entryKind: string;
  acceptedAt: string;
  objective: string;
  body?: string;
  promptContent?: CanonicalPromptContent;
  contentKind: 'INLINE' | 'CANONICAL_BLOB';
  contentDigest: string;
  contentSize: number;
  blocks: Array<{ blockId: string; ordinal: number; kind: string; toolCallId?: string; toolName?: string }>;
  toolResults: Array<{ attemptId?: string; assistantEntryId: string; toolCallId: string; resultEntryId: string; resultState: string }>;
}

export interface AgentTaskActivityPage {
  activities: AgentTaskActivityRecord[];
  nextCursor?: string;
}

/**
 * Lower the task inspector's canonical entry rows into the same message model
 * used by the root transcript. Canonical assistant envelopes are storage
 * records, not user-facing prose; tool results stay attached to their exact
 * assistant entry and tool call instead of becoming standalone JSON messages.
 */
export function projectAgentTaskConversation(
  records: readonly AgentTaskActivityRecord[],
  projectedActivities: readonly SubagentActivity[] = [],
): Message[] {
  const messages: Message[] = [];
  const projectedById = new Map(projectedActivities.map((activity) => [activity.id, activity]));
  const representedProjectedIds = new Set<string>();
  const ordered = [...records].sort((left, right) => (
    left.entrySequence - right.entrySequence || left.entryId.localeCompare(right.entryId)
  ));

  for (const record of ordered) {
    const body = record.body ?? '';
    if (record.entryKind === 'USER_MESSAGE' || record.entryKind === 'INTER_AGENT_MESSAGE') {
      messages.push({
        id: record.entryId,
        turnId: record.turnId,
        entrySequence: record.entrySequence,
        role: 'user',
        userKind: record.entryKind === 'INTER_AGENT_MESSAGE' ? 'steer' : 'prompt',
        time: formatTime(record.acceptedAt),
        body,
      });
      representedProjectedIds.add(record.entryId);
      continue;
    }

    if (record.entryKind === 'ASSISTANT_MESSAGE' || record.entryKind === 'ASSISTANT_TOOL_REQUEST') {
      const projected = projectedById.get(record.entryId);
      if (projected) representedProjectedIds.add(record.entryId);
      const traces = projected?.traces?.map((trace) => ({
        ...trace,
        artifact: trace.artifact ? { ...trace.artifact } : undefined,
      })) ?? record.blocks
        .filter((block) => block.kind === 'TOOL_CALL')
        .sort((left, right) => left.ordinal - right.ordinal)
        .map((block): ToolTrace => {
          const name = block.toolName || 'Tool';
          const result = record.toolResults.find((candidate) => (
            candidate.assistantEntryId === record.entryId
            && candidate.toolCallId === block.toolCallId
          ));
          const status = toolResultStatus(result?.resultState);
          return {
            id: block.toolCallId || block.blockId,
            kind: toolKind(name),
            toolName: name,
            title: toolDisplayName(name),
            subtitle: status === 'running' ? '等待结果' : status === 'completed' ? '已完成' : toolFailureLabel(result?.resultState),
            status,
            resultEntryId: result?.resultEntryId,
            resultState: result?.resultState,
            meta: status === 'completed' ? '操作完成' : status === 'cancelled' ? '操作已取消' : status === 'failed' ? '操作未完成' : undefined,
          };
        });
      messages.push({
        id: record.entryId,
        turnId: record.turnId,
        entrySequence: record.entrySequence,
        role: 'assistant',
        assistantKind: record.entryKind === 'ASSISTANT_MESSAGE' ? 'terminal' : 'tool-request',
        time: formatTime(record.acceptedAt),
        body: projected?.body ?? taskAssistantBody(record.entryKind, body),
        reasoning: projected?.reasoning?.map((block) => ({ ...block })),
        traces: traces.length ? traces : undefined,
        status: projected?.status === 'running' ? 'running' : 'completed',
      });
      continue;
    }

    if (record.entryKind === 'TOOL_RESULT') {
      const resultRef = record.toolResults.find((candidate) => candidate.resultEntryId === record.entryId);
      const target = resultRef
        ? messages.find((message) => message.id === resultRef.assistantEntryId)
        : undefined;
      const trace = target?.traces?.find((candidate) => candidate.id === resultRef?.toolCallId);
      if (trace) {
        trace.status = toolResultStatus(resultRef?.resultState);
        trace.subtitle = trace.status === 'completed' ? '已完成' : toolFailureLabel(resultRef?.resultState);
        trace.resultText = body;
        trace.resultContent = record.promptContent;
        trace.resultEntryId = record.entryId;
        trace.resultState = resultRef?.resultState;
        trace.resultSummary = summarizeToolResult(trace.toolName, body);
        trace.meta = trace.status === 'completed' ? '操作完成' : trace.status === 'cancelled' ? '操作已取消' : '操作未完成';
      } else {
        messages.push({
          id: record.entryId,
          turnId: record.turnId,
          entrySequence: record.entrySequence,
          role: 'assistant',
          assistantKind: 'tool-request',
          time: formatTime(record.acceptedAt),
          body: '',
          status: 'completed',
          traces: [{
            id: resultRef?.toolCallId || record.entryId,
            kind: 'artifact',
            title: '操作结果',
            subtitle: '已记录',
            status: toolResultStatus(resultRef?.resultState),
            resultText: body,
            resultContent: record.promptContent,
            resultEntryId: record.entryId,
            resultState: resultRef?.resultState,
            resultSummary: summarizeToolResult(undefined, body),
            associationPending: true,
          }],
        });
      }
      representedProjectedIds.add(record.entryId);
      continue;
    }

    if (record.entryKind === 'TERMINAL_OBSERVATION') {
      const target = [...messages].reverse().find((message) => message.role === 'assistant');
      const trace: ToolTrace = {
        id: record.entryId,
        kind: 'terminal',
        title: '命令进展',
        subtitle: '已记录',
        status: 'completed',
        resultText: body,
      };
      if (target) target.traces = [...(target.traces ?? []), trace];
      else messages.push({
        id: record.entryId,
        turnId: record.turnId,
        entrySequence: record.entrySequence,
        role: 'assistant',
        assistantKind: 'tool-request',
        time: formatTime(record.acceptedAt),
        body: '',
        traces: [trace],
        status: 'completed',
      });
      representedProjectedIds.add(record.entryId);
    }
  }

  for (const activity of projectedActivities) {
    if (representedProjectedIds.has(activity.id) || messages.some((message) => message.id === activity.id)) continue;
    messages.push({
      id: activity.id,
      role: 'assistant',
      assistantKind: activity.status === 'running' ? 'live' : 'terminal',
      time: activity.time,
      body: activity.body,
      reasoning: activity.reasoning?.map((block) => ({ ...block })),
      traces: activity.traces?.map((trace) => ({ ...trace })),
      status: activity.status === 'running' ? 'running' : 'completed',
    });
  }
  return messages;
}

function taskAssistantBody(entryKind: string, body: string): string {
  if (entryKind === 'ASSISTANT_TOOL_REQUEST' || !body) return '';
  try {
    const value = JSON.parse(body) as { blocks?: unknown; draft_identity?: unknown };
    if (Array.isArray(value.blocks) && typeof value.draft_identity === 'string') return '';
  } catch {
    // Natural-language assistant content is not expected to be JSON.
  }
  return body;
}

export interface RuntimeConnection {
  readonly sessionId: string;
  readonly role: 'observer' | 'controller';
  readonly generation: number;
  current(): RuntimeProjection;
  snapshot(): Promise<RuntimeProjection>;
  observe(signal?: AbortSignal): Promise<RuntimeProjection>;
  submitPrompt(
    commandId: string,
    content: EditablePromptContent,
    permission: PermissionMode,
  ): Promise<CommandReceipt>;
  cancelQueuedPrompt(commandId: string, queueItemId: string): Promise<CommandReceipt>;
  steerQueuedPrompt(commandId: string, queueItemId: string, targetTurnId: string): Promise<CommandReceipt>;
  stopActiveTurn(reference: UserControlCommandRef): Promise<CommandReceipt>;
  cancelSubagentTask(reference: UserControlCommandRef): Promise<CommandReceipt>;
  terminateBackgroundProcess(reference: UserControlCommandRef): Promise<CommandReceipt>;
  queryControlCommand(reference: UserControlCommandRef): Promise<UserControlQueryResult>;
  listBackgroundProcesses(cursor?: string): Promise<BackgroundProcessPage>;
  readBackgroundProcessLog(processId: string, outputCursor?: string): Promise<BackgroundProcessLog>;
  readCanonicalEntryContent(entryId: string, digest: string, size: number): Promise<string>;
  readPromptImage(image: CanonicalPromptImagePart): Promise<Uint8Array>;
  readPromptForEdit(content: CanonicalPromptContent): Promise<EditablePromptContent>;
  compactContext(targetTurnId?: string): Promise<CommandReceipt>;
  acceptSubagentCompletion(taskId: string, permission: PermissionMode): Promise<CommandReceipt>;
  enterPlan(reason: string, permission: PermissionMode): Promise<CommandReceipt>;
  readInteraction(interaction: RuntimeInteractionSummary): Promise<RuntimeInteractionContent>;
  resolveInteraction(
    interaction: RuntimeInteractionSummary,
    resolution: RuntimeInteractionSubmission,
  ): Promise<CommandReceipt | { submitted: boolean }>;
  queryCommand(commandId: string): Promise<CommandReceipt | undefined>;
  readToolArtifact(resultEntryId: string, offsetChars: number, maxChars?: number): Promise<ToolArtifactPage>;
  close(): Promise<void>;
}

export interface CommandReceipt {
  commandId: string;
  status: 'succeeded' | 'rejected' | 'pending' | 'failed';
  targetId?: string;
  publicCode?: string;
  publicMessage?: string;
  promptDelivery?: {
    queueItemId: string;
    queueStatus: string;
    consumedEntryId?: string;
    deliveryMode: 'new-turn' | 'steer';
  };
  planDraftDecision?: 'approve' | 'revise' | 'cancel';
  planContinuationTurnId?: string;
  userControl?: UserControlReceipt;
}

export type RuntimeCommandKind =
  | 'SUBMIT_PROMPT'
  | 'CANCEL_QUEUED_PROMPT'
  | 'STEER_QUEUED_PROMPT'
  | 'STOP_ACTIVE_TURN'
  | 'CANCEL_SUBAGENT_TASK'
  | 'TERMINATE_BACKGROUND_PROCESS'
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
  fork_eligible?: boolean;
  entry_owner_kind?: 'EXECUTED_TURN' | 'IMPORTED_HISTORY';
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
  tool_result?: {
    assistant_entry_id?: string; tool_call_id?: string; result_state?: string;
    artifact_disposition?: string; source_coverage?: string; display_kind?: string;
    source_coverage_reason?: string; artifact_unavailability_reason?: string;
  };
  input_source?: { queue_item_id?: string; command_id?: string; delivery_mode?: string };
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
  completion_accepted?: boolean;
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
  completion_accepted?: boolean;
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
  initial_context_base?: {
    base_kind: 'FULL_HISTORY' | 'SNAPSHOT';
    source_through_sequence?: string | number;
    display_after_entry_sequence?: string | number;
  };
  session_lifecycle?: string;
  latest_root_turn?: { turn_id?: string; status?: string; terminal_reason?: string };
  active_turns?: ProtocolActiveTurn[];
  prompt_queue?: Array<{
    queue_item_id?: string; command_id?: string; queue_sequence?: string | number;
    status?: string; delivery_mode?: string; target_turn_id?: string; accepted_at_utc?: string;
    content?: ProtocolContent; permission?: { requested_mode?: string; effective_mode?: string };
  }>;
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
  channel_kind?: string;
  channel_tool_call_id?: string;
  channel_attempt_id?: string;
  proposed_entry_id?: string;
  payload?: Record<string, Record<string, unknown>>;
}

interface ProtocolSettlement {
  kind: string;
  draft_identity?: string;
  generation_id?: string;
  channel_kind?: string;
  channel_tool_call_id?: string;
  channel_attempt_id?: string;
  proposed_entry_id?: string;
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
  host_session_id?: string;
  active_root_turn_id?: string;
  control_admission_deadline_ms?: string | number;
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
  presentation_notices?: string[];
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

interface LiveToolResult {
  id: string;
  turnId: string;
  scopeKind: string;
  taskId: string;
  draftIdentity?: string;
  generationId?: string;
  blockId?: string;
  assistantEntryId?: string;
  toolCallId?: string;
  attemptId?: string;
  proposedEntryId?: string;
  text: string;
  hasText: boolean;
  ended: boolean;
  resultState?: string;
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
  prompt_delivery?: {
    queue_item_id?: string; queue_status?: string; consumed_entry_id?: string;
    delivery_mode?: string;
  };
  plan_draft_decision?: string;
  plan_continuation_turn_id?: string;
  user_control?: Record<string, unknown>;
}

interface ProtocolBackgroundProcess {
  process_id: string;
  command?: string;
  cwd?: string;
  status?: string;
  exit_code?: string | number;
  physical_state?: string;
  io_mode?: string;
  stream_id?: string;
  output_cursor?: string;
  retained_from_cursor?: string;
  duration_seconds?: number;
  timed_out?: boolean;
  origin?: {
    turn_id?: string;
    scope_kind?: string;
    subagent_task_id?: string;
  };
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
  readonly memory = new LocalMemoryApi();
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

  async modelCatalog(refresh = false): Promise<ModelCatalogReadModel> {
    return apiRequest<ModelCatalogReadModel>(
      refresh ? '/api/model-catalog/refresh' : '/api/model-catalog',
      refresh ? { method: 'POST' } : undefined,
    );
  }

  async localSettings(): Promise<LocalSettingsReadModel> {
    return apiRequest<LocalSettingsReadModel>('/api/local-settings');
  }

  async addModelConfiguration(input: ModelConfigurationInput) {
    return apiRequest<{
      model_configuration: ModelConfigurationSummary;
      wire_shape_warning: boolean;
    }>('/api/model-configurations', {
      method: 'POST',
      body: JSON.stringify(input),
    });
  }

  async deleteModelConfiguration(connectionId: string) {
    return apiRequest<{
      model_configuration_id: string;
      deleted: boolean;
      model_configurations: ModelConfigurationSummary[];
    }>(`/api/model-configurations/${encodeURIComponent(connectionId)}`, {
      method: 'DELETE',
    });
  }

  async testModelConfiguration(input: ModelConfigurationInput) {
    return apiRequest<{ status: 'ready' }>('/api/model-configurations/test', {
      method: 'POST',
      body: JSON.stringify(input),
    });
  }

  async savePostgres(runtimeDsn: string, adminDsn: string | null) {
    return apiRequest<LocalSettingsReadModel & { restart_required: boolean }>(
      '/api/local-settings/postgres',
      {
        method: 'PUT',
        body: JSON.stringify({ runtime_dsn: runtimeDsn, admin_dsn: adminDsn }),
      },
    );
  }

  async checkPostgres(): Promise<Record<string, unknown>> {
    return apiRequest<Record<string, unknown>>('/api/local-settings/postgres/check', { method: 'POST' });
  }

  async migratePostgres(): Promise<Record<string, unknown>> {
    return apiRequest<Record<string, unknown>>('/api/local-settings/postgres/migrate', { method: 'POST' });
  }

  async resetPostgres(target: NonNullable<LocalSettingsSummary['postgres']>): Promise<{ database_name: string; restart_required: boolean }> {
    return apiRequest('/api/local-settings/postgres/reset', {
      method: 'POST', body: JSON.stringify({ ...target, confirmed: true }),
    });
  }

  async putDashScopeCredential(kind: 'embedding' | 'rerank', apiKey: string): Promise<boolean> {
    const value = await apiRequest<{ configured: boolean }>(
      `/api/local-settings/dashscope-credentials/${kind}`,
      { method: 'PUT', body: JSON.stringify({ api_key: apiKey }) },
    );
    return value.configured;
  }

  async deleteDashScopeCredential(kind: 'embedding' | 'rerank'): Promise<boolean> {
    const value = await apiRequest<{ configured: boolean }>(
      `/api/local-settings/dashscope-credentials/${kind}`,
      { method: 'DELETE' },
    );
    return value.configured;
  }

  async updateModelCallBinding(
    sessionId: string,
    binding: ModelCallBindingPayload,
  ): Promise<ModelCallBindingUpdate> {
    const value = await apiRequest<{
      model_call_binding: ModelCallBindingPayload;
      reasoning_preference_reset: boolean;
    }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/model-call-binding`,
      { method: 'PUT', body: JSON.stringify(binding) },
    );
    return {
      modelCallBinding: value.model_call_binding,
      reasoningPreferenceReset: value.reasoning_preference_reset,
    };
  }

  async listSessions(): Promise<SessionSummary[]> {
    const payload = await apiRequest<{ sessions: Array<Record<string, unknown>> }>('/api/sessions');
    return payload.sessions.map(projectSessionSummary);
  }

  async reopenRuntime(sessionId: string): Promise<{
    status: 'reopened' | 'deferred';
    publicCode?: string;
  }> {
    const payload = await apiRequest<{
      status: 'reopened' | 'deferred';
      public_code?: string;
    }>(`/api/sessions/${encodeURIComponent(sessionId)}/runtime/reopen`, {
      method: 'POST',
    });
    return { status: payload.status, publicCode: payload.public_code };
  }

  async listSessionTaskGroups(sessionId: string, cursor?: string): Promise<AgentTaskGroupPage> {
    const query = new URLSearchParams({ limit: '50' });
    if (cursor) query.set('cursor', cursor);
    const payload = await apiRequest<{
      groups?: Array<{
        group_id: string;
        parent_turn_id: string;
        first_accepted_at: string;
        task_count: string | number;
        status_counts?: Record<string, string | number>;
        single_task_label?: string | null;
      }>;
      total_count?: string | number;
      remaining_count?: string | number;
      next_cursor?: string | null;
    }>(`/api/sessions/${encodeURIComponent(sessionId)}/task-groups?${query.toString()}`);
    return {
      groups: (payload.groups ?? []).map((group) => ({
        id: group.group_id,
        parentTurnId: group.parent_turn_id,
        firstAcceptedAt: group.first_accepted_at,
        taskCount: numeric(group.task_count),
        statusCounts: {
          pending: numeric(group.status_counts?.pending),
          active: numeric(group.status_counts?.active),
          waiting: numeric(group.status_counts?.waiting),
          completed: numeric(group.status_counts?.completed),
          cancelled: numeric(group.status_counts?.cancelled),
          failed: numeric(group.status_counts?.failed),
          interrupted: numeric(group.status_counts?.interrupted),
          blocked: numeric(group.status_counts?.blocked),
        },
        singleTaskLabel: group.single_task_label || undefined,
      })),
      totalCount: numeric(payload.total_count),
      remainingCount: numeric(payload.remaining_count),
      nextCursor: payload.next_cursor || undefined,
    };
  }

  async listSessionTasks(sessionId: string, cursor?: string, batchId?: string): Promise<AgentTaskPage> {
    const query = new URLSearchParams({ limit: '50' });
    if (cursor) query.set('cursor', cursor);
    if (batchId) query.set('batch_id', batchId);
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

  async listSessionTaskActivities(sessionId: string, taskId: string, cursor?: string): Promise<AgentTaskActivityPage> {
    const query = new URLSearchParams({ limit: '50' });
    if (cursor) query.set('cursor', cursor);
    const payload = await apiRequest<{
      activities?: Array<{
        entry_id: string;
        turn_id: string;
        entry_sequence: string | number;
        entry_kind: string;
        accepted_at: string;
        objective: string;
        content: { kind: 'INLINE' | 'CANONICAL_BLOB'; inline_content?: string; digest: string; size: string | number; media_type?: string; codec?: string };
        blocks?: Array<{ block_id: string; ordinal: string | number; kind: string; tool_call_id?: string | null; tool_name?: string | null }>;
        tool_results?: Array<{ attempt_id?: string | null; assistant_entry_id: string; tool_call_id: string; result_entry_id: string; result_state: string }>;
      }>;
      next_cursor?: string | null;
    }>(`/api/sessions/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(taskId)}/activities?${query.toString()}`);
    return {
      activities: (payload.activities ?? []).map((activity) => {
        const promptContent = activity.entry_kind === 'TOOL_RESULT'
          && activity.content.media_type === PROMPT_BODY_MEDIA_TYPE
          ? decodePromptContent(activity.content, {
              kind: 'entry', entryId: activity.entry_id,
            })
          : undefined;
        return {
        entryId: activity.entry_id,
        turnId: activity.turn_id,
        entrySequence: numeric(activity.entry_sequence),
        entryKind: activity.entry_kind,
        acceptedAt: activity.accepted_at,
        objective: activity.objective,
        body: promptContent
          ? promptContentTextProjection(promptContent)
          : activity.content.inline_content
            ? decodeBase64(activity.content.inline_content)
            : undefined,
        promptContent,
        contentKind: activity.content.kind,
        contentDigest: activity.content.digest,
        contentSize: numeric(activity.content.size),
        blocks: (activity.blocks ?? []).map((block) => ({
          blockId: block.block_id,
          ordinal: numeric(block.ordinal),
          kind: block.kind,
          toolCallId: block.tool_call_id || undefined,
          toolName: block.tool_name || undefined,
        })),
        toolResults: (activity.tool_results ?? []).map((result) => ({
          attemptId: result.attempt_id || undefined,
          assistantEntryId: result.assistant_entry_id,
          toolCallId: result.tool_call_id,
          resultEntryId: result.result_entry_id,
          resultState: result.result_state,
        })),
        };
      }),
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
    input: SkillImportInput,
  ): Promise<{
    installation: SkillInstallResult;
    adoption: ProjectCapabilityAdoption;
    capabilities: CapabilitySnapshot;
  }> {
    const payload = await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/skills/install`,
      {
        method: 'POST',
        body: JSON.stringify({ source_path: input.sourcePath, name: input.name, description: input.description }),
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
    input: McpEditInput,
  ): Promise<ProjectCapabilityMutationResult> {
    const payload = await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp`,
      {
        method: 'POST',
        body: JSON.stringify({
          server_id: input.serverId,
          config: input.config,
          secret_changes: input.secretChanges,
        }),
      },
    );
    return projectCapabilityMutation(payload);
  }

  async testProjectMcp(sessionId: string, input: McpEditInput): Promise<McpConnectionTestResult> {
    return apiRequest<McpConnectionTestResult>(`/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/test`, {
      method: 'POST', body: JSON.stringify({server_id: input.serverId, config: input.config,
        secret_changes: input.secretChanges, retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false}),
    });
  }

  async importProjectMcp(sessionId: string, input: McpImportSelection): Promise<ProjectCapabilityMutationResult> {
    return projectCapabilityMutation(await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/import`,
      {method: 'POST', body: JSON.stringify(input)},
    ));
  }

  async projectMcpAuthorization(sessionId: string, serverId: string, action: 'login' | 'status' | 'cancel' | 'logout') {
    const suffix = action === 'login' ? 'authorize' : action === 'cancel' ? 'authorization/cancel' : 'authorization';
    return apiRequest<{state: string; error: string | null}>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/${encodeURIComponent(serverId)}/${suffix}`,
      {method: action === 'status' ? 'GET' : action === 'logout' ? 'DELETE' : 'POST', ...(action === 'status' ? {} : {body: '{}'})},
    );
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

  async removeProjectSkill(sessionId: string, skill: SkillCapability): Promise<ProjectCapabilityMutationResult> {
    return projectCapabilityMutation(await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/skills/remove`,
      {method: 'POST', body: JSON.stringify({path: skill.path, expected: skill.removalIdentity})},
    ));
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

  async updateProjectMcp(sessionId: string, input: McpEditInput, expectedIdentity: string): Promise<ProjectCapabilityMutationResult> {
    return projectCapabilityMutation(await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/${encodeURIComponent(input.serverId)}`,
      { method: 'PUT', body: JSON.stringify({ config: input.config, expected_identity: expectedIdentity,
        secret_changes: input.secretChanges, retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false }) },
    ));
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

  async previewSkillImport(sourcePath: string): Promise<SkillImportCandidate[]> {
    const result = await apiRequest<{ items: Array<{ source_path: string; name: string; description: string; valid: boolean; details: string[] }> }>('/api/capabilities/skills/preview', {
      method: 'POST', body: JSON.stringify({ source_path: sourcePath }),
    });
    return result.items.map((item) => ({ sourcePath: item.source_path, name: item.name, description: item.description, valid: item.valid, details: item.details }));
  }

  async installUserSkill(input: SkillImportInput, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/skills/install',
      {
        method: 'POST',
        body: JSON.stringify({ source_path: input.sourcePath, name: input.name, description: input.description, ...(activeSessionId ? { active_session_id: activeSessionId } : {}) }),
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

  async createUserMcp(input: McpEditInput, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>('/api/capabilities/mcp', {
      method: 'POST',
      body: JSON.stringify({ server_id: input.serverId, config: input.config, secret_changes: input.secretChanges,
        ...(activeSessionId ? { active_session_id: activeSessionId } : {}) }),
    }));
  }

  async removeUserSkill(skill: UserSkillCapability, activeSessionId?: string) {
    if (!skill.removalIdentity) throw new Error('技能目录尚未确认，请刷新后重试。');
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>('/api/capabilities/skills/remove', {
      method: 'POST', body: JSON.stringify({path: skill.path, expected: skill.removalIdentity,
        ...(activeSessionId ? {active_session_id: activeSessionId} : {})}),
    }));
  }

  async updateUserMcp(input: McpEditInput, expectedIdentity: string, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/mcp/${encodeURIComponent(input.serverId)}`, {
        method: 'PUT',
        body: JSON.stringify({ config: input.config, secret_changes: input.secretChanges, expected_identity: expectedIdentity,
          retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false,
          ...(activeSessionId ? { active_session_id: activeSessionId } : {}) }),
      }));
  }

  async testUserMcp(input: McpEditInput): Promise<McpConnectionTestResult> {
    return apiRequest('/api/capabilities/mcp/test', { method: 'POST', body: JSON.stringify({
      server_id: input.serverId, config: input.config, secret_changes: input.secretChanges,
      retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false,
    }) });
  }

  async previewMcpImport(input: McpImportSource): Promise<McpImportPreview[]> {
    return (await apiRequest<{items: McpImportPreview[]}>('/api/capabilities/mcp/import/preview', {method: 'POST', body: JSON.stringify(input)})).items;
  }

  async importUserMcp(input: McpImportSelection, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>('/api/capabilities/mcp/import', {method: 'POST', body: JSON.stringify({...input, active_session_id: activeSessionId})}));
  }

  async removeUserMcp(serverId: string, expectedIdentity: string, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/mcp/${encodeURIComponent(serverId)}`, {
        method: 'DELETE', body: JSON.stringify({ expected_identity: expectedIdentity,
          ...(activeSessionId ? { active_session_id: activeSessionId } : {}) }),
      }));
  }

  async userMcpAuthorization(serverId: string, action: 'login' | 'status' | 'cancel' | 'logout') {
    const suffix = action === 'login' ? 'authorize' : action === 'cancel' ? 'authorization/cancel' : 'authorization';
    return apiRequest<{ state: string; error: string | null }>(
      `/api/capabilities/mcp/${encodeURIComponent(serverId)}/${suffix}`, {
        method: action === 'status' ? 'GET' : action === 'logout' ? 'DELETE' : 'POST',
        ...(action === 'status' ? {} : { body: '{}' }),
      });
  }

  async previewPluginImport(sourcePath: string) {
    return apiRequest<PluginImportDiscovery>('/api/capabilities/plugins/preview-import', {
      method: 'POST', body: JSON.stringify({source_path: sourcePath}),
    });
  }

  async installUserPlugin(sourcePath: string, activeSessionId?: string, options?: PluginImportOptions) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/plugins/install',
      {
        method: 'POST',
        body: JSON.stringify({ source_path: sourcePath, ...options, ...(activeSessionId ? { active_session_id: activeSessionId } : {}) }),
      },
    ));
  }

  async setUserPluginEnabled(
    pluginId: string,
    packageInstallId: string,
    enabled: boolean,
    connectionReview: NonNullable<UserPluginCapability['connectionReview']>,
    activeSessionId?: string,
  ) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/plugins/${encodeURIComponent(pluginId)}/enabled`,
      {
        method: 'POST',
        body: JSON.stringify({
          enabled,
          package_install_id: packageInstallId,
          connection_review: connectionReview,
          ...(activeSessionId ? { active_session_id: activeSessionId } : {}),
        }),
      },
    ));
  }

  async pluginMcpAuthorization(plugin: UserPluginCapability, connection: PluginMcpConnection, action: 'login' | 'status' | 'cancel' | 'logout') {
    return apiRequest<{state: string; error: string | null}>(
      `/api/capabilities/plugins/${encodeURIComponent(plugin.id)}/mcp/${encodeURIComponent(connection.serverId)}/authorization`,
      {method: 'POST', body: JSON.stringify({package_install_id: plugin.packageInstallId, action})},
    );
  }

  async removeUserPlugin(pluginId: string, packageInstallId: string, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/plugins/${encodeURIComponent(pluginId)}`,
      {
        method: 'DELETE',
        body: JSON.stringify({ package_install_id: packageInstallId, ...(activeSessionId ? { active_session_id: activeSessionId } : {}) }),
      },
    ));
  }

  async updatePluginConnection(plugin: UserPluginCapability, connection: PluginMcpConnection, input: PluginMcpEditInput, activeSessionId?: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/plugins/${encodeURIComponent(plugin.id)}/mcp/${encodeURIComponent(connection.serverId)}`,
      { method: 'PUT', body: JSON.stringify({ package_install_id: plugin.packageInstallId,
        expected_overlay: connection.overlay, overlay: input.overlay, secret_changes: input.secretChanges,
        retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false,
        ...(activeSessionId ? { active_session_id: activeSessionId } : {}) }) },
    ));
  }

  async openCapabilityRoot(root: 'agents' | 'pulsara'): Promise<void> {
    await apiRequest(`/api/capabilities/roots/${root}/open`, { method: 'POST' });
  }

  async forkConversation(sessionId: string, anchorEntryId: string, childSessionId: string): Promise<ForkOutcome> {
    return apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}/fork`, {
      method: 'POST', body: JSON.stringify({ anchor_entry_id: anchorEntryId, child_session_id: childSessionId }),
    });
  }

  async readSession(sessionId: string): Promise<SessionSummary | null> {
    const payload = await apiRequest<{ session: Record<string, unknown> | null }>(`/api/sessions/${encodeURIComponent(sessionId)}`);
    return payload.session ? projectSessionSummary(payload.session) : null;
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
  private liveResults = new Map<string, LiveToolResult>();
  private promptTransitions = new Map<string, LocalPromptSubmission>();
  private taskProgress = new Map<string, LiveTaskProgress>();
  private control: ProtocolCanonicalControl = {};
  private liveControl: ProtocolLiveControlSnapshot = {};
  private eventSequence = 0;
  private liveOwnerEpoch = 0;
  private liveRevision = 0;
  private liveControlOwnerEpoch = 0;
  private liveControlRevision = 0;
  private presentationNotices: string[] = [];
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
    await this.hydrateProjectionContent();
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
    await this.hydrateProjectionContent();
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
    this.presentationNotices = [...(observation.presentation_notices ?? [])];
    for (const committed of observation.committed ?? []) {
      if (committed.projection_kind === 'IMMUTABLE_ENTRY' && committed.entry) {
        this.entries.set(committed.entry.entry_id, committed.entry);
      } else if (committed.projection_kind === 'CURRENT_CONTROL' && committed.current_control) {
        this.control = committed.current_control;
      }
    }
    this.applyLive(observation.live ?? [], observation.settlements ?? []);
    for (const event of observation.live_control ?? []) {
      if (event.kind === 'LIVE_INTERACTION_CLOSED') {
        const previous = this.liveControl.current_interaction;
        const hasReason = (observation.live ?? []).some((live) =>
          live.event_type === 'INTERACTION_CLOSED'
          && live.payload?.interaction_closed?.interaction_id === previous?.interaction_id);
        if (previous?.interaction_kind === 'TOOL_CONFIRMATION' && !hasReason) {
          this.presentationNotices.push('这项确认已结束；原因暂不可确认。');
        }
        delete this.liveControl.current_interaction;
      }
      else if (event.interaction) this.liveControl.current_interaction = event.interaction;
    }
    this.eventSequence = numeric(observation.through_event_sequence ?? this.eventSequence);
    this.liveOwnerEpoch = numeric(observation.live_owner_epoch ?? this.liveOwnerEpoch);
    this.liveRevision = numeric(observation.through_live_revision ?? this.liveRevision);
    this.liveControlOwnerEpoch = numeric(observation.live_control_owner_epoch ?? this.liveControlOwnerEpoch);
    this.liveControlRevision = numeric(observation.through_live_control_revision ?? this.liveControlRevision);
    await this.hydrateProjectionContent();
    return this.project();
  }

  submitPrompt(
    commandId: string,
    content: EditablePromptContent,
    permission: PermissionMode,
  ): Promise<CommandReceipt> {
    return this.command('SUBMIT_PROMPT', {
      prompt_content: editablePromptToTransport(content),
      requested_permission_mode: protocolPermissionModes[permission],
    }, commandId);
  }

  cancelQueuedPrompt(commandId: string, queueItemId: string): Promise<CommandReceipt> {
    return this.command('CANCEL_QUEUED_PROMPT', { target_queue_item_id: queueItemId }, commandId);
  }

  steerQueuedPrompt(commandId: string, queueItemId: string, targetTurnId: string): Promise<CommandReceipt> {
    return this.command('STEER_QUEUED_PROMPT', { target_queue_item_id: queueItemId, target_turn_id: targetTurnId }, commandId);
  }

  stopActiveTurn(reference: UserControlCommandRef): Promise<CommandReceipt> {
    this.assertControlReference(reference, 'STOP_ACTIVE_TURN', 'ROOT_TURN');
    return this.command('STOP_ACTIVE_TURN', {
      client_submission_id: '',
      expected_session_id: reference.sessionId,
      expected_host_session_id: reference.hostSessionId,
      target_turn_id: reference.targetId,
    }, reference.commandId);
  }

  cancelSubagentTask(reference: UserControlCommandRef): Promise<CommandReceipt> {
    this.assertControlReference(reference, 'CANCEL_SUBAGENT_TASK', 'SUBAGENT_TASK');
    return this.controlCommand(reference, 'subagent_task_id');
  }

  terminateBackgroundProcess(reference: UserControlCommandRef): Promise<CommandReceipt> {
    this.assertControlReference(reference, 'TERMINATE_BACKGROUND_PROCESS', 'BACKGROUND_PROCESS');
    return this.controlCommand(reference, 'target_process_id');
  }

  async queryControlCommand(
    reference: UserControlCommandRef,
  ): Promise<UserControlQueryResult> {
    const operation = `USER_CONTROL_${reference.operation}`;
    const targetKind = `USER_CONTROL_${reference.targetKind}`;
    const frame = await this.post<{
      query_command?: {
        found?: boolean;
        control_query_status?: string;
        outcome?: ProtocolCommandOutcome;
      };
      error?: ProtocolError;
    }>('query-command', {
      command_id: reference.commandId,
      expected_control: {
        operation,
        session_id: reference.sessionId,
        host_session_id: reference.hostSessionId,
        target_kind: targetKind,
        target_id: reference.targetId,
      },
    });
    assertProtocolFrame(frame);
    const query = frame.query_command;
    if (!query) throw new RuntimeApiError(
      'CONTROL_QUERY_INVALID', '控制查询没有返回可识别的结果。', false,
    );
    if (query.control_query_status === 'CONTROL_QUERY_FOUND') {
      if (!query.found || !query.outcome) throw new RuntimeApiError(
        'CONTROL_QUERY_INVALID', '控制查询缺少已确认的操作结果。', false,
      );
      return { status: 'FOUND', receipt: projectCommand(query.outcome) };
    }
    if (query.control_query_status === 'CONTROL_QUERY_RESULT_UNAVAILABLE') {
      return { status: 'RESULT_UNAVAILABLE' };
    }
    if (query.control_query_status === 'CONTROL_QUERY_OWNER_UNAVAILABLE') {
      return { status: 'OWNER_UNAVAILABLE' };
    }
    throw new RuntimeApiError(
      'CONTROL_QUERY_INVALID', '控制查询返回了未知状态。', false,
    );
  }

  async listBackgroundProcesses(cursor?: string): Promise<BackgroundProcessPage> {
    const hostSessionId = this.liveControl.host_session_id ?? '';
    if (!hostSessionId) throw new RuntimeApiError(
      'BACKGROUND_OWNER_UNAVAILABLE', '后台命令 owner 尚未确认。', true,
    );
    const frame = await this.post<{
      background_processes?: {
        session_id?: string;
        host_session_id?: string;
        processes?: ProtocolBackgroundProcess[];
        next_cursor?: string;
      };
      error?: ProtocolError;
    }>('list-background-processes', {
      expected_session_id: this.sessionId,
      expected_host_session_id: hostSessionId,
      cursor: cursor ?? '',
      maximum_items: 50,
    });
    assertProtocolFrame(frame);
    const page = frame.background_processes;
    if (
      !page
      || page.session_id !== this.sessionId
      || page.host_session_id !== hostSessionId
    ) throw new RuntimeApiError(
      'BACKGROUND_OWNER_MISMATCH', '后台命令列表来自另一个会话。', true,
    );
    return {
      processes: (page.processes ?? []).map(projectBackgroundProcess),
      nextCursor: page.next_cursor || undefined,
    };
  }

  async readBackgroundProcessLog(
    processId: string,
    outputCursor?: string,
  ): Promise<BackgroundProcessLog> {
    const hostSessionId = this.liveControl.host_session_id ?? '';
    const frame = await this.post<{
      background_process_log?: {
        session_id?: string;
        host_session_id?: string;
        process?: ProtocolBackgroundProcess;
        output?: string;
        output_cursor?: string;
        retained_from_cursor?: string;
        gap_before_output?: boolean;
        truncated_by_response_bound?: boolean;
        source_coverage?: string;
      };
      error?: ProtocolError;
    }>('read-background-process-log', {
      expected_session_id: this.sessionId,
      expected_host_session_id: hostSessionId,
      process_id: processId,
      output_cursor: outputCursor ?? '',
      max_output_chars: 32_000,
    });
    assertProtocolFrame(frame);
    const value = frame.background_process_log;
    if (
      !value?.process
      || value.process.process_id !== processId
      || value.session_id !== this.sessionId
      || value.host_session_id !== hostSessionId
    ) throw new RuntimeApiError(
      'BACKGROUND_LOG_MISMATCH', '后台命令输出来自另一个目标。', true,
    );
    return {
      process: projectBackgroundProcess(value.process),
      output: value.output ?? '',
      outputCursor: value.output_cursor ?? '',
      retainedFromCursor: value.retained_from_cursor ?? '',
      gapBeforeOutput: Boolean(value.gap_before_output),
      truncatedByResponseBound: Boolean(value.truncated_by_response_bound),
      sourceCoverage: value.source_coverage ?? '',
    };
  }

  async readCanonicalEntryContent(
    entryId: string,
    digest: string,
    size: number,
  ): Promise<string> {
    if (!entryId || !digest.startsWith('sha256:') || size < 0) {
      throw new RuntimeApiError(
        'CONTENT_REFERENCE_INVALID', '这条任务活动缺少精确内容身份。', false,
      );
    }
    const reference: ProtocolContent = { digest, size };
    await this.hydrateContentReference(
      reference,
      { entry_id: entryId },
      '这条任务活动暂时无法完整读取。',
    );
    if (reference.inline_content === undefined) return '';
    return decodeContent(reference, { kind: 'entry', entryId });
  }

  async readPromptImage(image: CanonicalPromptImagePart): Promise<Uint8Array> {
    const target = image.owner.kind === 'entry'
      ? { entry_id: image.owner.entryId }
      : { queue_item_id: image.owner.queueItemId };
    return this.readExactContentBytes(
      {
        ...target,
        image_ref_ordinal: image.refOrdinal,
      },
      image.digest,
      image.encodedBytes,
      '这张图片暂时无法完整读取。',
      false,
    );
  }

  async readPromptForEdit(
    content: CanonicalPromptContent,
  ): Promise<EditablePromptContent> {
    const parts: Array<EditablePromptContent['parts'][number]> = [];
    for (const part of content.parts) {
      parts.push(part.type === 'text'
        ? { type: 'text' as const, text: part.text }
        : {
          type: 'image' as const,
          source: 'local' as const,
          bytes: await this.readPromptImage(part),
          declaredMediaType: part.mediaType,
        });
    }
    return { parts };
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
    return this.command('ENTER_PLAN', { plan_reason: reason, requested_permission_mode: protocolPermissionModes[permission] });
  }

  async readInteraction(
    interaction: RuntimeInteractionSummary,
  ): Promise<RuntimeInteractionContent> {
    if (interaction.kind === 'capability-form') {
      const response = await this.post<{form: Record<string, unknown>}>('read-capability-form', {
        interaction_id: interaction.id, expected_owner_epoch: this.liveControlOwnerEpoch,
        expected_live_revision: this.liveControlRevision,
      });
      return {kind: 'capability-form', form: response.form};
    }
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
        question: question.question ?? '',
        options: (question.options ?? []).map((option) => ({
          ordinal: numeric(option.ordinal),
          label: option.label ?? '',
          description: option.description ?? '',
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
    return { kind: 'plan-draft', body };
  }

  async resolveInteraction(
    interaction: RuntimeInteractionSummary,
    resolution: RuntimeInteractionSubmission,
  ): Promise<CommandReceipt | { submitted: boolean }> {
    const current = this.project().interaction;
    if (!current || current.id !== interaction.id || current.kind !== interaction.kind) {
      throw new RuntimeApiError('INTERACTION_STALE', '这项确认已经更新，请查看最新内容。', true);
    }
    if (interaction.kind === 'capability-form') {
      if (resolution.kind !== 'capability') throw new RuntimeApiError('INTERACTION_INVALID', '请提交配置或取消。', false);
      return this.post<{submitted: boolean}>('resolve-capability-form', {
        interaction_id: interaction.id, expected_owner_epoch: this.liveControlOwnerEpoch,
        expected_live_revision: this.liveControlRevision, decision: resolution.decision,
        submission: resolution.decision === 'SUBMIT' ? resolution.submission ?? {} : null,
      });
    }
    if (resolution.kind === 'capability') throw new RuntimeApiError('INTERACTION_INVALID', '当前不是能力配置表单。', false);
    if (interaction.kind === 'tool-confirmation') {
      if (resolution.kind !== 'tool') {
        throw new RuntimeApiError('INTERACTION_INVALID', '请选择是否允许这次操作。', false);
      }
      const frame = await this.post<{
        command_outcome?: ProtocolCommandOutcome;
        error?: ProtocolError;
      }>('resolve-interaction', {
        command_id: resolution.commandId,
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
    const commandId = `command:web:${crypto.randomUUID()}`;
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
        draft_decision?: string;
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
    if (resolution.kind === 'plan-draft') {
      const observedDecision = {
        PLAN_DRAFT_APPROVE: 'approve',
        PLAN_DRAFT_REVISE: 'revise',
        PLAN_DRAFT_CANCEL: 'cancel',
      }[outcome.draft_decision ?? ''];
      const continuationTurnId = outcome.continuation_turn_id || undefined;
      if (
        observedDecision !== resolution.decision
        || (resolution.decision !== 'cancel') !== Boolean(continuationTurnId)
      ) {
        throw new RuntimeApiError('INTERACTION_RESPONSE_INVALID', '规划结果与本次选择不一致。', true);
      }
      return {
        commandId,
        status: 'succeeded',
        planDraftDecision: resolution.decision,
        planContinuationTurnId: continuationTurnId,
      };
    }
    return {
      commandId,
      status: 'succeeded',
      targetId: outcome.continuation_turn_id || interaction.workflowId,
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

  async readToolArtifact(
    resultEntryId: string,
    offsetChars: number,
    maxChars = 32_000,
  ): Promise<ToolArtifactPage> {
    const frame = await this.post<{
      tool_artifact?: {
        result_entry_id?: string; text?: string; offset_chars?: string | number;
        returned_chars?: string | number; total_chars?: string | number;
        has_more?: boolean; next_offset_chars?: string | number;
      };
      error?: ProtocolError;
    }>('read-tool-artifact', {
      result_entry_id: resultEntryId, offset_chars: offsetChars, max_chars: maxChars,
    });
    assertProtocolFrame(frame);
    const page = frame.tool_artifact;
    if (!page || page.result_entry_id !== resultEntryId || numeric(page.offset_chars) !== offsetChars) {
      throw new RuntimeApiError('TOOL_ARTIFACT_RESPONSE_INVALID', '工具原始输出暂时无法读取。', true);
    }
    return {
      resultEntryId,
      text: page.text ?? '',
      offsetChars,
      returnedChars: numeric(page.returned_chars),
      totalChars: numeric(page.total_chars),
      hasMore: Boolean(page.has_more),
      nextOffsetChars: page.has_more ? numeric(page.next_offset_chars) : undefined,
    };
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
    suppliedCommandId?: string,
  ): Promise<CommandReceipt> {
    const commandId = suppliedCommandId ?? `command:web:${crypto.randomUUID()}`;
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

  private controlCommand(
    reference: UserControlCommandRef,
    targetField: 'subagent_task_id' | 'target_process_id',
  ): Promise<CommandReceipt> {
    return this.command(reference.operation, {
      client_submission_id: '',
      expected_session_id: reference.sessionId,
      expected_host_session_id: reference.hostSessionId,
      [targetField]: reference.targetId,
    }, reference.commandId);
  }

  private assertControlReference(
    reference: UserControlCommandRef,
    operation: UserControlCommandRef['operation'],
    targetKind: UserControlCommandRef['targetKind'],
  ): void {
    if (
      reference.operation !== operation
      || reference.targetKind !== targetKind
      || reference.sessionId !== this.sessionId
      || reference.hostSessionId !== (this.liveControl.host_session_id ?? '')
      || !reference.targetId
      || !reference.commandId.startsWith('command:control:')
    ) {
      throw new RuntimeApiError(
        'CONTROL_REFERENCE_INVALID',
        '控制操作的 owner 或目标身份不一致。',
        false,
      );
    }
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

  private async hydrateProjectionContent(): Promise<void> {
    while (true) {
      await this.hydrateCanonicalContent();
      const transitioned = await this.hydrateQueuedPromptContent();
      if (!transitioned) return;
      await this.refreshCanonicalAfterQueueTransition();
      await this.reconcilePromptQueueTransition(transitioned);
    }
  }

  private async refreshCanonicalAfterQueueTransition(): Promise<void> {
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
  }

  private async hydrateCanonicalContent(): Promise<void> {
    for (const entry of this.entries.values()) {
      await this.hydrateContentReference(
        entry.content,
        { entry_id: entry.entry_id },
        '这条会话内容暂时无法完整读取。',
      );
      for (const block of entry.blocks ?? []) {
        await this.hydrateContentReference(
          block.content,
          { entry_id: entry.entry_id, block_id: block.block_id },
          '这条模型回复暂时无法完整读取。',
        );
      }
      for (const block of entry.reasoning_blocks ?? []) {
        await this.hydrateContentReference(
          block.content,
          { entry_id: entry.entry_id, block_id: block.block_id },
          '这段思考内容暂时无法完整读取。',
        );
      }
    }
  }

  private async hydrateQueuedPromptContent(): Promise<
    NonNullable<ProtocolCanonicalControl['prompt_queue']>[number] | undefined
  > {
    for (const item of this.control.prompt_queue ?? []) {
      if (!item.queue_item_id) {
        throw new RuntimeApiError(
          'CONTENT_REFERENCE_MISSING',
          '等待处理的输入缺少精确身份。',
          true,
        );
      }
      try {
        await this.hydrateContentReference(
          item.content,
          { queue_item_id: item.queue_item_id },
          '等待处理的输入暂时无法完整读取。',
        );
      } catch (error) {
        if (error instanceof RuntimeApiError && error.code === 'CONTENT_QUEUE_NOT_PENDING') {
          return item;
        }
        throw error;
      }
    }
    return undefined;
  }

  private async reconcilePromptQueueTransition(
    item: NonNullable<ProtocolCanonicalControl['prompt_queue']>[number],
  ): Promise<void> {
    const commandId = item.command_id ?? '';
    const queueItemId = item.queue_item_id ?? '';
    if (!commandId || !queueItemId) {
      throw new RuntimeApiError(
        'PROMPT_TRANSITION_IDENTITY_INVALID',
        '等待处理的输入缺少可核对的身份。',
        true,
      );
    }
    const consumed = [...this.entries.values()].some((entry) => (
      entry.input_source?.command_id === commandId
      && entry.input_source?.queue_item_id === queueItemId
    ));
    const pending = (this.control.prompt_queue ?? []).some((candidate) => (
      candidate.command_id === commandId
      && candidate.queue_item_id === queueItemId
      && candidate.status === 'PENDING'
    ));
    if (consumed || pending) {
      this.promptTransitions.delete(commandId);
      return;
    }

    const receipt = await this.queryCommand(commandId);
    if (!receipt) {
      this.promptTransitions.set(commandId, this.queueTransitionSubmission(
        item,
        'unknown',
        undefined,
        '本地服务尚未返回这条输入的最终状态。',
      ));
      return;
    }
    if (
      receipt.commandId !== commandId
      || receipt.promptDelivery?.queueItemId !== queueItemId
    ) {
      throw new RuntimeApiError(
        'PROMPT_TRANSITION_IDENTITY_INVALID',
        '等待处理的输入返回了不一致的身份。',
        true,
      );
    }
    const queueStatus = receipt.promptDelivery.queueStatus.toUpperCase();
    const status: LocalPromptSubmission['status'] = queueStatus === 'CONSUMED'
      ? 'consumed'
      : queueStatus === 'CANCELLED'
        ? 'cancelled'
        : queueStatus === 'REJECTED'
          ? 'rejected'
          : 'unknown';
    this.promptTransitions.set(commandId, this.queueTransitionSubmission(
      item,
      status,
      receipt.publicCode,
      receipt.publicMessage,
      receipt.promptDelivery.consumedEntryId,
    ));
  }

  private queueTransitionSubmission(
    item: NonNullable<ProtocolCanonicalControl['prompt_queue']>[number],
    status: LocalPromptSubmission['status'],
    outcomeCode?: string,
    detail?: string,
    consumedEntryId?: string,
  ): LocalPromptSubmission {
    const hasInlineBody = item.content?.inline_content !== undefined;
    const queueItemId = item.queue_item_id!;
    return {
      sessionId: this.sessionId,
      connectionGeneration: this.generation,
      commandId: item.command_id!,
      queueItemId,
      consumedEntryId,
      content: hasInlineBody
        ? decodePromptContent(item.content, { kind: 'queue', queueItemId })
        : undefined,
      contentUnavailable: !hasInlineBody,
      deliveryMode: deliveryMode(item.delivery_mode),
      targetTurnId: item.target_turn_id || undefined,
      permission: protocolPermission(item.permission?.effective_mode),
      observedPending: true,
      status,
      outcomeCode,
      detail,
    };
  }

  private async hydrateContentReference(
    reference: ProtocolContent | undefined,
    target: { entry_id: string; block_id?: string } | { queue_item_id: string },
    publicMessage: string,
  ): Promise<void> {
    if (!reference || reference.inline_content !== undefined || numeric(reference.size) === 0) return;
    const expectedSize = numeric(reference.size);
    const expectedDigest = reference.digest ?? '';
    const complete = await this.readExactContentBytes(
      target,
      expectedDigest,
      expectedSize,
      publicMessage,
      true,
    );
    reference.inline_content = encodeBase64Bytes(complete);
  }

  private async readExactContentBytes(
    target: (
      | { entry_id: string; block_id?: string }
      | { queue_item_id: string }
    ) & { image_ref_ordinal?: number },
    expectedDigest: string,
    expectedSize: number,
    publicMessage: string,
    requireUtf8: boolean,
  ): Promise<Uint8Array> {
    const chunks: Uint8Array[] = [];
    let offset = 0;
    while (true) {
      const frame = await this.post<{
        content?: {
          digest?: string; complete_size?: string | number; offset_bytes?: string | number;
          content?: string; complete?: boolean;
        };
        error?: ProtocolError;
      }>('read-content', {
        ...target,
        offset_bytes: offset,
        limit_bytes: 1 << 20,
      });
      assertProtocolFrame(frame);
      const chunk = frame.content;
      if (
        !chunk || chunk.digest !== expectedDigest
        || numeric(chunk.complete_size) !== expectedSize
        || numeric(chunk.offset_bytes) !== offset
      ) {
        throw new RuntimeApiError('CONTENT_REFERENCE_INVALID', publicMessage, true);
      }
      const bytes = decodeBase64Bytes(chunk.content ?? '');
      chunks.push(bytes);
      offset += bytes.length;
      if (chunk.complete) break;
      if (bytes.length === 0 || offset >= expectedSize) {
        throw new RuntimeApiError('CONTENT_REFERENCE_INVALID', publicMessage, true);
      }
    }
    if (offset !== expectedSize) {
      throw new RuntimeApiError('CONTENT_REFERENCE_INVALID', publicMessage, true);
    }
    const complete = new Uint8Array(expectedSize);
    let cursor = 0;
    for (const chunk of chunks) {
      complete.set(chunk, cursor);
      cursor += chunk.length;
    }
    await verifyContentIntegrity(complete, expectedDigest, publicMessage, requireUtf8);
    return complete;
  }

  private applyLive(events: ProtocolLiveEvent[], settlements: ProtocolSettlement[]) {
    for (const event of events) {
      const payload = event.payload ?? {};
      if (event.event_type === 'INTERACTION_CLOSED') {
        const closed = payload.interaction_closed ?? {};
        if (closed.interaction_id === this.liveControl.current_interaction?.interaction_id) {
          const reasons: Record<string, string> = {
            'interaction:expired': '确认已过期，本次操作未获授权。',
            'interaction:turn-cancelled': '原任务已停止，这项待确认操作已结束。',
            'interaction:host-closing': '运行环境已关闭，原确认已结束。',
            'interaction:mcp-config-changed': '操作条件已变化，这项确认已失效。',
            'interaction:admission-rejected': '操作条件已变化，这项确认已失效。',
            'interaction:outcome-unknown': '运行已中断，操作结果待核实。',
          };
          if (String(closed.reason) !== 'RESOLVED') this.presentationNotices.push(
            reasons[String(closed.reason)] ?? '这项确认已结束；原因暂不可确认。',
          );
        }
        continue;
      }
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
            summary: String(progress.public_summary ?? ''),
          });
        }
        continue;
      }
      if (event.event_type.startsWith('TOOL_RESULT_')) {
        this.applyLiveToolResult(event, payload);
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
        const trace = current.traces.find((candidate) => candidate.id === toolCallId);
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
      if (
        event.event_type.startsWith('TEXT_')
        || event.event_type.startsWith('THINKING_')
        || event.event_type.startsWith('TOOL_')
      ) {
        this.drafts.set(identity, current);
      }
    }
    this.reconcileLiveToolResults();
    for (const settlement of settlements) {
      const identity = settlement.draft_identity || settlement.generation_id;
      if (settlement.channel_kind?.includes('TOOL_RESULT')) {
        for (const [key, result] of this.liveResults) {
          if (this.liveResultMatches(result, settlement)) this.liveResults.delete(key);
        }
      } else if (identity) {
        this.drafts.delete(identity);
      }
    }
  }

  private liveResultStableKey(event: ProtocolLiveEvent): string | undefined {
    const exactChannel = event.channel_attempt_id || event.proposed_entry_id;
    const fallbackParts = [
        event.scope_kind ?? '', event.scope_subagent_task_id ?? '', event.turn_id ?? '',
        event.draft_identity ?? event.generation_id ?? '', event.block_id ?? '',
        event.channel_tool_call_id ?? '',
    ];
    const channelIdentity = exactChannel || (fallbackParts.some(Boolean) ? fallbackParts.join(':') : '');
    return channelIdentity ? `live-result:${channelIdentity}` : undefined;
  }

  private reconcileLiveToolResults(): void {
    for (const result of this.liveResults.values()) {
      const attempt = result.attemptId
        ? (this.control.tool_attempts ?? []).find((item) => item.attempt_id === result.attemptId)
        : undefined;
      const proposed = result.proposedEntryId ? this.entries.get(result.proposedEntryId) : undefined;
      const assistantEntryId = attempt?.assistant_entry_id ?? proposed?.tool_result?.assistant_entry_id;
      const toolCallId = attempt?.tool_call_id ?? proposed?.tool_result?.tool_call_id;
      if (assistantEntryId) result.assistantEntryId = assistantEntryId;
      if (toolCallId) result.toolCallId = toolCallId;
    }
  }

  private liveResultMatches(result: LiveToolResult, event: ProtocolSettlement): boolean {
    if (event.channel_attempt_id && result.attemptId === event.channel_attempt_id) return true;
    if (event.proposed_entry_id && result.proposedEntryId === event.proposed_entry_id) return true;
    if (event.draft_identity && result.draftIdentity === event.draft_identity) return true;
    if (event.generation_id && result.generationId === event.generation_id) return true;
    const attempt = event.channel_attempt_id
      ? (this.control.tool_attempts ?? []).find((item) => item.attempt_id === event.channel_attempt_id)
      : undefined;
    const proposed = event.proposed_entry_id ? this.entries.get(event.proposed_entry_id) : undefined;
    const assistantEntryId = attempt?.assistant_entry_id ?? proposed?.tool_result?.assistant_entry_id;
    const toolCallId = event.channel_tool_call_id
      || attempt?.tool_call_id
      || proposed?.tool_result?.tool_call_id;
    return Boolean(
      assistantEntryId && toolCallId
      && result.assistantEntryId === assistantEntryId
      && result.toolCallId === toolCallId,
    );
  }

  private applyLiveToolResult(event: ProtocolLiveEvent, payload: Record<string, Record<string, unknown>>) {
    const item = event.event_type === 'TOOL_RESULT_START'
      ? payload.tool_result_start ?? {}
      : event.event_type === 'TOOL_RESULT_DELTA'
        ? payload.tool_result_delta ?? {}
        : payload.tool_result_end ?? {};
    const payloadCallId = String(item.tool_call_id ?? '');
    const payloadAttemptId = String(item.attempt_id ?? '');
    const augmented = {
      ...event,
      channel_tool_call_id: event.channel_tool_call_id || payloadCallId || undefined,
      channel_attempt_id: event.channel_attempt_id || payloadAttemptId || undefined,
    };
    const key = this.liveResultStableKey(augmented);
    if (!key) return;
    const attempt = augmented.channel_attempt_id
      ? (this.control.tool_attempts ?? []).find((candidate) => candidate.attempt_id === augmented.channel_attempt_id)
      : undefined;
    const proposed = augmented.proposed_entry_id ? this.entries.get(augmented.proposed_entry_id) : undefined;
    const matched = [...this.liveResults.entries()].find(([, candidate]) => (
      (augmented.channel_attempt_id && candidate.attemptId === augmented.channel_attempt_id)
      || (augmented.proposed_entry_id && candidate.proposedEntryId === augmented.proposed_entry_id)
      || (augmented.draft_identity && candidate.draftIdentity === augmented.draft_identity)
      || (augmented.generation_id && candidate.generationId === augmented.generation_id)
    ));
    const current = matched?.[1] ?? this.liveResults.get(key) ?? {
      id: key,
      turnId: augmented.turn_id ?? '',
      scopeKind: augmented.scope_kind ?? '',
      taskId: augmented.scope_subagent_task_id ?? '',
      draftIdentity: augmented.draft_identity,
      generationId: augmented.generation_id,
      blockId: augmented.block_id,
      assistantEntryId: attempt?.assistant_entry_id ?? proposed?.tool_result?.assistant_entry_id,
      toolCallId: augmented.channel_tool_call_id || attempt?.tool_call_id || proposed?.tool_result?.tool_call_id,
      attemptId: augmented.channel_attempt_id,
      proposedEntryId: augmented.proposed_entry_id,
      text: '', hasText: false, ended: false,
    };
    if (matched && matched[0] !== key) this.liveResults.delete(matched[0]);
    current.draftIdentity ||= augmented.draft_identity;
    current.generationId ||= augmented.generation_id;
    current.blockId ||= augmented.block_id;
    current.attemptId ||= augmented.channel_attempt_id;
    current.proposedEntryId ||= augmented.proposed_entry_id;
    current.assistantEntryId ||= attempt?.assistant_entry_id ?? proposed?.tool_result?.assistant_entry_id;
    current.toolCallId ||= augmented.channel_tool_call_id || attempt?.tool_call_id || proposed?.tool_result?.tool_call_id;
    if (event.event_type === 'TOOL_RESULT_DELTA') {
      current.text += String(item.text ?? '');
      current.hasText = true;
    } else if (event.event_type === 'TOOL_RESULT_END') {
      current.resultState = String(item.result_state ?? '').toUpperCase();
      current.text = Object.prototype.hasOwnProperty.call(item, 'final_text')
        ? String(item.final_text ?? '')
        : current.text;
      current.hasText = true;
      current.ended = true;
    }
    this.liveResults.set(key, current);
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
    this.reconcileLiveToolResults();
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
      activeTurnIds,
    );
    const canonicalRootTurnIds = orderedRootTurnIdsFromEntries(canonical);
    annotateSubagentCompletionSources(
      messages,
      agentTasks,
      canonicalRootTurnIds,
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
        agentTasks,
        visibleDrafts,
      ),
    );
    this.applyProjectedLiveResults(messages, activeTurnIds);
    const hasActiveDraft = visibleDrafts.length > 0;
    const activeTurn = (this.control.active_turns ?? []).find(
      (turn) => turn.scope_kind !== 'SUBAGENT_TASK',
    );
    const todo = projectTodo(this.liveControl);
    const interaction = projectInteraction(this.liveControl, this.control);
    return {
      messages,
      canonicalRootTurnIds,
      presentationNotices: this.presentationNotices,
      contextCompaction: projectContextCompaction(this.control),
      initialContextBase: this.control.initial_context_base,
      isRunning: Boolean(activeTurn) || hasActiveDraft,
      queuedCount: numeric(this.control.prompt_queue_total_count),
      queuedPrompts: projectQueuedPrompts(this.control),
      promptTransitions: this.projectPromptTransitions(),
      planMode: Boolean(
        this.control.active_plan_workflow
        && Object.keys(this.control.active_plan_workflow).length,
      ),
      activeTurnId: this.liveControl.active_root_turn_id || activeTurn?.turn_id,
      hostSessionId: this.liveControl.host_session_id || undefined,
      controlAdmissionDeadlineMs: numeric(
        this.liveControl.control_admission_deadline_ms,
      ) || undefined,
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

  private projectPromptTransitions(): LocalPromptSubmission[] {
    const consumedCommands = new Set([...this.entries.values()].flatMap((entry) => (
      entry.input_source?.command_id ? [entry.input_source.command_id] : []
    )));
    const pendingCommands = new Set((this.control.prompt_queue ?? []).flatMap((item) => (
      item.status === 'PENDING' && item.command_id ? [item.command_id] : []
    )));
    for (const commandId of this.promptTransitions.keys()) {
      if (consumedCommands.has(commandId) || pendingCommands.has(commandId)) {
        this.promptTransitions.delete(commandId);
      }
    }
    return [...this.promptTransitions.values()];
  }

  private applyProjectedLiveResults(messages: Message[], activeTurnIds: Set<string>) {
    const traceTargets = new Map<string, ToolTrace>();
    for (const message of messages) {
      for (const trace of message.traces ?? []) traceTargets.set(`${message.id}:${trace.id}`, trace);
      for (const run of message.subagentRuns ?? []) {
        for (const activity of run.activities) {
          for (const trace of activity.traces ?? []) traceTargets.set(`${activity.id}:${trace.id}`, trace);
        }
      }
    }
    for (const result of this.liveResults.values()) {
      const trace = result.assistantEntryId && result.toolCallId
        ? traceTargets.get(`${result.assistantEntryId}:${result.toolCallId}`)
        : undefined;
      if (trace && !trace.resultEntryId) {
        if (result.hasText) trace.resultText = result.text;
        trace.resultState = result.resultState;
        trace.status = result.ended ? toolResultStatus(result.resultState) : 'running';
        trace.subtitle = result.ended
          ? trace.status === 'completed' ? '已完成' : toolFailureLabel(result.resultState)
          : '正在接收结果';
        continue;
      }
      if (trace || !activeTurnIds.has(result.turnId)) continue;
      const detachedTrace: ToolTrace = {
        id: result.id,
        kind: 'artifact',
        title: '操作结果',
        subtitle: '调用信息尚未加载',
        status: result.ended ? toolResultStatus(result.resultState) : 'running',
        associationPending: true,
        resultState: result.resultState,
        ...(result.hasText ? { resultText: result.text } : {}),
      };
      if (result.taskId) {
        const run = messages.flatMap((message) => message.subagentRuns ?? [])
          .find((candidate) => candidate.id === result.taskId);
        if (run) {
          run.activities.push({
            id: `live-result:${result.id}`, time: '现在', body: '', status: 'running', traces: [detachedTrace],
          });
        }
      } else {
        messages.push({
          id: `live-result:${result.id}`, turnId: result.turnId, role: 'assistant',
          assistantKind: 'tool-request', time: '现在', body: '', status: 'running', traces: [detachedTrace],
        });
      }
    }
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
    credentialScopeKey: typeof value.credential_scope_key === 'string' ? value.credential_scope_key : undefined,
    adoption: {
      scope: 'workspace',
      pending: Boolean(adoption.pending),
      attention: adoption.attention === 'PROJECT_CAPABILITY_ADOPTION_FAILED'
        || adoption.attention === 'PROJECT_MCP_ADOPTION_INCOMPLETE'
        ? adoption.attention
        : undefined,
      when: 'next-provider-dispatch',
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
        removalIdentity: projectSkillRemovalIdentity(item.removal_identity),
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
          config: asRecord(server.config),
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
    when: 'next-provider-dispatch',
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
        removalIdentity: projectSkillRemovalIdentity(item.removal_identity),
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
          config: asRecord(server.config),
          currentIdentity: String(server.current_identity ?? ''),
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
        mcpConnections: recordArray(item.mcp_connections).map((connection) => ({
          serverId: String(connection.server_id), defaults: asRecord(connection.defaults), config: asRecord(connection.config),
          overlay: connection.overlay === null ? null : asRecord(connection.overlay),
          credentialOwner: asRecord(connection.credential_owner) as unknown as PluginMcpConnection['credentialOwner'],
          connectionInputs: recordArray(connection.connection_inputs) as NonNullable<PluginMcpConnection['connectionInputs']>,
        })),
        connectionReview: recordArray(item.connection_review) as NonNullable<UserPluginCapability['connectionReview']>,
        effectiveSkillNames: stringArray(item.effective_skill_names),
        effectiveMcpServerIds: stringArray(item.effective_mcp_server_ids),
        details: stringArray(item.details),
      })),
      details: stringArray(plugins.details),
    },
    adoption: Object.keys(adoption).length > 0 ? {
      updatedSessions: numeric(adoption.updated_sessions),
      pendingSessions: numeric(adoption.pending_sessions),
      attentionSessions: numeric(adoption.attention_sessions),
    } : undefined,
  };
}

function projectSkillRemovalIdentity(value: unknown): UserSkillCapability['removalIdentity'] {
  const raw = asRecord(value);
  const names = ['root_device', 'root_inode', 'directory_device', 'directory_inode'] as const;
  if (names.some((name) => typeof raw[name] !== 'string' || !/^\d+$/.test(raw[name] as string))) return undefined;
  return Object.fromEntries(names.map((name) => [name, raw[name]])) as NonNullable<UserSkillCapability['removalIdentity']>;
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
    capabilities: projectUserCapabilitySnapshot({
      ...asRecord(value.capabilities),
      ...(value.adoption === undefined ? {} : { adoption: value.adoption }),
    }),
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

function projectModelCallBinding(value: unknown): SessionSummary['modelCallBinding'] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const binding = value as Record<string, unknown>;
  if (typeof binding.connection_id !== 'string') return null;
  const raw = binding.reasoning;
  let reasoning: ModelCallBindingPayload['reasoning'] = null;
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    const selection = raw as Record<string, unknown>;
    if (selection.kind === 'effort' && (selection.value === null || typeof selection.value === 'string')) {
      reasoning = { kind: 'effort', value: selection.value as string | null };
    } else if (selection.kind === 'toggle' && typeof selection.enabled === 'boolean') {
      reasoning = { kind: 'toggle', enabled: selection.enabled };
    } else if (selection.kind === 'budget_tokens' && typeof selection.tokens === 'number') {
      reasoning = { kind: 'budget_tokens', tokens: selection.tokens };
    } else {
      return null;
    }
  } else if (raw !== null) {
    return null;
  }
  return { connection_id: binding.connection_id, reasoning };
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
    modelCallBinding: projectModelCallBinding(value.model_call_binding),
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
  activeTurnIds: ReadonlySet<string>,
): Message[] {
  const messages: Message[] = [];
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
      const promptContent = entry.entry_kind === 'PLAN_CONTINUATION'
        ? undefined
        : decodePromptContent(entry.content, { kind: 'entry', entryId: entry.entry_id });
      messages.push({
        id: entry.entry_id,
        turnId: entry.turn_id,
        entrySequence: numeric(entry.entry_sequence),
        role: 'user',
        userKind,
        time: formatTime(entry.accepted_at_utc),
        body: entry.entry_kind === 'PLAN_CONTINUATION'
          ? projectPlanContinuation(decodeContent(entry.content))
          : promptContentTextProjection(promptContent!),
        promptContent,
        inputSource: entry.input_source?.queue_item_id && entry.input_source.command_id
          ? {
            queueItemId: entry.input_source.queue_item_id,
            commandId: entry.input_source.command_id,
            deliveryMode: deliveryMode(entry.input_source.delivery_mode),
          }
          : undefined,
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
      messages.push({
        id: entry.entry_id,
        turnId: entry.turn_id,
        entrySequence: numeric(entry.entry_sequence),
        role: 'assistant',
        assistantKind: entry.entry_kind === 'ASSISTANT_MESSAGE' ? 'terminal' : 'tool-request',
        forkEligible: entry.fork_eligible === true,
        entryOwnerKind: entry.entry_owner_kind,
        time: formatTime(entry.accepted_at_utc),
        body: text || (entry.entry_kind === 'ASSISTANT_MESSAGE' ? decodeContent(entry.content) : ''),
        reasoning: reasoning.length ? reasoning : undefined,
        status: 'completed',
        traces: traces.length ? traces : undefined,
      });
      continue;
    }
    if (entry.entry_kind === 'TOOL_RESULT') {
      const resultRef = entry.tool_result;
      const projectedResult = decodeToolResultContent(entry);
      const target = resultRef
        ? messages.find((message) => message.id === resultRef.assistant_entry_id)
        : undefined;
      const pendingTrace = target?.traces?.find((item) => item.id === resultRef?.tool_call_id);
      if (pendingTrace) {
        const result = projectedResult.text;
        const resultState = resultRef?.result_state;
        const succeeded = resultState === 'SUCCESS';
        const cancelled = resultState === 'CANCELLED'
          || resultState === 'CANCELLED_BEFORE_DISPATCH';
        pendingTrace.status = succeeded ? 'completed' : cancelled ? 'cancelled' : 'failed';
        pendingTrace.subtitle = succeeded ? '已完成' : toolFailureLabel(resultState);
        pendingTrace.resultText = result;
        pendingTrace.resultContent = projectedResult.content;
        pendingTrace.resultEntryId = entry.entry_id;
        pendingTrace.resultState = resultState;
        pendingTrace.resultSummary = summarizeToolResult(pendingTrace.toolName, result);
        pendingTrace.artifact = projectToolArtifact(resultRef);
        pendingTrace.meta = succeeded ? '操作完成' : cancelled ? '操作已取消' : '操作未完成';
        continue;
      }
      const fallbackTrace: ToolTrace = {
        id: entry.entry_id,
        kind: 'artifact',
        title: '操作结果',
        subtitle: '已记录',
        status: toolResultStatus(resultRef?.result_state),
        resultText: projectedResult.text,
        resultContent: projectedResult.content,
        resultEntryId: entry.entry_id,
        resultState: resultRef?.result_state,
        resultSummary: summarizeToolResult(undefined, projectedResult.text),
        artifact: projectToolArtifact(resultRef),
        associationPending: true,
      };
      messages.push({
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
        resultText: decodeContent(entry.content),
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
  tasks: AgentTask[],
  drafts: LiveDraft[],
): SubagentRun[] {
  const taskById = new Map(tasks.map((task) => [task.id, task]));
  const runs = new Map<string, SubagentRun>();
  const order = new Map<string, number>();
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
    const projectedToolResult = entry.entry_kind === 'TOOL_RESULT'
      ? decodeToolResultContent(entry)
      : undefined;
    const content = entry.entry_kind === 'USER_MESSAGE'
      ? decodeContent(entry.content, { kind: 'entry', entryId: entry.entry_id })
      : projectedToolResult?.text ?? decodeContent(entry.content);
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
      const resultRef = entry.tool_result;
      const target = resultRef
        ? run.activities.find((activity) => activity.id === resultRef.assistant_entry_id)
        : undefined;
      const pendingTrace = target?.traces?.find((item) => item.id === resultRef?.tool_call_id);
      if (pendingTrace) {
        const resultState = resultRef?.result_state;
        const succeeded = resultState === 'SUCCESS';
        const cancelled = resultState === 'CANCELLED'
          || resultState === 'CANCELLED_BEFORE_DISPATCH';
        pendingTrace.status = succeeded ? 'completed' : cancelled ? 'cancelled' : 'failed';
        pendingTrace.subtitle = succeeded ? '已完成' : toolFailureLabel(resultState);
        pendingTrace.resultText = content;
        pendingTrace.resultContent = projectedToolResult?.content;
        pendingTrace.resultEntryId = entry.entry_id;
        pendingTrace.resultState = resultState;
        pendingTrace.resultSummary = summarizeToolResult(pendingTrace.toolName, content);
        pendingTrace.artifact = projectToolArtifact(resultRef);
        pendingTrace.meta = succeeded ? '操作完成' : cancelled ? '操作已取消' : '操作未完成';
      } else {
        // The request can be outside a paginated history window. Keep this
        // result visible without assigning it to a different tool call.
        run.activities.push({
          id: entry.entry_id, time: formatTime(entry.accepted_at_utc), body: '', status: 'completed',
          traces: [{
            id: entry.entry_id, kind: 'artifact', title: '操作结果', subtitle: '已记录',
            status: toolResultStatus(resultRef?.result_state),
            resultText: content, resultContent: projectedToolResult?.content,
            resultEntryId: entry.entry_id,
            resultState: resultRef?.result_state,
            resultSummary: summarizeToolResult(undefined, content),
            artifact: projectToolArtifact(resultRef), associationPending: true,
          }],
        });
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
        resultText: content,
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

  for (const run of runs.values()) {
    if (['pending', 'running', 'waiting'].includes(run.status)) continue;
    for (const activity of run.activities) {
      for (const trace of activity.traces ?? []) settleHistoricalTrace(trace);
    }
  }

  return [...runs.values()].sort(
    (left, right) => (order.get(left.id) ?? 0) - (order.get(right.id) ?? 0),
  );
}

function settleHistoricalTrace(trace: ToolTrace): void {
  if (trace.status !== 'running') return;
  trace.status = 'cancelled';
  trace.subtitle = '已结束';
  trace.meta = '结果待核实';
  trace.resultSummary = '原运行已结束，操作结果尚未记录；需要核实实际状态。';
}

function createsSubagentTasks(trace: ToolTrace): boolean {
  const name = trace.toolName?.toLowerCase() ?? '';
  return name === 'create_agent_tasks' || name === 'spawn_agent';
}

function createdSubagentTaskIds(trace: ToolTrace): ReadonlySet<string> | undefined {
  if (!createsSubagentTasks(trace)) return new Set();
  if (trace.resultState === undefined) return undefined;
  if (trace.resultState !== 'SUCCESS') return new Set();
  if (!trace.resultText) return undefined;
  try {
    const result = JSON.parse(trace.resultText) as Record<string, unknown>;
    if (trace.toolName?.toLowerCase() === 'spawn_agent') {
      return new Set(typeof result.task_id === 'string' ? [result.task_id] : []);
    }
    if (!Array.isArray(result.tasks)) return new Set();
    return new Set(result.tasks.flatMap((task) => {
      if (!task || typeof task !== 'object' || Array.isArray(task)) return [];
      const taskId = (task as Record<string, unknown>).task_id;
      return typeof taskId === 'string' ? [taskId] : [];
    }));
  } catch {
    return undefined;
  }
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
    const creators = messages.flatMap((message) => {
      if (message.role !== 'assistant' || message.turnId !== parentId) return [];
      return (message.traces ?? [])
        .filter(createsSubagentTasks)
        .map((trace) => ({ message, taskIds: createdSubagentTaskIds(trace) }));
    });
    for (const run of group) {
      const exact = creators.find((creator) => creator.taskIds?.has(run.id));
      const unresolvedMessages = [...new Set(
        creators
          .filter((creator) => creator.taskIds === undefined)
          .map((creator) => creator.message),
      )];
      const target = exact?.message
        ?? (unresolvedMessages.length === 1 ? unresolvedMessages[0] : undefined);
      if (!target) continue;
      target.subagentRuns = [...(target.subagentRuns ?? []), run];
    }
  }
}

function summarizeToolResult(toolName: string | undefined, content: string): string | undefined {
  if (!content) return undefined;
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    const planControl = String(value.plan_control ?? '');
    if (planControl === 'QUESTION_ANSWERED') return '已记录你的选择。';
    if (planControl === 'DRAFT_SUBMITTED_FOR_REVIEW') return '方案已提交，等待你的确认。';
    if (toolName === 'edit_file' && typeof value.diff === 'string' && value.diff) return value.diff;
    const path = typeof value.path === 'string' ? value.path : '';
    if (toolName === 'write_file' && path && typeof value.bytes_written === 'number') {
      return `已创建 ${path} · ${value.bytes_written} 字节`;
    }
    if (toolName === 'read_file' && path && typeof value.total_lines === 'number') {
      const offset = typeof value.offset === 'number' ? value.offset : 1;
      const returnedLines = typeof value.content === 'string' && value.content
        ? value.content.split('\n').length
        : 0;
      const window = returnedLines
        ? `返回第 ${offset}–${offset + returnedLines - 1} 行`
        : '本次窗口为空';
      return `${path} · ${window} · 文件共 ${value.total_lines} 行${value.truncated ? ' · 还有后续内容' : ''}`;
    }
    const message = value.message ?? value.error;
    if (typeof message === 'string' && message) return message;
    for (const key of ['output', 'stdout', 'stderr', 'content', 'result', 'text', 'summary']) {
      const field = value[key];
      if (typeof field === 'string' && field) return field;
    }
    if (toolName === 'terminal' || toolName === 'terminal_process') {
      const nestedProcess = asRecord(value.process);
      const process = Object.keys(nestedProcess).length ? nestedProcess : value;
      const status = String(process.status ?? '').toLowerCase();
      const exitCode = typeof process.exit_code === 'number' ? process.exit_code : undefined;
      if (status === 'running') {
        return toolName === 'terminal' ? '命令仍在后台运行。' : '命令仍在运行。';
      }
      if (status === 'blocked') return '命令未获准执行。';
      if (status === 'killed') return '命令已停止。';
      if (status === 'timeout' || process.timed_out === true) return '命令已超时。';
      if (exitCode === -1) return status === 'error' ? '命令执行失败。' : '命令退出状态尚未确定。';
      if (exitCode !== undefined) return `命令已结束，退出码 ${exitCode}。`;
    }
    return undefined;
  } catch {
    return undefined;
  }
}

function toolResultStatus(state?: string): ToolTrace['status'] {
  if (state === 'SUCCESS') return 'completed';
  if (state === 'CANCELLED' || state === 'CANCELLED_BEFORE_DISPATCH') return 'cancelled';
  return state ? 'failed' : 'running';
}

function projectToolArtifact(
  value?: ProtocolEntry['tool_result'],
): ToolTrace['artifact'] | undefined {
  const disposition = value?.artifact_disposition;
  const coverage = value?.source_coverage;
  const display = value?.display_kind;
  if (
    !['NOT_REQUIRED', 'AVAILABLE', 'INCOMPLETE', 'UNAVAILABLE'].includes(disposition ?? '')
    || !['COMPLETE', 'RETAINED_SNAPSHOT'].includes(coverage ?? '')
    || !['COMPLETE', 'HEAD_TAIL'].includes(display ?? '')
  ) return undefined;
  return {
    disposition: disposition as NonNullable<ToolTrace['artifact']>['disposition'],
    sourceCoverage: coverage as NonNullable<ToolTrace['artifact']>['sourceCoverage'],
    displayKind: display as NonNullable<ToolTrace['artifact']>['displayKind'],
    sourceCoverageReason: value?.source_coverage_reason || undefined,
    unavailabilityReason: value?.artifact_unavailability_reason || undefined,
  };
}

function deliveryMode(value?: string): 'new-turn' | 'steer' {
  return value === 'STEER_ACTIVE_TURN' ? 'steer' : 'new-turn';
}

function projectQueuedPrompts(control: ProtocolCanonicalControl): QueuedPrompt[] {
  return [...(control.prompt_queue ?? [])]
    .filter((item) => item.queue_item_id && item.command_id && item.status === 'PENDING')
    .sort((left, right) => numeric(left.queue_sequence) - numeric(right.queue_sequence))
    .map((item) => {
      const queueItemId = item.queue_item_id!;
      return {
        queueItemId,
        commandId: item.command_id!,
        sequence: numeric(item.queue_sequence),
        status: 'pending' as const,
        deliveryMode: deliveryMode(item.delivery_mode),
        targetTurnId: item.target_turn_id || undefined,
        content: decodePromptContent(item.content, { kind: 'queue', queueItemId }),
        permission: protocolPermission(item.permission?.effective_mode),
        submittedAt: item.accepted_at_utc || undefined,
        requestedPermission: protocolPermission(item.permission?.requested_mode),
      };
    });
}

function protocolPermission(value?: string): PermissionMode | undefined {
  return ({
    PERMISSION_MODE_READ_ONLY: 'read-only',
    PERMISSION_MODE_ASK_PERMISSIONS: 'ask-permissions',
    PERMISSION_MODE_ACCEPT_EDITS: 'accept-edits',
    PERMISSION_MODE_BYPASS_PERMISSIONS: 'bypass-permissions',
  } as Record<string, PermissionMode>)[value ?? ''];
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
        label: item.text,
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
      ...(live?.interaction_kind === 'CAPABILITY_FORM'
        ? { kind: 'capability-form' as const }
        : { kind: 'tool-confirmation' as const, expiresAtUtc: String(live?.expires_at_utc ?? ''),
          decisionInProgress: live?.decision_in_progress === true }),
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
      label: task.label || `子任务 ${index + 1}`,
      role: task.display_role || profileLabel(task.profile),
      profile: task.profile,
      objective: task.objective,
      status,
      parentId: task.parent_turn_id,
      batchId: task.batch_id,
      taskKey: task.task_key,
      context: projectTaskContext(task.context_mode, task.context_last_n_turns),
      pendingReason: task.pending_reason || undefined,
      terminalReason: task.terminal_reason || undefined,
      terminalPublicDetail: task.terminal_public_detail
        ? task.terminal_public_detail
        : undefined,
      completionAccepted: Boolean(task.completion_accepted),
      dependencyIds: task.dependency_task_ids ?? [],
      summary: task.result_summary || undefined,
      progress: !isTerminalTaskStatus(status) && live?.summary
        ? live.summary
        : undefined,
      result: task.result_id ? {
        id: task.result_id,
        summary: task.result_summary ?? '',
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
    label: dependency.label || undefined,
    status: taskStatus(String(dependency.status ?? '')),
    resultSummary: dependency.result_summary
      ? dependency.result_summary
      : undefined,
  }));
  const result = task.result?.id ? {
    id: task.result.id,
    entryId: task.result.entry_id || undefined,
    summary: task.result.summary ?? '',
    outputPreview: task.result.output_preview
      ? task.result.output_preview
      : undefined,
    diagnostics: Array.isArray(task.result.diagnostics)
      ? task.result.diagnostics
      : [],
  } : undefined;
  return {
    id: task.id,
    label: task.label || task.task_key || '子任务',
    role: task.display_role || profileLabel(task.profile),
    profile: task.profile,
    objective: task.objective ?? '',
    status: taskStatus(String(task.status ?? '')),
    parentId: task.parent_turn_id,
    batchId: task.batch_id,
    taskKey: task.task_key,
    context: projectTaskContext(task.context?.mode, task.context?.last_n_turns),
    pendingReason: task.pending_reason || undefined,
    terminalReason: task.terminal_reason || undefined,
    terminalPublicDetail: task.terminal_public_detail
      ? task.terminal_public_detail
      : undefined,
    completionAccepted: Boolean(task.completion_accepted),
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

function orderedRootTurnIdsFromEntries(entries: ProtocolEntry[]): string[] {
  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const entry of entries) {
    if (entry.scope_kind !== 'ROOT' || !entry.turn_id || seen.has(entry.turn_id)) continue;
    seen.add(entry.turn_id);
    ordered.push(entry.turn_id);
  }
  return ordered;
}

function annotateSubagentCompletionSources(
  messages: Message[],
  tasks: AgentTask[],
  rootTurnIds: readonly string[],
): void {
  const taskById = new Map(tasks.map((task) => [task.id, task]));
  const turnIndex = new Map(rootTurnIds.map((turnId, index) => [turnId, index]));
  for (const message of messages) {
    if (message.userKind !== 'subagent-completion' || !message.sourceSubagentTaskId) continue;
    message.sourceSubagentRelation = undefined;
    const source = taskById.get(message.sourceSubagentTaskId);
    if (!source) continue;
    message.sourceSubagentLabel = source.label;
    if (!source.parentId || !message.turnId) continue;
    if (source.parentId === message.turnId) {
      message.sourceSubagentRelation = 'current';
      continue;
    }
    const sourceIndex = turnIndex.get(source.parentId);
    const targetIndex = turnIndex.get(message.turnId);
    if (sourceIndex === undefined || targetIndex === undefined || sourceIndex >= targetIndex) continue;
    message.sourceSubagentRelation = sourceIndex + 1 === targetIndex ? 'previous' : 'earlier';
  }
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
  annotateSubagentCompletionSources(
    messages,
    merged,
    projection.canonicalRootTurnIds ?? [],
  );
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
      : value.status === 'FAILED'
        ? 'failed'
        : 'rejected';
  const prompt = value.prompt_delivery;
  const control = asRecord(value.user_control);
  const process = asRecord(control.process);
  const monitor = asRecord(control.monitor);
  const feedback = asRecord(control.feedback);
  const hasControl = Object.keys(control).length > 0;
  const hasProcess = Object.keys(process).length > 0;
  const hasMonitor = Object.keys(monitor).length > 0;
  const hasFeedback = Object.keys(feedback).length > 0;
  const execution = String(control.execution ?? '').replace(/^USER_CONTROL_/u, '');
  return {
    commandId: value.command_id,
    status,
    targetId: value.target_id || undefined,
    publicCode: value.public_code || undefined,
    publicMessage: value.public_message || undefined,
    promptDelivery: prompt?.queue_item_id
      ? {
        queueItemId: prompt.queue_item_id,
        queueStatus: prompt.queue_status ?? '',
        consumedEntryId: prompt.consumed_entry_id || undefined,
        deliveryMode: deliveryMode(prompt.delivery_mode),
      }
      : undefined,
    planDraftDecision: ({
      PLAN_DRAFT_APPROVE: 'approve',
      PLAN_DRAFT_REVISE: 'revise',
      PLAN_DRAFT_CANCEL: 'cancel',
    } as const)[value.plan_draft_decision as 'PLAN_DRAFT_APPROVE' | 'PLAN_DRAFT_REVISE' | 'PLAN_DRAFT_CANCEL'] ?? undefined,
    planContinuationTurnId: value.plan_continuation_turn_id || undefined,
    userControl: hasControl ? {
      accepted: Boolean(control.accepted),
      execution: (
        ['NOT_STARTED', 'RUNNING', 'FINISHED'].includes(execution)
          ? execution
          : 'NOT_STARTED'
      ) as 'NOT_STARTED' | 'RUNNING' | 'FINISHED',
      process: hasProcess ? {
        disposition: String(process.disposition ?? ''),
        status: String(process.status ?? ''),
        exitCode: process.exit_code === undefined ? undefined : numeric(process.exit_code),
        physicalState: String(process.physical_state ?? ''),
        groupAlive: process.group_alive === undefined ? undefined : Boolean(process.group_alive),
      } : undefined,
      monitor: hasMonitor ? {
        monitorId: String(monitor.monitor_id ?? ''),
        outcome: String(monitor.outcome ?? ''),
        detail: String(monitor.detail ?? '') || undefined,
      } : undefined,
      feedback: hasFeedback ? {
        canonicalStatus: String(feedback.canonical_status ?? ''),
        inclusionStatus: String(feedback.inclusion_status ?? ''),
        ownerAvailability: String(feedback.owner_availability ?? ''),
        reason: String(feedback.reason ?? '') || undefined,
        targetRootTurnId: String(feedback.target_root_turn_id ?? '') || undefined,
        entryId: String(feedback.entry_id ?? '') || undefined,
        contextBindingRevisionId: String(feedback.context_binding_revision_id ?? '') || undefined,
        modelCallIndex: feedback.model_call_index === undefined
          ? undefined
          : numeric(feedback.model_call_index),
        transportInvocationAttempted: Boolean(feedback.transport_invocation_attempted),
        transportInvocationSucceeded: feedback.transport_invocation_succeeded === undefined
          ? undefined
          : Boolean(feedback.transport_invocation_succeeded),
        transportDetail: String(feedback.transport_detail ?? '') || undefined,
      } : undefined,
    } : undefined,
  };
}

function projectBackgroundProcess(value: ProtocolBackgroundProcess): BackgroundProcess {
  return {
    processId: value.process_id,
    command: value.command ?? '',
    cwd: value.cwd ?? '',
    status: value.status ?? 'unknown',
    exitCode: value.exit_code === undefined ? undefined : numeric(value.exit_code),
    physicalState: value.physical_state ?? 'unknown',
    ioMode: value.io_mode ?? '',
    streamId: value.stream_id ?? '',
    outputCursor: value.output_cursor ?? '',
    retainedFromCursor: value.retained_from_cursor ?? '',
    durationSeconds: value.duration_seconds ?? 0,
    timedOut: Boolean(value.timed_out),
    originTurnId: value.origin?.turn_id ?? '',
    originSubagentTaskId: value.origin?.subagent_task_id || undefined,
  };
}

function decodeContent(
  content?: ProtocolContent,
  promptOwner?: { kind: 'entry'; entryId: string } | { kind: 'queue'; queueItemId: string },
): string {
  if (!content?.inline_content) return '';
  const decoded = decodeBase64(content.inline_content);
  if (content.media_type !== PROMPT_BODY_MEDIA_TYPE) return decoded;
  if (!promptOwner) {
    throw new RuntimeApiError(
      'CONTENT_INTEGRITY_INVALID',
      '输入正文缺少可读取的 owner。',
      true,
    );
  }
  try {
    return promptContentTextProjection(decodeCanonicalPromptContent(decoded, promptOwner));
  } catch (error) {
    if (error instanceof RuntimeApiError) throw error;
    throw new RuntimeApiError('CONTENT_INTEGRITY_INVALID', '输入正文格式无效。', true);
  }
}

function decodeToolResultContent(entry: ProtocolEntry): {
  text: string;
  content?: CanonicalPromptContent;
} {
  if (entry.content?.media_type !== PROMPT_BODY_MEDIA_TYPE) {
    return { text: decodeContent(entry.content) };
  }
  const content = decodePromptContent(entry.content, {
    kind: 'entry', entryId: entry.entry_id,
  });
  return { text: promptContentTextProjection(content), content };
}

function decodePromptContent(
  content: ProtocolContent | undefined,
  owner: { kind: 'entry'; entryId: string } | { kind: 'queue'; queueItemId: string },
): CanonicalPromptContent {
  if (!content?.inline_content || content.media_type !== PROMPT_BODY_MEDIA_TYPE) {
    throw new RuntimeApiError('CONTENT_INTEGRITY_INVALID', '输入正文格式无效。', true);
  }
  try {
    return decodeCanonicalPromptContent(decodeBase64(content.inline_content), owner);
  } catch {
    throw new RuntimeApiError('CONTENT_INTEGRITY_INVALID', '输入正文格式无效。', true);
  }
}

function decodeBase64(value: string): string {
  if (!value) return '';
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(decodeBase64Bytes(value));
  } catch {
    throw new RuntimeApiError(
      'CONTENT_INTEGRITY_INVALID',
      '会话内容没有通过完整性校验。',
      true,
    );
  }
}

async function verifyContentIntegrity(
  bytes: Uint8Array,
  expectedDigest: string,
  publicMessage: string,
  requireUtf8: boolean,
): Promise<void> {
  try {
    const digest = await globalThis.crypto.subtle.digest(
      'SHA-256', Uint8Array.from(bytes).buffer,
    );
    const actualDigest = `sha256:${[...new Uint8Array(digest)]
      .map((value) => value.toString(16).padStart(2, '0'))
      .join('')}`;
    if (actualDigest !== expectedDigest) throw new Error('digest mismatch');
    if (requireUtf8) new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    throw new RuntimeApiError('CONTENT_INTEGRITY_INVALID', publicMessage, true);
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
