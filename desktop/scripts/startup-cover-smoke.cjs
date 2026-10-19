const { app, BrowserWindow } = require('electron');
const assert = require('node:assert/strict');
const path = require('node:path');
const { once } = require('node:events');
const { StartupCover } = require('../dist/startup-cover');

let host;
app.whenReady().then(async () => {
  app.dock?.hide();
  host = new BrowserWindow({ show: false, width: 1000, height: 700, webPreferences: { sandbox: true } });
  const cover = new StartupCover(host, path.resolve(__dirname, '../assets/startup.html'), path.resolve(__dirname, '../dist/preload.js'));
  await cover.show();
  console.log('Startup document loaded');
  const view = host.contentView.children.find(child => child.webContents !== host.webContents);
  assert.ok(view);
  const contents = view.webContents;
  await contents.executeJavaScript('window.startupIdentity = "original-startup-page"');
  for (const stage of ['loading workbench', 'reading preferences', 'connecting session']) {
    await host.loadURL(`data:text/html,<h1>${stage}</h1>`);
    assert.ok(host.contentView.children.includes(view));
    assert.equal(view.getVisible(), true);
    assert.equal(await contents.executeJavaScript('window.startupIdentity'), 'original-startup-page');
  }
  for (const width of [1000, 760]) {
    host.setSize(width, 700);
    await new Promise(resolve => setTimeout(resolve, 100));
    const geometry = await contents.executeJavaScript(`(() => {
      const logo = document.querySelector('.mark').getBoundingClientRect();
      const title = document.querySelector('h1').getBoundingClientRect();
      return { logo: logo.x + logo.width / 2, title: title.x + title.width / 2, center: innerWidth / 2 };
    })()`);
    assert.equal(geometry.logo, geometry.center);
    assert.equal(geometry.title, geometry.center);
    assert.equal(view.getBounds().width, host.getContentSize()[0]);
  }
  assert.equal(cover.accepts({ sender: contents, senderFrame: contents.mainFrame }), true);
  assert.equal(cover.accepts({ sender: host.webContents, senderFrame: host.webContents.mainFrame }), false);
  host.showInactive();
  const destroyed = once(contents, 'destroyed');
  await cover.finish();
  assert.equal(host.contentView.children.includes(view), false);
  await destroyed;
  assert.equal(contents.isDestroyed(), true);
  await cover.finish(); // Reloads after the initial cover was removed are harmless.
  await cover.show('fixture startup failure');
  const failure = host.contentView.children.find(child => child.webContents !== host.webContents);
  const failureContents = failure.webContents;
  assert.equal(await failureContents.executeJavaScript('document.getElementById("actions").hidden'), false);
  assert.equal(await failureContents.executeJavaScript('document.getElementById("status").textContent'), 'fixture startup failure');
  const failureDestroyed = once(failureContents, 'destroyed');
  host.destroy();
  await failureDestroyed;
  assert.equal(failureContents.isDestroyed(), true);
  console.log('PASS: one startup document survives workbench navigation; logo centered; fade, error controls, trust and cleanup verified.');
  app.quit();
}).catch(error => { console.error(error); host?.destroy(); app.exit(1); });
