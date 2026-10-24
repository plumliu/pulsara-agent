// Disposable integration evidence; never reads saved settings or their database.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {execFile} = require('node:child_process');
const {promisify} = require('node:util');
const execute = promisify(execFile);
const root = process.cwd();
const {BundledPostgres} = require(path.join(root, 'desktop/dist/postgres'));
const {createPosixPostgresOperations} = require(path.join(root, 'desktop/dist/postgres-posix'));
const {desktopResourcePaths} = require(path.join(root, 'desktop/dist/resource-paths'));
const {PythonRuntime} = require(path.join(root, 'desktop/dist/runtime'));
const {posixHostOperations} = require(path.join(root, 'desktop/dist/host-posix'));

(async () => {
  const home = await fs.realpath(await fs.mkdtemp('/tmp/paA-'));
  const resources = desktopResourcePaths(path.join(root, 'desktop/resources'));
  const makePg = () => new BundledPostgres(createPosixPostgresOperations(resources.postgresBin, home));
  const pg = makePg();
  const events = [];
  const runtime = new PythonRuntime(resources, home, event => events.push(event), posixHostOperations);
  try {
    await pg.start();
    assert.equal(pg.initialized, true);
    assert.equal(pg.startedHere, true);
    const second = makePg();
    await second.start();
    assert.equal(second.initialized, false);
    assert.equal(second.startedHere, false);
    const sql = `CREATE EXTENSION vector; CREATE EXTENSION pg_search;
      CREATE TABLE documents (id bigint PRIMARY KEY, body text, embedding vector(3));
      INSERT INTO documents VALUES (1,'phase a portability','[1,2,3]');
      CREATE INDEX documents_search ON documents USING bm25 (id,body) WITH (key_field='id');
      SELECT 'search=' || count(*) FROM documents WHERE body @@@ pdb.match('portability');
      SELECT 'vector=' || (embedding <-> '[1,2,3]'::vector)::text FROM documents;`;
    const result = await execute(path.join(resources.postgresBin, 'psql'), ['-XAt', '-v', 'ON_ERROR_STOP=1', '-h', pg.socket, '-U', 'pulsara_admin', '-d', 'postgres', '-c', sql]);
    assert.match(result.stdout, /search=1/);
    assert.match(result.stdout, /vector=0/);
    console.log('real PG init/reuse + pg_search/vector passed', {home});
    await pg.resetConfirmed();
    assert.equal(pg.startedHere, false);
    await assert.rejects(fs.stat(pg.directory), {code: 'ENOENT'});
    const origin = await runtime.start(async () => {await pg.start(); return pg.settings();}, true);
    assert.match(origin, /^http:\/\/127\.0\.0\.1:\d+$/);
    assert.equal((await fetch(origin)).status, 200);
    const quit = await runtime.inspectQuit();
    assert.equal(quit.type, 'quit-state');
    assert.equal(quit.activeTasks, 0);
    assert.equal(quit.scheduledTasks, 0);
    await runtime.stop();
    assert.equal(runtime.running, false);
    await runtime.waitForPreviousOwner();
    await pg.stop();
    const reopened = await runtime.start(async () => {await pg.start(); return pg.settings();}, false);
    assert.equal((await fetch(reopened)).status, 200);
    await runtime.stop();
    await runtime.waitForPreviousOwner();
    await pg.stop();
    console.log('fresh installed wheel kernel ready/inspect/normal quit/lock release/reopen passed');
    console.log('runtime events', events.map(value => value.type));
    console.log('All real POSIX lifecycle assertions passed');
  } catch (error) {
    for (const name of ['kernel.log', 'postgres.log']) {
      try { console.error(name, await fs.readFile(path.join(home, 'desktop', name), 'utf8')); } catch {}
    }
    throw error;
  } finally {
    await runtime.stop().catch(error => console.error('runtime cleanup failed', error.message));
    try {await pg.operations.run('pg_ctl', ['status', '-D', pg.directory]); await pg.stop();}
    catch (error) {if (error.code !== 3 && error.code !== 'ENOENT') throw error;}
    await fs.rm(home, {recursive:true});
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
