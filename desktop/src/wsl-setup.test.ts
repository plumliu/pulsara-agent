import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { loadWslPayload, parseWslDistributions, wslResourcePaths } from './wsl-setup';

test('WSL discovery joins actual names to localized version/default observations', () => {
  const names = ['Ubuntu','Ubuntu  中文','Legacy'];
  const verbose = '\ufeff  NAME              STATE          VERSION\r\n* Ubuntu            Running        2\r\n  Ubuntu  中文      已停止         2\r\n  Legacy            Stopped        1\r\n';
  assert.deepEqual(parseWslDistributions(Buffer.from(verbose,'utf16le'),Buffer.from(names.join('\r\n')+'\r\n','utf16le')),[
    {name:'Ubuntu',version:2,default:true},{name:'Ubuntu  中文',version:2,default:false},{name:'Legacy',version:1,default:false},
  ]);
  assert.throws(() => parseWslDistributions(Buffer.from(verbose,'utf16le'),Buffer.from('Missing\n')),/不明确/);
});

test('Linux bundle paths use POSIX even on the Windows host', () => {
  assert.deepEqual(wslResourcePaths('/home/user/中文 资源'),{
    root:'/home/user/中文 资源',pythonExecutable:'/home/user/中文 资源/python/bin/python3',playwrightBrowsers:'/home/user/中文 资源/playwright',postgresBin:'/home/user/中文 资源/postgres/bin',packageCli:'/home/user/中文 资源/bin/pulsara',
  });
  assert.throws(() => wslResourcePaths('C:\\resources'));
});

test('payload manifest carries only the fixed archive content boundary', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(),'pulsara-payload-manifest-'));
  try {
    await mkdir(path.join(root,'wsl-payload'));
    const file = path.join(root,'wsl-payload','manifest.json');
    await writeFile(file,JSON.stringify({version:'0.1.0-alpha.6',sha256:'a'.repeat(64),platform:'linux-x86_64'}));
    assert.deepEqual(await loadWslPayload(root),{archive:path.join(root,'wsl-payload','payload.tar'),sha256:'a'.repeat(64),version:'0.1.0-alpha.6',interopHelper:path.join(root,'windows-interop','pulsara-interop.exe')});
    for (const invalid of [{version:'../escape',sha256:'a'.repeat(64),platform:'linux-x86_64'},{version:'1',sha256:'bad',platform:'linux-x86_64'},{version:'1',sha256:'a'.repeat(64),platform:'win32'}]) {
      await writeFile(file,JSON.stringify(invalid));
      await assert.rejects(loadWslPayload(root),/无效/);
    }
  } finally { await rm(root,{recursive:true,force:true}); }
});
