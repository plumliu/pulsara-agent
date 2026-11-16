import { isCanonicalIdentity } from './identity';

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
export interface PresentationResult { targets: PresentationTarget[] }
export interface PresentationRequest {
  sequence: string;
  signal: AbortSignal;
  read(): Promise<PresentationResult | undefined>;
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string' && !!value.trim() && !value.includes('\0');
const closed = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));

/** Only actual accepted display facts enter this path; public tool prose is inert. */
export function parsePresentationTargets(value: unknown): PresentationResult | undefined {
  if (!Array.isArray(value)) return;
  const targets: PresentationTarget[] = [];
  for (const item of value) {
    if (!record(item) || !record(item.target)) return;
    const target = item.target;
    if (target.kind === 'PATH') {
      if (!closed(item, ['target']) || !closed(target, ['kind', 'path']) || !text(target.path) || !target.path.startsWith('/')) return;
      targets.push({ type: 'file', path: target.path });
    } else if (target.kind === 'URL') {
      if (!closed(item, ['target']) || !closed(target, ['kind', 'url']) || !text(target.url)) return;
      targets.push({ type: 'browser', url: target.url });
    } else if (target.kind === 'AGENT') {
      if (!closed(item, ['target', 'root_session_id']) || !closed(target, ['kind', 'agent_id'])
        || !isCanonicalIdentity(target.agent_id, 'session') || !isCanonicalIdentity(item.root_session_id, 'session')) return;
      targets.push({ type: 'agent', agent_id: target.agent_id, root_session_id: item.root_session_id });
    } else return;
  }
  return { targets };
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
