"use strict";
async function requestQuit() {
    if (quittingRequest || quitting || resetting)
        return;
    quittingRequest = true;
    try {
        const work = await runtime.inspectQuit();
        if (work.error || work.activeTasks || work.scheduledTasks) {
            focusWindow();
            const choice = await dialog.showMessageBox(window, { type: 'question', message: '退出 Pulsara？', detail: work.error || '退出会停止当前工作及后台调度。关闭窗口可以继续在后台运行。', buttons: ['继续后台运行', '退出 Pulsara'], defaultId: 0, cancelId: 0 });
            if (choice.response !== 1)
                return;
        }
        quitting = true;
        await showStartup('正在停止并结算当前工作，等待内核退出…', 'stopping');
        await runtime.stop();
        if (starting)
            await starting;
        // If startup never owned a ready kernel and reused a database, don't stop another owner's instance.
        if (postgres.startedHere || (databaseReady && !startupError))
            await postgres.stop();
        app.exit(0);
    }
    catch (error) {
        quitting = false;
        await showStartup(`退出尚未完成：${error instanceof Error ? error.message : String(error)}`);
    }
    finally {
        quittingRequest = false;
    }
}
