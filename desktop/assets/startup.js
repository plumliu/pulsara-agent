const parameters = new URLSearchParams(location.search);
const error = parameters.get('error');
const phase = parameters.get('phase') || 'starting';
if (error) document.getElementById('status').textContent = error;
document.getElementById('actions').hidden = phase !== 'failed';
document.querySelector('.hint').textContent = phase === 'stopping' || phase === 'resetting'
  ? '正在安全关闭内核与后台进程。'
  : phase === 'failed' ? '请查看具体原因。重试不会自动清除数据。' : '数据库和内核由应用启动，请稍候。';
document.getElementById('retry').onclick = () => window.pulsaraDesktop.retryStartup();
document.getElementById('logs').onclick = () => window.pulsaraDesktop.showLogs();
document.getElementById('quit').onclick = () => window.pulsaraDesktop.quit();
