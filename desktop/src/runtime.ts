import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import type { HostOperations } from './host-operations';
import type { DesktopResourcePaths } from './resource-paths';

export type RuntimeEvent = {type: string; origin?: string; certificatePem?: string; url?: string; error?: string; requestId?: string; activeTasks?: number; scheduledTasks?: number; database_state?: string; phase?: 'startup' | 'runtime' | 'shutdown'; clean?: boolean};
export interface WslRuntimeOptions {
  distribution: string;
  user: string;
  interopHelper: string;
  openOAuthBrowser(url: string): Promise<void>;
}
type RuntimePostgres = {runtime_dsn: string; admin_dsn: string};
export class PythonRuntime {
  private child?: ChildProcessWithoutNullStreams;
  private preparing = false;
  private preparation?: Promise<void>;
  private exit?: Promise<{code: number | null; signal: NodeJS.Signals | null; startSent: boolean; stopped: boolean; settlementFailure: boolean; cleanStartupSettlement: boolean; failure?: string}>;
  private request = 0;
  private inspections = new Map<string, (value: RuntimeEvent) => void>();
  private certificate?: string;
  constructor(readonly resources: DesktopResourcePaths, readonly home: string, readonly onEvent: (event: RuntimeEvent) => void, private readonly host: HostOperations, private readonly wsl?: WslRuntimeOptions) {}
  get certificatePem(): string | undefined { return this.certificate; }
  async waitForPreviousOwner(): Promise<void> {
    await this.host.execFile(this.resources.pythonExecutable, ['-m', 'pulsara_agent.desktop_runtime', '--wait-owner'], {env:{...process.env,PULSARA_HOME:this.home}});
  }
  async start(preparePostgres: () => Promise<RuntimePostgres>, initialize: () => boolean): Promise<string> {
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
      this.certificate = undefined;
      let announced = false;
      let startSent = false;
      let stopped = false;
      let startupFailure = false;
      let cleanStartupSettlement = false;
      let settlementFailure = false;
      let controlFailure = false;
      let failure: string | undefined;
      this.exit = new Promise(resolve => child.once('close', (code, signal) => { this.child = undefined; resolve({code, signal, startSent, stopped, settlementFailure, cleanStartupSettlement, failure}); }));
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
        child.once('error', reject);
        child.once('close', (code, signal) => {
          for (const done of this.inspections.values()) done({type: 'failed', error: '内核进程已退出。'});
          this.inspections.clear();
          if (!announced) reject(new Error(`内核在就绪前退出 (${code ?? signal})。请查看内核日志。`));
          else this.onEvent({type: 'exited', error: `内核进程已退出 (${code ?? signal})。`});
        });
        let pending = '';
        let invalid = false;
        const decoder = new TextDecoder('utf-8', {fatal:true});
        const invalidFrame = () => {
          invalid = true;
          controlFailure = true;
          cleanStartupSettlement = false;
          startupFailure = false;
          failure = '内核返回了无效的宿主消息';
          this.send(child, {type:'shutdown'}, true);
          reject(new Error('内核返回了无效的宿主消息，正在关闭该进程。'));
        };
        const accept = (line: string) => {
          let event: RuntimeEvent;
          try {
            event = JSON.parse(line);
            if (!event || typeof event !== 'object' || typeof event.type !== 'string' || !['starting','ready','failed','stopping','stopped','quit-state','open-oauth-browser'].includes(event.type)) throw new Error('Invalid frame');
          } catch {
            invalidFrame();
            return;
          }
          if (event.type === 'open-oauth-browser') {
            void (async () => {
              let opened = false;
              try {
                if (!this.wsl || this.child !== child || child.stdin.writableEnded || child.stdin.destroyed
                  || typeof event.requestId !== 'string' || !event.requestId || typeof event.url !== 'string') throw new Error('Invalid OAuth request');
                const url = new URL(event.url);
                if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid OAuth URL');
                await this.wsl.openOAuthBrowser(url.href);
                opened = true;
              } catch { /* The SDK owner receives a failed open, never a fabricated authorization. */ }
              if (this.child === child && typeof event.requestId === 'string') this.send(child, {type:'open-oauth-browser-result', requestId:event.requestId, opened});
            })();
            return;
          }
          if (event.requestId !== undefined) { this.inspections.get(event.requestId)?.(event); this.inspections.delete(event.requestId); }
          if (event.type === 'ready' && typeof event.origin === 'string') {
            if (this.wsl && (typeof event.certificatePem !== 'string' || !event.certificatePem.includes('-----BEGIN CERTIFICATE-----'))) {
              controlFailure = true;
              failure = 'WSL 内核没有提供本次连接证书。';
              this.send(child, {type:'shutdown'}, true);
              reject(new Error(failure));
              return;
            }
            this.certificate = event.certificatePem;
            announced = true;
            startupFailure = false;
            cleanStartupSettlement = false;
            resolve(event.origin);
          }
          if (event.type === 'stopped') {
            stopped = true;
            settlementFailure ||= event.clean === false;
            cleanStartupSettlement = startupFailure && !announced && !controlFailure && !settlementFailure && event.clean === true;
          }
          if (event.type === 'failed') {
            if (!controlFailure) failure = event.error ?? '内核报告失败。';
            if (!announced && event.phase !== 'startup') controlFailure = true;
            settlementFailure ||= event.phase === 'shutdown';
            startupFailure = !announced && !controlFailure && event.phase === 'startup';
            cleanStartupSettlement = false;
            reject(new Error(failure));
          }
          this.onEvent(event);
        };
        child.stdout.on('data', (chunk: Buffer) => {
          if (invalid) return;
          try {
            pending += decoder.decode(chunk, {stream:true});
            let newline: number;
            while (!invalid && (newline = pending.indexOf('\n')) >= 0) {
              const line = pending.slice(0, newline);
              pending = pending.slice(newline + 1);
              accept(line);
            }
          } catch { invalidFrame(); }
        });
        child.stdout.on('end', () => {
          try { pending += decoder.decode(); if (pending) invalidFrame(); }
          catch { invalidFrame(); }
        });
        child.stdout.on('error', invalidFrame);
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
      startSent = this.send(child, {type: 'start', postgres, initialize:initialize(),
        ...(this.wsl ? {host: {platform:'win32', executionDomain:'wsl', distribution:this.wsl.distribution, user:this.wsl.user, interopHelper:this.wsl.interopHelper}} : {}),
      });
      return ready;
    } finally {
      this.preparing = false;
      prepared();
    }
  }
  private send(child: ChildProcessWithoutNullStreams, value: object, end = false): boolean {
    if (child.exitCode !== null || child.signalCode !== null || child.stdin.destroyed || child.stdin.writableEnded) return false;
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
    if (child) this.send(child, {type:'shutdown'}, true);
    const result = await this.exit;
    const settledStartupFailure = result?.cleanStartupSettlement && result.code === 1 && result.signal === null;
    if (result?.startSent && !settledStartupFailure && (result.code !== 0 || result.failure || result.settlementFailure || (this.wsl && !result.stopped))) {
      throw new Error(`内核退出时报告收尾失败 (${result.failure ?? (result.settlementFailure ? '内核报告收尾不完整' : this.wsl && !result.stopped ? '没有收到完整收尾消息' : result.code ?? result.signal)})；数据库尚未停止，请检查日志。`);
    }
  }
}
