import { execFile, execFileSync, spawn } from 'node:child_process';
import { appendFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import type { HostOperations } from './host-operations';

const execute = promisify(execFile);

/** Native macOS/Linux mechanisms; callers own admission, results and lifetimes. */
export const posixHostOperations: HostOperations = {
  resolveHome(pythonExecutable) {
    return execFileSync(pythonExecutable, ['-c', 'from pulsara_agent.capability.pulsara_home import require_pulsara_home; print(require_pulsara_home())'], { encoding: 'utf8' }).trim();
  },
  spawn(file, args, options) {
    return spawn(file, args, { ...options, stdio: ['pipe', 'pipe', 'pipe'] });
  },
  execFile(file, args, options) {
    return execute(file, args, options);
  },
  async prepareRuntimeDirectory(home) {
    await mkdir(path.join(home, 'desktop'), { recursive: true, mode: 0o700 });
  },
  async appendRuntimeLog(home, chunk) {
    await appendFile(path.join(home, 'desktop', 'kernel.log'), chunk, { mode: 0o600 });
  },
};
