import { requestExecutionFromWire, type RequestExecution } from './request-execution';
import { parseMcpAuthorizationResult, type McpAuthorizationRequest, type McpAuthorizationResult } from './mcp-authorization-result';
import { parsePresentationResult, type PresentationRequest } from './presentation-targets';
import { desktopHost } from './desktop-host';
import { randomIdentity, isCanonicalIdentity } from './identity';
import type { FilePreviewApi } from './file-preview';
import { LocalMemoryApi } from './memory-api';
import type { UserHooksApi } from './hooks-api';
import type {
  VisualizationOccurrence,
  PluginImportOptions,
  PluginImportDiscovery,
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
  TodoRun,
  ToolTrace,
  UserCapabilitySnapshot,
  UserSkillCapability,
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
  host?: {platform:'win32'; executionDomain:'wsl'; distribution:string; user:string};
  application: { name: string; version: string; transport: string };
  protocol: { major: number; minor: number };
  runtime: { status: string; origin: string; database_state: DatabaseDataPlaneState };
  local_settings: LocalSettingsSummary;
  model_configurations: ModelConfigurationSummary[];
  model_catalog_loading: boolean;
  model_catalog_recovering: boolean;
  model_catalog_error: {kind: string; status_code: number | null; error_types: string[]} | null;
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

export interface SessionEphemeralResults {
  session_id: string;
  host_session_id: string | null;
  suggestion: {
    state: 'awaiting_completion' | 'pending' | 'ready' | 'absent';
    origin_turn_id: string | null;
    turn_id: string | null;
    final_entry_id: string | null;
    model_call_binding: ModelCallBindingPayload | null;
    text: string | null;
  };
  title: { state: 'pending' | 'adopted' | 'absent' };
}

/** Disposable preview of the selected next-turn model, not admission authority. */
export interface ContextUsagePreview {
  state: 'ready' | 'empty' | 'updating' | 'compacting' | 'unavailable';
  connection_id: string | null;
  model_id?: string;
  input_tokens?: number | null;
  input_budget_tokens?: number;
  budget_source?: 'heuristic' | 'reported_input_anchor' | null;
  model_switch_pending?: boolean;
  automatic_compaction_available?: boolean;
  compaction_expected?: boolean;
  compaction_trigger_tokens?: number;
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
  unavailable_reason?: string;
  authentication: 'bearer_api_key' | 'none';
  credential_configured: boolean;
  route_name?: string;
  display_name?: string;
  context_tokens?: number;
  context_window_tokens?: number | null;
  max_output_tokens?: number;
  tool_call?: boolean | null;
  input_modalities?: string[] | null;
  output_modalities?: string[] | null;
  reasoning: ReasoningControlSummary;
  reasoning_wire_profile?: ReasoningWireProfile;
  default_reasoning?: ReasoningSelectionPayload | null;
}

export type CustomReasoningProfile =
  | 'provider_default'
  | 'effort'
  | 'toggle'
  | 'enable_thinking'
  | 'thinking_type'
  | 'thinking_effort'
  | 'broad_compat';

export type ReasoningWireProfile = CustomReasoningProfile | 'catalog_standard';

export type ModelConfigurationInput =
  | {
    source: 'models_dev';
    route_id: string;
    model_id: string;
    wire_api: 'openai_chat_completions' | 'openai_responses';
    reasoning_wire_profile: ReasoningWireProfile;
    api_key: string | null;
    context_window_tokens: number | null;
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
    input_modalities: string[] | null;
    reasoning:
      | { kind: 'provider_default' }
      | { kind: 'toggle' }
      | { kind: 'enable_thinking' }
      | { kind: 'thinking_type' }
      | { kind: 'effort' | 'thinking_effort' | 'broad_compat'; values: string[] };
  };

export interface ModelConfigurationDetail {
  configuration: ModelConfigurationInput;
  base_url: string;
  credential_configured: boolean;
}

export interface ModelCatalogWireApi {
  wire_api: 'openai_chat_completions' | 'openai_responses';
  executable: boolean;
  reason: string | null;
  endpoint: string | null;
  reasoning?: ReasoningControlSummary;
  reasoning_wire_profiles?: ReasoningWireProfile[];
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

export type MemoryModelSlot = 'embedding' | 'decision' | 'rerank';
export type RetrievalShape = 'openai_embedding' | 'flat_rerank' | 'nested_rerank' | 'system_one' | 'openai_decisions';
export type RankingMode = 'off' | 'decision' | 'rerank';
export interface RetrievalConnection {
  endpoint: string; model_id: string; shape: RetrievalShape;
  authentication: 'bearer_api_key' | 'none'; credential_configured: boolean;
}
export interface MemoryRetrievalSettings {
  embedding: RetrievalConnection | null; embedding_enabled: boolean; history_enabled: boolean;
  decision: RetrievalConnection | null; rerank: RetrievalConnection | null; ranking_mode: RankingMode;
}
export interface RetrievalModelInput {
  connection: Omit<RetrievalConnection, 'credential_configured'>;
  key_action: 'keep' | 'replace' | 'clear'; api_key?: string; activate: boolean;
  confirm_reembed?: boolean;
  expected_embedding?: Pick<RetrievalConnection, 'endpoint' | 'model_id' | 'shape'> | null;
}
export interface RetrievalTestResult { status: 'ready'; elapsed_ms: number; dimensions?: number; result_count?: number }

export interface LocalSettingsSummary {
  state?: 'ready' | 'unavailable';
  pulsara_home?: string;
  memory_retrieval: MemoryRetrievalSettings;
  input_suggestions_enabled: boolean;
}

export interface LocalSettingsReadModel {
  local_settings: LocalSettingsSummary;
  model_configurations: ModelConfigurationSummary[];
  database_state: DatabaseDataPlaneState;
}

export interface TurnInterruptionNotice {
  ownerKind: 'EXECUTED_TURN' | 'IMPORTED_HISTORY';
  ownerId: string;
  reason: string;
  publicDetail?: string;
  terminalAtUtc: string;
  displayAfterEntrySequence: number;
  /** Disposable render position for entries presented inside an existing tool trace. */
  displayAfterMessageId?: string;
  displayAfterEntryId?: string;
}
interface ProtocolTurnInterruptionNotice {
  owner_kind: TurnInterruptionNotice['ownerKind'];
  owner_id: string;
  reason: string;
  public_detail?: string;
  terminal_at_utc: string;
  display_after_entry_sequence: string | number;
}

export type CanonicalInput = Pick<Message, 'id' | 'userKind' | 'inputSource' | 'contentState' | 'historyContextState'>;

function canonicalInputs(messages: Message[]): CanonicalInput[] {
  return messages.filter(message => message.role === 'user' && message.inputSource).map(({id, userKind, inputSource, contentState, historyContextState}) =>
    ({id, userKind, inputSource, contentState, historyContextState}));
}

export interface ProviderRetryObservation {
  session_id: string; turn_id: string; resolved_model_call_id: string; draft_id: string;
  attempt: number; retry_number: number; max_retries: number;
  phase: 'waiting' | 'reconnecting';
  reason: 'connection_recovery' | 'provider_recovery' | 'tool_argument_regeneration';
  remaining_seconds: number; has_semantic_output: boolean;
}
export type ProviderRetry = ProviderRetryObservation & { observedAt: number };

export interface RuntimeProjection {
  /** Canonical session turn order within loaded windows; absence does not establish adjacency. */
  canonicalTurnIds?: readonly string[];
  messages: Message[];
  /** Input handoff facts across all resident entries, independent of the reading band. */
  canonicalInputs: CanonicalInput[];
  history?: BrowserHistoryState;
  interruptionNotices?: TurnInterruptionNotice[];
  presentationNotices?: string[];
  contextCompaction?: ContextCompactionBoundary;
  initialContextBase?: ProtocolCanonicalControl['initial_context_base'];
  providerRetry?: ProviderRetry | null;
  isRunning: boolean;
  pendingSteerPage?: { targetTurnId: string; totalCount: number; nextCursor: string | null };
  queuedCount: number;
  queuedPrompts: QueuedPrompt[];
  promptTransitions: LocalPromptSubmission[];
  planMode: boolean;
  activeTurnId?: string;
  manualCompactionCommandId?: string;
  hostSessionId?: string;
  controlAdmissionDeadlineMs?: number;
  control: ProtocolCanonicalControl;
  liveControl: ProtocolLiveControlSnapshot;
  todo?: TodoRun;
  eventSequence: number;
  liveOwnerEpoch: number;
  liveRevision: number;
  liveControlOwnerEpoch: number;
  liveControlRevision: number;
  interaction?: RuntimeInteractionSummary;
}

interface QueuedPromptBase {
  queueItemId: string;
  sequence: string;
  status: 'pending';
  contentUnavailable?: boolean;
  inputOrigin?: string;
  scheduledInput?: Message['scheduledInput'];
  cellOutput?: Message['cellOutput'];
  waitingOn?: readonly string[];
  deliveryMode: 'new-turn' | 'steer';
  targetTurnId?: string;
  content: CanonicalPromptContent;
  permission?: PermissionMode;
  submittedAt?: string;
  requestedPermission?: PermissionMode;
}

export type QueuedPrompt = QueuedPromptBase & (
  | { sourceKind: 'command'; commandId: string }
  | { sourceKind: 'agent'; commandId?: never; sourceSessionId?: string; sourceWorkId?: string }
  | { sourceKind: 'cell'; commandId?: never; sourceSessionId?: string; sourceTurnId?: string; sourceExecAttemptId?: string }
  | { sourceKind: 'system'; commandId?: never }
);

export interface QueueLane { deliveryMode: 'steer'; targetTurnId: string }
export interface QueueItemStatus {
  queueItemId: string; status: 'PENDING' | 'CONSUMED' | 'CANCELLED' | 'REJECTED';
  deliveryMode: 'new-turn' | 'steer'; targetTurnId?: string; consumedEntryId?: string;
}
export interface QueuedPromptAction {
  sessionId: string;
  connectionGeneration: number;
  commandId: string;
  kind: 'send' | 'edit' | 'delete';
  source: QueuedPrompt;
  restoredContent?: EditablePromptContent;
  targetTurnId?: string;
  reviewInteraction?: RuntimeInteractionSummary;
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
  admissionDeadlineMs: number;
  commandId: string;
  operation: 'STOP_ACTIVE_TURN' | 'STOP_CONTEXT_COMPACTION' | 'TERMINATE_BACKGROUND_PROCESS';
  sessionId: string;
  hostSessionId: string;
  targetKind: 'ROOT_TURN' | 'MANUAL_COMPACTION' | 'BACKGROUND_PROCESS';
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
    commandId: randomIdentity('command'),
    admissionDeadlineMs: deadline,
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
  execution?: RequestExecution | null;
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
    revisionPrompt?: EditablePromptContent;
    sourceQueueItemId?: string;
  };

/** App freezes a tool command before transport; forms/plan keep their own semantics. */
export type RuntimeInteractionSubmission =
  | Exclude<RuntimeInteractionResolution, { kind: 'tool' }>
  | { kind: 'tool'; decision: 'allow' | 'deny'; commandId: string };

/** Browser boundary for the local Pulsara application. */
export interface SessionSearchItem {
  session: SessionSummary;
  matchKind: 'title' | 'user' | 'assistant' | 'recent';
  snippet: string;
}
export interface SessionSearchPage { items: SessionSearchItem[]; nextCursor: string | null }

export interface ModelDiscoveryInput {
  base_url: string;
  authentication: 'bearer_api_key' | 'none';
  api_key: string | null;
  connection_id: string | null;
}

export interface RuntimeAdapter {
  userAvatar(): Promise<{ configured: boolean }>;
  uploadUserAvatar(file: File): Promise<{ configured: boolean }>;
  clearUserAvatar(): Promise<{ configured: boolean }>;
  workspaceAvailability(sessionId: string, signal?: AbortSignal): Promise<WorkspaceAvailability>;
  restoreWorkspace(sessionId: string): Promise<WorkspaceAvailability>;
  searchSessions(query: string, lifecycle: 'ALL' | 'OPEN' | 'ARCHIVED', cursor?: string, signal?: AbortSignal): Promise<SessionSearchPage>;
  readonly memory: LocalMemoryApi;
  readonly hooks: UserHooksApi;
  bootstrap(): Promise<RuntimeBootstrap>;
  modelCatalog(refresh?: boolean): Promise<ModelCatalogReadModel>;
  discoverModels(input: ModelDiscoveryInput, signal?: AbortSignal): Promise<{ model_ids: string[] }>;
  localSettings(): Promise<LocalSettingsReadModel>;
  setInputSuggestionsEnabled(enabled: boolean): Promise<{ enabled: boolean }>;
  addModelConfiguration(input: ModelConfigurationInput): Promise<{ model_configuration: ModelConfigurationSummary; wire_shape_warning: boolean }>;
  modelConfiguration(connectionId: string): Promise<ModelConfigurationDetail>;
  updateModelConfiguration(connectionId: string, input: ModelConfigurationInput): Promise<{ model_configuration: ModelConfigurationSummary; wire_shape_warning: boolean }>;
  deleteModelConfiguration(connectionId: string): Promise<{
    model_configuration_id: string;
    deleted: boolean;
    model_configurations: ModelConfigurationSummary[];
  }>;
  testModelConfiguration(input: ModelConfigurationInput, connectionId?: string): Promise<{ status: 'ready' }>;
  checkPostgres(): Promise<Record<string, unknown>>;
  resetPostgres(): Promise<{ database_name: string; restart_required: boolean }>;
  saveMemoryModel(slot: MemoryModelSlot, input: RetrievalModelInput): Promise<MemoryRetrievalSettings>;
  clearMemoryModel(slot: MemoryModelSlot): Promise<MemoryRetrievalSettings>;
  testMemoryModel(slot: MemoryModelSlot, input: RetrievalModelInput): Promise<RetrievalTestResult>;
  setHistoryEnabled(enabled: boolean): Promise<MemoryRetrievalSettings>;
  setEmbeddingEnabled(enabled: boolean): Promise<MemoryRetrievalSettings>;
  setMemoryRanking(mode: RankingMode): Promise<MemoryRetrievalSettings>;
  ephemeralResults(sessionId: string, signal?: AbortSignal): Promise<SessionEphemeralResults>;
  contextUsage(sessionId: string, signal?: AbortSignal): Promise<ContextUsagePreview>;
  updateModelCallBinding(sessionId: string, binding: ModelCallBindingPayload): Promise<ModelCallBindingUpdate>;
  reopenRuntime(sessionId: string): Promise<{
    status: 'reopened' | 'deferred';
    publicCode?: string;
  }>;
  connect(sessionId: string, takeover?: boolean): Promise<RuntimeConnection | CanonicalHistoryView>;
  openAgentReader(rootSessionId: string, agentSessionId: string, signal?: AbortSignal): Promise<AgentConversationReader>;
  createAgentContentReader(rootSessionId: string, agentSessionId: string): AgentContentReader;
  createSession(selection: SessionWorkspaceSelection, modelCallBinding?: ModelCallBindingPayload): Promise<SessionSummary>;
  pickWorkspaceDirectory(signal?: AbortSignal): Promise<string | null>;
  completeWorkspacePaths(sessionId: string, prefix: string, cursor: string | null, signal: AbortSignal): Promise<import('./file-reference').WorkspacePathPage>;
  forkConversation(sessionId: string, anchorEntryId: string): Promise<ForkOutcome>;
  deleteSession(sessionId: string): Promise<{ status: 'DELETED' | 'ABSENT'; session_id: string }>;
  deleteArchivedSession(sessionId: string): Promise<{
    status: 'DELETED' | 'ABSENT' | 'NOT_ARCHIVED'; session_id: string;
    cleanup_warning: { code: 'SESSION_DELETE_QUARANTINED'; message: string } | null;
  }>;
  listArchivedSessions(): Promise<SessionSummary[]>;
  renameSession(sessionId: string, title: string): Promise<{ session_id: string; title: string }>;
  archiveSession(sessionId: string): Promise<{ status: 'ARCHIVED'; session_id: string }>;
  unarchiveSession(sessionId: string): Promise<{ status: 'OPEN'; session_id: string }>;
  readSession(sessionId: string): Promise<SessionSummary | null>;
  listSessions(): Promise<SessionSummary[]>;
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
  inspectUserCapabilities(signal?: AbortSignal): Promise<UserCapabilitySnapshot>;
  refreshUserCapabilities(): Promise<UserCapabilitySnapshot>;
  previewSkillImport(sourcePath: string): Promise<SkillImportCandidate[]>;
  installUserSkill(input: SkillImportInput): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  setUserSkillEnabled(skillPath: string, enabled: boolean): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  removeUserSkill(skill: UserSkillCapability): Promise<{
    operation: CapabilityOperation; capabilities: UserCapabilitySnapshot;
  }>;
  createUserMcp(input: McpEditInput): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  testUserMcp(input: McpEditInput): Promise<McpConnectionTestResult>;
  previewMcpImport(input: McpImportSource): Promise<McpImportPreview[]>;
  importUserMcp(input: McpImportSelection): Promise<{operation: CapabilityOperation; capabilities: UserCapabilitySnapshot}>;
  updateUserMcp(input: McpEditInput, expectedIdentity: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  removeUserMcp(serverId: string, expectedIdentity: string): Promise<{
    operation: CapabilityOperation; capabilities: UserCapabilitySnapshot;
  }>;
  userMcpAuthorization(serverId: string, action: 'login' | 'status' | 'cancel' | 'logout'): Promise<{ state: string; error: string | null }>;
  modelMcpAuthorizationStatus(target: McpAuthorizationResult['authorization']): Promise<{ state: string; error: string | null }>;
  pluginMcpAuthorization(plugin: UserPluginCapability, connection: PluginMcpConnection, action: 'login' | 'status' | 'cancel' | 'logout'): Promise<{ state: string; error: string | null }>;
  previewPluginImport(sourcePath: string): Promise<PluginImportDiscovery>;
  installUserPlugin(sourcePath: string, options?: PluginImportOptions): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  setUserPluginEnabled(
    pluginId: string,
    packageInstallId: string,
    enabled: boolean,
    connectionReview: NonNullable<UserPluginCapability['connectionReview']>,
  ): Promise<{ operation: CapabilityOperation; capabilities: UserCapabilitySnapshot }>;
  removeUserPlugin(pluginId: string, packageInstallId: string): Promise<{
    operation: CapabilityOperation;
    capabilities: UserCapabilitySnapshot;
  }>;
  updatePluginConnection(plugin: UserPluginCapability, connection: PluginMcpConnection, input: PluginMcpEditInput): Promise<{ operation: CapabilityOperation; capabilities: UserCapabilitySnapshot }>;
  openCapabilityRoot(root: 'agents' | 'pulsara'): Promise<void>;
}

export interface AgentConversationReader {
  readonly view: 'live' | 'history';
  readonly sessionId: string;
  current(): RuntimeProjection;
  snapshot(): Promise<RuntimeProjection>;
  observe(signal?: AbortSignal): Promise<RuntimeProjection>;
  subscribe(listener: () => void): () => void;
  readHistory(windowId?: string, direction?: 'older' | 'newer', signal?: AbortSignal): Promise<RuntimeProjection>;
  hydrateEntries(ids: string[], signal?: AbortSignal, includeReasoning?: boolean): Promise<RuntimeProjection>;
  readLatestHistory(signal?: AbortSignal): Promise<RuntimeProjection>;
  readProcessEnd(entryId: string, signal: AbortSignal): Promise<RuntimeProjection>;
  retainHistory(interest: HistoryResidence): RuntimeProjection | undefined;
  locateEntry(entryId: string, signal: AbortSignal): Promise<RuntimeProjection>;
  locateAnnotationSource(entryId: string, signal: AbortSignal): Promise<RuntimeProjection>;
  readQueuePage(cursor?: string, signal?: AbortSignal, lane?: QueueLane): Promise<{items: QueuedPrompt[]; nextCursor: string | null}>;
  readQueueStatus(queueItemId: string, signal?: AbortSignal): Promise<QueueItemStatus | null>;
  readQueueItem(queueItemId: string, signal?: AbortSignal): Promise<QueuedPrompt | null>;
  readToolArtifact(resultEntryId: string, offsetChars: number, maxChars?: number): Promise<ToolArtifactPage>;
  readPromptImage(image: CanonicalPromptImagePart): Promise<Uint8Array>;
  readImageReference(imageRef: string, signal?: AbortSignal): Promise<SavedImageData>;
  readVisualizationThumbnail(entryId: string, ordinal: number, digest: string, size: number, signal: AbortSignal): Promise<string>;
  readVisualizationContent(entryId: string, ordinal: number, digest: string, size: number, item?: VisualizationOccurrence, signal?: AbortSignal): Promise<string | Uint8Array>;
  close(): Promise<void>;
}

export type AgentContentReader = Pick<AgentConversationReader,
  'readToolArtifact' | 'readPromptImage' | 'readImageReference' | 'readVisualizationContent' | 'readVisualizationThumbnail'>;

export interface RuntimeConnection {
  readonly availability: WorkspaceAvailability;
  subscribeMcpAuthorization?: (listener: (request: McpAuthorizationRequest) => void) => () => void;
  subscribePresentation?: (listener: (request: PresentationRequest) => void) => () => void;
  readHistory?: (windowId?: string, direction?: 'older' | 'newer', signal?: AbortSignal) => Promise<RuntimeProjection>;
  hydrateEntries?: (ids: string[], signal?: AbortSignal, includeReasoning?: boolean) => Promise<RuntimeProjection>;
  readLatestHistory?: (signal?: AbortSignal) => Promise<RuntimeProjection>;
  readProcessEnd?: (entryId: string, signal: AbortSignal) => Promise<RuntimeProjection>;
  retainHistory?: (interest: HistoryResidence) => RuntimeProjection | undefined;
  locateEntry?: (entryId: string, signal: AbortSignal) => Promise<RuntimeProjection>;
  subscribe?: (listener: () => void) => () => void;
  readQueuePage(cursor?: string, signal?: AbortSignal, lane?: QueueLane): Promise<{items: QueuedPrompt[]; nextCursor: string | null}>;
  readQueueStatus(queueItemId: string, signal?: AbortSignal): Promise<QueueItemStatus | null>;
  readQueueItem(queueItemId: string, signal?: AbortSignal): Promise<QueuedPrompt | null>;
  locateAnnotationSource?: (entryId: string, signal: AbortSignal) => Promise<RuntimeProjection>;
  readonly filePreview: FilePreviewApi;
  importFiles(files: readonly File[], signal: AbortSignal): Promise<import('./file-reference').ImportedPath>;
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
  stopContextCompaction(reference: UserControlCommandRef): Promise<CommandReceipt>;
  terminateBackgroundProcess(reference: UserControlCommandRef): Promise<CommandReceipt>;
  queryControlCommand(reference: UserControlCommandRef): Promise<UserControlQueryResult>;
  listBackgroundProcesses(cursor?: string): Promise<BackgroundProcessPage>;
  readBackgroundProcessLog(processId: string, outputCursor?: string): Promise<BackgroundProcessLog>;
  readCanonicalEntryContent(entryId: string, digest: string, size: number): Promise<string>;
  readVisualizationThumbnail(entryId: string, ordinal: number, digest: string, size: number, signal: AbortSignal): Promise<string>;
  readVisualizationContent(entryId: string, ordinal: number, digest: string, size: number, item?: VisualizationOccurrence, signal?: AbortSignal): Promise<string | Uint8Array>;
  readImageReference(imageRef: string, signal?: AbortSignal): Promise<SavedImageData>;
  readPromptImage(image: CanonicalPromptImagePart): Promise<Uint8Array>;
  readPromptForEdit(content: CanonicalPromptContent): Promise<EditablePromptContent>;
  compactContext(): Promise<CommandReceipt>;
  enterPlan(reason: string, permission: PermissionMode): Promise<CommandReceipt>;
  readInteraction(interaction: RuntimeInteractionSummary): Promise<RuntimeInteractionContent>;
  resolveInteraction(
    interaction: RuntimeInteractionSummary,
    resolution: RuntimeInteractionSubmission,
    commandId?: string,
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
    execution: RequestExecution | null;
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
  | 'STOP_CONTEXT_COMPACTION'
  | 'TERMINATE_BACKGROUND_PROCESS'
  | 'COMPACT_CONTEXT'
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

/** A discarded observation owner needs a fresh HELLO and canonical baseline. */
export function agentReaderRequiresReopen(error: unknown): boolean {
  return error instanceof RuntimeGapError || (error instanceof RuntimeApiError && new Set([
    'STALE_ATTACHMENT', 'CONNECTION_CLOSED', 'PROTOCOL_TRANSPORT_CLOSED',
    'LOCAL_RESOURCE_NOT_FOUND', 'HTTP_404', 'SESSION_WRITER_UNAVAILABLE',
  ]).has(error.code));
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
  root_final?: boolean;
  entry_owner_kind?: 'EXECUTED_TURN' | 'IMPORTED_HISTORY';
  turn_id: string;
  entry_sequence: string | number;
  entry_kind: string;
  scope_kind?: string;
  content?: ProtocolContent;
  blocks?: ProtocolAssistantBlock[];
  reasoning_blocks?: ProtocolReasoningBlock[];
  accepted_at_utc?: string;
  tool_result?: {
    assistant_entry_id?: string; tool_call_id?: string; result_state?: string;
    attempt_id?: string; invocation_id?: string; source_kind?: string; source_exec_attempt_id?: string;
    call_sequence?: string | number; tool_name?: string; original_arguments_json?: string;
    artifact_disposition?: string; source_coverage?: string; display_kind?: string;
    source_coverage_reason?: string; artifact_unavailability_reason?: string;
  };
  input_origin?: string;
  input_source?: { queue_item_id?: string; command_id?: string; delivery_mode?: string; source_session_id?: string; source_work_id?: string; source_turn_id?: string; source_tool_attempt_id?: string };
  scheduled_input?: { task_id: string; task_revision: string | number; due_at_utc: string; trigger_source: 'TIMER' | 'RUN_ONCE' };
  visualizations?: Array<{
    ordinal: string | number; state: string; content_digest?: string;
    content_size?: string | number; failure_code?: string; failure_detail?: string;
    source_filename?: string; media_type?: string; codec?: string; image_ref?: string; width?: number; height?: number;
  }>;
}

interface ProtocolActiveTurn {
  turn_id: string;
  status: string;
  scope_kind?: string;
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
    status?: string; delivery_mode?: string; target_turn_id?: string; accepted_at_utc?: string; input_origin?: string;
    source_session_id?: string; source_work_id?: string; source_turn_id?: string; source_tool_attempt_id?: string; scheduled_input?: ProtocolEntry['scheduled_input']; waiting_on?: string[];
    content?: ProtocolContent; requested_permission_mode?: string; permission?: { requested_mode?: string; effective_mode?: string };
  }>;
  prompt_queue_total_count?: string | number;
  prompt_queue_next_cursor?: string | number;
  pending_steer_total_count?: string | number;
  pending_steer_target_turn_id?: string;
  pending_steer_next_cursor?: string | number;
  tool_attempts?: ProtocolToolAttempt[];
  active_turn_total_count?: string | number;
  tool_attempt_total_count?: string | number;
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
  committed_entry_id?: string;
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
  root_compaction_in_progress?: boolean;
  input_admission_deferred?: boolean;
  host_session_id?: string;
  active_root_turn_id?: string;
  manual_compaction_command_id?: string;
  control_admission_deadline_ms?: string | number;
}

interface ProtocolTodoRunSnapshot {
  todo_run_id: string;
  todo_revision?: string | number;
  scope_kind?: string;
  disposition?: string;
  ordered_items?: Array<{ ordinal?: number; text: string; status: string }>;
  pending_count?: string | number;
  in_progress_count?: string | number;
  completed_count?: string | number;
}

interface ProtocolCanonicalSnapshot {
  session_id: string;
  writer_generation?: string | number;
  entry_sequence_cut?: string | number;
  event_sequence_cut?: string | number;
  entries?: ProtocolEntry[];
  interruption_notices?: ProtocolTurnInterruptionNotice[];
  older_history_cursor?: ProtocolHistoryCursor;
  control?: ProtocolCanonicalControl;
}

interface ProtocolHistoryCursor {
  session_id: string;
  cut_sequence: string | number;
  event_sequence_cut?: string | number;
  entry_sequence: string | number;
}

interface ProtocolHistoryPage {
  entries?: ProtocolEntry[];
  interruption_notices?: ProtocolTurnInterruptionNotice[];
  older_history_cursor?: ProtocolHistoryCursor;
  has_more?: boolean;
}

export interface BrowserHistoryState {
  /** Identity of the last successful explicit navigation; process-local only. */
  readingOwner: object;
  atLatest: boolean;
  initialReady: boolean;
  initialError?: string;
  hasOlder: boolean;
  loading: boolean;
  error?: string;
  windows: Array<{id: string; firstEntryId?: string; lastEntryId?: string; entryIds: string[]; hasOlder: boolean; hasNewer: boolean; loading: boolean; boundaryDirection?: 'older' | 'newer'; error?: string; contextState: 'loading' | 'ready' | 'error'; contextError?: string}>;
  compactions: Array<{eventId: string; adoptedAfterEntrySequence: number; turnId: string; displayAfterEntryId?: string}>;
}
/** The current browser consumers; canonical history remains re-readable. */
export interface HistoryResidence {
  readingOwner?: object;
  anchorEntryId?: string;
  followLatest?: boolean;
  visibleEntryIds: string[];
  pinnedEntryIds: string[];
}
interface BrowserWindowCursor extends ProtocolHistoryCursor { direction: 'older' | 'newer' }
interface BrowserWindow {
  session_id: string;
  cut_sequence: string;
  event_sequence_cut: string;
  entries: ProtocolEntry[];
  older_cursor?: BrowserWindowCursor;
  newer_cursor?: BrowserWindowCursor;
}
interface BrowserPageContext {
  session_id: string; cut_sequence: string; event_sequence_cut: string; entry_ids: string[];
  groups: Array<{owner_kind: string; owner_id: string; initial_entry_id: string; final_entry_id?: string; previous_owner_id?: string; next_owner_id?: string}>;
  entry_contexts: Array<{entry_id: string; owner_kind: string; owner_id: string; parent_turn_id?: string; segment_id: string; segment_start_entry_id: string; previous_entry_id?: string; next_entry_id?: string}>;
  interruption_notices: ProtocolTurnInterruptionNotice[];
  boundary_entries?: Array<{event_id: string; event_sequence: string; entry_id: string; entry_sequence: string}>;
  compactions: Array<{event_id: string; event_sequence: string; turn_id: string; context_binding_revision_id: string; source_through_sequence: string; adopted_after_entry_sequence: string; accepted_at_utc: string}>;
}
interface BrowserHistoryPage {
  id: string; entryIds: string[]; cut?: {cut_sequence: string; event_sequence_cut: string};
  older?: ProtocolHistoryCursor | BrowserWindowCursor; newer?: BrowserWindowCursor;
  loading: boolean; readDirection?: 'older' | 'newer'; error?: string; read?: Promise<void>;
  contextState: 'loading' | 'ready' | 'error'; contextError?: string; context?: BrowserPageContext; contextRead?: Promise<void>; needsFreshContext?: boolean; requiredContextEventCut?: string;
}
function waitForRead<T>(read: Promise<T>, signal?: AbortSignal): Promise<T> {
  signal?.throwIfAborted();
  if (!signal) return read;
  return new Promise<T>((resolve, reject) => {
    const cancel = () => reject(signal.reason);
    signal.addEventListener('abort', cancel, {once: true});
    read.then(resolve, reject).finally(() => signal.removeEventListener('abort', cancel));
  });
}
function decimal(value: string | number | undefined): string {
  if (value === undefined || (typeof value === 'number' && !Number.isSafeInteger(value)) || !/^(0|[1-9][0-9]*)$/.test(String(value))) throw new RuntimeApiError('HISTORY_SEQUENCE_INVALID', '会话游标格式无效。', false);
  const result = BigInt(value);
  if (result > BigInt('18446744073709551615')) throw new RuntimeApiError('HISTORY_SEQUENCE_INVALID', '会话游标超出协议范围。', false);
  return result.toString();
}
export function compareSequence(a: string | number | undefined, b: string | number | undefined): number {
  const left = BigInt(decimal(a)), right = BigInt(decimal(b)); return left < right ? -1 : left > right ? 1 : 0;
}

interface ConnectPayload {
  workspace_availability: WorkspaceAvailability;
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
  provider_retry: ProviderRetryObservation | null;
  through_event_sequence?: string | number;
  committed?: Array<{
    projection_kind: string;
    event_id?: string;
    event_type?: string;
    event_sequence?: string | number;
    entry?: ProtocolEntry;
    current_control?: ProtocolCanonicalControl;
    interruption_notice?: ProtocolTurnInterruptionNotice;
    control_read_event_sequence?: string | number;
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
  root_compaction_in_progress?: boolean;
  manual_compaction_command_id?: string;
  active_root_turn_id?: string;
  gap?: { kind: string; reason?: string };
}

interface LiveDraft {
  id: string;
  committedEntryId?: string;
  turnId: string;
  scopeKind: string;
  body: string;
  reasoning: ReasoningBlock[];
  traces: ToolTrace[];
}

interface LiveToolResult {
  sourceExecAttemptId?: string;
  committedEntryId?: string;
  id: string;
  turnId: string;
  scopeKind: string;
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
    execution?: { status?: string; terminal_reason?: string };
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
  };
}

const browserInstanceStorageKey = 'pulsara-browser-instance-id-v2';

function resolveBrowserInstanceId(): string {
  const next = () => randomIdentity('transport');
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
  if (navigation?.type === 'reload' && existing && isCanonicalIdentity(existing, 'transport')) {
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
  userAvatar(): Promise<{ configured: boolean }> {
    return apiRequest('/api/user-avatar');
  }
  uploadUserAvatar(file: File): Promise<{ configured: boolean }> {
    return httpRequest('/api/user-avatar', { method: 'PUT', body: file, headers: { 'Content-Type': file.type || 'application/octet-stream' } });
  }
  clearUserAvatar(): Promise<{ configured: boolean }> {
    return apiRequest('/api/user-avatar', { method: 'DELETE' });
  }
  private chatConnection?: LocalRuntimeConnection;
  private connectAttempt = 0;
  private sessionRequest<T>(sessionId: string, url: string, init: RequestInit): Promise<T> {
    const connection = this.chatConnection;
    if (!connection || connection.sessionId !== sessionId || connection.role !== 'controller') {
      throw new RuntimeApiError('CHAT_CONTROLLER_REQUIRED', '此窗口仅供查看。', false);
    }
    return apiRequest(url, {...init, headers: {...init.headers, ...connection.chatWriteHeaders()}});
  }
  workspaceAvailability(sessionId: string, signal?: AbortSignal): Promise<WorkspaceAvailability> {
    return apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}/workspace-availability`, {signal});
  }
  restoreWorkspace(sessionId: string): Promise<WorkspaceAvailability> {
    const connection = this.chatConnection;
    return apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}/restore-workspace`, {
      method: 'POST', body: '{}',
      headers: connection?.sessionId === sessionId ? connection.chatWriteHeaders() : {},
    });
  }

  private sessionTitleRevision = 0;
  readonly memory = new LocalMemoryApi();
  readonly hooks: UserHooksApi = {
    list: (signal) => apiRequest('/api/capabilities/hooks', {signal}),
    config: (signal) => apiRequest('/api/capabilities/hooks/local', {signal}),
    save: (config, text) => apiRequest('/api/capabilities/hooks/local', {method: 'PUT', body: JSON.stringify({exists: config.exists, expected_text: config.text, text})}),
    review: (pluginId, signal) => apiRequest(`/api/capabilities/hooks/review${pluginId ? `?plugin_id=${encodeURIComponent(pluginId)}` : ''}`, {signal}),
    trust: (source) => apiRequest('/api/capabilities/hooks/trust', {method: 'POST', body: JSON.stringify({plugin_id: source.plugin_id ?? null, definition_digest: source.definition_digest,
      trust: {enabled: source.enabled, trusted_definition_digest: source.trusted_definition_digest, trusted_at: source.trusted_at}, accepted: true})}),
  };
  private readonly browserInstanceId: string;

  constructor(browserInstanceId = resolveBrowserInstanceId()) {
    this.browserInstanceId = browserInstanceId;
  }

  async bootstrap(): Promise<RuntimeBootstrap> {
    return apiRequest<RuntimeBootstrap>('/api/app/bootstrap');
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

  async setInputSuggestionsEnabled(enabled: boolean): Promise<{ enabled: boolean }> {
    return apiRequest('/api/local-settings/input-suggestions', {
      method: 'PUT', body: JSON.stringify({ enabled }),
    });
  }

  async discoverModels(input: ModelDiscoveryInput, signal?: AbortSignal) {
    return apiRequest<{ model_ids: string[] }>('/api/model-configurations/discover', {
      method: 'POST', body: JSON.stringify(input), signal,
    });
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

  async modelConfiguration(connectionId: string) {
    return apiRequest<ModelConfigurationDetail>(`/api/model-configurations/${encodeURIComponent(connectionId)}`);
  }

  async updateModelConfiguration(connectionId: string, input: ModelConfigurationInput) {
    return apiRequest<{ model_configuration: ModelConfigurationSummary; wire_shape_warning: boolean }>(
      `/api/model-configurations/${encodeURIComponent(connectionId)}`, { method: 'PUT', body: JSON.stringify(input) },
    );
  }

  async testModelConfiguration(input: ModelConfigurationInput, connectionId?: string) {
    return apiRequest<{ status: 'ready' }>(connectionId
      ? `/api/model-configurations/${encodeURIComponent(connectionId)}/test`
      : '/api/model-configurations/test', {
      method: 'POST',
      body: JSON.stringify(input),
    });
  }

  async checkPostgres(): Promise<Record<string, unknown>> {
    return apiRequest<Record<string, unknown>>('/api/local-settings/postgres/check', { method: 'POST' });
  }

  async resetPostgres(): Promise<{ database_name: string; restart_required: boolean }> {
    return apiRequest('/api/local-settings/postgres/reset', {
      method: 'POST', body: JSON.stringify({ confirmed: true }),
    });
  }

  async saveMemoryModel(slot: MemoryModelSlot, input: RetrievalModelInput): Promise<MemoryRetrievalSettings> {
    return apiRequest(`/api/local-settings/memory-retrieval/${slot}`, { method: 'PUT', body: JSON.stringify(input) });
  }
  async clearMemoryModel(slot: MemoryModelSlot): Promise<MemoryRetrievalSettings> {
    return apiRequest(`/api/local-settings/memory-retrieval/${slot}`, { method: 'DELETE' });
  }
  async testMemoryModel(slot: MemoryModelSlot, input: RetrievalModelInput): Promise<RetrievalTestResult> {
    return apiRequest(`/api/local-settings/memory-retrieval/${slot}/test`, { method: 'POST', body: JSON.stringify(input) });
  }
  async setHistoryEnabled(enabled: boolean): Promise<MemoryRetrievalSettings> {
    return apiRequest('/api/local-settings/memory-retrieval/history-enabled', { method: 'PUT', body: JSON.stringify({ enabled }) });
  }
  async setEmbeddingEnabled(enabled: boolean): Promise<MemoryRetrievalSettings> {
    return apiRequest('/api/local-settings/memory-retrieval/embedding-enabled', { method: 'PUT', body: JSON.stringify({ enabled }) });
  }
  async setMemoryRanking(mode: RankingMode): Promise<MemoryRetrievalSettings> {
    return apiRequest('/api/local-settings/memory-retrieval/ranking-mode', { method: 'PUT', body: JSON.stringify({ mode }) });
  }

  async ephemeralResults(sessionId: string, signal?: AbortSignal): Promise<SessionEphemeralResults> {
    return apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}/ephemeral-results`, { signal });
  }

  async contextUsage(sessionId: string, signal?: AbortSignal): Promise<ContextUsagePreview> {
    return apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}/context-usage`, { signal });
  }

  async updateModelCallBinding(
    sessionId: string,
    binding: ModelCallBindingPayload,
  ): Promise<ModelCallBindingUpdate> {
    const value = await this.sessionRequest<{
      model_call_binding: ModelCallBindingPayload;
      reasoning_preference_reset: boolean;
    }>(sessionId, `/api/sessions/${encodeURIComponent(sessionId)}/model-call-binding`,
      { method: 'PUT', body: JSON.stringify(binding) },
    );
    return {
      modelCallBinding: value.model_call_binding,
      reasoningPreferenceReset: value.reasoning_preference_reset,
    };
  }

  async searchSessions(query: string, lifecycle: 'ALL' | 'OPEN' | 'ARCHIVED' = 'ALL', cursor?: string, signal?: AbortSignal): Promise<SessionSearchPage> {
    const payload = await apiRequest<{ items: Array<{ session: Record<string, unknown>; match_kind: SessionSearchItem['matchKind']; snippet: string }>; next_cursor: string | null }>('/api/sessions/search', {
      method: 'POST', body: JSON.stringify({ query, lifecycle, ...(!query.trim() ? { limit: 5 } : {}), ...(cursor ? { cursor } : {}) }), signal,
    });
    return { items: payload.items.map(item => ({ session: projectSessionSummary(item.session), matchKind: item.match_kind, snippet: item.snippet })), nextCursor: payload.next_cursor };
  }

  async listSessions(): Promise<SessionSummary[]> {
    // A response started before a confirmed rename must not undo its display.
    for (;;) {
      const revision = this.sessionTitleRevision;
      const payload = await apiRequest<{ sessions: Array<Record<string, unknown>> }>('/api/sessions');
      if (revision === this.sessionTitleRevision) return payload.sessions.map(projectSessionSummary);
    }
  }

  async reopenRuntime(sessionId: string): Promise<{
    status: 'reopened' | 'deferred';
    publicCode?: string;
  }> {
    const payload = await this.sessionRequest<{
      status: 'reopened' | 'deferred';
      public_code?: string;
    }>(sessionId, `/api/sessions/${encodeURIComponent(sessionId)}/runtime/reopen`, {
      method: 'POST',
    });
    return { status: payload.status, publicCode: payload.public_code };
  }

  async inspectCapabilities(sessionId: string): Promise<CapabilitySnapshot> {
    const payload = await apiRequest<Record<string, unknown>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities`,
    );
    if (payload.available === false) {
      throw new RuntimeApiError('CAPABILITIES_UNAVAILABLE', '未连接运行时，当前项目能力状态不可用。', false);
    }
    return projectCapabilitySnapshot(payload);
  }

  async reconnectMcpServer(
    sessionId: string,
    serverId: string,
  ): Promise<CapabilitySnapshot> {
    const payload = await this.sessionRequest<Record<string, unknown>>(sessionId,
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
    const payload = await this.sessionRequest<Record<string, unknown>>(sessionId, `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/skills/install`,
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
    const payload = await this.sessionRequest<Record<string, unknown>>(sessionId,
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
    const payload = await this.sessionRequest<Record<string, unknown>>(sessionId,
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
    return this.sessionRequest<McpConnectionTestResult>(sessionId, `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/test`, {
      method: 'POST', body: JSON.stringify({server_id: input.serverId, config: input.config,
        secret_changes: input.secretChanges, retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false}),
    });
  }

  async importProjectMcp(sessionId: string, input: McpImportSelection): Promise<ProjectCapabilityMutationResult> {
    return projectCapabilityMutation(await this.sessionRequest<Record<string, unknown>>(sessionId,
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/import`,
      {method: 'POST', body: JSON.stringify(input)},
    ));
  }

  async projectMcpAuthorization(sessionId: string, serverId: string, action: 'login' | 'status' | 'cancel' | 'logout'): Promise<{state: string; error: string | null}> {
    const suffix = action === 'login' ? 'authorize' : action === 'cancel' ? 'authorization/cancel' : 'authorization';
    const url = `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/${encodeURIComponent(serverId)}/${suffix}`;
    if (action === 'status') return apiRequest(url);
    return this.sessionRequest(sessionId, url, {method: action === 'logout' ? 'DELETE' : 'POST', body: '{}'});
  }

  async setProjectMcpEnabled(
    sessionId: string,
    serverId: string,
    configIdentity: string,
    enabled: boolean,
  ): Promise<ProjectCapabilityMutationResult> {
    const payload = await this.sessionRequest<Record<string, unknown>>(sessionId,
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/${encodeURIComponent(serverId)}/enabled`,
      {
        method: 'POST',
        body: JSON.stringify({ enabled, config_identity: configIdentity }),
      },
    );
    return projectCapabilityMutation(payload);
  }

  async removeProjectSkill(sessionId: string, skill: SkillCapability): Promise<ProjectCapabilityMutationResult> {
    return projectCapabilityMutation(await this.sessionRequest<Record<string, unknown>>(sessionId,
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/skills/remove`,
      {method: 'POST', body: JSON.stringify({path: skill.path, expected: skill.removalIdentity})},
    ));
  }

  async removeProjectMcp(
    sessionId: string,
    serverId: string,
    configIdentity: string,
  ): Promise<ProjectCapabilityMutationResult> {
    const payload = await this.sessionRequest<Record<string, unknown>>(sessionId,
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/${encodeURIComponent(serverId)}`,
      {
        method: 'DELETE',
        body: JSON.stringify({ config_identity: configIdentity }),
      },
    );
    return projectCapabilityMutation(payload);
  }

  async updateProjectMcp(sessionId: string, input: McpEditInput, expectedIdentity: string): Promise<ProjectCapabilityMutationResult> {
    return projectCapabilityMutation(await this.sessionRequest<Record<string, unknown>>(sessionId,
      `/api/sessions/${encodeURIComponent(sessionId)}/capabilities/mcp/${encodeURIComponent(input.serverId)}`,
      { method: 'PUT', body: JSON.stringify({ config: input.config, expected_identity: expectedIdentity,
        secret_changes: input.secretChanges, retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false }) },
    ));
  }

  async inspectUserCapabilities(signal?: AbortSignal): Promise<UserCapabilitySnapshot> {
    return projectUserCapabilitySnapshot(await apiRequest<Record<string, unknown>>('/api/capabilities', {signal}));
  }

  async refreshUserCapabilities(): Promise<UserCapabilitySnapshot> {
    return projectUserCapabilitySnapshot(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/refresh',
      { method: 'POST', body: '{}' },
    ));
  }

  async previewSkillImport(sourcePath: string): Promise<SkillImportCandidate[]> {
    const result = await apiRequest<{ items: Array<{ source_path: string; name: string; description: string; valid: boolean; details: string[] }> }>('/api/capabilities/skills/preview', {
      method: 'POST', body: JSON.stringify({ source_path: sourcePath }),
    });
    return result.items.map((item) => ({ sourcePath: item.source_path, name: item.name, description: item.description, valid: item.valid, details: item.details }));
  }

  async installUserSkill(input: SkillImportInput) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/skills/install',
      {
        method: 'POST',
        body: JSON.stringify({ source_path: input.sourcePath, name: input.name, description: input.description }),
      },
    ));
  }

  async setUserSkillEnabled(skillPath: string, enabled: boolean) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/skills/enabled',
      {
        method: 'POST',
        body: JSON.stringify({
          path: skillPath,
          enabled,
        }),
      },
    ));
  }

  async createUserMcp(input: McpEditInput) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>('/api/capabilities/mcp', {
      method: 'POST',
      body: JSON.stringify({ server_id: input.serverId, config: input.config, secret_changes: input.secretChanges }),
    }));
  }

  async removeUserSkill(skill: UserSkillCapability) {
    if (!skill.removalIdentity) throw new Error('技能目录尚未确认，请刷新后重试。');
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>('/api/capabilities/skills/remove', {
      method: 'POST', body: JSON.stringify({path: skill.path, expected: skill.removalIdentity}),
    }));
  }

  async updateUserMcp(input: McpEditInput, expectedIdentity: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/mcp/${encodeURIComponent(input.serverId)}`, {
        method: 'PUT',
        body: JSON.stringify({ config: input.config, secret_changes: input.secretChanges, expected_identity: expectedIdentity,
          retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false }),
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

  async importUserMcp(input: McpImportSelection) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>('/api/capabilities/mcp/import', {method: 'POST', body: JSON.stringify(input)}));
  }

  async removeUserMcp(serverId: string, expectedIdentity: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/mcp/${encodeURIComponent(serverId)}`, {
        method: 'DELETE', body: JSON.stringify({ expected_identity: expectedIdentity }),
      }));
  }

  async modelMcpAuthorizationStatus(target: McpAuthorizationResult['authorization']) {
    return apiRequest<{ state: string; error: string | null }>('/api/capabilities/mcp/authorization-result', {
      method: 'POST', body: JSON.stringify({ owner: target.owner, config_identity: target.config_identity }),
    });
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

  async installUserPlugin(sourcePath: string, options?: PluginImportOptions) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      '/api/capabilities/plugins/install',
      {
        method: 'POST',
        body: JSON.stringify({ source_path: sourcePath, ...options }),
      },
    ));
  }

  async setUserPluginEnabled(
    pluginId: string,
    packageInstallId: string,
    enabled: boolean,
    connectionReview: NonNullable<UserPluginCapability['connectionReview']>,
  ) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/plugins/${encodeURIComponent(pluginId)}/enabled`,
      {
        method: 'POST',
        body: JSON.stringify({
          enabled,
          package_install_id: packageInstallId,
          connection_review: connectionReview,
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

  async removeUserPlugin(pluginId: string, packageInstallId: string) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/plugins/${encodeURIComponent(pluginId)}`,
      {
        method: 'DELETE',
        body: JSON.stringify({ package_install_id: packageInstallId }),
      },
    ));
  }

  async updatePluginConnection(plugin: UserPluginCapability, connection: PluginMcpConnection, input: PluginMcpEditInput) {
    return projectUserCapabilityOperation(await apiRequest<Record<string, unknown>>(
      `/api/capabilities/plugins/${encodeURIComponent(plugin.id)}/mcp/${encodeURIComponent(connection.serverId)}`,
      { method: 'PUT', body: JSON.stringify({ package_install_id: plugin.packageInstallId,
        expected_overlay: connection.overlay, overlay: input.overlay, secret_changes: input.secretChanges,
        retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false }) },
    ));
  }

  async openCapabilityRoot(root: 'agents' | 'pulsara'): Promise<void> {
    const target = await apiRequest<{ path: string; reveal: boolean }>(`/api/capabilities/roots/${root}/open`, { method: 'POST' });
    const host = desktopHost();
    if (!host) throw new Error('请从 Pulsara 桌面应用打开目录。');
    await host.openPath(target);
  }

  async deleteSession(sessionId: string): Promise<{ status: 'DELETED' | 'ABSENT'; session_id: string }> {
    return apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE', body: JSON.stringify({ confirm_permanent_delete: true }),
    });
  }

  async listArchivedSessions(): Promise<SessionSummary[]> {
    const result = await apiRequest<{ sessions: Record<string, unknown>[] }>('/api/sessions/archived');
    return result.sessions.map(projectSessionSummary);
  }

  async deleteArchivedSession(sessionId: string) {
    return apiRequest<{
      status: 'DELETED' | 'ABSENT' | 'NOT_ARCHIVED'; session_id: string;
      cleanup_warning: { code: 'SESSION_DELETE_QUARANTINED'; message: string } | null;
    }>(`/api/sessions/${encodeURIComponent(sessionId)}/archived`, {
      method: 'DELETE', body: JSON.stringify({ confirm_permanent_delete: true }),
    });
  }

  async renameSession(sessionId: string, title: string): Promise<{ session_id: string; title: string }> {
    const result = await apiRequest<{ session_id: string; title: string }>(`/api/sessions/${encodeURIComponent(sessionId)}/title`, {
      method: 'PUT', body: JSON.stringify({ title }),
    });
    this.sessionTitleRevision += 1;
    return result;
  }

  async archiveSession(sessionId: string): Promise<{ status: 'ARCHIVED'; session_id: string }> {
    return apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}/archive`, { method: 'POST', body: '{}' });
  }

  async unarchiveSession(sessionId: string): Promise<{ status: 'OPEN'; session_id: string }> {
    return apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}/unarchive`, { method: 'POST', body: '{}' });
  }

  async forkConversation(sessionId: string, anchorEntryId: string): Promise<ForkOutcome> {
    return this.sessionRequest(sessionId, `/api/sessions/${encodeURIComponent(sessionId)}/fork`, {
      method: 'POST', body: JSON.stringify({ anchor_entry_id: anchorEntryId }),
    });
  }

  async readSession(sessionId: string): Promise<SessionSummary | null> {
    const payload = await apiRequest<{ session: Record<string, unknown> | null }>(`/api/sessions/${encodeURIComponent(sessionId)}`);
    return payload.session ? projectSessionSummary(payload.session) : null;
  }

  async completeWorkspacePaths(sessionId: string, prefix: string, cursor: string | null, signal: AbortSignal): Promise<import('./file-reference').WorkspacePathPage> {
    return apiRequest(`/api/sessions/${encodeURIComponent(sessionId)}/path-candidates`, {
      method: 'POST', body: JSON.stringify({ prefix, cursor }), signal,
    });
  }

  async pickWorkspaceDirectory(signal?: AbortSignal): Promise<string | null> {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const host = desktopHost();
    if (!host) throw new Error('请从 Pulsara 桌面应用选择目录。');
    if (host.platform === 'win32') {
      const {pickLinuxWorkspaceDirectory} = await import('../components/workspace-directory-picker');
      return pickLinuxWorkspaceDirectory({
        chooseNative: () => host.chooseWorkspaceDirectory(),
        list: (path, cursor, requestSignal) => {
          const query = new URLSearchParams();
          if (path !== undefined) query.set('path', path);
          if (cursor !== null) query.set('cursor', cursor);
          return apiRequest(`/api/workspace-directories?${query}`, {signal: requestSignal});
        },
        create: (path, name, requestSignal) => apiRequest('/api/workspace-directories', {method:'POST', body:JSON.stringify({path, name}), signal:requestSignal}),
        select: (path, requestSignal) => apiRequest('/api/workspace-directories/select', {method:'POST', body:JSON.stringify({path}), signal:requestSignal}),
      }, signal);
    }
    const path = await host.chooseDirectory();
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    return path;
  }

  async createSession(selection: SessionWorkspaceSelection, modelCallBinding?: ModelCallBindingPayload): Promise<SessionSummary> {
    const payload = await apiRequest<{ session: Record<string, unknown> }>('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ ...(selection.kind === 'quick'
        ? { workspace_kind: 'quick' }
        : { workspace_kind: 'project', workspace_path: selection.path }),
        ...(modelCallBinding ? { model_call_binding: modelCallBinding } : {}),
      }),
    });
    return projectSessionSummary(payload.session);
  }

  async connect(sessionId: string, takeover = false): Promise<RuntimeConnection | CanonicalHistoryView> {
    const attempt = ++this.connectAttempt;
    const payload = await apiRequest<ConnectPayload | {view: "history"; session_id: string; workspace_availability: WorkspaceAvailability; snapshot: {snapshot: {snapshot: ProtocolCanonicalSnapshot}}}>(
      `/api/sessions/${encodeURIComponent(sessionId)}/connections`,
      {
        method: 'POST',
        body: JSON.stringify({
          browser_instance_id: this.browserInstanceId,
          ...(takeover ? { takeover: true } : {}),
        }),
      },
    );
    if ('view' in payload && payload.view === 'history') {
      const history = new CanonicalHistoryView(payload.session_id, payload.workspace_availability, payload.snapshot.snapshot.snapshot);
      await history.initialize();
      if (attempt === this.connectAttempt) this.chatConnection = undefined;
      return history;
    }
    const connection = new LocalRuntimeConnection(payload as ConnectPayload);
    await connection.initialize();
    if (attempt === this.connectAttempt) this.chatConnection = connection;
    return connection;
  }

  async openAgentReader(rootSessionId: string, agentSessionId: string, signal?: AbortSignal): Promise<AgentConversationReader> {
    const base = `/api/sessions/${encodeURIComponent(rootSessionId)}/agents/${encodeURIComponent(agentSessionId)}`;
    const payload = await apiRequest<(ConnectPayload & {view:'live'}) | {
      view:'history'; session_id:string; workspace_availability:WorkspaceAvailability;
      snapshot:{snapshot:{snapshot:ProtocolCanonicalSnapshot}};
    }>(`${base}/reader`, {method:'POST', body:JSON.stringify({browser_instance_id:this.browserInstanceId}), signal});
    const release = async () => {
      if ('connection_id' in payload && payload.connection_id) {
        await apiRequest(`/api/connections/${encodeURIComponent(payload.connection_id)}`, {method:'DELETE', keepalive:true}).catch(() => undefined);
      }
    };
    if (!['live', 'history'].includes(payload.view) || payload.session_id !== agentSessionId
      || (payload.view === 'live' && (payload.role !== 'observer' || !payload.connection_id
        || payload.snapshot?.snapshot?.session_id !== agentSessionId)) || signal?.aborted) {
      await release();
      signal?.throwIfAborted();
      throw new RuntimeApiError('READER_IDENTITY_INVALID', '子代理阅读连接身份不一致。', false);
    }
    if (payload.view === 'history') {
      if (payload.snapshot?.snapshot?.snapshot?.session_id !== agentSessionId) throw new RuntimeApiError('READER_IDENTITY_INVALID', '子代理快照身份不一致。', false);
      const history = new CanonicalHistoryView(payload.session_id, payload.workspace_availability,
        payload.snapshot.snapshot.snapshot, 'SUBAGENT', `${base}/history`, `${base}/queue`);
      try { await history.initialize(); signal?.throwIfAborted(); return history; }
      catch (error) { await history.close(); throw error; }
    }
    let connection: LocalRuntimeConnection | undefined;
    try {
      connection = new LocalRuntimeConnection(payload, 'SUBAGENT', `${base}/history`, `${base}/queue`);
      await connection.initialize();
      signal?.throwIfAborted();
      return connection;
    } catch (error) { if (connection) await connection.close(); else await release(); throw error; }
  }

  createAgentContentReader(rootSessionId: string, agentSessionId: string): AgentContentReader {
    const base = `/api/sessions/${encodeURIComponent(rootSessionId)}/agents/${encodeURIComponent(agentSessionId)}`;
    const read = async <T>(operation: (reader: CanonicalHistoryView) => Promise<T>): Promise<T> => {
      const reader = new CanonicalHistoryView(agentSessionId, {path:'', outcome:'UNAVAILABLE', reason:null},
        {session_id:agentSessionId, entries:[], event_sequence_cut:'0'}, 'SUBAGENT', `${base}/history`, `${base}/queue`);
      try { return await operation(reader); } finally { await reader.close(); }
    };
    return {
      readToolArtifact:(id, offset, max) => read(reader => reader.readToolArtifact(id, offset, max)),
      readPromptImage:image => read(reader => reader.readPromptImage(image)),
      readImageReference:(ref, signal) => read(reader => reader.readImageReference(ref, signal)),
      readVisualizationContent:(id, ordinal, digest, size, item, signal) => read(reader => reader.readVisualizationContent(id, ordinal, digest, size, item, signal)),
      readVisualizationThumbnail:(id, ordinal, digest, size, signal) => read(reader => reader.readVisualizationThumbnail(id, ordinal, digest, size, signal)),
    };
  }
}

abstract class CanonicalHistoryReader {
  protected entries = new Map<string, ProtocolEntry>();
  protected control: ProtocolCanonicalControl = {};
  protected eventSequence = 0;
  protected eventSequenceCursor = "0";
  protected writerGeneration = 0;
  protected interruptionNotices = new Map<string, ProtocolTurnInterruptionNotice>();
  protected mergeInterruptionNotices(notices: ProtocolTurnInterruptionNotice[]) {
    for (const notice of notices) {
      if (!['EXECUTED_TURN', 'IMPORTED_HISTORY'].includes(notice.owner_kind) || !notice.owner_id || !notice.reason
        || !Number.isSafeInteger(numeric(notice.display_after_entry_sequence)) || numeric(notice.display_after_entry_sequence) < 1
        || !Number.isFinite(Date.parse(notice.terminal_at_utc))) {
        throw new RuntimeApiError('INTERRUPTION_NOTICE_INVALID', '中断详情暂时无法读取。', true);
      }
      const key = `${notice.owner_kind}:${notice.owner_id}`;
      const previous = this.interruptionNotices.get(key);
      if (previous && (previous.reason !== notice.reason || previous.public_detail !== notice.public_detail
        || previous.terminal_at_utc !== notice.terminal_at_utc
        || numeric(previous.display_after_entry_sequence) !== numeric(notice.display_after_entry_sequence))) {
        throw new RuntimeApiError('INTERRUPTION_NOTICE_CONFLICT', '中断历史返回了不一致的事实。', true);
      }
      this.interruptionNotices.set(key, notice);
    }
  }
  protected projectInterruptionNotices(messages: Message[]): TurnInterruptionNotice[] {
    const mounted: TurnInterruptionNotice[] = [];
    const readingIds = this.readingIds();
    for (const notice of this.interruptionNotices.values()) {
      const sequence = numeric(notice.display_after_entry_sequence);
      if (![...this.entries.values()].some(entry => numeric(entry.entry_sequence) === sequence)) continue;
      const source = [...this.entries.values()].find(entry => numeric(entry.entry_sequence) === sequence);
      if (!source || !readingIds.has(source.entry_id)) continue;
      const message = source && messages.find(message => message.id === source.entry_id || message.sourceEntryIds?.includes(source.entry_id));
      mounted.push({ownerKind: notice.owner_kind, ownerId: notice.owner_id, reason: notice.reason,
        publicDetail: notice.public_detail, terminalAtUtc: notice.terminal_at_utc,
        displayAfterEntrySequence: sequence, displayAfterEntryId: source?.entry_id, displayAfterMessageId: message?.id});
    }
    return mounted.sort((a,b) => a.displayAfterEntrySequence-b.displayAfterEntrySequence || a.ownerId.localeCompare(b.ownerId));
  }
  abstract readonly sessionId: string;
  protected get queuePath(): string { return `/api/sessions/${encodeURIComponent(this.sessionId)}/queue`; }
  protected get historyPath(): string { return `/api/sessions/${encodeURIComponent(this.sessionId)}/history`; }
  protected abstract post<T>(suffix: string, body: object, signal?: AbortSignal): Promise<T>;
  protected abstract project(): RuntimeProjection;

  async readQueuePage(cursor?: string, signal?: AbortSignal, lane?: QueueLane): Promise<{items: QueuedPrompt[]; nextCursor: string | null}> {
    const page = await apiRequest<{items: NonNullable<ProtocolCanonicalControl['prompt_queue']>; next_cursor: string | null}>(
      this.queuePath, {method:'POST', body:JSON.stringify({...(cursor ? {cursor} : {}), ...(lane ? {delivery_mode: 'STEER_ACTIVE_TURN', target_turn_id: lane.targetTurnId} : {})}), signal});
    const current = [] as NonNullable<ProtocolCanonicalControl['prompt_queue']>;
    const refreshed: QueuedPrompt[] = [];
    for (const item of page.items) {
      if (!item.queue_item_id) continue;
      try {
        await this.hydrateContentReference(item.content, {queue_item_id:item.queue_item_id}, '等待处理的输入暂时无法完整读取。', signal);
      } catch (error) {
        if (!(error instanceof RuntimeApiError) || error.code !== 'CONTENT_QUEUE_NOT_PENDING') throw error;
        if (!await this.reconcileReadOnlyQueueItem(item.queue_item_id, signal)) continue;
        const latest = await this.readQueueItem(item.queue_item_id, signal);
        if (latest) refreshed.push(latest);
        continue;
      }
      current.push(item);
    }
    this.rememberQueueContent({prompt_queue:current});
    return {items:[...projectQueuedPrompts({prompt_queue:current}), ...refreshed].sort((a,b) => compareSequence(a.sequence,b.sequence)), nextCursor:page.next_cursor};
  }

  async readQueueStatus(queueItemId: string, signal?: AbortSignal): Promise<QueueItemStatus | null> {
    const result = await apiRequest<{item: (NonNullable<ProtocolCanonicalControl['prompt_queue']>[number] & {consumed_entry_id?: string}) | null}>(
      this.queuePath, {method:'POST', body:JSON.stringify({queue_item_id:queueItemId}), signal});
    const item = result.item;
    if (!item) return null;
    if (item.queue_item_id !== queueItemId || !['PENDING','CONSUMED','CANCELLED','REJECTED'].includes(item.status ?? '') || !['NEW_TURN','STEER_ACTIVE_TURN'].includes(item.delivery_mode ?? '') || (item.status === 'CONSUMED' && !item.consumed_entry_id)) throw new RuntimeApiError('PROMPT_TRANSITION_IDENTITY_INVALID', '输入状态返回了不一致的身份。', true);
    const previous = this.control.prompt_queue?.find(row=>row.queue_item_id===queueItemId && row.status==='PENDING');
    if (previous && item.status !== 'PENDING') {
      previous.status = item.status;
      const countKey = previous.delivery_mode === 'STEER_ACTIVE_TURN' ? 'pending_steer_total_count' : 'prompt_queue_total_count';
      if (this.control[countKey] !== undefined) this.control[countKey] = Math.max(0, numeric(this.control[countKey]) - 1);
      this.changed();
    }
    return { queueItemId, status: item.status as QueueItemStatus['status'], deliveryMode: deliveryMode(item.delivery_mode), targetTurnId: item.target_turn_id || undefined, consumedEntryId: item.consumed_entry_id || undefined };
  }

  async readQueueItem(queueItemId: string, signal?: AbortSignal): Promise<QueuedPrompt | null> {
    const result = await apiRequest<{item: (NonNullable<ProtocolCanonicalControl['prompt_queue']>[number] & {consumed_entry_id?:string}) | null}>(
      this.queuePath, {method:'POST', body:JSON.stringify({queue_item_id:queueItemId}), signal});
    if (!result.item) { this.knownQueuedContent.delete(queueItemId); return null; }
    if (result.item.queue_item_id !== queueItemId) throw new RuntimeApiError('PROMPT_TRANSITION_IDENTITY_INVALID', '等待处理的输入返回了不一致的身份。', true);
    if (result.item.status !== 'PENDING') {
      if (result.item.status === 'CONSUMED' && result.item.consumed_entry_id) {
        void this.readLocatedEntry(result.item.consumed_entry_id, signal ?? new AbortController().signal).then(()=>this.changed()).catch(()=>{});
      } else if (result.item.status !== 'CONSUMED') this.knownQueuedContent.delete(queueItemId);
      return null;
    }
    try {
      await this.hydrateContentReference(result.item.content, {queue_item_id:queueItemId}, '等待处理的输入暂时无法完整读取。', signal);
    } catch (error) {
      if (!(error instanceof RuntimeApiError) || error.code !== 'CONTENT_QUEUE_NOT_PENDING') throw error;
      if (await this.reconcileReadOnlyQueueItem(queueItemId, signal)) throw error;
      return null;
    }
    this.rememberQueueContent({prompt_queue:[result.item]});
    return projectQueuedPrompts({prompt_queue:[result.item]})[0] ?? null;
  }

  protected async reconcileReadOnlyQueueItem(queueItemId: string, signal?: AbortSignal): Promise<boolean> {
    const result = await apiRequest<{item: (NonNullable<ProtocolCanonicalControl['prompt_queue']>[number] & {consumed_entry_id?:string}) | null}>(
      this.queuePath, {method:'POST', body:JSON.stringify({queue_item_id:queueItemId}), signal});
    if (result.item && result.item.queue_item_id !== queueItemId) throw new RuntimeApiError('PROMPT_TRANSITION_IDENTITY_INVALID', '等待处理的输入返回了不一致的身份。', true);
    if (!result.item || (result.item.status !== 'PENDING' && result.item.status !== 'CONSUMED')) this.knownQueuedContent.delete(queueItemId);
    if (result.item?.status === 'CONSUMED' && result.item.consumed_entry_id) {
      void this.readLocatedEntry(result.item.consumed_entry_id, signal ?? new AbortController().signal).then(()=>this.changed()).catch(()=>{});
    }
    return result.item?.status === 'PENDING';
  }
  protected pages: BrowserHistoryPage[] = [];
  private listeners = new Set<() => void>();
  private contentReads = new Map<string, Promise<void>>();
  private contentErrors = new Map<string, string>();
  private immutableReads = new Map<string, Promise<string>>();
  // Page-inline bytes already passed that page's byte admission. Only later
  // exact-body reads (or bytes carried into a reference-only replacement) need
  // a separate viewport lifetime. Weak keys retain neither bodies nor entries.
  private supplementalBodies = new WeakSet<ProtocolContent>();
  protected readerClosed = false;
  protected controlReadSequence = '0';
  private pageCounter = 0;
  private initialContextReady = false;
  private knownQueuedContent = new Map<string, CanonicalPromptContent>();
  private tailPage?: BrowserHistoryPage;
  private pairedTailPage?: BrowserHistoryPage;
  private locatingEntries = new Set<string>();
  private readingEntryId?: string;
  private navigation?: object;
  private readingOwner: object = {};
  // An explicit process-end navigation selects admitted pages, independently
  // of adjacent cached pages. This is a single continuous reading aperture,
  // not another history cache or a folded gap in the timeline.
  private readingAperture?: BrowserHistoryPage[];
  private entryWindowBytes = new WeakMap<ProtocolEntry, number>();

  private readingCluster(): BrowserHistoryPage[] {
    const clusters = this.connectedWindows(this.readingAperture ?? this.pages);
    if (this.readingEntryId) return clusters.find(cluster => cluster.some(page => page.entryIds.includes(this.readingEntryId!))) ?? [];
    return clusters.find(cluster => this.tailPage && cluster.includes(this.tailPage)) ?? clusters.at(-1) ?? [];
  }
  private readingIds(): Set<string> { return new Set(this.readingCluster().flatMap(page => page.entryIds)); }
  private readingAtLatest(): boolean { return !this.tailPage || this.readingIds().has(this.tailPage.entryIds.at(-1)!); }
  protected readingMessages(messages: Message[]): Message[] {
    const ids = this.readingIds(), atLatest = this.readingAtLatest();
    return messages.filter(message => this.entries.has(message.id) ? ids.has(message.id) : atLatest);
  }

  async readLatestHistory(signal?: AbortSignal): Promise<RuntimeProjection> {
    signal?.throwIfAborted();
    const navigation = {}; this.navigation = navigation;
    const target = this.tailPage;
    if (target) await waitForRead(this.loadPageContext(target), signal);
    signal?.throwIfAborted();
    if (this.readerClosed || this.navigation !== navigation) throw new DOMException('Navigation superseded', 'AbortError');
    const latest = this.connectedWindows().find(cluster => this.tailPage && cluster.includes(this.tailPage));
    if (target && (target.contextState !== 'ready' || !latest?.includes(target))) throw new Error('最新消息暂时无法读取，请重试。');
    this.readingEntryId = undefined;
    this.readingAperture = undefined;
    this.readingOwner = navigation;
    return this.project();
  }

  async readProcessEnd(entryId: string, signal: AbortSignal): Promise<RuntimeProjection> {
    signal.throwIfAborted();
    if (this.readerClosed) throw new DOMException('Reader closed', 'AbortError');
    const navigation = {}; this.navigation = navigation;
    this.locatingEntries.add(entryId);
    try {
      // A complete short turn already inside the reading interval needs no
      // navigation. Reuse canonical page context and the existing admission
      // budget; a large connected cache alone is never permission to expand
      // an unbounded turn. Bodies remain under normal viewport hydration.
      const reading = this.readingCluster();
      const ids = this.readingIds();
      const cachedFinal = this.entries.get(entryId);
      const group = reading.flatMap(page => page.contextState === 'ready' && !page.needsFreshContext
        && page.context && compareSequence(page.context.event_sequence_cut, this.presentationBoundaryEventSequence) >= 0
        ? page.context.groups : []).find(group => group.final_entry_id === entryId);
      const initial = group && ids.has(group.initial_entry_id) ? this.entries.get(group.initial_entry_id) : undefined;
      if (initial && cachedFinal && ids.has(entryId) && cachedFinal.entry_kind === 'ASSISTANT_MESSAGE'
        && (cachedFinal.root_final || cachedFinal.fork_eligible) && this.entryContentState(entryId) === 'ready') {
        const span = [...ids].map(id => this.entries.get(id)!).filter(entry =>
          compareSequence(entry.entry_sequence, initial.entry_sequence) >= 0
          && compareSequence(entry.entry_sequence, cachedFinal.entry_sequence) <= 0);
        const complete = BigInt(span.length) === BigInt(decimal(cachedFinal.entry_sequence)) - BigInt(decimal(initial.entry_sequence)) + BigInt(1);
        if (complete && span.length <= 128 && span.reduce((bytes, entry) => bytes + (this.entryWindowBytes.get(entry) ?? Infinity), 0) <= (2 << 20)
          && span.every(entry => reading.some(page => page.contextState === 'ready' && !page.needsFreshContext
            && page.context && compareSequence(page.context.event_sequence_cut, this.presentationBoundaryEventSequence) >= 0
            && page.entryIds.includes(entry.entry_id)))) {
          // No navigation took place. Keep ownership so in-flight edge
          // reads still attach to this same continuous interval.
          return this.project();
        }
      }
      // Missing or long processes still select one server-admitted window.
      this.readingAperture ??= this.readingCluster();
      const window = await this.historyPost<BrowserWindow>('window', {
        anchor_entry_id: entryId, maximum_entries: 128, maximum_serialized_bytes: 2 << 20,
      }, signal);
      signal.throwIfAborted();
      if (this.readerClosed || this.navigation !== navigation) throw new DOMException('Navigation superseded', 'AbortError');
      const final = window.entries.find(entry => entry.entry_id === entryId);
      if (window.session_id !== this.sessionId || !final || final.entry_kind !== 'ASSISTANT_MESSAGE'
        || !(final.root_final || final.fork_eligible)) throw new RuntimeApiError('HISTORY_FINAL_MISSING', '找不到这轮的最终回复。', false);
      decimal(window.cut_sequence); decimal(window.event_sequence_cut);
      if (window.entries.some((entry, index) => compareSequence(entry.entry_sequence, window.cut_sequence) > 0
        || (index > 0 && BigInt(decimal(entry.entry_sequence)) !== BigInt(decimal(window.entries[index - 1].entry_sequence)) + BigInt(1)))) {
        throw new Error('处理过程窗口不连续。');
      }
      for (const cursor of [window.older_cursor, window.newer_cursor]) if (cursor && (cursor.session_id !== this.sessionId
        || compareSequence(cursor.cut_sequence, window.cut_sequence) !== 0
        || compareSequence(cursor.event_sequence_cut ?? '0', window.event_sequence_cut) !== 0)) {
        throw new Error('处理过程窗口游标身份不一致。');
      }
      this.mergeEntries(window.entries);
      const page = this.installPage(window.entries, window, window.older_cursor, window.newer_cursor, false, true)!;
      await waitForRead(this.loadPageContext(page), signal);
      if (page.contextState !== 'ready') throw new RuntimeApiError('HISTORY_CONTEXT_UNAVAILABLE', '处理过程暂时无法读取，请重试。', true);
      await this.hydrateEntries([entryId], signal);
      if (this.entryContentState(entryId) !== 'ready') throw new RuntimeApiError('HISTORY_CONTENT_UNAVAILABLE', '最终回复暂时无法读取，请重试。', true);
      signal.throwIfAborted();
      if (this.readerClosed || this.navigation !== navigation) throw new DOMException('Navigation superseded', 'AbortError');
      this.readingEntryId = entryId;
      this.readingAperture = [page];
      this.readingOwner = navigation;
      // Reopening may admit the exact same entries with fresh boundary cursors.
      // Keep physical read/tail owners, but do not accumulate settled duplicate
      // pages for repeated disclosure clicks.
      this.pages = this.pages.filter(candidate => candidate === page || candidate === this.tailPage || candidate === this.pairedTailPage
        || candidate.read || candidate.contextRead || candidate.entryIds.length !== page.entryIds.length
        || candidate.entryIds.some((id, index) => id !== page.entryIds[index]));
      return this.project();
    } finally { this.locatingEntries.delete(entryId); }
  }

  protected transientHistoryEntries(): string[] { return []; }
  protected releaseSettledHistory(visible: ReadonlySet<string>): boolean { void visible; return false; }
  protected hasCanonicalContext(entryId: string): boolean {
    return this.entries.has(entryId) && this.pages.some(page => page.contextState === 'ready' && page.entryIds.includes(entryId));
  }

  retainHistory(interest: HistoryResidence): RuntimeProjection | undefined {
    if (this.readerClosed || !this.initialContextReady || (interest.readingOwner && interest.readingOwner !== this.readingOwner)) return;
    // Hidden or not-yet-laid-out views supply no reading position.
    if (!interest.visibleEntryIds.length && !interest.pinnedEntryIds.length) return;
    // Observation has no paired history cut until its original context read
    // succeeds. Keep the bridge and handoffs intact rather than mutating state
    // without publishing it or inventing a cursor.
    const reading = this.readingCluster();
    const readingIds = new Set(reading.flatMap(page => page.entryIds));
    const visual = [...interest.visibleEntryIds, ...interest.pinnedEntryIds, ...(interest.anchorEntryId ? [interest.anchorEntryId] : [])].filter(id => readingIds.has(id));
    // A queued report from a retired DOM must never navigate back to its island.
    if (!visual.length) return;
    const previousAnchor = this.readingEntryId;
    if (interest.followLatest && this.readingAtLatest()) this.readingEntryId = undefined;
    else this.readingEntryId = (interest.anchorEntryId && readingIds.has(interest.anchorEntryId) ? interest.anchorEntryId : visual[0]);
    if (this.tailPage && !this.tailPage.cut) return previousAnchor !== this.readingEntryId ? this.project() : undefined;
    const requested = new Set(visual);
    if (this.readingEntryId) requested.add(this.readingEntryId);
    let changed = this.releaseSettledHistory(requested) || previousAnchor !== this.readingEntryId;
    const bodies = new Set([...requested, ...this.locatingEntries, ...this.transientHistoryEntries()]);
    for (const key of this.contentReads.keys()) bodies.add(key.replace(/:reasoning$/, ''));
    for (const entry of this.entries.values()) if (entry.input_source?.queue_item_id) {
      const key = entry.input_source.queue_item_id;
      // Until the current control owner also observes consumption, retain the
      // accepted identity that suppresses its stale pending-queue projection.
      if (this.control.prompt_queue?.some(item => item.queue_item_id === entry.input_source!.queue_item_id
        && item.status === 'PENDING')) bodies.add(entry.entry_id);
      if (!this.knownQueuedContent.has(key)) continue;
      if (this.hasCanonicalContext(entry.entry_id) && (!requested.has(entry.entry_id) || this.entryContentState(entry.entry_id) === 'ready')) {
        this.knownQueuedContent.delete(key); changed = true;
      }
      else bodies.add(entry.entry_id);
    }
    const ordered = [...this.pages].sort((a, b) => compareSequence(this.entries.get(a.entryIds[0])!.entry_sequence, this.entries.get(b.entryIds[0])!.entry_sequence));
    const keep = new Set<BrowserHistoryPage>();
    const orderedEntries = [...this.entries.values()].sort((a, b) => compareSequence(a.entry_sequence, b.entry_sequence));
    const positions = new Map(orderedEntries.map((entry, index) => [entry.entry_id, index]));
    const keepIds = new Set(bodies);
    // Each side is one existing history-request budget, independent of observe
    // batch size. Whole admitted pages and explicit consumers may exceed it.
    const buffer = (edge: number, direction: -1 | 1, includeEdge = false) => {
      let bytes = 0, count = 0;
      for (let index = edge + (includeEdge ? 0 : direction); index >= 0 && index < orderedEntries.length; index += direction) {
        const entry = orderedEntries[index];
        const previous = orderedEntries[index - direction];
        if (!(includeEdge && index === edge) && previous && BigInt(decimal(entry.entry_sequence)) !== BigInt(decimal(previous.entry_sequence)) + BigInt(direction)) break;
        const size = this.entryWindowBytes.get(entry) ?? 0;
        if (count >= 128 || bytes + size > (2 << 20)) break;
        keepIds.add(entry.entry_id); bytes += size; count++;
      }
    };
    const visiblePositions = [...requested].map(id => positions.get(id)).filter((index): index is number => index !== undefined);
    if (visiblePositions.length) {
      const first = visiblePositions.reduce((a, b) => Math.min(a, b)), last = visiblePositions.reduce((a, b) => Math.max(a, b));
      for (let index = first; index <= last; index++) keepIds.add(orderedEntries[index].entry_id);
      buffer(first, -1); buffer(last, 1);
    }
    const tail = this.tailPage?.entryIds.at(-1);
    if (tail && positions.has(tail)) buffer(positions.get(tail)!, -1, true);
    for (const page of ordered) if (page.entryIds.some(id => keepIds.has(id))) keep.add(page);
    if (this.tailPage) keep.add(this.tailPage);
    if (this.pairedTailPage) keep.add(this.pairedTailPage);
    for (const page of this.pages) if (page.read || page.contextRead) keep.add(page);
    const retained = ordered.filter(page => keep.has(page));
    changed ||= retained.length !== this.pages.length;
    const tailCut = this.pairedTailPage?.cut;
    const sequence = (page: BrowserHistoryPage, last = false) => decimal(this.entries.get(last ? page.entryIds.at(-1)! : page.entryIds[0])!.entry_sequence);
    for (let index = 0; index < retained.length; index++) {
      const page = retained[index], position = ordered.indexOf(page);
      const first = sequence(page), last = sequence(page, true);
      if (!page.older && position > 0 && (!index || BigInt(sequence(retained[index - 1], true)) + BigInt(1) < BigInt(first)) && page.cut) {
        page.older = {session_id: this.sessionId, ...page.cut, entry_sequence: first, direction: 'older'};
        changed = true;
      }
      if (!page.read && position + 1 < ordered.length && (index + 1 === retained.length || BigInt(last) + BigInt(1) < BigInt(sequence(retained[index + 1])))) {
        // A live tail can be newer than this old page's cut. Use a complete
        // server-admitted pair, never observation counters or a mixed pair.
        const cut = page.cut && compareSequence(page.cut.cut_sequence, sequence(ordered.at(-1)!, true)) >= 0 ? page.cut : tailCut;
        if (cut && compareSequence(last, cut.cut_sequence) < 0 && (!page.newer
          || page.newer.cut_sequence !== cut.cut_sequence || page.newer.event_sequence_cut !== cut.event_sequence_cut
          || page.newer.entry_sequence !== last)) {
          page.newer = {session_id: this.sessionId, ...cut, entry_sequence: last, direction: 'newer'};
          changed = true;
        }
      }
    }
    const ids = new Set(retained.flatMap(page => page.entryIds));
    this.pages = this.pages.filter(page => keep.has(page));
    if (this.readingAperture) this.readingAperture = this.readingAperture.filter(page => keep.has(page));
    for (const [id, entry] of this.entries) {
      if (!ids.has(id)) { this.entries.delete(id); changed = true; continue; }
      if (bodies.has(id)) continue;
      for (const ref of [entry.content, ...entry.blocks?.map(block => block.content) ?? [], ...entry.reasoning_blocks?.map(block => block.content) ?? []]) {
        if (ref?.inline_content !== undefined && this.supplementalBodies.has(ref)) {
          delete ref.inline_content; changed = true;
        }
      }
    }
    for (const key of this.contentErrors.keys()) if (!ids.has(key.replace(/:reasoning$/, ''))) this.contentErrors.delete(key);
    const sequences = new Set([...this.entries.values()].map(entry => decimal(entry.entry_sequence)));
    for (const [key, notice] of this.interruptionNotices) if (!sequences.has(decimal(notice.display_after_entry_sequence))) this.interruptionNotices.delete(key);
    if (changed) return this.project();
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  protected changed() { if (!this.readerClosed) for (const listener of this.listeners) listener(); }
  protected disposeReader() { this.readerClosed = true; this.listeners.clear(); }
  protected historyPost<T>(suffix: string, body: object, signal?: AbortSignal): Promise<T> {
    if (this.readerClosed) throw new RuntimeApiError('HISTORY_CLOSED', '会话已切换。', false);
    return apiRequest<T>(`${this.historyPath}/${suffix}`, {
      method: 'POST', body: JSON.stringify(body), signal,
    });
  }
  protected mergeEntries(entries: ProtocolEntry[], observed = false) {
    for (const entry of entries) {
      const old = this.entries.get(entry.entry_id);
      // A stale page cannot discard verified immutable bytes or overwrite
      // dynamic metadata already installed by the ordered observation owner.
      if (old) {
        const preserve = (next?: ProtocolContent, previous?: ProtocolContent) => {
          if (next && previous?.inline_content !== undefined && next.digest === previous.digest && String(next.size ?? 0) === String(previous.size ?? 0)) {
            if (next.inline_content === undefined) this.supplementalBodies.add(next);
            next.inline_content = previous.inline_content;
          }
        };
        preserve(entry.content, old.content);
        for (const block of [...entry.blocks ?? [], ...entry.reasoning_blocks ?? []]) {
          preserve(block.content, [...old.blocks ?? [], ...old.reasoning_blocks ?? []].find(value => value.block_id === block.block_id)?.content);
        }
        if (!observed) { entry.root_final = old.root_final; entry.fork_eligible = old.fork_eligible; }
      }
      this.entryWindowBytes.set(entry, new TextEncoder().encode(JSON.stringify(entry)).byteLength);
      this.entries.set(entry.entry_id, entry);
    }
  }
  protected installPage(entries: ProtocolEntry[], cut?: {cut_sequence: string | number; event_sequence_cut?: string | number}, older?: ProtocolHistoryCursor | BrowserWindowCursor, newer?: BrowserWindowCursor, tail = false, independentAperture = false): BrowserHistoryPage | undefined {
    if (!entries.length) return;
    const previousTail = this.tailPage?.entryIds.at(-1);
    // A refreshed snapshot can lead the ordered observation cursor. Replaying
    // its older acceptance batches must not replace the actual retained tail.
    tail = tail && (!this.tailPage || compareSequence(entries.at(-1)!.entry_sequence,
      this.entries.get(this.tailPage.entryIds.at(-1)!)!.entry_sequence) >= 0);
    const appendToAperture = tail && this.readingAperture && previousTail
      && this.readingIds().has(previousTail)
      && BigInt(decimal(entries[0].entry_sequence)) <= BigInt(decimal(this.entries.get(previousTail)!.entry_sequence)) + BigInt(1);
    const ids = entries.map(entry => entry.entry_id);
    if (independentAperture) this.pages = this.pages.filter(page => page === this.tailPage || page === this.pairedTailPage
      || this.readingAperture?.includes(page) || page.read || page.contextRead || page.entryIds.length !== ids.length
      || page.entryIds.some((id, index) => id !== ids[index]));
    // A newly selected aperture needs the response's original boundary cursors.
    // Reusing a cached page whose edge was already consumed would hide older
    // history, or require mutating another in-flight page owner's cursor.
    const same = !independentAperture && this.pages.find(page => page.entryIds.length === ids.length && page.entryIds.every((id, index) => id === ids[index]));
    if (same) {
      if (tail) { this.tailPage = same; if (same.cut) this.pairedTailPage = same; }
      return same;
    }
    const page: BrowserHistoryPage = {id: `history:${++this.pageCounter}`, entryIds: ids,
      cut: cut && cut.event_sequence_cut !== undefined ? {cut_sequence: decimal(cut.cut_sequence), event_sequence_cut: decimal(cut.event_sequence_cut)} : undefined,
      older, newer, loading: false, contextState: 'loading'};
    this.pages.push(page);
    if (appendToAperture) this.readingAperture!.push(page);
    if (tail) { this.tailPage = page; if (page.cut) this.pairedTailPage = page; }
    void this.loadPageContext(page);
    return page;
  }
  protected installTail(snapshot: ProtocolCanonicalSnapshot) {
    const entries = snapshot.entries ?? [];
    if (!entries.length) this.initialContextReady = true;
    this.mergeEntries(entries, true);
    this.mergeInterruptionNotices(snapshot.interruption_notices ?? []);
    this.installPage(entries, snapshot.entry_sequence_cut !== undefined ? {cut_sequence: snapshot.entry_sequence_cut, event_sequence_cut: snapshot.event_sequence_cut} : snapshot.older_history_cursor, snapshot.older_history_cursor, undefined, true);
  }
  protected async loadPageContext(page: BrowserHistoryPage, refresh = false, requiredEventCut?: string): Promise<void> {
    if (this.readerClosed || !this.pages.includes(page)) return;
    if (refresh) page.needsFreshContext = true;
    if (requiredEventCut && (!page.requiredContextEventCut || compareSequence(requiredEventCut, page.requiredContextEventCut) > 0)) page.requiredContextEventCut = requiredEventCut;
    if (page.contextRead) {
      await page.contextRead;
      if (this.readerClosed || page.contextState === 'error') return;
      if (page.needsFreshContext || (page.context && compareSequence(page.context.event_sequence_cut, this.presentationBoundaryEventSequence) < 0)) return this.loadPageContext(page, true, this.presentationBoundaryEventSequence);
      return;
    }
    if (!refresh && page.contextState === 'ready' && !page.needsFreshContext && page.context && compareSequence(page.context.event_sequence_cut, this.presentationBoundaryEventSequence) >= 0) return;
    const fresh = page.needsFreshContext === true;
    const operation = (async () => {
      page.contextState = 'loading'; page.contextError = undefined;
      try {
        const value = await this.historyPost<BrowserPageContext>('context', {
          entry_ids: page.entryIds, ...(!fresh ? page.cut : undefined), maximum_serialized_bytes: 2 << 20,
        });
        if (this.readerClosed || !this.pages.includes(page)) return;
        if (value.session_id !== this.sessionId || value.entry_ids.length !== page.entryIds.length
          || !page.entryIds.every(id => value.entry_ids.includes(id))) throw new Error('历史展示上下文身份不一致。');
        decimal(value.cut_sequence); decimal(value.event_sequence_cut);
        page.cut ??= {cut_sequence: decimal(value.cut_sequence), event_sequence_cut: decimal(value.event_sequence_cut)};
        if (page === this.tailPage) this.pairedTailPage = page;
        if (fresh && page.requiredContextEventCut && compareSequence(value.event_sequence_cut, page.requiredContextEventCut) < 0) throw new Error('历史展示上下文早于已观察到的边界。');
        if (!page.context || compareSequence(value.event_sequence_cut, page.context.event_sequence_cut) >= 0) page.context = value;
        if (fresh && (!page.requiredContextEventCut || compareSequence(value.event_sequence_cut, page.requiredContextEventCut) >= 0)) page.needsFreshContext = false;
        page.contextState = page.needsFreshContext ? 'loading' : 'ready';
        if (page === this.pages[0] && page.contextState === 'ready') this.initialContextReady = true;
        this.mergeInterruptionNotices(value.interruption_notices ?? []);
      } catch (error) {
        if (this.readerClosed || !this.pages.includes(page)) return;
        page.contextState = 'error'; page.contextError = error instanceof Error ? error.message : '历史分组暂时无法读取。';
      }
      this.changed();
    })();
    page.contextRead = operation;
    // Publish only after installing the in-flight read so a retry caused by a
    // subscriber joins this request rather than advancing the history cursor.
    this.changed();
    try { await operation; } finally { if (page.contextRead === operation) page.contextRead = undefined; }
    if (!this.readerClosed && this.pages.includes(page) && page.contextState !== 'error' && page.context && compareSequence(page.context.event_sequence_cut, this.presentationBoundaryEventSequence) < 0) {
      return this.loadPageContext(page, true, this.presentationBoundaryEventSequence);
    }
  }
  protected presentationBoundaryEventSequence = "0";
  private pendingBoundaryEvents = new Set<string>();
  private boundaryRead?: Promise<void>;
  private boundaryError?: string;
  protected refreshBoundaryOccurrences(eventIds: string[]) {
    for (const id of eventIds) this.pendingBoundaryEvents.add(id);
    if (this.boundaryRead || !this.pendingBoundaryEvents.size || this.readerClosed) return;
    const ids = [...this.pendingBoundaryEvents].slice(0, 128); // Existing observe event admission.
    const operation = (async () => {
      this.boundaryError = undefined;
      try {
        const value = await this.historyPost<BrowserPageContext>('context', {entry_ids: [], boundary_event_ids: ids, maximum_serialized_bytes: 2 << 20});
        if (this.readerClosed) return;
        if (value.session_id !== this.sessionId || !ids.every(id => value.boundary_entries?.some(boundary => boundary.event_id === id))) throw new Error('历史边界身份不一致。');
        const edges = new Set(value.boundary_entries!.map(boundary => boundary.entry_id));
        for (const page of this.pages) if (page.entryIds.some(id => edges.has(id))) void this.loadPageContext(page, true, value.event_sequence_cut);
        for (const id of ids) this.pendingBoundaryEvents.delete(id);
      } catch (error) { if (!this.readerClosed) this.boundaryError = error instanceof Error ? error.message : '历史边界暂时无法读取，请重试。'; }
      this.changed();
    })();
    this.boundaryRead = operation;
    void operation.finally(() => {
      if (this.boundaryRead === operation) this.boundaryRead = undefined;
      if (!this.boundaryError && this.pendingBoundaryEvents.size) this.refreshBoundaryOccurrences([]);
    });
  }
  private connectedWindows(pages = this.pages): BrowserHistoryPage[][] {
    const ordered = pages.filter(page => page.entryIds.length).sort((a,b) => compareSequence(this.entries.get(a.entryIds[0])!.entry_sequence, this.entries.get(b.entryIds[0])!.entry_sequence));
    const clusters: BrowserHistoryPage[][] = [];
    let lastSequence = BigInt(0);
    for (const page of ordered) {
      const cluster = clusters.at(-1);
      const end = BigInt(decimal(this.entries.get(page.entryIds.at(-1)!)!.entry_sequence));
      if (cluster && BigInt(decimal(this.entries.get(page.entryIds[0])!.entry_sequence)) <= lastSequence + BigInt(1)) {
        cluster.push(page);
        if (end > lastSequence) lastSequence = end;
      } else { clusters.push([page]); lastSequence = end; }
    }
    return clusters;
  }
  protected historyState(): BrowserHistoryState {
    const reading = this.readingCluster();
    const windows = (reading.length ? [reading] : []).map(cluster => {
      const ids = [...new Set(cluster.flatMap(page => page.entryIds))].sort((a,b) => compareSequence(this.entries.get(a)!.entry_sequence, this.entries.get(b)!.entry_sequence));
      const first = cluster.find(page => page.entryIds[0] === ids[0])!;
      const last = cluster.findLast(page => page.entryIds.at(-1) === ids.at(-1))!;
      return {id: this.pages.find(page => cluster.includes(page))!.id, firstEntryId: ids[0], lastEntryId: ids.at(-1), entryIds: ids,
        hasOlder: Boolean(first.older), hasNewer: Boolean(last.newer), loading: cluster.some(page => page.loading),
        boundaryDirection: cluster.find(page => page.loading || page.error)?.readDirection,
        error: cluster.find(page => page.error || page.contextError)?.error ?? cluster.find(page => page.contextError)?.contextError,
        contextState: cluster.some(page => page.contextState === 'error') ? 'error' as const : cluster.some(page => page.contextState !== 'ready') ? 'loading' as const : 'ready' as const,
        contextError: cluster.find(page => page.contextError)?.contextError};
    });
    const compactions = new Map<string, BrowserPageContext['compactions'][number]>();
    for (const page of reading) for (const item of page.context?.compactions ?? []) {
      // A boundary belongs here only when the actual canonical edge is held.
      if (page.entryIds.some(id => compareSequence(this.entries.get(id)!.entry_sequence, item.adopted_after_entry_sequence) === 0)) compactions.set(item.event_id, item);
    }
    return {readingOwner: this.readingOwner, atLatest: this.readingAtLatest(), initialReady: this.initialContextReady || !this.pages.length, initialError: this.initialContextReady ? undefined : this.pages[0]?.contextError, windows, hasOlder: Boolean(windows[0]?.hasOlder), loading: windows.some(window => window.loading), error: this.boundaryError ?? windows.find(window => window.error)?.error,
      compactions: [...compactions.values()].map(item => ({eventId: item.event_id, adoptedAfterEntrySequence: numeric(item.adopted_after_entry_sequence), turnId: item.turn_id, displayAfterEntryId: [...this.entries.values()].find(entry => compareSequence(entry.entry_sequence, item.adopted_after_entry_sequence) === 0)?.entry_id}))};
  }
  async readHistory(windowId?: string, direction: 'older' | 'newer' = 'older', signal?: AbortSignal): Promise<RuntimeProjection> {
    signal?.throwIfAborted();
    if (this.boundaryError) { this.refreshBoundaryOccurrences([]); await this.boundaryRead; return this.project(); }
    const cluster = this.readingAperture ? this.readingCluster()
      : this.connectedWindows().find(pages => pages.some(page => page.id === windowId)) ?? this.readingCluster();
    const readingOwner = this.readingOwner;
    if (this.readingAperture && windowId && !cluster.some(page => page.id === windowId)) return this.project();
    if (!cluster?.length) return this.project();
    const pendingContext = cluster.filter(page => page.contextRead || page.contextState !== 'ready');
    if (pendingContext.length) {
      await waitForRead(Promise.all(pendingContext.map(page => this.loadPageContext(page))), signal);
      return this.project();
    }
    const ordered = [...cluster].sort((a,b) => compareSequence(this.entries.get(a.entryIds[0])!.entry_sequence, this.entries.get(b.entryIds[0])!.entry_sequence));
    const page = direction === 'older' ? ordered[0] : ordered.at(-1)!;
    if (page.read) { await page.read; signal?.throwIfAborted(); return this.project(); }
    const cursor = direction === 'older' ? page.older : page.newer;
    if (!cursor) return this.project();
    const operation = (async () => {
      page.loading = true; page.readDirection = direction; page.error = undefined; this.changed();
      try {
        let entries: ProtocolEntry[], older: ProtocolHistoryCursor | BrowserWindowCursor | undefined, newer: BrowserWindowCursor | undefined;
        if ('direction' in cursor) {
          const window = await this.historyPost<BrowserWindow>('window', {cursor, maximum_entries: 128, maximum_serialized_bytes: 2 << 20}, signal);
          if (window.session_id !== this.sessionId || compareSequence(window.cut_sequence, cursor.cut_sequence) !== 0
            || compareSequence(window.event_sequence_cut, cursor.event_sequence_cut ?? '0') !== 0) throw new Error('历史窗口身份不一致。');
          if (window.entries.some(entry => direction === 'older' ? compareSequence(entry.entry_sequence, cursor.entry_sequence) >= 0
            : compareSequence(entry.entry_sequence, cursor.entry_sequence) <= 0)) throw new Error('历史窗口没有向请求方向推进。');
          for (const next of [window.older_cursor, window.newer_cursor]) if (next && (next.session_id !== this.sessionId
            || compareSequence(next.cut_sequence, cursor.cut_sequence) !== 0
            || compareSequence(next.event_sequence_cut, cursor.event_sequence_cut ?? '0') !== 0
            || (next.direction === direction && (direction === 'older'
              ? compareSequence(next.entry_sequence, cursor.entry_sequence) >= 0 : compareSequence(next.entry_sequence, cursor.entry_sequence) <= 0)))) throw new Error('历史窗口游标没有向前推进。');
          entries = window.entries; older = window.older_cursor; newer = window.newer_cursor;
        } else {
          const frame = await this.historyPost<{history_page?: ProtocolHistoryPage; error?: ProtocolError}>('history', {cursor, maximum_entries: 128, maximum_serialized_bytes: 2 << 20}, signal);
          assertProtocolFrame(frame);
          if (!frame.history_page) throw new Error('较早的会话内容暂时无法加载。');
          const next = frame.history_page.older_history_cursor;
          if (frame.history_page.has_more && (!next || next.session_id !== cursor.session_id
            || compareSequence(next.cut_sequence, cursor.cut_sequence) !== 0
            || (next.event_sequence_cut === undefined) !== (cursor.event_sequence_cut === undefined)
            || compareSequence(next.event_sequence_cut ?? '0', cursor.event_sequence_cut ?? '0') !== 0
            || compareSequence(next.entry_sequence, cursor.entry_sequence) >= 0)) throw new Error('历史游标没有向前推进。');
          entries = frame.history_page.entries ?? []; older = frame.history_page.has_more ? next : undefined;
        }
        signal?.throwIfAborted();
        if (this.readerClosed || !this.pages.includes(page)) return;
        if (direction === 'older' ? page.older !== cursor : page.newer !== cursor) return;
        this.mergeEntries(entries);
        const admitted = this.installPage(entries, cursor, older, newer, false,
          Boolean(this.readingAperture?.includes(page) && this.readingOwner === readingOwner));
        if (admitted && this.readingAperture?.includes(page) && this.readingOwner === readingOwner) {
          const edge = this.entries.get(direction === 'older' ? page.entryIds[0] : page.entryIds.at(-1)!)!;
          const neighbour = this.entries.get(direction === 'older' ? admitted.entryIds.at(-1)! : admitted.entryIds[0])!;
          if (BigInt(decimal(neighbour.entry_sequence)) !== BigInt(decimal(edge.entry_sequence)) + BigInt(direction === 'older' ? -1 : 1)) {
            throw new Error('历史窗口不连续。');
          }
          if (!this.readingAperture.includes(admitted)) this.readingAperture.push(admitted);
        }
        if (direction === 'older') page.older = undefined; else page.newer = undefined;
      } catch (error) {
        if (!signal?.aborted && !this.readerClosed) page.error = error instanceof Error ? error.message : '历史暂时无法加载。';
        if (signal?.aborted) throw error;
      } finally { page.loading = false; this.changed(); }
    })();
    page.read = operation;
    try { await operation; } finally { if (page.read === operation) page.read = undefined; }
    return this.project();
  }
  locateEntry(entryId: string, signal: AbortSignal): Promise<RuntimeProjection> {
    return this.navigateToEntry(entryId, signal, false);
  }
  private async navigateToEntry(entryId: string, signal: AbortSignal, annotation: boolean): Promise<RuntimeProjection> {
    const navigation = {}; this.navigation = navigation;
    this.locatingEntries.add(entryId);
    try {
      await this.readLocatedEntry(entryId, signal);
      const entry = this.entries.get(entryId);
      if (annotation && (!entry || !['ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST'].includes(entry.entry_kind))) throw new RuntimeApiError('ANNOTATION_SOURCE_MISSING', '当前会话中找不到来源回复。', false);
      if (this.entryContentState(entryId) !== 'ready') throw new RuntimeApiError('HISTORY_CONTENT_UNAVAILABLE', this.contentErrors.get(entryId) ?? '来源正文暂时无法读取，请重试。', true);
      signal.throwIfAborted();
      if (this.readerClosed || this.navigation !== navigation) throw new DOMException('Navigation superseded', 'AbortError');
      const withinAperture = this.readingAperture && this.readingIds().has(entryId);
      this.readingEntryId = entryId;
      if (!withinAperture) this.readingAperture = undefined;
      this.readingOwner = navigation;
      return this.project();
    }
    finally { this.locatingEntries.delete(entryId); }
  }
  private async readLocatedEntry(entryId: string, signal: AbortSignal): Promise<RuntimeProjection> {
    signal.throwIfAborted();
    if (!this.entries.has(entryId)) {
      const window = await this.historyPost<BrowserWindow>('window', {anchor_entry_id: entryId, maximum_entries: 128, maximum_serialized_bytes: 2 << 20}, signal);
      signal.throwIfAborted();
      if (this.readerClosed) throw new RuntimeApiError('HISTORY_CLOSED', '会话已切换。', false);
      if (window.session_id !== this.sessionId || !window.entries.some(entry => entry.entry_id === entryId)) throw new RuntimeApiError('HISTORY_ANCHOR_MISSING', '找不到来源记录。', false);
      this.mergeEntries(window.entries);
      this.installPage(window.entries, window, window.older_cursor, window.newer_cursor);
    }
    for (const page of this.pages.filter(page => page.entryIds.includes(entryId))) {
      await waitForRead(this.loadPageContext(page), signal);
      if (page.contextRead) await waitForRead(page.contextRead, signal);
    }
    signal.throwIfAborted();
    const contextPage = this.pages.find(page => page.entryIds.includes(entryId) && page.contextState === 'ready');
    if (!contextPage) throw new RuntimeApiError('HISTORY_CONTEXT_UNAVAILABLE', '来源区域的展示上下文暂时无法读取，请重试。', true);
    // A result may render inside its request, and an expanded process also
    // needs reasoning. Read that actual host before switching: waiting for an
    // offscreen IntersectionObserver would deadlock source navigation.
    const cluster = this.readingAperture && this.readingIds().has(entryId) ? this.readingCluster()
      : this.connectedWindows().find(pages => pages.some(page => page.entryIds.includes(entryId)))!;
    const ids = new Set(cluster.flatMap(page => page.entryIds));
    const entries = [...this.entries.values()].filter(entry => ids.has(entry.entry_id))
      .sort((a, b) => compareSequence(a.entry_sequence, b.entry_sequence));
    const hosts = [
      ...projectEntries(entries, new Set(), () => true),
    ];
    const host = hosts.find(item => item.id === entryId || item.sourceEntryIds?.includes(entryId));
    const sourceIds = host?.sourceEntryIds ?? [host?.id ?? entryId];
    const hostPages = this.pages.filter(page => page.entryIds.some(id => sourceIds.includes(id)));
    await waitForRead(Promise.all(hostPages.map(page => this.loadPageContext(page))), signal);
    if (sourceIds.some(id => !hostPages.some(page => page.contextState === 'ready' && page.entryIds.includes(id)))) {
      throw new RuntimeApiError('HISTORY_CONTEXT_UNAVAILABLE', '来源区域的展示上下文暂时无法读取，请重试。', true);
    }
    const hostEntry = this.entries.get(host?.id ?? entryId);
    const includeReasoning = !(hostEntry?.root_final || hostEntry?.fork_eligible) || host?.id !== entryId;
    await this.hydrateEntries(sourceIds, signal, includeReasoning);
    for (const id of sourceIds) if (this.entryContentState(id) !== 'ready' || (includeReasoning && this.entryContentState(id, true) !== 'ready')) {
      throw new RuntimeApiError('HISTORY_CONTENT_UNAVAILABLE', this.contentErrors.get(`${id}:reasoning`) ?? '来源正文暂时无法读取，请重试。', true);
    }
    signal.throwIfAborted();
    return this.project();
  }
  locateAnnotationSource(entryId: string, signal: AbortSignal): Promise<RuntimeProjection> {
    return this.navigateToEntry(entryId, signal, true);
  }
  protected entryContentState(entryId: string, reasoning = false): 'unloaded' | 'loading' | 'ready' | 'error' {
    const key = reasoning ? `${entryId}:reasoning` : entryId;
    if (this.contentErrors.has(key)) return 'error';
    if (this.contentReads.has(key)) return 'loading';
    const entry = this.entries.get(entryId);
    const refs = entry ? reasoning ? entry.reasoning_blocks?.map(block => block.content) ?? [] : [entry.content, ...entry.blocks?.map(block => block.content) ?? []] : [];
    return refs.some(ref => ref && ref.inline_content === undefined && numeric(ref.size) > 0) ? 'unloaded' : 'ready';
  }
  async hydrateEntries(entryIds: string[], signal?: AbortSignal, includeReasoning = false): Promise<RuntimeProjection> {
    await waitForRead(Promise.all(entryIds.map(async entryId => {
      signal?.throwIfAborted();
      const key = includeReasoning ? `${entryId}:reasoning` : entryId;
      const pending = this.contentReads.get(key);
      if (pending) { await waitForRead(pending, signal); return; }
      const entry = this.entries.get(entryId);
      if (!entry || (this.entryContentState(entryId) === 'ready' && (!includeReasoning || this.entryContentState(entryId, true) === 'ready'))) return;
      this.contentErrors.delete(key);
      const operation = (async () => {
        try {
          await this.hydrateContentReference(entry.content, {entry_id: entryId}, '这条会话内容暂时无法完整读取。');
          for (const block of [...entry.blocks ?? [], ...(includeReasoning ? entry.reasoning_blocks ?? [] : [])]) await this.hydrateContentReference(block.content, {entry_id: entryId, block_id: block.block_id}, '这段正文暂时无法完整读取。');
          this.contentErrors.delete(entryId);
        } catch (error) { if (!this.readerClosed && this.entries.has(entryId)) this.contentErrors.set(key, error instanceof Error ? error.message : '正文暂时无法读取。'); }
      })();
      this.contentReads.set(key, operation); this.changed();
      try { await operation; } finally { if (this.contentReads.get(key) === operation) this.contentReads.delete(key); this.changed(); }
    })), signal);
    signal?.throwIfAborted();
    return this.project();
  }
  protected presentationHistory(messages: Message[]): BrowserHistoryState {
    const state = this.historyState();
    const host = (id?: string) => messages.find(message => message.id === id || message.sourceEntryIds?.includes(id ?? ''))?.id;
    for (const window of state.windows) {
      window.firstEntryId = host(window.firstEntryId) ?? window.entryIds.map(id => host(id)).find(Boolean);
      window.lastEntryId = host(window.lastEntryId) ?? window.entryIds.map(id => host(id)).filter(Boolean).at(-1);
    }
    for (const item of state.compactions) item.displayAfterEntryId = host(item.displayAfterEntryId);
    return state;
  }
  protected windowJoinPredicate(): (a: string, b: string) => boolean {
    const membership = new Map<string, number>();
    this.connectedWindows().forEach((cluster, index) => { for (const page of cluster) for (const id of page.entryIds) membership.set(id, index); });
    const reading = this.readingAperture && this.readingIds();
    return (a,b) => (!reading || (!reading.has(a) && !reading.has(b)) || (reading.has(a) && reading.has(b)))
      && membership.has(a) && membership.get(a) === membership.get(b);
  }
  protected rememberQueueContent(control: ProtocolCanonicalControl) {
    for (const item of control.prompt_queue ?? []) if (item.queue_item_id && item.content?.inline_content !== undefined) {
      this.knownQueuedContent.set(item.queue_item_id, decodePromptContent(item.content, {kind:'queue', queueItemId: item.queue_item_id}));
    }
  }
  protected decorateHistory(messages: Message[]): Message[] {
    const contexts = new Map<string, {entry: BrowserPageContext['entry_contexts'][number]; page: BrowserHistoryPage}>();
    for (const page of this.pages) for (const entry of page.context?.entry_contexts ?? []) {
      const previous = contexts.get(entry.entry_id);
      if (!previous || compareSequence(page.context!.event_sequence_cut, previous.page.context!.event_sequence_cut) >= 0) contexts.set(entry.entry_id, {entry, page});
    }
    const windows = this.historyState().windows;
    for (const message of messages) {
      const sourceIds = message.sourceEntryIds ?? [message.id];
      message.sourceEntryIds = sourceIds;
      const context = contexts.get(message.id);
      const page = context?.page ?? this.pages.find(page => page.entryIds.includes(message.id));
      message.historyGroupId = context?.entry.segment_id;
      // Context may name a final after its historical cut. This is only an
      // explicit navigation target, never evidence that this slice completed.
      message.historyFinalEntryId = context?.page.context?.groups.find(group =>
        group.owner_kind === context.entry.owner_kind && group.owner_id === context.entry.owner_id)?.final_entry_id;
      message.historyInitialEntryId = context?.page.context?.groups.find(group =>
        group.owner_kind === context.entry.owner_kind && group.owner_id === context.entry.owner_id)?.initial_entry_id;
      // Installed exact-entry context remains usable while its page refreshes.
      // The page/window owns refresh loading and errors; new entries without
      // installed context still wait before receiving a semantic group.
      message.historyContextState = context ? 'ready' : page?.contextState;
      message.historyContextError = context ? undefined : page?.contextError;
      message.historyWindowId = windows.find(window => window.entryIds.includes(message.id))?.id;
      message.historyEarlierUnloaded = Boolean(context && context.entry.segment_start_entry_id && !windows.some(window => window.entryIds.includes(context.entry.segment_start_entry_id)));
      // Live observations keep their advisory presentation until canonical
      // metadata supplies a stable host; they are never verified bodies.
      if (!this.entries.has(message.id)) continue;
      const states = sourceIds.map(id => this.entryContentState(id));
      message.contentState = states.includes('error') ? 'error' : states.includes('loading') ? 'loading' : states.includes('unloaded') ? 'unloaded' : 'ready';
      message.contentError = sourceIds.map(id => this.contentErrors.get(id)).find(Boolean);
      // An attached tool result can still be loading while the request/card
      // itself is fully known. Do not replace that visible card with a loader.
      if (message.role === 'assistant' && message.traces?.length
        && this.entryContentState(message.id) === 'ready' && message.contentState !== 'ready') {
        message.pendingAssistant = { body: message.body, reasoning: message.reasoning, traces: message.traces };
      }
      if (message.inputSource?.queueItemId) {
        const key = message.inputSource.queueItemId;
        if (message.contentState === 'ready') this.knownQueuedContent.delete(key);
        else message.pendingContent = this.knownQueuedContent.get(key);
      }
      message.reasoningContentState = this.entryContentState(message.id, true);
      message.reasoningContentError = this.contentErrors.get(`${message.id}:reasoning`);

    }
    return messages;
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

  async readVisualizationThumbnail(entryId: string, ordinal: number, digest: string, size: number, signal: AbortSignal): Promise<string> {
    const result = await this.historyPost<{ image: string }>('visualization-thumbnail', {
      entry_id: entryId, ordinal, digest, size,
    }, signal);
    if (!result.image?.startsWith('data:image/png;base64,')) throw new Error('缩略图暂时无法生成。');
    return result.image;
  }

  async readVisualizationContent(
    entryId: string, ordinal: number, digest: string, size: number, item?: VisualizationOccurrence, signal?: AbortSignal,
  ): Promise<string | Uint8Array> {
    if (!entryId || !Number.isSafeInteger(ordinal) || ordinal < 0
      || !/^sha256:[0-9a-f]{64}$/.test(digest) || !Number.isSafeInteger(size) || size < 1) {
      throw new RuntimeApiError('VISUALIZATION_REFERENCE_INVALID', '可视化内容缺少精确身份。', false);
    }
    const image = Boolean(item?.imageRef);
    const expected: ContentMedia = image
      ? { media_type: item?.mediaType, codec: 'binary', image_ref: item?.imageRef, width: item?.width, height: item?.height }
      : { media_type: 'text/html', codec: 'utf-8' };
    const bytes = await this.readExactContentBytes(
      { entry_id: entryId, visualization_ordinal: ordinal }, digest, size,
      '可视化内容暂时无法读取。', !image, signal, expected,
    );
    return image ? bytes : new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  }

  async readImageReference(imageRef: string, signal?: AbortSignal): Promise<SavedImageData> {
    if (!isCanonicalIdentity(imageRef, 'image')) throw new Error('图片引用无效。');
    let metadata: ContentMedia | undefined;
    const bytes = await this.readExactContentBytes(
      { image_ref: imageRef }, undefined, undefined, '图片暂时无法读取。', false, signal,
      { image_ref: imageRef }, value => { metadata = value; },
    );
    return { bytes, mediaType: metadata!.media_type!, width: metadata!.width!, height: metadata!.height! };
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

  async readToolArtifact(
    resultEntryId: string,
    offsetChars: number,
    maxChars = 32_000,
  ): Promise<ToolArtifactPage> {
    const frame = await this.historyPost<{
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

  protected async hydrateContentReference(
    reference: ProtocolContent | undefined,
    target: { entry_id: string; block_id?: string } | { queue_item_id: string },
    publicMessage: string,
    signal?: AbortSignal,
  ): Promise<void> {
    if (!reference || reference.inline_content !== undefined || numeric(reference.size) === 0) return;
    const expectedSize = numeric(reference.size);
    const expectedDigest = reference.digest ?? '';
    if ('entry_id' in target) {
      const resident = this.entries.get(target.entry_id);
      const ref = target.block_id ? [...resident?.blocks ?? [], ...resident?.reasoning_blocks ?? []].find(block => block.block_id === target.block_id)?.content : resident?.content;
      if (ref?.inline_content !== undefined && ref.digest === expectedDigest && numeric(ref.size) === expectedSize) {
        reference.inline_content = ref.inline_content;
        this.supplementalBodies.add(reference);
        return;
      }
    }
    const key = JSON.stringify([target, expectedDigest, expectedSize]);
    let pending = this.immutableReads.get(key);
    if (!pending) {
      pending = this.readExactContentBytes(target, expectedDigest, expectedSize, publicMessage, true)
        .then(bytes => {
          const encoded = encodeBase64Bytes(bytes);
          return encoded;
        });
      this.immutableReads.set(key, pending);
      void pending.finally(() => { if (this.immutableReads.get(key) === pending) this.immutableReads.delete(key); }).catch(() => undefined);
    }
    const body = await waitForRead(pending, signal);
    signal?.throwIfAborted();
    if (this.readerClosed) return;
    reference.inline_content = body;
    this.supplementalBodies.add(reference);
    if ('entry_id' in target) {
      const current = this.entries.get(target.entry_id);
      const currentRef = target.block_id ? [...current?.blocks ?? [], ...current?.reasoning_blocks ?? []].find(block => block.block_id === target.block_id)?.content : current?.content;
      if (currentRef?.digest === expectedDigest && numeric(currentRef.size) === expectedSize) {
        currentRef.inline_content = body;
        this.supplementalBodies.add(currentRef);
      }
    }
  }

  protected async readExactContentBytes(
    target: (
      | { entry_id: string; block_id?: string }
      | { queue_item_id: string }
      | { image_ref: string }
    ) & { image_ref_ordinal?: number; visualization_ordinal?: number },
    expectedDigest: string | undefined,
    expectedSize: number | undefined,
    publicMessage: string,
    requireUtf8: boolean,
    signal?: AbortSignal,
    expectedMedia?: ContentMedia,
    onMetadata?: (value: ContentMedia) => void,
  ): Promise<Uint8Array> {
    const chunks: Uint8Array[] = [];
    let offset = 0;
    let frozenMedia: ContentMedia | undefined;
    while (true) {
      signal?.throwIfAborted();
      const frame = await this.historyPost<{
        content?: {
          digest?: string; complete_size?: string | number; offset_bytes?: string | number;
          content?: string; complete?: boolean; media_type?: string; codec?: string; image_ref?: string; width?: number; height?: number;
        };
        error?: ProtocolError;
      }>('read-content', {
        ...target,
        offset_bytes: offset,
        limit_bytes: 1 << 20,
      }, signal);
      signal?.throwIfAborted();
      assertProtocolFrame(frame);
      const chunk = frame.content;
      if (chunk && offset === 0) {
        expectedDigest ??= chunk.digest;
        expectedSize ??= numeric(chunk.complete_size);
      }
      if (!expectedDigest || !/^sha256:[0-9a-f]{64}$/.test(expectedDigest) || !Number.isSafeInteger(expectedSize) || expectedSize! < 0) {
        throw new RuntimeApiError('CONTENT_REFERENCE_INVALID', publicMessage, true);
      }
      if (expectedMedia && chunk) {
        const media: ContentMedia = { media_type: chunk.media_type, codec: chunk.codec,
          image_ref: chunk.image_ref, width: chunk.width === undefined ? undefined : numeric(chunk.width), height: chunk.height === undefined ? undefined : numeric(chunk.height) };
        // Match MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES: existing per-read physical image bound.
        const image = Boolean(expectedMedia.image_ref);
        if ((image && (!isCanonicalIdentity(media.image_ref, 'image') || !['image/png', 'image/jpeg', 'image/webp'].includes(media.media_type ?? '') || media.codec !== 'binary' || !Number.isSafeInteger(media.width) || media.width! < 1 || !Number.isSafeInteger(media.height) || media.height! < 1 || expectedSize! < 1 || expectedSize! > (16 << 20)))
          || (!image && (media.image_ref !== undefined || media.width !== undefined || media.height !== undefined))
          || Object.entries(expectedMedia).some(([key, value]) => value !== undefined && media[key as keyof ContentMedia] !== value)
          || (frozenMedia && Object.keys(media).some(key => media[key as keyof ContentMedia] !== frozenMedia![key as keyof ContentMedia]))) {
          throw new RuntimeApiError('CONTENT_REFERENCE_INVALID', publicMessage, true);
        }
        frozenMedia = media;
        onMetadata?.(media);
      }
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
      if (bytes.length === 0 || offset >= expectedSize!) {
        throw new RuntimeApiError('CONTENT_REFERENCE_INVALID', publicMessage, true);
      }
    }
    if (offset !== expectedSize) {
      throw new RuntimeApiError('CONTENT_REFERENCE_INVALID', publicMessage, true);
    }
    const complete = new Uint8Array(expectedSize!);
    let cursor = 0;
    for (const chunk of chunks) {
      complete.set(chunk, cursor);
      cursor += chunk.length;
    }
    await verifyContentIntegrity(complete, expectedDigest, publicMessage, requireUtf8);
    signal?.throwIfAborted();
    return complete;
  }
}

export interface WorkspaceAvailability {
  path: string;
  outcome: 'AVAILABLE' | 'MISSING' | 'UNAVAILABLE';
  reason: string | null;
}

export class CanonicalHistoryView extends CanonicalHistoryReader {
  readonly view = 'history';
  private closed = false;
  constructor(readonly sessionId: string, readonly availability: WorkspaceAvailability,
              snapshot: ProtocolCanonicalSnapshot, private readonly scopeKind: 'ROOT' | 'SUBAGENT' = 'ROOT',
              private readonly historyRoot?: string, private readonly queueRoot?: string) {
    super();
    this.installTail(snapshot);
    this.control = snapshot.control ?? {};
    this.eventSequence = numeric(snapshot.event_sequence_cut);
    this.controlReadSequence = decimal(snapshot.event_sequence_cut ?? 0);
  }
  protected get historyPath() { return this.historyRoot ?? super.historyPath; }
  protected get queuePath() { return this.queueRoot ?? super.queuePath; }
  protected post<T>(suffix: string, body: object, signal?: AbortSignal): Promise<T> {
    if (this.closed) throw new RuntimeApiError('HISTORY_CLOSED', '会话已切换。', false);
    return apiRequest(`${this.historyPath}/${suffix}`, {
      method: 'POST', body: JSON.stringify(body), signal,
    });
  }
  async initialize() {
    // Only metadata is installed here. The observer and composer do not wait
    // for page context or immutable content reads.
    void this.hydrateReadonlyQueue();
  }
  private async hydrateReadonlyQueue() {
    for (const item of this.control.prompt_queue ?? []) {
      try { await this.hydrateContentReference(item.content, {queue_item_id: item.queue_item_id!}, '待运行输入暂时无法读取。'); }
      catch (error) {
        if (this.scopeKind === 'SUBAGENT' && item.queue_item_id
          && error instanceof RuntimeApiError && error.code === 'CONTENT_QUEUE_NOT_PENDING') {
          try { await this.reconcileReadOnlyQueueItem(item.queue_item_id); await this.refreshSnapshot(false); return; }
          catch { /* Preserve the last known projection until a successful read. */ }
        }
      }
    }
    this.rememberQueueContent(this.control);
    this.changed();
  }
  current(): RuntimeProjection { return this.project(); }
  async snapshot(): Promise<RuntimeProjection> {
    return this.refreshSnapshot(true);
  }
  private async refreshSnapshot(hydrate: boolean): Promise<RuntimeProjection> {
    const frame = await this.post<{snapshot?: {snapshot?: ProtocolCanonicalSnapshot}; error?:ProtocolError}>('snapshot', {});
    assertProtocolFrame(frame);
    const snapshot = frame.snapshot?.snapshot;
    if (!snapshot || snapshot.session_id !== this.sessionId) throw new RuntimeApiError('PROTOCOL_RESPONSE_INVALID', '会话快照身份不一致。', true);
    this.installTail(snapshot);
    this.control = snapshot.control ?? {};
    this.eventSequence = numeric(snapshot.event_sequence_cut);
    this.controlReadSequence = decimal(snapshot.event_sequence_cut ?? 0);
    if (hydrate) void this.hydrateReadonlyQueue();
    this.changed();
    return this.project();
  }
  async observe(signal?: AbortSignal): Promise<RuntimeProjection> {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    return this.snapshot();
  }
  async close() { this.closed = true; this.disposeReader(); }
  protected project(): RuntimeProjection {
    const canonical = [...this.entries.values()].sort((a,b) => numeric(a.entry_sequence) - numeric(b.entry_sequence));
    const sameWindow = this.windowJoinPredicate();
    const messages = projectEntries(canonical, new Set(), sameWindow);
    const canonicalTurnIds = orderedTurnIdsFromEntries(canonical, this.scopeKind);
    this.decorateHistory(messages);
    const readingMessages = this.readingMessages(messages);
    return {messages: readingMessages, canonicalInputs: canonicalInputs(messages), history: this.presentationHistory(readingMessages), interruptionNotices: this.projectInterruptionNotices(readingMessages), canonicalTurnIds,
      presentationNotices: [], contextCompaction: projectContextCompaction(this.control),
      initialContextBase: this.control.initial_context_base, isRunning: this.scopeKind === 'SUBAGENT' && Boolean(this.control.active_turns?.length),
      pendingSteerPage: projectPendingSteerPage(this.control),
      queuedCount: numeric(this.control.prompt_queue_total_count), queuedPrompts: projectQueuedPrompts(this.control),
      promptTransitions: [], planMode: Boolean(this.control.active_plan_workflow),
      control: this.control, liveControl: {}, todo: undefined, eventSequence: this.eventSequence,
      liveOwnerEpoch: 0, liveRevision: 0, liveControlOwnerEpoch: 0, liveControlRevision: 0};
  }
}

class LocalRuntimeConnection extends CanonicalHistoryReader implements RuntimeConnection {
  readonly view = 'live' as const;
  readonly availability: WorkspaceAvailability;
  readonly sessionId: string;
  readonly role: 'observer' | 'controller';
  readonly generation: number;
  private readonly connectionId: string;
  private drafts = new Map<string, LiveDraft>();
  private liveResults = new Map<string, LiveToolResult>();
  protected releaseSettledHistory(visible: ReadonlySet<string>): boolean {
    let changed = false;
    // Accepted, context-confirmed offscreen content can be reread canonically.
    // Uncommitted drafts/results and current interactions keep their live bytes.
    for (const [key, draft] of this.drafts) if (draft.committedEntryId && !visible.has(draft.committedEntryId) && this.hasCanonicalContext(draft.committedEntryId)) {
      this.drafts.delete(key); changed = true;
    }
    for (const [key, result] of this.liveResults) if (result.committedEntryId && this.hasCanonicalContext(result.committedEntryId)
      && ![result.committedEntryId, result.assistantEntryId, result.proposedEntryId].some(id => id && visible.has(id))) {
      this.liveResults.delete(key); changed = true;
    }
    for (const entry of this.entries.values()) if (entry.input_source?.command_id && !visible.has(entry.entry_id) && this.hasCanonicalContext(entry.entry_id)) {
      changed = this.promptTransitions.delete(entry.input_source.command_id) || changed;
    }
    return changed;
  }
  protected transientHistoryEntries(): string[] {
    return [...this.drafts.values()].flatMap(value => value.committedEntryId ? [value.committedEntryId] : [])
      .concat([...this.liveResults.values()].flatMap(value => [value.committedEntryId, value.proposedEntryId, value.assistantEntryId].filter((id): id is string => Boolean(id))));
  }
  private promptTransitions = new Map<string, LocalPromptSubmission>();
  private liveControl: ProtocolLiveControlSnapshot = {};
  private liveOwnerEpoch = 0;
  private liveRevision = 0;
  private liveControlOwnerEpoch = 0;
  private liveControlRevision = 0;
  private presentationNotices: string[] = [];
  private providerRetry: ProviderRetry | null = null;
  private closed = false;
  private presentationWatermark = '0';
  private presentationEpoch = new AbortController();
  private presentationListeners = new Set<(request: PresentationRequest) => void>();
  private authorizationListeners = new Set<(request: McpAuthorizationRequest) => void>();
  subscribeMcpAuthorization(listener: (request: McpAuthorizationRequest) => void) {
    this.authorizationListeners.add(listener);
    return () => { this.authorizationListeners.delete(listener); };
  }
  subscribePresentation(listener: (request: PresentationRequest) => void) {
    this.presentationListeners.add(listener);
    return () => { this.presentationListeners.delete(listener); };
  }
  private observeToolCompletion(entry: ProtocolEntry, sequence: string) {
    if (compareSequence(sequence, this.presentationWatermark) <= 0) return;
    this.presentationWatermark = sequence;
    const result = entry.tool_result;
    if (this.role !== 'controller' || entry.entry_kind !== 'TOOL_RESULT' || entry.scope_kind !== 'ROOT'
      || entry.entry_owner_kind === 'IMPORTED_HISTORY' || result?.result_state !== 'SUCCESS'
      || result.source_coverage !== 'COMPLETE' || result.display_kind !== 'COMPLETE'
      || !result.assistant_entry_id || !result.tool_call_id) return;
    const signal = this.presentationEpoch.signal;
    const read = async (toolName: string) => {
      let assistant = this.entries.get(result.assistant_entry_id!);
      if (!assistant) {
        const window = await this.historyPost<BrowserWindow>('window', { anchor_entry_id: result.assistant_entry_id, maximum_entries: 1, maximum_serialized_bytes: 2 << 20 }, signal);
        if (window.session_id !== this.sessionId) return;
        assistant = window.entries.find(item => item.entry_id === result.assistant_entry_id);
      }
      if (signal.aborted || this.closed || assistant?.scope_kind !== 'ROOT' || assistant.turn_id !== entry.turn_id
        || assistant.entry_owner_kind === 'IMPORTED_HISTORY'
        || !assistant.blocks?.some(block => block.block_kind === 'TOOL_CALL' && block.tool_call_id === result.tool_call_id && block.tool_name === toolName)) return;
      await this.hydrateContentReference(entry.content, { entry_id: entry.entry_id }, '展示请求暂时无法读取。', signal);
      if (signal.aborted || this.closed || entry.content?.inline_content === undefined) return;
      return decodeBase64(entry.content.inline_content);
    };
    for (const listener of this.presentationListeners) listener({ sequence, signal, read: async () => {
      const text = await read('open_in_pulsara');
      return text === undefined ? undefined : parsePresentationResult(text);
    } });
    for (const listener of this.authorizationListeners) listener({ signal, read: async () => {
      const text = await read('manage_capability');
      return text === undefined ? undefined : parseMcpAuthorizationResult(text);
    } });
  }

  chatWriteHeaders(): Record<string, string> {
    if (this.closed) throw new RuntimeApiError('CHAT_CONNECTION_CLOSED', '会话连接已关闭。', false);
    return {'X-Pulsara-Connection-Id': this.connectionId, 'X-Pulsara-Connection-Generation': String(this.generation)};
  }

  constructor(payload: ConnectPayload, private readonly scopeKind: 'ROOT' | 'SUBAGENT' = 'ROOT',
              private readonly historyRoot?: string, private readonly queueRoot?: string) {
    super();
    this.availability = payload.workspace_availability;
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
  protected get historyPath() { return this.historyRoot ?? super.historyPath; }
  protected get queuePath() { return this.queueRoot ?? super.queuePath; }

  readonly filePreview: FilePreviewApi = {
    open: (path, basePreview, readingRoot) => this.previewRequest('', { path, base_preview: basePreview, reading_root: readingRoot }),
    close: async token => { await this.previewRequest('', { read_token: token }, 'DELETE'); },
    page: (token, cursor, mode, signal) => this.previewRequest('/page', { read_token: token, cursor, mode }, 'POST', signal),
    action: async (token, action) => { const target = await this.previewRequest<{path:string;reveal:boolean}>('/action', { read_token: token, action }); const host=desktopHost();if(!host)throw new Error('桌面操作当前不可用。');await host.openPath(target); },
  };

  private previewRequest<T>(suffix: string, body: object, method = 'POST', signal?: AbortSignal): Promise<T> {
    if (this.closed) return Promise.reject(new RuntimeApiError('PREVIEW_EXPIRED', '会话连接已关闭。', false));
    return apiRequest<T>(`/api/connections/${encodeURIComponent(this.connectionId)}/file-preview${suffix}`, {
      method, body: JSON.stringify(body), signal,
    });
  }

  async initialize(): Promise<void> { void this.hydrateQueuedPromptContent(); }

  async importFiles(files: readonly File[], signal: AbortSignal): Promise<import('./file-reference').ImportedPath> {
    if (this.closed || this.role !== 'controller') throw new RuntimeApiError('IMPORT_CONTROLLER_REQUIRED', '当前页面没有文件导入控制权。', true);
    if (!files.length) throw new Error('没有可导入的文件。');
    const headers: Record<string, string> = { 'X-Pulsara-Connection-Generation': String(this.generation) };
    if (files.length !== 1) throw new Error('单文件导入只接受一个文件。');
    headers['Content-Type'] = 'application/octet-stream';
    headers['X-Pulsara-Filename'] = JSON.stringify(files[0].name).replace(/[^\x20-\x7e]/g,
      character => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
    return httpRequest(`/api/connections/${encodeURIComponent(this.connectionId)}/import-file`,
      { method: 'POST', body: files[0], headers, signal });
  }

  current(): RuntimeProjection {
    return this.project();
  }

  private observationOwner: Promise<unknown> = Promise.resolve();
  private observationInstalled = false;
  private ordered<T>(operation: () => Promise<T>): Promise<T> {
    const next = this.observationOwner.then(operation);
    this.observationOwner = next.catch(() => undefined);
    return next;
  }
  snapshot(): Promise<RuntimeProjection> {
    return this.ordered(async () => {
      const frame = this.historyRoot
        ? await this.historyPost<{snapshot?: {snapshot?: ProtocolCanonicalSnapshot}; error?: ProtocolError}>('snapshot', {})
        : await apiRequest<{snapshot?: {snapshot?: ProtocolCanonicalSnapshot}; error?: ProtocolError}>(`/api/sessions/${encodeURIComponent(this.sessionId)}/history-snapshot`);
      assertProtocolFrame(frame);
      const snapshot = frame.snapshot?.snapshot;
      if (!snapshot) throw new RuntimeApiError('PROTOCOL_RESPONSE_INVALID', '本地服务返回的数据不完整。', true);
      this.replaceSnapshot(snapshot);
      void this.hydrateQueuedPromptContent();
      return this.project();
    });
  }
  observe(signal?: AbortSignal): Promise<RuntimeProjection> {
    return this.ordered(() => this.observeNext(signal));
  }
  private async observeNext(signal?: AbortSignal): Promise<RuntimeProjection> {
    const frame = await this.post<{ observation?: ObservationPayload; error?: ProtocolError }>('observe', {
      after_event_sequence: this.eventSequenceCursor,
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
    this.providerRetry = observation.provider_retry ? { ...observation.provider_retry, observedAt: performance.now() } : null;
    const liveRingGap = observation.gap?.kind === 'LIVE_GAP'
      && observation.gap.reason === 'LIVE_RING_OVERFLOW';
    if (observation.gap?.kind && !liveRingGap) {
      throw new RuntimeGapError(observation.gap.kind, '连接状态需要刷新。');
    }
    this.presentationNotices = [...(observation.presentation_notices ?? [])];
    if (liveRingGap) {
      // A fresh advisory cut cannot reconstruct the missing process.
      // Preserve canonical entries, hydrated content and the controller owner.
      this.drafts.clear();
      this.liveResults.clear();
      this.presentationNotices.push('部分临时进展已丢失，已从当前进展继续；已保存的会话记录不受影响。');
    }
    this.liveControl.root_compaction_in_progress = observation.root_compaction_in_progress === true;
    this.liveControl.manual_compaction_command_id = observation.manual_compaction_command_id || '';
    this.liveControl.active_root_turn_id = observation.active_root_turn_id || '';
    this.rememberQueueContent(this.control);
    for (const committed of observation.committed ?? []) {
      if (committed.interruption_notice) this.mergeInterruptionNotices([committed.interruption_notice]);
      if (committed.projection_kind === 'IMMUTABLE_ENTRY' && committed.entry) {
        this.mergeEntries([committed.entry], true);
      }
      if (committed.current_control && compareSequence(committed.control_read_event_sequence ?? observation.through_event_sequence ?? 0, this.controlReadSequence) >= 0) {
        this.control = committed.current_control;
        this.controlReadSequence = decimal(committed.control_read_event_sequence ?? observation.through_event_sequence ?? 0);
      }
    }
    for (const committed of observation.committed ?? []) {
      if (committed.projection_kind === 'IMMUTABLE_ENTRY' && committed.entry && committed.event_sequence !== undefined)
        this.observeToolCompletion(committed.entry, decimal(committed.event_sequence));
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
    this.eventSequenceCursor = decimal(observation.through_event_sequence ?? this.eventSequenceCursor);
    this.eventSequence = numeric(this.eventSequenceCursor);
    const appended = (observation.committed ?? []).flatMap(item => item.projection_kind === 'IMMUTABLE_ENTRY' && item.entry ? [item.entry] : []);
    if (appended.length) this.installPage(appended, undefined, undefined, undefined, true);
    const boundaryOccurrences = (observation.committed ?? []).filter(item => ['COMPACTION_ADOPTED', 'TURN_INTERRUPTED'].includes(item.event_type ?? '') && item.event_id);
    for (const occurrence of boundaryOccurrences) if (compareSequence(occurrence.event_sequence ?? this.eventSequenceCursor, this.presentationBoundaryEventSequence) > 0) this.presentationBoundaryEventSequence = decimal(occurrence.event_sequence ?? this.eventSequenceCursor);
    this.refreshBoundaryOccurrences(boundaryOccurrences.map(item => item.event_id!));
    this.liveOwnerEpoch = numeric(observation.live_owner_epoch ?? this.liveOwnerEpoch);
    this.liveRevision = numeric(observation.through_live_revision ?? this.liveRevision);
    this.liveControlOwnerEpoch = numeric(observation.live_control_owner_epoch ?? this.liveControlOwnerEpoch);
    this.liveControlRevision = numeric(observation.through_live_control_revision ?? this.liveControlRevision);
    const todosInvalidated = (observation.live ?? []).some(
      event => event.event_type === 'TODO_SNAPSHOT_UPDATED',
    );
    if (liveRingGap || todosInvalidated) {
      // TODO live events invalidate the owner snapshot; their separate stream
      // can lag a previously read baseline, including across run changes.
      const controlFrame = await this.post<{
        live_control_snapshot?: { snapshot?: ProtocolLiveControlSnapshot };
        error?: ProtocolError;
      }>('live-control-snapshot', {}, signal);
      assertProtocolFrame(controlFrame);
      const baseline = controlFrame.live_control_snapshot?.snapshot;
      if (!baseline) throw new RuntimeApiError('PROTOCOL_RESPONSE_INVALID', '当前运行状态暂时无法读取。', true);
      if (liveRingGap) {
        this.liveControl = baseline;
        this.liveControlOwnerEpoch = numeric(baseline.owner_epoch);
        this.liveControlRevision = numeric(baseline.live_revision);
      } else {
        // Keep interaction state and its cursor together: the ordinary control
        // suffix still owns those transitions after this TODO-only refresh.
        this.liveControl.current_todos = baseline.current_todos ?? [];
      }
    }
    void this.hydrateQueuedPromptContent();
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
      control_admission_deadline_ms: reference.admissionDeadlineMs,
      target_turn_id: reference.targetId,
    }, reference.commandId);
  }

  stopContextCompaction(reference: UserControlCommandRef): Promise<CommandReceipt> {
    this.assertControlReference(reference, 'STOP_CONTEXT_COMPACTION', 'MANUAL_COMPACTION');
    return this.controlCommand(reference, 'target_compaction_command_id');
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
        admission_deadline_ms: reference.admissionDeadlineMs,
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

  async readPromptForEdit(
    content: CanonicalPromptContent,
  ): Promise<EditablePromptContent> {
    const parts: Array<EditablePromptContent['parts'][number]> = [];
    for (const part of content.parts) {
      parts.push(part.type === 'annotation' ? { ...part, source: part.source ? { ...part.source } : null } : part.type === 'text'
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

  compactContext(): Promise<CommandReceipt> {
    return this.command('COMPACT_CONTEXT');
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
    suppliedCommandId?: string,
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
    const commandId = suppliedCommandId ?? randomIdentity('command');
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
          ...(resolution.revisionPrompt
            ? { revision_prompt: editablePromptToTransport(resolution.revisionPrompt) } : {}),
          ...(resolution.sourceQueueItemId ? { source_queue_item_id: resolution.sourceQueueItemId } : {}),
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

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.presentationEpoch.abort();
    this.presentationListeners.clear();
    this.authorizationListeners.clear();
    this.disposeReader();
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
    const commandId = suppliedCommandId ?? randomIdentity('command');
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
    targetField: 'target_process_id' | 'target_compaction_command_id',
  ): Promise<CommandReceipt> {
    return this.command(reference.operation, {
      client_submission_id: '',
      expected_session_id: reference.sessionId,
      expected_host_session_id: reference.hostSessionId,
      control_admission_deadline_ms: reference.admissionDeadlineMs,
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
      || !isCanonicalIdentity(reference.commandId, 'command')
    ) {
      throw new RuntimeApiError(
        'CONTROL_REFERENCE_INVALID',
        '控制操作的 owner 或目标身份不一致。',
        false,
      );
    }
  }

  protected async post<T>(
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
    if (compareSequence(snapshot.event_sequence_cut ?? 0, this.eventSequenceCursor) < 0) return;
    this.presentationEpoch.abort();
    this.presentationEpoch = new AbortController();
    this.presentationWatermark = decimal(snapshot.event_sequence_cut ?? 0);
    this.rememberQueueContent(this.control);
    this.installTail(snapshot);
    if (compareSequence(snapshot.event_sequence_cut ?? 0, this.controlReadSequence) >= 0) {
      this.control = snapshot.control ?? {};
      this.controlReadSequence = decimal(snapshot.event_sequence_cut ?? 0);
    }
    if (!this.observationInstalled) {
      this.eventSequenceCursor = decimal(snapshot.event_sequence_cut ?? 0);
      this.eventSequence = numeric(snapshot.event_sequence_cut);
      this.observationInstalled = true;
    }
    this.writerGeneration = numeric(snapshot.writer_generation);
  }

  private queueRead?: Promise<void>;
  private async hydrateQueuedPromptContent(): Promise<void> {
    if (this.queueRead) return this.queueRead;
    const operation = (async () => {
      for (const item of this.control.prompt_queue ?? []) {
        if (['consumed', 'cancelled', 'rejected'].includes(this.promptTransitions.get(item.command_id ?? '')?.status ?? '')) continue;
        try { await this.hydrateContentReference(item.content, {queue_item_id: item.queue_item_id!}, '等待处理的输入暂时无法完整读取。'); }
        catch (error) {
          if (error instanceof RuntimeApiError && error.code === 'CONTENT_QUEUE_NOT_PENDING') {
            try { await this.reconcilePromptQueueTransition(item, true); } catch { /* Exact status remains unknown; never resubmit. */ }
          }
        }
      }
      this.rememberQueueContent(this.control);
      this.changed();
    })();
    this.queueRead = operation;
    try { await operation; } finally { if (this.queueRead === operation) this.queueRead = undefined; }
  }

  private async reconcilePromptQueueTransition(
    item: NonNullable<ProtocolCanonicalControl['prompt_queue']>[number],
    retiredContent = false,
  ): Promise<void> {
    const commandId = item.command_id ?? '';
    const queueItemId = item.queue_item_id ?? '';
    if (!queueItemId) {
      throw new RuntimeApiError(
        'PROMPT_TRANSITION_IDENTITY_INVALID',
        '等待处理的输入缺少可核对的身份。',
        true,
      );
    }
    if (!commandId) {
      await this.reconcileReadOnlyQueueItem(queueItemId);
      await this.snapshot();
      return;
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
    if (consumed || (pending && !retiredContent)) {
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
      receipt.promptDelivery.execution,
    ));
    if (status === 'consumed' && receipt.promptDelivery.consumedEntryId) {
      void this.locateEntry(receipt.promptDelivery.consumedEntryId, new AbortController().signal).then(() => this.changed()).catch(() => undefined);
    }
  }

  private queueTransitionSubmission(
    item: NonNullable<ProtocolCanonicalControl['prompt_queue']>[number],
    status: LocalPromptSubmission['status'],
    outcomeCode?: string,
    detail?: string,
    consumedEntryId?: string,
    execution?: RequestExecution | null,
  ): LocalPromptSubmission {
    const hasInlineBody = item.content?.inline_content !== undefined;
    const queueItemId = item.queue_item_id!;
    return {
      sessionId: this.sessionId,
      connectionGeneration: this.generation,
      commandId: item.command_id!,
      queueItemId,
      consumedEntryId,
      execution,
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

  private applyLive(events: ProtocolLiveEvent[], settlements: ProtocolSettlement[]) {
    for (const event of events) {
      const payload = event.payload ?? {};
      if (event.event_type === 'INTERACTION_CLOSED') {
        const closed = payload.interaction_closed ?? {};
        if (closed.interaction_id === this.liveControl.current_interaction?.interaction_id) {
          const reasons: Record<string, string> = {
            CANCELLED: '已取消这次配置。',
            'interaction:user-steer': '已取消这项确认，继续处理你的新指示。',
            'interaction:expired': '确认已过期，本次操作未获授权。',
            'interaction:turn-cancelled': '原任务已停止，这项待确认操作已结束。',
            'interaction:host-closing': '运行环境已关闭，原确认已结束。',
            'interaction:mcp-config-changed': '操作条件已变化，这项确认已失效。',
            'interaction:admission-rejected': '操作条件已变化，这项确认已失效。',
            'interaction:outcome-unknown': '运行已中断，操作结果待核实。',
          };
          if (!['RESOLVED', 'SUBMITTED'].includes(String(closed.reason))) this.presentationNotices.push(
            reasons[String(closed.reason)] ?? '这项确认已结束；原因暂不可确认。',
          );
        }
        continue;
      }
      if (event.event_type === 'TODO_SNAPSHOT_UPDATED') {
        // The current owner snapshot, not retained live payloads, owns TODOs.
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
        body: '',
        reasoning: [],
        traces: [],
      };
      if (event.scope_kind) current.scopeKind = event.scope_kind;
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
          trace.title = toolDisplayName(toolName, argumentsJson);
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
          if (!this.liveResultMatches(result, settlement)) continue;
          if (result.scopeKind === this.scopeKind && settlement.kind === 'LIVE_GENERATION_COMMITTED' && settlement.committed_entry_id) {
            result.committedEntryId = settlement.committed_entry_id;
          } else this.liveResults.delete(key);
        }
      } else if (identity) {
        const draft = this.drafts.get(identity);
        if (draft?.scopeKind === this.scopeKind && settlement.kind === 'LIVE_GENERATION_COMMITTED' && settlement.committed_entry_id) {
          // Keep already displayed bytes through the asynchronous history read.
          // The exact SDK settlement supplies the binding; this is no live run.
          draft.committedEntryId = settlement.committed_entry_id;
        } else this.drafts.delete(identity);
      }
    }
  }

  private liveResultStableKey(event: ProtocolLiveEvent): string | undefined {
    const exactChannel = event.channel_attempt_id || event.proposed_entry_id;
    const fallbackParts = [
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
      sourceExecAttemptId: String(item.source_exec_attempt_id ?? '') || undefined,
      turnId: augmented.turn_id ?? '',
      scopeKind: augmented.scope_kind ?? '',
      draftIdentity: augmented.draft_identity,
      generationId: augmented.generation_id,
      blockId: augmented.block_id,
      assistantEntryId: String(item.assistant_entry_id ?? '') || attempt?.assistant_entry_id || proposed?.tool_result?.assistant_entry_id,
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
    current.sourceExecAttemptId ||= String(item.source_exec_attempt_id ?? '') || proposed?.tool_result?.source_exec_attempt_id || undefined;
    current.assistantEntryId ||= String(item.assistant_entry_id ?? '') || attempt?.assistant_entry_id || proposed?.tool_result?.assistant_entry_id;
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

  protected project(): RuntimeProjection {
    this.reconcileLiveToolResults();
    const canonical = [...this.entries.values()].sort(
      (a, b) => numeric(a.entry_sequence) - numeric(b.entry_sequence),
    );
    const activeTurnIds = new Set(
      (this.control.active_turns ?? []).map((turn) => turn.turn_id),
    );
    const visibleDrafts = [...this.drafts.values()].filter((draft) => {
      if (draft.committedEntryId) return false;
      return Boolean(draft.turnId) && activeTurnIds.has(draft.turnId);
    });
    const sameWindow = this.windowJoinPredicate();
    const messages = projectEntries(
      canonical,
      activeTurnIds, sameWindow,
    );
    const canonicalTurnIds = orderedTurnIdsFromEntries(canonical, this.scopeKind);
    for (const draft of visibleDrafts) {
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
    this.decorateHistory(messages);
    for (const [identity, draft] of this.drafts) {
      if (!draft.committedEntryId) continue;
      const message = messages.find(item => item.id === draft.committedEntryId || item.sourceEntryIds?.includes(draft.committedEntryId!));
      if (!message) continue;
      if (message.turnId !== draft.turnId) { this.drafts.delete(identity); continue; }
      if (message.historyContextState === 'ready' && message.contentState === 'ready'
        && (!message.reasoningContentState || message.reasoningContentState === 'ready')) {
        this.drafts.delete(identity);
      } else {
        message.pendingAssistant = {
          body: message.contentState === 'ready' ? message.body : draft.body,
          reasoning: message.reasoningContentState === 'ready' ? message.reasoning : draft.reasoning,
          traces: message.traces?.length ? message.traces : draft.traces,
        };
      }
    }
    this.applyProjectedLiveResults(messages, activeTurnIds);
    const hasActiveDraft = visibleDrafts.length > 0;
    const manualCommandId = this.liveControl.manual_compaction_command_id;
    const activeTurn = manualCommandId ? undefined : this.control.active_turns?.[0];
    const todo = projectTodo(this.liveControl, this.scopeKind);
    const interaction = projectInteraction(this.liveControl, this.control);
    const readingMessages = this.readingMessages(messages);
    return {
      messages: readingMessages,
      canonicalInputs: canonicalInputs(messages),
      history: this.presentationHistory(readingMessages),
      interruptionNotices: this.projectInterruptionNotices(readingMessages),
      canonicalTurnIds,
      presentationNotices: this.presentationNotices,
      providerRetry: this.providerRetry,
      contextCompaction: projectContextCompaction(this.control),
      initialContextBase: this.control.initial_context_base,
      isRunning: !manualCommandId && (Boolean(this.liveControl.active_root_turn_id) || Boolean(activeTurn) || hasActiveDraft),
      pendingSteerPage: projectPendingSteerPage(this.control),
      queuedCount: numeric(this.control.prompt_queue_total_count),
      queuedPrompts: projectQueuedPrompts(this.control).filter(item => !(item.sourceKind === 'command' && ['consumed', 'cancelled', 'rejected'].includes(this.promptTransitions.get(item.commandId)?.status ?? '')) && !canonical.some(entry => entry.input_source?.queue_item_id === item.queueItemId)),
      promptTransitions: this.projectPromptTransitions(),
      planMode: Boolean(
        this.control.active_plan_workflow
        && Object.keys(this.control.active_plan_workflow).length,
      ),
      activeTurnId: this.liveControl.active_root_turn_id || activeTurn?.turn_id,
      manualCompactionCommandId: manualCommandId || undefined,
      hostSessionId: this.liveControl.host_session_id || undefined,
      controlAdmissionDeadlineMs: numeric(
        this.liveControl.control_admission_deadline_ms,
      ) || undefined,
      control: this.control,
      liveControl: this.liveControl,
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
      const transition = this.promptTransitions.get(commandId)!;
      const ready = [...this.entries.values()].some(entry => entry.input_source?.command_id === commandId && this.entryContentState(entry.entry_id) === 'ready');
      if ((consumedCommands.has(commandId) && ready) || (pendingCommands.has(commandId) && !['consumed', 'cancelled', 'rejected'].includes(transition.status))) this.promptTransitions.delete(commandId);
    }
    return [...this.promptTransitions.values()];
  }

  private applyProjectedLiveResults(messages: Message[], activeTurnIds: Set<string>) {
    const traceTargets = new Map<string, ToolTrace>();
    for (const message of messages) {
      for (const trace of message.traces ?? []) traceTargets.set(`${message.id}:${trace.id}`, trace);
    }
    for (const [key, result] of this.liveResults) {
      if (result.sourceExecAttemptId || (result.committedEntryId && this.entries.get(result.committedEntryId)?.tool_result?.source_kind === 'CODE_MODE_CALL')) {
        if (result.committedEntryId && this.entryContentState(result.committedEntryId) === 'ready') this.liveResults.delete(key);
        continue;
      }
      const trace = result.assistantEntryId && result.toolCallId
        ? traceTargets.get(`${result.assistantEntryId}:${result.toolCallId}`)
        : undefined;
      if (result.committedEntryId) {
        if (this.entryContentState(result.committedEntryId) === 'ready' && this.entries.has(result.committedEntryId)) {
          this.liveResults.delete(key);
        } else if (result.hasText && trace?.resultEntryId === result.committedEntryId) {
          const owner = messages.find(message => message.id === result.assistantEntryId && message.turnId === result.turnId);
          if (owner) owner.pendingAssistant = {
            body: owner.pendingAssistant?.body ?? owner.body,
            reasoning: owner.pendingAssistant?.reasoning ?? owner.reasoning,
            traces: (owner.pendingAssistant?.traces ?? owner.traces)?.map(item => item.id === trace.id
              ? { ...item, resultText: result.text } : item),
          };
        }
        // A settled preview must not become detached activity or live authority.
        continue;
      }
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
      {
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
  return httpRequest<T>(path, { ...init, headers: init.body
    ? { 'Content-Type': 'application/json', ...init.headers } : init.headers });
}

async function httpRequest<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
    });
  } catch (error) {
    init.signal?.throwIfAborted();
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
        skillPath: String(item.skill_path ?? ''),
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
        skillPath: String(item.skill_path ?? ''),
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
        hookCount: numeric(item.hook_count),
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

export function projectRootStatus(root: {status?: string} | null | undefined): import('./pulsara-types').SessionStatus | null {
  if (!root) return null;
  if (root.status === 'RUNNING') return 'running';
  if (root.status === 'COMPLETED') return 'completed';
  if (root.status === 'INTERRUPTED') return 'interrupted';
  throw new RuntimeApiError('ROOT_STATUS_INVALID', '会话状态暂时无法读取。', false);
}

function projectSessionSummary(value: Record<string, unknown>): SessionSummary {
  const lifecycle = String(value.lifecycle ?? 'OPEN');
  const rawWorkspace = value.workspace as Record<string, unknown> | undefined;
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
    lifecycle: lifecycle === 'ARCHIVED' ? 'ARCHIVED' : 'OPEN',
    canArchive: value.can_archive === true,
    subtitle: String(value.subtitle ?? lifecycle),
    status: projectRootStatus(value.latest_root_turn as {status?: string} | null),
    updatedAt: formatRelativeTime(String(value.updated_at ?? '')),
    live: Boolean(value.live),
    subagentCount: typeof value.subagent_count === 'number' ? value.subagent_count : undefined,
    modelCallBinding: projectModelCallBinding(value.model_call_binding),
    workspace,
  };
}

function projectEntries(
  entries: ProtocolEntry[],
  activeTurnIds: ReadonlySet<string>,
  sameWindow: (a: string, b: string) => boolean = () => true,
): Message[] {
  const messages: Message[] = [];
  for (const entry of entries) {
    if (
      entry.entry_kind === 'USER_MESSAGE'
      || entry.entry_kind === 'USER_STEER'
      || entry.entry_kind === 'PLAN_CONTINUATION'
      || entry.entry_kind === 'TERMINAL_OBSERVATION'
    ) {
      const continuationBody = entry.entry_kind === 'PLAN_CONTINUATION'
        ? projectPlanContinuation(decodeContent(entry.content)) : undefined;
      if (entry.entry_kind === 'PLAN_CONTINUATION' && continuationBody === undefined) continue;
      const userKind: NonNullable<Message['userKind']> = entry.input_source?.delivery_mode === 'STEER_ACTIVE_TURN' || entry.entry_kind === 'USER_STEER'
        ? 'steer'
        : entry.entry_kind === 'PLAN_CONTINUATION'
          ? 'plan-continuation'
          : entry.scheduled_input ? 'scheduled' : 'prompt';
      const promptContent = entry.entry_kind === 'PLAN_CONTINUATION'
        ? undefined
        : entry.content?.inline_content !== undefined ? decodePromptContent(entry.content, { kind: 'entry', entryId: entry.entry_id }) : undefined;
      messages.push({
        id: entry.entry_id,
        turnId: entry.turn_id,
        entrySequence: numeric(entry.entry_sequence),
        role: 'user',
        userKind,
        scheduledInput: projectScheduledInput(entry.scheduled_input),
        cellOutput: entry.entry_kind === 'TERMINAL_OBSERVATION' && entry.content?.inline_content !== undefined ? decodeCellOutput(entry.content).source : undefined,
        time: formatTime(entry.accepted_at_utc),
        body: entry.entry_kind === 'PLAN_CONTINUATION'
          ? continuationBody ?? ''
          : promptContent ? promptContentTextProjection(promptContent) : '',
        promptContent,
        inputOrigin: entry.input_origin || undefined,
        inputSource: projectInputSource(entry.input_source, entry.input_origin),
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
        rootFinal: entry.root_final === true,
        entryOwnerKind: entry.entry_owner_kind,
        time: formatTime(entry.accepted_at_utc),
        body: text || (entry.entry_kind === 'ASSISTANT_MESSAGE' ? decodeContent(entry.content) : ''),
        reasoning: reasoning.length ? reasoning : undefined,
        status: 'completed',
        traces: traces.length ? traces : undefined,
        visualizations: projectVisualizations(entry.visualizations),
      });
      continue;
    }
    if (entry.entry_kind === 'TOOL_RESULT') {
      if (entry.tool_result?.source_kind === 'CODE_MODE_CALL') continue;
      const resultRef = entry.tool_result;
      const projectedResult = decodeToolResultContent(entry);
      const target = resultRef
        ? messages.find((message) => message.id === resultRef.assistant_entry_id && sameWindow(message.id, entry.entry_id))
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
        pendingTrace.attemptId = resultRef?.attempt_id;
        target!.sourceEntryIds = [...(target!.sourceEntryIds ?? [target!.id]), entry.entry_id];
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

  }
  // Program calls retain their real entries and render inside their source
  // exec card. A page without that source never creates an orphan bubble.
  for (const entry of entries) {
    const source = entry.tool_result;
    if (entry.entry_kind !== 'TOOL_RESULT' || source?.source_kind !== 'CODE_MODE_CALL') continue;
    const owner = messages.find(message => message.traces?.some(trace => trace.attemptId === source.source_exec_attempt_id));
    const parent = owner?.traces?.find(trace => trace.attemptId === source.source_exec_attempt_id);
    if (!parent || !owner) continue;
    const result = decodeToolResultContent(entry);
    const child: ToolTrace = {
      id: source.invocation_id || entry.entry_id, attemptId: source.attempt_id,
      kind: 'artifact', toolName: source.tool_name, title: source.tool_name || '操作结果',
      argumentsJson: source.original_arguments_json, subtitle: '已记录',
      status: toolResultStatus(source.result_state), resultText: result.text,
      resultContent: result.content, resultEntryId: entry.entry_id, resultState: source.result_state,
      resultSummary: summarizeToolResult(source.tool_name, result.text), artifact: projectToolArtifact(source),
    };
    parent.programCalls = [...(parent.programCalls ?? []), child];
    owner.sourceEntryIds = [...(owner.sourceEntryIds ?? [owner.id]), entry.entry_id];
  }
  for (const message of messages) {
    if (message.turnId && activeTurnIds.has(message.turnId)) continue;
    for (const trace of message.traces ?? []) {
      settleHistoricalTrace(trace);
    }
  }
  return messages;
}

function settleHistoricalTrace(trace: ToolTrace): void {
  if (trace.status !== 'running') return;
  trace.status = 'cancelled';
  trace.subtitle = '已结束';
  trace.meta = '结果待核实';
  trace.resultSummary = '原运行已结束，操作结果尚未记录；需要核实实际状态。';
}

function summarizeToolResult(toolName: string | undefined, content: string): string | undefined {
  if (!content) return undefined;
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    const planControl = String(value.plan_control ?? '');
    if (planControl === 'EXITED_PLAN') return '已退出规划，继续处理任务。';
    if (planControl === 'QUESTION_ANSWERED') return '已记录你的选择。';
    if (planControl === 'DRAFT_SUBMITTED_FOR_REVIEW') return '方案已提交，等待你的确认。';
    if (toolName === 'edit_file' && typeof value.diff === 'string' && value.diff) return value.diff;
    const fileTarget = value.target as { kind?: string; path?: unknown } | undefined;
    const path = fileTarget?.kind === 'PATH' && typeof fileTarget.path === 'string' ? fileTarget.path : '';
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
    if (toolName === 'exec_process' || toolName === 'manage_process') {
      const nestedProcess = asRecord(value.process);
      const process = Object.keys(nestedProcess).length ? nestedProcess : value;
      const status = String(process.status ?? '').toLowerCase();
      const exitCode = typeof process.exit_code === 'number' ? process.exit_code : undefined;
      if (status === 'running') {
        return toolName === 'exec_process' ? '命令仍在后台运行。' : '命令仍在运行。';
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

type InputSourceIdentity =
  | { sourceKind: 'command'; commandId: string }
  | { sourceKind: 'agent'; sourceSessionId?: string; sourceWorkId?: string }
  | { sourceKind: 'cell'; sourceSessionId?: string; sourceTurnId?: string; sourceExecAttemptId?: string }
  | { sourceKind: 'system' };

function inputSourceIdentity(
  item: { command_id?: string; source_session_id?: string; source_work_id?: string; source_turn_id?: string; source_tool_attempt_id?: string },
  origin?: string,
): InputSourceIdentity {
  if (origin === 'CELL_OUTPUT') return { sourceKind: 'cell', sourceSessionId: item.source_session_id, sourceTurnId: item.source_turn_id, sourceExecAttemptId: item.source_tool_attempt_id };
  if (origin === 'SCHEDULED_TASK') return { sourceKind: 'system' };
  if (item.command_id) return {sourceKind: 'command', commandId: item.command_id};
  if (origin?.startsWith('AGENT_')) {
    return {sourceKind: 'agent', sourceSessionId: item.source_session_id || undefined,
      sourceWorkId: item.source_work_id || undefined};
  }
  return {sourceKind: 'system'};
}

function projectInputSource(
  item: ProtocolEntry['input_source'], origin?: string,
): Message['inputSource'] {
  if (!item) return undefined;
  const identity = inputSourceIdentity(item, origin);
  const mode = deliveryMode(item.delivery_mode);
  if (item.queue_item_id) return {identityKind:'queue', queueItemId:item.queue_item_id, deliveryMode:mode, ...identity};
  if (identity.sourceKind === 'command') return {identityKind:'revision', deliveryMode:mode, ...identity};
  if (identity.sourceKind === 'agent' || identity.sourceKind === 'cell') return {identityKind:'origin', deliveryMode:mode, ...identity};
  return undefined;
}

function projectPendingSteerPage(control: ProtocolCanonicalControl): RuntimeProjection['pendingSteerPage'] {
  return control.pending_steer_target_turn_id ? { targetTurnId: control.pending_steer_target_turn_id, totalCount: numeric(control.pending_steer_total_count), nextCursor: control.pending_steer_next_cursor === undefined ? null : decimal(control.pending_steer_next_cursor) } : undefined;
}

function projectQueuedPrompts(control: ProtocolCanonicalControl): QueuedPrompt[] {
  return [...(control.prompt_queue ?? [])]
    .filter((item) => item.queue_item_id && item.status === 'PENDING')
    .sort((left, right) => compareSequence(left.queue_sequence, right.queue_sequence))
    .map((item) => {
      const queueItemId = item.queue_item_id!;
      return {
        queueItemId,
        ...inputSourceIdentity(item, item.input_origin),
        sequence: decimal(item.queue_sequence),
        status: 'pending' as const,
        inputOrigin: item.input_origin,
        scheduledInput: projectScheduledInput(item.scheduled_input),
        cellOutput: item.input_origin === 'CELL_OUTPUT' && item.content?.inline_content !== undefined ? decodeCellOutput(item.content).source : undefined,
        waitingOn: item.waiting_on?.length ? item.waiting_on : undefined,
        deliveryMode: deliveryMode(item.delivery_mode),
        targetTurnId: item.target_turn_id || undefined,
        content: item.content?.inline_content !== undefined ? decodePromptContent(item.content, { kind: 'queue', queueItemId }) : {parts: []},
        contentUnavailable: item.content?.inline_content === undefined,
        permission: protocolPermission(item.input_origin === 'SCHEDULED_TASK'
          ? item.permission?.effective_mode : item.requested_permission_mode),
        submittedAt: item.accepted_at_utc || undefined,
        requestedPermission: protocolPermission(item.input_origin === 'SCHEDULED_TASK'
          ? item.permission?.requested_mode : item.requested_permission_mode),
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

function projectPlanContinuation(content: string): string | undefined {
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    const transition = String(value.transition ?? value.handoff_kind ?? '').toUpperCase();
    if (transition === 'EXITED_PLAN') return undefined;
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
    title: toolDisplayName(name, argumentsPreview),
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
    .filter(block => Boolean(block.body) || (entry.reasoning_blocks ?? []).some(reference => reference.block_id === block.id && reference.content?.inline_content === undefined && numeric(reference.content?.size) > 0));
}

function reasoningKind(value: string): ReasoningBlock['kind'] {
  return value.includes('SUMMARY') ? 'summary' : 'full';
}

function toolDisplayName(name: string, argumentsJson?: string): string {
  const normalized = name.toLowerCase();
  if (normalized === 'reload_capabilities') return '刷新能力';
  if (normalized === 'query_capabilities') {
    try {
      return Object.hasOwn(JSON.parse(argumentsJson ?? '{}'), 'target') ? '查看能力详情' : '查看能力';
    } catch { return '查看能力'; }
  }
  if (normalized === 'use_new_mcp_tool') return '调用 MCP 工具';
  if (normalized.includes('wait_agent')) return '等待任务';
  if (normalized.includes('send_agent_message')) return '发送任务消息';
  if (normalized.includes('stop_agent')) return '停止任务';
  if (normalized === 'query_agents') return '查看任务';
  if (normalized === 'exec_cell') return '运行脚本';
  if (normalized === 'query_cells') return '查看脚本';
  if (normalized === 'manage_cell') return '管理脚本';
  if (normalized === 'query_processes') return '查看命令';
  if (normalized === 'manage_process') return '管理命令';
  if (normalized === 'query_schedules') return '查看定时计划';
  if (normalized === 'manage_schedule') return '管理定时计划';
  if (normalized === 'todo') return '更新 TODO';
  if (normalized.includes('ask_plan_question')) return '提出规划问题';
  if (normalized.includes('exit_plan')) {
    if (argumentsJson !== undefined) {
      try {
        const { plan } = JSON.parse(argumentsJson) as { plan?: unknown };
        return plan == null || (typeof plan === 'string' && !plan.trim()) ? '退出规划' : '提交规划方案';
      } catch { /* Arguments may still be streaming. */ }
    }
    return '结束规划';
  }
  if (normalized.includes('read_artifact')) return '读取保留内容';
  if (normalized.includes('write_file')) return '写入文件';
  if (normalized.includes('read_file')) return '读取文件';
  if (normalized.includes('apply_patch') || normalized.includes('edit')) return '更新文件';
  if (normalized.includes('search') || normalized.includes('grep')) return '搜索内容';
  if (normalized.includes('list')) return '浏览文件';
  if (normalized.includes('terminal') || normalized.includes('shell') || normalized.includes('exec')) return '运行命令';
  if (normalized.includes('browser')) return '操作浏览器';
  return '使用工具';
}

function toolArgumentSummary(name: string, content: string): string {
  try {
    const value = JSON.parse(content) as Record<string, unknown>;
    const normalized = name.toLowerCase();
    if (normalized === 'reload_capabilities') return '重新载入 Skill、MCP 与 Hook';
    if (normalized === 'query_capabilities' && !Object.hasOwn(value, 'target')) {
      return typeof value.kind === 'string' ? `查看 ${value.kind} 能力` : '读取能力及来源';
    }
    if (normalized === 'query_capabilities') {
      const target = value.target && typeof value.target === 'object' ? value.target as Record<string, unknown> : {};
      return [target.plugin_id, target.runtime_server_id ?? target.server_id, target.tool_name ?? target.name].filter((item) => typeof item === 'string').join(' · ') || '查看所选能力的详情';
    }
    if (normalized === 'use_new_mcp_tool') return '调用已经检查的 MCP 工具';

    if (normalized.includes('wait_agent')) return '等待指定任务出现新进展';
    if (normalized.includes('send_agent_message')) return '向任务发送补充信息';
    if (normalized.includes('stop_agent')) return '停止指定任务';
    if (normalized === 'query_agents') return '读取当前任务状态';
    if (normalized === 'exec_cell') return typeof value.path === 'string' ? value.path : '执行 JavaScript 脚本';
    if (normalized === 'query_cells') return Object.hasOwn(value, 'target') ? '读取脚本状态与源码' : '读取脚本列表';
    if (normalized === 'manage_cell') return value.action === 'stop' ? '停止脚本' : '等待脚本输出';
    if (normalized === 'todo') return '更新当前工作清单';
    if (name.toLowerCase().includes('ask_plan_question')) return String(value.question ?? '等待你的选择');
    if (name.toLowerCase().includes('exit_plan')) return value.plan == null || (typeof value.plan === 'string' && !value.plan.trim()) ? '继续处理任务' : String(value.summary ?? '等待你确认方案');
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
  if (normalized === 'exec_cell' || normalized === 'query_cells' || normalized === 'manage_cell') return undefined;
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

function projectTodo(control: ProtocolLiveControlSnapshot, scopeKind: 'ROOT' | 'SUBAGENT'): TodoRun | undefined {
  const run = control.current_todos?.find((item) => (
    item.scope_kind === scopeKind && item.disposition !== 'CLOSED'
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

function orderedTurnIdsFromEntries(entries: ProtocolEntry[], scopeKind: 'ROOT' | 'SUBAGENT'): string[] {
  const seen = new Set<string>();
  const ordered: string[] = [];
  for (const entry of entries) {
    if (entry.scope_kind !== scopeKind || !entry.turn_id || seen.has(entry.turn_id)) continue;
    seen.add(entry.turn_id);
    ordered.push(entry.turn_id);
  }
  return ordered;
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
  let requestExecution: RequestExecution | null = null;
  if (prompt) {
    if (prompt.delivery_mode === 'NEW_TURN') {
      if (!prompt.execution) throw new Error('NEW_TURN delivery requires execution');
      requestExecution = requestExecutionFromWire(prompt.execution);
    } else if (prompt.execution) {
      throw new Error('Steer delivery cannot have request execution');
    }
  }
  const control = asRecord(value.user_control);
  const process = asRecord(control.process);
  const feedback = asRecord(control.feedback);
  const hasControl = Object.keys(control).length > 0;
  const hasProcess = Object.keys(process).length > 0;
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
        execution: requestExecution,
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
  if (entry.content?.media_type !== PROMPT_BODY_MEDIA_TYPE || entry.content.inline_content === undefined) {
    return { text: decodeContent(entry.content) };
  }
  const content = decodePromptContent(entry.content, {
    kind: 'entry', entryId: entry.entry_id,
  });
  return { text: promptContentTextProjection(content), content };
}

function projectScheduledInput(value: ProtocolEntry['scheduled_input']): Message['scheduledInput'] {
  return value ? { taskId: value.task_id, taskRevision: numeric(value.task_revision), dueAtUtc: value.due_at_utc, triggerSource: value.trigger_source } : undefined;
}

function decodeCellOutput(content: ProtocolContent | undefined): { source: NonNullable<Message['cellOutput']>; text: string } {
  const value = JSON.parse(decodeContent(content)) as Record<string, unknown>;
  if (value.schema_version !== 'cell_output.v1' || typeof value.cell_id !== 'string' || typeof value.source_turn_id !== 'string'
    || typeof value.source_exec_attempt_id !== 'string' || typeof value.record_sequence !== 'number' || !value.record || typeof value.record !== 'object') {
    throw new RuntimeApiError('CONTENT_INTEGRITY_INVALID', '脚本输出格式无效。', true);
  }
  const record = value.record as Record<string, unknown>;
  const kind = String(record.kind ?? '');
  const text = typeof record.text === 'string' ? record.text
    : JSON.stringify(kind === 'error' ? record.error : record, null, 2);
  return { source: { cellId: value.cell_id, sourceTurnId: value.source_turn_id, sourceExecAttemptId: value.source_exec_attempt_id, recordSequence: value.record_sequence, kind }, text };
}

function decodePromptContent(
  content: ProtocolContent | undefined,
  owner: { kind: 'entry'; entryId: string } | { kind: 'queue'; queueItemId: string },
): CanonicalPromptContent {
  if (content?.media_type === 'application/vnd.pulsara.terminal-observation+json') return { parts: [{ type: 'text', text: decodeCellOutput(content).text }] };
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
  if (['exec_cell', 'query_cells', 'manage_cell'].includes(lower)) return 'artifact';
  if (['exec_process', 'query_processes', 'manage_process'].includes(lower)) return 'terminal';
  if (lower.includes('terminal') || lower.includes('exec')) return 'terminal';
  if (lower.includes('read')) return 'read';
  if (lower.includes('edit') || lower.includes('write')) return 'edit';
  if (lower.includes('search')) return 'search';
  if (lower.includes('mcp')) return 'mcp';
  return 'artifact';
}

function projectVisualizations(items: ProtocolEntry['visualizations']): VisualizationOccurrence[] {
  return (items ?? []).map(item => ({
    ordinal: numeric(item.ordinal), state: item.state === 'READY' ? 'READY' : 'FAILED',
    sourceFilename: item.source_filename || undefined, contentDigest: item.content_digest || undefined,
    contentSize: item.state === 'READY' ? numeric(item.content_size) : undefined,
    mediaType: item.media_type, codec: item.codec, imageRef: item.image_ref,
    width: item.width === undefined ? undefined : numeric(item.width), height: item.height === undefined ? undefined : numeric(item.height),
    failureCode: item.failure_code, failureDetail: item.failure_detail,
  }));
}

export interface SavedImageData { bytes: Uint8Array; mediaType: string; width: number; height: number }
interface ContentMedia { media_type?: string; codec?: string; image_ref?: string; width?: number; height?: number }
