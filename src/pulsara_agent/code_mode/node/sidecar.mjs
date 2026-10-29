// Only framed JSON goes to stdout. Each cell owns a separate Worker and QuickJS VM.
import { Worker } from 'node:worker_threads';
import { performance } from 'node:perf_hooks';

let limits;
const cells = new Map(), queue = [];
let hostBuffer, host, active = 0, input = Buffer.alloc(0), closing = false;
const send = message => process.stdout.write(JSON.stringify(message) + '\n');
function release(cell, bytes) {
  Atomics.sub(cell.usage, 0, BigInt(bytes)); Atomics.sub(host, 0, BigInt(bytes));
}
function reserve(cell, bytes) {
  const amount = BigInt(bytes);
  if (Atomics.add(cell.usage, 0, amount) + amount > BigInt(limits.cell_bytes)) {
    Atomics.sub(cell.usage, 0, amount); return false;
  }
  if (Atomics.add(host, 0, amount) + amount > BigInt(limits.host_bytes)) {
    Atomics.sub(host, 0, amount); Atomics.sub(cell.usage, 0, amount); return false;
  }
  return true;
}
function terminal(cell, state, error) {
  if (cell.terminal) return;
  cell.terminal = true;
  Atomics.store(cell.flags, 1, 1);
  send({type: 'terminal', cell_id: cell.id, state, ...(error ? {error} : {})});
  if (!cell.worker) {
    const index = queue.indexOf(cell);
    if (index >= 0) queue.splice(index, 1);
    if (cell.sourceCharge) { release(cell, cell.sourceCharge); cell.sourceCharge = 0; }
    delete cell.start;
    send({type: 'vm_released', cell_id: cell.id});
  } else cell.worker.terminate().catch(() => {});
}
function schedule() {
  while (!closing && queue.length && active < limits.vm_slots) {
    const cell = queue.shift();
    if (cell.terminal) continue;
    active++;
    const worker = new Worker(new URL('./worker.mjs', import.meta.url), {
      workerData: {...cell.start, limits, cellBuffer: cell.buffer,
        timingBuffer: cell.timingBuffer, hostBuffer},
      // Guest has no Node API. Worker stdout/stderr are never the IPC channel.
      stdout: true, stderr: true,
    });
    cell.worker = worker;
    delete cell.start;
    // SDK/WASI diagnostics must not corrupt protocol or grow an unbounded collector.
    worker.stdout.resume(); worker.stderr.resume();
    send({type: 'running', cell_id: cell.id});
    worker.on('message', message => {
      if (message.type === 'segment') {
        for (const previous of cell.timers) clearTimeout(previous);
        cell.timers.clear();
        const sequence = message.sequence;
        cell.activeSequence = sequence;
        const elapsed = performance.now() - cell.timing[0] + cell.timing[1];
        const timer = setTimeout(() => {
          cell.timers.delete(timer);
          if (!cell.terminal && Atomics.load(cell.flags, 0) === sequence)
            terminal(cell, 'failed', {name: 'ResourceError', code: 'watchdog',
              message: 'Code Mode active VM segment exceeded the external watchdog'});
        }, Math.max(0, limits.watchdog_ms - elapsed));
        timer.unref(); cell.timers.add(timer);
      } else if (message.type === 'segment_end') {
        if (cell.activeSequence === message.sequence) {
          for (const timer of cell.timers) clearTimeout(timer);
          cell.timers.clear();
        }
      } else if (message.type === 'ready') {
        if (cell.sourceCharge) { release(cell, cell.sourceCharge); cell.sourceCharge = 0; }
      } else if (message.type === 'terminal') {
        terminal(cell, message.state, message.error);
      } else if (!cell.terminal || ['output','call','prepare','delivered','discard_preparation'].includes(message.type)) {
        if (message.type === 'discard_preparation') {
          const charge = cell.preparations.get(message.preparation);
          if (charge !== undefined) { release(cell, charge); cell.preparations.delete(message.preparation); }
        }
        send({...message, cell_id: cell.id});
      }
    });
    worker.on('error', error => terminal(cell, 'failed', {name: error.name,
      message: String(error.message).slice(0, Math.min(4096, limits.control_bytes - limits.page_bytes) - 512)}));
    worker.on('exit', code => {
      active--; cell.worker = null;
      for (const timer of cell.timers) clearTimeout(timer);
      cell.timers.clear();
      if (!cell.terminal) terminal(cell, 'failed', {name: 'WorkerError',
        message: `Code Mode Worker exited without terminal settlement (${code})`});
      if (cell.sourceCharge) { release(cell, cell.sourceCharge); cell.sourceCharge = 0; }
      send({type: 'vm_released', cell_id: cell.id});
      schedule();
    });
  }
}
function command(message) {
  if (message.type === 'configure') {
    if (limits) throw new Error('Code Mode sidecar already configured');
    limits = message.limits;
    hostBuffer = new SharedArrayBuffer(8); host = new BigInt64Array(hostBuffer);
    send({type: 'configured'}); return;
  }
  if (!limits) throw new Error('Unconfigured Code Mode sidecar');
  if (message.type === 'start') {
    if (closing || cells.has(message.cell_id)) throw new Error('Invalid Code Mode start');
    const buffer = new SharedArrayBuffer(16), timingBuffer = new SharedArrayBuffer(16);
    const cell = {id: message.cell_id, buffer, usage: new BigInt64Array(buffer, 0, 1), flags: new Int32Array(buffer, 8), timingBuffer,
      timing: new Float64Array(timingBuffer), timers: new Set(), worker: null, terminal: false,
      start: {source: message.source, toolNames: message.tool_names}, sourceCharge: 0, preparations: new Map()};
    const sourceCharge = Buffer.byteLength(JSON.stringify(message), 'utf8') + 256;
    if (!reserve(cell, sourceCharge + limits.control_bytes + (message.catalog_bytes || 0))) {
      send({type: 'rejected', cell_id: cell.id, code: 'data_boundary',
        message: 'Code Mode current data boundary cannot admit this source'}); return;
    }
    cell.sourceCharge = sourceCharge;
    cells.set(cell.id, cell); queue.push(cell);
    send({type: 'admitted', cell_id: cell.id}); schedule(); return;
  }
  if (message.type === 'close') {
    closing = true;
    for (const cell of cells.values()) terminal(cell, 'terminated');
    send({type: 'closed'});
    return;
  }
  const cell = cells.get(message.cell_id);
  if (!cell) return;
  if (message.type === 'stop') terminal(cell, 'terminated');
  else if (message.type === 'fail') terminal(cell, 'failed', message.error);
  else if (message.type === 'release') release(cell, message.bytes);
  else if (message.type === 'retain') {
    const accepted = reserve(cell, message.bytes);
    send({type: 'retained', cell_id: cell.id, reservation: message.reservation, accepted});
  }
  else if (message.type === 'forget') {
    if (!cell.terminal || cell.worker) throw new Error('Cannot forget a live VM');
    release(cell, Atomics.load(cell.usage, 0)); cells.delete(cell.id);
  } else if (message.type === 'result' && cell.worker && !cell.terminal) {
    const charge = Buffer.byteLength(JSON.stringify(message), 'utf8') + 256;
    if (!reserve(cell, charge)) {
      terminal(cell, 'failed', {name: 'ResourceError', code: 'data_boundary',
        message: 'Code Mode result delivery exceeded the current data boundary'});
      return;
    }
    cell.worker.postMessage(message);
    // Result frame no longer stays in the sidecar; VM heap has its own limit.
    // Python and Worker retain this charge until the explicit delivered handshake.
    cell.resultCharge = charge;
  } else if (message.type === 'resume' && cell.worker && !cell.terminal) {
    if (cell.resultCharge) {
      if (message.keep_preparation) cell.preparations.set(message.request_id, cell.resultCharge);
      else release(cell, cell.resultCharge);
      cell.resultCharge = 0;
    }
    cell.worker.postMessage(message);
  }
}
process.stdin.on('data', chunk => {
  input = input.length ? Buffer.concat([input, chunk]) : chunk;
  const boundary = limits ? limits.cell_bytes + limits.control_bytes : 64 * 1024;
  while (true) {
    const end = input.indexOf(10);
    if (end < 0) break;
    if (end > boundary) throw new Error('Code Mode IPC frame exceeded its physical boundary');
    const line = input.subarray(0, end); input = input.subarray(end + 1);
    command(JSON.parse(line.toString('utf8')));
  }
  if (input.length > boundary) throw new Error('Code Mode IPC frame exceeded its physical boundary');
});
process.stdin.on('end', () => {
  closing = true;
  for (const cell of cells.values()) terminal(cell, 'terminated');
});
