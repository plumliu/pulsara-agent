// Research adapter imports the actual compiled production lifecycle owner.
const {spawn, spawnSync} = require('node:child_process');
const {StringDecoder} = require('node:string_decoder');
const path = require('node:path');
const assert = require('node:assert/strict');
const implementation = process.argv[4] || 'baseline';
const {BundledPostgres} = require(implementation === 'candidate' ? './candidate-compiled/postgres.js' : './compiled/postgres.js');

const [home, scenario] = process.argv.slice(2);
const repo = '/home/plumwsl/workspaces/pulsara_agent';
const linux = repo + '/output/windows-g0/pg_admission_r3/round4';
const python = repo + '/.venv/bin/python';
const wsl = path.join(process.env.SystemRoot, 'System32', 'wsl.exe');
const base = ['-d', 'Ubuntu', '-u', 'plumwsl', '--cd', repo, '--exec'];
const events = [];
function emit(event, values = {}) {
  const record = {event, time: Date.now(), ...values};
  events.push(record);
  process.stdout.write(JSON.stringify(record) + '\n');
}
function control(action, ...args) {
  const argv = [...base, python, linux + '/control.py', action, home, ...args.map(String)];
  const result = spawnSync(wsl, argv, {encoding: 'utf8', windowsHide: true});
  emit('observation', {argv, exit: result.status, stdout: result.stdout, stderr: result.stderr});
  assert.equal(result.status, 0);
  return JSON.parse(result.stdout);
}
function transportFailure(error, sent) {
  error.postgresExecution = sent ? 'unknown' : 'not-submitted';
  return error;
}
const errorInfo = error => ({code: error.code, message: error.message, stdout: error.stdout, stderr: error.stderr, postgresExecution: error.postgresExecution});
let sequence = 0;
let cancelledStartSettled = false, foreignInjected = false, foreignStartArgs;

async function call(method, payload = {}, override) {
  const id = ++sequence;
  let target = override ?? (method === 'run:pg_ctl:start' ? scenario : 'none');
  if (target === 'foreign-before-readback') {
    target = 'ready-cancel';
    foreignStartArgs = payload.args;
  }
  const argv = [...base, python, linux + '/helper.py', repo, home, method, target];
  const child = spawn(wsl, argv, {stdio: ['pipe', 'pipe', 'pipe'], shell: false, windowsHide: true});
  emit('carrier-spawn', {id, method, carrierPid: child.pid, executable: wsl, argv});
  let result, ready, sent = false, noOperation = false, truncated = false, admissionClosed = false;
  let stderr = '', chain = Promise.resolve();
  child.stdin.on('error', error => emit('stdin-error', {id, error: errorInfo(error)}));
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', chunk => {stderr += chunk;});
  const decoder = new StringDecoder('utf8');
  let pending = '';
  function lineReceived(line) {
    chain = chain.then(async () => {
      const frame = JSON.parse(line);
      emit('helper-frame', {id, method, frame});
      if (frame.type === 'result') result = frame;
      if (frame.type === 'no-operation') noOperation = true;
      if (frame.type === 'gate' && frame.stage === 'before-ready') {
        admissionClosed = true;
        child.stdin.end();
        emit('stdin-eof', {id, boundary: 'before-ready'});
        control('signal', frame.pid, frame.start_ticks, 'SIGCONT');
      }
      if (frame.type === 'ready') {
        ready = frame;
        if (admissionClosed || target === 'ready-eof' || target === 'ready-cancel') {
          admissionClosed = true;
          child.stdin.end();
          emit(target === 'ready-cancel' ? 'cancel-before-operation' : 'stdin-eof', {id, boundary: 'after-ready'});
        } else {
          child.stdin.write(JSON.stringify({operation: method, ...payload}) + '\n');
          sent = true;
          emit('operation-written', {id, method});
          if (target === 'operation-eof') {
            child.stdin.end();
            emit('stdin-eof', {id, boundary: 'after-operation'});
          }
        }
      }
      if (frame.type === 'launched' && ['helper-death', 'carrier-death', 'node-death'].includes(target)) {
        const gate = control('waitgate', 'gate');
        emit('at-physical-gate', {id, gate, helper: ready, carrierPid: child.pid});
        if (target === 'helper-death') {
          control('signal', ready.pid, ready.start_ticks, 'SIGKILL');
          emit('after-helper-death', {id, state: control('state')});
          control('resume-pg');
        }
        if (target === 'carrier-death') {
          child.kill();
          emit('carrier-terminated', {id, carrierPid: child.pid, state: control('state')});
          control('resume-pg');
        }
        if (target === 'node-death') emit('node-kill-ready', {id, helper: ready, carrierPid: child.pid});
      }
      if (frame.type === 'gate' && frame.stage === 'before-result') {
        control('waitgate', 'helper-gate');
        truncated = true;
        child.stdout.destroy();
        emit('stdout-read-end-closed', {id, boundary: 'settled-before-result'});
        control('signal', frame.pid, frame.start_ticks, 'SIGCONT');
      }
    });
  }
  child.stdout.on('data', chunk => {
    pending += decoder.write(chunk);
    let index;
    while ((index = pending.indexOf('\n')) >= 0) {
      const line = pending.slice(0, index);
      pending = pending.slice(index + 1);
      lineReceived(line);
    }
  });
  child.stdout.on('end', () => {pending += decoder.end();});
  return new Promise((resolve, reject) => {
    child.once('error', error => reject(transportFailure(error, sent)));
    child.once('close', async (code, signal) => {
      try {
        await chain;
        emit('carrier-close', {id, method, code, signal, sent, noOperation, truncated, stderr});
        if (method === 'run:pg_ctl:start' && !sent && noOperation && code === 0) cancelledStartSettled = true;
        if (pending.length) {truncated = true; emit('partial-final-frame', {id, pending});}
        if (truncated || code !== 0 || !result) {
          const error = new Error(sent ? 'physical operation result unknown' : 'operation not submitted; helper exited');
          error.code = sent ? 'PG_OUTCOME_UNKNOWN' : 'PG_NOT_EXECUTED';
          error.postgresExecution = sent ? 'unknown' : 'not-submitted';
          error.stderr = stderr;
          if (scenario === 'foreign-before-readback' && method === 'run:pg_ctl:start' && !sent && noOperation && !foreignInjected) {
            foreignInjected = true;
            emit('independent-start-before-catch-readback');
            await call('run:pg_ctl:start', {args: foreignStartArgs}, 'none');
            emit('independent-start-settled', {state: control('state')});
          }
          throw error;
        }
        if (!result.ok) throw Object.assign(new Error(result.error.message), result.error, sent ? {} : {postgresExecution: 'not-submitted'});
        resolve(result.value);
      } catch (error) {
        if (!error.postgresExecution && (code !== 0 || truncated || pending.length || !result)) transportFailure(error, sent);
        reject(error);
      }
    });
  });
}

const operations = {
  directory: home + '/desktop/postgres',
  socket: home + '/desktop/socket',
  logPath: home + '/desktop/postgres.log',
  socketAddressBytes: Buffer.byteLength(home + '/desktop/socket/.s.PGSQL.5432'),
  prepareLayout: () => call('prepareLayout'),
  readVersion: () => call('readVersion'),
  readInstance: form => call('readInstance', {form}),
  socketMode: () => call('socketMode'),
  resetPathsMatch: () => call('resetPathsMatch'),
  dataDirectoryInfo: () => call('dataDirectoryInfo'),
  run: (binary, args) => call(binary === 'initdb' ? 'run:initdb' : `run:pg_ctl:${args[0]}`, {args}),
  removeDataDirectory: () => call('removeDataDirectory'),
};
const owner = new BundledPostgres(operations);
const snapshot = () => ({initialized: owner.initialized, startedHere: owner.startedHere});

async function main() {
  emit('node-start', {pid: process.pid, scenario, implementation, platform: process.platform, node: process.version});
  if (scenario === 'normal') {
    await owner.start();
    assert.deepEqual(snapshot(), {initialized: true, startedHere: true});
    emit('owner-started', {owner: snapshot(), settings: owner.settings(), query: control('query')});
    const reused = new BundledPostgres(operations);
    await reused.start();
    assert.equal(reused.startedHere, false);
    emit('owner-reused', {initialized: reused.initialized, startedHere: reused.startedHere});
    control('data-sentinel');
    const before = control('state');
    await owner.resetConfirmed();
    assert.equal(owner.startedHere, false);
    const reset = control('state');
    assert.equal(reset.data_exists, false);
    assert.equal(reset.status.exit, 4);
    assert.equal(reset.pg.inode, before.pg.inode);
    assert.equal(reset.socket_exists, true);
    emit('owner-reset-running', {owner: snapshot(), before, reset});
    await owner.start();
    assert.equal(control('query').exit, 0);
    await owner.stop();
    emit('owner-stopped', {owner: snapshot(), state: control('state')});
  } else {
    let failure;
    try {await owner.start();} catch (error) {failure = errorInfo(error);}
    emit('owner-result', {owner: snapshot(), failure: failure || null,
      mainQuitWouldStop: owner.startedHere, state: control('state')});
    if (scenario === 'operation-eof') {
      assert.equal(failure, undefined);
      assert.equal(owner.startedHere, true);
    } else if (scenario === 'foreign-before-readback') {
      assert.equal(failure.code, 'PG_NOT_EXECUTED');
      assert.equal(owner.startedHere, implementation !== 'candidate');
      assert.equal(control('query').exit, 0);
      const starts = events.filter(event => event.event === 'carrier-spawn' && event.method === 'run:pg_ctl:start');
      assert.equal(starts.length, 2);
      assert.equal(events.filter(event => event.event === 'operation-written' && event.id === starts[0].id).length, 0);
      assert.equal(events.filter(event => event.event === 'operation-written' && event.id === starts[1].id).length, 1);
    } else if (['before-ready-eof', 'ready-eof', 'ready-cancel'].includes(scenario)) {
      assert.equal(failure.code, 'PG_NOT_EXECUTED');
      assert.equal(owner.startedHere, false);
      assert.equal(control('state').status.exit, 3);
      const attempt = events.find(event => event.event === 'carrier-spawn' && event.method === 'run:pg_ctl:start');
      assert.equal(events.filter(event => event.event === 'operation-written' && event.id === attempt.id).length, 0);
      assert(events.some(event => event.event === 'helper-frame' && event.id === attempt.id && event.frame.type === 'no-operation'));
      assert(events.some(event => event.event === 'carrier-close' && event.id === attempt.id && event.code === 0));
    } else if (scenario !== 'node-death') {
      assert.equal(failure.code, 'PG_OUTCOME_UNKNOWN');
      if (['reply-loss', 'unterminated-result'].includes(scenario)) {
        assert.equal(owner.startedHere, implementation !== 'candidate');
        assert.equal(control('query').exit, 0);
      }
    }
  }
  emit('case-complete', {scenario, owner: snapshot()});
}
if (process.argv[2] === '--transport-selfcheck') {
  const child = spawn(path.join(__dirname, 'not-present-wsl.exe'), [], {shell: false, windowsHide: true});
  child.once('error', original => {
    const originalCode = original.code;
    const cause = {fixture: 'original cause'};
    original.cause = cause;
    assert.equal(transportFailure(original, false), original);
    assert.equal(original.code, originalCode);
    assert.equal(original.code, 'ENOENT');
    assert.equal(original.cause, cause);
    assert.equal(original.postgresExecution, 'not-submitted');
    process.stdout.write(JSON.stringify({passed: true, node: process.version, code: original.code, postgresExecution: original.postgresExecution, sameError: true, causePreserved: true}) + '\n');
  });
} else main().catch(error => {
  emit('harness-failure', {error: errorInfo(error), stack: error.stack, owner: snapshot()});
  process.exitCode = 1;
});
