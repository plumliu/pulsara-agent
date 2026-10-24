// Mechanical compiler use: retain the original Runtime/PG and original Quit body.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '../../../..');
const ts = require(path.join(repo, 'desktop/node_modules/typescript'));
const options = { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, strict: true };
function transpile(source, name) { return ts.transpileModule(source, { compilerOptions: options, fileName: name }).outputText; }
const runtime = fs.readFileSync(path.join(repo, 'desktop/src/runtime.ts'), 'utf8').replaceAll('\r\n', '\n');
const needle = "    if (!child) return;\n    this.send(child, {type:'shutdown'}, true);";
assert.equal(runtime.split(needle).length, 2);
const candidate = runtime.replace(needle, "    if (child) this.send(child, {type:'shutdown'}, true);");
fs.writeFileSync(path.join(__dirname, 'runtime.original.cjs'), transpile(runtime, 'runtime.ts'));
fs.writeFileSync(path.join(__dirname, 'runtime.candidate.cjs'), transpile(candidate, 'runtime.ts'));
fs.writeFileSync(path.join(__dirname, 'runtime-candidate.diff'), "-    if (!child) return;\n-    this.send(child, {type:'shutdown'}, true);\n+    if (child) this.send(child, {type:'shutdown'}, true);\n");
const pg = fs.readFileSync(path.join(repo, 'desktop/src/postgres.ts'), 'utf8');
fs.writeFileSync(path.join(__dirname, 'postgres.original.cjs'), transpile(pg, 'postgres.ts'));
const main = fs.readFileSync(path.join(repo, 'desktop/src/main.ts'), 'utf8');
const parsed = ts.createSourceFile('main.ts', main, ts.ScriptTarget.ES2022, true);
const quit = parsed.statements.filter(statement => ts.isFunctionDeclaration(statement) && statement.name?.text === 'requestQuit');
assert.equal(quit.length, 1);
fs.writeFileSync(path.join(__dirname, 'main-quit.original.js'), transpile(quit[0].getText(parsed), 'main-quit.ts'));
console.log(JSON.stringify({ generated: true, candidateReplacementCount: 1, originalQuitFunctionCount: quit.length, typescript: ts.version }));
