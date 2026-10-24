// Research shell: actual compiled Runtime/PG and mechanically extracted main Quit.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { spawn, execFile, spawnSync } = require('node:child_process');
const { promisify } = require('node:util');
const { PassThrough } = require('node:stream');
const assert = require('node:assert/strict');
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const repo = path.resolve(__dirname, '../../../..');
const { PythonRuntime } = require(config.candidate ? './runtime.candidate.cjs' : './runtime.original.cjs');
const { BundledPostgres } = require('./postgres.original.cjs');
const { createClient } = require('./distro-client.cjs');
const linux = '/home/plumwsl/workspaces/pulsara_agent';
const bundledPython = linux + '/desktop/resources/python/bin/python3';
const guestPython = bundledPython;
const wsl = path.join(process.env.SystemRoot, 'System32', 'wsl.exe');
const wslBase = ['-d', "PulsaraG0-kernel6-20261025-d88d705e", '-u', 'plumwsl', '--cd', linux, '--exec'];
const execute = promisify(execFile);
const rows = [];
let carrier, runtime, quitError, appExit, pgStopCalls = 0;
function emit(event, facts = {}) {
    const row = { event, time: Date.now(), ...facts };
    rows.push(row);
    fs.appendFileSync(config.hostRecord, JSON.stringify(row) + '\n');
}
function guestArgs(action, ...args) { return [...wslBase, guestPython, config.guestControl, action, config.home, ...args]; }
function control(action, ...args) {
    const completed = spawnSync(wsl, guestArgs(action, ...args), { encoding: 'utf8', windowsHide: true, maxBuffer: 4 * 1024 * 1024 });
    assert.equal(completed.status, 0, completed.stderr);
    const value = completed.stdout.trim() ? JSON.parse(completed.stdout) : null;
    emit('kernel-observation', { action, value });
    return value;
}
async function until(predicate, seconds = 25) {
    const deadline = Date.now() + seconds * 1000;
    while (!predicate()) {
        assert(Date.now() < deadline, 'research observation deadline');
        await new Promise(resolve => setTimeout(resolve, 5));
    }
}
function kernelRows() {
    if (!fs.existsSync(config.kernelRecordWindows))
        return [];
    return fs.readFileSync(config.kernelRecordWindows, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
}
const pgClient = createClient(config.home, row => emit('original-pg-client', { row }));
const operations = Object.create(pgClient.operations);
operations.run = async (binary, args) => {
    if (binary === 'pg_ctl' && args[0] === 'stop') {
        pgStopCalls += 1;
        emit('before-original-pg-stop', { state: control('state', config.kernelRecord), runtimeRunning: runtime.running });
    }
    return pgClient.operations.run(binary, args);
};
const postgres = new BundledPostgres(operations);
const resources = { root: linux + '/desktop/resources', pythonExecutable: bundledPython, playwrightBrowsers: linux + '/desktop/resources/playwright', postgresBin: linux + '/desktop/resources/postgres/bin', packageCli: linux + '/desktop/resources/bin/pulsara' };
const host = {
    resolveHome: () => config.home,
    async prepareRuntimeDirectory() { await execute(wsl, guestArgs('prepare'), { windowsHide: true }); },
    async appendRuntimeLog(_home, chunk) {
        const child = spawn(wsl, guestArgs('log'), { windowsHide: true, stdio: ['pipe', 'ignore', 'pipe'] });
        child.stdin.end(chunk);
        await new Promise((resolve, reject) => { child.once('error', reject); child.once('close', code => code === 0 ? resolve() : reject(new Error('kernel log append failed'))); });
    },
    async execFile(file, args, options) {
        return execute(wsl, [...wslBase, '/usr/bin/env', 'PULSARA_HOME=' + config.home, file, ...args], { windowsHide: true, encoding: 'utf8' });
    },
    spawn(file, args, options) {
        const environment = ['/usr/bin/env', 'PULSARA_HOME=' + config.home, 'PYTHONUNBUFFERED=1', 'PLAYWRIGHT_BROWSERS_PATH=' + resources.playwrightBrowsers, 'PYTHONPATH=' + config.researchDirectory, 'G0_KERNEL_RECORD=' + config.kernelRecord, 'G0_KERNEL_MODE=' + config.scenario, 'G0_KERNEL_CLOSE_GATE=' + (config.closeGate ? '1' : '0')];
        carrier = spawn(wsl, [...wslBase, ...environment, file, ...args], { windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'] });
        emit('actual-wsl-carrier-spawned', { pid: carrier.pid, file, args, home: config.home });
        carrier.once('close', (code, signal) => { emit('actual-wsl-carrier-close', { code, signal }); queueMicrotask(() => emit('after-original-child-clear', { runtimeRunning: runtime.running, childPresent: Boolean(runtime.child), code, signal })); });
        const write = carrier.stdin.write.bind(carrier.stdin);
        let firstStart = true;
        carrier.stdin.write = (chunk, encoding, callback) => {
            const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, typeof encoding === 'string' ? encoding : 'utf8');
            emit('actual-kernel-control-write', { base64: bytes.toString('base64'), text: bytes.toString('utf8') });
            if (firstStart && bytes.includes(Buffer.from('"type":"start"'))) {
                firstStart = false;
                if (config.scenario === 'fragmented') {
                    const index = bytes.findIndex(value => value >= 128);
                    assert(index >= 0, 'Unicode DSN gives a real multi-byte split');
                    write(bytes.subarray(0, index + 1));
                    setImmediate(() => { write(bytes.subarray(index + 1, index + 2)); setImmediate(() => write(bytes.subarray(index + 2))); });
                    emit('start-frame-fragmented-inside-utf8', { index });
                    return true;
                }
                if (config.scenario === 'after-start-eof') {
                    const accepted = write(bytes);
                    carrier.stdin.end();
                    emit('real-stdin-eof-after-start');
                    return accepted;
                }
            }
            return write(chunk, encoding, callback);
        };
        const end = carrier.stdin.end.bind(carrier.stdin);
        carrier.stdin.end = (...values) => { emit('actual-kernel-control-end', { text: typeof values[0] === 'string' ? values[0] : null }); return end(...values); };
        const raw = carrier.stdout;
        if (['malformed-output', 'partial-output', 'unicode-failure'].includes(config.scenario)) {
            const filtered = new PassThrough();
            carrier.stdout = filtered;
            let injected = false;
            raw.on('data', chunk => {
                emit('underlying-real-kernel-output', { base64: chunk.toString('base64') });
                if (config.scenario === 'unicode-failure') {
                    for (const byte of chunk)
                        filtered.write(Buffer.from([byte]));
                }
                else if (!injected) {
                    injected = true;
                    if (config.scenario === 'partial-output')
                        filtered.end('{"type":"rea');
                    else
                        filtered.write('not-json\n');
                    emit('research-output-channel-fault', { kind: config.scenario });
                }
            });
            raw.on('end', () => { if (!filtered.writableEnded)
                filtered.end(); });
        }
        return carrier;
    },
};
runtime = new PythonRuntime(resources, config.home, event => emit('original-runtime-event', { event }), host);
function originalQuit(databaseReady, startupError) {
    const state = { runtime, postgres, starting: undefined, resetting: false, quittingRequest: false, quitting: false, databaseReady, startupError,
        window: undefined, focusWindow() { }, dialog: { showMessageBox: async () => ({ response: 1 }) },
        async showStartup(message, phase) { emit('original-main-startup-message', { message, phase }); if (message.startsWith('退出尚未完成'))
            quitError = message; },
        app: { exit(code) { appExit = code; emit('original-main-app-exit-requested', { code }); } }, };
    vm.createContext(state);
    vm.runInContext(fs.readFileSync(path.join(__dirname, 'main-quit.original.js'), 'utf8') + '\nthis.invokeOriginalQuit = requestQuit;', state);
    return state.invokeOriginalQuit();
}
async function main() {
    emit('research-host-started', { pid: process.pid, versions: process.versions, execPath: process.execPath, candidate: config.candidate, scenario: config.scenario });
    let startupError, origin;
    const started = runtime.start(async () => {
        if (['before-start-eof', 'carrier-before-start', 'host-before-start'].includes(config.scenario)) {
            await until(() => kernelRows().some(row => row.event === 'kernel-process-started'));
            if (config.scenario === 'before-start-eof')
                throw new Error('G0 refused database preparation before start frame');
            if (config.scenario === 'host-before-start') {
                emit('research-host-kill-ready', { state: control('state', config.kernelRecord) });
                await new Promise(() => { });
            }
            carrier.kill('SIGKILL');
            await until(() => runtime.child === undefined);
            return postgres.settings();
        }
        await postgres.start();
        return postgres.settings();
    }, true);
    try {
        origin = await started;
        emit('actual-kernel-ready', { origin, state: control('state', config.kernelRecord) });
    }
    catch (error) {
        startupError = error;
        emit('actual-runtime-start-rejected', { message: error.message });
    }
    if (config.scenario === 'distro-loss') {
        assert(origin);
        emit('temporary-distro-terminate-ready');
        await until(() => runtime.child === undefined, 120);
        emit('temporary-distro-carrier-closed-before-restart', {runtimeRunning: runtime.running});
        await until(() => fs.existsSync(config.resumeObservationPath), 120);
    }
    if (config.scenario === 'host-loss') {
        assert(origin);
        emit('research-host-kill-ready', { state: control('state', config.kernelRecord) });
        await new Promise(() => { });
    }
    if (config.scenario === 'kernel-crash') {
        assert(origin);
        control('signal', config.kernelRecord, 'SIGKILL');
        await until(() => runtime.child === undefined);
        emit('before-quit-after-kernel-crash', { runtimeRunning: runtime.running, state: control('state', config.kernelRecord) });
    }
    if (config.scenario === 'carrier-loss') {
        assert(origin);
        carrier.kill('SIGKILL');
        await until(() => runtime.child === undefined);
        const lost = control('observe-loss', config.kernelRecord);
        emit('before-quit-after-carrier-loss', { runtimeRunning: runtime.running, state: lost });
    }
    if (config.scenario === 'ready-eof-gated') {
        assert(origin);
        carrier.stdin.end();
        const gate = await execute(wsl, guestArgs('wait-close-gate', config.kernelRecord), { windowsHide: true, encoding: 'utf8' });
        emit('clean-eof-close-gate-observed', { value: JSON.parse(gate.stdout) });
        const pendingQuit = originalQuit(true, undefined);
        await new Promise(resolve => setTimeout(resolve, 50));
        const held = control('state', config.kernelRecord);
        emit('clean-eof-main-still-joining', { pgStopCalls, runtimeRunning: runtime.running, state: held });
        assert.equal(pgStopCalls, 0);
        assert.equal(held.kernel.alive, true);
        assert.equal(held.lock.acquired, false);
        control('release-close');
        await pendingQuit;
    }
    else
        await originalQuit(Boolean(origin), startupError);
    emit('original-main-quit-returned', { appExit: appExit ?? null, quitError: quitError ?? null, pgStopCalls, runtimeRunning: runtime.running, state: control('state', config.kernelRecord) });
    if (config.closeGate)
        control('release-close');
    await runtime.waitForPreviousOwner();
    emit('original-wait-previous-owner-returned', { state: control('state', config.kernelRecord) });
    fs.writeFileSync(config.resultPath, JSON.stringify({ rows, appExit: appExit ?? null, quitError: quitError ?? null, pgStopCalls }, null, 2));
}
async function executeMain() {
    try {
        await main();
        emit('research-case-finished');
    }
    catch (error) {
        emit('research-case-failed', { error: error.stack });
        fs.writeFileSync(config.resultPath, JSON.stringify({ rows, failure: error.stack }, null, 2));
        process.exitCode = 1;
    }
}
if (process.versions.electron) {
    const { app } = require('electron');
    app.setPath('userData', config.profilePath);
    app.commandLine.appendSwitch('disable-gpu');
    app.whenReady().then(async () => { await executeMain(); app.exit(process.exitCode || 0); });
}
else {
    executeMain();
}
