const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {spawn,execFileSync}=require('node:child_process');
const {createInterface}=require('node:readline');
const base=__dirname, python=process.argv[2]||'/usr/bin/python3', label=process.argv[3]||'python312';
const wsl=path.join(process.env.SystemRoot,'System32','wsl.exe');
const env={...process.env};for(const k of Object.keys(env))if(k.toUpperCase()==='WSLENV')delete env[k];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function facts(pid) {
  const command=`Get-CimInstance Win32_Process -Filter 'ProcessId = ${pid}' | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress`;
  const raw=execFileSync('powershell.exe',['-NoProfile','-NonInteractive','-Command',command],{encoding:'utf8',windowsHide:true});
  return raw.trim()?JSON.parse(raw):null;
}
async function run(action,mode) {
  const record=path.join(base,`${label}-${action}-${mode}.windows.jsonl`);fs.writeFileSync(record,'');
  const linuxRecord=record.replaceAll('\\','/').replace(/^C:/,'/mnt/c');
  const code=`import json,os,signal,subprocess,sys,time\ndef emit(event,**kw):\n d=dict(event=event,pid=os.getpid(),time=time.time(),**kw);open(sys.argv[4],"a").write(json.dumps(d)+"\\n")\n try: print(json.dumps(d),flush=True)\n except BrokenPipeError: sys.stdout=open(os.devnull,"w")\nemit("linux-root-start",pgid=os.getpgrp(),python=sys.version)\na=[sys.argv[1],sys.argv[2],"parent",sys.argv[3],sys.argv[5],sys.argv[6]] if sys.argv[5]!="native" else ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe","-NoProfile","-NonInteractive","-File",sys.argv[2].replace("windows-pe.cjs","windows-native-parent.ps1"),sys.argv[3],sys.argv[6]]\np=subprocess.Popen(a,start_new_session=True,stdin=subprocess.DEVNULL)\nemit("linux-popen",child_pid=p.pid,child_pgid=os.getpgid(p.pid))\nif sys.argv[6]=="cancel":\n command=sys.stdin.readline();emit("linux-control",command=command.strip())\n try: os.killpg(p.pid,signal.SIGTERM);emit("linux-killpg-sent")\n except ProcessLookupError: emit("linux-killpg-missing")\nelse:\n time.sleep(.5)\nrc=p.wait();emit("linux-parent-wait",returncode=rc)\nif sys.argv[6]=="carrier": time.sleep(9)\nemit("linux-root-exit")`;
  const linuxLog=linuxRecord+'.linux.jsonl';fs.writeFileSync(record+'.linux.jsonl','');
  const pe=path.join(base,'node-pin.exe').replaceAll('\\','/').replace(/^C:/,'/mnt/c');
  const args=['-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl','--exec',python,'-u','-c',code,pe,path.join(base,'windows-pe.cjs'),record,linuxLog,mode,action];
  console.log(JSON.stringify({event:'command',node:process.version,file:wsl,args}));
  const start=Date.now(),child=spawn(wsl,args,{shell:false,stdio:['pipe','pipe','pipe'],env});
  const events=[],lines=[];let closed=false;let linuxRoot;
  child.stderr.on('data',b=>console.log(JSON.stringify({event:'carrier-stderr',text:b.toString()})));
  createInterface({input:child.stdout}).on('line',line=>{lines.push(line);let value;try{value=JSON.parse(line)}catch{}if(value?.event==='linux-root-start')linuxRoot=value.pid;console.log(JSON.stringify({event:'stdout',elapsed:Date.now()-start,line}))});
  child.on('exit',(code,signal)=>{events.push({event:'exit',code,signal,elapsed:Date.now()-start});console.log(JSON.stringify(events.at(-1)))});
  child.on('close',(code,signal)=>{closed=true;events.push({event:'close',code,signal,elapsed:Date.now()-start});console.log(JSON.stringify(events.at(-1)))});
  child.stdin.on('error',e=>console.log(JSON.stringify({event:'stdin-error',code:e.code})));
  let windowsStart;
  for(let n=0;n<100;n++){const rows=fs.readFileSync(record,'utf8').trim().split('\n').filter(Boolean).map(l=>JSON.parse(l));windowsStart=rows.find(r=>r.event==='windows-child-start');if(windowsStart)break;await sleep(100)}
  assert.ok(windowsStart,'owned Windows child must identify itself before fault');
  const before=facts(windowsStart.pid);if(action!=='rootexit')assert.ok(before?.CommandLine.includes(path.join(base,'windows-pe.cjs')));
  console.log(JSON.stringify({event:'windows-facts-before-fault',action,mode,facts:before,parentFacts:facts(windowsStart.ppid),linuxRoot,carrierPid:child.pid,carrierClosed:closed}));
  if(action==='cancel')child.stdin.end('cancel\n');
  else if(action==='carrier'){assert.equal(child.kill(),true);console.log(JSON.stringify({event:'killed-exact-owned-carrier',pid:child.pid}));}
  else child.stdin.end();
  await sleep(1000);
  console.log(JSON.stringify({event:'windows-facts-after-one-second',action,mode,carrierClosed:closed,facts:facts(windowsStart.pid),parentFacts:facts(windowsStart.ppid),linuxRecord:fs.readFileSync(record+'.linux.jsonl','utf8')}));
  await sleep(6500);
  console.log(JSON.stringify({event:'windows-facts-after-work',action,mode,carrierClosed:closed,facts:facts(windowsStart.pid),parentFacts:facts(windowsStart.ppid),windowsRecord:fs.readFileSync(record,'utf8'),linuxRecord:fs.readFileSync(record+'.linux.jsonl','utf8')}));
  assert.equal(facts(windowsStart.pid),null,'finite owned Windows child should have exited');
  assert.equal(facts(windowsStart.ppid),null,'finite owned Windows parent should have exited');
  const guestCheck=`import json,os,sys; p="/proc/"+sys.argv[1]; a=open(p+"/cmdline","rb").read().decode().replace("\\0"," ") if os.path.exists(p) else ""; print(json.dumps({"pid":int(sys.argv[1]),"exists":os.path.exists(p),"ownedMarkerMatches":sys.argv[2] in a,"cmdline":a}))`;
  await sleep(action==='carrier'?10000:1);
  const guest=execFileSync(wsl,['-d','Ubuntu','-u','plumwsl','--exec',python,'-c',guestCheck,String(linuxRoot),record],{encoding:'utf8',env});
  console.log(JSON.stringify({event:'linux-root-cleanup-facts',action,mode,facts:JSON.parse(guest),linuxRecord:fs.readFileSync(record+'.linux.jsonl','utf8')}));
  assert.equal(JSON.parse(guest).exists,false,'finite owned Linux root should have exited');
  for(let n=0;n<50&&!closed;n++)await sleep(100);
  assert.ok(closed,'owned carrier should eventually close');
  console.log(JSON.stringify({event:'case-complete',action,mode,events}));
}
(async()=>{for(const mode of (process.argv[4]?[process.argv[4]]:['inherit','detached','detached-pipe']))for(const action of (process.argv[5]?[process.argv[5]]:['rootexit','cancel','carrier']))await run(action,mode)})().catch(e=>{console.error(e);process.exitCode=1});
