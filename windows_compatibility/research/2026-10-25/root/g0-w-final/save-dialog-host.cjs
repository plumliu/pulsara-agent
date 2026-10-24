// Real production HTTP owners over WSL TLS; isolated Electron test session.
const {app,BrowserWindow,session}=require('electron');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path'),readline=require('node:readline'),assert=require('node:assert/strict');
const out=__dirname,report={tests:[],events:[],requests:[],certChecks:[],versions:process.versions};
// PowerShell can return before a GUI executable and close its output pipe.
// The experimental owner therefore writes logs directly to its owned file.
console.log=(...v)=>fs.appendFileSync(path.join(out,'save-dialog-direct.log'),v.map(String).join(' ')+'\n');
console.error=console.log;
app.setPath('userData',path.join(out,'save-dialog-profile'));
app.on('window-all-closed',()=>{});
let child,closed,win,ses,ready,retired=false;const waiters=[];
function save(){fs.writeFileSync(path.join(out,'save-dialog-results.json'),JSON.stringify(report,null,2))}
function next(event){return new Promise(resolve=>waiters.push({event,resolve}))}
function deadline(promise,label,ms=20000){return new Promise((resolve,reject)=>{const t=setTimeout(()=>reject(Error('observation deadline: '+label)),ms);promise.then(x=>{clearTimeout(t);resolve(x)},e=>{clearTimeout(t);reject(e)})})}
async function command(op){const pending=next({revoke:'revoked',replace:'replaced',inspect:'inspect'}[op]);child.stdin.write(JSON.stringify({op})+'\n');return deadline(pending,op)}
async function test(name,fn){try{const detail=await deadline(fn(),name);report.tests.push({name,pass:true,detail});console.log('PASS',name)}catch(e){report.tests.push({name,pass:false,error:e.stack});console.log('FAIL',name,e.message)}save()}
async function request(route,body,extra={}){const r=await ses.fetch(ready.origin+route,{method:'POST',headers:{'Content-Type':'application/json',Origin:ready.origin,'Sec-Fetch-Site':'same-origin',...extra},body:JSON.stringify(body)});return {status:r.status,value:await r.json()}}
async function preview(name){const r=await request('/api/connections/owner/file-preview',{path:ready.workspace+'/'+name});assert.equal(r.status,200,JSON.stringify(r));return r.value}
async function main(){
 const env={...process.env};for(const k of Object.keys(env))if(k.toUpperCase()==='WSLENV')delete env[k];
 child=spawn('C:\\Windows\\System32\\wsl.exe',['-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl/workspaces/pulsara_agent','--exec','/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python','-u','-c',fs.readFileSync(path.join(out,'../round5/http_guest.py'),'utf8')],{shell:false,stdio:['pipe','pipe','pipe'],windowsHide:true,env});
 closed=new Promise(resolve=>child.once('close',(code,signal)=>{report.carrierExit={code,signal};resolve()}));
 child.stderr.on('data',b=>fs.appendFileSync(path.join(out,'save-dialog-guest.stderr.log'),b));
 const first=next('ready');readline.createInterface({input:child.stdout}).on('line',line=>{const e=JSON.parse(line);report.events.push(e);const i=waiters.findIndex(w=>w.event===e.event);if(i>=0)waiters.splice(i,1)[0].resolve(e)});
 ready=await deadline(first,'ready');ses=session.fromPartition('g0-save-dialog-'+crypto.randomUUID(),{cache:false});
 ses.setCertificateVerifyProc((r,done)=>{const same=r.certificate.data.trim()===ready.certificate.trim();report.certChecks.push({same,hostname:r.hostname});done(!retired&&same&&r.hostname==='127.0.0.1'?0:-2)});
 // Electron's bundled PDF UI is a local component extension, not another server.
 // Its data requests still pass the exact HTTPS origin and certificate checks.
 const pdfExtension='mhjfbmdgcfjbbpaeojofohoefgiehjai';
 ses.webRequest.onBeforeRequest((r,done)=>{let allowed=false;const frameUrl=r.frame?.url;try{const u=new URL(r.url);allowed=!retired&&(u.origin===ready.origin||(u.protocol==='chrome-extension:'&&u.hostname===pdfExtension)||(u.protocol==='chrome:'&&u.hostname==='resources'&&frameUrl?.startsWith('chrome-extension://'+pdfExtension+'/')))}catch{}report.requests.push({url:r.url,allowed,resourceType:r.resourceType,frameUrl});done({cancel:!allowed})});
 ses.setPermissionRequestHandler((_wc,_p,cb)=>cb(false));
 win=new BrowserWindow({show:true,width:1000,height:850,webPreferences:{session:ses,sandbox:true,contextIsolation:true,nodeIntegration:false,webviewTag:false,navigateOnDragDrop:false,backgroundThrottling:false}});
 win.webContents.on('console-message',(_e,...args)=>{report.rendererMessages??=[];report.rendererMessages.push(args.map(String))});
 win.webContents.setWindowOpenHandler(()=>({action:'deny'}));await win.loadURL(ready.origin+'/');

 const target=path.join(out,'save-dialog-existing.txt');
 const newTarget=path.join(out,'save-dialog-cancelled.txt');
 assert.ok(!fs.existsSync(target)&&!fs.existsSync(newTarget),'fresh output filenames');
 fs.writeFileSync(target,'G0-owned overwrite sentinel');
 const item=await preview('中文.txt');
 win.setTitle('G0 Windows 保存对话框验收');
 await win.webContents.executeJavaScript("document.body.textContent='G0 保存对话框：只操作本次 output 中的合成测试文件。'");
 for (const stage of ['cancel-dialog','decline-overwrite','accept-overwrite']) {
  report.stage=stage;save();
  const result=await new Promise(resolve=>{
   ses.once('will-download',(_e,download)=>{
    download.setSaveDialogOptions({title:'G0 '+stage,defaultPath:stage==='cancel-dialog'?newTarget:target,properties:['showOverwriteConfirmation']});
    report.dialogOptions=download.getSaveDialogOptions();save();
    download.once('done',(_e,state)=>resolve({state,path:download.getSavePath(),bytes:download.getReceivedBytes()}));
   });
   win.webContents.downloadURL(ready.origin+item.content_url+'?download=1');
  });
  if(stage==='accept-overwrite') {
   assert.equal(result.state,'completed');
   assert.equal(fs.readFileSync(target,'utf8'),'中文预览 / Windows WSL2\n');
  } else {
   assert.equal(result.state,'cancelled');
   assert.equal(fs.readFileSync(target,'utf8'),'G0-owned overwrite sentinel');
   assert.ok(!fs.existsSync(newTarget));
  }
  report.tests.push({name:stage,pass:true,detail:result});save();
 }
 report.stage='complete';save();
}
app.whenReady().then(main).catch(e=>{report.fatal=e.stack;console.error(e)}).finally(async()=>{retired=true;if(win&&!win.isDestroyed()){const destroyed=new Promise(resolve=>win.once('closed',resolve));win.close();await destroyed}await ses?.closeAllConnections();if(child&&!child.stdin.destroyed)child.stdin.end('{"op":"stop"}\n');if(closed)await deadline(closed,'guest close').catch(e=>report.cleanupError=String(e));report.summary={passed:report.tests.filter(x=>x.pass).length,failed:report.tests.filter(x=>!x.pass).length};save();console.log(JSON.stringify(report.summary));if(report.fatal||report.cleanupError||report.summary.failed)app.exit(1);else app.quit()});
