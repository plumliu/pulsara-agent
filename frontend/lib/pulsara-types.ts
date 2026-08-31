export type AppView =
  | 'overview'
  | 'workbench'
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
  userKind?: 'prompt' | 'steer' | 'plan-continuation' | 'subagent-result';
  sourceSubagentResultId?: string;
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
  accepted: boolean;
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

export const protocolPermissionModes: Record<PermissionMode, string> = {
  'accept-edits': 'PERMISSION_MODE_ACCEPT_EDITS',
  'read-only': 'PERMISSION_MODE_READ_ONLY',
  'ask-permissions': 'PERMISSION_MODE_ASK_PERMISSIONS',
  'bypass-permissions': 'PERMISSION_MODE_BYPASS_PERMISSIONS',
};
