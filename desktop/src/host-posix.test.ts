import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtemp, readFile, realpath, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { posixHostOperations } from './host-posix';

test('home resolution remains synchronous and preserves resolver launch failures', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'pulsara-host-home-'));
  try {
    const python = path.join(root, 'private python');
    await writeFile(python, '#!/bin/sh\nprintf "  /tmp/中文 home  \\n"\n', { mode: 0o755 });
    assert.equal(posixHostOperations.resolveHome(python), '/tmp/中文 home');
    assert.throws(() => posixHostOperations.resolveHome(path.join(root, 'missing')), { code: 'ENOENT' });
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('finite execution preserves literal argv, explicit environment, output and nonzero status', async () => {
  const args = ['中文 space', "'quoted'", '$(not-a-shell)', ''];
  const script = 'process.stdout.write(JSON.stringify({args:process.argv.slice(1),value:process.env.PULSARA_HOST_TEST}));process.stderr.write("diagnostic");';
  const output = await posixHostOperations.execFile(process.execPath, ['-e', script, ...args], { env: { PULSARA_HOST_TEST: '值' } });
  assert.deepEqual(JSON.parse(output.stdout), { args, value: '值' });
  assert.equal(output.stderr, 'diagnostic');
  await assert.rejects(
    posixHostOperations.execFile(process.execPath, ['-e', 'process.stdout.write("partial");process.stderr.write("failure");process.exit(7);'], { env: process.env }),
    (error: Error & { code?: number; stdout?: string; stderr?: string }) => {
      assert.equal(error.code, 7);
      assert.equal(error.stdout, 'partial');
      assert.equal(error.stderr, 'failure');
      return true;
    },
  );
});

test('stream execution keeps three byte pipes and the supplied working directory', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'pulsara-host-stream-'));
  try {
    const child = posixHostOperations.spawn(process.execPath, ['-e', 'process.stderr.write(process.cwd());process.stdin.pipe(process.stdout);'], { env: process.env, cwd: root });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout.on('data', (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on('data', (chunk: Buffer) => stderr.push(chunk));
    const closed = once(child, 'close');
    const bytes = Buffer.from([0, 0xff, 13, 10, 0xe4, 0xb8, 0xad]);
    child.stdin.end(bytes);
    assert.deepEqual(await closed, [0, null]);
    assert.deepEqual(Buffer.concat(stdout), bytes);
    assert.equal(Buffer.concat(stderr).toString(), await realpath(root));
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('runtime storage creates private files, appends exact bytes and retains filesystem failures', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'pulsara-host-log-'));
  try {
    await posixHostOperations.prepareRuntimeDirectory(root);
    await posixHostOperations.appendRuntimeLog(root, Buffer.from([0, 0xff, 13, 10]));
    await posixHostOperations.appendRuntimeLog(root, Buffer.from('中文'));
    const directory = path.join(root, 'desktop');
    const log = path.join(directory, 'kernel.log');
    assert.equal((await stat(directory)).mode & 0o777, 0o700);
    assert.equal((await stat(log)).mode & 0o777, 0o600);
    assert.deepEqual(await readFile(log), Buffer.concat([Buffer.from([0, 0xff, 13, 10]), Buffer.from('中文')]));
    const file = path.join(root, 'not-a-home');
    await writeFile(file, 'occupied');
    await assert.rejects(posixHostOperations.prepareRuntimeDirectory(file), { code: 'ENOTDIR' });
    await assert.rejects(posixHostOperations.appendRuntimeLog(file, Buffer.from('log')), { code: 'ENOTDIR' });
  } finally { await rm(root, { recursive: true, force: true }); }
});
