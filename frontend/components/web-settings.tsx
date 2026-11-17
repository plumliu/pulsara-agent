import { useEffect, useId, useRef, useState } from 'react';
import { Check, Globe, LoaderCircle, Pencil, Plus, X } from 'lucide-react';
import type { RuntimeAdapter, WebProvider, WebSettingsStatus } from '../lib/runtime-adapter';

type Notify = (title: string, detail?: string, tone?: 'neutral' | 'success' | 'warning') => void;
const providers: Record<WebProvider, string> = { deepseek: 'DeepSeek', firecrawl: 'Firecrawl', tavily: 'Tavily', exa: 'Exa' };
const free: WebSettingsStatus = { mode: 'free', connection: null, deepseek_candidate: null };

export function WebSettingsPanel({ adapter, value = free, onChanged, onNotify }: {
  adapter: RuntimeAdapter; value?: WebSettingsStatus; onChanged: (value: WebSettingsStatus) => void; onNotify: Notify;
}) {
  const [editing, setEditing] = useState(false);
  const connection = value.connection;
  return <section className="settings-group web-settings" aria-labelledby="web-settings-title">
    <header><Globe size={16} /><div><h2 id="web-settings-title">网页搜索与读取</h2><p>默认免费使用，也可选用自己的服务账户。</p></div></header>
    <article className="model-config-card">
      <span className="model-config-card__icon"><Globe size={15} /></span>
      <span><strong>{value.mode === 'account' && connection ? providers[connection.provider] : '自动免费'}</strong>
        <small>{value.mode === 'account' ? connection?.credential_source === 'model_connection' ? `使用模型连接：${connection.model_connection_label ?? 'DeepSeek'}` : '优先使用已配置的账户' : '无需配置即可搜索和读取网页'}</small>
      </span>
      <div className="model-config-card__actions">
        {connection && <span className={`credential-state credential-state--${connection.has_api_key ? 'present' : 'missing'}`}><i />{connection.has_api_key ? '密钥已配置' : '账户密钥不可用 · 当前免费使用'}</span>}
        <div className="model-config-card__buttons"><button type="button" className="model-edit-trigger" onClick={() => setEditing(true)}>{connection ? <Pencil size={13} /> : <Plus size={13} />}{connection ? '修改' : '配置账户'}</button></div>
      </div>
    </article>
    {editing && <WebSettingsDialog adapter={adapter} onChanged={onChanged} onNotify={onNotify} onClose={() => setEditing(false)} />}
  </section>;
}

function WebSettingsDialog({ adapter, onChanged, onClose, onNotify }: {
  adapter: RuntimeAdapter; onChanged: (value: WebSettingsStatus) => void; onClose: () => void; onNotify: Notify;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const autoTooltipId = useId();
  const [status, setStatus] = useState<WebSettingsStatus>();
  const [provider, setProvider] = useState<WebProvider>('deepseek');
  const [apiKey, setApiKey] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { dialog.current?.showModal(); }, []);
  useEffect(() => {
    setLoading(true); setError('');
    let active = true;
    void adapter.webSettings().then(next => {
      if (!active) return;
      setStatus(next); setProvider(next.connection?.provider ?? 'deepseek');
    }, failure => { if (active) setError(String(failure)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [adapter, loadAttempt]);
  const canKeep = Boolean(status?.connection?.has_api_key && status.connection.credential_source === 'api_key' && status.connection.provider === provider);
  const ready = apiKey ? Boolean(apiKey.trim()) : canKeep;
  const candidate = status?.deepseek_candidate;
  const autoDescription = candidate
    ? '试试使用模型配置里已保存的 DeepSeek API Key。'
    : '在模型配置里保存 DeepSeek API Key 后，就能在这里一键使用。';
  async function run(action: 'save' | 'auto' | 'free') {
    setBusy(true); setError('');
    try {
      const next = action === 'free' ? await adapter.clearWebSettings()
        : action === 'auto' ? await adapter.useDeepSeekWebConnection()
        : await adapter.saveWebSettings(apiKey ? { provider, key_action: 'replace', api_key: apiKey } : { provider, key_action: 'keep' });
      setApiKey(''); onChanged(next); onClose();
      onNotify(action === 'free' ? '已使用自动免费' : '网页服务账户已保存', undefined, 'success');
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  }
  return <dialog ref={dialog} className="model-config-dialog web-settings-dialog" aria-labelledby="web-dialog-title" onClose={onClose} onCancel={event => { if (busy) event.preventDefault(); }}>
    <section className="settings-group model-add-panel">
      <header><Globe size={16} /><div><h2 id="web-dialog-title">配置网页服务</h2><p>配置网页搜索和抓取服务。</p></div><button type="button" className="settings-header-action" aria-label="关闭网页服务配置" disabled={busy} onClick={onClose}><X size={15} /></button></header>
      <fieldset disabled={loading || !status || busy} className="web-settings-fields">
        <div className="settings-form-grid">
          <label><span>供应商</span><select value={provider} onChange={event => { setProvider(event.target.value as WebProvider); setApiKey(''); setError(''); }}>{Object.entries(providers).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
          <label><span>API key</span><input type="password" autoComplete="new-password" value={apiKey} onChange={event => setApiKey(event.target.value)} placeholder={canKeep ? '留空保留现有密钥' : '填写该供应商的 API key'} /></label>
        </div>
        {status?.connection?.credential_source === 'model_connection' && provider === status.connection.provider && <p className="web-settings-reference">{status.connection.has_api_key ? `当前使用：${status.connection.model_connection_label ?? 'DeepSeek 模型连接'}。填写密钥可改为独立账户。` : '已保存的模型连接不可用，当前使用自动免费。可自动配置其他连接，或填写独立密钥。'}</p>}
      </fieldset>
      {loading && <p className="web-settings-loading" role="status"><LoaderCircle size={13} />正在读取配置…</p>}
      {error && <p role="alert" className="retrieval-error">{error}</p>}
      {!loading && !status && <div className="web-settings-retry"><button type="button" className="settings-header-action" onClick={() => setLoadAttempt(attempt => attempt + 1)}>重试读取配置</button></div>}
      <footer><div className="form-actions">
        {status?.connection && <button type="button" disabled={loading || busy} className="web-settings-reset" onClick={() => void run('free')}>使用自动免费</button>}
        {provider === 'deepseek' && <span className="web-settings-auto">
          <button type="button" aria-label="自动配置 DeepSeek" aria-describedby={autoTooltipId} disabled={loading || !status || busy || !candidate} onClick={() => void run('auto')}>自动配置</button>
          <span id={autoTooltipId} className="web-settings-auto-tooltip" role="tooltip">{autoDescription}</span>
        </span>}
        <button type="button" disabled={busy} onClick={onClose}>取消</button>
        <button type="button" className="primary-action" disabled={loading || !status || busy || !ready} onClick={() => void run('save')}>{busy ? <LoaderCircle size={13} /> : <Check size={13} />}保存</button>
      </div></footer>
    </section>
  </dialog>;
}
