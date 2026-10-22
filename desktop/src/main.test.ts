import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

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
} = {}) {
  const cover = deferred();
  const previous = deferred();
  const database = deferred();
  const spawned = deferred();
  const preparingDatabase = deferred();
  const stopping = deferred();
  const calls: string[] = [];
  const failures: string[] = [];
  const handlers = new Map<string, (event: unknown) => Promise<unknown>>();
  const dialogOptions: { defaultPath: string; properties: string[] }[] = [];
  const app = Object.assign(new EventEmitter(), {
    isPackaged: false, setName() {}, setPath() {}, getPath(name: string) {
      if (name === 'home') return '/tmp/home';
      if (name === 'desktop') {
        if (options.desktop instanceof Error) throw options.desktop;
        return options.desktop ?? '/tmp/home/桌面';
      }
      return '/tmp/pulsara-main-test';
    },
    requestSingleInstanceLock() { return true; }, exit(code: number) { calls.push('exit:' + code); }, quit() {},
    whenReady() { return { then(action: () => Promise<void>) { return ready = action(); } }; },
  });
  let ready: Promise<void>;
  class Window extends EventEmitter {
    static current: Window;
    constructor() { super(); Window.current = this; }
    webContents = Object.assign(new EventEmitter(), {
      mainFrame: { url: 'http://127.0.0.1:12345/' },
      setWindowOpenHandler() {}, session: { setPermissionRequestHandler() {} },
    });
    isMinimized() { return false; }
    show() {}
    focus() {}
    async loadURL(url: string) { calls.push('navigate:' + url); }
  }
  class Cover {
    accepts() { return true; }
    async show(error = '', phase = 'starting') {
      if (phase === 'stopping') { calls.push('stopping-cover'); stopping.resolve(); return; }
      if (error) { failures.push(error); calls.push('failure-cover'); return; }
      calls.push('cover-loading');
      await cover.promise;
      calls.push('cover-loaded');
    }
  }
  class Postgres {
    async start() {
      calls.push('database-preparing'); preparingDatabase.resolve();
      await database.promise;
      calls.push('database-prepared');
    }
    settings() { return { runtime_dsn: 'runtime', admin_dsn: 'admin' }; }
  }
  class Runtime {
    running = false;
    preparation?: Promise<void>;
    async waitForPreviousOwner() { calls.push('owner-wait'); await previous.promise; }
    async start(prepare: () => Promise<unknown>) {
      this.running = true;
      calls.push('child-spawned'); spawned.resolve();
      const preparation = prepare();
      this.preparation = preparation.then(() => {}, () => {});
      try {
        await preparation;
        calls.push('start-frame');
        return 'http://127.0.0.1:12345/';
      } catch (error) {
        // PythonRuntime owns the real EOF/close behavior, covered by its
        // physical-child tests. Here main must await that owner's rejection.
        calls.push('child-eof-and-close');
        this.running = false;
        throw error;
      }
    }
    async inspectQuit() { return { activeTasks: 0, scheduledTasks: 0 }; }
    async stop() { await this.preparation; }
  }
  const electron = {
    app, BrowserWindow: Window,
    dialog: { async showOpenDialog(parent: Window, input: typeof dialogOptions[number]) {
      assert.equal(parent, Window.current);
      dialogOptions.push(input);
      return { canceled: options.canceled ?? false, filePaths: options.canceled ? [] : ['/tmp/chosen/项目 '] };
    } },
    ipcMain: { handle(name: string, action: (event: unknown) => Promise<unknown>) { handlers.set(name, action); } },
    Menu: { buildFromTemplate() {}, setApplicationMenu() {} },
  };
  const modules: Record<string, unknown> = {
    electron,
    'node:fs/promises': { async stat() {
      if (options.desktopState === 'missing') throw new Error('ENOENT');
      return { isDirectory: () => options.desktopState !== 'file' };
    } },
    'node:child_process': { execFileSync() { return '/tmp/pulsara-main-test'; } },
    './postgres': { BundledPostgres: Postgres }, './runtime': { PythonRuntime: Runtime },
    './ui-preferences': { UiPreferences: class {} },
    './cli-install': {}, './oauth-return': {},
    './browser-owner': { BrowserOwner: class { async retireHost() {} } },
    './startup-cover': { StartupCover: Cover },
  };
  runInNewContext(readFileSync(path.join(__dirname, 'main.js'), 'utf8'), {
    exports: {}, __dirname, console, URL, Error,
    process: { platform: 'darwin', env: {} },
    require(name: string) { return Object.hasOwn(modules, name) ? modules[name] : require(name); },
  });
  return { dialogOptions, trustedEvent: () => ({ sender: Window.current.webContents, senderFrame: Window.current.webContents.mainFrame }), cover, previous, database, spawned, preparingDatabase, stopping, handlers, calls, failures, ready: ready! };
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
