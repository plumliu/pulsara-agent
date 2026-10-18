const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const prepare = require('./linux-hooks.cjs');

test('Debian CLI lifecycle preserves an unrelated command and removes only its own symlink', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'pulsara-deb-'));
  try {
    await prepare({ electronPlatformName: 'linux', packager: { projectDir: root } });
    const profile = await fs.readFile(path.join(root, 'build/linux-apparmor-profile'), 'utf8');
    assert.ok(profile.includes('"/opt/${sanitizedProductName}/${executable}.bin"'));
    assert.ok(profile.includes('userns,'));
    const bin = path.join(root, 'bin');
    const target = path.join(root, 'payload/bin/pulsara');
    const command = path.join(bin, 'pulsara');
    await fs.mkdir(bin);
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.writeFile(target, '#!/bin/sh\n');
    const scripts = {};
    for (const name of ['after-install', 'after-remove']) {
      const generated = await fs.readFile(path.join(root, `build/linux-${name}.sh`), 'utf8');
      assert.ok(generated.includes('apparmor'), 'retains Electron Builder integration');
      const marker = name === 'after-install' ? '# The desktop executable' : '# Never remove';
      scripts[name] = generated.slice(generated.indexOf(marker))
        .replaceAll('/usr/bin/pulsara', command)
        .replaceAll('/opt/${sanitizedProductName}/resources/bin/pulsara', target);
    }
    const run = name => execFileSync('/bin/sh', ['-c', scripts[name]], { encoding: 'utf8', stdio: 'pipe' });
    await fs.writeFile(command, 'unrelated command');
    run('after-install');
    assert.equal(await fs.readFile(command, 'utf8'), 'unrelated command');
    run('after-remove');
    assert.equal(await fs.readFile(command, 'utf8'), 'unrelated command');
    await fs.unlink(command);
    run('after-install');
    assert.equal(await fs.readlink(command), target);
    run('after-install');
    assert.equal(await fs.readlink(command), target);
    run('after-remove');
    await assert.rejects(fs.lstat(command), { code: 'ENOENT' });
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});
