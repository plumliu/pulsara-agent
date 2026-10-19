import { PresentationContext } from './presentation-provider';
import { parsePresentationResult } from '../lib/presentation-targets';
import { PresentationResult } from './presentation-result';
import { desktopHost, writeClipboardText } from '../lib/desktop-host';

import { captureHistoryAnchor, restoreHistoryAnchor, waitForHistoryEntry, type HistoryReadingAnchor } from '../lib/history-dom';
import { splitSkillReferences } from '../lib/skill-reference';

import { createPortal } from 'react-dom';
import { usePromptHover } from '../lib/prompt-hover';
import { AgentInputAvatar, UserAvatar } from './user-avatar';


import { CapabilityInteractionEditor } from './capability-interaction-editor';

import {
  ArrowDown,
  ArrowLeft,
  AlarmClock,
  Plus,
  FolderPlus,
  BookOpenText,
  Bot,
  BrainCircuit,
  Braces,
  Check,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Square,
  Clock3,
  Copy,
  CornerDownRight,
  FileDiff,
  FileText,
  FileSearch,
  Upload,
  GitFork,
  ListTodo,
  LoaderCircle,
  MessageSquarePlus,
  Minimize2,
  MoreHorizontal,
  PanelRight,
  Play,
  Pencil,
  Trash2,
  RotateCcw,
  ArrowUp,
  ShieldCheck,
  Settings,
  Sparkles,
  TerminalSquare,
  TriangleAlert,
  WandSparkles,
  Zap,
} from 'lucide-react';
import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import { TurnInterruptionHistoryNotice } from './turn-interruption-notice';
import { WorkspaceRecoveryDock } from './workspace-recovery-dock';
import type { TurnInterruptionNotice } from '../lib/runtime-adapter';
import { ContextUsageIndicator } from './context-usage-indicator';
import type {
  ContextUsagePreview,
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
import type { Message, PermissionMode, ReasoningBlock, RuntimeStatus, SessionSummary, SkillCapability, SubagentRun, TodoRun, ToolTrace, Workspace } from '../lib/pulsara-types';
import { permissionLabels, permissionModeOrder } from '../lib/pulsara-types';
import { MarkdownBody, MarkdownInline, type MarkdownNotify } from './markdown-body';
import { AnnotationBody, ResponseAnnotations } from './response-annotations';
import { PromptComposer } from './prompt-composer';
import { PromptContentView } from './prompt-content-view';
import { WelcomeTypewriter } from './welcome-typewriter';
import { AnimatedDisclosure } from './animated-disclosure';
import { builtinToolSummary } from '../lib/builtin-tool-summary';
import { ToolResultDisplayContext } from '../lib/tool-result-display';
import { PromptDraftStore } from '../lib/prompt-draft';
import { useWorkbenchFileDrop } from '../lib/workbench-file-drop';
import { promptContentTextProjection } from '../lib/prompt-content';


export interface WorkbenchHistory {
  hasOlder: boolean;
  loading: boolean;
  error?: string;
  compactions?: Array<{ eventId: string; adoptedAfterEntrySequence: number; displayBeforeEntryId?: string; displayAfterEntryId?: string }>;
  windows: Array<{
    id: string; firstEntryId?: string; lastEntryId?: string; entryIds?: string[];
    hasOlder: boolean; hasNewer: boolean; loading: boolean; error?: string;
    contextState?: 'loading' | 'ready' | 'error';
  }>;
}

interface WorkbenchViewProps {
  history?: WorkbenchHistory;
  onLoadHistory?: (windowId?: string, direction?: 'older' | 'newer') => Promise<void>;
  onHydrateEntries?: (entryIds: string[], includeReasoning?: boolean) => Promise<void>;
  onLocateEntry?: (entryId: string, signal: AbortSignal) => Promise<void>;
  onLocateAnnotation?: (entryId: string, signal: AbortSignal) => Promise<void>;
  onCompletePaths?: import('../lib/file-reference').CompleteWorkspacePaths;
  focusSourceEntry?: { sessionId: string; entryId: string };
  workspace: Workspace;
  session: SessionSummary;
  messages: Message[];
  interruptionNotices?: TurnInterruptionNotice[];
  contextCompaction?: ContextCompactionBoundary;
  contextCompactionInProgress?: boolean;
  initialContextBase?: ProtocolCanonicalControl['initial_context_base'];
  onFork: (entryId: string) => Promise<void>;
  todo?: TodoRun;
  activePlanMode: boolean;
  isRunning: boolean;
  activeTurnId?: string;
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
  onReadContextUsage?: (signal: AbortSignal) => Promise<ContextUsagePreview>;
  contextUsageRevision?: number | string;
  interaction?: RuntimeInteractionSummary;
  toolDecisionPending?: boolean;
  canControl: boolean;
  historyOnly?: boolean;
  isObserver: boolean;
  workspaceAvailability?: import('../lib/runtime-adapter').WorkspaceAvailability;
  workspaceRecoveryBusy?: boolean;
  workspaceRecoveryError?: string;
  onRestoreWorkspace?: () => Promise<void>;
  onRecheckWorkspace?: () => Promise<void>;
  permission: PermissionMode;
  skills: SkillCapability[];
  focusTaskId?: string;
  focusTaskRevision: number;
  focusTaskHighlighted: boolean;
  onReconnect: () => void;
  onTakeControl: () => void;
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
  onRenameSession: () => void;
  onOpenScheduledTask?: () => void;
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
  onReadVisualization?: import("./inline-visualization").ReadVisualization;
  onReadVisualizationThumbnail?: import("./inline-visualization").ReadVisualizationThumbnail;
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
    // Complete inline results may also carry the canonical artifact reread hint.
    // That hint is presentation text, outside the structured result body.
    const artifactHint = value.indexOf('\n\n[TOOL OUTPUT ARTIFACT:');
    const parsed = JSON.parse(artifactHint < 0 ? value : value.slice(0, artifactHint)) as unknown;
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
    if (trace.toolName !== 'inspect_capability') continue;
    const descriptor = parseJsonObject(trace.resultText);
    const toolRef = stringValue(descriptor?.tool_ref);
    const target = objectField(descriptor, 'target');
    const serverId = stringValue(target?.server_id);
    const remoteToolName = stringValue(target?.tool_name);
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
  if (!name || !['list_capabilities', 'inspect_capability', 'use_new_mcp_tool'].includes(name)) {
    return undefined;
  }
  const args = parseJsonObject(trace.argumentsJson);
  const result = parseJsonObject(trace.resultText);
  if (name === 'list_capabilities') {
    if (!Array.isArray(result?.items) || typeof result?.total_count !== 'number') return undefined;
    const items = objectArrayField(result, 'items');
    const total = numberValue(result?.total_count);
    return {
      subtitle: `观察到 ${total} 项能力${result?.completeness === 'PARTIAL' ? ' · 观察不完整' : ''}`,
      rows: total > items.length ? [{ label: '当前页', value: `${items.length} / ${total}` }] : [],
      items: items.map((item) => ({ name: stringValue(item.name) || '未命名能力', detail: stringValue(item.status) })),
      suppressGenericSuccess: true,
    };
  }
  if (name === 'inspect_capability') {
    const target = objectField(result, 'target') ?? objectField(args, 'target');
    if (!stringValue(target?.kind).startsWith('MCP_')) return undefined;
    const serverId = stringValue(target?.server_id) || stringValue(target?.runtime_server_id);
    const toolName = stringValue(target?.tool_name) || stringValue(target?.name);
    return {
      subtitle: [serverId, toolName].filter(Boolean).join(' · ') || 'MCP 详情',
      rows: [{ label: 'MCP 服务', value: serverId }, ...(toolName ? [{ label: '所选能力', value: toolName }] : [])],
      items: [], suppressGenericSuccess: true,
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
  onNotify,
}: {
  trace: ToolTrace;
  skills: SkillCapability[];
  mcpToolRefs: ReadonlyMap<string, McpToolIdentity>;
  artifactOwnerKey: string;
  onReadToolArtifact: WorkbenchViewProps['onReadToolArtifact'];
  onReadPromptImage: WorkbenchViewProps['onReadPromptImage'];
  onNotify: MarkdownNotify;
}) {
  const copyOutput = (text: string) => {
    void writeClipboardText(text).catch(() => onNotify('无法复制输出', '系统剪贴板暂时不可用，请重试', 'warning'));
  };
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
  const showTargets = useContext(PresentationContext);
  const presentation = trace.toolName === 'open_in_pulsara' && trace.status === 'completed' && trace.artifact?.sourceCoverage === 'COMPLETE' && trace.artifact?.displayKind === 'COMPLETE'
    ? parsePresentationResult(trace.resultText ?? '') : undefined;
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
    presentation || trace.command || diffText || hasTerminalOutput || hasRawResult
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
      <article data-result-entry={trace.resultEntryId} className={`trace-card${expanded ? ' is-expanded' : ''}`}>
        {expandable ? (
          <button
            data-thread-disclosure="" className="trace-card__summary"
            onClick={() => {
              if (expanded) closeArtifactPage();
              else terminalFollowTailRef.current = true;
              setExpanded((value) => !value);
            }}
            aria-expanded={expanded}
            aria-label={`${expanded ? '收起' : '展开'}工具详情：${trace.toolName ?? trace.title}`}
          >{summary}</button>
        ) : <div data-thread-disclosure="" className="trace-card__summary">{summary}</div>}
        {expandable && <AnimatedDisclosure unmountOnClose open={expanded} className="trace-card__disclosure">
          {presentation && <PresentationResult result={presentation} onOpen={showTargets} />}
          {trace.resultContent ? (
            <section className="tool-result-images" aria-label="工具读取的图片">
              <PromptContentView
                content={trace.resultContent}
                variant="tool"
                onReadImage={onReadPromptImage}
              />
            </section>
          ) : (
            (!presentation || hasRawResult || canReadArtifact) && <div className="terminal-output">
            {trace.command && <div className="terminal-command"><span>$</span> {trace.command}</div>}
            {mcpDetail && <McpTraceDetails detail={mcpDetail} />}
            {hasTerminalOutput && (
              <section className="terminal-stream" aria-label="命令输出">
                <header>
                  <strong>输出</strong>
                  <button type="button" aria-label="复制命令输出" onClick={() => copyOutput(terminalOutput)}><Copy size={12} /></button>
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
                  <button type="button" aria-label="复制工具原始结果" onClick={() => copyOutput(trace.resultText ?? '')}><Copy size={12} /></button>
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
                          onClick={() => copyOutput(artifactPage.text)}
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
            {trace.meta && !presentation && <footer><span>{trace.meta}</span></footer>}
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
        data-thread-disclosure="" className="reasoning-row__summary"
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
        <ChevronRight className="reasoning-row__chevron" size={13} />
      </button>
      <AnimatedDisclosure unmountOnClose open={expanded}>
        <div className="reasoning-row__body assistant-markdown"><MarkdownBody body={block.body} onNotify={onNotify} streaming={running} /></div>
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
  unknown: '状态待读取',
};

function SubagentRunCard({
  run,
  focused,
  skills,
  mcpToolRefs,
  artifactOwnerKey,
  onReadToolArtifact,
  onReadPromptImage,
  onReadVisualization,
  onReadVisualizationThumbnail,
  onNotify,
}: {
  run: SubagentRun;
  focused: boolean;
  skills: SkillCapability[];
  mcpToolRefs: ReadonlyMap<string, McpToolIdentity>;
  artifactOwnerKey: string;
  onReadToolArtifact: WorkbenchViewProps['onReadToolArtifact'];
  onReadPromptImage: WorkbenchViewProps['onReadPromptImage'];
  onReadVisualization: NonNullable<WorkbenchViewProps['onReadVisualization']>;
  onReadVisualizationThumbnail: WorkbenchViewProps['onReadVisualizationThumbnail'];
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
      <button data-thread-disclosure="" className="subagent-run__header" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
        <span className={`agent-mini-icon color-${run.color}`}><Bot size={13} /></span>
        <span className="subagent-run__identity"><strong>{run.label}</strong><small>{run.role}</small></span>
        <span className={`subagent-run__status subagent-run__status--${run.status}`}>
          {run.status === 'running' ? <LoaderCircle size={11} /> : run.status === 'completed' ? <Check size={11} /> : null}
          {subagentStatusLabels[run.status]}
        </span>
        <ChevronDown size={13} />
      </button>
      <AnimatedDisclosure unmountOnClose open={expanded}>
        <div className="subagent-run__body">
          {run.objective && <div className="subagent-objective"><span>目标</span><p>{run.objective}</p></div>}
          {run.activities.map((activity) => (
            <HistoryMessageHost key={activity.id} message={{ ...activity, status: undefined, role: 'assistant', historyIncludeReasoning: true }}>
            <section className="subagent-activity">
              {activity.reasoning?.length ? (
                <ReasoningDisclosure
                  blocks={activity.reasoning}
                  onNotify={onNotify}
                  nested
                />
              ) : null}
              {activity.body && (activity.kind === 'guidance' ? (
                <div className="subagent-guidance">
                  <span><CornerDownRight size={11} /> 主 Agent 补充</span>
                  <div className="assistant-markdown"><MarkdownBody body={activity.body} onNotify={onNotify} /></div>
                </div>
              ) : <div className="assistant-markdown"><MarkdownBody assistantChat visualization={activity.visualizations ? {entryId: activity.id, items: activity.visualizations, onRead: onReadVisualization, onReadThumbnail: onReadVisualizationThumbnail} : undefined} body={activity.body} onNotify={onNotify} streaming={activity.status === 'running'} /></div>)}
              {activity.traces?.length ? <div className="execution-rail subagent-execution">{activity.traces.map((trace) => <TraceCard onNotify={onNotify} key={`${artifactOwnerKey}:${trace.id}:${trace.resultEntryId ?? ''}`} trace={trace} skills={skills} mcpToolRefs={mcpToolRefs} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} onReadPromptImage={onReadPromptImage} />)}</div> : null}
            </section>
            </HistoryMessageHost>
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
  onReadVisualization,
  onReadVisualizationThumbnail,
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
  onReadVisualization: NonNullable<WorkbenchViewProps['onReadVisualization']>;
  onReadVisualizationThumbnail: WorkbenchViewProps['onReadVisualizationThumbnail'];
  onNotify: MarkdownNotify;
}) {
  const settled = runs.filter((run) => !['pending', 'running', 'waiting'].includes(run.status)).length;
  return (
    <section className="subagent-group" aria-label="任务执行">
      <header className="subagent-group__header">
        <span><GitFork size={13} /><strong>任务执行</strong></span>
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
          onReadVisualization={onReadVisualization} onReadVisualizationThumbnail={onReadVisualizationThumbnail}
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



function SubagentCompletionEvent({ message, visible }: { message: Message; visible: boolean }) {
  const { trigger, details, position, show, hide, keep, dismiss } = usePromptHover<HTMLElement>(310);
  useEffect(() => { if (!visible) dismiss(); }, [visible, dismiss]);
  const helpId = `${message.id}-subagent-completion-help`;
  const sourceResult = message.sourceSubagentLabel ? `${message.sourceSubagentLabel} 的结果` : '任务结果';
  const title = message.sourceSubagentRelation === 'previous'
    ? `上一轮 ${sourceResult}已加入本轮对话`
    : message.sourceSubagentRelation === 'earlier'
      ? `此前 ${sourceResult}已加入本轮对话`
      : `${sourceResult}已加入本轮对话`;
  return <>
    <article ref={trigger} className="subagent-completion-event" aria-label={title}
      aria-describedby={position && visible ? helpId : undefined} tabIndex={0}
      onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
      <span className="subagent-completion-event__icon"><GitFork size={13} /></span>
      <div className="subagent-completion-event__copy"><strong>{title}</strong></div>
      <time>{message.time}</time>
    </article>
    {position && visible && createPortal(<span ref={details} id={helpId}
      className="subagent-completion-event__tooltip" role="tooltip" data-above={position.above}
      style={{ left: position.left, top: position.top, translate: position.above ? '0 calc(-100% - 7px)' : '0 7px' }}
      onMouseEnter={keep} onMouseLeave={hide}>
      该结果已记录到当前对话，不会重新运行任务。
    </span>, document.body)}
  </>;
}

function UserMessage({
  message,
  label = '你',
  senderKind = 'user',
  pendingContent,
  deliveryStatus,
  onReadPromptImage = unavailablePromptImage,
  visible = true,
}: {
  message: Message;
  label?: string;
  senderKind?: 'user' | 'agent';
  pendingContent?: LocalPromptSubmission['content'];
  deliveryStatus?: string;
  onReadPromptImage?: WorkbenchViewProps['onReadPromptImage'];
  visible?: boolean;
}) {
  const content = pendingContent ?? message.promptContent;
  const sourceTextStyle = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } as const;
  if (message.userKind === 'subagent-completion') {
    return <SubagentCompletionEvent message={message} visible={visible} />;
  }

  if (message.userKind === 'steer') {
    const renderSteer = (body: ReactNode) => (
      <article className="user-steer" aria-label="引导">
        <span className="user-steer__icon"><CornerDownRight size={13} /></span>
        <div className="user-steer__content">
          <header><strong>引导</strong><time>{message.time}</time></header>
          {body}
        </div>
      </article>
    );
    return content ? <div className="user-steer-turn"><PromptContentView content={content} variant="message"
      onReadImage={onReadPromptImage} renderMessageBody={renderSteer} /></div>
      : renderSteer(<p style={sourceTextStyle}>{message.body}</p>);
  }

  const renderBubble = (body: ReactNode) => body ? <div className="user-message">{body}</div> : null;

  return (
    <article className="user-turn">
      <header className="user-heading">
        <strong>{message.userKind === 'scheduled' ? '定时任务' : label}</strong>
        {message.userKind === 'scheduled' ? <AgentInputAvatar kind="scheduled" />
          : senderKind === 'agent' ? <AgentInputAvatar kind="agent" /> : <UserAvatar />}
      </header>
      {content
        ? <PromptContentView
            content={content}
            variant="message"
            onReadImage={onReadPromptImage}
            renderMessageBody={renderBubble}
          />
        : renderBubble(<p style={sourceTextStyle}>{message.body}</p>)}
      <div className="message-foot">{deliveryStatus ? <span role="status">{deliveryStatus}</span> : <time>{message.time}</time>}</div>
    </article>
  );
}

function AssistantHeading({ message, response, label = 'Pulsara' }: { message: Message; response: boolean; label?: string }) {
  return (
    <header className={`assistant-heading${response ? ' assistant-heading--response' : ' assistant-heading--run-start'}`}>
      <div className="assistant-avatar" aria-hidden="true">
        {/* Local static asset: the standalone app has no Next image service. */}
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
  canFork,
  artifactOwnerKey,
  onReadToolArtifact,
  onReadPromptImage,
  onReadVisualization,
  onReadVisualizationThumbnail,
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
  canFork: boolean;
  artifactOwnerKey: string;
  onReadToolArtifact: WorkbenchViewProps['onReadToolArtifact'];
  onReadPromptImage: WorkbenchViewProps['onReadPromptImage'];
  onReadVisualization: NonNullable<WorkbenchViewProps['onReadVisualization']>;
  onReadVisualizationThumbnail?: WorkbenchViewProps['onReadVisualizationThumbnail'];
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
              <AnnotationBody entryId={message.assistantKind !== 'live' && !isStreaming ? message.id : undefined} body={message.body}>
                <MarkdownBody assistantChat visualization={message.visualizations ? { entryId: message.id, items: message.visualizations ?? [], onRead: onReadVisualization, onReadThumbnail: onReadVisualizationThumbnail } : undefined} annotationSource={!isStreaming && message.assistantKind !== 'live'} body={message.body} onNotify={onNotify} streaming={isStreaming} />
              </AnnotationBody>
            </div>
            {(canCopyResponse || message.forkEligible) && (
              <div className="response-actions">
                <button
                  onClick={() => {
                    void writeClipboardText(message.body).then(
                      () => onNotify('已复制回复'),
                      () => onNotify('无法复制回复', '系统剪贴板暂时不可用，请重试'),
                    );
                  }}
                  aria-label="复制回复"
                ><Copy size={13} /></button>
                {canFork && message.forkEligible && (
                  <button aria-label="从此处分叉" title="从此处分叉" disabled={forking} aria-busy={forking}
                    onClick={() => {
                      if (!canFork || forkInFlight.current) return;
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

      {message.traces && <div className="execution-rail">{message.traces.map((trace) => <TraceCard onNotify={onNotify} key={`${artifactOwnerKey}:${trace.id}:${trace.resultEntryId ?? ''}`} trace={trace} skills={skills} mcpToolRefs={mcpToolRefs} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} onReadPromptImage={onReadPromptImage} />)}</div>}
      {message.subagentRuns?.length ? (
        <SubagentGroup onReadVisualization={onReadVisualization} onReadVisualizationThumbnail={onReadVisualizationThumbnail} runs={message.subagentRuns} focusTaskId={focusTaskId} focusTaskRevision={focusTaskRevision} focusTaskHighlighted={focusTaskHighlighted} skills={skills} mcpToolRefs={mcpToolRefs} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} onReadPromptImage={onReadPromptImage} onNotify={onNotify} />
      ) : null}

    </article>
  );
}

function isCompleteAnswer(message: Message, taskFinalAnswerId?: string): boolean {
  // Final-answer presentation is independent of whether this historical answer
  // can serve as a new fork anchor. Task conversations use their accepted result.
  return message.role === 'assistant' && message.assistantKind === 'terminal'
    && message.status !== 'running'
    && (message.rootFinal === true || message.forkEligible === true || message.id === taskFinalAnswerId);
}

const HistoryHydrationContext = createContext<WorkbenchViewProps['onHydrateEntries']>(undefined);

function HistoryMessageHost({ message, children, knownContent, contextLoadingSummarized = false }: { message: Message; children: ReactNode; knownContent?: ReactNode; contextLoadingSummarized?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const hydrate = useContext(HistoryHydrationContext);
  const contextPending = message.historyContextState === 'loading' || message.historyContextState === 'error';
  const includeReasoning = message.historyIncludeReasoning === true;
  const contentStates = [message.contentState, ...(includeReasoning ? [message.reasoningContentState] : [])];
  const contentState = contentStates.includes('error') ? 'error' : contentStates.includes('loading') ? 'loading'
    : contentStates.includes('unloaded') ? 'unloaded' : 'ready';
  const pending = contentState !== 'ready';
  const sourceIds = message.sourceEntryIds?.length ? message.sourceEntryIds : [message.id];
  const read = () => hydrate?.(sourceIds, includeReasoning).catch(() => {});
  useEffect(() => {
    const host = ref.current;
    if (!host || host.closest('[inert]') || !hydrate || contextPending || contentState !== 'unloaded') return;
    const read = () => { void hydrate(message.sourceEntryIds?.length ? message.sourceEntryIds : [message.id], includeReasoning).catch(() => {}); };
    if (typeof IntersectionObserver === 'undefined') {
      const viewport = host.closest('.thread-scroll')?.getBoundingClientRect();
      const rect = host.getBoundingClientRect();
      if (!viewport || (rect.bottom >= viewport.top && rect.top <= viewport.bottom)) read();
      return;
    }
    const observer = new IntersectionObserver(entries => {
      if (!host.closest('[inert]') && entries.some(entry => entry.isIntersecting)) { observer.disconnect(); read(); }
    }, { root: host.closest('.thread-scroll'), rootMargin: '200px' });
    observer.observe(host);
    return () => observer.disconnect();
  }, [contextPending, hydrate, message.id, contentState, message.sourceEntryIds, includeReasoning]);
  const summarized = contextLoadingSummarized && message.historyContextState === 'loading';
  if (summarized && !knownContent) return null;
  return <div ref={ref} data-source-entry={message.id} data-content-state={contextPending ? 'loading' : contentState} data-source-entries={message.sourceEntryIds?.join(' ')}>
    {summarized ? knownContent : contextPending ? <div role="status">{knownContent}{message.historyContextState === 'error' ? '记录分组读取失败，请重试此页。' : '正在读取记录分组…'}</div>
      : pending ? <div role="status" className="history-content-status">
        {knownContent}
        {contentState === 'error' ? message.contentError ?? message.reasoningContentError ?? '正文读取失败。' : '已保存，正文读取中…'}
        {contentState === 'error' && hydrate && <button type="button" onClick={() => { void read(); }}>重试正文</button>}
      </div> : children}
  </div>;
}

function ConversationRun({ messages, renderMessage, focusRequest, completed, active, assistantLabel, taskFinalAnswerId, historyLoading = false }: {
  messages: Message[];
  renderMessage: (message: Message, startsRun: boolean, visible?: boolean) => ReactNode;
  focusRequest?: object | number;
  taskFinalAnswerId?: string;
  historyLoading?: boolean;
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
      ? true : disclosure.complete !== complete || disclosure.active !== active ? active && !complete : disclosure.expanded });
  }
  const process = messages.flatMap((message) => {
    if (message !== answer) return [message];
    return message.reasoning?.length || (message.reasoningContentState && message.reasoningContentState !== 'ready') || message.traces?.length || message.subagentRuns?.length
      ? [{ ...message, id: `${message.id}:process`, sourceEntryIds: message.sourceEntryIds ?? [message.id], body: '', forkEligible: false, visualizations: undefined }] : [];
  });
  const hasProcess = process.some((message) => message.role === 'assistant' || message.userKind === 'subagent-completion');
  if (!hasProcess) return renderMessage(answer!, true);
  return <section className="conversation-run" data-process-expanded={disclosure.expanded}>
    {!historyLoading && messages.some(message => message.historyEarlierUnloaded) && <div role="status" className="history-boundary">本轮较早过程尚未加载</div>}
    <AssistantHeading message={messages[0]} response={Boolean(messages[0].body.trim())} label={assistantLabel} />
    <button type="button" className="conversation-run__toggle"
      data-thread-disclosure="" aria-expanded={disclosure.expanded}
      aria-label={disclosure.expanded ? '收起中间过程' : '展开中间过程'}
      onClick={() => setDisclosure({ ...disclosure, expanded: !disclosure.expanded })}>
      <span>{complete ? '处理过程' : '中间过程'}</span><ChevronRight size={13} />
    </button>
    {process.map((message) => {
      // User guidance stays visible; tool-result notifications belong to the process.
      const hidden = !disclosure.expanded && !message.visualizations?.length
        && (message.role === 'assistant' || message.userKind === 'subagent-completion');
      return <AnimatedDisclosure key={message.id} open={!hidden} unmountOnClose className="conversation-run__disclosure" contentClassName="conversation-run__step-content">
        <div className="conversation-run__step">{renderMessage({ ...message, historyIncludeReasoning: disclosure.expanded || hidden }, false, !hidden)}</div>
      </AnimatedDisclosure>;
    })}
    {answer && renderMessage({ ...answer, historyIncludeReasoning: false, reasoning: undefined, traces: undefined, subagentRuns: undefined }, false)}
  </section>;
}

function previousBoundary(previous: Message, current: Message): boolean {
  return !previous.historyGroupId || previous.historyGroupId !== current.historyGroupId
    || previous.historyWindowId !== current.historyWindowId;
}

function findToolChainConnections(messages: Message[]): {
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
    if (previousBoundary(messages[index - 1], messages[index])) continue;
    const previous = messages[index - 1];
    const current = messages[index];
    if (!endsWithTools(previous) || !startsWithTools(current)) continue;
    after.add(previous.id);
    before.add(current.id);
  }
  return { before, after };
}

export function ConversationMessages({
  history, onLoadHistory, localSubmissions = [], activeTurnId,
  messages,
  interruptionNotices = [],
  skills = [],
  artifactOwnerKey,
  onReadToolArtifact,
  onNotify,
  onFork = async () => undefined,
  canFork = false,
  onReadPromptImage = unavailablePromptImage,
  onReadVisualization = unavailableVisualization,
  onReadVisualizationThumbnail,
  userLabel = '你',
  userSenderKind = 'user',
  assistantLabel = 'Pulsara',
  taskFinalAnswerId,
  contextCompactionInProgress = false,
  isRunning = false,
  focusTaskId,
  focusTaskRevision = 0,
  focusTaskHighlighted = false,
  focusSourceEntry,
}: {
  history?: WorkbenchHistory;
  onLoadHistory?: WorkbenchViewProps['onLoadHistory'];
  localSubmissions?: LocalPromptSubmission[];
  activeTurnId?: string;
  messages: Message[];
  interruptionNotices?: TurnInterruptionNotice[];
  skills?: SkillCapability[];
  artifactOwnerKey: string;
  onReadToolArtifact: (resultEntryId: string, offsetChars: number) => Promise<ToolArtifactPage>;
  onNotify: MarkdownNotify;
  onFork?: (entryId: string) => Promise<void>;
  canFork?: boolean;
  onReadPromptImage?: WorkbenchViewProps['onReadPromptImage'];
  onReadVisualization?: WorkbenchViewProps['onReadVisualization'];
  onReadVisualizationThumbnail?: WorkbenchViewProps['onReadVisualizationThumbnail'];
  userLabel?: string;
  userSenderKind?: 'user' | 'agent';
  assistantLabel?: string;
  taskFinalAnswerId?: string;
  contextCompactionInProgress?: boolean;
  isRunning?: boolean;
  focusTaskId?: string;
  focusTaskRevision?: number;
  focusTaskHighlighted?: boolean;
  focusSourceEntry?: WorkbenchViewProps['focusSourceEntry'];
}) {
  const toolChainConnections = useMemo(
    () => findToolChainConnections(messages), [messages],
  );
  const mcpToolRefs = useMemo(() => buildMcpToolRefIndex(messages), [messages]);
  const renderMessage = (message: Message, startsRun: boolean, visible = true) => {
    const pendingContent = message.role === 'user'
      ? localSubmissions.find(item => item.commandId === message.inputSource?.commandId
        && (!item.queueItemId || item.queueItemId === message.inputSource?.queueItemId))?.content ?? message.pendingContent
      : undefined;
    return <HistoryMessageHost key={message.id} message={message}
      contextLoadingSummarized={history?.windows.some(window => window.id === message.historyWindowId && window.contextState === 'loading')} knownContent={pendingContent
      ? <UserMessage message={message} label={userLabel} senderKind={userSenderKind} onReadPromptImage={onReadPromptImage} pendingContent={pendingContent} deliveryStatus="本地已知内容" /> : undefined}>
      {message.role === 'user'
        ? <UserMessage message={message} label={userLabel} senderKind={userSenderKind} onReadPromptImage={onReadPromptImage} visible={visible} />
        : <AssistantMessage message={message} responseActionEligible={isCompleteAnswer(message, taskFinalAnswerId)} startsAssistantRun={startsRun}
            joinsPreviousToolChain={toolChainConnections.before.has(message.id)}
            joinsNextToolChain={toolChainConnections.after.has(message.id)}
            focusTaskId={focusTaskId} focusTaskRevision={focusTaskRevision}
            focusTaskHighlighted={focusTaskHighlighted} skills={skills} mcpToolRefs={mcpToolRefs}
            onNotify={onNotify} onFork={onFork} canFork={canFork} artifactOwnerKey={artifactOwnerKey}
            onReadToolArtifact={onReadToolArtifact} onReadPromptImage={onReadPromptImage}
            onReadVisualization={onReadVisualization}
            onReadVisualizationThumbnail={onReadVisualizationThumbnail}
            assistantLabel={assistantLabel} />}
    </HistoryMessageHost>;
  };
  const completedTurns = new Set(messages.filter(message => isCompleteAnswer(message, taskFinalAnswerId)).map(message => message.turnId).filter(Boolean));
  const content: ReactNode[] = [];
  if (history?.error && !history.windows.some(window => window.error === history.error)) {
    content.push(<div key="history-context-error" className="history-boundary" role="region" aria-label="历史上下文读取失败">
      <span role="alert">{history.error}</span>
      {onLoadHistory && <button type="button" disabled={history.loading} onClick={() => { void onLoadHistory().catch(() => {}); }}>重试历史上下文</button>}
    </div>);
  }
  let run: Message[] = [];
  let segmentBoundary = 'start';
  const flush = () => {
    if (!run.length) return;
    const sourceInProcess = focusSourceEntry && run.some(message => {
      // The accepted answer's body is always mounted outside the disclosure.
      // Its tool-result aliases and tasks still belong to the process.
      if (message.id === focusSourceEntry.entryId) return !isCompleteAnswer(message, taskFinalAnswerId);
      return message.sourceEntryIds?.includes(focusSourceEntry.entryId)
        || message.traces?.some(trace => trace.resultEntryId === focusSourceEntry.entryId);
    });
    const focused = sourceInProcess
      ? focusSourceEntry
      : focusTaskHighlighted && run.some(message => message.subagentRuns?.some(task => task.id === focusTaskId))
        ? focusTaskRevision : undefined;
    content.push(<ConversationRun key={`${artifactOwnerKey}:${run[0].historyGroupId ?? `${run[0].entryOwnerKind ?? 'owner'}:${run[0].turnId ?? run[0].id}`}:${run[0].historyWindowId ?? ''}:${run[0].historyGroupId ? '' : segmentBoundary}`} messages={run}
      renderMessage={renderMessage} focusRequest={focused} assistantLabel={assistantLabel} taskFinalAnswerId={taskFinalAnswerId}
      historyLoading={history?.windows.some(window => window.id === run[0].historyWindowId && (window.loading || window.contextState === 'loading'))}
      completed={Boolean(run[0].turnId && completedTurns.has(run[0].turnId))}
      active={isRunning && run[0].turnId === activeTurnId} />);
    run = [];
  };
  type TimelineItem = { message: Message; windowId?: string } | { notice: TurnInterruptionNotice; windowId: string };
  const timeline: TimelineItem[] = messages.map(message => ({ message, windowId: message.historyWindowId
    ?? history?.windows.find(window => window.entryIds?.includes(message.id) || window.firstEntryId === message.id || window.lastEntryId === message.id)?.id }));
  for (const notice of interruptionNotices.filter(item => !item.displayAfterMessageId)) {
    // USER_CONTROL_FEEDBACK has no chat bubble. Its exact retained canonical
    // entry still provides a timeline location inside its own loaded window.
    const window = history?.windows.find(item => notice.displayAfterEntryId && item.entryIds?.includes(notice.displayAfterEntryId));
    if (!window) continue;
    const windowIndex = history!.windows.indexOf(window);
    const position = timeline.findIndex(item => {
      const itemWindow = history!.windows.findIndex(candidate => candidate.id === item.windowId);
      if (itemWindow > windowIndex || (itemWindow < 0 && 'message' in item && item.message.entrySequence === undefined)) return true;
      if (itemWindow !== windowIndex) return false;
      const sequence = 'message' in item ? item.message.entrySequence : item.notice.displayAfterEntrySequence;
      return sequence !== undefined && sequence > notice.displayAfterEntrySequence;
    });
    timeline.splice(position < 0 ? timeline.length : position, 0, { notice, windowId: window.id });
  }
  const shownWindows = new Set<string>();
  const emitNotice = (notice: TurnInterruptionNotice, independent = false) => {
    flush();
    content.push(<div key={`${notice.ownerKind}:${notice.ownerId}`} data-source-entry={independent ? notice.displayAfterEntryId : undefined}>
      <TurnInterruptionHistoryNotice notice={notice} />
    </div>);
    segmentBoundary = `${notice.ownerKind}:${notice.ownerId}`;
  };
  timeline.forEach((item, itemIndex) => {
    const window = history?.windows.find(candidate => candidate.id === item.windowId);
    if (window && !shownWindows.has(window.id)) {
      shownWindows.add(window.id);
      flush();
      const gap = history!.windows.indexOf(window) > 0;
      if (window.hasOlder || window.error || window.loading || window.contextState === 'loading' || gap) content.push(<div key={`history:${window.id}`} className="history-boundary" role="region" aria-label={gap ? '中间记录尚未加载' : '较早记录'}>
        {window.loading || window.contextState === 'loading' ? <span role="status">正在加载…</span> : <>
          <span>{gap ? '中间记录尚未加载' : '较早记录尚未加载'}</span>
          {window.error && <span role="alert">{window.error}</span>}
          {onLoadHistory && (window.hasOlder || window.error) && <button type="button" onClick={() => { void onLoadHistory(window.id, 'older').catch(() => {}); }}>{window.error ? '重试' : '加载更早记录'}</button>}
        </>}
      </div>);
    }
    if ('notice' in item) emitNotice(item.notice, true);
    else {
      const message = item.message;
      for (const marker of history?.compactions ?? []) if (marker.displayBeforeEntryId === message.id) {
        flush(); content.push(<ContextCompactionDivider key={marker.eventId} />); segmentBoundary = marker.eventId;
      }
      if (message.historyContextState === 'loading' || message.historyContextState === 'error') {
        flush(); content.push(renderMessage(message, false));
      } else {
        const newInput = message.role === 'user'
          && message.userKind !== 'steer' && message.userKind !== 'subagent-completion';
        if (newInput || (run.length && (run[0].historyGroupId !== message.historyGroupId || run[0].historyWindowId !== message.historyWindowId || run[0].turnId !== message.turnId))) flush();
        if (message.role === 'user' && message.userKind !== 'subagent-completion' && !run.length) content.push(renderMessage(message, false));
        else run.push(message);
        if (isCompleteAnswer(message, taskFinalAnswerId)) flush();
      }
      for (const marker of history?.compactions ?? []) if (!marker.displayBeforeEntryId && marker.displayAfterEntryId === message.id) {
        flush(); content.push(<ContextCompactionDivider key={marker.eventId} />); segmentBoundary = marker.eventId;
      }
      for (const notice of interruptionNotices.filter(n => n.displayAfterMessageId === message.id)) emitNotice(notice);
    }
    if (window?.hasNewer && timeline.findLastIndex(candidate => candidate.windowId === window.id) === itemIndex) {
      flush(); content.push(<div key={`newer:${window.id}`} className="history-boundary">
        <span>后续记录尚未加载</span>
        {onLoadHistory && <button type="button" disabled={window.loading} onClick={() => { void onLoadHistory(window.id, 'newer').catch(() => {}); }}>加载后续记录</button>}
      </div>);
    }
  });
  flush();
  if (contextCompactionInProgress) content.push(<ContextCompactionDivider key="compaction-progress" inProgress />);
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

function ContextCompactionDivider({ inherited = false, inProgress = false }: { inherited?: boolean; inProgress?: boolean }) {
  const label = inProgress ? '上下文正在压缩' : inherited ? '已保留分叉点的有效上下文，压缩前记录请在原会话查看' : '上下文已压缩';
  return (
    <div
      className="context-compaction-divider"
      role={inProgress ? "status" : "separator"}
      aria-busy={inProgress || undefined}
      aria-label={label}
    >
      <span className="context-compaction-divider__line" aria-hidden="true" />
      <span className="context-compaction-divider__label">{inProgress && <LoaderCircle className="context-compaction-divider__spinner" size={12} aria-hidden="true" />}{label}</span>
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
  history, onLoadHistory, onHydrateEntries, onLocateEntry,
  focusSourceEntry,
  workspace,
  session,
  messages,
  interruptionNotices,
  contextCompaction,
  contextCompactionInProgress = false,
  initialContextBase,
  onFork,
  todo,
  activePlanMode,
  isRunning,
  activeTurnId,
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
  onReadContextUsage,
  contextUsageRevision = 0,
  interaction,
  toolDecisionPending = false,
  canControl,
  historyOnly = false,
  isObserver,
  workspaceAvailability,
  workspaceRecoveryBusy = false,
  workspaceRecoveryError,
  onRestoreWorkspace,
  onRecheckWorkspace,
  permission,
  skills,
  focusTaskId,
  focusTaskRevision,
  focusTaskHighlighted,
  onReconnect,
  onTakeControl,
  onNewSession,
  canCreateSession,
  onToggleInspector,
  onOpenModelSettings,
  onModelCallBindingChange,
  onSend,
  onStop,
  onCompact,
  onRenameSession,
  onOpenScheduledTask,
  onReopenRuntime,
  runtimeReopenBusy,
  onReadInteraction,
  onResolveInteraction,
  artifactOwnerKey,
  onReadToolArtifact,
  onReadPromptImage,
  onReadVisualization = unavailableVisualization,
  onReadVisualizationThumbnail,
  promptDraftStore: draftStore,
  onLocateAnnotation,
  onCompletePaths,
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
  const [welcomeState, setWelcomeState] = useState({ sessionId: session.id, started: false });
  if (welcomeState.sessionId !== session.id) {
    setWelcomeState({ sessionId: session.id, started: false });
  }
  const welcomeDeparture = useRef<{ sessionId: string; top: number } | null>(null);
  const [compacting, setCompacting] = useState(false);
  const compactionBusy = compacting || contextCompactionInProgress;
  const directoryReadOnly = workspaceAvailability?.outcome === 'MISSING' || workspaceAvailability?.outcome === 'UNAVAILABLE';
  const readOnlyDock = isObserver || directoryReadOnly || historyOnly;
  const sessionStatusLabel = isObserver ? '观察中'
    : workspaceAvailability?.outcome === 'MISSING' ? '目录丢失'
    : workspaceAvailability?.outcome === 'UNAVAILABLE' ? '目录不可用'
    : session.status === 'running' ? '进行中'
    : session.status === 'completed' ? '已完成'
    : session.status === 'interrupted' ? '已中断' : undefined;
  const [sessionActionsOpen, setSessionActionsOpen] = useState(false);
  const [permissionOpen, setPermissionOpen] = useState(false);
  const [skillOpen, setSkillOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [menuSession, setMenuSession] = useState(session.id);
  if (menuSession !== session.id) {
    setMenuSession(session.id);
    setSessionActionsOpen(false);
    setAddOpen(false);
    setSkillOpen(false);
  }
  const addTrigger = useRef<HTMLButtonElement>(null);
  const addMenu = useRef<HTMLDivElement>(null);
  const [modelOpen, setModelOpen] = useState(false);
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const [previousCanControl, setPreviousCanControl] = useState(canControl);
  if (previousCanControl !== canControl) {
    setPreviousCanControl(canControl);
    if (!canControl) {
      setSessionActionsOpen(false); setAddOpen(false); setSkillOpen(false);
      setPermissionOpen(false); setModelOpen(false); setReasoningOpen(false);
    }
  }
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
  const canonicalSubmission = (item: LocalPromptSubmission) => messages.some(message => message.inputSource?.commandId === item.commandId
    && (!item.queueItemId || message.inputSource.queueItemId === item.queueItemId));
  const conversationSubmissions = localSubmissions.filter(item => !canonicalSubmission(item) && item.displayAsMessage
    && item.deliveryMode === 'new-turn' && item.outcomeCode !== 'USER_REDIRECTED_TO_STEER');
  const preparingSubmission = !isRunning && runtimeStatus === 'online'
    && conversationSubmissions.find(item => ['synchronizing', 'queued'].includes(item.status));
  const awaitingFirstActivity = isRunning && runtimeStatus === 'online' && activeTurnId && !interaction
    && !messages.some(message => message.role === 'assistant' && message.turnId === activeTurnId);
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
    ...localSubmissions.filter(item => !canonicalSubmission(item) && !conversationCommandIds.has(item.commandId) && item.outcomeCode !== 'USER_REDIRECTED_TO_STEER')
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
          {queued?.inputOrigin === 'SCHEDULED_TASK' && <small>定时任务</small>}
          {local?.contentUnavailable || !item.content
            ? <p>正文未能在队列终止前完成读取。</p>
            : <PromptContentView content={item.content} variant="queue" onReadImage={onReadPromptImage} />}
          {!isObserver && <div className="composer-queue__actions">
            <button type="button" aria-label="发送" title="作为引导发送到当前任务" disabled={!queued || queued.inputOrigin === 'SCHEDULED_TASK' || busy || editingQueue || !canControl || !isRunning}
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
  const selectedModel = modelConfigurations.find((item) => item.id === modelCallBinding?.connection_id);
  const modelBindingMissing = Boolean(modelCallBinding && !selectedModel);
  const modelReady = Boolean(
    modelCallBinding
    && selectedModel?.status === 'ready'
    && (selectedModel.authentication === 'none' || selectedModel.credential_configured),
  );
  const hasCanonicalHistory = Boolean(history?.hasOlder || history?.windows.some(window => window.entryIds?.length));
  const welcome = Boolean(!hasCanonicalHistory && !history?.loading && session.id && !readOnlyDock && messages.length === 0
    && !isRunning && localSubmissions.length === 0 && queuedPrompts.length === 0
    && !interaction && initialContextBase?.base_kind !== 'SNAPSHOT' && !contextCompaction
    && session.status !== 'interrupted' && runtimeStatus === 'online'
    && !(welcomeState.sessionId === session.id && welcomeState.started));
  const hasCompactionContext = hasCanonicalHistory || messages.length > 0 || isRunning
    || initialContextBase?.base_kind === 'SNAPSHOT' || Boolean(contextCompaction);
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

  useLayoutEffect(() => {
    if (addOpen) addMenu.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus({ preventScroll: true });
  }, [addOpen, skillOpen]);
  useEffect(() => {
    if (!addOpen && !modelOpen && !reasoningOpen && !skillOpen && !permissionOpen) return;
    const close = () => {
      setAddOpen(false); setModelOpen(false); setReasoningOpen(false);
      setSkillOpen(false); setPermissionOpen(false);
    };
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !composerWrapRef.current?.contains(event.target)) close();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      close();
      if (addOpen) addTrigger.current?.focus();
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [addOpen, modelOpen, reasoningOpen, skillOpen, permissionOpen]);
  // Page context provides exact marker hosts. A latest control summary alone
  // cannot place a boundary inside a partially loaded history window.
  const [locatedFocus, setLocatedFocus] = useState<WorkbenchViewProps['focusSourceEntry']>();
  const currentSessionRef = useRef(session.id);
  const readingAnchor = useRef<HistoryReadingAnchor | undefined>(undefined);
  // A manual disclosure owns its button's viewport position until the reader
  // scrolls or explicitly navigates. Tail-follow must not move it during resize.
  const disclosureAnchor = useRef<{ element: HTMLElement; top: number } | undefined>(undefined);
  const programmaticTop = useRef<number | undefined>(undefined);
  const userScrollPending = useRef(false);
  const userScrollFrame = useRef<number | undefined>(undefined);
  const locateRequest = useRef<AbortController | undefined>(undefined);
  const loadRequests = useRef(new Map<string, Promise<void>>());
  const resetReading = useCallback(() => {
    disclosureAnchor.current = undefined;
    followLatestRef.current = false;
    setAtBottom(false);
    readingAnchor.current = threadRef.current ? captureHistoryAnchor(threadRef.current) : undefined;
  }, []);
  const beginUserScroll = useCallback((towardLatest = false) => {
    disclosureAnchor.current = undefined;
    userScrollPending.current = true;
    if (userScrollFrame.current !== undefined) cancelAnimationFrame(userScrollFrame.current);
    // A wheel/key gesture at the physical edge may produce no scroll event.
    // End suppression at the browser's next layout opportunity in either case.
    userScrollFrame.current = requestAnimationFrame(() => {
      userScrollFrame.current = undefined;
      userScrollPending.current = false;
      const thread = threadRef.current;
      if (towardLatest && thread && thread.scrollHeight - thread.scrollTop - thread.clientHeight < 1) {
        followLatestRef.current = true;
        setAtBottom(true);
        readingAnchor.current = undefined;
        programmaticTop.current = undefined;
      } else if (!followLatestRef.current && thread) readingAnchor.current = captureHistoryAnchor(thread);
    });
  }, []);
  useLayoutEffect(() => {
    currentSessionRef.current = session.id;
    disclosureAnchor.current = undefined;
    followLatestRef.current = true;
    setAtBottom(followLatestRef.current);
    readingAnchor.current = undefined;
    userScrollPending.current = false;
    programmaticTop.current = undefined;
    loadRequests.current.clear();
    return () => {
      locateRequest.current?.abort();
      if (userScrollFrame.current !== undefined) cancelAnimationFrame(userScrollFrame.current);
    };
  }, [session.id]);
  useLayoutEffect(() => {
    if (focusSourceEntry?.sessionId === session.id) followLatestRef.current = false;
  }, [focusSourceEntry, session.id]);
  const loadHistory = useCallback((windowId?: string, direction: 'older' | 'newer' = 'older'): Promise<void> => {
    if (!onLoadHistory) return Promise.resolve();
    const key = `${session.id}:${windowId ?? ''}:${direction}`;
    const existing = loadRequests.current.get(key);
    if (existing) return existing;
    userScrollPending.current = false;
    if (userScrollFrame.current !== undefined) cancelAnimationFrame(userScrollFrame.current);
    resetReading();
    const request = Promise.resolve().then(() => onLoadHistory(windowId, direction)).finally(() => {
      if (loadRequests.current.get(key) === request) loadRequests.current.delete(key);
    });
    loadRequests.current.set(key, request);
    return request;
  }, [onLoadHistory, resetReading, session.id]);
  const locateEntry = useCallback(async (entryId: string, signal: AbortSignal, annotation = false) => {
    if (signal.aborted || currentSessionRef.current !== session.id) throw new DOMException('定位已取消', 'AbortError');
    locateRequest.current?.abort();
    const request = new AbortController(); locateRequest.current = request;
    const abort = () => request.abort();
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) request.abort();
    const owner = session.id;
    resetReading(); readingAnchor.current = undefined;
    try {
      const read = annotation ? onLocateAnnotation : onLocateEntry;
      if (read) await read(entryId, request.signal);
      if (request.signal.aborted || currentSessionRef.current !== owner) throw new DOMException('定位已取消', 'AbortError');
      setLocatedFocus({ sessionId: owner, entryId });
      const thread = threadRef.current;
      if (!thread) return;
      const target = await waitForHistoryEntry(thread, entryId, request.signal);
      if (request.signal.aborted || currentSessionRef.current !== owner) throw new DOMException('定位已取消', 'AbortError');
      locatingTaskRef.current = true;
      target.scrollIntoView({ block: 'center', behavior: 'instant' });
      programmaticTop.current = thread.scrollTop;
      readingAnchor.current = captureHistoryAnchor(thread);
      locatingTaskRef.current = false;
    } finally {
      signal.removeEventListener('abort', abort);
    }
  }, [onLocateAnnotation, onLocateEntry, resetReading, session.id]);
  const locateAnnotation = useCallback((entryId: string, signal: AbortSignal) => locateEntry(entryId, signal, true), [locateEntry]);
  useEffect(() => {
    if (!focusSourceEntry || focusSourceEntry.sessionId !== session.id) return;
    const request = new AbortController();
    void Promise.resolve().then(() => locateEntry(focusSourceEntry.entryId, request.signal)).catch(error => {
      if (!request.signal.aborted) onNotify('无法定位来源', error instanceof Error ? error.message : '读取失败', 'warning');
    });
    return () => request.abort();
  }, [focusSourceEntry, session.id, locateEntry, onNotify]);

  const restoreReadingPosition = useCallback(() => {
    const thread = threadRef.current;
    if (!thread) return;
    const clicked = disclosureAnchor.current;
    if (clicked && thread.contains(clicked.element) && !clicked.element.closest('[inert]')) {
      if (!userScrollPending.current) {
        const delta = clicked.element.getBoundingClientRect().top - thread.getBoundingClientRect().top - clicked.top;
        if (Math.abs(delta) > .5) thread.scrollTop += delta;
        programmaticTop.current = thread.scrollTop;
      }
      return;
    }
    disclosureAnchor.current = undefined;
    if (followLatestRef.current) {
      const previous = thread.scrollTop;
      thread.scrollTop = thread.scrollHeight;
      if (thread.scrollTop !== previous) programmaticTop.current = thread.scrollTop;
    } else if (readingAnchor.current && !userScrollPending.current) {
      programmaticTop.current = restoreHistoryAnchor(thread, readingAnchor.current);
    }
  }, []);
  useLayoutEffect(restoreReadingPosition);

  const insertSkill = useCallback((name: string) => {
    if (editingQueue || queueClicks.current.has('composer-edit')) return;
    const marker = `/${name}`;
    if (splitSkillReferences(draft.text).some(reference => typeof reference !== 'string' && reference.name === name)) return;
    draftStore.insertText(session.id, `${marker} `, true);
    setSkillOpen(false);
    setAddOpen(false);
    window.requestAnimationFrame(() => draftStore.focus(session.id));
  }, [draft.text, draftStore, editingQueue, session.id]);

  const fileEntryBlocked = !canControl || isObserver || runtimeStatus !== 'online' || editingQueue || submitting;
  const fileDrop = useWorkbenchFileDrop({ sessionId: session.id, store: draftStore,
    blocked: () => fileEntryBlocked || queueClicks.current.has('composer-edit'), notify: onNotify });
  const pickerCurrent = useRef({ sessionId: session.id, blocked: fileEntryBlocked });
  pickerCurrent.current = { sessionId: session.id, blocked: fileEntryBlocked };
  const openFilePicker = (directory: boolean) => {
    if (fileEntryBlocked || queueClicks.current.has('composer-edit')) return;
    const selectedSession = session.id;
    setAddOpen(false);
    const host = desktopHost();
    if (!host) { onNotify('文件未加入草稿', '请从 Pulsara 桌面应用选择本机文件。', 'warning'); return; }
    void (directory ? host.chooseDirectory().then(path => path ? [path] : []) : host.chooseFiles()).then(paths => {
      if (!paths.length) return;
      if (pickerCurrent.current.sessionId !== selectedSession || pickerCurrent.current.blocked || queueClicks.current.has('composer-edit')) {
        onNotify('文件未加入草稿', '会话已切换或输入框暂不可编辑，请重新选择。', 'warning'); return;
      }
      draftStore.insertPaths(selectedSession, paths, directory, 'end');
    }).catch(error => onNotify('文件未加入草稿', error instanceof Error ? error.message : '请选择文件后重试。', 'warning'));
  };
  const updateJumpPosition = useCallback(() => {
    const workbench = workbenchRef.current;
    const composer = composerWrapRef.current;
    if (!workbench || !composer) return;
    const composerTop = composer.getBoundingClientRect().top;
    const maximum = Math.max(12, Math.floor(workbench.clientHeight - 105));
    const todoTrigger = composer.querySelector<HTMLElement>('.todo-dock__trigger');
    const todoPopover = composer.querySelector<HTMLElement>('.todo-dock__popover');
    if (todoTrigger && todoPopover) {
      // Keep the scrollable TODO list below the topbar and leave room for the
      // latest button (the same 105px ceiling used below), plus its 12px gap.
      const popoverBottom = composer.offsetTop + todoTrigger.getBoundingClientRect().top - composerTop - 9;
      todoPopover.style.maxHeight = `${Math.max(0, popoverBottom - (workbench.clientHeight - maximum) - 12)}px`;
    }
    let protrusion = 0;
    for (const element of composer.querySelectorAll<HTMLElement>('.todo-dock__trigger, .todo-dock__popover')) {
      protrusion = Math.max(protrusion, composerTop - element.getBoundingClientRect().top);
    }
    // The composer is a direct positioned child of the workbench. offsetTop
    // follows layout, unlike its viewport rect during the welcome departure
    // animation. Relative TODO protrusions cancel that shared translation.
    const requested = Math.ceil(workbench.clientHeight - composer.offsetTop + protrusion + 12);
    const next = Math.min(maximum, requested);
    setJumpBottom((current) => current === next ? current : next);
  }, []);

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

  const submit = useCallback(async () => {
    if (!canControl) return;
    if (!draft.hasContent || submitting || editingQueue) return;
    if (!modelReady) {
      if (!welcome) onNotify(
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
      setWelcomeState({ sessionId: session.id, started: true });
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
  }, [canControl, activePlanMode, draft.hasContent, draftStore, editingQueue, isRunning,
    modelBindingMissing, modelReady, onNotify, onPermissionChange, onSend,
    permission, requestPlan, session.id, setRequestPlan, submitting, welcome]);

  const changeBinding = async (binding: ModelCallBindingPayload) => {
    if (!canControl || modelBindingBusy) return;
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
  }, [atBottom, interaction?.id, isObserver, queuedCount, session.id, todo?.id, updateJumpPosition, welcome]);

  useEffect(() => {
    const thread = threadRef.current;
    const column = thread?.querySelector('.thread-column');
    if (!thread || !column || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(restoreReadingPosition);
    observer.observe(column);
    return () => observer.disconnect();
  }, [session.id, restoreReadingPosition]);

  useEffect(() => {
    if (!focusTaskHighlighted || !focusTaskId || focusTaskRevision < 1) return;
    const thread = threadRef.current;
    if (!thread) return;
    resetReading(); readingAnchor.current = undefined;
    const locate = () => {
      const target = [...thread.querySelectorAll<HTMLElement>('[data-task-id]')].find(element => element.dataset.taskId === focusTaskId);
      if (!target) return;
      target.querySelector<HTMLButtonElement>('.subagent-run__header')?.focus({ preventScroll: true });
      target.scrollIntoView({ block: 'center', behavior: 'instant' });
      programmaticTop.current = thread.scrollTop;
      readingAnchor.current = captureHistoryAnchor(thread);
      observer.disconnect();
    };
    const observer = new MutationObserver(locate);
    observer.observe(thread, { childList: true, subtree: true });
    locate();
    return () => observer.disconnect();
  }, [focusTaskHighlighted, focusTaskId, focusTaskRevision, session.id, resetReading]);

  return (
    <HistoryHydrationContext.Provider value={onHydrateEntries}>
    <ResponseAnnotations sessionId={session.id} store={draftStore} onLocate={locateAnnotation} onNotify={onNotify}
      canAnnotate={canControl && !isObserver && runtimeStatus === 'online' && Boolean(session.id) && !editingQueue}>
    <section className={`workbench${welcome ? ' is-welcome' : ''}${fileDrop.dragging ? ' is-file-dragging' : ''}`} aria-label="会话工作台" ref={workbenchRef} {...fileDrop.handlers}>
      {fileDrop.dragging && <div className="workbench-file-drop" role="status">
        <span><Upload size={22} aria-hidden="true" />松开以添加文件</span>
      </div>}
      <header className="topbar" onDoubleClick={event => { if (!(event.target as HTMLElement).closest('button')) void desktopHost()?.toggleMaximize(); }}>
        <div className="session-title">
          {sessionStatusLabel && <span className={`live-badge live-badge--${readOnlyDock ? "readonly" : session.status}`}>{sessionStatusLabel}</span>}
          <div><div className="session-title-heading"><h2>{session.title}</h2>
            {onOpenScheduledTask && <button type="button" className="session-schedule-button" title="查看此会话的活跃定时任务" aria-label="查看此会话的活跃定时任务" onClick={onOpenScheduledTask}><AlarmClock size={16} aria-hidden="true" /></button>}
          </div><p>{workspace.kind === 'quick' ? '快速开始' : '指定目录'} · {workspace.path}</p></div>
        </div>
        <div className="topbar-actions">
          {queuedDisplayCount > 0 && <span className="queue-badge">{queuedDisplayCount} 条等待处理</span>}
          <div className="popover-anchor session-actions" ref={sessionActionsRef}>
            <button
              ref={sessionActionsTriggerRef}
              className={`icon-button${sessionActionsOpen ? ' is-active' : ''}`}
              type="button"
              aria-label="更多会话操作"
              aria-expanded={sessionActionsOpen}
              disabled={!session.id || runtimeReopenBusy || !canControl}
              onClick={() => setSessionActionsOpen((open) => !open)}
            >
              {runtimeReopenBusy ? <LoaderCircle size={15} className="session-actions__busy" /> : <MoreHorizontal size={16} />}
            </button>
            {sessionActionsOpen && <div className="menu-popover session-actions-menu" aria-label="会话操作">
              <button type="button" onClick={() => { if (!canControl) return; setSessionActionsOpen(false); onRenameSession(); }}>
                <Pencil size={15} />
                <span><strong>重命名</strong><small>给你的会话起个名字</small></span>
              </button>
              <button type="button" onClick={() => { if (!canControl) return; setSessionActionsOpen(false); onReopenRuntime(); }}>
                <RotateCcw size={15} />
                <span><strong>重新加载会话</strong><small>从数据库重新组装上下文</small></span>
              </button>
            </div>}
          </div>
          {canControl && (
            <button
              className={`ghost-button context-compact-button${compactionBusy ? ' is-compacting' : ''}`}
              aria-label={compactionBusy ? '压缩中…' : '压缩上下文'}
              aria-busy={compactionBusy}
              disabled={compactionBusy || !hasCompactionContext || runtimeStatus !== 'online'}
              title={!hasCompactionContext ? '开始对话后即可压缩上下文' : undefined}
              onClick={() => {
                setCompacting(true);
                void onCompact().finally(() => setCompacting(false));
              }}
            >
              {compactionBusy ? <LoaderCircle size={13} aria-hidden="true" /> : <Minimize2 size={13} aria-hidden="true" />}
              <span>{compactionBusy ? '压缩中…' : '压缩上下文'}</span>
            </button>
          )}
          <button className={`icon-button${inspectorOpen ? ' is-active' : ''}`} onClick={onToggleInspector} aria-label="切换检查器"><PanelRight size={15} /></button>
        </div>
      </header>

      <div
        className="thread-scroll"
        ref={threadRef}
        style={{ overflowAnchor: 'none', scrollBehavior: 'auto' }}
        onClickCapture={event => {
          // Only in-flow disclosures move the reading anchor. ARIA expanded
          // also describes floating file/annotation popovers that add no height.
          const button = (event.target as HTMLElement).closest<HTMLElement>('button[data-thread-disclosure]');
          if (!button || !event.currentTarget.contains(button)) return;
          resetReading();
          userScrollPending.current = false;
          disclosureAnchor.current = { element: button, top: button.getBoundingClientRect().top - event.currentTarget.getBoundingClientRect().top };
        }}
        onWheel={event => {
          if (event.deltaY < 0) resetReading();
          beginUserScroll(event.deltaY > 0);
        }}
        onTouchStart={() => beginUserScroll()}
        onPointerDown={event => { if (event.target === event.currentTarget) beginUserScroll(); }}
        onPointerUp={() => { userScrollPending.current = false; }}
        onTouchEnd={() => { userScrollPending.current = false; }}
        onKeyDown={event => {
          if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) resetReading();
          if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) beginUserScroll(['ArrowDown', 'PageDown', 'End'].includes(event.key) || (event.key === ' ' && !event.shiftKey));
        }}
        onScroll={(event) => {
          const element = event.currentTarget;
          if (!userScrollPending.current && programmaticTop.current !== undefined
            && Math.abs(element.scrollTop - programmaticTop.current) < 1) { programmaticTop.current = undefined; return; }
          if (locatingTaskRef.current) return;
          if (disclosureAnchor.current && !userScrollPending.current) return;
          userScrollPending.current = false;
          const nextAtBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
          followLatestRef.current = nextAtBottom;
          setAtBottom(nextAtBottom);
          readingAnchor.current = nextAtBottom ? undefined : captureHistoryAnchor(element);
          if (!nextAtBottom && element.scrollTop < 160 && history?.hasOlder && !history.loading && !history.error) {
            void loadHistory(history.windows[0]?.id).catch(() => {});
          }
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
              <span>点击下方的“创建会话”，开始你的第一个任务。</span>
            </div>
          )}
          {initialContextBase?.base_kind === 'SNAPSHOT' && <ContextCompactionDivider inherited />}
          {history && !messages.length && !interruptionNotices?.length && (hasCanonicalHistory || history.loading || history.error) && <div className="history-boundary" role="status">
            {history.loading ? '正在读取最近记录…' : history.error ?? '此区域有已保存记录，暂无主对话正文。'}
            {onLoadHistory && (history.hasOlder || history.error) && <button type="button" disabled={history.loading} onClick={() => { void loadHistory(history.windows[0]?.id).catch(() => {}); }}>{history.error ? '重试' : '加载更早记录'}</button>}
          </div>}
          <ConversationMessages history={history} onLoadHistory={loadHistory} localSubmissions={localSubmissions} activeTurnId={activeTurnId} messages={messages} interruptionNotices={interruptionNotices} skills={skills} artifactOwnerKey={artifactOwnerKey} isRunning={isRunning}
            onReadToolArtifact={onReadToolArtifact} onReadPromptImage={onReadPromptImage}
            onReadVisualization={onReadVisualization}
            onReadVisualizationThumbnail={onReadVisualizationThumbnail}
            onNotify={onNotify} onFork={onFork} canFork={canControl}
            contextCompactionInProgress={contextCompactionInProgress}
            focusTaskId={focusTaskId} focusTaskRevision={focusTaskRevision}
            focusTaskHighlighted={focusTaskHighlighted} focusSourceEntry={locatedFocus?.sessionId === session.id ? locatedFocus : focusSourceEntry} />
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
                          : '已送达'} />
            </div>
          ))}
          {(preparingSubmission || awaitingFirstActivity) && (
            <article className="assistant-turn assistant-turn--operational assistant-turn--run-start" aria-label="Pulsara 处理状态">
              <AssistantHeading message={{ id: 'pending-preparation', role: 'assistant', time: '', body: '' }} response={false} />
              <div className="assistant-progress" role="status"><i aria-hidden="true" />
                {preparingSubmission ? '准备开始…' : '正在处理…'}
              </div>
            </article>
          )}
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
          disclosureAnchor.current = undefined;
          readingAnchor.current = undefined; userScrollPending.current = false;
          setAtBottom(true); restoreReadingPosition();
        }}>
          <ArrowDown size={13} /> 回到最新
        </button>
      )}

      {!session.id ? (
        <div className="composer-wrap composer-wrap--session-required" ref={composerWrapRef}>
          <div className="composer-frame">
            <div className="composer composer--session-required">
              <div className="session-required-composer">
                <span className="session-required-composer__icon" aria-hidden="true"><Sparkles size={15} /></span>
                <span className="session-required-composer__copy">
                  <strong>创建或选择会话后开始</strong>
                  <small>提问、写代码，或一起探索一个想法。</small>
                </span>
                <button className="secondary-action" type="button" disabled={!canCreateSession} onClick={onNewSession}>
                  <MessageSquarePlus size={13} /> 创建会话
                </button>
              </div>
            </div>
          </div>
          <p className="composer-note">当前没有活动会话</p>
        </div>
      ) : !readOnlyDock ? <div className="composer-wrap" ref={composerWrapRef}>
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
          {composerQueue}
          <div className={`composer${draft.hasContent ? ' has-content' : ''}`}>
          <div className="composer-editor" ref={composerEditorRef}>
            {((requestPlan && !isRunning) || activePlanMode) && <span className="composer-plan-status"><WandSparkles size={12} />{activePlanMode ? '规划进行中' : '本轮先规划'}</span>}
            <PromptComposer
              key={session.id}
              store={draftStore}
              sessionId={session.id}
              disabled={runtimeStatus !== 'online' || !session.id || editingQueue}
              placeholder={isRunning ? '输入下一轮任务…' : '让 Pulsara 处理复杂工作…'}
              onSubmit={() => void submit()}
              onNotify={onNotify}
              skills={skills}
              onCompletePaths={onCompletePaths}
            />
          </div>
          <div className="composer-actions">
            <div className="composer-utilities">
              <div className="composer-add-anchor">
                <button ref={addTrigger} type="button" className="composer-add-trigger" aria-label="添加文件、技能或规划" aria-haspopup="dialog" aria-expanded={addOpen}
                  disabled={fileEntryBlocked}
                  onClick={() => { setAddOpen(value => !value); setSkillOpen(false); setModelOpen(false); setReasoningOpen(false); setPermissionOpen(false); }}
                  onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); setAddOpen(true); setSkillOpen(false); setModelOpen(false); setReasoningOpen(false); setPermissionOpen(false); requestAnimationFrame(() => addMenu.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()); } }}><Plus size={18} /></button>
                {addOpen && <div ref={addMenu} className={`menu-popover composer-add-menu${skillOpen ? ' is-skills' : ''}`} role="dialog" aria-label="添加内容与模式" onKeyDown={event => {
                  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
                  const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
                  const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
                  const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
                  event.preventDefault(); buttons[next]?.focus();
                }}>
                  {skillOpen ? <>
                    <button type="button" className="mode-chip composer-skills-back" aria-label="返回添加菜单" onClick={() => setSkillOpen(false)}><ArrowLeft size={12} /><span>技能</span></button>
                    <div className="skill-menu__list">
                      {skills.map(skill => {
                        const selected = skill.configured || splitSkillReferences(draft.text).some(reference => typeof reference !== 'string' && reference.name === skill.name);
                        return <button key={`${skill.name}:${skill.location}`} className={selected ? 'is-selected' : ''} onClick={() => insertSkill(skill.name)} disabled={skill.configured || editingQueue}>
                          <span><strong>/{skill.name}</strong><small>{skill.description}</small></span>{selected && <Check size={13} />}
                        </button>;
                      })}
                    </div>
                  </> : <>
                  <button type="button" className="mode-chip" disabled={fileEntryBlocked} onClick={() => openFilePicker(false)}><FileText size={12} /> 添加文件</button>
                  <button type="button" className="mode-chip" disabled={fileEntryBlocked} onClick={() => openFilePicker(true)}><FolderPlus size={12} /> 添加文件夹</button>
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
                      disabled={submitting || editingQueue || skills.length === 0}
                    ><BookOpenText size={12} /> 技能 <ChevronRight className="composer-skill-chevron" size={10} /></button>
                <button
                  className={`mode-chip${(requestPlan && !isRunning) || activePlanMode ? ' is-active' : ''}`}
                  onClick={() => { setRequestPlan((value) => !value); setAddOpen(false); draftStore.focus(session.id); }}
                  disabled={isRunning || activePlanMode || submitting}
                  aria-pressed={requestPlan && !isRunning}
                  title={isRunning ? '当前运行结束后可为下一轮启用规划' : undefined}
                ><WandSparkles size={12} /> {activePlanMode ? '规划进行中' : requestPlan && !isRunning ? '本轮先规划' : '先规划'}</button>
                </>}
                </div>}
              </div>
              <div className="popover-anchor">
                <button className={`mode-chip permission-chip${permission === 'bypass-permissions' ? ' is-danger' : ''}`} onClick={() => {setPermissionOpen((value) => !value); setAddOpen(false); setReasoningOpen(false); setSkillOpen(false); setModelOpen(false);}} aria-expanded={permissionOpen} disabled={submitting}>
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
            <div className="composer-controls">
              {modelReady && onReadContextUsage && <ContextUsageIndicator
                sessionId={session.id} binding={modelCallBinding!} revision={contextUsageRevision}
                isRunning={isRunning} readUsage={onReadContextUsage} />}
              <div className="popover-anchor model-picker">
                <button aria-label={modelBindingMissing ? '模型配置已删除' : selectedModel?.display_name ?? selectedModel?.model_id ?? '选择模型'} className={`mode-chip model-chip${modelOpen ? ' is-active' : ''}${welcome && !modelReady && !modelOpen ? ' needs-selection' : ''}`} title={modelBindingMissing ? '模型配置已删除' : modelConnectionLabel(selectedModel)} onClick={() => { setModelOpen((value) => !value); setAddOpen(false); setReasoningOpen(false); setSkillOpen(false); setPermissionOpen(false); }} aria-expanded={modelOpen} disabled={modelBindingBusy}>
                  <Bot className="model-chip__icon" size={15} aria-hidden="true" /><span className="model-chip__label">{modelBindingMissing ? '模型配置已删除' : selectedModel?.display_name ?? selectedModel?.model_id ?? '选择模型'}</span><ChevronDown className="model-chip__chevron" size={10} />
                </button>
                {modelOpen && <div className="menu-popover model-menu">
                  <div className="model-menu__header">
                    <span className="menu-label">此会话的模型</span>
                    <button type="button" className="model-menu__settings" aria-label="模型设置" title="模型设置" onClick={onOpenModelSettings}><Settings size={15} /></button>
                  </div>
                  <div className="model-menu__list">
                  {modelConfigurations.length ? modelConfigurations.map((connection) => <button key={connection.id} className={connection.id === modelCallBinding?.connection_id ? 'is-selected' : ''} disabled={connection.status !== 'ready' || modelBindingBusy} onClick={() => void changeBinding({ connection_id: connection.id, reasoning: connection.default_reasoning ?? null })}>
                    <span><strong>{connection.route_name ?? connection.route_id} · {connection.display_name ?? connection.model_id}</strong><small>{connection.wire_api === 'openai_responses' ? 'Responses' : 'Chat Completions'} · {connection.authentication === 'none' ? '无需认证' : connection.credential_configured ? '密钥已配置' : '密钥未配置'}</small></span>
                    {connection.id === modelCallBinding?.connection_id && <Check size={13} />}
                  </button>) : <div className="model-menu__empty">还没有模型配置。</div>}
                  </div>
                </div>}
              </div>
              <div className="popover-anchor">
                <button aria-label={reasoningSelectionLabel(selectedModel, modelCallBinding?.reasoning)} title={reasoningSelectionLabel(selectedModel, modelCallBinding?.reasoning)} className={`mode-chip reasoning-chip${reasoningOpen ? ' is-active' : ''}`} onClick={() => { setReasoningOpen((value) => !value); setAddOpen(false); setModelOpen(false); setSkillOpen(false); setPermissionOpen(false); }} aria-expanded={reasoningOpen} disabled={!selectedModel || selectedModel.reasoning.kind !== 'selectable' || modelBindingBusy}>
                  <BrainCircuit size={15} aria-hidden="true" /><span className="reasoning-chip__label">{reasoningSelectionLabel(selectedModel, modelCallBinding?.reasoning)}</span> {selectedModel?.reasoning.kind === 'selectable' && <ChevronDown className="reasoning-chip__chevron" size={10} />}
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
              <div className="composer-submit">
                {isRunning && !draft.hasContent ? (
                  <button
                    className="send-button is-stop"
                    onClick={onStop}
                    aria-label="停止本轮运行"
                    title="停止主助手本轮生成和后续执行；已启动操作仍按各自规则收尾，任务、排队输入和后台命令不会自动取消。"
                  ><Square size={14} fill="currentColor" strokeWidth={0} aria-hidden="true" /></button>
                ) : (
                  <button className="send-button" onClick={() => void submit()} disabled={!draft.hasContent || draft.pendingFiles > 0 || draft.failedFiles > 0 || submitting || runtimeStatus !== 'online' || !session.id || (!modelReady && !welcome)} aria-label={isRunning ? '排队发送' : '发送'}>
                    {isRunning ? <Play size={14} fill="currentColor" /> : <ArrowUp size={16} />}
                  </button>
                )}
              </div>
            </div>
          </div>
          </div>
        </div>
        {!welcome && !modelReady && <p className="composer-note composer-note--attention">{modelBindingMissing ? '原模型配置已删除，请重新选择' : '请先为此会话选择模型配置'}</p>}
      </div> : (
        <div className="observer-wrap composer-wrap" ref={composerWrapRef}>
          {composerQueue}
          <WorkspaceRecoveryDock isObserver={isObserver} availability={workspaceAvailability} busy={workspaceRecoveryBusy} error={workspaceRecoveryError}
            onTakeControl={onTakeControl} onRestore={onRestoreWorkspace} onRecheck={onRecheckWorkspace} onNotify={onNotify} />
        </div>
      )}
    </section>
    </ResponseAnnotations>
    </HistoryHydrationContext.Provider>
  );
}
