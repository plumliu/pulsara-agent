// Output-only probe. Production BundledPostgres retains lifecycle ownership.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {execFile} = require('node:child_process');
const {promisify} = require('node:util');
const exec = promisify(execFile);
const repo = process.argv[2];
const output = __dirname;
const {BundledPostgres} = require(path.join(repo,'desktop/dist/postgres.js'));
const {createPosixPostgresOperations} = require(path.join(repo,'desktop/dist/postgres-posix.js'));
const bin = path.join(repo,'desktop/resources/postgres/bin');
const python = path.join(repo,'desktop/resources/python/bin/python3');
const records = [];
function record(scenario, values={}) { const item={scenario,...values}; records.push(item); console.log(JSON.stringify(item)); }
async function run(binary,args) {return exec(path.join(bin,binary),args,{env:{...process.env,LC_ALL:'C'},maxBuffer:4*1024*1024});}
async function status(data) { try { const result=await run('pg_ctl',['status','-D',data]); return {code:0,...result}; } catch(error) { if(error.code===3)return {code:3,stdout:error.stdout,stderr:error.stderr}; throw error; } }
(async()=>{
  const home=await fs.realpath(await fs.mkdtemp('/tmp/g0p-'));
  process.env.PULSARA_HOME=home;
  const operations=createPosixPostgresOperations(bin,home);
  const pg=new BundledPostgres(operations);
  const pid=path.join(operations.directory,'postmaster.pid'), version=path.join(operations.directory,'PG_VERSION');
  const sentinel=path.join(operations.directory,'g0-preserved');
  let originalPid, originalVersion, failure;
  const query=async()=> (await run('psql',['-XAt','-h',operations.socket,'-p','5432','-U','pulsara_admin','-d','postgres','-c',"SELECT current_setting('data_directory'),pg_postmaster_start_time();"])).stdout.trim();
  try {
    record('scope',{repo,home,directory:operations.directory,socket:operations.socket,uid:process.getuid(),bin,python});
    await pg.start(); assert.equal(pg.initialized,true); assert.equal(pg.startedHere,true);
    originalPid=await fs.readFile(pid,'utf8'); originalVersion=await fs.readFile(version,'utf8');
    await fs.writeFile(sentinel,'private G0 sentinel\n',{mode:0o600});
    const initial=await query(); record('real-start',{initialized:pg.initialized,startedHere:pg.startedHere,status:await status(operations.directory),database:initial});
    const reuse=new BundledPostgres(createPosixPostgresOperations(bin,home)); await reuse.start(); assert.equal(reuse.startedHere,false); record('reuse',{startedHere:reuse.startedHere});
    const queried=await exec(python,['-I',path.join(output,'queries.py'),home],{env:{...process.env,PULSARA_HOME:home},maxBuffer:4*1024*1024});
    record('clean-v0-search-vector',{stdout:queried.stdout,stderr:queried.stderr});
    for(const [scenario,line,value] of [['socket-mismatch',4,operations.socket+'-other'],['raw-directory-mismatch',1,operations.directory+'/.']]) {
      const changed=originalPid.split('\n');changed[line]=value;await fs.writeFile(pid,changed.join('\n'));
      const candidate=new BundledPostgres(createPosixPostgresOperations(bin,home));
      if(line===4) await assert.rejects(candidate.start(),/已有数据库进程的目录或 socket 与应用专用实例不一致/);
      else {await candidate.start();assert.equal(candidate.startedHere,false);}
      await assert.rejects(candidate.resetConfirmed(),/运行实例与专用目录不匹配，拒绝重置/);
      assert.equal(await query(),initial);assert.equal((await status(operations.directory)).code,0);assert.equal(await fs.readFile(sentinel,'utf8'),'private G0 sentinel\n');
      record(scenario,{rejected:true,runningDataPreserved:true}); await fs.writeFile(pid,originalPid);
    }
    await pg.stop();assert.equal((await status(operations.directory)).code,3);
    // A real pg_ctl zero-second readiness wait is a fault input, not a product deadline.
    const zeroWaitOps=createPosixPostgresOperations(bin,home);
    const physicalRun=zeroWaitOps.run;
    zeroWaitOps.run=(binary,args)=>physicalRun(binary,binary==='pg_ctl'&&args[0]==='start'?[...args,'-t','0']:args);
    const zeroWait=new BundledPostgres(zeroWaitOps);
    let readinessError;
    try {await zeroWait.start();} catch(error) {readinessError={message:error.message,code:error.code,stdout:error.stdout,stderr:error.stderr};}
    if(readinessError) {
      assert.notEqual(readinessError.code,3);
      record('real-readiness-failure',{error:readinessError,startedHere:zeroWait.startedHere,initialized:zeroWait.initialized,readback:await status(operations.directory)});
      // Allow the fault-injected startup to publish its instance facts before cleanup.
      await new Promise(resolve=>setTimeout(resolve,200));
      const instance=await zeroWaitOps.readInstance('resolved');assert.equal(instance.directory,operations.directory);assert.equal(instance.socket,operations.socket);
      await run('pg_ctl',['stop','-w','-D',operations.directory,'-m','fast']);
    } else {record('real-readiness-failure-not-triggered',{startedHere:zeroWait.startedHere});await zeroWait.stop();}
    assert.equal((await status(operations.directory)).code,3);
    const control=await fs.stat(path.join(operations.directory,'global/pg_control'));
    await fs.writeFile(version,'16\n'); const incompatible=new BundledPostgres(createPosixPostgresOperations(bin,home));
    await assert.rejects(incompatible.start(),/数据库目录属于 PostgreSQL 16/);
    const preserved=await fs.stat(path.join(operations.directory,'global/pg_control'));
    assert.equal(preserved.ino,control.ino);assert.equal(preserved.size,control.size);assert.equal(await fs.readFile(version,'utf8'),'16\n');assert.equal(await fs.readFile(sentinel,'utf8'),'private G0 sentinel\n');
    record('version-refusal',{preserved:true,status3BeforeEdit:true});await fs.writeFile(version,originalVersion);
    const reopened=new BundledPostgres(createPosixPostgresOperations(bin,home));await reopened.start();assert.equal(reopened.initialized,false);assert.equal(reopened.startedHere,true);record('reopen',{query:await query()});
    const outside=path.join(home,'workspace-preserved');await fs.writeFile(outside,'outside PG data\n');
    await reopened.resetConfirmed();originalPid=undefined;originalVersion=undefined;
    await assert.rejects(fs.stat(operations.directory),{code:'ENOENT'});assert.equal(await fs.readFile(outside,'utf8'),'outside PG data\n');
    await assert.rejects(run('pg_ctl',['status','-D',operations.directory]),error=>error.code===4&&error.stderr.includes('does not exist'));
    record('precise-reset',{onlyOwnedDataRemoved:true,statusAfterRemoval:4,socketRetained:(await fs.stat(operations.socket)).isDirectory(),outsidePreserved:true});
    const rebuilt=new BundledPostgres(createPosixPostgresOperations(bin,home));await rebuilt.start();assert.equal(rebuilt.initialized,true);await rebuilt.stop();record('reset-rebuild',{initialized:true,finalStatus:await status(operations.directory)});
  } catch(error) {failure=error;record('failure',{message:error.message,stack:error.stack,stdout:error.stdout,stderr:error.stderr});}
  finally {
    try {
      if(originalPid) await fs.writeFile(pid,originalPid);if(originalVersion)await fs.writeFile(version,originalVersion);
      let current=await status(operations.directory);if(current.code===0){await run('pg_ctl',['stop','-w','-D',operations.directory,'-m','fast']);current=await status(operations.directory);}assert.equal(current.code,3);
      assert.ok(home.startsWith('/tmp/g0p-'));assert.equal(await fs.realpath(home),home);assert.equal((await fs.stat(home)).uid,process.getuid());
      await fs.rm(home,{recursive:true});record('cleanup',{home,finalStatus:current,removed:true});
    } catch(error) {failure??=error;record('cleanup-failed-preserved',{home,message:error.message});}
    await fs.writeFile(path.join(output,'pg-contract.json'),JSON.stringify({passed:!failure,records},null,2)+'\n');
  }
  if(failure)throw failure;
})().catch(error=>{console.error(error);process.exitCode=1;});
