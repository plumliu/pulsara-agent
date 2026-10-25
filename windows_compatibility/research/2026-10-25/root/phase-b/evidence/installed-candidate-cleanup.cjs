const assert = require('node:assert/strict');
const path = require('node:path');
const {WslSetup,wslResourcePaths}=require('../../../desktop/dist/wsl-setup.js');
const {createWslHostOperations,executeWsl}=require('../../../desktop/dist/host-wsl.js');
const {createWslPostgresOperations}=require('../../../desktop/dist/postgres-wsl.js');
const {BundledPostgres}=require('../../../desktop/dist/postgres.js');
const {PythonRuntime}=require('../../../desktop/dist/runtime.js');
const home='/home/plumwsl/.pulsara';
const expectedSystem='7700516472209506840';
async function main() {
  const mode=process.argv[2];
  assert.ok(['inspect','stop','reset'].includes(mode));
  const target=await new WslSetup(path.join(process.env.APPDATA,'Pulsara/wsl-selection.json')).inspect({distro:'Ubuntu',user:'plumwsl'});
  assert.equal(target.uid,1000); assert.equal(target.home,'/home/plumwsl');
  const resources=wslResourcePaths(target.home+'/.local/share/pulsara/runtime/0.1.0-alpha.6');
  const host=createWslHostOperations(target,home,path.resolve(__dirname,'cleanup-logs'));
  await new PythonRuntime(resources,home,()=>{},host).waitForPreviousOwner();
  const operations=createWslPostgresOperations(resources,home,host);
  const pg=new BundledPostgres(operations);
  if(mode==='reset') {
    const control=await executeWsl(target,'/usr/bin/env',['LC_ALL=C',resources.postgresBin+'/pg_controldata',home+'/desktop/postgres']);
    assert.match(control.stdout,new RegExp('Database system identifier:\\s+'+expectedSystem+'(?:\\s|$)'));
    await pg.resetConfirmed();
    console.log(JSON.stringify({mode,kernelOwnerFree:true,systemIdentifier:expectedSystem,dataDirectoryRemoved:true,externalDevelopmentCleanup:true}));
    return;
  }
  const probe=`import json,os,psycopg
assert os.getuid()==1000
with psycopg.connect("host=/home/plumwsl/.pulsara/desktop/socket port=5432 user=pulsara_admin dbname=pulsara",autocommit=True) as c:
 row=c.execute("SELECT system_identifier::text FROM pg_control_system()").fetchone()
 facts={"systemIdentifier":row[0],"data":c.execute("SHOW data_directory").fetchone()[0],"socket":c.execute("SHOW unix_socket_directories").fetchone()[0],"listen":c.execute("SHOW listen_addresses").fetchone()[0],"sessions":c.execute("SELECT count(*) FROM pulsara_v3.sessions").fetchone()[0]}
 print(json.dumps(facts))`;
  const observed=JSON.parse((await executeWsl(target,resources.pythonExecutable,['-I','-c',probe])).stdout);
  assert.deepEqual(observed,{systemIdentifier:expectedSystem,data:home+'/desktop/postgres',socket:home+'/desktop/socket',listen:'',sessions:0});
  assert.deepEqual(await operations.readInstance('raw'),{directory:observed.data,socket:observed.socket});
  if(mode==='stop') {
    await pg.stop();
    await assert.rejects(operations.run('pg_ctl',['status','-D',observed.data]),error=>error.code===3&&error.postgresExecution==='failed');
  }
  console.log(JSON.stringify({mode,kernelOwnerFree:true,...observed,stopped:mode==='stop',externalDevelopmentCleanup:true}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
