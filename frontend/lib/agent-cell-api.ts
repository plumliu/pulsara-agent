import { inspectorTab, type PreparedWorkspaceTab } from '../components/desktop-workspace';

export interface AgentRow {
  agent_id: string; title: string | null;
  current_work: { work_id: string; work_title: string | null; status: 'running' } | null;
  queued_work_count: number; has_running_turn: boolean; host_session_id?: string | null;
}
export function agentLabel(agent: Pick<AgentRow, 'agent_id' | 'title'>) { return agent.title || `子代理 ${agent.agent_id.split(':').at(-1)!.slice(0, 6)}`; }
export function workTitle(title: string | null | undefined) { return title ?? '未命名任务'; }
export function agentActivity(agent: AgentRow) { return agent.current_work ? '工作中' : agent.has_running_turn ? '处理中' : agent.queued_work_count > 0 ? '排队中' : '空闲'; }
export function agentTaskLabel(agent: AgentRow) { return agent.current_work ? `当前任务：${workTitle(agent.current_work.work_title)}` : agentActivity(agent); }
export interface CellRow { target: { kind: 'CELL'; cell_id: string }; state: string; has_unread_output: boolean; pending_calls: number; source_exec_attempt_id: string }
export interface WorkFact { agent_id: string; work_id: string; work_title: string | null; status: string; terminal_reason?: string | null; consumed_entry_id?: string | null; turn_id?: string | null }
export interface WorkInput { kind: 'dependency' | 'material'; source_work_id: string; ordinal: number }
export interface Participant { agent_id: string; work_id: string | null; kinds: string[] }
export type SourcePage = { status: 'AVAILABLE'; text: string; offset_chars: number; returned_chars: number; total_chars: number; has_more: boolean; next_offset_chars: number | null } | { status: 'UNAVAILABLE'; reason: string };
export type CellGraphResult = CellGraphPage | { status: 'CLOSED'; message: string };
export interface CellGraphPage { cell: CellRow; participation_coverage: 'complete' | 'incomplete' | 'unavailable'; section: 'works' | 'participants'; items: Array<WorkFact | Participant>; next_cursor: string | null }
export class AgentCellError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
async function request<T>(path: string, body?: object, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store', signal,
    ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new AgentCellError(response.status, payload?.error?.message ?? '暂时无法读取。');
  return payload as T;
}
const rootPath = (root: string) => `/api/sessions/${encodeURIComponent(root)}`;
const cellsPath = (root: string, host: string) => `${rootPath(root)}/hosts/${encodeURIComponent(host)}/cells`;
export const agentCellApi = {
  agents: (root: string, cursor?: string, signal?: AbortSignal) => request<{ agents: AgentRow[]; next_cursor: string | null }>(`${rootPath(root)}/agents`, { ...(cursor ? { cursor } : {}), max_items: 50 }, signal),
  agent: (root: string, agent: string, signal?: AbortSignal) => request<{ agent: AgentRow }>(`${rootPath(root)}/agents/${encodeURIComponent(agent)}`, undefined, signal),
  cells: (root: string, host: string, cursor?: string, signal?: AbortSignal) => request<{ cells: CellRow[]; next_cursor: string | null; has_more: boolean }>(cellsPath(root, host), { ...(cursor ? { cursor } : {}), max_items: 50 }, signal),
  cell: (root: string, host: string, cell: string, signal?: AbortSignal) => request<{ cell: CellRow }>(`${cellsPath(root, host)}/${encodeURIComponent(cell)}`, undefined, signal),
  source: (root: string, attempt: string, offset = 0, signal?: AbortSignal) => request<{ source_exec_attempt_id: string; source: SourcePage; live_cell: { host_session_id: string; cell_id: string } | null }>(`${rootPath(root)}/cell-sources/${encodeURIComponent(attempt)}`, { offset_chars: offset }, signal),
  graph: (root: string, host: string, cell: string, section: 'works' | 'participants', cursor?: string, signal?: AbortSignal) => request<CellGraphResult>(`${cellsPath(root, host)}/${encodeURIComponent(cell)}/graph`, { section, ...(cursor ? { cursor } : {}), max_items: 50 }, signal),
  work: (root: string, work: string, signal?: AbortSignal) => request<{ work: WorkFact; inputs: WorkInput[] }>(`${rootPath(root)}/works/${encodeURIComponent(work)}/inputs`, {}, signal),
};
export type SessionNavigation =
  | { kind: 'subagents'; rootSessionId: string }
  | { kind: 'cells'; rootSessionId: string; hostSessionId: string }
  | { kind: 'agent'; rootSessionId: string; agentSessionId: string }
  | { kind: 'cell'; rootSessionId: string; sourceExecAttemptId: string; hostSessionId?: string; cellId?: string }
  | { kind: 'cell-graph'; rootSessionId: string; hostSessionId: string; cellId: string };
/** Validate a specific target against its actual owner before publishing a tab. */
export async function prepareSessionTarget(intent: SessionNavigation, valid: () => boolean): Promise<PreparedWorkspaceTab | undefined> {
  let tab;
  if (intent.kind === 'agent') {
    const result = await agentCellApi.agent(intent.rootSessionId, intent.agentSessionId);
    tab = inspectorTab(intent.rootSessionId, 'agent', agentLabel(result.agent), intent.agentSessionId);
  } else if (intent.kind === 'cell' || intent.kind === 'cell-graph') {
    if (intent.kind === 'cell') {
      const source = await agentCellApi.source(intent.rootSessionId, intent.sourceExecAttemptId);
      tab = { id: JSON.stringify([intent.rootSessionId, 'cell', intent.sourceExecAttemptId]), sessionId: intent.rootSessionId, kind: 'cell' as const, title: intent.cellId || '脚本源码', sourceExecAttemptId: intent.sourceExecAttemptId, entityId: source.live_cell?.cell_id, hostSessionId: source.live_cell?.host_session_id };
    } else {
      const graph = await agentCellApi.graph(intent.rootSessionId, intent.hostSessionId, intent.cellId, 'works');
      if ('status' in graph) throw new Error(graph.message);
      tab = inspectorTab(intent.rootSessionId, intent.kind, '工作流图', intent.cellId, intent.hostSessionId);
    }
  } else if (intent.kind === 'cells') {
    await agentCellApi.cells(intent.rootSessionId, intent.hostSessionId);
    tab = inspectorTab(intent.rootSessionId, 'cells', 'cell', undefined, intent.hostSessionId);
  } else {
    await agentCellApi.agents(intent.rootSessionId);
    tab = inspectorTab(intent.rootSessionId, 'subagents', '子代理');
  }
  if (!valid()) return undefined;
  return { adopt: () => valid() ? tab : undefined, release: () => {} };
}
