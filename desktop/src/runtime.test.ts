import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { watch, existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { PythonRuntime, type RuntimeEvent } from './runtime';
import type { HostOperations } from './host-operations';
import { posixHostOperations } from './host-posix';
import { desktopResourcePaths } from './resource-paths';

const settings = {runtime_dsn: 'test-runtime-socket', admin_dsn: 'test-admin-socket'};
const shellQuote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";

async function fixture(mode = 'normal', host: HostOperations = posixHostOperations) {
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
    output({type:'ready', origin:'http://127.0.0.1:12345', database_state:'ready'});
  } else if (message.type === 'inspect-quit') {
    if (typeof message.requestId !== 'string') throw new Error('requestId must be a string');
    output({type:'quit-state', requestId:message.requestId, activeTasks:1, scheduledTasks:2});
  } else if (message.type === 'shutdown') {
    output({type:'stopping'});
    if (mode === 'drain') {
      const release = path.join(home, 'release');
      const finish = () => { if (fs.existsSync(release)) { output({type:'stopped'}); process.exit(0); } };
      fs.watch(home, finish);
      fs.writeFileSync(path.join(home, 'closing'), 'physical child still alive');
      finish();
    } else { output({type:'stopped'}); process.exit(mode === 'close-failure' ? 1 : 0); }
  }
});
`);
  await writeFile(path.join(bin, 'python3'), `#!/bin/sh\nexec ${shellQuote(process.execPath)} ${shellQuote(script)} "$@"\n`, {mode:0o755});
  const events: RuntimeEvent[] = [];
  const runtime = new PythonRuntime(desktopResourcePaths(resources), home, event => events.push(event), host);
  return {root, home, runtime, events,
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

function waitForFile(filename: string): Promise<void> {
  return new Promise(resolve => {
    const changed = () => { if (existsSync(filename)) { watcher.close(); resolve(); } };
    const watcher = watch(path.dirname(filename), changed);
    changed();
  });
}

test('host uses one child, private startup frame and correlated quit inspection', {timeout:10000}, async () => {
  const value = await fixture();
  try {
    await value.runtime.waitForPreviousOwner();
    const first = value.runtime.start(async () => settings, true);
    // Both calls enter before the filesystem preparation finishes.
    await assert.rejects(value.runtime.start(async () => settings, true), /不能启动第二个内核/);
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

test('stop waits for the child to finish settlement and permits concurrent stop callers', {timeout:10000}, async () => {
  const value = await fixture('drain');
  try {
    await value.runtime.start(async () => settings, true);
    const closing = waitForFile(path.join(value.home, 'closing'));
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
    await assert.rejects(value.runtime.start(async () => settings, true), /无效的宿主消息/);
    await value.runtime.stop();
    assert.equal(value.runtime.running, false);
    assert.equal((await value.calls()).filter(value => value.type === 'shutdown').length, 1);
  } finally { await value.close(); }
});

test('quit during asynchronous launch preparation still stops the child once spawned', {timeout:10000}, async () => {
  const value = await fixture();
  try {
    const started = value.runtime.start(async () => settings, true);
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
    await value.runtime.start(async () => settings, true);
    await assert.rejects(value.runtime.stop(), /收尾失败/);
    assert.equal(value.runtime.running, false);
  } finally { await value.close(); }
});

test('child starts before PostgreSQL finishes, but start and inspection frames stay gated', {timeout:10000}, async () => {
  const value = await fixture();
  let release!: () => void;
  const database = new Promise<void>(resolve => { release = resolve; });
  const started = value.runtime.start(async () => { await database; return settings; }, true);
  try {
    await waitForFile(path.join(value.home, 'spawned'));
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

test('PostgreSQL failure sends no start frame and joins the unused child before rejection', {timeout:10000}, async () => {
  const value = await fixture();
  let release!: () => void;
  const database = new Promise<void>(resolve => { release = resolve; });
  const failure = new Error('database preparation failed');
  const started = value.runtime.start(async () => { await database; throw failure; }, true);
  const rejected = assert.rejects(started, error => error === failure);
  try {
    await waitForFile(path.join(value.home, 'spawned'));
    release();
    await rejected;
    assert.equal(value.runtime.running, false);
    assert.equal(existsSync(path.join(value.home, 'calls.jsonl')), false);
    assert.equal(value.events.some(event => event.type === 'ready'), false);
  } finally { release(); await value.close(); }
});

test('an early child exit is observed while PostgreSQL preparation is still settling', {timeout:10000}, async () => {
  const value = await fixture('early-exit');
  let release!: () => void;
  const database = new Promise<void>(resolve => { release = resolve; });
  const started = value.runtime.start(async () => { await database; return settings; }, true);
  const rejected = assert.rejects(started, /内核在就绪前退出/);
  try {
    await waitForFile(path.join(value.home, 'spawned'));
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
    await assert.rejects(value.runtime.start(async () => { prepared = true; return settings; }, true), error => error === failure);
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
    await value.runtime.start(async () => settings, true);
    await attempted;
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(value.events.some(event => event.type === 'failed' && event.error === `无法写入内核日志：${String(failure)}`));
    assert.equal(value.runtime.running, true);
    await value.runtime.stop();
    assert.equal(value.runtime.running, false);
    assert.equal((await value.calls()).filter(value => value.type === 'shutdown').length, 1);
  } finally { await value.close(); }
});
