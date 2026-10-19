import test from 'node:test';
import assert from 'node:assert/strict';
import { isOAuthReturn } from './oauth-return';

test('OAuth return link is an exact focus-only entry point', () => {
  assert.equal(isOAuthReturn('pulsara://oauth/return'), true);
  for (const url of ['https://oauth/return', 'pulsara://oauth/return?code=secret', 'pulsara://oauth/return#authorized', 'pulsara://other/return']) {
    assert.equal(isOAuthReturn(url), false);
  }
});
