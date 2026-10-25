const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict'),{randomUUID,createHash}=require('node:crypto');
const {WslSetup}=require('../../../desktop/dist/wsl-setup'),{executeWsl}=require('../../../desktop/dist/host-wsl');
const out=__dirname,result={cases:[]};
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
 const setup=new WslSetup(path.join(out,'unused-selection.json')),target=await setup.inspect({distro:'Ubuntu',user:'plumwsl'});
 const helper=path.join(out,'interop-version.exe'),small=path.join(out,'invalid-payload.bin'),large=path.join(out,'cancel-payload.bin');
 await fs.writeFile(small,'not a tar archive');
 const digest=createHash('sha256').update('not a tar archive').digest('hex');
 const versions=[];
 const payload=(archive,sha256)=>{const version='smoke-invalid-'+randomUUID();versions.push(version);return{archive,sha256,version,interopHelper:helper};};
 const inspect=async()=>JSON.parse((await executeWsl(target,'/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python',['-I','-c',"import json,sys;from pathlib import Path;p=Path(sys.argv[1]);print(json.dumps({'stages':[{'name':d.name,'bytes':(d/'payload.tar').stat().st_size if (d/'payload.tar').exists() else 0} for d in p.glob('.install-*') if d.is_dir()],'versions':[d.name for d in p.iterdir()] if p.exists() else []}))",target.home+'/.local/share/pulsara/runtime'],target.home)).stdout);
 async function rejected(name,input,options,expected){try{await setup.prepare(target,input,options);assert.fail('invalid installation admitted')}catch(error){assert.ok(expected(error),String(error));result.cases.push({name,error: String(error),code:error.code??null});}}
 try{
  await rejected('missing archive',payload(path.join(out,'missing-'+randomUUID()),digest),{},error=>error.code==='ENOENT');
  await rejected('wrong content digest',payload(small,'0'.repeat(64)),{},error=>String(error).includes('sha256sum'));
  await rejected('invalid archive layout',payload(small,digest),{},error=>String(error).includes('tar:'));
  const before=new AbortController(),reason=new Error('known cancellation before installation');before.abort(reason);
  await rejected('cancel before submit',payload(small,digest),{signal:before.signal},error=>error===reason);
  const file=await fs.open(large,'w');await file.truncate(1024*1024*1024);await file.close();
  const controller=new AbortController(),cancelReason=new Error('cancel after actual partial archive bytes');
  let settled=false;
  const pending=rejected('cancel after actual bytes',payload(large,'0'.repeat(64)),{signal:controller.signal},error=>error===cancelReason).finally(()=>{settled=true;});
  const deadline=Date.now()+20000;let during;
  while(!settled){during=await inspect();if(during.stages.some(v=>v.bytes>0)){controller.abort(cancelReason);break;}assert.ok(Date.now()<deadline,'finite cancellation gate observation');await pause(20);}
  await pending;assert.ok(during.stages.some(v=>v.bytes>0));result.partialBytesBeforeCancel=during;
  const final=await inspect();assert.deepEqual(final.stages,[]);assert.ok(versions.every(version=>!final.versions.includes(version)));result.final=final;
  const lock=await executeWsl(target,'/usr/bin/flock',['-n',target.home+'/.local/share/pulsara/runtime/.install.lock','/usr/bin/true'],target.home);assert.equal(lock.stdout,'');result.installLockFree=true;result.passed=true;
 }finally{await fs.rm(small,{force:true});await fs.rm(large,{force:true});}
})().catch(error=>{result.failure=error.stack;process.exitCode=1;}).finally(()=>fs.writeFile(path.join(out,'setup-failure-results.json'),JSON.stringify(result,null,2)));
