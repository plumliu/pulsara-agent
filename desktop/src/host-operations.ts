import type { ChildProcessWithoutNullStreams } from 'node:child_process';

export interface HostExecOptions {
  env: NodeJS.ProcessEnv;
}

/** Physical host operations only; runtime and PostgreSQL retain their policies. */
export interface HostOperations {
  resolveHome(pythonExecutable: string): string;
  spawn(file: string, args: readonly string[], options: { env: NodeJS.ProcessEnv; cwd: string }): ChildProcessWithoutNullStreams;
  execFile(file: string, args: readonly string[], options: HostExecOptions): Promise<{ stdout: string; stderr: string }>;
  prepareRuntimeDirectory(home: string): Promise<void>;
  appendRuntimeLog(home: string, chunk: Buffer): Promise<void>;
}
