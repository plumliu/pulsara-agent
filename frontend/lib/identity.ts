/** Pulsara-owned caller identities. Scope and authority belong to their owners. */
const alphabet = 'abcdefghijklmnopqrstuvwxyz234567';
export type CallerIdentityKind = 'command' | 'request' | 'transport';

export function randomIdentity(kind: CallerIdentityKind): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let buffer = 0;
  let bits = 0;
  let payload = '';
  for (const byte of bytes) {
    buffer = (buffer << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      bits -= 5;
      payload += alphabet[(buffer >>> bits) & 31];
    }
  }
  if (bits) payload += alphabet[(buffer << (5 - bits)) & 31];
  return `${kind}:${payload}`;
}

export function isCanonicalIdentity(value: unknown, kind: string): value is string {
  return typeof value === 'string'
    && value.startsWith(`${kind}:`)
    && /^[a-z2-7]{25}[aeimquy4]$/.test(value.slice(kind.length + 1));
}
