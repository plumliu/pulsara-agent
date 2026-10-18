import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { watch, existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { PythonRuntime, type RuntimeEvent } from './runtime';

const settings = {runtime_dsn: 'test-runtime-socket', admin_dsn: 'test-admin-socket'};
const shellQuote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";

async function fixture(mode = 'normal') {
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
const output = value => process.stdout.write(JSON.stringify(value) + '\\n');
if (process.argv.includes('--wait-owner')) process.exit(0);
const lines = require('node:readline').createInterface({input:process.stdin});
lines.on('line', line => {
  const message = JSON.parse(line);
  fs.appendFileSync(path.join(home, 'calls.jsonl'), JSON.stringify(message) + '\\n');
  if (message.type === 'start') {
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
  const runtime = new PythonRuntime(resources, home, event => events.push(event));
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
    const first = value.runtime.start(settings, true);
    // Both calls enter before the filesystem preparation finishes.
    await assert.rejects(value.runtime.start(settings, true), /不能启动第二个内核/);
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
    await value.runtime.start(settings, true);
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
    await assert.rejects(value.runtime.start(settings, true), /无效的宿主消息/);
    await value.runtime.stop();
    assert.equal(value.runtime.running, false);
    assert.equal((await value.calls()).filter(value => value.type === 'shutdown').length, 1);
  } finally { await value.close(); }
});

test('quit during asynchronous launch preparation still stops the child once spawned', {timeout:10000}, async () => {
  const value = await fixture();
  try {
    const started = value.runtime.start(settings, true);
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
    await value.runtime.start(settings, true);
    await assert.rejects(value.runtime.stop(), /收尾失败/);
    assert.equal(value.runtime.running, false);
  } finally { await value.close(); }
});
