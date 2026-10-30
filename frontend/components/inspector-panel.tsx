import { WorkspaceLoading } from './workspace-loading';
import { McpAuthorizationStatus } from './mcp-authorization';
import {
  AlertTriangle,
  Blocks,
  Bot,
  ChevronRight,
  ExternalLink,
  FolderCog,
  Globe,
  Info,
  GitFork,
  ListChecks,
  LoaderCircle,
  Plus,
  RefreshCw,
  Server,
  TerminalSquare,
  Trash2,
  Wrench,
} from 'lucide-react';
import { useContext, useEffect, useId, useRef, useState } from 'react';
import { createPortal, flushSync } from 'react-dom';
import { McpEditor } from './mcp-editor';
import { McpImporter } from './mcp-importer';
import { SkillImporter } from './skill-importer';
import type {
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
} from '../lib/pulsara-types';
import type { MarkdownNotify } from './markdown-body';
import { inspectorTab, DesktopWorkspace, useDesktopWorkspace, type WorkspaceTab } from './desktop-workspace';
import { FileLinkContext } from './file-link-context';
import { BackgroundTerminalPanel } from './background-terminal-panel';
import { AgentList, CellGraph, CellList, CellSource, transitionWorkspace } from './agent-cell-workspace';
import { AgentConversationPane } from './agent-conversation-pane';
import { prepareSessionTarget, type SessionNavigation } from '../lib/agent-cell-api';
import { AnimatedDisclosure } from './animated-disclosure';
import { usePromptHover } from '../lib/prompt-hover';
import type {
  BackgroundProcessLog,
  BackgroundProcessPage,
  ModelConfigurationSummary,
  CommandReceipt,
  ToolArtifactPage,
  CanonicalPromptImagePart,
  UserControlCommandRef,
  UserControlQueryResult,
  RuntimeAdapter,
} from '../lib/runtime-adapter';

interface InspectorPanelProps {
  adapter: Pick<RuntimeAdapter, 'openAgentReader' | 'createAgentContentReader'>;
  projectMcpForms: ProjectMcpForms;
  session: SessionSummary;
  sessionContentAvailable: boolean;
  isOpen: boolean;
  modelConfigurations?: readonly ModelConfigurationSummary[];
  canControl: boolean;
  capabilities?: CapabilitySnapshot;
  capabilityLoading: boolean;
  capabilityError?: string;
  capabilityBusy?: string;
  onReadVisualization?: import("./inline-visualization").ReadVisualization;
  onReadVisualizationThumbnail?: import("./inline-visualization").ReadVisualizationThumbnail;
  onReadPromptImage: (image: CanonicalPromptImagePart) => Promise<Uint8Array>;
  onReadToolArtifact: (resultEntryId: string, offsetChars: number) => Promise<ToolArtifactPage>;
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
  onOpenUserCapabilities: (kind?: ProjectCapabilityKind) => void;
  onShowProjectScopeNotice: (kind: ProjectCapabilityKind) => void;
  onPickDirectory: () => Promise<string | null>;
  onOpenHomeSettings: () => void;
  onNotify: MarkdownNotify;
  onClose: () => void;
}

interface ProjectMcpForms {
  authorizationKey?: (server: McpServerCapability) => string;
  preview: (input: McpImportSource) => Promise<McpImportPreview[]>;
  import: (input: McpImportSelection) => Promise<boolean>;
  test: (input: McpEditInput) => Promise<McpConnectionTestResult>;
  authorize: (server: McpServerCapability, action: 'login' | 'status' | 'cancel' | 'logout') => Promise<void>;
}

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

function SkillPrecedenceInfo({ onOpenHomeSettings }: { onOpenHomeSettings: () => void }) {
  const { trigger, details, position, show, hide, keep, dismiss } = usePromptHover<HTMLButtonElement, HTMLDivElement>(280);
  const tooltipId = useId();
  return <>
    <button ref={trigger} type="button" className="project-capability-info" aria-label="技能覆盖规则"
      aria-describedby={position ? tooltipId : undefined}
      onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide} onClick={show}>
      <Info size={13} aria-hidden="true" />
    </button>
    {position && createPortal(<div ref={details} id={tooltipId} className="project-capability-tooltip" role="tooltip"
      style={{ left: position.left, top: position.top, translate: position.above ? '0 -100%' : undefined }}
      onMouseEnter={keep} onMouseLeave={hide} onFocus={keep} onBlur={hide}>
      <strong>同名技能</strong>
      <p>优先使用靠前的版本，其余版本会保留。</p>
      <ol>
        <li>当前目录下的 .pulsara/skills</li><li>当前目录下的 .agents/skills</li><li><a href="#pulsara-home" onClick={(event) => { event.preventDefault(); dismiss(); onOpenHomeSettings(); }}>用户级 Pulsara 目录</a>下的 skills</li><li>家目录下的 .agents/skills</li>
        <li>当前目录的插件提供的技能</li><li>用户级插件提供的技能</li><li>Pulsara 自带的技能</li>
      </ol>
    </div>, document.body)}
  </>;
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
  onNotify,
  initialKind,
  onPickDirectory,
  returnFocusTo,
  onClose,
  onPreviewSkills,
  onInstallSkill,
  onCreateMcp,
  credentialScopeKey,
  mcpForms,
}: {
  onNotify: MarkdownNotify;
  mcpForms: ProjectMcpForms;
  initialKind: ProjectCapabilityKind;
  onPickDirectory: () => Promise<string | null>;
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

  if (initialKind === 'mcp') return createPortal(<McpEditor credentialScopeKey={credentialScopeKey} onClose={onClose} onTest={mcpForms.test} onNotify={onNotify} onSave={async (input) => { await onCreateMcp(input); return true; }} />, document.body);
  return createPortal(<SkillImporter onPickDirectory={onPickDirectory} scopeLabel="应用到这个目录的所有会话" onPreview={onPreviewSkills} onInstall={async (input) => {await onInstallSkill(input); return true;}} onClose={onClose} />, document.body);

}

function ProjectCapabilityPanel({
  onNotify,
  onOpenMcp,
  selectedMcpId,
  canControl,
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
  onShowProjectScopeNotice,
  onPickDirectory,
  onOpenHomeSettings,
}: {
  onNotify: MarkdownNotify;
  onOpenMcp: (server: McpServerCapability) => void;
  selectedMcpId?: string;
  canControl: boolean;
  mcpForms: ProjectMcpForms;
  kind: ProjectCapabilityKind;
  setKind: (kind: ProjectCapabilityKind) => void;
  snapshot?: CapabilitySnapshot;
  loading?: boolean;
  error?: string;
  onRetry: () => void;
  busy?: string;
  onToggleSkill: (skill: SkillCapability, enabled: boolean) => Promise<void>;
  onPreviewSkills: (sourcePath: string) => Promise<SkillImportCandidate[]>;
  onInstallSkill: (input: SkillImportInput) => Promise<void>;
  onCreateMcp: (input: McpEditInput) => Promise<void>;
  onEditMcp: (server: McpServerCapability, input: McpEditInput) => Promise<void>;
  onToggleMcp: (server: McpServerCapability, enabled: boolean) => Promise<void>;
  onRemoveMcp: (server: McpServerCapability) => Promise<void>;
  onReconnectMcp: (server: McpServerCapability) => Promise<void>;
  onOpenUserCapabilities: (kind?: ProjectCapabilityKind) => void;
  onShowProjectScopeNotice: (kind: ProjectCapabilityKind) => void;
  onPickDirectory: () => Promise<string | null>;
  onOpenHomeSettings: () => void;
  onRemoveSkill: (skill: SkillCapability) => Promise<void>;
}) {
  const fileLink = useContext(FileLinkContext);
  const [editingMcp, setEditingMcp] = useState<McpServerCapability>();
  const [importingMcp, setImportingMcp] = useState(false);
  const [removingSkill, setRemovingSkill] = useState<string>();
  const [inheritedExpanded, setInheritedExpanded] = useState(false);
  const [dialogKind, setDialogKind] = useState<ProjectCapabilityKind>();
  const [dialogOpener, setDialogOpener] = useState<HTMLButtonElement | null>(null);
  const [previousCanControl, setPreviousCanControl] = useState(canControl);
  if (previousCanControl !== canControl) {
    setPreviousCanControl(canControl);
    if (!canControl) {
      setEditingMcp(undefined); setImportingMcp(false);
      setRemovingSkill(undefined); setDialogKind(undefined);
    }
  }
  const skills = snapshot?.skills.items ?? [];
  const servers = (snapshot?.mcp.servers ?? []).filter(server => !selectedMcpId || server.id === selectedMcpId);
  const projectSkills = skills.filter((item) => item.source === 'workspace');
  const inheritedSkills = skills.filter((item) => item.source !== 'workspace');
  const projectMcp = servers.filter((item) => item.source === 'workspace');
  const inheritedMcp = servers.filter((item) => item.source !== 'workspace');
  const rows = kind === 'skills' ? projectSkills.length + inheritedSkills.length : projectMcp.length + inheritedMcp.length;
  const capabilityAttention = kind === 'skills'
    ? [
      ...(snapshot?.skills.issues.filter((issue) => issue.kind !== 'shadowed').map(skillIssueSummary) ?? []),
      ...(snapshot?.skills.details.length ? ['部分技能目录暂时无法完整读取'] : []),
    ]
    : [
      ...(snapshot?.mcp.collisions.map((collision) => `${collision.name} 存在同名工具`) ?? []),
      ...servers.filter((server) => server.hasFailure).map((server) => `${server.name}：${mcpFailureReason(server.failureCategory)}`),
    ];

  const openMcpDetail = (server: McpServerCapability, source: HTMLButtonElement) => {
    if (!document.startViewTransition) {
      onOpenMcp(server);
      return;
    }
    const summary = source.closest<HTMLElement>('.project-capability-row__summary')!;
    const card = source.closest<HTMLElement>('.project-capability-row')!;
    const overview = source.closest<HTMLElement>('.project-capability-panel')!;
    // Only a user click has a source card. Direct tab opens need no shared-element transition.
    summary.style.viewTransitionName = 'mcp-connection';
    card.style.viewTransitionName = 'mcp-card';
    overview.style.viewTransitionName = 'mcp-overview';
    const transition = document.startViewTransition({
      types: ['mcp-detail'],
      update: () => flushSync(() => onOpenMcp(server)),
    });
    void transition.ready.catch(() => undefined);
    void transition.finished.finally(() => {
      summary.style.removeProperty('view-transition-name');
      card.style.removeProperty('view-transition-name');
      overview.style.removeProperty('view-transition-name');
    }).catch(() => undefined);
  };

  const renderSkill = (skill: SkillCapability) => (
    <article className={`project-capability-row${skill.enabled ? '' : ' is-disabled'}`} key={`${skill.source}:${skill.id}:${skill.path}`}>
      <span className="project-capability-row__icon"><Wrench size={13} /></span>
      <button type="button" className="project-capability-row__copy skill-read-button" onClick={event => fileLink?.open(skill.path, event.currentTarget)} title={skill.path}><strong>{skill.name}</strong><small>{skill.description}</small></button>
      {skill.editable && skill.removalIdentity && (removingSkill === skill.path ? <>
        <button className="secondary-ghost" disabled={!canControl || Boolean(busy)} onClick={() => setRemovingSkill(undefined)}>取消</button>
        <button className="danger-ghost" disabled={!canControl || Boolean(busy)} onClick={() => void onRemoveSkill(skill).then(() => setRemovingSkill(undefined), () => {})}>确认删除</button>
      </> : <button className="danger-ghost" aria-label={`删除 ${skill.name}`} disabled={!canControl || Boolean(busy)} onClick={() => setRemovingSkill(skill.path)}><Trash2 size={12} /></button>)}
      {skill.editable ? (
        <CapabilitySwitch checked={skill.enabled} disabled={!canControl || Boolean(busy)} label={`${skill.enabled ? '关闭' : '开启'} ${skill.name}`} onChange={(enabled) => void onToggleSkill(skill, enabled)} />
      ) : (
        <span className="project-capability-row__source">{capabilitySourceLabels[skill.source]}</span>
      )}
    </article>
  );

  const renderMcp = (server: McpServerCapability) => {
    const rowKey = `${server.source}:${server.id}`;
    const status = server.needsApproval ? '需要确认' : mcpStatusLabels[server.status];
    return (
      <article className={`project-capability-row project-capability-row--mcp${server.enabled ? '' : ' is-disabled'}${selectedMcpId ? ' project-mcp-detail' : ''}`} key={rowKey}>
        <div className="project-capability-row__summary">
          {selectedMcpId ? <div className="project-capability-row__identity">
            <span className="project-capability-row__icon"><Server size={13} /></span>
            <span className="project-capability-row__copy"><strong>{server.name}</strong><small>{status}{server.toolCount ? ` · ${server.toolCount} 个工具` : ''}</small></span>
          </div> : <button className="project-capability-row__identity" type="button" onClick={event => openMcpDetail(server, event.currentTarget)}>
            <span className="project-capability-row__icon"><Server size={13} /></span>
            <span className="project-capability-row__copy"><strong>{server.name}</strong><small>{status}{server.toolCount ? ` · ${server.toolCount} 个工具` : ''}</small></span>
          </button>}
          {server.editable ? (
            <CapabilitySwitch checked={server.enabled} disabled={!canControl || Boolean(busy)} label={`${server.enabled ? '关闭' : '开启'} ${server.name}`} onChange={(enabled) => void onToggleMcp(server, enabled)} />
          ) : (
            <span className="project-capability-row__source">{capabilitySourceLabels[server.source]}</span>
          )}
        </div>
        {selectedMcpId && <div className="project-capability-row__detail">
            {server.transport && <p><code>{server.transport.kind === 'stdio' ? '本地命令' : 'HTTP'}</code><span className="project-capability-row__transport">{server.transport.detail}</span></p>}
            {server.availableToSubagents && <small><Bot size={11} /> 子代理也可使用</small>}
            {server.hasFailure && <small><AlertTriangle size={11} /> {mcpFailureReason(server.failureCategory)}</small>}
            {server.instructions && <p>{server.instructions}</p>}
            {server.tools.length > 0 && (
              <ul>{server.tools.map((tool) => <li key={tool.name}><code>{tool.name}</code><span>{tool.description || 'MCP 工具'}</span></li>)}</ul>
            )}
            <footer>
              <McpAuthorizationStatus owner={mcpForms.authorizationKey?.(server)} />
              {server.editable && server.config && <button type="button" disabled={!canControl || Boolean(busy)} onClick={() => setEditingMcp(server)}>编辑连接</button>}
              {server.effective && server.status !== 'disabled' && <button type="button" disabled={!canControl || Boolean(busy)} onClick={() => void onReconnectMcp(server)}><RefreshCw size={11} /> 重新连接</button>}
              {server.editable && <button className="is-danger" type="button" disabled={!canControl || Boolean(busy)} onClick={() => void onRemoveMcp(server)}><Trash2 size={11} /> 移除</button>}
            </footer>
        </div>}
      </article>
    );
  };

  return (
    <div className="project-capability-panel" aria-busy={loading}>
      {!selectedMcpId && <section className="inspector-section project-capability-overview">
        <div className="section-label"><span>{snapshot?.workspaceKind === 'quick' ? '工作目录能力' : '项目能力'}</span>{kind === 'skills' && <SkillPrecedenceInfo onOpenHomeSettings={onOpenHomeSettings} />}</div>
        <p>这里的修改会应用到同一目录的所有会话。</p>
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
          <div className="project-capability-tabs sliding-tabs" data-active={kind} role="tablist" aria-label="能力类型">
            <button role="tab" aria-selected={kind === 'skills'} className={kind === 'skills' ? 'is-active' : ''} onClick={() => { setKind('skills'); setInheritedExpanded(false); }}>技能 <span>{skills.length}</span></button>
            <button role="tab" aria-selected={kind === 'mcp'} className={kind === 'mcp' ? 'is-active' : ''} onClick={() => { setKind('mcp'); setInheritedExpanded(false); }}>MCP <span>{servers.length}</span></button>
          </div>
          <div className="project-capability-actions" role="group" aria-label="能力操作">
            {kind === 'mcp' && <button className="project-capability-action" type="button" disabled={!canControl || Boolean(busy)} onClick={() => { setImportingMcp(true); onShowProjectScopeNotice('mcp'); }}>导入 MCP</button>}
            <button
              className="project-capability-action"
              type="button"
              disabled={!canControl || Boolean(busy)}
              onClick={(event) => {
                setDialogOpener(event.currentTarget);
                setDialogKind(kind);
                onShowProjectScopeNotice(kind);
              }}
            ><Plus size={12} /> 添加</button>
          </div>
        </div>
      </section>}

      {error && <div className="task-inventory-notice task-inventory-notice--error"><AlertTriangle size={15} /><span><strong>没有读完整</strong><small>{error}</small></span><button onClick={onRetry}><RefreshCw size={11} /> 重试</button></div>}
      {!selectedMcpId && !error && capabilityAttention.length > 0 && (
        <div className="task-inventory-notice task-inventory-notice--error">
          <AlertTriangle size={15} />
          <span><strong>有 {capabilityAttention.length} 项需要留意</strong><small>{capabilityAttention.join('；')}</small></span>
        </div>
      )}
      {!error && loading && !snapshot && <WorkspaceLoading/>}
      {!loading && !error && selectedMcpId && !servers.length && <div className="inspector-empty" role="status">此 MCP 连接已不可用。标签保留原连接身份。</div>}
      {!selectedMcpId && (!loading || snapshot) && !error && rows === 0 && (kind !== 'skills' || snapshot?.skills.status !== 'attention') && <div className="inspector-empty"><Blocks size={18} /><span>这个目录还没有{kind === 'skills' ? '技能' : ' MCP'}</span></div>}

      {selectedMcpId && servers.length > 0 && <section className="inspector-section project-mcp-detail-panel">{servers.map(renderMcp)}</section>}
      {!selectedMcpId && snapshot && rows > 0 && (
        <section className="inspector-section project-capability-list">
          {(kind === 'skills' ? projectSkills : projectMcp).length > 0 && (
            <div className="project-capability-group"><header><FolderCog size={11} /><span>从目录中加载</span></header>{kind === 'skills' ? projectSkills.map(renderSkill) : projectMcp.map(renderMcp)}</div>
          )}
          {(kind === 'skills' ? inheritedSkills : inheritedMcp).length > 0 && (
            <div className={`project-capability-group project-capability-group--inherited${inheritedExpanded ? ' is-expanded' : ''}`}>
              <header>
                <button className="project-capability-group__toggle" type="button" aria-expanded={inheritedExpanded} onClick={() => setInheritedExpanded((value) => !value)}>
                  <Blocks size={11} /><span>继承的能力</span><small>{kind === 'skills' ? inheritedSkills.length : inheritedMcp.length}</small><ChevronRight size={11} />
                </button>
                <button className="project-capability-group__manage" type="button" onClick={() => onOpenUserCapabilities(kind)}>管理 <ExternalLink size={10} /></button>
              </header>
              <AnimatedDisclosure
                open={inheritedExpanded}
                className="project-capability-group__disclosure"
                contentClassName="project-capability-group__items"
              >
                {kind === 'skills' ? inheritedSkills.map(renderSkill) : inheritedMcp.map(renderMcp)}
              </AnimatedDisclosure>
            </div>
          )}
        </section>
      )}
      {busy && <div className="project-capability-busy"><LoaderCircle size={12} /> {busy}</div>}
      {canControl && dialogKind && snapshot?.credentialScopeKey && <ProjectCapabilityDialog onPickDirectory={onPickDirectory} onNotify={onNotify} mcpForms={mcpForms} initialKind={dialogKind} credentialScopeKey={snapshot.credentialScopeKey} returnFocusTo={dialogOpener} onClose={() => setDialogKind(undefined)} onPreviewSkills={onPreviewSkills} onInstallSkill={onInstallSkill} onCreateMcp={onCreateMcp} />}
      {canControl && editingMcp && snapshot?.credentialScopeKey && createPortal(<McpEditor authorizationKey={mcpForms.authorizationKey?.(editingMcp)} credentialScopeKey={snapshot.credentialScopeKey} server={{...editingMcp, enabled: editingMcp.configuredEnabled, config: editingMcp.config!, currentIdentity: editingMcp.configIdentity!, transport: editingMcp.transport!} satisfies UserMcpServerCapability} onClose={() => setEditingMcp(undefined)} onTest={mcpForms.test} onNotify={onNotify} onAuthorization={(action) => mcpForms.authorize(editingMcp, action)} onSave={async (input) => { await onEditMcp(editingMcp, input); return true; }} />, document.body)}
      {canControl && importingMcp && createPortal(<McpImporter onPreview={mcpForms.preview} onImport={mcpForms.import} onClose={() => setImportingMcp(false)} />, document.body)}
    </div>
  );
}

export function InspectorPanel({
  adapter,
  projectMcpForms,
  session,
  sessionContentAvailable,
  isOpen,
  modelConfigurations,
  canControl,
  capabilities,
  capabilityLoading,
  capabilityError,
  capabilityBusy,
  onReadToolArtifact,
  onReadPromptImage,
  onReadVisualization,
  onReadVisualizationThumbnail,
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
  onShowProjectScopeNotice,
  onPickDirectory,
  onOpenHomeSettings,
  onNotify,
  onClose,
}: InspectorPanelProps) {
  const workspace = useDesktopWorkspace();
  const [capabilityKind, setCapabilityKind] = useState<ProjectCapabilityKind>('skills');
  const workLocationRevision = useRef(0);
  const currentOwner = useRef({ root: session.id, host: backgroundHostSessionId, visible: workspace?.visible, available: sessionContentAvailable });
  currentOwner.current = { root: session.id, host: backgroundHostSessionId, visible: workspace?.visible, available: sessionContentAvailable };

  const open = (kind: Exclude<WorkspaceTab['kind'], 'browser'>, title: string, entityId?: string, hostSessionId?: string, workId?: string) => sessionContentAvailable && workspace?.open({
    ...inspectorTab(session.id, kind, title, entityId, hostSessionId),
    ...(workId ? { workLocation: { workId, requestId: ++workLocationRevision.current } } : {}),
  });
  const openPrepared = async (intent: SessionNavigation, source?: HTMLElement, workId?: string, originHost?: string) => {
    if (!workspace || !sessionContentAvailable) return;
    workspace.interact();
    const version = workspace.interactionVersion();
    const valid = () => currentOwner.current.available && currentOwner.current.visible === true && currentOwner.current.root === intent.rootSessionId
      && (!('hostSessionId' in intent) || currentOwner.current.host === intent.hostSessionId)
      && (!originHost || currentOwner.current.host === originHost)
      && workspace.interactionVersion() === version;
    let prepared: Awaited<ReturnType<typeof prepareSessionTarget>>;
    try {
      prepared = await prepareSessionTarget(intent, valid);
      if (!valid() || !prepared) return;
      const tab = prepared.adopt();
      if (!tab || !valid()) return;
      if (workId && tab.kind === 'agent') tab.workLocation = { workId, requestId: ++workLocationRevision.current };
      if (source?.isConnected) transitionWorkspace(source, () => workspace.open(tab));
      else workspace.open(tab);
    } catch (error) {
      if (valid()) onNotify('无法打开内容', error instanceof Error ? error.message : '暂时无法读取。', 'warning');
    } finally { prepared?.release(); }
  };
  const renderTab = (tab: WorkspaceTab) => {
    if (!session.id) return <div className="workspace-empty" role="status"><strong>创建或选择会话后查看内容</strong></div>;
    if (tab.kind === 'subagents') return <AgentList root={session.id} onOpen={(agent, source) => void openPrepared({ kind: 'agent', rootSessionId: session.id, agentSessionId: agent.agent_id }, source)} />;
    if (tab.kind === 'cells-unavailable') return <div className="inspector-empty" role="status">暂无可用运行实例</div>;
    if (tab.kind === 'cells') {
      if (backgroundHostSessionId && tab.hostSessionId !== backgroundHostSessionId) return <div className="inspector-empty" role="status">对象已不可用</div>;
      return <CellList root={session.id} host={tab.hostSessionId} onOpen={(cell, host, source) => void openPrepared({ kind: 'cell', rootSessionId: session.id, hostSessionId: host, cellId: cell.target.cell_id }, source)} />;
    }
    if (tab.kind === 'agent') {
      tab.view ??= { top: 0, left: 0 };
      tab.view.agentReadingState ??= { choices: new Map() };
      return <AgentConversationPane key={`${session.id}:${tab.entityId}`} rootSessionId={session.id} agentSessionId={tab.entityId} agentName={tab.title} adapter={adapter} onNotify={onNotify} readingState={tab.view.agentReadingState} workLocation={tab.workLocation} />;
    }
    if (tab.kind === 'cell' || tab.kind === 'cell-graph') {
      if (backgroundHostSessionId && tab.hostSessionId !== backgroundHostSessionId) return <div className="inspector-empty" role="status">对象已不可用</div>;
      if (tab.kind === 'cell') return <CellSource root={session.id} host={tab.hostSessionId} cell={tab.entityId} onGraph={source => void openPrepared({ kind: 'cell-graph', rootSessionId: session.id, hostSessionId: tab.hostSessionId, cellId: tab.entityId }, source)} />;
      tab.view ??= { top: 0, left: 0 };
      tab.view.graph ??= { top: 0, left: 0 };
      const root = session.id; const host = tab.hostSessionId;
      const openAgent = (agentId: string, workId?: string) => { void openPrepared({ kind: 'agent', rootSessionId: root, agentSessionId: agentId }, undefined, workId, host); };
      return <CellGraph root={root} host={host} cell={tab.entityId} viewport={tab.view.graph} onAgent={agentId => openAgent(agentId)} onWork={work => openAgent(work.agent_id, work.work_id)} />;
    }
    if ((tab.kind === 'process' || tab.kind === 'mcp') && tab.hostSessionId !== backgroundHostSessionId) return <div className="inspector-empty" role="status">原运行实例已不可用。此标签不会改绑到新的实例。</div>;
    if (tab.kind === 'background' || tab.kind === 'process') return <BackgroundTerminalPanel processId={tab.kind === "process" ? tab.entityId : undefined} onOpenProcess={process => open("process", process.command, process.processId, backgroundHostSessionId)}
            ownerKey={backgroundOwnerKey}
            sessionId={session.id}
            hostSessionId={backgroundHostSessionId}
            controlAdmissionDeadlineMs={backgroundControlAdmissionDeadlineMs}
            canControl={canControl}
            loadProcesses={onLoadBackgroundProcesses}
            readLog={onReadBackgroundProcessLog}
            terminateProcess={onTerminateBackgroundProcess}
            queryControl={onQueryControl}
          />;
    if (tab.kind === 'file' || tab.kind === 'visualization' || tab.kind === 'browser') return <div className="inspector-empty" role="status">对象已不可用</div>;
    if (tab.kind !== 'capabilities' && tab.kind !== 'mcp') { const unhandled: never = tab; return unhandled; }
    return <ProjectCapabilityPanel selectedMcpId={tab.kind === "mcp" ? tab.entityId : undefined} onOpenMcp={server => open("mcp", server.name, server.id, backgroundHostSessionId)}
            canControl={canControl}
            onNotify={onNotify}
            key={session.id}
            kind={tab.kind === "mcp" ? "mcp" : capabilityKind}
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
            onShowProjectScopeNotice={onShowProjectScopeNotice}
            onPickDirectory={onPickDirectory}
            onOpenHomeSettings={onOpenHomeSettings}
          />;
  };
  return <DesktopWorkspace isOpen={isOpen} onClose={onClose} renderTab={renderTab} sessionContentAvailable={sessionContentAvailable} toolbar={<nav aria-label="工作区入口">
    <button type="button" aria-label="能力" title="能力" onClick={() => open('capabilities', '能力')}><Blocks size={13} />能力</button>

    <button type="button" title="进程" onClick={() => open('background', '进程')}><TerminalSquare size={13} />进程</button>
    <button type="button" title="子代理" onClick={() => open('subagents', '子代理')}><Bot size={13} />子代理</button>
    <button type="button" title="cell" onClick={() => session.id && backgroundHostSessionId ? open('cells', 'cell', undefined, backgroundHostSessionId) : open('cells-unavailable', 'cell')}><GitFork size={13} />cell</button>
    <button type="button" title="网页" aria-label="网页" onClick={() => workspace?.browserTabs.open()}><Globe size={13} />网页</button>
  </nav>} />;
}
