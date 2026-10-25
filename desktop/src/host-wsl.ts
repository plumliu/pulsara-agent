import { execFile, spawn } from 'node:child_process';
import path from 'node:path';
import { appendFile, mkdir } from 'node:fs/promises';
import { promisify } from 'node:util';
import type { HostOperations } from './host-operations';

export interface WslSelection { distro: string; user: string; }
export interface WslTarget extends WslSelection { uid: number; home: string; }
const execute = promisify(execFile);
const guestEnvironment = ['PULSARA_HOME', 'PYTHONUNBUFFERED', 'PLAYWRIGHT_BROWSERS_PATH'] as const;

export function windowsWslExecutable(): string {
  const root = process.env.SystemRoot ?? process.env.WINDIR;
  if (!root || !path.win32.isAbsolute(root)) throw new Error('无法定位 Windows 系统 WSL。');
  return path.win32.join(root, 'System32', 'wsl.exe');
}

export function wslHostEnvironment(): NodeJS.ProcessEnv {
  return Object.fromEntries(Object.entries(process.env).filter(([key]) => key.toUpperCase() !== 'WSLENV'));
}

export function guestAbsolutePath(value: string): string {
  if (!value.startsWith('/') || value.startsWith('//') || /[\\\x00-\x1f\x7f]/.test(value)
      || (value !== '/' && value.slice(1).split('/').some(part => !part || part === '.' || part === '..'))) throw new Error('需要明确的 Linux 绝对路径。');
  return value;
}

export function validateWslSelection(target: { distro: string; user?: string }): void {
  for (const value of [target.distro, ...(target.user === undefined ? [] : [target.user])]) {
    if (typeof value !== 'string' || !value || value.startsWith('-') || /[\x00-\x1f\x7f]/.test(value)) throw new Error('无效的 WSL 目标。');
  }
}

export function wslArguments(target: { distro: string; user?: string }, file: string, args: readonly string[], cwd: string): string[] {
  validateWslSelection(target);
  guestAbsolutePath(file); guestAbsolutePath(cwd);
  return ['-d', target.distro, ...(target.user === undefined ? [] : ['-u', target.user]), '--cd', cwd, '--exec', file, ...args];
}

/** One finite platform command, with Node's bounded exec and original failure. */
export async function executeWsl(target: { distro: string; user?: string }, file: string, args: readonly string[], cwd = '/', input?: Buffer): Promise<{stdout: string; stderr: string}> {
  const pending = execute(windowsWslExecutable(), wslArguments(target, file, args, cwd), {
    env: wslHostEnvironment(), windowsHide: true, shell: false, encoding: 'utf8',
    maxBuffer: 4 * 1024 * 1024, // Existing finite host-command per-stream boundary.
  });
  if (input !== undefined) {
    const writing = new Promise<void>((resolve, reject) => {
      const stdin = pending.child.stdin!;
      stdin.once('error', reject);
      stdin.end(input, () => resolve());
    });
    const [result, written] = await Promise.allSettled([pending, writing]);
    if (result.status === 'rejected') throw result.reason;
    if (written.status === 'rejected') throw written.reason;
    return result.value;
  }
  return pending;
}

export async function hostPathInWsl(target: WslSelection, windowsPath: string): Promise<string> {
  if (!path.win32.isAbsolute(windowsPath) || /[\x00-\x1f\x7f]/.test(windowsPath)) throw new Error('需要 Windows 绝对路径。');
  const result = await executeWsl(target, '/usr/bin/wslpath', ['-a', '-u', windowsPath]);
  return guestAbsolutePath(result.stdout.trim());
}

export function createWslHostOperations(target: WslTarget, home: string, hostLogDirectory: string): HostOperations {
  target = Object.freeze({...target});
  validateWslSelection(target); guestAbsolutePath(target.home); guestAbsolutePath(home);
  if (!path.isAbsolute(hostLogDirectory)) throw new Error('需要 Windows 宿主日志绝对目录。');
  if (!Number.isSafeInteger(target.uid) || target.uid <= 0) throw new Error('WSL 应用必须使用已确认的非 root 用户。');
  const invocation = (file: string, args: readonly string[], options: {env: NodeJS.ProcessEnv; cwd: string}) => {
    guestAbsolutePath(file);
    const environment = guestEnvironment.flatMap(key => options.env[key] === undefined ? [] : [key + '=' + options.env[key]]);
    // Let the original EOF/application owner settle after a carrier hangup.
    const command = args[0] === '-m' && args[1] === 'pulsara_agent.desktop_runtime'
      ? ['-I', '-c', "import signal,runpy; signal.signal(signal.SIGHUP,signal.SIG_IGN); runpy.run_module('pulsara_agent.desktop_runtime',run_name='__main__',alter_sys=True)", ...args.slice(2)] : [...args];
    return wslArguments(target, '/usr/bin/env', ['--', ...environment, file, ...command], options.cwd);
  };
  return {
    resolveHome() { return home; },
    spawn(file, args, options) {
      return spawn(windowsWslExecutable(), invocation(file, args, options), {env:wslHostEnvironment(), windowsHide:true, shell:false, stdio:['pipe','pipe','pipe']});
    },
    async execFile(file, args, options) {
      return execute(windowsWslExecutable(), invocation(file, args, {...options, cwd:home}), {env:wslHostEnvironment(), windowsHide:true, shell:false, encoding:'utf8', maxBuffer:4*1024*1024});
    },
    async prepareRuntimeDirectory(value) {
      if (value !== home) throw new Error('应用数据目录与已确认目标不一致。');
      await executeWsl(target, '/bin/sh', ['-eu', '-c', 'umask 077; test "$(realpath -e -- "$1")" = "$1"; test -O "$1"; mkdir -p -- "$1/desktop"; test "$(realpath -e -- "$1/desktop")" = "$1/desktop"; test -O "$1/desktop"', 'pulsara-runtime-directory', home], target.home);
    },
    async appendRuntimeLog(value, chunk) {
      if (value !== home) throw new Error('应用数据目录与已确认目标不一致。');
      await mkdir(hostLogDirectory,{recursive:true});
      await appendFile(path.join(hostLogDirectory,'kernel.log'),chunk,{mode:0o600});
    },
  };
}
