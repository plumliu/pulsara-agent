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
    for (const target of result.targets) {
      if (!target || typeof target !== 'object') return;
      const fields: Record<string, string[]> = { file: ['path'], browser: ['url'], task: ['task_id'], terminal: ['process_id', 'host_session_id'], mcp: ['runtime_server_id', 'host_session_id'], panel: ['name'] };
      const required = fields[target.type];
      if (!required || Object.keys(target).length !== required.length + 1 || required.some(key => typeof target[key] !== 'string' || !target[key].trim())) return;
      if (target.type === 'file' && !target.path.startsWith('/')) return;
      // URL policy belongs to the browser owner: one rejected URL must not discard other targets.
      if (target.type === 'panel' && !['capabilities', 'tasks', 'background', 'browser'].includes(target.name)) return;
    }
    if (result.errors !== undefined && (!Array.isArray(result.errors) || result.errors.some((error: { index: number; message: string }) => !error || !Number.isSafeInteger(error.index) || error.index < 0 || typeof error.message !== 'string'))) return;
    return result;
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
