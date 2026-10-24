const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {spawn,execFileSync}=require('node:child_process');
const wsl=path.join(process.env.SystemRoot,'System32','wsl.exe');
const bundled=process.argv[3]==='bundled';
const python=process.argv[2]||'/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python';
const repo='/home/plumwsl/workspaces/pulsara_agent';
const home='/home/plumwsl/g0-homes/terminal-eof-20261024-interop'+(bundled?'-bundled':'');
const env={...process.env};for(const k of Object.keys(env))if(k.toUpperCase()==='WSLENV')delete env[k];
function execute(args){console.log(JSON.stringify({event:'command',file:wsl,args}));return execFileSync(wsl,args,{env,encoding:'utf8'})}
function guest(args){return ['-d','Ubuntu','-u','plumwsl','--cd',repo,'--exec',...args]}
const setup='import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); assert not p.exists(); p.parent.mkdir(mode=0o700,exist_ok=True); p.mkdir(mode=0o700); assert p.resolve()==p; print({"home":str(p),"uid":os.getuid(),"mode":oct(p.stat().st_mode & 0o777),"python":sys.version})';
console.log(execute(guest([python,'-c',setup,home])));
const guestEnv=['/usr/bin/env',...(bundled?['-u','PYTHONPATH']:['PYTHONPATH='+repo+'/src']),'PULSARA_HOME='+home,'PYTHONUNBUFFERED=1'];
console.log(execute(guest([...guestEnv,python,'-c','import importlib.util,sys; print({"python":sys.version,"moduleOrigin":importlib.util.find_spec("pulsara_agent").origin})'])));
const invocation=guest([...guestEnv,python,'-m','pulsara_agent.desktop_runtime']);
console.log(JSON.stringify({event:'runtime-source',origin:bundled?'bundled resource Python and installed wheel; PYTHONPATH unset':'explicit source PYTHONPATH; not bundled wheel',node:process.version,home,invocation}));
const child=spawn(wsl,invocation,{shell:false,stdio:['pipe','pipe','pipe'],env});
const out=[],err=[];child.stdout.on('data',b=>out.push(b));child.stderr.on('data',b=>err.push(b));
child.on('error',e=>{console.error(e);process.exitCode=1});
child.stdin.end();
child.once('close',(code,signal)=>{
  const stdout=Buffer.concat(out).toString(),stderr=Buffer.concat(err).toString();
  console.log(JSON.stringify({event:'runtime-close',pid:child.pid,code,signal,stdout,stderr}));
  try{
    assert.equal(code,0);assert.equal(signal,null);
    assert.deepEqual(stdout.trim().split('\n').map(l=>JSON.parse(l).type),['stopping','stopped']);
    console.log(execute(guest([...guestEnv,python,'-m','pulsara_agent.desktop_runtime','--wait-owner'])));
    const cleanup='import fcntl,json,os,pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]); assert str(p) in ["/home/plumwsl/g0-homes/terminal-eof-20261024-interop","/home/plumwsl/g0-homes/terminal-eof-20261024-interop-bundled"] and p.resolve()==p and not p.is_symlink(); files=[str(q.relative_to(p)) for q in p.rglob("*")]; assert set(files)<=set(["desktop","desktop/kernel.lock"]), files; q=p/"desktop/kernel.lock"; f=open(q,"a"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); print(json.dumps({"files":files,"lockAcquired":True,"settingsCreated":False,"databaseCreated":False})); f.close(); shutil.rmtree(p); print(json.dumps({"cleanup":"own exact temporary home removed","exists":p.exists()}))';
    console.log(execute(guest([python,'-c',cleanup,home])));
    console.log(JSON.stringify({event:'pass',scope:(bundled?'bundled':'source')+' runtime pre-start EOF, wait-owner and exact own-home cleanup'}));
  }catch(e){console.error(e);process.exitCode=1}
});
