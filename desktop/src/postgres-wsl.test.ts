import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {checkWslMaintenance, createWslPostgresOperations} from './postgres-wsl';
import type {HostOperations} from './host-operations';

const resources = {root: '/bundle', pythonExecutable: '/bundle/python/bin/python3', postgresBin: '/bundle/postgres/bin', playwrightBrowsers: '/bundle/playwright', packageCli: '/bundle/bin/pulsara'};
function fixture(program: string, signal?: AbortSignal, observe?: (child: ReturnType<typeof spawn>) => void) {
  let closed = false;
  const host: Pick<HostOperations, 'spawn'> = {
    spawn(file, args, options) {
      assert.equal(file, resources.pythonExecutable);
      assert.equal(options.cwd, '/home/test/private');
      assert.deepEqual(options.env, {PULSARA_HOME: '/home/test/private'});
      const method = args.at(-1)!;
      const child = spawn(process.execPath, ['-e', program, method], {stdio: ['pipe', 'pipe', 'pipe']});
      child.once('close', () => { closed = true; });
      observe?.(child);
      return child;
    },
  };
  return {host, operations: createWslPostgresOperations(resources, '/home/test/private', host, () => signal), closed: () => closed};
}
const ready = "process.stdout.write(JSON.stringify({type:'ready',method:process.argv[1]})+'\\n');";
const input = "let raw='';process.stdin.on('data',c=>raw+=c);process.stdin.on('end',()=>{";

test('finite PostgreSQL operation waits for close and preserves split UTF-8', async () => {
  const program = ready + input + `
    require('node:assert/strict').deepEqual(JSON.parse(raw), {operation:'readVersion'});
    require('node:assert/strict').equal(raw.split('\\n').length,2);
    const bytes=Buffer.from(JSON.stringify({type:'result',ok:true,value:'17中文\\n'})+'\\n');
    (async()=>{for(const b of bytes){process.stdout.write(Buffer.from([b])); await new Promise(setImmediate);}})();
  });`;
  const {operations, closed} = fixture(program);
  assert.equal(await operations.readVersion(), '17中文\n');
  assert.equal(closed(), true);
});

test('finite PostgreSQL result requires one complete valid reply and normal carrier close', async () => {
  const result = JSON.stringify({type: 'result', ok: true, value: {stdout: '', stderr: ''}}) + '\n';
  for (const [name, body] of [
    ['partial', `process.stdout.write(${JSON.stringify(result.slice(0, -1))});`],
    ['duplicate', `process.stdout.write(${JSON.stringify(result + result)});`],
    ['malformed', "process.stdout.write('{broken}\\n');"],
    ['wrong-schema', "process.stdout.write(JSON.stringify({type:'result',ok:true,value:{stdout:3,stderr:''}})+'\\n');"],
    ['invalid-utf8', "process.stdout.write(Buffer.from([255,10]));"],
    ['abnormal-close', `process.stdout.write(${JSON.stringify(result)});process.exitCode=1;`],
  ]) {
    const {operations, closed} = fixture(ready + input + body + '});');
    await assert.rejects(operations.run('pg_ctl', ['start']), error => {
      assert.equal((error as {postgresExecution: unknown}).postgresExecution, 'unknown', name);
      assert.equal(closed(), true);
      return true;
    });
  }
});

test('finite PostgreSQL physical rejection and settled command failure remain distinct', async () => {
  const busy = fixture("process.stdout.write(JSON.stringify({type:'result',ok:false,error:{code:'PG_LIFETIME_BUSY',message:'busy',execution:'not-submitted',stdout:'',stderr:''}})+'\\n');");
  await assert.rejects(busy.operations.run('pg_ctl', ['start']), error => {
    assert.equal((error as {code: unknown}).code, 'PG_LIFETIME_BUSY');
    assert.equal((error as {postgresExecution: unknown}).postgresExecution, 'not-submitted');
    return true;
  });
  const failed = fixture(ready + input + "process.stdout.write(JSON.stringify({type:'result',ok:false,error:{code:3,message:'stopped',execution:'failed',stdout:'not running',stderr:''}})+'\\n');});");
  await assert.rejects(failed.operations.run('pg_ctl', ['status']), error => {
    assert.equal((error as {code: unknown}).code, 3);
    assert.equal((error as {postgresExecution: unknown}).postgresExecution, 'failed');
    assert.equal((error as {stdout: unknown}).stdout, 'not running');
    return true;
  });
});

test('cancellation before operation blocks late ready and joins the real helper', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'pulsara-pg-eof-'));
  try {
    const marker = path.join(root, 'input-bytes');
    const abort = new AbortController();
    const program = input + `require('node:fs').writeFileSync(${JSON.stringify(marker)},String(Buffer.byteLength(raw)));` + ready + '});';
    const {operations, closed} = fixture(program, abort.signal);
    const pending = operations.readVersion();
    abort.abort();
    await assert.rejects(pending, error => {
      assert.equal((error as {postgresExecution: unknown}).postgresExecution, 'not-submitted');
      assert.equal(closed(), true);
      return true;
    });
    assert.equal(await readFile(marker, 'utf8'), '0');
  } finally { await rm(root, {recursive: true, force: true}); }
});

test('cancellation after the irreversible write preserves unknown and waits for close', async () => {
  const abort = new AbortController();
  const program = ready + input + "require('node:assert/strict').equal(JSON.parse(raw).operation,'run:pg_ctl:start');process.stdout.write(JSON.stringify({type:'result',ok:true,value:{stdout:'',stderr:''}})+'\\n');});";
  const {operations, closed} = fixture(program, abort.signal, child => child.stdin!.once('finish', () => abort.abort()));
  await assert.rejects(operations.run('pg_ctl', ['start']), error => {
    assert.equal((error as {postgresExecution: unknown}).postgresExecution, 'unknown');
    assert.equal(closed(), true);
    return true;
  });
});

test('maintenance uses the same finite client without a lifecycle action', async () => {
  const program = ready + input + "require('node:assert/strict').deepEqual(JSON.parse(raw),{operation:'maintenanceCheck'});process.stdout.write(JSON.stringify({type:'result',ok:true,value:null})+'\\n');});";
  const {host, closed} = fixture(program);
  await checkWslMaintenance(resources, '/home/test/private', host);
  assert.equal(closed(), true);
});

test('each finite reply stream keeps the existing four MiB boundary', async () => {
  for (const stream of ['stdout', 'stderr']) {
    const body = `process.${stream}.write(Buffer.alloc(4*1024*1024+1,120));process.stdout.write(JSON.stringify({type:'result',ok:true,value:{stdout:'',stderr:''}})+'\\n');`;
    const {operations, closed} = fixture(ready + input + body + '});');
    await assert.rejects(operations.run('pg_ctl', ['start']), error => {
      assert.equal((error as {postgresExecution: unknown}).postgresExecution, 'unknown');
      assert.equal(closed(), true);
      return true;
    });
  }
});
