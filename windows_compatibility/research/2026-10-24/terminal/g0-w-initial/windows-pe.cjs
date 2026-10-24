const fs=require('node:fs');
const {spawn}=require('node:child_process');
const [role,record,mode,action]=process.argv.slice(2);
function log(event,extra={}) { const value={event,pid:process.pid,ppid:process.ppid,time:Date.now(),node:process.version,mode,...extra}; fs.appendFileSync(record,JSON.stringify(value)+'\n'); return value; }
if(role==='parent') {
  log('windows-parent-start');
  const child=spawn(process.execPath,[__filename,'child',record,mode,action],{shell:false,windowsHide:true,detached:mode.startsWith('detached'),stdio:mode==='detached'?['ignore','ignore','ignore','ipc']:['inherit','inherit','inherit','ipc']});
  child.once('spawn',()=>log('windows-parent-spawn',{childPid:child.pid}));
  child.once('message',()=>{log('windows-child-ready',{childPid:child.pid});child.disconnect();child.unref();if(action==='rootexit')process.exit(0);else setTimeout(()=>{log('windows-parent-finished');process.exit(0)},8000)});
} else if(role==='child') {
  const value=log('windows-child-start');
  if(mode!=='detached') console.log(JSON.stringify(value));
  if(process.send)process.send({ready:true});
  // Probe-local finite work prevents an unknown platform cleanup path leaving a permanent child.
  setTimeout(()=>{log('windows-child-finished');if(mode!=='detached')console.log(JSON.stringify({event:'windows-child-final',pid:process.pid}));},action==='nodecrash'?14000:6000);
} else {throw Error('unexpected role')}
