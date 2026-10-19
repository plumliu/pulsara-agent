import { useEffect, useId, useState } from 'react';
import { ChevronRight, CircleAlert, RefreshCw, RotateCcw, TerminalSquare } from 'lucide-react';
import { AnimatedDisclosure } from './animated-disclosure';
import { desktopHost, type CliStatus } from '../lib/desktop-host';

const labels = { ready: '使用 pulsara 命令打开或管理应用。', missing: '未注册', outdated: '需要更新', conflict: '已有同名命令', error: '暂时不可用' };

export function CliSettings() {
  const host = desktopHost();
  const [status, setStatus] = useState<CliStatus>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState(false);
  const detailsId = useId();
  useEffect(() => {
    if (!host) return;
    let current = true;
    void host.cliStatus().then(value => { if (current) setStatus(value); }).catch(reason => { if (current) setError(String(reason)); });
    return () => { current = false; };
  }, [host]);
  if (!host) return null;
  const run = async (repair = false) => {
    setBusy(true); setError('');
    try { setStatus(await (repair ? host.repairCli(status?.state === 'conflict') : host.cliStatus())); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };
  return <section className="settings-cli" aria-label="命令行入口">
    <div className="setting-row settings-cli-summary">
    <span className="setting-row__icon"><TerminalSquare size={15}/></span>
    <div className="setting-row__copy">
      <div className="settings-cli-heading">
        <strong>命令行入口</strong>
        {status?.state === 'conflict' && <button className="settings-cli-conflict" aria-expanded={expanded} aria-controls={detailsId} onClick={() => setExpanded(value => !value)}><CircleAlert size={12} /><span>已有同名命令</span><ChevronRight size={12} /></button>}
      </div>
      {status?.state !== 'conflict' && <small role="status">{status ? labels[status.state] : error ? '检查失败' : '正在检查…'}</small>}
    </div>
    <div className="setting-row__control settings-service-actions">
      <button disabled={busy} onClick={() => void run()}><RefreshCw size={13} />重新检查</button>
      {status?.managedBy === 'application' && <button className="subtle-danger" disabled={busy} title={status.state === 'conflict' ? '只替换命令入口，保留原有安装目录' : '重置桌面版命令入口'} onClick={() => void run(true)}><RotateCcw size={13} />重置入口</button>}
    </div>
    {status && <input className="settings-readonly-path settings-cli-path" aria-label="命令行路径" disabled value={status.existingTarget ?? status.path} title={status.existingTarget ?? status.path} />}
    {status?.managedBy === 'package' && status.state !== 'ready' && status.state !== 'conflict' && <small className="settings-cli-notice">入口由 deb 管理，请处理同名冲突后重新安装 Pulsara。</small>}
    {status?.state === 'ready' && status.shadowedBy && <small className="settings-cli-notice">应用环境中的同名命令：<code>{status.shadowedBy}</code>。终端环境可能不同，可运行 <code>command -v pulsara</code> 查看实际入口。</small>}
    {(error || status?.message) && <small role="alert" className="settings-cli-notice">{error || status?.message}</small>}
    </div>
    {status?.state === 'conflict' && <div id={detailsId}>
      <AnimatedDisclosure open={expanded}>
        <div className="settings-cli-details" role="region" aria-label="命令行冲突详情">
          <p>此入口已被其他安装占用，自动注册已保留原命令。</p>
          <dl>
            <div><dt>命令入口</dt><dd>{status.path}</dd></div>
            {status.existingTarget && <div><dt>当前指向</dt><dd>{status.existingTarget}</dd></div>}
          </dl>
          <div className="settings-cli-resolution"><span>{status.managedBy === 'package' ? '处理方式' : '替换后'}</span><p>{status.managedBy === 'package' ? '处理同名冲突后，通过 deb 重新安装 Pulsara。' : '此入口改用桌面版，原安装目录保留。'}</p></div>
        </div>
      </AnimatedDisclosure>
    </div>}
  </section>;
}
