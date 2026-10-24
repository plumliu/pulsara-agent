// Actual TS lifecycle owner, using the single reusable research transport.
const {spawn} = require('node:child_process');
const path = require('node:path');
const assert = require('node:assert/strict');
const {BundledPostgres} = require('./candidate-compiled/postgres.js');
const {createClient} = require('./client.cjs');
const [home, scenario] = process.argv.slice(2);
const repo = '/home/plumwsl/workspaces/pulsara_agent';
const remote = repo + '/output/windows-g0/pg_admission_r3/round5';
const python = repo + '/.venv/bin/python';
const wsl = path.join(process.env.SystemRoot, 'System32', 'wsl.exe');
const base = ['-d', 'Ubuntu', '-u', 'plumwsl', '--cd', repo, '--exec'];
const client = createClient(home, row => process.stdout.write(JSON.stringify(row) + '\n'));
const {operations, control, events, emit} = client;
const errorInfo = error => ({code: error.code, message: error.message, stdout: error.stdout, stderr: error.stderr, postgresExecution: error.postgresExecution});
const owner = new BundledPostgres(operations);
const snapshot = () => ({initialized: owner.initialized, startedHere: owner.startedHere});
async function failed(action) {try {await action(); return null;} catch (error) {return errorInfo(error);}}
async function main() {
  emit('node-start', {pid: process.pid, scenario, node: process.version});
  if (scenario === 'readonly-missing') {
    const error = await failed(() => operations.readVersion());
    assert.equal(error.code, 'ENOENT'); assert.equal(error.postgresExecution, undefined);
    assert(!owner.initialized && !owner.startedHere);
    emit('readonly-known-missing', {error, state: control('state')});
  } else if (scenario === 'utf8-fragmented') {
    client.setFault('fragmented'); await owner.start();
    const instance = await operations.readInstance('raw');
    assert.equal(instance.directory, operations.directory); assert.equal(instance.socket, operations.socket);
    const closes = events.filter(row => row.event === 'carrier-close');
    assert(closes.some(row => row.leadingTailSplits > 0));
    emit('utf8-codepoint-splits-verified', {instance, splits: closes.reduce((sum, row) => sum + row.leadingTailSplits, 0)});
    client.setFault('none'); await owner.stop();
  } else if (scenario === 'functional') {
    client.setFault('fragmented'); await owner.start();
    assert.equal(owner.startedHere, true); assert.equal(control('wait-query').exit, 0);
    const reuse = new BundledPostgres(operations); await reuse.start(); assert.equal(reuse.startedHere, false);
    emit('functional-queries', {result: control('queries', 'initial')});
    control('data-sentinel'); const initial = control('state'); const initialQuery = control('query');
    const wrong = new BundledPostgres({...operations, socket: operations.socket + '-wrong'});
    assert.match((await failed(() => wrong.resetConfirmed())).message, /不匹配/);
    assert.equal(control('query').stdout, initialQuery.stdout);
    const originalPid = control('state').pidfile;
    try {
      const lines = originalPid.split('\n'); lines[1] += '/.'; control('pidfile', JSON.stringify(lines.join('\n')));
      await new BundledPostgres(operations).start();
      assert.match((await failed(() => new BundledPostgres(operations).resetConfirmed())).message, /不匹配/);
    } finally {control('pidfile', JSON.stringify(originalPid));}
    await owner.stop(); control('version', '16');
    assert.match((await failed(() => new BundledPostgres(operations).start())).message, /PostgreSQL 16/);
    assert.equal(control('state').data_sentinel, 'data-sentinel'); control('version', '17');
    client.setFault('none'); client.setMoved(true); await owner.start();
    emit('moved-resource-queries', {result: control('queries', 'verify'), state: control('state')});
    await owner.resetConfirmed(); const reset = control('state');
    assert.equal(reset.status.exit, 4); assert.equal(reset.pg.inode, initial.pg.inode);
    assert.equal(reset.outside_sentinel, 'outside-sentinel'); assert(reset.socket_exists);
    emit('owner-reset-complete', {owner: snapshot(), state: reset});
  } else if (scenario === 'kernel-lock') {
    await owner.start();
    const holder = spawn(wsl, [...base, python, remote + '/kernel-holder.py', home], {stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, shell: false});
    emit('carrier-spawn', {id: 'holder', method: 'kernel-holder', carrierPid: holder.pid});
    await new Promise(resolve => holder.stdout.once('data', chunk => {emit('kernel-held', {frame: JSON.parse(chunk)}); resolve();}));
    const error = await failed(() => owner.resetConfirmed());
    assert.equal(error.code, 'PG_KERNEL_BUSY'); assert.equal(error.postgresExecution, 'not-submitted');
    assert.equal(control('query').exit, 0); emit('kernel-maintenance-refused', {error, owner: snapshot(), state: control('state')});
    holder.stdin.end(); await new Promise(resolve => holder.once('close', resolve));
    await owner.stop();
  } else if (['stop-ready-eof', 'remove-ready-eof', 'stop-reply-loss', 'stop-death', 'remove-death', 'remove-partial-death', 'remove-reply-loss', 'concurrent-start'].includes(scenario)) {
    await owner.start(); control('data-sentinel'); const before = control('state');
    client.setFault(scenario); const error = await failed(() => owner.resetConfirmed());
    assert(error); assert.equal(owner.startedHere, true);
    const state = control('state');
    if (scenario === 'remove-reply-loss') {assert(!state.data_exists); assert.equal(state.status.exit, 4);}
    else if (scenario === 'remove-partial-death') {assert(state.data_exists); assert.equal(state.partial_deletion.still_exists, false); assert(state.kernel.acquired && state.pg.acquired);}
    else {assert(state.data_exists); assert.equal(state.data_sentinel, 'data-sentinel');}
    if (scenario === 'concurrent-start') {assert.equal(error.code, 'PG_LIFETIME_BUSY'); assert.equal(error.postgresExecution, 'not-submitted'); assert.equal(control('wait-query').exit, 0);}
    else if (scenario.endsWith('ready-eof')) assert.equal(error.postgresExecution, 'not-submitted');
    else assert.equal(error.postgresExecution, 'unknown');
    emit('owner-fault-result', {error, owner: snapshot(), before, state});
  } else {
    client.setFault(scenario);
    const error = await failed(() => owner.start()); const state = control('state');
    if (scenario === 'initdb-eof') {assert.equal(error, null); assert(owner.startedHere);}
    else if (scenario === 'readiness-fail') {
      assert.equal(error.code, 1); assert.equal(error.postgresExecution, undefined); assert(owner.startedHere);
      assert.equal(control('wait-query').exit, 0);
    } else if (scenario === 'start-fail') {assert.equal(error.code, 1); assert.equal(error.postgresExecution, undefined); assert(!owner.startedHere); assert.equal(state.status.exit, 3);}
    else {
      assert(error); assert(!owner.startedHere);
      if (scenario === 'initdb-ready-eof') {assert.equal(error.postgresExecution, 'not-submitted'); assert.equal(owner.initialized, false); assert.equal(state.data_exists, false);}
      else if (['early-request', 'duplicate-request', 'malformed-request', 'partial-request'].includes(scenario)) {assert.equal(error.postgresExecution, 'not-submitted'); assert.equal(state.status.exit, 3);}
      else if (scenario === 'malformed-ready') {assert.equal(error.postgresExecution, 'not-submitted'); assert.equal(state.status.exit, 3);}
      else assert.equal(error.postgresExecution, 'unknown');
    }
    emit('owner-fault-result', {error, owner: snapshot(), state});
  }
  emit('case-complete', {scenario, owner: snapshot()});
}
main().catch(error => {emit('harness-failure', {error: errorInfo(error), stack: error.stack, owner: snapshot()}); process.exitCode = 1;});
