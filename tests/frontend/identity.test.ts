import { afterEach, describe, expect, it, vi } from 'vitest';
import { deriveIdentity, isCanonicalIdentity, randomIdentity } from '../../frontend/lib/identity';

afterEach(() => vi.restoreAllMocks());
describe('caller identity encoding', () => {
  it('preserves the complete 128-bit vector without noncanonical padding', () => {
    vi.spyOn(crypto, 'getRandomValues').mockImplementation((array) => {
      const bytes = array as Uint8Array;
      expect(bytes.length).toBe(16);
      bytes.set(Array.from({ length: 16 }, (_, i) => i));
      return array;
    });
    expect(randomIdentity('command')).toBe('command:aaaqeayeaudaocajbifqydiob4');
  });
  it('matches Python Unicode and framed derivation vectors', () => {
    expect(deriveIdentity('entry', 'fixture', '中文🙂', 'a:b/c', '', 'e\u0301')).toBe('entry:hbumehpzj53h3gbt572hhjtpbq');
    expect(deriveIdentity('task', 'spawn', 'attempt:' + 'a'.repeat(26), '0')).toBe('task:htl27gtxmwe2htij6arwsvscbq');
    expect(() => deriveIdentity('entry', 'fixture', '\ud800')).toThrow();
    expect(() => isCanonicalIdentity('unknown:' + 'a'.repeat(26), 'unknown' as never)).toThrow();
  });
  it('rejects wrong kinds, truncation, old UUIDs and unused nonzero padding bits', () => {
    expect(isCanonicalIdentity('command:' + 'a'.repeat(26), 'command')).toBe(true);
    for (const value of ['command:abc', 'command:' + 'a'.repeat(26) + '\n', 'COMMAND:' + 'a'.repeat(26), 'command:' + 'a'.repeat(25) + 'b', 'request:' + 'a'.repeat(26), '00000000-0000-4000-8000-000000000001']) {
      expect(isCanonicalIdentity(value, 'command')).toBe(false);
    }
  });
});

it('rejects removed artifact identity allocation', () => { expect(() => randomIdentity('artifact' as never)).toThrow(); });
