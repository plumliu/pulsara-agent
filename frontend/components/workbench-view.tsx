'use client';

import {
  ArrowDown,
  Bot,
  Braces,
  Check,
  ChevronDown,
  ChevronRight,
  CircleStop,
  Clipboard,
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
  Sparkles,
  TerminalSquare,
  UserRound,
  WandSparkles,
  Zap,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type {
  RuntimeInteractionContent,
  RuntimeInteractionResolution,
  RuntimeInteractionSummary,
} from '../lib/runtime-adapter';
import type { Message, PermissionMode, ReasoningBlock, RuntimeStatus, SessionSummary, SubagentRun, TodoRun, ToolTrace, Workspace } from '../lib/pulsara-types';
import { permissionLabels } from '../lib/pulsara-types';

interface WorkbenchViewProps {
  workspace: Workspace;
  session: SessionSummary;
  messages: Message[];
  todo?: TodoRun;
  activePlanMode: boolean;
  isRunning: boolean;
  inspectorOpen: boolean;
  queuedCount: number;
  runtimeStatus: RuntimeStatus;
  runtimeError?: string;
  modelName?: string;
  interaction?: RuntimeInteractionSummary;
  canControl: boolean;
  isObserver: boolean;
  permission: PermissionMode;
  focusTaskId?: string;
  focusTaskRevision: number;
  onReconnect: () => void;
  onTakeControl: () => void;
  onOpenSidebar: () => void;
  onToggleInspector: () => void;
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

function TraceCard({ trace }: { trace: ToolTrace }) {
  const [expanded, setExpanded] = useState(false);
  const Icon = traceIcons[trace.kind];
  const expandable = Boolean(trace.command || trace.output?.length);
  const summary = (
    <>
      <span className={`trace-icon trace-icon--${trace.kind}`}><Icon size={14} /></span>
      <span className="trace-summary-copy">
        <strong>{trace.title}</strong>
        <small>{trace.subtitle}</small>
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
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
          >{summary}</button>
        ) : <div className="trace-card__summary">{summary}</div>}
        {expanded && (
          <div className="terminal-output">
            {trace.command && <div className="terminal-command"><span>$</span> {trace.command}</div>}
            {trace.output?.map((line, index) => (
              <div className={index === trace.output!.length - 1 ? 'terminal-success' : ''} key={`${trace.id}-${line}`}>
                {line}{index === trace.output!.length - 1 && trace.status === 'running' ? <span className="terminal-cursor" /> : null}
              </div>
            ))}
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
}: {
  todo?: TodoRun;
  interactionOpen: boolean;
}) {
  const [open, setOpen] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);
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

  if (!todo || items.length === 0) return null;

  return (
    <div className={`todo-dock${expanded ? ' is-open' : ''}`} ref={containerRef}>
      {expanded && (
        <section className="todo-dock__popover" id="current-todo-list" aria-label="本轮清单" aria-live="polite">
          <header className="todo-dock__header">
            <span><ListTodo size={14} /><strong>本轮清单</strong></span>
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
        aria-label={expanded ? '收起本轮清单' : '展开本轮清单'}
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

function MarkdownBody({ body }: { body: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
      }}
    >
      {body}
    </ReactMarkdown>
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
          {!expanded && <small>{reasoningPreview(block.body, running)}</small>}
        </span>
        {running && <span className="reasoning-row__live"><i />思考中</span>}
        <ChevronRight className="reasoning-row__chevron" size={12} />
      </button>
      {expanded && <div className="reasoning-row__body">{block.body}</div>}
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

function SubagentRunCard({ run, focused }: { run: SubagentRun; focused: boolean }) {
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
              {activity.traces?.length ? <div className="execution-rail subagent-execution">{activity.traces.map((trace) => <TraceCard key={trace.id} trace={trace} />)}</div> : null}
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
}: {
  runs: SubagentRun[];
  focusTaskId?: string;
  focusTaskRevision: number;
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
          focused={focusTaskId === run.id}
        />
      ))}</div>
    </section>
  );
}

function UserMessage({ message }: { message: Message }) {
  if (message.userKind === 'subagent-result') {
    const helpId = `${message.id}-subagent-result-help`;
    return (
      <article
        className="subagent-result-event"
        aria-label="已带入子任务结果"
        aria-describedby={helpId}
        tabIndex={0}
      >
        <span className="subagent-result-event__icon"><GitFork size={13} /></span>
        <div className="subagent-result-event__copy">
          <strong>已带入子任务结果</strong>
          <small>Pulsara 正在基于这份结果继续处理</small>
        </div>
        <time>{message.time}</time>
        <span id={helpId} className="subagent-result-event__tooltip" role="tooltip">
          这份子任务结果已作为新的上下文交给 Pulsara，并按你选择的本轮权限继续处理。
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
          <p>{message.body}</p>
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
        <p>{message.body}</p>
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
  focusTaskId,
  focusTaskRevision,
  onNotify,
}: {
  message: Message;
  startsAssistantRun: boolean;
  focusTaskId?: string;
  focusTaskRevision: number;
  onNotify: WorkbenchViewProps['onNotify'];
}) {
  const hasNaturalLanguage = Boolean(message.body.trim());
  const hasReasoning = Boolean(message.reasoning?.length);
  const hasOperationalContent = Boolean(
    hasReasoning || message.traces?.length || message.subagentRuns?.length,
  );
  const className = [
    'assistant-turn',
    hasNaturalLanguage ? 'assistant-turn--response' : 'assistant-turn--operational',
    hasNaturalLanguage && hasReasoning ? 'assistant-turn--response-with-reasoning' : '',
    startsAssistantRun ? 'assistant-turn--run-start' : '',
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
            <div className="response-actions">
              <button
                onClick={() => {
                  void navigator.clipboard.writeText(message.body).then(
                    () => onNotify('已复制回复'),
                    () => onNotify('无法复制回复', '浏览器没有授予剪贴板权限'),
                  );
                }}
                aria-label="复制回复"
              ><Clipboard size={12} /></button>
            </div>
          </div>
        </>
      )}

      {!hasNaturalLanguage && !hasOperationalContent && message.status === 'running' && (
        <div className="assistant-progress"><i /> 正在处理…</div>
      )}

      {message.traces && <div className="execution-rail">{message.traces.map((trace) => <TraceCard key={trace.id} trace={trace} />)}</div>}
      {message.subagentRuns?.length ? (
        <SubagentGroup runs={message.subagentRuns} focusTaskId={focusTaskId} focusTaskRevision={focusTaskRevision} />
      ) : null}
    </article>
  );
}

function findAssistantRunStarts(messages: Message[]): ReadonlySet<string> {
  const starts = new Set<string>();
  let assistantRunOpen = false;
  for (const message of messages) {
    if (message.role === 'user') {
      if (message.userKind !== 'steer') assistantRunOpen = false;
      continue;
    }
    if (!assistantRunOpen) starts.add(message.id);
    assistantRunOpen = true;
  }
  return starts;
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
  const prompt = interaction.kind === 'tool-confirmation' ? interaction.prompt : '';
  const optionsKey = interaction.kind === 'tool-confirmation' ? interaction.options.join('\u0000') : '';
  const workflowId = interaction.kind === 'tool-confirmation' ? '' : interaction.workflowId;
  const workflowRevision = interaction.kind === 'tool-confirmation' ? 0 : interaction.workflowRevision;

  useEffect(() => {
    let current = true;
    const requested: RuntimeInteractionSummary = interactionKind === 'tool-confirmation'
      ? {
        id: interactionId,
        kind: 'tool-confirmation',
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
          <span>{interaction.kind === 'tool-confirmation' ? '需要你的确认' : interaction.kind === 'plan-question' ? '规划需要你的选择' : '方案已准备好'}</span>
          <small>{interaction.kind === 'tool-confirmation' ? '只决定这一次操作' : '确认后 Pulsara 会继续这次工作'}</small>
        </div>
      </header>

      {!content && !loadError && <div className="interaction-loading"><LoaderCircle size={14} /> 正在准备内容…</div>}
      {loadError && <p className="interaction-error">{loadError}</p>}

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

export function WorkbenchView({
  workspace,
  session,
  messages,
  todo,
  activePlanMode,
  isRunning,
  inspectorOpen,
  queuedCount,
  runtimeStatus,
  runtimeError,
  modelName,
  interaction,
  canControl,
  isObserver,
  permission,
  focusTaskId,
  focusTaskRevision,
  onReconnect,
  onTakeControl,
  onOpenSidebar,
  onToggleInspector,
  onSend,
  onStop,
  onCompact,
  onReadInteraction,
  onResolveInteraction,
  onNotify,
  onPermissionChange,
}: WorkbenchViewProps) {
  const [draft, setDraft] = useState('');
  const [requestPlan, setRequestPlan] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [compacting, setCompacting] = useState(false);
  const [permissionOpen, setPermissionOpen] = useState(false);
  const [atBottom, setAtBottom] = useState(true);
  const followLatestRef = useRef(true);
  const locatingTaskRef = useRef(false);
  const threadRef = useRef<HTMLDivElement>(null);
  const wordCount = draft.trim().length;
  const lastMessageLength = messages.at(-1)?.body.length ?? 0;
  const assistantRunStarts = useMemo(() => findAssistantRunStarts(messages), [messages]);

  const composerHint = useMemo(() => {
    if (!isRunning) return 'Enter 发送 · Shift Enter 换行';
    return 'Enter 排队下一轮 · ⌘ Enter 引导当前任务';
  }, [isRunning]);

  const submit = async (steer: boolean) => {
    const value = draft.trim();
    if (!value || submitting) return;
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
    onPermissionChange('accept-edits');
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
    if (!focusTaskId || focusTaskRevision < 1) return;
    followLatestRef.current = false;
    locatingTaskRef.current = true;
    let settleFrame = 0;
    let restoreFrame = 0;
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
        const taskHeader = target.querySelector<HTMLButtonElement>('.subagent-run__header');
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
      locatingTaskRef.current = false;
    };
  }, [focusTaskId, focusTaskRevision]);

  return (
    <section className="workbench" aria-label="会话工作台">
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
          {messages.map((message) => message.role === 'user'
            ? <UserMessage key={message.id} message={message} />
            : (
              <AssistantMessage
                key={message.id}
                message={message}
                startsAssistantRun={assistantRunStarts.has(message.id)}
                focusTaskId={focusTaskId}
                focusTaskRevision={focusTaskRevision}
                onNotify={onNotify}
              />
            ))}
          {canControl && interaction && (
            <InteractionCard
              key={`${interaction.id}:${interaction.kind === 'tool-confirmation' ? 'live' : interaction.workflowRevision}`}
              interaction={interaction}
              onRead={onReadInteraction}
              onResolve={onResolveInteraction}
            />
          )}
        </div>
      </div>

      {!atBottom && (
        <button className="jump-to-bottom" onClick={() => {
          followLatestRef.current = true;
          threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' });
        }}>
          <ArrowDown size={13} /> 回到最新
        </button>
      )}

      {!isObserver ? <div className="composer-wrap">
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
          />
          <div className={`composer${draft ? ' has-content' : ''}`}>
          <div className="composer-editor">
            <Sparkles size={14} />
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
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
            <div>
              <span className="mode-chip model-chip" title="由本机启动配置提供"><Bot size={12} /> {modelName || '当前模型'}</span>
              <button
                className={`mode-chip${(requestPlan && !isRunning) || activePlanMode ? ' is-active' : ''}`}
                onClick={() => setRequestPlan((value) => !value)}
                disabled={isRunning || activePlanMode || submitting}
                aria-pressed={requestPlan && !isRunning}
                title={isRunning ? '当前运行结束后可为下一轮启用规划' : undefined}
              ><WandSparkles size={12} /> {activePlanMode ? '规划进行中' : requestPlan && !isRunning ? '本轮先规划' : '先规划'}</button>
              <div className="popover-anchor">
                <button className="mode-chip permission-chip" onClick={() => setPermissionOpen((value) => !value)} aria-expanded={permissionOpen} disabled={submitting}>
                  <ShieldCheck size={12} /> {permissionLabels[permission]} <ChevronDown size={10} />
                </button>
                {permissionOpen && (
                  <div className="menu-popover permission-menu permission-menu--composer">
                    <span className="menu-label">本轮权限</span>
                    {(Object.keys(permissionLabels) as PermissionMode[]).map((mode) => (
                      <button key={mode} className={mode === permission ? 'is-selected' : ''} onClick={() => { onPermissionChange(mode); setPermissionOpen(false); }}>
                        <span><strong>{permissionLabels[mode]}</strong><small>{mode === 'accept-edits' ? '允许编辑；敏感操作仍会询问' : mode === 'read-only' ? '只观察和读取，不做改动' : mode === 'ask-permissions' ? '每次有副作用的操作都询问' : '跳过询问，仅限可信目录'}</small></span>
                        {mode === permission && <Check size={13} />}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
            <div>
              {isRunning && !draft ? (
                <button className="send-button is-stop" onClick={onStop} aria-label="停止当前运行"><CircleStop size={15} /></button>
              ) : (
                <button className="send-button" onClick={() => void submit(false)} disabled={!draft.trim() || submitting || runtimeStatus !== 'online' || !session.id} aria-label={isRunning ? '排队发送' : '发送'}>
                  {isRunning ? <Play size={14} fill="currentColor" /> : <Send size={14} />}
                </button>
              )}
            </div>
          </div>
          </div>
        </div>
        <p className="composer-note">{composerHint} · 规划与权限只作用于本轮</p>
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
