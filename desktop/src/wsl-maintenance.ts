import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { createWslHostOperations, executeWsl, guestAbsolutePath } from './host-wsl';
import { loadWslPayload, WslSetup, wslResourcePaths } from './wsl-setup';
import { checkWslMaintenance } from './postgres-wsl';

/** Finite installer admission: no GUI, resource install, kernel launch or PG stop. */
export async function checkInstalledWslMaintenance(userData: string, version: string): Promise<void> {
  const selectionFile = path.join(userData, 'wsl-selection.json');
  let selection;
  try { selection = JSON.parse(await readFile(selectionFile, 'utf8')); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error; }
  if (!selection || typeof selection.distro !== 'string' || typeof selection.user !== 'string') throw new Error('已保存的 WSL 目标无效，无法确认维护状态。');
  if (!/^[a-zA-Z0-9][a-zA-Z0-9.+_-]*$/.test(version)) throw new Error('应用资源版本无效。');
  const target = await new WslSetup(selectionFile).inspect(selection);
  const resources = wslResourcePaths(path.posix.join(target.home, '.local/share/pulsara/runtime', version));
  const resolved = await executeWsl(target, resources.pythonExecutable, ['-I', '-c',
    'from pulsara_agent.capability.pulsara_home import require_pulsara_home; print(require_pulsara_home())']);
  const home = guestAbsolutePath(resolved.stdout.trim());
  const host = createWslHostOperations(target, home, path.join(userData, 'logs'));
  await checkWslMaintenance(resources, home, host);
}

/** Called only by an explicit uninstall, never by the installer's update pass. */
export async function uninstallWsl(userData: string, version: string, mode: string, script: string, resourcesRoot: string): Promise<void> {
  if (mode !== 'keep' && mode !== 'all') throw new Error('无效的卸载选项。');
  if (!/^[a-zA-Z0-9][a-zA-Z0-9.+_-]*$/.test(version)) throw new Error('应用资源版本无效。');
  const selectionFile = path.join(userData, 'wsl-selection.json');
  let selection;
  try { selection = JSON.parse(await readFile(selectionFile, 'utf8')); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error; }
  if (!selection || typeof selection.distro !== 'string' || typeof selection.user !== 'string') throw new Error('无法确认 WSL 环境。');
  const setup = new WslSetup(selectionFile);
  const target = await setup.inspect(selection);
  const root = path.posix.join(target.home, '.local/share/pulsara/runtime', version);
  const existing = await executeWsl(target, '/bin/sh', ['-c', 'if test -e "$1" || test -L "$1"; then printf present; else printf absent; fi', 'pulsara-uninstall', root]);
  if (existing.stdout === 'absent') {
    // A previous uninstall may have removed the runtime before Windows cleanup failed.
    const payload = await loadWslPayload(resourcesRoot);
    if (payload.version !== version) throw new Error('卸载资源版本不匹配。');
    await setup.prepare(target, payload);
  } else if (existing.stdout !== 'present') throw new Error('无法确认应用资源目录。');
  await executeWsl(target, wslResourcePaths(root).pythonExecutable,
    ['-I', '-', root, target.home, mode], target.home, await readFile(script));
}
