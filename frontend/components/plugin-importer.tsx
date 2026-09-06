'use client';

import { useState } from 'react';
import { FileInput, LoaderCircle, PackageOpen, X } from 'lucide-react';
import type { PluginImportOptions, PluginImportDiscovery } from '../lib/pulsara-types';

const formatLabels = {native: 'Agent Plugins 1.0（原生）', claude: 'Claude', codex: 'Codex', cursor: 'Cursor'};

export function PluginImporter({onClose, onPreview, onInstall}: {
  onClose: () => void;
  onPreview: (path: string) => Promise<PluginImportDiscovery>;
  onInstall: (path: string, options?: PluginImportOptions) => Promise<boolean>;
}) {
  const [path, setPath] = useState('');
  const [format, setFormat] = useState<PluginImportOptions['source_format']>();
  const [discovery, setDiscovery] = useState<PluginImportDiscovery>();
  const selected = discovery?.candidates.find((candidate) => candidate.source_format === format);
  const preview = selected?.preview;
  const [classifications, setClassifications] = useState<Record<string, string>>({});
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const reset = () => { setDiscovery(undefined); setFormat(undefined); setClassifications({}); setValues({}); setError(''); };
  const fields = preview?.mcp.flatMap((server) => server.fields.map((field) => ({...field, key: `${server.server_id}.${field.target}`}))) ?? [];
  const category = (field: typeof fields[number]) => field.private ? 'private' : field.target === 'endpoint' || field.target.startsWith('oauth:') ? 'public' : classifications[field.key];
  const variables = new Map<string, {private: boolean; hasDefault: boolean; file: boolean}>();
  for (const field of fields) for (const variable of field.variables) {
    const previous = variables.get(variable.name);
    variables.set(variable.name, {
      private: Boolean(previous?.private || category(field) !== 'public'),
      hasDefault: Boolean(variable.has_default && (previous?.hasDefault ?? true)),
      file: Boolean(variable.file_path),
    });
  }
  const ready = Boolean(preview && fields.every((field) => category(field)) &&
    [...variables].every(([name, item]) => !item.file && (item.private || item.hasDefault || values[name]?.trim())));
  const inspect = async () => {
    setBusy(true); reset();
    try {
      const result = await onPreview(path.trim());
      setDiscovery(result);
      if (result.candidates.length === 1) setFormat(result.candidates[0].source_format);
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : '无法预览所选插件。'); }
    finally { setBusy(false); }
  };
  const install = async () => {
    if (busy || !ready || !format) return;
    setBusy(true); setError('');
    // Do not retain public values when a field is reclassified as private.
    const publicValues = Object.fromEntries(Object.entries(values).filter(([name]) => variables.get(name)?.private === false));
    try {
      if (await onInstall(path.trim(), {
        source_format: format, classifications, public_values: publicValues,
      })) onClose();
      else setError('插件尚未安装，请检查提示并保留此表单重试。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '安装失败。'); }
    finally { setBusy(false); }
  };
  return <div className="capability-add-panel" role="dialog" aria-modal="true" aria-label="安装插件">
    <div className="capability-add-panel__backdrop" onClick={busy ? undefined : onClose} />
    <section><header><div><span>添加到这台设备 · 安装后保持关闭</span><h2>安装插件</h2></div><button aria-label="关闭" disabled={busy} onClick={onClose}><X size={16} /></button></header>
      <div className="capability-dialog-body">
      <label className="capability-field"><span>本地目录</span><input autoFocus value={path} disabled={busy} onChange={(event) => {setPath(event.target.value); reset();}} placeholder="/绝对路径/到/目录" /></label>
      <p className="capability-detail-note">自动识别目录中的插件发行版；不执行脚本，不支持 JS / TS 宿主插件。</p>
      <button className="secondary-action" disabled={busy || !path.trim()} onClick={() => void inspect()}><FileInput size={14} />读取预览</button>
      {discovery && discovery.candidates.length > 1 && <fieldset className="plugin-distributions"><legend>选择发行版</legend>
        <p>各版本可能包含不同组件，只安装所选版本，不合并。</p>
        {discovery.candidates.map((candidate) => <div className="plugin-distribution-card" key={candidate.source_format}>
          <label className="plugin-distribution-option">
            <input type="radio" name="plugin-distribution" aria-label={formatLabels[candidate.source_format]} disabled={busy || !candidate.preview} checked={format === candidate.source_format} onChange={() => {setFormat(candidate.source_format); setClassifications({}); setValues({}); setError('');}} />
            <span className="plugin-distribution-copy"><strong>{formatLabels[candidate.source_format]}</strong><small>{candidate.manifest}</small>
              {candidate.preview ? <span>{candidate.preview.skills.length} 个技能 · {candidate.preview.mcp.length} 个 MCP 服务 · {candidate.preview.hooks.length} 类 Hook</span> : <span className="capability-inline-error">{candidate.error}</span>}
            </span>
          </label>
          {candidate.preview && <details className="plugin-distribution-components"><summary>查看组件</summary><p>技能：{candidate.preview.skills.join('、') || '无'}；MCP：{candidate.preview.mcp.map((server) => server.server_id).join('、') || '无'}；Hooks：{candidate.preview.hooks.join('、') || '无'}</p></details>}
        </div>)}
      </fieldset>}
      {selected && discovery?.candidates.length === 1 && <p className="capability-detail-note">已识别：{formatLabels[selected.source_format]} · {selected.manifest}</p>}
      {selected?.error && <p role="alert">{selected.error}</p>}
      {preview && <div className="skill-import-candidate"><h3><PackageOpen size={16} /> {preview.name}</h3>
        <p>{preview.skills.length} 个技能 · {preview.mcp.length} 个 MCP 服务 · {preview.hooks.length} 类 Hook</p>
        {preview.skills.length > 0 && <p>技能：{preview.skills.join('、')}</p>}
        {preview.mcp.length > 0 && <p>MCP：{preview.mcp.map((server) => server.server_id).join('、')}</p>}
        {preview.notices.length > 0 && <details className="capability-disclosure"><summary>导入说明</summary>{preview.notices.map((notice, index) => <p key={index}>{notice}</p>)}</details>}
        {fields.filter(field => field.variables.length > 0 || field.private || !(field.target === 'endpoint' || field.target.startsWith('oauth:'))).map((field) => <label className="capability-field" key={field.key}><span>{field.key}</span>
          {field.private ? <small>密钥不进入安装包；安装后在插件的连接设置中填写。</small> : field.target === 'endpoint' || field.target.startsWith('oauth:') ? <small>普通连接参数，请勿填写密钥。</small> :
            <select aria-label={`${field.key} 用途`} value={classifications[field.key] ?? ''} disabled={busy} onChange={(event) => {setClassifications((current) => ({...current, [field.key]: event.target.value})); setValues({});}}><option value="">请选择用途</option><option value="private">私有值 / 密钥</option><option value="public">普通值（可公开）</option></select>}
        </label>)}
        {[...variables].map(([name, item]) => item.private ? <p key={name}>{name}：安装后在连接设置中填写密钥。</p> : <label key={name} className="capability-field"><span>{name}{item.hasDefault ? '（可使用来源默认值）' : ''}</span><input value={values[name] ?? ''} disabled={busy || item.file} autoComplete="off" onChange={(event) => setValues((current) => {const next = {...current}; if (event.target.value) next[name] = event.target.value; else delete next[name]; return next;})} />{item.file && <small>此参数要求本机文件，请单独导入 MCP 并明确选择文件。</small>}</label>)}
      </div>}
      {error && <p role="alert">{error}</p>}
      <p className="capability-detail-note">安装会保留技能资源，不执行脚本、不连接 MCP。之后请核对插件组件与连接凭据，再明确启用。</p>
      </div>
      <footer><button className="secondary-action" disabled={busy} onClick={onClose}>取消</button><button className="capability-primary" disabled={busy || !path.trim() || !ready} onClick={() => void install()}>{busy ? <LoaderCircle size={14} /> : <PackageOpen size={14} />}安装</button></footer>
    </section>
  </div>;
}
