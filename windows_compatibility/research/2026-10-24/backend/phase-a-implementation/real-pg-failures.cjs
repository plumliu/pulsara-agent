// Independent experiment only. Uses final dist and a private, newly created cluster.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {execFile} = require('node:child_process');
const {promisify} = require('node:util');
const execute = promisify(execFile);
const repo = process.cwd();
const {BundledPostgres} = require(path.join(repo, 'desktop/dist/postgres.js'));
const {createPosixPostgresOperations} = require(path.join(repo, 'desktop/dist/postgres-posix.js'));
const bin = path.join(repo, 'desktop/resources/postgres/bin');
const output = __dirname;
const results = [];
function record(scenario, value) {
  results.push({scenario, ...value});
  console.log(JSON.stringify(results.at(-1)));
}
async function run(binary, args) {
  return execute(path.join(bin, binary), args, {env: {...process.env, LC_ALL: 'C'}, maxBuffer: 4 * 1024 * 1024});
}
async function status(directory) {
  try { const value = await run('pg_ctl', ['status', '-D', directory]); return {code: 0, ...value}; }
  catch (error) {
    if (error.code === 3) return {code: 3, stdout: error.stdout, stderr: error.stderr};
    throw error;
  }
}

(async () => {
  const home = await fs.realpath(await fs.mkdtemp('/tmp/pga-'));
  process.env.HOME = home;
  const operations = createPosixPostgresOperations(bin, home);
  const original = new BundledPostgres(operations);
  const pidPath = path.join(operations.directory, 'postmaster.pid');
  const versionPath = path.join(operations.directory, 'PG_VERSION');
  const sentinel = path.join(operations.directory, 'probe-preserved');
  let actualPidFile;
  let actualVersion;
  let bodyError;
  const database = async () => (await run('psql', ['-X', '-A', '-t', '-h', operations.socket, '-p', '5432', '-U', 'pulsara_admin', '-d', 'postgres', '-c', "SELECT current_setting('server_version'), current_setting('data_directory'), current_setting('unix_socket_directories'), pg_postmaster_start_time();"])).stdout.trim();
  record('scope', {repo, dist: path.join(repo, 'desktop/dist/postgres.js'), bin, home, directory: operations.directory, socket: operations.socket});
  try {
    await original.start();
    assert.equal(original.initialized, true);
    assert.equal(original.startedHere, true);
    actualPidFile = await fs.readFile(pidPath, 'utf8');
    actualVersion = await fs.readFile(versionPath, 'utf8');
    await fs.writeFile(sentinel, 'only this experiment owns this cluster\n', {mode: 0o600});
    const initialDatabase = await database();
    record('real-start', {initialized: original.initialized, startedHere: original.startedHere, status: await status(operations.directory), database: initialDatabase});

    const socketMismatch = actualPidFile.split('\n');
    socketMismatch[4] = operations.socket + '-different';
    await fs.writeFile(pidPath, socketMismatch.join('\n'));
    const candidate = new BundledPostgres(createPosixPostgresOperations(bin, home));
    let reuseError;
    await assert.rejects(candidate.start(), error => { reuseError = error.message; return error.message === '已有数据库进程的目录或 socket 与应用专用实例不一致。'; });
    let resetError;
    await assert.rejects(candidate.resetConfirmed(), error => { resetError = error.message; return error.message === '运行实例与专用目录不匹配，拒绝重置。'; });
    assert.equal(candidate.initialized, false);
    assert.equal(candidate.startedHere, false);
    assert.equal((await status(operations.directory)).code, 0);
    assert.equal(await database(), initialDatabase);
    assert.equal(await fs.readFile(sentinel, 'utf8'), 'only this experiment owns this cluster\n');
    record('socket-mismatch-rejected', {reuseError, resetError, initialized: candidate.initialized, startedHere: candidate.startedHere, status: await status(operations.directory), databaseUnchanged: true, sentinelPreserved: true});
    await fs.writeFile(pidPath, actualPidFile);

    const rawMismatch = actualPidFile.split('\n');
    rawMismatch[1] = operations.directory + '/.';
    await fs.writeFile(pidPath, rawMismatch.join('\n'));
    const spelling = new BundledPostgres(createPosixPostgresOperations(bin, home));
    await spelling.start();
    assert.equal(spelling.startedHere, false);
    let spellingError;
    await assert.rejects(spelling.resetConfirmed(), error => { spellingError = error.message; return error.message === '运行实例与专用目录不匹配，拒绝重置。'; });
    assert.equal((await status(operations.directory)).code, 0);
    assert.equal(await database(), initialDatabase);
    record('raw-resolved-difference-retained', {reuse: 'accepted existing normalized instance', startedHere: spelling.startedHere, resetError: spellingError, status: await status(operations.directory), databaseUnchanged: true});
    await fs.writeFile(pidPath, actualPidFile);

    await original.stop();
    assert.equal((await status(operations.directory)).code, 3);
    actualPidFile = undefined;
    const control = await fs.stat(path.join(operations.directory, 'global/pg_control'));
    await fs.writeFile(versionPath, '16\n');
    const incompatible = new BundledPostgres(createPosixPostgresOperations(bin, home));
    let versionError;
    await assert.rejects(incompatible.start(), error => { versionError = error.message; return error.message === '数据库目录属于 PostgreSQL 16，当前应用需要 17。请明确执行开发期数据库重置；启动失败不会自动清库。'; });
    assert.equal(incompatible.initialized, false);
    assert.equal(incompatible.startedHere, false);
    assert.equal(await fs.readFile(versionPath, 'utf8'), '16\n');
    assert.equal(await fs.readFile(sentinel, 'utf8'), 'only this experiment owns this cluster\n');
    const afterControl = await fs.stat(path.join(operations.directory, 'global/pg_control'));
    assert.equal(afterControl.ino, control.ino);
    assert.equal(afterControl.size, control.size);
    // Restore only our own deliberate metadata edit before asking the real PG tool for status.
    await fs.writeFile(versionPath, actualVersion);
    assert.equal((await status(operations.directory)).code, 3);
    record('version-mismatch-rejected', {versionError, initialized: incompatible.initialized, startedHere: incompatible.startedHere, versionRetainedBeforeRestore: '16', dataAndSentinelPreserved: true, stopped: true});
  } catch (error) {
    bodyError = error;
    record('failure', {message: error.message, stack: error.stack});
  } finally {
    try {
      if (actualPidFile) await fs.writeFile(pidPath, actualPidFile);
      if (actualVersion) await fs.writeFile(versionPath, actualVersion);
      let current = await status(operations.directory);
      if (current.code === 0) {
        await run('pg_ctl', ['stop', '-w', '-D', operations.directory, '-m', 'fast']);
        current = await status(operations.directory);
      }
      assert.equal(current.code, 3);
      assert.equal(await fs.realpath(home), home);
      assert.ok(home.startsWith('/private/tmp/pga-') || home.startsWith('/tmp/pga-'));
      await fs.rm(home, {recursive: true});
      record('cleanup', {exactDataDirectory: operations.directory, exactPgCtl: path.join(bin, 'pg_ctl'), finalStatus: current, temporaryHomeRemoved: true});
    } catch (cleanupError) {
      record('cleanup-failed-preserved', {home, error: cleanupError.message});
      bodyError ??= cleanupError;
    }
    await fs.writeFile(path.join(output, 'real-pg-failures.json'), JSON.stringify({passed: !bodyError, results}, null, 2) + '\n');
  }
  if (bodyError) throw bodyError;
})().catch(error => { console.error(error); process.exitCode = 1; });
