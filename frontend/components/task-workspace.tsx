'use client';

import {
  AlertTriangle,
  Ban,
  Bot,
  Check,
  ChevronRight,
  LoaderCircle,
  Maximize2,
  Scan,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { AgentTask, SkillCapability, SubagentActivity, TaskStatus } from '../lib/pulsara-types';
import { MarkdownBody, type MarkdownNotify } from './markdown-body';
import type {
  AgentTaskActivityRecord,
  BackgroundProcess,
  BackgroundProcessPage,
  ToolArtifactPage,
} from '../lib/runtime-adapter';
import { projectAgentTaskConversation } from '../lib/runtime-adapter';
import { ConversationMessages } from './workbench-view';

const labels: Record<TaskStatus, string> = {
  pending: '待开始', running: '进行中', waiting: '等待依赖', completed: '已完成',
  cancelled: '已取消', failed: '失败', interrupted: '已中断', blocked: '依赖未完成',
};

const active = (status: TaskStatus) => (
  status === 'pending' || status === 'running' || status === 'waiting'
);

function groupTitle(tasks: AgentTask[]): string {
  if (tasks.length === 1) return tasks[0]?.label || tasks[0]?.taskKey || '子任务';
  const date = tasks[0]?.acceptedAt ? new Date(tasks[0].acceptedAt) : undefined;
  const time = date && !Number.isNaN(date.getTime())
    ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date)
    : undefined;
  return time ? `子任务组 · ${time}` : '子任务组';
}

function statusIcon(status: TaskStatus) {
  if (status === 'running') return <LoaderCircle className="is-spinning" size={12} />;
  if (status === 'completed') return <Check size={12} />;
  if (status === 'failed' || status === 'blocked') return <AlertTriangle size={12} />;
  if (status === 'cancelled' || status === 'interrupted') return <Ban size={12} />;
  return <span className="task-node__dot" />;
}

const TASK_GRAPH_NODE_WIDTH = 240;
const TASK_GRAPH_NODE_HEIGHT = 58;
const TASK_GRAPH_COLUMN_GAP = 88;
const TASK_GRAPH_ROW_GAP = 22;

interface TaskGraphEdge {
  from: string;
  to: string;
}

function layoutTaskGraph(tasks: AgentTask[], edges: TaskGraphEdge[]) {
  const incoming = new Map<string, string[]>();
  for (const edge of edges) {
    incoming.set(edge.to, [...(incoming.get(edge.to) ?? []), edge.from]);
  }
  const depths = new Map<string, number>();
  const depthOf = (taskId: string, visiting = new Set<string>()): number => {
    const known = depths.get(taskId);
    if (known !== undefined) return known;
    if (visiting.has(taskId)) return 0;
    const nextVisiting = new Set(visiting).add(taskId);
    const depth = (incoming.get(taskId) ?? []).reduce(
      (maximum, dependencyId) => Math.max(maximum, depthOf(dependencyId, nextVisiting) + 1),
      0,
    );
    depths.set(taskId, depth);
    return depth;
  };
  const levels = new Map<number, AgentTask[]>();
  for (const task of tasks) {
    const depth = depthOf(task.id);
    levels.set(depth, [...(levels.get(depth) ?? []), task]);
  }
  const columnCount = Math.max(0, ...levels.keys()) + 1;
  const maximumRows = Math.max(1, ...[...levels.values()].map((level) => level.length));
  const width = columnCount * TASK_GRAPH_NODE_WIDTH
    + (columnCount - 1) * TASK_GRAPH_COLUMN_GAP;
  const height = maximumRows * TASK_GRAPH_NODE_HEIGHT
    + (maximumRows - 1) * TASK_GRAPH_ROW_GAP;
  const positions = new Map<string, { x: number; y: number }>();
  for (const [depth, level] of levels) {
    const levelHeight = level.length * TASK_GRAPH_NODE_HEIGHT
      + (level.length - 1) * TASK_GRAPH_ROW_GAP;
    const offsetY = (height - levelHeight) / 2;
    level.forEach((task, row) => positions.set(task.id, {
      x: depth * (TASK_GRAPH_NODE_WIDTH + TASK_GRAPH_COLUMN_GAP),
      y: offsetY + row * (TASK_GRAPH_NODE_HEIGHT + TASK_GRAPH_ROW_GAP),
    }));
  }
  return { width, height, positions };
}

function TaskGraphDialog({
  tasks,
  canControl,
  onCancel,
  onNotify,
  activities,
  loadActivities,
  loadBackgroundProcesses,
  skills,
  artifactOwnerKey,
  onReadToolArtifact,
  onClose,
  opener,
}: {
  tasks: AgentTask[];
  canControl: boolean;
  onCancel: (task: AgentTask) => Promise<void>;
  onNotify: MarkdownNotify;
  activities: ReadonlyMap<string, SubagentActivity[]>;
  loadActivities: (taskId: string, cursor?: string) => Promise<{ activities: AgentTaskActivityRecord[]; nextCursor?: string }>;
  loadBackgroundProcesses: (cursor?: string) => Promise<BackgroundProcessPage>;
  skills: SkillCapability[];
  artifactOwnerKey: string;
  onReadToolArtifact: (resultEntryId: string, offsetChars: number) => Promise<ToolArtifactPage>;
  onClose: () => void;
  opener: HTMLElement | null;
}) {
  const [selectedId, setSelectedId] = useState(tasks.length === 1 ? tasks[0]?.id : undefined);
  const [activityRead, setActivityRead] = useState<{
    taskId: string;
    activities: AgentTaskActivityRecord[];
    error?: string;
  }>();
  const [processRead, setProcessRead] = useState<{
    taskId: string;
    processes: BackgroundProcess[];
    error?: string;
  }>();
  const [zoom, setZoom] = useState(1);
  const dialogRef = useRef<HTMLDivElement>(null);
  const markerId = useId().replaceAll(':', '');
  const activityRevision = useRef(0);
  const processRevision = useRef(0);
  const selected = tasks.find((task) => task.id === selectedId);
  const canonicalActivities = activityRead?.taskId === selected?.id
    ? (activityRead?.activities ?? [])
    : [];
  const activityError = activityRead?.taskId === selected?.id
    ? activityRead?.error
    : undefined;
  const projectedTaskActivities = selected ? (activities.get(selected.id) ?? []) : [];
  const conversationMessages = projectAgentTaskConversation(
    canonicalActivities,
    projectedTaskActivities,
  );
  if (
    selected?.objective
    && !conversationMessages.some((message) => message.role === 'user' && message.userKind === 'prompt')
  ) {
    const accepted = selected.acceptedAt ? new Date(selected.acceptedAt) : undefined;
    conversationMessages.unshift({
      id: `task-objective:${selected.id}`,
      role: 'user',
      userKind: 'prompt',
      time: accepted && !Number.isNaN(accepted.getTime())
        ? accepted.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
        : '任务创建时',
      body: selected.objective,
    });
  }
  const resultBody = selected?.result
    ? (selected.result.summary || selected.result.outputPreview || '')
    : '';
  if (
    selected?.result
    && resultBody
    && !conversationMessages.some((message) => (
      message.role === 'assistant' && message.body === resultBody
    ))
  ) {
    const terminal = selected.terminalAt ? new Date(selected.terminalAt) : undefined;
    conversationMessages.push({
      id: `task-result:${selected.result.id}`,
      role: 'assistant',
      assistantKind: 'terminal',
      sourceSubagentTaskId: selected.id,
      time: terminal && !Number.isNaN(terminal.getTime())
        ? terminal.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
        : '任务结束时',
      body: resultBody,
    });
  }
  const associatedProcesses = processRead?.taskId === selected?.id
    ? (processRead?.processes ?? [])
    : [];
  const processError = processRead?.taskId === selected?.id
    ? processRead?.error
    : undefined;
  const downstream = selectedId
    ? tasks.filter((task) => task.dependencies?.some((dependency) => dependency.id === selectedId))
    : [];
  const taskIds = new Set(tasks.map((task) => task.id));
  const edges = tasks.flatMap((task) => (
    (task.dependencies ?? [])
      .filter((dependency) => taskIds.has(dependency.id))
      .map((dependency) => ({ from: dependency.id, to: task.id }))
  ));
  const graph = layoutTaskGraph(tasks, edges);
  const selectedEdges = selectedId
    ? new Set(edges.filter((edge) => edge.from === selectedId || edge.to === selectedId)
      .map((edge) => `${edge.from}:${edge.to}`))
    : new Set<string>();

  useEffect(() => {
    const taskId = selected?.id;
    const requestRevision = ++activityRevision.current;
    if (!taskId) return;
    void (async () => {
      const all: AgentTaskActivityRecord[] = [];
      const seen = new Set<string>();
      let cursor: string | undefined;
      try {
        do {
          const page = await loadActivities(taskId, cursor);
          if (requestRevision !== activityRevision.current) return;
          all.push(...page.activities);
          cursor = page.nextCursor;
          if (cursor) {
            if (seen.has(cursor)) throw new Error('任务活动分页游标发生循环。');
            seen.add(cursor);
          }
        } while (cursor);
        setActivityRead({ taskId, activities: all });
      } catch (caught) {
        if (requestRevision === activityRevision.current) setActivityRead({
          taskId,
          activities: [],
          error: caught instanceof Error ? caught.message : '任务活动暂时无法读取。',
        });
      }
    })();
    return () => { activityRevision.current += 1; };
  }, [loadActivities, selected?.id]);

  useEffect(() => {
    const taskId = selected?.id;
    const requestRevision = ++processRevision.current;
    if (!taskId) return;
    void (async () => {
      const all: BackgroundProcess[] = [];
      const seen = new Set<string>();
      let cursor: string | undefined;
      try {
        do {
          const page = await loadBackgroundProcesses(cursor);
          if (requestRevision !== processRevision.current) return;
          all.push(...page.processes);
          cursor = page.nextCursor;
          if (cursor) {
            if (seen.has(cursor)) throw new Error('后台命令分页游标发生循环。');
            seen.add(cursor);
          }
        } while (cursor);
        setProcessRead({
          taskId,
          processes: all.filter((process) => (
            process.originSubagentTaskId === taskId
          )),
        });
      } catch (caught) {
        if (requestRevision === processRevision.current) setProcessRead({
          taskId,
          processes: [],
          error: caught instanceof Error ? caught.message : '关联后台命令暂时无法读取。',
        });
      }
    })();
    return () => { processRevision.current += 1; };
  }, [loadBackgroundProcesses, selected?.id]);

  useEffect(() => {
    const application = document.querySelector<HTMLElement>('main.pulsara-shell');
    const previous = application?.inert ?? false;
    if (application) application.inert = true;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    window.requestAnimationFrame(() => dialogRef.current?.focus());
    return () => {
      window.removeEventListener('keydown', onKey);
      if (application) application.inert = previous;
      window.requestAnimationFrame(() => opener?.focus());
    };
  }, [onClose, opener]);

  return createPortal(
    <div className="task-graph-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) {
        setSelectedId(undefined);
        onClose();
      }
    }}>
      <div className={`task-graph-dialog${selected ? ' has-detail' : ''}`} role="dialog" aria-modal="true" aria-label={groupTitle(tasks)} tabIndex={-1} ref={dialogRef}>
        <header>
          <span><strong>{groupTitle(tasks)}</strong><small>{tasks.length} 个节点</small></span>
          <button type="button" aria-label="关闭任务图" onClick={onClose}><X size={15} /></button>
        </header>
        <div className="task-graph-dialog__body">
          <div className="task-graph-canvas" onClick={(event) => {
            if (event.target === event.currentTarget && tasks.length > 1) setSelectedId(undefined);
          }}>
            <div className="task-graph-controls" aria-label="任务图视图控制">
              <button type="button" aria-label="缩小任务图" onClick={() => setZoom((value) => Math.max(.7, value - .1))}><ZoomOut size={13} /></button>
              <button type="button" aria-label="适应任务图" onClick={() => setZoom(1)}><Scan size={13} /></button>
              <button type="button" aria-label="放大任务图" onClick={() => setZoom((value) => Math.min(1.3, value + .1))}><ZoomIn size={13} /></button>
            </div>
            <div className="task-graph-stage" style={{
              width: graph.width,
              height: graph.height,
              transform: `scale(${zoom})`,
            }}>
              {edges.length > 0 && <svg className="task-graph-edges" viewBox={`0 0 ${graph.width} ${graph.height}`} aria-label="任务依赖关系">
                <defs><marker id={markerId} markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" /></marker></defs>
                {edges.map((edge) => {
                  const from = graph.positions.get(edge.from)!;
                  const to = graph.positions.get(edge.to)!;
                  const outgoing = edges.filter((candidate) => candidate.from === edge.from);
                  const incomingEdges = edges.filter((candidate) => candidate.to === edge.to);
                  const fromOffset = (outgoing.indexOf(edge) - (outgoing.length - 1) / 2) * 7;
                  const toOffset = (incomingEdges.indexOf(edge) - (incomingEdges.length - 1) / 2) * 7;
                  const fromX = from.x + TASK_GRAPH_NODE_WIDTH;
                  const fromY = from.y + TASK_GRAPH_NODE_HEIGHT / 2 + fromOffset;
                  const toX = to.x;
                  const toY = to.y + TASK_GRAPH_NODE_HEIGHT / 2 + toOffset;
                  const controlX = (fromX + toX) / 2;
                  const key = `${edge.from}:${edge.to}`;
                  return <path key={key} className={selectedEdges.has(key) ? 'is-selected' : ''} d={`M ${fromX} ${fromY} C ${controlX} ${fromY}, ${controlX} ${toY}, ${toX} ${toY}`} markerEnd={`url(#${markerId})`} />;
                })}
              </svg>}
              <div className="task-graph-nodes">
              {tasks.map((task) => {
                const connected = selectedId && edges.some((edge) => (
                  (edge.from === selectedId && edge.to === task.id)
                  || (edge.to === selectedId && edge.from === task.id)
                ));
                const position = graph.positions.get(task.id)!;
                return (
                <button
                  type="button"
                  key={task.id}
                  data-task-id={task.id}
                  className={`task-node task-node--${task.status}${selectedId === task.id ? ' is-selected' : ''}${connected ? ' is-connected' : ''}`}
                  aria-pressed={selectedId === task.id}
                  style={{ left: position.x, top: position.y }}
                  onClick={() => setSelectedId(task.id)}
                >
                  <span className="task-node__icon">{statusIcon(task.status)}</span>
                  <span><strong>{task.label}</strong><small>{task.role}</small></span>
                  <em>{labels[task.status]}</em>
                </button>
                );
              })}
              </div>
            </div>
          </div>
          {selected && (
            <aside className="task-node-detail" aria-label={`${selected.label} 详情`}>
              <header><span><Bot size={13} /><strong>{selected.label}</strong></span><small>{labels[selected.status]}</small></header>
              {selected.dependencies?.length ? <section><h4>依赖</h4><ul>{selected.dependencies.map((dependency) => <li key={dependency.id}><span>{dependency.label || dependency.taskKey || dependency.id}</span><small>{labels[dependency.status]}</small></li>)}</ul></section> : null}
              {selected.progress && <section><h4>最新进展</h4><p>{selected.progress}</p></section>}
              <section className="task-node-detail__activities">
                <h4>任务对话{conversationMessages.length ? ` · ${conversationMessages.length} 条消息` : ''}</h4>
                {activityError && <p className="is-attention">{activityError}</p>}
                {conversationMessages.length ? (
                  <div className="task-conversation">
                    <ConversationMessages
                      messages={conversationMessages}
                      skills={skills}
                      artifactOwnerKey={`${artifactOwnerKey}:${selected.id}`}
                      onReadToolArtifact={onReadToolArtifact}
                      onNotify={onNotify}
                      userLabel="主任务"
                      assistantLabel={selected.label || selected.role}
                    />
                  </div>
                ) : !activityError ? <p>当前规范历史中没有可显示的活动。</p> : null}
              </section>
              {selected.terminalPublicDetail && <section className="is-attention"><h4>执行结果</h4><MarkdownBody body={selected.terminalPublicDetail} onNotify={onNotify} /></section>}
              {selected.result?.diagnostics?.length ? <section className="task-node-detail__result">
                <h4>任务诊断</h4>
                {selected.result.diagnostics?.length ? <ul>{selected.result.diagnostics.map((diagnostic, index) => <li key={`${selected.result?.id}:diagnostic:${index}`}><span>{typeof diagnostic.message === 'string' ? diagnostic.message : JSON.stringify(diagnostic.message)}</span></li>)}</ul> : null}
              </section> : null}
              {canControl && active(selected.status) && <footer>
                <button className="is-danger" type="button" onClick={() => {
                  const impact = [
                    downstream.length > 0
                      ? `下游任务：${downstream.map((task) => task.label || task.taskKey || task.id).join('、')}；实际依赖结果由 kernel 结算。`
                      : undefined,
                    associatedProcesses.length > 0
                      ? `关联后台命令：${associatedProcesses.map((process) => process.command).join('、')}；取消任务不会自动终止这些命令。`
                      : undefined,
                    processError ? `关联后台命令状态暂时无法确认：${processError}` : undefined,
                  ].filter((item): item is string => Boolean(item));
                  if (impact.length > 0 && !window.confirm(
                    `取消“${selected.label || selected.taskKey || selected.id}”？\n\n${impact.join('\n')}`,
                  )) return;
                  void onCancel(selected);
                }}><Ban size={12} /> 取消任务</button>
              </footer>}
            </aside>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function TaskWorkspace({
  tasks,
  loading,
  error,
  canControl,
  onRetry,
  onCancel,
  onNotify,
  activities,
  loadActivities,
  loadBackgroundProcesses,
  skills = [],
  artifactOwnerKey = 'task-activity',
  onReadToolArtifact = async () => { throw new Error('完整工具输出暂时无法读取。'); },
}: {
  tasks: AgentTask[];
  loading: boolean;
  error?: string;
  canControl: boolean;
  onRetry: () => void;
  onCancel: (task: AgentTask) => Promise<void>;
  onNotify: MarkdownNotify;
  activities: ReadonlyMap<string, SubagentActivity[]>;
  loadActivities: (taskId: string, cursor?: string) => Promise<{ activities: AgentTaskActivityRecord[]; nextCursor?: string }>;
  loadBackgroundProcesses: (cursor?: string) => Promise<BackgroundProcessPage>;
  skills?: SkillCapability[];
  artifactOwnerKey?: string;
  onReadToolArtifact?: (resultEntryId: string, offsetChars: number) => Promise<ToolArtifactPage>;
}) {
  const [openGroup, setOpenGroup] = useState<string>();
  const [opener, setOpener] = useState<HTMLElement | null>(null);
  const groups = useMemo(() => {
    const value = new Map<string, AgentTask[]>();
    for (const task of tasks) {
      if (!task.batchId) continue;
      const id = task.batchId;
      value.set(id, [...(value.get(id) ?? []), task]);
    }
    return [...value.entries()];
  }, [tasks]);
  const selected = groups.find(([id]) => id === openGroup)?.[1];
  const incomplete = tasks.filter((task) => !task.batchId);

  if (error) return <div className="task-inventory-notice task-inventory-notice--error"><AlertTriangle size={15} /><span><strong>任务没有读完整</strong><small>{error}</small></span><button onClick={onRetry}>重试</button></div>;
  if (incomplete.length) return <div className="task-inventory-notice task-inventory-notice--error"><AlertTriangle size={15} /><span><strong>任务批次数据不完整</strong><small>TASK_BATCH_DATA_INCOMPLETE：{incomplete.map((task) => task.id).join('、')}</small></span><button onClick={onRetry}>重试</button></div>;
  if (loading && groups.length === 0) return <div className="task-inventory-notice"><LoaderCircle className="is-spinning" size={15} /><span><strong>正在读取任务组</strong><small>恢复任务和依赖关系…</small></span></div>;
  if (!groups.length) return <div className="inspector-empty"><Bot size={18} /><span>这个会话还没有子任务</span></div>;

  return <div className="task-workspace">
    {groups.map(([id, group]) => {
      const running = group.filter((task) => active(task.status)).length;
      const attention = group.filter((task) => task.status === 'failed' || task.status === 'blocked' || task.status === 'interrupted').length;
      const completed = group.filter((task) => task.status === 'completed').length;
      return <button key={id} type="button" className="task-group-card" onClick={(event) => { setOpener(event.currentTarget); setOpenGroup(id); }}>
        <span className="task-group-card__icon"><Maximize2 size={14} /></span>
        <span><strong>{groupTitle(group)}</strong><small>{completed} 已完成 · {running} 进行中{attention ? ` · ${attention} 需留意` : ''}</small></span>
        <span className="task-group-card__progress"><i style={{ width: `${group.length ? (completed / group.length) * 100 : 0}%` }} /></span>
        <ChevronRight size={14} />
      </button>;
    })}
    {selected && <TaskGraphDialog tasks={selected} canControl={canControl} onCancel={onCancel} onNotify={onNotify} activities={activities} loadActivities={loadActivities} loadBackgroundProcesses={loadBackgroundProcesses} skills={skills} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} opener={opener} onClose={() => setOpenGroup(undefined)} />}
  </div>;
}
