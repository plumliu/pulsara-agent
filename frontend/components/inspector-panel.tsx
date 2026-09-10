'use client';

import {
  AlertTriangle,
  Ban,
  Blocks,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Clock3,
  ExternalLink,
  FolderCog,
  GitFork,
  Layers3,
  ListChecks,
  LocateFixed,
  LoaderCircle,
  PanelRightClose,
  Plus,
  RefreshCw,
  Server,
  Sparkles,
  Trash2,
  Wrench,
} from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
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
  PermissionMode,
  SessionSummary,
  SkillCatalogIssue,
  SkillCapability,
  SkillImportInput,
  SkillImportCandidate,
  TaskStatus,
  TodoRun,
} from '../lib/pulsara-types';
import { permissionLabels } from '../lib/pulsara-types';
import { MarkdownBody, type MarkdownNotify } from './markdown-body';

interface InspectorPanelProps {
  projectMcpForms: ProjectMcpForms;
  session: SessionSummary;
  isOpen: boolean;
  agentTasks: AgentTask[];
  todo?: TodoRun;
  loading: boolean;
  canControl: boolean;
  isRunning: boolean;
  permission: PermissionMode;
  capabilities?: CapabilitySnapshot;
  capabilityLoading: boolean;
  capabilityError?: string;
  capabilityBusy?: string;
  error?: string;
  onRetry: () => void;
  onLocate: (taskId: string) => void;
  onAcceptCompletion: (task: AgentTask) => void;
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

type TaskFilter = 'all' | 'active' | 'attention' | 'settled';
type InspectorView = 'tasks' | 'capabilities';
type ProjectCapabilityKind = 'skills' | 'mcp';

const statusLabels: Record<TaskStatus, string> = {
  pending: '待开始',
  running: '进行中',
  waiting: '等待依赖',
  completed: '已完成',
  cancelled: '已取消',
  failed: '失败',
  interrupted: '已中断',
  blocked: '依赖未完成',
};

function isActive(status: TaskStatus): boolean {
  return status === 'pending' || status === 'running' || status === 'waiting';
}

function isTerminal(status: TaskStatus): boolean {
  return !isActive(status);
}

function needsAttention(status: TaskStatus): boolean {
  return status === 'failed' || status === 'interrupted' || status === 'blocked';
}

function taskStatusIcon(status: TaskStatus) {
  if (status === 'running') return <LoaderCircle size={11} />;
  if (status === 'completed') return <Check size={11} />;
  if (status === 'failed' || status === 'blocked') return <AlertTriangle size={11} />;
  if (status === 'interrupted' || status === 'cancelled') return <Ban size={11} />;
  if (status === 'waiting') return <Clock3 size={11} />;
  return <CircleDashed size={11} />;
}

function taskExplanation(task: AgentTask): string | undefined {
  if (task.status === 'waiting') return '正在等待前置任务完成。';
  if (task.status === 'pending') return '已经创建，正在等待可用的执行位置。';
  if (task.status === 'blocked') return '前置任务未能完成，因此这项工作没有开始。';
  if (task.status === 'interrupted') return '本次执行已中断；Pulsara 不会在进程重启后自动续跑。';
  if (task.status === 'failed') return '这项工作没有成功完成，主任务仍可继续处理其他结果。';
  if (task.status === 'cancelled') return '这项工作已被停止。';
  return undefined;
}

function formatTaskTime(value?: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return undefined;
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function diagnosticText(value: Record<string, unknown>): string | undefined {
  for (const key of ['public_message', 'message', 'detail', 'summary', 'title']) {
    const candidate = value[key];
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
  }
  return undefined;
}

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
          {kind === 'mcp' && <button type="button" disabled={Boolean(busy)} onClick={() => setImportingMcp(true)}>导入 MCP</button>}
          <div role="tablist" aria-label="能力类型">
            <button role="tab" aria-selected={kind === 'skills'} className={kind === 'skills' ? 'is-active' : ''} onClick={() => { setKind('skills'); setInheritedExpanded(false); }}>技能 <span>{projectSkills.length}</span></button>
            <button role="tab" aria-selected={kind === 'mcp'} className={kind === 'mcp' ? 'is-active' : ''} onClick={() => { setKind('mcp'); setInheritedExpanded(false); }}>MCP <span>{projectMcp.length}</span></button>
          </div>
          <button
            className="project-capability-add"
            type="button"
            disabled={Boolean(busy)}
            onClick={(event) => {
              setDialogOpener(event.currentTarget);
              setDialogKind(kind);
            }}
          ><Plus size={12} /> 添加</button>
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

function TaskCard({
  task,
  canControl,
  isRunning,
  permission,
  onLocate,
  onAcceptCompletion,
  onNotify,
}: {
  task: AgentTask;
  canControl: boolean;
  isRunning: boolean;
  permission: PermissionMode;
  onLocate: (taskId: string) => void;
  onAcceptCompletion: (task: AgentTask) => void;
  onNotify: MarkdownNotify;
}) {
  const [expanded, setExpanded] = useState(
    task.status === 'running' || task.status === 'waiting' || needsAttention(task.status),
  );
  const diagnostics = (task.result?.diagnostics ?? [])
    .map(diagnosticText)
    .filter((item): item is string => Boolean(item));
  const explanation = taskExplanation(task);
  const acceptedAt = formatTaskTime(task.acceptedAt);
  const terminalAt = formatTaskTime(task.terminalAt);
  const failed = task.status !== 'completed';
  const terminal = isTerminal(task.status);
  const deliveryLabel = task.completionDelivered
    ? failed ? 'Pulsara 已收到这项问题' : 'Pulsara 已收到结果'
    : terminal
      ? isRunning ? '本轮结束后可继续处理' : failed ? '这项问题尚未用于对话' : '结果尚未用于对话'
      : '结果会自动交给 Pulsara';

  return (
    <article className={`session-task session-task--${task.status}${expanded ? ' is-expanded' : ''}`}>
      <button
        className="session-task__summary"
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span className={`agent-mini-icon color-${task.color}`}><Bot size={12} /></span>
        <span className="session-task__identity">
          <strong>{task.label}</strong>
          <small>{task.progress || task.role}</small>
        </span>
        <span className={`session-task__status session-task__status--${task.status}`}>
          {taskStatusIcon(task.status)}{statusLabels[task.status]}
        </span>
        <ChevronDown size={12} />
      </button>

      {expanded && (
        <div className="session-task__detail">
          <section className="session-task__objective">
            <span>目标</span>
            <div className="session-task__markdown"><MarkdownBody body={task.objective || '未提供单独目标。'} onNotify={onNotify} /></div>
          </section>

          <dl className="session-task__facts">
            <div><dt>分工</dt><dd>{task.role}</dd></div>
            <div><dt>上下文</dt><dd>{task.context?.mode === 'last-n' ? `最近 ${task.context.lastNTurns ?? 0} 轮` : '独立上下文'}</dd></div>
            {acceptedAt && <div><dt>创建于</dt><dd>{acceptedAt}</dd></div>}
            {terminalAt && <div><dt>结束于</dt><dd>{terminalAt}</dd></div>}
          </dl>

          {explanation && <p className="session-task__explanation">{explanation}</p>}
          {task.terminalPublicDetail && (
            <section className="session-task__progress">
              <span><AlertTriangle size={11} /> 发生了什么</span>
              <div className="session-task__markdown"><MarkdownBody body={task.terminalPublicDetail} onNotify={onNotify} /></div>
            </section>
          )}

          {task.dependencies?.length ? (
            <section className="session-task__dependencies">
              <span>依赖</span>
              <ul>
                {task.dependencies.map((dependency) => (
                  <li key={dependency.id}>
                    <i className={`dependency-dot dependency-dot--${dependency.status}`} />
                    <span>{dependency.label || dependency.taskKey || '前置任务'}</span>
                    <small>{statusLabels[dependency.status]}</small>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {task.progress && isActive(task.status) && (
            <section className="session-task__progress">
              <span><Sparkles size={11} /> 最新进展</span>
              <p>{task.progress}</p>
            </section>
          )}

          {task.result && (
            <section className="session-task__result">
              <header>
                <span><CheckCircle2 size={12} /> 任务结果</span>
                {task.completionDelivered && <small><Check size={10} /> 已用于对话</small>}
              </header>
              {task.result.summary && <div className="session-task__markdown"><MarkdownBody body={task.result.summary} onNotify={onNotify} /></div>}
              {task.result.outputPreview && (
                <details>
                  <summary>查看输出摘录</summary>
                  <div className="session-task__markdown"><MarkdownBody body={task.result.outputPreview} onNotify={onNotify} /></div>
                </details>
              )}
              {diagnostics.length ? (
                <details>
                  <summary>查看诊断信息（{diagnostics.length}）</summary>
                  <ul className="session-task__diagnostics">{diagnostics.map((item, index) => <li key={`${task.id}:diagnostic:${index}`}>{item}</li>)}</ul>
                </details>
              ) : null}
              {!diagnostics.length && task.result.diagnostics.length > 0 && (
                <p className="session-task__diagnostic-count">已记录 {task.result.diagnostics.length} 条结构化诊断信息。</p>
              )}
            </section>
          )}

          <footer className="session-task__actions">
            <button type="button" onClick={() => onLocate(task.id)}><LocateFixed size={11} /> 在对话中查看</button>
            <span className="session-task__delivery-state" title={deliveryLabel}>{deliveryLabel}</span>
            {canControl && terminal && !isRunning && !task.completionDelivered && (
              <span className="session-task__continue-action">
                <button
                  className="is-primary"
                  type="button"
                  aria-describedby={`${task.id}-continue-help`}
                  onClick={() => onAcceptCompletion(task)}
                >
                  <Sparkles size={11} /> {failed ? '让 Pulsara 处理这个问题' : '用这份结果继续'}
                </button>
                <span id={`${task.id}-continue-help`} className="session-task__continue-tooltip" role="tooltip">
                  启动新一轮，让 Pulsara 基于这项工作的{failed ? '问题' : '结果'}继续处理；不会重新运行子任务。本轮使用“{permissionLabels[permission]}”权限。
                </span>
              </span>
            )}
          </footer>
        </div>
      )}
    </article>
  );
}

export function InspectorPanel({
  projectMcpForms,
  session,
  isOpen,
  agentTasks,
  todo,
  loading,
  canControl,
  isRunning,
  permission,
  capabilities,
  capabilityLoading,
  capabilityError,
  capabilityBusy,
  error,
  onRetry,
  onLocate,
  onAcceptCompletion,
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
  const [view, setView] = useState<InspectorView>('tasks');
  const [capabilityKind, setCapabilityKind] = useState<ProjectCapabilityKind>('skills');
  const [filter, setFilter] = useState<TaskFilter>('all');
  const completedTodo = (todo?.items ?? []).filter((item) => item.status === 'completed').length;
  const activeCount = agentTasks.filter((task) => isActive(task.status)).length;
  const attentionCount = agentTasks.filter((task) => needsAttention(task.status)).length;
  const visibleTasks = useMemo(() => agentTasks.filter((task) => {
    if (filter === 'active') return isActive(task.status);
    if (filter === 'attention') return needsAttention(task.status);
    if (filter === 'settled') return !isActive(task.status);
    return true;
  }), [agentTasks, filter]);
  const groups = useMemo(() => {
    const grouped = new Map<string, AgentTask[]>();
    for (const task of visibleTasks) {
      const key = task.batchId || task.parentId || task.id;
      grouped.set(key, [...(grouped.get(key) ?? []), task]);
    }
    return [...grouped.entries()];
  }, [visibleTasks]);

  return (
    <aside className={`inspector-panel${isOpen ? ' is-open' : ''}`} aria-label="当前会话详情">
      <header className="inspector-tabs">
        <span><strong>当前会话</strong><small>{session.title}</small></span>
        <button className="inspector-close" onClick={onClose} aria-label="关闭检查器"><PanelRightClose size={14} /></button>
        <nav aria-label="详情视图">
          <button className={view === 'tasks' ? 'is-active' : ''} onClick={() => setView('tasks')}><ListChecks size={12} /> 子任务</button>
          <button aria-label="项目能力" className={view === 'capabilities' ? 'is-active' : ''} onClick={() => setView('capabilities')}><Blocks size={12} /> 能力</button>
        </nav>
      </header>
      <div className="inspector-scroll">
        {view === 'tasks' ? <>
        <section className="inspector-section task-overview">
          <div className="section-label"><span>任务概览</span>{loading && <small><LoaderCircle size={10} /> 正在同步</small>}</div>
          <div className="task-overview__stats">
            <div><strong>{agentTasks.length}</strong><span>全部</span></div>
            <div><strong>{activeCount}</strong><span>进行中</span></div>
            <div className={attentionCount ? 'has-attention' : ''}><strong>{attentionCount}</strong><span>需留意</span></div>
            <div><strong>{todo?.items.length ? `${completedTodo}/${todo.items.length}` : '—'}</strong><span>TODO清单</span></div>
          </div>
        </section>

        <section className="inspector-section session-task-list">
          <div className="section-label"><span>子任务</span><small>{agentTasks.length ? '随会话保存' : ''}</small></div>
          <div className="task-filter" role="tablist" aria-label="筛选子任务">
            {([
              ['all', '全部'],
              ['active', '进行中'],
              ['attention', '需留意'],
              ['settled', '已结束'],
            ] as Array<[TaskFilter, string]>).map(([id, label]) => (
              <button key={id} className={filter === id ? 'is-active' : ''} onClick={() => setFilter(id)} role="tab" aria-selected={filter === id}>{label}</button>
            ))}
          </div>

          {error && (
            <div className="task-inventory-notice task-inventory-notice--error">
              <AlertTriangle size={15} /><span><strong>没有读完整</strong><small>{error}</small></span><button onClick={onRetry}><RefreshCw size={11} /> 重试</button>
            </div>
          )}

          {!error && loading && agentTasks.length === 0 && (
            <div className="task-inventory-notice"><LoaderCircle size={15} /><span><strong>正在恢复子任务</strong><small>从当前会话中读取完整记录…</small></span></div>
          )}

          {!loading && !error && agentTasks.length === 0 && (
            <div className="inspector-empty"><ListChecks size={18} /><span>这个会话还没有子任务</span></div>
          )}

          {!loading && agentTasks.length > 0 && visibleTasks.length === 0 && (
            <div className="inspector-empty"><ListChecks size={18} /><span>没有符合筛选条件的子任务</span></div>
          )}

          <div className="task-groups">
            {groups.map(([groupId, tasks], index) => (
              <section className="task-batch" key={groupId}>
                <header>
                  <span>{tasks[0]?.batchId ? <Layers3 size={11} /> : <GitFork size={11} />}<strong>第 {index + 1} 组</strong></span>
                  <small>{tasks.length} 项 · {tasks.filter((task) => isActive(task.status)).length} 项进行中</small>
                </header>
                <div>{tasks.map((task) => <TaskCard key={task.id} task={task} canControl={canControl} isRunning={isRunning} permission={permission} onLocate={onLocate} onAcceptCompletion={onAcceptCompletion} onNotify={onNotify} />)}</div>
              </section>
            ))}
          </div>
        </section>
        </> : (
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
