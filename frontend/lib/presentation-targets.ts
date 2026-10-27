/** The tool requests views, never execution or confirmation of rendering. */
export type PresentationTarget =
  | { type: 'file'; path: string }
  | { type: 'browser'; url: string }
  | { type: 'task'; task_id: string }
  | { type: 'terminal'; process_id: string; host_session_id: string }
  | { type: 'mcp'; runtime_server_id: string; host_session_id: string }
  | { type: 'panel'; name: 'capabilities' | 'tasks' | 'background' | 'browser' };
export interface PresentationResult { targets: PresentationTarget[]; errors?: Array<{ index: number; message: string }> }
export interface PresentationRequest {
  sequence: string;
  signal: AbortSignal;
  read(): Promise<PresentationResult | undefined>;
}
export function parsePresentationResult(text: string): PresentationResult | undefined {
  try {
    const result = JSON.parse(text);
    if (!result || !Array.isArray(result.targets) || Object.keys(result).some(key => !['targets', 'errors'].includes(key))) return;
    const targets: PresentationTarget[] = [];
    for (const item of result.targets) {
      if (!item || typeof item !== 'object') return;
      if (item.type === 'task') {
        if (Object.keys(item).length !== 2 || typeof item.task_id !== 'string' || !item.task_id.trim()) return;
        targets.push(item);
        continue;
      }
      const target = item.target;
      if (!target || typeof target !== 'object') return;
      const kinds: Record<string, [string, string]> = { PATH: ['file', 'path'], URL: ['browser', 'url'], PROCESS: ['terminal', 'process_id'], MCP_SERVER: ['mcp', 'runtime_server_id'], PANEL: ['panel', 'name'] };
      const shape = kinds[target.kind];
      if (!shape || Object.keys(target).length !== 2 || typeof target[shape[1]] !== 'string' || !target[shape[1]].trim()) return;
      const bound = target.kind === 'PROCESS' || target.kind === 'MCP_SERVER';
      if (Object.keys(item).length !== (bound ? 2 : 1) || (bound && (typeof item.host_session_id !== 'string' || !item.host_session_id.trim()))) return;
      if (target.kind === 'PATH' && !target.path.startsWith('/')) return;
      // Browser policy remains with the browser owner so one bad URL cannot discard other views.
      if (target.kind === 'PANEL' && !['capabilities', 'tasks', 'background', 'browser'].includes(target.name)) return;
      targets.push({ type: shape[0], [shape[1]]: target[shape[1]], ...(bound ? { host_session_id: item.host_session_id } : {}) } as PresentationTarget);
    }
    if (result.errors !== undefined && (!Array.isArray(result.errors) || result.errors.some((error: { index: number; message: string }) => !error || !Number.isSafeInteger(error.index) || error.index < 0 || typeof error.message !== 'string'))) return;
    return { ...result, targets };
  } catch { return; }
}
export function presentationLabel(target: PresentationTarget) {
  if (target.type === 'file') return target.path.split('/').pop() || target.path;
  if (target.type === 'browser') return target.url;
  if (target.type === 'task') return target.task_id;
  if (target.type === 'terminal') return target.process_id;
  if (target.type === 'mcp') return target.runtime_server_id;
  return { capabilities: '会话能力', tasks: '任务', background: '后台进程', browser: '新建网页' }[target.name];
}
