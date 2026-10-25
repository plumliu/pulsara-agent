const {app,session}=require('electron'),{spawn}=require('node:child_process'),fs=require('node:fs'),path=require('node:path'),readline=require('node:readline'),assert=require('node:assert/strict'),{rootCertificates}=require('node:tls'),{randomUUID}=require('node:crypto');
const {createBackendSession}=require('../../../desktop/dist/backend-session');
const out=__dirname,result={cases:[]};app.setPath('userData',path.join(out,'readiness-smoke-profile'));app.on('window-all-closed',()=>{});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const checkpoint=()=>fs.writeFileSync(path.join(out,'readiness-smoke-progress.json'),JSON.stringify(result,null,2));
async function run(name){
 const row={name,events:[],requests:[]};result.cases.push(row);let owner;
 const env={...process.env};delete env.WSLENV;delete env.ELECTRON_RUN_AS_NODE;
 const child=spawn('C:\\Windows\\System32\\wsl.exe',['-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl','--exec','/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python','-u','/mnt/c/python_workspace/pulsara-agent/output/windows-b/host-setup/readiness-guest.py'],{env,windowsHide:true,shell:false,stdio:['pipe','pipe','pipe']});
 const closed=new Promise(resolve=>child.once('close',(code,signal)=>{row.carrier={code,signal};resolve();}));
 child.stderr.on('data',chunk=>fs.appendFileSync(path.join(out,'readiness-guest.stderr.log'),chunk));
 const waiters=[];const wait=event=>{const existing=row.events.find(value=>value.event===event);return existing?Promise.resolve(existing):new Promise(resolve=>waiters.push({event,resolve}));};
 readline.createInterface({input:child.stdout}).on('line',line=>{const event=JSON.parse(line);row.events.push(event);checkpoint();for(const waiting of [...waiters])if(waiting.event===event){waiters.splice(waiters.indexOf(waiting),1);waiting.resolve(event);}});
 const ready=await wait('prepared');row.identity={pid:ready.pid,starttime:ready.starttime,origin:ready.origin,home:ready.home};
 const serve=async mode=>{const pending=wait('serving');child.stdin.write('serve '+mode+'\n');await pending;};
 const guard=setTimeout(()=>{row.fixtureObservationTimeout=true;checkpoint();owner?.seal();child.stdin.write('stop\n');},15000);
 try{
  if(['pending-retire','wrong-certificate','unauthorized'].includes(name))await serve(name==='pending-retire'?'pending':name==='unauthorized'?'unauthorized':'normal');
  const native=session.fromPartition('readiness-native-'+randomUUID(),{cache:false});
  const original=native.fetch.bind(native);let observedFetch=original;
  row.nativeFetchDescriptor=Object.fromEntries(Object.entries(Object.getOwnPropertyDescriptor(native,'fetch')??{}).filter(([key])=>key!=='value'));
  const observed=new Proxy({},{get(_target,key){if(key==='fetch')return(...args)=>observedFetch(...args);const value=native[key];return typeof value==='function'?value.bind(native):value;}});
  owner=createBackendSession({...ready,certificatePem:name==='wrong-certificate'?rootCertificates[0]:ready.certificatePem},{fromPartition(){return observed;}});
  let retirement,started=false;
  const external=new AbortController(),reason=new Error('known external startup cancellation');
  observedFetch=async(url,options)=>{
   const request={url,method:options.method,at:Date.now()};row.requests.push(request);checkpoint();
   try{const response=await original(url,options);request.status=response.status;checkpoint();return response;}
   catch(error){request.error={name:error.name,message:error.message};checkpoint();
    if(error.message==='net::ERR_CONNECTION_REFUSED'){
     if(name==='delayed-listen'&&row.requests.length===2&&!started){started=true;void serve('normal');}
     if(name==='sampling-retire')setImmediate(()=>{retirement=owner.retire(async()=>{});});
     if(name==='external-cancel')setImmediate(()=>external.abort(reason));
    }
    throw error;
   }
  };
  const pending=owner.waitUntilReachable(name==='external-cancel'?external.signal:undefined);
  if(name==='delayed-listen'){
   await pending;assert.ok(row.requests.length>=3);assert.equal(row.requests.at(-1).status,200);assert.ok(row.requests.slice(0,-1).every(value=>value.error.message==='net::ERR_CONNECTION_REFUSED'));
  }else if(name==='pending-retire'){
   const rejection=assert.rejects(pending,{name:'AbortError'});await wait('head-start');await owner.retire(async()=>{});await rejection;assert.equal(row.requests.length,1);
  }else if(name==='sampling-retire'){
   await assert.rejects(pending,{name:'AbortError'});await retirement;assert.equal(row.requests.length,1);
  }else if(name==='external-cancel'){
   await assert.rejects(pending,error=>error===reason);assert.equal(row.requests.length,1);
  }else if(name==='unauthorized'){
   await assert.rejects(pending,/HTTP 401/);assert.equal(row.requests.length,1);
  }else{
   await assert.rejects(pending,error=>{row.startupFailure=error.message;return error.message!=='net::ERR_CONNECTION_REFUSED';});assert.equal(row.requests.length,1);
  }
  assert.ok(row.requests.every(value=>value.url===ready.origin+'/'&&value.method==='HEAD'));
  await owner.retire(async()=>{});
  await assert.rejects(owner.waitUntilReachable(),{name:'AbortError'});
  const deadline=Date.now()+10000;
  for(;;){const observed=new Promise(resolve=>waiters.push({event:'inspect',resolve}));child.stdin.write('inspect\n');row.after=await observed;if(row.after.active===0)break;assert.ok(Date.now()<deadline,'finite fixture remote-close observation');await pause(10);}
  assert.notEqual(row.fixtureObservationTimeout,true);row.passed=true;
 }finally{
  clearTimeout(guard);
  if(owner)await owner.retire(async()=>{});
  child.stdin.end('stop\n');await closed;assert.deepEqual(row.carrier,{code:0,signal:null});assert.equal(row.events.at(-1).event,'cleaned');assert.equal(row.events.at(-1).exists,false);
 }
}
app.whenReady().then(async()=>{for(const name of ['delayed-listen','sampling-retire','external-cancel','pending-retire','wrong-certificate','unauthorized'])await run(name);result.passed=true;}).catch(error=>{result.failure=error.stack;}).finally(()=>{fs.writeFileSync(path.join(out,'readiness-smoke-results.json'),JSON.stringify(result,null,2));app.exit(result.passed?0:1);});
