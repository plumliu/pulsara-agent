import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  CommandReceipt,
  RuntimeAdapter,
  RuntimeBootstrap,
  RuntimeConnection,
  RuntimeInteractionContent,
  RuntimeInteractionSummary,
  RuntimeProjection,
} from '../lib/runtime-adapter';
import type { AgentTask, SessionSummary, SessionWorkspaceSelection } from '../lib/pulsara-types';
import PulsaraApp from './pulsara-app';

afterEach(cleanup);

const bootstrap: RuntimeBootstrap = {
  application: { name: 'Pulsara', version: '0.1.0', transport: 'local' },
  workspace: {
    id: 'workspace',
    name: 'pulsara_agent',
    path: '/tmp/pulsara_agent',
    kind: 'project',
  },
  provider: {
    provider: 'local-test',
    endpoint_origin: 'http://localhost',
    pro_model: 'test-model',
    flash_model: 'test-mini',
    api_key_set: true,
  },
  protocol: { major: 3, minor: 0 },
  runtime: { status: 'ready', origin: 'http://localhost' },
};

const initialSession: SessionSummary = {
  id: 'session-1',
  title: '准备发布',
  subtitle: '1 条记录',
  status: 'running',
  updatedAt: '刚刚',
  live: false,
};

function projection(body = '我已经开始检查。'): RuntimeProjection {
  return {
    messages: [{
      id: 'assistant-1',
      role: 'assistant',
      time: '现在',
      body,
      status: 'running',
    }],
    isRunning: true,
    queuedCount: 0,
    planMode: false,
    activeTurnId: 'turn-1',
    control: {},
    liveControl: {},
    agentTasks: [],
    todo: {
      id: 'todo-root',
      items: [
        { id: 'todo-root:0', label: '检查实现', status: 'completed' },
        { id: 'todo-root:1', label: '运行验证', status: 'in-progress' },
      ],
    },
    eventSequence: 1,
    liveOwnerEpoch: 1,
    liveRevision: 1,
    liveControlOwnerEpoch: 1,
    liveControlRevision: 1,
  };
}

class FakeConnection implements RuntimeConnection {
  readonly role: 'controller' | 'observer';
  readonly generation = 1;
  private value: RuntimeProjection;

  constructor(
    readonly sessionId: string,
    value = projection(),
    role: 'controller' | 'observer' = 'controller',
  ) {
    this.value = value;
    this.role = role;
  }

  current() {
    return this.value;
  }

  async snapshot() {
    return this.value;
  }

  observe(signal?: AbortSignal): Promise<RuntimeProjection> {
    return new Promise((_, reject) => {
      signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    });
  }

  submitPrompt = vi.fn(async (): Promise<CommandReceipt> => {
    return { commandId: 'command-1', status: 'succeeded', publicMessage: '任务已经开始。' };
  });

  async steerActiveTurn(): Promise<CommandReceipt> {
    return { commandId: 'command-2', status: 'succeeded' };
  }

  async stopActiveTurn(): Promise<CommandReceipt> {
    return { commandId: 'command-3', status: 'succeeded' };
  }

  async compactContext(): Promise<CommandReceipt> {
    return { commandId: 'command-4', status: 'succeeded' };
  }

  acceptSubagentResult = vi.fn(async (): Promise<CommandReceipt> => {
    return { commandId: 'command-result', status: 'succeeded' };
  });

  enterPlan = vi.fn(async (): Promise<CommandReceipt> => {
    return { commandId: 'command-5', status: 'succeeded' };
  });

  async readInteraction(interaction: RuntimeInteractionSummary): Promise<RuntimeInteractionContent> {
    switch (interaction.kind) {
      case 'plan-question':
        return { kind: 'plan-question', question: '选择发布方式', options: [], allowFreeText: true };
      case 'plan-draft':
        return { kind: 'plan-draft', body: '# 实施方案\n\n- 检查契约\n- 验证结果' };
      case 'tool-confirmation':
        return { kind: 'tool-confirmation', prompt: interaction.prompt, options: interaction.options };
    }
  }

  resolveInteraction = vi.fn(async (): Promise<CommandReceipt> => {
    return { commandId: 'command-6', status: 'succeeded' };
  });

  async queryCommand(): Promise<CommandReceipt | undefined> {
    return undefined;
  }

  async close() {}
}

class FakeAdapter implements RuntimeAdapter {
  sessions = [initialSession];
  taskInventory: AgentTask[] = [];
  lastConnection?: FakeConnection;
  connectionValue?: RuntimeProjection;
  connectionRole: 'controller' | 'observer' = 'controller';
  connectCalls: Array<{ sessionId: string; takeover: boolean }> = [];
  createSession = vi.fn(async (selection: SessionWorkspaceSelection) => {
    const created: SessionSummary = {
      id: 'session-2',
      title: '新会话',
      subtitle: '刚刚创建',
      status: 'waiting',
      updatedAt: '刚刚',
      live: true,
      workspace: selection.kind === 'quick'
        ? { id: 'quick-workspace', name: '快速开始', path: '/tmp/pulsara-quick', kind: 'quick' }
        : { id: 'project-workspace', name: 'project', path: selection.path, kind: 'project' },
    };
    this.sessions = [created, ...this.sessions];
    return created;
  });

  async bootstrap() {
    return bootstrap;
  }

  listSessions = vi.fn(async () => this.sessions.map((session) => ({ ...session })));

  listSessionTasks = vi.fn(async () => ({
    tasks: this.taskInventory.map((task) => ({ ...task })),
    totalCount: this.taskInventory.length,
    remainingCount: 0,
  }));

  async connect(sessionId: string, takeover = false) {
    this.connectCalls.push({ sessionId, takeover });
    if (takeover) this.connectionRole = 'controller';
    this.sessions = this.sessions.map((session) => (
      session.id === sessionId ? { ...session, live: true } : session
    ));
    const connection = new FakeConnection(
      sessionId,
      sessionId === 'session-2'
        ? { ...projection(''), messages: [], isRunning: false, activeTurnId: undefined }
        : this.connectionValue ?? projection(),
      this.connectionRole,
    );
    this.lastConnection = connection;
    return connection;
  }
}

describe('PulsaraApp', () => {
  it('opens the real workbench state supplied by its adapter', async () => {
    render(<PulsaraApp adapter={new FakeAdapter()} />);
    expect(await screen.findByRole('heading', { name: '准备发布' })).toBeTruthy();
    expect(screen.getByLabelText('发送给 Pulsara')).toBeTruthy();
    expect(await screen.findByLabelText('本轮清单')).toBeTruthy();
    expect(screen.getByRole('button', { name: '收起本轮清单' }).textContent).toContain('第 2 / 2 步');
    expect(screen.getByText('检查实现')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '收起本轮清单' }));
    expect(screen.queryByLabelText('本轮清单')).toBeNull();
    expect(screen.getByRole('button', { name: '展开本轮清单' })).toBeTruthy();
    expect(screen.queryByText(/文件已更改|项变更/)).toBeNull();
    expect(screen.queryByRole('button', { name: '添加附件' })).toBeNull();
    expect(screen.queryByRole('button', { name: '能力' })).toBeNull();
    expect(screen.queryByRole('button', { name: '记忆' })).toBeNull();
  });

  it('keeps a second page stable as an observer until the user explicitly takes control', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionRole = 'observer';
    render(<PulsaraApp adapter={adapter} />);

    expect((await screen.findAllByText('这个会话正在另一个窗口中操作')).length).toBeGreaterThan(0);
    expect(screen.getByText('这里仍会实时显示对话、思考和任务进展。')).toBeTruthy();
    expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
    expect(screen.queryByRole('button', { name: '压缩上下文' })).toBeNull();
    expect(screen.getAllByText('旁观中').length).toBeGreaterThan(0);

    fireEvent.click(screen.getAllByRole('button', { name: '在此窗口继续' })[0]!);

    expect(await screen.findByLabelText('发送给 Pulsara')).toBeTruthy();
    expect(screen.getByRole('button', { name: '压缩上下文' })).toBeTruthy();
    expect(adapter.connectCalls).toContainEqual({ sessionId: 'session-1', takeover: true });
  });

  it('explains when manual context compaction cannot shrink the current context', async () => {
    const adapter = new FakeAdapter();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    adapter.lastConnection!.compactContext = vi.fn(async (): Promise<CommandReceipt> => ({
      commandId: 'command-compact-not-needed',
      status: 'succeeded',
      publicCode: 'NOT_NEEDED',
      publicMessage: 'CONTEXT_ALREADY_COMPACT',
    }));

    fireEvent.click(screen.getByRole('button', { name: '压缩上下文' }));

    expect(await screen.findByText('无需整理上下文')).toBeTruthy();
    expect(screen.getByText('当前上下文已经较紧凑，本次整理无法进一步缩小。')).toBeTruthy();
  });

  it('shows provider reasoning as an expandable full or summary disclosure', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection('最终回答'),
      messages: [{
        id: 'assistant-reasoning',
        role: 'assistant',
        time: '现在',
        body: '最终回答',
        status: 'completed',
        reasoning: [{
          id: 'reasoning-summary',
          kind: 'summary',
          body: '先检查输入。\n再生成最终回答。',
        }],
      }],
      isRunning: false,
    };
    render(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByText('思考摘要')).toBeTruthy();
    expect(screen.getByText('先检查输入。')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '展开思考摘要' }));
    expect(screen.getByText(/再生成最终回答。/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '收起思考摘要' })).toBeTruthy();
  });

  it('marks real speaker turns without breaking a continuous reasoning and tool flow', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'user-prompt', role: 'user', userKind: 'prompt', time: '18:10',
        body: '检查完整流程。', status: 'completed',
      }, {
        id: 'assistant-tool', role: 'assistant', time: '18:11', body: '', status: 'completed',
        reasoning: [{ id: 'reasoning-before-tool', kind: 'full', body: '先读取文件。' }],
        traces: [{
          id: 'trace-read', kind: 'read', title: '读取文件', subtitle: '已完成',
          status: 'completed', meta: '操作完成',
        }],
      }, {
        id: 'user-steer', role: 'user', userKind: 'steer', time: '18:12',
        body: '也检查真实页面。', status: 'waiting',
      }, {
        id: 'assistant-tool-after-steer', role: 'assistant', time: '18:12', body: '', status: 'completed',
        reasoning: [{ id: 'reasoning-after-steer', kind: 'full', body: '继续检查真实页面。' }],
      }, {
        id: 'assistant-final', role: 'assistant', time: '18:13', body: '检查已经完成。', status: 'completed',
        reasoning: [{ id: 'reasoning-after-tool', kind: 'summary', body: '整理检查结果。' }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByText('检查已经完成。')).toBeTruthy();

    expect(container.querySelectorAll('.user-heading')).toHaveLength(1);
    const userHeading = container.querySelector('.user-heading');
    expect(userHeading?.textContent).toContain('你');
    expect(userHeading?.firstElementChild?.tagName).toBe('STRONG');
    expect(userHeading?.lastElementChild?.classList.contains('user-avatar')).toBe(true);
    expect(container.querySelectorAll('.user-steer')).toHaveLength(1);
    expect(screen.getByText('你 · 引导')).toBeTruthy();
    expect(container.querySelectorAll('.assistant-heading')).toHaveLength(2);
    expect(container.querySelectorAll('.assistant-heading--run-start')).toHaveLength(1);
    expect(container.querySelectorAll('.assistant-heading--response')).toHaveLength(1);
    expect(container.querySelectorAll('.assistant-turn--operational .assistant-heading')).toHaveLength(1);

    const finalTurn = container.querySelector('.assistant-turn--response-with-reasoning');
    expect(finalTurn?.firstElementChild?.classList.contains('reasoning-disclosure')).toBe(true);
    expect(finalTurn?.children[1]?.classList.contains('assistant-heading--response')).toBe(true);
  });

  it('renders an accepted subtask result as a neutral continuation event', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'accepted-result', role: 'user', userKind: 'subagent-result', time: '18:14',
        body: '{"status":"accepted","task_id":"internal"}',
        sourceSubagentResultId: 'subagent-result:internal',
        status: 'completed',
      }, {
        id: 'assistant-after-result', role: 'assistant', time: '18:15',
        body: '我会基于子任务结果继续。', status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByLabelText('已带入子任务结果')).toBeTruthy();
    expect(screen.getByText('Pulsara 正在基于这份结果继续处理')).toBeTruthy();
    expect(screen.getByRole('tooltip').textContent).toContain('作为新的上下文交给 Pulsara');
    expect(screen.queryByText(/"status":"accepted"/)).toBeNull();
    expect(container.querySelectorAll('.user-heading')).toHaveLength(0);
    expect(container.querySelectorAll('.assistant-turn--run-start')).toHaveLength(1);
  });

  it('navigates between the major product surfaces', async () => {
    render(<PulsaraApp adapter={new FakeAdapter()} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '总览' }));
    expect(screen.getByText(/准备好继续/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    expect(screen.getByRole('heading', { name: '设置' })).toBeTruthy();
  });

  it('refreshes loaded session facts and distinguishes the current page connection', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [
      initialSession,
      {
        id: 'session-resumable',
        title: '继续检查',
        subtitle: '13 条记录',
        status: 'completed',
        updatedAt: '5 分钟前',
        live: false,
      },
    ];
    render(<PulsaraApp adapter={adapter} />);

    await screen.findByRole('heading', { name: '准备发布' });
    expect(await screen.findByText('1 条记录 · 当前会话')).toBeTruthy();
    expect(screen.getByText('13 条记录 · 可恢复')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /继续检查/ }));
    await screen.findByRole('heading', { name: '继续检查' });
    expect(await screen.findByText('13 条记录 · 当前会话')).toBeTruthy();
    expect(screen.getByText('1 条记录 · 已载入')).toBeTruthy();
    await waitFor(() => expect(adapter.listSessions).toHaveBeenCalledTimes(3));
  });

  it('creates a quick-start session before asking for the first task', async () => {
    const adapter = new FakeAdapter();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    expect(screen.getByRole('heading', { name: '新建会话' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: /快速开始/ })).toBeTruthy();
    expect(screen.getByRole('radio', { name: /指定目录/ })).toBeTruthy();
    expect(screen.queryByText('想完成什么？')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
    await waitFor(() => expect(adapter.createSession).toHaveBeenCalledWith({ kind: 'quick' }));
    expect(await screen.findByText('这个会话还没有消息')).toBeTruthy();
  });

  it('binds plan and permission choices to the next composer submission', async () => {
    const adapter = new FakeAdapter();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
    await screen.findByText('这个会话还没有消息');

    fireEvent.click(screen.getByRole('button', { name: '先规划' }));
    fireEvent.click(screen.getByRole('button', { name: /接受编辑/ }));
    fireEvent.click(screen.getByRole('button', { name: /只读/ }));
    const composer = screen.getByLabelText('发送给 Pulsara');
    fireEvent.change(composer, { target: { value: '验证新的前端任务' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('验证新的前端任务')).toBeTruthy();
    expect(adapter.lastConnection?.enterPlan).toHaveBeenCalledWith('验证新的前端任务', 'read-only');
    expect(adapter.lastConnection?.submitPrompt).toHaveBeenCalledWith('验证新的前端任务', 'read-only');
  });

  it('creates a session for an explicitly selected directory', async () => {
    const adapter = new FakeAdapter();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    fireEvent.click(screen.getByRole('radio', { name: /指定目录/ }));
    fireEvent.change(await screen.findByPlaceholderText('/Users/you/path/to/project'), { target: { value: '/tmp/project' } });
    fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
    await waitFor(() => expect(adapter.createSession).toHaveBeenCalledWith({ kind: 'project', path: '/tmp/project' }));
  });

  it('renders a plan draft as Markdown and resolves it through the real interaction surface', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [],
      isRunning: false,
      activeTurnId: undefined,
      interaction: {
        id: 'plan-review-1',
        kind: 'plan-draft',
        workflowId: 'plan-1',
        workflowRevision: 2,
      },
    };
    render(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByRole('heading', { name: '实施方案' })).toBeTruthy();
    expect(screen.getByText('检查契约')).toBeTruthy();
    expect(screen.queryByLabelText('本轮清单')).toBeNull();
    expect(screen.getByRole('button', { name: '展开本轮清单' }).hasAttribute('disabled')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /批准并继续/ }));

    await waitFor(() => expect(adapter.lastConnection?.resolveInteraction).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'plan-review-1', kind: 'plan-draft' }),
      { kind: 'plan-draft', decision: 'approve' },
    ));
  });

  it('shows real child execution inline and expands its details', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection('正在并行检查。'),
      messages: [{
        id: 'assistant-subagents',
        role: 'assistant',
        time: '现在',
        body: '正在并行检查。',
        status: 'completed',
        subagentRuns: [{
          id: 'task-readme',
          label: '检查 README',
          role: '研究',
          objective: '读取 README.md 并报告一级标题。',
          status: 'completed',
          color: 'blue',
          summary: '# Pulsara',
          activities: [{
            id: 'activity-readme',
            time: '现在',
            body: '# Pulsara',
            status: 'completed',
            traces: [{
              id: 'trace-readme', kind: 'read', title: '读取文件', subtitle: '已完成',
              status: 'completed', meta: '操作完成', output: ['已读取 README.md · 1 行'],
            }],
          }],
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };
    render(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByText('子任务执行')).toBeTruthy();
    const task = screen.getByRole('button', { name: /检查 README/ });
    expect(task).toBeTruthy();
    fireEvent.click(task);
    expect(screen.getByText('读取 README.md 并报告一级标题。')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /读取文件/ }));
    expect(screen.getByText('已读取 README.md · 1 行')).toBeTruthy();
  });

  it('keeps the complete durable child-task history inside its session', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection('主任务继续运行。'),
      messages: [{
        id: 'assistant-root', role: 'assistant', time: '现在', body: '主任务继续运行。',
        status: 'running',
      }],
      agentTasks: [],
    };
    adapter.taskInventory = [{
      id: 'task-complete', label: '汇总研究', role: '研究', profile: 'research_worker',
      objective: '### 汇总目标\n\n整理可见结果。', status: 'completed',
      parentId: 'turn-1', batchId: 'batch-1', taskKey: 'research',
      context: { mode: 'last-n', lastNTurns: 4 }, dependencyIds: [],
      acceptedAt: '2026-08-30T12:00:00Z', terminalAt: '2026-08-30T12:01:00Z',
      summary: '### 研究结论\n\n页面符合契约。', color: 'blue',
      result: {
        id: 'result-complete', entryId: 'entry-result', summary: '### 研究结论\n\n页面符合契约。',
        outputPreview: '- 完整输出', diagnostics: [{ message: '目视检查通过' }], accepted: false,
      },
    }, {
      id: 'task-interrupted', label: '中断检查', role: '验证', objective: '验证重启边界。',
      status: 'interrupted', parentId: 'turn-2', dependencyIds: [], color: 'amber',
    }, {
      id: 'task-blocked', label: '等待产物', role: '整合', objective: '整合前置产物。',
      status: 'blocked', parentId: 'turn-1', batchId: 'batch-1', dependencyIds: ['task-interrupted'],
      dependencies: [{ id: 'task-interrupted', label: '中断检查', status: 'interrupted' }], color: 'violet',
    }];

    const { container } = render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    await waitFor(() => expect(adapter.listSessionTasks).toHaveBeenCalled());

    expect(screen.queryByRole('button', { name: '任务' })).toBeNull();
    expect(await screen.findByText('2 项需留意')).toBeTruthy();
    expect(screen.getAllByText('已中断').length).toBeGreaterThan(0);
    expect(screen.getAllByText('依赖未完成').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/第 \d+ 组/)).toHaveLength(2);

    const taskCard = [...container.querySelectorAll<HTMLElement>('.session-task')]
      .find((element) => element.textContent?.includes('汇总研究'))!;
    fireEvent.click(within(taskCard).getByRole('button', { name: /汇总研究/ }));
    expect(screen.getByRole('heading', { name: '汇总目标' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: '研究结论' })).toBeTruthy();
    expect(within(taskCard).getByText('目视检查通过')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /接受编辑/ }));
    fireEvent.click(screen.getByRole('button', { name: /只读/ }));
    const continueButton = within(taskCard).getByRole('button', { name: '带入会话并继续' });
    expect(within(taskCard).getByRole('tooltip').textContent).toContain('作为新消息交给 Pulsara，并以“只读”权限立即继续处理');
    fireEvent.click(continueButton);
    await waitFor(() => expect(adapter.lastConnection?.acceptSubagentResult).toHaveBeenCalledWith('result-complete', 'read-only'));
    expect(await screen.findByText('已带入会话')).toBeTruthy();

    fireEvent.click(within(taskCard).getByRole('button', { name: '在对话中查看' }));
    await waitFor(() => expect(
      container.querySelector('.subagent-run.is-focused[data-task-id="task-complete"]'),
    ).toBeTruthy());
  });
});
