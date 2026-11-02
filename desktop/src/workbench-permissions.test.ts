import assert from 'node:assert/strict';
import test from 'node:test';
import type { WebContents, Session } from 'electron';
import { installWorkbenchPermissions } from './workbench-permissions';

test('only the current workbench main frame may request user-triggered fullscreen', () => {
  let request!: NonNullable<Parameters<Session['setPermissionRequestHandler']>[0]>;
  let check!: NonNullable<Parameters<Session['setPermissionCheckHandler']>[0]>;
  const contents = {session:{
    setPermissionRequestHandler(handler: typeof request) {request=handler;},
    setPermissionCheckHandler(handler: typeof check) {check=handler;},
  }} as unknown as WebContents;
  let origin: string | undefined = 'http://127.0.0.1:55169';
  installWorkbenchPermissions(contents, () => origin);
  const valid = {isMainFrame:true, requestingUrl:origin+'/'};
  assert.equal(check(contents,'fullscreen',origin,valid),true);
  let granted: boolean | undefined;
  request(contents,'fullscreen',value=>{granted=value;},valid);
  assert.equal(granted,true);
  for (const details of [{...valid,isMainFrame:false},{...valid,requestingUrl:'about:srcdoc'},{...valid,requestingUrl:'https://example.com/'},{...valid,requestingUrl:'http://127.0.0.1:55170/'},{isMainFrame:true}]) {
    assert.equal(check(contents,'fullscreen',origin,details),false);
  }
  assert.equal(check(null,'fullscreen',origin,valid),false);
  assert.equal(check({} as WebContents,'fullscreen',origin,valid),false);
  for (const permission of ['automatic-fullscreen','media','clipboard-read','display-capture'] as const) assert.equal(check(contents,permission,origin,valid),false);
  origin=undefined;
  assert.equal(check(contents,'fullscreen','http://127.0.0.1:55169',valid),false);
});
