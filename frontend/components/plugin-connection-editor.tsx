'use client';

import { McpEditor } from './mcp-editor';
import type { McpEditInput, PluginMcpConnection, PluginMcpEditInput, UserMcpServerCapability, UserPluginCapability } from '../lib/pulsara-types';

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};

export function PluginConnectionEditor({ plugin, connection, onClose, onSave, onAuthorization }: {
  plugin: UserPluginCapability; connection: PluginMcpConnection; onClose: () => void;
  onSave: (input: PluginMcpEditInput) => Promise<boolean>;
  onAuthorization?: (action: 'login' | 'status' | 'cancel' | 'logout') => Promise<void>;
}) {
  const transport = record(connection.config.transport);
  const server: UserMcpServerCapability = {
    id: connection.serverId, name: `${plugin.name} · ${connection.serverId}`, enabled: plugin.enabled,
    status: plugin.enabled ? 'configured' : 'disabled', required: false, availableToSubagents: true,
    toolCount: 0, resourceCount: 0, resourceTemplateCount: 0, promptCount: 0, instructions: '', hasFailure: false,
    transport: { kind: transport.type === 'stdio' ? 'stdio' : 'http', summary: String(transport.command ?? transport.endpoint ?? '') },
    tools: [], config: connection.config, currentIdentity: plugin.packageInstallId,
  };
  const save = (input: McpEditInput) => {
    const next = record(input.config.transport);
    const defaults = record(connection.defaults.transport);
    const prior = connection.overlay ?? {};
    const difference = (values: unknown, base: unknown, previous: unknown) => Object.fromEntries(
      Object.entries(record(values)).filter(([name, value]) => value !== record(base)[name] || name in record(previous)),
    );
    return onSave({
      overlay: {
        local_server_id: connection.serverId, transport_kind: next.type,
        endpoint: next.type === 'stdio' ? null : next.endpoint !== defaults.endpoint || prior.endpoint != null ? next.endpoint : null,
        public_headers: difference(input.config.public_headers, connection.defaults.public_headers, prior.public_headers),
        environment: difference(next.env, defaults.env, prior.environment),
        secret_environment: record(next.secret_env), auth: input.config.auth,
      },
      secretChanges: input.secretChanges, retainCredentialsConfirmed: input.retainCredentialsConfirmed,
    });
  };
  return <McpEditor server={server} credentialOwner={connection.credentialOwner} connectionInputs={connection.connectionInputs} packageDefinition={connection.defaults} onClose={onClose} onSave={save} onAuthorization={onAuthorization} onRestoreDefaults={connection.overlay ? () => onSave({overlay: null, secretChanges: []}) : undefined} />;
}
