'use client';

import { useState } from 'react';
import { FileInput, LoaderCircle, X } from 'lucide-react';
import type { McpImportPreview, McpImportSelection, McpImportSource } from '../lib/pulsara-types';

export function McpImporter({ onPreview, onImport, onClose }: {
  onPreview: (input: McpImportSource) => Promise<McpImportPreview[]>;
  onImport: (input: McpImportSelection) => Promise<boolean>;
  onClose: () => void;
}) {
  const [source, setSource] = useState<McpImportSource>({ content: '', shape: 'auto' });
  const [items, setItems] = useState<McpImportPreview[]>();
  const [selected, setSelected] = useState('');
  const [classifications, setClassifications] = useState<Record<string, string>>({});
  const [values, setValues] = useState<Record<string, string>>({});
  const [transport, setTransport] = useState<'streamable_http' | 'sse'>('streamable_http');
  const [busy, setBusy] = useState(false);
  const [allowLocal, setAllowLocal] = useState(false);
  const [error, setError] = useState('');
  const [installed, setInstalled] = useState<Set<string>>(new Set());
  const item = items?.find((candidate) => candidate.server_id === selected);
  const choose = (candidate?: McpImportPreview) => {
    setSelected(candidate?.server_id ?? ''); setClassifications({}); setValues({});
    setAllowLocal(false);
    setTransport(candidate?.transport === 'sse' ? 'sse' : 'streamable_http');
  };
  const changeSource = (update: Partial<McpImportSource>) => { setSource((current) => ({...current, ...update})); setItems(undefined); setInstalled(new Set()); setValues({}); };
  const preview = async () => {
    setBusy(true); setError('');
    try { const candidates = await onPreview(source); setItems(candidates); choose(candidates[0]); if (!candidates.length) setError('所选配置没有声明 MCP 服务。'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '无法读取配置。'); }
    finally { setBusy(false); }
  };
  const submit = async () => {
    if (!item) return;
    setBusy(true); setError('');
    try {
      if (await onImport({...source, selected_server_id: item.server_id, classifications, values, ...(item.transport === 'stdio' ? {} : {transport, allow_http_localhost: allowLocal})})) {
        setInstalled((current) => new Set([...current, item.server_id])); setValues({});
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : '导入失败。'); }
    finally { setBusy(false); }
  };
  const variables = new Map<string, {private: boolean; optional: boolean; filePath?: string | null}>();
  for (const field of item?.fields ?? []) for (const variable of field.variables) {
    const isPrivate = field.private || (!(field.target === 'endpoint' || field.target.startsWith('oauth:')) && classifications[field.target] !== 'public');
    const previous = variables.get(variable.name);
    variables.set(variable.name, {private: Boolean(previous?.private || isPrivate), optional: variable.has_default || variable.environment && isPrivate, filePath: variable.file_path});
  }
  const classified = item?.fields.every((field) => field.private || field.target === 'endpoint' || field.target.startsWith('oauth:') || classifications[field.target]);
  return <div className="capability-add-panel" role="dialog" aria-modal="true" aria-label="导入 MCP 配置">
    <div className="capability-add-panel__backdrop" onClick={busy ? undefined : onClose} />
    <section><header><div><span>一次性转换 · 不运行安装脚本</span><h2>导入 MCP 配置</h2></div><button aria-label="关闭" disabled={busy} onClick={onClose}><X size={16} /></button></header>
      <div className="capability-dialog-body">
      <label className="capability-field"><span>读取配置文件</span><input type="file" accept=".json,.jsonc,application/json" disabled={busy} onChange={(event) => { const file = event.target.files?.[0]; if (!file) return; if (file.size > 1024 * 1024) { setError('文件超过 MCP 配置的 1 MiB 上限。'); return; } setBusy(true); void file.text().then((content) => changeSource({content}), () => setError('无法读取所选文件。')).finally(() => setBusy(false)); }} /><small>仅读取你选中的文件；内容只留在当前表单，不进入聊天或日志。</small></label>
      <label className="capability-field"><span>或粘贴 JSON / JSONC</span><textarea aria-label="配置内容" value={source.content} disabled={busy} rows={7} onChange={(event) => changeSource({content: event.target.value})} spellCheck={false} /></label>
      <div className="capability-form-grid"><label className="capability-field"><span>来源格式</span><select value={source.shape} disabled={busy} onChange={(event) => changeSource({shape: event.target.value as McpImportSource['shape']})}><option value="auto">自动识别（有歧义时提示）</option><option value="mcpServers">mcpServers</option><option value="opencode">OpenCode mcp</option><option value="map">裸服务列表</option><option value="server">单个服务</option></select></label><label className="capability-field"><span>单服务 ID（需要时填写）</span><input value={source.server_id ?? ''} disabled={busy} onChange={(event) => changeSource({server_id: event.target.value || undefined})} /></label></div>
      <button className="secondary-action" disabled={busy || !source.content.trim()} onClick={() => void preview()}><FileInput size={14} />读取预览</button>
      {items && items.length > 0 && <label className="capability-field"><span>选择服务（分别导入）</span><select value={selected} disabled={busy} onChange={(event) => choose(items.find((candidate) => candidate.server_id === event.target.value))}>{items.map((candidate) => <option key={candidate.server_id} value={candidate.server_id}>{candidate.server_id}{installed.has(candidate.server_id) ? ' · 已导入' : ''}</option>)}</select></label>}
      {item && <div className="skill-import-candidate">
        {item.transport !== 'stdio' && <label className="capability-choice"><input type="checkbox" checked={allowLocal} disabled={busy} onChange={(event) => setAllowLocal(event.target.checked)} /><span>允许本机 HTTP 地址（仅用于本机服务）</span></label>}
        {item.notices.map((notice, index) => <p key={index}>{notice}</p>)}
        {item.issues.map((issue, index) => <p role="alert" key={index}><code>{issue.path}</code>：{issue.message}</p>)}
        {item.transport && item.transport !== 'stdio' && <label className="capability-field"><span>远程连接方式</span><select value={transport} onChange={(event) => setTransport(event.target.value as typeof transport)} disabled={busy}><option value="streamable_http">Streamable HTTP</option><option value="sse">SSE</option></select></label>}
        {item.fields.map((field) => <div key={field.target} className="capability-field"><span>{field.target}</span>
          {field.private ? <small>私有值：仅保存到本机，不向模型提供。</small> : field.target === 'endpoint' || field.target.startsWith('oauth:') ? <small>普通连接参数，请勿在地址或此字段中放入密钥。</small> : <select aria-label={`${field.target} 用途`} value={classifications[field.target] ?? ''} disabled={busy} onChange={(event) => setClassifications((current) => ({...current, [field.target]: event.target.value}))}><option value="">请选择值的用途</option><option value="private">私有值 / 密钥</option><option value="public">普通值（可公开）</option></select>}
          {field.template_literals && <label className="capability-choice"><input type="checkbox" disabled={busy} checked={classifications[field.target + ':template_literals'] === 'public'} onChange={(event) => setClassifications((current) => ({...current, [field.target + ':template_literals']: event.target.checked ? 'public' : ''}))} /><span>模板的常量部分不包含秘密</span></label>}
        </div>)}
        {[...variables].map(([name, options]) => <label key={name} className="capability-field"><span>{options.filePath ? `来源请求文件：${options.filePath}` : name}{options.optional ? '（可留空，保留来源默认值或环境引用）' : '（待填写）'}</span>
          {options.filePath ? <><input type="file" disabled={busy} onChange={(event) => {
            const file = event.target.files?.[0];
            setValues((current) => {const next = {...current}; delete next[name]; return next;});
            if (!file) return;
            if (file.size > 1024 * 1024) {setError('所选输入文件超过 MCP 配置的 1 MiB 上限。'); return;}
            setBusy(true);
            void file.text().then((content) => setValues((current) => ({...current, [name]: content})), () => setError('无法读取所选输入文件。')).finally(() => setBusy(false));
          }} /><small>仅在你明确选择后读取，不会自动访问来源指定的本机路径。{name in values ? '已读取所选文件。' : ''}</small></>
            : <input autoComplete="off" type={options.private ? 'password' : 'text'} value={values[name] ?? ''} disabled={busy} onChange={(event) => setValues((current) => { const next = {...current}; if (event.target.value) next[name] = event.target.value; else delete next[name]; return next; })} />}
        </label>)}
      </div>}
      {error && <p role="alert">{error}</p>}
      <p className="capability-detail-note">导入后按配置中的启用选择生效；本地命令可能被启动，请确认来源可信。保存不等于连接测试成功。</p>
      </div>
      <footer><button className="secondary-action" disabled={busy} onClick={onClose}>关闭</button><button className="capability-primary" disabled={busy || !item || Boolean(item.issues.length) || !classified || installed.has(selected)} onClick={() => void submit()}>{busy ? <LoaderCircle size={14} /> : <FileInput size={14} />}{installed.has(selected) ? '已导入' : '确认导入此服务'}</button></footer>
    </section>
  </div>;
}
