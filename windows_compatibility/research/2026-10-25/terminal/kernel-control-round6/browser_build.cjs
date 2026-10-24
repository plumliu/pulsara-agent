// Mechanically select the original main close handler; no copied GUI owner.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '../../../..');
const ts = require(path.join(repo, 'desktop/node_modules/typescript'));
const parsed = ts.createSourceFile('main.ts', fs.readFileSync(path.join(repo, 'desktop/src/main.ts'), 'utf8'), ts.ScriptTarget.ES2022, true);
const selected = [];
function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(parsed) === 'window.on' && node.arguments[0]?.getText(parsed) === "'close'") selected.push(node);
    ts.forEachChild(node, visit);
}
visit(parsed);
assert.equal(selected.length, 1);
const statement = ts.transpileModule(selected[0].getText(parsed) + ';', {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
fs.writeFileSync(path.join(__dirname, 'main-window-close.original.js'), statement);
let source = fs.readFileSync(path.join(__dirname, 'owner.cjs'), 'utf8');
function once(before, after) {assert.equal(source.split(before).length, 2); source = source.replace(before, after);}
once('let carrier, runtime, quitError, appExit, pgStopCalls = 0;', 'let carrier, runtime, quitError, appExit, pgStopCalls = 0, researchWindow;');
once("app.commandLine.appendSwitch('disable-gpu');", "app.commandLine.appendSwitch('disable-gpu'); app.on('window-all-closed', () => {});");
once('function originalQuit(databaseReady, startupError) {', 'function originalQuit(databaseReady, startupError, viaWindow = false) {');
once('window: undefined,', "window: researchWindow, tray: undefined, process: {platform: 'win32'},");
once('    return state.invokeOriginalQuit();', `    if (!viaWindow) return state.invokeOriginalQuit();
    return new Promise((resolve, reject) => {
        state.requestQuit = () => state.invokeOriginalQuit().then(resolve, reject);
        vm.runInContext(fs.readFileSync(path.join(__dirname, 'main-window-close.original.js'), 'utf8'), state);
        researchWindow.on('close', () => emit('actual-browser-window-close-event'));
        setTimeout(() => researchWindow.close(), 500);
    });`);
once("    if (config.scenario === 'ready-eof-gated') {", `    if (config.scenario === 'window-close') {
        const {BrowserWindow} = require('electron');
        researchWindow = new BrowserWindow({width: 640, height: 360, show: false, title: 'Pulsara G0 关闭验证', webPreferences: {sandbox: true, contextIsolation: true, nodeIntegration: false}});
        await researchWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent('<meta charset="utf-8"><h2>Pulsara G0 关闭验证</h2><p>此研究窗口将通过原 main close / requestQuit 关闭实际内核。</p>'));
        researchWindow.show();
        emit('actual-browser-window-visible', {visible: researchWindow.isVisible(), id: researchWindow.id});
        await originalQuit(true, undefined, true);
        assert.equal(appExit, 0); assert.equal(pgStopCalls, 1);
        emit('original-window-quit-completed-before-research-destroy');
        researchWindow.destroy();
    } else if (config.scenario === 'ready-eof-gated') {`);
fs.writeFileSync(path.join(__dirname, 'browser-owner.cjs'), ts.createPrinter().printFile(ts.createSourceFile('browser-owner.cjs', source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.JS)));
