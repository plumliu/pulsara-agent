const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const repo = process.cwd();
const out = path.join(repo, 'output/windows-compat-20261024/desktop/phase-a-implementation');
const asar = require(path.join(repo, 'desktop/node_modules/@electron/asar'));
const packageRoot = path.join(repo, 'output/windows-compat-20261024/root/phase-a-implementation');
const {desktopResourcePaths} = require(path.join(repo, 'desktop/dist/resource-paths.js'));
const {cliLauncherScript} = require(path.join(repo, 'desktop/dist/cli-install.js'));
const modules = ['main','runtime','postgres','resource-paths','host-operations','host-posix','postgres-operations','postgres-posix','cli-install'];
const report = {comparison: 'complete Buffer.equals, no digests', modules, packages: [], cli: [], isolation: 'No GUI, no registerCli, no real user CLI operation. Only --version/--help and an isolated source probe.'};
function execute(file, args, options) {
  const result = spawnSync(file, args, {...options, encoding: 'utf8'});
  const observed = {file, args, status: result.status, signal: result.signal, stdout: result.stdout ?? '', stderr: result.stderr ?? '', error: result.error?.message};
  assert.ifError(result.error);
  assert.equal(result.signal, null, JSON.stringify(observed));
  assert.equal(result.status, 0, JSON.stringify(observed));
  return observed;
}
try {
  for (const [label, suffix] of [['package', 'package/mac-arm64/pulsara.app'], ['installed-clone', 'installed/pulsara.app']]) {
    const app = path.join(packageRoot, suffix);
    const resources = path.join(app, 'Contents/Resources');
    const archive = path.join(resources, 'app.asar');
    const comparisons = modules.map(name => {
      const entry = `dist/${name}.js`;
      const packaged = asar.extractFile(archive, entry);
      const current = fs.readFileSync(path.join(repo, 'desktop', entry));
      const equal = packaged.equals(current);
      assert.ok(equal, `${label}: ${entry} differs from current dist`);
      return {entry, packagedBytes: packaged.length, currentBytes: current.length, equal};
    });
    report.packages.push({label, app, archive, comparisons});
    const scratch = fs.mkdtempSync(path.join(out, `cli-${label}-`));
    const relocated = path.join(scratch, "中文 资源's path");
    fs.mkdirSync(relocated);
    const copiedPython = path.join(relocated, 'python');
    // APFS clone copies every file to the test-owned tree; this is not a symlink alias.
    execute('/bin/cp', ['-cR', path.join(resources, 'python'), copiedPython]);
    assert.equal(fs.lstatSync(copiedPython).isSymbolicLink(), false);
    const pythonFiles = ['__init__.py', '__main__.py', 'cli.py'];
    for (const name of pythonFiles) {
      const relative = `lib/python3.12/site-packages/pulsara_agent/${name}`;
      assert.ok(fs.readFileSync(path.join(resources, 'python', relative)).equals(fs.readFileSync(path.join(copiedPython, relative))), `${label}: copied ${name} differs`);
    }
    const paths = desktopResourcePaths(relocated);
    const cli = path.join(scratch, 'pulsara');
    const script = cliLauncherScript(paths.pythonExecutable);
    fs.writeFileSync(cli, script, {mode: 0o755, flag: 'wx'});
    const isolatedHome = path.join(scratch, 'unused-pulsara-home');
    const env = {PATH: '/usr/bin:/bin:/usr/sbin:/sbin', LANG: 'en_US.UTF-8', PYTHONDONTWRITEBYTECODE: '1', PULSARA_HOME: isolatedHome};
    const probe = execute(paths.pythonExecutable, ['-I','-B','-c','import json,sys,pulsara_agent; print(json.dumps({"executable":sys.executable,"prefix":sys.prefix,"module":pulsara_agent.__file__,"version":pulsara_agent.__version__},ensure_ascii=False))'], {env, cwd: scratch});
    const source = JSON.parse(probe.stdout);
    assert.equal(source.executable, paths.pythonExecutable);
    assert.equal(source.prefix, copiedPython);
    assert.ok(source.module.startsWith(path.join(copiedPython,'lib/python3.12/site-packages/pulsara_agent/')));
    const version = execute(cli, ['--version'], {env, cwd: scratch});
    assert.equal(version.stdout.trim(), source.version);
    const help = execute(cli, ['--help'], {env, cwd: scratch});
    assert.match(help.stdout, /^usage: pulsara/m);
    assert.match(help.stdout, /--version/);
    assert.match(help.stdout.replace(/\s+/g, ' '), /Launch or focus the installed Pulsara desktop application/);
    assert.equal(fs.existsSync(isolatedHome), false, 'Read-only CLI unexpectedly created Pulsara home');
    fs.writeFileSync(path.join(out, `${label}-cli-version.stdout.txt`), version.stdout);
    fs.writeFileSync(path.join(out, `${label}-cli-help.stdout.txt`), help.stdout);
    report.cli.push({label, scratch, relocated, copiedPython, script, source, commands: [probe, version, help], noPulsaraHomeCreated: true});
    fs.rmSync(scratch, {recursive: true});
    report.cli.at(-1).scratchRemoved = true;
  }
  report.pass = true;
} catch (error) {
  report.pass = false;
  report.failure = error.stack;
  process.exitCode = 1;
} finally {
  fs.writeFileSync(path.join(out, 'package-cli-evidence.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({pass: report.pass, packages: report.packages.length, comparisons: report.packages.reduce((sum, item) => sum + item.comparisons.length, 0), cliRuns: report.cli.length, failure: report.failure}, null, 2));
}
