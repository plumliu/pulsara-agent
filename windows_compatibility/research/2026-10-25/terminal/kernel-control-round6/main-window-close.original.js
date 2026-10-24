window.on('close', event => {
    if (quitting)
        return;
    event.preventDefault();
    if (process.platform === 'darwin' || tray)
        window.hide();
    else
        void dialog.showMessageBox(window, { message: '关闭 Pulsara', buttons: ['最小化', '退出'], defaultId: 0, cancelId: 0 }).then(result => result.response ? void requestQuit() : window.minimize());
});
