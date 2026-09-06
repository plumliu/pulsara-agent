'use client';

import { useEffect, useState } from 'react';
import { ChevronDown, LoaderCircle, Plus, Save, Trash2, X, PlugZap } from 'lucide-react';
import type { McpEditInput, McpCredentialOwner, McpConnectionTestResult, UserMcpServerCapability, PluginMcpConnection } from '../lib/pulsara-types';

export const mcpAuthLabels: Record<string, string> = {none: '无需认证', bearer: 'Bearer Token', static_headers: '秘密 Header', oauth: '浏览器登录'};

type SecretRow = { name: string; value: string; clear: boolean; reference?: unknown; source?: 'managed' | 'environment'; environmentName?: string };
const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const pretty = (value: unknown) => JSON.stringify(value ?? {}, null, 2);
function pairs(text: string): Record<string, string> {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.values(value).some((item) => typeof item !== 'string')) {
    throw new Error('请使用名称与文字值组成的 JSON 对象。');
  }
  return value as Record<string, string>;
}

export function McpEditor({ server, onClose, onSave, onTest, onAuthorization, onRestoreDefaults, credentialOwner, credentialScopeKey, packageDefinition, connectionInputs = [] }: {
  server?: UserMcpServerCapability;
  onClose: () => void;
  onSave: (input: McpEditInput) => Promise<boolean>;
  onTest?: (input: McpEditInput) => Promise<McpConnectionTestResult>;
  onAuthorization?: (action: 'login' | 'status' | 'cancel' | 'logout') => Promise<void>;
  onRestoreDefaults?: () => Promise<boolean>;
  credentialOwner?: McpCredentialOwner;
  credentialScopeKey?: string;
  packageDefinition?: Record<string, unknown>;
  connectionInputs?: PluginMcpConnection['connectionInputs'];
}) {
  const [inputValues, setInputValues] = useState<Record<string, string>>({});
  const [clearInputs, setClearInputs] = useState<Record<string, boolean>>({});
  const original = server?.config ?? {};
  const [confirmRestore, setConfirmRestore] = useState(false);
  const previousTransport = object(original.transport);
  const previousAuth = object(original.auth);
  const [id, setId] = useState(server?.id ?? '');
  const [name, setName] = useState(server?.name ?? '');
  const [kind, setKind] = useState(String(previousTransport.type ?? 'streamable_http'));
  const [endpoint, setEndpoint] = useState(String(previousTransport.endpoint ?? ''));
  const [command, setCommand] = useState(String(previousTransport.command ?? ''));
  const [args, setArgs] = useState(JSON.stringify(previousTransport.args ?? []));
  const [cwd, setCwd] = useState(String(previousTransport.cwd ?? '.'));
  const [env, setEnv] = useState(pretty(previousTransport.env));
  const [headers, setHeaders] = useState(pretty(original.public_headers));
  const [auth, setAuth] = useState(String(previousAuth.type ?? 'none'));
  const [bearer, setBearer] = useState('');
  const [bearerSource, setBearerSource] = useState<string>(object(previousAuth.reference).source === 'environment' ? 'environment' : 'managed');
  const [bearerEnvironment, setBearerEnvironment] = useState(String(object(previousAuth.reference).name ?? ''));
  const [clearBearer, setClearBearer] = useState(false);
  const [headerSecrets, setHeaderSecrets] = useState<SecretRow[]>(Object.entries(object(previousAuth.headers)).map(([name, reference]) => ({ name, reference, value: '', clear: false })));
  const [envSecrets, setEnvSecrets] = useState<SecretRow[]>(Object.entries(object(previousTransport.secret_env)).map(([name, reference]) => ({ name, reference, value: '', clear: false })));
  const [clientId, setClientId] = useState(String(previousAuth.client_id ?? ''));
  const [clientSecret, setClientSecret] = useState('');
  const [clientSecretSource, setClientSecretSource] = useState(previousAuth.client_secret ? object(previousAuth.client_secret).source === 'environment' ? 'environment' : 'managed' : 'none');
  const [clientSecretEnvironment, setClientSecretEnvironment] = useState(String(object(previousAuth.client_secret).name ?? ''));
  const [clearClientSecret, setClearClientSecret] = useState(false);
  const [scope, setScope] = useState(String(previousAuth.scope ?? ''));
  const [redirect, setRedirect] = useState(String(previousAuth.redirect_uri ?? 'http://127.0.0.1:17839/callback'));
  const [resource, setResource] = useState(String(previousAuth.resource ?? ''));
  const [clientMetadata, setClientMetadata] = useState(String(previousAuth.client_metadata_url ?? ''));
  const [enabled, setEnabled] = useState(server?.enabled ?? true);
  const [subagents, setSubagents] = useState(server?.availableToSubagents ?? false);
  const [allowLocal, setAllowLocal] = useState(Boolean(previousTransport.allow_http_localhost));
  const [networkPolicy, setNetworkPolicy] = useState(String(previousTransport.network_policy ?? 'PUBLIC_ONLY'));
  const [stateless, setStateless] = useState(Boolean(previousTransport.proved_stateless));
  const [policies, setPolicies] = useState<Record<string, unknown>>({});
  const [perToolTimeout, setPerToolTimeout] = useState<string>();
  const [perToolEffect, setPerToolEffect] = useState<string>();
  const policy = {...original, ...policies};
  const exposure = object(policy.exposure_policy);
  const effect = object(policy.effect_policy);
  const setPolicy = (key: string, value: unknown) => setPolicies((prior) => ({...prior, [key]: value}));
  const setExposure = (key: string, value: unknown) => setPolicy('exposure_policy', {...exposure, [key]: value});
  const [retainConfirmed, setRetainConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [testResult, setTestResult] = useState('');
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape' && !busy) onClose(); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [busy, onClose]);
  const destinationChanged = Boolean(server && (kind !== previousTransport.type || (kind === 'stdio'
    ? command !== previousTransport.command || args !== JSON.stringify(previousTransport.args ?? []) || cwd !== String(previousTransport.cwd ?? '.')
    : endpoint !== previousTransport.endpoint)));
  const bearerRetained = bearerSource === 'environment'
    ? object(previousAuth.reference).source === 'environment' && object(previousAuth.reference).name === bearerEnvironment
    : Boolean(previousAuth.reference && object(previousAuth.reference).source !== 'environment' && !bearer && !clearBearer);
  const clientSecretRetained = clientSecretSource === 'environment'
    ? object(previousAuth.client_secret).source === 'environment' && object(previousAuth.client_secret).name === clientSecretEnvironment
    : clientSecretSource === 'managed' && Boolean(previousAuth.client_secret && object(previousAuth.client_secret).source !== 'environment' && !clientSecret && !clearClientSecret);
  const retainsCredential = kind === 'stdio' ? envSecrets.some((row) => row.reference && !row.value && !row.clear) : auth === 'bearer' ? bearerRetained : auth === 'static_headers' ? headerSecrets.some((row) => row.reference && !row.value && !row.clear) : auth === 'oauth' && clientSecretRetained;

  const submit = async (testing = false) => {
    if (busy) return;
    setError('');
    setBusy(true);
    try {
      const changes: McpEditInput['secretChanges'] = [];
      const secret = (bindingName: string, value: string, clear: boolean, prior?: unknown) => {
        if (prior && !value && !clear) return prior;
        const binding = { owner: credentialOwner ?? { kind: 'local' as const, scope_key: credentialScopeKey ?? 'user', server_id: id.trim(), plugin_id: null }, name: bindingName };
        if (value || clear) changes.push({ binding, value: clear ? null : value });
        return { source: 'managed', binding };
      };
      const secretPairs = (rows: SecretRow[], prefix: string) => {
        if (new Set(rows.map((row) => row.name.toLowerCase())).size !== rows.length) throw new Error('名称不能重复。');
        return Object.fromEntries(rows.map((row) => [row.name,
          (row.source ?? object(row.reference).source) === 'environment'
            ? {source: 'environment', name: row.environmentName ?? object(row.reference).name}
            : secret(prefix + row.name, row.value, row.clear, row.reference)]));
      };
      for (const input of connectionInputs) {
        if (input.private && (inputValues[input.name] || clearInputs[input.name])) {
          secret('input:' + input.name, inputValues[input.name] ?? '', Boolean(clearInputs[input.name]));
        }
      }
      let authentication: Record<string, unknown> = { type: 'none' };
      if (kind !== 'stdio') {
        if (auth === 'bearer') authentication = { type: auth, reference: bearerSource === 'environment'
          ? {source: 'environment', name: bearerEnvironment}
          : secret('bearer', bearer, clearBearer, previousAuth.type === auth && object(previousAuth.reference).source !== 'environment' ? previousAuth.reference : undefined) };
        if (auth === 'static_headers') authentication = { type: auth, headers: secretPairs(headerSecrets, 'header:') };
        if (auth === 'oauth') authentication = { type: auth, client_id: clientId || null,
          client_secret: clientSecretSource === 'none' ? null : clientSecretSource === 'environment'
            ? {source: 'environment', name: clientSecretEnvironment}
            : secret('oauth-client-secret', clientSecret, clearClientSecret, object(previousAuth.client_secret).source !== 'environment' ? previousAuth.client_secret : undefined),
          scope: scope || null, redirect_uri: redirect, resource: resource || null, client_metadata_url: clientMetadata || null };
      }
      const argv: unknown = JSON.parse(args);
      if (!Array.isArray(argv) || argv.some((value) => typeof value !== 'string')) throw new Error('参数需要是字符串数组，例如 ["-y", "包名"]。');
      const transport = kind === 'stdio'
        ? { type: kind, command, args: argv, cwd, env: pairs(env), secret_env: secretPairs(envSecrets, 'env:') }
        : { ...(previousTransport.type === kind ? previousTransport : {}), type: kind, endpoint, allow_http_localhost: allowLocal, network_policy: networkPolicy, proved_stateless: kind === 'streamable_http' && stateless };
      const extra = {...policies};
      if (perToolTimeout !== undefined) {
        const values = JSON.parse(perToolTimeout);
        if (!values || typeof values !== 'object' || Array.isArray(values) || Object.values(values).some((value) => typeof value !== 'number' || !Number.isInteger(value))) throw new Error('工具超时需要是工具名与整数毫秒组成的 JSON 对象。');
        extra.per_tool_timeout_ms = values;
      }
      if (perToolEffect !== undefined) extra.effect_policy = {...effect, tool_effect_overrides: pairs(perToolEffect)};
      const input: McpEditInput = { serverId: id.trim(), retainCredentialsConfirmed: retainConfirmed, config: { ...original, ...extra, display_name: name.trim() || id.trim(), enabled, transport, auth: authentication, public_headers: kind === 'stdio' ? {} : pairs(headers), scope_policy: subagents ? 'ROOT_AND_SUBAGENTS' : 'ROOT_ONLY' }, secretChanges: changes };
      if (testing && onTest) {
        setTestResult('');
        const result = await onTest(input);
        setTestResult(result.status === 'ready'
          ? `连接正常：${result.tools} 个工具、${result.resources} 项资源、${result.resource_templates} 个资源模板、${result.prompts} 个提示词。未保存或启用。`
          : ({ credential_required: '请填写连接凭据。', authorization_required: '请先保存配置并登录授权。', timeout: '连接或目录读取超时；仍可保存后再试。', schema_bound_exceeded: '已连接服务，但工具定义超出资源限制，无法采用完整工具目录；不是登录授权失败。仍可保存配置。', failed: '连接或目录读取失败，请检查地址、认证和协议；仍可保存。' })[result.status]);
      } else if (await onSave(input)) onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '配置无法保存，请检查填写内容。');
    } finally { setBusy(false); }
  };

  const secretRows = (rows: SecretRow[], setRows: (rows: SecretRow[]) => void, label: string) => (
    <div className="capability-field capability-field--wide"><span>{label}</span>
      {rows.map((row, index) => <div className="mcp-secret-row" key={index}>
        <input aria-label={`${label}名称 ${index + 1}`} value={row.name} onChange={(event) => setRows(rows.map((item, i) => i === index ? { ...item, name: event.target.value, reference: undefined } : item))} placeholder="名称" />
        <select aria-label={`${label}来源 ${index + 1}`} value={row.source ?? (object(row.reference).source === 'environment' ? 'environment' : 'managed')} onChange={(event) => setRows(rows.map((item, i) => i === index ? {...item, source: event.target.value as SecretRow['source'], reference: undefined, value: '', clear: false} : item))}><option value="managed">本机保存</option><option value="environment">Host 环境变量</option></select>
        {(row.source ?? object(row.reference).source) === 'environment'
          ? <input aria-label={`${label}环境变量名 ${index + 1}`} value={row.environmentName ?? String(object(row.reference).name ?? '')} onChange={(event) => setRows(rows.map((item, i) => i === index ? {...item, environmentName: event.target.value} : item))} placeholder="Host 启动时的环境变量名" />
          : <input type="password" autoComplete="new-password" aria-label={`${label}值 ${index + 1}`} value={row.value} onChange={(event) => setRows(rows.map((item, i) => i === index ? { ...item, value: event.target.value, clear: false } : item))} placeholder={row.reference ? '留空保留已保存的值' : '秘密值（仅保存到本机）'} />}
        <button type="button" className="danger-ghost" aria-label={`移除${label} ${index + 1}`} onClick={() => setRows(rows.filter((_, i) => i !== index))}><Trash2 size={14} /></button>
      </div>)}
      <button className="secondary-ghost" type="button" onClick={() => setRows([...rows, { name: '', value: '', clear: false }])}><Plus size={13} />添加一项</button>
    </div>
  );
  return <div className="capability-add-panel mcp-editor" role="dialog" aria-modal="true" aria-label={server ? '编辑 MCP 服务' : '添加 MCP 服务'}>
    <div className="capability-add-panel__backdrop" onClick={busy ? undefined : onClose} />
    <section><header><div><span>本机连接 · 凭据不会交给模型</span><h2>{server ? '编辑 MCP 服务' : '添加 MCP 服务'}</h2></div><button disabled={busy} onClick={onClose} aria-label="关闭"><X size={16} /></button></header>
      <div className="capability-form-grid capability-dialog-body">
        <label className="capability-field"><span>服务 ID</span><input autoFocus disabled={Boolean(server)} value={id} onChange={(event) => setId(event.target.value)} /></label>
        <label className="capability-field"><span>显示名称</span><input disabled={Boolean(packageDefinition)} value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label className="capability-field capability-field--wide"><span>连接方式</span><select disabled={Boolean(packageDefinition)} value={kind} onChange={(event) => setKind(event.target.value)}><option value="streamable_http">Streamable HTTP</option><option value="sse">SSE</option><option value="stdio">本地命令</option></select></label>
        {packageDefinition && <details className="capability-field capability-field--wide" open={confirmRestore || undefined}><summary>插件默认连接（只读）</summary><pre>{pretty(packageDefinition)}</pre><small>保存仅覆盖本机实例；同名 Header / 环境变量会覆盖这里的默认值，不修改插件文件。</small>
          {onRestoreDefaults && <div className="capability-detail-actions">{confirmRestore ? <>
            <p>恢复上方默认目的地和认证设置，并清除不再使用的本机凭据与授权。不会修改插件文件或吊销远端密钥。</p>
            <button className="secondary-ghost" disabled={busy} onClick={() => setConfirmRestore(false)}>取消恢复</button>
            <button className="danger-ghost" disabled={busy} onClick={() => void (async () => {
              setBusy(true); setError('');
              try {if (await onRestoreDefaults()) onClose();}
              catch (cause) {setError(cause instanceof Error ? cause.message : '未恢复默认连接。');}
              finally {setBusy(false);}
            })()}>确认恢复默认连接</button>
          </> : <button className="secondary-ghost" disabled={busy} onClick={() => setConfirmRestore(true)}>恢复插件默认连接</button>}</div>}
        </details>}
        {connectionInputs.map((input) => input.private ? <label key={input.name} className="capability-field capability-field--wide"><span>{input.title}{input.required ? '（连接所需）' : '（可选）'}</span>
          <input type="password" autoComplete="new-password" aria-label={input.title} value={inputValues[input.name] ?? ''} placeholder="留空保留本机值；尚未配置时可先保存" onChange={(event) => {setInputValues({...inputValues, [input.name]: event.target.value}); setClearInputs({...clearInputs, [input.name]: false});}} />
          <button type="button" className="danger-ghost" onClick={() => {setInputValues({...inputValues, [input.name]: ''}); setClearInputs({...clearInputs, [input.name]: true});}}>{clearInputs[input.name] ? '保存时清除该值' : '清除本机值'}</button>
        </label> : <p key={input.name} className="capability-field capability-field--wide">{input.title}：{input.default} <small>此普通参数已应用到下方连接字段，可直接修改对应字段。</small></p>)}
        {kind === 'stdio' ? <>
          <label className="capability-field capability-field--wide"><span>命令</span><input aria-label="命令" disabled={Boolean(packageDefinition)} value={command} onChange={(event) => setCommand(event.target.value)} placeholder="npx" /><small>启用后会在本机启动这个命令，请只使用可信来源。</small></label>
          <label className="capability-field capability-field--wide"><span>参数（JSON 数组）</span><textarea disabled={Boolean(packageDefinition)} value={args} onChange={(event) => setArgs(event.target.value)} /></label>
          <label className="capability-field"><span>工作目录（相对项目）</span><input disabled={Boolean(packageDefinition)} value={cwd} onChange={(event) => setCwd(event.target.value)} /></label>
          {secretRows(envSecrets, setEnvSecrets, '秘密环境变量')}
        </> : <>
          <label className="capability-field capability-field--wide"><span>服务地址</span><input value={endpoint} onChange={(event) => setEndpoint(event.target.value)} placeholder="https://example.com/mcp" /></label>
          <label className="capability-field capability-field--wide"><span>认证方式</span><select value={auth} onChange={(event) => setAuth(event.target.value)}><option value="none">无需认证</option><option value="bearer">Bearer Token</option><option value="static_headers">秘密 Header</option><option value="oauth">浏览器登录（OAuth）</option></select></label>
          {auth === 'bearer' && <><label className="capability-field"><span>Token 来源</span><select value={bearerSource} onChange={(event) => { setBearerSource(event.target.value); setBearer(''); setClearBearer(false); }}><option value="managed">本机保存</option><option value="environment">Host 环境变量</option></select></label>
            {bearerSource === 'environment' ? <label className="capability-field"><span>Token 环境变量名</span><input aria-label="Token 环境变量名" value={bearerEnvironment} onChange={(event) => setBearerEnvironment(event.target.value)} /><small>只引用 Host 启动时继承的变量；不能引用 PULSARA_API_KEY。</small></label>
              : <label className="capability-field capability-field--wide"><span>Token</span><input type="password" autoComplete="new-password" value={bearer} onChange={(event) => { setBearer(event.target.value); setClearBearer(false); }} placeholder={previousAuth.reference ? '留空保留已保存的值' : '仅保存到本机'} />{Boolean(previousAuth.reference) && object(previousAuth.reference).source !== 'environment' && <button className="danger-ghost" onClick={() => { setBearer(''); setClearBearer(true); }}>{clearBearer ? '保存时清除凭据' : '清除已保存凭据'}</button>}</label>}
          </>}
          {auth === 'static_headers' && secretRows(headerSecrets, setHeaderSecrets, '秘密 Header')}
          {auth === 'oauth' && <><p className="capability-field--wide">先保存连接，再从服务详情中点击“登录授权”。保存不会弹出浏览器，也不会自动启用服务。</p><label className="capability-field"><span>客户端 ID（可选）</span><input value={clientId} onChange={(event) => setClientId(event.target.value)} /></label><label className="capability-field"><span>客户端密钥来源</span><select value={clientSecretSource} onChange={(event) => {setClientSecretSource(event.target.value); setClientSecret(''); setClearClientSecret(false);}}><option value="none">不使用客户端密钥</option><option value="managed">本机保存</option><option value="environment">Host 环境变量</option></select></label>
            {clientSecretSource === 'environment' && <label className="capability-field"><span>客户端密钥环境变量名</span><input value={clientSecretEnvironment} onChange={(event) => setClientSecretEnvironment(event.target.value)} /></label>}
            {clientSecretSource === 'managed' && <label className="capability-field"><span>客户端密钥</span><input type="password" autoComplete="new-password" value={clientSecret} onChange={(event) => { setClientSecret(event.target.value); setClearClientSecret(false); }} placeholder={previousAuth.client_secret ? '留空保留' : ''} />{Boolean(previousAuth.client_secret) && <button className="danger-ghost" onClick={() => { setClientSecret(''); setClearClientSecret(true); }}>保存时清除密钥值</button>}</label>}
            <label className="capability-field"><span>请求权限（scope，可选）</span><input value={scope} onChange={(event) => setScope(event.target.value)} /></label><label className="capability-field"><span>本机回调地址</span><input value={redirect} onChange={(event) => setRedirect(event.target.value)} /></label><label className="capability-field"><span>资源地址（可选）</span><input value={resource} onChange={(event) => setResource(event.target.value)} /></label><label className="capability-field"><span>客户端元数据 URL（可选）</span><input value={clientMetadata} onChange={(event) => setClientMetadata(event.target.value)} /></label></>}
        </>}
        <details className="capability-field capability-field--wide mcp-advanced"><summary><span>更多连接选项</span><ChevronDown size={15} aria-hidden="true" /></summary>
          <div className="mcp-advanced-fields">
          <label className="capability-field"><span>{kind === 'stdio' ? '普通环境变量' : '普通 Header'}（JSON 对象，不填写密钥）</span><textarea value={kind === 'stdio' ? env : headers} onChange={(event) => kind === 'stdio' ? setEnv(event.target.value) : setHeaders(event.target.value)} /></label>
          {kind !== 'stdio' && <label className="capability-check"><input type="checkbox" checked={allowLocal} onChange={(event) => setAllowLocal(event.target.checked)} />允许本机 HTTP 测试地址</label>}
          {!packageDefinition && <>
            {kind !== 'stdio' && <label className="capability-field"><span>连接网络范围</span><select value={networkPolicy} onChange={(event) => setNetworkPolicy(event.target.value)}><option value="PUBLIC_ONLY">仅公开网络（与明确允许的本机地址）</option><option value="ALLOW_PRIVATE">也允许内网地址</option></select></label>}
            <div className="mcp-option-group">
            {kind === 'streamable_http' && <label className="capability-check"><input type="checkbox" checked={stateless} onChange={(event) => setStateless(event.target.checked)} />服务明确支持无状态请求</label>}
            <label className="capability-check"><input type="checkbox" checked={Boolean(policy.required)} onChange={(event) => setPolicy('required', event.target.checked)} />将此服务标记为必需</label>
            <label className="capability-check"><input type="checkbox" checked={Boolean(policy.supports_parallel_tool_calls)} onChange={(event) => setPolicy('supports_parallel_tool_calls', event.target.checked)} />服务明确支持并行工具调用</label>
            </div>
            {kind === 'streamable_http' && stateless && <label className="capability-field"><span>无状态请求并发数</span><input type="number" min={1} max={16} value={String(policy.stateless_http_max_in_flight ?? 1)} onChange={(event) => setPolicy('stateless_http_max_in_flight', Number(event.target.value))} /></label>}
            <label className="capability-field"><span>单次工具超时（毫秒）</span><input type="number" min={1000} max={600000} value={String(policy.default_tool_timeout_ms ?? 600000)} onChange={(event) => setPolicy('default_tool_timeout_ms', Number(event.target.value))} /></label>
            <label className="capability-check"><input type="checkbox" checked={policy.catalog_refresh_interval_ms !== 'DISABLED'} onChange={(event) => setPolicy('catalog_refresh_interval_ms', event.target.checked ? 300000 : 'DISABLED')} />定期刷新服务目录</label>
            {policy.catalog_refresh_interval_ms !== 'DISABLED' && <label className="capability-field"><span>目录刷新间隔（毫秒）</span><input type="number" min={30000} max={86400000} value={String(policy.catalog_refresh_interval_ms ?? 300000)} onChange={(event) => setPolicy('catalog_refresh_interval_ms', Number(event.target.value))} /></label>}
            <label className="capability-check"><input type="checkbox" checked={exposure.include_tool_names != null} onChange={(event) => setExposure('include_tool_names', event.target.checked ? [] : null)} />只提供指定工具</label>
            {exposure.include_tool_names != null && <label className="capability-field"><span>提供的工具名（每行一个）</span><textarea value={(exposure.include_tool_names as string[]).join('\n')} onChange={(event) => setExposure('include_tool_names', event.target.value.split('\n').filter(Boolean))} /></label>}
            <label className="capability-field"><span>隐藏的工具名（每行一个）</span><textarea value={((exposure.exclude_tool_names ?? []) as string[]).join('\n')} onChange={(event) => setExposure('exclude_tool_names', event.target.value.split('\n').filter(Boolean))} /></label>
            <label className="capability-field"><span>遇到无效工具定义</span><select value={String(exposure.invalid_tool_policy ?? 'FAIL_SERVER')} onChange={(event) => setExposure('invalid_tool_policy', event.target.value)}><option value="FAIL_SERVER">报告服务目录不可用</option><option value="OMIT_INVALID">略过无效工具，保留其他工具</option></select></label>
            <label className="capability-field"><span>默认工具作用</span><select value={String(effect.default_effect ?? 'AUTO')} onChange={(event) => setPolicy('effect_policy', {...effect, default_effect: event.target.value})}><option value="AUTO">使用服务声明</option><option value="READ_ONLY">只读</option><option value="EXTERNAL_EFFECT">会改变外部状态</option></select></label>
            <label className="capability-field"><span>单独设置工具超时（JSON：工具名 → 毫秒）</span><textarea value={perToolTimeout ?? pretty(policy.per_tool_timeout_ms)} onChange={(event) => setPerToolTimeout(event.target.value)} /></label>
            <label className="capability-field"><span>单独设置工具作用（JSON）</span><textarea value={perToolEffect ?? pretty(effect.tool_effect_overrides)} onChange={(event) => setPerToolEffect(event.target.value)} /><small>工具名对应 READ_ONLY（只读）或 EXTERNAL_EFFECT（改变外部状态）。</small></label>
          </>}
          </div>
        </details>
        {!packageDefinition && <><label className="capability-check capability-field--wide"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />启用此服务</label>
        <label className="capability-check capability-field--wide"><input type="checkbox" checked={subagents} onChange={(event) => setSubagents(event.target.checked)} />也向子任务提供</label></>}
        {destinationChanged && retainsCredential && <label className="capability-check capability-field--wide"><input type="checkbox" checked={retainConfirmed} onChange={(event) => setRetainConfirmed(event.target.checked)} />我确认将保留的凭据用于新的连接目标。</label>}
        {error && <p role="alert" className="capability-field--wide">{error}</p>}
        {testResult && <p role="status" className="capability-field--wide">{testResult}</p>}
        {previousAuth.type === 'oauth' && onAuthorization && <div className="capability-field--wide"><small>以下操作使用已保存的连接，不使用当前未保存的修改。</small><div className="capability-detail-actions">
          {(['login', 'status', 'cancel', 'logout'] as const).map((action) => <button className="secondary-action" disabled={busy} key={action} onClick={() => void (async () => {
            setBusy(true); try { await onAuthorization(action); } finally { setBusy(false); }
          })()}>{({login: '登录授权', status: '查看授权状态', cancel: '取消登录', logout: '清除本机授权'})[action]}</button>)}
        </div></div>}
      </div>
      <footer><button className="secondary-action" disabled={busy} onClick={onClose}>取消</button>{onTest && <button className="secondary-action" disabled={busy || !id.trim() || (destinationChanged && retainsCredential && !retainConfirmed)} onClick={() => void submit(true)}><PlugZap size={14} />测试连接</button>}<button className="capability-primary" disabled={busy || !id.trim() || !(kind === 'stdio' ? command : endpoint) || (destinationChanged && retainsCredential && !retainConfirmed)} onClick={() => void submit()}>{busy ? <LoaderCircle size={14} /> : <Save size={14} />}保存连接</button></footer>
    </section>
  </div>;
}
