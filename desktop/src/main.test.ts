import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import { cliLauncherScript, type CliRegistration } from './cli-install';
import type { DesktopResourcePaths } from './resource-paths';
import {wslResourcePaths} from './wsl-setup';

function deferred<T = void>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function fixture(options: {
  desktop?: string | Error;
  desktopState?: 'directory' | 'missing' | 'file';
  canceled?: boolean;
  chosenDirectory?: string;
  platform?: 'darwin' | 'linux' | 'win32';
  packagedResources?: string;
  stopFailure?: Error;
  wslSelected?: boolean;
  maintenance?: boolean;
  uninstallMode?: string;
  maintenanceFailure?: Error;
  initialized?: boolean;
  windowDestroyGate?: Promise<void>;
  sessionRetireFailure?: Error;
  backendReadyGate?: Promise<void>;
} = {}) {
  const cover = deferred();
  const previous = deferred();
  const database = deferred();
  const spawned = deferred();
  const preparingDatabase = deferred();
  const stopping = deferred();
  const destroying = deferred();
  const calls: string[] = [];
  const switches: string[] = [];
  let readyRequested = false;
  const workbenchUrl = options.platform === 'win32' ? 'https://127.0.0.1:12345/' : 'http://127.0.0.1:12345/';
  const failures: string[] = [];
  const handlers = new Map<string, (event: unknown, value?: unknown) => Promise<unknown>>();
  const dialogOptions: { defaultPath: string; properties: string[] }[] = [];
  const composition: {
    titleBarStyle?: string;
    titleBarOverlay?: {height: number; color: string; symbolColor: string};
    autoHideMenuBar?: boolean;
    python?: string;
    runtime?: { resources: DesktopResourcePaths; home: string; host: unknown };
    postgres?: { bin: string; home: string };
    cli?: CliRegistration;
    initialize?: boolean;
  } = {};
  const host = { resolveHome(python: string) { composition.python = python; return '/tmp/pulsara-main-test'; } };
  const app = Object.assign(new EventEmitter(), {
    commandLine: {appendSwitch(name: string) { assert.equal(readyRequested, false); switches.push(name); }},
    isPackaged: options.packagedResources !== undefined, setName() {}, setPath() {}, setAsDefaultProtocolClient() { return true; }, getPath(name: string) {
      if (name === 'home') return '/tmp/home';
      if (name === 'desktop') {
        if (options.desktop instanceof Error) throw options.desktop;
        return options.desktop ?? '/tmp/home/桌面';
      }
      return '/tmp/pulsara-main-test';
    },
    requestSingleInstanceLock() { return true; }, exit(code: number) { calls.push('exit:' + code); }, quit() {}, getVersion() { return 'test-version'; },
    whenReady() { readyRequested = true; return { then(action: () => Promise<void>) { return ready = action(); } }; },
  });
  let ready: Promise<void>;
  class Window extends EventEmitter {
    static current: Window;
    constructor(options: {titleBarStyle?: string; titleBarOverlay?: typeof composition.titleBarOverlay; autoHideMenuBar?: boolean}) {
      super(); Window.current = this; composition.titleBarStyle = options.titleBarStyle;
      composition.titleBarOverlay = options.titleBarOverlay; composition.autoHideMenuBar = options.autoHideMenuBar;
    }
    setTitleBarOverlay(options: typeof composition.titleBarOverlay) { composition.titleBarOverlay = options; }
    webContents = Object.assign(new EventEmitter(), {
      mainFrame: { url: workbenchUrl },
      isDestroyed() { return false; },
      setWindowOpenHandler() {}, session: { setPermissionRequestHandler() {} },
    });
    isMinimized() { return false; }
    isMaximized() { return false; }
    getBounds() { return {x:0,y:0,width:1440,height:920}; }
    setBounds() {}
    destroy() {
      calls.push('window-destroyed'); destroying.resolve();
      const finish = () => { this.webContents.emit('destroyed'); this.emit('closed'); };
      if (options.windowDestroyGate) void options.windowDestroyGate.then(finish); else finish();
    }
    show() {}
    focus() {}
    async loadURL(url: string) { calls.push('navigate:' + url); }
  }
  class Cover {
    accepts() { return true; }
    async retire() { calls.push('cover-retired'); }
    async show(error = '', phase = 'starting') {
      if (phase === 'stopping') { calls.push('stopping-cover'); stopping.resolve(); return; }
      if (phase === 'setup') { calls.push('setup-cover'); return; }
      if (error) { failures.push(error); calls.push('failure-cover'); return; }
      calls.push('cover-loading');
      await cover.promise;
      calls.push('cover-loaded');
    }
  }
  class Postgres {
    initialized = false;
    constructor(operations: { bin: string; home: string }) { composition.postgres = operations; }
    async start() {
      calls.push('database-preparing'); preparingDatabase.resolve();
      await database.promise;
      this.initialized = options.initialized ?? true;
      calls.push('database-prepared');
    }
    settings() { return { runtime_dsn: 'runtime', admin_dsn: 'admin' }; }
    async stop() { calls.push('database-stopped'); }
  }
  class Runtime {
    certificatePem = 'test-certificate';
    constructor(resources: DesktopResourcePaths, home: string, _onEvent: unknown, operations: unknown) {
      composition.runtime = { resources, home, host: operations };
    }
    running = false;
    preparation?: Promise<void>;
    async waitForPreviousOwner() { calls.push('owner-wait'); await previous.promise; }
    async start(prepare: () => Promise<unknown>, initialize: () => boolean) {
      this.running = true;
      calls.push('child-spawned'); spawned.resolve();
      const preparation = prepare();
      this.preparation = preparation.then(() => {}, () => {});
      try {
        await preparation;
        composition.initialize = initialize();
        calls.push('start-frame');
        return workbenchUrl;
      } catch (error) {
        // PythonRuntime owns the real EOF/close behavior, covered by its
        // physical-child tests. Here main must await that owner's rejection.
        calls.push('child-eof-and-close');
        this.running = false;
        throw error;
      }
    }
    async inspectQuit() { return { activeTasks: 0, scheduledTasks: 0 }; }
    async stop() {
      await this.preparation;
      if (options.stopFailure) throw options.stopFailure;
    }
  }
  const electron = {
    app, BrowserWindow: Window,
    dialog: { showErrorBox(_title: string, message: string) { failures.push(message); }, async showOpenDialog(parent: Window, input: typeof dialogOptions[number]) {
      assert.equal(parent, Window.current);
      dialogOptions.push(input);
      return { canceled: options.canceled ?? false, filePaths: options.canceled ? [] : [options.chosenDirectory ?? '/tmp/chosen/项目 '] };
    } },
    ipcMain: { handle(name: string, action: (event: unknown) => Promise<unknown>) { handlers.set(name, action); }, on() {} },
    Menu: { buildFromTemplate() {}, setApplicationMenu() {} },
  };
  const modules: Record<string, unknown> = {
    './wsl-maintenance': {
      async checkInstalledWslMaintenance() { calls.push('maintenance-check'); if (options.maintenanceFailure) throw options.maintenanceFailure; },
      async uninstallWsl(_home: string, _version: string, mode: string) { calls.push(`uninstall:${mode}`); if (options.maintenanceFailure) throw options.maintenanceFailure; },
    },
    electron,
    'node:fs/promises': { async stat() {
      if (options.desktopState === 'missing') throw new Error('ENOENT');
      return { isDirectory: () => options.desktopState !== 'file' };
    } },
    './host-posix': { posixHostOperations: host },
    './postgres-posix': { createPosixPostgresOperations(bin: string, home: string) { return { bin, home }; } },
    './postgres': { BundledPostgres: Postgres }, './runtime': { PythonRuntime: Runtime },
    './host-wsl': {createWslHostOperations() { return host; }},
    './postgres-wsl': {createWslPostgresOperations(resources: DesktopResourcePaths, home: string) { return {bin:resources.postgresBin, home}; }},
    './wsl-setup': {
      WslSetup: class {
        async discover() { return {distros:[], saved:options.wslSelected ? {distro:'Ubuntu', user:'default-user'} : undefined}; }
        async inspect(selection: object) { calls.push('wsl-inspect'); return {...selection, uid:1000, home:'/home/default-user'}; }
        async select(selection: object) { calls.push('wsl-select'); return {...selection, uid:1000, home:'/home/default-user'}; }
        async prepare(target: object) { calls.push('wsl-prepare'); return {target, home:'/home/default-user/.pulsara', resources:wslResourcePaths('/home/default-user/runtime'), hostHelperPath:'/mnt/c/Pulsara/interop.exe'}; }
      },
      async loadWslPayload() { return {version:'test-version'}; },
    },
    './backend-session': {createBackendSession(input: {origin: string; certificatePem: string}) {
      assert.equal(input.origin, 'https://127.0.0.1:12345'); assert.equal(input.certificatePem, 'test-certificate');
      calls.push('tls-installed');
      return {session:{}, async waitUntilReachable(signal: AbortSignal) {
        calls.push('tls-readiness');
        if (options.backendReadyGate) await Promise.race([options.backendReadyGate, new Promise<void>((_resolve,reject) => {
          const abort = () => { calls.push('tls-readiness-aborted'); reject(signal.reason); };
          if (signal.aborted) abort(); else signal.addEventListener('abort',abort,{once:true});
        })]);
        calls.push('tls-ready');
      }, seal() { calls.push('tls-sealed'); }, async retire(settle: () => Promise<void>) {
        calls.push('tls-retire-attempt');
        if (options.sessionRetireFailure) throw options.sessionRetireFailure;
        await settle(); calls.push('tls-retired');
      }};
    }},
    './ui-preferences': { UiPreferences: class {
      values: Record<string,string> = {};
      read() { return {...this.values}; }
      write(input: {key:string;value:string}) { this.values[input.key] = input.value; }
    } },
    './cli-install': {
      cliLauncherScript,
      async cliStatus(config: CliRegistration) { composition.cli = config; return { state: 'ready' }; },
      async registerCli(config: CliRegistration) { composition.cli = config; return { state: 'ready' }; },
    }, './oauth-return': {},
    './browser-owner': { BrowserOwner: class { async retireHost() {} } },
    './startup-cover': { StartupCover: Cover },
  };
  runInNewContext(readFileSync(path.join(__dirname, 'main.js'), 'utf8'), {
    exports: {}, __dirname, console, URL, Error, AbortController,
    process: { platform: options.platform ?? 'darwin', resourcesPath: options.packagedResources, env: {}, argv:options.uninstallMode ? ['Pulsara.exe',`--pulsara-uninstall=${options.uninstallMode}`] : options.maintenance ? ['Pulsara.exe','--pulsara-maintenance-check'] : [] },
    require(name: string) { return Object.hasOwn(modules, name) ? modules[name] : require(name); },
  });
  return { composition, host, dialogOptions, trustedEvent: () => ({ sender: Window.current.webContents, senderFrame: Window.current.webContents.mainFrame }), cover, previous, database, spawned, preparingDatabase, stopping, destroying, handlers, calls, switches, failures, ready: ready! };
}

test('Windows requires environment confirmation before creating a kernel or preparing PostgreSQL', async () => {
  const value = fixture({platform:'win32'});
  await value.ready;
  assert.equal(value.composition.titleBarStyle, 'hidden');
  assert.equal(value.composition.autoHideMenuBar, true);
  assert.deepEqual(value.switches, ['disable-gpu-compositing']);
  assert.equal(JSON.stringify(value.composition.titleBarOverlay), JSON.stringify({height:44,color:'#faf8f2',symbolColor:'#32343d'}));
  assert.ok(value.calls.includes('setup-cover'));
  assert.equal(value.calls.includes('child-spawned'), false);
  assert.equal(value.calls.includes('database-preparing'), false);
  await value.handlers.get('desktop:quit')!({});
  assert.equal(value.calls.at(-1), 'exit:0');
});

for (const failure of [undefined, new Error('kernel busy')]) {
  test(`Windows installer check is finite and never starts the product: ${failure ? 'busy' : 'idle'}`, async () => {
    const value = fixture({platform:'win32', maintenance:true, maintenanceFailure:failure});
    await value.ready.catch(() => {});
    await Promise.resolve();
    assert.deepEqual(value.calls, ['maintenance-check', `exit:${failure ? 1 : 0}`]);
    assert.equal(value.handlers.size, 0);
    assert.equal(value.composition.runtime, undefined);
    assert.equal(value.composition.postgres, undefined);
  });
}

for (const mode of ['keep', 'all']) for (const failure of [undefined, new Error('cleanup failed')]) {
  test(`Windows uninstall ${mode} exits without starting the product: ${failure ? 'failed' : 'done'}`, async () => {
    const value = fixture({platform:'win32', uninstallMode:mode, maintenanceFailure:failure});
    await value.ready.catch(() => {});
    await Promise.resolve();
    assert.deepEqual(value.calls, [`uninstall:${mode}`, `exit:${failure ? 1 : 0}`]);
    assert.equal(value.handlers.size, 0);
    assert.equal(value.composition.runtime, undefined);
    assert.equal(value.composition.postgres, undefined);
  });
}

test('Windows revalidates its saved target and pins a fresh window before workbench navigation', async () => {
  const value = fixture({platform:'win32', wslSelected:true});
  value.previous.resolve(); value.database.resolve(); value.cover.resolve();
  await value.ready;
  assert.equal(value.composition.runtime!.home, '/home/default-user/.pulsara');
  assert.equal(value.composition.runtime!.resources.pythonExecutable, '/home/default-user/runtime/python/bin/python3');
  assert.ok(value.calls.indexOf('wsl-inspect') < value.calls.indexOf('wsl-prepare'));
  assert.ok(value.calls.indexOf('wsl-prepare') < value.calls.indexOf('child-spawned'));
  assert.ok(value.calls.indexOf('tls-installed') < value.calls.indexOf('navigate:https://127.0.0.1:12345/'));
  await value.handlers.get('desktop:quit')!(value.trustedEvent());
  assert.ok(value.calls.indexOf('tls-sealed') < value.calls.indexOf('tls-retired'));
  assert.ok(value.calls.indexOf('tls-retired') < value.calls.indexOf('database-stopped'));
  assert.equal(value.calls.at(-1), 'exit:0');
});

test('Windows native overlay follows the saved application theme', async () => {
  const value = fixture({platform:'win32',wslSelected:true});
  value.previous.resolve(); value.database.resolve(); value.cover.resolve();
  await value.ready;
  await value.handlers.get('desktop:write-ui-preference')!(value.trustedEvent(),{key:'pulsara-theme',value:'dark'});
  assert.equal(value.composition.titleBarOverlay?.color,'#202228');
  assert.equal(value.composition.titleBarOverlay?.symbolColor,'#dad5cb');
  await value.handlers.get('desktop:write-ui-preference')!(value.trustedEvent(),{key:'pulsara-theme',value:'light'});
  assert.equal(value.composition.titleBarOverlay?.color,'#faf8f2');
});

for (const candidate of [
  {path: '\\\\wsl.localhost\\Ubuntu\\home\\default-user\\中文 项目', linuxPath: '/home/default-user/中文 项目'},
  {path: '\\\\wsl$\\Ubuntu\\tmp', linuxPath: '/tmp'},
  {path: 'C:\\Users\\user\\Documents', linuxPath: null},
  {path: '\\\\wsl.localhost\\OtherUbuntu\\home\\default-user', linuxPath: null},
  {path: '\\\\wsl.localhost\\Ubuntu\\home\\default-user\\..\\other', linuxPath: null},
]) {
  test(`Windows native workspace browse returns a candidate, never admission: ${candidate.path}`, async () => {
    const value = fixture({platform:'win32', wslSelected:true, chosenDirectory:candidate.path});
    value.previous.resolve(); value.database.resolve(); value.cover.resolve();
    await value.ready;
    const result = await value.handlers.get('desktop:choose-workspace-directory')!(value.trustedEvent());
    assert.equal(JSON.stringify(result), JSON.stringify(candidate));
    assert.equal(value.dialogOptions[0].defaultPath, '\\\\wsl.localhost\\Ubuntu\\home\\default-user');
    assert.deepEqual(Array.from(value.dialogOptions[0].properties), ['openDirectory']);
  });
}

test('Windows native workspace browse cancellation returns no candidate', async () => {
  const value = fixture({platform:'win32', wslSelected:true, canceled:true});
  value.previous.resolve(); value.database.resolve(); value.cover.resolve();
  await value.ready;
  assert.equal(await value.handlers.get('desktop:choose-workspace-directory')!(value.trustedEvent()), null);
});

for (const initialized of [false, true]) {
  test(`Windows derives initialization permission after the PG attempt: ${initialized}`, async () => {
    const value = fixture({platform:'win32', wslSelected:true, initialized});
    value.previous.resolve(); value.database.resolve(); value.cover.resolve();
    await value.ready;
    assert.equal(value.composition.initialize, initialized);
  });
}

test('Windows quit during native window replacement joins retirement and never navigates the retiring session', async () => {
  const gate = deferred();
  const value = fixture({platform:'win32', wslSelected:true, windowDestroyGate:gate.promise});
  value.previous.resolve(); value.database.resolve(); value.cover.resolve();
  await value.destroying.promise;
  const quit = value.handlers.get('desktop:quit')!({});
  await Promise.resolve();
  assert.equal(value.calls.includes('database-stopped'), false);
  gate.resolve();
  await Promise.all([value.ready, quit]);
  assert.equal(value.calls.some(call => call.startsWith('navigate:')), false);
  assert.ok(value.calls.indexOf('tls-retired') < value.calls.indexOf('database-stopped'));
  assert.equal(value.calls.at(-1), 'exit:0');
});

test('Windows repeated Quit retains failed Session retirement and cannot stop PostgreSQL or exit cleanly', async () => {
  const failure = new Error('Session connections did not retire');
  const value = fixture({platform:'win32', wslSelected:true, sessionRetireFailure:failure});
  value.previous.resolve(); value.database.resolve(); value.cover.resolve();
  await value.ready;
  const startupMessages = value.failures.length;
  for (let attempt = 0; attempt < 2; attempt++) {
    await value.handlers.get('desktop:quit')!(value.trustedEvent());
    assert.equal(value.calls.includes('database-stopped'), false);
    assert.equal(value.calls.includes('exit:0'), false);
  }
  assert.ok(value.calls.filter(call => call === 'tls-retire-attempt').length >= 2);
  assert.equal(value.failures.length - startupMessages, 2);
  assert.ok(value.failures.slice(startupMessages).every(message => message.includes(failure.message)));
});

for (const quitBeforeReachable of [false, true]) {
  test(`Windows waits for its pinned host connection before navigation: ${quitBeforeReachable ? 'cancel' : 'reachable'}`, async () => {
    const reachable = deferred();
    const value = fixture({platform:'win32', wslSelected:true, backendReadyGate:reachable.promise});
    value.previous.resolve(); value.database.resolve(); value.cover.resolve();
    // Let the real main orchestration reach the injected transport boundary.
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(value.calls.includes('tls-readiness'));
    assert.equal(value.calls.some(call => call.startsWith('navigate:')), false);
    if (quitBeforeReachable) {
      await value.handlers.get('desktop:quit')!(value.trustedEvent());
      await value.ready;
      assert.ok(value.calls.includes('tls-readiness-aborted'));
      assert.equal(value.calls.some(call => call.startsWith('navigate:')), false);
      assert.equal(value.calls.at(-1), 'exit:0');
    } else {
      reachable.resolve();
      await value.ready;
      assert.ok(value.calls.indexOf('tls-ready') < value.calls.indexOf('navigate:https://127.0.0.1:12345/'));
    }
  });
}

test('main overlaps cover loading and kernel preparation but gates the start frame and navigation', async () => {
  const value = fixture();
  await value.spawned.promise;
  assert.equal(value.calls.includes('owner-wait'), true);
  assert.equal(value.calls.includes('database-preparing'), false);
  assert.equal(value.calls.includes('start-frame'), false);
  value.previous.resolve();
  await value.preparingDatabase.promise;
  assert.equal(value.calls.includes('cover-loaded'), false);
  value.database.resolve();
  await Promise.resolve();
  assert.equal(value.calls.includes('start-frame'), false);
  value.cover.resolve();
  await value.ready;
  assert.deepEqual(value.calls.slice(-3), ['cover-loaded', 'start-frame', 'navigate:http://127.0.0.1:12345/']);
  assert.deepEqual(value.failures, []);
});

test('cover failure joins PostgreSQL preparation and child cleanup before showing its error', async () => {
  const value = fixture();
  value.previous.resolve();
  await value.preparingDatabase.promise;
  value.cover.reject(new Error('cover document unavailable'));
  await Promise.resolve();
  assert.deepEqual(value.failures, []);
  assert.equal(value.calls.includes('child-eof-and-close'), false);
  value.database.resolve();
  await value.ready;
  assert.deepEqual(value.failures, ['cover document unavailable']);
  assert.deepEqual(value.calls.slice(-3), ['database-prepared', 'child-eof-and-close', 'failure-cover']);
  assert.equal(value.calls.includes('start-frame'), false);
});

test('database failure waits for the original cover before replacing it', async () => {
  const value = fixture();
  value.previous.resolve();
  await value.preparingDatabase.promise;
  value.database.reject(new Error('database preparation failed'));
  await Promise.resolve();
  assert.deepEqual(value.failures, []);
  value.cover.resolve();
  await value.ready;
  assert.deepEqual(value.failures, ['database preparation failed']);
  assert.deepEqual(value.calls.slice(-3), ['cover-loaded', 'child-eof-and-close', 'failure-cover']);
  assert.equal(value.calls.includes('start-frame'), false);
});

test('previous-owner failure joins the cover and child cleanup without preparing PostgreSQL', async () => {
  const value = fixture();
  value.previous.reject(new Error('old owner unavailable'));
  await Promise.resolve();
  assert.deepEqual(value.failures, []);
  value.cover.resolve();
  await value.ready;
  assert.deepEqual(value.failures, ['old owner unavailable']);
  assert.equal(value.calls.includes('database-preparing'), false);
  assert.equal(value.calls.includes('start-frame'), false);
  assert.equal(value.calls.includes('child-eof-and-close'), true);
});

test('quit during the old-owner wait settles the imported child without starting PostgreSQL', async () => {
  const value = fixture();
  await value.spawned.promise;
  const quitting = value.handlers.get('desktop:quit')!({});
  await value.stopping.promise;
  value.previous.resolve();
  value.cover.resolve();
  await Promise.all([quitting, value.ready]);
  assert.equal(value.calls.includes('database-preparing'), false);
  assert.equal(value.calls.includes('start-frame'), false);
  assert.equal(value.calls.includes('child-eof-and-close'), true);
  assert.equal(value.calls.at(-1), 'exit:0');
  assert.deepEqual(value.failures, []);
});

for (const failed of [false, true]) {
  test(`quit ${failed ? 'keeps PostgreSQL and reports an unclean runtime exit' : 'stops PostgreSQL after a clean runtime exit'}`, async () => {
    const stopFailure = failed ? new Error('kernel cleanup failed') : undefined;
    const value = fixture({stopFailure});
    value.previous.resolve(); value.database.resolve(); value.cover.resolve();
    await value.ready;
    await value.handlers.get('desktop:quit')!(value.trustedEvent());
    assert.equal(value.calls.includes('database-stopped'), !failed);
    assert.equal(value.calls.includes('exit:0'), !failed);
    if (failed) {
      assert.deepEqual(value.failures, ['退出尚未完成：kernel cleanup failed']);
    } else {
      assert.deepEqual(value.failures, []);
      assert.deepEqual(value.calls.slice(-2), ['database-stopped', 'exit:0']);
    }
  });
}

for (const scenario of [
  { name: 'localized desktop', options: { desktop: '/tmp/home/桌面' }, expected: '/tmp/home/桌面' },
  { name: 'configured desktop elsewhere', options: { desktop: '/mnt/work/Desktop' }, expected: '/mnt/work/Desktop' },
  { name: 'desktop lookup unavailable', options: { desktop: new Error('Unavailable') }, expected: '/tmp/home' },
  { name: 'desktop missing', options: { desktopState: 'missing' as const }, expected: '/tmp/home' },
  { name: 'desktop path is a file', options: { desktopState: 'file' as const }, expected: '/tmp/home' },
  { name: 'desktop configured as home', options: { desktop: '/tmp/home' }, expected: '/tmp/home' },
  { name: 'cancellation', options: { canceled: true }, expected: '/tmp/home/桌面' },
]) {
  test(`directory picker uses ${scenario.name} without changing the selected path`, async () => {
    const value = fixture(scenario.options);
    value.previous.resolve();
    value.database.resolve();
    value.cover.resolve();
    await value.ready;
    const selected = await value.handlers.get('desktop:choose-directory')!(value.trustedEvent());
    assert.equal(selected, scenario.options.canceled ? null : '/tmp/chosen/项目 ');
    assert.equal(value.dialogOptions.length, 1);
    assert.equal(value.dialogOptions[0].defaultPath, scenario.expected);
    assert.deepEqual(Array.from(value.dialogOptions[0].properties), ['openDirectory', 'createDirectory']);
  });
}
