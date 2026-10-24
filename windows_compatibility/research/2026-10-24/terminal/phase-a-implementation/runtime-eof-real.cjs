const assert = require('node:assert/strict');
const { once } = require('node:events');
const fs = require('node:fs/promises');
const path = require('node:path');

const repository = process.argv[2];
assert.ok(path.isAbsolute(repository));
const { PythonRuntime } = require(path.join(repository, 'desktop/dist/runtime.js'));
const { posixHostOperations } = require(path.join(repository, 'desktop/dist/host-posix.js'));
const { desktopResourcePaths } = require(path.join(repository, 'desktop/dist/resource-paths.js'));

(async () => {
  const home = await fs.realpath(await fs.mkdtemp('/tmp/prt-eof-'));
  const resources = desktopResourcePaths(path.join(repository, 'desktop/resources'));
  const observations = [];
  let child;
  let childClosed = false;
  let commands = 0;
  const host = {
    ...posixHostOperations,
    spawn(file, args, options) {
      child = posixHostOperations.spawn(file, args, options);
      observations.push({ event: 'spawn-called', file, args, cwd: options.cwd });
      child.once('spawn', () => observations.push({ event: 'spawn', pid: child.pid }));
      child.once('exit', (code, signal) => observations.push({ event: 'exit', pid: child.pid, code, signal }));
      child.once('close', (code, signal) => { childClosed = true; observations.push({ event: 'close', pid: child.pid, code, signal }); });
      return child;
    },
    execFile(file, args, options) {
      commands++;
      observations.push({ event: 'finite-exec', file, args });
      return posixHostOperations.execFile(file, args, options);
    },
  };
  const events = [];
  const runtime = new PythonRuntime(resources, home, event => events.push(event), host);
  const expected = new Error('experiment: PostgreSQL preparation rejected before start frame');
  let prepared = 0;
  try {
    let rejected;
    try {
      await runtime.start(async () => {
        prepared++;
        await once(child, 'spawn');
        throw expected;
      }, true);
    } catch (error) { rejected = error; }
    assert.strictEqual(rejected, expected);
    assert.equal(prepared, 1);
    assert.equal(childClosed, true);
    assert.equal(runtime.running, false);
    assert.equal(child.exitCode, 0);
    assert.equal(child.signalCode, null);
    assert.ok(child.stdin.writableEnded);
    assert.equal(events.some(event => event.type === 'ready' || event.type === 'starting' || event.type === 'failed'), false);
    assert.deepEqual(events.map(event => event.type), ['stopping', 'stopped']);
    assert.throws(() => process.kill(child.pid, 0), { code: 'ESRCH' });
    const beforeWait = await fs.readdir(path.join(home, 'desktop'));
    assert.deepEqual(beforeWait, []);
    await runtime.waitForPreviousOwner();
    await runtime.waitForPreviousOwner();
    assert.equal(commands, 2);
    await runtime.stop();
    assert.equal(runtime.running, false);
    const afterWait = await fs.readdir(path.join(home, 'desktop'));
    assert.deepEqual(afterWait, ['kernel.lock']);
    assert.deepEqual(await fs.readdir(home), ['desktop']);
    console.log(JSON.stringify({
      result: 'passed', home, python: resources.pythonExecutable,
      preparePostgresCalls: prepared, postgresStarted: false,
      originalErrorIdentityPreserved: rejected === expected,
      childPid: child.pid, childClosed, childExitCode: child.exitCode,
      stdinEnded: child.stdin.writableEnded, runtimeRunning: runtime.running,
      events, observations, beforeWait, afterWait,
      waitOwnerCompletedTwice: true,
      retainedLockFile: 'expected lifetime-lock inode only; no held owner (two independent --wait-owner completions)',
      settingsCreated: false, postgresDirectoryCreated: false,
    }, null, 2));
  } finally {
    if (runtime.running) await runtime.stop();
    await fs.rm(home, { recursive: true });
    console.log(JSON.stringify({ cleanup: 'temporary experiment home removed', home }));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
