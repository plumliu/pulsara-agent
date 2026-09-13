'use client';

import {
  AlertTriangle,
  Blocks,
  Bot,
  ChevronDown,
  ExternalLink,
  FolderCog,
  ListChecks,
  LoaderCircle,
  PanelRightClose,
  Plus,
  RefreshCw,
  Server,
  Sparkles,
  TerminalSquare,
  Trash2,
  Wrench,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { McpEditor } from './mcp-editor';
import { McpImporter } from './mcp-importer';
import { SkillImporter } from './skill-importer';
import type {
  AgentTask,
  CapabilitySnapshot,
  McpEditInput,
  McpImportSource,
  McpImportPreview,
  McpImportSelection,
  McpConnectionTestResult,
  McpServerCapability,
  UserMcpServerCapability,
  SessionSummary,
  SkillCatalogIssue,
  SkillCapability,
  SkillImportInput,
  SkillImportCandidate,
  SubagentActivity,
} from '../lib/pulsara-types';
import type { MarkdownNotify } from './markdown-body';
import { TaskWorkspace } from './task-workspace';
import { BackgroundTerminalPanel } from './background-terminal-panel';
import type {
  BackgroundProcessLog,
  BackgroundProcessPage,
  CommandReceipt,
  ToolArtifactPage,
  UserControlCommandRef,
  UserControlQueryResult,
} from '../lib/runtime-adapter';

interface InspectorPanelProps {
  projectMcpForms: ProjectMcpForms;
  session: SessionSummary;
  isOpen: boolean;
  agentTasks: AgentTask[];
  loading: boolean;
  canControl: boolean;
  capabilities?: CapabilitySnapshot;
  capabilityLoading: boolean;
  capabilityError?: string;
  capabilityBusy?: string;
  error?: string;
  onRetry: () => void;
  taskActivities: ReadonlyMap<string, SubagentActivity[]>;
  onLoadTaskActivities: (taskId: string, cursor?: string) => ReturnType<import('../lib/runtime-adapter').RuntimeAdapter['listSessionTaskActivities']>;
  taskArtifactOwnerKey: string;
  onReadToolArtifact: (resultEntryId: string, offsetChars: number) => Promise<ToolArtifactPage>;
  onCancelTask: (task: AgentTask) => Promise<void>;
  backgroundOwnerKey: string;
  backgroundHostSessionId?: string;
  backgroundControlAdmissionDeadlineMs?: number;
  onLoadBackgroundProcesses: (cursor?: string) => Promise<BackgroundProcessPage>;
  onReadBackgroundProcessLog: (processId: string, cursor?: string) => Promise<BackgroundProcessLog>;
  onTerminateBackgroundProcess: (reference: UserControlCommandRef) => Promise<CommandReceipt>;
  onQueryControl: (reference: UserControlCommandRef) => Promise<UserControlQueryResult>;
  onRetryCapabilities: () => void;
  onToggleProjectSkill: (skill: SkillCapability, enabled: boolean) => Promise<void>;
  onPreviewProjectSkills: (sourcePath: string) => Promise<SkillImportCandidate[]>;
  onInstallProjectSkill: (input: SkillImportInput) => Promise<void>;
  onRemoveProjectSkill: (skill: SkillCapability) => Promise<void>;
  onCreateProjectMcp: (input: McpEditInput) => Promise<void>;
  onEditProjectMcp: (server: McpServerCapability, input: McpEditInput) => Promise<void>;
  onToggleProjectMcp: (server: McpServerCapability, enabled: boolean) => Promise<void>;
  onRemoveProjectMcp: (server: McpServerCapability) => Promise<void>;
  onReconnectProjectMcp: (server: McpServerCapability) => Promise<void>;
  onOpenUserCapabilities: () => void;
  onNotify: MarkdownNotify;
  onClose: () => void;
}

interface ProjectMcpForms {
  preview: (input: McpImportSource) => Promise<McpImportPreview[]>;
  import: (input: McpImportSelection) => Promise<boolean>;
  test: (input: McpEditInput) => Promise<McpConnectionTestResult>;
  authorize: (server: McpServerCapability, action: 'login' | 'status' | 'cancel' | 'logout') => Promise<void>;
}

type InspectorView = 'tasks' | 'capabilities' | 'background';
type ProjectCapabilityKind = 'skills' | 'mcp';

const capabilitySourceLabels = {
  workspace: '此目录',
  user: '用户级',
  plugin: '插件',
  bundled: '内置',
  host: '启动配置',
} as const;

const mcpStatusLabels: Record<McpServerCapability['status'], string> = {
  disabled: '已关闭',
  configured: '待连接',
  connecting: '连接中',
  discovering: '正在读取工具',
  ready: '已连接',
  'failed-retryable': '需要重连',
  failed: '需要留意',
  updating: '正在更新',
  closed: '已关闭',
};

function mcpFailureReason(category?: string): string {
  if (!category) return '连接没有完成，可展开后重试';
  if (category === 'FileNotFoundError') return '找不到启动命令，请检查本地安装';
  if (category === 'TimeoutError') return '连接超时，请检查网络或服务状态';
  if (
    category === 'McpProtocolConformanceError'
    || category === 'MCP_PROTOCOL_CONFORMANCE_FAILED'
    || category === 'McpWireBoundExceeded'
    || category === 'ValueError'
  ) {
    return '服务返回的内容不符合当前 MCP 要求';
  }
  if (
    category === 'McpTransportOperationError'
    || category === 'MCP_TRANSPORT_FAILED'
    || category === 'MCPError'
  ) return '连接在通信时中断，请检查服务状态后重试';
  return '连接没有完成，可展开后重试';
}

function skillIssueSummary(issue: SkillCatalogIssue): string {
  if (issue.kind !== 'invalid' || !issue.path) return issue.title;
  const segments = issue.path.split(/[\\/]/u).filter(Boolean);
  const last = segments.at(-1);
  const name = last?.toLowerCase() === 'skill.md' ? segments.at(-2) : last;
  return name ? `${name} 没有通过检查` : issue.title;
}

function CapabilitySwitch({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      className={`project-capability-switch${checked ? ' is-on' : ''}`}
      role="switch"
      aria-label={label}
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <i />
    </button>
  );
}

function ProjectCapabilityDialog({
  initialKind,
  returnFocusTo,
  onClose,
  onPreviewSkills,
  onInstallSkill,
  onCreateMcp,
  credentialScopeKey,
  mcpForms,
}: {
  mcpForms: ProjectMcpForms;
  initialKind: ProjectCapabilityKind;
  returnFocusTo?: HTMLElement | null;
  onClose: () => void;
  onPreviewSkills: (sourcePath: string) => Promise<SkillImportCandidate[]>;
  onInstallSkill: (input: SkillImportInput) => Promise<void>;
  onCreateMcp: (input: McpEditInput) => Promise<void>;
  credentialScopeKey: string;
}) {

  useEffect(() => {
    const application = document.querySelector<HTMLElement>('main.pulsara-shell');
    const wasInert = application?.inert ?? false;
    const opener = returnFocusTo ?? undefined;
    if (application) application.inert = true;
    return () => {
      if (application) application.inert = wasInert;
      window.requestAnimationFrame(() => opener?.focus());
    };
  }, [returnFocusTo]);

  if (initialKind === 'mcp') return createPortal(<McpEditor credentialScopeKey={credentialScopeKey} onClose={onClose} onTest={mcpForms.test} onSave={async (input) => { await onCreateMcp(input); return true; }} />, document.body);
  return createPortal(<SkillImporter scopeLabel="应用到这个目录的所有会话" onPreview={onPreviewSkills} onInstall={async (input) => {await onInstallSkill(input); return true;}} onClose={onClose} />, document.body);

}

function ProjectCapabilityPanel({
  kind,
  setKind,
  mcpForms,
  snapshot,
  loading,
  error,
  busy,
  onRetry,
  onToggleSkill,
  onPreviewSkills,
  onInstallSkill,
  onRemoveSkill,
  onCreateMcp,
  onEditMcp,
  onToggleMcp,
  onRemoveMcp,
  onReconnectMcp,
  onOpenUserCapabilities,
}: {
  mcpForms: ProjectMcpForms;
  kind: ProjectCapabilityKind;
  setKind: (kind: ProjectCapabilityKind) => void;
  snapshot?: CapabilitySnapshot;
  loading: boolean;
  error?: string;
  busy?: string;
  onRetry: () => void;
  onToggleSkill: (skill: SkillCapability, enabled: boolean) => Promise<void>;
  onPreviewSkills: (sourcePath: string) => Promise<SkillImportCandidate[]>;
  onInstallSkill: (input: SkillImportInput) => Promise<void>;
  onCreateMcp: (input: McpEditInput) => Promise<void>;
  onEditMcp: (server: McpServerCapability, input: McpEditInput) => Promise<void>;
  onToggleMcp: (server: McpServerCapability, enabled: boolean) => Promise<void>;
  onRemoveMcp: (server: McpServerCapability) => Promise<void>;
  onReconnectMcp: (server: McpServerCapability) => Promise<void>;
  onOpenUserCapabilities: () => void;
  onRemoveSkill: (skill: SkillCapability) => Promise<void>;
}) {
  const [expandedMcp, setExpandedMcp] = useState<string>();
  const [editingMcp, setEditingMcp] = useState<McpServerCapability>();
  const [importingMcp, setImportingMcp] = useState(false);
  const [removingSkill, setRemovingSkill] = useState<string>();
  const [inheritedExpanded, setInheritedExpanded] = useState(false);
  const [dialogKind, setDialogKind] = useState<ProjectCapabilityKind>();
  const [dialogOpener, setDialogOpener] = useState<HTMLButtonElement | null>(null);
  const skills = snapshot?.skills.items ?? [];
  const servers = snapshot?.mcp.servers ?? [];
  const projectSkills = skills.filter((item) => item.source === 'workspace');
  const inheritedSkills = skills.filter((item) => item.source !== 'workspace');
  const projectMcp = servers.filter((item) => item.source === 'workspace');
  const inheritedMcp = servers.filter((item) => item.source !== 'workspace');
  const rows = kind === 'skills' ? projectSkills.length + inheritedSkills.length : projectMcp.length + inheritedMcp.length;
  const capabilityAttention = kind === 'skills'
    ? [
      ...(snapshot?.skills.issues.map(skillIssueSummary) ?? []),
      ...(snapshot?.skills.details.length ? ['部分技能目录暂时无法完整读取'] : []),
    ]
    : [
      ...(snapshot?.mcp.collisions.map((collision) => `${collision.name} 存在同名工具`) ?? []),
      ...servers.filter((server) => server.hasFailure).map((server) => `${server.name}：${mcpFailureReason(server.failureCategory)}`),
    ];

  const renderSkill = (skill: SkillCapability) => (
    <article className={`project-capability-row${skill.enabled ? '' : ' is-disabled'}`} key={`${skill.source}:${skill.id}:${skill.path}`}>
      <span className="project-capability-row__icon"><Wrench size={13} /></span>
      <span className="project-capability-row__copy"><strong>{skill.name}</strong><small>{skill.description}</small></span>
      {skill.editable && skill.removalIdentity && (removingSkill === skill.path ? <>
        <button className="secondary-ghost" disabled={Boolean(busy)} onClick={() => setRemovingSkill(undefined)}>取消</button>
        <button className="danger-ghost" disabled={Boolean(busy)} onClick={() => void onRemoveSkill(skill).then(() => setRemovingSkill(undefined), () => {})}>确认删除</button>
      </> : <button className="danger-ghost" aria-label={`删除 ${skill.name}`} disabled={Boolean(busy)} onClick={() => setRemovingSkill(skill.path)}><Trash2 size={12} /></button>)}
      {skill.editable ? (
        <CapabilitySwitch checked={skill.enabled} disabled={Boolean(busy)} label={`${skill.enabled ? '关闭' : '开启'} ${skill.name}`} onChange={(enabled) => void onToggleSkill(skill, enabled)} />
      ) : (
        <span className="project-capability-row__source">{capabilitySourceLabels[skill.source]}</span>
      )}
    </article>
  );

  const renderMcp = (server: McpServerCapability) => {
    const expanded = expandedMcp === `${server.source}:${server.id}`;
    const rowKey = `${server.source}:${server.id}`;
    const status = server.needsApproval ? '需要确认' : mcpStatusLabels[server.status];
    return (
      <article className={`project-capability-row project-capability-row--mcp${server.enabled ? '' : ' is-disabled'}${expanded ? ' is-expanded' : ''}`} key={rowKey}>
        <div className="project-capability-row__summary">
          <button className="project-capability-row__identity" type="button" onClick={() => setExpandedMcp(expanded ? undefined : rowKey)}>
            <span className="project-capability-row__icon"><Server size={13} /></span>
            <span className="project-capability-row__copy"><strong>{server.name}</strong><small>{status}{server.toolCount ? ` · ${server.toolCount} 个工具` : ''}</small></span>
          </button>
          {server.editable ? (
            <CapabilitySwitch checked={server.enabled} disabled={Boolean(busy)} label={`${server.enabled ? '关闭' : '开启'} ${server.name}`} onChange={(enabled) => void onToggleMcp(server, enabled)} />
          ) : (
            <span className="project-capability-row__source">{capabilitySourceLabels[server.source]}</span>
          )}
          <button className="project-capability-row__expand" type="button" aria-label={expanded ? '收起详情' : '展开详情'} onClick={() => setExpandedMcp(expanded ? undefined : rowKey)}><ChevronDown size={12} /></button>
        </div>
        {expanded && (
          <div className="project-capability-row__detail">
            {server.transport && <p><code>{server.transport.kind === 'stdio' ? '本地命令' : 'HTTP'}</code><span className="project-capability-row__transport">{server.transport.detail}</span></p>}
            {server.availableToSubagents && <small><Bot size={11} /> 子代理也可使用</small>}
            {server.hasFailure && <small><AlertTriangle size={11} /> {mcpFailureReason(server.failureCategory)}</small>}
            {server.instructions && <p>{server.instructions}</p>}
            {server.tools.length > 0 && (
              <ul>{server.tools.map((tool) => <li key={tool.name}><code>{tool.name}</code><span>{tool.description || 'MCP 工具'}</span></li>)}</ul>
            )}
            <footer>
              {server.editable && server.config && <button type="button" disabled={Boolean(busy)} onClick={() => setEditingMcp(server)}>编辑连接</button>}
              {server.effective && server.status !== 'disabled' && <button type="button" disabled={Boolean(busy)} onClick={() => void onReconnectMcp(server)}><RefreshCw size={11} /> 重新连接</button>}
              {server.editable && <button className="is-danger" type="button" disabled={Boolean(busy)} onClick={() => void onRemoveMcp(server)}><Trash2 size={11} /> 移除</button>}
            </footer>
          </div>
        )}
      </article>
    );
  };

  return (
    <div className="project-capability-panel">
      <section className="inspector-section project-capability-overview">
        <div className="section-label"><span>{snapshot?.workspaceKind === 'quick' ? '工作目录能力' : '项目能力'}</span>{loading && <small><LoaderCircle size={10} /> 正在同步</small>}</div>
        <p>这里的修改会应用到同一目录的所有会话。</p>
        {snapshot?.adoption.pending && <div className="project-capability-pending"><Sparkles size={12} /><span><strong>更改已保存</strong><small>会话将在下一次模型请求前的安全时机载入；新连接就绪后可用。</small></span></div>}
        {snapshot?.adoption.attention && !snapshot.adoption.pending && (
          <div className="task-inventory-notice task-inventory-notice--error">
            <AlertTriangle size={15} />
            {snapshot.adoption.attention === 'PROJECT_MCP_ADOPTION_INCOMPLETE' ? (
              <span><strong>部分项目连接未载入</strong><small>这次对话已继续使用上一次可用配置；请检查项目 MCP 后重新切换相关连接。</small></span>
            ) : (
              <span><strong>项目能力配置需要处理</strong><small>这次对话已继续使用上一次可用配置；请检查这个目录中的技能与连接设置。</small></span>
            )}
          </div>
        )}
        <div className="project-capability-toolbar">
          <div className="project-capability-tabs" role="tablist" aria-label="能力类型">
            <button role="tab" aria-selected={kind === 'skills'} className={kind === 'skills' ? 'is-active' : ''} onClick={() => { setKind('skills'); setInheritedExpanded(false); }}>技能 <span>{skills.length}</span></button>
            <button role="tab" aria-selected={kind === 'mcp'} className={kind === 'mcp' ? 'is-active' : ''} onClick={() => { setKind('mcp'); setInheritedExpanded(false); }}>MCP <span>{servers.length}</span></button>
          </div>
          <div className="project-capability-actions" role="group" aria-label="能力操作">
            {kind === 'mcp' && <button className="project-capability-action" type="button" disabled={Boolean(busy)} onClick={() => setImportingMcp(true)}>导入 MCP</button>}
            <button
              className="project-capability-action"
              type="button"
              disabled={Boolean(busy)}
              onClick={(event) => {
                setDialogOpener(event.currentTarget);
                setDialogKind(kind);
              }}
            ><Plus size={12} /> 添加</button>
          </div>
        </div>
      </section>

      {error && <div className="task-inventory-notice task-inventory-notice--error"><AlertTriangle size={15} /><span><strong>没有读完整</strong><small>{error}</small></span><button onClick={onRetry}><RefreshCw size={11} /> 重试</button></div>}
      {!error && capabilityAttention.length > 0 && (
        <div className="task-inventory-notice task-inventory-notice--error">
          <AlertTriangle size={15} />
          <span><strong>有 {capabilityAttention.length} 项需要留意</strong><small>{capabilityAttention.join('；')}</small></span>
        </div>
      )}
      {!error && loading && !snapshot && <div className="task-inventory-notice"><LoaderCircle size={15} /><span><strong>正在读取项目能力</strong><small>整理这个目录中的技能和连接…</small></span></div>}
      {!loading && !error && rows === 0 && <div className="inspector-empty"><Blocks size={18} /><span>这个目录还没有{kind === 'skills' ? '技能' : ' MCP'}</span></div>}

      {snapshot && rows > 0 && (
        <section className="inspector-section project-capability-list">
          {(kind === 'skills' ? projectSkills : projectMcp).length > 0 && (
            <div className="project-capability-group"><header><FolderCog size={11} /><span>从目录中加载</span></header>{kind === 'skills' ? projectSkills.map(renderSkill) : projectMcp.map(renderMcp)}</div>
          )}
          {(kind === 'skills' ? inheritedSkills : inheritedMcp).length > 0 && (
            <div className={`project-capability-group project-capability-group--inherited${inheritedExpanded ? ' is-expanded' : ''}`}>
              <header>
                <button className="project-capability-group__toggle" type="button" aria-expanded={inheritedExpanded} onClick={() => setInheritedExpanded((value) => !value)}>
                  <Blocks size={11} /><span>继承的能力</span><small>{kind === 'skills' ? inheritedSkills.length : inheritedMcp.length}</small><ChevronDown size={11} />
                </button>
                <button className="project-capability-group__manage" type="button" onClick={onOpenUserCapabilities}>管理 <ExternalLink size={10} /></button>
              </header>
              {inheritedExpanded && (kind === 'skills' ? inheritedSkills.map(renderSkill) : inheritedMcp.map(renderMcp))}
            </div>
          )}
        </section>
      )}
      {busy && <div className="project-capability-busy"><LoaderCircle size={12} /> {busy}</div>}
      {dialogKind && snapshot?.credentialScopeKey && <ProjectCapabilityDialog mcpForms={mcpForms} initialKind={dialogKind} credentialScopeKey={snapshot.credentialScopeKey} returnFocusTo={dialogOpener} onClose={() => setDialogKind(undefined)} onPreviewSkills={onPreviewSkills} onInstallSkill={onInstallSkill} onCreateMcp={onCreateMcp} />}
      {editingMcp && snapshot?.credentialScopeKey && createPortal(<McpEditor credentialScopeKey={snapshot.credentialScopeKey} server={{...editingMcp, enabled: editingMcp.configuredEnabled, config: editingMcp.config!, currentIdentity: editingMcp.configIdentity!, transport: editingMcp.transport!} satisfies UserMcpServerCapability} onClose={() => setEditingMcp(undefined)} onTest={mcpForms.test} onAuthorization={(action) => mcpForms.authorize(editingMcp, action)} onSave={async (input) => { await onEditMcp(editingMcp, input); return true; }} />, document.body)}
      {importingMcp && createPortal(<McpImporter onPreview={mcpForms.preview} onImport={mcpForms.import} onClose={() => setImportingMcp(false)} />, document.body)}
    </div>
  );
}

export function InspectorPanel({
  projectMcpForms,
  session,
  isOpen,
  agentTasks,
  loading,
  canControl,
  capabilities,
  capabilityLoading,
  capabilityError,
  capabilityBusy,
  error,
  onRetry,
  taskActivities,
  onLoadTaskActivities,
  taskArtifactOwnerKey,
  onReadToolArtifact,
  onCancelTask,
  backgroundOwnerKey,
  backgroundHostSessionId,
  backgroundControlAdmissionDeadlineMs,
  onLoadBackgroundProcesses,
  onReadBackgroundProcessLog,
  onTerminateBackgroundProcess,
  onQueryControl,
  onRetryCapabilities,
  onToggleProjectSkill,
  onPreviewProjectSkills,
  onInstallProjectSkill,
  onRemoveProjectSkill,
  onCreateProjectMcp,
  onEditProjectMcp,
  onToggleProjectMcp,
  onRemoveProjectMcp,
  onReconnectProjectMcp,
  onOpenUserCapabilities,
  onNotify,
  onClose,
}: InspectorPanelProps) {
  const [view, setView] = useState<InspectorView>('capabilities');
  const [capabilityKind, setCapabilityKind] = useState<ProjectCapabilityKind>('skills');

  return (
    <aside className={`inspector-panel${isOpen ? ' is-open' : ''}`} aria-label="当前会话详情">
      <header className="inspector-tabs">
        <span><strong>当前会话</strong><small>{session.title}</small></span>
        <button className="inspector-close" onClick={onClose} aria-label="关闭检查器"><PanelRightClose size={14} /></button>
        <nav aria-label="详情视图">
          <button aria-label="项目能力" className={view === 'capabilities' ? 'is-active' : ''} onClick={() => setView('capabilities')}><Blocks size={12} /> 能力</button>
          <button className={view === 'tasks' ? 'is-active' : ''} onClick={() => setView('tasks')}><ListChecks size={12} /> 任务</button>
          <button className={view === 'background' ? 'is-active' : ''} onClick={() => setView('background')}><TerminalSquare size={12} /> 后台终端</button>
        </nav>
      </header>
      <div className="inspector-scroll">
        {!session.id ? (
          <div className="inspector-empty">
            {view === 'background' ? <TerminalSquare size={18} /> : view === 'tasks' ? <ListChecks size={18} /> : <Blocks size={18} />}
            <span>创建或选择会话后查看{view === 'background' ? '后台终端' : view === 'tasks' ? '任务' : '项目能力'}</span>
          </div>
        ) : view === 'tasks' ? (
          <TaskWorkspace
            tasks={agentTasks}
            loading={loading}
            error={error}
            canControl={canControl}
            onRetry={onRetry}
            onCancel={onCancelTask}
            onNotify={onNotify}
            activities={taskActivities}
            loadActivities={onLoadTaskActivities}
            loadBackgroundProcesses={onLoadBackgroundProcesses}
            skills={(capabilities?.skills.items ?? []).filter((skill) => skill.enabled && skill.effective)}
            artifactOwnerKey={taskArtifactOwnerKey}
            onReadToolArtifact={onReadToolArtifact}
          />
        ) : view === 'background' ? (
          <BackgroundTerminalPanel
            ownerKey={backgroundOwnerKey}
            sessionId={session.id}
            hostSessionId={backgroundHostSessionId}
            controlAdmissionDeadlineMs={backgroundControlAdmissionDeadlineMs}
            canControl={canControl}
            loadProcesses={onLoadBackgroundProcesses}
            readLog={onReadBackgroundProcessLog}
            terminateProcess={onTerminateBackgroundProcess}
            queryControl={onQueryControl}
          />
        ) : (
          <ProjectCapabilityPanel
            key={session.id}
            kind={capabilityKind}
            setKind={setCapabilityKind}
            mcpForms={projectMcpForms}
            snapshot={capabilities}
            loading={capabilityLoading}
            error={capabilityError}
            busy={capabilityBusy}
            onRetry={onRetryCapabilities}
            onToggleSkill={onToggleProjectSkill}
            onPreviewSkills={onPreviewProjectSkills}
            onInstallSkill={onInstallProjectSkill}
            onRemoveSkill={onRemoveProjectSkill}
            onCreateMcp={onCreateProjectMcp}
            onEditMcp={onEditProjectMcp}
            onToggleMcp={onToggleProjectMcp}
            onRemoveMcp={onRemoveProjectMcp}
            onReconnectMcp={onReconnectProjectMcp}
            onOpenUserCapabilities={onOpenUserCapabilities}
          />
        )}
      </div>
    </aside>
  );
}
