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
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
  Workflow,
  X,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import type {
  McpCreateInput,
  McpServerStatus,
  UserCapabilitySnapshot,
  UserMcpServerCapability,
  UserPluginCapability,
  UserSkillCapability,
} from '../lib/pulsara-types';

type CapabilityTab = 'plugins' | 'mcp' | 'skills';
type AddKind = 'plugin' | 'mcp' | 'skill';

interface CapabilityViewProps {
  snapshot?: UserCapabilitySnapshot;
  loading: boolean;
  error?: string;
  onRefresh: () => Promise<void>;
  onOpenRoot: (root: 'agents' | 'pulsara') => Promise<void>;
  onInstallSkill: (sourcePath: string) => Promise<boolean>;
  onInstallPlugin: (sourcePath: string) => Promise<boolean>;
  onCreateMcp: (input: McpCreateInput) => Promise<boolean>;
  onToggleSkill: (skill: UserSkillCapability, enabled: boolean) => Promise<boolean>;
  onToggleMcp: (server: UserMcpServerCapability, enabled: boolean) => Promise<boolean>;
  onTogglePlugin: (plugin: UserPluginCapability, enabled: boolean) => Promise<boolean>;
  onRemovePlugin: (plugin: UserPluginCapability) => Promise<boolean>;
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

function AddPanel({ kind, onClose, onInstallSkill, onInstallPlugin, onCreateMcp }: {
  kind: AddKind;
  onClose: () => void;
  onInstallSkill: (sourcePath: string) => Promise<boolean>;
  onInstallPlugin: (sourcePath: string) => Promise<boolean>;
  onCreateMcp: (input: McpCreateInput) => Promise<boolean>;
}) {
  const [path, setPath] = useState('');
  const [serverId, setServerId] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [transport, setTransport] = useState<'http' | 'stdio'>('http');
  const [endpoint, setEndpoint] = useState('');
  const [command, setCommand] = useState('');
  const [args, setArgs] = useState('');
  const [subagents, setSubagents] = useState(false);
  const [busy, setBusy] = useState(false);
  const title = kind === 'skill' ? '安装技能' : kind === 'plugin' ? '安装插件' : '添加 MCP 服务';

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    const succeeded = kind === 'skill'
      ? await onInstallSkill(path.trim())
      : kind === 'plugin'
        ? await onInstallPlugin(path.trim())
        : await onCreateMcp({
          serverId: serverId.trim(),
          displayName: displayName.trim(),
          transport,
          endpoint: transport === 'http' ? endpoint.trim() : undefined,
          command: transport === 'stdio' ? command.trim() : undefined,
          args: args.split('\n').map((item) => item.trim()).filter(Boolean),
          availableToSubagents: subagents,
        });
    setBusy(false);
    if (succeeded) onClose();
  };
  const valid = kind === 'mcp'
    ? Boolean(serverId.trim() && (transport === 'http' ? endpoint.trim() : command.trim()))
    : Boolean(path.trim());

  return (
    <div className="capability-add-panel" role="dialog" aria-modal="true" aria-label={title}>
      <div className="capability-add-panel__backdrop" onClick={onClose} />
      <section>
        <header><div><span>添加到这台设备</span><h2>{title}</h2></div><button onClick={onClose} aria-label="关闭"><X size={16} /></button></header>
        {kind !== 'mcp' ? (
          <label className="capability-field"><span>本地目录</span><input autoFocus value={path} onChange={(event) => setPath(event.target.value)} placeholder={kind === 'skill' ? '/绝对路径/到/技能目录' : '/绝对路径/到/插件目录'} /><small>{kind === 'skill' ? '目录中需要包含 SKILL.md。' : '目录中需要包含插件清单；安装后默认保持关闭。'}</small></label>
        ) : (
          <div className="capability-form-grid">
            <label className="capability-field"><span>服务 ID</span><input autoFocus value={serverId} onChange={(event) => setServerId(event.target.value)} placeholder="docs-search" /></label>
            <label className="capability-field"><span>显示名称</span><input value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="文档搜索" /></label>
            <div className="capability-field capability-field--wide"><span>连接方式</span><div className="capability-segmented"><button className={transport === 'http' ? 'is-active' : ''} onClick={() => setTransport('http')}>HTTP</button><button className={transport === 'stdio' ? 'is-active' : ''} onClick={() => setTransport('stdio')}>本地命令</button></div></div>
            {transport === 'http' ? <label className="capability-field capability-field--wide"><span>服务地址</span><input value={endpoint} onChange={(event) => setEndpoint(event.target.value)} placeholder="https://example.com/mcp" /></label> : <><label className="capability-field capability-field--wide"><span>命令</span><input value={command} onChange={(event) => setCommand(event.target.value)} placeholder="uvx mcp-server" /></label><label className="capability-field capability-field--wide"><span>参数</span><textarea value={args} onChange={(event) => setArgs(event.target.value)} placeholder="每行一个参数" /></label></>}
            <label className="capability-check capability-field--wide"><input type="checkbox" checked={subagents} onChange={(event) => setSubagents(event.target.checked)} /><span><strong>也向子任务提供</strong><small>子任务可以使用此服务公开的工具。</small></span></label>
          </div>
        )}
        <footer><button className="secondary-action" onClick={onClose}>取消</button><button className="capability-primary" disabled={!valid || busy} onClick={() => void submit()}>{busy ? <LoaderCircle size={14} /> : <Plus size={14} />}{kind === 'mcp' ? '添加服务' : '安装'}</button></footer>
      </section>
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
  onInstallPlugin,
  onCreateMcp,
  onToggleSkill,
  onToggleMcp,
  onTogglePlugin,
  onRemovePlugin,
}: CapabilityViewProps) {
  const [tab, setTab] = useState<CapabilityTab>('plugins');
  const [query, setQuery] = useState('');
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [rootMenuOpen, setRootMenuOpen] = useState(false);
  const [addKind, setAddKind] = useState<AddKind>();
  const [expanded, setExpanded] = useState<string>();
  const [busyKey, setBusyKey] = useState<string>();
  const [removeCandidate, setRemoveCandidate] = useState<string>();
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const plugins = useMemo(() => (snapshot?.plugins.items ?? []).filter((item) => !normalizedQuery || `${item.name} ${item.description} ${item.id}`.toLocaleLowerCase().includes(normalizedQuery)), [normalizedQuery, snapshot?.plugins.items]);
  const mcp = useMemo(() => (snapshot?.mcp.servers ?? []).filter((item) => !normalizedQuery || `${item.name} ${item.id} ${item.transport.summary}`.toLocaleLowerCase().includes(normalizedQuery)), [normalizedQuery, snapshot?.mcp.servers]);
  const skills = useMemo(() => (snapshot?.skills.items ?? []).filter((item) => !normalizedQuery || `${item.name} ${item.description}`.toLocaleLowerCase().includes(normalizedQuery)), [normalizedQuery, snapshot?.skills.items]);

  const togglePlugin = async (plugin: UserPluginCapability) => {
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
      <div className="capability-page__inner">
        <header className="capability-page-header">
          <div><h1>能力</h1><p>管理这台设备上的插件、MCP 与技能。</p></div>
          <div className="capability-page-actions">
            <div className="capability-menu"><button className="secondary-action" onClick={() => { setRootMenuOpen((value) => !value); setAddMenuOpen(false); }}><FolderOpen size={14} /> 浏览目录</button>{rootMenuOpen && <div className="capability-menu__popover"><button onClick={() => { setRootMenuOpen(false); void onOpenRoot('agents'); }}><FolderOpen size={14} /><span><strong>.agents</strong><small>{snapshot?.roots.find((item) => item.kind === 'agents')?.path}</small></span><ExternalLink size={12} /></button><button onClick={() => { setRootMenuOpen(false); void onOpenRoot('pulsara'); }}><FolderOpen size={14} /><span><strong>.pulsara</strong><small>{snapshot?.roots.find((item) => item.kind === 'pulsara')?.path}</small></span><ExternalLink size={12} /></button></div>}</div>
            <div className="capability-menu"><button className="capability-primary" onClick={() => { setAddMenuOpen((value) => !value); setRootMenuOpen(false); }}>添加 <ChevronDown size={13} /></button>{addMenuOpen && <div className="capability-menu__popover is-compact"><button onClick={() => { setAddKind('plugin'); setAddMenuOpen(false); }}><PackageOpen size={14} /><span><strong>安装插件</strong><small>从本地目录添加</small></span></button><button onClick={() => { setAddKind('mcp'); setAddMenuOpen(false); }}><PlugZap size={14} /><span><strong>添加 MCP</strong><small>连接远程服务或本地命令</small></span></button><button onClick={() => { setAddKind('skill'); setAddMenuOpen(false); }}><BookOpenText size={14} /><span><strong>安装技能</strong><small>从本地目录添加</small></span></button></div>}</div>
          </div>
        </header>

        <div className="capability-page-toolbar">
          <div className="capability-tabs" role="tablist">
            <button className={tab === 'plugins' ? 'is-active' : ''} onClick={() => setTab('plugins')} role="tab">插件 <span>{snapshot?.plugins.items.length ?? 0}</span></button>
            <button className={tab === 'mcp' ? 'is-active' : ''} onClick={() => setTab('mcp')} role="tab">MCP <span>{snapshot?.mcp.servers.length ?? 0}</span></button>
            <button className={tab === 'skills' ? 'is-active' : ''} onClick={() => setTab('skills')} role="tab">技能 <span>{snapshot?.skills.items.length ?? 0}</span></button>
          </div>
          <div className="capability-search"><Search size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`搜索${tab === 'plugins' ? '插件' : tab === 'mcp' ? ' MCP' : '技能'}`} /></div>
          <button className="capability-refresh" onClick={() => void onRefresh()} disabled={loading} aria-label="刷新能力">{loading ? <LoaderCircle size={14} /> : <RefreshCw size={14} />}</button>
        </div>

        {error && <div className="capability-page-notice"><CircleAlert size={15} /><span>{error}</span></div>}
        {snapshot?.adoption && snapshot.adoption.attentionSessions > 0 && <div className="capability-page-notice"><CircleAlert size={15} /><span>{snapshot.adoption.attentionSessions} 个已打开会话需要稍后重试。</span></div>}

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
                {open && <div className="capability-list-detail"><div className="capability-detail-stats"><span><strong>{plugin.skillCount}</strong> 技能</span><span><strong>{plugin.mcpCount}</strong> MCP 服务</span>{plugin.author && <span>作者 {plugin.author}</span>}</div><p title={plugin.packageRoot}>{plugin.packageRoot}</p>{plugin.enabled && <div className="capability-success"><Check size={13} /> 已用于当前打开的会话</div>}<div className="capability-detail-actions">{removeCandidate === plugin.id ? <><span className="capability-remove-warning">确定从 Pulsara 中移除？</span><button className="secondary-ghost" onClick={() => setRemoveCandidate(undefined)}>取消</button><button className="danger-ghost" disabled={busyKey === `remove:${plugin.id}`} onClick={() => void removePlugin(plugin)}><Trash2 size={13} />确认移除</button></> : <button className="danger-ghost" onClick={() => setRemoveCandidate(plugin.id)}><Trash2 size={13} />移除插件</button>}</div></div>}
              </article>;
            }) : <EmptyState>{normalizedQuery ? '没有匹配的插件。' : '还没有安装用户插件。'}</EmptyState>
          ) : tab === 'mcp' ? (
            mcp.length ? mcp.map((server) => {
              const open = expanded === `mcp:${server.id}`;
              return <article className={`capability-list-item capability-list-item--mcp${open ? ' is-expanded' : ''}`} key={server.id}>
                <div className="capability-list-row" onClick={() => setExpanded(open ? undefined : `mcp:${server.id}`)}>
                  <span className="capability-list-icon"><PlugZap size={18} /></span>
                  <span className="capability-list-copy"><strong>{server.name}</strong><small>{server.transport.summary}</small></span>
                  <span className={`capability-live-status status-${server.status}`}><i />{mcpStatusLabels[server.status]}</span>
                  <ChevronRight className={open ? 'is-open' : ''} size={14} />
                  <Toggle checked={server.enabled} busy={busyKey === `mcp:${server.id}`} label={`${server.enabled ? '关闭' : '开启'} ${server.name}`} onChange={() => void toggleMcp(server)} />
                </div>
                {open && <div className="capability-list-detail"><div className="capability-detail-stats"><span><strong>{server.toolCount}</strong> 工具</span><span><strong>{server.resourceCount}</strong> 资源</span><span><Workflow size={12} /> {server.availableToSubagents ? '主任务与子任务' : '仅主任务'}</span><span>{server.transport.kind === 'stdio' ? '本地命令' : 'HTTP'}</span></div>{server.enabled && (server.toolCount > 0 || server.resourceCount > 0) && <div className="capability-success"><Check size={13} /> Pulsara 会按需使用已发现的能力</div>}{server.instructions && <p>{server.instructions}</p>}{server.tools.length > 0 && <div className="capability-tool-grid">{server.tools.map((tool) => <div key={`${server.id}:${tool.remoteName}`}><code>{tool.name}</code><span>{tool.description || '没有说明'}</span></div>)}</div>}</div>}
              </article>;
            }) : <EmptyState>{normalizedQuery ? '没有匹配的 MCP 服务。' : <>还没有用户 MCP 服务。配置文件位于 <code>{snapshot?.mcp.configPath}</code>。</>}</EmptyState>
          ) : (
            skills.length ? skills.map((skill) => <article className={`capability-list-item capability-list-item--skill${skill.enabled ? '' : ' is-disabled'}`} key={`${skill.root}:${skill.path}`}><div className="capability-list-row"><span className="capability-list-icon"><BookOpenText size={18} /></span><span className="capability-list-copy"><strong>{skill.name}</strong><small>{skill.description}</small></span><span className="capability-source-pill">{skill.root === 'agents' ? '.agents' : '.pulsara'}</span><Toggle checked={skill.enabled} busy={busyKey === `skill:${skill.path}`} label={`${skill.enabled ? '关闭' : '开启'} ${skill.name}`} onChange={() => void toggleSkill(skill)} /></div></article>) : <EmptyState>{normalizedQuery ? '没有匹配的技能。' : '还没有安装用户技能。'}</EmptyState>
          )}
        </div>

        <footer className="capability-page-footer"><Box size={13} /><span>这里只显示用户目录中的能力；项目目录与 Pulsara 自带内容不会出现在这里。</span></footer>
      </div>
      {addKind && <AddPanel kind={addKind} onClose={() => setAddKind(undefined)} onInstallSkill={onInstallSkill} onInstallPlugin={onInstallPlugin} onCreateMcp={onCreateMcp} />}
    </section>
  );
}
