import { Box, Check, FileText, Globe2, PlugZap, ShieldCheck, Trash2, Workflow, ArrowRight } from 'lucide-react';
import { InteractionCardHeader, type InteractionTone } from './interaction-card-header';
import { useRef, useState } from 'react';
import { McpEditor, mcpAuthLabels } from './mcp-editor';
import { HookSourceReview } from './hook-source-review';
import { PluginConnectionEditor } from './plugin-connection-editor';
import type { McpCredentialOwner, UserMcpServerCapability, PluginMcpConnection } from '../lib/pulsara-types';
import type { RuntimeInteractionResolution } from '../lib/runtime-adapter';

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
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
  const target = record(prefill.target);
  const plugin = record(form.plugin);
  const scope = form.scope === 'WORKSPACE' ? '当前项目' : '所有会话';
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
      id: String(target.server_id), name: String(config.display_name ?? target.server_id), config,
      currentIdentity: String(record(form.expected_current).expected_identity ?? ''), enabled: config.enabled !== false,
      status: 'configured', required: false, availableToSubagents: Boolean(config.available_to_subagents),
      toolCount: 0, resourceCount: 0, resourceTemplateCount: 0, promptCount: 0,
      instructions: '', hasFailure: false, tools: [],
      transport: {kind: transport.type === 'stdio' ? 'stdio' : 'http', summary: String(transport.endpoint ?? transport.command ?? '')},
    };
    return <McpEditor presentation="inline" title={action === 'ADD_LOCAL_MCP' ? `连接 ${server.name}` : `编辑 ${server.name} 连接`} scopeLabel={scope} server={server} credentialOwner={form.credential_owner as McpCredentialOwner}
      onClose={cancel} onSave={input => submit({config: input.config, secret_changes: input.secretChanges,
        retain_credentials_confirmed: input.retainCredentialsConfirmed ?? false})} />;
  }
  if (action === 'CONFIGURE_PLUGIN_MCP_CONNECTION') {
    const connection = record(form.connection);
    const view = {name: String(plugin.name), enabled: Boolean(plugin.enabled),
      packageInstallId: String(plugin.package_install_id)};
    return <PluginConnectionEditor presentation="inline" scopeLabel={scope} plugin={view} connection={{serverId: String(connection.server_id),
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
  const config = record(prefill.config);
  const transport = record(config.transport);
  const identity = String(target.plugin_id ?? target.server_id ?? target.skill_path ?? prefill.source_path ?? record(prefill.hook_source).path ?? '');
  const name = String(plugin.name ?? config.display_name ?? prefill.name ?? identity);
  const removing = action === 'REMOVE_LOCAL_MCP' || action === 'REMOVE_PLUGIN' || action === 'REMOVE_LOOSE_SKILL';
  const isHook = action.includes('HOOK');
  const isSkill = action.includes('SKILL');
  const isPlugin = action.includes('PLUGIN');
  const tone: InteractionTone = removing ? 'red' : isHook ? 'amber' : isSkill ? 'green' : isPlugin ? 'violet' : 'blue';
  const Icon = removing ? Trash2 : isHook ? ShieldCheck : isSkill ? FileText : isPlugin ? Box : action === 'AUTHORIZE_MCP' ? Globe2 : PlugZap;
  const title = action === 'AUTHORIZE_MCP' ? `登录 ${name}` : removing ? `移除 ${name}？`
    : action === 'SET_PLUGIN_ENABLED' ? `${prefill.enabled ? '启用' : '关闭'} ${name}`
    : action === 'SET_LOOSE_SKILL_ENABLED' ? `${prefill.enabled ? '启用' : '关闭'}技能`
    : action === 'SET_HOOK_SOURCE_ENABLED' ? `${prefill.enabled ? '启用' : '关闭'} Hooks`
    : actions[action] ?? '确认能力配置';
  const primary = action === 'AUTHORIZE_MCP' ? '前往登录' : removing ? '移除并继续'
    : hookReview ? '信任并继续' : enableReview ? '启用插件' : action === 'INSTALL_LOOSE_SKILL' || action === 'INSTALL_PLUGIN' ? '安装并继续' : '确认并继续';
  return <div className="capability-interaction" data-tone={tone} aria-busy={busy}>
    <InteractionCardHeader icon={Icon} title={title} tone={tone} scope={scope} />
    <div className="capability-form-review">
      {!hookReview && <div className="capability-review-target">
        <span className="capability-review-icon"><Icon size={20} /></span>
        <div><strong>{name || actions[action]}</strong>{identity && identity !== name && <code>{identity}</code>}{prefill.description != null && <p>{String(prefill.description)}</p>}
          {transport.endpoint != null && <code>{String(transport.endpoint)}</code>}
        </div>
      </div>}
      {enableReview && <>
        <div className="capability-review-components">
          {Array.isArray(plugin.skills) && plugin.skills.map((item, index) => {const skill = record(item); return <div className="capability-review-component" key={`skill-${index}`} data-tone="green">
            <span className="capability-review-icon"><FileText size={17} /></span><div><strong>{String(skill.name)}</strong><p>{String(skill.description ?? '')}</p></div><span className="capability-review-kind">技能</span>
          </div>;})}
          {Array.isArray(plugin.mcp) && plugin.mcp.map((item, index) => {
            const connection = record(item); const config = record(connection.config);
            const transport = record(config.transport); const auth = record(config.auth);
            return <div className="capability-review-component" key={`mcp-${index}`} data-tone="blue">
              <span className="capability-review-icon"><PlugZap size={17} /></span><div><strong>{String(connection.server_id)}</strong>
                <code>{String(transport.endpoint ?? transport.command ?? '')}</code>
                {transport.type === 'stdio' && <><p>参数：{JSON.stringify(transport.args ?? [])}</p><p>目录：{String(transport.cwd ?? '')}</p></>}
                <p>认证：{mcpAuthLabels[String(auth.type)] ?? '未指定'}</p>
                {Array.isArray(connection.credentials) && connection.credentials.map((value, n) => {const credential = record(value); return <p className={credential.present ? 'capability-credential-ready' : undefined} key={n}>{Boolean(credential.present) && <Check size={12} />}{String(credential.name)}：{credential.present ? '已配置' : '需要配置'}</p>;})}
              </div><span className="capability-review-kind">MCP</span>
            </div>;
          })}
          {Array.isArray(plugin.hooks) && plugin.hooks.map((item, index) => {const hook = record(item);return <div className="capability-review-component" key={`hook-${index}`} data-tone="amber"><span className="capability-review-icon"><Workflow size={17} /></span><div><strong>{String(hook.event)}</strong><code>{String(hook.command)}</code></div><span className="capability-review-kind">Hook</span></div>;})}
        </div>
        <label className="capability-choice"><input type="checkbox" checked={reviewed} disabled={busy} onChange={event => setReviewed(event.target.checked)} /><span>我已查看这些能力，并信任插件来源。</span></label>
      </>}
      {hookReview && <>
        <HookSourceReview hook={hook} />
        <label className="capability-choice"><input type="checkbox" checked={reviewed} disabled={busy} onChange={event => setReviewed(event.target.checked)} /><span>我已检查这些命令，允许它们运行。</span></label>
      </>}
      {(action === 'REMOVE_LOCAL_MCP' || action === 'REMOVE_PLUGIN') && <div className="capability-review-consequence"><Trash2 size={15} /><p>已保存的连接凭据也会清除。对话记录会保留。</p></div>}
    </div>
    <div className="interaction-actions capability-interaction-actions">
      <button disabled={busy} onClick={cancel}>取消</button>
      <button className={`is-primary${removing ? ' is-danger' : ''}`} disabled={busy || ((enableReview || hookReview) && !reviewed)} onClick={() => void submit(enableReview ? {enable_review_accepted: true} : hookReview ? {hook_review_accepted: true} : {})}>{primary}{!removing && <ArrowRight size={14} />}</button>
    </div>
  </div>;
}
