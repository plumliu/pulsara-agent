import type { McpCredentialOwner, PublicPackageId } from './pulsara-types';
import { isCanonicalIdentity } from './identity';

export function parsePublicPackageId(value: unknown): PublicPackageId | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  const object = value as Record<string, unknown>;
  const keys = Object.keys(object);
  if (object.visibility === 'WITHHELD' && keys.length === 1) return {visibility: 'WITHHELD'};
  if (object.visibility === 'VISIBLE' && keys.length === 2 && isCanonicalIdentity(object.value, 'package')) return {visibility: 'VISIBLE', value: object.value};
  return;
}

export interface McpAuthorizationResult {
  scope: 'USER' | 'WORKSPACE';
  authorization: {
    owner: McpCredentialOwner;
    config_identity: string;
    display_identity: string;
    package_install_id: PublicPackageId | null;
  };
}
export interface McpAuthorizationRequest {
  signal: AbortSignal;
  read(): Promise<McpAuthorizationResult | undefined>;
}

/** Read the accepted login fact, independent of the model-facing result projection. */
export function parseMcpAuthorizationResult(value: unknown): McpAuthorizationResult | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  const target = value as Record<string, unknown>;
  if (!target.owner || typeof target.owner !== 'object' || Array.isArray(target.owner)) return;
  const owner = target.owner as Record<string, unknown>;
  if ((target.scope !== 'USER' && target.scope !== 'WORKSPACE')
    || (owner.kind !== 'local' && owner.kind !== 'plugin')
    || typeof owner.scope_key !== 'string' || !owner.scope_key
    || typeof owner.server_id !== 'string' || !owner.server_id
    || (owner.kind === 'plugin' ? typeof owner.plugin_id !== 'string' || !owner.plugin_id : owner.plugin_id !== null)
    || typeof target.config_identity !== 'string' || !target.config_identity
    || typeof target.display_identity !== 'string' || !target.display_identity
    || (owner.kind === 'plugin' ? !isCanonicalIdentity(target.package_install_id, 'package') : target.package_install_id !== null)) return;
  return {
    scope: target.scope,
    authorization: {
      owner: {kind: owner.kind, scope_key: owner.scope_key, server_id: owner.server_id, plugin_id: owner.plugin_id as string | null},
      config_identity: target.config_identity,
      display_identity: target.display_identity,
      package_install_id: owner.kind === 'plugin' ? {visibility: 'VISIBLE', value: target.package_install_id as string} : null,
    },
  };
}
