import assert from 'node:assert/strict';
import { test } from 'node:test';
import { allowBrowserRequest, browserBounds, browserURL } from './browser-policy';

test('URL identity uses full WHATWG href and rejects credentials, guessing and privileged schemes', () => {
  assert.equal(browserURL(' https://EXAMPLE.com/Case?q=1#Part ', '49123'), 'https://example.com/Case?q=1#Part');
  for (const value of ['localhost:3000', '/tmp/file.html', 'file:///tmp/file.html', 'javascript:alert(1)', 'data:text/html,x', 'about:blank', 'https://user:pass@example.com/']) assert.throws(() => browserURL(value, '49123'));
  for (const value of ['http://localhost:3000/', 'http://127.0.0.1:3000/', 'http://[::1]:3000/']) assert.equal(browserURL(value, '49123'), value);
});
test('partition network policy protects control port across aliases and all request types', () => {
  for (const type of ['mainFrame', 'subFrame', 'xhr', 'webSocket', 'other']) {
    for (const host of ['localhost', '127.0.0.1', '[::1]', 'alias.example']) {
      for (const scheme of ['http', 'https', 'ws', 'wss']) assert.equal(allowBrowserRequest(`${scheme}://${host}:49123/control`, type, '49123', false), false);
    }
    assert.equal(allowBrowserRequest('file:///etc/hosts', type, '49123', false), false);
    assert.equal(allowBrowserRequest('https://ordinary.example/', type, '49123', true), false);
  }
  assert.equal(allowBrowserRequest('https://example.com/', 'xhr', '443', false), false);
  assert.equal(allowBrowserRequest('ws://localhost/socket', 'webSocket', '80', false), false);
  assert.equal(allowBrowserRequest('data:text/plain,fixture', 'image', '49123', false), true);
  assert.equal(allowBrowserRequest('blob:https://example.com/id', 'other', '49123', false), true);
  assert.equal(allowBrowserRequest('data:text/html,fixture', 'mainFrame', '49123', false), false);
  assert.equal(allowBrowserRequest('about:blank', 'mainFrame', '49123', false), false);
});
test('native bounds use host zoom only, retain offscreen animation geometry, and reject invalid native coordinates', () => {
  assert.deepEqual(browserBounds({x: 1000, y: 30, width: 336, height: 500}, 1.25), {x:1250,y:38,width:420,height:625});
  for (const bounds of [{x:NaN,y:0,width:1,height:1}, {x:0,y:0,width:0,height:1}, {x:Infinity,y:0,width:1,height:1}, {x:1e20,y:0,width:1,height:1}]) assert.throws(() => browserBounds(bounds, 1));
});
