const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict'),vm=require('node:vm'),Module=require('node:module'),cp=require('node:child_process'),util=require('node:util'),{randomUUID}=require('node:crypto');
const repo=process.cwd(),out=__dirname,host=require(path.join(repo,'desktop/dist/host-wsl')),real=require(path.join(repo,'desktop/dist/wsl-setup')),target={distro:'Ubuntu',user:'plumwsl',uid:1000,home:'/home/plumwsl'},python='/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/python/bin/python3';
const label='enospc-'+randomUUID(),version='b-final-'+randomUUID(),bad=path.join(out,label+'.bin'),result={label,version,injection:'test-only LD_PRELOAD write errno ENOSPC after 4096 bytes; no actual full filesystem',cases:[],commandInjected:0};let privateRoot;
(async()=>{
 privateRoot=(await host.executeWsl(target,'/usr/bin/mktemp',['-d','/home/plumwsl/.pbf-enospc-XXXXXXXX'],target.home)).stdout.trim();assert.ok(privateRoot.startsWith('/home/plumwsl/.pbf-enospc-'));result.privateRoot=privateRoot;
 const library=privateRoot+'/write-fault.so';await host.executeWsl(target,'/usr/bin/cc',['-shared','-fPIC','-o',library,'/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/desktop/enospc-write.c','-ldl'],target.home);
 const state=async()=>JSON.parse((await host.executeWsl(target,python,['-I','-c',`import json;from pathlib import Path
root=Path.home()/'.local/share/pulsara/runtime';s=Path.home()/'.pulsara/local-settings.yaml';p=Path.home()/'.pulsara/desktop/postgres'
def meta(x):
 v=x.stat();return [v.st_ino,v.st_size,v.st_mtime_ns,v.st_uid,v.st_mode]
print(json.dumps(dict(stages=[x.name for x in root.glob('.install-*')],versionExists=(root/'${version}').exists(),marker=(root/'0.1.0-alpha.10/.pulsara-payload.sha256').read_text(),settings=meta(s),pgData=meta(p))))`],target.home)).stdout);
 result.before=await state();
 function alter(args){const copy=[...args],argv=[...copy[1]],i=argv.indexOf('--exec');if(i>=0&&argv[i+1]==='/bin/sh'&&argv[i+2]==='-c'&&argv[i+3].includes('cat > "$stage/payload.tar"')){argv.splice(i+1,1,'/usr/bin/env','LD_PRELOAD='+library,'/bin/sh');copy[1]=argv;result.commandInjected++;}return copy;}
 function wrapped(...args){return cp.execFile(...alter(args))}wrapped[util.promisify.custom]=(...args)=>cp.execFile[util.promisify.custom](...alter(args));
 const filename=path.join(repo,'desktop/dist/wsl-setup.js'),requireFrom=Module.createRequire(filename),module={exports:{}};
 vm.runInThisContext(Module.wrap(await fs.readFile(filename,'utf8')),{filename})(module.exports,id=>id==='node:child_process'?{...cp,execFile:wrapped}:requireFrom(id),module,filename,path.dirname(filename));
 const payload=await real.loadWslPayload(path.join(repo,'desktop/resources'));await fs.writeFile(bad,Buffer.alloc(1024*1024,85));let error;
 try{await new module.exports.WslSetup(path.join(out,label+'-unused.json')).prepare(target,{...payload,version,archive:bad,sha256:'0'.repeat(64)});assert.fail('write fault admitted')}catch(e){error=e;}
 result.error={message:String(error),code:error.code,stderr:error.stderr};assert.equal(error.code,1);assert.ok(error.stderr.includes('PBF_ENOSPC injected after actual prefix write'));assert.ok(error.stderr.includes('No space left on device'));assert.equal(result.commandInjected,1);
 result.after=await state();assert.deepEqual(result.after,result.before);assert.equal(result.after.versionExists,false);await host.executeWsl(target,'/usr/bin/flock',['-n',target.home+'/.local/share/pulsara/runtime/.install.lock','/usr/bin/true'],target.home);result.installLockFree=true;result.passed=true;
})().catch(e=>{result.failure=e.stack;process.exitCode=1}).finally(async()=>{await fs.rm(bad,{force:true});if(privateRoot){await host.executeWsl(target,python,['-I','-c',`from pathlib import Path;import os,shutil,sys;p=Path(sys.argv[1]);assert p.resolve()==p and p.parent==Path.home() and p.name.startswith('.pbf-enospc-') and p.stat().st_uid==os.getuid();shutil.rmtree(p);assert not p.exists()`,privateRoot],target.home);result.privateRootRemoved=true}await fs.writeFile(path.join(out,'enospc-results.json'),JSON.stringify(result,null,2)+'\n')});
