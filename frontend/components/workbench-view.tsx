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
  PanelRight,
  Play,
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
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  LocalPromptSubmission,
  ToolArtifactPage,
} from '../lib/runtime-adapter';
import type { Message, PermissionMode, ReasoningBlock, RuntimeStatus, SessionSummary, SkillCapability, SubagentRun, TodoRun, ToolTrace, Workspace } from '../lib/pulsara-types';
import { permissionLabels, permissionModeOrder } from '../lib/pulsara-types';
import { MarkdownBody, MarkdownInline } from './markdown-body';

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
  runtimeStatus: RuntimeStatus;
  runtimeError?: string;
  modelConfigurations: ModelConfigurationSummary[];
  modelCallBinding?: ModelCallBindingPayload | null;
  interaction?: RuntimeInteractionSummary;
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
  onToggleInspector: () => void;
  onOpenModelSettings: () => void;
  onModelCallBindingChange: (binding: ModelCallBindingPayload) => Promise<void>;
  onSend: (
    text: string,
    steer: boolean,
    permission: PermissionMode,
    requestPlan: boolean,
  ) => Promise<boolean>;
  onStop: () => void;
  onCompact: () => Promise<void>;
  onReadInteraction: (
    interaction: RuntimeInteractionSummary,
  ) => Promise<RuntimeInteractionContent>;
  onResolveInteraction: (
    interaction: RuntimeInteractionSummary,
    resolution: RuntimeInteractionResolution,
  ) => Promise<boolean>;
  artifactOwnerKey: string;
  onReadToolArtifact: (resultEntryId: string, offsetChars: number) => Promise<ToolArtifactPage>;
  onNotify: (title: string, detail?: string) => void;
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
}: {
  trace: ToolTrace;
  skills: SkillCapability[];
  mcpToolRefs: ReadonlyMap<string, McpToolIdentity>;
  artifactOwnerKey: string;
  onReadToolArtifact: WorkbenchViewProps['onReadToolArtifact'];
}) {
  const [expanded, setExpanded] = useState(false);
  const [artifactPage, setArtifactPage] = useState<ToolArtifactPage>();
  const [artifactBusy, setArtifactBusy] = useState(false);
  const [artifactError, setArtifactError] = useState('');
  const artifactRequestRevision = useRef(0);
  const artifactOwnerKeyRef = useRef(artifactOwnerKey);
  const artifactResultEntryIdRef = useRef(trace.resultEntryId);
  useEffect(() => {
    artifactRequestRevision.current += 1;
    return () => { artifactRequestRevision.current += 1; };
  }, []);
  const Icon = traceIcons[trace.kind];
  const skill = traceSkill(trace, skills);
  const mcpDetail = mcpTraceDetail(trace, mcpToolRefs);
  const purpose = skill ? `正在使用 ${skill.name} Skill` : trace.title;
  const subtitle = mcpDetail?.subtitle ?? trace.subtitle;
  const hasRawResult = Object.prototype.hasOwnProperty.call(trace, 'resultText');
  const parsedResult = parseJsonObject(trace.resultText);
  const diffText = trace.toolName === 'edit_file' ? stringValue(parsedResult?.diff) : '';
  const canReadArtifact = Boolean(
    trace.resultEntryId
    && (trace.artifact?.disposition === 'AVAILABLE' || trace.artifact?.disposition === 'INCOMPLETE'),
  );
  const expandable = Boolean(trace.command || hasRawResult || mcpDetail || canReadArtifact);
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
          <strong>{trace.toolName ?? trace.title}</strong>
          {trace.toolName && purpose !== trace.toolName
            ? <span className="trace-purpose">{purpose}</span>
            : null}
        </span>
        <small>{subtitle}</small>
      </span>
      <span className={`trace-state trace-state--${trace.status}`}>
        {trace.status === 'running' ? `进行中 · ${trace.duration ?? ''}` : trace.duration ?? trace.meta}
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
              setExpanded((value) => !value);
            }}
            aria-expanded={expanded}
            aria-label={`${expanded ? '收起' : '展开'}工具详情：${trace.toolName ?? trace.title}`}
          >{summary}</button>
        ) : <div className="trace-card__summary">{summary}</div>}
        {expanded && (
          <div className="terminal-output">
            {trace.command && <div className="terminal-command"><span>$</span> {trace.command}</div>}
            {mcpDetail && <McpTraceDetails detail={mcpDetail} />}
            {diffText
              ? <pre className="tool-result-diff tool-output-scroll" aria-label="文件差异">{diffText}</pre>
              : trace.resultSummary && <div className="tool-result-summary">{trace.resultSummary}</div>}
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
                          onClick={() => readArtifactPage(artifactPage.nextOffsetChars)}
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

function ReasoningRow({ block }: { block: ReasoningBlock }) {
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
          {!expanded && <small><MarkdownInline body={reasoningPreview(block.body, running)} /></small>}
        </span>
        {running && <span className="reasoning-row__live"><i />思考中</span>}
        <ChevronRight className="reasoning-row__chevron" size={12} />
      </button>
      {expanded && <div className="reasoning-row__body assistant-markdown"><MarkdownBody body={block.body} /></div>}
    </section>
  );
}

function ReasoningDisclosure({
  blocks,
  nested = false,
}: {
  blocks: ReasoningBlock[];
  nested?: boolean;
}) {
  return (
    <div className={`reasoning-disclosure${nested ? ' reasoning-disclosure--nested' : ''}`}>
      {blocks.map((block) => <ReasoningRow key={block.id} block={block} />)}
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
}: {
  run: SubagentRun;
  focused: boolean;
  skills: SkillCapability[];
  mcpToolRefs: ReadonlyMap<string, McpToolIdentity>;
  artifactOwnerKey: string;
  onReadToolArtifact: WorkbenchViewProps['onReadToolArtifact'];
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
      {expanded && (
        <div className="subagent-run__body">
          {run.objective && <div className="subagent-objective"><span>目标</span><p>{run.objective}</p></div>}
          {run.activities.map((activity) => (
            <section className="subagent-activity" key={activity.id}>
              {activity.reasoning?.length ? (
                <ReasoningDisclosure
                  blocks={activity.reasoning}
                  nested
                />
              ) : null}
              {activity.body && (activity.kind === 'guidance' ? (
                <div className="subagent-guidance">
                  <span><CornerDownRight size={11} /> 主任务补充</span>
                  <div className="assistant-markdown"><MarkdownBody body={activity.body} /></div>
                </div>
              ) : <div className="assistant-markdown"><MarkdownBody body={activity.body} /></div>)}
              {activity.traces?.length ? <div className="execution-rail subagent-execution">{activity.traces.map((trace) => <TraceCard key={`${artifactOwnerKey}:${trace.id}:${trace.resultEntryId ?? ''}`} trace={trace} skills={skills} mcpToolRefs={mcpToolRefs} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} />)}</div> : null}
            </section>
          ))}
          {showSummary && run.summary && (
            <section className="subagent-summary"><span>结果</span><div className="assistant-markdown"><MarkdownBody body={run.summary} /></div></section>
          )}
        </div>
      )}
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
}: {
  runs: SubagentRun[];
  focusTaskId?: string;
  focusTaskRevision: number;
  focusTaskHighlighted: boolean;
  skills: SkillCapability[];
  mcpToolRefs: ReadonlyMap<string, McpToolIdentity>;
  artifactOwnerKey: string;
  onReadToolArtifact: WorkbenchViewProps['onReadToolArtifact'];
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
        />
      ))}</div>
    </section>
  );
}

function UserMessage({ message }: { message: Message }) {
  const sourceTextStyle = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } as const;
  if (message.userKind === 'subagent-completion') {
    const helpId = `${message.id}-subagent-completion-help`;
    return (
      <article
        className="subagent-completion-event"
        aria-label="Pulsara 已收到子任务进展"
        aria-describedby={helpId}
        tabIndex={0}
      >
        <span className="subagent-completion-event__icon"><GitFork size={13} /></span>
        <div className="subagent-completion-event__copy">
          <strong>Pulsara 已收到子任务进展</strong>
          <small>主任务会结合这项工作的结果继续处理</small>
        </div>
        <time>{message.time}</time>
        <span id={helpId} className="subagent-completion-event__tooltip" role="tooltip">
          Pulsara 已把这项工作的进展用于当前处理；这不是你发送的新消息，也不会重新运行子任务。
        </span>
      </article>
    );
  }

  if (message.userKind === 'steer') {
    return (
      <article className="user-steer" aria-label="你的引导">
        <span className="user-steer__icon"><CornerDownRight size={13} /></span>
        <div className="user-steer__content">
          <header><strong>你 · 引导</strong><time>{message.time}</time></header>
          <p style={sourceTextStyle}>{message.body}</p>
        </div>
      </article>
    );
  }

  return (
    <article className="user-turn">
      <header className="user-heading">
        <strong>你</strong>
        <span className="user-avatar"><UserRound size={14} /></span>
      </header>
      <div className="user-message">
        <p style={sourceTextStyle}>{message.body}</p>
        <div className="message-foot"><time>{message.time}</time></div>
      </div>
    </article>
  );
}

function AssistantHeading({ message, response }: { message: Message; response: boolean }) {
  return (
    <header className={`assistant-heading${response ? ' assistant-heading--response' : ' assistant-heading--run-start'}`}>
      <div className="assistant-avatar"><span /></div>
      <div className="assistant-identity">
        <strong>Pulsara</strong>
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
}: {
  message: Message;
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
}) {
  const [forking, setForking] = useState(false);
  const forkInFlight = useRef(false);
  const hasNaturalLanguage = Boolean(message.body.trim());
  const canCopyResponse = hasNaturalLanguage
    && message.assistantKind === 'terminal'
    && message.status !== 'running';
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
      {startsAssistantRun && <AssistantHeading message={message} response={hasNaturalLanguage} />}

      {message.reasoning?.length ? (
        <ReasoningDisclosure blocks={message.reasoning} />
      ) : null}

      {hasNaturalLanguage && (
        <>
          {!startsAssistantRun && <AssistantHeading message={message} response />}
          <div className="assistant-copy">
            <div className="assistant-markdown"><MarkdownBody body={message.body} /></div>
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

      {message.traces && <div className="execution-rail">{message.traces.map((trace) => <TraceCard key={`${artifactOwnerKey}:${trace.id}:${trace.resultEntryId ?? ''}`} trace={trace} skills={skills} mcpToolRefs={mcpToolRefs} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} />)}</div>}
      {message.subagentRuns?.length ? (
        <SubagentGroup runs={message.subagentRuns} focusTaskId={focusTaskId} focusTaskRevision={focusTaskRevision} focusTaskHighlighted={focusTaskHighlighted} skills={skills} mcpToolRefs={mcpToolRefs} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} />
      ) : null}
    </article>
  );
}

function findAssistantRunStarts(
  messages: Message[],
  contextCompactionIndex = -1,
): ReadonlySet<string> {
  const starts = new Set<string>();
  let assistantRunOpen = false;
  for (const [index, message] of messages.entries()) {
    if (index === contextCompactionIndex) assistantRunOpen = false;
    if (message.role === 'user') {
      if (message.userKind !== 'steer') assistantRunOpen = false;
      continue;
    }
    if (!assistantRunOpen) starts.add(message.id);
    assistantRunOpen = true;
  }
  return starts;
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
  onRead,
  onResolve,
}: {
  interaction: RuntimeInteractionSummary;
  onRead: WorkbenchViewProps['onReadInteraction'];
  onResolve: WorkbenchViewProps['onResolveInteraction'];
}) {
  const [content, setContent] = useState<RuntimeInteractionContent>();
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState(false);
  const [freeText, setFreeText] = useState('');
  const [revisionOpen, setRevisionOpen] = useState(false);
  const [feedback, setFeedback] = useState('');
  const interactionId = interaction.id;
  const interactionKind = interaction.kind;
  const prompt = 'prompt' in interaction ? interaction.prompt : '';
  const optionsKey = 'options' in interaction ? interaction.options.join('\u0000') : '';
  const workflowId = 'workflowId' in interaction ? interaction.workflowId : '';
  const workflowRevision = 'workflowRevision' in interaction ? interaction.workflowRevision : 0;

  useEffect(() => {
    let current = true;
    const requested: RuntimeInteractionSummary = interactionKind === 'tool-confirmation' || interactionKind === 'capability-form'
      ? {
        id: interactionId,
        kind: interactionKind,
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
  }, [interactionId, interactionKind, onRead, optionsKey, prompt, workflowId, workflowRevision]);

  const resolve = async (resolution: RuntimeInteractionResolution) => {
    if (busy) return;
    setBusy(true);
    const accepted = await onResolve(interaction, resolution);
    if (!accepted) setBusy(false);
  };

  return (
    <section className={`interaction-card interaction-card--${interaction.kind}`} aria-live="polite">
      <header className="interaction-card__header">
        <span className="interaction-card__icon">
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
          <div className="interaction-actions">
            <button disabled={busy} onClick={() => void resolve({ kind: 'tool', decision: 'deny' })}>拒绝</button>
            <button className="is-primary" disabled={busy} onClick={() => void resolve({ kind: 'tool', decision: 'allow' })}>
              {busy ? <LoaderCircle size={13} /> : <Check size={13} />} 允许本次操作
            </button>
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
          <div className="plan-draft-body"><MarkdownBody body={content.body} /></div>
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
  runtimeStatus,
  runtimeError,
  modelConfigurations,
  modelCallBinding,
  interaction,
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
  onToggleInspector,
  onOpenModelSettings,
  onModelCallBindingChange,
  onSend,
  onStop,
  onCompact,
  onReadInteraction,
  onResolveInteraction,
  artifactOwnerKey,
  onReadToolArtifact,
  onNotify,
  onPermissionChange,
}: WorkbenchViewProps) {
  // Drafts are window-local and session-owned, never imported Fork material.
  // Keep the source draft available when returning from a newly opened child.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [planRequests, setPlanRequests] = useState<Record<string, boolean>>({});
  const draft = drafts[session.id] ?? '';
  const requestPlan = planRequests[session.id] ?? false;
  const setDraft = useCallback((value: string | ((current: string) => string)) => {
    setDrafts(current => ({ ...current, [session.id]: typeof value === 'function' ? value(current[session.id] ?? '') : value }));
  }, [session.id]);
  const setRequestPlan = useCallback((value: boolean | ((current: boolean) => boolean)) => {
    setPlanRequests(current => ({ ...current, [session.id]: typeof value === 'function' ? value(current[session.id] ?? false) : value }));
  }, [session.id]);
  const [submitting, setSubmitting] = useState(false);
  const [compacting, setCompacting] = useState(false);
  const [permissionOpen, setPermissionOpen] = useState(false);
  const [skillOpen, setSkillOpen] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [modelBindingBusy, setModelBindingBusy] = useState(false);
  const [atBottom, setAtBottom] = useState(true);
  const [jumpBottom, setJumpBottom] = useState(126);
  const followLatestRef = useRef(true);
  const locatingTaskRef = useRef(false);
  const workbenchRef = useRef<HTMLElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const composerWrapRef = useRef<HTMLDivElement>(null);
  const composerInputRef = useRef<HTMLTextAreaElement>(null);
  const optionsTriggerRef = useRef<HTMLButtonElement>(null);
  const budgetInputRef = useRef<HTMLInputElement>(null);
  const composerComposingRef = useRef(false);
  const wordCount = draft.trim().length;
  const selectedModel = modelConfigurations.find((item) => item.id === modelCallBinding?.connection_id);
  const modelBindingMissing = Boolean(modelCallBinding && !selectedModel);
  const modelReady = Boolean(
    modelCallBinding
    && selectedModel?.status === 'ready'
    && (selectedModel.authentication === 'none' || selectedModel.credential_configured),
  );
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
  const assistantRunStarts = useMemo(
    () => findAssistantRunStarts(messages, contextCompactionIndex),
    [contextCompactionIndex, messages],
  );
  const toolChainConnections = useMemo(
    () => findToolChainConnections(messages, contextCompactionIndex),
    [contextCompactionIndex, messages],
  );
  const mcpToolRefs = useMemo(() => buildMcpToolRefIndex(messages), [messages]);

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
    const marker = `$${name}`;
    setDraft((current) => {
      const alreadySelected = new RegExp(`(^|\\s)\\$${name}(?=\\s|$)`).test(current);
      if (alreadySelected) return current;
      return current.trim() ? `${marker} ${current}` : `${marker} `;
    });
    setSkillOpen(false);
    setOptionsOpen(false);
    window.requestAnimationFrame(() => composerInputRef.current?.focus());
  }, [setDraft]);

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

  const composerHint = useMemo(() => {
    if (!isRunning) return 'Enter 发送 · Shift Enter 换行';
    return 'Enter 排队下一轮 · ⌘ Enter 引导当前任务';
  }, [isRunning]);

  const submit = async (steer: boolean) => {
    const value = draft.trim();
    if (!value || submitting) return;
    if (!steer && !modelReady) {
      onNotify(
        modelBindingMissing ? '原模型配置已删除' : '请先选择模型配置',
        modelBindingMissing
          ? '请为这个会话显式选择另一条模型配置。'
          : '模型会固定到这个会话，直到你再次更改。',
      );
      setModelOpen(true);
      return;
    }
    setSubmitting(true);
    const accepted = await onSend(
      value,
      steer,
      permission,
      !steer && requestPlan && !isRunning && !activePlanMode,
    );
    setSubmitting(false);
    if (!accepted) return;
    setDraft((current) => current.trim() === value ? '' : current);
    setRequestPlan(false);
    onPermissionChange('bypass-permissions');
  };

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
  }, [atBottom, interaction?.id, lastMessageLength, messages.length]);

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
    <section className="workbench" aria-label="会话工作台" ref={workbenchRef}>
      <header className="topbar">
        <div className="session-title">
          <button className="mobile-menu-button" onClick={onOpenSidebar} aria-label="打开会话侧栏"><Menu size={17} /></button>
          <span className={`live-badge live-badge--${session.status}`}>{session.status === 'running' ? '进行中' : session.status === 'completed' ? '已完成' : session.status === 'waiting' ? '等待中' : session.status === 'interrupted' ? '已中断' : '草稿'}</span>
          <div><h2>{session.title}</h2><p>{workspace.kind === 'quick' ? '快速开始' : '指定目录'} · {workspace.path}</p></div>
        </div>
        <div className="topbar-actions">
          {queuedCount > 0 && <span className="queue-badge">{queuedCount} 条等待处理</span>}
          {isObserver && <span className="observer-badge"><Eye size={11} /> 旁观中</span>}
          {canControl && (
            <button
              className={`ghost-button${compacting ? ' is-compacting' : ''}`}
              disabled={compacting}
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
          {session.status === 'interrupted' && runtimeStatus === 'online' && (
            <div className="runtime-banner" role="status">
              本轮执行已中断，未正常完成。你可以发送新消息继续。
            </div>
          )}
          {runtimeStatus !== 'online' && (
            <div className={`runtime-banner runtime-banner--${runtimeStatus}`}>
              <span>{runtimeStatus === 'starting' ? '正在连接本地服务…' : runtimeStatus === 'reconnecting' ? '连接中断，正在重新连接…' : runtimeError ?? '本地服务未连接。'}</span>
              {(runtimeStatus === 'offline' || runtimeStatus === 'failed') && <button onClick={onReconnect}>重新连接</button>}
            </div>
          )}
          {messages.length === 0 && runtimeStatus === 'online' && (
            <div className="conversation-empty">
              <Sparkles size={20} />
              <strong>{session.id ? '这个会话还没有消息' : '准备开始一次真实运行'}</strong>
              <span>{session.id ? '在下方输入目标，Pulsara 会立即开始处理。' : '新建会话后，任务进展和回复会持续显示在这里。'}</span>
            </div>
          )}
          {initialContextBase?.base_kind === 'SNAPSHOT' && <ContextCompactionDivider inherited />}
          {messages.map((message, index) => (
            <div key={message.id} data-memory-entry={message.id} style={{ display: 'contents' }}>
              {contextCompactionIndex === index && contextCompaction && (
                <ContextCompactionDivider />
              )}
              {message.role === 'user'
                ? <UserMessage message={message} />
                : (
                  <AssistantMessage
                    message={message}
                    startsAssistantRun={assistantRunStarts.has(message.id)}
                    joinsPreviousToolChain={toolChainConnections.before.has(message.id)}
                    joinsNextToolChain={toolChainConnections.after.has(message.id)}
                    focusTaskId={focusTaskId}
                    focusTaskRevision={focusTaskRevision}
                    focusTaskHighlighted={focusTaskHighlighted}
                    skills={skills}
                    mcpToolRefs={mcpToolRefs}
                    onNotify={onNotify}
                    onFork={onFork}
                    artifactOwnerKey={artifactOwnerKey}
                    onReadToolArtifact={onReadToolArtifact}
                  />
                )}
            </div>
          ))}
          {contextCompactionIndex === messages.length && contextCompaction && (
            <ContextCompactionDivider />
          )}
          {(queuedPrompts.length > 0 || localSubmissions.length > 0) && (
            <section className="prompt-queue" aria-label="等待处理的输入">
              <header><MessageSquarePlus size={13} /><strong>等待处理的输入</strong></header>
              {queuedPrompts.map((item) => (
                <article key={item.queueItemId} data-queue-item-id={item.queueItemId}>
                  <div className="prompt-queue__meta">
                    <span>{item.deliveryMode === 'steer' ? '补充当前任务' : '下一轮任务'}</span>
                    {item.deliveryMode === 'steer' && (
                      <span>目标轮次：{item.targetTurnId ?? '未知'}</span>
                    )}
                    <span>适用权限：{item.permission ? permissionLabels[item.permission] : '未知'}</span>
                  </div>
                  <pre>{item.body}</pre>
                </article>
              ))}
              {localSubmissions.map((item) => (
                <article key={item.commandId} data-command-id={item.commandId} className="is-local">
                  <div className="prompt-queue__meta">
                    <span>{item.status === 'sending'
                      ? '正在发送'
                      : item.status === 'queued'
                        ? '已进入队列'
                        : item.status === 'synchronizing'
                          ? '正在核对投递状态'
                          : item.status === 'consumed'
                            ? item.outcomeCode === 'TURN_INTERRUPTED'
                              ? '已接收 · 执行已中断'
                              : '输入已接收'
                            : item.status === 'cancelled'
                              ? '队列已取消'
                              : item.status === 'rejected'
                                ? '队列已拒绝'
                                : '提交状态未知'}</span>
                    <span>投递类型：{item.deliveryMode === 'steer' ? '补充当前任务' : '下一轮任务'}</span>
                    {item.deliveryMode === 'steer' && (
                      <span>目标轮次：{item.targetTurnId ?? '未知'}</span>
                    )}
                    <span>适用权限：{item.permission
                      ? permissionLabels[item.permission]
                      : item.deliveryMode === 'steer' ? '继承当前轮次' : '未知'}</span>
                  </div>
                  {item.bodyUnavailable
                    ? <p className="prompt-queue__detail">正文未能在队列终止前完成读取。</p>
                    : <pre>{item.body}</pre>}
                  {item.detail && <p className="prompt-queue__detail">{item.detail}</p>}
                </article>
              ))}
            </section>
          )}
          {canControl && interaction && (
            <InteractionCard
              key={`${interaction.id}:${'workflowRevision' in interaction ? interaction.workflowRevision : 'live'}`}
              interaction={interaction}
              onRead={onReadInteraction}
              onResolve={onResolveInteraction}
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
                <button className="secondary-action" type="button" onClick={onNewSession}>
                  <MessageSquarePlus size={13} /> 创建会话
                </button>
              </div>
            </div>
          </div>
          <p className="composer-note">当前没有活动会话</p>
        </div>
      ) : !isObserver ? <div className="composer-wrap" ref={composerWrapRef}>
        {queuedCount > 0 && (
          <div className="queued-input">
            <MessageSquarePlus size={12} /><span>{queuedCount} 条输入正在等待处理</span>
          </div>
        )}
        <div className="composer-frame">
          <TodoDock
            key={todo?.id ?? 'no-todo'}
            todo={todo}
            interactionOpen={Boolean(interaction)}
            onLayoutChange={updateJumpPosition}
          />
          <div className={`composer${draft ? ' has-content' : ''}`}>
          <div className="composer-editor">
            <Sparkles size={14} />
            <textarea
              ref={composerInputRef}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onCompositionStart={() => {
                composerComposingRef.current = true;
              }}
              onCompositionEnd={() => {
                composerComposingRef.current = false;
              }}
              onBlur={() => {
                composerComposingRef.current = false;
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  const nativeEvent = event.nativeEvent;
                  if (
                    composerComposingRef.current
                    || nativeEvent.isComposing
                    || nativeEvent.keyCode === 229
                  ) return;
                  event.preventDefault();
                  void submit(event.metaKey || event.ctrlKey);
                }
              }}
              rows={1}
              disabled={runtimeStatus !== 'online' || !session.id}
              placeholder={isRunning ? '补充指令，或引导当前运行…' : '让 Pulsara 处理复杂工作…'}
              aria-label="发送给 Pulsara"
            />
            {wordCount > 0 && <span className="draft-count">{wordCount}</span>}
          </div>
          <div className="composer-actions">
            <div className="composer-controls">
              <div className="popover-anchor model-picker">
                <button className={`mode-chip model-chip${modelOpen ? ' is-active' : ''}`} title={modelBindingMissing ? '模型配置已删除' : modelConnectionLabel(selectedModel)} onClick={() => { setModelOpen((value) => !value); setOptionsOpen(false); setReasoningOpen(false); setSkillOpen(false); setPermissionOpen(false); }} aria-expanded={modelOpen} disabled={modelBindingBusy}>
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
                    disabled={submitting}
                  ><BookOpenText size={12} /> 技能 <ChevronDown size={10} /></button>
                  {skillOpen && (
                    <div className="menu-popover skill-menu skill-menu--composer">
                      <span className="menu-label">用于本轮</span>
                      <div className="skill-menu__list">
                        {skills.map((skill) => {
                          const selected = skill.configured || new RegExp(`(^|\\s)\\$${skill.name}(?=\\s|$)`).test(draft);
                          return (
                            <button key={`${skill.name}:${skill.location}`} className={selected ? 'is-selected' : ''} onClick={() => insertSkill(skill.name)} disabled={skill.configured}>
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
            <div>
              {isRunning && !draft ? (
                <button className="send-button is-stop" onClick={onStop} aria-label="停止当前运行"><CircleStop size={15} /></button>
              ) : (
                <button className="send-button" onClick={() => void submit(false)} disabled={!draft.trim() || submitting || runtimeStatus !== 'online' || !session.id || !modelReady} aria-label={isRunning ? '排队发送' : '发送'}>
                  {isRunning ? <Play size={14} fill="currentColor" /> : <Send size={14} />}
                </button>
              )}
            </div>
          </div>
          </div>
        </div>
        <p className={`composer-note${!modelReady ? ' composer-note--attention' : ''}`}>{!modelReady ? (modelBindingMissing ? '原模型配置已删除，请重新选择' : '请先为此会话选择模型配置') : composerHint} · 规划与权限只作用于本轮</p>
      </div> : (
        <div className="observer-wrap">
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
