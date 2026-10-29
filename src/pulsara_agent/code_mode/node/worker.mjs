import { parentPort, workerData } from 'node:worker_threads';
import { readFile } from 'node:fs/promises';
import { performance } from 'node:perf_hooks';
import { QuickJS } from '../../_vendor/code_mode/quickjs-wasi/dist/index.js';

const {limits, cellBuffer, hostBuffer, timingBuffer, toolNames} = workerData;
const cell = new Int32Array(cellBuffer, 8), used = new BigInt64Array(cellBuffer, 0, 1);
const host = new BigInt64Array(hostBuffer);
const timing = new Float64Array(timingBuffer);
const diagnosticBytes = Math.min(4096, limits.control_bytes - limits.page_bytes);
let source = workerData.source; delete workerData.source;
const pending = new Map(), unhandled = new Map(), preparationCharges = new Map();
let vm, root, helper, deferredFactory, parse, render, diagnose, errorFactory;
let active = false, segmentStart = 0, segmentSpent = 0, segmentSequence = 0;
let returnRendered = false, returnPresent = false, returnText, returnCharge = 0;
let accepting = true;
let tripped = false, ended = false, requestId = 0n, sequence = 0n, awaitingResume = null;
const owned = [];
const send = message => parentPort.postMessage(message);
const wireBytes = message => Buffer.byteLength(JSON.stringify(message), 'utf8') + 1;
function reserve(bytes) {
  const amount = BigInt(bytes);
  const local = Atomics.add(used, 0, amount) + amount;
  if (local > BigInt(limits.cell_bytes)) {
    Atomics.sub(used, 0, amount);
    throw new RangeError('Code Mode cell data boundary exceeded');
  }
  const total = Atomics.add(host, 0, amount) + amount;
  if (total > BigInt(limits.host_bytes)) {
    Atomics.sub(host, 0, amount); Atomics.sub(used, 0, amount);
    throw new RangeError('Code Mode Host data boundary exceeded');
  }
}
function release(bytes) {
  const amount = BigInt(bytes);
  Atomics.sub(used, 0, amount); Atomics.sub(host, 0, amount);
}
function emit(kind, text) {
  if(ended || Atomics.load(cell, 1)) throw new Error('Code Mode cell has stopped');
  const message = {type: 'output', record: {kind, text}};
  if (Buffer.byteLength(text, 'utf8') > limits.value_bytes)
    throw new RangeError('Code Mode output value boundary exceeded');
  const charge = wireBytes(message) + 256;
  reserve(charge);
  send({...message, charge});
}
function segment(fn, continuation = false) {
  if (active) return fn(); // synchronous host re-entry shares this segment
  if (!continuation) segmentSpent = 0;
  // Every active slice has a distinct watchdog tag, including a resumed slice
  // sharing the same computation budget after an external delivery callback.
  segmentSequence = (segmentSequence + 1) | 0;
  if (!segmentSequence) segmentSequence = 1;
  segmentStart = performance.now(); active = true;
  timing[0] = segmentStart; timing[1] = segmentSpent;
  Atomics.store(cell, 0, segmentSequence);
  send({type: 'segment', sequence: segmentSequence});
  try {
    const value = fn();
    if (performance.now() - segmentStart + segmentSpent >= limits.segment_ms) {
      tripped = true;
      throw new RangeError('Code Mode continuous computation boundary exceeded');
    }
    return value;
  } finally {
    segmentSpent += performance.now() - segmentStart;
    active = false; Atomics.store(cell, 0, 0);
    send({type:'segment_end',sequence:segmentSequence});
  }
}
function clippedError(value) {
  // Control diagnostics have reserved bounded space even if ordinary output is full.
  const bytes = Buffer.from(JSON.stringify(value), 'utf8');
  if (bytes.length <= diagnosticBytes - 512) return value;
  return {name: String(value?.name ?? 'Error').slice(0, 80),
    message: String(value?.message ?? 'JavaScript execution failed')
      .slice(0, Math.max(1, Math.floor((diagnosticBytes - 1024) / 4))),
    ...(value?.code ? {code: value.code} : {}),
    ...(value?.state ? {state: value.state} : {}),
    ...(value?.result_id ? {result_id: value.result_id} : {}),
    ...(value?.result_entry_id ? {result_entry_id: value.result_entry_id} : {}),
    ...(value?.artifact ? {artifact: value.artifact} : {}),
    diagnostic_truncated: true};
}
function fail(error) {
  if (ended) return;
  accepting = false;
  let diagnostic;
  try {
    // Reading SDK exception properties may execute guest getters: stay timed.
    diagnostic = segment(() => ({name: error.name || 'Error', message: error.message || String(error),
      ...(error.stack ? {stack: error.stack} : {})}), true);
  } catch { diagnostic = {name: 'Error', message: tripped
    ? 'Code Mode continuous computation boundary exceeded' : 'JavaScript execution failed'}; }
  error.dispose?.();
  finish('failed', clippedError(diagnostic));
}
function finish(state, error) {
  if (ended) return;
  ended = true;
  if(returnCharge) {release(returnCharge);returnCharge=0;returnText=null;}
  send({type: 'terminal', state, ...(error ? {error} : {})});
  for (const record of unhandled.values()) { record.promise.dispose(); record.reason.dispose(); }
  unhandled.clear();
  for (const record of pending.values()) disposeDeferred(record);
  pending.clear();
  root?.dispose();
  for (const handle of owned) if (!handle.disposed) handle.dispose();
  vm?.dispose();
  parentPort.close();
}
function disposeDeferred(record) {
  record.promise.dispose(); record.resolve.dispose(); record.reject.dispose();
}
function newRequest(type, name, json, preparation) {
  if (root?.promiseState === 2) accepting = false;
  if (!accepting || ended || tripped || Atomics.load(cell, 1)) throw new Error('Code Mode cell has stopped');
  if (Buffer.byteLength(json, 'utf8') > limits.value_bytes)
    throw new RangeError('Code Mode bridge value boundary exceeded');
  const id = String(++requestId);
  const message = {type, request_id: id, ...(type === 'call' ? {sequence: String(++sequence), tool_name: name} : {}),
    arguments_json: json, ...(preparation === null ? {} : {preparation})};
  // A pending call also reserves its eventual small accepted-result locator.
  // The same allowance transfers to a late output if the VM disappears.
  const charge = wireBytes(message) + 1024;
  reserve(charge);
  let record;
  try {
    const value = vm.callFunction(deferredFactory, vm.undefined);
    try { record = {promise: value.getProp('promise'), resolve: value.getProp('resolve'),
      reject: value.getProp('reject'), charge, type}; } finally { value.dispose(); }
    pending.set(id, record);
    send({...message, charge});
    // The SDK disposes host-function returned handles. Keep our three owned handles.
    return record.promise.dup();
  } catch (error) { release(charge); throw error; }
}
async function checkCompletion() {
  if (ended || awaitingResume !== null) return;
  try {
    if (root.promiseState === 2 || unhandled.size) {
      accepting = false;
      let reason;
      if (root.promiseState === 2) {
        const result = await vm.resolvePromise(root);
        reason = result.error;
      } else reason = unhandled.values().next().value.reason.dup();
      try {
        const error = segment(() => JSON.parse(vm.callFunction(diagnose, vm.undefined, reason)
          .consume(handle => handle.toString())), true);
        reason.dispose(); reason = null;
        finish('failed', clippedError(error));
      } finally { reason?.dispose(); }
      return;
    }
    if (pending.size) return;
    if (root.promiseState === 0) {
      finish('failed', {name: 'Error', code: 'stalled',
        message: 'Root Promise has no pending jobs or external progress source'});
      return;
    }
    if (!returnRendered) {
      const result = await vm.resolvePromise(root);
      try {
        segment(() => {
          returnRendered = true;
          returnPresent = !result.value.isUndefined;
          if (returnPresent) {
            const text = vm.callFunction(render, vm.undefined, result.value);
            try { returnText = text.toString(); } finally { text.dispose(); }
            returnCharge = wireBytes({type:'output',record:{kind:'return',text:returnText}}) + 256;
            reserve(returnCharge);
          }
          vm.executePendingJobs();
        }, true);
      } finally { result.value.dispose(); }
      if (pending.size) return;
      if (unhandled.size) return checkCompletion();
    }
    if (returnPresent) {
      send({type:'output', record:{kind:'return',text:returnText}, charge:returnCharge});
      returnCharge = 0; returnText = null;
    }
    finish('completed');
  } catch (error) { fail(error); }
}

try {
  const wasm = await readFile(new URL('../../_vendor/code_mode/quickjs-wasi/quickjs.wasm', import.meta.url));
  vm = await QuickJS.create({wasm, memoryLimit: limits.heap_bytes, maxStackSize: limits.stack_bytes,
    interruptHandler: () => {
      tripped = tripped || !!Atomics.load(cell, 1)
        || (active && performance.now() - segmentStart + segmentSpent >= limits.segment_ms);
      return tripped;
    },
    onUnhandledRejection: (promise, reason, handled) => {
      const id = promise.identity, record = unhandled.get(id);
      if (handled) {
        if (record) { unhandled.delete(id); record.promise.dispose(); record.reason.dispose(); }
      } else if (!record) unhandled.set(id, {promise: promise.dup(), reason: reason.dup()});
    },
  });
  const guest = await readFile(new URL('./guest.js', import.meta.url), 'utf8');
  segment(() => {
    const invoke = vm.newFunction('pulsaraInvoke', (name, json, preparation) =>
      newRequest('call', name.toString(), json.toString(), preparation.isNull ? null : preparation.toString()));
    const prepare = vm.newFunction('pulsaraPrepare', json => newRequest('prepare', '', json.toString(), null));
    const discard = vm.newFunction('pulsaraDiscard', id => {
      const key = id.toString();
      const charge = preparationCharges.get(key);
      if (charge !== undefined) { release(charge); preparationCharges.delete(key); }
      send({type: 'discard_preparation', preparation: key}); return vm.undefined;
    });
    const output = vm.newFunction('pulsaraOutput', (kind, text) => {
      emit(kind.toString(), text.toString()); return vm.undefined;
    });
    const names = vm.hostToHandle(toolNames);
    const valueLimit = vm.newNumber(limits.value_bytes);
    const diagnosticChars = vm.newNumber(Math.max(1, Math.floor((diagnosticBytes - 1024) / 4)));
    const install = vm.evalCode(guest, 'pulsara-guest.js');
    try { helper = vm.callFunction(install, vm.undefined, invoke, prepare, discard, output, names, valueLimit, diagnosticChars); }
    finally { install.dispose(); names.dispose(); valueLimit.dispose(); diagnosticChars.dispose(); invoke.dispose(); prepare.dispose(); discard.dispose(); output.dispose(); }
    owned.push(helper);
    for (const [key, assign] of [
      ['deferred', h => deferredFactory = h], ['parse', h => parse = h], ['render', h => render = h],
      ['diagnose', h => diagnose = h], ['error', h => errorFactory = h],
    ]) { const handle = helper.getProp(key); owned.push(handle); assign(handle); }
    root = vm.evalCode(`(async function() {\n${source}\n})()`, 'cell.js');
    source = null;
    vm.markPromiseHandled(root);
    vm.executePendingJobs();
  });
  send({type: 'ready'});
  await checkCompletion();
} catch (error) { fail(error); }

parentPort.on('message', async message => {
  if (ended) return;
  try {
    if (message.type === 'result') {
      if (awaitingResume !== null) throw new Error('Overlapping Code Mode result delivery');
      const record = pending.get(message.request_id);
      if (!record) throw new Error('Unknown Code Mode result request');
      segment(() => {
        const json = vm.newString(message.json);
        let value;
        try { value = vm.callFunction(parse, vm.undefined, json); } finally { json.dispose(); }
        try {
          if (message.error) {
            const error = vm.callFunction(errorFactory, vm.undefined, value);
            try { vm.callFunction(record.reject, vm.undefined, error).dispose(); } finally { error.dispose(); }
          } else vm.callFunction(record.resolve, vm.undefined, value).dispose();
        } finally { value.dispose(); }
      });
      awaitingResume = message.request_id;
      pending.delete(message.request_id); disposeDeferred(record);
      if (record.type === 'prepare') preparationCharges.set(message.request_id, record.charge);
      else release(record.charge);
      // Promise is settled; continuations wait until the existing delivery owner commits.
      send({type: 'delivered', request_id: message.request_id});
    } else if (message.type === 'resume') {
      if (awaitingResume !== message.request_id) throw new Error('Mismatched Code Mode continuation');
      awaitingResume = null;
      segment(() => vm.executePendingJobs(), true);
      await checkCompletion();
    }
  } catch (error) { fail(error); }
});
