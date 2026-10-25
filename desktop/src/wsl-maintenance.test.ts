import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {test} from 'node:test';
import {runInNewContext} from 'node:vm';
import {wslResourcePaths} from './wsl-setup';

function fixture(present: boolean, failure?: Error) {
  const calls: string[] = [];
  const target = {distro:'Ubuntu', user:'test-user', uid:1000, home:'/home/test-user'};
  const exports = {} as {uninstallWsl: (...args: string[]) => Promise<void>};
  const source = Buffer.from('isolated cleanup script');
  const modules: Record<string, unknown> = {
    'node:fs/promises': {async readFile(file: string) {
      return file.endsWith('wsl-selection.json') ? JSON.stringify(target) : source;
    }},
    './wsl-setup': {wslResourcePaths,
      async loadWslPayload() { return {version:'1.0'}; },
      WslSetup: class {
        async inspect() { calls.push('inspect'); return target; }
        async prepare() { calls.push('prepare'); }
      },
    },
    './host-wsl': {async executeWsl(selected: unknown, file: string, args: string[], cwd?: string, input?: Buffer) {
      assert.equal(selected, target);
      if (file === '/bin/sh') return {stdout:present ? 'present' : 'absent'};
      assert.equal(file, '/home/test-user/.local/share/pulsara/runtime/1.0/python/bin/python3');
      assert.deepEqual(Array.from(args.slice(0, 4)), ['-I','-','/home/test-user/.local/share/pulsara/runtime/1.0','/home/test-user']);
      assert.equal(cwd, target.home);
      assert.equal(input, source);
      calls.push(`cleanup:${args[4]}`);
      if (failure) throw failure;
      return {stdout:''};
    }},
  };
  runInNewContext(readFileSync(path.join(__dirname,'wsl-maintenance.js'),'utf8'), {
    exports, require(name: string) { return modules[name] ?? require(name); },
  });
  return {calls, run:(mode: string) => exports.uninstallWsl('userData','1.0',mode,'uninstall.py','resources')};
}

for (const present of [true, false]) for (const mode of ['keep','all']) {
  test(`uninstall ${mode} uses the bound user and restores a missing cleanup runtime: ${present}`, async () => {
    const value = fixture(present);
    await value.run(mode);
    assert.deepEqual(value.calls, ['inspect', ...(!present ? ['prepare'] : []), `cleanup:${mode}`]);
  });
}

test('cleanup failure reaches the uninstaller and invalid modes do nothing', async () => {
  const value = fixture(true, new Error('busy'));
  await assert.rejects(value.run('all'), /busy/);
  const invalid = fixture(true);
  await assert.rejects(invalid.run('other'), /无效/);
  assert.deepEqual(invalid.calls, []);
});
