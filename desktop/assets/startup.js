const parameters = new URLSearchParams(location.search);
const error = parameters.get('error');
const phase = parameters.get('phase') || 'starting';
if (window.pulsaraDesktop.platform !== 'darwin') {
  document.getElementById('window-controls').hidden = false;
  document.getElementById('window-minimize').onclick = () => window.pulsaraDesktop.minimize();
  document.getElementById('window-maximize').onclick = () => window.pulsaraDesktop.toggleMaximize();
  document.getElementById('window-close').onclick = () => window.pulsaraDesktop.closeWindow();
}
if (error) {
  if (phase === 'failed') {
    document.getElementById('status').textContent = error.startsWith('退出尚未完成：')
      ? '退出尚未完成' : error.startsWith('数据库重置未完成：') ? '数据库重置未完成' : '工作台连接未完成';
    document.getElementById('error-details').hidden = false;
    document.getElementById('error-detail-text').textContent = error;
  } else document.getElementById('status').textContent = error;
}
document.getElementById('actions').hidden = !['failed', 'starting'].includes(phase);
document.getElementById('retry').hidden = phase !== 'failed';
document.getElementById('logs').hidden = phase !== 'failed';
const hint = document.querySelector('.hint');
hint.hidden = phase !== 'failed';
if (phase === 'failed') hint.textContent = error?.startsWith('退出尚未完成：')
  ? '当前工作尚未确认安全关闭，应用与数据仍保留。可查看详情或日志。'
  : '重试不会自动清除数据。可展开技术详情或查看日志。';
document.getElementById('retry').onclick = () => window.pulsaraDesktop.retryStartup();
document.getElementById('logs').onclick = () => window.pulsaraDesktop.showLogs();
document.getElementById('quit').onclick = () => window.pulsaraDesktop.quit();

if (phase === 'setup') {
  const form = document.getElementById('wsl-setup');
  const select = document.getElementById('wsl-distribution');
  const identity = document.getElementById('wsl-identity');
  const confirm = document.getElementById('wsl-confirm');
  const refresh = document.getElementById('wsl-refresh');
  let target;
  let inspection;
  form.hidden = false;
  document.getElementById('status').textContent = error || '连接你已有的 Linux 开发环境';
  document.getElementById('actions').hidden = false;
  document.getElementById('retry').hidden = true;
  document.getElementById('logs').hidden = true;
  const inspect = async () => {
    target = undefined; confirm.disabled = true;
    const current = {}; inspection = current;
    identity.textContent = '正在检查默认用户与 Linux 文件系统…';
    try {
      const observed = await window.pulsaraDesktop.wslInspect(select.value);
      if (inspection !== current) return;
      target = observed;
      identity.textContent = `${observed.user} · ${observed.home}`;
      confirm.disabled = false;
    } catch (reason) { if (inspection === current) identity.textContent = String(reason.message || reason); }
  };
  const discover = async () => {
    refresh.disabled = true; select.disabled = true; confirm.disabled = true; target = undefined;
    try {
      const found = await window.pulsaraDesktop.wslDiscover();
      if (found.savedError) document.getElementById('status').textContent = found.savedError;
      select.replaceChildren();
      for (const distro of found.distros.filter(value => value.version === 2)) {
        const option = document.createElement('option'); option.value = distro.name;
        option.textContent = distro.name + (distro.default ? '（系统默认）' : ''); option.selected = distro.default;
        select.append(option);
      }
      if (!select.options.length) { identity.textContent = '没有发现可用的 WSL2 发行版，请先完成安装。'; return; }
      select.disabled = false;
      await inspect();
    } catch (reason) { identity.textContent = String(reason.message || reason); }
    finally { refresh.disabled = false; }
  };
  select.onchange = () => void inspect();
  refresh.onclick = () => void discover();
  form.onsubmit = async event => {
    event.preventDefault();
    if (!target || confirm.disabled) return;
    confirm.disabled = true; select.disabled = true; refresh.disabled = true;
    try { await window.pulsaraDesktop.wslSelect({distro:target.distro, user:target.user}); }
    catch (reason) { identity.textContent = String(reason.message || reason); confirm.disabled = false; select.disabled = false; refresh.disabled = false; }
  };
  document.getElementById('wsl-help').onclick = event => {
    event.preventDefault();
    void window.pulsaraDesktop.openExternal(event.currentTarget.href);
  };
  void discover();
}
