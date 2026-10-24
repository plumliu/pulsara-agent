import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import type { HostOperations } from './host-operations';
import type { DesktopResourcePaths } from './resource-paths';

export type RuntimeEvent = {type: string; origin?: string; error?: string; requestId?: string; activeTasks?: number; scheduledTasks?: number; database_state?: string};
type RuntimePostgres = {runtime_dsn: string; admin_dsn: string};
export class PythonRuntime {
  private child?: ChildProcessWithoutNullStreams;
  private preparing = false;
  private preparation?: Promise<void>;
  private exit?: Promise<{code: number | null; signal: NodeJS.Signals | null}>;
  private reachedReady = false;
  private request = 0;
  private inspections = new Map<string, (value: RuntimeEvent) => void>();
  constructor(readonly resources: DesktopResourcePaths, readonly home: string, readonly onEvent: (event: RuntimeEvent) => void, private readonly host: HostOperations) {}
  async waitForPreviousOwner(): Promise<void> {
    await this.host.execFile(this.resources.pythonExecutable, ['-m', 'pulsara_agent.desktop_runtime', '--wait-owner'], {env:{...process.env,PULSARA_HOME:this.home}});
  }
  async start(preparePostgres: () => Promise<RuntimePostgres>, initialize: boolean): Promise<string> {
    if (this.child || this.preparing) throw new Error('内核进程尚未退出，不能启动第二个内核。');
    this.preparing = true;
    let prepared!: () => void;
    this.preparation = new Promise(resolve => { prepared = resolve; });
    try {
      await this.host.prepareRuntimeDirectory(this.home);
      const child = this.host.spawn(this.resources.pythonExecutable, ['-m', 'pulsara_agent.desktop_runtime'], {
        env: {...process.env, PULSARA_HOME: this.home, PYTHONUNBUFFERED: '1', PLAYWRIGHT_BROWSERS_PATH: this.resources.playwrightBrowsers},
        cwd: this.home,
      });
      this.child = child;
      this.reachedReady = false;
      this.exit = new Promise(resolve => child.once('close', (code, signal) => { this.child = undefined; resolve({code, signal}); }));
      child.stdin.on('error', (error: NodeJS.ErrnoException) => {
        // A child closing its inherited pipe is an ordinary exit race. Shutdown
        // still waits for 'close'; a pipe failure never authorizes force-killing.
        if (!['EPIPE', 'ERR_STREAM_DESTROYED', 'ERR_STREAM_WRITE_AFTER_END'].includes(error.code ?? '')) {
          this.onEvent({type:'failed', error:`内核控制通道不可用：${error.message}`});
        }
      });
      child.stderr.on('data', (chunk: Buffer) => {
        void this.host.appendRuntimeLog(this.home, chunk)
          .catch(error => this.onEvent({type:'failed', error:`无法写入内核日志：${String(error)}`}));
      });
      const ready = new Promise<string>((resolve, reject) => {
        let announced = false;
        child.once('error', reject);
        child.once('close', (code, signal) => {
          for (const done of this.inspections.values()) done({type: 'failed', error: '内核进程已退出。'});
          this.inspections.clear();
          if (!announced) reject(new Error(`内核在就绪前退出 (${code ?? signal})。请查看内核日志。`));
          else this.onEvent({type: 'exited', error: `内核进程已退出 (${code ?? signal})。`});
        });
        const lines = createInterface({input: child.stdout});
        lines.on('line', line => {
          let event: RuntimeEvent;
          try {
            event = JSON.parse(line);
            if (!event || typeof event !== 'object' || typeof event.type !== 'string' || !['starting','ready','failed','stopping','stopped','quit-state'].includes(event.type)) throw new Error('Invalid frame');
          } catch {
            this.send(child, {type:'shutdown'}, true);
            reject(new Error('内核返回了无效的宿主消息，正在关闭该进程。'));
            return;
          }
          if (event.requestId !== undefined) { this.inspections.get(event.requestId)?.(event); this.inspections.delete(event.requestId); }
          if (event.type === 'ready' && typeof event.origin === 'string') { announced = true; this.reachedReady = true; resolve(event.origin); }
          if (event.type === 'failed') reject(new Error(event.error ?? '内核启动失败。'));
          this.onEvent(event);
        });
      });
      // The child can import the kernel while PostgreSQL starts. Its original
      // stdin start frame still gates every database operation. Observe early
      // child failures while the database owner finishes physical preparation.
      void ready.catch(() => {});
      let postgres: RuntimePostgres;
      try {
        postgres = await preparePostgres();
      } catch (error) {
        // No start frame was sent. EOF lets the original child owner exit
        // without acquiring a writer or touching an unready database.
        if (!child.stdin.destroyed && !child.stdin.writableEnded) child.stdin.end();
        await this.exit;
        throw error;
      }
      this.send(child, {type: 'start', postgres, initialize});
      return ready;
    } finally {
      this.preparing = false;
      prepared();
    }
  }
  private send(child: ChildProcessWithoutNullStreams, value: object, end = false): boolean {
    if (child.stdin.destroyed || child.stdin.writableEnded) return false;
    const line = JSON.stringify(value) + '\n';
    if (end) child.stdin.end(line);
    else child.stdin.write(line);
    return true;
  }
  get running() { return this.child !== undefined || this.preparing; }
  async inspectQuit(): Promise<RuntimeEvent> {
    // Before the start frame there can be no accepted work. Do not put an
    // inspection ahead of that frame while PostgreSQL is still preparing.
    if (this.preparing || !this.child) return {type: 'inspect-quit', activeTasks: 0, scheduledTasks: 0};
    const requestId = String(++this.request);
    return new Promise(resolve => {
      // This bounds only a UI status query. Unknown status asks the user; it never kills work.
      const timer = setTimeout(() => { this.inspections.delete(requestId); resolve({type:'failed', error:'暂时无法确认后台工作状态。'}); }, 2000);
      this.inspections.set(requestId, value => { clearTimeout(timer); resolve(value); });
      if (!this.send(this.child!, {type:'inspect-quit', requestId})) {
        clearTimeout(timer);
        this.inspections.delete(requestId);
        resolve({type:'failed', error:'内核正在关闭，无法读取后台工作状态。'});
      }
    });
  }
  async stop(): Promise<void> {
    await this.preparation;
    const child = this.child;
    if (!child) return;
    this.send(child, {type:'shutdown'}, true);
    const result = await this.exit;
    if (this.reachedReady && result?.code !== 0) throw new Error(`内核退出时报告收尾失败 (${result?.code ?? result?.signal})；数据库尚未停止，请检查日志。`);
  }
}
