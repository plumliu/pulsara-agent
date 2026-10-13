import { afterEach, describe, expect, it, vi } from 'vitest';
import { isCanonicalIdentity, randomIdentity } from '../../frontend/lib/identity';

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
  it('rejects wrong kinds, truncation, old UUIDs and unused nonzero padding bits', () => {
    expect(isCanonicalIdentity('command:' + 'a'.repeat(26), 'command')).toBe(true);
    for (const value of ['command:abc', 'command:' + 'a'.repeat(25) + 'b', 'request:' + 'a'.repeat(26), '00000000-0000-4000-8000-000000000001']) {
      expect(isCanonicalIdentity(value, 'command')).toBe(false);
    }
  });
});
