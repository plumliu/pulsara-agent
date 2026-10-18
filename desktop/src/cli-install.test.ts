import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';
import { cliLauncherScript } from './cli-install';

const afterPack = require('../scripts/linux-cli-entry.cjs');
const { generateAppRunScript } = require('app-builder-lib/out/targets/appimage/appImageUtil');
const quote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";

test('installed AppImage CLI survives absent mounted resources and preserves arguments and exit status', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "pulsara cli's "));
  try {
    const image = path.join(root, "Pulsara's image.AppImage");
    const fixture = path.join(root, 'fixture.cjs');
    await writeFile(fixture, 'console.log(JSON.stringify(process.argv.slice(2))); process.exit(37);');
    await writeFile(image, `#!/bin/sh\nexec ${quote(process.execPath)} ${quote(fixture)} "$@"\n`, { mode: 0o755 });
    const cli = path.join(root, 'pulsara');
    await writeFile(cli, cliLauncherScript('/unmounted/nonexistent/resources', image), { mode: 0o755 });
    const args = ['mcp', 'a b', "quote's", '$(touch forbidden)', '中文'];
    const result = spawnSync(cli, args, { encoding: 'utf8' });
    assert.equal(result.status, 37, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), ['--pulsara-cli', ...args]);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('Linux packaged CLI dispatches before Electron without a display; GUI retains its arguments', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "pulsara dispatch's "));
  try {
    const executable = path.join(root, 'pulsara-desktop');
    const python = path.join(root, 'resources/python/bin/python3');
    const fixture = path.join(root, 'fixture.cjs');
    await mkdir(path.dirname(python), { recursive: true });
    await writeFile(fixture, 'console.log(JSON.stringify(process.argv.slice(2))); process.exit(29);');
    for (const [filename, kind] of [[executable, 'electron'], [python, 'python']]) {
      await writeFile(filename, `#!/bin/sh\nexec ${quote(process.execPath)} ${quote(fixture)} ${kind} "$@"\n`, { mode: 0o755 });
    }
    await afterPack({ electronPlatformName: 'linux', appOutDir: root, packager: { executableName: 'pulsara-desktop' } });
    const env = { ...process.env };
    delete env.DISPLAY;
    delete env.WAYLAND_DISPLAY;
    const args = ['mcp', 'a b', "quote's", '$(touch forbidden)', '中文'];
    for (const prefix of [['--pulsara-cli'], ['--no-sandbox', '--pulsara-cli']]) {
      const result = spawnSync(executable, [...prefix, ...args], { encoding: 'utf8', env });
      assert.equal(result.status, 29, result.stderr);
      assert.deepEqual(JSON.parse(result.stdout), ['python', '-m', 'pulsara_agent', ...args]);
    }
    const result = spawnSync(executable, ['--no-sandbox', '--pulsara-launch'], { encoding: 'utf8' });
    assert.equal(result.status, 29, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), ['electron', '--no-sandbox', '--pulsara-launch']);

    // Exercise the dependency's actual launcher, including its sandbox probe;
    // this adapter must not grow its own AppImage mount or AppRun implementation.
    const appRun = path.join(root, 'AppRun');
    await writeFile(appRun, generateAppRunScript({ ExecutableName: 'pulsara-desktop', ProductName: 'Pulsara', ProductFilename: 'Pulsara' }), { mode: 0o755 });
    delete env.APPIMAGE_EXIT_AFTER_INSTALL;
    const fromAppRun = spawnSync(appRun, ['--pulsara-cli', ...args], {
      encoding: 'utf8', env: { ...env, APPDIR: root, APPIMAGE: '/persistent/Pulsara.AppImage' },
    });
    assert.equal(fromAppRun.status, 29, fromAppRun.stderr);
    assert.deepEqual(JSON.parse(fromAppRun.stdout), ['python', '-m', 'pulsara_agent', ...args]);
  } finally { await rm(root, { recursive: true, force: true }); }
});

import { readFile, symlink, readlink, chmod } from 'node:fs/promises';
import { registerCli, cliStatus } from './cli-install';

test('automatic registration creates and updates its own launcher after the app moves', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'pulsara-cli-register-'));
  try {
    const config = { destination: path.join(root, 'bin/pulsara'), script: cliLauncherScript('/Applications/Pulsara.app/Contents/Resources'), searchPath: path.join(root, 'bin') };
    assert.equal((await registerCli(config)).state, 'ready');
    const moved = { ...config, script: cliLauncherScript('/other/Pulsara.app/Contents/Resources') };
    assert.equal((await cliStatus(moved)).state, 'outdated');
    assert.equal((await registerCli(moved)).state, 'ready');
    assert.equal(await readFile(config.destination, 'utf8'), moved.script);
    await chmod(config.destination, 0o644);
    assert.equal((await registerCli(moved)).state, 'ready');
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('old CLI symlinks are preserved automatically and explicit replacement never modifies their target', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'pulsara-cli-conflict-'));
  try {
    const old = path.join(root, 'uv-cli');
    await writeFile(old, '#!/bin/sh\necho old\n', { mode: 0o755 });
    const destination = path.join(root, 'pulsara');
    await symlink(old, destination);
    const config = { destination, script: cliLauncherScript('/app/resources'), searchPath: root };
    const before = await registerCli(config);
    assert.equal(before.state, 'conflict');
    assert.equal(before.existingTarget, old);
    assert.equal(await readlink(destination), old);
    assert.equal((await registerCli(config, true)).state, 'ready');
    assert.equal(await readFile(old, 'utf8'), '#!/bin/sh\necho old\n');
    await rm(destination);
    await symlink(path.join(root, 'missing-old-cli'), destination);
    assert.equal((await registerCli(config)).state, 'conflict');
    assert.equal((await registerCli(config, true)).state, 'ready');
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('package entries remain package-owned; PATH shadowing is reported without changing other commands', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'pulsara-cli-package-'));
  try {
    const bin = path.join(root, 'bin'), first = path.join(root, 'first');
    await mkdir(bin); await mkdir(first);
    const packageTarget = path.join(root, 'bundled-cli');
    await writeFile(packageTarget, '#!/bin/sh\nexit 0\n', {mode:0o755});
    const config = { destination:path.join(bin,'pulsara'), packageTarget, script:'unused', searchPath:`${first}${path.delimiter}${bin}` };
    assert.equal((await registerCli(config)).state, 'missing');
    await symlink(packageTarget, config.destination);
    await writeFile(path.join(first,'pulsara'), 'old', {mode:0o755});
    const status = await registerCli(config);
    assert.equal(status.state,'ready');
    assert.equal(status.managedBy,'package');
    assert.equal(status.shadowedBy,path.join(first,'pulsara'));
    assert.equal(await readlink(config.destination),packageTarget);
  } finally { await rm(root, { recursive: true, force: true }); }
});
