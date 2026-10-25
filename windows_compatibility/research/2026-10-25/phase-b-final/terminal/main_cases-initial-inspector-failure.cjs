const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {spawn,execFileSync}=require('node:child_process');
const output=__dirname;
const scenario=process.argv[2] || 'quit';
assert(['quit','host-eof'].includes(scenario));
const python='/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/python/bin/python3';
const root='/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10';
const executable='C:/Users/plumlocal/AppData/Local/Programs/pulsara/Pulsara.exe';
const resources='C:/Users/plumlocal/AppData/Local/Programs/pulsara/resources';
const wsl=process.env.SystemRoot+'/System32/wsl.exe';
const make="from pathlib import Path;import tempfile,json,os;os.umask(0o077);r=Path(tempfile.mkdtemp(prefix='.bmain-',dir='/home/plumwsl'));(r/'home').mkdir(mode=448);(r/'workspace').mkdir(mode=448);print(json.dumps(dict(root=str(r),home=str(r/'home'),workspace=str(r/'workspace'))))";
const config=JSON.parse(execFileSync(wsl,['-d','Ubuntu','-u','plumwsl','--exec',python,'-I','-c',make],{encoding:'utf8'}));
const uid=path.basename(config.root);
config.windowsHome=path.join(output,uid+'-electron');
config.kernelEvents='/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/terminal/'+scenario+'-kernel.jsonl';
config.hostEvents=path.join(output,scenario+'-host.jsonl');
config.child='/mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/terminal/interop-child.exe';
config.bootstrap='/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/terminal/kernel_bootstrap.py';
config.guestConfig='/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/terminal/'+scenario+'-config.json';
config.installedRoot=root;config.resources=resources;config.scenario=scenario;
fs.mkdirSync(config.windowsHome,{recursive:true});
fs.writeFileSync(path.join(config.windowsHome,'wsl-selection.json'),JSON.stringify({distro:'Ubuntu',user:'plumwsl'}));
fs.writeFileSync(path.join(output,scenario+'-config.json'),JSON.stringify(config,null,2));
const emit=(kind,fields={})=>fs.appendFileSync(config.hostEvents,JSON.stringify({timeNs:String(BigInt(Date.now())*1000000n),kind,...fields})+'\n');
const child=spawn(executable,['--inspect-brk=127.0.0.1:0'],{windowsHide:true,env:{...process.env},stdio:['ignore','pipe','pipe']});
emit('owned-exe-spawn',{pid:child.pid,executable,windowsHome:config.windowsHome});
let endpoint='',stderr='';
child.stderr.on('data',chunk=>{stderr+=chunk;fs.appendFileSync(path.join(output,scenario+'-exe-stderr.log'),chunk);const found=stderr.match(/Debugger listening on (ws:\/\/127\.0\.0\.1:\d+\/[^\s]+)/);if(found)endpoint=found[1];});
child.stdout.on('data',chunk=>fs.appendFileSync(path.join(output,scenario+'-exe-stdout.log'),chunk));
let exited=false;const exit=new Promise(resolve=>child.once('close',(code,signal)=>{exited=true;emit('owned-exe-close',{code,signal});resolve({code,signal});}));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function until(condition,label){const began=Date.now();while(!condition()){if(exited)throw new Error('owned exe exited before '+label);if(Date.now()-began>120000)throw new Error('test observation deadline: '+label);await sleep(25);}}
let socket,counter=0;const requests=new Map();
function call(method,params={}){return new Promise((resolve,reject)=>{const id=++counter;requests.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});}
const injection=`(()=>{
 const M=process.getBuiltinModule('module'),fs=process.getBuiltinModule('fs'),path=process.getBuiltinModule('path');
 const cfg=${JSON.stringify(config)};
 const electron=M._load('electron',null,false);
 electron.app.setPath('userData',cfg.windowsHome);
 if(electron.app.getPath('userData')!==cfg.windowsHome)throw new Error('userData not isolated');
 const emit=(kind,fields={})=>fs.appendFileSync(cfg.hostEvents,JSON.stringify({timeNs:String(BigInt(Date.now())*1000000n),kind,...fields})+'\\n');
 emit('before-main-userdata-isolated',{path:electron.app.getPath('userData'),version:electron.app.getVersion()});
 const load=M._load;
 M._load=function(request,parent,isMain){const value=load.apply(this,arguments);
  if(parent&&parent.filename.includes('app.asar')&&request==='./wsl-setup'&&!value.__testObserved){
   value.__testObserved=true;
   value.WslSetup.prototype.prepare=async function(target,payload){
    if(target.user!=='plumwsl'||target.distro!=='Ubuntu'||target.uid!==1000)throw new Error('unexpected target');
    emit('private-prepare-only',{target,home:cfg.home,version:payload.version});
    const root=cfg.installedRoot;
    return {target,home:cfg.home,resources:{root,pythonExecutable:root+'/python/bin/python3',postgresBin:root+'/postgres/bin',playwrightBrowsers:root+'/playwright',packageCli:root+'/bin/pulsara'},hostHelperPath:'/mnt/c/Users/plumlocal/AppData/Local/Programs/pulsara/resources/windows-interop/pulsara-interop.exe'};
   };
  }
  if(parent&&parent.filename.includes('app.asar')&&request==='./host-wsl'&&!value.__testObserved){
   value.__testObserved=true;const create=value.createWslHostOperations;
   value.createWslHostOperations=function(){const host=create.apply(this,arguments),spawn=host.spawn;
    host.spawn=function(file,args,options){const inject=args[0]==='-m'&&args[1]==='pulsara_agent.desktop_runtime';
     const child=spawn.call(this,file,inject?['-I',cfg.bootstrap,cfg.guestConfig]:args,options);
     if(inject){emit('actual-carrier-spawn',{pid:child.pid,home:options.env.PULSARA_HOME});child.stdout.on('data',chunk=>fs.appendFileSync(cfg.hostEvents+'.runtime-raw',chunk));child.once('close',(code,signal)=>emit('actual-carrier-close',{code,signal}));}
     return child;};return host;};
  }
  if(parent&&parent.filename.includes('app.asar')&&request==='./postgres'&&!value.__testObserved){
   value.__testObserved=true;const stop=value.BundledPostgres.prototype.stop;
   value.BundledPostgres.prototype.stop=async function(){emit('original-pg-stop-enter',{directory:this.directory});const r=await stop.apply(this,arguments);emit('original-pg-stop-returned');return r;};
  }
  return value;
 };
 const same=fs.readFileSync(path.join(cfg.resources,'app.asar/dist/main.js')).equals(fs.readFileSync('C:/python_workspace/pulsara-agent/desktop/dist/main.js'));
 if(!same)throw new Error('installed main differs from built candidate');emit('installed-main-bytes-equal',{same});
 return 'injected-before-single-instance-lock';
})()`;
(async()=>{
 await until(()=>endpoint,'inspector endpoint');
 socket=new WebSocket(endpoint);await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
 socket.onmessage=event=>{const value=JSON.parse(event.data);const request=requests.get(value.id);if(request){requests.delete(value.id);value.error?request.reject(new Error(JSON.stringify(value.error))):request.resolve(value.result);}};
 await call('Runtime.enable');
 const injected=await call('Runtime.evaluate',{expression:injection,returnByValue:true});
 if(injected.exceptionDetails)throw new Error(JSON.stringify(injected.exceptionDetails));emit('inspector-injection-result',injected);
 await call('Runtime.runIfWaitingForDebugger');
 await until(()=>fs.existsSync(config.kernelEvents)&&fs.readFileSync(config.kernelEvents,'utf8').includes('active-original-terminal'),'active original terminal');
 emit('active-task-observed');
 await until(()=>fs.existsSync(config.hostEvents+'.runtime-raw')&&fs.readFileSync(config.hostEvents+'.runtime-raw','utf8').includes('"type":"ready"'),'original runtime ready');
 if(scenario==='quit'){
  emit('invoke-original-app-quit');
  await call('Runtime.evaluate',{expression:"process.getBuiltinModule('module')._load('electron',null,false).app.quit()"});
  socket.close();
 }else{
  emit('exact-owned-main-terminate',{pid:child.pid});
  child.kill('SIGKILL');socket.close();
 }
 const result=await exit;fs.writeFileSync(path.join(output,scenario+'-main-result.json'),JSON.stringify({config,result},null,2));
})().catch(error=>{emit('harness-failed',{error:String(error),stack:error.stack});console.error(error);process.exitCode=1;});
