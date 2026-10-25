import {test} from 'node:test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, rm, readFile, symlink, stat, realpath} from 'node:fs/promises';
import path from 'node:path';
import {BundledPostgres} from './postgres';
import {createPosixPostgresOperations} from './postgres-posix';
import type {PostgresOperations} from './postgres-operations';

function observedOperations() {
  const calls: string[] = [];
  const directory = '/home/test/desktop/postgres';
  const socket = '/home/test/desktop/socket';
  const operations: PostgresOperations = {
    directory, socket, logPath: '/home/test/desktop/postgres.log', socketAddressBytes: 64,
    async prepareLayout() { calls.push('prepare'); },
    async readVersion() { calls.push('version'); return '17\n'; },
    async readInstance() { calls.push('instance'); return {directory, socket}; },
    async socketMode() { calls.push('socket-mode'); return 0o700; },
    async resetPathsMatch() { calls.push('reset-paths'); return true; },
    async dataDirectoryInfo() { calls.push('data-info'); return {uid: 42, currentUserUid: 42, isDirectory: true}; },
    async run(binary, args) { calls.push(`${binary}:${args[0]}`); return {stdout: '', stderr: ''}; },
    async removeDataDirectory() { calls.push('remove'); },
  };
  return {operations, calls};
}

test('initialization permission belongs only to the current confirmed initdb attempt', async () => {
  const {operations} = observedOperations();
  let created = false;
  operations.readVersion = async () => { if (!created) throw Object.assign(new Error('absent'), {code:'ENOENT'}); return '17'; };
  operations.run = async binary => { if (binary === 'initdb') created = true; return {stdout:'',stderr:''}; };
  const pg = new BundledPostgres(operations);
  await pg.start();
  assert.equal(pg.initialized, true);
  await pg.start();
  assert.equal(pg.initialized, false);
  operations.prepareLayout = async () => { throw new Error('layout unavailable'); };
  await assert.rejects(pg.start(), /layout unavailable/);
  assert.equal(pg.initialized, false);
});

test('failed readiness keeps a verified started server owned for explicit shutdown', async () => {
  // Keep the fixture inside PostgreSQL's Unix-socket pathname limit on macOS.
  const root = await mkdtemp('/tmp/pg-owner-');
  const pg = new BundledPostgres(createPosixPostgresOperations(path.join(root, 'postgres/bin'), root));
  try {
    await mkdir(path.join(root, 'postgres/bin'), {recursive:true});
    await mkdir(pg.directory, {recursive:true});
    await writeFile(path.join(pg.directory, 'PG_VERSION'), '17\n');
    await writeFile(path.join(root, 'postgres/bin/pg_ctl'), `#!/bin/sh
action="$1"; shift
while [ "$#" -gt 0 ]; do
  if [ "$1" = -D ]; then shift; data="$1"; fi
  shift
done
case "$action" in
  status) test -f "$data/postmaster.pid" || exit 3 ;;
  start) printf '123\\n%s\\n0\\n5432\\n%s\\n' "$data" "$(dirname "$data")/socket" > "$data/postmaster.pid"; exit 1 ;;
  stop) printf stopped > "$data/stop-result" ;;
esac
`, {mode:0o755});
    await assert.rejects(pg.start());
    assert.equal(pg.startedHere, true);
    await pg.stop();
    assert.equal(await readFile(path.join(pg.directory,'stop-result'),'utf8'), 'stopped');
  } finally { await rm(root, {recursive:true,force:true}); }
});

test('start accepts the resolved instance directory while reset requires its exact saved spelling', async () => {
  const {operations, calls} = observedOperations();
  operations.readInstance = async directoryForm => {
    calls.push('instance');
    return {directory: directoryForm === 'resolved' ? operations.directory : operations.directory + '/.', socket: operations.socket};
  };
  const pg = new BundledPostgres(operations);
  await pg.start();
  assert.equal(pg.initialized, false);
  assert.equal(pg.startedHere, false);
  assert.deepEqual(calls, ['prepare', 'version', 'pg_ctl:status', 'instance']);
  calls.length = 0;
  await assert.rejects(pg.resetConfirmed(), /运行实例与专用目录不匹配/);
  assert.deepEqual(calls, ['reset-paths', 'data-info', 'version', 'pg_ctl:status', 'instance']);
});

test('only a settled numeric status code 3 permits a start and failed readiness preserves the original error', async () => {
  const {operations, calls} = observedOperations();
  const unavailable = Object.assign(new Error('launch failed'), {code: 'ENOENT'});
  operations.run = async () => { throw unavailable; };
  const pg = new BundledPostgres(operations);
  await assert.rejects(pg.start(), error => error === unavailable);
  assert.equal(pg.startedHere, false);
  assert.deepEqual(calls, ['prepare', 'version']);

  calls.length = 0;
  const readiness = Object.assign(new Error('readiness failed'), {code: 1, postgresExecution: 'failed'});
  let statuses = 0;
  operations.run = async (binary, args) => {
    calls.push(`${binary}:${args[0]}`);
    if (args[0] === 'status' && statuses++ === 0) throw Object.assign(new Error('stopped'), {code: 3, postgresExecution: 'failed'});
    if (args[0] === 'start') throw readiness;
    return {stdout: '', stderr: ''};
  };
  operations.readInstance = async () => { calls.push('instance'); throw new Error('unverified instance'); };
  await assert.rejects(pg.start(), error => error === readiness);
  assert.equal(pg.startedHere, false);
  assert.deepEqual(calls, ['prepare', 'version', 'pg_ctl:status', 'socket-mode', 'pg_ctl:start', 'pg_ctl:status', 'instance']);
});

test('unsubmitted, unknown and unclassified start failures cannot adopt another running instance', async () => {
  for (const details of [
    {code: 'ENOENT', postgresExecution: 'not-submitted'},
    {code: 3, postgresExecution: 'unknown'},
    {code: 1},
  ]) {
    for (const alreadyOwned of [false, true]) {
      const {operations, calls} = observedOperations();
      const failure = Object.assign(new Error('this start has no confirmed outcome'), details);
      let statuses = 0;
      operations.run = async (binary, args) => {
        calls.push(`${binary}:${args[0]}`);
        if (args[0] === 'status' && statuses++ === 0) throw Object.assign(new Error('stopped'), {code: 3, postgresExecution: 'failed'});
        if (args[0] === 'start') throw failure;
        // A different caller has started this exact layout after our attempt failed.
        return {stdout: '', stderr: ''};
      };
      const pg = new BundledPostgres(operations);
      pg.startedHere = alreadyOwned;
      await assert.rejects(pg.start(), error => error === failure);
      assert.equal(pg.startedHere, alreadyOwned);
      assert.deepEqual(calls, ['prepare', 'version', 'pg_ctl:status', 'socket-mode', 'pg_ctl:start']);
    }
  }
});

test('status 3 without a confirmed process failure cannot authorize start or reset', async () => {
  for (const details of [
    {code: 3, postgresExecution: 'unknown'},
    {code: 3, postgresExecution: 'not-submitted'},
    {code: 3},
    {code: '3', postgresExecution: 'failed'},
  ]) {
    const {operations, calls} = observedOperations();
    const failure = Object.assign(new Error('status result is not confirmed'), details);
    operations.run = async (binary, args) => { calls.push(`${binary}:${args[0]}`); throw failure; };
    const pg = new BundledPostgres(operations);
    await assert.rejects(pg.start(), error => error === failure);
    assert.equal(pg.startedHere, false);
    assert.deepEqual(calls, ['prepare', 'version', 'pg_ctl:status']);
    calls.length = 0;
    pg.startedHere = true;
    await assert.rejects(pg.resetConfirmed(), error => error === failure);
    assert.equal(pg.startedHere, true);
    assert.deepEqual(calls, ['reset-paths', 'data-info', 'version', 'pg_ctl:status']);
  }
});

test('first initialization retains the original argv and only updates state after successful physical commands', async () => {
  const {operations, calls} = observedOperations();
  operations.readVersion = async () => { throw Object.assign(new Error('no version'), {code: 'ENOENT'}); };
  const commands: Array<{binary: string; args: readonly string[]}> = [];
  operations.run = async (binary, args) => {
    commands.push({binary, args});
    if (args[0] === 'status') throw Object.assign(new Error('stopped'), {code: 3, postgresExecution: 'failed'});
    return {stdout: '', stderr: ''};
  };
  const pg = new BundledPostgres(operations);
  await pg.start();
  assert.equal(pg.initialized, true);
  assert.equal(pg.startedHere, true);
  assert.deepEqual(commands, [
    {binary: 'initdb', args: ['-D', pg.directory, '-U', 'pulsara_admin', '--auth-local=trust', '--auth-host=reject', '--encoding=UTF8', '--locale=C']},
    {binary: 'pg_ctl', args: ['status', '-D', pg.directory]},
    {binary: 'pg_ctl', args: ['start', '-w', '-D', pg.directory, '-l', operations.logPath, '-o', `-c listen_addresses='' -k '${pg.socket}' -c unix_socket_permissions=0700 -c port=5432 -c shared_preload_libraries=pg_search`]},
  ]);
  assert.deepEqual(calls, ['prepare', 'socket-mode']);
});

test('reset never removes data after a stop failure and clears ownership only after removal succeeds', async () => {
  const {operations, calls} = observedOperations();
  const stopFailure = new Error('stop failed');
  operations.run = async (binary, args) => {
    calls.push(`${binary}:${args[0]}`);
    if (args[0] === 'stop') throw stopFailure;
    return {stdout: '', stderr: ''};
  };
  const pg = new BundledPostgres(operations);
  pg.startedHere = true;
  await assert.rejects(pg.resetConfirmed(), error => error === stopFailure);
  assert.equal(pg.startedHere, true);
  assert.deepEqual(calls, ['reset-paths', 'data-info', 'version', 'pg_ctl:status', 'instance', 'pg_ctl:stop']);

  calls.length = 0;
  operations.run = async (binary, args) => { calls.push(`${binary}:${args[0]}`); return {stdout: '', stderr: ''}; };
  const removeFailure = new Error('remove failed');
  operations.removeDataDirectory = async () => { calls.push('remove'); throw removeFailure; };
  await assert.rejects(pg.resetConfirmed(), error => error === removeFailure);
  assert.equal(pg.startedHere, true);
  assert.equal(calls.at(-1), 'remove');
  operations.removeDataDirectory = async () => { calls.push('remove'); };
  await pg.resetConfirmed();
  assert.equal(pg.startedHere, false);
});

test('the Unix socket byte boundary and DSN escaping remain lifecycle-owner behavior', async () => {
  const {operations, calls} = observedOperations();
  const pg = new BundledPostgres({...operations, socketAddressBytes: 104});
  await assert.rejects(pg.start(), /PULSARA_HOME 路径过长/);
  assert.deepEqual(calls, ['prepare']);
  await new BundledPostgres({...operations, socketAddressBytes: 103}).start();
  const socket = "/home/test's\\data/desktop/socket";
  const quoted = new BundledPostgres({...operations, socket});
  const escaped = socket.replaceAll('\\', '\\\\').replaceAll("'", "\\'");
  assert.deepEqual(quoted.settings(), {
    runtime_dsn: `host='${escaped}' port=5432 dbname=pulsara user=pulsara_runtime`,
    admin_dsn: `host='${escaped}' port=5432 dbname=pulsara user=pulsara_admin`,
  });
});

test('POSIX reset refuses a linked data directory and preserves its actual target', async () => {
  const root = await mkdtemp('/tmp/pg-reset-');
  const operations = createPosixPostgresOperations(path.join(root, 'postgres/bin'), root);
  const pg = new BundledPostgres(operations);
  try {
    await operations.prepareLayout();
    const target = path.join(root, 'untouched');
    await mkdir(target);
    await writeFile(path.join(target, 'PG_VERSION'), '17\n');
    await symlink(target, pg.directory);
    await assert.rejects(pg.resetConfirmed(), /数据库目录包含链接/);
    assert.equal(await readFile(path.join(target, 'PG_VERSION'), 'utf8'), '17\n');
    assert.equal((await stat(operations.socket)).mode & 0o777, 0o700);
  } finally { await rm(root, {recursive: true, force: true}); }
});

test('reset keeps the raw PID-directory rejection when its caller working directory has been deleted', async () => {
  const root = await realpath(await mkdtemp('/tmp/pg-cwd-'));
  try {
    // A separate process owns the removed cwd; the Node test runner never changes cwd.
    const script = `
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {BundledPostgres} = require(process.argv[2]);
const {createPosixPostgresOperations} = require(process.argv[3]);
(async () => {
  const root = process.argv[1];
  const bin = path.join(root, 'bin');
  await fs.mkdir(bin);
  await fs.writeFile(path.join(bin, 'pg_ctl'), '#!/bin/sh\\nexit 0\\n', {mode: 0o755});
  const operations = createPosixPostgresOperations(bin, root);
  await operations.prepareLayout();
  await fs.mkdir(operations.directory);
  await fs.writeFile(path.join(operations.directory, 'PG_VERSION'), '17\\n');
  await fs.writeFile(path.join(operations.directory, 'postmaster.pid'), '123\\n');
  const removed = path.join(root, 'removed-cwd');
  await fs.mkdir(removed);
  process.chdir(removed);
  await fs.rmdir(removed);
  assert.throws(() => process.cwd(), {code: 'ENOENT'});
  const pg = new BundledPostgres(operations);
  pg.startedHere = true;
  await assert.rejects(pg.resetConfirmed(), error => error.message === '运行实例与专用目录不匹配，拒绝重置。');
  assert.equal(pg.startedHere, true);
  assert.equal(await fs.readFile(path.join(operations.directory, 'PG_VERSION'), 'utf8'), '17\\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
`;
    await promisify(execFile)(process.execPath, ['-e', script, root, path.join(__dirname, 'postgres.js'), path.join(__dirname, 'postgres-posix.js')]);
  } finally { await rm(root, {recursive: true, force: true}); }
});
