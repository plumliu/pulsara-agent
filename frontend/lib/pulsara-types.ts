export type AppView =
  | 'overview'
  | 'workbench'
  | 'capabilities'
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

export type SessionStatus =
  | 'running'
  | 'waiting'
  | 'completed'
  | 'interrupted'
  | 'draft';

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
  adoption: {
    scope: 'workspace';
    pending: boolean;
    attention?: 'PROJECT_CAPABILITY_ADOPTION_FAILED' | 'PROJECT_MCP_ADOPTION_INCOMPLETE';
    when: 'next-user-turn';
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
  effectiveSkillNames: string[];
  effectiveMcpServerIds: string[];
  details: string[];
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
  when: 'next-user-turn';
}

export interface ProjectCapabilityMutationResult {
  operation: CapabilityOperation;
  adoption: ProjectCapabilityAdoption;
  capabilities: CapabilitySnapshot;
}

export interface McpCreateInput {
  serverId: string;
  displayName: string;
  transport: 'http' | 'stdio';
  endpoint?: string;
  command?: string;
  args: string[];
  availableToSubagents: boolean;
}

export type SessionWorkspaceSelection =
  | { kind: 'quick' }
  | { kind: 'project'; path: string };

export interface SessionSummary {
  id: string;
  title: string;
  subtitle: string;
  status: SessionStatus;
  updatedAt: string;
  live: boolean;
  pinned?: boolean;
  unread?: boolean;
  taskCounts?: {
    total: number;
    active: number;
    waiting: number;
    attention: number;
  };
  workspace?: Workspace;
}

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
  id: string;
  kind: 'terminal' | 'read' | 'edit' | 'search' | 'artifact' | 'mcp';
  toolName?: string;
  title: string;
  subtitle: string;
  status: TaskStatus;
  duration?: string;
  command?: string;
  output?: string[];
  meta?: string;
}

export interface SubagentActivity {
  id: string;
  time: string;
  body: string;
  kind?: 'work' | 'guidance';
  reasoning?: ReasoningBlock[];
  status?: TaskStatus;
  traces?: ToolTrace[];
}

export interface ReasoningBlock {
  id: string;
  kind: 'full' | 'summary';
  body: string;
  active?: boolean;
}

export interface SubagentRun {
  id: string;
  label: string;
  role: string;
  objective: string;
  status: TaskStatus | 'ended';
  parentId?: string;
  summary?: string;
  color: 'blue' | 'amber' | 'violet' | 'green';
  activities: SubagentActivity[];
}

export interface Message {
  id: string;
  turnId?: string;
  role: 'user' | 'assistant';
  userKind?: 'prompt' | 'steer' | 'plan-continuation' | 'subagent-completion';
  assistantKind?: 'terminal' | 'tool-request' | 'live';
  sourceSubagentTaskId?: string;
  time: string;
  body: string;
  reasoning?: ReasoningBlock[];
  model?: string;
  status?: SessionStatus;
  traces?: ToolTrace[];
  subagentRuns?: SubagentRun[];
}

export interface AgentTask {
  id: string;
  label: string;
  role: string;
  profile?: string;
  objective: string;
  status: TaskStatus;
  parentId?: string;
  batchId?: string;
  taskKey?: string;
  context?: {
    mode: 'none' | 'last-n';
    lastNTurns?: number;
  };
  pendingReason?: string;
  terminalReason?: string;
  terminalPublicDetail?: string;
  completionDelivered: boolean;
  acceptedAt?: string;
  terminalAt?: string;
  dependencyIds: string[];
  dependencies?: AgentTaskDependency[];
  summary?: string;
  progress?: string;
  result?: AgentTaskResult;
  color: 'blue' | 'amber' | 'violet' | 'green';
}

export interface AgentTaskDependency {
  id: string;
  taskKey?: string;
  label?: string;
  status: TaskStatus;
  resultSummary?: string;
}

export interface AgentTaskResult {
  id: string;
  entryId?: string;
  summary: string;
  outputPreview?: string;
  diagnostics: Array<Record<string, unknown>>;
}

export interface ToastMessage {
  id: number;
  title: string;
  detail?: string;
  tone?: 'neutral' | 'success' | 'warning';
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
