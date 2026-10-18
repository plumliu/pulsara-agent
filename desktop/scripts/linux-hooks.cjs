// Electron Builder owns its sandbox/AppArmor/desktop integration. Extend its
// installed templates with only Pulsara's separate CLI symlink lifecycle.
const fs = require('node:fs/promises');
const path = require('node:path');
module.exports = async function prepareLinuxHooks(context) {
  if (context.electronPlatformName !== 'linux') return;
  const root = context.packager.projectDir;
  const templates = path.join(path.dirname(require.resolve('app-builder-lib/package.json')), 'templates/linux');
  const build = path.join(root, 'build');
  await fs.mkdir(build, { recursive: true });
  const install = `
# The desktop executable has its own name; pulsara is exclusively the CLI.
cli_target='/opt/\${sanitizedProductName}/resources/bin/pulsara'
if [ ! -e /usr/bin/pulsara ] && [ ! -L /usr/bin/pulsara ]; then
  ln -s "$cli_target" /usr/bin/pulsara
elif [ "$(readlink /usr/bin/pulsara)" != "$cli_target" ]; then
  echo 'Pulsara CLI was not installed: /usr/bin/pulsara belongs to another installation. Remove or rename it explicitly, then reinstall Pulsara.' >&2
fi
`;
  const remove = `
# Never remove another application's command (including a subsequently replaced one).
if [ "$(readlink /usr/bin/pulsara)" = '/opt/\${sanitizedProductName}/resources/bin/pulsara' ]; then
  rm /usr/bin/pulsara
fi
`;
  for (const [name, extra] of [['after-install', install], ['after-remove', remove]]) {
    const upstream = await fs.readFile(path.join(templates, name + '.tpl'), 'utf8');
    await fs.writeFile(path.join(build, 'linux-' + name + '.sh'), upstream + '\n' + extra);
  }
  const profile = await fs.readFile(path.join(templates, 'apparmor-profile.tpl'), 'utf8');
  const executablePath = '"/opt/${sanitizedProductName}/${executable}"';
  if (!profile.includes(executablePath)) throw new Error('Electron Builder AppArmor template changed');
  await fs.writeFile(path.join(build, 'linux-apparmor-profile'), profile.replace(executablePath, '"/opt/${sanitizedProductName}/${executable}.bin"'));
};
if (require.main === module) {
  module.exports({ electronPlatformName: process.platform, packager: { projectDir: path.resolve(__dirname, '..') } }).catch(error => { console.error(error); process.exitCode = 1; });
}
