/** electron-builder afterPack adapter for the installed Linux executable.
 *
 * electron-builder owns the Debian desktop integration and sandbox setup.
 * This small executable wrapper dispatches management commands before Electron starts,
 * so headless CLI use does not require a display or acquire the GUI singleton.
 */
const { rename, writeFile } = require('node:fs/promises');
const path = require('node:path');

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== 'linux') return;
  const name = context.packager.executableName;
  if (typeof name !== 'string' || !/^[A-Za-z0-9._-]+$/.test(name)) throw new Error('Invalid packaged executable name');
  const executable = path.join(context.appOutDir, name);
  const binary = `${name}.bin`;
  await rename(executable, path.join(context.appOutDir, binary));
  await writeFile(executable, `#!/bin/sh
set -eu
PULSARA_APP_DIRECTORY=$(dirname "$(readlink -f "$0")")
if [ "\${1-}" = "--pulsara-cli" ]; then
  shift
  exec "$PULSARA_APP_DIRECTORY/resources/python/bin/python3" -m pulsara_agent "$@"
fi
exec "$PULSARA_APP_DIRECTORY/${binary}" "$@"
`, {mode:0o755, flag:'wx'});
};
