'use client';

import {
  BookOpenText,
  Box,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  ExternalLink,
  FolderOpen,
  LoaderCircle,
  MoreHorizontal,
  PackageOpen,
  PlugZap,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
  Workflow,
} from 'lucide-react';
import { useState } from 'react';
import { McpEditor, mcpAuthLabels } from './mcp-editor';
import { SkillImporter } from './skill-importer';
import { McpImporter } from './mcp-importer';
import { PluginConnectionEditor } from './plugin-connection-editor';
import { PluginImporter } from './plugin-importer';
import type {
  PluginImportOptions,
  PluginImportDiscovery,
  McpEditInput,
  PluginMcpConnection,
  PluginMcpEditInput,
  McpConnectionTestResult,
  McpImportSource,
  McpImportSelection,
  McpImportPreview,
  SkillImportInput,
  SkillImportCandidate,
  McpServerStatus,
  UserCapabilitySnapshot,
  UserMcpServerCapability,
  UserPluginCapability,
  UserSkillCapability,
} from '../lib/pulsara-types';

type CapabilityTab = 'plugins' | 'mcp' | 'skills';
type AddKind = 'plugin' | 'mcp' | 'skill' | 'mcp-import';

interface CapabilityViewProps {
  snapshot?: UserCapabilitySnapshot;
  loading: boolean;
  error?: string;
  onRefresh: () => Promise<void>;
  onOpenRoot: (root: 'agents' | 'pulsara') => Promise<void>;
  onInstallSkill: (input: SkillImportInput) => Promise<boolean>;
  onPreviewSkills: (sourcePath: string) => Promise<SkillImportCandidate[]>;
  onInstallPlugin: (sourcePath: string, options?: PluginImportOptions) => Promise<boolean>;
  onPreviewPlugin: (sourcePath: string) => Promise<PluginImportDiscovery>;
  onCreateMcp: (input: McpEditInput) => Promise<boolean>;
  onTestMcp: (input: McpEditInput) => Promise<McpConnectionTestResult>;
  onPreviewMcpImport: (input: McpImportSource) => Promise<McpImportPreview[]>;
  onImportMcp: (input: McpImportSelection) => Promise<boolean>;
  onEditMcp: (server: UserMcpServerCapability, input: McpEditInput) => Promise<boolean>;
  onRemoveMcp: (server: UserMcpServerCapability) => Promise<boolean>;
  onMcpAuthorization: (server: UserMcpServerCapability, action: 'login' | 'status' | 'cancel' | 'logout') => Promise<void>;
  onToggleSkill: (skill: UserSkillCapability, enabled: boolean) => Promise<boolean>;
  onRemoveSkill: (skill: UserSkillCapability) => Promise<boolean>;
  onToggleMcp: (server: UserMcpServerCapability, enabled: boolean) => Promise<boolean>;
  onTogglePlugin: (plugin: UserPluginCapability, enabled: boolean) => Promise<boolean>;
  onRemovePlugin: (plugin: UserPluginCapability) => Promise<boolean>;
  onEditPluginConnection: (plugin: UserPluginCapability, connection: PluginMcpConnection, input: PluginMcpEditInput) => Promise<boolean>;
  onPluginMcpAuthorization: (plugin: UserPluginCapability, connection: PluginMcpConnection, action: 'login' | 'status' | 'cancel' | 'logout') => Promise<void>;
}

const mcpStatusLabels: Record<McpServerStatus, string> = {
  disabled: '已关闭',
  configured: '已配置',
  connecting: '正在连接',
  discovering: '正在读取',
  ready: '已连接',
  'failed-retryable': '需要重试',
  failed: '需要留意',
  updating: '正在更新',
  closed: '已关闭',
};

function Toggle({ checked, busy, label, onChange }: {
  checked: boolean;
  busy: boolean;
  label: string;
  onChange: () => void;
}) {
  return (
    <button
      className={`capability-switch${checked ? ' is-on' : ''}`}
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={busy}
      onClick={(event) => { event.stopPropagation(); onChange(); }}
    >
      {busy ? <LoaderCircle size={12} /> : <i />}
    </button>
  );
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return <div className="capability-page-empty"><Sparkles size={18} /><span>{children}</span></div>;
}

function CapabilityDrawer({ open, children }: { open: boolean; children: React.ReactNode }) {
  return (
    <div className={`capability-drawer${open ? ' is-open' : ''}`} aria-hidden={!open} inert={!open}>
      <div className="capability-drawer__inner">{children}</div>
    </div>
  );
}


export function CapabilityView({
  snapshot,
  loading,
  error,
  onRefresh,
  onOpenRoot,
  onInstallSkill,
  onPreviewSkills,
  onInstallPlugin,
  onPreviewPlugin,
  onCreateMcp,
  onTestMcp,
  onPreviewMcpImport,
  onImportMcp,
  onEditMcp,
  onRemoveMcp,
  onMcpAuthorization,
  onToggleSkill,
  onRemoveSkill,
  onToggleMcp,
  onTogglePlugin,
  onRemovePlugin,
  onEditPluginConnection,
  onPluginMcpAuthorization,
}: CapabilityViewProps) {
  const [tab, setTab] = useState<CapabilityTab>('plugins');
  const [query, setQuery] = useState('');
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [rootMenuOpen, setRootMenuOpen] = useState(false);
  const [addKind, setAddKind] = useState<AddKind>();
  const [editingMcp, setEditingMcp] = useState<UserMcpServerCapability>();
  const [editingPluginConnection, setEditingPluginConnection] = useState<{plugin: UserPluginCapability; connection: PluginMcpConnection}>();
  const [enableCandidate, setEnableCandidate] = useState<UserPluginCapability>();
  const [expanded, setExpanded] = useState<string>();
  const [busyKey, setBusyKey] = useState<string>();
  const [removeCandidate, setRemoveCandidate] = useState<string>();
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const pluginMcp = (snapshot?.plugins.items ?? []).flatMap((plugin) => (plugin.mcpConnections ?? []).map((connection) => ({plugin, connection})))
    .filter(({plugin, connection}) => !normalizedQuery || `${plugin.name} ${connection.serverId}`.toLocaleLowerCase().includes(normalizedQuery));
  const plugins = (snapshot?.plugins.items ?? []).filter((item) => !normalizedQuery || `${item.name} ${item.description} ${item.id}`.toLocaleLowerCase().includes(normalizedQuery));
  const mcp = (snapshot?.mcp.servers ?? []).filter((item) => !normalizedQuery || `${item.name} ${item.id} ${item.transport.summary}`.toLocaleLowerCase().includes(normalizedQuery));
  const skills = (snapshot?.skills.items ?? []).filter((item) => !normalizedQuery || `${item.name} ${item.description}`.toLocaleLowerCase().includes(normalizedQuery));

  const togglePlugin = async (plugin: UserPluginCapability) => {
    if (!plugin.enabled) { setEnableCandidate(plugin); return; }
    const key = `plugin:${plugin.id}`;
    setBusyKey(key);
    await onTogglePlugin(plugin, !plugin.enabled);
    setBusyKey(undefined);
  };
  const toggleMcp = async (server: UserMcpServerCapability) => {
    const key = `mcp:${server.id}`;
    setBusyKey(key);
    await onToggleMcp(server, !server.enabled);
    setBusyKey(undefined);
  };
  const toggleSkill = async (skill: UserSkillCapability) => {
    const key = `skill:${skill.path}`;
    setBusyKey(key);
    await onToggleSkill(skill, !skill.enabled);
    setBusyKey(undefined);
  };
  const removePlugin = async (plugin: UserPluginCapability) => {
    setBusyKey(`remove:${plugin.id}`);
    const removed = await onRemovePlugin(plugin);
    setBusyKey(undefined);
    if (removed) {
      setRemoveCandidate(undefined);
      setExpanded(undefined);
    }
  };

  return (
    <section className="surface-view capability-page">
        <header className="page-header capability-page-header">
          <div><span className="page-kicker">扩展能力</span><h1>能力</h1><p>管理这台设备上的插件、MCP 与技能。</p></div>
          <div className="capability-page-actions">
            <button className="secondary-action" onClick={() => setAddKind('mcp-import')}><PlugZap size={14} />导入 MCP 配置</button>
            <div className="capability-menu"><button className="secondary-action" onClick={() => { setRootMenuOpen((value) => !value); setAddMenuOpen(false); }}><FolderOpen size={14} /> 浏览目录</button>{rootMenuOpen && <div className="capability-menu__popover"><button onClick={() => { setRootMenuOpen(false); void onOpenRoot('agents'); }}><FolderOpen size={14} /><span><strong>.agents</strong><small>{snapshot?.roots.find((item) => item.kind === 'agents')?.path}</small></span><ExternalLink size={12} /></button><button onClick={() => { setRootMenuOpen(false); void onOpenRoot('pulsara'); }}><FolderOpen size={14} /><span><strong>.pulsara</strong><small>{snapshot?.roots.find((item) => item.kind === 'pulsara')?.path}</small></span><ExternalLink size={12} /></button></div>}</div>
            <div className="capability-menu"><button className="capability-primary" onClick={() => { setAddMenuOpen((value) => !value); setRootMenuOpen(false); }}>添加 <ChevronDown size={13} /></button>{addMenuOpen && <div className="capability-menu__popover is-compact"><button onClick={() => { setAddKind('plugin'); setAddMenuOpen(false); }}><PackageOpen size={14} /><span><strong>安装插件</strong><small>从本地目录添加</small></span></button><button onClick={() => { setAddKind('mcp'); setAddMenuOpen(false); }}><PlugZap size={14} /><span><strong>添加 MCP</strong><small>连接远程服务或本地命令</small></span></button><button onClick={() => { setAddKind('skill'); setAddMenuOpen(false); }}><BookOpenText size={14} /><span><strong>安装技能</strong><small>从本地目录添加</small></span></button></div>}</div>
          </div>
        </header>

      <div className="capability-page__inner">
        <div className="capability-page-toolbar">
          <div className="capability-tabs" role="tablist">
            <button className={tab === 'plugins' ? 'is-active' : ''} onClick={() => setTab('plugins')} role="tab">插件 <span>{snapshot?.plugins.items.length ?? 0}</span></button>
            <button className={tab === 'mcp' ? 'is-active' : ''} onClick={() => setTab('mcp')} role="tab">MCP <span>{(snapshot?.mcp.servers.length ?? 0) + (snapshot?.plugins.items ?? []).reduce((total, plugin) => total + (plugin.mcpConnections?.length ?? 0), 0)}</span></button>
            <button className={tab === 'skills' ? 'is-active' : ''} onClick={() => setTab('skills')} role="tab">技能 <span>{snapshot?.skills.items.length ?? 0}</span></button>
          </div>
          <div className="capability-search"><Search size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`搜索${tab === 'plugins' ? '插件' : tab === 'mcp' ? ' MCP' : '技能'}`} /></div>
          <button className="capability-refresh" onClick={() => void onRefresh()} disabled={loading} aria-label="刷新能力">{loading ? <LoaderCircle size={14} /> : <RefreshCw size={14} />}</button>
        </div>

        {error && <div className="capability-page-notice"><CircleAlert size={15} /><span>{error}</span></div>}
        {snapshot?.adoption && snapshot.adoption.attentionSessions > 0 && <div className="capability-page-notice"><CircleAlert size={15} /><span>{snapshot.adoption.attentionSessions} 个已打开会话需要稍后重试。</span></div>}
        {snapshot?.adoption && snapshot.adoption.pendingSessions > 0 && <div className="capability-page-notice"><CircleAlert size={15} /><span>配置已保存，{snapshot.adoption.pendingSessions} 个会话将在下次模型请求前的安全时机载入。</span></div>}

        <div className="capability-page-list">
          {loading && !snapshot ? <EmptyState>正在读取这台设备上的能力…</EmptyState> : tab === 'plugins' ? (
            plugins.length ? plugins.map((plugin) => {
              const open = expanded === `plugin:${plugin.id}`;
              return <article className={`capability-list-item capability-list-item--plugin${open ? ' is-expanded' : ''}`} key={plugin.id}>
                <div className="capability-list-row" onClick={() => setExpanded(open ? undefined : `plugin:${plugin.id}`)}>
                  <span className="capability-list-icon"><PackageOpen size={18} /></span>
                  <span className="capability-list-copy"><strong>{plugin.name}</strong><small>{plugin.description}</small></span>
                  <span className="capability-list-meta">{plugin.version ? `v${plugin.version}` : '本地插件'}</span>
                  <button className="capability-row-more" aria-label={`查看 ${plugin.name}`}><MoreHorizontal size={15} /></button>
                  <Toggle checked={plugin.enabled} busy={busyKey === `plugin:${plugin.id}`} label={`${plugin.enabled ? '关闭' : '开启'} ${plugin.name}`} onChange={() => void togglePlugin(plugin)} />
                </div>
                <CapabilityDrawer open={open}>
                  <div className="capability-list-detail"><div className="capability-detail-stats"><span><strong>{plugin.skillCount}</strong> 技能</span><span><strong>{plugin.mcpCount}</strong> MCP 服务</span>{plugin.author && <span>作者 {plugin.author}</span>}</div>{plugin.enabled && <div className="capability-success"><Check size={13} /> 已启用；会话将在安全时机采用</div>}{(plugin.mcpConnections ?? []).map((connection) => <div className="capability-detail-actions" key={connection.serverId}><span>{connection.serverId}{!plugin.enabled && ' · 插件已关闭'}</span><button className="secondary-ghost" onClick={() => setEditingPluginConnection({plugin, connection})}><PlugZap size={13} />配置连接</button></div>)}<div className="capability-detail-actions">{removeCandidate === plugin.id ? <><span className="capability-remove-warning">确定从 Pulsara 中移除？</span><button className="secondary-ghost" onClick={() => setRemoveCandidate(undefined)}>取消</button><button className="danger-ghost" disabled={busyKey === `remove:${plugin.id}`} onClick={() => void removePlugin(plugin)}><Trash2 size={13} />确认移除</button></> : <button className="danger-ghost" onClick={() => setRemoveCandidate(plugin.id)}><Trash2 size={13} />移除插件</button>}</div></div>
                </CapabilityDrawer>
              </article>;
            }) : <EmptyState>{normalizedQuery ? '没有匹配的插件。' : '还没有安装用户插件。'}</EmptyState>
          ) : tab === 'mcp' ? (
            <>
            {pluginMcp.map(({plugin, connection}) => {
              const open = expanded === `plugin-mcp:${plugin.id}:${connection.serverId}`;
              return <article className={`capability-list-item capability-list-item--mcp${plugin.enabled ? '' : ' is-disabled'}${open ? ' is-expanded' : ''}`} key={`plugin:${plugin.id}:${connection.serverId}`}>
                <div className="capability-list-row capability-list-row--connection" onClick={() => setExpanded(open ? undefined : `plugin-mcp:${plugin.id}:${connection.serverId}`)}>
                  <span className="capability-list-icon"><PlugZap size={18} /></span>
                  <span className="capability-list-copy"><strong>{connection.serverId}</strong><small>来自插件 · {plugin.name}</small></span>
                  <span className="capability-source-pill">{plugin.enabled ? '随插件启用' : '插件已关闭'}</span>
                  <ChevronRight className={open ? 'is-open' : ''} size={14} />
                </div>
                <CapabilityDrawer open={open}>
                  <div className="capability-list-detail"><div className="capability-detail-actions"><button className="secondary-ghost" onClick={() => setEditingPluginConnection({plugin, connection})}>配置连接与凭据</button></div></div>
                </CapabilityDrawer>
              </article>;
            })}
            {mcp.length ? mcp.map((server) => {
              const open = expanded === `mcp:${server.id}`;
              return <article className={`capability-list-item capability-list-item--mcp${open ? ' is-expanded' : ''}`} key={server.id}>
                <div className="capability-list-row" onClick={() => setExpanded(open ? undefined : `mcp:${server.id}`)}>
                  <span className="capability-list-icon"><PlugZap size={18} /></span>
                  <span className="capability-list-copy"><strong>{server.name}</strong><small>{server.transport.summary}</small></span>
                  <span className={`capability-live-status status-${server.status}`}><i />{mcpStatusLabels[server.status]}</span>
                  <ChevronRight className={open ? 'is-open' : ''} size={14} />
                  <Toggle checked={server.enabled} busy={busyKey === `mcp:${server.id}`} label={`${server.enabled ? '关闭' : '开启'} ${server.name}`} onChange={() => void toggleMcp(server)} />
                </div>
                <CapabilityDrawer open={open}>
                  <div className="capability-list-detail"><div className="capability-detail-stats"><span><strong>{server.toolCount}</strong> 工具</span><span><strong>{server.resourceCount}</strong> 资源</span><span><Workflow size={12} /> {server.availableToSubagents ? '主任务与子任务' : '仅主任务'}</span><span>{server.transport.kind === 'stdio' ? '本地命令' : 'HTTP'}</span></div>{server.enabled && (server.toolCount > 0 || server.resourceCount > 0) && <div className="capability-success"><Check size={13} /> Pulsara 会按需使用已发现的能力</div>}{server.instructions && <p>{server.instructions}</p>}{server.tools.length > 0 && <div className="capability-tool-grid">{server.tools.map((tool) => <div key={`${server.id}:${tool.remoteName}`}><code>{tool.name}</code><span>{tool.description || '没有说明'}</span></div>)}</div>}</div>
                  <div className="capability-detail-actions capability-drawer-actions">
                  <button className="secondary-ghost" onClick={() => setEditingMcp(server)}>编辑连接与凭据</button>
                  {(server.config.auth as { type?: string } | undefined)?.type === 'oauth' && <>
                    <button className="secondary-ghost" onClick={() => void onMcpAuthorization(server, 'login')}>登录授权</button>
                    <button className="secondary-ghost" onClick={() => void onMcpAuthorization(server, 'status')}>查看登录状态</button>
                    <button className="secondary-ghost" onClick={() => void onMcpAuthorization(server, 'cancel')}>取消登录</button>
                    <button className="secondary-ghost" onClick={() => void onMcpAuthorization(server, 'logout')}>注销本地授权</button>
                  </>}
                  {removeCandidate === `mcp:${server.id}` ? <>
                    <span className="capability-remove-warning">同时清除该连接保存的凭据？</span>
                    <button className="secondary-ghost" onClick={() => setRemoveCandidate(undefined)}>取消</button>
                    <button className="danger-ghost" disabled={busyKey === `remove-mcp:${server.id}`} onClick={() => void (async () => {
                      setBusyKey(`remove-mcp:${server.id}`);
                      try { if (await onRemoveMcp(server)) setRemoveCandidate(undefined); } finally { setBusyKey(undefined); }
                    })()}><Trash2 size={13} />确认移除</button>
                  </> : <button className="danger-ghost" onClick={() => setRemoveCandidate(`mcp:${server.id}`)}><Trash2 size={13} />移除服务</button>}
                  </div>
                </CapabilityDrawer>
              </article>;
            }) : pluginMcp.length ? null : <EmptyState>{normalizedQuery ? '没有匹配的 MCP 服务。' : <>还没有 MCP 服务。配置文件位于 <code>{snapshot?.mcp.configPath}</code>。</>}</EmptyState>}
            </>
          ) : (
            skills.length ? skills.map((skill) => {
              const open = expanded === `skill:${skill.path}`;
              return <article className={`capability-list-item capability-list-item--skill${skill.enabled ? '' : ' is-disabled'}${open ? ' is-expanded' : ''}`} key={`${skill.root}:${skill.path}`}>
                <div className="capability-list-row" onClick={() => setExpanded(open ? undefined : `skill:${skill.path}`)}>
                  <span className="capability-list-icon"><BookOpenText size={18} /></span><span className="capability-list-copy"><strong>{skill.name}</strong><small>{skill.description}</small></span>
                  <span className="capability-source-pill">{skill.root === 'agents' ? '.agents' : '.pulsara'}</span>
                  <Toggle checked={skill.enabled} busy={busyKey === `skill:${skill.path}`} label={`${skill.enabled ? '关闭' : '开启'} ${skill.name}`} onChange={() => void toggleSkill(skill)} />
                </div>
                <CapabilityDrawer open={open}>
                  <div className="capability-list-detail capability-list-detail--skill">
                    <p>{skill.root === 'agents' ? '删除后，其他应用也可能无法使用此技能。' : '删除后，Pulsara将不再使用此技能。'}</p>
                    <div className="capability-detail-actions">{removeCandidate === `skill:${skill.path}` ? <>
                      <button className="secondary-ghost" onClick={() => setRemoveCandidate(undefined)}>取消</button>
                      <button className="danger-ghost" disabled={busyKey === `remove-skill:${skill.path}`} onClick={() => void (async () => {
                        setBusyKey(`remove-skill:${skill.path}`);
                        try { if (await onRemoveSkill(skill)) setRemoveCandidate(undefined); } finally { setBusyKey(undefined); }
                      })()}><Trash2 size={13} />确认删除技能</button>
                    </> : <button className="danger-ghost" onClick={() => setRemoveCandidate(`skill:${skill.path}`)}><Trash2 size={13} />删除技能</button>}</div>
                  </div>
                </CapabilityDrawer>
              </article>;
            }) : <EmptyState>{normalizedQuery ? '没有匹配的技能。' : '还没有安装用户技能。'}</EmptyState>
          )}
        </div>

        <footer className="capability-page-footer"><Box size={13} /><span>这里只显示用户目录中的能力；项目目录与 Pulsara 自带内容不会出现在这里。</span></footer>
      </div>
      {addKind === 'plugin' && <PluginImporter onClose={() => setAddKind(undefined)} onInstall={onInstallPlugin} onPreview={onPreviewPlugin} />}
      {enableCandidate && <div className="capability-add-panel" role="dialog" aria-modal="true" aria-label="确认启用插件">
        <div className="capability-add-panel__backdrop" onClick={busyKey ? undefined : () => setEnableCandidate(undefined)} />
        <section><header><div><span>检查当前安装包与连接</span><h2>启用 {enableCandidate.name}</h2></div></header>
          <div className="capability-dialog-body">
          <p>启用后，插件可在会话的安全时机启动本地程序、连接外部服务并运行已声明的 Hook。请只启用你信任的来源。</p>
          <p className="capability-detail-note">{enableCandidate.packageRoot}</p>
          {(enableCandidate.mcpConnections ?? []).map((connection) => {
            const transport = connection.config.transport as Record<string, unknown>;
            const credentials = enableCandidate.connectionReview?.find((item) => item.overlay.local_server_id === connection.serverId)?.credentials ?? [];
            return <div className="skill-import-candidate" key={connection.serverId}><strong>{connection.serverId}</strong>
              <code>{String(transport.endpoint ?? transport.command ?? '')}</code>
              {transport.type === 'stdio' && <small>{JSON.stringify(transport.args ?? [])} · {String(transport.cwd ?? '默认工作目录')}</small>}
              <small>认证：{mcpAuthLabels[String((connection.config.auth as Record<string, unknown> | undefined)?.type ?? 'none')] ?? '未指定'}</small>
              {credentials.map((item) => <small key={item.name}>{item.name}：{item.present ? '已配置' : '待配置'}</small>)}
            </div>;
          })}
          </div>
          <footer><button className="secondary-action" disabled={Boolean(busyKey)} onClick={() => setEnableCandidate(undefined)}>取消</button>
            <button className="capability-primary" disabled={Boolean(busyKey)} onClick={() => void (async () => {
              setBusyKey(`plugin:${enableCandidate.id}`);
              try { if (await onTogglePlugin(enableCandidate, true)) setEnableCandidate(undefined); }
              finally { setBusyKey(undefined); }
            })()}>确认启用</button></footer>
        </section>
      </div>}
      {editingPluginConnection && <PluginConnectionEditor plugin={editingPluginConnection.plugin} connection={editingPluginConnection.connection} onClose={() => setEditingPluginConnection(undefined)} onSave={(input) => onEditPluginConnection(editingPluginConnection.plugin, editingPluginConnection.connection, input)} onAuthorization={(action) => onPluginMcpAuthorization(editingPluginConnection.plugin, editingPluginConnection.connection, action)} />}
      {addKind === 'skill' && <SkillImporter onClose={() => setAddKind(undefined)} onInstall={onInstallSkill} onPreview={onPreviewSkills} />}
      {addKind === 'mcp' && <McpEditor onClose={() => setAddKind(undefined)} onSave={onCreateMcp} onTest={onTestMcp} />}
      {addKind === 'mcp-import' && <McpImporter onClose={() => setAddKind(undefined)} onPreview={onPreviewMcpImport} onImport={onImportMcp} />}
      {editingMcp && <McpEditor server={editingMcp} onClose={() => setEditingMcp(undefined)} onSave={(input) => onEditMcp(editingMcp, input)} onTest={onTestMcp} />}
    </section>
  );
}
