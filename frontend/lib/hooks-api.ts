export interface HookSource extends Record<string, unknown> {
  path: string;
  plugin_id?: string;
  source_disposition: string;
  trust_state: string;
  enabled: boolean;
  definition_digest: string | null;
  trusted_definition_digest: string | null;
  trusted_at: string | null;
  definition_count: number;
  can_trust: boolean;
  exists?: boolean;
  editable?: boolean;
  definitions?: Array<{event: string; command: string; matcher: string; async: boolean}>;
  diagnostics: Array<{code: string; message: string}>;
}
export interface HookConfigText { exists: boolean; text: string; path: string }
export interface HookMutation { status: 'APPLIED' | 'ATTENTION'; message: string | null; pending_sessions: number }
export interface UserHooksApi {
  list(signal?: AbortSignal): Promise<{sources: HookSource[]; details: string[]}>;
  config(signal?: AbortSignal): Promise<HookConfigText>;
  save(config: HookConfigText, text: string): Promise<HookMutation>;
  review(pluginId?: string, signal?: AbortSignal): Promise<HookSource>;
  trust(source: HookSource): Promise<HookMutation>;
}
