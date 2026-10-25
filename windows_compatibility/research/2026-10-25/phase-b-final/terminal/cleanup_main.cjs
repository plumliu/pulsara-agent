const fs=require('node:fs');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const repo=path.resolve(__dirname,'../../..');
const {BundledPostgres}=require(path.join(repo,'desktop/dist/postgres'));
const {createWslPostgresOperations,checkWslMaintenance}=require(path.join(repo,'desktop/dist/postgres-wsl'));
const {createWslHostOperations}=require(path.join(repo,'desktop/dist/host-wsl'));
const label=process.argv[2];
const cfg=JSON.parse(fs.readFileSync(path.join(__dirname,label+'-config.json')));
assert(cfg.root.startsWith('/home/plumwsl/.bmain-')&&cfg.home===cfg.root+'/home');
const root=cfg.installedRoot;
const resources={root,pythonExecutable:root+'/python/bin/python3',postgresBin:root+'/postgres/bin',playwrightBrowsers:root+'/playwright',packageCli:root+'/bin/pulsara'};
const host=createWslHostOperations({distro:'Ubuntu',user:'plumwsl',uid:1000,home:'/home/plumwsl'},cfg.home,path.join(cfg.windowsHome,'logs'));
const facts={config:cfg,externalCleanupOnly:true,formalHomeOperation:'none'};
(async()=>{
 await host.execFile(resources.pythonExecutable,['-m','pulsara_agent.desktop_runtime','--wait-owner'],{env:{PULSARA_HOME:cfg.home}});facts.originalWaitOwnerCompleted=true;
 if(cfg.scenario==='host-eof'){
  try{await checkWslMaintenance(resources,cfg.home,host);throw new Error('maintenance incorrectly admitted a live private PG');}
  catch(error){if(String(error).includes('incorrectly admitted'))throw error;facts.originalMaintenanceRejected={error:String(error),postgresExecution:error.postgresExecution};}
 }
 const before=await host.execFile(resources.postgresBin+'/psql',['-XAt','-h',cfg.home+'/desktop/socket','-U','pulsara_admin','-d','postgres','-c',"SELECT current_setting('data_directory'),system_identifier FROM pg_control_system()"],{env:{PULSARA_HOME:cfg.home}}).catch(error=>({notRunning:error.code,stderr:error.stderr}));facts.before=before;
 const pg=new BundledPostgres(createWslPostgresOperations(resources,cfg.home,host));
 await pg.resetConfirmed();facts.originalResetCompleted=true;
 const code="from pathlib import Path;import sys,os,shutil,json;r=Path(sys.argv[1]);w=Path(sys.argv[2]);assert r.parent==Path('/home/plumwsl') and r.name.startswith('.bmain-') and r.resolve()==r and r.stat().st_uid==os.getuid();assert not (r/'home/desktop/postgres').exists();assert w==r/'workspace' or (w.parent==Path('/home/plumwsl/workspaces') and w.name==r.name);remaining=[]\nfor p in Path('/proc').iterdir():\n if not p.name.isdigit() or int(p.name)==os.getpid():continue\n try:\n  if str(r).encode() in (p/'cmdline').read_bytes():remaining.append(int(p.name))\n except (FileNotFoundError,ProcessLookupError,PermissionError):pass\nassert not remaining,remaining\nif w.exists() and w.parent!=r:shutil.rmtree(w)\nshutil.rmtree(r);print(json.dumps(dict(rootRemoved=not r.exists(),workspaceRemoved=not w.exists(),remainingExactOwned=remaining)))";
 const clean=await host.execFile(resources.pythonExecutable,['-I','-c',code,cfg.root,cfg.workspace],{env:{PULSARA_HOME:cfg.home}});facts.cleanup=JSON.parse(clean.stdout);
 fs.writeFileSync(path.join(__dirname,label+'-cleanup.json'),JSON.stringify(facts,null,2));console.log(JSON.stringify(facts));
})().catch(error=>{console.error(error);process.exitCode=1;});
