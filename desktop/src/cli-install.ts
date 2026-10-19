import path from 'node:path';

const shellQuote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";

/** The AppImage filename outlives its temporary mount; bundled Python does not. */
export function cliLauncherScript(resources: string, appImage?: string): string {
  if (appImage && !path.isAbsolute(appImage)) throw new Error('AppImage 路径必须为绝对路径。');
  const command = appImage
    ? `${shellQuote(appImage)} --pulsara-cli`
    : `${shellQuote(path.join(resources, 'python', 'bin', 'python3'))} -m pulsara_agent`;
  return `#!/bin/sh\n# Pulsara desktop command-line launcher\nexec ${command} "$@"\n`;
}

import { access, lstat, mkdir, mkdtemp, readFile, readlink, rename, rm, writeFile } from 'node:fs/promises';
import { constants } from 'node:fs';

export interface CliStatus {
  state: 'ready' | 'missing' | 'outdated' | 'conflict' | 'error';
  path: string;
  existingTarget?: string;
  message?: string;
  managedBy: 'application' | 'package';
  shadowedBy?: string;
}
export interface CliRegistration {
  destination: string;
  script: string;
  packageTarget?: string;
  searchPath: string;
}
const launcherMarker = '#!/bin/sh\n# Pulsara desktop command-line launcher\n';
const missing = (error: unknown) => (error as NodeJS.ErrnoException).code === 'ENOENT';

/** Inspect only the app environment, not the user's terminal shell. Absence
 * from this PATH cannot establish a terminal configuration problem. Never
 * source shell profiles or execute a discovered command. */
export async function cliStatus(config: CliRegistration): Promise<CliStatus> {
  const directories = config.searchPath.split(path.delimiter).filter(p => path.isAbsolute(p));
  const result: CliStatus = { state: 'missing', path: config.destination, managedBy: config.packageTarget ? 'package' : 'application' };
  try {
    const info = await lstat(config.destination);
    if (info.isSymbolicLink()) {
      const target = path.resolve(path.dirname(config.destination), await readlink(config.destination));
      result.existingTarget = target;
      result.state = config.packageTarget && target === config.packageTarget ? 'ready' : 'conflict';
    } else if (info.isFile() && !config.packageTarget) {
      const content = await readFile(config.destination, 'utf8');
      result.state = content === config.script && (info.mode & 0o111) !== 0 ? 'ready' : content.startsWith(launcherMarker) ? 'outdated' : 'conflict';
    } else result.state = 'conflict';
    if (result.state === 'ready') await access(config.packageTarget ?? config.destination, constants.X_OK);
  } catch (error) {
    if (!missing(error)) { result.state = 'error'; result.message = error instanceof Error ? error.message : String(error); }
    else result.state = 'missing';
  }
  for (const directory of directories) {
    const candidate = path.join(directory, 'pulsara');
    try {
      await access(candidate, constants.X_OK);
      if (candidate !== config.destination) result.shadowedBy = candidate;
      break;
    } catch { /* Not an executable PATH entry. */ }
  }
  return result;
}

/** Node owns filesystem operations; replacement renames the entry, never writes through an old symlink. */
export async function registerCli(config: CliRegistration, replaceConflict = false): Promise<CliStatus> {
  const before = await cliStatus(config);
  if (config.packageTarget || before.state === 'ready' || before.state === 'error' || (before.state === 'conflict' && !replaceConflict)) return before;
  let temporary: string | undefined;
  try {
    await mkdir(path.dirname(config.destination), { recursive: true });
    if (before.state === 'missing') {
      // Exclusive creation also protects a command installed concurrently with startup.
      await writeFile(config.destination, config.script, { mode: 0o755, flag: 'wx' });
    } else {
      temporary = await mkdtemp(path.join(path.dirname(config.destination), '.pulsara-cli-'));
      const file = path.join(temporary, 'pulsara');
      await writeFile(file, config.script, { mode: 0o755, flag: 'wx' });
      await rename(file, config.destination);
    }
    return await cliStatus(config);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'EEXIST') return cliStatus(config);
    return { ...before, state: 'error', message: error instanceof Error ? error.message : String(error) };
  } finally { if (temporary) await rm(temporary, { recursive: true, force: true }); }
}
