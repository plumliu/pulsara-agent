import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { createWslHostOperations, executeWsl, guestAbsolutePath } from './host-wsl';
import { WslSetup, wslResourcePaths } from './wsl-setup';
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
