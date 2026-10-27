import type { McpCredentialOwner } from './pulsara-types';

export interface McpAuthorizationResult {
  scope: 'USER' | 'WORKSPACE';
  authorization: {
    owner: McpCredentialOwner;
    config_identity: string;
    display_identity: string;
    package_install_id: string | null;
  };
}
export interface McpAuthorizationRequest {
  signal: AbortSignal;
  read(): Promise<McpAuthorizationResult | undefined>;
}

/** Only the host's complete manage_capability result can announce a completed login. */
export function parseMcpAuthorizationResult(text: string): McpAuthorizationResult | undefined {
  try {
    const result = JSON.parse(text);
    const target = result?.authorization;
    const connection = result?.target;
    const owner = target?.owner;
    if (result?.action !== 'AUTHORIZE_MCP' || result.status !== 'APPLIED' || result.current?.authorized !== true
      || !['USER', 'WORKSPACE'].includes(connection?.scope)
      || connection?.kind !== 'MCP_CONNECTION'
      || !owner || !['local', 'plugin'].includes(owner.kind)
      || typeof owner.scope_key !== 'string' || !owner.scope_key
      || typeof owner.server_id !== 'string' || !owner.server_id
      || connection.server_id !== owner.server_id
      || connection.source_kind !== (owner.kind === 'plugin' ? 'PLUGIN' : 'LOCAL')
      || (owner.kind === 'plugin' ? connection.plugin_id !== owner.plugin_id : 'plugin_id' in connection)
      || (owner.kind === 'plugin' ? typeof owner.plugin_id !== 'string' || !owner.plugin_id : owner.plugin_id !== null)
      || typeof target.config_identity !== 'string' || !target.config_identity
      || typeof target.display_identity !== 'string' || !target.display_identity
      || (owner.kind === 'plugin' ? typeof target.package_install_id !== 'string' || !target.package_install_id : target.package_install_id !== null)) return;
    return { scope: connection.scope, authorization: target };
  } catch { return; }
}
