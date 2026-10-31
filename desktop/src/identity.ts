/** Canonical identities; allocation, scope and authority stay with callers. */
import { sha256 } from '@noble/hashes/sha256';
import { identityKinds, checkIdentityKind, encodeIdentityPayload } from './identity-core.mjs';
export { identityKinds, randomIdentity, isCanonicalIdentity } from './identity-core.mjs';
export type IdentityKind = typeof identityKinds[number];

export function deriveIdentity(kind: IdentityKind, role: string, ...parts: string[]): string {
  checkIdentityKind(kind);
  if (!role || typeof role !== 'string' || parts.some(part => typeof part !== 'string')) throw new Error('identity derivation requires a role and string parts');
  const values = ['pulsara-id-v1', kind, role, ...parts];
  // Python UTF-8 encoding rejects unpaired surrogates; preserve the same contract.
  if (values.some(value => /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value))) throw new Error('identity derivation requires valid Unicode');
  const bytes = new TextEncoder().encode(JSON.stringify(values));
  return `${kind}:${encodeIdentityPayload(sha256(bytes).slice(0, 16))}`;
}
