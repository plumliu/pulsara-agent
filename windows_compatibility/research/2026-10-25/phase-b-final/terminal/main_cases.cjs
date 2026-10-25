const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {spawn,execFileSync}=require('node:child_process');
const output=__dirname;
const scenario=process.argv[2] || 'quit';
const label=process.argv[3] || scenario;
assert(['quit','host-eof'].includes(scenario));
const python='/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/python/bin/python3';
const root='/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10';
const executable='C:/Users/plumlocal/AppData/Local/Programs/pulsara/Pulsara.exe';
const resources='C:/Users/plumlocal/AppData/Local/Programs/pulsara/resources';
const wsl=process.env.SystemRoot+'/System32/wsl.exe';
const make="from pathlib import Path;import tempfile,json,os;os.umask(0o077);r=Path(tempfile.mkdtemp(prefix='.bmain-',dir='/home/plumwsl'));(r/'home').mkdir(mode=448);w=Path('/home/plumwsl/workspaces')/r.name;w.mkdir(mode=448);print(json.dumps(dict(root=str(r),home=str(r/'home'),workspace=str(w))))";
const config=JSON.parse(execFileSync(wsl,['-d','Ubuntu','-u','plumwsl','--exec',python,'-I','-c',make],{encoding:'utf8'}));
const uid=path.basename(config.root);
config.windowsHome=path.join(output,uid+'-electron');
config.kernelEvents='/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/terminal/'+label+'-kernel.jsonl';
config.hostEvents=path.join(output,label+'-host.jsonl');
config.child='/mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/terminal/interop-child.exe';
config.bootstrap='/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/terminal/kernel_bootstrap.py';
config.guestConfig='/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/terminal/'+label+'-config.json';
config.installedRoot=root;config.resources=resources;config.scenario=scenario;
fs.mkdirSync(config.windowsHome,{recursive:true});
fs.writeFileSync(path.join(config.windowsHome,'wsl-selection.json'),JSON.stringify({distro:'Ubuntu',user:'plumwsl'}));
fs.writeFileSync(path.join(output,label+'-config.json'),JSON.stringify(config,null,2));
const emit=(kind,fields={})=>fs.appendFileSync(config.hostEvents,JSON.stringify({timeNs:String(BigInt(Date.now())*1000000n),kind,...fields})+'\n');
const child=spawn(executable,['--inspect-brk=127.0.0.1:0'],{windowsHide:true,env:{...process.env},stdio:['ignore','pipe','pipe']});
emit('owned-exe-spawn',{pid:child.pid,executable,windowsHome:config.windowsHome});
let endpoint='',stderr='';
child.stderr.on('data',chunk=>{stderr+=chunk;fs.appendFileSync(path.join(output,label+'-exe-stderr.log'),chunk);const found=stderr.match(/Debugger listening on (ws:\/\/127\.0\.0\.1:\d+\/[^\s]+)/);if(found)endpoint=found[1];});
child.stdout.on('data',chunk=>fs.appendFileSync(path.join(output,label+'-exe-stdout.log'),chunk));
let exited=false;const exit=new Promise(resolve=>child.once('close',(code,signal)=>{exited=true;emit('owned-exe-close',{code,signal});resolve({code,signal});}));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function until(condition,label){const began=Date.now();while(!condition()){if(exited)throw new Error('owned exe exited before '+label);if(Date.now()-began>120000)throw new Error('test observation deadline: '+label);await sleep(25);}}
let socket,counter=0,paused;const requests=new Map(),scripts=new Map();
const frameUrl=frame=>frame.url || scripts.get(frame.location.scriptId) || '';
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
 socket.onmessage=event=>{const value=JSON.parse(event.data);if(value.method==='Debugger.scriptParsed')scripts.set(value.params.scriptId,value.params.url);if(value.method==='Debugger.paused')paused=value.params;const request=requests.get(value.id);if(request){requests.delete(value.id);value.error?request.reject(new Error(JSON.stringify(value.error))):request.resolve(value.result);}};
 await call('Runtime.enable');
 await call('Debugger.enable');
 await call('Debugger.setBreakpointByUrl',{lineNumber:0,urlRegex:'app\\.asar[/\\\\]dist[/\\\\]main\\.js$'});
 await call('Runtime.runIfWaitingForDebugger');
 while(true){
  await until(()=>paused,'entry breakpoint');
  if(paused.callFrames.some(frame=>/app\.asar[/\\]dist[/\\]main\.js$/.test(frameUrl(frame))))break;
  emit('resume-internal-bootstrap',{frames:paused.callFrames.map(frame=>frameUrl(frame))});paused=undefined;await call('Debugger.resume');
 }
 const frame=paused.callFrames.find(frame=>/app\.asar[/\\]dist[/\\]main\.js$/.test(frameUrl(frame)));
 const injected=await call('Debugger.evaluateOnCallFrame',{callFrameId:frame.callFrameId,expression:injection,returnByValue:true});
 if(injected.exceptionDetails)throw new Error(JSON.stringify(injected.exceptionDetails));emit('inspector-injection-result',injected);
 await call('Debugger.resume');
 const kernelHostPath=path.join(output,label+'-kernel.jsonl');
 await until(()=>fs.existsSync(kernelHostPath)&&fs.readFileSync(kernelHostPath,'utf8').includes('active-original-terminal'),'active original terminal');
 emit('active-task-observed');
 await until(()=>fs.existsSync(config.hostEvents+'.runtime-raw')&&/"type"\s*:\s*"ready"/.test(fs.readFileSync(config.hostEvents+'.runtime-raw','utf8')),'original runtime ready');
 if(scenario==='quit'){
  emit('invoke-original-app-quit');
  await call('Runtime.evaluate',{expression:"process.getBuiltinModule('module')._load('electron',null,false).app.quit()"});
  socket.close();
 }else{
  const kernelRows=fs.readFileSync(kernelHostPath,'utf8').trim().split('\n').map(JSON.parse);
  const active=kernelRows.find(row=>row.kind==='active-original-terminal');
  const windowsReady=path.join(output,label+'-windows-held.json'),windowsResult=path.join(output,label+'-windows-waits.json');
  const windowsObserver=spawn(path.join(output,'fault.exe'),['watch',active.states[0].ready.job,windowsReady,windowsResult],{windowsHide:true,stdio:['ignore','ignore','pipe']});
  windowsObserver.stderr.on('data',chunk=>fs.appendFileSync(path.join(output,label+'-windows-observer-error.log'),chunk));
  const windowsObserverExit=new Promise(resolve=>windowsObserver.once('close',code=>resolve(code)));
  await until(()=>fs.existsSync(windowsReady),'held exact Windows scope members');
  emit('windows-scope-handles-held',{facts:JSON.parse(fs.readFileSync(windowsReady))});
  const witness=spawn(wsl,['-d','Ubuntu','-u','plumwsl','--exec',python,'-I','/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/terminal/observe_exit.py',config.guestConfig,'/mnt/c/python_workspace/pulsara-agent/output/phase-b-final/terminal/'+label+'-witness.json'],{windowsHide:true,stdio:['ignore','pipe','pipe']});
  let witnessReady=false;
  witness.stdout.on('data',chunk=>{fs.appendFileSync(path.join(output,label+'-witness.log'),chunk);if(String(chunk).includes('"ready": true'))witnessReady=true;});
  witness.stderr.on('data',chunk=>fs.appendFileSync(path.join(output,label+'-witness-error.log'),chunk));
  const witnessExit=new Promise(resolve=>witness.once('close',code=>resolve(code)));
  await until(()=>witnessReady,'held kernel pidfd witness');
  emit('exact-owned-main-terminate',{pid:child.pid});
  child.kill('SIGKILL');socket.close();
  const code=await witnessExit;assert.equal(code,0);emit('exact-kernel-witness-returned',{code});
  const windowsCode=await windowsObserverExit;assert.equal(windowsCode,0);emit('exact-windows-witness-returned',{code:windowsCode,facts:JSON.parse(fs.readFileSync(windowsResult))});
 }
 const result=await exit;fs.writeFileSync(path.join(output,label+'-main-result.json'),JSON.stringify({config,result},null,2));
})().catch(error=>{emit('harness-failed',{error:String(error),stack:error.stack});socket?.close();console.error(error);process.exitCode=1;});
