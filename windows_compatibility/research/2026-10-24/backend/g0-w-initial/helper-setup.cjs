const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const repo=process.argv[2];
const {BundledPostgres}=require(path.join(repo,'desktop/dist/postgres.js'));
const {createPosixPostgresOperations}=require(path.join(repo,'desktop/dist/postgres-posix.js'));
(async()=>{
 const home=await fs.realpath(await fs.mkdtemp('/tmp/g0h-'));
 assert.equal((await fs.stat(home)).uid,process.getuid());
 const pg=new BundledPostgres(createPosixPostgresOperations(path.join(repo,'desktop/resources/postgres/bin'),home));
 await pg.start();await pg.stop();
 console.log(JSON.stringify({home,uid:process.getuid(),data:pg.directory,socket:pg.socket}));
})().catch(error=>{console.error(error);process.exitCode=1;});
