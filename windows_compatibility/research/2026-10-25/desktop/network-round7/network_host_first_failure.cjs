// G0-only actual Electron Session/net/powerMonitor owner; never sleeps the system.
const {app,BrowserWindow,session,net,powerMonitor}=require('electron');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path'),readline=require('node:readline'),assert=require('node:assert/strict'),{X509Certificate,randomUUID}=require('node:crypto');
const out=__dirname,label='n7-'+randomUUID(),manual=process.argv.includes('--manual-resume');
app.setPath('userData',path.join(out,label+'-profile'));app.on('window-all-closed',()=>{});
const report={label,manual,windowsPid:process.pid,versions:process.versions,events:[],requests:[],checks:[],tests:[],powers:[],guestSpawns:0};
const file=path.join(out,label+'-results.json');
const save=()=>fs.writeFileSync(file,JSON.stringify(report,null,2));
const log=(event,values={})=>{report.events.push({event,time:Date.now(),...values});save()};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const deadline=(p,label,ms=15000)=>new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('probe observation deadline '+label)),ms);p.then(v=>{clearTimeout(timer);resolve(v)},e=>{clearTimeout(timer);reject(e)})});
const guestEvents=[],waiters=[];let child,closed,ready,win,primary;const policies=[];
function next(event){const existing=guestEvents.find(e=>e.event===event);return existing?Promise.resolve(existing):new Promise(r=>waiters.push({event,resolve:r}))}
async function inspect(){const promise=new Promise(r=>waiters.push({event:'inspect',resolve:r}));child.stdin.write('{"op":"inspect"}\n');return deadline(promise,'inspect')}
function policy(label){
 const ses=session.fromPartition('g0-network-'+label+'-'+randomUUID(),{cache:false}),value={ses,retired:false,label};policies.push(value);
 ses.setCertificateVerifyProc((r,done)=>{const same=r.certificate.data.replace(/\r\n/g,'\n').trim()===ready.certificate.replace(/\r\n/g,'\n').trim();const allow=!value.retired&&same&&r.hostname==='127.0.0.1';report.checks.push({label,time:Date.now(),same,allow,errorCode:r.errorCode,verificationResult:r.verificationResult});done(allow?0:-2)});
 ses.webRequest.onBeforeRequest((r,done)=>{let allow=false;try{allow=!value.retired&&new URL(r.url).origin===ready.origin}catch{}report.requests.push({label,time:Date.now(),url:r.url,allow});done({cancel:!allow})});
 ses.setPermissionRequestHandler((_wc,_p,cb)=>cb(false));return value;
}
async function fresh(p){const r=await p.ses.fetch(ready.origin+'/healthz',{cache:'no-store'});assert.equal(r.status,200);return r.json()}
async function rendererState(){return win.webContents.executeJavaScript('({...window.n7,last:window.n7.last})')}
async function test(name,fn){const detail=await fn();report.tests.push({name,passed:true,detail});save();return detail}
function pendingRequest(route,method='GET'){
 const request=net.request({url:ready.origin+route,method,session:primary.ses,redirect:'error'}),state={route,bytes:0,errors:[],closed:false};
 request.on('error',error=>state.errors.push(String(error)));
 request.on('response',response=>{state.status=response.statusCode;response.on('data',chunk=>state.bytes+=chunk.length);response.on('error',e=>state.errors.push(String(e)));response.on('aborted',()=>state.aborted=true)});
 const settled=new Promise(resolve=>request.once('close',()=>{state.closed=true;resolve(state)}));
 return {request,state,settled};
}
async function main(){
 const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;for(const key of Object.keys(env))if(key.toUpperCase()==='WSLENV')delete env[key];
 child=spawn(path.join(process.env.SystemRoot,'System32','wsl.exe'),['-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl/workspaces/pulsara_agent','--exec','/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python','-u','-c',fs.readFileSync(path.join(out,'network_guest.py'),'utf8')],{shell:false,windowsHide:true,stdio:['pipe','pipe','pipe'],env});report.guestSpawns++;report.carrierPid=child.pid;
 closed=new Promise(resolve=>child.once('close',(code,signal)=>{report.carrierExit={code,signal};resolve()}));child.stdin.on('error',e=>log('stdin-error',{error:String(e)}));
 child.stderr.on('data',b=>fs.appendFileSync(path.join(out,label+'-guest.stderr.log'),b));
 readline.createInterface({input:child.stdout}).on('line',line=>{const value=JSON.parse(line);guestEvents.push(value);const i=waiters.findIndex(w=>w.event===value.event);if(i>=0)waiters.splice(i,1)[0].resolve(value);save()});
 ready=await deadline(next('ready'),'ready');report.ready=ready;const certificate=new X509Certificate(ready.certificate);report.validFrom=certificate.validFrom;report.validTo=certificate.validTo;
 primary=policy('launch');await fresh(primary);
 win=new BrowserWindow({show:false,webPreferences:{session:primary.ses,sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});win.webContents.setWindowOpenHandler(()=>({action:'deny'}));await win.loadURL(ready.origin+'/');
 await win.webContents.executeJavaScript(`(()=>{window.n7={count:0,opens:0,errors:0,last:null};window.n7source=new EventSource('/g0/sse');n7source.onopen=()=>n7.opens++;n7source.onerror=()=>n7.errors++;n7source.onmessage=e=>{n7.count++;n7.last=JSON.parse(e.data)};return true})()`);
 await sleep(400);
 powerMonitor.on('suspend',()=>{report.powers.push({event:'suspend',time:Date.now()});save()});
 powerMonitor.on('resume',()=>{report.powers.push({event:'resume',time:Date.now()});save()});
 await test('natural certificate boundary with same live guest and SSE',async()=>{const before=await rendererState();assert.equal(before.last.pid,ready.pid);const wait=Math.max(0,Date.parse(certificate.validTo)-Date.now()+1200);await sleep(wait);assert.ok(Date.now()>Date.parse(certificate.validTo));const after=await rendererState();assert.ok(after.count>before.count);assert.equal(after.last.pid,ready.pid);await fresh(primary);const freshPolicy=policy('natural-expired-new-context');await fresh(freshPolicy);const checks=report.checks.filter(c=>c.label===freshPolicy.label);assert.ok(checks.some(c=>c.same&&c.allow));freshPolicy.retired=true;await freshPolicy.ses.closeAllConnections();return {before,after,newContextChecks:checks,guestPid:ready.pid,guestSpawns:report.guestSpawns,clockChanged:false}});
 if(manual){
  const readyFile=path.join(out,'manual-ready.json'),continueFile=path.join(out,label+'.continue'),stopFile=path.join(out,label+'.stop');fs.writeFileSync(readyFile,JSON.stringify({ready:true,label,results:file,continueFile,stopFile,windowsPid:process.pid,linuxPid:ready.pid,origin:ready.origin,validTo:certificate.validTo,actions:'Use Windows Start > Power > Sleep, wait briefly, wake; do not shut down or change VPN. Root coordinates this action.'},null,2));
  log('manual-suspend-ready',{readyFile});
  while(!fs.existsSync(continueFile)&&!fs.existsSync(stopFile)){await sleep(1000);const state=await rendererState();report.manualLatest={time:Date.now(),state};save()}
  if(fs.existsSync(continueFile)){await test('actual powerMonitor suspend and resume observed',async()=>{assert.ok(report.powers.some(e=>e.event==='suspend'));assert.ok(report.powers.some(e=>e.event==='resume'));await fresh(primary);const state=await rendererState();assert.equal(state.last.pid,ready.pid);assert.equal(report.guestSpawns,1);return {powers:report.powers,state,samePid:true,sameCertificate:true,guestSpawns:report.guestSpawns}})}
 }
 await test('old Session retires SSE slow import download and concurrent pending',async()=>{
  const upload=pendingRequest('/api/connections/owner/import-file','POST');upload.request.chunkedEncoding=true;upload.request.setHeader('Origin',ready.origin);upload.request.setHeader('Content-Type','application/octet-stream');upload.request.setHeader('X-Pulsara-Filename','"n7-slow.bin"');upload.request.setHeader('X-Pulsara-Connection-Generation','7');upload.request.write(Buffer.alloc(65536,85));
  const timer=setInterval(()=>{if(!primary.retired)try{upload.request.write(Buffer.alloc(65536,85))}catch(e){upload.state.errors.push(String(e))}},100);
  const pending=Array.from({length:3},()=>pendingRequest('/g0/pending'));for(const item of pending)item.request.end();
  let item,downloadCancelQueued=false,downloadTerminal=false;function cancelDownload(){if(!item||downloadCancelQueued)return;downloadCancelQueued=true;setImmediate(()=>{if(!downloadTerminal)item.cancel()})}
  const downloadTarget=path.join(out,label+'-partial.bin');const downloadDone=new Promise(resolve=>primary.ses.once('will-download',(_e,value)=>{item=value;value.setSavePath(downloadTarget);value.on('updated',(_e,state)=>{report.downloadUpdates??=[];report.downloadUpdates.push({time:Date.now(),state,bytes:value.getReceivedBytes()});if(primary.retired&&state==='interrupted')cancelDownload()});value.once('done',(_e,state)=>{downloadTerminal=true;resolve({state,bytes:value.getReceivedBytes()})})}));win.webContents.downloadURL(ready.origin+'/g0/download');
  await sleep(600);const before=await inspect();assert.ok(before.stages.length>0);assert.equal(before.imports.length,0);
  const retireTime=Date.now();primary.retired=true;clearInterval(timer);await primary.ses.closeAllConnections();cancelDownload();
  const destroyed=new Promise(resolve=>win.webContents.once('destroyed',resolve));win.destroy();await destroyed;
  const settled=await deadline(Promise.all([upload.settled,...pending.map(p=>p.settled),downloadDone]),'retired requests');assert.ok(settled.slice(0,4).every(v=>v.closed));assert.ok(['cancelled','interrupted'].includes(settled[4].state));
  await primary.ses.clearStorageData();const rejected=await Promise.all(Array.from({length:4},async()=>{try{await fresh(primary);return false}catch{return true}}));assert.ok(rejected.every(Boolean));
  let after;for(let count=0;count<50;count++){after=await inspect();if(after.stages.length===0&&guestEvents.filter(e=>e.event==='network-handler-opened').length===guestEvents.filter(e=>e.event==='network-handler-closed').length)break;await sleep(50)}
  assert.equal(after.stages.length,0);assert.equal(after.imports.length,0);assert.equal(guestEvents.filter(e=>e.event==='network-handler-opened').length,guestEvents.filter(e=>e.event==='network-handler-closed').length);
  const size=fs.existsSync(downloadTarget)?fs.statSync(downloadTarget).size:0;if(fs.existsSync(downloadTarget))fs.unlinkSync(downloadTarget);
  return {before,after,retireTime,settled,rejected,webContentsDestroyed:true,partialBytesBeforeOwnedCleanup:size,oneGuest:report.guestSpawns===1};
 });
}
app.whenReady().then(main).catch(error=>{report.fatal=error.stack;save()}).finally(async()=>{for(const p of policies){p.retired=true;await p.ses.closeAllConnections().catch(e=>report.connectionCleanup=String(e))}if(win&&!win.isDestroyed())win.destroy();if(child&&!child.stdin.destroyed)child.stdin.end('{"op":"stop"}\n');if(closed)await deadline(closed,'guest close').catch(e=>report.cleanupError=String(e));report.guestEvents=guestEvents;report.ended=Date.now();save();app.exit(report.fatal||report.cleanupError?1:0)});
