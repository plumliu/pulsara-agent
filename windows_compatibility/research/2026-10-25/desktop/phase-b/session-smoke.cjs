const {app,BrowserWindow}=require('electron');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path'),readline=require('node:readline'),assert=require('node:assert/strict');
const {createBackendSession}=require('../../../desktop/dist/backend-session');
const out=__dirname,records=[],result={records};
app.setPath('userData',path.join(out,'session-smoke-profile'));
app.on('window-all-closed',()=>{});
let child,closed,window,owner;const waiting=[];
const next=event=>new Promise(resolve=>waiting.push({event,resolve}));
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function observe(){const pending=next('inspect');child.stdin.write('inspect\n');return pending;}
app.whenReady().then(async()=>{
 const env={...process.env};delete env.WSLENV;delete env.ELECTRON_RUN_AS_NODE;
 child=spawn('C:\\Windows\\System32\\wsl.exe',['-d','Ubuntu','-u','plumwsl','--exec','/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python','-u','/mnt/c/python_workspace/pulsara-agent/output/windows-b/host-setup/session-guest.py'],{env,windowsHide:true,shell:false,stdio:['pipe','pipe','pipe']});
 const readyPromise=next('ready');
 closed=new Promise(resolve=>child.once('close',(code,signal)=>{result.carrierExit={code,signal};resolve();}));
 child.stderr.on('data',chunk=>fs.appendFileSync(path.join(out,'session-guest.stderr.log'),chunk));
 readline.createInterface({input:child.stdout}).on('line',line=>{const event=JSON.parse(line);records.push(event);const index=waiting.findIndex(w=>w.event===event.event);if(index>=0)waiting.splice(index,1)[0].resolve(event);});
 const ready=await readyPromise;result.ready=ready;
 owner=createBackendSession(ready);
 const response=await owner.session.fetch(ready.origin+'/');assert.equal(response.status,200);await response.text();
 window=new BrowserWindow({show:false,webPreferences:{session:owner.session,sandbox:true,contextIsolation:true,nodeIntegration:false}});
 await window.loadURL(ready.origin+'/');
 await window.webContents.executeJavaScript("(()=>{window.count=0;window.stream=new EventSource('/sse');stream.onmessage=()=>count++;return true})()");
 const controllers=[new AbortController(),new AbortController()];
 const pending=controllers.map(c=>owner.session.fetch(ready.origin+'/pending',{signal:c.signal}).then(()=>{throw Error('pending unexpectedly answered')},()=>({aborted:true})));
 let download;
 const downloaded=new Promise(resolve=>owner.session.once('will-download',(_e,item)=>{download=item;item.setSavePath(path.join(out,'session-smoke.bin'));item.once('done',(_e,state)=>resolve(state));}));
 window.webContents.downloadURL(ready.origin+'/download');
 const deadline=Date.now()+10000;let before;
 do {before=await observe();if(before.active.filter(x=>x==='/pending').length===2&&before.active.includes('/download')&&await window.webContents.executeJavaScript('count')>0)break;assert.ok(Date.now()<deadline,'finite smoke observation');await pause(30);}while(true);
 result.before=before;
 await owner.retire(async()=>{for(const controller of controllers)controller.abort();const destroyed=new Promise(resolve=>window.webContents.once('destroyed',resolve));window.destroy();await destroyed;await Promise.all(pending);});
 result.downloadState=await downloaded;assert.equal(result.downloadState,'cancelled');
 for(let attempt=0;attempt<100;attempt++){result.after=await observe();if(result.after.active.length===0)break;await pause(20);}
 assert.deepEqual(result.after.active,[]);
 await assert.rejects(owner.session.fetch(ready.origin+'/'));
 result.passed=true;
}).catch(error=>{result.failure=error.stack;}).finally(async()=>{
 try{if(owner)await owner.retire(async()=>{if(window&&!window.isDestroyed()){const done=new Promise(resolve=>window.webContents.once('destroyed',resolve));window.destroy();await done;}});if(child&&!child.stdin.destroyed)child.stdin.end('stop\n');if(closed)await closed;}catch(error){result.cleanupFailure=error.stack;}
 fs.writeFileSync(path.join(out,'session-smoke-results.json'),JSON.stringify(result,null,2));app.exit(result.passed&&!result.cleanupFailure?0:1);
});
