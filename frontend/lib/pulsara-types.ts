import type { CanonicalPromptContent } from './prompt-content';

export type AppView =
  | 'overview'
  | 'workbench'
  | 'capabilities'
  | 'memory'
  | 'scheduled'
  | 'settings';

export type RuntimeStatus =
  | 'starting'
  | 'online'
  | 'reconnecting'
  | 'offline'
  | 'failed';

export type PermissionMode =
  | 'accept-edits'
  | 'read-only'
  | 'ask-permissions'
  | 'bypass-permissions';

export type SessionStatus = 'running' | 'completed' | 'interrupted';

export type TaskStatus =
  | 'pending'
  | 'running'
  | 'waiting'
  | 'completed'
  | 'cancelled'
  | 'failed'
  | 'interrupted'
  | 'blocked';

export interface Workspace {
  id: string;
  name: string;
  path: string;
  kind: 'quick' | 'project';
}

export type McpServerStatus =
  | 'disabled'
  | 'configured'
  | 'connecting'
  | 'discovering'
  | 'ready'
  | 'failed-retryable'
  | 'failed'
  | 'updating'
  | 'closed';

export interface McpToolCapability {
  name: string;
  remoteName: string;
  description: string;
  effect: 'read-only' | 'external-effect';
  availableToSubagents: boolean;
  parallelSafe: boolean;
}

export interface McpServerCapability {
  id: string;
  name: string;
  source: 'workspace' | 'user' | 'plugin' | 'host';
  editable: boolean;
  configIdentity?: string;
  config?: Record<string, unknown>;
  enabled: boolean;
  configuredEnabled: boolean;
  needsApproval: boolean;
  effective: boolean;
  status: McpServerStatus;
  required: boolean;
  availableToSubagents: boolean;
  toolCount: number;
  discoveredToolCount: number;
  resourceCount: number;
  resourceTemplateCount: number;
  promptCount: number;
  instructions: string;
  hasFailure: boolean;
  failureCategory?: string;
  transport?: { kind: 'stdio' | 'http'; summary: string; detail: string };
  tools: McpToolCapability[];
}

export interface SkillCapability {
  removalIdentity?: UserSkillCapability['removalIdentity'];
  id: string;
  name: string;
  description: string;
  location: string;
  path: string;
  source: 'workspace' | 'user' | 'plugin' | 'bundled';
  editable: boolean;
  enabled: boolean;
  effective: boolean;
  configured: boolean;
  authoringNotes: string[];
}

export interface SkillCatalogIssue {
  kind: 'invalid' | 'shadowed' | 'conflict';
  title: string;
  path?: string;
  details: string[];
}

export interface CapabilitySnapshot {
  sessionId: string;
  workspacePath: string;
  workspaceKind: 'quick' | 'project';
  credentialScopeKey?: string;
  adoption: {
    scope: 'workspace';
    pending: boolean;
    attention?: 'PROJECT_CAPABILITY_ADOPTION_FAILED' | 'PROJECT_MCP_ADOPTION_INCOMPLETE';
    when: 'next-provider-dispatch';
  };
  skills: {
    status: 'ready' | 'attention';
    configPath: string;
    items: SkillCapability[];
    issues: SkillCatalogIssue[];
    details: string[];
    roots: Array<{ path: string; scope: 'workspace' | 'user' }>;
  };
  mcp: {
    configPath: string;
    servers: McpServerCapability[];
    collisions: Array<{
      name: string;
      members: Array<{ serverId: string; toolName: string }>;
    }>;
  };
}

export interface SkillInstallResult {
  status: string;
  installed: boolean;
  message: string;
  sourcePath: string;
  destinationPath?: string;
  details: string[];
}

export interface UserSkillCapability {
  removalIdentity?: Record<'root_device' | 'root_inode' | 'directory_device' | 'directory_inode', string>;
  name: string;
  description: string;
  location: string;
  path: string;
  enabled: boolean;
  root: 'agents' | 'pulsara';
  authoringNotes: string[];
}

export interface UserMcpServerCapability {
  id: string;
  config: Record<string, unknown>;
  currentIdentity: string;
  name: string;
  enabled: boolean;
  status: McpServerStatus;
  required: boolean;
  availableToSubagents: boolean;
  toolCount: number;
  resourceCount: number;
  resourceTemplateCount: number;
  promptCount: number;
  instructions: string;
  hasFailure: boolean;
  failureCategory?: string;
  transport: { kind: 'stdio' | 'http'; summary: string };
  tools: McpToolCapability[];
}

export interface UserPluginCapability {
  id: string;
  name: string;
  description: string;
  version?: string;
  author?: string;
  enabled: boolean;
  packageInstallId: string;
  packageRoot: string;
  skillCount: number;
  mcpCount: number;
  hookCount: number;
  effectiveSkillNames: string[];
  effectiveMcpServerIds: string[];
  details: string[];
  mcpConnections?: PluginMcpConnection[];
  connectionReview?: { overlay: Record<string, unknown>; credentials: {name: string; present: boolean; sources: string[]}[] }[];
}

export interface McpCredentialOwner {
  kind: 'local' | 'plugin'; scope_key: string; server_id: string; plugin_id: string | null;
}

export interface PluginMcpConnection {
  serverId: string;
  defaults: Record<string, unknown>;
  config: Record<string, unknown>;
  overlay: Record<string, unknown> | null;
  credentialOwner: McpCredentialOwner;
  connectionInputs?: Array<{name: string; title: string; private: boolean; required: boolean; default: string | null}>;
}

export interface PluginMcpEditInput {
  overlay: Record<string, unknown> | null;
  secretChanges: McpEditInput['secretChanges'];
  retainCredentialsConfirmed?: boolean;
}

export interface UserCapabilitySnapshot {
  roots: Array<{ kind: 'agents' | 'pulsara'; path: string }>;
  skills: {
    status: 'ready' | 'attention';
    configPath: string;
    items: UserSkillCapability[];
    issues: SkillCatalogIssue[];
    details: string[];
    roots: Array<{ kind: 'agents' | 'pulsara'; path: string }>;
  };
  mcp: {
    configPath: string;
    servers: UserMcpServerCapability[];
  };
  plugins: {
    status: 'ready' | 'attention';
    items: UserPluginCapability[];
    details: string[];
  };
  adoption?: {
    updatedSessions: number;
    pendingSessions: number;
    attentionSessions: number;
  };
}

export interface CapabilityOperation {
  status: string;
  success: boolean;
  message: string;
  details: string[];
  pluginId?: string;
}

export interface ProjectCapabilityAdoption {
  scope: 'workspace';
  pendingSessions: number;
  when: 'next-provider-dispatch';
}

export interface ProjectCapabilityMutationResult {
  operation: CapabilityOperation;
  adoption: ProjectCapabilityAdoption;
  capabilities: CapabilitySnapshot;
}

export interface McpEditInput {
  serverId: string;
  retainCredentialsConfirmed?: boolean;
  config: Record<string, unknown>;
  secretChanges: Array<{
    binding: { owner: McpCredentialOwner; name: string };
    value: string | null;
  }>;
}

export interface McpConnectionTestResult {
  status: 'ready' | 'credential_required' | 'authorization_required' | 'timeout' | 'schema_bound_exceeded' | 'failed';
  tools: number;
  resources: number;
  resource_templates: number;
  prompts: number;
}

export type SessionWorkspaceSelection =
  | { kind: 'quick' }
  | { kind: 'project'; path: string };

export interface SessionSummary {
  canArchive?: boolean;
  lifecycle?: 'OPEN' | 'ARCHIVED';
  id: string;
  title: string;
  subtitle: string;
  status: SessionStatus | null;
  updatedAt: string;
  live: boolean;
  pinned?: boolean;
  unread?: boolean;
  modelCallBinding?: {
    connection_id: string;
    reasoning:
      | { kind: 'effort'; value: string | null }
      | { kind: 'toggle'; enabled: boolean }
      | { kind: 'budget_tokens'; tokens: number }
      | null;
  } | null;
  workspace?: Workspace;
}

export type ArchivedSessionDeletionItem = {
  session: SessionSummary;
  status: 'pending' | 'running' | 'DELETED' | 'ABSENT' | 'NOT_ARCHIVED' | 'failed' | 'unknown';
  detail?: string;
};

export interface TodoItem {
  id: string;
  label: string;
  status: 'pending' | 'in-progress' | 'completed';
}

export interface TodoRun {
  id: string;
  items: TodoItem[];
}

export interface ToolTrace {
  attemptId?: string;
  programCalls?: ToolTrace[];
  id: string;
  kind: 'terminal' | 'read' | 'edit' | 'search' | 'artifact' | 'mcp';
  toolName?: string;
  title: string;
  subtitle: string;
  status: TaskStatus;
  duration?: string;
  argumentsJson?: string;
  resultText?: string;
  resultContent?: CanonicalPromptContent;
  resultEntryId?: string;
  resultState?: string;
  resultSummary?: string;
  associationPending?: boolean;
  artifact?: {
    disposition: 'NOT_REQUIRED' | 'AVAILABLE' | 'INCOMPLETE' | 'UNAVAILABLE';
    sourceCoverage: 'COMPLETE' | 'RETAINED_SNAPSHOT';
    displayKind: 'COMPLETE' | 'HEAD_TAIL';
    sourceCoverageReason?: string;
    unavailabilityReason?: string;
  };
  command?: string;
  meta?: string;
}

export interface ReasoningBlock {
  id: string;
  kind: 'full' | 'summary';
  body: string;
  active?: boolean;
}

export interface Message {
  sourceEntryIds?: string[];
  historyGroupId?: string;
  /** Navigation hint only: page context may name a final beyond its cut. */
  historyFinalEntryId?: string;
  historyInitialEntryId?: string;
  historyWindowId?: string;
  historyEarlierUnloaded?: boolean;
  historyContextState?: 'loading' | 'ready' | 'error';
  historyContextError?: string;
  historyIncludeReasoning?: boolean;
  reasoningContentState?: 'unloaded' | 'loading' | 'ready' | 'error';
  reasoningContentError?: string;
  contentState?: 'unloaded' | 'loading' | 'ready' | 'error';
  contentError?: string;
  id: string;
  forkEligible?: boolean;
  rootFinal?: boolean;
  entryOwnerKind?: 'EXECUTED_TURN' | 'IMPORTED_HISTORY';
  turnId?: string;
  entrySequence?: number;
  role: 'user' | 'assistant';
  userKind?: 'prompt' | 'scheduled' | 'steer' | 'plan-continuation';
  scheduledInput?: { taskId: string; taskRevision: number; dueAtUtc: string; triggerSource: 'TIMER' | 'RUN_ONCE' };
  inputSource?: {
    queueItemId: string;
    commandId: string;
    deliveryMode: 'new-turn' | 'steer';
  };
  assistantKind?: 'terminal' | 'tool-request' | 'live';
  time: string;
  body: string;
  promptContent?: CanonicalPromptContent;
  pendingContent?: CanonicalPromptContent;
  // Known card content while grouping/body reads finish; streamed bytes require
  // the exact settlement entry binding. Never establishes canonical readiness.
  pendingAssistant?: Pick<Message, 'body' | 'reasoning' | 'traces'>;
  reasoning?: ReasoningBlock[];
  model?: string;
  status?: SessionStatus;
  traces?: ToolTrace[];
  visualizations?: VisualizationOccurrence[];
}

export interface VisualizationOccurrence {
  mediaType?: string;
  codec?: string;
  imageRef?: string;
  width?: number;
  height?: number;
  ordinal: number;
  state: 'READY' | 'FAILED';
  sourceFilename?: string;
  contentDigest?: string;
  contentSize?: number;
  failureCode?: string;
  failureDetail?: string;
}

export interface ToastMessage {
  id: number;
  title: string;
  detail?: string | { before: string; label: string; after: string; href: string; onClick: () => void };
  tone?: 'neutral' | 'success' | 'warning';
  onClick?: () => void;
}

export const permissionLabels: Record<PermissionMode, string> = {
  'accept-edits': '接受编辑',
  'read-only': '只读',
  'ask-permissions': '每次询问',
  'bypass-permissions': '完全访问',
};

export const permissionModeOrder: readonly PermissionMode[] = [
  'read-only',
  'ask-permissions',
  'accept-edits',
  'bypass-permissions',
];

export const protocolPermissionModes: Record<PermissionMode, string> = {
  'accept-edits': 'PERMISSION_MODE_ACCEPT_EDITS',
  'read-only': 'PERMISSION_MODE_READ_ONLY',
  'ask-permissions': 'PERMISSION_MODE_ASK_PERMISSIONS',
  'bypass-permissions': 'PERMISSION_MODE_BYPASS_PERMISSIONS',
};
export interface SkillImportInput {
  sourcePath: string;
  name?: string;
  description?: string;
}

export interface SkillImportCandidate extends SkillImportInput {
  name: string;
  description: string;
  valid: boolean;
  details: string[];
}
export interface McpImportSource {
  content: string;
  shape: 'auto' | 'mcpServers' | 'opencode' | 'map' | 'server';
  server_id?: string;
}
export interface McpImportPreview {
  server_id: string;
  transport: 'stdio' | 'streamable_http' | 'sse' | null;
  fields: Array<{ target: string; private: boolean; template_literals: boolean; variables: Array<{ name: string; has_default: boolean; environment: boolean; file_path?: string | null }> }>;
  issues: Array<{ path: string; message: string }>;
  notices: string[];
}
export interface PluginImportOptions {
  mcp_scope_policies?: Record<string, 'ROOT_ONLY' | 'ROOT_AND_SUBAGENTS'>;
  source_format: 'native' | 'claude' | 'codex' | 'cursor';
  classifications: Record<string, string>;
  public_values: Record<string, string>;
}
export interface PluginImportPreview {
  name: string;
  source_format: PluginImportOptions['source_format'];
  skills: string[];
  hooks: string[];
  mcp: McpImportPreview[];
  notices: string[];
}
export interface PluginImportDiscovery {
  candidates: Array<{
    source_format: PluginImportOptions['source_format'];
    manifest: string;
    preview: PluginImportPreview | null;
    error: string | null;
  }>;
}

export interface McpImportSelection extends McpImportSource {
  selected_server_id: string;
  classifications: Record<string, string>;
  values: Record<string, string>;
  transport?: 'streamable_http' | 'sse';
  allow_http_localhost?: boolean;
}
