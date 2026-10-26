import assert from 'node:assert/strict';
import { cp, mkdtemp, readFile, readdir, rm, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createPackage, extractFile, listPackage } from '@electron/asar';
import { getFileMatchers } from 'app-builder-lib/out/fileMatcher';

for (const platform of ['win', 'mac', 'linux'] as const) {
test(`${platform} shell archive keeps only startup fonts and preserves workbench font assets`, async () => {
  const root = path.resolve(__dirname, '..');
  const metadata = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'));
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'pulsara-shell-fonts-'));
  try {
    const staging = path.join(temporary, 'app');
    const matchers = getFileMatchers(metadata.build, 'files', staging, {
      defaultSrc: root, globalOutDir: path.join(root, 'release'),
      customBuildOptions: metadata.build[platform], macroExpander: value => value,
    });
    assert.ok(matchers);
    assert.equal(matchers.length, 1);
    const filter = matchers[0].createFilter();
    await cp(root, staging, { recursive: true, filter: async source => filter(source, await stat(source)) });
    const archive = path.join(temporary, 'app.asar');
    await createPackage(staging, archive);
    const files = listPackage(archive, { isPack: false }).map(value => value.replace(/^\//, ''));
    const html = extractFile(archive, 'assets/startup.html').toString();
    assert.match(html, /href="startup-fonts.css"/);
    assert.ok(!files.includes('assets/fonts.css'));
    const css = extractFile(archive, 'assets/startup-fonts.css').toString();
    const startupFonts = [...css.matchAll(/url\("\.\/fonts\/([^"/]+)"\)/g)].map(match => match[1]);
    assert.equal(startupFonts.length, 4);
    assert.deepEqual(files.filter(value => value.endsWith('.woff2')).sort(),
      startupFonts.map(name => 'assets/fonts/' + name).sort());
    for (const name of startupFonts) {
      assert.deepEqual(extractFile(archive, 'assets/fonts/' + name),
        await readFile(path.join(root, 'assets/fonts', name)));
    }
    // A newly introduced workbench font must not silently grow the native shell.
    assert.equal(filter(path.join(root, 'assets/fonts/misans-future.woff2'),
      await stat(path.join(root, 'assets/fonts/inter-roman.woff2'))), false);
    const renderer = path.resolve(root, '../src/pulsara_agent/desktop_app/static/assets');
    const rendered = await readdir(renderer);
    for (const name of (await readdir(path.join(root, 'assets/fonts'))).filter(value => value.startsWith('misans-') && value.endsWith('.woff2'))) {
      assert.ok(!files.includes('assets/fonts/' + name));
      const published = rendered.find(value => value.startsWith(name.slice(0, -6) + '-') && value.endsWith('.woff2'));
      assert.ok(published, `Missing workbench face: ${name}`);
      assert.deepEqual(await readFile(path.join(renderer, published)),
        await readFile(path.join(root, 'assets/fonts', name)));
    }
    assert.ok(files.includes('assets/fonts/licenses/MiSans-license.pdf'));
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});
}
