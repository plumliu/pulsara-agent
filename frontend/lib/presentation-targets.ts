/** The tool requests views, never execution or confirmation of rendering. */
export type PresentationTarget =
  | { type: 'file'; path: string }
  | { type: 'browser'; url: string }
  | { type: 'terminal'; process_id: string; host_session_id: string }
  | { type: 'mcp'; runtime_server_id: string; host_session_id: string }
  | { type: 'agent'; agent_id: string; root_session_id: string }
  | { type: 'cell'; cell_id: string; root_session_id: string; host_session_id: string }
  | { type: 'panel'; name: 'capabilities' | 'background' | 'browser' }
  | { type: 'panel'; name: 'subagents'; root_session_id: string }
  | { type: 'panel'; name: 'cells'; root_session_id: string; host_session_id: string };
export interface PresentationResult { targets: PresentationTarget[]; errors?: Array<{ index: number; message: string }> }
export interface PresentationRequest {
  sequence: string;
  signal: AbortSignal;
  read(): Promise<PresentationResult | undefined>;
}
const fields = { PATH: ['file', 'path'], URL: ['browser', 'url'], PROCESS: ['terminal', 'process_id'], MCP_SERVER: ['mcp', 'runtime_server_id'], PANEL: ['panel', 'name'], AGENT: ['agent', 'agent_id'], CELL: ['cell', 'cell_id'] } as const;
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string' && !!value.trim() && !value.includes('\0');
const closed = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
export function parsePresentationResult(raw: string): PresentationResult | undefined {
  try {
    const result: unknown = JSON.parse(raw);
    if (!record(result) || !Array.isArray(result.targets) || Object.keys(result).some(key => !['targets', 'errors'].includes(key))) return;
    const targets: PresentationTarget[] = [];
    for (const item of result.targets) {
      if (!record(item) || !record(item.target)) return;
      const target = item.target;
      if (typeof target.kind !== 'string' || !Object.hasOwn(fields, target.kind)) return;
      const [type, field] = fields[target.kind as keyof typeof fields];
      const value = target[field];
      if (!closed(target, ['kind', field]) || !text(value)) return;
      const rootBound = target.kind === 'AGENT' || target.kind === 'CELL' || (target.kind === 'PANEL' && ['subagents', 'cells'].includes(value));
      const hostBound = ['PROCESS', 'MCP_SERVER', 'CELL'].includes(target.kind) || (target.kind === 'PANEL' && value === 'cells');
      if (!closed(item, ['target', ...(rootBound ? ['root_session_id'] : []), ...(hostBound ? ['host_session_id'] : [])])) return;
      if ((rootBound && !text(item.root_session_id)) || (hostBound && !text(item.host_session_id))) return;
      if (target.kind === 'PATH' && !value.startsWith('/')) return;
      if (target.kind === 'AGENT' && !/^session:[a-z2-7]{25}[aeimquy4]$/.test(value)) return;
      if (target.kind === 'CELL' && !/^cell:[a-z2-7]{25}[aeimquy4]$/.test(value)) return;
      // Browser policy remains with the browser owner so one bad URL cannot discard other views.
      if (target.kind === 'PANEL' && !['capabilities', 'background', 'subagents', 'cells', 'browser'].includes(value)) return;
      targets.push({ type, [field]: value, ...(rootBound ? { root_session_id: item.root_session_id } : {}), ...(hostBound ? { host_session_id: item.host_session_id } : {}) } as PresentationTarget);
    }
    if (result.errors !== undefined && (!Array.isArray(result.errors) || result.errors.some(error => !record(error) || !closed(error, ['index', 'message']) || !Number.isSafeInteger(error.index) || (error.index as number) < 0 || typeof error.message !== 'string'))) return;
    return { targets, ...(result.errors !== undefined ? { errors: result.errors as PresentationResult['errors'] } : {}) };
  } catch { return; }
}
export function presentationLabel(target: PresentationTarget) {
  if (target.type === 'file') return target.path.split('/').pop() || target.path;
  if (target.type === 'browser') return target.url;
  if (target.type === 'terminal') return target.process_id;
  if (target.type === 'mcp') return target.runtime_server_id;
  if (target.type === 'agent') return target.agent_id;
  if (target.type === 'cell') return target.cell_id;
  return { capabilities: '能力', background: '进程', subagents: '子代理', cells: 'cell', browser: '网页' }[target.name];
}
