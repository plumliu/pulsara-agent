import { useEffect, useRef, useState } from 'react';
import { AudioLines, Bot, Check, LoaderCircle, Pencil, Plus, RefreshCw, Trash2, X } from 'lucide-react';
import type { AsrConnection, AsrModelInput, RuntimeAdapter } from '../lib/runtime-adapter';

type Notify = (title: string, detail?: string, tone?: 'neutral' | 'success' | 'warning') => void;

export function AsrSettingsPanel({ adapter, value, onChanged, onNotify }: {
  adapter: RuntimeAdapter; value: AsrConnection | null; onChanged: (value: AsrConnection | null) => void; onNotify: Notify;
}) {
  const [editing, setEditing] = useState(false);
  const [clearing, setClearing] = useState(false);
  async function clearCache() {
    setClearing(true);
    try { const result = await adapter.clearOldAsrCache(); onNotify(result.message, undefined, 'success'); }
    catch (error) { onNotify('清理转写缓存失败', String(error), 'warning'); }
    finally { setClearing(false); }
  }
  const [deletePending, setDeletePending] = useState(false);
  const [deleting, setDeleting] = useState(false);
  async function remove() {
    setDeleting(true);
    try {
      onChanged(await adapter.clearAsrModel());
      setDeletePending(false);
    } catch (error) {
      onNotify('删除转写配置失败', String(error), 'warning');
    } finally {
      setDeleting(false);
    }
  }
  return <section className="settings-group" aria-labelledby="asr-settings-title">
    <header><AudioLines size={16} /><div><h2 id="asr-settings-title">ASR 配置</h2><p>转写和连接测试会上传音频并可能计费。</p></div></header>
    <article className="model-config-card">
      <span className="model-config-card__icon"><Bot size={15} /></span>
      <span><strong title={value?.model_id}>{value?.model_id ?? '尚未配置转写模型'}</strong>
        <small>OpenAI Transcriptions</small>
        {value && <code title={value.endpoint}>{value.endpoint}</code>}
      </span>
      <div className="model-config-card__actions">
        {value && <span className="credential-state credential-state--present"><i />密钥已配置</span>}
        {deletePending && value ? <div className="model-delete-confirmation">
          <button disabled={deleting} onClick={() => setDeletePending(false)}>取消</button>
          <button className="subtle-danger" disabled={deleting} onClick={() => void remove()}><Trash2 size={13} />确认删除</button>
        </div> : <div className="model-config-card__buttons">
          <button className="model-edit-trigger" onClick={() => setEditing(true)}>{value ? <Pencil size={13} /> : <Plus size={13} />}{value ? '修改' : '配置'}</button>
          {value && <button className="model-delete-trigger subtle-danger" onClick={() => setDeletePending(true)}><Trash2 size={13} />删除</button>}
        </div>}
      </div>
    </article>
    <div className="asr-cache-settings"><p className="settings-caption">清理超过 24 小时未使用的转写缓存。</p>
      <button type="button" disabled={clearing} className="settings-header-action" onClick={() => void clearCache()}>{clearing && <LoaderCircle size={13} />}清理旧转写缓存</button></div>
    {editing && <AsrDialog adapter={adapter} value={value} onClose={() => setEditing(false)} onChanged={onChanged} onNotify={onNotify} />}
  </section>;
}

function AsrDialog({ adapter, value, onChanged, onClose, onNotify }: {
  adapter: RuntimeAdapter; value: AsrConnection | null; onChanged: (value: AsrConnection | null) => void; onClose: () => void; onNotify: Notify;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [endpoint, setEndpoint] = useState(value?.endpoint ?? '');
  const [model, setModel] = useState(value?.model_id ?? '');
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState<'save' | 'test' | null>(null);
  const ready = Boolean(endpoint.trim() && model.trim() && (key || (value && endpoint.trim() === value.endpoint)));
  const [error, setError] = useState('');
  useEffect(() => { dialog.current?.showModal(); }, []);
  const input = (): AsrModelInput => ({ connection: { endpoint: endpoint.trim(), model_id: model.trim() },
    key_action: key ? 'replace' : 'keep', ...(key ? { api_key: key } : {}) });
  async function run(action: 'save' | 'test') {
    setBusy(action); setError('');
    try {
      if (action === 'test') {
        const result = await adapter.testAsrModel(input());
        onNotify('转写连接测试通过', `${result.granularity === 'word' ? '词级' : '段级'}时间戳 · ${Math.round(result.elapsed_ms)} ms`, 'success');
      } else {
        const result = await adapter.saveAsrModel(input());
        onChanged(result); onClose();
      }
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(null); }
  }
  return <dialog ref={dialog} className="model-config-dialog retrieval-dialog" aria-labelledby="asr-dialog-title" onClose={onClose} onCancel={event => { if (busy) event.preventDefault(); }}>
    <section className="settings-group model-add-panel">
    <header><Pencil size={16} /><div><h2 id="asr-dialog-title">配置 ASR 模型</h2></div><button type="button" className="settings-header-action" aria-label="关闭转写配置" disabled={Boolean(busy)} onClick={onClose}><X size={15} /></button></header>
    <fieldset disabled={Boolean(busy)} className="retrieval-fields"><div className="settings-form-grid">
      <label className="retrieval-wide"><span>请求地址</span><input type="url" value={endpoint} onChange={event => setEndpoint(event.target.value)} placeholder="https://api.openai.com/v1/audio/transcriptions" /></label>
      <label><span>模型 ID</span><input value={model} onChange={event => setModel(event.target.value)} /></label>
      <label><span>API key</span><input type="password" autoComplete="new-password" value={key} onChange={event => setKey(event.target.value)} placeholder={value ? '同一请求地址留空保留' : '填写 API key'} /></label>
    </div></fieldset>
    {error && <p role="alert" className="retrieval-error">{error}</p>}
    <footer><span className="retrieval-fee">转写和连接测试会上传音频并可能计费。</span><div className="form-actions">
      <button type="button" disabled={Boolean(busy)} onClick={onClose}>取消</button>
      <button type="button" disabled={!ready || Boolean(busy)} onClick={() => void run('test')}>{busy === 'test' ? <LoaderCircle size={13} /> : <RefreshCw size={13} />}测试连接</button>
      <button type="button" className="primary-action" disabled={!ready || Boolean(busy)} onClick={() => void run('save')}>{busy === 'save' ? <LoaderCircle size={13} /> : <Check size={13} />}保存</button>
    </div></footer>
    </section>
  </dialog>;
}
