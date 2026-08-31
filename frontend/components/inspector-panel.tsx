'use client';

import {
  AlertTriangle,
  Ban,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Clock3,
  GitFork,
  Layers3,
  ListChecks,
  LocateFixed,
  LoaderCircle,
  PanelRightClose,
  RefreshCw,
  Sparkles,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { AgentTask, PermissionMode, SessionSummary, TaskStatus, TodoRun } from '../lib/pulsara-types';
import { permissionLabels } from '../lib/pulsara-types';

interface InspectorPanelProps {
  session: SessionSummary;
  isOpen: boolean;
  agentTasks: AgentTask[];
  todo?: TodoRun;
  loading: boolean;
  canControl: boolean;
  permission: PermissionMode;
  error?: string;
  onRetry: () => void;
  onLocate: (taskId: string) => void;
  onAcceptResult: (task: AgentTask) => void;
  onClose: () => void;
}

type TaskFilter = 'all' | 'active' | 'attention' | 'settled';

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

function Markdown({ children }: { children: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>;
}

function TaskCard({
  task,
  canControl,
  permission,
  onLocate,
  onAcceptResult,
}: {
  task: AgentTask;
  canControl: boolean;
  permission: PermissionMode;
  onLocate: (taskId: string) => void;
  onAcceptResult: (task: AgentTask) => void;
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
            <div className="session-task__markdown"><Markdown>{task.objective || '未提供单独目标。'}</Markdown></div>
          </section>

          <dl className="session-task__facts">
            <div><dt>分工</dt><dd>{task.role}</dd></div>
            <div><dt>上下文</dt><dd>{task.context?.mode === 'last-n' ? `最近 ${task.context.lastNTurns ?? 0} 轮` : '独立上下文'}</dd></div>
            {acceptedAt && <div><dt>创建于</dt><dd>{acceptedAt}</dd></div>}
            {terminalAt && <div><dt>结束于</dt><dd>{terminalAt}</dd></div>}
          </dl>

          {explanation && <p className="session-task__explanation">{explanation}</p>}

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
                {task.result.accepted && <small><Check size={10} /> 已带入会话</small>}
              </header>
              {task.result.summary && <div className="session-task__markdown"><Markdown>{task.result.summary}</Markdown></div>}
              {task.result.outputPreview && (
                <details>
                  <summary>查看输出摘录</summary>
                  <div className="session-task__markdown"><Markdown>{task.result.outputPreview}</Markdown></div>
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
            {canControl && task.result && !task.result.accepted && (
              <span className="session-task__continue-action">
                <button
                  className="is-primary"
                  type="button"
                  aria-describedby={`${task.id}-continue-help`}
                  onClick={() => onAcceptResult(task)}
                >
                  <Sparkles size={11} /> 带入会话并继续
                </button>
                <span id={`${task.id}-continue-help`} className="session-task__continue-tooltip" role="tooltip">
                  把这份子任务结果作为新消息交给 Pulsara，并以“{permissionLabels[permission]}”权限立即继续处理。
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
  session,
  isOpen,
  agentTasks,
  todo,
  loading,
  canControl,
  permission,
  error,
  onRetry,
  onLocate,
  onAcceptResult,
  onClose,
}: InspectorPanelProps) {
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
    <aside className={`inspector-panel${isOpen ? ' is-open' : ''}`} aria-label="当前会话的子任务">
      <header className="inspector-tabs">
        <span><strong>当前会话</strong><small>{session.title}</small></span>
        <button className="inspector-close" onClick={onClose} aria-label="关闭检查器"><PanelRightClose size={14} /></button>
      </header>
      <div className="inspector-scroll">
        <section className="inspector-section task-overview">
          <div className="section-label"><span>任务概览</span>{loading && <small><LoaderCircle size={10} /> 正在同步</small>}</div>
          <div className="task-overview__stats">
            <div><strong>{agentTasks.length}</strong><span>全部</span></div>
            <div><strong>{activeCount}</strong><span>进行中</span></div>
            <div className={attentionCount ? 'has-attention' : ''}><strong>{attentionCount}</strong><span>需留意</span></div>
            <div><strong>{todo?.items.length ? `${completedTodo}/${todo.items.length}` : '—'}</strong><span>本轮清单</span></div>
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
                <div>{tasks.map((task) => <TaskCard key={task.id} task={task} canControl={canControl} permission={permission} onLocate={onLocate} onAcceptResult={onAcceptResult} />)}</div>
              </section>
            ))}
          </div>
        </section>
      </div>
    </aside>
  );
}
