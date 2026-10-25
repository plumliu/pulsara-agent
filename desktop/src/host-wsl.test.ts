import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createWslHostOperations, guestAbsolutePath, wslArguments, wslHostEnvironment } from './host-wsl';

test('WSL argv fixes the confirmed target and never interprets paths as shell source', () => {
  assert.deepEqual(wslArguments({distro:'Ubuntu 中文',user:'user'},'/home/user/资源/python/bin/python3',['-c','literal $() " text'],'/home/user/工程'),['-d','Ubuntu 中文','-u','user','--cd','/home/user/工程','--exec','/home/user/资源/python/bin/python3','-c','literal $() " text']);
  for (const value of ['C:\\home','relative','/home/../other','/home//other','/home/\0other']) assert.throws(()=>guestAbsolutePath(value));
  assert.throws(()=>wslArguments({distro:'--exec',user:'user'},'/usr/bin/true',[],'/'));
});

test('Windows automatic environment forwarding is removed without disabling interop', () => {
  const previous=process.env.WSLENV;
  try {
    process.env.WSLENV='PRIVATE_TOKEN/u:PATH/l';
    const env=wslHostEnvironment();
    assert.equal(Object.keys(env).some(key=>key.toUpperCase()==='WSLENV'),false);
    const pathKey=Object.keys(process.env).find(key=>key.toUpperCase()==='PATH')!;
    assert.equal(env[pathKey],process.env[pathKey]);
  } finally { if(previous===undefined)delete process.env.WSLENV;else process.env.WSLENV=previous; }
});

test('runtime diagnostics stay in the host log directory and preserve exact bytes', async () => {
  const root=await mkdtemp(path.join(os.tmpdir(),'pulsara-host-log-'));
  try {
    const host=createWslHostOperations({distro:'Ubuntu',user:'user',uid:1000,home:'/home/user'},'/home/user/.pulsara',path.join(root,'logs'));
    const first=Buffer.from('中文 diagnostic\n'),second=Buffer.from([0,1,255]);
    await host.appendRuntimeLog('/home/user/.pulsara',first);
    await host.appendRuntimeLog('/home/user/.pulsara',second);
    assert.deepEqual(await readFile(path.join(root,'logs/kernel.log')),Buffer.concat([first,second]));
    await assert.rejects(host.appendRuntimeLog('/other',first),/不一致/);
    assert.throws(()=>createWslHostOperations({distro:'Ubuntu',user:'root',uid:0,home:'/root'},'/root/.pulsara',root),/非 root/);
  } finally { await rm(root,{recursive:true,force:true}); }
});
