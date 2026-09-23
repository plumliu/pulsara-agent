'use client';

import { CapabilityInteractionEditor } from './capability-interaction-editor';

import {
  ArrowDown,
  BookOpenText,
  Bot,
  BrainCircuit,
  Braces,
  Check,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  CircleStop,
  Clock3,
  Copy,
  CornerDownRight,
  FileDiff,
  FileText,
  FileSearch,
  GitFork,
  Eye,
  ListTodo,
  Menu,
  LoaderCircle,
  MessageSquarePlus,
  MoreHorizontal,
  PanelRight,
  Play,
  Pencil,
  Trash2,
  RotateCcw,
  Send,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  TerminalSquare,
  TriangleAlert,
  UserRound,
  WandSparkles,
  Zap,
} from 'lucide-react';
import {
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import type {
  ContextCompactionBoundary,
  ProtocolCanonicalControl,
  ModelCallBindingPayload,
  ModelConfigurationSummary,
  ReasoningSelectionPayload,
  RuntimeInteractionContent,
  RuntimeInteractionResolution,
  RuntimeInteractionSummary,
  QueuedPrompt,
  QueuedPromptAction,
  LocalPromptSubmission,
  ToolArtifactPage,
  CanonicalPromptImagePart,
  EditablePromptContent,
} from '../lib/runtime-adapter';
import type { Message, PermissionMode, ReasoningBlock, RuntimeStatus, SessionSummary, SkillCapability, SubagentRun, TodoRun, ToolTrace, VisualizationOccurrence, Workspace } from '../lib/pulsara-types';
import { permissionLabels, permissionModeOrder } from '../lib/pulsara-types';
import { MarkdownBody, MarkdownInline, type MarkdownNotify } from './markdown-body';
import { PromptComposer } from './prompt-composer';
import { PromptContentView } from './prompt-content-view';
import { WelcomeTypewriter } from './welcome-typewriter';
import { AnimatedDisclosure } from './animated-disclosure';
import { builtinToolSummary } from '../lib/builtin-tool-summary';
import { ToolResultDisplayContext } from '../lib/tool-result-display';
import { PromptDraftStore } from '../lib/prompt-draft';
import { promptContentTextProjection } from '../lib/prompt-content';
import {
  usableVisualizationRootRect,
  visualizationFrameMeasurementScript,
  visualizationLayoutMessageType,
  type VisualizationRootRect,
} from '../lib/visualization-frame';

interface WorkbenchViewProps {
  focusMemoryEntry?: { sessionId: string; entryId: string };
  workspace: Workspace;
  session: SessionSummary;
  messages: Message[];
  contextCompaction?: ContextCompactionBoundary;
  initialContextBase?: ProtocolCanonicalControl['initial_context_base'];
  onFork: (entryId: string) => Promise<void>;
  todo?: TodoRun;
  activePlanMode: boolean;
  isRunning: boolean;
  inspectorOpen: boolean;
  queuedCount: number;
  queuedPrompts: QueuedPrompt[];
  localSubmissions: LocalPromptSubmission[];
  queueActions: QueuedPromptAction[];
  onQueueAction: (item: QueuedPrompt, kind: QueuedPromptAction['kind']) => Promise<void>;
  onQueueActionHandled: (commandId: string) => void;
  runtimeStatus: RuntimeStatus;
  runtimeError?: string;
  modelConfigurations: ModelConfigurationSummary[];
  modelCallBinding?: ModelCallBindingPayload | null;
  interaction?: RuntimeInteractionSummary;
  toolDecisionPending?: boolean;
  canControl: boolean;
  isObserver: boolean;
  permission: PermissionMode;
  skills: SkillCapability[];
  focusTaskId?: string;
  focusTaskRevision: number;
  focusTaskHighlighted: boolean;
  onReconnect: () => void;
  onTakeControl: () => void;
  onOpenSidebar: () => void;
  onNewSession: () => void;
  canCreateSession: boolean;
  onToggleInspector: () => void;
  onOpenModelSettings: () => void;
  onModelCallBindingChange: (binding: ModelCallBindingPayload) => Promise<void>;
  onSend: (
    content: EditablePromptContent,
    permission: PermissionMode,
    requestPlan: boolean,
  ) => Promise<boolean>;
  onStop: () => void;
  onCompact: () => Promise<void>;
  onReopenRuntime: () => void;
  runtimeReopenBusy: boolean;
  onReadInteraction: (
    interaction: RuntimeInteractionSummary,
  ) => Promise<RuntimeInteractionContent>;
  onResolveInteraction: (
    interaction: RuntimeInteractionSummary,
    resolution: RuntimeInteractionResolution,
  ) => Promise<boolean>;
  artifactOwnerKey: string;
  onReadToolArtifact: (resultEntryId: string, offsetChars: number) => Promise<ToolArtifactPage>;
  onReadPromptImage: (image: CanonicalPromptImagePart) => Promise<Uint8Array>;
  onReadVisualization?: (entryId: string, ordinal: number, digest: string, size: number) => Promise<string>;
  promptDraftStore: PromptDraftStore;
  onNotify: MarkdownNotify;
  onPermissionChange: (permission: PermissionMode) => void;
}

const traceIcons: Record<ToolTrace['kind'], typeof TerminalSquare> = {
  terminal: TerminalSquare,
  read: FileSearch,
  edit: FileDiff,
  search: FileSearch,
  artifact: Braces,
  mcp: Zap,
};

type JsonObject = Record<string, unknown>;

interface McpToolIdentity {
  serverId: string;
  remoteToolName: string;
}

interface McpDetailRow {
  label: string;
  value: string;
}

interface McpDetailItem {
  name: string;
  detail?: string;
}

interface McpTraceDetail {
  subtitle: string;
  rows: McpDetailRow[];
  items: McpDetailItem[];
  suppressGenericSuccess: boolean;
}

function parseJsonObject(value?: string): JsonObject | undefined {
  if (!value) return undefined;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as JsonObject
      : undefined;
  } catch {
    return undefined;
  }
}

function terminalOutputText(value: string | undefined, running: boolean): string | undefined {
  if (running) return value ?? '';
  if (value === undefined) return undefined;
  const result = parseJsonObject(value);
  if (
    typeof result?.output === 'string'
    && (
      typeof result.terminal_process_action === 'string'
      || typeof result.exit_code === 'number'
    )
  ) return result.output;
  return value;
}

function objectField(value: JsonObject | undefined, key: string): JsonObject | undefined {
  const field = value?.[key];
  return field && typeof field === 'object' && !Array.isArray(field)
    ? field as JsonObject
    : undefined;
}

function objectArrayField(value: JsonObject | undefined, key: string): JsonObject[] {
  const field = value?.[key];
  return Array.isArray(field)
    ? field.filter((item): item is JsonObject => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
    : [];
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function numberValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function mcpStatusLabel(value: unknown): string {
  const status = stringValue(value).toUpperCase();
  if (status === 'READY') return '已就绪';
  if (status === 'CONNECTING' || status === 'DISCOVERING' || status === 'UPDATING') return '连接中';
  if (status === 'DISABLED') return '已关闭';
  if (status === 'FAILED_RETRYABLE') return '等待重试';
  if (status === 'FAILED_TERMINAL') return '连接失败';
  return status ? status.toLowerCase() : '状态未知';
}

function collectMessageTraces(messages: Message[]): ToolTrace[] {
  const traces: ToolTrace[] = [];
  for (const message of messages) {
    traces.push(...(message.traces ?? []));
    for (const run of message.subagentRuns ?? []) {
      for (const activity of run.activities) traces.push(...(activity.traces ?? []));
    }
  }
  return traces;
}

function buildMcpToolRefIndex(messages: Message[]): ReadonlyMap<string, McpToolIdentity> {
  const result = new Map<string, McpToolIdentity>();
  for (const trace of collectMessageTraces(messages)) {
    if (trace.toolName !== 'inspect_new_mcp_tool') continue;
    const descriptor = parseJsonObject(trace.resultText);
    const toolRef = stringValue(descriptor?.tool_ref);
    const serverId = stringValue(descriptor?.server_id);
    const remoteToolName = stringValue(descriptor?.remote_tool_name);
    if (!toolRef || !serverId || !remoteToolName) continue;
    result.set(toolRef, {
      serverId,
      remoteToolName,
    });
  }
  return result;
}

function normalizedSkillDocumentPath(value: string): string {
  return value.replaceAll('\\', '/').replace(/\/+$/u, '');
}

function traceSkill(trace: ToolTrace, skills: SkillCapability[]): SkillCapability | undefined {
  if (trace.toolName !== 'read_file') return undefined;
  const requested = stringValue(parseJsonObject(trace.argumentsJson)?.path);
  const normalized = normalizedSkillDocumentPath(requested);
  if (!normalized || normalized.split('/').at(-1) !== 'SKILL.md') return undefined;
  return skills.find((skill) => (
    skill.enabled
    && skill.effective
    && [skill.path, skill.location]
      .filter(Boolean)
      .map(normalizedSkillDocumentPath)
      .includes(normalized)
  ));
}

function mcpTraceDetail(
  trace: ToolTrace,
  toolRefs: ReadonlyMap<string, McpToolIdentity>,
): McpTraceDetail | undefined {
  const name = trace.toolName;
  if (!name || !['list_mcp_servers', 'inspect_new_mcp_tool', 'use_new_mcp_tool'].includes(name)) {
    return undefined;
  }
  const args = parseJsonObject(trace.argumentsJson);
  const result = parseJsonObject(trace.resultText);
  if (name === 'list_mcp_servers') {
    const requestedServer = stringValue(args?.server_id);
    const server = objectField(result, 'server');
    const servers = objectArrayField(result, 'servers');
    const tools = objectArrayField(result, 'tools');
    if (server || requestedServer) {
      const serverId = stringValue(server?.server_id) || requestedServer;
      const rows: McpDetailRow[] = [{ label: 'MCP 服务', value: serverId }];
      if (server) rows.push({ label: '连接状态', value: mcpStatusLabel(server.public_status) });
      return {
        subtitle: `${serverId} · ${tools.length} 个工具`,
        rows,
        items: tools.map((tool) => ({
          name: stringValue(tool.remote_tool_name) || stringValue(tool.provider_tool_name) || '未命名工具',
        })),
        suppressGenericSuccess: true,
      };
    }
    const total = numberValue(result?.total_server_count) || servers.length;
    return {
      subtitle: `发现 ${total} 个 MCP 服务`,
      rows: total > servers.length
        ? [{ label: '当前页', value: `${servers.length} / ${total}` }]
        : [],
      items: servers.map((item) => ({
        name: stringValue(item.server_id) || '未命名服务',
        detail: `${mcpStatusLabel(item.public_status)} · ${numberValue(item.tool_count)} 个工具`,
      })),
      suppressGenericSuccess: true,
    };
  }
  if (name === 'inspect_new_mcp_tool') {
    const serverId = stringValue(result?.server_id) || stringValue(args?.server_id) || '未知服务';
    const remoteToolName = stringValue(result?.remote_tool_name) || stringValue(args?.tool_name) || '未知工具';
    return {
      subtitle: `${serverId} · ${remoteToolName}`,
      rows: [
      { label: 'MCP 服务', value: serverId },
      { label: '检查的工具', value: remoteToolName },
      ],
      items: [],
      suppressGenericSuccess: true,
    };
  }
  const toolRef = stringValue(args?.tool_ref);
  const identity = toolRefs.get(toolRef);
  const rows: McpDetailRow[] = identity
    ? [
      { label: 'MCP 服务', value: identity.serverId },
      { label: '调用的工具', value: identity.remoteToolName },
    ]
    : [{ label: '调用方式', value: '使用此前检查通过的 MCP 工具' }];
  return {
    subtitle: identity
      ? `${identity.serverId} · ${identity.remoteToolName}`
      : '调用已经检查的 MCP 工具',
    rows,
    items: [],
    suppressGenericSuccess: true,
  };
}

function McpTraceDetails({ detail }: { detail: McpTraceDetail }) {
  return (
    <section className="mcp-trace-details" aria-label="MCP 操作详情">
      {detail.rows.length ? (
        <dl>{detail.rows.map((row) => (
          <div key={`${row.label}:${row.value}`}><dt>{row.label}</dt><dd>{row.value}</dd></div>
        ))}</dl>
      ) : null}
      {detail.items.length ? (
        <div className="mcp-trace-list">{detail.items.map((item, index) => (
          <div className="mcp-trace-list__item" key={`${item.name}:${index}`}>
            <strong>{item.name}</strong>{item.detail && <span>{item.detail}</span>}
          </div>
        ))}</div>
      ) : null}
    </section>
  );
}

function TraceCard({
  trace,
  skills,
  mcpToolRefs,
  artifactOwnerKey,
  onReadToolArtifact,
  onReadPromptImage,
}: {
  trace: ToolTrace;
  skills: SkillCapability[];
  mcpToolRefs: ReadonlyMap<string, McpToolIdentity>;
  artifactOwnerKey: string;
  onReadToolArtifact: WorkbenchViewProps['onReadToolArtifact'];
  onReadPromptImage: WorkbenchViewProps['onReadPromptImage'];
}) {
  const { showBuiltinToolResults } = useContext(ToolResultDisplayContext);
  const [expanded, setExpanded] = useState(false);
  const [artifactPage, setArtifactPage] = useState<ToolArtifactPage>();
  const [artifactBusy, setArtifactBusy] = useState(false);
  const [artifactError, setArtifactError] = useState('');
  const artifactRequestRevision = useRef(0);
  const artifactOwnerKeyRef = useRef(artifactOwnerKey);
  const artifactResultEntryIdRef = useRef(trace.resultEntryId);
  const terminalOutputRef = useRef<HTMLPreElement>(null);
  const terminalFollowTailRef = useRef(true);
  useEffect(() => {
    artifactRequestRevision.current += 1;
    return () => { artifactRequestRevision.current += 1; };
  }, []);
  const Icon = traceIcons[trace.kind];
  const skill = traceSkill(trace, skills);
  const mcpDetail = mcpTraceDetail(trace, mcpToolRefs);
  const builtinSummary = builtinToolSummary(trace);
  const purpose = skill ? `使用 ${skill.name} 技能` : builtinSummary?.title ?? trace.title;
  const subtitle = mcpDetail?.subtitle ?? builtinSummary?.subtitle ?? trace.subtitle;
  const parsedResult = parseJsonObject(trace.resultText);
  const terminalOutput = trace.toolName === 'terminal'
    ? terminalOutputText(trace.resultText, trace.status === 'running')
    : undefined;
  const hasTerminalOutput = terminalOutput !== undefined;
  const terminalYieldedToBackground = Boolean(
    trace.toolName === 'terminal'
    && trace.status === 'completed'
    && parsedResult?.status === 'running'
    && parsedResult.yielded_to_background === true,
  );
  useLayoutEffect(() => {
    if (!expanded || trace.status !== 'running' || !terminalFollowTailRef.current) return;
    const output = terminalOutputRef.current;
    if (output) output.scrollTop = output.scrollHeight;
  }, [expanded, terminalOutput, trace.status]);
  // MCP provider names follow the kernel naming contract; the late-tool bridge
  // also returns an external tool's output. UI icon categories are not origins.
  const showRawResult = showBuiltinToolResults
    || trace.toolName?.startsWith('mcp__')
    || trace.toolName === 'use_new_mcp_tool';
  const hasRawResult = showRawResult && Object.prototype.hasOwnProperty.call(trace, 'resultText');
  const diffText = trace.toolName === 'edit_file' ? stringValue(parsedResult?.diff) : '';
  const canReadArtifact = Boolean(
    showRawResult && trace.resultEntryId
    && (trace.artifact?.disposition === 'AVAILABLE' || trace.artifact?.disposition === 'INCOMPLETE'),
  );
  const expandable = Boolean(
    trace.command || diffText || hasTerminalOutput || hasRawResult
    || trace.resultContent || mcpDetail || canReadArtifact
  );
  const artifactAtEnd = Boolean(artifactPage && !artifactPage.hasMore);
  const artifactIsSinglePage = Boolean(
    artifactAtEnd
    && artifactPage?.offsetChars === 0
    && artifactPage.returnedChars === artifactPage.totalChars,
  );
  const closeArtifactPage = () => {
    artifactRequestRevision.current += 1;
    setArtifactPage(undefined);
    setArtifactBusy(false);
    setArtifactError('');
  };
  const readArtifactPage = (offsetChars: number) => {
    const expectedOwnerKey = artifactOwnerKey;
    const expectedResultEntryId = trace.resultEntryId!;
    const requestRevision = ++artifactRequestRevision.current;
    setArtifactBusy(true);
    setArtifactError('');
    void onReadToolArtifact(expectedResultEntryId, offsetChars).then(
      (page) => {
        if (
          requestRevision !== artifactRequestRevision.current
          || artifactOwnerKeyRef.current !== expectedOwnerKey
          || artifactResultEntryIdRef.current !== expectedResultEntryId
        ) return;
        setArtifactPage(page);
      },
      () => {
        if (
          requestRevision !== artifactRequestRevision.current
          || artifactOwnerKeyRef.current !== expectedOwnerKey
          || artifactResultEntryIdRef.current !== expectedResultEntryId
        ) return;
        setArtifactError('完整输出暂时无法读取。');
      },
    ).finally(() => {
      if (
        requestRevision === artifactRequestRevision.current
        && artifactOwnerKeyRef.current === expectedOwnerKey
        && artifactResultEntryIdRef.current === expectedResultEntryId
      ) setArtifactBusy(false);
    });
  };
  const summary = (
    <>
      <span className={`trace-icon trace-icon--${trace.kind}`}><Icon size={14} /></span>
      <span className="trace-summary-copy">
        <span className="trace-summary-title">
          <strong>{builtinSummary ? purpose : trace.toolName ?? trace.title}</strong>
          {!builtinSummary && trace.toolName && purpose !== trace.toolName
            ? <span className="trace-purpose">{purpose}</span>
            : null}
        </span>
        <small>{subtitle}</small>
      </span>
      <span className={`trace-state trace-state--${trace.status}`}>
        {trace.status === 'running' ? `进行中 · ${trace.duration ?? ''}` : trace.duration ?? (builtinSummary ? undefined : trace.meta)}
      </span>
      {expandable && <ChevronRight className="trace-chevron" size={13} />}
    </>
  );

  return (
    <div className={`trace-node trace-node--${trace.status}`}>
      <span className="trace-node__dot" />
      <article className={`trace-card${expanded ? ' is-expanded' : ''}`}>
        {expandable ? (
          <button
            className="trace-card__summary"
            onClick={() => {
              if (expanded) closeArtifactPage();
              else terminalFollowTailRef.current = true;
              setExpanded((value) => !value);
            }}
            aria-expanded={expanded}
            aria-label={`${expanded ? '收起' : '展开'}工具详情：${trace.toolName ?? trace.title}`}
          >{summary}</button>
        ) : <div className="trace-card__summary">{summary}</div>}
        {expandable && <AnimatedDisclosure open={expanded} className="trace-card__disclosure">
          {trace.resultContent ? (
            <section className="tool-result-images" aria-label="工具读取的图片">
              <PromptContentView
                content={trace.resultContent}
                variant="tool"
                onReadImage={onReadPromptImage}
              />
            </section>
          ) : (
            <div className="terminal-output">
            {trace.command && <div className="terminal-command"><span>$</span> {trace.command}</div>}
            {mcpDetail && <McpTraceDetails detail={mcpDetail} />}
            {hasTerminalOutput && (
              <section className="terminal-stream" aria-label="命令输出">
                <header>
                  <strong>输出</strong>
                  <button type="button" aria-label="复制命令输出" onClick={() => void navigator.clipboard.writeText(terminalOutput)}><Copy size={12} /></button>
                </header>
                <pre
                  ref={terminalOutputRef}
                  className="tool-output-scroll"
                  onScroll={(event) => {
                    const output = event.currentTarget;
                    terminalFollowTailRef.current = output.scrollHeight
                      - output.scrollTop - output.clientHeight <= 8;
                  }}
                >
                  {terminalOutput === '' ? <span className="empty-result">（暂无输出）</span> : terminalOutput}
                  {trace.status === 'running' && <span className="terminal-cursor" aria-label="命令仍在执行" />}
                </pre>
                {terminalYieldedToBackground && (
                  <p className="terminal-stream__backgrounded"><em>已转到后台运行</em></p>
                )}
              </section>
            )}
            {diffText
              ? <pre className="tool-result-diff tool-output-scroll" aria-label="文件差异">{diffText}</pre>
              : showRawResult && trace.toolName !== 'terminal' && trace.resultSummary
                && <div className="tool-result-summary">{trace.resultSummary}</div>}
            {hasRawResult && (
              <section className="tool-result-raw" aria-label="工具原始结果">
                <header>
                  <strong>原始结果</strong>
                  <button type="button" aria-label="复制工具原始结果" onClick={() => void navigator.clipboard.writeText(trace.resultText ?? '')}><Copy size={12} /></button>
                </header>
                <pre className="tool-output-scroll">{trace.resultText === '' ? <span className="empty-result">（空字符串）</span> : trace.resultText}</pre>
              </section>
            )}
            {canReadArtifact && trace.resultEntryId && (
              <section className={`tool-artifact-page${artifactPage ? ' is-open' : ''}`} aria-label="完整工具输出">
                {trace.artifact?.sourceCoverage === 'RETAINED_SNAPSHOT' && (
                  <p className="tool-artifact-page__notice"><TriangleAlert size={12} />仅保留快照；页码相对于保留内容。</p>
                )}
                {artifactPage ? (
                  <>
                    <header className="tool-artifact-page__header">
                      <div className="tool-artifact-page__title">
                        <span className="tool-artifact-page__icon"><BookOpenText size={13} /></span>
                        <strong>完整输出</strong>
                        <span className="tool-artifact-page__range">
                          {artifactPage.offsetChars + 1}–{artifactPage.offsetChars + artifactPage.returnedChars} / {artifactPage.totalChars}
                        </span>
                      </div>
                      <div className="tool-artifact-page__actions">
                        <button
                          type="button"
                          className="tool-artifact-page__icon-button"
                          aria-label="复制当前工具输出页"
                          title="复制当前页"
                          onClick={() => void navigator.clipboard.writeText(artifactPage.text)}
                        ><Copy size={13} /></button>
                        <button
                          type="button"
                          className="tool-artifact-page__icon-button"
                          disabled={artifactBusy}
                          aria-label="收起完整输出"
                          title="收起完整输出"
                          onClick={closeArtifactPage}
                        ><ChevronUp size={14} /></button>
                      </div>
                    </header>
                    <pre className="tool-output-scroll">{artifactPage.text}</pre>
                    <footer className="tool-artifact-page__footer">
                      <span className="tool-artifact-page__coverage">
                        <i />{trace.artifact?.sourceCoverage === 'RETAINED_SNAPSHOT' ? '保留快照' : '完整保留'}
                      </span>
                      {artifactAtEnd ? (
                        <span
                          className="tool-artifact-page__complete"
                          role="status"
                          aria-label="完整输出读取状态"
                          title={artifactIsSinglePage ? '完整输出只有这一页' : '已读取到完整输出末页'}
                        ><Check size={12} />已到末页</span>
                      ) : (
                        <button
                          type="button"
                          className="tool-artifact-page__next"
                          disabled={artifactBusy}
                          aria-label={artifactBusy ? '正在读取完整输出' : '读取下一页'}
                          onClick={() => {
                            if (artifactPage.nextOffsetChars !== undefined) {
                              void readArtifactPage(artifactPage.nextOffsetChars);
                            }
                          }}
                        >{artifactBusy ? <LoaderCircle className="is-spinning" size={13} /> : <ArrowDown size={13} />}{artifactBusy ? '正在读取…' : '继续读取'}</button>
                      )}
                    </footer>
                  </>
                ) : (
                  <button
                    type="button"
                    className="tool-artifact__trigger"
                    disabled={artifactBusy}
                    aria-label={artifactBusy ? '正在读取完整输出' : artifactError ? '重新读取完整输出' : '查看完整输出'}
                    onClick={() => readArtifactPage(0)}
                  >
                    <span className="tool-artifact__trigger-icon">
                      {artifactBusy ? <LoaderCircle className="is-spinning" size={13} /> : <BookOpenText size={13} />}
                    </span>
                    <span className="tool-artifact__trigger-copy">
                      <strong>{artifactBusy ? '正在读取…' : artifactError ? '重新读取完整输出' : '查看完整输出'}</strong>
                      <small>{artifactError || '按需加载保留的工具结果'}</small>
                    </span>
                    <ChevronRight size={13} />
                  </button>
                )}
              </section>
            )}
            {trace.meta && <footer><span>{trace.meta}</span></footer>}
            </div>
          )}
        </AnimatedDisclosure>}
      </article>
    </div>
  );
}

function TodoDock({
  todo,
  interactionOpen,
  onLayoutChange,
}: {
  todo?: TodoRun;
  interactionOpen: boolean;
  onLayoutChange: () => void;
}) {
  const [open, setOpen] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);
  const popoverRef = useRef<HTMLElement>(null);
  const items = todo?.items ?? [];
  const completed = items.filter((item) => item.status === 'completed').length;
  const activeIndex = items.findIndex((item) => item.status === 'in-progress');
  const pendingIndex = items.findIndex((item) => item.status === 'pending');
  const currentStep = activeIndex >= 0
    ? activeIndex + 1
    : pendingIndex >= 0
      ? pendingIndex + 1
      : items.length;
  const expanded = open && !interactionOpen;

  useEffect(() => {
    if (!expanded) return undefined;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePointer);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [expanded]);

  useEffect(() => {
    let frame = window.requestAnimationFrame(onLayoutChange);
    if (typeof ResizeObserver === 'undefined') {
      return () => window.cancelAnimationFrame(frame);
    }
    const observer = new ResizeObserver(() => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(onLayoutChange);
    });
    if (containerRef.current) observer.observe(containerRef.current);
    if (popoverRef.current) observer.observe(popoverRef.current);
    return () => {
      observer.disconnect();
      window.cancelAnimationFrame(frame);
    };
  }, [expanded, items.length, onLayoutChange]);

  if (!todo || items.length === 0) return null;

  return (
    <div className={`todo-dock${expanded ? ' is-open' : ''}`} ref={containerRef}>
      {expanded && (
        <section
          ref={popoverRef}
          className="todo-dock__popover"
          id="current-todo-list"
          aria-label="TODO清单"
          aria-live="polite"
          onAnimationEnd={onLayoutChange}
        >
          <header className="todo-dock__header">
            <span><ListTodo size={14} /><strong>TODO清单</strong></span>
            <small>{completed} / {items.length} 已完成</small>
          </header>
          <div className="todo-dock__progress" aria-hidden="true">
            <span style={{ width: `${(completed / items.length) * 100}%` }} />
          </div>
          <ol>
            {items.map((item) => (
              <li className={`todo-dock__item todo-dock__item--${item.status}`} key={item.id}>
                <span className="todo-dock__marker" aria-hidden="true">
                  {item.status === 'completed' ? <Check size={10} /> : null}
                </span>
                <span>{item.label}</span>
                {item.status === 'in-progress' && <small>正在进行</small>}
              </li>
            ))}
          </ol>
        </section>
      )}
      <button
        className="todo-dock__trigger"
        type="button"
        onClick={() => setOpen((value) => !value)}
        disabled={interactionOpen}
        aria-expanded={expanded}
        aria-controls="current-todo-list"
        aria-label={expanded ? '收起TODO清单' : '展开TODO清单'}
        title={interactionOpen ? '完成当前确认后可查看清单' : undefined}
      >
        <span className={`todo-dock__trigger-state${completed === items.length ? ' is-complete' : ''}`}>
          {completed === items.length ? <Check size={10} /> : null}
        </span>
        <span>{completed === items.length ? `${items.length} / ${items.length} 步已完成` : `第 ${currentStep} / ${items.length} 步`}</span>
        <ChevronDown size={12} />
      </button>
    </div>
  );
}

function reasoningPreview(body: string, running: boolean): string {
  const lines = body
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  return (running ? lines.at(-1) : lines[0]) ?? body.trim();
}

function ReasoningRow({ block, onNotify }: { block: ReasoningBlock; onNotify: MarkdownNotify }) {
  const [expanded, setExpanded] = useState(false);
  const title = block.kind === 'summary' ? '思考摘要' : '思考';
  const running = Boolean(block.active);
  return (
    <section className={`reasoning-row${expanded ? ' is-expanded' : ''}`}>
      <button
        className="reasoning-row__summary"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        aria-label={`${expanded ? '收起' : '展开'}${title}`}
      >
        <span className="reasoning-row__icon"><Sparkles size={11} /></span>
        <span className="reasoning-row__copy">
          <strong>{title}</strong>
          {!expanded && <small><MarkdownInline body={reasoningPreview(block.body, running)} onNotify={onNotify} /></small>}
        </span>
        {running && <span className="reasoning-row__live"><i />思考中</span>}
        <ChevronRight className="reasoning-row__chevron" size={12} />
      </button>
      <AnimatedDisclosure open={expanded}>
        <div className="reasoning-row__body assistant-markdown"><MarkdownBody body={block.body} onNotify={onNotify} /></div>
      </AnimatedDisclosure>
    </section>
  );
}

function ReasoningDisclosure({
  blocks,
  onNotify,
  nested = false,
}: {
  blocks: ReasoningBlock[];
  onNotify: MarkdownNotify;
  nested?: boolean;
}) {
  return (
    <div className={`reasoning-disclosure${nested ? ' reasoning-disclosure--nested' : ''}`}>
      {blocks.map((block) => <ReasoningRow key={block.id} block={block} onNotify={onNotify} />)}
    </div>
  );
}

const subagentStatusLabels: Record<SubagentRun['status'], string> = {
  pending: '待开始',
  running: '进行中',
  waiting: '等待中',
  completed: '已完成',
  cancelled: '已取消',
  failed: '失败',
  interrupted: '已中断',
  blocked: '依赖未完成',
  ended: '已结束',
};

function SubagentRunCard({
  run,
  focused,
  skills,
  mcpToolRefs,
  artifactOwnerKey,
  onReadToolArtifact,
  onReadPromptImage,
  onNotify,
}: {
  run: SubagentRun;
  focused: boolean;
  skills: SkillCapability[];
  mcpToolRefs: ReadonlyMap<string, McpToolIdentity>;
  artifactOwnerKey: string;
  onReadToolArtifact: WorkbenchViewProps['onReadToolArtifact'];
  onReadPromptImage: WorkbenchViewProps['onReadPromptImage'];
  onNotify: MarkdownNotify;
}) {
  const [expanded, setExpanded] = useState(
    focused || run.status === 'running' || run.status === 'waiting' || run.status === 'pending',
  );
  const lastBody = [...run.activities].reverse().find((activity) => activity.body)?.body.trim();
  const showSummary = Boolean(run.summary?.trim() && run.summary.trim() !== lastBody);

  return (
    <article
      className={`subagent-run subagent-run--${run.status}${expanded ? ' is-expanded' : ''}${focused ? ' is-focused' : ''}`}
      data-task-id={run.id}
    >
      <button className="subagent-run__header" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
        <span className={`agent-mini-icon color-${run.color}`}><Bot size={13} /></span>
        <span className="subagent-run__identity"><strong>{run.label}</strong><small>{run.role}</small></span>
        <span className={`subagent-run__status subagent-run__status--${run.status}`}>
          {run.status === 'running' ? <LoaderCircle size={11} /> : run.status === 'completed' ? <Check size={11} /> : null}
          {subagentStatusLabels[run.status]}
        </span>
        <ChevronDown size={13} />
      </button>
      <AnimatedDisclosure open={expanded}>
        <div className="subagent-run__body">
          {run.objective && <div className="subagent-objective"><span>目标</span><p>{run.objective}</p></div>}
          {run.activities.map((activity) => (
            <section className="subagent-activity" key={activity.id}>
              {activity.reasoning?.length ? (
                <ReasoningDisclosure
                  blocks={activity.reasoning}
                  onNotify={onNotify}
                  nested
                />
              ) : null}
              {activity.body && (activity.kind === 'guidance' ? (
                <div className="subagent-guidance">
                  <span><CornerDownRight size={11} /> 主任务补充</span>
                  <div className="assistant-markdown"><MarkdownBody body={activity.body} onNotify={onNotify} /></div>
                </div>
              ) : <div className="assistant-markdown"><MarkdownBody body={activity.body} onNotify={onNotify} /></div>)}
              {activity.traces?.length ? <div className="execution-rail subagent-execution">{activity.traces.map((trace) => <TraceCard key={`${artifactOwnerKey}:${trace.id}:${trace.resultEntryId ?? ''}`} trace={trace} skills={skills} mcpToolRefs={mcpToolRefs} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} onReadPromptImage={onReadPromptImage} />)}</div> : null}
            </section>
          ))}
          {showSummary && run.summary && (
            <section className="subagent-summary"><span>结果</span><div className="assistant-markdown"><MarkdownBody body={run.summary} onNotify={onNotify} /></div></section>
          )}
        </div>
      </AnimatedDisclosure>
    </article>
  );
}

function SubagentGroup({
  runs,
  focusTaskId,
  focusTaskRevision,
  focusTaskHighlighted,
  skills,
  mcpToolRefs,
  artifactOwnerKey,
  onReadToolArtifact,
  onReadPromptImage,
  onNotify,
}: {
  runs: SubagentRun[];
  focusTaskId?: string;
  focusTaskRevision: number;
  focusTaskHighlighted: boolean;
  skills: SkillCapability[];
  mcpToolRefs: ReadonlyMap<string, McpToolIdentity>;
  artifactOwnerKey: string;
  onReadToolArtifact: WorkbenchViewProps['onReadToolArtifact'];
  onReadPromptImage: WorkbenchViewProps['onReadPromptImage'];
  onNotify: MarkdownNotify;
}) {
  const settled = runs.filter((run) => !['pending', 'running', 'waiting'].includes(run.status)).length;
  return (
    <section className="subagent-group" aria-label="子任务执行">
      <header className="subagent-group__header">
        <span><GitFork size={13} /><strong>子任务执行</strong></span>
        <small>{settled} / {runs.length} 已结束</small>
      </header>
      <div className="subagent-group__runs">{runs.map((run) => (
        <SubagentRunCard
          key={`${run.id}:${focusTaskId === run.id ? focusTaskRevision : 0}`}
          run={run}
          focused={focusTaskHighlighted && focusTaskId === run.id}
          skills={skills}
          mcpToolRefs={mcpToolRefs}
          artifactOwnerKey={artifactOwnerKey}
          onReadToolArtifact={onReadToolArtifact}
          onReadPromptImage={onReadPromptImage}
          onNotify={onNotify}
        />
      ))}</div>
    </section>
  );
}

const unavailablePromptImage = async (): Promise<Uint8Array> => {
  throw new Error('这张图片当前无法读取。');
};

const unavailableVisualization = async (): Promise<string> => {
  throw new Error('Visualization content reader is unavailable');
};

const visualizationCsp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; worker-src 'none'; frame-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; navigate-to 'none'";

function VisualizationPanel({ entryId, visualization, onRead }: {
  entryId: string;
  visualization: VisualizationOccurrence;
  onRead: NonNullable<WorkbenchViewProps['onReadVisualization']>;
}) {
  const [state, setState] = useState<{ html?: string; error?: string }>({});
  const [probeWidth, setProbeWidth] = useState<number | null>(null);
  const [rootRect, setRootRect] = useState<VisualizationRootRect | null>(null);
  const probeWidthRef = useRef<number | null>(null);
  const widthProbeRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const ordinal = visualization.ordinal;
  const digest = visualization.visualizationRef;
  const size = visualization.contentSize;
  useEffect(() => {
    if (visualization.state !== 'READY' || !digest || !size) return;
    let active = true;
    void onRead(entryId, ordinal, digest, size).then(
      (html) => { if (active) { setRootRect(null); setState({ html }); } },
      () => { if (active) setState({ error: '已保存的可视化暂时无法读取。' }); },
    );
    return () => { active = false; };
  }, [entryId, ordinal, digest, size, onRead, visualization.state]);
  useEffect(() => {
    const probe = widthProbeRef.current;
    if (!probe) return;
    const measure = () => {
      const width = Math.max(1, probe.clientWidth - 2);
      if (probeWidthRef.current === width) return;
      probeWidthRef.current = width;
      setProbeWidth(width);
      setRootRect(null);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(probe);
    measure();
    return () => observer.disconnect();
  }, [state.html]);
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      const frame = frameRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      const message = event.data;
      if (!message || typeof message !== 'object' || message.type !== visualizationLayoutMessageType) return;
      if (message.mode === 'page') {
        setRootRect(null);
      } else if (message.mode === 'root') {
        setRootRect(usableVisualizationRootRect(message.rect, frame.clientWidth, frame.clientHeight));
      }
    };
    window.addEventListener('message', receive);
    return () => window.removeEventListener('message', receive);
  }, []);
  if (visualization.state === 'FAILED') {
    return <div className="assistant-visualization assistant-visualization--failed" role="status">
      {visualization.failureDetail ?? '可视化未能生成。'}
    </div>;
  }
  if (!digest || !size) {
    return <div className="assistant-visualization assistant-visualization--failed" role="status">可视化引用不完整。</div>;
  }
  if (state.error) {
    return <div className="assistant-visualization assistant-visualization--failed" role="status">{state.error}</div>;
  }
  if (state.html === undefined) {
    return <div className="assistant-visualization assistant-visualization--loading" role="status">正在加载可视化…</div>;
  }
  const documentBody = state.html.replace(/^\s*<!doctype[^>]*>/i, '');
  const source = `<!doctype html><meta http-equiv="Content-Security-Policy" content="${visualizationCsp}">${documentBody}${visualizationFrameMeasurementScript}`;
  return <div className="assistant-visualization" data-visualization-ordinal={ordinal}
    data-visualization-layout={rootRect ? 'root' : 'page'}
    style={rootRect ? { width: Math.ceil(rootRect.width) + 2 } : undefined}>
    <div className="assistant-visualization__width-probe" ref={widthProbeRef} aria-hidden="true" />
    <div className="assistant-visualization__viewport" style={rootRect ? { height: Math.ceil(rootRect.height) } : undefined}>
      <iframe ref={frameRef} title={`可视化 ${ordinal + 1}`} sandbox="allow-scripts"
        referrerPolicy="no-referrer" srcDoc={source}
        style={{
          width: probeWidth === null ? '100%' : probeWidth,
          transform: rootRect ? `translate(${-rootRect.x}px, ${-rootRect.y}px)` : undefined,
        }} />
    </div>
  </div>;
}

function UserMessage({
  message,
  label = '你',
  pendingContent,
  deliveryStatus,
  onReadPromptImage = unavailablePromptImage,
}: {
  message: Message;
  label?: string;
  pendingContent?: LocalPromptSubmission['content'];
  deliveryStatus?: string;
  onReadPromptImage?: WorkbenchViewProps['onReadPromptImage'];
}) {
  const content = pendingContent ?? message.promptContent;
  const sourceTextStyle = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } as const;
  if (message.userKind === 'subagent-completion') {
    const helpId = `${message.id}-subagent-completion-help`;
    const sourceResult = message.sourceSubagentLabel
      ? `${message.sourceSubagentLabel} 的结果`
      : '子任务结果';
    const title = message.sourceSubagentRelation === 'previous'
      ? `上一轮 ${sourceResult}已加入本轮对话`
      : message.sourceSubagentRelation === 'earlier'
        ? `此前 ${sourceResult}已加入本轮对话`
        : `${sourceResult}已加入本轮对话`;
    return (
      <article
        className="subagent-completion-event"
        aria-label={title}
        aria-describedby={helpId}
        tabIndex={0}
      >
        <span className="subagent-completion-event__icon"><GitFork size={13} /></span>
        <div className="subagent-completion-event__copy">
          <strong>{title}</strong>
          <small>子任务结果已记录到当前对话</small>
        </div>
        <time>{message.time}</time>
        <span id={helpId} className="subagent-completion-event__tooltip" role="tooltip">
          该结果已记录到当前对话；这不是你发送的新消息，也不会重新运行子任务。
        </span>
      </article>
    );
  }

  if (message.userKind === 'steer') {
    return (
      <article className="user-steer" aria-label="引导">
        <span className="user-steer__icon"><CornerDownRight size={13} /></span>
        <div className="user-steer__content">
          <header><strong>引导</strong><time>{message.time}</time></header>
          {content
            ? <PromptContentView
                content={content}
                variant="message"
                onReadImage={onReadPromptImage}
              />
            : <p style={sourceTextStyle}>{message.body}</p>}
        </div>
      </article>
    );
  }

  return (
    <article className="user-turn">
      <header className="user-heading">
        <strong>{label}</strong>
        <span className="user-avatar"><UserRound size={14} /></span>
      </header>
      <div className="user-message">
        {content
          ? <PromptContentView
              content={content}
              variant="message"
              onReadImage={onReadPromptImage}
            />
          : <p style={sourceTextStyle}>{message.body}</p>}
        <div className="message-foot">{deliveryStatus ? <span role="status">{deliveryStatus}</span> : <time>{message.time}</time>}</div>
      </div>
    </article>
  );
}

function AssistantHeading({ message, response, label = 'Pulsara' }: { message: Message; response: boolean; label?: string }) {
  return (
    <header className={`assistant-heading${response ? ' assistant-heading--response' : ' assistant-heading--run-start'}`}>
      <div className="assistant-avatar" aria-hidden="true">
        {/* Local static asset: the standalone app has no Next image service. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/assets/pulsara-icon.png" width={27} height={27} alt="" draggable={false} />
      </div>
      <div className="assistant-identity">
        <strong>{label}</strong>
        {message.status === 'running' && (
          <span className="thinking-label"><i /> {response ? '正在回复' : '正在执行'}</span>
        )}
      </div>
      {message.model && <span className="model-label">{message.model}</span>}
    </header>
  );
}

function AssistantMessage({
  message,
  responseActionEligible,
  startsAssistantRun,
  joinsPreviousToolChain,
  joinsNextToolChain,
  focusTaskId,
  focusTaskRevision,
  focusTaskHighlighted,
  skills,
  mcpToolRefs,
  onNotify,
  onFork,
  artifactOwnerKey,
  onReadToolArtifact,
  onReadPromptImage,
  onReadVisualization,
  assistantLabel,
}: {
  message: Message;
  responseActionEligible: boolean;
  startsAssistantRun: boolean;
  joinsPreviousToolChain: boolean;
  joinsNextToolChain: boolean;
  focusTaskId?: string;
  focusTaskRevision: number;
  focusTaskHighlighted: boolean;
  skills: SkillCapability[];
  mcpToolRefs: ReadonlyMap<string, McpToolIdentity>;
  onNotify: WorkbenchViewProps['onNotify'];
  onFork: WorkbenchViewProps['onFork'];
  artifactOwnerKey: string;
  onReadToolArtifact: WorkbenchViewProps['onReadToolArtifact'];
  onReadPromptImage: WorkbenchViewProps['onReadPromptImage'];
  onReadVisualization: NonNullable<WorkbenchViewProps['onReadVisualization']>;
  assistantLabel?: string;
}) {
  const [forking, setForking] = useState(false);
  const forkInFlight = useRef(false);
  const hasNaturalLanguage = Boolean(message.body.trim());
  const isStreaming = message.status === 'running';
  const canCopyResponse = hasNaturalLanguage && responseActionEligible;
  const hasReasoning = Boolean(message.reasoning?.length);
  const hasOperationalContent = Boolean(
    hasReasoning || message.traces?.length || message.subagentRuns?.length,
  );
  const className = [
    'assistant-turn',
    hasNaturalLanguage ? 'assistant-turn--response' : 'assistant-turn--operational',
    hasNaturalLanguage && hasReasoning ? 'assistant-turn--response-with-reasoning' : '',
    startsAssistantRun ? 'assistant-turn--run-start' : '',
    joinsPreviousToolChain ? 'assistant-turn--tool-chain-before' : '',
    joinsNextToolChain ? 'assistant-turn--tool-chain-after' : '',
  ].filter(Boolean).join(' ');

  return (
    <article className={className}>
      {startsAssistantRun && <AssistantHeading message={message} response={hasNaturalLanguage} label={assistantLabel} />}

      {message.reasoning?.length ? (
        <ReasoningDisclosure blocks={message.reasoning} onNotify={onNotify} />
      ) : null}

      {hasNaturalLanguage && (
        <>
          <div className="assistant-copy">
            <div className={`assistant-markdown${isStreaming ? '' : ' assistant-markdown--pretty'}`}>
              <MarkdownBody body={message.body} onNotify={onNotify} />
            </div>
            {(canCopyResponse || message.forkEligible) && (
              <div className="response-actions">
                <button
                  onClick={() => {
                    void navigator.clipboard.writeText(message.body).then(
                      () => onNotify('已复制回复'),
                      () => onNotify('无法复制回复', '浏览器没有授予剪贴板权限'),
                    );
                  }}
                  aria-label="复制回复"
                ><Copy size={13} /></button>
                {message.forkEligible && (
                  <button aria-label="从此处分叉" title="从此处分叉" disabled={forking} aria-busy={forking}
                    onClick={() => {
                      if (forkInFlight.current) return;
                      forkInFlight.current = true;
                      setForking(true);
                      void onFork(message.id).finally(() => { forkInFlight.current = false; setForking(false); });
                    }}
                  >{forking ? <LoaderCircle size={13} /> : <GitFork size={13} />}</button>
                )}
                <time className="response-time">{message.time}</time>
              </div>
            )}
          </div>
        </>
      )}

      {!hasNaturalLanguage && !hasOperationalContent && message.status === 'running' && (
        <div className="assistant-progress"><i /> 正在处理…</div>
      )}

      {message.traces && <div className="execution-rail">{message.traces.map((trace) => <TraceCard key={`${artifactOwnerKey}:${trace.id}:${trace.resultEntryId ?? ''}`} trace={trace} skills={skills} mcpToolRefs={mcpToolRefs} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} onReadPromptImage={onReadPromptImage} />)}</div>}
      {message.subagentRuns?.length ? (
        <SubagentGroup runs={message.subagentRuns} focusTaskId={focusTaskId} focusTaskRevision={focusTaskRevision} focusTaskHighlighted={focusTaskHighlighted} skills={skills} mcpToolRefs={mcpToolRefs} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} onReadPromptImage={onReadPromptImage} onNotify={onNotify} />
      ) : null}
      {message.visualizations?.map((visualization) => (
        <VisualizationPanel
          key={`${artifactOwnerKey}:${message.id}:${visualization.ordinal}`}
          entryId={message.id}
          visualization={visualization}
          onRead={onReadVisualization}
        />
      ))}
    </article>
  );
}

function isCompleteAnswer(message: Message, taskFinalAnswerId?: string): boolean {
  // ROOT history already carries authoritative terminal-final eligibility.
  // Task conversations instead identify the message from their accepted result.
  return message.role === 'assistant' && message.assistantKind === 'terminal'
    && message.status !== 'running'
    && (message.forkEligible === true || message.id === taskFinalAnswerId);
}

function ConversationRun({ messages, renderMessage, focusRequest, completed, active, assistantLabel, taskFinalAnswerId }: {
  messages: Message[];
  renderMessage: (message: Message, startsRun: boolean) => ReactNode;
  focusRequest?: object | number;
  taskFinalAnswerId?: string;
  completed: boolean;
  active: boolean;
  assistantLabel: string;
}) {
  const answer = messages.find(message => isCompleteAnswer(message, taskFinalAnswerId));
  const complete = completed || Boolean(answer);
  const [disclosure, setDisclosure] = useState({
    complete, active, focusRequest, expanded: Boolean(focusRequest) || (active && !complete),
  });
  // A completed answer closes the live process once. Later rerenders preserve
  // the reader's choice; explicit navigation reveals its target again.
  if (disclosure.complete !== complete || disclosure.active !== active || disclosure.focusRequest !== focusRequest) {
    setDisclosure({ complete, active, focusRequest, expanded: disclosure.focusRequest !== focusRequest && Boolean(focusRequest)
      ? true : active && !complete });
  }
  const process = messages.flatMap((message) => {
    if (message !== answer) return [message];
    return message.reasoning?.length || message.traces?.length || message.subagentRuns?.length
      ? [{ ...message, id: `${message.id}:process`, body: '', forkEligible: false, visualizations: undefined }] : [];
  });
  const hasProcess = process.some((message) => message.role === 'assistant');
  if (!hasProcess) return renderMessage(answer!, true);
  return <section className="conversation-run" data-process-expanded={disclosure.expanded}>
    <AssistantHeading message={messages[0]} response={Boolean(messages[0].body.trim())} label={assistantLabel} />
    <button type="button" className="conversation-run__toggle"
      aria-expanded={disclosure.expanded}
      aria-label={disclosure.expanded ? '收起中间过程' : '展开中间过程'}
      onClick={() => setDisclosure({ ...disclosure, expanded: !disclosure.expanded })}>
      <span>{complete ? '处理过程' : '中间过程'}</span><ChevronRight size={13} />
    </button>
    {process.map((message) => <div key={message.id}
      className="conversation-run__step"
      aria-hidden={message.role === 'assistant' && !disclosure.expanded && !message.visualizations?.length}
      inert={message.role === 'assistant' && !disclosure.expanded && !message.visualizations?.length}>
      <div className="conversation-run__step-content">{renderMessage(message, false)}</div>
    </div>)}
    {answer && renderMessage({ ...answer, reasoning: undefined, traces: undefined, subagentRuns: undefined }, false)}
  </section>;
}

function findToolChainConnections(messages: Message[], contextCompactionIndex = -1): {
  before: ReadonlySet<string>;
  after: ReadonlySet<string>;
} {
  const before = new Set<string>();
  const after = new Set<string>();
  const startsWithTools = (message: Message) => message.role === 'assistant'
    && Boolean(message.traces?.length)
    && !message.body.trim()
    && !message.reasoning?.length;
  const endsWithTools = (message: Message) => message.role === 'assistant'
    && Boolean(message.traces?.length)
    && !message.subagentRuns?.length;

  for (let index = 1; index < messages.length; index += 1) {
    if (index === contextCompactionIndex) continue;
    const previous = messages[index - 1];
    const current = messages[index];
    if (!endsWithTools(previous) || !startsWithTools(current)) continue;
    after.add(previous.id);
    before.add(current.id);
  }
  return { before, after };
}

export function ConversationMessages({
  messages,
  skills = [],
  artifactOwnerKey,
  onReadToolArtifact,
  onNotify,
  onFork = async () => undefined,
  onReadPromptImage = unavailablePromptImage,
  onReadVisualization = unavailableVisualization,
  userLabel = '你',
  assistantLabel = 'Pulsara',
  taskFinalAnswerId,
  contextCompactionIndex = -1,
  isRunning = false,
  focusTaskId,
  focusTaskRevision = 0,
  focusTaskHighlighted = false,
  focusMemoryEntry,
}: {
  messages: Message[];
  skills?: SkillCapability[];
  artifactOwnerKey: string;
  onReadToolArtifact: (resultEntryId: string, offsetChars: number) => Promise<ToolArtifactPage>;
  onNotify: MarkdownNotify;
  onFork?: (entryId: string) => Promise<void>;
  onReadPromptImage?: WorkbenchViewProps['onReadPromptImage'];
  onReadVisualization?: WorkbenchViewProps['onReadVisualization'];
  userLabel?: string;
  assistantLabel?: string;
  taskFinalAnswerId?: string;
  contextCompactionIndex?: number;
  isRunning?: boolean;
  focusTaskId?: string;
  focusTaskRevision?: number;
  focusTaskHighlighted?: boolean;
  focusMemoryEntry?: WorkbenchViewProps['focusMemoryEntry'];
}) {
  const toolChainConnections = useMemo(
    () => findToolChainConnections(messages, contextCompactionIndex), [messages, contextCompactionIndex],
  );
  const mcpToolRefs = useMemo(() => buildMcpToolRefIndex(messages), [messages]);
  const renderMessage = (message: Message, startsRun: boolean) => (
    <div key={message.id} data-memory-entry={message.id} style={{ display: 'contents' }}>
      {message.role === 'user'
        ? <UserMessage message={message} label={userLabel} onReadPromptImage={onReadPromptImage} />
        : <AssistantMessage message={message} responseActionEligible={isCompleteAnswer(message, taskFinalAnswerId)} startsAssistantRun={startsRun}
            joinsPreviousToolChain={toolChainConnections.before.has(message.id)}
            joinsNextToolChain={toolChainConnections.after.has(message.id)}
            focusTaskId={focusTaskId} focusTaskRevision={focusTaskRevision}
            focusTaskHighlighted={focusTaskHighlighted} skills={skills} mcpToolRefs={mcpToolRefs}
            onNotify={onNotify} onFork={onFork} artifactOwnerKey={artifactOwnerKey}
            onReadToolArtifact={onReadToolArtifact} onReadPromptImage={onReadPromptImage}
            onReadVisualization={onReadVisualization}
            assistantLabel={assistantLabel} />}
    </div>
  );
  const completedTurns = new Set(messages.filter(message => isCompleteAnswer(message, taskFinalAnswerId)).map(message => message.turnId).filter(Boolean));
  const content: ReactNode[] = [];
  let run: Message[] = [];
  const flush = () => {
    if (!run.length) return;
    const focused = focusMemoryEntry && run.some(message => message.id === focusMemoryEntry.entryId)
      ? focusMemoryEntry
      : focusTaskHighlighted && run.some(message => message.subagentRuns?.some(task => task.id === focusTaskId))
        ? focusTaskRevision : undefined;
    content.push(<ConversationRun key={`${artifactOwnerKey}:${run[0].id}`} messages={run}
      renderMessage={renderMessage} focusRequest={focused} assistantLabel={assistantLabel} taskFinalAnswerId={taskFinalAnswerId}
      completed={Boolean(run[0].turnId && completedTurns.has(run[0].turnId))}
      active={isRunning && run[0].turnId === messages.at(-1)?.turnId} />);
    run = [];
  };
  messages.forEach((message, index) => {
    if (index === contextCompactionIndex) {
      flush();
      content.push(<ContextCompactionDivider key="compaction" />);
    }
    const newInput = message.role === 'user'
      && message.userKind !== 'steer' && message.userKind !== 'subagent-completion';
    if (newInput || (run.length && message.turnId && run[0].turnId !== message.turnId)) flush();
    if (message.role === 'user' && !run.length) content.push(renderMessage(message, false));
    else run.push(message);
    if (isCompleteAnswer(message, taskFinalAnswerId)) flush();
  });
  flush();
  if (contextCompactionIndex === messages.length) content.push(<ContextCompactionDivider key="compaction" />);
  return content;
}

function permissionPrompt(prompt: string): string {
  const toolName = prompt.match(/^Allow\s+(.+?)\??$/i)?.[1]?.toLowerCase() ?? '';
  if (toolName.includes('write') || toolName.includes('edit') || toolName.includes('patch')) {
    return 'Pulsara 想修改工作目录中的文件。是否允许本次操作？';
  }
  if (toolName.includes('read') || toolName.includes('search')) {
    return 'Pulsara 想读取或搜索工作目录中的内容。是否允许本次操作？';
  }
  if (toolName.includes('browser')) {
    return 'Pulsara 想操作本地浏览器。是否允许本次操作？';
  }
  if (toolName.includes('terminal') || toolName.includes('shell') || toolName.includes('exec')) {
    return 'Pulsara 想在工作目录中运行一条命令。是否允许本次操作？';
  }
  return 'Pulsara 需要执行一项可能产生改动的操作。是否允许本次操作？';
}

function InteractionCard({
  interaction,
  toolDecisionPending,
  onRead,
  onResolve,
  onNotify,
}: {
  interaction: RuntimeInteractionSummary;
  toolDecisionPending: boolean;
  onRead: WorkbenchViewProps['onReadInteraction'];
  onResolve: WorkbenchViewProps['onResolveInteraction'];
  onNotify: MarkdownNotify;
}) {
  const [content, setContent] = useState<RuntimeInteractionContent>();
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState(false);
  const [freeText, setFreeText] = useState('');
  const [revisionOpen, setRevisionOpen] = useState(false);
  const [feedback, setFeedback] = useState('');
  const interactionId = interaction.id;
  const interactionKind = interaction.kind;
  const [now, setNow] = useState(Date.now);
  const expiresAtUtc = interaction.kind === 'tool-confirmation' ? interaction.expiresAtUtc : '';
  const decisionInProgress = interaction.kind === 'tool-confirmation' && interaction.decisionInProgress;
  const expired = Boolean(expiresAtUtc) && Date.parse(expiresAtUtc) <= now;
  useEffect(() => {
    if (!expiresAtUtc) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [expiresAtUtc]);
  const toolBusy = busy || decisionInProgress || toolDecisionPending;
  const toolDisabled = toolBusy || expired || !expiresAtUtc;
  const secondsLeft = Math.max(0, Math.ceil((Date.parse(expiresAtUtc) - now) / 1000));
  const prompt = 'prompt' in interaction ? interaction.prompt : '';
  const optionsKey = 'options' in interaction ? interaction.options.join('\u0000') : '';
  const workflowId = 'workflowId' in interaction ? interaction.workflowId : '';
  const workflowRevision = 'workflowRevision' in interaction ? interaction.workflowRevision : 0;

  useEffect(() => {
    let current = true;
    const requested: RuntimeInteractionSummary = interactionKind === 'tool-confirmation' || interactionKind === 'capability-form'
      ? {
        id: interactionId,
        ...(interactionKind === 'tool-confirmation'
          ? { kind: 'tool-confirmation' as const, expiresAtUtc, decisionInProgress }
          : { kind: 'capability-form' as const }),
        prompt,
        options: optionsKey ? optionsKey.split('\u0000') : [],
      }
      : { id: interactionId, kind: interactionKind, workflowId, workflowRevision };
    void onRead(requested).then(
      (value) => {
        if (current) setContent(value);
      },
      () => {
        if (current) setLoadError('内容暂时无法读取，请等待更新后重试。');
      },
    );
    return () => { current = false; };
  }, [interactionId, interactionKind, onRead, optionsKey, prompt, workflowId, workflowRevision, expiresAtUtc, decisionInProgress]);

  const resolve = async (resolution: RuntimeInteractionResolution) => {
    if (busy || (resolution.kind === 'tool' && toolDisabled)) return;
    setBusy(true);
    const accepted = await onResolve(interaction, resolution);
    if (!accepted) setBusy(false);
  };

  return (
    <section className={`interaction-card interaction-card--${interaction.kind}`} aria-live="polite">
      <header className="interaction-card__header">
        <span className="interaction-card__icon" aria-hidden="true">
          {interaction.kind === 'tool-confirmation' ? <ShieldCheck size={15} /> : <FileText size={15} />}
        </span>
        <div>
          <span>{interaction.kind === 'capability-form' ? '需要你的配置' : interaction.kind === 'tool-confirmation' ? '需要你的确认' : interaction.kind === 'plan-question' ? '规划需要你的选择' : '方案已准备好'}</span>
          <small>{interaction.kind === 'tool-confirmation' ? '只决定这一次操作' : '确认后 Pulsara 会继续这次工作'}</small>
        </div>
      </header>

      {!content && !loadError && <div className="interaction-loading"><LoaderCircle size={14} /> 正在准备内容…</div>}
      {loadError && <p className="interaction-error">{loadError}</p>}
      {content?.kind === 'capability-form' && <CapabilityInteractionEditor form={content.form} onResolve={resolution => onResolve(interaction, resolution)} />}

      {content?.kind === 'tool-confirmation' && (
        <>
          <p className="interaction-question">{permissionPrompt(content.prompt)}</p>
          <div className="tool-confirmation-footer">
            <div className="tool-confirmation-status">
              {toolBusy ? <LoaderCircle size={15} className="is-spinning" /> : <Clock3 size={15} />}
              <div>
                <span>{toolBusy ? '正在处理确认' : expired ? '已到截止时间' : <span aria-live="off">剩余 {Math.floor(secondsLeft / 60)} 分 {secondsLeft % 60} 秒</span>}</span>
                <small>{toolBusy ? '决定结果确认后将继续更新' : expired ? '正在等待后台确认状态' : <time dateTime={expiresAtUtc} aria-live="off">截至 {new Date(expiresAtUtc).toLocaleTimeString('zh-CN')}</time>}</small>
              </div>
            </div>
            <div className="interaction-actions">
              <button disabled={toolDisabled} onClick={() => void resolve({ kind: 'tool', decision: 'deny' })}>拒绝</button>
              <button className="is-primary" disabled={toolDisabled} onClick={() => void resolve({ kind: 'tool', decision: 'allow' })}>
                {toolBusy ? <LoaderCircle size={13} /> : <Check size={13} />} 允许本次操作
              </button>
            </div>
          </div>
        </>
      )}

      {content?.kind === 'plan-question' && (
        <>
          <p className="interaction-question">{content.question}</p>
          <div className="plan-options">
            {content.options.map((option) => (
              <button
                key={option.ordinal}
                disabled={busy}
                onClick={() => void resolve({ kind: 'plan-question-option', optionOrdinal: option.ordinal })}
              >
                <span><strong>{option.label}</strong>{option.recommended && <em>建议</em>}</span>
                {option.description && <small>{option.description}</small>}
              </button>
            ))}
          </div>
          {content.allowFreeText && (
            <div className="interaction-text-answer">
              <textarea value={freeText} onChange={(event) => setFreeText(event.target.value)} placeholder="或者写下你的选择…" rows={2} />
              <button className="is-primary" disabled={busy || !freeText.trim()} onClick={() => void resolve({ kind: 'plan-question-text', text: freeText.trim() })}>提交回答</button>
            </div>
          )}
        </>
      )}

      {content?.kind === 'plan-draft' && (
        <>
          <div className="plan-draft-body"><MarkdownBody body={content.body} onNotify={onNotify} /></div>
          {revisionOpen ? (
            <div className="interaction-text-answer interaction-text-answer--revision">
              <textarea value={feedback} onChange={(event) => setFeedback(event.target.value)} placeholder="告诉 Pulsara 需要怎样修改方案…" rows={3} autoFocus />
              <div className="interaction-actions">
                <button disabled={busy} onClick={() => setRevisionOpen(false)}>返回</button>
                <button className="is-primary" disabled={busy || !feedback.trim()} onClick={() => void resolve({ kind: 'plan-draft', decision: 'revise', feedback })}>提交修改意见</button>
              </div>
            </div>
          ) : (
            <div className="interaction-actions interaction-actions--plan">
              <button disabled={busy} onClick={() => void resolve({ kind: 'plan-draft', decision: 'cancel' })}>取消规划</button>
              <button disabled={busy} onClick={() => setRevisionOpen(true)}>提出修改</button>
              <button className="is-primary" disabled={busy} onClick={() => void resolve({ kind: 'plan-draft', decision: 'approve' })}>
                {busy ? <LoaderCircle size={13} /> : <Check size={13} />} 批准并继续
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function ContextCompactionDivider({ inherited = false }: { inherited?: boolean }) {
  const label = inherited ? '已保留分叉点的有效上下文，压缩前记录请在原会话查看' : '上下文已压缩';
  return (
    <div
      className="context-compaction-divider"
      role="separator"
      aria-label={label}
    >
      <span className="context-compaction-divider__line" aria-hidden="true" />
      <span>{label}</span>
      <span className="context-compaction-divider__line" aria-hidden="true" />
    </div>
  );
}

function modelConnectionLabel(connection?: ModelConfigurationSummary): string {
  if (!connection) return '选择模型';
  const protocol = connection.wire_api === 'openai_responses' ? 'Responses' : 'Chat';
  return `${connection.route_name ?? connection.route_id} · ${connection.display_name ?? connection.model_id} · ${protocol}`;
}

function reasoningSelectionLabel(
  connection: ModelConfigurationSummary | undefined,
  selection: ReasoningSelectionPayload | null | undefined,
): string {
  if (!connection) return '推理不可用';
  if (selection?.kind === 'effort') return selection.value === null || selection.value === 'none' ? '推理关闭' : `推理 ${selection.value}`;
  if (selection?.kind === 'toggle') return selection.enabled ? '推理开启' : '推理关闭';
  if (selection?.kind === 'budget_tokens') return `推理 ${selection.tokens.toLocaleString('zh-CN')} tokens`;
  if (connection.reasoning.kind === 'fixed_on') return '推理固定开启';
  if (connection.reasoning.kind === 'provider_default') return '推理由提供方决定';
  return '无推理选项';
}

function AnimatedQueueItem({ children }: { children: ReactNode }) {
  return <div className="composer-queue__item"><div className="composer-queue__item-content">{children}</div></div>;
}

export function WorkbenchView({
  focusMemoryEntry,
  workspace,
  session,
  messages,
  contextCompaction,
  initialContextBase,
  onFork,
  todo,
  activePlanMode,
  isRunning,
  inspectorOpen,
  queuedCount,
  queuedPrompts,
  localSubmissions,
  queueActions,
  onQueueAction,
  onQueueActionHandled,
  runtimeStatus,
  runtimeError,
  modelConfigurations,
  modelCallBinding,
  interaction,
  toolDecisionPending = false,
  canControl,
  isObserver,
  permission,
  skills,
  focusTaskId,
  focusTaskRevision,
  focusTaskHighlighted,
  onReconnect,
  onTakeControl,
  onOpenSidebar,
  onNewSession,
  canCreateSession,
  onToggleInspector,
  onOpenModelSettings,
  onModelCallBindingChange,
  onSend,
  onStop,
  onCompact,
  onReopenRuntime,
  runtimeReopenBusy,
  onReadInteraction,
  onResolveInteraction,
  artifactOwnerKey,
  onReadToolArtifact,
  onReadPromptImage,
  onReadVisualization = unavailableVisualization,
  promptDraftStore: draftStore,
  onNotify,
  onPermissionChange,
}: WorkbenchViewProps) {
  useSyncExternalStore(draftStore.subscribe, draftStore.getVersion, draftStore.getVersion);
  const draft = draftStore.summary(session.id);
  const [planRequests, setPlanRequests] = useState<Record<string, boolean>>({});
  const requestPlan = planRequests[session.id] ?? false;
  const setRequestPlan = useCallback((value: boolean | ((current: boolean) => boolean)) => {
    setPlanRequests(current => ({ ...current, [session.id]: typeof value === 'function' ? value(current[session.id] ?? false) : value }));
  }, [session.id]);
  const [submitting, setSubmitting] = useState(false);
  const [welcomeState, setWelcomeState] = useState({ sessionId: session.id, started: false, options: false });
  if (welcomeState.sessionId !== session.id) {
    setWelcomeState({ sessionId: session.id, started: false, options: false });
  }
  const welcomeDeparture = useRef<{ sessionId: string; top: number } | null>(null);
  const [compacting, setCompacting] = useState(false);
  const [sessionActionsOpen, setSessionActionsOpen] = useState(false);
  const [permissionOpen, setPermissionOpen] = useState(false);
  const [skillOpen, setSkillOpen] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [modelBindingBusy, setModelBindingBusy] = useState(false);
  const [preparingQueueEdit, setPreparingQueueEdit] = useState<string>();
  const [atBottom, setAtBottom] = useState(true);
  const [jumpBottom, setJumpBottom] = useState(126);
  const followLatestRef = useRef(true);
  const locatingTaskRef = useRef(false);
  const workbenchRef = useRef<HTMLElement>(null);
  const sessionActionsRef = useRef<HTMLDivElement>(null);
  const sessionActionsTriggerRef = useRef<HTMLButtonElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const composerWrapRef = useRef<HTMLDivElement>(null);
  const composerEditorRef = useRef<HTMLDivElement>(null);
  const acceptedDraftLayout = useRef<{ sessionId: string; height: number; restoreFocus: boolean } | null>(null);
  const optionsTriggerRef = useRef<HTMLButtonElement>(null);
  const budgetInputRef = useRef<HTMLInputElement>(null);
  const handledQueueActions = useRef(new Set<string>());
  const queueClicks = useRef(new Set<string>());
  const activeQueueActions = queueActions.filter(item => item.sessionId === session.id);
  const pendingEditRestoration = activeQueueActions.find(item => item.kind === 'edit' && item.status === 'accepted' && !item.handled);
  // If another draft exists despite the reservation, keep both texts and let
  // the user finish/clear that draft. The accepted edit restores once empty.
  const editRestoreConflict = pendingEditRestoration && draft.hasContent;
  const editingQueue = preparingQueueEdit?.startsWith(`${session.id}:`) === true
    || activeQueueActions.some(item => item.kind === 'edit' && (
    ['submitting', 'unknown'].includes(item.status)
    || (item.status === 'accepted' && !item.handled && !draft.hasContent)
    ));
  const conversationSubmissions = localSubmissions.filter(item => item.displayAsMessage
    && item.deliveryMode === 'new-turn' && item.outcomeCode !== 'USER_REDIRECTED_TO_STEER');
  const conversationCommandIds = new Set(conversationSubmissions.map(item => item.commandId));
  const queuedDisplayCount = queuedCount - queuedPrompts.filter(item => conversationCommandIds.has(item.commandId)).length;
  const visibleQueue = queuedPrompts.filter(item => item.deliveryMode === 'new-turn'
    && !conversationCommandIds.has(item.commandId)
    && !activeQueueActions.some(action => action.status === 'accepted' && action.source.queueItemId === item.queueItemId));
  const pendingSteers = queuedPrompts.filter(item => item.deliveryMode === 'steer'
    && !messages.some(message => message.userKind === 'steer'
      && message.inputSource?.commandId === item.commandId && message.inputSource.queueItemId === item.queueItemId));
  for (const action of activeQueueActions) {
    const delivery = action.receipt?.promptDelivery;
    if (action.kind !== 'send' || action.status !== 'accepted' || !delivery
      || !['PENDING', 'CONSUMED'].includes(delivery.queueStatus)) continue;
    const consumed = messages.some(message => message.userKind === 'steer'
      && message.inputSource?.commandId === action.commandId
      && message.inputSource.queueItemId === delivery.queueItemId);
    if (consumed || pendingSteers.some(item => item.commandId === action.commandId && item.queueItemId === delivery.queueItemId)) continue;
    pendingSteers.push({ ...action.source, queueItemId: delivery.queueItemId,
      commandId: action.commandId, deliveryMode: 'steer', targetTurnId: action.targetTurnId,
      submittedAt: action.submittedAt });
  }
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
    for (const action of queueActions) {
      if (action.sessionId !== session.id || action.kind === 'send' || action.status !== 'accepted'
        || action.handled || handledQueueActions.current.has(action.commandId)) continue;
      if (action.kind === 'edit') {
        // Editing reserves the empty composer until the exact cancellation is
        // known. Never overwrite a draft, including one restored by another action.
        if (draft.hasContent || !action.restoredContent) continue;
        if (!draftStore.restoreIfEmpty(session.id, action.restoredContent)) continue;
        if (action.source.requestedPermission) onPermissionChange(action.source.requestedPermission);
        setRequestPlan(false);
      }
      handledQueueActions.current.add(action.commandId);
      onQueueActionHandled(action.commandId);
      requestAnimationFrame(() => {
        const next = queuedPrompts.filter(item => item.deliveryMode === 'new-turn'
          && item.sequence > action.source.sequence && visibleQueue.some(visible => visible.queueItemId === item.queueItemId))[0];
        const button = action.kind === 'delete' && next
          ? [...(composerWrapRef.current?.querySelectorAll<HTMLButtonElement>('button[data-delete-queue]') ?? [])]
            .find(item => item.dataset.deleteQueue === next.queueItemId)
          : undefined;
        if (button) button.focus();
        else {
          draftStore.focus(session.id, action.kind === 'edit' ? 'end' : undefined);
        }
      });
    }
    });
    return () => cancelAnimationFrame(frame);
  }, [draft.hasContent, draftStore, onPermissionChange, onQueueActionHandled,
    queueActions, queuedPrompts, session.id, setRequestPlan, visibleQueue]);

  const queueAction = async (item: QueuedPrompt, kind: QueuedPromptAction['kind']) => {
    if (!canControl || isObserver || editingQueue || queueClicks.current.has('composer-edit') || queueClicks.current.has(item.queueItemId)) return;
    if (kind === 'edit' && (draft.hasContent || submitting)) {
      onNotify('请先处理当前草稿', '当前草稿和排队输入都会保留。');
      return;
    }
    if (kind === 'edit') queueClicks.current.add('composer-edit');
    queueClicks.current.add(item.queueItemId);
    const preparationOwner = `${session.id}:${item.queueItemId}`;
    if (kind === 'edit') setPreparingQueueEdit(preparationOwner);
    try { await onQueueAction(item, kind); }
    finally {
      queueClicks.current.delete(item.queueItemId);
      if (kind === 'edit') {
        queueClicks.current.delete('composer-edit');
        setPreparingQueueEdit((current) => current === preparationOwner
          ? undefined
          : current);
      }
    }
  };
  // Keep one presentation and React identity while a local submission becomes
  // an accepted queue item; only the existing action availability changes.
  const queueCards = [
    ...visibleQueue.map(queued => ({ queued, local: undefined })),
    ...localSubmissions.filter(item => !conversationCommandIds.has(item.commandId) && item.outcomeCode !== 'USER_REDIRECTED_TO_STEER')
      .map(local => ({ queued: undefined, local })),
  ];
  const composerQueue = (queueCards.length > 0 || editRestoreConflict) && (
    <section className="composer-queue" aria-label="等待处理的输入">
      {editRestoreConflict && <AnimatedQueueItem key="edit-restore-conflict"><article data-command-id={pendingEditRestoration.commandId} className="is-local">
        <Pencil size={13} aria-hidden="true" />
        <PromptContentView
          content={pendingEditRestoration.restoredContent ?? pendingEditRestoration.source.content}
          variant="queue"
          onReadImage={onReadPromptImage}
        />
        <small role="status">已取消排队，原文等待恢复；请先处理当前草稿。</small>
      </article></AnimatedQueueItem>}
      {queueCards.map(({ queued, local }) => {
        const item = queued ?? local!;
        const action = queued && activeQueueActions.find(action => action.source.queueItemId === queued.queueItemId
          && ['submitting', 'unknown'].includes(action.status));
        const busy = Boolean(action) || Boolean(local && ['sending', 'synchronizing'].includes(local.status));
        const notice = action?.status === 'unknown' ? '正在核对操作状态'
          : local?.status === 'unknown' ? '提交状态未知'
            : local?.status === 'rejected' ? '队列已拒绝'
              : local?.status === 'cancelled' ? '队列已取消'
                : local?.status === 'consumed' && local.outcomeCode === 'TURN_INTERRUPTED' ? '已接收 · 执行已中断'
                  : undefined;
        return <AnimatedQueueItem key={item.commandId}><article
          data-queue-item-id={queued?.queueItemId} data-command-id={item.commandId} aria-busy={busy}>
          <CornerDownRight size={13} aria-hidden="true" />
          {local?.contentUnavailable || !item.content
            ? <p>正文未能在队列终止前完成读取。</p>
            : <PromptContentView content={item.content} variant="queue" onReadImage={onReadPromptImage} />}
          {!isObserver && <div className="composer-queue__actions">
            <button type="button" aria-label="发送" title="作为引导发送到当前任务" disabled={!queued || busy || editingQueue || !canControl || !isRunning}
              onClick={() => { if (queued) void queueAction(queued, 'send'); }}><CornerDownRight size={13} />发送</button>
            <button type="button" aria-label="编辑" title="取消排队并放回输入框" disabled={!queued || busy || editingQueue || !canControl}
              onClick={() => { if (queued) void queueAction(queued, 'edit'); }}><Pencil size={13} />编辑</button>
            <button type="button" aria-label="删除" title="取消这条排队输入" disabled={!queued || busy || editingQueue || !canControl}
              data-delete-queue={queued?.queueItemId} onClick={() => { if (queued) void queueAction(queued, 'delete'); }}><Trash2 size={13} />删除</button>
          </div>}
          {notice && <small className="composer-queue__notice" role="status"><span>{notice}</span>{local?.detail && <>：<span>{local.detail}</span></>}</small>}
        </article></AnimatedQueueItem>;
      })}
    </section>
  );
  const wordCount = draft.text.trim().length;
  const selectedModel = modelConfigurations.find((item) => item.id === modelCallBinding?.connection_id);
  const modelBindingMissing = Boolean(modelCallBinding && !selectedModel);
  const modelReady = Boolean(
    modelCallBinding
    && selectedModel?.status === 'ready'
    && (selectedModel.authentication === 'none' || selectedModel.credential_configured),
  );
  const welcome = Boolean(session.id && !isObserver && messages.length === 0
    && !isRunning && localSubmissions.length === 0 && queuedPrompts.length === 0
    && !interaction && initialContextBase?.base_kind !== 'SNAPSHOT' && !contextCompaction
    && session.status !== 'interrupted' && runtimeStatus === 'online'
    && !(welcomeState.sessionId === session.id && welcomeState.started));
  const composerOptionsVisible = !welcome || welcomeState.options
    || modelOpen || reasoningOpen || skillOpen || permissionOpen;
  const hasCompactionContext = messages.length > 0 || isRunning
    || initialContextBase?.base_kind === 'SNAPSHOT' || Boolean(contextCompaction);
  useEffect(() => {
    setSessionActionsOpen(false);
  }, [session.id]);
  useEffect(() => {
    if (!sessionActionsOpen) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !sessionActionsRef.current?.contains(event.target)) {
        setSessionActionsOpen(false);
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setSessionActionsOpen(false);
      sessionActionsTriggerRef.current?.focus();
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [sessionActionsOpen]);
  useLayoutEffect(() => {
    const departure = welcomeDeparture.current;
    if (welcome || !departure) return;
    welcomeDeparture.current = null;
    const composer = composerWrapRef.current;
    if (!composer || departure.sessionId !== session.id
      || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    const offset = departure.top - composer.getBoundingClientRect().top;
    const animation = composer.animate?.([
      { transform: `translateY(${offset}px)` }, { transform: 'translateY(0)' },
    ], { duration: 560, easing: 'cubic-bezier(.22, 1, .36, 1)' });
    return () => animation?.cancel();
  }, [welcome, session.id]);
  const lastMessageLength = messages.at(-1)?.body.length ?? 0;
  useEffect(() => {
    if (!optionsOpen && !modelOpen && !reasoningOpen && !skillOpen && !permissionOpen) return;
    const close = () => {
      setOptionsOpen(false); setModelOpen(false); setReasoningOpen(false);
      setSkillOpen(false); setPermissionOpen(false);
    };
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !composerWrapRef.current?.contains(event.target)) close();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      close();
      if (optionsOpen && optionsTriggerRef.current?.getClientRects().length) optionsTriggerRef.current.focus();
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [optionsOpen, modelOpen, reasoningOpen, skillOpen, permissionOpen]);
  const contextCompactionIndex = useMemo(() => {
    if (!contextCompaction) return -1;
    const nextCanonical = messages.findIndex((message) => (
      message.entrySequence !== undefined
      && message.entrySequence > contextCompaction.adoptedAfterEntrySequence
    ));
    if (nextCanonical >= 0) return nextCanonical;
    let lastCanonical = -1;
    messages.forEach((message, index) => {
      if (message.entrySequence !== undefined) lastCanonical = index;
    });
    return lastCanonical + 1;
  }, [contextCompaction, messages]);

  const locatedMemoryRequest = useRef<typeof focusMemoryEntry>(undefined);
  useEffect(() => {
    if (!focusMemoryEntry || locatedMemoryRequest.current === focusMemoryEntry || focusMemoryEntry.sessionId !== session.id) return;
    const target = [...(threadRef.current?.querySelectorAll<HTMLElement>('[data-memory-entry]') ?? [])]
      .find(element => element.dataset.memoryEntry === focusMemoryEntry.entryId)?.firstElementChild;
    if (!target) return;
    locatedMemoryRequest.current = focusMemoryEntry;
    followLatestRef.current = false;
    const frame = requestAnimationFrame(() => target.scrollIntoView({ block: 'center' }));
    return () => cancelAnimationFrame(frame);
  }, [focusMemoryEntry, session.id, messages]);

  const insertSkill = useCallback((name: string) => {
    if (editingQueue || queueClicks.current.has('composer-edit')) return;
    const marker = `$${name}`;
    if (new RegExp(`(^|\\s)\\$${name}(?=\\s|$)`).test(draft.text)) return;
    draftStore.insertText(session.id, `${marker} `, true);
    setSkillOpen(false);
    setOptionsOpen(false);
    window.requestAnimationFrame(() => draftStore.focus(session.id));
  }, [draft.text, draftStore, editingQueue, session.id]);

  const updateJumpPosition = useCallback(() => {
    const workbench = workbenchRef.current;
    const composer = composerWrapRef.current;
    if (!workbench || !composer) return;
    const workbenchBox = workbench.getBoundingClientRect();
    const obstacleTops = [composer.getBoundingClientRect().top];
    for (const element of composer.querySelectorAll<HTMLElement>('.todo-dock__trigger, .todo-dock__popover')) {
      obstacleTops.push(element.getBoundingClientRect().top);
    }
    const obstacleTop = Math.min(...obstacleTops);
    const requested = Math.ceil(workbenchBox.bottom - obstacleTop + 12);
    const maximum = Math.max(112, Math.floor(workbenchBox.height - 105));
    const next = Math.min(maximum, Math.max(112, requested));
    setJumpBottom((current) => current === next ? current : next);
  }, []);

  useEffect(() => {
    const composer = composerWrapRef.current;
    if (!composer) return;
    updateJumpPosition();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(updateJumpPosition);
    observer.observe(composer);
    return () => observer.disconnect();
  }, [updateJumpPosition]);

  useLayoutEffect(() => {
    const previous = acceptedDraftLayout.current;
    acceptedDraftLayout.current = null;
    const editor = composerEditorRef.current;
    if (!previous || previous.sessionId !== session.id || !editor) return;
    if (previous.restoreFocus) draftStore.focus(session.id);
    const height = editor.getBoundingClientRect().height;
    if (previous.height === height || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    // The accepted submission clears the editor below the queue. Animate that
    // layout change too, otherwise a stable queue card still jumps vertically.
    const animation = editor.animate?.([
      { height: `${previous.height}px`, overflow: 'hidden' },
      { height: `${height}px`, overflow: 'hidden' },
    ], { duration: 240, easing: 'cubic-bezier(.2, .7, .2, 1)' });
    return () => animation?.cancel();
  }, [draft.hasContent, draft.revision, draftStore, session.id]);

  const composerHint = useMemo(() => {
    if (!isRunning) return 'Enter 发送 · Shift Enter 换行';
    return 'Enter 排队下一轮 · Shift Enter 换行';
  }, [isRunning]);

  const submit = useCallback(async () => {
    if (!draft.hasContent || submitting || editingQueue) return;
    if (!modelReady) {
      if (welcome) {
        setWelcomeState(current => ({ ...current, options: true }));
      } else onNotify(
        modelBindingMissing ? '原模型配置已删除' : '请先选择模型配置',
        modelBindingMissing
          ? '请为这个会话显式选择另一条模型配置。'
          : '模型会固定到这个会话，直到你再次更改。',
      );
      setModelOpen(true);
      return;
    }
    setSubmitting(true);
    let snapshot;
    try {
      snapshot = await draftStore.capture(session.id);
    } catch (error) {
      setSubmitting(false);
      onNotify(
        '输入还未准备好',
        error instanceof Error ? error.message : '请稍后重试。',
      );
      return;
    }
    if (welcome && composerWrapRef.current) {
      welcomeDeparture.current = { sessionId: session.id, top: composerWrapRef.current.getBoundingClientRect().top };
      setWelcomeState({ sessionId: session.id, started: true, options: false });
    }
    let accepted = false;
    try {
      accepted = await onSend(
        snapshot.content,
        permission,
        requestPlan && !isRunning && !activePlanMode,
      );
    } finally {
      setSubmitting(false);
    }
    if (!accepted) return;
    const editorHeight = composerEditorRef.current?.getBoundingClientRect().height;
    acceptedDraftLayout.current = editorHeight === undefined ? null : {
      sessionId: session.id,
      height: editorHeight,
      restoreFocus: composerEditorRef.current?.contains(document.activeElement) === true,
    };
    if (!draftStore.clearIfSnapshot(session.id, snapshot)) acceptedDraftLayout.current = null;
    setRequestPlan(false);
    onPermissionChange('bypass-permissions');
  }, [activePlanMode, draft.hasContent, draftStore, editingQueue, isRunning,
    modelBindingMissing, modelReady, onNotify, onPermissionChange, onSend,
    permission, requestPlan, session.id, setRequestPlan, submitting, welcome]);

  const changeBinding = async (binding: ModelCallBindingPayload) => {
    if (modelBindingBusy) return;
    setModelBindingBusy(true);
    try {
      await onModelCallBindingChange(binding);
      setModelOpen(false);
      setReasoningOpen(false);
    } catch (error) {
      onNotify('模型选择未保存', error instanceof Error ? error.message : '请稍后重试。');
    } finally {
      setModelBindingBusy(false);
    }
  };

  useEffect(() => {
    if (!followLatestRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      const thread = threadRef.current;
      if (thread) thread.scrollTop = thread.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [atBottom, conversationSubmissions.length, interaction?.id, lastMessageLength, messages.length]);

  useEffect(() => {
    const workbench = workbenchRef.current;
    const composer = composerWrapRef.current;
    let frame = window.requestAnimationFrame(updateJumpPosition);
    const schedule = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(updateJumpPosition);
    };
    window.addEventListener('resize', schedule);
    if (typeof ResizeObserver === 'undefined' || !workbench || !composer) {
      return () => {
        window.removeEventListener('resize', schedule);
        window.cancelAnimationFrame(frame);
      };
    }
    const observer = new ResizeObserver(schedule);
    observer.observe(workbench);
    observer.observe(composer);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', schedule);
      window.cancelAnimationFrame(frame);
    };
  }, [interaction?.id, isObserver, queuedCount, todo?.id, updateJumpPosition]);

  useEffect(() => {
    const thread = threadRef.current;
    const column = thread?.querySelector('.thread-column');
    if (!thread || !column || !atBottom || typeof ResizeObserver === 'undefined') return undefined;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        if (followLatestRef.current) thread.scrollTop = thread.scrollHeight;
      });
    });
    observer.observe(column);
    return () => {
      observer.disconnect();
      window.cancelAnimationFrame(frame);
    };
  }, [atBottom]);

  useEffect(() => {
    if (!focusTaskHighlighted || !focusTaskId || focusTaskRevision < 1) return;
    followLatestRef.current = false;
    locatingTaskRef.current = true;
    let settleFrame = 0;
    let restoreFrame = 0;
    let taskHeader: HTMLButtonElement | null = null;
    const frame = window.requestAnimationFrame(() => {
      setAtBottom(false);
      const target = [...(threadRef.current?.querySelectorAll<HTMLElement>('[data-task-id]') ?? [])]
        .find((element) => element.dataset.taskId === focusTaskId);
      const thread = threadRef.current;
      if (!target || !thread) {
        locatingTaskRef.current = false;
        return;
      }
      settleFrame = window.requestAnimationFrame(() => {
        const previousScrollBehavior = thread.style.scrollBehavior;
        thread.style.scrollBehavior = 'auto';
        taskHeader = target.querySelector<HTMLButtonElement>('.subagent-run__header');
        if (taskHeader) taskHeader.focus({ preventScroll: true });
        const threadBox = thread.getBoundingClientRect();
        const targetBox = target.getBoundingClientRect();
        const desiredTop = Math.max(
          0,
          thread.scrollTop + targetBox.top - threadBox.top
            - Math.max(0, thread.clientHeight - targetBox.height) / 2,
        );
        thread.scrollTop = desiredTop;
        restoreFrame = window.requestAnimationFrame(() => {
          thread.style.scrollBehavior = previousScrollBehavior;
          locatingTaskRef.current = false;
        });
      });
    });
    return () => {
      window.cancelAnimationFrame(frame);
      window.cancelAnimationFrame(settleFrame);
      window.cancelAnimationFrame(restoreFrame);
      if (taskHeader && document.activeElement === taskHeader) taskHeader.blur();
      locatingTaskRef.current = false;
    };
  }, [focusTaskHighlighted, focusTaskId, focusTaskRevision]);

  return (
    <section className={`workbench${welcome ? ' is-welcome' : ''}`} aria-label="会话工作台" ref={workbenchRef}>
      <header className="topbar">
        <div className="session-title">
          <button className="mobile-menu-button" onClick={onOpenSidebar} aria-label="打开会话侧栏"><Menu size={17} /></button>
          <span className={`live-badge live-badge--${session.status}`}>{session.status === 'running' ? '进行中' : session.status === 'completed' ? '已完成' : session.status === 'waiting' ? '等待中' : session.status === 'interrupted' ? '已中断' : '草稿'}</span>
          <div><h2>{session.title}</h2><p>{workspace.kind === 'quick' ? '快速开始' : '指定目录'} · {workspace.path}</p></div>
        </div>
        <div className="topbar-actions">
          {queuedDisplayCount > 0 && <span className="queue-badge">{queuedDisplayCount} 条等待处理</span>}
          {isObserver && <span className="observer-badge"><Eye size={11} /> 旁观中</span>}
          <div className="popover-anchor session-actions" ref={sessionActionsRef}>
            <button
              ref={sessionActionsTriggerRef}
              className={`icon-button${sessionActionsOpen ? ' is-active' : ''}`}
              type="button"
              aria-label="更多会话操作"
              aria-expanded={sessionActionsOpen}
              disabled={!session.id || runtimeReopenBusy}
              onClick={() => setSessionActionsOpen((open) => !open)}
            >
              {runtimeReopenBusy ? <LoaderCircle size={15} className="session-actions__busy" /> : <MoreHorizontal size={16} />}
            </button>
            {sessionActionsOpen && <div className="menu-popover session-actions-menu" aria-label="会话操作">
              <button type="button" onClick={() => { setSessionActionsOpen(false); onReopenRuntime(); }}>
                <RotateCcw size={15} />
                <span><strong>重新载入当前会话运行时</strong><small>仅作用于当前会话；空闲时从已保存记录重建</small></span>
              </button>
            </div>}
          </div>
          {canControl && (
            <button
              className={`ghost-button${compacting ? ' is-compacting' : ''}`}
              disabled={compacting || !hasCompactionContext || runtimeStatus !== 'online'}
              title={!hasCompactionContext ? '开始对话后即可压缩上下文' : undefined}
              onClick={() => {
                setCompacting(true);
                void onCompact().finally(() => setCompacting(false));
              }}
            >
              {compacting ? <LoaderCircle size={12} /> : <RotateCcw size={12} />}
              {compacting ? '正在整理上下文' : '压缩上下文'}
            </button>
          )}
          <button className={`icon-button${inspectorOpen ? ' is-active' : ''}`} onClick={onToggleInspector} aria-label="切换检查器"><PanelRight size={15} /></button>
        </div>
      </header>

      <div
        className="thread-scroll"
        ref={threadRef}
        onScroll={(event) => {
          const element = event.currentTarget;
          if (locatingTaskRef.current) {
            setAtBottom(false);
            return;
          }
          const nextAtBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
          followLatestRef.current = nextAtBottom;
          setAtBottom(nextAtBottom);
        }}
      >
        <div className="thread-column">
          {runtimeStatus !== 'online' && (
            <div className={`runtime-banner runtime-banner--${runtimeStatus}`}>
              <span>{runtimeStatus === 'starting' ? '正在连接本地服务…' : runtimeStatus === 'reconnecting' ? '连接中断，正在重新连接…' : runtimeError ?? '本地服务未连接。'}</span>
              {(runtimeStatus === 'offline' || runtimeStatus === 'failed') && <button onClick={onReconnect}>重新连接</button>}
            </div>
          )}
          {!welcome && !session.id && messages.length === 0 && conversationSubmissions.length === 0 && runtimeStatus === 'online' && (
            <div className="conversation-empty">
              <Sparkles size={20} />
              <strong>{session.id ? '这个会话还没有消息' : '准备开始一次真实运行'}</strong>
              <span>{session.id ? '在下方输入目标，Pulsara 会立即开始处理。' : '新建会话后，任务进展和回复会持续显示在这里。'}</span>
            </div>
          )}
          {initialContextBase?.base_kind === 'SNAPSHOT' && <ContextCompactionDivider inherited />}
          <ConversationMessages messages={messages} skills={skills} artifactOwnerKey={artifactOwnerKey} isRunning={isRunning}
            onReadToolArtifact={onReadToolArtifact} onReadPromptImage={onReadPromptImage}
            onReadVisualization={onReadVisualization}
            onNotify={onNotify} onFork={onFork} contextCompactionIndex={contextCompactionIndex}
            focusTaskId={focusTaskId} focusTaskRevision={focusTaskRevision}
            focusTaskHighlighted={focusTaskHighlighted} focusMemoryEntry={focusMemoryEntry} />
          {session.status === 'interrupted' && !isRunning && (
            <p className="conversation-interruption" role="status">本轮回复已中断。</p>
          )}
          {conversationSubmissions.map(item => (
            <div key={item.commandId} data-pending-message={item.commandId}
              aria-busy={item.status === 'sending' || item.status === 'synchronizing'}>
              <UserMessage message={{ id: item.commandId, role: 'user', userKind: 'prompt', time: '',
                body: item.content ? promptContentTextProjection(item.content) : '消息内容暂时无法读取。' }}
                pendingContent={item.content} onReadPromptImage={onReadPromptImage}
                deliveryStatus={item.status === 'sending' ? '正在发送…'
                  : item.status === 'rejected' ? `发送失败${item.detail ? `：${item.detail}` : ''}`
                    : item.status === 'cancelled' ? '发送已取消'
                      : item.status === 'unknown' ? '发送状态待确认'
                        : item.status === 'consumed' ? item.outcomeCode === 'TURN_INTERRUPTED' ? '已接收 · 执行已中断' : '已接收'
                          : '正在开始…'} />
            </div>
          ))}
          {pendingSteers.map(item => (
            <div key={item.queueItemId} data-queue-item-id={item.queueItemId} data-action-command-id={item.commandId}
              aria-description="引导已提交，等待当前任务接收">
              <UserMessage message={{ id: item.queueItemId, turnId: item.targetTurnId,
                role: 'user', userKind: 'steer', body: promptContentTextProjection(item.content),
                promptContent: item.content, status: 'completed',
                time: item.submittedAt ? new Date(item.submittedAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '' }}
                onReadPromptImage={onReadPromptImage} />
            </div>
          ))}
          {canControl && interaction && (
            <InteractionCard
              key={`${interaction.id}:${'workflowRevision' in interaction ? interaction.workflowRevision : 'live'}`}
              interaction={interaction}
              toolDecisionPending={toolDecisionPending}
              onRead={onReadInteraction}
              onResolve={onResolveInteraction}
              onNotify={onNotify}
            />
          )}
        </div>
      </div>

      {!atBottom && (
        <button className="jump-to-bottom" style={{ bottom: `${jumpBottom}px` }} onClick={() => {
          followLatestRef.current = true;
          threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' });
        }}>
          <ArrowDown size={13} /> 回到最新
        </button>
      )}

      {!session.id ? (
        <div className="composer-wrap composer-wrap--session-required">
          <div className="composer-frame">
            <div className="composer composer--session-required">
              <div className="session-required-composer">
                <span className="session-required-composer__icon" aria-hidden="true"><Sparkles size={15} /></span>
                <span className="session-required-composer__copy">
                  <strong>创建或选择会话后开始</strong>
                  <small>模型、推理、规划和本轮权限都会跟随当前会话。</small>
                </span>
                <button className="secondary-action" type="button" disabled={!canCreateSession} onClick={onNewSession}>
                  <MessageSquarePlus size={13} /> 创建会话
                </button>
              </div>
            </div>
          </div>
          <p className="composer-note">当前没有活动会话</p>
        </div>
      ) : !isObserver ? <div className="composer-wrap" ref={composerWrapRef}>
        {composerQueue}
        <div className="composer-frame">
          <div className="welcome-heading" aria-hidden={!welcome}>
            <WelcomeTypewriter key={session.id} active={welcome} cycling={!draft.hasContent} />
          </div>
          <TodoDock
            key={todo?.id ?? 'no-todo'}
            todo={todo}
            interactionOpen={Boolean(interaction)}
            onLayoutChange={updateJumpPosition}
          />
          <div className={`composer${draft.hasContent ? ' has-content' : ''}`}>
          <div className="composer-editor" ref={composerEditorRef}>
            <Sparkles size={14} />
            <PromptComposer
              key={session.id}
              store={draftStore}
              sessionId={session.id}
              disabled={runtimeStatus !== 'online' || !session.id || editingQueue}
              placeholder={isRunning ? '输入下一轮任务…' : '让 Pulsara 处理复杂工作…'}
              onSubmit={() => void submit()}
              onNotify={onNotify}
            />
            {wordCount > 0 && <span className="draft-count">{wordCount}</span>}
            {welcome && <button type="button" className={`welcome-options-trigger${!modelReady && !composerOptionsVisible ? ' needs-selection' : ''}`} aria-label="输入选项"
              aria-expanded={composerOptionsVisible} onClick={() => setWelcomeState(current => ({ ...current, options: !composerOptionsVisible }))}>
              <SlidersHorizontal size={15} />
            </button>}
            <div className="composer-submit">
              {isRunning && !draft.hasContent ? (
                <button
                  className="send-button is-stop"
                  onClick={onStop}
                  aria-label="停止本轮运行"
                  title="停止主助手本轮生成和后续执行；已启动操作仍按各自规则收尾，子任务、排队输入和后台命令不会自动取消。"
                ><CircleStop size={15} /></button>
              ) : (
                <button className="send-button" onClick={() => void submit()} disabled={!draft.hasContent || submitting || runtimeStatus !== 'online' || !session.id || (!modelReady && !welcome)} aria-label={isRunning ? '排队发送' : '发送'}>
                  {isRunning ? <Play size={14} fill="currentColor" /> : <Send size={14} />}
                </button>
              )}
            </div>
          </div>
          <div className={`composer-drawer${composerOptionsVisible ? ' is-open' : ''}`} inert={!composerOptionsVisible} aria-hidden={!composerOptionsVisible}>
          <div className="composer-drawer__content">
          <div className="composer-actions">
            <div className="composer-controls">
              <div className="popover-anchor model-picker">
                <button className={`mode-chip model-chip${modelOpen ? ' is-active' : ''}${welcome && !modelReady && composerOptionsVisible && !modelOpen ? ' needs-selection' : ''}`} title={modelBindingMissing ? '模型配置已删除' : modelConnectionLabel(selectedModel)} onClick={() => { setModelOpen((value) => !value); setOptionsOpen(false); setReasoningOpen(false); setSkillOpen(false); setPermissionOpen(false); }} aria-expanded={modelOpen} disabled={modelBindingBusy}>
                  <Bot size={12} /><span className="model-chip__label">{modelBindingMissing ? '模型配置已删除' : modelConnectionLabel(selectedModel)}</span><ChevronDown size={10} />
                </button>
                {modelOpen && <div className="menu-popover model-menu">
                  <span className="menu-label">此会话的模型</span>
                  {modelConfigurations.length ? modelConfigurations.map((connection) => <button key={connection.id} className={connection.id === modelCallBinding?.connection_id ? 'is-selected' : ''} disabled={connection.status !== 'ready' || modelBindingBusy} onClick={() => void changeBinding({ connection_id: connection.id, reasoning: connection.default_reasoning ?? null })}>
                    <span><strong>{connection.route_name ?? connection.route_id} · {connection.display_name ?? connection.model_id}</strong><small>{connection.wire_api === 'openai_responses' ? 'Responses' : 'Chat Completions'} · {connection.authentication === 'none' ? '无需认证' : connection.credential_configured ? '密钥已配置' : '密钥未配置'}</small></span>
                    {connection.id === modelCallBinding?.connection_id && <Check size={13} />}
                  </button>) : <div className="model-menu__empty"><span>还没有模型配置。</span><button onClick={onOpenModelSettings}>前往设置添加</button></div>}
                  {modelConfigurations.length > 0 && <button className="model-menu__settings" onClick={onOpenModelSettings}>管理模型配置</button>}
                </div>}
              </div>
              <button ref={optionsTriggerRef} className={`mode-chip composer-options-trigger${optionsOpen ? ' is-active' : ''}`} aria-label="本轮选项" aria-expanded={optionsOpen} aria-controls="composer-options" onClick={() => { setOptionsOpen(value => !value); setModelOpen(false); setReasoningOpen(false); setSkillOpen(false); setPermissionOpen(false); }}>
                <SlidersHorizontal size={12} /><span>选项</span><small className={permission === 'bypass-permissions' ? 'is-danger' : ''}>{permissionLabels[permission]}</small>{((requestPlan && !isRunning) || activePlanMode) && <i title="已启用规划" />}<ChevronDown size={10} />
              </button>
              <div id="composer-options" className={`composer-secondary-controls${optionsOpen ? ' is-open' : ''}`}>
              <div className="popover-anchor">
                <button className={`mode-chip${reasoningOpen ? ' is-active' : ''}`} onClick={() => { setReasoningOpen((value) => !value); setModelOpen(false); setSkillOpen(false); setPermissionOpen(false); }} aria-expanded={reasoningOpen} disabled={!selectedModel || selectedModel.reasoning.kind !== 'selectable' || modelBindingBusy}>
                  <BrainCircuit size={12} /> {reasoningSelectionLabel(selectedModel, modelCallBinding?.reasoning)} {selectedModel?.reasoning.kind === 'selectable' && <ChevronDown size={10} />}
                </button>
                {reasoningOpen && selectedModel?.reasoning.kind === 'selectable' && modelCallBinding && <div className="menu-popover reasoning-menu">
                  {selectedModel.reasoning.effort && <><span className="menu-label">推理档位</span>{selectedModel.reasoning.effort.values.map((effort) => {
                    const selected = modelCallBinding.reasoning?.kind === 'effort' && modelCallBinding.reasoning.value === effort;
                    return <button key={effort ?? 'provider-none'} className={selected ? 'is-selected' : ''} onClick={() => void changeBinding({ connection_id: modelCallBinding.connection_id, reasoning: { kind: 'effort', value: effort } })}><span><strong>{effort === null || effort === 'none' ? '关闭' : effort}</strong></span>{selected && <Check size={13} />}</button>;
                  })}</>}
                  {selectedModel.reasoning.toggle && <><span className="menu-label">推理开关</span>{[true, false].map((enabled) => {
                    const selected = modelCallBinding.reasoning?.kind === 'toggle' && modelCallBinding.reasoning.enabled === enabled;
                    return <button key={enabled ? 'enabled' : 'disabled'} className={selected ? 'is-selected' : ''} onClick={() => void changeBinding({ connection_id: modelCallBinding.connection_id, reasoning: { kind: 'toggle', enabled } })}><span><strong>{enabled ? '开启' : '关闭'}</strong></span>{selected && <Check size={13} />}</button>;
                  })}</>}
                  {selectedModel.reasoning.budget_tokens?.minimum != null && selectedModel.reasoning.budget_tokens.maximum != null && <div className="reasoning-budget"><span className="menu-label">Token 预算</span><div><input ref={budgetInputRef} type="number" min={selectedModel.reasoning.budget_tokens.minimum} max={selectedModel.reasoning.budget_tokens.maximum} defaultValue={modelCallBinding.reasoning?.kind === 'budget_tokens' ? modelCallBinding.reasoning.tokens : Math.ceil((selectedModel.reasoning.budget_tokens.minimum + selectedModel.reasoning.budget_tokens.maximum) / 2)} /><button onClick={() => { const tokens = Number(budgetInputRef.current?.value); if (Number.isInteger(tokens)) void changeBinding({ connection_id: modelCallBinding.connection_id, reasoning: { kind: 'budget_tokens', tokens } }); }}>应用</button></div><small>{selectedModel.reasoning.budget_tokens.minimum.toLocaleString('zh-CN')}–{selectedModel.reasoning.budget_tokens.maximum.toLocaleString('zh-CN')}</small></div>}
                </div>}
              </div>
              {skills.length > 0 && (
                <div className="popover-anchor">
                  <button
                    className={`mode-chip${skillOpen ? ' is-active' : ''}`}
                    onClick={() => {
                      setSkillOpen((value) => !value);
                      setPermissionOpen(false);
                      setReasoningOpen(false);
                      setModelOpen(false);
                    }}
                    aria-expanded={skillOpen}
                    aria-label="选择技能"
                    disabled={submitting || editingQueue}
                  ><BookOpenText size={12} /> 技能 <ChevronDown size={10} /></button>
                  {skillOpen && (
                    <div className="menu-popover skill-menu skill-menu--composer">
                      <span className="menu-label">用于本轮</span>
                      <div className="skill-menu__list">
                        {skills.map((skill) => {
                          const selected = skill.configured || new RegExp(`(^|\\s)\\$${skill.name}(?=\\s|$)`).test(draft.text);
                          return (
                            <button key={`${skill.name}:${skill.location}`} className={selected ? 'is-selected' : ''} onClick={() => insertSkill(skill.name)} disabled={skill.configured || editingQueue}>
                              <span><strong>${skill.name}</strong><small>{skill.description}</small></span>
                              {selected && <Check size={13} />}
                            </button>
                          );
                        })}
                      </div>
                      <small className="skill-menu__note">技能名称会加入输入，由 Pulsara 在本轮读取。</small>
                    </div>
                  )}
                </div>
              )}
              <button
                className={`mode-chip${(requestPlan && !isRunning) || activePlanMode ? ' is-active' : ''}`}
                onClick={() => setRequestPlan((value) => !value)}
                disabled={isRunning || activePlanMode || submitting}
                aria-pressed={requestPlan && !isRunning}
                title={isRunning ? '当前运行结束后可为下一轮启用规划' : undefined}
              ><WandSparkles size={12} /> {activePlanMode ? '规划进行中' : requestPlan && !isRunning ? '本轮先规划' : '先规划'}</button>
              <div className="popover-anchor">
                <button className={`mode-chip permission-chip${permission === 'bypass-permissions' ? ' is-danger' : ''}`} onClick={() => {setPermissionOpen((value) => !value); setReasoningOpen(false); setSkillOpen(false); setModelOpen(false);}} aria-expanded={permissionOpen} disabled={submitting}>
                  {permission === 'bypass-permissions' ? <TriangleAlert size={12} /> : <ShieldCheck size={12} />} {permissionLabels[permission]} <ChevronDown size={10} />
                </button>
                {permissionOpen && (
                  <div className="menu-popover permission-menu permission-menu--composer">
                    <span className="menu-label">本轮权限</span>
                    {permissionModeOrder.map((mode) => (
                      <button key={mode} className={`${mode === permission ? 'is-selected' : ''}${mode === 'bypass-permissions' ? ' permission-option--danger' : ''}`} onClick={() => { onPermissionChange(mode); setPermissionOpen(false); }}>
                        <span><strong className="permission-option-title">{permissionLabels[mode]}{mode === 'bypass-permissions' && <TriangleAlert className="permission-warning-icon" size={13} aria-hidden="true" />}</strong><small>{mode === 'accept-edits' ? '工作区编辑直接执行；其他操作询问' : mode === 'read-only' ? '只观察和读取，不做改动' : mode === 'ask-permissions' ? '每次有副作用的操作都询问' : '跳过询问，可操作本机'}</small></span>
                        {mode === permission && <Check size={13} />}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              </div>
            </div>

          </div>
          </div>
          </div>
          </div>
        </div>
        {!welcome && <p className={`composer-note${!modelReady ? ' composer-note--attention' : ''}`}>{!modelReady ? (modelBindingMissing ? '原模型配置已删除，请重新选择' : '请先为此会话选择模型配置') : composerHint} · 规划与权限只作用于本轮</p>}
      </div> : (
        <div className="observer-wrap composer-wrap">
          {composerQueue}
          <div className="observer-dock">
            <span className="observer-dock__icon"><Eye size={15} /></span>
            <span><strong>这个会话正在另一个窗口中操作</strong><small>这里仍会实时显示对话、思考和任务进展。</small></span>
            <button type="button" onClick={onTakeControl}>在此窗口继续</button>
          </div>
        </div>
      )}
    </section>
  );
}
