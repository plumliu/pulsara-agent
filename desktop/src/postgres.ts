import type {PostgresOperations} from './postgres-operations';

export const PG_MAJOR = '17';

/** PostgreSQL owns recovery and locking. The shell only addresses its private data directory. */
export class BundledPostgres {
  readonly directory: string;
  readonly socket: string;
  initialized = false;
  startedHere = false;
  constructor(readonly operations: PostgresOperations) {
    this.directory = operations.directory;
    this.socket = operations.socket;
  }
  private async verifyRunningInstance(): Promise<void> {
    const instance = await this.operations.readInstance('resolved');
    if (instance.directory !== this.directory || instance.socket !== this.socket) throw new Error('已有数据库进程的目录或 socket 与应用专用实例不一致。');
  }
  async start(): Promise<void> {
    await this.operations.prepareLayout();
    // Unix socket path length is a platform protocol boundary, not a task lifetime cap.
    if (this.operations.socketAddressBytes >= 104) throw new Error('PULSARA_HOME 路径过长，无法创建 PostgreSQL Unix socket。请选择较短的应用数据目录。');
    let version: string | undefined;
    try { version = (await this.operations.readVersion()).trim(); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    if (version && version !== PG_MAJOR) throw new Error(`数据库目录属于 PostgreSQL ${version}，当前应用需要 ${PG_MAJOR}。请明确执行开发期数据库重置；启动失败不会自动清库。`);
    if (!version) {
      // initdb itself refuses a nonempty directory. Never erase a failed or unknown installation.
      await this.operations.run('initdb', ['-D', this.directory, '-U', 'pulsara_admin', '--auth-local=trust', '--auth-host=reject', '--encoding=UTF8', '--locale=C']);
      this.initialized = true;
    }
    try {
      await this.operations.run('pg_ctl', ['status', '-D', this.directory]);
      await this.verifyRunningInstance();
      return;
    } catch (error) {
      if ((error as {code?: unknown}).code !== 3) throw error;
    }
    const socketMode = await this.operations.socketMode();
    if ((socketMode & 0o077) !== 0) throw new Error('数据库 socket 目录必须仅当前用户可访问。');
    const socketArgument = "'" + this.socket.replaceAll("'", "'\\''") + "'";
    const options = `-c listen_addresses='' -k ${socketArgument} -c unix_socket_permissions=0700 -c port=5432 -c shared_preload_libraries=pg_search`;
    try {
      await this.operations.run('pg_ctl', ['start', '-w', '-D', this.directory, '-l', this.operations.logPath, '-o', options]);
    } catch (error) {
      // pg_ctl's readiness wait may expire while the server it launched is alive.
      // Retain ownership only after PostgreSQL confirms this exact private instance.
      try {
        await this.operations.run('pg_ctl', ['status', '-D', this.directory]);
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
    await this.operations.run('pg_ctl', ['stop', '-w', '-D', this.directory, '-m', 'fast']);
  }
  async resetConfirmed(): Promise<void> {
    // Caller has explicit user confirmation and has waited for the old kernel lock.
    if (!(await this.operations.resetPathsMatch())) throw new Error('数据库目录包含链接，拒绝重置。');
    const info = await this.operations.dataDirectoryInfo();
    if (info.uid !== info.currentUserUid || !info.isDirectory) throw new Error('数据库目录不属于当前用户。');
    const version = (await this.operations.readVersion()).trim();
    if (!/^\d+$/.test(version)) throw new Error('无法验证专用 PostgreSQL 数据目录。');
    let running = false;
    try { await this.operations.run('pg_ctl',['status','-D',this.directory]); running = true; }
    catch (error) { if ((error as {code?:unknown}).code !== 3) throw error; }
    if (running) {
      const instance = await this.operations.readInstance('raw');
      if (instance.directory !== this.directory || instance.socket !== this.socket) throw new Error('运行实例与专用目录不匹配，拒绝重置。');
      await this.stop();
    }
    await this.operations.removeDataDirectory();
    this.startedHere = false;
  }
}
