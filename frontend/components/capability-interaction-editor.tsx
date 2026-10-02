'use client';

import { useRef, useState } from 'react';
import { McpEditor, mcpAuthLabels } from './mcp-editor';
import { PluginConnectionEditor } from './plugin-connection-editor';
import type { McpCredentialOwner, UserMcpServerCapability, UserPluginCapability, PluginMcpConnection } from '../lib/pulsara-types';
import type { RuntimeInteractionResolution } from '../lib/runtime-adapter';

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const hookTrust: Record<string, string> = {TRUSTED: '已信任', UNTRUSTED: '尚未信任', MODIFIED: '定义或契约已变化，需重新审阅', DISABLED: '已关闭', UNAVAILABLE: '当前不可用'};
const hookEvents: Record<string, string> = {SessionStart: '对话开始时', SessionEnd: '对话结束时', UserPromptSubmit: '发送消息时', PreToolUse: '工具调用前', PermissionRequest: '请求工具权限时', PostToolUse: '工具调用完成后', PreCompact: '压缩上下文前', PostCompact: '压缩上下文后', SubagentStart: '任务开始时', SubagentStop: '任务结束时', Stop: '模型准备结束时'};
const actions: Record<string, string> = {
  INSTALL_LOOSE_SKILL: '安装技能', SET_LOOSE_SKILL_ENABLED: '更改技能开关', REMOVE_LOOSE_SKILL: '删除技能',
  TRUST_HOOK_SOURCE: '信任 Hook 来源', REVOKE_HOOK_TRUST: '撤销 Hook 信任', SET_HOOK_SOURCE_ENABLED: '更改 Hook 来源开关',
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
  const hookReview = action === 'TRUST_HOOK_SOURCE';
  const hook = record(prefill.hook_source);
  const enableReview = action === 'SET_PLUGIN_ENABLED' && prefill.enabled === true;
  return <div className="capability-form-review">
    <p>{actions[action] ?? '确认能力配置'} · {form.scope === 'WORKSPACE' ? '当前项目' : '本机所有会话'}</p>
    <p>{String(prefill.plugin_id ?? prefill.server_id ?? prefill.skill_path ?? prefill.source_path ?? '')}</p>
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
    {hookReview && <>
      <p>请审阅此来源的完整命令。信任后，后续匹配事件可以运行这些命令。</p>
      <p>来源：{hook.plugin_id ? `插件 ${String(hook.plugin_id)}` : '本地 Hook'} · {String(hook.path)}</p>
      <p>范围：{form.scope === 'WORKSPACE' ? `${String(hook.workspace_path ?? '')}（该项目的对话）` : '所有对话'} · {hook.enabled ? '已开启' : '已关闭'} · {hookTrust[String(hook.trust_disposition)] ?? '尚未信任'}</p>
      <p>当前匹配清单是观察结果；后续满足原始规则的工具也可触发。</p>
      {hook.tool_inventory_complete === false && <p>当前远端工具目录尚不完整，匹配清单仅包含已观察到的工具。</p>}
      {Array.isArray(hook.definitions) && hook.definitions.map((value, index) => {
        const definition = record(value);
        const operations = Array.isArray(definition.matched_operations) ? definition.matched_operations.map(String) : [];
        return <div key={index}>
          <p>{hookEvents[String(definition.event)] ?? String(definition.event)}</p>
          {definition.is_tool_event === true && <p>适用操作：{operations.length ? operations.join('、') : '当前未发现匹配工具'}</p>}
          {definition.matches_all === true && <p>此规则也覆盖后续满足原规则的工具；当前清单不是授权白名单。</p>}
          <pre style={{whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'}}>{String(definition.command)}</pre>
          <details><summary>原始匹配规则与执行详情</summary>
            <p>原始 matcher：<code>{String(definition.matcher)}</code></p>
            <p>脚本收到 Pulsara 原生工具名与参数；匹配别名不转换脚本输入。</p>
            {Array.isArray(definition.matching_aliases) && definition.matching_aliases.map((item, n) => {const mapping = record(item); return <p key={n}>{Array.isArray(mapping.aliases) ? mapping.aliases.join('、') : ''} → {String(mapping.tool_name)}</p>;})}
            <p>声明环境：<code>{JSON.stringify(hook.declaration_environment)}</code></p>
            {definition.commandWindows != null && <><p>Windows 命令</p><pre>{String(definition.commandWindows)}</pre></>}
            <p>超时：{String(definition.timeout)} 秒 · 异步：{String(definition.async)} · 上下文阈值：{String(definition.additionalContextLimit)}</p>
            <p>{String(definition.statusMessage ?? '')}</p>
          </details>
        </div>;
      })}
      <label className="capability-choice"><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} /><span>我已审阅并信任这些 Hook 定义</span></label>
    </>}
    {action === 'AUTHORIZE_MCP' && <p>确认后将打开服务商登录页。无需把登录信息或密钥发给模型。</p>}
    {(action === 'REMOVE_LOCAL_MCP' || action === 'REMOVE_PLUGIN') && <p>将移除该能力及其连接凭据；已有对话记录不会删除。</p>}
    <div className="interaction-actions">
      <button disabled={busy} onClick={cancel}>取消</button>
      <button className="is-primary" disabled={busy || ((enableReview || hookReview) && !reviewed)} onClick={() => void submit(enableReview ? {enable_review_accepted: true} : hookReview ? {hook_review_accepted: true} : {})}>确认并继续</button>
    </div>
  </div>;
}
