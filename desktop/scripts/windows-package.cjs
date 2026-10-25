// Build-time only: no compiler or Windows Python is needed by installed users.
const path = require('node:path');
const {execFileSync} = require('node:child_process');
module.exports = async context => {
  if (context.electronPlatformName !== 'win32') return;
  const root = context.packager.projectDir;
  execFileSync(path.resolve(root,'../.venv/Scripts/python.exe'),[path.join(root,'scripts/prepare_windows_resources.py')],{cwd:root,stdio:'inherit',windowsHide:true,shell:false});
};
