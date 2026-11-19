import { mcpAuthorizationKey } from './mcp-authorization';
import { McpEditor } from './mcp-editor';
import type { McpEditInput, PluginMcpConnection, PluginMcpEditInput, UserMcpServerCapability, UserPluginCapability } from '../lib/pulsara-types';

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};

function sameConfiguration(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((value, index) => sameConfiguration(value, right[index]));
  }
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  const entries = Object.entries(left);
  const other = record(right);
  return entries.length === Object.keys(other).length
    && entries.every(([key, value]) => Object.hasOwn(other, key) && sameConfiguration(value, other[key]));
}

export function PluginConnectionEditor({ plugin, connection, savedDestination, prefillRestoresDefaults, onClose, onSave, onAuthorization, presentation = 'dialog', scopeLabel }: {
  presentation?: 'dialog' | 'inline'; scopeLabel?: string;
  plugin: Pick<UserPluginCapability, 'name' | 'enabled' | 'packageInstallId'>; connection: PluginMcpConnection; onClose: () => void;
  savedDestination?: Record<string, unknown>;
  prefillRestoresDefaults?: boolean;
  onSave: (input: PluginMcpEditInput) => Promise<boolean>;
  onAuthorization?: (action: 'login' | 'status' | 'cancel' | 'logout') => Promise<void>;
}) {
  const transport = record(connection.config.transport);
  const server: Omit<UserMcpServerCapability, 'currentIdentity'> = {
    id: connection.serverId, name: String(connection.config.display_name ?? connection.serverId), enabled: plugin.enabled,
    status: plugin.enabled ? 'configured' : 'disabled', availableToSubagents: connection.config.scope_policy === 'ROOT_AND_SUBAGENTS',
    toolCount: 0, resourceCount: 0, resourceTemplateCount: 0, promptCount: 0, instructions: '', hasFailure: false,
    transport: { kind: transport.type === 'stdio' ? 'stdio' : 'http', summary: String(transport.command ?? transport.endpoint ?? '') },
    tools: [], config: connection.config,
  };
  const toOverlay = (config: Record<string, unknown>) => {
    const next = record(config.transport);
    const baseline = prefillRestoresDefaults ? connection.config : connection.defaults;
    const defaults = record(baseline.transport);
    const prior = connection.overlay ?? {};
    const difference = (values: unknown, base: unknown, previous: unknown) => Object.fromEntries(
      Object.entries(record(values)).filter(([name, value]) => !sameConfiguration(value, record(base)[name]) || name in record(previous)),
    );
    return {
      local_server_id: connection.serverId, transport_kind: next.type,
      scope_policy: config.scope_policy,
      endpoint: next.type === 'stdio' ? null : next.endpoint !== defaults.endpoint || prior.endpoint != null ? next.endpoint : null,
      public_headers: difference(config.public_headers, baseline.public_headers, prior.public_headers),
      environment: difference(next.env, defaults.env, prior.environment),
      secret_environment: prefillRestoresDefaults ? difference(next.secret_env, defaults.secret_env, {}) : record(next.secret_env),
      auth: prefillRestoresDefaults && sameConfiguration(config.auth, baseline.auth) ? {type: 'none'} : config.auth,
    };
  };
  const save = (input: McpEditInput) => {
    const overlay = toOverlay(input.config);
    const unchangedRestore = prefillRestoresDefaults && input.secretChanges.length === 0
      && sameConfiguration(overlay, toOverlay(connection.config));
    return onSave({
      overlay: unchangedRestore ? null : overlay,
      secretChanges: input.secretChanges, retainCredentialsConfirmed: input.retainCredentialsConfirmed,
    });
  };
  return <McpEditor presentation={presentation} title={presentation === 'inline' ? `配置 ${server.name} 连接` : undefined} sourceName={presentation === 'inline' ? plugin.name : undefined} scopeLabel={scopeLabel} authorizationKey={plugin.packageInstallId.visibility === 'VISIBLE' ? mcpAuthorizationKey('plugin', plugin.packageInstallId.value, connection.serverId) : undefined} server={server} savedDestination={savedDestination} credentialOwner={connection.credentialOwner} connectionInputs={connection.connectionInputs} packageDefinition={connection.defaults} onClose={onClose} onSave={save} onAuthorization={onAuthorization} onRestoreDefaults={connection.overlay ? () => onSave({overlay: null, secretChanges: []}) : undefined} />;
}
