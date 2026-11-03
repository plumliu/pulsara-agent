/** Shared dependency-free identity core for renderer, Electron and Code Mode. */
export const identityKinds = /** @type {const} */ (['workspace', 'session', 'blob', 'image', 'command', 'queue', 'turn', 'entry', 'snapshot', 'binding', 'history', 'block', 'replay', 'attempt', 'result', 'call', 'batch', 'task', 'agent-result', 'agent-message', 'decision', 'plan', 'plan-interaction', 'permission', 'schedule', 'memory', 'relation', 'event', 'connection', 'package', 'process', 'cell', 'stream', 'subscription', 'terminal-decision', 'observation', 'todo-run', 'todo-settlement', 'borrow', 'interaction', 'host', 'launch', 'attachment', 'transport', 'request', 'operation', 'compact-attempt', 'epoch', 'permit', 'planning', 'bootstrap', 'preparation', 'replay-reservation', 'extension', 'observer', 'generation', 'terminal-owner', 'mcp-candidate', 'mcpref', 'slot', 'settlement', 'fence', 'context', 'segment', 'page', 'tab', 'browser-instance', 'presentation', 'mcp-tool', 'reasoning-block', 'search-document', 'draft-node', 'draft-import', 'draft-asset', 'skill', 'mcp-server', 'view']);
/** @typedef {(typeof identityKinds)[number]} IdentityKind */
const knownKinds = new Set(identityKinds);
const alphabet = 'abcdefghijklmnopqrstuvwxyz234567';

/** @param {string} kind @returns {asserts kind is IdentityKind} */
export function checkIdentityKind(kind) {
  if (!knownKinds.has(/** @type {IdentityKind} */ (kind))) throw new Error('INVALID_ID: unknown identity kind');
}

/** @param {Uint8Array} bytes */
export function encodeIdentityPayload(bytes) {
  let buffer = 0, bits = 0, payload = '';
  for (const byte of bytes) {
    buffer = (buffer << 8) | byte;
    bits += 8;
    while (bits >= 5) { bits -= 5; payload += alphabet[(buffer >>> bits) & 31]; }
  }
  if (bits) payload += alphabet[(buffer << (5 - bits)) & 31];
  return payload;
}

/** @param {IdentityKind} kind */
export function randomIdentity(kind) {
  checkIdentityKind(kind);
  return `${kind}:${encodeIdentityPayload(globalThis.crypto.getRandomValues(new Uint8Array(16)))}`;
}

/** @param {unknown} value @param {IdentityKind} kind @returns {value is string} */
export function isCanonicalIdentity(value, kind) {
  checkIdentityKind(kind);
  return typeof value === 'string' && value.startsWith(`${kind}:`)
    && value.length === kind.length + 27
    && /^[a-z2-7]{25}[aeimquy4]$/.test(value.slice(kind.length + 1));
}
