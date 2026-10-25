// Real production WslSetup; all selection/invalid archives are private to this directory.
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict'),{randomUUID}=require('node:crypto');
const repo=process.cwd(),out=__dirname,{WslSetup,loadWslPayload}=require(path.join(repo,'desktop/dist/wsl-setup')),{executeWsl}=require(path.join(repo,'desktop/dist/host-wsl'));
const label='setup-'+randomUUID(),selection=path.join(out,label+'-selection.json'),bad=path.join(out,label+'-partial.bin');
const result={label,cases:[],installedVersion:'0.1.0-alpha.10',productionModule:path.join(repo,'desktop/dist/wsl-setup.js')};
const py='/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/python/bin/python3',base='/home/plumwsl/.local/share/pulsara/runtime';let setup,target;
const inspect=async()=>JSON.parse((await executeWsl(target,py,['-I','-c',`import json,os;from pathlib import Path
base=Path('${base}'); home=Path.home()/'.pulsara'
def meta(p):
 s=p.stat(); return dict(inode=s.st_ino,uid=s.st_uid,mode=oct(s.st_mode&0o777),size=s.st_size,mtimeNs=s.st_mtime_ns)
print(json.dumps(dict(stages=[dict(name=p.name,bytes=(p/'payload.tar').stat().st_size if (p/'payload.tar').exists() else 0) for p in base.glob('.install-*') if p.is_dir()],markers={p.name:(p/'.pulsara-payload.sha256').read_text() for p in base.iterdir() if p.is_dir() and (p/'.pulsara-payload.sha256').is_file()},settings=meta(home/'local-settings.yaml'),pgData=meta(home/'desktop/postgres'))))`],target.home)).stdout);
async function reject(name,fn,check){try{await fn();assert.fail('unexpected admission: '+name)}catch(e){assert.ok(check(e),e.stack);result.cases.push({name,rejected:true,error:String(e),code:e.code??null});}}
(async()=>{
 setup=new WslSetup(selection);const actual=await setup.discover();assert.ok(actual.distros.some(x=>x.name==='Ubuntu'&&x.version===2));result.discovered=actual.distros;
 target=await setup.inspect({distro:'Ubuntu',user:'plumwsl'});assert.equal(target.uid,1000);result.target=target;
 await reject('missing existing distro',()=>setup.inspect({distro:'Pulsara-Missing-'+randomUUID()}),e=>String(e).includes('已存在'));
 await reject('unknown Linux user',()=>setup.inspect({distro:'Ubuntu',user:'pulsara-absent-'+randomUUID()}),e=>e.code!==0);
 await reject('root user',()=>setup.inspect({distro:'Ubuntu',user:'root'}),e=>String(e).includes('非 root'));
 await fs.writeFile(selection,'not JSON');const corrupt=await setup.discover();assert.ok(corrupt.savedError&&!corrupt.saved);assert.equal(await fs.readFile(selection,'utf8'),'not JSON');result.cases.push({name:'corrupt selection never overwrites itself',passed:true});
 await setup.select({distro:'Ubuntu',user:'plumwsl'});assert.deepEqual((await setup.discover()).saved,{distro:'Ubuntu',user:'plumwsl'});result.cases.push({name:'explicit target persists/reopens only private selection',passed:true});
 const payload=await loadWslPayload(path.join(repo,'desktop/resources'));result.before=await inspect();
 const prepared=await setup.prepare(target,payload);assert.equal(prepared.resources.root,base+'/0.1.0-alpha.10');result.existing=prepared;result.cases.push({name:'real installed alpha10 original prepare revalidates',passed:true});
 await reject('same version different digest',()=>setup.prepare(target,{...payload,sha256:'0'.repeat(64)}),e=>String(e).includes('Command failed'));
 const invalidVersion='b-final-'+randomUUID();const controller=new AbortController(),reason=new Error('cancel after observed actual setup bytes');const file=await fs.open(bad,'w');await file.truncate(512*1024*1024);await file.close();
 let settled=false;const pending=reject('partial input cancellation joins finite installer',()=>setup.prepare(target,{...payload,version:invalidVersion,archive:bad,sha256:'0'.repeat(64)},{signal:controller.signal}),e=>e===reason).finally(()=>settled=true);
 const limit=Date.now()+20000;while(!settled){const state=await inspect();const current=state.stages.filter(s=>!result.before.stages.some(old=>old.name===s.name));if(current.some(s=>s.bytes>0)){result.partialObserved=current;controller.abort(reason);break;}assert.ok(Date.now()<limit,'test-only observation deadline, not product lifetime cap');await new Promise(r=>setTimeout(r,20));}
 await pending;assert.ok(result.partialObserved?.some(s=>s.bytes>0));result.after=await inspect();assert.deepEqual(result.after.markers,result.before.markers);assert.deepEqual(result.after.settings,result.before.settings);assert.deepEqual(result.after.pgData,result.before.pgData);assert.deepEqual(result.after.stages,result.before.stages);
 await executeWsl(target,'/usr/bin/flock',['-n',base+'/.install.lock','/usr/bin/true'],target.home);result.installLockFree=true;result.passed=true;
})().catch(e=>{result.failure=e.stack;process.exitCode=1}).finally(async()=>{await fs.rm(selection,{force:true});await fs.rm(selection+'.tmp',{force:true});await fs.rm(bad,{force:true});result.localInputsRemoved=true;await fs.writeFile(path.join(out,'setup-results.json'),JSON.stringify(result,null,2)+'\n')});
