import path from 'node:path';
import type {HostOperations} from './host-operations';
import type {DesktopResourcePaths} from './resource-paths';
import type {PostgresCommandError, PostgresOperations} from './postgres-operations';

const OUTPUT_LIMIT = 4 * 1024 * 1024;
type Reply = {type: 'result'; ok: true; value: unknown} | {
  type: 'result'; ok: false;
  error: {code: string | number; message: string; execution: PostgresCommandError['postgresExecution']; stdout: string; stderr: string};
};
function keys(value: unknown, expected: string[]): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).sort().join(',') === expected.sort().join(',');
}
function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
function failure(error: unknown, submitted: boolean): PostgresCommandError {
  const original = error instanceof Error ? error : new Error(String(error));
  return Object.assign(original, {postgresExecution: submitted ? 'unknown' as const : 'not-submitted' as const});
}
function validateReply(method: string, value: unknown): asserts value is Reply {
  check(value !== null && typeof value === 'object', 'Invalid PostgreSQL result');
  const frame = value as Reply;
  check(keys(frame, frame.ok ? ['type', 'ok', 'value'] : ['type', 'ok', 'error']) && frame.type === 'result' && typeof frame.ok === 'boolean', 'Invalid PostgreSQL result envelope');
  if (!frame.ok) {
    const error = frame.error;
    check(keys(error, ['code', 'message', 'execution', 'stdout', 'stderr'])
      && (typeof error.code === 'string' || Number.isInteger(error.code))
      && typeof error.message === 'string' && typeof error.stdout === 'string' && typeof error.stderr === 'string'
      && ['not-submitted', 'unknown', 'failed'].includes(error.execution), 'Invalid PostgreSQL error');
    check(error.execution !== 'failed' || (typeof error.code === 'number' && Number.isInteger(error.code) && error.code > 0), 'Invalid settled PostgreSQL exit');
    return;
  }
  const result = frame.value;
  if (method.startsWith('run:')) check(keys(result, ['stdout', 'stderr']) && typeof result.stdout === 'string' && typeof result.stderr === 'string', 'Invalid PostgreSQL command output');
  else if (['prepareLayout', 'removeDataDirectory', 'maintenanceCheck'].includes(method)) check(result === null, 'Invalid PostgreSQL void result');
  else if (method === 'readVersion') check(typeof result === 'string', 'Invalid PostgreSQL version');
  else if (method === 'socketMode') check(Number.isInteger(result), 'Invalid PostgreSQL socket mode');
  else if (method === 'resetPathsMatch') check(typeof result === 'boolean', 'Invalid PostgreSQL path result');
  else if (method === 'readInstance') check(keys(result, ['directory', 'socket']) && [result.directory, result.socket].every(item => item === null || typeof item === 'string'), 'Invalid PostgreSQL instance');
  else if (method === 'dataDirectoryInfo') check(keys(result, ['uid', 'currentUserUid', 'isDirectory']) && Number.isInteger(result.uid) && Number.isInteger(result.currentUserUid) && typeof result.isDirectory === 'boolean', 'Invalid PostgreSQL directory observation');
  else throw new Error('Unknown PostgreSQL operation');
}

/** Fixed guest facts only; the existing BundledPostgres remains the lifecycle owner. */
function postgresClient(
  resources: DesktopResourcePaths, home: string, host: Pick<HostOperations, 'spawn'>,
  currentSignal: () => AbortSignal | undefined = () => undefined,
) {
  if (!path.posix.isAbsolute(home) || path.posix.normalize(home) !== home || home.includes('\0')) throw new Error('Invalid Linux PostgreSQL home');

  async function call(method: string, payload: Record<string, unknown> = {}): Promise<unknown> {
    const signal = currentSignal();
    if (signal?.aborted) throw failure(new Error('PostgreSQL operation was cancelled before submission'), false);
    let child;
    try {
      child = host.spawn(resources.pythonExecutable, [
        '-m', 'pulsara_agent.desktop_guest_ops', '--postgres-bin', resources.postgresBin, '--method', method,
      ], {env: {PULSARA_HOME: home}, cwd: home});
    } catch (error) {
      throw failure(error, false);
    }
    return new Promise((resolve, reject) => {
      let submitted = false, ready = false, result: Reply | undefined, invalid: Error | undefined;
      let pending = '', stdoutBytes = 0, stderrBytes = 0;
      const stderr: Buffer[] = [];
      const decoder = new TextDecoder('utf-8', {fatal: true});
      const closeInput = () => { if (!child.stdin.writableEnded && !child.stdin.destroyed) child.stdin.end(); };
      const fail = (error: unknown) => {
        invalid ??= error instanceof Error ? error : new Error(String(error));
        closeInput();
      };
      const abort = () => fail(new Error('PostgreSQL operation was cancelled; its physical result must settle'));
      signal?.addEventListener('abort', abort, {once: true});
      child.once('error', fail);
      child.stdin.on('error', fail);
      child.stdout.on('error', fail);
      child.stderr.on('error', fail);
      if (signal?.aborted) abort();
      child.stderr.on('data', (chunk: Buffer) => {
        stderrBytes += chunk.length;
        if (stderrBytes <= OUTPUT_LIMIT) stderr.push(chunk);
        else fail(new Error('PostgreSQL stderr exceeded the finite output boundary'));
      });
      child.stdout.on('data', (chunk: Buffer) => {
        stdoutBytes += chunk.length;
        if (stdoutBytes > OUTPUT_LIMIT) { fail(new Error('PostgreSQL reply exceeded the finite output boundary')); return; }
        if (invalid) return;
        try {
          pending += decoder.decode(chunk, {stream: true});
          let newline: number;
          while ((newline = pending.indexOf('\n')) >= 0) {
            const line = pending.slice(0, newline);
            pending = pending.slice(newline + 1);
            const frame = JSON.parse(line);
            check(!result, 'PostgreSQL frame after result');
            if (frame?.type === 'ready') {
              check(keys(frame, ['type', 'method']) && frame.method === method && !ready, 'Invalid or duplicate PostgreSQL ready');
              ready = true;
              // No await between the last admission check and the irreversible write.
              if (invalid || signal?.aborted || child.stdin.destroyed || child.stdin.writableEnded) { abort(); continue; }
              submitted = true;
              child.stdin.end(Buffer.from(JSON.stringify({operation: method, ...payload}) + '\n', 'utf8'));
            } else {
              validateReply(method, frame);
              check(submitted || (!frame.ok && frame.error.execution === 'not-submitted'), 'Unsolicited PostgreSQL result');
              result = frame;
            }
          }
        } catch (error) { fail(error); }
      });
      child.once('close', (code, signalName) => {
        signal?.removeEventListener('abort', abort);
        try {
          pending += decoder.decode();
          if (invalid) throw invalid;
          check(code === 0 && signalName === null && pending === '' && result !== undefined, `PostgreSQL helper result is incomplete (${code ?? signalName})`);
          if (!result.ok) {
            const error = Object.assign(new Error(result.error.message), {
              code: result.error.code, stdout: result.error.stdout, stderr: result.error.stderr,
              postgresExecution: result.error.execution,
            });
            reject(error);
            return;
          }
          resolve(result.value);
        } catch (error) {
          reject(Object.assign(failure(error, submitted), {stderr: Buffer.concat(stderr).toString('utf8')}));
        }
      });
    });
  }

  return call;
}

export function createWslPostgresOperations(
  resources: DesktopResourcePaths, home: string, host: Pick<HostOperations, 'spawn'>,
  currentSignal: () => AbortSignal | undefined = () => undefined,
): PostgresOperations {
  const call = postgresClient(resources, home, host, currentSignal);
  const directory = path.posix.join(home, 'desktop', 'postgres');
  const socket = path.posix.join(home, 'desktop', 'socket');
  return {
    directory, socket, logPath: path.posix.join(home, 'desktop', 'postgres.log'),
    socketAddressBytes: Buffer.byteLength(path.posix.join(socket, '.s.PGSQL.5432')),
    async prepareLayout() { await call('prepareLayout'); },
    async readVersion() { return await call('readVersion') as string; },
    async readInstance(form) {
      const value = await call('readInstance', {form}) as {directory: string | null; socket: string | null};
      return {directory: value.directory ?? undefined, socket: value.socket ?? undefined};
    },
    async socketMode() { return await call('socketMode') as number; },
    async resetPathsMatch() { return await call('resetPathsMatch') as boolean; },
    async dataDirectoryInfo() { return await call('dataDirectoryInfo') as Awaited<ReturnType<PostgresOperations['dataDirectoryInfo']>>; },
    async run(binary, args) {
      if (binary !== 'initdb' && (binary !== 'pg_ctl' || !['start', 'status', 'stop'].includes(args[0]))) throw failure(new Error('Unsupported PostgreSQL command'), false);
      return await call(binary === 'initdb' ? 'run:initdb' : `run:pg_ctl:${args[0]}`, {args}) as {stdout: string; stderr: string};
    },
    async removeDataDirectory() { await call('removeDataDirectory'); },
  };
}

/** Installer admission observes this instant; it never stops or changes a database. */
export async function checkWslMaintenance(resources: DesktopResourcePaths, home: string, host: Pick<HostOperations, 'spawn'>): Promise<void> {
  await postgresClient(resources, home, host)('maintenanceCheck');
}
