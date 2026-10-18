import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, readFile, chmod, stat, realpath, rm } from 'node:fs/promises';
import path from 'node:path';

const execute = promisify(execFile);
export const PG_MAJOR = '17';

/** PostgreSQL owns recovery and locking. The shell only addresses its private data directory. */
export class BundledPostgres {
  readonly directory: string;
  readonly socket: string;
  initialized = false;
  startedHere = false;
  constructor(readonly resourceRoot: string, readonly home: string) {
    this.directory = path.join(home, 'desktop', 'postgres');
    this.socket = path.join(home, 'desktop', 'socket');
  }
  private binary(name: string) { return path.join(this.resourceRoot, 'postgres', 'bin', name); }
  private async run(name: string, args: string[]) {
    return execute(this.binary(name), args, { env: {...process.env, LC_ALL: 'C'}, maxBuffer: 4 * 1024 * 1024 });
  }
  private async verifyRunningInstance(): Promise<void> {
    const lines = (await readFile(path.join(this.directory, 'postmaster.pid'), 'utf8')).split('\n');
    if (path.resolve(lines[1] ?? '') !== this.directory || lines[4] !== this.socket) throw new Error('已有数据库进程的目录或 socket 与应用专用实例不一致。');
  }
  async start(): Promise<void> {
    await mkdir(path.dirname(this.directory), {recursive: true, mode: 0o700});
    await mkdir(this.socket, {recursive: true, mode: 0o700});
    await chmod(this.socket, 0o700);
    // Unix socket path length is a platform protocol boundary, not a task lifetime cap.
    if (Buffer.byteLength(path.join(this.socket, '.s.PGSQL.5432')) >= 104) throw new Error('PULSARA_HOME 路径过长，无法创建 PostgreSQL Unix socket。请选择较短的应用数据目录。');
    let version: string | undefined;
    try { version = (await readFile(path.join(this.directory, 'PG_VERSION'), 'utf8')).trim(); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    if (version && version !== PG_MAJOR) throw new Error(`数据库目录属于 PostgreSQL ${version}，当前应用需要 ${PG_MAJOR}。请明确执行开发期数据库重置；启动失败不会自动清库。`);
    if (!version) {
      // initdb itself refuses a nonempty directory. Never erase a failed or unknown installation.
      await this.run('initdb', ['-D', this.directory, '-U', 'pulsara_admin', '--auth-local=trust', '--auth-host=reject', '--encoding=UTF8', '--locale=C']);
      this.initialized = true;
    }
    try {
      await this.run('pg_ctl', ['status', '-D', this.directory]);
      await this.verifyRunningInstance();
      return;
    } catch (error) {
      if ((error as {code?: unknown}).code !== 3) throw error;
    }
    const socketInfo = await stat(this.socket);
    if ((socketInfo.mode & 0o077) !== 0) throw new Error('数据库 socket 目录必须仅当前用户可访问。');
    const socketArgument = "'" + this.socket.replaceAll("'", "'\\''") + "'";
    const options = `-c listen_addresses='' -k ${socketArgument} -c unix_socket_permissions=0700 -c port=5432 -c shared_preload_libraries=pg_search`;
    try {
      await this.run('pg_ctl', ['start', '-w', '-D', this.directory, '-l', path.join(this.home, 'desktop', 'postgres.log'), '-o', options]);
    } catch (error) {
      // pg_ctl's readiness wait may expire while the server it launched is alive.
      // Retain ownership only after PostgreSQL confirms this exact private instance.
      try {
        await this.run('pg_ctl', ['status', '-D', this.directory]);
        await this.verifyRunningInstance();
        this.startedHere = true;
      } catch { /* Unknown or stopped state does not authorize a stop. */ }
      throw error;
    }
    this.startedHere = true;
  }
  settings() {
    const host = this.socket.replaceAll('\\', '\\\\').replaceAll("'", "\\'");
    return {
      runtime_dsn: `host='${host}' port=5432 dbname=pulsara user=pulsara_runtime`,
      admin_dsn: `host='${host}' port=5432 dbname=pulsara user=pulsara_admin`,
    };
  }
  async stop(): Promise<void> {
    // Called only after our Python owner has exited; never signal a PID guessed by the shell.
    await this.run('pg_ctl', ['stop', '-w', '-D', this.directory, '-m', 'fast']);
  }
  async resetConfirmed(): Promise<void> {
    // Caller has explicit user confirmation and has waited for the old kernel lock.
    if (await realpath(this.directory) !== this.directory || await realpath(path.dirname(this.directory)) !== path.dirname(this.directory)) throw new Error('数据库目录包含链接，拒绝重置。');
    const info = await stat(this.directory);
    if (info.uid !== process.getuid?.() || !info.isDirectory()) throw new Error('数据库目录不属于当前用户。');
    const version = (await readFile(path.join(this.directory, 'PG_VERSION'), 'utf8')).trim();
    if (!/^\d+$/.test(version)) throw new Error('无法验证专用 PostgreSQL 数据目录。');
    let running = false;
    try { await this.run('pg_ctl',['status','-D',this.directory]); running = true; }
    catch (error) { if ((error as {code?:unknown}).code !== 3) throw error; }
    if (running) {
      const lines = (await readFile(path.join(this.directory, 'postmaster.pid'),'utf8')).split('\n');
      if (lines[1] !== this.directory || lines[4] !== this.socket) throw new Error('运行实例与专用目录不匹配，拒绝重置。');
      await this.stop();
    }
    await rm(this.directory, {recursive: true});
    this.startedHere = false;
  }
}
