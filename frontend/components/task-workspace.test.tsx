import { ToolResultDisplayContext } from '../lib/tool-result-display';
import type { ReactElement, PropsWithChildren } from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AgentTask } from '../lib/pulsara-types';
import { TaskWorkspace } from './task-workspace';

function renderWithRawResults(ui: ReactElement) {
  return render(ui, { wrapper: ({ children }: PropsWithChildren) => (
    <ToolResultDisplayContext.Provider value={{ showBuiltinToolResults: true, onChange: () => {} }}>
      {children}
    </ToolResultDisplayContext.Provider>
  ) });
}

afterEach(() => { vi.unstubAllGlobals(); cleanup(); });

const task = (overrides: Partial<AgentTask> = {}): AgentTask => ({
  id: 'task-a',
  label: '精确子任务',
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

    fireEvent.click(screen.getByRole('button', { name: /精确子任务/ }));
    const detail = screen.getByLabelText('精确子任务 详情');
    expect(within(detail).queryByText('结果尚未加入主对话。')).toBeNull();
    expect(within(detail).queryByRole('button', { name: '用这份结果继续' })).toBeNull();
    expect(within(detail).queryByText(/启动主助手继续处理/)).toBeNull();
    await waitFor(() => expect(within(detail).getByText('UI_SLOW_DONE')).toBeTruthy());
    expect(within(detail).queryByRole('heading', { name: '任务结果' })).toBeNull();
    expect(within(detail).getByRole('heading', { name: '任务对话 · 2 条消息' })).toBeTruthy();
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

    fireEvent.click(screen.getByRole('button', { name: /精确子任务/ }));
    const detail = screen.getByLabelText('精确子任务 详情');
    await waitFor(() => expect(loadActivities).toHaveBeenCalled());
    expect(detail.querySelectorAll('.task-conversation .user-turn')).toHaveLength(1);
    expect(detail.querySelectorAll('.task-conversation .assistant-turn')).toHaveLength(1);
    expect(within(detail).getByText('主任务')).toBeTruthy();
    expect(within(detail).getAllByText('精确子任务')).toHaveLength(2);
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

    const opener = screen.getByRole('button', { name: /精确子任务/ });
    fireEvent.click(opener);
    expect(screen.getByRole('dialog', { name: '精确子任务' })).toBeTruthy();
    expect(screen.getByLabelText('精确子任务 详情')).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
    await waitFor(() => expect(loadActivities).toHaveBeenCalledWith('task-a', undefined));
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
    expect(screen.queryByText('子任务组')).toBeNull();
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

    fireEvent.click(screen.getByRole('button', { name: /子任务组/ }));
    const dialog = screen.getByRole('dialog', { name: /子任务组/ });
    const left = (label: string) => Number.parseFloat(
      (within(dialog).getByRole('button', { name: new RegExp(label) }) as HTMLElement).style.left,
    );

    expect(dialog.querySelectorAll('.task-graph-edges > path')).toHaveLength(5);
    expect(left('subagent-1')).toBe(left('subagent-3'));
    expect(left('subagent-2')).toBe(left('subagent-4'));
    expect(left('subagent-2')).toBeGreaterThan(left('subagent-1'));
    expect(left('subagent-5')).toBeGreaterThan(left('subagent-2'));
  });

  it('draws only exact dependencies and reads every canonical activity page for the selected node', async () => {
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

    fireEvent.click(screen.getByRole('button', { name: /子任务组/ }));
    const dialog = screen.getByRole('dialog', { name: /子任务组/ });
    expect(dialog.querySelectorAll('.task-graph-edges > path')).toHaveLength(1);
    fireEvent.click(within(dialog).getByRole('button', { name: /后续节点/ }));
    await waitFor(() => expect(loadActivities).toHaveBeenNthCalledWith(2, 'task-b', 'activity:next'));
    expect(within(dialog).getByText('第一页活动')).toBeTruthy();
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

    fireEvent.click(screen.getByRole('button', { name: /子任务组/ }));
    const dialog = screen.getByRole('dialog', { name: /子任务组/ });
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
