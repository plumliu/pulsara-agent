// Uses the real file:// startup document, its CSP, preload and current cover owner.
// No product main/kernel/settings/CLI is loaded or invoked.
const {app, BrowserWindow} = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const repository = process.cwd();
const selectedStartup = process.env.PULSARA_FONT_STARTUP_FILE;
const startupFile = selectedStartup || path.join(repository, 'desktop/assets/startup.html');
const evidenceFile = selectedStartup ? 'startup-asar-fonts.json' : 'startup-fonts.json';
const {StartupCover} = require(path.join(repository, 'desktop/dist/startup-cover'));
app.setPath('userData', path.join(__dirname, selectedStartup ? 'startup-asar-font-profile' : 'startup-font-profile'));
async function main() {
  await app.whenReady();
  app.dock?.hide();
  const preload = path.join(repository, 'desktop/dist/preload.js');
  const window = new BrowserWindow({show:false, width:1000, height:900, webPreferences:{sandbox:true, preload}});
  const cover = new StartupCover(window, startupFile, preload);
  try {
    await cover.show();
    const contents = window.contentView.children.find(child => child.webContents !== window.webContents).webContents;
    const observed = await contents.executeJavaScript(`(async () => {
      document.getElementById('status').textContent = '中文 工作台 Startup Pulsara 0123456789';
      document.getElementById('wsl-setup').hidden = false;
      const option = document.createElement('option');
      option.textContent = 'Ubuntu 中文环境';
      document.getElementById('wsl-distribution').append(option);
      const samples = [
        {family:'Pulsara UI',weight:400,text:'Startup'},
        {family:'Pulsara UI CJK',weight:400,text:'中文'},
        {family:'Pulsara UI',weight:500,text:'Ubuntu'},
        {family:'Pulsara UI CJK',weight:500,text:'环境'},
        {family:'Pulsara Brand',weight:700,text:'Pulsara'},
      ];
      const loaded = [];
      for (const sample of samples) {
        const faces = await document.fonts.load(sample.weight + ' 16px "' + sample.family + '"', sample.text);
        loaded.push({...sample, faces:faces.map(face => ({family:face.family,status:face.status,weight:face.weight,style:face.style}))});
      }
      await document.fonts.ready;
      const styles = Object.fromEntries(['#status','h1','#wsl-distribution'].map(selector => {
        const s = getComputedStyle(document.querySelector(selector));
        return [selector,{family:s.fontFamily,size:s.fontSize,weight:s.fontWeight,lineHeight:s.lineHeight,synthesis:s.fontSynthesis}];
      }));
      return {url:location.href,csp:document.querySelector('meta[http-equiv="Content-Security-Policy"]').content,
        loaded,styles,resources:performance.getEntriesByType('resource').map(item => item.name),
        stylesheets:[...document.styleSheets].map(sheet => ({href:sheet.href,
          faces:[...sheet.cssRules].filter(rule => rule.type === CSSRule.FONT_FACE_RULE).map(rule => ({
            family:rule.style.getPropertyValue('font-family'),source:rule.style.getPropertyValue('src')
          }))}))};
    })()`);
    for (const sample of observed.loaded) {
      assert.ok(sample.faces.length, `missing face: ${sample.family} ${sample.weight}`);
      assert.ok(sample.faces.every(face => face.status === 'loaded'), `failed face: ${sample.family}`);
    }
    assert.ok(observed.csp.includes("font-src 'self'"));
    assert.ok(observed.styles['#status'].family.includes('Pulsara UI CJK'));
    assert.ok(observed.styles.h1.family.includes('Pulsara Brand'));
    assert.ok(observed.styles['#wsl-distribution'].family.includes('Pulsara UI CJK'));
    assert.equal(observed.styles['#status'].size, '16px');
    assert.equal(observed.styles.h1.size, '32px');
    assert.equal(observed.styles['#wsl-distribution'].size, '15px');
    // Chromium's Resource Timing omits local file:// sheets. Inspect the actual
    // loaded CSSOM as well as the loaded FontFaces, rather than infer from it.
    const fontSheet = observed.stylesheets.find(sheet => sheet.href?.endsWith('/fonts.css'));
    assert.ok(fontSheet, 'canonical font stylesheet was not loaded');
    assert.equal(fontSheet.faces.length, 9);
    for (const face of fontSheet.faces) {
      const source = /url\(["']?([^"')]+)["']?\)/.exec(face.source)?.[1];
      assert.ok(source, `missing font source: ${face.family}`);
      assert.equal(new URL(source, fontSheet.href).protocol, 'file:', 'remote font source');
    }
    assert.ok(observed.resources.every(url => url.startsWith('file:')), 'startup made a remote resource request');
    contents.debugger.attach('1.3');
    await contents.debugger.sendCommand('DOM.enable');
    await contents.debugger.sendCommand('CSS.enable');
    const {root} = await contents.debugger.sendCommand('DOM.getDocument');
    observed.platformFonts = {};
    for (const selector of ['#status','h1']) {
      const {nodeId} = await contents.debugger.sendCommand('DOM.querySelector', {nodeId:root.nodeId,selector});
      const {fonts} = await contents.debugger.sendCommand('CSS.getPlatformFontsForNode', {nodeId});
      assert.ok(fonts.some(font => font.glyphCount > 0), `no rendered glyphs: ${selector}`);
      assert.ok(fonts.every(font => font.isCustomFont), `system face rendered: ${selector}`);
      observed.platformFonts[selector] = fonts;
    }
    fs.writeFileSync(path.join(__dirname,evidenceFile), JSON.stringify({startupFile,platform:process.platform,arch:process.arch,versions:process.versions,...observed},null,2)+'\n');
    console.log('PASS real startup file CSP, local font faces, rendered custom glyphs and unchanged font sizes');
  } finally {
    await cover.retire();
    window.destroy();
  }
}
main().then(() => app.quit()).catch(error => {console.error(error);app.exit(1);});
