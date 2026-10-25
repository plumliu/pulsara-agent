'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const asar = require('C:/python_workspace/pulsara-agent/desktop/node_modules/@electron/asar');

const root = __dirname;
const repo = 'C:/python_workspace/pulsara-agent';
const guestRoot = '/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/backend';
const installed = '/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10';
const python = installed + '/python/bin/python3';
const wsl = path.join(process.env.SystemRoot, 'System32', 'wsl.exe');
const base = ['-d', 'Ubuntu', '-u', 'plumwsl', '--cd', '/home/plumwsl', '--exec'];
const app = path.join(process.env.LOCALAPPDATA, 'Programs/pulsara/resources/app.asar');
const modules = path.join(root, 'installed-ts');
fs.mkdirSync(modules, {recursive: true});
for (const name of ['postgres.js', 'postgres-operations.js', 'postgres-wsl.js', 'host-wsl.js']) {
  const bytes = asar.extractFile(app, 'dist/' + name);
  assert.deepEqual(bytes, fs.readFileSync(path.join(repo, 'desktop/dist', name)));
  const target = path.join(modules, name);
  if (fs.existsSync(target)) assert.deepEqual(bytes, fs.readFileSync(target));
  else fs.writeFileSync(target, bytes);
}
const {BundledPostgres} = require(path.join(modules, 'postgres.js'));
const {createWslPostgresOperations, checkWslMaintenance} = require(path.join(modules, 'postgres-wsl.js'));
const {createWslHostOperations} = require(path.join(modules, 'host-wsl.js'));
const resources = {root: installed, pythonExecutable: python, postgresBin: installed + '/postgres/bin', playwrightBrowsers: installed + '/playwright', packageCli: installed + '/bin/pulsara'};
const target = {distro: 'Ubuntu', user: 'plumwsl', uid: 1000, home: '/home/plumwsl'};

function linux(args) {
  return execFileSync(wsl, [...base, ...args], {encoding: 'utf8', windowsHide: true, maxBuffer: 4 * 1024 * 1024});
}
function pythonCode(code, ...args) { return linux([python, '-I', '-c', code, ...args]); }
function failed(action) { return action().then(() => null, error => ({code: error.code, execution: error.postgresExecution, message: error.message, stdout: error.stdout, stderr: error.stderr})); }
const stateOf = owner => ({startedHere: owner.startedHere, initialized: owner.initialized});
function event(caseRoot, kind, value) {
  fs.appendFileSync(path.join(caseRoot, 'events.jsonl'), JSON.stringify({time: new Date().toISOString(), kind, ...value}) + '\n');
}

async function runCase(name, fault, library) {
  const out = path.join(root, name);
  fs.mkdirSync(out);
  const home = pythonCode("import tempfile,os;h=tempfile.mkdtemp(prefix='.pbfpg-',dir='/home/plumwsl');os.chmod(h,0o700);open(h+'/outside','w').write('outside-sentinel');print(h)").trim();
  const config = {home, installed, repo: '/home/plumwsl/workspaces/pulsara_agent', library, ...fault};
  const configFile = path.join(out, 'config.json');
  const guestConfig = guestRoot + '/' + name + '/config.json';
  fs.writeFileSync(configFile, JSON.stringify(config, null, 2));
  const control = (action, value) => JSON.parse(linux([python, '-I', guestRoot + '/control.py', guestConfig, action, ...(value === undefined ? [] : [typeof value === 'string' ? value : JSON.stringify(value)])]));
  const physical = createWslHostOperations(target, home, out);
  let armed = false, abort, sequence = 0, active;
  let targetCreated, inputClosed;
  const targetSpawned = new Promise(resolve => { targetCreated = resolve; });
  const targetInputClosed = new Promise(resolve => { inputClosed = resolve; });
  const calls = [];
  const host = {...physical, spawn(file, args, options) {
    assert.equal(file, python);
    assert.deepEqual(args.slice(0, 2), ['-m', 'pulsara_agent.desktop_guest_ops']);
    const method = args.at(-1);
    const id = String(++sequence);
    const useFault = armed && method === config.method;
    const child = physical.spawn(file, ['-I', guestRoot + '/bootstrap.py', '--config', guestConfig, '--invocation', id, '--armed', useFault ? 'yes' : 'no', ...args.slice(2)], options);
    const row = {id, method, armed: useFault, carrierPid: child.pid, operationBytes: 0};
    calls.push(row);
    if (useFault) {
      active = row;
      targetCreated();
      child.stdin.once('finish', () => inputClosed());
    }
    const end = child.stdin.end.bind(child.stdin);
    child.stdin.end = function(chunk, ...rest) {
      if (typeof chunk === 'string' || Buffer.isBuffer(chunk)) row.operationBytes += Buffer.byteLength(chunk);
      return end(chunk, ...rest);
    };
    child.once('close', (code, signal) => { row.closed = {code, signal}; });
    return child;
  }};
  const operations = createWslPostgresOperations(resources, home, host, () => abort?.signal);
  const owner = new BundledPostgres(operations);
  const result = {name, config, provenance: {app, installedTsExactCurrentDist: true, node: process.version, productionOwner: true, productionClient: true, productionWslHost: true, installedHelperWithTestGates: true}};
  function record(kind, value) { event(out, kind, value); result[kind] = value; }
  try {
    if (name !== 'init-loss') {
      const initializer = name === 'stop-loss' ? owner : new BundledPostgres(operations);
      await initializer.start();
      record('initialQuery', control('wait-query'));
      if (name !== 'stop-loss') await initializer.stop();
      control('sentinel');
    }
    armed = true;
    if (name === 'pre-cancel') abort = new AbortController();
    const pending = failed(() => name === 'stop-loss' ? owner.stop() : owner.start());
    // Let the original async client settle its preceding calls before entering
    // a synchronous observer. Native gates also need the actual operation+EOF.
    await targetSpawned;
    if (name !== 'pre-cancel') await targetInputClosed;
    if (name === 'pre-cancel') {
      const gate = control('wait-gate-ready');
      record('admissionGate', {gate, facts: control('state')});
      abort.abort();
      await targetInputClosed;
      armed = false;
      control('resume', gate);
      control('wait-helper-exit', gate);
      // Keep this Node's original close/catch queued while a second actual TS
      // owner explicitly starts the same private stopped cluster.
      const foreign = JSON.parse(execFileSync(process.execPath, [__filename, 'control-start', configFile], {encoding: 'utf8', windowsHide: true}));
      record('foreignOwner', foreign);
      const error = await pending;
      assert.equal(error.execution, 'not-submitted');
      assert.equal(active.operationBytes, 0);
      assert.equal(owner.startedHere, false);
      record('outcome', {error, owner: stateOf(owner), target: active, facts: control('state')});
      abort = undefined;
      await owner.stop();
    } else {
      const gate = control('wait-gate-native');
      const helper = control('helper', active.id);
      record('nativeGate', {gate, helper, children: control('children', active.id), facts: control('state')});
      if (name === 'readiness') {
        const readyFailure = control('wait-gate-failed-result');
        record('settledFailureGate', {helper: readyFailure, facts: control('state')});
        control('resume', gate);
        record('readyBeforeFailureReply', control('wait-query'));
        control('resume', readyFailure);
        const error = await pending;
        assert.equal(error.execution, 'failed');
        assert.equal(typeof error.code, 'number');
        assert.equal(owner.startedHere, true);
        record('outcome', {error, owner: stateOf(owner), calls, facts: control('state')});
        armed = false;
        await owner.stop();
      } else {
        control('kill-helper', helper);
        armed = false;
        const facts = control('state');
        if (name === 'start-loss') {
          assert.equal(facts.status.exit, 3);
          assert.equal(facts.locks['kernel.lock'].busy, false);
          assert.equal(facts.locks['pg-lifetime.lock'].busy, true);
        } else {
          assert.equal(facts.locks['kernel.lock'].busy, true);
          if (name === 'init-loss') assert.equal(facts.locks['pg-lifetime.lock'].busy, true);
          else {
            assert.equal(facts.status.exit, 3);
            assert.equal(facts.locks['pg-lifetime.lock'].busy, false);
          }
        }
        const competitor = new BundledPostgres(createWslPostgresOperations(resources, home, host));
        const competition = {start: await failed(() => competitor.start()), reset: await failed(() => owner.resetConfirmed()), maintenance: await failed(() => checkWslMaintenance(resources, home, host))};
        for (const value of Object.values(competition)) {
          assert(value);
          assert.equal(value.execution, 'not-submitted');
          assert.equal(value.code, name === 'start-loss' ? 'PG_LIFETIME_BUSY' : 'PG_KERNEL_BUSY');
        }
        const untouched = control('state');
        assert.equal(untouched.outside, 'outside-sentinel');
        if (name !== 'init-loss') assert.equal(untouched.sentinel, 'data-sentinel');
        record('inflightRefusals', {facts, competition, untouched});
        const afterCompetitors = calls.length;
        control('resume', gate);
        if (name === 'start-loss') record('lateQuery', control('wait-query'));
        else control('wait-helper-exit', gate);
        const error = await pending;
        assert.equal(error.execution, 'unknown');
        assert.equal(calls.length, afterCompetitors, 'The failed original call must not add readback or replay');
        if (name === 'init-loss') assert.equal(owner.initialized, false);
        if (name === 'stop-loss') assert.equal(owner.startedHere, true);
        if (name === 'start-loss') {
          assert.equal(owner.startedHere, false);
          await owner.stop();
        }
        record('outcome', {error, owner: stateOf(owner), target: active, facts: control('state')});
      }
    }
    armed = false;
    if (control('state').data_exists) await owner.resetConfirmed();
    else {
      assert.equal(name, 'init-loss');
      await checkWslMaintenance(resources, home, host);
      record('emptyLayoutMaintenance', {passed: true, facts: control('state')});
    }
    const final = control('state');
    assert.equal(final.data_exists, false);
    assert.equal(final.outside, 'outside-sentinel');
    assert.equal(final.locks['kernel.lock'].busy, false);
    assert.equal(final.locks['pg-lifetime.lock'].busy, false);
    record('cleanup', {owner: stateOf(owner), facts: final, calls});
    result.passed = true;
  } catch (error) {
    result.failure = {message: error.message, stack: error.stack, execution: error.postgresExecution, code: error.code};
    throw error;
  } finally {
    fs.writeFileSync(path.join(out, 'result.json'), JSON.stringify(result, null, 2));
  }
  return result;
}

async function main() {
  if (process.argv[2] === 'control-start' || process.argv[2] === 'control-cleanup') {
    const config = JSON.parse(fs.readFileSync(process.argv[3]));
    const real = createWslHostOperations(target, config.home, root);
    const owner = new BundledPostgres(createWslPostgresOperations(resources, config.home, real));
    if (process.argv[2] === 'control-start') await owner.start();
    else await owner.resetConfirmed();
    console.log(JSON.stringify(stateOf(owner)));
    return;
  }
  const toolHome = pythonCode("import tempfile,os;h=tempfile.mkdtemp(prefix='.pbf-tools-',dir='/home/plumwsl');os.chmod(h,0o700);print(h)").trim();
  const library = toolHome + '/fault.so';
  fs.writeFileSync(path.join(root, 'tool-home.json'), JSON.stringify({home: toolHome, library}));
  fs.appendFileSync(path.join(root, 'tool-homes.jsonl'), JSON.stringify({home: toolHome, library}) + '\n');
  linux(['/usr/bin/gcc', '-shared', '-fPIC', '-O0', '-o', library, guestRoot + '/fault.c', '-ldl']);
  const selected = process.argv[2] || 'all';
  const cases = [
    ['pre-cancel', {method: 'run:pg_ctl:start', before_ready: true}],
    ['start-loss', {method: 'run:pg_ctl:start', native_stage: 'postgres-exec'}],
    ['stop-loss', {method: 'run:pg_ctl:stop', native_stage: 'pgctl-stop-tail'}],
    ['init-loss', {method: 'run:initdb', native_stage: 'initdb-exec'}],
    ['readiness', {method: 'run:pg_ctl:start', native_stage: 'postgres-exec', before_failed_result: true, readiness_timeout: true}],
  ];
  for (const [name, fault] of cases) if (selected === 'all' || selected === name) {
    await runCase(name, fault, library);
    console.log(JSON.stringify({case: name, passed: true}));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
