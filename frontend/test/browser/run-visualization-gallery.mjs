/** Run against `npm run dev:local -- --port 5194`; artifacts go to output/playwright. */
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import checkGallery from './visualization-gallery-check.mjs';
import checkExtra from './visualization-gallery-extra.mjs';

const directory = dirname(fileURLToPath(import.meta.url));
const root = fileURLToPath(new URL('../../../', import.meta.url));
const outputDir = join(root, 'output/playwright');
await mkdir(outputDir, { recursive: true });
const python = join(root, '.venv/bin/python');
// Reuse the Playwright version/browser already installed in the repo's uv env.
const packageDir = execFileSync(python, ['-c', 'import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent / "driver/package")'], {encoding:'utf8'}).trim();
const { chromium } = createRequire(import.meta.url)(packageDir);
const origin = process.argv[2] ?? 'http://127.0.0.1:5194';
const entry = await mkdtemp(join(root, 'frontend/local/.gallery-check-'));
await writeFile(join(entry, 'index.html'), `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pulsara 图集验收</title><div id="root"></div><script type="module" src="/@fs${join(directory, 'visualization-gallery-fixture.tsx')}"></script>`);
const url = origin + '/' + entry.split('/').at(-1) + '/index.html';
const browser = await chromium.launch({headless:process.env.PULSARA_BROWSER_TEST_HEADED !== '1'});
try {
  const page = await browser.newPage();
  await page.goto(url);
  await page.waitForFunction(() => window.galleryFixtures?.length > 0);
  const fixtures = await page.evaluate(() => window.galleryFixtures);
  const inputs = join(outputDir,'gallery-html');
  await mkdir(inputs, {recursive:true});
  for (const [index, html] of fixtures.entries()) await writeFile(join(inputs, `${index}.html`), html);
  execFileSync(python, ['-c', `
import asyncio,sys
from pathlib import Path
from time import monotonic
from pulsara_agent.conversation_kernel.visualization_screenshot import VisualizationScreenshotOwner
async def run():
    output=Path(sys.argv[2]);output.mkdir(exist_ok=True)
    owner=VisualizationScreenshotOwner()
    try:
        for source in sorted(Path(sys.argv[1]).glob('*.html')):
            image=await owner.render(source.read_bytes(),deadline_monotonic=monotonic()+20,thumbnail=True)
            (output/(source.stem+'.png')).write_bytes(image)
    finally: await owner.aclose()
asyncio.run(run())
`, inputs, join(directory, 'generated-thumbnails')], {cwd:root});
  const checks = await checkGallery(page, url, outputDir);
  await page.close();
  const touch = await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,reducedMotion:'reduce'});
  const extra = await checkExtra(await touch.newPage(), url, outputDir);
  const result = {gallery:checks, extra};
  await writeFile(join(outputDir,'gallery-browser-results.json'), JSON.stringify(result,null,2));
  console.log(JSON.stringify(result,null,2));
} finally { await browser.close(); await rm(entry, {recursive:true}); }
