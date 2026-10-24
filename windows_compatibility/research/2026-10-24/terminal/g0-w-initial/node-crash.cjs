const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {spawn,execFileSync}=require('node:child_process');
const base=__dirname,label='nodecrash-'+process.pid,record=path.join(base,label+'-nodecrash-native.windows.jsonl');
const python='/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function facts(pid){const raw=execFileSync('powershell.exe',['-NoProfile','-NonInteractive','-Command',`Get-CimInstance Win32_Process -Filter 'ProcessId = ${pid}' | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress`],{encoding:'utf8',windowsHide:true});return raw.trim()?JSON.parse(raw):null}
(async()=>{
  const log=fs.openSync(path.join(base,label+'.inner-host.log'),'w');
  const args=[path.join(base,'interop-lifecycle.cjs'),python,label,'native','nodecrash'];
  console.log(JSON.stringify({event:'command',file:process.execPath,args,node:process.version}));
  const host=spawn(process.execPath,args,{shell:false,stdio:['ignore',log,log],windowsHide:true});
  let row;
  for(let n=0;n<150;n++){if(fs.existsSync(record))row=fs.readFileSync(record,'utf8').trim().split('\n').filter(Boolean).map(l=>JSON.parse(l)).find(r=>r.event==='windows-child-start');if(row)break;await sleep(100)}
  assert.ok(row,'Windows child identified before fault');
  const before=facts(row.pid);assert.ok(before.CommandLine.includes(record));
  console.log(JSON.stringify({event:'before-host-crash',host:facts(host.pid),child:before,parent:facts(row.ppid)}));
  assert.ok(host.kill('SIGKILL'));
  await new Promise(r=>host.once('close',r));fs.closeSync(log);
  console.log(JSON.stringify({event:'host-crashed',hostPid:host.pid,exitCode:host.exitCode,signalCode:host.signalCode}));
  await sleep(1000);
  console.log(JSON.stringify({event:'after-host-crash-one-second',host:facts(host.pid),child:facts(row.pid),parent:facts(row.ppid),linuxRecord:fs.readFileSync(record+'.linux.jsonl','utf8')}));
  await sleep(15000);
  const child=facts(row.pid),parent=facts(row.ppid);
  console.log(JSON.stringify({event:'cleanup',host:facts(host.pid),child,parent,windowsRecord:fs.readFileSync(record,'utf8'),linuxRecord:fs.readFileSync(record+'.linux.jsonl','utf8')}));
  assert.equal(child,null);assert.equal(parent,null);
})().catch(e=>{console.error(e);process.exitCode=1});
