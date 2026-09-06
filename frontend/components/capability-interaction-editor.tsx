'use client';

import { useRef, useState } from 'react';
import { McpEditor, mcpAuthLabels } from './mcp-editor';
import { PluginConnectionEditor } from './plugin-connection-editor';
import type { McpCredentialOwner, UserMcpServerCapability, UserPluginCapability, PluginMcpConnection } from '../lib/pulsara-types';
import type { RuntimeInteractionResolution } from '../lib/runtime-adapter';

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const actions: Record<string, string> = {
  ADD_LOCAL_MCP: '添加 MCP 连接', UPDATE_LOCAL_MCP: '编辑 MCP 连接', REMOVE_LOCAL_MCP: '删除 MCP 连接',
  INSTALL_PLUGIN: '安装插件（暂不启用）', SET_PLUGIN_ENABLED: '更改插件启用状态', REMOVE_PLUGIN: '删除插件',
  CONFIGURE_PLUGIN_MCP_CONNECTION: '配置插件连接', AUTHORIZE_MCP: '在浏览器中登录', CLEAR_MCP_AUTHORIZATION: '退出 MCP 登录',
};

export function CapabilityInteractionEditor({form, onResolve}: {
  form: Record<string, unknown>;
  onResolve: (resolution: RuntimeInteractionResolution) => Promise<boolean>;
}) {
  const [busy, setBusy] = useState(false);
  // Shared editors close after a successful save. That close is not a second
  // CANCEL; async callbacks can still hold the previous render's busy value.
  const resolving = useRef(false);
  const [reviewed, setReviewed] = useState(false);
  const action = String(form.action);
  const prefill = record(form.prefill);
  const plugin = record(form.plugin);
  const submit = async (submission: Record<string, unknown>) => {
    if (resolving.current) return false;
    resolving.current = true;
    setBusy(true);
    try {
      const accepted = await onResolve({kind: 'capability', decision: 'SUBMIT', submission});
      if (!accepted) { resolving.current = false; setBusy(false); }
      return accepted;
    } catch { resolving.current = false; setBusy(false); return false; }
  };
  const cancel = () => {
    if (resolving.current) return;
    resolving.current = true; setBusy(true);
    void onResolve({kind: 'capability', decision: 'CANCEL'}).then(accepted => {
      if (!accepted) { resolving.current = false; setBusy(false); }
    }).catch(() => { resolving.current = false; setBusy(false); });
  };
  if (action === 'ADD_LOCAL_MCP' || action === 'UPDATE_LOCAL_MCP') {
    const config = record(prefill.config);
    const transport = record(config.transport);
    const server: UserMcpServerCapability = {
      id: String(prefill.server_id), name: String(config.display_name ?? prefill.server_id), config,
      currentIdentity: String(record(form.expected_current).expected_identity ?? ''), enabled: config.enabled !== false,
      status: 'configured', required: false, availableToSubagents: Boolean(config.available_to_subagents),
      toolCount: 0, resourceCount: 0, resourceTemplateCount: 0, promptCount: 0,
      instructions: '', hasFailure: false, tools: [],
      transport: {kind: transport.type === 'stdio' ? 'stdio' : 'http', summary: String(transport.endpoint ?? transport.command ?? '')},
    };
    return <McpEditor server={server} credentialOwner={form.credential_owner as McpCredentialOwner}
      onClose={cancel} onSave={input => submit({config: input.config, secret_changes: input.secretChanges,
        retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false})} />;
  }
  if (action === 'CONFIGURE_PLUGIN_MCP_CONNECTION') {
    const connection = record(form.connection);
    const view: UserPluginCapability = {id: String(plugin.id), name: String(plugin.name), description: '',
      enabled: Boolean(plugin.enabled), packageInstallId: String(plugin.package_install_id), packageRoot: '',
      skillCount: 0, mcpCount: 0, effectiveSkillNames: [], effectiveMcpServerIds: [], details: []};
    return <PluginConnectionEditor plugin={view} connection={{serverId: String(connection.server_id),
      defaults: record(connection.defaults), config: record(connection.config),
      overlay: connection.overlay == null ? null : record(connection.overlay),
      connectionInputs: connection.connection_inputs as PluginMcpConnection['connectionInputs'],
      credentialOwner: connection.credential_owner as McpCredentialOwner}}
      onClose={cancel} onSave={input => submit({overlay: input.overlay, secret_changes: input.secretChanges,
        retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false})} />;
  }
  const enableReview = action === 'SET_PLUGIN_ENABLED' && prefill.enabled === true;
  return <div className="capability-form-review">
    <p>{actions[action] ?? '确认能力配置'} · {form.scope === 'WORKSPACE' ? '当前项目' : '本机所有会话'}</p>
    <p>{String(prefill.plugin_id ?? prefill.server_id ?? prefill.source_path ?? '')}</p>
    {enableReview && <>
      <p>启用后，下列能力将供后续工作使用。请确认你信任插件来源。</p>
      {Array.isArray(plugin.skills) && plugin.skills.map((item, index) => <p key={`skill-${index}`}>Skill · {String(record(item).name)} — {String(record(item).description)}</p>)}
      {Array.isArray(plugin.mcp) && plugin.mcp.map((item, index) => {
        const connection = record(item); const config = record(connection.config);
        const transport = record(config.transport); const auth = record(config.auth);
        return <div key={`mcp-${index}`}><p>MCP · {String(connection.server_id)}</p>
          <code>{String(transport.endpoint ?? transport.command ?? '')}</code>
          {transport.type === 'stdio' && <p>参数：{JSON.stringify(transport.args ?? [])} · 目录：{String(transport.cwd ?? '')}</p>}
          <p>认证：{mcpAuthLabels[String(auth.type)] ?? '未指定'}</p>
          {Array.isArray(connection.credentials) && connection.credentials.map((value, n) => {
            const credential = record(value);
            return <p key={n}>{String(credential.name)}：{credential.present ? '已配置' : '需要配置'}</p>;
          })}
        </div>;
      })}
      {Array.isArray(plugin.hooks) && plugin.hooks.map((item, index) => <p key={`hook-${index}`}>Hook · {String(record(item).event)} · <code>{String(record(item).command)}</code></p>)}
      <label className="capability-choice"><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} /><span>我已审阅并允许启用此插件</span></label>
    </>}
    {action === 'AUTHORIZE_MCP' && <p>确认后将打开服务商登录页。无需把登录信息或密钥发给模型。</p>}
    {(action === 'REMOVE_LOCAL_MCP' || action === 'REMOVE_PLUGIN') && <p>将移除该能力及其连接凭据；已有对话记录不会删除。</p>}
    <div className="interaction-actions">
      <button disabled={busy} onClick={cancel}>取消</button>
      <button className="is-primary" disabled={busy || (enableReview && !reviewed)} onClick={() => void submit(enableReview ? {enable_review_accepted: true} : {})}>确认并继续</button>
    </div>
  </div>;
}
