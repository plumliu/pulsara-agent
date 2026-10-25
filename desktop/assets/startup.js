const parameters = new URLSearchParams(location.search);
const error = parameters.get('error');
const phase = parameters.get('phase') || 'starting';
if (window.pulsaraDesktop.platform === 'linux') {
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
document.getElementById('retry').onclick = () => window.pulsaraDesktop.retryStartup();
document.getElementById('logs').onclick = () => window.pulsaraDesktop.showLogs();
document.getElementById('quit').onclick = () => window.pulsaraDesktop.quit();

if (phase === 'setup') {
  const form = document.getElementById('wsl-setup');
  const select = document.getElementById('wsl-distribution');
  const identity = document.getElementById('wsl-identity');
  const confirm = document.getElementById('wsl-confirm');
  const confirmLabel = confirm.querySelector('span');
  const refresh = document.getElementById('wsl-refresh');
  let target;
  let inspection;
  form.hidden = false;
  document.getElementById('status').textContent = error || '连接你已有的 Linux 开发环境';
  const inspect = async () => {
    target = undefined; confirm.disabled = true;
    const current = {}; inspection = current;
    identity.hidden = true;
    confirmLabel.textContent = '正在检查…';
    try {
      const observed = await window.pulsaraDesktop.wslInspect(select.value);
      if (inspection !== current) return;
      target = observed;
      confirm.disabled = false;
    } catch (reason) {
      if (inspection === current) { identity.textContent = String(reason.message || reason); identity.hidden = false; }
    } finally { if (inspection === current) confirmLabel.textContent = '连接并继续'; }
  };
  const discover = async () => {
    refresh.disabled = true; select.disabled = true; confirm.disabled = true; target = undefined;
    inspection = undefined;
    identity.hidden = true;
    confirmLabel.textContent = '正在检查…';
    refresh.setAttribute('aria-busy', 'true');
    try {
      const found = await window.pulsaraDesktop.wslDiscover();
      if (found.savedError) document.getElementById('status').textContent = found.savedError;
      select.replaceChildren();
      for (const distro of found.distros.filter(value => value.version === 2)) {
        const option = document.createElement('option'); option.value = distro.name;
        option.textContent = distro.name + (distro.default ? '（系统默认）' : ''); option.selected = distro.default;
        select.append(option);
      }
      if (!select.options.length) {
        select.add(new Option('未发现可用环境', ''));
        identity.textContent = '没有发现可用的 WSL2 发行版，请先完成安装。'; identity.hidden = false;
        return;
      }
      select.disabled = false;
      await inspect();
    } catch (reason) { identity.textContent = String(reason.message || reason); identity.hidden = false; }
    finally { refresh.disabled = false; refresh.setAttribute('aria-busy', 'false'); confirmLabel.textContent = '连接并继续'; }
  };
  select.onchange = () => void inspect();
  refresh.onclick = () => void discover();
  form.onsubmit = async event => {
    event.preventDefault();
    if (!target || confirm.disabled) return;
    confirm.disabled = true; select.disabled = true; refresh.disabled = true;
    identity.hidden = true;
    confirmLabel.textContent = '正在连接…';
    try { await window.pulsaraDesktop.wslSelect({distro:target.distro, user:target.user}); }
    catch (reason) { identity.textContent = String(reason.message || reason); identity.hidden = false; confirmLabel.textContent = '连接并继续'; confirm.disabled = false; select.disabled = false; refresh.disabled = false; }
  };
  document.getElementById('wsl-help').onclick = event => {
    event.preventDefault();
    void window.pulsaraDesktop.openExternal(event.currentTarget.href);
  };
  void discover();
}
