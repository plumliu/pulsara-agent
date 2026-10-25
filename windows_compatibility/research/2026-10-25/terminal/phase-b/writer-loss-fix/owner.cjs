const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {BundledPostgres} = require('../../../../desktop/dist/postgres');
const {createWslPostgresOperations} = require('../../../../desktop/dist/postgres-wsl');
const {createWslHostOperations} = require('../../../../desktop/dist/host-wsl');
const python = '/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7/python/bin/python3';
const wsl = process.env.SystemRoot + '/System32/wsl.exe';
const args = ['-d','Ubuntu','-u','plumwsl','--exec',python];
const label = process.argv[2] || 'baseline';
if (!/^[a-z0-9-]+$/.test(label)) throw new Error('invalid run label');
const config = JSON.parse(execFileSync(wsl,[...args,'-c',"import json,tempfile;from pathlib import Path;r=Path(tempfile.mkdtemp(prefix='.hostlease-',dir='/home/plumwsl'));h=r/'home';w=r/'workspace';h.mkdir(mode=448);w.mkdir(mode=448);print(json.dumps(dict(directory=str(r),home=str(h),workspace=str(w))))"],{encoding:'utf8'}));
const root = '/home/plumwsl/workspaces/pulsara_agent/desktop/resources';
const resources = {root,pythonExecutable:python,postgresBin:root+'/postgres/bin',playwrightBrowsers:root+'/playwright',packageCli:''};
const host = createWslHostOperations({distro:'Ubuntu',user:'plumwsl',uid:1000,home:'/home/plumwsl'},config.home,__dirname);
const pg = new BundledPostgres(createWslPostgresOperations(resources,config.home,host));
const configPath = path.join(__dirname,label+'-config.json');
fs.writeFileSync(configPath,JSON.stringify(config));
const facts={config,formalClusterTouched:false};
(async()=>{
 try {
  await pg.start();
  const probe = label.startsWith('regression-pg') ? 'pytest_probe.py' : label.startsWith('candidate') ? 'probe_candidate.py' : 'probe.py';
  const command=['/mnt/c/python_workspace/pulsara-agent/output/phase-b/terminal/writer-loss-fix/'+probe,'/mnt/c/python_workspace/pulsara-agent/output/phase-b/terminal/writer-loss-fix/'+label+'-config.json'];
  const probePython = label.startsWith('regression-pg') ? '/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python' : python;
  try { const result=await host.execFile(probePython,command,{env:{PULSARA_HOME:config.home}});facts.probe=result; }
  catch(error){ facts.probeError={message:error.message,stdout:error.stdout,stderr:error.stderr}; }
 } finally {
  await pg.resetConfirmed();
  facts.pgResetCompleted=true;
  facts.cleanup=execFileSync(wsl,[...args,'-c',"from pathlib import Path;import sys,os,shutil,json;p=Path(sys.argv[1]);assert p.parent==Path('/home/plumwsl') and p.name.startswith('.hostlease-') and p.resolve()==p and p.stat().st_uid==os.getuid();assert not (p/'home/desktop/postgres').exists();shutil.rmtree(p);print(json.dumps(dict(removed=str(p))))",config.directory],{encoding:'utf8'});
  fs.writeFileSync(path.join(__dirname,label+'-result.json'),JSON.stringify(facts,null,2));
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
