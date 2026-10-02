import { ToolResultDisplayContext } from '../../frontend/lib/tool-result-display';
import type { ReactElement, PropsWithChildren } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentTask } from '../../frontend/lib/pulsara-types';
import { TaskWorkspace } from '../../frontend/components/task-workspace';

function renderWithRawResults(ui: ReactElement) {
  return render(ui, { wrapper: ({ children }: PropsWithChildren) => (
    <ToolResultDisplayContext.Provider value={{ showBuiltinToolResults: true, onChange: () => {} }}>
      {children}
    </ToolResultDisplayContext.Provider>
  ) });
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} });
});

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); cleanup(); });

const task = (overrides: Partial<AgentTask> = {}): AgentTask => ({
  id: 'task-a',
  label: '精确任务',
  role: '验证者',
  objective: '检查 exact owner。',
  status: 'running',
  parentId: 'turn-root',
  batchId: 'batch-a',
  taskKey: 'verify',
  completionAccepted: false,
  dependencyIds: [],
  color: 'blue',
  ...overrides,
});

describe('TaskWorkspace PR03 hard cut', () => {
  it('shows structured task diagnostics even when they have no message field', () => {
    const diagnostic = { check: 'read_file', result: 'SUCCESS', evidence: 'total_lines=2' };
    render(<TaskWorkspace tasks={[task({ status: 'completed', result: {
      id: 'result', summary: '验证完成', diagnostics: [diagnostic, { message: '只读约束已满足' }],
    } })]} loading={false} canControl={false} onRetry={vi.fn()} onCancel={vi.fn()}
      onNotify={vi.fn()} activities={new Map()} loadActivities={vi.fn(async () => ({ activities: [] }))}
      loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))} />);
    fireEvent.click(screen.getByRole('button', { name: /精确任务/ }));
    const detail = screen.getByRole('complementary', { name: '精确任务 详情' });
    expect(within(detail).getByRole('heading', { name: '任务诊断' })).toBeTruthy();
    expect(within(detail).getByText(JSON.stringify(diagnostic))).toBeTruthy();
    expect(within(detail).getByText('只读约束已满足')).toBeTruthy();
  });

  it('keeps internal task metadata out of the detail and puts cancellation in its header', () => {
    const cancel = vi.fn(async () => undefined);
    const internalTask = task({ context: { mode: 'worker-history', historyTaskId: 'internal-history-id' },
      materialTaskIds: ['internal-material-id'],
      result: { id: 'result', summary: '可读任务结果', outputPreview: '', diagnostics: [], data: { internal: 'raw-json-value' } } });
    const props = { tasks: [internalTask], loading: false, canControl: true, onRetry: vi.fn(), onCancel: cancel,
      onNotify: vi.fn(), activities: new Map(), loadActivities: vi.fn(async () => ({ activities: [] })),
      loadBackgroundProcesses: vi.fn(async () => ({ processes: [] })) };
    const view = render(<TaskWorkspace {...props} />);
    fireEvent.click(screen.getByRole('button', { name: /精确任务/ }));
    const detail = screen.getByRole('complementary', { name: '精确任务 详情' });
    expect(detail.textContent).not.toMatch(/历史来源|终态材料来源|结构化结果|internal-history-id|internal-material-id|raw-json-value/);
    expect(within(detail).getByText('可读任务结果')).toBeTruthy();
    const button = within(detail).getByRole('button', { name: '取消任务' });
    expect(button.closest('header')?.parentElement).toBe(detail);
    fireEvent.click(button);
    expect(cancel).toHaveBeenCalledWith(internalTask);
    view.rerender(<TaskWorkspace {...props} canControl={false} />);
    expect(within(detail).queryByRole('button', { name: '取消任务' })).toBeNull();
  });

  it('shows named inherited conversation separately from the current task', async () => {
    const record = { turnId: 'turn-a', entrySequence: 1, entryKind: 'USER_MESSAGE', acceptedAt: '2026-10-02T10:00:00Z',
      objective: '原任务目标', body: '原任务目标', contentKind: 'INLINE' as const, contentDigest: 'sha256:source', contentSize: 18, blocks: [], toolResults: [] };
    render(<TaskWorkspace tasks={[task({ context: { mode: 'worker-history', historyTaskId: 'private-source-id' } })]}
      loading={false} canControl={false} onRetry={vi.fn()} onCancel={vi.fn()} onNotify={vi.fn()} activities={new Map()}
      loadActivities={vi.fn(async () => ({ inheritedContext: { sourceLabel: '初次审阅', materials: [{ label: '已压缩的上下文', body: '保留的历史摘要' }] },
        activities: [{ ...record, entryId: 'source', inherited: true, sourceTaskLabel: '初次审阅' },
          { ...record, entryId: 'source-answer', entrySequence: 2, entryKind: 'ASSISTANT_MESSAGE', turnStatus: 'COMPLETED',
            inherited: true, sourceTaskLabel: '初次审阅', body: '{"blocks":[],"draft_identity":"internal"}',
            blocks: [{ blockId: 'text', ordinal: 0, kind: 'TEXT', text: '祖先任务的最终回复' }] },
          { ...record, turnId: 'turn-b', entrySequence: 4, entryId: 'current', inherited: false, body: '检查 exact owner。' }] }))}
      loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))} />);
    fireEvent.click(screen.getByRole('button', { name: /精确任务/ }));
    const title = await screen.findByText('上下文继承自：初次审阅');
    const inherited = title.closest('details')!;
    expect(within(inherited).getByText('原任务目标')).toBeTruthy();
    expect(within(inherited).getByText('祖先任务的最终回复')).toBeTruthy();
    expect(within(inherited).getByText('保留的历史摘要')).toBeTruthy();
    expect(within(inherited).queryByText('检查 exact owner。')).toBeNull();
    const detail = screen.getByRole('complementary', { name: '精确任务 详情' });
    expect(within(detail).getAllByText('检查 exact owner。')).toHaveLength(1);
    expect(detail.textContent).not.toContain('private-source-id');
  });

  it('shows model name and reasoning without connection IDs or raw JSON', () => {
    render(<TaskWorkspace tasks={[task({modelConnectionId:'model-connection:private-id', modelId:'openai/gpt-6-luna', reasoning:{kind:'effort',value:'high'}})]}
      modelConfigurations={[{id:'model-connection:private-id', model_id:'openai/gpt-6-luna', display_name:'GPT-6 Luna'} as import('../../frontend/lib/runtime-adapter').ModelConfigurationSummary]}
      loading={false} canControl={false} onRetry={vi.fn()} onCancel={vi.fn()} onNotify={vi.fn()}
      activities={new Map()} loadActivities={vi.fn(async()=>({activities:[]}))} loadBackgroundProcesses={vi.fn(async()=>({processes:[]}))}/>);
    fireEvent.click(screen.getByRole('button',{name:/精确任务/}));
    const detail = screen.getByRole('complementary',{name:'精确任务 详情'});
    expect(within(detail).getByText('GPT-6 Luna')).toBeTruthy();
    expect(within(detail).getByText('high')).toBeTruthy();
    expect(detail.textContent).not.toContain('model-connection:');
    expect(detail.textContent).not.toContain('"kind"');
  });

  it.each([false, true])('shows read-only running capacity for control=%s', async canControl => {
    const readCapacity = vi.fn(async () => ({ target: 4, occupied: 2 }));
    render(<TaskWorkspace tasks={[]} loading={false} canControl={canControl} onRetry={vi.fn()} onCancel={vi.fn()} onNotify={vi.fn()}
      readCapacity={readCapacity} activities={new Map()}
      loadActivities={vi.fn(async () => ({ activities: [] }))} loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))} />);
    expect(await screen.findByText('2 运行')).toBeTruthy();
    expect(screen.getByText('任务并发数')).toBeTruthy();
    expect(screen.queryByRole('spinbutton')).toBeNull();
    expect(screen.queryByRole('button', { name: '应用并发上限' })).toBeNull();
    expect(screen.queryByText('上限 4')).toBeNull();
  });

  it('changes direction for narrow canvases without resetting a zoomed view when scrollbars resize it', () => {
    let width = 1000;
    let height = 770;
    let resize = () => {};
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => width);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(() => height);
    vi.stubGlobal('ResizeObserver', class { constructor(callback: () => void) { resize = callback; } observe() {} disconnect() {} });
    const tasks = Array.from({length:16},(_,i)=>task({id:`node-${i}`,label:`node-${i}`,dependencyIds:i ? [`node-${i-1}`] : []}));
    render(<TaskWorkspace tasks={tasks} loading={false} canControl={false} onRetry={vi.fn()} onCancel={vi.fn()}
      onNotify={vi.fn()} activities={new Map()} loadActivities={vi.fn(async()=>({activities:[]}))}
      loadBackgroundProcesses={vi.fn(async()=>({processes:[]}))}/>);
    fireEvent.click(screen.getByRole('button',{name:/任务组/}));
    const node = screen.getByRole('button',{name:'node-1验证者进行中'});
    expect(node.style.left).toBe('328px');
    act(()=>{width=390; resize();});
    expect(node.style.left).toBe('0px');
    expect(node.style.top).toBe('122px');
    const canvas = screen.getByRole('region',{name:'任务图画布'});
    canvas.scrollTop = 1000;
    fireEvent.click(screen.getByRole('button',{name:'放大任务图'}));
    expect(canvas.scrollTop).toBeGreaterThan(1000);
    const zoomedTop = canvas.scrollTop;
    act(()=>{width=380; height=760; resize();});
    expect(canvas.scrollTop).toBe(zoomedTop);
    fireEvent.click(screen.getByRole('button',{name:'适应任务图'}));
    expect(canvas.scrollTop).toBeCloseTo(0);
  });

  it('shows a cross-batch dependency as a dashed boundary node and edge', () => {
    render(<TaskWorkspace
      tasks={[task({
        dependencyIds: ['task-from-older-batch'],
        dependencies: [{ id: 'task-from-older-batch', label: '先前审阅', status: 'completed' }],
      })]}
      loading={false} canControl={false} onRetry={vi.fn()} onCancel={vi.fn()}
      onNotify={vi.fn()} activities={new Map()}
      loadActivities={vi.fn(async () => ({ activities: [] }))}
      loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))}
    />);
    fireEvent.click(screen.getByRole('button', { name: /精确任务/ }));
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByLabelText('其他任务组依赖来源：先前审阅')).toBeTruthy();
    expect(dialog.querySelectorAll('.task-graph-edges > path.is-external')).toHaveLength(1);
  });

  it('reads a cross-group node on demand and supports retry without inventing task details', async () => {
    const source = task({ id: 'prior', label: '先前审阅', status: 'completed', objective: '核对发布事实', modelId: 'GPT-6 Luna', reasoning: { kind: 'effort', value: 'high' } });
    const loadTask = vi.fn().mockRejectedValueOnce(new Error('读取暂时失败')).mockResolvedValue(source);
    const loadActivities = vi.fn(async () => ({ activities: [] }));
    render(<TaskWorkspace tasks={[task({ dependencyIds: ['prior'], dependencies: [{ id: 'prior', label: '先前审阅', status: 'completed' }] })]}
      loadTask={loadTask} loading={false} canControl={false} onRetry={vi.fn()} onCancel={vi.fn()} onNotify={vi.fn()}
      activities={new Map()} loadActivities={loadActivities} loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))} />);
    fireEvent.click(screen.getByRole('button', { name: /精确任务/ }));
    expect(loadTask).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '其他任务组依赖来源：先前审阅' }));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '读取暂时失败');
    expect(screen.queryByText('核对发布事实')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }));
    const detail = await screen.findByRole('complementary', { name: '先前审阅 详情' });
    expect(within(detail).getByText('核对发布事实')).toBeTruthy();
    expect(within(detail).getByText('GPT-6 Luna')).toBeTruthy();
    expect(within(detail).getByText('high')).toBeTruthy();
    expect(loadTask).toHaveBeenLastCalledWith('prior');
    expect(loadActivities).toHaveBeenCalledWith('prior');
    expect(screen.getByRole('dialog').querySelectorAll('.task-node')).toHaveLength(2);
  });

  it('ignores a late cross-group read after another node is selected', async () => {
    let resolve!: (task: AgentTask) => void;
    const loadTask = vi.fn(() => new Promise<AgentTask>(accept => { resolve = accept; }));
    const loadActivities = vi.fn(async () => ({ activities: [] }));
    render(<TaskWorkspace tasks={[task({ dependencyIds: ['prior'], dependencies: [{ id: 'prior', label: '先前审阅', status: 'completed' }] })]}
      loadTask={loadTask} loading={false} canControl={false} onRetry={vi.fn()} onCancel={vi.fn()} onNotify={vi.fn()}
      activities={new Map()} loadActivities={loadActivities} loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))} />);
    fireEvent.click(screen.getByRole('button', { name: /精确任务/ }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(screen.getByRole('button', { name: '其他任务组依赖来源：先前审阅' }));
    expect(screen.getByRole('status').textContent).toContain('正在读取任务');
    fireEvent.click(within(dialog).getByRole('button', { name: /精确任务/ }));
    await act(async () => { resolve(task({ id: 'prior', label: '先前审阅' })); });
    expect(screen.queryByRole('complementary', { name: '先前审阅 详情' })).toBeNull();
    expect(screen.getByRole('complementary', { name: '精确任务 详情' })).toBeTruthy();
    expect(loadActivities).not.toHaveBeenCalledWith('prior');
  });

  it('keeps an in-flight cross-group read across equivalent task projections', async () => {
    let resolve!: (task: AgentTask) => void;
    const source = task({ id: 'prior', label: '先前审阅', status: 'running' });
    const loadTask = vi.fn(() => new Promise<AgentTask>(accept => { resolve = accept; }));
    const local = task({ dependencyIds: ['prior'], dependencies: [{ id: 'prior', label: '先前审阅', status: 'running' }] });
    const props = { tasks: [local], loadTask, loading: false, canControl: false, onRetry: vi.fn(), onCancel: vi.fn(), onNotify: vi.fn(),
      activities: new Map(), loadActivities: vi.fn(async () => ({ activities: [] })), loadBackgroundProcesses: vi.fn(async () => ({ processes: [] })) };
    const view = render(<TaskWorkspace {...props} />);
    fireEvent.click(screen.getByRole('button', { name: /精确任务/ }));
    fireEvent.click(screen.getByRole('button', { name: '其他任务组依赖来源：先前审阅' }));
    view.rerender(<TaskWorkspace {...props} tasks={[{ ...local }]} />);
    expect(loadTask).toHaveBeenCalledTimes(1);
    await act(async () => { resolve(source); });
    expect(screen.getByRole('complementary', { name: '先前审阅 详情' })).toBeTruthy();
    view.rerender(<TaskWorkspace {...props} tasks={[{ ...local, dependencies: [{ id: 'prior', label: '先前审阅', status: 'completed' }] }]} />);
    expect(loadTask).toHaveBeenCalledTimes(2);
    await act(async () => { resolve({ ...source, status: 'completed' }); });
    expect(within(screen.getByRole('complementary', { name: '先前审阅 详情' })).getByText('已完成')).toBeTruthy();
  });

  it('renders a completed task result as the final root-style assistant message', async () => {
    render(<TaskWorkspace
      tasks={[task({
        status: 'completed',
        terminalAt: '2026-09-11T10:02:00Z',
        result: {
          id: 'result-a', entryId: 'entry-result-a', summary: 'UI_SLOW_DONE', diagnostics: [],
        },
      })]}
      loading={false}
      canControl
      onRetry={vi.fn()}
      onCancel={vi.fn()}
      onNotify={vi.fn()}
      activities={new Map()}
      loadActivities={vi.fn(async () => ({ activities: [] }))}
      loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))}
    />);

    fireEvent.click(screen.getByRole('button', { name: /精确任务/ }));
    const detail = screen.getByLabelText('精确任务 详情');
    expect(within(detail).queryByText('结果尚未加入主对话。')).toBeNull();
    expect(within(detail).queryByRole('button', { name: '用这份结果继续' })).toBeNull();
    expect(within(detail).queryByText(/启动主助手继续处理/)).toBeNull();
    await waitFor(() => expect(within(detail).getByText('UI_SLOW_DONE')).toBeTruthy());
    expect(within(detail).queryByRole('heading', { name: '任务结果' })).toBeNull();
    expect(within(detail).getByRole('heading', { name: '任务对话' })).toBeTruthy();
    expect(detail.querySelectorAll('.task-conversation .assistant-turn--response')).toHaveLength(1);
    expect(within(detail).getByRole('button', { name: '复制回复' })).toBeTruthy();
  });

  it('projects canonical task rows into the same conversational bubbles and tool cards as the root transcript', async () => {
    const loadActivities = vi.fn(async () => ({
      activities: [
        {
          entryId: 'entry-objective', turnId: 'turn-a', entrySequence: 1,
          entryKind: 'USER_MESSAGE', acceptedAt: '2026-09-11T10:00:00Z',
          objective: '检查 exact owner。', body: '检查 exact owner。', contentKind: 'INLINE' as const,
          contentDigest: 'sha256:objective', contentSize: 19, blocks: [], toolResults: [],
        },
        {
          entryId: 'entry-request', turnId: 'turn-a', entrySequence: 2,
          entryKind: 'ASSISTANT_TOOL_REQUEST', acceptedAt: '2026-09-11T10:00:01Z',
          objective: '检查 exact owner。',
          body: '{"blocks":[{"kind":"TOOL_CALL","tool_call_id":"call-one","tool_name":"terminal"}],"draft_identity":"internal-only"}',
          contentKind: 'INLINE' as const, contentDigest: 'sha256:request', contentSize: 120,
          blocks: [{ blockId: 'block-one', ordinal: 0, kind: 'TOOL_CALL', toolCallId: 'call-one', toolName: 'terminal' }],
          toolResults: [{
            attemptId: 'attempt-one', assistantEntryId: 'entry-request', toolCallId: 'call-one',
            resultEntryId: 'entry-result', resultState: 'SUCCESS',
          }],
        },
        {
          entryId: 'entry-result', turnId: 'turn-a', entrySequence: 3,
          entryKind: 'TOOL_RESULT', acceptedAt: '2026-09-11T10:00:02Z',
          objective: '检查 exact owner。', body: '{"exit_code":0,"output":"done"}',
          contentKind: 'INLINE' as const, contentDigest: 'sha256:result', contentSize: 31,
          blocks: [], toolResults: [{
            attemptId: 'attempt-one', assistantEntryId: 'entry-request', toolCallId: 'call-one',
            resultEntryId: 'entry-result', resultState: 'SUCCESS',
          }],
        },
      ],
    }));
    renderWithRawResults(<TaskWorkspace
      tasks={[task()]}
      loading={false}
      canControl
      onRetry={vi.fn()}
      onCancel={vi.fn()}
      onNotify={vi.fn()}
      activities={new Map()}
      loadActivities={loadActivities}
      loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))}
    />);

    fireEvent.click(screen.getByRole('button', { name: /精确任务/ }));
    const detail = screen.getByLabelText('精确任务 详情');
    await waitFor(() => expect(loadActivities).toHaveBeenCalled());
    expect(detail.querySelectorAll('.task-conversation .user-turn')).toHaveLength(1);
    expect(detail.querySelectorAll('.task-conversation .assistant-turn')).toHaveLength(1);
    expect(within(detail).getByText('主 Agent')).toBeTruthy();
    expect(within(detail).getAllByText('精确任务')).toHaveLength(2);
    expect(within(detail).queryByText(/draft_identity/)).toBeNull();
    expect(within(detail).queryByText(/ASSISTANT_TOOL_REQUEST/)).toBeNull();
    const tool = within(detail).getByRole('button', { name: '展开工具详情：terminal' });
    fireEvent.click(tool);
    expect(within(detail).getByText('{"exit_code":0,"output":"done"}')).toBeTruthy();
  });

  it('opens a single-node group with the node selected and no subagent input', async () => {
    const loadActivities = vi.fn(async () => ({
      activities: [{
        entryId: 'entry-a', turnId: 'turn-a', entrySequence: 3,
        entryKind: 'ASSISTANT_MESSAGE', acceptedAt: '2026-09-11T10:00:00Z',
        objective: '检查 exact owner。', body: '规范活动正文', contentKind: 'INLINE' as const,
        contentDigest: 'sha256:a', contentSize: 18, blocks: [], toolResults: [],
      }],
    }));
    render(<TaskWorkspace
      tasks={[task()]}
      loading={false}
      canControl
      onRetry={vi.fn()}
      onCancel={vi.fn()}
      onNotify={vi.fn()}
      activities={new Map([['task-a', [{ id: 'entry-a', time: '现在', body: '规范活动正文' }]]])}
      loadActivities={loadActivities}
      loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))}
    />);

    const opener = screen.getByRole('button', { name: /精确任务/ });
    fireEvent.click(opener);
    expect(screen.getByRole('dialog', { name: '精确任务' })).toBeTruthy();
    expect(screen.getByLabelText('精确任务 详情')).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
    await waitFor(() => expect(loadActivities).toHaveBeenCalledWith('task-a'));
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  it('does not invent a group when batch identity is absent', () => {
    render(<TaskWorkspace
      tasks={[task({ batchId: undefined })]}
      loading={false}
      canControl
      onRetry={vi.fn()}
      onCancel={vi.fn()}
      onNotify={vi.fn()}
      activities={new Map()}
      loadActivities={vi.fn(async () => ({ activities: [] }))}
      loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))}
    />);
    expect(screen.getByText(/TASK_BATCH_DATA_INCOMPLETE：task-a/)).toBeTruthy();
    expect(screen.queryByText('任务组')).toBeNull();
  });

  it('lays out a dependency DAG in distinct horizontal levels', () => {
    const one = task({ id: 'task-1', label: 'subagent-1', taskKey: 'one', status: 'completed' });
    const three = task({ id: 'task-3', label: 'subagent-3', taskKey: 'three', status: 'completed' });
    const two = task({
      id: 'task-2', label: 'subagent-2', taskKey: 'two', status: 'completed',
      dependencyIds: ['task-1'],
      dependencies: [{ id: 'task-1', label: 'subagent-1', status: 'completed' }],
    });
    const four = task({
      id: 'task-4', label: 'subagent-4', taskKey: 'four', status: 'completed',
      dependencyIds: ['task-1', 'task-3'],
      dependencies: [
        { id: 'task-1', label: 'subagent-1', status: 'completed' },
        { id: 'task-3', label: 'subagent-3', status: 'completed' },
      ],
    });
    const five = task({
      id: 'task-5', label: 'subagent-5', taskKey: 'five', status: 'completed',
      dependencyIds: ['task-2', 'task-4'],
      dependencies: [
        { id: 'task-2', label: 'subagent-2', status: 'completed' },
        { id: 'task-4', label: 'subagent-4', status: 'completed' },
      ],
    });
    render(<TaskWorkspace
      tasks={[one, two, three, four, five]}
      loading={false}
      canControl
      onRetry={vi.fn()}
      onCancel={vi.fn()}
      onNotify={vi.fn()}
      activities={new Map()}
      loadActivities={vi.fn(async () => ({ activities: [] }))}
      loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))}
    />);

    fireEvent.click(screen.getByRole('button', { name: /任务组/ }));
    const dialog = screen.getByRole('dialog', { name: /任务组/ });
    const left = (label: string) => Number.parseFloat(
      (within(dialog).getByRole('button', { name: new RegExp(label) }) as HTMLElement).style.left,
    );

    expect(dialog.querySelectorAll('.task-graph-edges > path')).toHaveLength(5);
    expect(left('subagent-1')).toBe(left('subagent-3'));
    expect(left('subagent-2')).toBe(left('subagent-4'));
    expect(left('subagent-2')).toBeGreaterThan(left('subagent-1'));
    expect(left('subagent-5')).toBeGreaterThan(left('subagent-2'));
  });

  it('draws only exact dependencies and reads canonical activity pages on demand', async () => {
    const cancel = vi.fn(async () => undefined);
    const loadActivities = vi.fn(async (_taskId: string, cursor?: string) => cursor
      ? {
        activities: [{
          entryId: 'entry-two', turnId: 'turn-b', entrySequence: 5,
          entryKind: 'TOOL_RESULT', acceptedAt: '2026-09-11T10:01:00Z',
          objective: '执行后续节点。', body: '第二页活动', contentKind: 'INLINE' as const,
          contentDigest: 'sha256:b', contentSize: 18, blocks: [], toolResults: [{
            attemptId: 'attempt-one', assistantEntryId: 'assistant-one',
            toolCallId: 'call-one', resultEntryId: 'entry-result-one', resultState: 'SUCCESS',
          }],
        }],
      }
      : {
        activities: [{
          entryId: 'entry-one', turnId: 'turn-b', entrySequence: 4,
          entryKind: 'ASSISTANT_MESSAGE', acceptedAt: '2026-09-11T10:00:00Z',
          objective: '执行后续节点。', body: '第一页活动', contentKind: 'INLINE' as const,
          contentDigest: 'sha256:a', contentSize: 18, blocks: [], toolResults: [],
        }],
        nextCursor: 'activity:next',
      });
    renderWithRawResults(<TaskWorkspace
      tasks={[
        task({ id: 'task-a', label: '前置节点', status: 'completed' }),
        task({
          id: 'task-b', label: '后续节点', objective: '执行后续节点。',
          dependencies: [{ id: 'task-a', label: '前置节点', status: 'completed' }],
          dependencyIds: ['task-a'],
        }),
      ]}
      loading={false}
      canControl
      onRetry={vi.fn()}
      onCancel={cancel}
      onNotify={vi.fn()}
      activities={new Map()}
      loadActivities={loadActivities}
      loadBackgroundProcesses={vi.fn(async () => ({ processes: [] }))}
    />);

    fireEvent.click(screen.getByRole('button', { name: /任务组/ }));
    const dialog = screen.getByRole('dialog', { name: /任务组/ });
    expect(dialog.querySelectorAll('.task-graph-edges > path')).toHaveLength(1);
    fireEvent.click(within(dialog).getByRole('button', { name: /后续节点/ }));
    await waitFor(() => expect(loadActivities).toHaveBeenCalledWith('task-b'));
    expect(within(dialog).getByText('第一页活动')).toBeTruthy();
    expect(loadActivities).not.toHaveBeenCalledWith('task-b', 'activity:next');
    fireEvent.click(within(dialog).getByRole('button', { name: '加载更多任务活动' }));
    await waitFor(() => expect(loadActivities).toHaveBeenCalledWith('task-b', 'activity:next'));
    const orphanResult = within(dialog).getByRole('button', { name: '展开工具详情：操作结果' });
    fireEvent.click(orphanResult);
    expect(within(dialog).getByText('第二页活动')).toBeTruthy();
    expect(within(dialog).queryByText('工具结果 · SUCCESS')).toBeNull();
    expect(dialog.querySelector('.task-graph-edges > path')?.classList.contains('is-selected')).toBe(true);
    fireEvent.click(within(dialog).getByRole('button', { name: '取消任务' }));
    expect(cancel).toHaveBeenCalledWith(expect.objectContaining({ id: 'task-b' }));
  });

  it('shows real downstream and background-command impact before exact cancellation', async () => {
    const confirm = vi.fn(() => true);
    vi.stubGlobal('confirm', confirm);
    const cancel = vi.fn(async () => undefined);
    render(<TaskWorkspace
      tasks={[
        task({ id: 'task-a', label: '被取消节点', status: 'running' }),
        task({
          id: 'task-b', label: '真实下游', status: 'waiting',
          dependencies: [{ id: 'task-a', label: '被取消节点', status: 'running' }],
          dependencyIds: ['task-a'],
        }),
      ]}
      loading={false}
      canControl
      onRetry={vi.fn()}
      onCancel={cancel}
      onNotify={vi.fn()}
      activities={new Map()}
      loadActivities={vi.fn(async () => ({ activities: [] }))}
      loadBackgroundProcesses={vi.fn(async () => ({ processes: [{
        processId: 'process-a', command: 'python retained.py', cwd: '/tmp',
        status: 'running', physicalState: 'RUNNING', ioMode: 'pipe',
        streamId: 'stream-a', outputCursor: '0', retainedFromCursor: '0',
        durationSeconds: 2, timedOut: false, originTurnId: 'turn-a',
        originSubagentTaskId: 'task-a',
      }] }))}
    />);

    fireEvent.click(screen.getByRole('button', { name: /任务组/ }));
    const dialog = screen.getByRole('dialog', { name: /任务组/ });
    fireEvent.click(within(dialog).getByRole('button', { name: /被取消节点/ }));
    await waitFor(() => expect(within(dialog).getByRole('button', { name: '取消任务' })).toBeTruthy());
    expect(within(dialog).queryByText(/下游任务：真实下游/)).toBeNull();
    expect(within(dialog).queryByText(/关联后台命令：python retained.py/)).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: '取消任务' }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('下游任务：真实下游'));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('关联后台命令：python retained.py'));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('取消任务不会自动终止'));
    expect(cancel).toHaveBeenCalledWith(expect.objectContaining({ id: 'task-a' }));
  });
});
