import {test} from 'node:test';
import assert from 'node:assert/strict';
import {copyFile, mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {createPosixPostgresOperations} from './postgres-posix';

test('local commands distinguish no submission, a settled exit and incomplete output', async t => {
  const root = await mkdtemp(path.join(tmpdir(), 'pulsara-pg-command-'));
  try {
    // A native fixture exercises execFile on both Windows and POSIX without a shell.
    await copyFile(process.execPath, path.join(root, process.platform === 'win32' ? 'pg_ctl.exe' : 'pg_ctl'));
    const operations = createPosixPostgresOperations(root, root);
    await t.test('missing executable never submitted', async () => {
      const absent = createPosixPostgresOperations(path.join(root, 'absent'), root);
      await assert.rejects(absent.run('pg_ctl', []), error => {
        const failure = error as Error & {code: unknown; postgresExecution: unknown};
        assert.equal(failure.code, 'ENOENT');
        assert.equal(failure.postgresExecution, 'not-submitted');
        return true;
      });
    });
    await t.test('synchronous invalid argv never submitted', async () => {
      await assert.rejects(operations.run('pg_ctl', ['\0']), error => {
        const failure = error as Error & {code: unknown; postgresExecution: unknown};
        assert.equal(failure.code, 'ERR_INVALID_ARG_VALUE');
        assert.equal(failure.postgresExecution, 'not-submitted');
        return true;
      });
    });
    await t.test('real numeric nonzero preserves command output', async () => {
      await assert.rejects(operations.run('pg_ctl', ['-e', "process.stdout.write('out'); process.stderr.write('err'); process.exitCode=7"]), error => {
        const failure = error as Error & {code: unknown; stdout: unknown; stderr: unknown; postgresExecution: unknown};
        assert.equal(failure.code, 7);
        assert.equal(failure.stdout, 'out');
        assert.equal(failure.stderr, 'err');
        assert.equal(failure.postgresExecution, 'failed');
        return true;
      });
    });
    await t.test('output overflow after a real side effect remains unknown', async () => {
      const marker = path.join(root, 'executed');
      await assert.rejects(operations.run('pg_ctl', ['-e', "require('node:fs').writeFileSync(process.argv[1], 'executed'); process.stdout.write(Buffer.alloc(4*1024*1024+1, 120)); setInterval(()=>{}, 1000)", marker]), error => {
        const failure = error as Error & {code: unknown; postgresExecution: unknown};
        assert.equal(failure.code, 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER');
        assert.equal(failure.postgresExecution, 'unknown');
        return true;
      });
      assert.equal(await readFile(marker, 'utf8'), 'executed');
    });
  } finally {
    await rm(root, {recursive: true, force: true});
  }
});
