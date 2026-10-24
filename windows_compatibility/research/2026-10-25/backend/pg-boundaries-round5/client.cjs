// Output-only PostgresOperations adapter; lifecycle remains the mechanically built TS owner.
const {spawn, spawnSync} = require('node:child_process');
const path = require('node:path');
const assert = require('node:assert/strict');
function createClient(home, onEvent = () => {}) {
  const repo = '/home/plumwsl/workspaces/pulsara_agent';
  const remote = repo + '/output/windows-g0/pg_admission_r3/round5';
  const python = repo + '/.venv/bin/python';
  const wsl = path.join(process.env.SystemRoot, 'System32', 'wsl.exe');
  const base = ['-d', 'Ubuntu', '-u', 'plumwsl', '--cd', repo, '--exec'];
  const events = [];
  let sequence = 0, activeFault = 'none', moved = false, lastStartArgs, injected = false;
  const schema = (condition, message) => {if (!condition) throw new Error(message);};
  const keys = (value, wanted) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join(',') === [...wanted].sort().join(',');
  const text = value => typeof value === 'string';
  const physical = value => keys(value, ['exit', 'stdout', 'stderr']) && Number.isInteger(value.exit) && text(value.stdout) && text(value.stderr);
  function emit(event, values = {}) {
    const row = {event, time: Date.now(), ...values}; events.push(row);
    onEvent(row);
  }
  function control(action, ...args) {
    const argv = [...base, python, remote + '/control.py', action, home, ...args.map(String)];
    const result = spawnSync(wsl, argv, {encoding: 'utf8', windowsHide: true, maxBuffer: 4 * 1024 * 1024});
    emit('observation', {argv, exit: result.status, stdout: result.stdout, stderr: result.stderr});
    assert.equal(result.status, 0); return JSON.parse(result.stdout);
  }
  const errorInfo = error => ({code: error.code, message: error.message, stdout: error.stdout, stderr: error.stderr, postgresExecution: error.postgresExecution});
  function outcome(error, sent) {error.postgresExecution = sent ? 'unknown' : 'not-submitted'; return error;}
  function target(method) {
    if (['fragmented'].includes(activeFault)) return activeFault;
    if (activeFault.startsWith('initdb-') && method === 'run:initdb') return activeFault === 'initdb-eof' ? 'operation-eof' : activeFault === 'initdb-ready-eof' ? 'ready-eof' : activeFault;
    if (activeFault.startsWith('stop-') && method === 'run:pg_ctl:stop') return activeFault === 'stop-reply-loss' ? 'reply-loss' : activeFault === 'stop-ready-eof' ? 'ready-eof' : activeFault;
    if (activeFault.startsWith('remove-') && method === 'removeDataDirectory') return activeFault === 'remove-reply-loss' ? 'reply-loss' : activeFault === 'remove-ready-eof' ? 'ready-eof' : activeFault;
    if (method === 'run:pg_ctl:start') return activeFault;
    return 'none';
  }
  function validateResult(method, frame) {
    schema(keys(frame, frame.ok ? ['type', 'ok', 'value'] : ['type', 'ok', 'error']) && typeof frame.ok === 'boolean', 'result envelope');
    if (!frame.ok) {
      const error = frame.error;
      schema(error && (text(error.code) || Number.isInteger(error.code)) && text(error.message) && ['settled', 'unknown', 'not-submitted'].includes(error.execution), 'typed error');
      schema(Object.keys(error).every(key => ['code', 'message', 'execution', 'stdout', 'stderr'].includes(key)), 'error extra field');
      if ('stdout' in error) schema(text(error.stdout) && text(error.stderr), 'error output');
      return;
    }
    const value = frame.value;
    if (method.startsWith('run:')) schema(keys(value, ['stdout', 'stderr']) && text(value.stdout) && text(value.stderr), 'run value');
    else if (['prepareLayout', 'removeDataDirectory'].includes(method)) schema(value === null, 'void value');
    else if (method === 'readVersion') schema(text(value), 'version value');
    else if (method === 'readInstance') schema(keys(value, ['directory', 'socket']) && text(value.directory) && (text(value.socket) || value.socket === null), 'instance value');
    else if (method === 'socketMode') schema(Number.isInteger(value), 'mode value');
    else if (method === 'resetPathsMatch') schema(typeof value === 'boolean', 'paths value');
    else if (method === 'dataDirectoryInfo') schema(keys(value, ['uid', 'currentUserUid', 'isDirectory']) && Number.isInteger(value.uid) && Number.isInteger(value.currentUserUid) && typeof value.isDirectory === 'boolean', 'data value');
    else throw new Error('unknown method');
  }
  
  async function call(method, payload = {}, override) {
    if (method === 'run:pg_ctl:start') lastStartArgs = payload.args;
    if (method === 'removeDataDirectory' && activeFault === 'concurrent-start' && !injected) {
      injected = true;
      await call('run:pg_ctl:start', {args: lastStartArgs}, 'none');
      emit('concurrent-start-complete', {state: control('state')});
    }
    const fault = override ?? target(method), id = ++sequence;
    const argv = [...base, python, remote + '/helper.py', repo, home, method, JSON.stringify({fault, moved})];
    const child = spawn(wsl, argv, {shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']});
    emit('carrier-spawn', {id, method, fault, carrierPid: child.pid, argv});
    let phase = 'ready', sent = false, result, pending = '', protocolError, bytes = 0, chunks = 0, leadingTailSplits = 0, stderr = '', stderrBytes = 0;
    let chain = Promise.resolve(), handledGate = false;
    const decoder = new TextDecoder('utf-8', {fatal: true});
    const bad = error => {
      if (!protocolError) {protocolError = error; emit('protocol-rejected', {id, method, reason: error.message, sent});}
      if (!sent && !child.stdin.writableEnded) child.stdin.end();
    };
    child.stdin.on('error', error => {emit('stdin-error', {id, code: error.code}); if (!sent) bad(error);});
    child.stderr.on('data', chunk => {
      stderrBytes += chunk.length;
      // Same finite-exec resource boundary as existing PostgresOperations execFile maxBuffer.
      if (stderrBytes <= 4 * 1024 * 1024) stderr += chunk.toString('utf8'); else bad(new Error('stderr maxBuffer'));
    });
    async function receive(frame) {
      if (protocolError) {emit('discarded-frame', {id, method, frame}); return;}
      schema(frame && typeof frame === 'object' && !Array.isArray(frame) && text(frame.type), 'frame object');
      schema(phase !== 'complete', 'frame after result');
      emit('helper-frame', {id, method, frame});
      if (frame.type === 'ready') {
        schema(phase === 'ready' && !sent && keys(frame, ['type', 'method', 'pid', 'start_ticks']) && frame.method === method && Number.isInteger(frame.pid) && text(frame.start_ticks), 'ready unique/method/schema');
        phase = 'active';
        let data = Buffer.from(JSON.stringify({operation: method, ...payload}) + '\n');
        if (fault === 'duplicate-request') data = Buffer.concat([data, data]);
        if (fault === 'malformed-request') data = Buffer.from('{"operation":broken}\n');
        if (fault === 'partial-request') data = data.subarray(0, data.length - 1);
        if (fault === 'ready-eof') {child.stdin.end(); emit('request-eof-before-operation', {id}); return;}
        sent = true; emit('operation-written', {id, method, requestBytes: data.length});
        if (fault === 'fragmented') {
          for (const byte of data) {child.stdin.write(Buffer.from([byte])); await new Promise(resolve => setImmediate(resolve));}
          emit('request-fragmented', {id, writes: data.length}); child.stdin.end();
        } else child.stdin.end(data);
        return;
      }
      if (frame.type === 'result') {
        validateResult(method, frame);
        schema(sent || (!frame.ok && frame.error.execution === 'not-submitted'), 'unsolicited success');
        result = frame; phase = 'complete'; return;
      }
      if (frame.type === 'no-operation') {schema(!sent && keys(frame, ['type', 'stage']) && ['before-ready', 'after-ready'].includes(frame.stage), 'no-operation schema'); phase = 'complete'; return;}
      schema(phase === 'active' && sent, 'progress before operation');
      if (frame.type === 'accepted') schema(keys(frame, ['type', 'method']) && frame.method === method, 'accepted schema');
      else if (frame.type === 'launched') {
        schema(keys(frame, ['type', 'method', 'child']) && frame.method === method && Number.isInteger(frame.child.pid), 'launched schema');
        if (!handledGate && ['initdb-death', 'stop-death', 'readiness-fail'].includes(fault)) {
          handledGate = true;
          const gate = control('waitgate', 'gate'); emit('at-physical-gate', {id, gate});
          if (fault === 'readiness-fail') {
            const query = control('wait-query'); assert.equal(query.exit, 0);
            emit('readiness-failed-but-server-ready', {id, query});
          } else {
            const ready = events.find(row => row.event === 'helper-frame' && row.id === id && row.frame.type === 'ready').frame;
            control('signal', ready.pid, ready.start_ticks, 'SIGKILL');
            emit('after-helper-death', {id, method, state: control('state')});
          }
          control('resume-pg');
        }
      } else if (frame.type === 'settled') schema(keys(frame, ['type', 'method', 'result', 'readback']) && frame.method === method && physical(frame.result) && physical(frame.readback), 'settled schema');
      else if (frame.type === 'deleted') schema(keys(frame, ['type', 'kernel_held', 'pg_held', 'lock_inode']) && frame.kernel_held && frame.pg_held && Number.isInteger(frame.lock_inode), 'deleted schema');
      else if (frame.type === 'gate') {
        schema(keys(frame, ['type', 'stage', 'pid', 'start_ticks']) && Number.isInteger(frame.pid) && text(frame.start_ticks), 'gate schema');
        control('waitgate', 'helper-gate');
        if (['before-delete', 'during-delete'].includes(frame.stage)) {
          control('signal', frame.pid, frame.start_ticks, 'SIGKILL');
          emit('after-helper-death', {id, method, state: control('state')});
        } else {
          schema(frame.stage === 'before-result', 'gate stage');
          bad(new Error('result read end deliberately closed')); child.stdout.destroy();
          control('signal', frame.pid, frame.start_ticks, 'SIGCONT');
        }
      } else throw new Error('unexpected frame type ' + frame.type);
    }
    if (fault === 'early-request') {
      sent = true; phase = 'active';
      const data = Buffer.from(JSON.stringify({operation: method, ...payload}) + '\n');
      child.stdin.end(data); emit('operation-written-before-ready', {id, method, requestBytes: data.length});
    }
    child.stdout.on('data', chunk => {
      bytes += chunk.length; chunks += 1;
      if (chunk.length && chunk[chunk.length - 1] >= 0xc2 && chunk[chunk.length - 1] <= 0xf4) leadingTailSplits += 1;
      if (bytes > 4 * 1024 * 1024) {bad(new Error('stdout maxBuffer')); return;}
      if (protocolError) return;
      try {
        pending += decoder.decode(chunk, {stream: true});
        let index;
        while ((index = pending.indexOf('\n')) >= 0) {
          const line = pending.slice(0, index); pending = pending.slice(index + 1);
          chain = chain.then(() => receive(JSON.parse(line))).catch(bad);
        }
      } catch (error) {bad(error);}
    });
    child.stdout.on('end', () => {try {pending += decoder.decode();} catch (error) {bad(error);}});
    return new Promise((resolve, reject) => {
      child.once('error', error => reject(outcome(error, sent)));
      child.once('close', async (code, signal) => {
        await chain;
        emit('carrier-close', {id, method, code, signal, sent, bytes, chunks, leadingTailSplits, pendingBytes: Buffer.byteLength(pending), stderr, protocolError: protocolError?.message});
        if (code !== 0 || protocolError || pending.length || !result) {
          const error = new Error(protocolError?.message || 'finite result incomplete'); error.code = sent ? 'PG_OUTCOME_UNKNOWN' : 'PG_NOT_EXECUTED';
          reject(outcome(error, sent)); return;
        }
        if (!result.ok) {
          const error = Object.assign(new Error(result.error.message), result.error);
          if (error.execution !== 'settled') error.postgresExecution = error.execution;
          reject(error); return;
        }
        resolve(result.value);
      });
    });
  }
  const operations = {
    directory: home + '/desktop/postgres', socket: home + '/desktop/socket', logPath: home + '/desktop/postgres.log',
    socketAddressBytes: Buffer.byteLength(home + '/desktop/socket/.s.PGSQL.5432'),
    prepareLayout: () => call('prepareLayout'), readVersion: () => call('readVersion'),
    readInstance: form => call('readInstance', {form}), socketMode: () => call('socketMode'),
    resetPathsMatch: () => call('resetPathsMatch'), dataDirectoryInfo: () => call('dataDirectoryInfo'),
    run: (binary, args) => call(binary === 'initdb' ? 'run:initdb' : 'run:pg_ctl:' + args[0], {args}),
    removeDataDirectory: () => call('removeDataDirectory'),
  };
  return {operations, call, control, events, emit,
    setFault(value) {activeFault = value;},
    setMoved(value) {moved = value;}};
}
module.exports = {createClient};
