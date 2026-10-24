// G0-only visible Electron GUI. Does not import production main or settings.
const {app,BrowserWindow,session}=require('electron');
const {spawn}=require('node:child_process');
const fs=require('node:fs'),path=require('node:path'),readline=require('node:readline');
app.setPath('userData',path.join(__dirname,'files-profile'));
const report={events:[],requests:[],certificates:[],started:new Date().toISOString(),versions:process.versions};
let child,win,policy,closed,retired=false;
function record(value){report.events.push(value);fs.writeFileSync(path.join(__dirname,'files-results.json'),JSON.stringify(report,null,2))}
app.whenReady().then(async()=>{
 const env={...process.env};for(const key of Object.keys(env))if(key.toUpperCase()==='WSLENV')delete env[key];
 child=spawn('C:\\Windows\\System32\\wsl.exe',['-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl/workspaces/pulsara_agent','--exec','/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python','-u','-c',fs.readFileSync(path.join(__dirname,'files_guest.py'),'utf8')],{shell:false,stdio:['pipe','pipe','pipe'],windowsHide:true,env});
 closed=new Promise(r=>child.on('close',(code,signal)=>{record({event:'carrier-close',code,signal});r()}));
 child.stderr.on('data',v=>fs.appendFileSync(path.join(__dirname,'files-guest.stderr.log'),v));
 const ready=await new Promise((resolve,reject)=>{child.once('error',reject);readline.createInterface({input:child.stdout}).on('line',line=>{const v=JSON.parse(line);record(v);if(v.event==='ready')resolve(v)});child.once('close',()=>reject(Error('guest exited before ready')))});
 policy=session.fromPartition('g0-files-'+crypto.randomUUID(),{cache:false});
 policy.setCertificateVerifyProc((req,done)=>{const same=req.certificate.data.trim()===ready.certificate.trim();report.certificates.push({hostname:req.hostname,same});done(!retired&&req.hostname==='127.0.0.1'&&same?0:-2)});
 policy.webRequest.onBeforeRequest((req,done)=>{let allow=false;try{allow=!retired&&new URL(req.url).origin===ready.origin}catch{}report.requests.push({url:req.url,allow});done({cancel:!allow})});
 policy.setPermissionRequestHandler((_wc,_permission,done)=>done(false));
 win=new BrowserWindow({width:1220,height:900,title:'G0 Windows WSL2 文件验收',webPreferences:{session:policy,sandbox:true,contextIsolation:true,nodeIntegration:false}});
 win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
 win.webContents.on('console-message',(_event,...args)=>record({event:'renderer-console',args:args.map(String)}));
 await win.loadURL(ready.origin+'/');
 record({event:'gui-ready',activeSession:null});
 win.on('close',()=>{retired=true});
});
app.on('window-all-closed',async()=>{retired=true;await policy?.closeAllConnections();if(child&&!child.stdin.destroyed)child.stdin.end();await closed;record({event:'host-stopped'});app.quit()});
process.on('uncaughtException',e=>{record({event:'fatal',error:e.stack});if(child&&!child.stdin.destroyed)child.stdin.end();app.quit()});
