import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { watchFile, unwatchFile, existsSync } from 'node:fs';
import { once } from 'node:events';
import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { PythonRuntime, type RuntimeEvent, type WslRuntimeOptions } from './runtime';
import type { HostOperations } from './host-operations';
import { posixHostOperations } from './host-posix';
import { desktopResourcePaths } from './resource-paths';

const settings = {runtime_dsn: 'test-runtime-socket', admin_dsn: 'test-admin-socket'};
async function fixture(mode = 'normal', host: HostOperations = posixHostOperations, wsl?: WslRuntimeOptions) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'pulsara-shell-test-'));
  const resources = path.join(root, 'resources');
  const home = path.join(root, 'home');
  const bin = path.join(resources, 'python', 'bin');
  await mkdir(bin, {recursive: true});
  await mkdir(home);
  const script = path.join(root, 'child.cjs');
  await writeFile(script, `
const fs = require('node:fs');
const path = require('node:path');
const home = process.env.PULSARA_HOME;
const mode = ${JSON.stringify(mode)};
fs.writeFileSync(path.join(home, 'spawned'), 'child imports started');
if (mode === 'early-exit') process.exit(3);
const output = value => process.stdout.write(JSON.stringify(value) + '\\n');
if (process.argv.includes('--wait-owner')) process.exit(0);
const lines = require('node:readline').createInterface({input:process.stdin});
lines.on('line', line => {
  const message = JSON.parse(line);
  fs.appendFileSync(path.join(home, 'calls.jsonl'), JSON.stringify(message) + '\\n');
  if (message.type === 'start') {
    if (mode === 'log') process.stderr.write('kernel diagnostic');
    if (mode === 'malformed') { process.stdout.write('not JSON\\n'); return; }
    if (mode === 'pre-ready-failure') { output({type:'failed', error:'startup cleanup failed'}); process.exit(0); }
    if (mode === 'pre-ready-exit') process.exit(3);
    if (mode === 'ready-on-shutdown-failure') return;
    output({type:'ready', origin:'http://127.0.0.1:12345', database_state:'ready', ...(mode.startsWith('wsl') ? {certificatePem:'-----BEGIN CERTIFICATE-----\\nfixture\\n-----END CERTIFICATE-----'} : {})});
    if (mode === 'wsl-oauth') output({type:'open-oauth-browser', requestId:'oauth-1', url:'https://example.com/authorize'});
    if (mode === 'wsl-bad-oauth') output({type:'open-oauth-browser', requestId:'oauth-1', url:'file:///etc/passwd'});
  } else if (message.type === 'open-oauth-browser-result') {
    fs.writeFileSync(path.join(home, 'oauth-result'), JSON.stringify(message));
  } else if (message.type === 'inspect-quit') {
    if (typeof message.requestId !== 'string') throw new Error('requestId must be a string');
    output({type:'quit-state', requestId:message.requestId, activeTasks:1, scheduledTasks:2});
  } else if (message.type === 'shutdown') {
    if (mode === 'ready-on-shutdown-failure') output({type:'ready', origin:'http://127.0.0.1:12345'});
    output({type:'stopping'});
    if (mode === 'failed-zero') output({type:'failed', error:'physical settlement failed'});
    if (mode === 'invalid-zero') process.stdout.write('invalid shutdown frame\\n');
    if (mode === 'wsl-no-stopped') process.exit(0);
    if (mode === 'wsl-partial-stopped') { process.stdout.write(JSON.stringify({type:'stopped'})); process.exit(0); }
    if (mode === 'wsl-invalid-utf8') { process.stdout.write(Buffer.from([0xc3,0x28,0x0a])); process.exit(0); }
    if (mode === 'drain') {
      const release = path.join(home, 'release');
      const finish = () => { if (fs.existsSync(release)) { output({type:'stopped'}); process.exit(0); } };
      fs.watch(home, finish);
      fs.writeFileSync(path.join(home, 'closing'), 'physical child still alive');
      finish();
    } else { output({type:'stopped'}); process.exit(['close-failure','ready-on-shutdown-failure'].includes(mode) ? 1 : 0); }
  }
});
`);
  const children: ChildProcessWithoutNullStreams[] = [];
  const fixtureHost: HostOperations = {
    ...host,
    spawn(_file, args, options) {
      const child = host.spawn(process.execPath, [script, ...args], options);
      children.push(child);
      return child;
    },
    execFile(_file, args, options) {
      return host.execFile(process.execPath, [script, ...args], options);
    },
  };
  const events: RuntimeEvent[] = [];
  const runtime = new PythonRuntime(desktopResourcePaths(resources), home, event => events.push(event), fixtureHost, wsl);
  return {root, home, runtime, events, children,
    async close() {
      await writeFile(path.join(home, 'release'), 'release');
      await runtime.stop().catch(() => {});
      await rm(root, {recursive:true, force:true});
    },
    async calls() {
      return (await readFile(path.join(home, 'calls.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    },
  };
}

function waitForFile(filename: string, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const cleanup = () => {
      unwatchFile(filename, changed);
      signal.removeEventListener('abort', aborted);
    };
    const changed = () => { if (existsSync(filename)) { cleanup(); resolve(); } };
    const aborted = () => { cleanup(); reject(signal.reason); };
    // Native directory notifications can miss fixture writes on macOS. Node
    // owns stat polling; the test's existing signal owns cancellation/cleanup.
    watchFile(filename, {interval:10}, changed);
    signal.addEventListener('abort', aborted, {once:true});
    changed();
  });
}

const wslOptions: WslRuntimeOptions = {distribution:'Ubuntu', user:'plumwsl', interopHelper:'/mnt/c/resources/pulsara-interop.exe', async openOAuthBrowser() {}};

test('cancelled fixture observation rejects and lets cleanup join the child', {timeout:10000}, async () => {
  const value = await fixture();
  const cancellation = new AbortController();
  const failure = new Error('fixture observation cancelled');
  try {
    await value.runtime.start(async () => settings, () => true);
    const waiting = waitForFile(path.join(value.home, 'never-created'), cancellation.signal);
    const rejected = assert.rejects(waiting, error => error === failure);
    cancellation.abort(failure);
    await rejected;
    await assert.rejects(waitForFile(path.join(value.home, 'never-created'), cancellation.signal), error => error === failure);
  } finally { await value.close(); }
  assert.equal(value.runtime.running, false);
  assert.equal(value.children[0].exitCode, 0);
  assert.equal(value.children[0].signalCode, null);
});

test('WSL startup sends the fixed host identity and requires the current certificate', {timeout:10000}, async () => {
  const value = await fixture('wsl-normal', posixHostOperations, wslOptions);
  try {
    await value.runtime.start(async () => settings, () => true);
    assert.match(value.runtime.certificatePem!, /BEGIN CERTIFICATE/);
    assert.deepEqual((await value.calls())[0].host, {platform:'win32', executionDomain:'wsl', distribution:'Ubuntu', user:'plumwsl', interopHelper:wslOptions.interopHelper});
    await value.runtime.stop();
  } finally { await value.close(); }
  const missing = await fixture('normal', posixHostOperations, wslOptions);
  try {
    await assert.rejects(missing.runtime.start(async () => settings, () => true), /本次连接证书/);
    await assert.rejects(missing.runtime.stop(), /收尾失败/);
    assert.equal(missing.runtime.running, false);
  } finally { await missing.close(); }
});

for (const mode of ['failed-zero', 'invalid-zero', 'wsl-no-stopped', 'wsl-partial-stopped', 'wsl-invalid-utf8']) {
  test(`shutdown rejects incomplete settlement even with exit zero: ${mode}`, {timeout:10000}, async () => {
    const value = await fixture(mode, posixHostOperations, mode.startsWith('wsl') ? wslOptions : undefined);
    try {
      await value.runtime.start(async () => settings, () => true);
      await assert.rejects(value.runtime.stop(), /收尾失败/);
      await assert.rejects(value.runtime.stop(), /收尾失败/);
      assert.equal(value.runtime.running, false);
    } finally { await value.close(); }
  });
}

for (const mode of ['pre-ready-failure', 'pre-ready-exit']) {
  test(`a submitted startup cannot be treated as an unused child: ${mode}`, {timeout:10000}, async () => {
    const value = await fixture(mode);
    try {
      await assert.rejects(value.runtime.start(async () => settings, () => false));
      await assert.rejects(value.runtime.stop(), /收尾失败/);
      assert.equal((await value.calls())[0].type, 'start');
    } finally { await value.close(); }
  });
}

for (const mode of ['wsl-oauth', 'wsl-bad-oauth']) {
  test(`OAuth bridge reports the real browser-open outcome: ${mode}`, {timeout:10000}, async t => {
    const opened: string[] = [];
    const value = await fixture(mode, posixHostOperations, {...wslOptions, async openOAuthBrowser(url) {opened.push(url);}});
    try {
      await value.runtime.start(async () => settings, () => true);
      await waitForFile(path.join(value.home, 'oauth-result'), t.signal);
      const result = JSON.parse(await readFile(path.join(value.home, 'oauth-result'), 'utf8'));
      assert.deepEqual(result, {type:'open-oauth-browser-result', requestId:'oauth-1', opened:mode === 'wsl-oauth'});
      assert.deepEqual(opened, mode === 'wsl-oauth' ? ['https://example.com/authorize'] : []);
      await value.runtime.stop();
    } finally { await value.close(); }
  });
}

test('host uses one child, private startup frame and correlated quit inspection', {timeout:10000}, async () => {
  const value = await fixture();
  try {
    await value.runtime.waitForPreviousOwner();
    const first = value.runtime.start(async () => settings, () => true);
    // Both calls enter before the filesystem preparation finishes.
    await assert.rejects(value.runtime.start(async () => settings, () => true), /不能启动第二个内核/);
    assert.equal(await first, 'http://127.0.0.1:12345');
    const status = await value.runtime.inspectQuit();
    assert.equal(status.activeTasks, 1);
    assert.equal(status.scheduledTasks, 2);
    await value.runtime.stop();
    assert.equal(value.runtime.running, false);
    const calls = await value.calls();
    assert.deepEqual(calls[0], {type:'start', postgres:settings, initialize:true});
    assert.equal(calls.filter(value => value.type === 'start').length, 1);
    assert.equal(typeof calls[1].requestId, 'string');
    assert.deepEqual(calls.at(-1), {type:'shutdown'});
  } finally { await value.close(); }
});

test('stop waits for the child to finish settlement and permits concurrent stop callers', {timeout:10000}, async t => {
  const value = await fixture('drain');
  try {
    await value.runtime.start(async () => settings, () => true);
    const closing = waitForFile(path.join(value.home, 'closing'), t.signal);
    let stopped = false;
    const first = value.runtime.stop().then(() => { stopped = true; });
    const second = value.runtime.stop();
    await closing;
    assert.equal(stopped, false);
    assert.equal(value.runtime.running, true);
    await writeFile(path.join(value.home, 'release'), 'settlement complete');
    await Promise.all([first, second]);
    assert.equal(value.runtime.running, false);
    assert.equal((await value.calls()).filter(value => value.type === 'shutdown').length, 1);
  } finally { await value.close(); }
});

test('malformed status closes the control pipe and explicit stop does not write after end', {timeout:10000}, async () => {
  const value = await fixture('malformed');
  try {
    await assert.rejects(value.runtime.start(async () => settings, () => true), /无效的宿主消息/);
    await assert.rejects(value.runtime.stop(), /收尾失败/);
    assert.equal(value.runtime.running, false);
    assert.equal((await value.calls()).filter(value => value.type === 'shutdown').length, 1);
  } finally { await value.close(); }
});

test('quit during asynchronous launch preparation still stops the child once spawned', {timeout:10000}, async () => {
  const value = await fixture();
  try {
    const started = value.runtime.start(async () => settings, () => true);
    const stopped = value.runtime.stop();
    await started;
    await stopped;
    assert.equal(value.runtime.running, false);
    assert.deepEqual((await value.calls()).map(value => value.type), ['start', 'shutdown']);
  } finally { await value.close(); }
});

test('a ready child reporting close failure rejects shutdown completion', {timeout:10000}, async () => {
  const value = await fixture('close-failure');
  try {
    await value.runtime.start(async () => settings, () => true);
    await assert.rejects(value.runtime.stop(), /收尾失败/);
    assert.equal(value.runtime.running, false);
  } finally { await value.close(); }
});

test('stop before any launch is a no-op for concurrent callers', async () => {
  const value = await fixture();
  try {
    await Promise.all([value.runtime.stop(), value.runtime.stop()]);
    assert.equal(value.runtime.running, false);
    assert.equal(value.children.length, 0);
  } finally { await value.close(); }
});

test('stop retains an already closed ready child nonzero result for every caller', {timeout:10000}, async () => {
  const value = await fixture('close-failure');
  try {
    await value.runtime.start(async () => settings, () => true);
    const child = value.children[0];
    const closed = once(child, 'close');
    child.stdin.end(JSON.stringify({type:'shutdown'}) + '\n');
    assert.deepEqual(await closed, [1, null]);
    assert.equal(value.runtime.running, false);
    const stops = await Promise.allSettled([value.runtime.stop(), value.runtime.stop()]);
    for (const result of stops) {
      assert.equal(result.status, 'rejected');
      if (result.status === 'rejected') assert.match(result.reason.message, /收尾失败 \(1\)/);
    }
    await assert.rejects(value.runtime.stop(), /收尾失败 \(1\)/);
    assert.equal((await value.calls()).filter(call => call.type === 'shutdown').length, 1);
  } finally { await value.close(); }
});

test('stop accepts an already closed ready child with exit zero', {timeout:10000}, async () => {
  const value = await fixture();
  try {
    await value.runtime.start(async () => settings, () => true);
    const child = value.children[0];
    const closed = once(child, 'close');
    child.stdin.end();
    assert.deepEqual(await closed, [0, null]);
    await Promise.all([value.runtime.stop(), value.runtime.stop()]);
    assert.equal(value.runtime.running, false);
    assert.deepEqual((await value.calls()).map(call => call.type), ['start']);
  } finally { await value.close(); }
});

test('a fresh launch cannot erase the failed exit awaited by an older stop', {timeout:10000}, async () => {
  const value = await fixture('normal', {
    ...posixHostOperations,
    async prepareRuntimeDirectory() {},
  });
  try {
    await value.runtime.start(async () => settings, () => true);
    const child = value.children[0];
    const closed = once(child, 'close');
    assert.equal(child.kill('SIGTERM'), true);
    assert.deepEqual(await closed, [null, 'SIGTERM']);
    assert.equal(value.runtime.running, false);
    const stopped = assert.rejects(value.runtime.stop(), /收尾失败 \(SIGTERM\)/);
    const restarted = value.runtime.start(async () => settings, () => false);
    await stopped;
    assert.equal(await restarted, 'http://127.0.0.1:12345');
    assert.equal(value.runtime.running, true);
    assert.equal(value.children.length, 2);
    await value.runtime.stop();
    await value.runtime.stop();
    assert.equal(value.runtime.running, false);
    assert.deepEqual((await value.calls()).map(call => call.type), ['start','start','shutdown']);
  } finally { await value.close(); }
});

test('a child reaching ready while stop is joining still reports its failed exit', {timeout:10000}, async () => {
  const value = await fixture('ready-on-shutdown-failure');
  try {
    const started = value.runtime.start(async () => settings, () => true);
    const stopped = assert.rejects(value.runtime.stop(), /收尾失败 \(1\)/);
    assert.equal(await started, 'http://127.0.0.1:12345');
    await stopped;
    assert.equal(value.runtime.running, false);
  } finally { await value.close(); }
});

test('child starts before PostgreSQL finishes, but start and inspection frames stay gated', {timeout:10000}, async t => {
  const value = await fixture();
  let release!: () => void;
  const database = new Promise<void>(resolve => { release = resolve; });
  const started = value.runtime.start(async () => { await database; return settings; }, () => true);
  try {
    await waitForFile(path.join(value.home, 'spawned'), t.signal);
    assert.equal(value.runtime.running, true);
    assert.equal(existsSync(path.join(value.home, 'calls.jsonl')), false);
    assert.deepEqual(await value.runtime.inspectQuit(), {type:'inspect-quit', activeTasks:0, scheduledTasks:0});
    let stopped = false;
    const stopping = value.runtime.stop().then(() => { stopped = true; });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(stopped, false);
    release();
    assert.equal(await started, 'http://127.0.0.1:12345');
    await stopping;
    assert.deepEqual((await value.calls()).map(call => call.type), ['start', 'shutdown']);
    assert.equal(value.runtime.running, false);
  } finally { release(); await value.close(); }
});

test('PostgreSQL failure sends no start frame and joins the unused child before rejection', {timeout:10000}, async t => {
  const value = await fixture();
  let release!: () => void;
  const database = new Promise<void>(resolve => { release = resolve; });
  const failure = new Error('database preparation failed');
  const started = value.runtime.start(async () => { await database; throw failure; }, () => true);
  const rejected = assert.rejects(started, error => error === failure);
  try {
    await waitForFile(path.join(value.home, 'spawned'), t.signal);
    release();
    await rejected;
    assert.equal(value.runtime.running, false);
    assert.equal(existsSync(path.join(value.home, 'calls.jsonl')), false);
    assert.equal(value.events.some(event => event.type === 'ready'), false);
  } finally { release(); await value.close(); }
});

test('an early child exit is observed while PostgreSQL preparation is still settling', {timeout:10000}, async t => {
  const value = await fixture('early-exit');
  let release!: () => void;
  const database = new Promise<void>(resolve => { release = resolve; });
  const started = value.runtime.start(async () => { await database; return settings; }, () => true);
  const rejected = assert.rejects(started, /内核在就绪前退出/);
  try {
    await waitForFile(path.join(value.home, 'spawned'), t.signal);
    // Observe physical pre-start exit before releasing PG. Sending into a live
    // child would instead be a submitted attempt with an unknown startup result.
    const child = value.children[0];
    if (child.exitCode === null && child.signalCode === null) await once(child, 'exit');
    assert.equal(value.runtime.running, true); // PG owner is still settling.
    const stopping = value.runtime.stop();
    release();
    await rejected;
    await stopping;
    assert.equal(value.runtime.running, false);
    assert.equal(existsSync(path.join(value.home, 'calls.jsonl')), false);
  } finally { release(); await value.close(); }
});

test('runtime directory failure rejects before spawning or preparing PostgreSQL', async () => {
  const failure = new Error('runtime directory unavailable');
  let spawned = false;
  let prepared = false;
  const value = await fixture('normal', {
    ...posixHostOperations,
    async prepareRuntimeDirectory() { throw failure; },
    spawn(...args) { spawned = true; return posixHostOperations.spawn(...args); },
  });
  try {
    await assert.rejects(value.runtime.start(async () => { prepared = true; return settings; }, () => true), error => error === failure);
    assert.equal(spawned, false);
    assert.equal(prepared, false);
    assert.equal(value.runtime.running, false);
    await value.runtime.stop();
  } finally { await value.close(); }
});

test('a log append failure remains a runtime event and does not replace shutdown ownership', {timeout:10000}, async () => {
  const failure = new Error('log append failed');
  let logged!: () => void;
  const attempted = new Promise<void>(resolve => { logged = resolve; });
  const value = await fixture('log', {
    ...posixHostOperations,
    async appendRuntimeLog() { logged(); throw failure; },
  });
  try {
    await value.runtime.start(async () => settings, () => true);
    await attempted;
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(value.events.some(event => event.type === 'failed' && event.error === `无法写入内核日志：${String(failure)}`));
    assert.equal(value.runtime.running, true);
    await value.runtime.stop();
    assert.equal(value.runtime.running, false);
    assert.equal((await value.calls()).filter(value => value.type === 'shutdown').length, 1);
  } finally { await value.close(); }
});
