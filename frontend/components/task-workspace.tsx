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
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import type { AgentTask, SkillCapability, SubagentActivity, TaskStatus } from '../lib/pulsara-types';
import { MarkdownBody, type MarkdownNotify } from './markdown-body';
import type {
  AgentTaskActivityRecord,
  AgentTaskGroup,
  BackgroundProcess,
  BackgroundProcessPage,
  ToolArtifactPage,
  ModelConfigurationSummary,
} from '../lib/runtime-adapter';
import { projectAgentTaskConversation, projectTaskActivities } from '../lib/runtime-adapter';
import { ConversationMessages } from './workbench-view';

const labels: Record<TaskStatus, string> = {
  pending: '待开始', running: '进行中', waiting: '等待依赖', completed: '已完成',
  cancelled: '已取消', failed: '失败', interrupted: '已中断', blocked: '依赖未完成',
};

const active = (status: TaskStatus) => (
  status === 'pending' || status === 'running' || status === 'waiting'
);

function TaskCapacitySummary({ readCapacity }: {
  readCapacity: () => Promise<{ target: number; occupied: number }>;
}) {
  const [occupied, setOccupied] = useState<number>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    let current = true;
    const refresh = () => { void readCapacity().then(value => {
      if (current) { setOccupied(value.occupied); setError(undefined); }
    }).catch(caught => { if (current) setError(caught instanceof Error ? caught.message : '并发状态暂时无法读取。'); }); };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { current = false; window.clearInterval(timer); };
  }, [readCapacity]);
  return <div className="task-capacity-summary">
    <span>子任务并发数</span>
    <small className={occupied ? 'is-running' : undefined}>{error ? '暂不可用' : occupied === undefined ? '读取中' : `${occupied} 运行`}</small>
    {error && <p role="alert">{error}</p>}
  </div>;
}

function taskReasoningLabel(value: unknown): string | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const selection = value as Record<string, unknown>;
  if (selection.kind === 'effort') {
    if (selection.value === null || selection.value === 'none') return '推理关闭';
    if (typeof selection.value === 'string') return `推理 ${selection.value}`;
  }
  if (selection.kind === 'toggle' && typeof selection.enabled === 'boolean') return selection.enabled ? '推理开启' : '推理关闭';
  if (selection.kind === 'budget_tokens' && typeof selection.tokens === 'number') return `推理 ${selection.tokens.toLocaleString('zh-CN')} tokens`;
  return undefined;
}

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
  external?: boolean;
}

function layoutTaskGraph(tasks: AgentTask[], edges: TaskGraphEdge[], vertical = false) {
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
  const width = vertical
    ? maximumRows * TASK_GRAPH_NODE_WIDTH + (maximumRows - 1) * TASK_GRAPH_ROW_GAP
    : columnCount * TASK_GRAPH_NODE_WIDTH + (columnCount - 1) * TASK_GRAPH_COLUMN_GAP;
  const height = vertical
    ? columnCount * TASK_GRAPH_NODE_HEIGHT + (columnCount - 1) * 64
    : maximumRows * TASK_GRAPH_NODE_HEIGHT + (maximumRows - 1) * TASK_GRAPH_ROW_GAP;
  const positions = new Map<string, { x: number; y: number }>();
  for (const [depth, level] of levels) {
    const levelHeight = level.length * TASK_GRAPH_NODE_HEIGHT
      + (level.length - 1) * TASK_GRAPH_ROW_GAP;
    const offsetY = (height - levelHeight) / 2;
    const levelWidth = level.length * TASK_GRAPH_NODE_WIDTH + (level.length - 1) * TASK_GRAPH_ROW_GAP;
    level.forEach((task, row) => positions.set(task.id, {
      x: vertical ? (width - levelWidth) / 2 + row * (TASK_GRAPH_NODE_WIDTH + TASK_GRAPH_ROW_GAP)
        : depth * (TASK_GRAPH_NODE_WIDTH + TASK_GRAPH_COLUMN_GAP),
      y: vertical ? depth * (TASK_GRAPH_NODE_HEIGHT + 64)
        : offsetY + row * (TASK_GRAPH_NODE_HEIGHT + TASK_GRAPH_ROW_GAP),
    }));
  }
  return { width, height, positions };
}

/** Browser scrolling owns the viewport; transforms only scale the graph inside
 * an explicitly sized scroll surface so every node stays reachable. */
function TaskGraphViewport({ width, height, focusX, focusY, onMeasure, onBlankClick, children }: {
  width: number; height: number; focusX: number; focusY: number; children: ReactNode; onMeasure: (width: number) => void; onBlankClick: () => void;
}) {
  const canvas = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({width: 0, height: 0});
  const [zoom, setZoom] = useState(1);
  const [dragging, setDragging] = useState(false);
  const pan = useRef<{id: number; x: number; y: number; left: number; top: number} | undefined>(undefined);
  const pendingCenter = useRef<{x: number; y: number} | undefined>(undefined);
  const moved = useRef(false);
  const spaceWidth = Math.max(size.width, width * zoom + 52);
  const spaceHeight = Math.max(size.height, height * zoom + 104);
  const left = (spaceWidth - width * zoom) / 2;
  const top = (spaceHeight - height * zoom) / 2;
  const fitZoom = Math.min(1, Math.max(1, size.width - 52) / width, Math.max(1, size.height - 104) / height);

  useLayoutEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const measure = () => {
      setSize({width: element.clientWidth, height: element.clientHeight});
      if (element.clientWidth) onMeasure(element.clientWidth);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [onMeasure]);
  useLayoutEffect(() => {
    // On opening or changing graph direction/focus, show a local task.
    // Scrollbar-driven size changes must not reset a zoomed or panned view.
    pendingCenter.current = {x: focusX, y: focusY};
  }, [width, height, focusX, focusY]);
  useLayoutEffect(() => {
    const element = canvas.current;
    const center = pendingCenter.current;
    if (!element || !center || !size.width) return;
    element.scrollLeft = left + center.x * zoom - size.width / 2;
    element.scrollTop = top + center.y * zoom - size.height / 2;
    pendingCenter.current = undefined;
  }, [zoom, left, top, size, focusX, focusY, width, height]);
  const changeZoom = (next: number, fit = false) => {
    const element = canvas.current;
    if (!element) return;
    const center = fit ? {x: width / 2, y: height / 2} : {
      x: (element.scrollLeft + size.width / 2 - left) / zoom,
      y: (element.scrollTop + size.height / 2 - top) / zoom,
    };
    pendingCenter.current = center;
    if (next === zoom) {
      element.scrollLeft = left + center.x * zoom - size.width / 2;
      element.scrollTop = top + center.y * zoom - size.height / 2;
      pendingCenter.current = undefined;
    } else setZoom(next);
  };
  const stopPan = () => { pan.current = undefined; setDragging(false); };
  return <div className="task-graph-surface">
    <div className="task-graph-controls" aria-label="任务图视图控制">
      <button type="button" aria-label="缩小任务图" onClick={() => changeZoom(Math.max(fitZoom / 2, zoom / 1.25))}><ZoomOut size={13} /></button>
      <button type="button" aria-label="适应任务图" onClick={() => changeZoom(fitZoom, true)}><Scan size={13} /></button>
      <button type="button" aria-label="原始大小" onClick={() => changeZoom(1)}>1:1</button>
      <button type="button" aria-label="放大任务图" onClick={() => changeZoom(Math.min(2, zoom * 1.25))}><ZoomIn size={13} /></button>
    </div>
    <div ref={canvas} className={`task-graph-canvas${dragging ? ' is-panning' : ''}`} role="region" aria-label="任务图画布" tabIndex={0}
      onPointerDown={event => {
        if (event.pointerType !== 'mouse' || event.button !== 0 || (event.target as HTMLElement).closest('button, a, input, textarea')) return;
        const element = event.currentTarget;
        pan.current = {id: event.pointerId, x: event.clientX, y: event.clientY, left: element.scrollLeft, top: element.scrollTop};
        moved.current = false;
        element.setPointerCapture(event.pointerId);
      }}
      onPointerMove={event => {
        const start = pan.current;
        if (!start || start.id !== event.pointerId) return;
        const dx = event.clientX - start.x;
        const dy = event.clientY - start.y;
        if (!moved.current && Math.hypot(dx, dy) < 4) return;
        moved.current = true;
        setDragging(true);
        event.currentTarget.scrollLeft = start.left - dx;
        event.currentTarget.scrollTop = start.top - dy;
      }}
      onPointerUp={event => {
        if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
        stopPan();
      }}
      onPointerCancel={stopPan} onLostPointerCapture={stopPan}
      onClick={event => {
        if (moved.current) { moved.current = false; return; }
        if (!(event.target as HTMLElement).closest('button, a, input, textarea')) onBlankClick();
      }}>
      <div className="task-graph-space" style={{width: spaceWidth, height: spaceHeight}}>
        <div className="task-graph-stage" style={{width, height, left, top, transform: `scale(${zoom})`}}>{children}</div>
      </div>
    </div>
  </div>;
}

function TaskGraphDialog({
  tasks,
  loadTask,
  modelConfigurations,
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
  loadTask?: (taskId: string) => Promise<AgentTask>;
  modelConfigurations: readonly ModelConfigurationSummary[];
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
  const [externalRead, setExternalRead] = useState<{ id: string; task?: AgentTask; error?: string }>();
  const [externalRetry, setExternalRetry] = useState(0);
  const localSelected = tasks.find(task => task.id === selectedId);
  const isLocal = Boolean(localSelected);
  const dependencyStatus = tasks.flatMap(task => task.dependencies ?? []).find(task => task.id === selectedId)?.status;
  useEffect(() => {
    if (!selectedId || isLocal) return;
    let current = true;
    setExternalRead(previous => previous?.id === selectedId ? { ...previous, error: undefined } : { id: selectedId });
    void (async () => {
      try {
        if (!loadTask) throw new Error('任务详情暂时无法读取。');
        const task = await loadTask(selectedId);
        if (current) setExternalRead({ id: selectedId, task });
      } catch (error) {
        if (current) setExternalRead({ id: selectedId, error: error instanceof Error ? error.message : '任务详情暂时无法读取。' });
      }
    })();
    return () => { current = false; };
  }, [selectedId, isLocal, dependencyStatus, loadTask, externalRetry]);
  const [activityRead, setActivityRead] = useState<{
    taskId: string;
    activities: AgentTaskActivityRecord[];
    nextCursor?: string;
    error?: string;
  }>();
  const [processRead, setProcessRead] = useState<{
    taskId: string;
    processes: BackgroundProcess[];
    nextCursor?: string;
    error?: string;
  }>();
  const dialogRef = useRef<HTMLDivElement>(null);
  const [vertical, setVertical] = useState(false);
  const measureCanvas = useCallback((width: number) => setVertical(width < 600), []);
  const markerId = useId().replaceAll(':', '');
  const activityRevision = useRef(0);
  const processRevision = useRef(0);
  const selected = localSelected
    ?? (externalRead?.id === selectedId ? externalRead?.task : undefined);
  const selectedModel = modelConfigurations.find((model) => model.id === selected?.modelConnectionId && model.model_id === selected?.modelId);
  const modelName = selectedModel?.display_name || selected?.modelId;
  const reasoningLabel = taskReasoningLabel(selected?.reasoning);
  const canonicalActivities = activityRead?.taskId === selected?.id
    ? (activityRead?.activities ?? [])
    : [];
  const activityError = activityRead?.taskId === selected?.id
    ? activityRead?.error
    : undefined;
  const projectedTaskActivities = selected ? projectTaskActivities(selected, activities.get(selected.id) ?? []) : [];
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
  const boundaryTasks = useMemo(() => {
    const localIds = new Set(tasks.map((task) => task.id));
    const sources = new Map<string, AgentTask>();
    for (const task of tasks) for (const id of task.dependencyIds) {
      if (localIds.has(id) || sources.has(id)) continue;
      const detail = task.dependencies?.find((item) => item.id === id);
      sources.set(id, {
        id, label: detail?.label || detail?.taskKey || id,
        role: '其他任务组 · 依赖来源', objective: '', status: detail?.status || 'waiting',
        completionAccepted: false, dependencyIds: [], color: 'violet',
      });
    }
    return [...sources.values()];
  }, [tasks]);
  // A status or summary update changes task rows, but never their positions.
  // Keep graph geometry until task membership or dependency structure changes.
  const structureKey = JSON.stringify([vertical, tasks.map((task) => [task.id, task.dependencyIds])]);
  const structure = useRef<{
    key: string;
    edges: TaskGraphEdge[];
    graph: ReturnType<typeof layoutTaskGraph>;
  }>(undefined);
  if (structure.current?.key !== structureKey) {
    const localIds = new Set(tasks.map((task) => task.id));
    const edges = tasks.flatMap((task) => task.dependencyIds.map((id) => ({
      from: id, to: task.id, external: !localIds.has(id),
    })));
    structure.current = {
      key: structureKey,
      edges,
      graph: layoutTaskGraph([...boundaryTasks, ...tasks], edges, vertical),
    };
  }
  const { edges, graph } = structure.current;
  const selectedEdges = selectedId
    ? new Set(edges.filter((edge) => edge.from === selectedId || edge.to === selectedId)
      .map((edge) => `${edge.from}:${edge.to}`))
    : new Set<string>();

  useEffect(() => {
    const taskId = selected?.id;
    const requestRevision = ++activityRevision.current;
    if (!taskId) return;
    void (async () => {
      try {
        const page = await loadActivities(taskId);
        if (requestRevision !== activityRevision.current) return;
        setActivityRead({ taskId, activities: page.activities, nextCursor: page.nextCursor });
      } catch (caught) {
        if (requestRevision === activityRevision.current) setActivityRead({
          taskId,
          activities: [],
          error: caught instanceof Error ? caught.message : '任务活动暂时无法读取。',
        });
      }
    })();
    return () => { activityRevision.current += 1; };
  }, [loadActivities, selected?.id, selected?.status, selected?.result?.id]);

  useEffect(() => {
    const taskId = selected?.id;
    const requestRevision = ++processRevision.current;
    if (!taskId) return;
    void (async () => {
      try {
        const page = await loadBackgroundProcesses();
        if (requestRevision !== processRevision.current) return;
        setProcessRead({
          taskId,
          processes: page.processes.filter((process) => (
            process.originSubagentTaskId === taskId
          )),
          nextCursor: page.nextCursor,
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
  }, [loadBackgroundProcesses, selected?.id, selected?.status]);

  const loadMoreActivities = async () => {
    const cursor = activityRead?.nextCursor;
    const taskId = selected?.id;
    if (!cursor || !taskId) return;
    const revision = activityRevision.current;
    try {
      const page = await loadActivities(taskId, cursor);
      if (revision !== activityRevision.current) return;
      setActivityRead((current) => current?.taskId === taskId && current.nextCursor === cursor
        ? { taskId, activities: [...current.activities, ...page.activities], nextCursor: page.nextCursor }
        : current);
    } catch (caught) {
      if (revision === activityRevision.current) setActivityRead((current) => current?.taskId === taskId
        ? { ...current, error: caught instanceof Error ? caught.message : '后续任务活动暂时无法读取。' }
        : current);
    }
  };

  const loadMoreProcesses = async () => {
    const cursor = processRead?.nextCursor;
    const taskId = selected?.id;
    if (!cursor || !taskId) return;
    const revision = processRevision.current;
    try {
      const page = await loadBackgroundProcesses(cursor);
      if (revision !== processRevision.current) return;
      setProcessRead((current) => current?.taskId === taskId && current.nextCursor === cursor
        ? { taskId, processes: [...current.processes, ...page.processes.filter((process) => process.originSubagentTaskId === taskId)], nextCursor: page.nextCursor }
        : current);
    } catch (caught) {
      if (revision === processRevision.current) setProcessRead((current) => current?.taskId === taskId
        ? { ...current, error: caught instanceof Error ? caught.message : '后续后台命令暂时无法读取。' }
        : current);
    }
  };

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
      <div className={`task-graph-dialog${selectedId ? ' has-detail' : ''}`} role="dialog" aria-modal="true" aria-label={groupTitle(tasks)} tabIndex={-1} ref={dialogRef}>
        <header>
          <span><strong>{groupTitle(tasks)}</strong><small>{tasks.length} 个节点</small></span>
          <button type="button" aria-label="关闭任务图" onClick={onClose}><X size={15} /></button>
        </header>
        <div className="task-graph-dialog__body">
          <TaskGraphViewport width={graph.width} height={graph.height} onMeasure={measureCanvas}
            onBlankClick={() => { if (tasks.length > 1) setSelectedId(undefined); }}
            focusX={(graph.positions.get(selectedId ?? tasks[0]?.id)?.x ?? 0) + TASK_GRAPH_NODE_WIDTH / 2}
            focusY={(graph.positions.get(selectedId ?? tasks[0]?.id)?.y ?? 0) + TASK_GRAPH_NODE_HEIGHT / 2}>
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
                  const bottomX = from.x + TASK_GRAPH_NODE_WIDTH / 2 + fromOffset;
                  const bottomY = from.y + TASK_GRAPH_NODE_HEIGHT;
                  const topX = to.x + TASK_GRAPH_NODE_WIDTH / 2 + toOffset;
                  const middleY = (bottomY + to.y) / 2;
                  const path = vertical
                    ? `M ${bottomX} ${bottomY} C ${bottomX} ${middleY}, ${topX} ${middleY}, ${topX} ${to.y}`
                    : `M ${fromX} ${fromY} C ${controlX} ${fromY}, ${controlX} ${toY}, ${toX} ${toY}`;
                  const key = `${edge.from}:${edge.to}`;
                  return <path key={key} className={`${edge.external ? 'is-external' : ''}${selectedEdges.has(key) ? ' is-selected' : ''}`} d={path} markerEnd={`url(#${markerId})`} />;
                })}
              </svg>}
              <div className="task-graph-nodes">
              {boundaryTasks.map((task) => {
                const position = graph.positions.get(task.id)!;
                return <button type="button" key={task.id} data-task-id={task.id}
                  className={`task-node task-node--boundary task-node--${task.status}${selectedId === task.id ? ' is-selected' : ''}`}
                  style={{ left: position.x, top: position.y }} aria-label={`其他任务组依赖来源：${task.label}`}
                  aria-pressed={selectedId === task.id} onClick={() => setSelectedId(task.id)}>
                  <span className="task-node__icon">{statusIcon(task.status)}</span>
                  <span><strong>{task.label}</strong><small>{task.role}</small></span>
                  <em>{labels[task.status]}</em>
                </button>;
              })}
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
          </TaskGraphViewport>
          {selectedId && !selected && <aside className="task-node-detail" aria-label="任务详情">
            <header><strong>{boundaryTasks.find(task => task.id === selectedId)?.label}</strong></header>
            <section>{externalRead?.id === selectedId && externalRead.error
              ? <><p role="alert">{externalRead.error}</p><button type="button" onClick={() => setExternalRetry(value => value + 1)}>重新读取</button></>
              : <p role="status"><LoaderCircle size={14} className="is-spinning" /> 正在读取任务…</p>}</section>
          </aside>}
          {selected && (
            <aside className="task-node-detail" aria-label={`${selected.label} 详情`}>
              <header><span className="task-node-detail__identity"><Bot size={13} /><strong>{selected.label}</strong></span>
                <small>{labels[selected.status]}</small>
              {canControl && active(selected.status) && <span className="task-node-detail__actions">
                <button className="task-node-detail__cancel" type="button" onClick={() => {
                  const impact = [
                    downstream.length > 0
                      ? `下游任务：${downstream.map((task) => task.label || task.taskKey || task.id).join('、')}；实际依赖结果由 kernel 结算。`
                      : undefined,
                    associatedProcesses.length > 0
                      ? `关联后台命令：${associatedProcesses.map((process) => process.command).join('、')}；取消任务不会自动终止这些命令。`
                      : undefined,
                    processError ? `关联后台命令状态暂时无法确认：${processError}` : undefined,
                    processRead?.nextCursor ? '还有未加载的后台命令页；取消前可继续检查。' : undefined,
                  ].filter((item): item is string => Boolean(item));
                  if (impact.length > 0 && !window.confirm(
                    `取消“${selected.label || selected.taskKey || selected.id}”？\n\n${impact.join('\n')}`,
                  )) return;
                  void onCancel(selected);
                }}><Ban size={12} aria-hidden="true" /><span>取消任务</span></button>
              </span>}
              </header>
              {selected.dependencies?.length ? <section><h4>依赖</h4><ul>{selected.dependencies.map((dependency) => <li key={dependency.id}><span>{dependency.label || dependency.taskKey || dependency.id}</span><small>{labels[dependency.status]}</small></li>)}</ul></section> : null}
              {modelName && <section className="task-node-detail__model"><h4>模型</h4><p><span>{modelName}</span>{reasoningLabel && <small>{reasoningLabel}</small>}</p></section>}
              {selected.progress && <section><h4>最新进展</h4><p>{selected.progress}</p></section>}
              <section className="task-node-detail__activities">
                <h4>任务对话</h4>
                {activityError && <p className="is-attention">{activityError}</p>}
                {conversationMessages.length ? (
                  <div className="task-conversation">
                    <ConversationMessages
                      messages={conversationMessages}
                      isRunning={active(selected.status)}
                      taskFinalAnswerId={selected.result ? conversationMessages.find(message => (
                        message.role === 'assistant' && message.body === resultBody
                      ))?.id : undefined}
                      skills={skills}
                      artifactOwnerKey={`${artifactOwnerKey}:${selected.id}`}
                      onReadToolArtifact={onReadToolArtifact}
                      onNotify={onNotify}
                      userLabel="主任务"
                      assistantLabel={selected.label || selected.role}
                    />
                  </div>
                ) : !activityError ? <p>当前规范历史中没有可显示的活动。</p> : null}
                {activityRead?.taskId === selected.id && activityRead.nextCursor && <button type="button" onClick={() => void loadMoreActivities()}>加载更多任务活动</button>}
              </section>
              {selected.terminalPublicDetail && <section className="task-node-detail__outcome">
                <h4>执行结果{(selected.status === 'failed' || selected.status === 'blocked') && <AlertTriangle size={13} aria-hidden="true" />}</h4>
                <MarkdownBody body={selected.terminalPublicDetail} onNotify={onNotify} />
              </section>}
              {selected.result?.diagnostics?.length ? <section className="task-node-detail__result">
                <h4>任务诊断</h4>
                {selected.result.diagnostics?.length ? <ul>{selected.result.diagnostics.map((diagnostic, index) => <li key={`${selected.result?.id}:diagnostic:${index}`}><span>{typeof diagnostic.message === 'string' ? diagnostic.message : JSON.stringify(diagnostic.message)}</span></li>)}</ul> : null}
              </section> : null}
              {processRead?.taskId === selected.id && processRead.nextCursor && <section><button type="button" onClick={() => void loadMoreProcesses()}>继续检查关联后台命令</button></section>}

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
  loadTask,
  modelConfigurations = [],
  groups: providedGroups,
  loadedGroupIds,
  totalGroupCount,
  onLoadGroup,
  onUnloadGroup,
  onLoadMoreGroups,
  readCapacity,
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
  loadTask?: (taskId: string) => Promise<AgentTask>;
  modelConfigurations?: readonly ModelConfigurationSummary[];
  groups?: AgentTaskGroup[];
  loadedGroupIds?: ReadonlySet<string>;
  totalGroupCount?: number;
  onUnloadGroup?: (groupId: string) => void;
  onLoadGroup?: (groupId: string) => Promise<void>;
  onLoadMoreGroups?: () => Promise<void>;
  readCapacity?: () => Promise<{ target: number; occupied: number }>;
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
  useEffect(() => () => {
    if (openGroup) onUnloadGroup?.(openGroup);
  }, [openGroup, onUnloadGroup]);
  const [loadingGroup, setLoadingGroup] = useState<string>();
  const [groupError, setGroupError] = useState<string>();
  const [opener, setOpener] = useState<HTMLElement | null>(null);
  const loadedTasksByGroup = useMemo(() => {
    const value = new Map<string, AgentTask[]>();
    for (const task of tasks) {
      if (!task.batchId) continue;
      const id = task.batchId;
      value.set(id, [...(value.get(id) ?? []), task]);
    }
    return [...value.entries()];
  }, [tasks]);
  const groups = providedGroups ?? loadedTasksByGroup.map(([id, group]) => ({
    id, parentTurnId: group[0]?.parentId ?? '', firstAcceptedAt: group[0]?.acceptedAt ?? '',
    taskCount: group.length,
    statusCounts: {
      pending: group.filter((task) => task.status === 'pending').length,
      active: group.filter((task) => task.status === 'running').length,
      waiting: group.filter((task) => task.status === 'waiting').length,
      completed: group.filter((task) => task.status === 'completed').length,
      cancelled: group.filter((task) => task.status === 'cancelled').length,
      failed: group.filter((task) => task.status === 'failed').length,
      interrupted: group.filter((task) => task.status === 'interrupted').length,
      blocked: group.filter((task) => task.status === 'blocked').length,
    },
    singleTaskLabel: group.length === 1 ? group[0]?.label : undefined,
  }));
  const selected = loadedTasksByGroup.find(([id]) => id === openGroup)?.[1];
  const incomplete = tasks.filter((task) => !task.batchId);

  if (error) return <div className="task-inventory-notice task-inventory-notice--error"><AlertTriangle size={15} /><span><strong>任务没有读完整</strong><small>{error}</small></span><button onClick={onRetry}>重试</button></div>;
  if (incomplete.length) return <div className="task-inventory-notice task-inventory-notice--error"><AlertTriangle size={15} /><span><strong>任务批次数据不完整</strong><small>TASK_BATCH_DATA_INCOMPLETE：{incomplete.map((task) => task.id).join('、')}</small></span><button onClick={onRetry}>重试</button></div>;
  if (loading && groups.length === 0) return <div className="task-inventory-notice"><LoaderCircle className="is-spinning" size={15} /><span><strong>正在读取任务组</strong><small>恢复任务和依赖关系…</small></span></div>;
  if (!groups.length) return <div className="task-workspace">{readCapacity && <TaskCapacitySummary readCapacity={readCapacity} />}<div className="inspector-empty"><Bot size={18} /><span>这个会话还没有子任务</span></div></div>;

  return <div className="task-workspace">
    {readCapacity && <TaskCapacitySummary readCapacity={readCapacity} />}
    {groups.map((group) => {
      const running = group.statusCounts.pending + group.statusCounts.active + group.statusCounts.waiting;
      const attention = group.statusCounts.failed + group.statusCounts.blocked + group.statusCounts.interrupted;
      const completed = group.statusCounts.completed;
      const title = group.singleTaskLabel || (group.firstAcceptedAt
        ? `子任务组 · ${new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(group.firstAcceptedAt))}`
        : '子任务组');
      return <button key={group.id} type="button" className="task-group-card" onClick={(event) => {
        setOpener(event.currentTarget);
        setOpenGroup(group.id);
        if (onLoadGroup && !loadedGroupIds?.has(group.id)) {
          setLoadingGroup(group.id);
          setGroupError(undefined);
          void onLoadGroup(group.id).catch((error) => setGroupError(error instanceof Error ? error.message : '任务组暂时无法读取。')).finally(() => setLoadingGroup(undefined));
        }
      }}>
        <span className="task-group-card__icon"><Maximize2 size={14} /></span>
        <span><strong>{title}</strong><small>{completed} 已完成 · {running} 进行中{attention ? ` · ${attention} 需留意` : ''}</small></span>
        <span className="task-group-card__progress"><i style={{ width: `${group.taskCount ? (completed / group.taskCount) * 100 : 0}%` }} /></span>
        <ChevronRight size={14} />
      </button>;
    })}
    {onLoadMoreGroups && groups.length < (totalGroupCount ?? groups.length) && <button type="button" onClick={() => void onLoadMoreGroups()}>加载更多任务组</button>}
    {groupError && <p role="alert">{groupError}</p>}
    {loadingGroup && <p>正在读取任务组…</p>}
    {selected && (!loadedGroupIds || loadedGroupIds.has(openGroup!)) && <TaskGraphDialog key={`${artifactOwnerKey}:${openGroup}`} tasks={selected} loadTask={loadTask} modelConfigurations={modelConfigurations} canControl={canControl} onCancel={onCancel} onNotify={onNotify} activities={activities} loadActivities={loadActivities} loadBackgroundProcesses={loadBackgroundProcesses} skills={skills} artifactOwnerKey={artifactOwnerKey} onReadToolArtifact={onReadToolArtifact} opener={opener} onClose={() => setOpenGroup(undefined)} />}
  </div>;
}
