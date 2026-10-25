const { app, BrowserWindow, ipcMain } = require('electron');
const assert = require('node:assert/strict');
const path = require('node:path');
const { once } = require('node:events');
const { StartupCover } = require('../dist/startup-cover');

let host;
// A failing native regression must exit rather than leave Electron's exception dialog open.
process.on('uncaughtException', error => { console.error(error); app.exit(1); });
async function within(promise, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), 2000);
    })]);
  } finally { clearTimeout(timer); }
}
function coverView() { return host.contentView.children.find(child => child.webContents !== host.webContents); }
async function fadeStarted(contents, stage) {
  // On macOS, showInactive() can leave an occluded renderer hidden. A fade is
  // only required for a visible page; establish that premise before observing it.
  host.show();
  host.focus();
  await within(contents.executeJavaScript(`new Promise(resolve => {
    const visible = () => {
      if (document.hidden) return;
      document.removeEventListener('visibilitychange', visible);
      resolve();
    };
    document.addEventListener('visibilitychange', visible);
    visible();
  })`), `${stage}: fade requires a visible renderer`);
  await contents.executeJavaScript(`(() => {
    const animate = document.body.animate.bind(document.body);
    document.body.animate = (...args) => {
      const animation = animate(...args);
      window.fadeCount = (window.fadeCount || 0) + 1;
      console.log('startup-fade-started');
      return animation;
    };
  })()`);
  return { started: new Promise(resolve => {
    const listener = event => {
      if (event.message === 'startup-fade-started') {
        contents.removeListener('console-message', listener);
        resolve();
      }
    };
    contents.on('console-message', listener);
  }) };
}
app.whenReady().then(async () => {
  app.dock?.hide();
  const preload = path.resolve(__dirname, '../dist/preload.js');
  host = new BrowserWindow({ show: false, width: 1000, height: 700, webPreferences: { sandbox: true, preload } });
  const cover = new StartupCover(host, path.resolve(__dirname, '../assets/startup.html'), preload);
  ipcMain.handle('desktop:startup-settled', event => {
    assert.equal(event.sender, host.webContents);
    assert.equal(event.senderFrame, host.webContents.mainFrame);
    return cover.finish();
  });
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

  // Exercise the production preload -> IPC -> cover -> renderer interaction gate.
  // The assertion runs while the native window is still hidden, before revealing it.
  await host.loadURL('data:text/html,<div id="root" inert>Workbench ready</div>');
  for (const transition of ['hide', 'minimize', 'destroy-contents', 'replace']) {
    host.restore();
    host.showInactive();
    await cover.show();
    const pendingView = coverView();
    const pendingContents = pendingView.webContents;
    const { started } = await fadeStarted(pendingContents, transition);
    const ready = host.webContents.executeJavaScript(`(() => {
      document.getElementById('root').inert = true;
      return Promise.all([window.pulsaraDesktop.startupSettled(), window.pulsaraDesktop.startupSettled()]).then(() => {
        document.getElementById('root').inert = false;
      });
    })()`);
    await within(started, `${transition}: fade never started`);
    assert.equal(await pendingContents.executeJavaScript('window.fadeCount'), 1, 'concurrent startup notifications share one fade');
    let replacement;
    if (transition === 'hide') host.hide();
    if (transition === 'minimize') host.minimize();
    if (transition === 'destroy-contents') pendingContents.close();
    if (transition === 'replace') {
      await cover.show('replacement error');
      replacement = coverView();
    }
    await within(ready, `${transition}: startup IPC remained blocked`);
    assert.equal(await host.webContents.executeJavaScript('document.getElementById("root").inert'), false);
    assert.equal(pendingContents.isDestroyed(), true);
    assert.equal(host.contentView.children.includes(pendingView), false);
    if (replacement) {
      assert.equal(coverView(), replacement, 'old fade must not remove its replacement');
      assert.equal(await replacement.webContents.executeJavaScript('document.getElementById("status").textContent'), '工作台连接未完成');
      assert.equal(await replacement.webContents.executeJavaScript('document.getElementById("error-detail-text").textContent'), 'replacement error');
      host.hide();
      await cover.finish();
    }
    console.log(`PASS: ${transition} settles startup IPC and releases the renderer interaction gate`);
  }

  // A hidden cover legitimately skips the fade but must still settle the same
  // preload -> IPC interaction gate and physically destroy its own contents.
  host.hide();
  await cover.show();
  const hiddenView = coverView();
  const hiddenContents = hiddenView.webContents;
  assert.equal(host.isVisible(), false);
  await within(hiddenContents.executeJavaScript(`new Promise(resolve => {
    const hidden = () => {
      if (!document.hidden) return;
      document.removeEventListener('visibilitychange', hidden);
      resolve();
    };
    document.addEventListener('visibilitychange', hidden);
    hidden();
  })`), 'hidden: renderer never became hidden');
  await hiddenContents.executeJavaScript(`(() => {
    document.body.animate = () => {
      throw new Error('A hidden startup cover must not animate');
    };
  })()`);
  const hiddenReady = host.webContents.executeJavaScript(`(() => {
    document.getElementById('root').inert = true;
    return window.pulsaraDesktop.startupSettled().then(() => {
      document.getElementById('root').inert = false;
    });
  })()`);
  await within(hiddenReady, 'hidden: startup IPC remained blocked');
  assert.equal(await host.webContents.executeJavaScript('document.getElementById("root").inert'), false);
  assert.equal(hiddenContents.isDestroyed(), true);
  assert.equal(host.contentView.children.includes(hiddenView), false);
  console.log('PASS: hidden startup skips animation and releases the renderer interaction gate');

  // Electron can clear view.webContents before the cover owner disposes the view.
  await cover.show();
  const earlyDestroyed = coverView();
  const earlyContents = earlyDestroyed.webContents;
  const earlyClosed = once(earlyContents, 'destroyed');
  earlyContents.close();
  await earlyClosed;
  assert.equal(earlyDestroyed.webContents, undefined);
  await cover.finish();
  // Competing show/retire requests can overlap native contents destruction.
  await Promise.all([cover.show('superseded status'), cover.show('current status')]);
  assert.equal(host.contentView.children.filter(child => child.webContents !== host.webContents).length, 1);
  assert.equal(await coverView().webContents.executeJavaScript('document.getElementById("error-detail-text").textContent'), 'current status');
  const superseded = cover.show('must not survive retirement');
  await cover.retire();
  await superseded;
  assert.equal(host.contentView.children.filter(child => child.webContents !== host.webContents).length, 0);
  await cover.show('退出尚未完成：StaleHostWriter: host writer generation is stale');
  assert.equal(await coverView().webContents.executeJavaScript('document.getElementById("status").textContent'), '退出尚未完成');
  assert.equal(await coverView().webContents.executeJavaScript('document.getElementById("error-detail-text").textContent'), '退出尚未完成：StaleHostWriter: host writer generation is stale');
  assert.equal(await coverView().webContents.executeJavaScript('document.getElementById("error-details").open'), false);
  await cover.show('PostgreSQL instance identity mismatch');
  assert.equal(await coverView().webContents.executeJavaScript('document.getElementById("status").textContent'), '工作台连接未完成');
  assert.equal(await coverView().webContents.executeJavaScript('document.getElementById("error-detail-text").textContent'), 'PostgreSQL instance identity mismatch');
  assert.equal(await coverView().webContents.executeJavaScript('document.querySelector(".hint").textContent'), '重试不会自动清除数据。可展开技术详情或查看日志。');
  await cover.show('fixture startup failure');
  const failure = host.contentView.children.find(child => child.webContents !== host.webContents);
  const failureContents = failure.webContents;
  assert.equal(await failureContents.executeJavaScript('document.getElementById("actions").hidden'), false);
  assert.equal(await failureContents.executeJavaScript('document.getElementById("status").textContent'), '工作台连接未完成');
  assert.deepEqual(await failureContents.executeJavaScript(`({text:document.getElementById('error-detail-text').textContent,open:document.getElementById('error-details').open,hidden:document.getElementById('error-details').hidden})`), {text:'fixture startup failure',open:false,hidden:false});
  const failureDestroyed = once(failureContents, 'destroyed');
  host.showInactive();
  const { started: closingStarted } = await fadeStarted(failureContents, 'closing');
  const closing = cover.finish();
  await within(closingStarted, 'closing: fade never started');
  host.destroy();
  await within(closing, 'closing: fade remained blocked after native window destruction');
  await failureDestroyed;
  assert.equal(failureContents.isDestroyed(), true);
  console.log('PASS: one startup document survives workbench navigation; logo centered; fade, error controls, trust and cleanup verified.');
  app.quit();
}).catch(error => { console.error(error); app.exit(1); });
