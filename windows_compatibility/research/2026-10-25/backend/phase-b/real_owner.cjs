const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const root = path.resolve(__dirname, '../../../..');
const {BundledPostgres} = require(path.join(root, 'desktop/dist/postgres'));
const {checkWslMaintenance, createWslPostgresOperations} = require(path.join(root, 'desktop/dist/postgres-wsl'));
const {createWslHostOperations} = require(path.join(root, 'desktop/dist/host-wsl'));
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const repo = '/home/plumwsl/workspaces/pulsara_agent';
const python = repo + '/.venv/bin/python';
const resources = {root: repo + '/desktop/resources', pythonExecutable: python, postgresBin: repo + '/desktop/resources/postgres/bin', playwrightBrowsers: repo + '/desktop/resources/playwright', packageCli: ''};
const target = {distro: 'Ubuntu', user: 'plumwsl', uid: 1000, home: '/home/plumwsl'};
const physical = createWslHostOperations(target, config.home, __dirname);
const host = {...physical, spawn(file, args, options) {
  // Test-only source lookup; the real WSL transport and production PG client are unchanged.
  return physical.spawn(file, ['-c', "import sys,runpy,pulsara_agent,pulsara_agent.desktop_app;s=sys.argv.pop(1);pulsara_agent.__path__.insert(0,s);pulsara_agent.desktop_app.__path__.insert(0,s+'/desktop_app');runpy.run_module('pulsara_agent.desktop_guest_ops',run_name='__main__')", config.source, ...args.slice(2)], options);
}};
const operations = createWslPostgresOperations(resources, config.home, host);
const pg = new BundledPostgres(operations);
function facts() {
  const code = "import json,sys,os,fcntl,subprocess;from pathlib import Path;h=Path(sys.argv[1]);r={};\nfor n in ['kernel.lock','pg-lifetime.lock']:\n f=os.open(h/'desktop'/n,os.O_RDWR|os.O_NOFOLLOW)\n try:\n  fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);b=False\n except BlockingIOError:b=True\n r[n]={'busy':b,'inode':os.fstat(f).st_ino};os.close(f)\nr['dataExists']=(h/'desktop/postgres').exists();r['outside']=(h/'outside').read_text();print(json.dumps(r))";
  return JSON.parse(execFileSync(process.env.SystemRoot + '/System32/wsl.exe', ['-d','Ubuntu','-u','plumwsl','--exec',python,'-c',code,config.home], {encoding:'utf8'}));
}
(async()=>{
  execFileSync(process.env.SystemRoot + '/System32/wsl.exe',['-d','Ubuntu','-u','plumwsl','--exec',python,'-c',"from pathlib import Path;import sys;Path(sys.argv[1],'outside').write_text('preserved')",config.home]);
  await checkWslMaintenance(resources, config.home, host);
  await pg.start(); assert.equal(pg.initialized,true);assert.equal(pg.startedHere,true);
  const queryCode="import json,sys,psycopg;\nwith psycopg.connect(host=sys.argv[1],user='pulsara_admin',dbname='postgres',autocommit=True) as c:\n r=c.execute(\"SELECT version(),current_setting('data_directory'),current_setting('unix_socket_directories'),current_setting('listen_addresses')\").fetchone();print(json.dumps(r))";
  const query=JSON.parse((await physical.execFile(python,['-c',queryCode,pg.socket],{env:{PULSARA_HOME:config.home}})).stdout);
  assert.match(query[0],/PostgreSQL 17\.9/);assert.equal(query[1],pg.directory);assert.equal(query[2],pg.socket);assert.equal(query[3],'');
  const running=facts();assert.equal(running['kernel.lock'].busy,false);assert.equal(running['pg-lifetime.lock'].busy,true);
  await assert.rejects(checkWslMaintenance(resources,config.home,host),error=>error.code==='PG_LIFETIME_BUSY');
  const reused = new BundledPostgres(createWslPostgresOperations(resources,config.home,host));
  await reused.start();assert.equal(reused.startedHere,false);assert.equal(reused.initialized,false);
  // The delete operation itself must recheck, even if an earlier caller checked stopped.
  await assert.rejects(operations.removeDataDirectory(),error=>error.code==='PG_LIFETIME_BUSY' && error.postgresExecution==='not-submitted');
  await pg.resetConfirmed(); assert.equal(pg.startedHere,false);
  const removed=facts();assert.equal(removed.dataExists,false);assert.equal(removed.outside,'preserved');
  assert.equal(removed['pg-lifetime.lock'].inode,running['pg-lifetime.lock'].inode);
  assert.equal(removed['kernel.lock'].busy,false);assert.equal(removed['pg-lifetime.lock'].busy,false);
  await pg.start();await pg.stop();
  await checkWslMaintenance(resources,config.home,host);
  const stopped=facts();assert.equal(stopped['pg-lifetime.lock'].busy,false);
  fs.writeFileSync(path.join(__dirname,'real-result.json'),JSON.stringify({passed:true,config,query,running,removed,stopped,maintenanceEmpty:true,maintenanceRunningRefused:true,maintenanceStopped:true,realOwner:true,productionClient:true,productionWslHost:true},null,2));
})().catch(async error=>{
  console.error(error);
  fs.writeFileSync(path.join(__dirname,'real-failure.json'),JSON.stringify({message:error.message,stack:error.stack,code:error.code,execution:error.postgresExecution},null,2));
  try {await pg.stop();} catch {}
  process.exitCode=1;
});
