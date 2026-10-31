import assert from 'node:assert/strict';
import { test } from 'node:test';
import { deriveIdentity, isCanonicalIdentity, randomIdentity } from './identity';

test('shared Electron identity uses the Python canonical vectors', () => {
  assert.equal(deriveIdentity('entry', 'fixture', '中文🙂', 'a:b/c', '', 'e\u0301'), 'entry:hbumehpzj53h3gbt572hhjtpbq');
  assert.equal(deriveIdentity('task', 'spawn', 'attempt:' + 'a'.repeat(26), '0'), 'task:htl27gtxmwe2htij6arwsvscbq');
  assert.ok(isCanonicalIdentity(randomIdentity('browser-instance'), 'browser-instance'));
  for (const value of ['entry:' + 'a'.repeat(25) + 'b', 'entry:' + 'a'.repeat(26) + '\n', 'ENTRY:' + 'a'.repeat(26), 'entry:abc']) {
    assert.equal(isCanonicalIdentity(value, 'entry'), false);
  }
  assert.throws(() => deriveIdentity('entry', 'fixture', '\ud800'));
});
