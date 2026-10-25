import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import { rootCertificates } from 'node:tls';
import type { Session } from 'electron';
import { createBackendSession } from './backend-session';

function fixture(fetch?: Session['fetch']) {
  const calls: string[] = [];
  const sessions: string[] = [];
  let verify!: (request: {hostname:string;certificate:{data:string}}, done:(code:number)=>void) => void;
  let before!: (request: {url:string;frame?:{url:string}},done:(result:{cancel:boolean})=>void) => void;
  const native = Object.assign(new EventEmitter(),{
    setCertificateVerifyProc(fn:typeof verify) { verify=fn; },
    webRequest:{onBeforeRequest(fn:typeof before) { before=fn; }},
    setPermissionRequestHandler() {},setPermissionCheckHandler() {},
    fetch:fetch ?? (async () => new Response(null,{status:200})),
    async closeAllConnections() { calls.push('connections'); },async clearStorageData() { calls.push('storage'); },
  });
  const owner = createBackendSession({origin:'https://127.0.0.1:12345',certificatePem:rootCertificates[0]}, {fromPartition(partition) { sessions.push(partition); return native as unknown as Session; }});
  const allowed = (url:string,frame?:string) => { let result!: boolean; before({url,...(frame?{frame:{url:frame}}:{})},value=>{result=!value.cancel;}); return result; };
  const verified = (hostname:string,certificate=rootCertificates[0]) => { let code!: number; verify({hostname,certificate:{data:certificate}},value=>{code=value;});return code===0; };
  return {owner,native,calls,sessions,allowed,verified};
}

test('one immutable full certificate and exact origin restrict every backend network request', () => {
  const value = fixture();
  assert.ok(value.sessions[0].startsWith('pulsara-workbench-'));
  assert.equal(value.verified('127.0.0.1'),true);
  assert.equal(value.verified('localhost'),false);
  assert.equal(value.verified('127.0.0.1',rootCertificates[1]),false);
  assert.equal(value.allowed('https://127.0.0.1:12345/path'),true);
  for (const url of ['http://127.0.0.1:12345/path','https://localhost:12345/path','https://127.0.0.1:12346/path','file:///home/user/file']) assert.equal(value.allowed(url),false);
  const pdf='chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.html';
  assert.equal(value.allowed(pdf),true);
  assert.equal(value.allowed('chrome://resources/pdf.js',pdf),true);
  assert.equal(value.allowed('chrome://resources/pdf.js','https://127.0.0.1:12345/'),false);
  assert.equal(value.allowed('chrome-extension://other/index.html'),false);
  value.owner.seal();
  assert.equal(value.allowed('https://127.0.0.1:12345/path'),false);
  assert.equal(value.verified('127.0.0.1'),false);
});

test('retirement seals admission and joins deferred native cancellation plus original owners', async () => {
  const value = fixture();
  const download = Object.assign(new EventEmitter(),{cancel(){value.calls.push('download-cancel');download.emit('done',{},'cancelled');}});
  value.native.emit('will-download',{},download);
  const retired = value.owner.retire(async()=>{value.calls.push('owners');assert.equal(value.allowed('https://127.0.0.1:12345/new'),false);});
  assert.equal(value.calls.includes('download-cancel'),false);
  await retired;
  assert.deepEqual(value.calls,['owners','connections','download-cancel','storage']);
  await value.owner.retire(async()=>{assert.fail('retirement owners run only once');});
});

test('an owner settlement failure stays a retirement failure after network cleanup', async () => {
  const value=fixture(), failure=new Error('old owner has not settled');
  await assert.rejects(value.owner.retire(async()=>{throw failure;}),error=>error===failure);
  assert.deepEqual(value.calls,['connections','storage']);
  assert.equal(value.allowed('https://127.0.0.1:12345/new'),false);
});

test('Windows readiness samples only HEAD on the original pinned session and shares one task', async () => {
  const requests: {url:unknown;init:RequestInit|undefined}[] = [];
  const value=fixture(async(url,init)=>{
    requests.push({url,init});
    if (requests.length===1) throw new Error('net::ERR_CONNECTION_REFUSED');
    return new Response(null,{status:200});
  });
  const pending=value.owner.waitUntilReachable();
  assert.equal(value.owner.waitUntilReachable(),pending);
  await pending;
  assert.equal(requests.length,2);
  for (const request of requests) {
    assert.equal(request.url,'https://127.0.0.1:12345/');
    assert.equal(request.init?.method,'HEAD');assert.equal(request.init?.redirect,'error');
    assert.ok(request.init?.signal);assert.equal(request.init?.body,undefined);
  }
  await value.owner.retire(async()=>{});
  await assert.rejects(value.owner.waitUntilReachable(),{name:'AbortError'});
});

test('readiness preserves authentication, TLS and other transport failures without retry', async () => {
  for (const failure of [new Error('net::ERR_CERT_AUTHORITY_INVALID'),new Error('prefix net::ERR_CONNECTION_REFUSED'),new Error('fetch failed',{cause:{code:'ECONNREFUSED'}})]) {
    let calls=0;
    const value=fixture(async()=>{calls++;throw failure;});
    await assert.rejects(value.owner.waitUntilReachable(),error=>error===failure);
    assert.equal(calls,1);await value.owner.retire(async()=>{});
  }
  let calls=0;
  const value=fixture(async()=>{calls++;return new Response(null,{status:401});});
  await assert.rejects(value.owner.waitUntilReachable(),/HTTP 401/);
  assert.equal(calls,1);await value.owner.retire(async()=>{});
});

test('external cancellation aborts the forwarding sampling wait with its original reason', async () => {
  let calls=0;
  const value=fixture(async()=>{calls++;throw new Error('net::ERR_CONNECTION_REFUSED');});
  const controller=new AbortController(),reason=new Error('startup cancelled');
  const pending=value.owner.waitUntilReachable(controller.signal);
  await new Promise<void>(resolve=>setImmediate(resolve));
  controller.abort(reason);
  await assert.rejects(pending,error=>error===reason);
  assert.equal(calls,1);await value.owner.retire(async()=>{});
});

test('retirement aborts and joins a native readiness request before clearing the session', async () => {
  let abortObserved!:()=>void,finishRequest!:()=>void;
  const observed=new Promise<void>(resolve=>{abortObserved=resolve;});
  const finish=new Promise<void>(resolve=>{finishRequest=resolve;});
  const value=fixture(async(_url,init)=>{
    await new Promise<void>(resolve=>init!.signal!.addEventListener('abort',()=>{abortObserved();resolve();},{once:true}));
    await finish;
    throw init!.signal!.reason;
  });
  const pending=value.owner.waitUntilReachable();
  const rejected=assert.rejects(pending,{name:'AbortError'});
  let retired=false;
  const retirement=value.owner.retire(async()=>{value.calls.push('owners');}).then(()=>{retired=true;});
  await observed;
  assert.equal(retired,false);assert.deepEqual(value.calls,['owners']);
  finishRequest();await rejected;await retirement;
  assert.deepEqual(value.calls,['owners','connections','storage']);
});

test('retirement joins a cancelled sampling wait and caches the same cleanup during owner reentry', async () => {
  let calls=0;
  const value=fixture(async()=>{calls++;throw new Error('net::ERR_CONNECTION_REFUSED');});
  const pending=value.owner.waitUntilReachable();
  const rejected=assert.rejects(pending,{name:'AbortError'});
  await new Promise<void>(resolve=>setImmediate(resolve));
  let reentered:Promise<void>|undefined;
  const retirement=value.owner.retire(async()=>{reentered=value.owner.retire(async()=>{assert.fail('cleanup must not run twice');});});
  await retirement;await rejected;
  assert.equal(reentered,retirement);assert.equal(calls,1);
  assert.deepEqual(value.calls,['connections','storage']);
});
