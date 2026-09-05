import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LocalMemoryApi } from '../lib/memory-api';
import type {
  CommandReceipt,
  RuntimeAdapter,
  RuntimeBootstrap,
  ModelCatalogReadModel,
  RuntimeConnection,
  RuntimeInteractionContent,
  RuntimeInteractionSummary,
  RuntimeProjection,
} from '../lib/runtime-adapter';
import type {
  AgentTask,
  CapabilitySnapshot,
  ProjectCapabilityMutationResult,
  SessionSummary,
  SessionWorkspaceSelection,
  UserCapabilitySnapshot,
} from '../lib/pulsara-types';
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
  protocol: { major: 3, minor: 0 },
  runtime: { status: 'ready', origin: 'http://localhost', database_state: 'ready' },
  database_state: 'ready',
  local_settings: {
    state: 'ready',
    postgres: { runtime_dsn: 'postgresql://pulsara@localhost/pulsara', admin_dsn: null },
    dashscope_credentials: { embedding_configured: false, rerank_configured: false },
  },
  model_configurations: [{
    id: 'model-connection:00000000000000000000000000000000',
    source: 'models_dev',
    route_id: 'test',
    route_name: 'Local Test',
    wire_api: 'openai_responses',
    model_id: 'test-model',
    display_name: 'test-model',
    base_url: 'http://localhost',
    status: 'ready',
    authentication: 'bearer_api_key',
    credential_configured: true,
    context_tokens: 256000,
    max_output_tokens: 8192,
    tool_call: true,
    reasoning: { kind: 'selectable', effort: { values: ['low', 'medium', 'high'] }, toggle: false, budget_tokens: null },
    default_reasoning: { kind: 'effort', value: 'medium' },
  }],
};

const initialSession: SessionSummary = {
  id: 'session-1',
  title: '准备发布',
  subtitle: '1 条记录',
  status: 'running',
  updatedAt: '刚刚',
  live: false,
  modelCallBinding: {
    connection_id: 'model-connection:00000000000000000000000000000000',
    reasoning: { kind: 'effort', value: 'medium' },
  },
};

const capabilitySnapshot: CapabilitySnapshot = {
  sessionId: 'session-1',
  workspacePath: '/tmp/pulsara_agent',
  workspaceKind: 'project',
  adoption: { scope: 'workspace', pending: false, when: 'next-user-turn' },
  skills: {
    status: 'ready',
    configPath: '/tmp/pulsara_agent/.pulsara/skills.yaml',
    items: [{
      id: 'bundled:pdf',
      name: 'pdf',
      description: '读取、创建并检查 PDF 文件。',
      location: 'bundled_skills/pdf',
      path: '/opt/pulsara/bundled_skills/pdf/SKILL.md',
      source: 'bundled',
      editable: false,
      enabled: true,
      effective: true,
      configured: false,
      authoringNotes: [],
    }],
    issues: [],
    details: [],
    roots: [],
  },
  mcp: {
    configPath: '/tmp/pulsara_agent/.pulsara/mcp.yaml',
    servers: [{
      id: 'local-docs',
      name: '本地文档',
      source: 'user',
      editable: false,
      enabled: true,
      configuredEnabled: true,
      needsApproval: false,
      effective: true,
      status: 'ready',
      required: false,
      availableToSubagents: true,
      toolCount: 1,
      discoveredToolCount: 1,
      resourceCount: 2,
      resourceTemplateCount: 0,
      promptCount: 1,
      instructions: '',
      hasFailure: false,
      tools: [{
        name: 'local_docs_search',
        remoteName: 'search',
        description: '搜索本地文档。',
        effect: 'read-only',
        availableToSubagents: true,
        parallelSafe: true,
      }],
    }],
    collisions: [],
  },
};

const userCapabilitySnapshot: UserCapabilitySnapshot = {
  roots: [
    { kind: 'agents', path: '/Users/test/.agents' },
    { kind: 'pulsara', path: '/Users/test/.pulsara' },
  ],
  skills: {
    status: 'ready',
    configPath: '/Users/test/.pulsara/skills.yaml',
    items: [{
      name: 'personal-pdf',
      description: '处理个人 PDF 工作流。',
      location: '~/.agents/skills/personal-pdf/SKILL.md',
      path: '/Users/test/.agents/skills/personal-pdf/SKILL.md',
      enabled: true,
      root: 'agents',
      authoringNotes: [],
    }],
    issues: [],
    details: [],
    roots: [{ kind: 'agents', path: '/Users/test/.agents/skills' }],
  },
  mcp: {
    configPath: '/Users/test/.pulsara/mcp.yaml',
    servers: [{
      id: 'personal-docs',
      name: '个人文档',
      enabled: true,
      status: 'ready',
      required: false,
      availableToSubagents: true,
      toolCount: 1,
      resourceCount: 0,
      resourceTemplateCount: 0,
      promptCount: 0,
      instructions: '',
      hasFailure: false,
      transport: { kind: 'http', summary: 'https://example.com/mcp' },
      tools: [],
    }],
  },
  plugins: {
    status: 'ready',
    items: [{
      id: 'personal-tools',
      name: 'Personal Tools',
      description: '个人工作流插件。',
      version: '1.0.0',
      enabled: true,
      packageInstallId: 'pkg_0123456789abcdef0123456789abcdef',
      packageRoot: '/Users/test/.pulsara/plugins/personal-tools',
      skillCount: 2,
      mcpCount: 1,
      effectiveSkillNames: ['personal-pdf'],
      effectiveMcpServerIds: ['personal-docs'],
      details: [],
    }],
    details: [],
  },
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
  private observer?: (value: RuntimeProjection) => void;

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
    return new Promise((resolve, reject) => {
      this.observer = resolve;
      signal?.addEventListener('abort', () => {
        this.observer = undefined;
        reject(new DOMException('Aborted', 'AbortError'));
      }, { once: true });
    });
  }

  emit(value: RuntimeProjection) {
    this.value = value;
    const observer = this.observer;
    this.observer = undefined;
    observer?.(value);
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

  acceptSubagentCompletion = vi.fn(async (): Promise<CommandReceipt> => {
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
  memory = new LocalMemoryApi();
  sessions = [initialSession];
  modelConfigurations = [...bootstrap.model_configurations];
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
    return { ...bootstrap, model_configurations: this.modelConfigurations };
  }

  async modelCatalog(): Promise<ModelCatalogReadModel> {
    return {
      status: 'ready' as const,
      routes: [{
        route_id: 'test',
        display_name: 'Local Test',
        models: [{
          model_id: 'test-model',
          display_name: 'test-model',
          wire_dialect: 'openai_compatible' as const,
          context_tokens: 256000,
          input_tokens: 256000,
          output_tokens: 8192,
          tool_call: true,
          wire_shape_hint: 'responses' as const,
          wire_apis: [{
            wire_api: 'openai_responses' as const,
            executable: true,
            reason: null,
            endpoint: 'http://localhost',
            recommended: true,
            reasoning: bootstrap.model_configurations[0].reasoning,
          }],
        }],
      }],
    };
  }

  async localSettings() {
    return {
      local_settings: bootstrap.local_settings,
      model_configurations: this.modelConfigurations,
      database_state: bootstrap.database_state,
    };
  }

  async addModelConfiguration() {
    return { model_configuration: bootstrap.model_configurations[0], wire_shape_warning: false };
  }

  async deleteModelConfiguration(connectionId: string) {
    const before = this.modelConfigurations.length;
    this.modelConfigurations = this.modelConfigurations.filter((item) => item.id !== connectionId);
    return {
      model_configuration_id: connectionId,
      deleted: this.modelConfigurations.length !== before,
      model_configurations: this.modelConfigurations,
    };
  }

  async testModelConfiguration() { return { status: 'ready' as const }; }

  async savePostgres() {
    return { ...(await this.localSettings()), restart_required: false };
  }

  async checkPostgres() { return { database_name: 'pulsara' }; }
  async migratePostgres() { return { database_name: 'pulsara' }; }
  async putDashScopeCredential() { return true; }
  async deleteDashScopeCredential() { return false; }

  async updateModelCallBinding(sessionId: string, binding: NonNullable<SessionSummary['modelCallBinding']>) {
    this.sessions = this.sessions.map((session) => session.id === sessionId
      ? { ...session, modelCallBinding: binding }
      : session);
    return { modelCallBinding: binding, reasoningPreferenceReset: false };
  }

  listSessions = vi.fn(async () => this.sessions.map((session) => ({ ...session })));

  listSessionTasks = vi.fn(async () => ({
    tasks: this.taskInventory.map((task) => ({ ...task })),
    totalCount: this.taskInventory.length,
    remainingCount: 0,
  }));

  inspectCapabilities = vi.fn(async (sessionId: string) => ({
    ...capabilitySnapshot,
    sessionId,
  }));

  reconnectMcpServer = vi.fn(async (sessionId: string) => ({
    ...capabilitySnapshot,
    sessionId,
  }));

  installSkill = vi.fn(async (sessionId: string, sourcePath: string) => ({
    installation: {
      status: 'INSTALLED',
      installed: true,
      message: '技能已经安装。',
      sourcePath,
      destinationPath: '/tmp/pulsara_agent/.pulsara/skills/pdf',
      details: [],
    },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-user-turn' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  setProjectSkillEnabled = vi.fn<RuntimeAdapter['setProjectSkillEnabled']>(async (sessionId: string) => ({
    operation: { status: 'DISABLED', success: true, message: '项目技能已关闭。', details: [] },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-user-turn' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  createProjectMcp = vi.fn(async (sessionId: string) => ({
    operation: { status: 'ADDED', success: true, message: '项目 MCP 已添加。', details: [] },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-user-turn' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  setProjectMcpEnabled = vi.fn(async (sessionId: string) => ({
    operation: { status: 'DISABLED', success: true, message: '项目 MCP 已关闭。', details: [] },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-user-turn' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  removeProjectMcp = vi.fn(async (sessionId: string) => ({
    operation: { status: 'REMOVED', success: true, message: '项目 MCP 已移除。', details: [] },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-user-turn' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  inspectUserCapabilities = vi.fn(async () => userCapabilitySnapshot);

  refreshUserCapabilities = vi.fn(async () => userCapabilitySnapshot);

  installUserSkill = vi.fn(async () => ({
    operation: { status: 'INSTALLED', success: true, message: '技能已安装。', details: [] },
    capabilities: userCapabilitySnapshot,
  }));

  setUserSkillEnabled = vi.fn(async () => ({
    operation: { status: 'DISABLED', success: true, message: '技能已关闭。', details: [] },
    capabilities: {
      ...userCapabilitySnapshot,
      skills: {
        ...userCapabilitySnapshot.skills,
        items: userCapabilitySnapshot.skills.items.map((item) => ({ ...item, enabled: false })),
      },
    },
  }));

  createUserMcp = vi.fn(async () => ({
    operation: { status: 'ADDED', success: true, message: 'MCP 服务已添加。', details: [] },
    capabilities: userCapabilitySnapshot,
  }));

  setUserMcpEnabled = vi.fn(async () => ({
    operation: { status: 'ENABLED', success: true, message: 'MCP 服务已开启。', details: [] },
    capabilities: userCapabilitySnapshot,
  }));

  installUserPlugin = vi.fn(async () => ({
    operation: { status: 'INSTALLED', success: true, message: '插件已安装。', details: [] },
    capabilities: userCapabilitySnapshot,
  }));

  setUserPluginEnabled = vi.fn(async () => ({
    operation: { status: 'ENABLED', success: true, message: '插件已开启。', details: [] },
    capabilities: userCapabilitySnapshot,
  }));

  removeUserPlugin = vi.fn(async () => ({
    operation: { status: 'REMOVED', success: true, message: '插件已移除。', details: [] },
    capabilities: userCapabilitySnapshot,
  }));

  openCapabilityRoot = vi.fn(async () => {});

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
  it('shows canonical ROOT interruption instead of treating idle as completed', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(), isRunning: false, activeTurnId: undefined,
      control: { latest_root_turn: {
        turn_id: 'turn-interrupted', status: 'INTERRUPTED',
        terminal_reason: 'FOREGROUND_EXECUTION_INTERRUPTED',
      } },
    };
    render(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByText('本轮执行已中断，未正常完成。你可以发送新消息继续。')).toBeTruthy();
    const workbench = screen.getByRole('region', { name: '会话工作台' });
    expect(within(workbench).getByText('已中断')).toBeTruthy();
    expect(within(workbench).queryByText('FOREGROUND_EXECUTION_INTERRUPTED')).toBeNull();
  });

  it('opens the real workbench state supplied by its adapter', async () => {
    render(<PulsaraApp adapter={new FakeAdapter()} />);
    expect(await screen.findByRole('heading', { name: '准备发布' })).toBeTruthy();
    expect(screen.getByLabelText('发送给 Pulsara')).toBeTruthy();
    expect(await screen.findByLabelText('TODO清单')).toBeTruthy();
    expect(screen.getAllByText('TODO清单')).toHaveLength(2);
    expect(screen.getByRole('button', { name: '收起TODO清单' }).textContent).toContain('第 2 / 2 步');
    expect(screen.getByText('检查实现')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '收起TODO清单' }));
    expect(screen.queryByLabelText('TODO清单')).toBeNull();
    expect(screen.getByRole('button', { name: '展开TODO清单' })).toBeTruthy();
    expect(screen.queryByText(/文件已更改|项变更/)).toBeNull();
    expect(screen.queryByRole('button', { name: '添加附件' })).toBeNull();
    expect(screen.getByRole('button', { name: '能力' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '记忆' })).toBeTruthy();
  });

  it('keeps the full session catalog in the composer and user-owned capabilities on the first-class page', async () => {
    const adapter = new FakeAdapter();
    render(<PulsaraApp adapter={adapter} />);

    const skillButton = await screen.findByRole('button', { name: '选择技能' });
    fireEvent.click(skillButton);
    fireEvent.click(screen.getByRole('button', { name: /\$pdf/ }));
    expect((screen.getByLabelText('发送给 Pulsara') as HTMLTextAreaElement).value).toBe('$pdf ');

    fireEvent.click(screen.getByRole('button', { name: '能力' }));
    expect(await screen.findByRole('heading', { name: '能力' })).toBeTruthy();
    expect(await screen.findByText('Personal Tools')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '查看 Personal Tools' }));
    expect(await screen.findByText('已用于当前打开的会话')).toBeTruthy();
    expect(screen.queryByText('/Users/test/.pulsara/plugins/personal-tools')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: /技能/ }));
    const skillSwitch = await screen.findByRole('switch', { name: '关闭 personal-pdf' });
    expect(skillSwitch.getAttribute('aria-checked')).toBe('true');
    fireEvent.click(skillSwitch);
    expect(adapter.setUserSkillEnabled).toHaveBeenCalledWith(
      '/Users/test/.agents/skills/personal-pdf/SKILL.md',
      false,
      'session-1',
    );
    expect(await screen.findByRole('switch', { name: '开启 personal-pdf' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '用于下一轮' })).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: /MCP/ }));
    expect(await screen.findByText('个人文档')).toBeTruthy();
    expect(screen.queryByText('本地文档')).toBeNull();
    expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session-1');
    expect(adapter.inspectUserCapabilities).toHaveBeenCalledWith('session-1');
  });

  it('manages directory capabilities in the session inspector and keeps inherited rows read-only', async () => {
    const adapter = new FakeAdapter();
    const projectSkill: CapabilitySnapshot['skills']['items'][number] = {
      id: 'pulsara:project-review',
      name: 'project-review',
      description: '检查当前项目。',
      location: '.pulsara/skills/project-review/SKILL.md',
      path: '/tmp/pulsara_agent/.pulsara/skills/project-review/SKILL.md',
      source: 'workspace',
      editable: true,
      enabled: true,
      effective: true,
      configured: false,
      authoringNotes: [],
    };
    const projectMcp: CapabilitySnapshot['mcp']['servers'][number] = {
      ...capabilitySnapshot.mcp.servers[0],
      id: 'project-docs',
      name: '项目文档',
      source: 'workspace',
      editable: true,
      configIdentity: 'project-config-v1',
      enabled: true,
      configuredEnabled: true,
      transport: { kind: 'http', summary: 'https://example.com/mcp', detail: 'https://example.com/mcp' },
    };
    const projectSnapshot: CapabilitySnapshot = {
      ...capabilitySnapshot,
      skills: {
        ...capabilitySnapshot.skills,
        items: [projectSkill, ...capabilitySnapshot.skills.items],
      },
      mcp: {
        ...capabilitySnapshot.mcp,
        servers: [projectMcp, ...capabilitySnapshot.mcp.servers],
      },
    };
    adapter.inspectCapabilities.mockResolvedValue(projectSnapshot);
    adapter.setProjectSkillEnabled.mockResolvedValue({
      operation: { status: 'DISABLED', success: true, message: '项目技能已关闭。', details: [] },
      adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-user-turn' },
      capabilities: {
        ...projectSnapshot,
        adoption: { scope: 'workspace', pending: true, when: 'next-user-turn' },
        skills: {
          ...projectSnapshot.skills,
          items: projectSnapshot.skills.items.map((item) => (
            item.id === projectSkill.id ? { ...item, enabled: false } : item
          )),
        },
      },
    });

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session-1'));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));

    expect(await within(inspector).findByText('项目能力')).toBeTruthy();
    expect(within(inspector).getByText('同一目录的所有会话', { exact: false })).toBeTruthy();
    const projectSwitch = within(inspector).getByRole('switch', { name: '关闭 project-review' });
    fireEvent.click(projectSwitch);
    await waitFor(() => expect(adapter.setProjectSkillEnabled).toHaveBeenCalledWith(
      'session-1',
      'pulsara:project-review',
      false,
    ));
    expect(await within(inspector).findByText('更改已保存')).toBeTruthy();
    expect(within(inspector).queryByRole('switch', { name: /pdf/ })).toBeNull();

    fireEvent.click(within(inspector).getByRole('tab', { name: /MCP/ }));
    expect(await within(inspector).findByText('项目文档')).toBeTruthy();
    expect(within(inspector).getByRole('switch', { name: '关闭 项目文档' })).toBeTruthy();
    expect(within(inspector).queryByRole('switch', { name: /本地文档/ })).toBeNull();
  });

  it('summarizes Skill conflicts without exposing internal absolute paths', async () => {
    const adapter = new FakeAdapter();
    const hiddenPath = '/Users/test/source/src/pulsara_agent/bundled_skills/pdf/SKILL.md';
    const hiddenWinner = '/Users/test/.pulsara/skills/pdf/SKILL.md';
    adapter.inspectCapabilities.mockResolvedValue({
      ...capabilitySnapshot,
      skills: {
        ...capabilitySnapshot.skills,
        issues: [{
          kind: 'shadowed',
          title: 'pdf 使用了优先级更高的版本',
          path: hiddenPath,
          details: [`当前使用：${hiddenWinner}`],
        }],
        details: ['internal producer unavailable'],
      },
    });

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));

    expect(await within(inspector).findByText(/pdf 使用了优先级更高的版本/)).toBeTruthy();
    expect(within(inspector).getByText(/部分技能目录暂时无法完整读取/)).toBeTruthy();
    expect(within(inspector).queryByText(hiddenPath, { exact: false })).toBeNull();
    expect(within(inspector).queryByText(hiddenWinner, { exact: false })).toBeNull();
    expect(within(inspector).queryByText(/internal producer unavailable/)).toBeNull();
  });

  it('keeps disabled or shadowed skills out of the composer suggestions', async () => {
    const adapter = new FakeAdapter();
    adapter.inspectCapabilities.mockResolvedValue({
      ...capabilitySnapshot,
      skills: {
        ...capabilitySnapshot.skills,
        items: [{
          id: 'pulsara:disabled-review',
          name: 'disabled-review',
          description: '当前目录中已停用的技能。',
          location: '.pulsara/skills/disabled-review/SKILL.md',
          path: '/tmp/pulsara_agent/.pulsara/skills/disabled-review/SKILL.md',
          source: 'workspace',
          editable: true,
          enabled: false,
          effective: false,
          configured: false,
          authoringNotes: [],
        }],
      },
    });

    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session-1'));
    await waitFor(() => expect(screen.queryByRole('button', { name: '选择技能' })).toBeNull());
  });

  it('renders the project capability dialog above the app and isolates its background', async () => {
    const adapter = new FakeAdapter();
    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    const addButton = await within(inspector).findByRole('button', { name: '添加' });
    addButton.focus();
    fireEvent.click(addButton);

    const dialog = await screen.findByRole('dialog', { name: '添加项目能力' });
    expect(inspector.contains(dialog)).toBe(false);
    const application = document.querySelector<HTMLElement>('main.pulsara-shell');
    expect(application?.inert).toBe(true);
    fireEvent.click(within(dialog).getByRole('button', { name: 'MCP' }));
    expect((within(dialog).getByRole('checkbox', { name: /允许子代理使用/ }) as HTMLInputElement).checked).toBe(false);

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '添加项目能力' })).toBeNull();
    expect(application?.inert).toBe(false);
    await waitFor(() => expect(document.activeElement).toBe(addButton));
  });

  it('keeps the project capability dialog open while its mutation is saving', async () => {
    const adapter = new FakeAdapter();
    let finishInstallation!: (value: Awaited<ReturnType<FakeAdapter['installSkill']>>) => void;
    adapter.installSkill.mockImplementation(() => new Promise((resolve) => {
      finishInstallation = resolve;
    }));

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    const addButton = await within(inspector).findByRole('button', { name: '添加' });
    fireEvent.click(addButton);
    const dialog = await screen.findByRole('dialog', { name: '添加项目能力' });
    fireEvent.change(within(dialog).getByPlaceholderText('/绝对路径/到/skill'), {
      target: { value: '/tmp/market-skill' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: '添加' }));

    await waitFor(() => expect(dialog.getAttribute('aria-busy')).toBe('true'));
    expect((addButton as HTMLButtonElement).disabled).toBe(true);
    for (const closeButton of within(dialog).getAllByRole('button', { name: '关闭' })) {
      expect((closeButton as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(closeButton);
    }
    const cancelButton = within(dialog).getByRole('button', { name: '取消' });
    expect((cancelButton as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(cancelButton);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: '添加项目能力' })).toBeTruthy();

    await act(async () => finishInstallation({
      installation: {
        status: 'INSTALLED',
        installed: true,
        message: '技能已经安装。',
        sourcePath: '/tmp/market-skill',
        destinationPath: '/tmp/pulsara_agent/.pulsara/skills/market-skill',
        details: [],
      },
      adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-user-turn' },
      capabilities: capabilitySnapshot,
    }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '添加项目能力' })).toBeNull());
  });

  it('refreshes a stale MCP snapshot after a rejected project mutation', async () => {
    const adapter = new FakeAdapter();
    const projectMcp: CapabilitySnapshot['mcp']['servers'][number] = {
      ...capabilitySnapshot.mcp.servers[0]!,
      id: 'project-docs',
      name: '项目文档',
      source: 'workspace',
      editable: true,
      configIdentity: 'approval-v1',
      transport: { kind: 'http', summary: 'https://example.com/mcp', detail: 'https://example.com/mcp' },
    };
    const projectSnapshot = {
      ...capabilitySnapshot,
      mcp: { ...capabilitySnapshot.mcp, servers: [projectMcp] },
    };
    adapter.inspectCapabilities.mockResolvedValue(projectSnapshot);
    adapter.setProjectMcpEnabled.mockRejectedValueOnce(new Error('项目能力已经变化，正在读取最新状态。'));

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(await within(inspector).findByRole('tab', { name: /MCP/ }));
    adapter.inspectCapabilities.mockClear();
    fireEvent.click(await within(inspector).findByRole('switch', { name: '关闭 项目文档' }));

    await waitFor(() => expect(adapter.setProjectMcpEnabled).toHaveBeenCalled());
    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session-1'));
  });

  it('shows a durable adoption warning and the actual production MCP failure reasons', async () => {
    const adapter = new FakeAdapter();
    adapter.inspectCapabilities.mockResolvedValue({
      ...capabilitySnapshot,
      adoption: {
        ...capabilitySnapshot.adoption,
        attention: 'PROJECT_MCP_ADOPTION_INCOMPLETE',
      },
      mcp: {
        ...capabilitySnapshot.mcp,
        servers: [
          {
            ...capabilitySnapshot.mcp.servers[0]!,
            id: 'protocol-failure',
            name: '协议样本',
            source: 'workspace',
            editable: true,
            configIdentity: 'project-config-protocol',
            status: 'failed',
            hasFailure: true,
            failureCategory: 'McpProtocolConformanceError',
          },
          {
            ...capabilitySnapshot.mcp.servers[0]!,
            id: 'transport-failure',
            name: '连接样本',
            source: 'workspace',
            editable: true,
            configIdentity: 'project-config-transport',
            status: 'failed-retryable',
            hasFailure: true,
            failureCategory: 'McpTransportOperationError',
          },
        ],
      },
    });

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    expect(await within(inspector).findByText('部分项目连接未载入')).toBeTruthy();
    fireEvent.click(within(inspector).getByRole('tab', { name: /MCP/ }));
    expect(await within(inspector).findByText(/服务返回的内容不符合当前 MCP 要求/)).toBeTruthy();
    expect(await within(inspector).findByText(/连接在通信时中断/)).toBeTruthy();
  });

  it('does not describe a general project capability adoption failure as an MCP-only problem', async () => {
    const adapter = new FakeAdapter();
    adapter.inspectCapabilities.mockResolvedValue({
      ...capabilitySnapshot,
      adoption: {
        ...capabilitySnapshot.adoption,
        attention: 'PROJECT_CAPABILITY_ADOPTION_FAILED',
      },
    });

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    expect(await within(inspector).findByText('项目能力配置需要处理')).toBeTruthy();
    expect(within(inspector).getByText(/技能与连接设置/)).toBeTruthy();
  });

  it('does not let an older capability inspection overwrite a completed mutation', async () => {
    const adapter = new FakeAdapter();
    const projectMcp: CapabilitySnapshot['mcp']['servers'][number] = {
      ...capabilitySnapshot.mcp.servers[0]!,
      id: 'project-docs',
      name: '项目文档',
      source: 'workspace',
      editable: true,
      configIdentity: 'approval-v1',
      enabled: true,
      configuredEnabled: true,
      effective: true,
      status: 'connecting',
      transport: {
        kind: 'http',
        summary: 'https://example.com/mcp',
        detail: 'https://example.com/mcp',
      },
    };
    const inspectingSnapshot: CapabilitySnapshot = {
      ...capabilitySnapshot,
      mcp: { ...capabilitySnapshot.mcp, servers: [projectMcp] },
    };
    let resolveOlderInspection!: (snapshot: CapabilitySnapshot) => void;
    const olderInspection = new Promise<CapabilitySnapshot>((resolve) => {
      resolveOlderInspection = resolve;
    });
    adapter.inspectCapabilities
      .mockResolvedValueOnce(inspectingSnapshot)
      .mockReturnValue(olderInspection);
    adapter.setProjectMcpEnabled.mockResolvedValue({
      operation: { status: 'DISABLED', success: true, message: '项目 MCP 已关闭。', details: [] },
      adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-user-turn' },
      capabilities: {
        ...inspectingSnapshot,
        mcp: {
          ...inspectingSnapshot.mcp,
          servers: [{
            ...projectMcp,
            configIdentity: 'approval-v2',
            enabled: false,
            configuredEnabled: false,
            effective: false,
            status: 'disabled',
          }],
        },
      },
    });

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(await within(inspector).findByRole('tab', { name: /MCP/ }));
    expect(await within(inspector).findByText(/连接中/)).toBeTruthy();
    await waitFor(
      () => expect(adapter.inspectCapabilities).toHaveBeenCalledTimes(2),
      { timeout: 1800 },
    );

    fireEvent.click(within(inspector).getByRole('switch', { name: '关闭 项目文档' }));
    expect(await within(inspector).findByRole('switch', { name: '开启 项目文档' })).toBeTruthy();
    await act(async () => resolveOlderInspection(inspectingSnapshot));
    expect(within(inspector).getByRole('switch', { name: '开启 项目文档' })).toBeTruthy();
  });

  it('does not paint a completed capability mutation onto a newly selected session', async () => {
    const adapter = new FakeAdapter();
    const secondSession: SessionSummary = {
      ...initialSession,
      id: 'session-2',
      title: '另一个会话',
      live: false,
      workspace: {
        id: 'workspace',
        name: 'pulsara_agent',
        path: '/tmp/pulsara_agent',
        kind: 'project',
      },
    };
    adapter.sessions = [initialSession, secondSession];
    const projectSkill: CapabilitySnapshot['skills']['items'][number] = {
      id: 'pulsara:project-review',
      name: 'project-review',
      description: '检查当前项目。',
      location: '.pulsara/skills/project-review/SKILL.md',
      path: '/tmp/pulsara_agent/.pulsara/skills/project-review/SKILL.md',
      source: 'workspace',
      editable: true,
      enabled: true,
      effective: true,
      configured: false,
      authoringNotes: [],
    };
    const firstSnapshot: CapabilitySnapshot = {
      ...capabilitySnapshot,
      sessionId: 'session-1',
      skills: { ...capabilitySnapshot.skills, items: [projectSkill] },
    };
    adapter.inspectCapabilities.mockImplementation(async (sessionId: string) => (
      sessionId === 'session-1'
        ? firstSnapshot
        : { ...capabilitySnapshot, sessionId: 'session-2' }
    ));
    let finishMutation!: (value: ProjectCapabilityMutationResult) => void;
    adapter.setProjectSkillEnabled.mockImplementation(() => (
      new Promise<ProjectCapabilityMutationResult>((resolve) => { finishMutation = resolve; })
    ));

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(await within(inspector).findByRole('switch', { name: '关闭 project-review' }));
    fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
    await screen.findByRole('heading', { name: '另一个会话' });

    await act(async () => {
      finishMutation({
        operation: { status: 'DISABLED', success: true, message: '项目技能已关闭。', details: [] },
        adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-user-turn' },
        capabilities: {
          ...firstSnapshot,
          skills: { ...firstSnapshot.skills, items: [{ ...projectSkill, enabled: false }] },
        },
      });
    });
    await waitFor(() => expect(within(inspector).queryByText('project-review')).toBeNull());
  });

  it('does not let a rejected mutation from an old session retire the new session inspection', async () => {
    const adapter = new FakeAdapter();
    const secondSession: SessionSummary = {
      ...initialSession,
      id: 'session-2',
      title: '新的能力会话',
      live: false,
      workspace: {
        id: 'workspace',
        name: 'pulsara_agent',
        path: '/tmp/pulsara_agent',
        kind: 'project',
      },
    };
    adapter.sessions = [initialSession, secondSession];
    const projectMcp: CapabilitySnapshot['mcp']['servers'][number] = {
      ...capabilitySnapshot.mcp.servers[0]!,
      id: 'project-docs',
      name: '项目文档',
      source: 'workspace',
      editable: true,
      configIdentity: 'approval-v1',
      transport: { kind: 'http', summary: 'https://example.com/mcp', detail: 'https://example.com/mcp' },
    };
    const firstSnapshot: CapabilitySnapshot = {
      ...capabilitySnapshot,
      sessionId: 'session-1',
      mcp: { ...capabilitySnapshot.mcp, servers: [projectMcp] },
    };
    let finishSecondInspection!: (value: CapabilitySnapshot) => void;
    const secondInspection = new Promise<CapabilitySnapshot>((resolve) => {
      finishSecondInspection = resolve;
    });
    adapter.inspectCapabilities.mockImplementation((sessionId: string) => (
      sessionId === 'session-1' ? Promise.resolve(firstSnapshot) : secondInspection
    ));
    let rejectOldMutation!: (reason: Error) => void;
    adapter.setProjectMcpEnabled.mockImplementation(() => new Promise((_, reject) => {
      rejectOldMutation = reject;
    }));

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(await within(inspector).findByRole('tab', { name: /MCP/ }));
    fireEvent.click(await within(inspector).findByRole('switch', { name: '关闭 项目文档' }));
    await waitFor(() => expect(adapter.setProjectMcpEnabled).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: /新的能力会话/ }));
    await screen.findByRole('heading', { name: '新的能力会话' });
    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session-2'));
    await act(async () => rejectOldMutation(new Error('项目能力已经变化。')));
    await act(async () => finishSecondInspection({
      ...capabilitySnapshot,
      sessionId: 'session-2',
    }));

    fireEvent.click(await within(inspector).findByRole('button', { name: /继承的能力/ }));
    expect(await within(inspector).findByText('本地文档')).toBeTruthy();
    await waitFor(() => expect(within(inspector).queryByText('正在读取项目能力')).toBeNull());
    expect(adapter.inspectCapabilities.mock.calls.filter(([sessionId]) => sessionId === 'session-1')).toHaveLength(1);
  });

  it('clears the pending capability notice after the next turn settles', async () => {
    const adapter = new FakeAdapter();
    adapter.inspectCapabilities
      .mockResolvedValueOnce({
        ...capabilitySnapshot,
        adoption: { ...capabilitySnapshot.adoption, pending: true },
      })
      .mockResolvedValue({
        ...capabilitySnapshot,
        adoption: { ...capabilitySnapshot.adoption, pending: false },
      });

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    expect(await within(inspector).findByText('更改已保存')).toBeTruthy();

    await act(async () => {
      adapter.lastConnection?.emit({
        ...projection('能力配置已采用。'),
        messages: [{
          id: 'assistant-settled',
          role: 'assistant',
          time: '现在',
          body: '能力配置已采用。',
          status: 'completed',
        }],
        isRunning: false,
        activeTurnId: undefined,
      });
    });

    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(within(inspector).queryByText('更改已保存')).toBeNull());
  });

  it('keeps refreshing a settling MCP connection until its status is final', async () => {
    const adapter = new FakeAdapter();
    let resolveReady!: (snapshot: CapabilitySnapshot) => void;
    const readySnapshot = new Promise<CapabilitySnapshot>((resolve) => {
      resolveReady = resolve;
    });
    adapter.inspectCapabilities
      .mockResolvedValueOnce({
        ...capabilitySnapshot,
        mcp: {
          ...capabilitySnapshot.mcp,
          servers: capabilitySnapshot.mcp.servers.map((server) => ({
            ...server,
            source: 'workspace' as const,
            editable: true,
            status: 'connecting' as const,
          })),
        },
      })
      .mockReturnValue(readySnapshot);

    const settledSnapshot: CapabilitySnapshot = {
        ...capabilitySnapshot,
        mcp: {
          ...capabilitySnapshot.mcp,
          servers: capabilitySnapshot.mcp.servers.map((server) => ({
            ...server,
            source: 'workspace' as const,
            editable: true,
            status: 'ready' as const,
          })),
        },
      };

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('tab', { name: /MCP/ }));

    expect(await within(inspector).findByText(/连接中/)).toBeTruthy();
    await waitFor(
      () => expect(adapter.inspectCapabilities).toHaveBeenCalledTimes(2),
      { timeout: 1800 },
    );
    resolveReady(settledSnapshot);
    expect(await within(inspector).findByText(/已连接/)).toBeTruthy();
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
    expect(screen.queryByRole('separator', { name: '上下文已压缩' })).toBeNull();
  });

  it('places the adopted context boundary between old and successor messages', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(),
      messages: [{
        id: 'before-compaction',
        entrySequence: 1,
        role: 'user',
        time: '10:00',
        body: '压缩前的消息',
      }, {
        id: 'after-compaction',
        entrySequence: 2,
        role: 'user',
        time: '10:01',
        body: '压缩后的消息',
      }],
      contextCompaction: {
        contextBindingRevisionId: 'revision-1',
        turnId: 'turn-1',
        sourceThroughSequence: 1,
        adoptedAfterEntrySequence: 1,
        acceptedAt: '2026-09-01T10:00:30Z',
      },
    };
    render(<PulsaraApp adapter={adapter} />);

    const before = await screen.findByText('压缩前的消息');
    const divider = screen.getByRole('separator', { name: '上下文已压缩' });
    const after = screen.getByText('压缩后的消息');

    expect(before.compareDocumentPosition(divider) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(divider.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(divider.querySelectorAll('.context-compaction-divider__line')).toHaveLength(2);
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
          body: '**先检查输入。**\n\n再生成最终回答。',
        }],
      }],
      isRunning: false,
    };
    render(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByText('思考摘要')).toBeTruthy();
    expect(screen.getByText('先检查输入。').tagName).toBe('STRONG');
    fireEvent.click(screen.getByRole('button', { name: '展开思考摘要' }));
    expect(screen.getByText(/再生成最终回答。/)).toBeTruthy();
    expect(screen.getByText('先检查输入。').tagName).toBe('STRONG');
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
          id: 'trace-read', kind: 'read', toolName: 'read_file', title: '读取文件', subtitle: '已完成',
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
    expect(screen.getByText('read_file')).toBeTruthy();
    expect(screen.getByText('读取文件')).toBeTruthy();

    const finalTurn = container.querySelector('.assistant-turn--response-with-reasoning');
    expect(finalTurn?.firstElementChild?.classList.contains('reasoning-disclosure')).toBe(true);
    expect(finalTurn?.children[1]?.classList.contains('assistant-heading--response')).toBe(true);
  });

  it('shows the exact tool name and keeps a terminal command inside its expanded output', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-terminal', role: 'assistant', time: '18:11', body: '', status: 'completed',
        traces: [{
          id: 'trace-terminal', kind: 'terminal', toolName: 'terminal', title: '运行命令',
          subtitle: '已完成', status: 'completed', command: 'printf "visible command"',
          output: ['visible command'], meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByText('terminal')).toBeTruthy();
    expect(screen.getByText('运行命令')).toBeTruthy();
    expect(container.querySelector('.terminal-command')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /terminal/ }));

    expect(container.querySelector('.terminal-command')?.textContent).toContain('printf "visible command"');
  });

  it('explains MCP meta-tool routing and identifies exact Skill documents', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-capabilities', role: 'assistant', time: '18:11', body: '', status: 'completed',
        traces: [{
          id: 'trace-reload', kind: 'artifact', toolName: 'reload_capabilities', title: '刷新能力',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          argumentsJson: '{}', resultText: '{"status":"RELOADED"}', output: ['操作已完成。'],
        }, {
          id: 'trace-skill', kind: 'read', toolName: 'read_file', title: '读取文件',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          argumentsJson: JSON.stringify({ path: '/opt/pulsara/bundled_skills/pdf/SKILL.md' }),
          resultText: JSON.stringify({ path: '/opt/pulsara/bundled_skills/pdf/SKILL.md', total_lines: 42 }),
          output: ['已读取 /opt/pulsara/bundled_skills/pdf/SKILL.md · 42 行'],
        }, {
          id: 'trace-list', kind: 'mcp', toolName: 'list_mcp_servers', title: '浏览 MCP 服务',
          subtitle: '已完成', status: 'completed', meta: '操作完成', argumentsJson: '{}',
          resultText: JSON.stringify({
            page_kind: 'SERVER_PAGE', total_server_count: 2,
            servers: [{ server_id: 'firecrawl', public_status: 'READY', tool_count: 3 }, {
              server_id: 'local-docs', public_status: 'CONNECTING', tool_count: 1,
            }],
          }),
          output: ['操作已完成。'],
        }, {
          id: 'trace-inspect', kind: 'mcp', toolName: 'inspect_new_mcp_tool', title: '检查 MCP 工具',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          argumentsJson: JSON.stringify({ server_id: 'firecrawl', tool_name: 'firecrawl_search' }),
          resultText: JSON.stringify({
            server_id: 'firecrawl', remote_tool_name: 'firecrawl_search',
            provider_tool_name: 'mcp__firecrawl__firecrawl_search', effect_kind: 'READ_ONLY',
            description: '搜索公开网页。', tool_ref: 'mcpref_firecrawl_search',
            input_schema: { type: 'object', properties: { query: { type: 'string' } } },
          }),
          output: ['操作已完成。'],
        }, {
          id: 'trace-use', kind: 'mcp', toolName: 'use_new_mcp_tool', title: '调用 MCP 工具',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          argumentsJson: JSON.stringify({
            tool_ref: 'mcpref_firecrawl_search', arguments: { query: 'Pulsara' },
          }),
          resultText: JSON.stringify({ content: '搜索完成' }), output: ['搜索完成'],
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByText('reload_capabilities')).toBeTruthy();
    expect(await screen.findByText('正在使用 pdf Skill')).toBeTruthy();
    expect(screen.queryByText('reload_plugins')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /list_mcp_servers/ }));
    const listDetails = container.querySelectorAll('.mcp-trace-details')[0] as HTMLElement;
    expect(within(listDetails).getByText('firecrawl')).toBeTruthy();
    expect(within(listDetails).getByText('local-docs')).toBeTruthy();
    expect(within(listDetails).getByText('已就绪 · 3 个工具')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /inspect_new_mcp_tool/ }));
    const inspectDetails = container.querySelectorAll('.mcp-trace-details')[1] as HTMLElement;
    expect(within(inspectDetails).getByText('检查的工具')).toBeTruthy();
    expect(within(inspectDetails).getByText('firecrawl_search')).toBeTruthy();
    expect(within(inspectDetails).queryByText('mcp__firecrawl__firecrawl_search')).toBeNull();
    expect(within(inspectDetails).queryByText('query')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /use_new_mcp_tool/ }));
    const useDetails = container.querySelectorAll('.mcp-trace-details')[2] as HTMLElement;
    expect(within(useDetails).getByText('调用的工具')).toBeTruthy();
    expect(within(useDetails).getByText('firecrawl_search')).toBeTruthy();
    expect(within(useDetails).queryByText(/"query": "Pulsara"/)).toBeNull();
  });

  it('offers copy only for the terminal assistant response', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-intermediate', role: 'assistant', assistantKind: 'tool-request',
        time: '18:11', body: '我先检查文件，然后继续处理。', status: 'completed',
        traces: [{
          id: 'trace-intermediate', kind: 'read', toolName: 'read_file', title: '读取文件',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
        }],
      }, {
        id: 'assistant-terminal-response', role: 'assistant', assistantKind: 'terminal',
        time: '18:12', body: '最终结果已经准备好。', status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    render(<PulsaraApp adapter={adapter} />);
    const intermediate = (await screen.findByText('我先检查文件，然后继续处理。')).closest('.assistant-copy');
    const terminal = screen.getByText('最终结果已经准备好。').closest('.assistant-copy');

    expect(intermediate).toBeTruthy();
    expect(terminal).toBeTruthy();
    expect(within(intermediate as HTMLElement).queryByRole('button', { name: '复制回复' })).toBeNull();
    expect(within(terminal as HTMLElement).getByRole('button', { name: '复制回复' })).toBeTruthy();
    expect(within(intermediate as HTMLElement).queryByText('18:11')).toBeNull();
    expect(within(terminal as HTMLElement).getByText('18:12', { selector: 'time' })).toBeTruthy();
    expect(screen.getAllByRole('button', { name: '复制回复' })).toHaveLength(1);
  });

  it('joins consecutive tool rails only when no visible content separates them', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-edit', role: 'assistant', assistantKind: 'tool-request',
        time: '18:11', body: '我先修正测试。', status: 'completed',
        traces: [{
          id: 'trace-edit', kind: 'edit', toolName: 'edit_file', title: '更新文件',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
        }],
      }, {
        id: 'assistant-terminal', role: 'assistant', assistantKind: 'tool-request',
        time: '18:12', body: '', status: 'completed',
        traces: [{
          id: 'trace-terminal', kind: 'terminal', toolName: 'terminal', title: '运行命令',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
        }],
      }, {
        id: 'assistant-reasoning-boundary', role: 'assistant', assistantKind: 'tool-request',
        time: '18:13', body: '', status: 'completed',
        reasoning: [{ id: 'reasoning-boundary', kind: 'full', body: '我需要先分析结果。' }],
        traces: [{
          id: 'trace-after-reasoning', kind: 'terminal', toolName: 'terminal', title: '运行命令',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    await screen.findByText('我先修正测试。');
    const turns = container.querySelectorAll('.assistant-turn');

    expect(turns).toHaveLength(3);
    expect(turns[0].classList.contains('assistant-turn--tool-chain-after')).toBe(true);
    expect(turns[1].classList.contains('assistant-turn--tool-chain-before')).toBe(true);
    expect(turns[1].classList.contains('assistant-turn--tool-chain-after')).toBe(false);
    expect(turns[2].classList.contains('assistant-turn--tool-chain-before')).toBe(false);
  });

  it('renders a delivered subtask completion as a neutral continuation event', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'accepted-result', role: 'user', userKind: 'subagent-completion', time: '18:14',
        body: '{"status":"accepted","task_id":"internal"}',
        sourceSubagentTaskId: 'task-internal',
        status: 'completed',
      }, {
        id: 'assistant-after-result', role: 'assistant', time: '18:15',
        body: '我会基于子任务结果继续。', status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByLabelText('Pulsara 已收到子任务进展')).toBeTruthy();
    expect(screen.getByText('Pulsara 已收到子任务进展')).toBeTruthy();
    expect(screen.getByRole('tooltip').textContent).toContain('Pulsara 已把这项工作的进展用于当前处理');
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

  it('replaces the entire composer until an active session exists', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [];
    render(<PulsaraApp adapter={adapter} />);

    await screen.findByText(/准备好继续/);
    fireEvent.click(screen.getByRole('button', { name: '会话' }));

    expect(await screen.findByText('创建或选择会话后开始')).toBeTruthy();
    expect(screen.getByText('当前没有活动会话')).toBeTruthy();
    expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
    expect(screen.queryByRole('button', { name: '选择模型' })).toBeNull();
    expect(screen.queryByRole('button', { name: '先规划' })).toBeNull();
    expect(screen.queryByRole('button', { name: /完全访问/ })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '创建会话' }));
    expect(screen.getByRole('heading', { name: '新建会话' })).toBeTruthy();
  });

  it('blocks the workbench and guides setup without touching session data in zero configuration', async () => {
    class ZeroConfigAdapter extends FakeAdapter {
      override listSessions = vi.fn(async (): Promise<SessionSummary[]> => {
        throw new Error('session data plane must not be called');
      });

      override async bootstrap(): Promise<RuntimeBootstrap> {
        return {
          ...bootstrap,
          runtime: {
            ...bootstrap.runtime,
            database_state: 'database_not_configured',
          },
          database_state: 'database_not_configured',
          local_settings: {
            state: 'ready',
            postgres: null,
            dashscope_credentials: { embedding_configured: false, rerank_configured: false },
          },
          model_configurations: [],
        };
      }

      override async localSettings() {
        return {
          local_settings: {
            state: 'ready' as const,
            postgres: null,
            dashscope_credentials: { embedding_configured: false, rerank_configured: false },
          },
          model_configurations: [],
          database_state: 'database_not_configured' as const,
        };
      }
    }

    const adapter = new ZeroConfigAdapter();
    render(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByRole('heading', { name: '先连接 PostgreSQL，再开始会话' })).toBeTruthy();
    expect(screen.getByLabelText('PostgreSQL 配置引导')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /新建会话/ })).toBeNull();
    expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
    expect(adapter.listSessions).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /前往本地服务设置/ }));
    expect(await screen.findByRole('heading', { name: 'PostgreSQL' })).toBeTruthy();
    expect(screen.getByText('尚未配置 PostgreSQL')).toBeTruthy();
  });

  it('replaces overview session content with PostgreSQL setup guidance while unavailable', async () => {
    class UnavailableDatabaseAdapter extends FakeAdapter {
      override listSessions = vi.fn(async (): Promise<SessionSummary[]> => {
        throw new Error('session data plane must not be called');
      });

      override async bootstrap(): Promise<RuntimeBootstrap> {
        return {
          ...bootstrap,
          runtime: { ...bootstrap.runtime, database_state: 'database_unavailable' },
          database_state: 'database_unavailable',
        };
      }
    }

    const adapter = new UnavailableDatabaseAdapter();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: 'PostgreSQL 当前无法连接' });
    fireEvent.click(screen.getByRole('button', { name: '总览' }));

    expect(await screen.findByText(/先准备好/)).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'PostgreSQL 当前无法连接' })).toBeTruthy();
    expect(screen.getByText('保存连接')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: '最近会话' })).toBeNull();
    expect(screen.queryByRole('button', { name: '开始新任务' })).toBeNull();
    expect(adapter.listSessions).not.toHaveBeenCalled();
  });

  it('adds a catalog-backed model configuration without retaining the typed API key', async () => {
    const adapter = new FakeAdapter();
    const add = vi.spyOn(adapter, 'addModelConfiguration');
    const { container } = render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));

    await screen.findByRole('option', { name: 'Local Test' });
    fireEvent.change(screen.getByLabelText('提供方'), { target: { value: 'test' } });
    await screen.findByRole('option', { name: /test-model · test-model/ });
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'test-model' } });
    await screen.findByRole('option', { name: 'Responses · models.dev 建议' });
    fireEvent.change(screen.getByLabelText('API 协议'), { target: { value: 'openai_responses' } });
    const secret = 'front-end-secret-sentinel';
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: secret } });
    expect(screen.getByText('确认连接')).toBeTruthy();
    expect(container.textContent).toContain('只支持与 OpenAI Chat Completions 或 Responses 兼容的接口');
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));

    await waitFor(() => expect(add).toHaveBeenCalledWith({
      source: 'models_dev',
      route_id: 'test',
      model_id: 'test-model',
      wire_api: 'openai_responses',
      api_key: secret,
    }));
    await waitFor(() => expect(screen.queryByLabelText('API key')).toBeNull());
    expect(container.textContent).not.toContain(secret);
  });

  it('deletes one model configuration without silently rebinding its sessions', async () => {
    const adapter = new FakeAdapter();
    const remove = vi.spyOn(adapter, 'deleteModelConfiguration');
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));

    const deleteButton = await screen.findByRole('button', {
      name: /删除模型配置 Local Test · test-model/,
    });
    fireEvent.click(deleteButton);
    expect(screen.getByText('删除后，已有会话不会自动改用其他模型。')).toBeTruthy();
    expect(remove).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(
      'model-connection:00000000000000000000000000000000',
    ));
    expect(await screen.findByText('还没有模型配置')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '会话' }));
    await waitFor(() => expect(
      document.querySelector('.mode-chip.model-chip')?.textContent,
    ).toContain('模型配置已删除'));
    expect(screen.getByText(/原模型配置已删除，请重新选择/)).toBeTruthy();
    expect(adapter.sessions[0].modelCallBinding?.connection_id).toBe(
      'model-connection:00000000000000000000000000000000',
    );
  });

  it('adds and independently tests a user-declared OpenAI-compatible model configuration', async () => {
    const adapter = new FakeAdapter();
    const add = vi.spyOn(adapter, 'addModelConfiguration');
    const test = vi.spyOn(adapter, 'testModelConfiguration');
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));
    fireEvent.click(screen.getByRole('button', { name: '自定义服务' }));

    fireEvent.change(screen.getByLabelText('配置名称'), { target: { value: 'Local Gateway' } });
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'http://127.0.0.1:9000/v1' } });
    fireEvent.change(screen.getByLabelText('Model ID'), { target: { value: 'local-model' } });
    fireEvent.change(screen.getByLabelText('API 协议'), { target: { value: 'openai_chat_completions' } });
    fireEvent.change(screen.getByLabelText('Reasoning 控制'), { target: { value: 'effort' } });
    fireEvent.change(screen.getByLabelText('Effort 列表'), { target: { value: 'low, high, high' } });
    const secret = 'custom-front-end-secret';
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: secret } });

    fireEvent.click(screen.getByRole('button', { name: '测试连接' }));
    await waitFor(() => expect(test).toHaveBeenCalledWith(expect.objectContaining({
      source: 'user_declared',
      configuration_name: 'Local Gateway',
      base_url: 'http://127.0.0.1:9000/v1',
      model_id: 'local-model',
      wire_api: 'openai_chat_completions',
      authentication: 'bearer_api_key',
      api_key: secret,
      context_tokens: 256000,
      max_output_tokens: 8192,
      tool_call: true,
      reasoning: { kind: 'effort', values: ['low', 'high'] },
    })));
    expect(add).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(add).toHaveBeenCalledWith(expect.objectContaining({
      source: 'user_declared',
      api_key: secret,
    })));
  });

  it('allows a user-declared no-auth target without rendering an API key field', async () => {
    const adapter = new FakeAdapter();
    const add = vi.spyOn(adapter, 'addModelConfiguration');
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));
    fireEvent.click(screen.getByRole('button', { name: '自定义服务' }));
    fireEvent.change(screen.getByLabelText('配置名称'), { target: { value: 'Local No Auth' } });
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'http://127.0.0.1:11434/v1' } });
    fireEvent.change(screen.getByLabelText('Model ID'), { target: { value: 'local-model' } });
    fireEvent.change(screen.getByLabelText('API 协议'), { target: { value: 'openai_responses' } });
    fireEvent.change(screen.getByLabelText('认证方式'), { target: { value: 'none' } });

    expect(screen.queryByLabelText('API key')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(add).toHaveBeenCalledWith(expect.objectContaining({
      source: 'user_declared',
      authentication: 'none',
      api_key: null,
      reasoning: { kind: 'provider_default' },
    })));
  });

  it('keeps saving available after an independent connection test fails', async () => {
    const adapter = new FakeAdapter();
    const test = vi.spyOn(adapter, 'testModelConfiguration').mockRejectedValue(new Error('endpoint rejected test'));
    const add = vi.spyOn(adapter, 'addModelConfiguration');
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));
    fireEvent.click(screen.getByRole('button', { name: '自定义服务' }));
    fireEvent.change(screen.getByLabelText('配置名称'), { target: { value: 'Save Anyway' } });
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'https://example.test/v1' } });
    fireEvent.change(screen.getByLabelText('Model ID'), { target: { value: 'future-model' } });
    fireEvent.change(screen.getByLabelText('API 协议'), { target: { value: 'openai_chat_completions' } });
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'draft-key' } });

    fireEvent.click(screen.getByRole('button', { name: '测试连接' }));
    await waitFor(() => expect(test).toHaveBeenCalledOnce());
    const save = screen.getByRole('button', { name: '保存配置' });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(add).toHaveBeenCalledWith(expect.objectContaining({
      source: 'user_declared',
      api_key: 'draft-key',
    })));
  });

  it('sorts provider names and model IDs alphabetically in their selectors', async () => {
    const adapter = new FakeAdapter();
    const originalCatalog = adapter.modelCatalog.bind(adapter);
    vi.spyOn(adapter, 'modelCatalog').mockImplementation(async () => {
      const catalog = await originalCatalog();
      const baseModel = catalog.routes[0].models[0];
      return {
        ...catalog,
        routes: [{
          route_id: 'zulu',
          display_name: 'Zulu Provider',
          models: [],
        }, {
          route_id: 'alpha',
          display_name: 'alpha Provider',
          models: [
            { ...baseModel, model_id: 'zeta-model', display_name: 'Zeta Model' },
            { ...baseModel, model_id: 'Alpha-model', display_name: 'Alpha Model' },
            { ...baseModel, model_id: 'middle-model', display_name: 'Middle Model' },
          ],
        }, {
          route_id: 'bravo',
          display_name: 'Bravo Provider',
          models: [],
        }],
      };
    });

    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));

    const providerSelect = await screen.findByLabelText('提供方') as unknown as HTMLSelectElement;
    expect([...providerSelect.options].slice(1).map((option) => option.value)).toEqual([
      'alpha', 'bravo', 'zulu',
    ]);

    fireEvent.change(providerSelect, { target: { value: 'alpha' } });
    const modelSelect = screen.getByLabelText('模型') as unknown as HTMLSelectElement;
    expect([...modelSelect.options].slice(1).map((option) => option.value)).toEqual([
      'Alpha-model', 'middle-model', 'zeta-model',
    ]);
  });

  it('keeps catalog routes visible when their wire adapters are not executable', async () => {
    const adapter = new FakeAdapter();
    const originalCatalog = adapter.modelCatalog.bind(adapter);
    vi.spyOn(adapter, 'modelCatalog').mockImplementation(async () => {
      const catalog = await originalCatalog();
      return {
        ...catalog,
        routes: [...catalog.routes, {
          route_id: 'catalog-only',
          display_name: 'Catalog Only Provider',
          models: [{
            model_id: 'catalog-only-model',
            display_name: 'Catalog Only Model',
            wire_dialect: 'provider_native' as const,
            context_tokens: 256000,
            input_tokens: 256000,
            output_tokens: 8192,
            tool_call: null,
            wire_shape_hint: null,
            wire_apis: [
              {
                wire_api: 'openai_chat_completions' as const,
                executable: false,
                reason: 'route_wire_adapter_unavailable',
                endpoint: null,
              },
              {
                wire_api: 'openai_responses' as const,
                executable: false,
                reason: 'route_wire_adapter_unavailable',
                endpoint: null,
              },
            ],
          }],
        }],
      };
    });

    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));

    await screen.findByRole('option', { name: 'Catalog Only Provider' });
    fireEvent.change(screen.getByLabelText('提供方'), { target: { value: 'catalog-only' } });
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'catalog-only-model' } });

    expect(screen.getByText(/没有声明 OpenAI-compatible 接口/)).toBeTruthy();
    expect((screen.getByLabelText('API 协议') as unknown as HTMLSelectElement).disabled).toBe(true);
    expect(screen.getByRole('option', { name: 'Chat Completions · 暂不支持' })).toBeTruthy();
    expect(screen.getByRole('option', { name: 'Responses · 暂不支持' })).toBeTruthy();
  });

  it('keeps an exact reasoning selection until the user changes it again', async () => {
    const adapter = new FakeAdapter();
    const update = vi.spyOn(adapter, 'updateModelCallBinding');
    const first = render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });

    fireEvent.click(screen.getByRole('button', { name: /推理 medium/ }));
    expect(screen.queryByText('Token 预算')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'high' }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(
      'session-1',
      {
        connection_id: 'model-connection:00000000000000000000000000000000',
        reasoning: { kind: 'effort', value: 'high' },
      },
    ));
    expect(await screen.findByRole('button', { name: /推理 high/ })).toBeTruthy();

    first.unmount();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    expect(screen.getByRole('button', { name: /推理 high/ })).toBeTruthy();
    expect(update).toHaveBeenCalledTimes(1);
  });

  it('shows a warning and adopts the kernel default when a reasoning choice is stale', async () => {
    const adapter = new FakeAdapter();
    adapter.updateModelCallBinding = vi.fn(async (sessionId, binding) => {
      const accepted = {
        ...binding,
        reasoning: { kind: 'effort' as const, value: 'medium' },
      };
      adapter.sessions = adapter.sessions.map((session) => session.id === sessionId
        ? { ...session, modelCallBinding: accepted }
        : session);
      return { modelCallBinding: accepted, reasoningPreferenceReset: true };
    });
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });

    fireEvent.click(screen.getByRole('button', { name: /推理 medium/ }));
    fireEvent.click(screen.getByRole('button', { name: 'high' }));

    expect(await screen.findByText('推理选项已更新')).toBeTruthy();
    expect(screen.getByText(/原选择已不再适用于该模型/)).toBeTruthy();
    expect(screen.getByRole('button', { name: /推理 medium/ })).toBeTruthy();
  });

  it('uses shared session presence and the simplified local overview chrome', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [
      initialSession,
      {
        id: 'session-loaded',
        title: '后台检查',
        subtitle: '8 条记录',
        status: 'waiting',
        updatedAt: '2 分钟前',
        live: true,
      },
      {
        id: 'session-resumable',
        title: '历史会话',
        subtitle: '21 条记录',
        status: 'completed',
        updatedAt: '昨天',
        live: false,
      },
    ];
    const { container } = render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });

    fireEvent.click(screen.getByRole('button', { name: '总览' }));

    expect(screen.getAllByText('最近会话')).toHaveLength(2);
    expect(screen.queryByText('最近工作')).toBeNull();
    const runtimeHealth = container.querySelector('.runtime-health');
    expect(runtimeHealth?.textContent).toBe('本地服务已连接');
    expect(runtimeHealth?.querySelector('small')).toBeNull();
    expect(container.querySelectorAll('.active-mission .session-presence--current')).toHaveLength(1);
    expect(container.querySelector('.mission-presence-column')?.textContent).toBe('当前会话');
    expect(container.querySelectorAll('.recent-table .session-presence--current')).toHaveLength(1);
    expect(container.querySelectorAll('.recent-table .session-presence--loaded')).toHaveLength(1);
    expect(container.querySelectorAll('.recent-table .session-presence--resumable')).toHaveLength(1);
    expect(Array.from(container.querySelectorAll('.recent-table .table-state'), (node) => node.textContent))
      .toEqual(['当前会话', '已载入', '可恢复']);
    const systemCard = container.querySelector('.system-card');
    expect(systemCard?.querySelector('.healthy-dot')).toBeNull();
    expect(systemCard?.querySelector('footer span')).toBeNull();
    expect(systemCard?.querySelector('footer')?.textContent).toBe('仅限这台设备');
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
    const { container } = render(<PulsaraApp adapter={adapter} />);

    await screen.findByRole('heading', { name: '准备发布' });
    expect(await screen.findByText('1 条记录 · 当前会话')).toBeTruthy();
    expect(screen.getByText('13 条记录 · 可恢复')).toBeTruthy();
    expect(container.querySelectorAll('.session-presence--current')).toHaveLength(1);
    expect(container.querySelectorAll('.session-presence--loaded')).toHaveLength(0);
    expect(container.querySelectorAll('.session-presence--resumable')).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: /继续检查/ }));
    await screen.findByRole('heading', { name: '继续检查' });
    expect(await screen.findByText('13 条记录 · 当前会话')).toBeTruthy();
    expect(screen.getByText('1 条记录 · 已载入')).toBeTruthy();
    expect(container.querySelectorAll('.session-presence--current')).toHaveLength(1);
    expect(container.querySelectorAll('.session-presence--loaded')).toHaveLength(1);
    expect(container.querySelectorAll('.session-presence--resumable')).toHaveLength(0);
    await waitFor(() => expect(adapter.listSessions).toHaveBeenCalledTimes(3));
  });

  it('groups sessions into collapsible project directories and a final quick-start section', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [{
      ...initialSession,
      workspace: { id: 'project-alpha', name: 'alpha', path: '/tmp/alpha', kind: 'project' },
    }, {
      id: 'session-alpha-2', title: '继续 alpha', subtitle: '7 条记录', status: 'completed',
      updatedAt: '3 分钟前', live: false,
      workspace: { id: 'project-alpha', name: 'alpha', path: '/tmp/alpha', kind: 'project' },
    }, {
      id: 'session-beta', title: '检查 beta', subtitle: '4 条记录', status: 'completed',
      updatedAt: '10 分钟前', live: false,
      workspace: { id: 'project-beta', name: 'beta', path: '/tmp/beta', kind: 'project' },
    }, {
      id: 'session-quick', title: '快速排查', subtitle: '2 条记录', status: 'completed',
      updatedAt: '昨天', live: false,
      workspace: { id: 'quick-one', name: '快速开始', path: '/tmp/pulsara-quick', kind: 'quick' },
    }];

    const { container } = render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const directoryTree = screen.getByRole('region', { name: '会话目录' });
    expect(container.querySelector('.workspace-heading')).toBeNull();
    expect(within(directoryTree).getByRole('button', { name: '从目录中打开' }).getAttribute('aria-expanded')).toBe('true');
    expect(within(directoryTree).getByRole('button', { name: '快速开始' }).getAttribute('aria-expanded')).toBe('true');
    const alphaGroup = within(directoryTree).getByRole('region', { name: 'alpha 会话' });
    expect(within(alphaGroup).getByText('准备发布')).toBeTruthy();
    expect(within(alphaGroup).getByText('继续 alpha')).toBeTruthy();
    expect(within(directoryTree).getByText('快速排查')).toBeTruthy();
    expect(within(directoryTree).queryByText('3 分钟前')).toBeNull();
    expect(within(directoryTree).queryByText('10 分钟前')).toBeNull();
    expect(within(directoryTree).queryByText('昨天')).toBeNull();
    expect(directoryTree.querySelector('.session-section:last-child')?.classList.contains('session-section--quick')).toBe(true);

    fireEvent.click(within(alphaGroup).getByRole('button', { name: 'alpha' }));
    expect(within(alphaGroup).queryByText('准备发布')).toBeNull();
    expect(within(alphaGroup).queryByText('继续 alpha')).toBeNull();

    fireEvent.click(within(directoryTree).getByRole('button', { name: '从目录中打开' }));
    fireEvent.click(within(directoryTree).getByRole('button', { name: '快速开始' }));
    expect(within(directoryTree).queryByRole('region', { name: 'alpha 会话' })).toBeNull();
    expect(within(directoryTree).queryByText('快速排查')).toBeNull();
    expect(within(directoryTree).getByRole('button', { name: '从目录中打开' })).toBeTruthy();
    expect(within(directoryTree).getByRole('button', { name: '快速开始' })).toBeTruthy();
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
    const { container } = render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
    await screen.findByText('这个会话还没有消息');

    fireEvent.click(screen.getByRole('button', { name: /选择模型/ }));
    fireEvent.click(screen.getByRole('button', { name: /Local Test · test-model/ }));
    await waitFor(() => expect(adapter.sessions[0].modelCallBinding?.connection_id).toBe(
      'model-connection:00000000000000000000000000000000',
    ));

    fireEvent.click(screen.getByRole('button', { name: '先规划' }));
    const permissionTrigger = screen.getByRole('button', { name: /完全访问/ });
    expect(permissionTrigger.classList.contains('is-danger')).toBe(true);
    fireEvent.click(permissionTrigger);
    expect(
      [...container.querySelectorAll('.permission-menu .permission-option-title')]
        .map((element) => element.textContent),
    ).toEqual(['只读', '每次询问', '接受编辑', '完全访问']);
    expect(
      container.querySelector('.permission-option--danger .permission-warning-icon'),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /只读/ }));
    const composer = screen.getByLabelText('发送给 Pulsara');
    fireEvent.change(composer, { target: { value: '验证新的前端任务' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('验证新的前端任务')).toBeTruthy();
    expect(adapter.lastConnection?.enterPlan).toHaveBeenCalledWith('验证新的前端任务', 'read-only');
    expect(adapter.lastConnection?.submitPrompt).toHaveBeenCalledWith('验证新的前端任务', 'read-only');
    expect(screen.getByRole('button', { name: /完全访问/ })).toBeTruthy();
  });

  it('leaves Enter to the input method while the composer is composing text', async () => {
    const adapter = new FakeAdapter();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const composer = screen.getByLabelText('发送给 Pulsara') as HTMLTextAreaElement;

    fireEvent.compositionStart(composer);
    fireEvent.change(composer, { target: { value: 'biruzhey' } });
    expect(fireEvent.keyDown(
      composer,
      { key: 'Enter', code: 'Enter', isComposing: true },
    )).toBe(true);

    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
    expect(composer.value).toBe('biruzhey');

    fireEvent.compositionEnd(composer);
    expect(fireEvent.keyDown(
      composer,
      { key: 'Enter', code: 'Enter', keyCode: 229 },
    )).toBe(true);
    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
    expect(composer.value).toBe('biruzhey');

    expect(fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' })).toBe(false);
    await waitFor(() => expect(adapter.lastConnection?.submitPrompt).toHaveBeenCalledWith(
      'biruzhey',
      'bypass-permissions',
    ));
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
    expect(screen.queryByLabelText('TODO清单')).toBeNull();
    expect(screen.getByRole('button', { name: '展开TODO清单' }).hasAttribute('disabled')).toBe(true);
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
        id: 'assistant-root', turnId: 'turn-1', role: 'assistant', time: '现在',
        body: '主任务继续运行。', status: 'running',
        traces: [{
          id: 'spawn-task', kind: 'mcp', toolName: 'spawn_agent', title: '创建子任务',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
      agentTasks: [],
    };
    adapter.taskInventory = [{
      id: 'task-complete', label: '汇总研究', role: '研究', profile: 'research_worker',
      objective: '### 汇总目标\n\n整理可见结果。', status: 'completed',
      parentId: 'turn-1', batchId: 'batch-1', taskKey: 'research',
      context: { mode: 'last-n', lastNTurns: 4 }, dependencyIds: [],
      completionDelivered: false,
      acceptedAt: '2026-08-30T12:00:00Z', terminalAt: '2026-08-30T12:01:00Z',
      summary: '### 研究结论\n\n页面符合契约。', color: 'blue',
      result: {
        id: 'result-complete', entryId: 'entry-result', summary: '### 研究结论\n\n页面符合契约。',
        outputPreview: '- 完整输出', diagnostics: [{ message: '目视检查通过' }],
      },
    }, {
      id: 'task-interrupted', label: '中断检查', role: '验证', objective: '验证重启边界。',
      status: 'interrupted', parentId: 'turn-2', dependencyIds: [], color: 'amber',
      completionDelivered: false,
    }, {
      id: 'task-blocked', label: '等待产物', role: '整合', objective: '整合前置产物。',
      status: 'blocked', parentId: 'turn-1', batchId: 'batch-1', dependencyIds: ['task-interrupted'],
      dependencies: [{ id: 'task-interrupted', label: '中断检查', status: 'interrupted' }], color: 'violet',
      completionDelivered: false,
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

    fireEvent.click(screen.getByRole('button', { name: /完全访问/ }));
    fireEvent.click(screen.getByRole('button', { name: /只读/ }));
    const continueButton = within(taskCard).getByRole('button', { name: '用这份结果继续' });
    expect(within(taskCard).getByRole('tooltip').textContent).toContain('启动新一轮，让 Pulsara 基于这项工作的结果继续处理');
    fireEvent.click(continueButton);
    await waitFor(() => expect(adapter.lastConnection?.acceptSubagentCompletion).toHaveBeenCalledWith('task-complete', 'read-only'));
    expect(await screen.findByText('Pulsara 已收到结果')).toBeTruthy();
    expect(screen.getByRole('button', { name: /完全访问/ })).toBeTruthy();

    vi.useFakeTimers();
    try {
      fireEvent.click(within(taskCard).getByRole('button', { name: '在对话中查看' }));
      const focusedRun = container.querySelector('.subagent-run.is-focused[data-task-id="task-complete"]');
      expect(focusedRun).toBeTruthy();
      expect(focusedRun?.classList.contains('is-expanded')).toBe(true);

      await act(async () => vi.advanceTimersByTimeAsync(2600));

      const settledRun = container.querySelector('.subagent-run[data-task-id="task-complete"]');
      expect(settledRun?.classList.contains('is-focused')).toBe(false);
      expect(settledRun?.classList.contains('is-expanded')).toBe(true);
      expect(document.activeElement).not.toBe(settledRun?.querySelector('.subagent-run__header'));
    } finally {
      vi.useRealTimers();
    }
  });
});
