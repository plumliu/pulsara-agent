const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),{once}=require('node:events');
const {WslSetup}=require('../../../desktop/dist/wsl-setup');
const {createWslHostOperations,executeWsl,hostPathInWsl}=require('../../../desktop/dist/host-wsl');
const out=__dirname,result={};
(async()=>{
 const target=await new WslSetup(path.join(out,'no-selection-written.json')).inspect({distro:'Ubuntu',user:'plumwsl'});
 const fixed={...target};
 const made=await executeWsl(fixed,'/bin/sh',['-c','umask 077; mktemp -d "$1/.pulsara-host-smoke-XXXXXXXX"','pulsara-host-test',fixed.home],fixed.home);
 const home=made.stdout.trim();result.target=fixed;result.home=home;
 try{
  const host=createWslHostOperations(target,home,path.join(out,'host-smoke-logs'));
  target.distro='invalid-after-binding';target.user='root';
  await host.prepareRuntimeDirectory(home);
  const payload=Buffer.from([0,255,1,10,228,184,173]);
  const child=host.spawn('/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python',['-I','-c',"import os,sys,json,base64; print(json.dumps(dict(uid=os.getuid(),cwd=os.getcwd(),home=os.getenv('PULSARA_HOME'),unlisted=os.getenv('PULSARA_UNLISTED_MARKER'),bytes=base64.b64encode(sys.stdin.buffer.read()).decode())))"],{cwd:home,env:{...process.env,PULSARA_HOME:home,PULSARA_UNLISTED_MARKER:'synthetic-not-to-forward'}});
  const closed=once(child,'close');let stdout='';child.stdout.on('data',chunk=>stdout+=chunk);child.stderr.on('data',chunk=>{result.stderr=(result.stderr??'')+chunk;});child.stdin.end(payload);
  const [code,signal]=await closed;assert.equal(code,0);assert.equal(signal,null);
  const actual=JSON.parse(stdout);assert.equal(actual.uid,fixed.uid);assert.equal(actual.cwd,home);assert.equal(actual.home,home);assert.equal(actual.unlisted,null);assert.deepEqual(Buffer.from(actual.bytes,'base64'),payload);result.child=actual;
  const finite=await host.execFile('/usr/bin/printf',['%s','中文 literal $() "'],{env:{PULSARA_HOME:home}});assert.equal(finite.stdout,'中文 literal $() "');result.finite=finite;
  const helper=await hostPathInWsl(fixed,path.resolve('desktop/native/windows-interop-helper/Program.cs'));assert.equal(helper,'/mnt/c/python_workspace/pulsara-agent/desktop/native/windows-interop-helper/Program.cs');result.convertedHostPath=helper;
  await host.appendRuntimeLog(home,Buffer.from('host diagnostic 中文\n'));assert.ok((await fs.readFile(path.join(out,'host-smoke-logs/kernel.log'),'utf8')).includes('host diagnostic 中文'));
  result.passed=true;
 }finally{
  await executeWsl(fixed,'/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python',['-I','-c',"from pathlib import Path;import shutil,os,sys; p=Path(sys.argv[1]);assert p.parent==Path.home() and p.name.startswith('.pulsara-host-smoke-') and p.stat().st_uid==os.getuid();shutil.rmtree(p);print(p.exists())",home],fixed.home).then(v=>{result.homeRemoved=v.stdout.trim()==='False';});
 }
})().catch(error=>{result.failure=error.stack;process.exitCode=1;}).finally(()=>fs.writeFile(path.join(out,'host-smoke-results.json'),JSON.stringify(result,null,2)));
