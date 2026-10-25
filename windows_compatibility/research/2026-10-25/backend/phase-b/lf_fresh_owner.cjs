const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const https = require('node:https');
const root = path.resolve(__dirname, '../../../..');
const {BundledPostgres} = require(path.join(root, 'desktop/dist/postgres'));
const {PythonRuntime} = require(path.join(root, 'desktop/dist/runtime'));
const {createWslPostgresOperations} = require(path.join(root, 'desktop/dist/postgres-wsl'));
const {createWslHostOperations} = require(path.join(root, 'desktop/dist/host-wsl'));
const config = JSON.parse(fs.readFileSync(path.join(__dirname, 'lf-fresh-config.json'), 'utf8'));
const repo = '/home/plumwsl/workspaces/pulsara_agent';
const resources = {root:repo+'/desktop/resources', pythonExecutable:config.python,
  postgresBin:repo+'/desktop/resources/postgres/bin', playwrightBrowsers:repo+'/desktop/resources/playwright', packageCli:''};
const physical = createWslHostOperations({distro:'Ubuntu',user:'plumwsl',uid:1000,home:'/home/plumwsl'}, config.home, __dirname);
function installed(args) {
  if (args[0] !== '-m') return args;
  return ['-c', "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));m=sys.argv.pop(1);runpy.run_module(m,run_name='__main__')", config.package, ...args.slice(1)];
}
const host = {...physical,
  spawn(file,args,options) {return physical.spawn(file,installed(args),options);},
  execFile(file,args,options) {return physical.execFile(file,installed(args),options);},
};
const pg = new BundledPostgres(createWslPostgresOperations(resources,config.home,host));
const events=[];
const rounds=[];
const runtime = new PythonRuntime(resources,config.home,event=>events.push({at:Date.now(),type:event.type,error:event.error}),host,
  {distribution:'Ubuntu',user:'plumwsl',interopHelper:'/mnt/c/python_workspace/pulsara-agent/desktop/resources/windows-interop/pulsara-interop.exe',
   async openOAuthBrowser(){throw new Error('No OAuth is part of this first-start test');}});
async function query() {
  const code = `import sys,json,time;sys.path.insert(0,sys.argv.pop(1))
import pulsara_agent,psycopg
from pulsara_agent.storage.postgres_connection_provider import PostgresRuntimeConnectionFactory
from pulsara_agent.storage.migrations.registry import POSTGRES_MIGRATION_REGISTRY
d='host='+sys.argv[1]+' port=5432 dbname=pulsara user=pulsara_runtime'
b=PostgresRuntimeConnectionFactory(d).verify_deep(deadline_monotonic=time.monotonic()+60)
with psycopg.connect(d) as c:
 c.execute('SET TRANSACTION READ ONLY')
 n=c.execute('SELECT count(*) FROM pulsara_v3.sessions').fetchone()[0]
 f=c.execute("SELECT proname,position(chr(13) in prosrc) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='pulsara_v3' ORDER BY proname").fetchall()
print(json.dumps({'package':pulsara_agent.__file__,'sessions':n,'functions':f,'deep_verified':True,'execution_crlf':POSTGRES_MIGRATION_REGISTRY.definition(0).resource_bytes().count(b'\\r\\n')}))`;
  return JSON.parse((await physical.execFile(config.python,['-c',code,config.package,pg.socket],{env:{PULSARA_HOME:config.home}})).stdout);
}
function bootstrap(url,certificate) {
  return new Promise((resolve,reject)=>{
    https.get(new URL('/api/app/bootstrap',url),{ca:certificate},response=>{
      const chunks=[];response.on('data',value=>chunks.push(value));response.on('end',()=>{
        try {assert.equal(response.statusCode,200);resolve(JSON.parse(Buffer.concat(chunks).toString()));}catch(error){reject(error);}
      });
    }).on('error',reject);
  });
}
async function observeBootstrap(url,certificate) {
  // Finite read-only forwarding observation; never retry a startup/SQL write.
  const observations=[];
  const until=Date.now()+10000;
  for(;;) {
    const at=Date.now();
    try {await bootstrap(url,certificate);observations.push({at,completedAt:Date.now(),status:200});return observations;}
    catch(error) {
      observations.push({at,completedAt:Date.now(),code:error.code,message:error.message});
      if(error.code!=='ECONNREFUSED' || Date.now()>=until) {error.observations=observations;throw error;}
      await new Promise(resolve=>setTimeout(resolve,250));
    }
  }
}
(async()=>{
  for(let attempt=0;attempt<2;attempt++) {
    const url=await runtime.start(async()=>{await runtime.waitForPreviousOwner();await pg.start();return pg.settings();},()=>pg.initialized);
    assert.equal(pg.initialized,attempt===0);
    assert.match(url,/^https:\/\/127\.0\.0\.1:/);
    const readyAt=Date.now();
    const round={attempt,initialized:pg.initialized,url,readyAt};rounds.push(round);
    let networkError;
    try {round.networkReadiness=await observeBootstrap(url,runtime.certificatePem);round.bootstrap=true;}
    catch(error){networkError=error;round.networkReadiness=error.observations;}
    const sql=await query();round.sql=sql;
    assert.equal(sql.sessions,0);assert.equal(sql.execution_crlf,0);
    assert.ok(sql.functions.every(value=>value[1]===0));
    assert.ok(sql.package.startsWith(config.package+'/'));
    fs.writeFileSync(path.join(__dirname,'lf-fresh-progress.json'),JSON.stringify({config,rounds,events},null,2));
    if(networkError) throw networkError;
    await runtime.stop();
    await pg.stop();
  }
  fs.writeFileSync(path.join(__dirname,'lf-fresh-result.json'),JSON.stringify({passed:true,config,rounds,events},null,2));
})().catch(async error=>{
  fs.writeFileSync(path.join(__dirname,'lf-fresh-failure.json'),JSON.stringify({message:error.message,stack:error.stack,rounds,events},null,2));
  try {await runtime.stop();}catch{}
  try {await pg.stop();}catch{}
  console.error(error);process.exitCode=1;
});
