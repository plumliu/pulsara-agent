import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LocalMemoryApi } from '../lib/memory-api';
import { promptContentTextProjection } from '../lib/prompt-content';
import { RuntimeApiError } from '../lib/runtime-adapter';
import type {
  CommandReceipt,
  BackgroundProcessLog,
  BackgroundProcessPage,
  ForkOutcome,
  LocalPromptSubmission,
  RuntimeAdapter,
  RuntimeBootstrap,
  ModelCatalogReadModel,
  RuntimeConnection,
  RuntimeInteractionContent,
  RuntimeInteractionSummary,
  RuntimeProjection,
  UserControlQueryResult,
  CanonicalPromptContent,
  CanonicalPromptImagePart,
  EditablePromptContent,
} from '../lib/runtime-adapter';
import type {
  AgentTask,
  CapabilitySnapshot,
  ProjectCapabilityMutationResult,
  SessionSummary,
  SessionWorkspaceSelection,
  UserCapabilitySnapshot,
} from '../lib/pulsara-types';
import { WELCOME_TYPEWRITER_PHRASES } from '../components/welcome-typewriter';
import PulsaraApp from './pulsara-app';

const isWelcomeHeading = (name: string) => (WELCOME_TYPEWRITER_PHRASES as readonly string[]).includes(name);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function textPrompt(text: string) {
  return { parts: [{ type: 'text' as const, text }] };
}

function stubClipboard(writeText: (value: string) => Promise<void>) {
  const current = navigator;
  vi.stubGlobal('navigator', new Proxy(current, {
    get(target, property) {
      if (property === 'clipboard') return { writeText };
      const value = Reflect.get(target, property, target) as unknown;
      return typeof value === 'function' ? value.bind(target) : value;
    },
  }));
}

async function typeComposer(value: string): Promise<HTMLElement> {
  const composer = screen.getByLabelText('发送给 Pulsara') as HTMLElement;
  await userEvent.click(composer);
  await userEvent.type(composer, value, { skipClick: true });
  return composer;
}

function composerIsDisabled(composer: HTMLElement): boolean {
  return composer.getAttribute('contenteditable') === 'false';
}

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

const SOURCE_FIDELITY_MARKDOWN = [
  '保留 `read_file`、edit_file、ROOT、Kernel、HostSession、read-only 与 bypass-permissions。',
  '',
  '```python',
  'from api import read_file, edit_file',
  'ROOT = "read-only"',
  'exit_code = 0',
  '```',
  '',
].join('\n');

const SOURCE_FIDELITY_USER_TEXT = [
  'F07-BEGIN',
  '```python',
  'from api import read_file, edit_file',
  'ROOT = "read-only"',
  '```',
  '{"tool":"edit_file","url":"https://example.test/Kernel?q=read-only"}',
  'F07-END',
].join('\n');

const capabilitySnapshot: CapabilitySnapshot = {
  sessionId: 'session-1',
  workspacePath: '/tmp/pulsara_agent',
  workspaceKind: 'project',
  credentialScopeKey: 'workspace-test',
  adoption: { scope: 'workspace', pending: false, when: 'next-provider-dispatch' },
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
      currentIdentity: 'current-personal-docs',
      config: { display_name: '个人文档', enabled: true, transport: { type: 'streamable_http', endpoint: 'https://example.com/mcp' }, auth: { type: 'none' } },
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
    queuedPrompts: [],
    promptTransitions: [],
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
  readonly generation: number;
  private value: RuntimeProjection;
  private observer?: (value: RuntimeProjection) => void;
  private closed = false;

  constructor(
    readonly sessionId: string,
    value = projection(),
    role: 'controller' | 'observer' = 'controller',
    private readonly interactionContent?: RuntimeInteractionContent,
    generation = 1,
    private readonly queryCommandResult?: CommandReceipt,
  ) {
    this.value = value;
    this.role = role;
    this.generation = generation;
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

  submitPrompt = vi.fn(async (commandId: string): Promise<CommandReceipt> => {
    return { commandId, status: 'succeeded', publicMessage: '任务已经开始。' };
  });

  async cancelQueuedPrompt(commandId: string, queueItemId: string): Promise<CommandReceipt> {
    return { commandId, status: 'succeeded', publicCode: 'PROMPT_CANCELLED', promptDelivery: {
      queueItemId, queueStatus: 'CANCELLED', deliveryMode: 'new-turn',
    } };
  }

  async steerQueuedPrompt(commandId: string): Promise<CommandReceipt> {
    return { commandId, status: 'succeeded' };
  }

  async stopActiveTurn(): Promise<CommandReceipt> {
    return { commandId: 'command-3', status: 'succeeded' };
  }

  async cancelSubagentTask(): Promise<CommandReceipt> {
    return { commandId: 'command-cancel-task', status: 'succeeded' };
  }

  async terminateBackgroundProcess(): Promise<CommandReceipt> {
    return { commandId: 'command-terminate-process', status: 'succeeded' };
  }

  async queryControlCommand(): Promise<UserControlQueryResult> {
    return { status: 'RESULT_UNAVAILABLE' };
  }

  async listBackgroundProcesses(): Promise<BackgroundProcessPage> {
    return { processes: [] };
  }

  async readBackgroundProcessLog(): Promise<BackgroundProcessLog> {
    throw new Error('No background process fixture');
  }

  async readCanonicalEntryContent(): Promise<string> {
    throw new Error('No canonical task activity fixture');
  }

  async readPromptImage(image: CanonicalPromptImagePart): Promise<Uint8Array> {
    void image;
    throw new Error('No prompt image fixture');
  }

  async readPromptForEdit(content: CanonicalPromptContent): Promise<EditablePromptContent> {
    const parts: EditablePromptContent['parts'][number][] = [];
    for (const part of content.parts) {
      parts.push(part.type === 'text'
        ? { type: 'text', text: part.text }
        : {
          type: 'image',
          source: 'local',
          bytes: await this.readPromptImage(part),
          declaredMediaType: part.mediaType,
        });
    }
    return { parts };
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
    if (this.interactionContent) return this.interactionContent;
    switch (interaction.kind) {
      case 'capability-form':
        return {kind: 'capability-form', form: {action: 'REMOVE_LOCAL_MCP', scope: 'USER', prefill: {server_id: 'fixture'}}};
      case 'plan-question':
        return { kind: 'plan-question', question: '选择发布方式', options: [], allowFreeText: true };
      case 'plan-draft':
        return { kind: 'plan-draft', body: '# 实施方案\n\n- 检查契约\n- 验证结果' };
      case 'tool-confirmation':
        return { kind: 'tool-confirmation', prompt: interaction.prompt, options: interaction.options };
    }
  }

  resolveInteraction = vi.fn(async (
    _interaction: RuntimeInteractionSummary,
    resolution: Parameters<RuntimeConnection['resolveInteraction']>[1],
  ): Promise<CommandReceipt> => {
    return {
      commandId: 'command-6', status: 'succeeded',
      ...(resolution.kind === 'plan-draft'
        ? {
          planDraftDecision: resolution.decision,
          ...(resolution.decision === 'cancel' ? {} : { planContinuationTurnId: 'turn-plan-next' }),
        }
        : {}),
    };
  });

  readToolArtifact = vi.fn(async () => {
    return {
      resultEntryId: 'result-1', text: '', offsetChars: 0, returnedChars: 0,
      totalChars: 0, hasMore: false,
    };
  });

  queryCommand = vi.fn<RuntimeConnection['queryCommand']>(async () => {
    if (this.closed) {
      throw new RuntimeApiError('CONNECTION_CLOSED', '本地连接已经关闭。', true);
    }
    return this.queryCommandResult;
  });

  async close() { this.closed = true; }
}

class FakeAdapter implements RuntimeAdapter {
  forkConversation = vi.fn(async (_sessionId: string, _entryId: string, childId: string): Promise<ForkOutcome> => {
    this.sessions = [{ ...initialSession, id: childId }, ...this.sessions];
    return { outcome: 'CREATED_AND_OPENED' as const, child_session_id: childId };
  });
  readSession = vi.fn(async (id: string) => this.sessions.find(session => session.id === id) ?? null);
  memory = new LocalMemoryApi();
  sessions = [initialSession];
  modelConfigurations = [...bootstrap.model_configurations];
  taskInventory: AgentTask[] = [];
  lastConnection?: FakeConnection;
  connectionValue?: RuntimeProjection;
  interactionContent?: RuntimeInteractionContent;
  connectionRole: 'controller' | 'observer' = 'controller';
  queryCommandResult?: CommandReceipt;
  connectionValues = new Map<string, RuntimeProjection>();
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
          input_modalities: ['text', 'image', 'audio', 'video', 'pdf'],
          output_modalities: ['text', 'future-output'],
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
  async resetPostgres() { return { database_name: 'pulsara', restart_required: false }; }
  async putDashScopeCredential() { return true; }
  async deleteDashScopeCredential() { return false; }

  async updateModelCallBinding(sessionId: string, binding: NonNullable<SessionSummary['modelCallBinding']>) {
    this.sessions = this.sessions.map((session) => session.id === sessionId
      ? { ...session, modelCallBinding: binding }
      : session);
    return { modelCallBinding: binding, reasoningPreferenceReset: false };
  }

  listSessions = vi.fn(async () => this.sessions.map((session) => ({ ...session })));

  listSessionTaskGroups = vi.fn(async () => {
    const batches = new Map<string, typeof this.taskInventory>();
    for (const task of this.taskInventory) {
      if (!task.batchId) continue;
      batches.set(task.batchId, [...(batches.get(task.batchId) ?? []), task]);
    }
    return {
      groups: [...batches].map(([id, tasks]) => ({
        id,
        parentTurnId: tasks[0]?.parentId ?? '',
        firstAcceptedAt: tasks[0]?.acceptedAt ?? '',
        taskCount: tasks.length,
        statusCounts: {
          pending: 0,
          active: tasks.filter((task) => task.status === 'running').length,
          waiting: tasks.filter((task) => task.status === 'waiting').length,
          completed: tasks.filter((task) => task.status === 'completed').length,
          cancelled: tasks.filter((task) => task.status === 'cancelled').length,
          failed: tasks.filter((task) => task.status === 'failed').length,
          interrupted: tasks.filter((task) => task.status === 'interrupted').length,
          blocked: tasks.filter((task) => task.status === 'blocked').length,
        },
        singleTaskLabel: tasks.length === 1 ? tasks[0]?.label : undefined,
      })),
      totalCount: batches.size,
      remainingCount: 0,
    };
  });

  listSessionTasks = vi.fn(async (_sessionId: string, _cursor?: string, batchId?: string) => {
    const tasks = this.taskInventory.filter((task) => !batchId || task.batchId === batchId);
    return {
      tasks: tasks.map((task) => ({ ...task })),
      totalCount: tasks.length,
      remainingCount: 0,
    };
  });

  listSessionTaskActivities = vi.fn(async () => ({ activities: [] }));

  inspectCapabilities = vi.fn(async (sessionId: string) => ({
    ...capabilitySnapshot,
    sessionId,
  }));

  reconnectMcpServer = vi.fn(async (sessionId: string) => ({
    ...capabilitySnapshot,
    sessionId,
  }));

  installSkill = vi.fn<RuntimeAdapter['installSkill']>(async (sessionId, input) => ({
    installation: {
      status: 'INSTALLED',
      installed: true,
      message: '技能已经安装。',
      sourcePath: input.sourcePath,
      destinationPath: '/tmp/pulsara_agent/.pulsara/skills/pdf',
      details: [],
    },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-provider-dispatch' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  setProjectSkillEnabled = vi.fn<RuntimeAdapter['setProjectSkillEnabled']>(async (sessionId: string) => ({
    operation: { status: 'DISABLED', success: true, message: '项目技能已关闭。', details: [] },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-provider-dispatch' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  createProjectMcp = vi.fn(async (sessionId: string) => ({
    operation: { status: 'ADDED', success: true, message: '项目 MCP 已添加。', details: [] },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-provider-dispatch' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  importProjectMcp = vi.fn(async (sessionId: string) => this.createProjectMcp(sessionId));
  testProjectMcp = vi.fn<RuntimeAdapter['testProjectMcp']>(async () => ({status: 'ready', tools: 1, resources: 0, resource_templates: 0, prompts: 0}));
  projectMcpAuthorization = vi.fn<RuntimeAdapter['projectMcpAuthorization']>(async () => ({state: 'awaiting_user', error: null}));

  removeProjectSkill = vi.fn(async (sessionId: string) => ({
    operation: {status: 'REMOVED', success: true, message: '技能已删除。', details: []},
    adoption: {scope: 'workspace' as const, pendingSessions: 1, when: 'next-provider-dispatch' as const},
    capabilities: {...capabilitySnapshot, sessionId},
  }));

  updateProjectMcp = vi.fn(async (sessionId: string) => ({
    operation: { status: 'UPDATED', success: true, message: '项目 MCP 已更新。', details: [] },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-provider-dispatch' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  setProjectMcpEnabled = vi.fn(async (sessionId: string) => ({
    operation: { status: 'DISABLED', success: true, message: '项目 MCP 已关闭。', details: [] },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-provider-dispatch' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  removeProjectMcp = vi.fn(async (sessionId: string) => ({
    operation: { status: 'REMOVED', success: true, message: '项目 MCP 已移除。', details: [] },
    adoption: { scope: 'workspace' as const, pendingSessions: 1, when: 'next-provider-dispatch' as const },
    capabilities: { ...capabilitySnapshot, sessionId },
  }));

  inspectUserCapabilities = vi.fn(async () => userCapabilitySnapshot);

  refreshUserCapabilities = vi.fn(async () => userCapabilitySnapshot);
  previewSkillImport = vi.fn<RuntimeAdapter['previewSkillImport']>(async () => []);
  previewMcpImport = vi.fn(async () => []);
  importUserMcp = vi.fn(async () => ({operation: {status: 'ADDED', success: true, message: '已导入', details: []}, capabilities: userCapabilitySnapshot}));
  testUserMcp = vi.fn(async () => ({status: 'ready' as const, tools: 1, resources: 0, resource_templates: 0, prompts: 0}));

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

  updateUserMcp = vi.fn(async () => ({
    operation: { status: 'ENABLED', success: true, message: 'MCP 服务已开启。', details: [] },
    capabilities: userCapabilitySnapshot,
  }));

  removeUserMcp = vi.fn(async () => ({
    operation: { status: 'REMOVED', success: true, message: 'MCP 服务已移除。', details: [] },
    capabilities: userCapabilitySnapshot,
  }));
  removeUserSkill = vi.fn(async () => ({
    operation: {status: 'REMOVED', success: true, message: '技能已删除。', details: []},
    capabilities: userCapabilitySnapshot,
  }));
  userMcpAuthorization = vi.fn(async () => ({ state: 'idle', error: null }));
  previewPluginImport = vi.fn(async () => ({candidates: [{source_format: 'codex' as const, manifest: '.codex-plugin/plugin.json', error: null, preview: {name: 'example', source_format: 'codex' as const, skills: [], hooks: [], mcp: [], notices: []}}]}));

  installUserPlugin = vi.fn(async () => ({
    operation: { status: 'INSTALLED', success: true, message: '插件已安装。', details: [] },
    capabilities: userCapabilitySnapshot,
  }));

  setUserPluginEnabled = vi.fn(async () => ({
    operation: { status: 'ENABLED', success: true, message: '插件已开启。', details: [] },
    capabilities: userCapabilitySnapshot,
  }));

  pluginMcpAuthorization = vi.fn(async () => ({state: 'idle', error: null}));

  updatePluginConnection = vi.fn(async () => ({ operation: { status: 'UPDATED', success: true, message: '已保存', details: [] }, capabilities: userCapabilitySnapshot }));
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
      this.connectionValues.get(sessionId) ?? (sessionId === 'session-2'
        ? { ...projection(''), messages: [], isRunning: false, activeTurnId: undefined }
        : this.connectionValue ?? projection()),
      this.connectionRole,
      this.interactionContent,
      this.connectCalls.length,
      this.queryCommandResult,
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
    const notice = await screen.findByText('本轮回复已中断。');
    expect(notice.classList.contains('conversation-interruption')).toBe(true);
    expect(notice.closest('.runtime-banner')).toBeNull();
    const workbench = screen.getByRole('region', { name: '会话工作台' });
    expect(within(workbench).getByText('已中断')).toBeTruthy();
    expect(within(workbench).queryByText('FOREGROUND_EXECUTION_INTERRUPTED')).toBeNull();
  });

  it('opens the real workbench state supplied by its adapter', async () => {
    render(<PulsaraApp adapter={new FakeAdapter()} />);
    expect(await screen.findByRole('heading', { name: '准备发布' })).toBeTruthy();
    expect(screen.getByLabelText('发送给 Pulsara')).toBeTruthy();
    expect(await screen.findByLabelText('TODO清单')).toBeTruthy();
    expect(screen.getAllByText('TODO清单')).toHaveLength(1);
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
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toBe('$pdf ');

    fireEvent.click(screen.getByRole('button', { name: '能力' }));
    expect(await screen.findByRole('heading', { name: '能力' })).toBeTruthy();
    expect(await screen.findByText('Personal Tools')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '查看 Personal Tools' }));
    expect(await screen.findByText('已启用；会话将在安全时机采用')).toBeTruthy();
    expect(screen.queryByText('已用于当前打开的会话')).toBeNull();
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
      adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-provider-dispatch' },
      capabilities: {
        ...projectSnapshot,
        adoption: { scope: 'workspace', pending: true, when: 'next-provider-dispatch' },
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

    const dialog = await screen.findByRole('dialog', { name: '导入技能' });
    expect(inspector.contains(dialog)).toBe(false);
    const application = document.querySelector<HTMLElement>('main.pulsara-shell');
    expect(application?.inert).toBe(true);
    fireEvent.keyDown(window, {key: 'Escape'});
    expect(application?.inert).toBe(false);
    fireEvent.click(within(inspector).getByRole('tab', { name: /MCP/ }));
    fireEvent.click(addButton);
    const mcpDialog = await screen.findByRole('dialog', { name: '添加 MCP 服务' });
    expect(inspector.contains(mcpDialog)).toBe(false);
    expect((within(mcpDialog).getByRole('checkbox', { name: '也向子任务提供' }) as HTMLInputElement).checked).toBe(false);

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '导入技能' })).toBeNull();
    expect(screen.queryByRole('dialog', { name: '添加 MCP 服务' })).toBeNull();
    expect(application?.inert).toBe(false);
    await waitFor(() => expect(document.activeElement).toBe(addButton));
  });

  it('keeps the project capability dialog open while its mutation is saving', async () => {
    const adapter = new FakeAdapter();
    adapter.previewSkillImport.mockResolvedValue([{sourcePath: '/tmp/market-skill', name: 'market-skill', description: 'A portable skill', valid: true, details: []}]);
    let finishInstallation!: (value: Awaited<ReturnType<FakeAdapter['installSkill']>>) => void;
    adapter.installSkill.mockImplementation(() => new Promise((resolve) => {
      finishInstallation = resolve;
    }));

    render(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('当前会话详情');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    const addButton = await within(inspector).findByRole('button', { name: '添加' });
    fireEvent.click(addButton);
    const dialog = await screen.findByRole('dialog', { name: '导入技能' });
    fireEvent.change(within(dialog).getByLabelText('来源目录'), {
      target: { value: '/tmp/market-skill' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: '读取预览' }));
    await within(dialog).findByText('/tmp/market-skill');
    fireEvent.click(within(dialog).getByRole('button', { name: '安装所选技能' }));

    await waitFor(() => expect(dialog.getAttribute('aria-busy')).toBe('true'));
    expect((addButton as HTMLButtonElement).disabled).toBe(true);
    for (const closeButton of within(dialog).getAllByRole('button', { name: '关闭' })) {
      expect((closeButton as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(closeButton);
    }
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: '导入技能' })).toBeTruthy();
    expect(adapter.installSkill).toHaveBeenCalledWith('session-1', {sourcePath: '/tmp/market-skill', name: 'market-skill', description: 'A portable skill', valid: true, details: []});

    await act(async () => finishInstallation({
      installation: {
        status: 'INSTALLED',
        installed: true,
        message: '技能已经安装。',
        sourcePath: '/tmp/market-skill',
        destinationPath: '/tmp/pulsara_agent/.pulsara/skills/market-skill',
        details: [],
      },
      adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-provider-dispatch' },
      capabilities: capabilitySnapshot,
    }));
    expect(await within(dialog).findByText('已安装')).toBeTruthy();
    fireEvent.keyDown(window, {key: 'Escape'});
    expect(screen.queryByRole('dialog', { name: '导入技能' })).toBeNull();
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
      adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-provider-dispatch' },
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
        adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-provider-dispatch' },
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
        id: 'user-prompt', turnId: 'turn-one', role: 'user', userKind: 'prompt', time: '18:10',
        body: '检查完整流程。', status: 'completed',
      }, {
        id: 'assistant-tool', turnId: 'turn-one', role: 'assistant', time: '18:11', body: '', status: 'completed',
        reasoning: [{ id: 'reasoning-before-tool', kind: 'full', body: '先读取文件。' }],
        traces: [{
          id: 'trace-read', kind: 'read', toolName: 'read_file', title: '读取文件', subtitle: '已完成',
          status: 'completed', meta: '操作完成',
        }],
      }, {
        id: 'user-steer', turnId: 'turn-one', role: 'user', userKind: 'steer', time: '18:12',
        body: '也检查真实页面。', status: 'waiting',
      }, {
        id: 'completion-one', turnId: 'turn-one', role: 'user', userKind: 'subagent-completion', time: '18:12',
        body: '', status: 'completed', sourceSubagentTaskId: 'task-one',
        sourceSubagentLabel: 'reader', sourceSubagentRelation: 'current',
      }, {
        id: 'assistant-tool-after-steer', turnId: 'turn-one', role: 'assistant', time: '18:12', body: '', status: 'completed',
        reasoning: [{ id: 'reasoning-after-steer', kind: 'full', body: '继续检查真实页面。' }],
      }, {
        id: 'assistant-final', turnId: 'turn-one', role: 'assistant', time: '18:13', body: '检查已经完成。', status: 'completed',
        reasoning: [{ id: 'reasoning-after-tool', kind: 'summary', body: '整理检查结果。' }],
      }, {
        id: 'user-prompt-two', turnId: 'turn-two', role: 'user', userKind: 'prompt', time: '18:14',
        body: '继续下一项。', status: 'completed',
      }, {
        id: 'assistant-final-two', turnId: 'turn-two', role: 'assistant', time: '18:15',
        body: '第二项也已经完成。', status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByText('检查已经完成。')).toBeTruthy();

    expect(container.querySelectorAll('.user-heading')).toHaveLength(2);
    for (const userHeading of container.querySelectorAll('.user-heading')) {
      expect(userHeading.textContent).toContain('你');
      expect(userHeading.firstElementChild?.tagName).toBe('STRONG');
      expect(userHeading.lastElementChild?.classList.contains('user-avatar')).toBe(true);
    }
    expect(container.querySelectorAll('.user-steer')).toHaveLength(1);
    expect(screen.getByText('引导')).toBeTruthy();
    expect(screen.queryByText('你 · 引导')).toBeNull();
    expect(screen.getByLabelText('reader 的结果已加入本轮对话')).toBeTruthy();
    expect(container.querySelectorAll('.assistant-heading')).toHaveLength(2);
    expect(container.querySelectorAll('.assistant-heading--run-start')).toHaveLength(1);
    expect(container.querySelectorAll('.assistant-heading--response')).toHaveLength(1);
    expect(container.querySelectorAll('.assistant-turn--operational .assistant-heading')).toHaveLength(1);
    expect(screen.getByText('read_file')).toBeTruthy();
    expect(screen.getByText('读取文件')).toBeTruthy();

    const finalTurn = container.querySelector('.assistant-turn--response-with-reasoning');
    expect(finalTurn?.firstElementChild?.classList.contains('reasoning-disclosure')).toBe(true);
    expect(finalTurn?.children[1]?.classList.contains('assistant-copy')).toBe(true);
  });

  it('shows a readable builtin name and keeps the command detail expandable', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-terminal', role: 'assistant', time: '18:11', body: '', status: 'completed',
        traces: [{
          id: 'trace-terminal', kind: 'terminal', toolName: 'terminal', title: '运行命令',
          subtitle: '已完成', status: 'completed', command: 'printf "visible command"',
          resultText: 'visible command', meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开中间过程' }));
    expect(screen.queryByText('terminal')).toBeNull();
    expect(screen.getByText('运行命令')).toBeTruthy();
    expect(container.querySelector('.terminal-command')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /terminal/ }));

    expect(container.querySelector('.terminal-command')?.textContent).toContain('printf "visible command"');
  });

  it('shows the real edit diff in a bounded output region without a redundant diff-copy button', async () => {
    const adapter = new FakeAdapter();
    const diff = ['@@ -1,40 +1,40 @@', '-old-001', '+new-001', ...Array.from(
      { length: 80 }, (_, index) => ` context-${String(index).padStart(3, '0')}`,
    )].join('\n');
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-edit-diff', role: 'assistant', time: '18:11', body: '', status: 'completed',
        traces: [{
          id: 'trace-edit-diff', kind: 'edit', toolName: 'edit_file', title: '更新文件',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          resultSummary: '操作已完成。', resultText: JSON.stringify({ status: 'success', diff }),
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开中间过程' }));
    fireEvent.click(await screen.findByRole('button', { name: /展开工具详情：edit_file/ }));

    const diffOutput = screen.getByLabelText('文件差异');
    expect(diffOutput.textContent).toBe(diff);
    expect(diffOutput.classList.contains('tool-result-diff')).toBe(true);
    expect(screen.queryByText('操作已完成。')).toBeNull();
    expect(screen.queryByRole('button', { name: '复制工具差异' })).toBeNull();
    expect(screen.queryByRole('button', { name: '复制工具原始结果' })).toBeNull();
    expect(container.querySelector('.tool-result-raw')).toBeNull();
  });

  it('does not render diff-shaped MCP data as a builtin file difference', async () => {
    const adapter = new FakeAdapter();
    const resultText = JSON.stringify({
      status: 'success', path: 'remote.txt', bytes_written: 42,
      diff: '@@ external report, not edit_file @@',
    });
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-mcp-diff', role: 'assistant', time: '18:11', body: '', status: 'completed',
        traces: [{
          id: 'trace-mcp-diff', kind: 'mcp', toolName: 'mcp__external__report', title: '使用工具',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          resultText, resultSummary: '@@ external report, not edit_file @@',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开中间过程' }));
    fireEvent.click(await screen.findByRole('button', { name: /展开工具详情：mcp__external__report/ }));

    expect(screen.queryByLabelText('文件差异')).toBeNull();
    expect(screen.getByLabelText('工具原始结果').textContent).toContain(resultText);
  });

  it('collapses retained artifact content independently from the tool card', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-artifact', role: 'assistant', time: '18:03', body: '', status: 'completed',
        traces: [{
          id: 'trace-artifact', kind: 'mcp', toolName: 'mcp__firecrawl__firecrawl_search', title: '搜索内容',
          subtitle: '已完成', status: 'completed', resultEntryId: 'result-1', resultText: '{"content":[]}',
          artifact: { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'COMPLETE' },
          meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开中间过程' }));
    const expand = await screen.findByRole('button', { name: /展开工具详情/ });
    fireEvent.click(expand);

    const collapse = screen.getByRole('button', { name: /收起工具详情/ });
    expect(collapse.textContent).not.toContain('收起详情');
    const artifactTrigger = screen.getByRole('button', { name: '查看完整输出' });
    expect(artifactTrigger.classList.contains('tool-artifact__trigger')).toBe(true);
    fireEvent.click(artifactTrigger);

    const end = await screen.findByRole('status', { name: '完整输出读取状态' });
    expect(end.textContent).toContain('已到末页');
    expect(screen.getByLabelText('完整工具输出').classList.contains('is-open')).toBe(true);
    expect(screen.getByRole('button', { name: '复制当前工具输出页' }).getAttribute('title')).toBe('复制当前页');

    fireEvent.click(screen.getByRole('button', { name: '收起完整输出' }));
    expect(screen.queryByRole('button', { name: '复制当前工具输出页' })).toBeNull();
    expect(screen.getByRole('button', { name: '查看完整输出' })).toBeTruthy();
    expect(screen.getByLabelText('工具原始结果')).toBeTruthy();

    fireEvent.click(collapse);
    expect(screen.queryByLabelText('工具原始结果')).toBeNull();
  });

  it('invalidates an in-flight artifact page when the whole tool card is collapsed', async () => {
    const adapter = new FakeAdapter();
    const page = deferred<Awaited<ReturnType<RuntimeConnection['readToolArtifact']>>>();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-artifact-race', role: 'assistant', time: '18:03', body: '', status: 'completed',
        traces: [{
          id: 'trace-artifact-race', kind: 'mcp', toolName: 'mcp__firecrawl__firecrawl_search',
          status: 'completed',
          title: '搜索内容', subtitle: '已完成', resultEntryId: 'result-race', resultText: '{}',
          artifact: { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'COMPLETE' },
          meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开中间过程' }));
    const expand = await screen.findByRole('button', { name: /展开工具详情/ });
    const active = adapter.lastConnection!;
    active.readToolArtifact.mockImplementationOnce(() => page.promise);
    fireEvent.click(expand);
    fireEvent.click(screen.getByRole('button', { name: '查看完整输出' }));
    await waitFor(() => expect(active.readToolArtifact).toHaveBeenCalledWith('result-race', 0));
    fireEvent.click(screen.getByRole('button', { name: /收起工具详情/ }));

    await act(async () => page.resolve({
      resultEntryId: 'result-race', text: '不应落回的迟到页', offsetChars: 0,
      returnedChars: 9, totalChars: 9, hasMore: false,
    }));
    fireEvent.click(screen.getByRole('button', { name: /展开工具详情/ }));

    expect(screen.queryByText('不应落回的迟到页')).toBeNull();
    expect(screen.getByRole('button', { name: '查看完整输出' })).toBeTruthy();
  });

  it('does not apply an old-session artifact page to a reused trace identity', async () => {
    const adapter = new FakeAdapter();
    const oldPage = deferred<Awaited<ReturnType<RuntimeConnection['readToolArtifact']>>>();
    const secondSession: SessionSummary = {
      ...initialSession,
      id: 'session-2',
      title: '另一个会话',
      live: false,
    };
    adapter.sessions = [initialSession, secondSession];
    const artifactProjection = (resultText: string): RuntimeProjection => ({
      ...projection(''),
      messages: [{
        id: 'assistant-shared', role: 'assistant', time: '18:03', body: '', status: 'completed',
        traces: [{
          id: 'trace-shared', kind: 'mcp', toolName: 'mcp__firecrawl__firecrawl_search',
          status: 'completed',
          title: '搜索内容', subtitle: '已完成', resultEntryId: 'result-shared', resultText,
          artifact: { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'COMPLETE' },
          meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    });
    adapter.connectionValues.set('session-1', artifactProjection('{"session":"A"}'));
    adapter.connectionValues.set('session-2', artifactProjection('{"session":"B"}'));

    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开中间过程' }));
    const expand = await screen.findByRole('button', { name: /展开工具详情/ });
    const first = adapter.lastConnection!;
    first.readToolArtifact.mockImplementationOnce(() => oldPage.promise);
    fireEvent.click(expand);
    fireEvent.click(screen.getByRole('button', { name: '查看完整输出' }));
    await waitFor(() => expect(first.readToolArtifact).toHaveBeenCalledWith('result-shared', 0));

    fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
    await screen.findByRole('heading', { name: '另一个会话' });
    const processToggle = screen.queryByRole('button', { name: '展开中间过程' });
    if (processToggle) fireEvent.click(processToggle);
    await act(async () => oldPage.resolve({
      resultEntryId: 'result-shared', text: '会话 A 的迟到 artifact', offsetChars: 0,
      returnedChars: 17, totalChars: 17, hasMore: false,
    }));
    const summary = screen.getByRole('button', { name: /工具详情：mcp__firecrawl__firecrawl_search/ });
    if (summary.getAttribute('aria-expanded') !== 'true') fireEvent.click(summary);

    expect(screen.queryByText('会话 A 的迟到 artifact')).toBeNull();
    expect(screen.getByRole('button', { name: '查看完整输出' })).toBeTruthy();
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
          argumentsJson: '{}', resultText: '{"status":"RELOADED"}',
        }, {
          id: 'trace-skill', kind: 'read', toolName: 'read_file', title: '读取文件',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          argumentsJson: JSON.stringify({ path: '/opt/pulsara/bundled_skills/pdf/SKILL.md' }),
          resultText: JSON.stringify({ path: '/opt/pulsara/bundled_skills/pdf/SKILL.md', total_lines: 42 }),
        }, {
          id: 'trace-list', kind: 'mcp', toolName: 'list_mcp_servers', title: '浏览 MCP 服务',
          subtitle: '已完成', status: 'completed', meta: '操作完成', argumentsJson: '{}',
          resultText: JSON.stringify({
            page_kind: 'SERVER_PAGE', total_server_count: 2,
            servers: [{ server_id: 'firecrawl', public_status: 'READY', tool_count: 3 }, {
              server_id: 'local-docs', public_status: 'CONNECTING', tool_count: 1,
            }],
          }),
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
        }, {
          id: 'trace-use', kind: 'mcp', toolName: 'use_new_mcp_tool', title: '调用 MCP 工具',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          argumentsJson: JSON.stringify({
            tool_ref: 'mcpref_firecrawl_search', arguments: { query: 'Pulsara' },
          }),
          resultText: JSON.stringify({ content: '搜索完成' }),
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开中间过程' }));
    expect(screen.getByText('刷新扩展能力')).toBeTruthy();
    expect(screen.queryByText('reload_capabilities')).toBeNull();
    expect(await screen.findByText('使用 pdf 技能')).toBeTruthy();
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

  it('copies the exact source Markdown instead of rendered or normalized text', async () => {
    const writeText = vi.fn(async () => undefined);
    stubClipboard(writeText);
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-source', role: 'assistant', assistantKind: 'terminal',
        time: '18:12', body: SOURCE_FIDELITY_MARKDOWN, status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '复制回复' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledExactlyOnceWith(SOURCE_FIDELITY_MARKDOWN));
    expect(await screen.findByText('已复制回复')).toBeTruthy();
  });

  it('copies one formula as LaTeX and reports through the shared toast stack', async () => {
    const writeText = vi.fn(async () => undefined);
    stubClipboard(writeText);
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-formula', role: 'assistant', assistantKind: 'terminal',
        time: '18:12', body: '能量关系为 $E=mc^2$。', status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '复制 LaTeX 公式' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledExactlyOnceWith('E=mc^2'));
    expect(await screen.findByText('公式已复制')).toBeTruthy();
  });

  it('renders accepted user prompts and steers with source line breaks and safe long-token wrapping', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'user-source', role: 'user', userKind: 'prompt',
        time: '18:10', body: SOURCE_FIDELITY_USER_TEXT, status: 'completed',
      }, {
        id: 'user-steer-source', role: 'user', userKind: 'steer',
        time: '18:11', body: SOURCE_FIDELITY_USER_TEXT, status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const bodies = [
      container.querySelector<HTMLElement>('.user-message > p'),
      container.querySelector<HTMLElement>('.user-steer__content > p'),
    ];

    for (const body of bodies) {
      expect(body).toBeTruthy();
      expect(body?.textContent).toBe(SOURCE_FIDELITY_USER_TEXT);
      expect(body?.style.whiteSpace).toBe('pre-wrap');
      expect(body?.style.overflowWrap).toBe('anywhere');
      expect(body?.querySelector('script')).toBeNull();
    }
  });

  it('keeps the existing copy failure feedback when clipboard permission is denied', async () => {
    const writeText = vi.fn(async () => { throw new Error('denied'); });
    stubClipboard(writeText);
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-source', role: 'assistant', assistantKind: 'terminal',
        time: '18:12', body: SOURCE_FIDELITY_MARKDOWN, status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '复制回复' }));

    expect(await screen.findByText('无法复制回复')).toBeTruthy();
    expect(screen.getByText('浏览器没有授予剪贴板权限')).toBeTruthy();
  });

  it('forks only server-eligible entries with one preselected identity and leaves the parent running', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(''), isRunning: true, messages: [
      { id: 'old-intermediate', role: 'assistant', assistantKind: 'terminal', body: '中间正文', time: '18:10', status: 'completed', forkEligible: false },
      { id: 'canonical-anchor', role: 'assistant', assistantKind: 'terminal', body: '已结算最终回复', time: '18:12', status: 'completed', forkEligible: true },
    ] };
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    adapter.forkConversation.mockImplementationOnce(async (_source, _anchor, child) => {
      await gate;
      adapter.sessions = [{ ...initialSession, id: child }, ...adapter.sessions];
      return { outcome: 'CREATED_AND_OPENED', child_session_id: child };
    });
    render(<PulsaraApp adapter={adapter} />);
    const button = await screen.findByRole('button', { name: '从此处分叉' });
    expect(screen.getAllByRole('button', { name: '从此处分叉' })).toHaveLength(1);
    fireEvent.click(button); fireEvent.click(button);
    expect(button.getAttribute('aria-busy')).toBe('true');
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(adapter.forkConversation).toHaveBeenCalledExactlyOnceWith(initialSession.id, 'canonical-anchor', expect.stringMatching(/^session:[0-9a-f]{32}$/));
    release();
    await screen.findByText('分叉已打开');
    expect(adapter.connectCalls.at(-1)?.sessionId).toBe(adapter.forkConversation.mock.calls[0][2]);
  });

  it('queries a lost Fork response without replaying the creation and keeps the parent selected', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(''), initialContextBase: { base_kind: 'SNAPSHOT', display_after_entry_sequence: 0 }, messages: [
      { id: 'imported-anchor', role: 'assistant', assistantKind: 'terminal', body: '继承的回复', time: '18:12', status: 'completed', forkEligible: true, entryOwnerKind: 'IMPORTED_HISTORY' },
    ] };
    adapter.forkConversation.mockImplementationOnce(async (_source, _anchor, child) => {
      adapter.sessions = [{ ...initialSession, id: child }, ...adapter.sessions];
      throw new Error('response lost');
    });
    render(<PulsaraApp adapter={adapter} />);
    const button = await screen.findByRole('button', { name: '从此处分叉' });
    expect(screen.getByRole('separator', { name: '已保留分叉点的有效上下文，压缩前记录请在原会话查看' })).toBeTruthy();
    fireEvent.click(button);
    await screen.findByText('分叉已创建，暂未打开');
    expect(adapter.forkConversation).toHaveBeenCalledTimes(1);
    expect(adapter.readSession).toHaveBeenCalledWith(adapter.forkConversation.mock.calls[0][2]);
    expect(adapter.connectCalls.at(-1)?.sessionId).toBe(initialSession.id);
  });

  it('starts a Fork with empty draft and planning off while retaining the source draft', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(''), isRunning: false, messages: [
      { id: 'anchor', role: 'assistant', assistantKind: 'terminal', body: '可分叉回复', time: '18:12', status: 'completed', forkEligible: true },
    ] };
    render(<PulsaraApp adapter={adapter} />);
    const textbox = await screen.findByRole('textbox', { name: '发送给 Pulsara' });
    await userEvent.click(textbox);
    await userEvent.type(textbox, '父会话未发送草稿', { skipClick: true });
    fireEvent.click(screen.getByRole('button', { name: '先规划' }));
    fireEvent.click(screen.getByRole('button', { name: '从此处分叉' }));
    await screen.findByText('分叉已打开');
    expect(screen.getByRole('textbox', { name: '发送给 Pulsara' }).textContent).toBe('');
    expect(screen.getByRole('button', { name: '先规划' }).getAttribute('aria-pressed')).toBe('false');
    const source = screen.getAllByRole('button').find(button => button.textContent?.includes(initialSession.title) && button.textContent?.includes('已载入'));
    expect(source).toBeTruthy();
    fireEvent.click(source!);
    await waitFor(() => expect(adapter.connectCalls.at(-1)?.sessionId).toBe(initialSession.id));
    expect(screen.getByRole('textbox', { name: '发送给 Pulsara' }).textContent)
      .toBe('父会话未发送草稿');
    expect(screen.getByRole('button', { name: '本轮先规划' }).getAttribute('aria-pressed')).toBe('true');
  });

  it.each(['CREATED_OPEN_DEFERRED', 'NOT_CREATED'] as const)('keeps the parent selected for %s', async (outcome) => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(''), messages: [
      { id: 'anchor', role: 'assistant', assistantKind: 'terminal', body: '保留父会话', time: '18:12', status: 'completed', forkEligible: true },
    ] };
    adapter.forkConversation.mockResolvedValueOnce({ outcome, child_session_id: 'child' });
    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '从此处分叉' }));
    await screen.findByText(outcome === 'NOT_CREATED' ? '未创建分叉' : '分叉已创建，暂未打开');
    expect(adapter.connectCalls.at(-1)?.sessionId).toBe(initialSession.id);
    expect(adapter.forkConversation).toHaveBeenCalledTimes(1);
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

  it('renders an accepted previous-root subtask completion without implying provider use', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      canonicalRootTurnIds: ['turn-source', 'turn-target'],
      agentTasks: [{ id: 'task-internal', parentId: 'turn-source', label: 'reader', role: '研究',
        objective: '读取结果', dependencyIds: [], status: 'completed', color: 'blue', completionAccepted: true }],
      messages: [{
        id: 'accepted-result', turnId: 'turn-target', role: 'user', userKind: 'subagent-completion', time: '18:14',
        body: '{"status":"accepted","task_id":"internal"}',
        sourceSubagentTaskId: 'task-internal',
        sourceSubagentLabel: 'reader',
        sourceSubagentRelation: 'previous',
        status: 'completed',
      }, {
        id: 'assistant-after-result', role: 'assistant', time: '18:15',
        body: '我会基于子任务结果继续。', status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = render(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByLabelText('上一轮 reader 的结果已加入本轮对话')).toBeTruthy();
    expect(screen.getByText('上一轮 reader 的结果已加入本轮对话')).toBeTruthy();
    expect(screen.getByRole('tooltip').textContent).toContain('已记录到当前对话');
    expect(screen.queryByText(/用于当前处理|会结合这项工作的结果/)).toBeNull();
    expect(screen.queryByText(/"status":"accepted"/)).toBeNull();
    expect(container.querySelectorAll('.user-heading')).toHaveLength(0);
    expect(container.querySelectorAll('.assistant-turn--run-start')).toHaveLength(1);
  });

  it('defaults builtin raw results off and remembers the settings switch across reloads', async () => {
    const saved = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => saved.get(key) ?? null,
      setItem: (key: string, value: string) => saved.set(key, value),
    });
    try {
      let view = render(<PulsaraApp adapter={new FakeAdapter()} />);
      await screen.findByRole('heading', { name: '准备发布' });
      fireEvent.click(screen.getByRole('button', { name: '设置' }));
      fireEvent.click(screen.getByRole('button', { name: '通用' }));
      const toggle = screen.getByRole('switch', { name: '显示内置工具原始结果' });
      expect(toggle.getAttribute('aria-checked')).toBe('false');
      fireEvent.click(toggle);
      expect(toggle.getAttribute('aria-checked')).toBe('true');
      expect(saved.get('pulsara-show-builtin-tool-results')).toBe('true');
      view.unmount();

      view = render(<PulsaraApp adapter={new FakeAdapter()} />);
      await screen.findByRole('heading', { name: '准备发布' });
      fireEvent.click(screen.getByRole('button', { name: '设置' }));
      fireEvent.click(screen.getByRole('button', { name: '通用' }));
      const restored = screen.getByRole('switch', { name: '显示内置工具原始结果' });
      expect(restored.getAttribute('aria-checked')).toBe('true');
      fireEvent.click(restored);
      expect(saved.get('pulsara-show-builtin-tool-results')).toBe('false');
      view.unmount();
    } finally {
      cleanup();
      vi.unstubAllGlobals();
    }
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

    const inspector = screen.getByRole('complementary', { name: '当前会话详情' });
    fireEvent.click(within(inspector).getByRole('button', { name: '后台终端' }));
    expect(within(inspector).getByText('创建或选择会话后查看后台终端')).toBeTruthy();
    expect(within(inspector).queryByText('后台终端不可用')).toBeNull();
    expect(adapter.connectCalls).toHaveLength(0);

    fireEvent.click(screen.getByRole('button', { name: '创建会话' }));
    expect(screen.getByRole('heading', { name: '新建会话' })).toBeTruthy();
  });

  it.each([false, true])('disables all creation entries after a bootstrap failure (retryable=%s)', async (retryable) => {
    const adapter = new FakeAdapter();
    vi.spyOn(adapter, 'bootstrap').mockRejectedValue(new RuntimeApiError('START_FAILED', '本地服务启动失败', retryable));
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByText('本地服务启动失败');
    expect((screen.getByRole('button', { name: /新建会话/ }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '创建会话' }) as HTMLButtonElement).disabled).toBe(true);
    for (const modifier of ['ctrlKey', 'metaKey']) {
      fireEvent.keyDown(window, { key: 'n', [modifier]: true });
      expect(screen.queryByRole('dialog', { name: '新建会话' })).toBeNull();
    }
    fireEvent.keyDown(window, { key: 'k', metaKey: true });
    const palette = screen.getByRole('dialog', { name: '命令面板' });
    expect((within(palette).getByRole('button', { name: /新建会话/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: '总览' }));
    expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '记忆' }));
    expect(screen.getByRole('heading', { name: retryable ? '本地服务连接已中断' : '本地服务连接失败' })).toBeTruthy();
    expect(adapter.createSession).not.toHaveBeenCalled();
  });

  it('keeps creation disabled until bootstrap and the initial session list are ready', async () => {
    const adapter = new FakeAdapter();
    let finishBootstrap!: (value: RuntimeBootstrap) => void;
    let finishList!: (value: SessionSummary[]) => void;
    vi.spyOn(adapter, 'bootstrap').mockImplementation(() => new Promise(resolve => { finishBootstrap = resolve; }));
    vi.spyOn(adapter, 'listSessions').mockImplementation(() => new Promise(resolve => { finishList = resolve; }));
    render(<PulsaraApp adapter={adapter} />);
    expect((screen.getByRole('button', { name: '创建会话' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => finishBootstrap(bootstrap));
    expect((screen.getByRole('button', { name: '创建会话' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => finishList([]));
    expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.keyDown(window, { key: 'n', metaKey: true });
    expect(screen.getByRole('dialog', { name: '新建会话' })).toBeTruthy();
  });

  it('blocks an already open creation dialog while reconnecting, then enables it after recovery', async () => {
    const adapter = new FakeAdapter();
    const connect = adapter.connect.bind(adapter);
    let disconnect!: (error: Error) => void;
    let recover!: () => void;
    vi.spyOn(adapter, 'connect')
      .mockImplementationOnce(async (...args) => {
        const connection = await connect(...args);
        vi.spyOn(connection, 'observe').mockImplementation(() => new Promise((_resolve, reject) => { disconnect = reject; }));
        return connection;
      })
      .mockImplementationOnce(async (...args) => {
        await new Promise<void>(resolve => { recover = resolve; });
        return connect(...args);
      });
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    const dialog = screen.getByRole('dialog', { name: '新建会话' });
    await act(async () => disconnect(new Error('connection lost')));
    expect((within(dialog).getByRole('button', { name: /^创建会话/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.submit(dialog.querySelector('form')!);
    expect(adapter.createSession).not.toHaveBeenCalled();
    await waitFor(() => expect(recover).toBeTypeOf('function'));
    await act(async () => recover());
    expect((within(dialog).getByRole('button', { name: /^创建会话/ }) as HTMLButtonElement).disabled).toBe(false);
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

    fireEvent.keyDown(window, { key: 'n', metaKey: true });
    expect(screen.queryByRole('dialog', { name: '新建会话' })).toBeNull();
    fireEvent.keyDown(window, { key: 'k', metaKey: true });
    expect((within(screen.getByRole('dialog', { name: '命令面板' })).getByRole('button', { name: /新建会话/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(window, { key: 'Escape' });

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
    expect(screen.getByText('文字、图片、音频、视频、PDF')).toBeTruthy();
    expect(screen.getByText('文字、future-output')).toBeTruthy();
    expect(screen.getByText(/Pulsara 当前支持文字、图片输入和文字回复/)).toBeTruthy();
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

    const imageInput = screen.getByLabelText('支持图像输入') as HTMLInputElement;
    expect(imageInput.checked).toBe(false);
    fireEvent.click(imageInput);
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }));
    await waitFor(() => expect(test).toHaveBeenCalledWith(expect.objectContaining({
      source: 'user_declared',
      configuration_name: 'Local Gateway',
      input_modalities: ['text', 'image'],
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
      input_modalities: ['text', 'image'],
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
      input_modalities: ['text'],
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
            input_modalities: null,
            output_modalities: null,
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

  it('collapses the inspector when leaving desktop width without forcing it open again', async () => {
    const media = new EventTarget();
    let matches = true;
    const removeListener = vi.spyOn(media, 'removeEventListener');
    vi.stubGlobal('matchMedia', vi.fn(() => Object.assign(media, { matches })));
    const view = render(<PulsaraApp adapter={new FakeAdapter()} />);
    try {
      await screen.findByRole('heading', { name: '准备发布' });
      const inspector = screen.getByRole('complementary', { name: '当前会话详情' });
      expect(inspector.classList.contains('is-open')).toBe(true);
      matches = false;
      act(() => media.dispatchEvent(Object.assign(new Event('change'), { matches })));
      expect(inspector.classList.contains('is-open')).toBe(false);
      fireEvent.click(screen.getByRole('button', { name: '切换检查器' }));
      expect(inspector.classList.contains('is-open')).toBe(true);
      fireEvent.click(screen.getByRole('button', { name: '收起详情侧栏' }));
      expect(inspector.classList.contains('is-open')).toBe(false);
      matches = true;
      act(() => media.dispatchEvent(Object.assign(new Event('change'), { matches })));
      expect(inspector.classList.contains('is-open')).toBe(false);
    } finally {
      view.unmount();
      vi.unstubAllGlobals();
    }
    expect(removeListener).toHaveBeenCalledWith('change', expect.any(Function));
  });

  it('uses the same reasoning control inside composer options and dismisses the panel without resetting it', async () => {
    const adapter = new FakeAdapter();
    const update = vi.spyOn(adapter, 'updateModelCallBinding');
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const trigger = screen.getByRole('button', { name: '本轮选项' });
    fireEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: /推理 medium/ }));
    fireEvent.click(screen.getByRole('button', { name: 'high' }));
    expect(await screen.findByRole('button', { name: /推理 high/ })).toBeTruthy();
    expect(update).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(trigger);
    expect(screen.getAllByRole('button', { name: /推理 high/ })).toHaveLength(1);
    fireEvent.pointerDown(screen.getByRole('heading', { name: '准备发布' }));
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    const label = document.querySelector('.model-chip__label');
    expect(label?.closest('button')?.title).toBe(label?.textContent);
    expect(update).toHaveBeenCalledTimes(1);
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

  it('uses shared session presence and shows useful local configuration facts', async () => {
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
    expect(screen.queryByText('正在发生')).toBeNull();
    expect(screen.queryByText('本地运行')).toBeNull();
    expect(container.querySelector('.active-mission-card')).toBeNull();
    expect(container.querySelector('.activity-card')).toBeNull();
    expect(container.querySelectorAll('.recent-table .session-presence--current')).toHaveLength(1);
    expect(container.querySelectorAll('.recent-table .session-presence--loaded')).toHaveLength(1);
    expect(container.querySelectorAll('.recent-table .session-presence--resumable')).toHaveLength(1);
    expect(Array.from(container.querySelectorAll('.recent-table .table-state'), (node) => node.textContent))
      .toEqual(['当前会话', '已载入', '可恢复']);
    const systemCard = container.querySelector('.system-card');
    expect(systemCard?.textContent).toContain('当前配置');
    expect(systemCard?.textContent).toContain('模型配置1 组可用1 组');
    expect(systemCard?.textContent).toContain('记忆检索DashScope Embedding未配置');
    expect(systemCard?.textContent).toContain('结果重排DashScope Rerank未配置');
    expect(systemCard?.querySelector('footer')).toBeNull();
  });

  it('reports configured retrieval services and the exact model configuration count', async () => {
    const adapter = new FakeAdapter();
    adapter.modelConfigurations = [
      ...adapter.modelConfigurations,
      {
        ...adapter.modelConfigurations[0],
        id: 'model-connection:11111111111111111111111111111111',
        route_id: 'backup',
        model_id: 'backup-model',
        display_name: 'backup-model',
      },
    ];
    adapter.bootstrap = vi.fn(async () => ({
      ...bootstrap,
      local_settings: {
        ...bootstrap.local_settings,
        dashscope_credentials: { embedding_configured: true, rerank_configured: true },
      },
      model_configurations: adapter.modelConfigurations,
    }));

    const { container } = render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '总览' }));

    const systemCard = container.querySelector('.system-card');
    expect(systemCard?.textContent).toContain('模型配置2 组可用2 组');
    expect(systemCard?.textContent).toContain('记忆检索DashScope Embedding已配置');
    expect(systemCard?.textContent).toContain('结果重排DashScope Rerank已配置');
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
    expect(await screen.findByRole('heading', { name: isWelcomeHeading })).toBeTruthy();
  });

  it('binds plan and permission choices to the next composer submission', async () => {
    const adapter = new FakeAdapter();
    const { container } = render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
    await screen.findByRole('heading', { name: isWelcomeHeading });

    fireEvent.click(screen.getByRole('button', { name: '输入选项' }));
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
    await typeComposer('验证新的前端任务');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('验证新的前端任务')).toBeTruthy();
    expect(adapter.lastConnection?.enterPlan).toHaveBeenCalledWith('验证新的前端任务', 'read-only');
    expect(adapter.lastConnection?.submitPrompt).toHaveBeenCalledWith(
      expect.stringMatching(/^command:web:/), textPrompt('验证新的前端任务'), 'read-only',
    );
    expect(screen.getByRole('button', { name: /完全访问/ })).toBeTruthy();
  });

  it('leaves Enter to the input method while the composer is composing text', async () => {
    const adapter = new FakeAdapter();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const composer = screen.getByLabelText('发送给 Pulsara');

    await userEvent.click(composer);
    fireEvent.compositionStart(composer);
    await userEvent.type(composer, 'biruzhey', { skipClick: true });
    expect(fireEvent.keyDown(
      composer,
      { key: 'Enter', code: 'Enter', isComposing: true },
    )).toBe(true);

    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
    expect(composer.textContent).toBe('biruzhey');

    fireEvent.compositionEnd(composer);
    expect(fireEvent.keyDown(
      composer,
      { key: 'Enter', code: 'Enter', keyCode: 229 },
    )).toBe(true);
    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
    expect(composer.textContent).toBe('biruzhey');

    expect(fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' })).toBe(false);
    await waitFor(() => expect(adapter.lastConnection?.submitPrompt).toHaveBeenCalledWith(
      expect.stringMatching(/^command:web:/), textPrompt('biruzhey'),
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
    adapter.interactionContent = { kind: 'plan-draft', body: SOURCE_FIDELITY_MARKDOWN };
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

    expect(await screen.findByText(/保留/)).toBeTruthy();
    expect(screen.getByText('read_file')).toBeTruthy();
    expect(screen.getByText(/ROOT = "read-only"/)).toBeTruthy();
    expect(screen.queryByLabelText('TODO清单')).toBeNull();
    expect(screen.getByRole('button', { name: '展开TODO清单' }).hasAttribute('disabled')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /批准并继续/ }));

    await waitFor(() => expect(adapter.lastConnection?.resolveInteraction).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'plan-review-1', kind: 'plan-draft' }),
      { kind: 'plan-draft', decision: 'approve' },
    ));
    expect(await screen.findByText('已创建按批准方案继续处理的任务。')).toBeTruthy();
  });

  it.each([
    ['cancel', '取消规划', { kind: 'plan-draft', decision: 'cancel' }],
    ['revise', '提交修改意见', {
      kind: 'plan-draft', decision: 'revise', feedback: '保留 `read_file` 与 ROOT',
    }],
  ] as const)('keeps source plan text and interaction identity when choosing %s', async (
    _name,
    action,
    resolution,
  ) => {
    const adapter = new FakeAdapter();
    adapter.interactionContent = { kind: 'plan-draft', body: SOURCE_FIDELITY_MARKDOWN };
    const interaction = {
      id: 'plan-review-source', kind: 'plan-draft' as const,
      workflowId: 'plan-source', workflowRevision: 11,
    };
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined, interaction,
    };
    render(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByText(/ROOT = "read-only"/)).toBeTruthy();
    if (_name === 'revise') {
      fireEvent.click(screen.getByRole('button', { name: '提出修改' }));
      fireEvent.change(screen.getByPlaceholderText('告诉 Pulsara 需要怎样修改方案…'), {
        target: { value: resolution.feedback },
      });
    }
    fireEvent.click(screen.getByRole('button', { name: action }));

    await waitFor(() => expect(adapter.lastConnection?.resolveInteraction).toHaveBeenCalledWith(
      interaction,
      resolution,
    ));
    expect(await screen.findByText(_name === 'revise'
      ? '已创建继续修订方案的任务。'
      : '未因本次取消启动这份方案的实施。你可以发送新任务。')).toBeTruthy();
  });

  it('shows exact duplicate queued bodies in queue order to an observer', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionRole = 'observer';
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: true, queuedCount: 2,
      queuedPrompts: [{
        queueItemId: 'queue-1', commandId: 'command-1', sequence: 1, status: 'pending',
        deliveryMode: 'steer', targetTurnId: 'turn-1', content: textPrompt('相同\n正文'), permission: 'read-only',
      }, {
        queueItemId: 'queue-2', commandId: 'command-2', sequence: 2, status: 'pending',
        deliveryMode: 'new-turn', content: textPrompt('相同\n正文'),
      }],
    };
    const { container } = render(<PulsaraApp adapter={adapter} />);

    const queue = await screen.findByRole('region', { name: '等待处理的输入' });
    expect([...queue.querySelectorAll('.prompt-content-body')].map((item) => item.textContent))
      .toEqual(['相同\n正文']);
    const steer = screen.getByRole('article', { name: '引导' });
    expect(within(steer).getByText('相同 正文')).toBeTruthy();
    expect(steer.closest('[data-queue-item-id]')?.getAttribute('data-queue-item-id')).toBe('queue-1');
    expect([...queue.querySelectorAll('article')].map((item) => item.dataset.queueItemId))
      .toEqual(['queue-2']);
    expect(queue.closest('.composer-wrap')).toBeTruthy();
    for (const name of ['发送', '编辑', '删除']) {
      expect(within(queue).queryByRole('button', { name })).toBeNull();
    }
    expect(within(queue).getByRole('button', { name: '展开' })).toBeTruthy();
    expect(container.querySelectorAll('.user-turn')).toHaveLength(0);
  });

  it('keeps an observed queue item visible when it reaches a terminal state', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionRole = 'observer';
    adapter.queryCommandResult = {
      commandId: 'command-observed', status: 'rejected', publicCode: 'USER_CANCELLED',
      publicMessage: 'Observed queue item was cancelled.',
      promptDelivery: {
        queueItemId: 'queue-observed', queueStatus: 'CANCELLED', deliveryMode: 'steer',
      },
    };
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: true, eventSequence: 1,
      queuedCount: 1,
      queuedPrompts: [{
        queueItemId: 'queue-observed', commandId: 'command-observed', sequence: 1,
        status: 'pending', deliveryMode: 'steer', targetTurnId: 'turn-observed',
        content: textPrompt('观察者看到的队列正文'), permission: 'ask-permissions',
      }],
    };
    render(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByText('观察者看到的队列正文')).toBeTruthy();
    const active = adapter.lastConnection!;

    active.emit({
      ...projection(''), messages: [], isRunning: true, eventSequence: 2,
      queuedCount: 0, queuedPrompts: [],
    });

    await waitFor(() => expect(active.queryCommand).toHaveBeenCalledWith('command-observed'));
    expect(await screen.findByText('队列已取消')).toBeTruthy();
    expect(screen.getByText('观察者看到的队列正文')).toBeTruthy();
    expect(screen.queryByText('目标轮次：turn-observed')).toBeNull();
    expect(screen.queryByText('适用权限：每次询问')).toBeNull();
    expect(screen.getByText('Observed queue item was cancelled.')).toBeTruthy();
  });

  it.each([
    ['cancelled', '队列已取消'],
    ['rejected', '队列已拒绝'],
  ] as const)('shows an initial-hydration %s terminal without a local submission', async (
    status,
    label,
  ) => {
    const adapter = new FakeAdapter();
    const transition: LocalPromptSubmission = {
      sessionId: 'session-1', connectionGeneration: 1,
      commandId: `command-${status}`, queueItemId: `queue-${status}`,
      contentUnavailable: true,
      deliveryMode: 'steer', targetTurnId: 'turn-race', permission: 'read-only',
      status, detail: `Queue ${status} before hydration completed.`,
    };
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: true, queuedCount: 0, queuedPrompts: [],
      promptTransitions: [transition],
    };

    render(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByText(label)).toBeTruthy();
    expect(screen.getByText('正文未能在队列终止前完成读取。')).toBeTruthy();
    expect(screen.queryByText('目标轮次：turn-race')).toBeNull();
    expect(screen.queryByText('适用权限：只读')).toBeNull();
    expect(screen.getByText(`Queue ${status} before hydration completed.`)).toBeTruthy();
  });

  it('shows an idle submission in the conversation through pending receipt and canonical adoption', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const active = adapter.lastConnection!;
    const receipt = deferred<CommandReceipt>();
    active.submitPrompt.mockImplementationOnce(() => receipt.promise);
    await typeComposer('空闲时直接显示的输入');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledTimes(1));
    const commandId = active.submitPrompt.mock.calls[0]![0];
    const bubble = document.querySelector(`[data-pending-message="${commandId}"]`)!;
    expect(bubble.closest('.thread-column')).toBeTruthy();
    expect(bubble.textContent).toContain('空闲时直接显示的输入');
    expect(bubble.textContent).toContain('正在发送');
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
    await act(async () => receipt.resolve({ commandId, status: 'pending', promptDelivery: {
      queueItemId: 'idle-queue', queueStatus: 'PENDING', deliveryMode: 'new-turn',
    } }));
    act(() => active.emit({ ...projection(''), messages: [], eventSequence: 2, queuedCount: 1,
      queuedPrompts: [{ queueItemId: 'idle-queue', commandId, sequence: 1, status: 'pending',
        deliveryMode: 'new-turn', content: textPrompt('空闲时直接显示的输入'), permission: 'read-only' }],
    }));
    await waitFor(() => expect(document.querySelector(`[data-pending-message="${commandId}"]`)).toBe(bubble));
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
    expect(screen.queryByText(/1 条等待处理/)).toBeNull();
    act(() => active.emit({ ...projection(''), eventSequence: 3, messages: [{
      id: 'idle-entry', role: 'user', userKind: 'prompt', turnId: 'turn-1', time: '现在',
      body: '空闲时直接显示的输入',
      inputSource: { commandId, queueItemId: 'idle-queue', deliveryMode: 'new-turn' },
    }] }));
    await waitFor(() => expect(document.querySelector('[data-pending-message]')).toBeNull());
    expect(screen.getAllByText('空闲时直接显示的输入')).toHaveLength(1);
    expect(active.submitPrompt).toHaveBeenCalledTimes(1);
  });

  it('separates consumed input delivery from an interrupted turn outcome', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    adapter.lastConnection?.submitPrompt.mockResolvedValueOnce({
      commandId: 'ignored-by-mock',
      status: 'rejected',
      publicCode: 'TURN_INTERRUPTED',
      publicMessage: 'The turn was interrupted and will not be replayed.',
      promptDelivery: {
        queueItemId: 'queue-consumed', queueStatus: 'CONSUMED',
        consumedEntryId: 'entry-consumed', deliveryMode: 'new-turn',
      },
    });

    await typeComposer('已经被消费的输入');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('输入已接收，执行已中断')).toBeTruthy();
    expect(screen.queryByText('输入被拒绝')).toBeNull();
    const bubble = document.querySelector('[data-pending-message]') as HTMLElement;
    expect(within(bubble).getByText('已接收 · 执行已中断')).toBeTruthy();
    expect(within(bubble).getByText('已经被消费的输入')).toBeTruthy();
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
  });

  it('keeps and reconciles a queued submission when its canonical pending row disappears', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: true, queuedCount: 0, queuedPrompts: [],
    };
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const active = adapter.lastConnection!;
    active.submitPrompt.mockResolvedValueOnce({
      commandId: 'ignored-by-mock', status: 'pending',
      promptDelivery: {
        queueItemId: 'queue-terminal', queueStatus: 'PENDING', deliveryMode: 'new-turn',
      },
    });
    active.queryCommand.mockResolvedValue({
      commandId: 'ignored-by-mock', status: 'rejected', publicCode: 'USER_CANCELLED',
      publicMessage: 'The queued prompt was cancelled.',
      promptDelivery: {
        queueItemId: 'queue-terminal', queueStatus: 'CANCELLED', deliveryMode: 'new-turn',
      },
    });

    await typeComposer('随后被取消的输入');
    fireEvent.click(screen.getByRole('button', { name: '排队发送' }));
    await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledTimes(1));
    const commandId = active.submitPrompt.mock.calls[0]?.[0] as string;

    active.emit({
      ...projection(''), messages: [], isRunning: true, eventSequence: 2,
      queuedCount: 1,
      queuedPrompts: [{
        queueItemId: 'queue-terminal', commandId, sequence: 1, status: 'pending',
        deliveryMode: 'new-turn', content: textPrompt('随后被取消的输入'), permission: 'accept-edits',
      }],
    });
    await waitFor(() => expect(document.querySelector(
      '[data-queue-item-id="queue-terminal"] .prompt-content-body',
    )?.textContent).toBe('随后被取消的输入'));

    active.emit({
      ...projection(''), messages: [], isRunning: true, eventSequence: 3,
      queuedCount: 0, queuedPrompts: [],
    });

    await waitFor(() => expect(active.queryCommand).toHaveBeenCalledWith(commandId));
    expect(await screen.findByText('队列已取消')).toBeTruthy();
    expect(screen.getByText('The queued prompt was cancelled.')).toBeTruthy();
  });

  it('queries the original command on the replacement connection after transport failure', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    adapter.queryCommandResult = {
      commandId: 'resolved-by-query', status: 'rejected', publicCode: 'TURN_INTERRUPTED',
      promptDelivery: {
        queueItemId: 'queue-network', queueStatus: 'CONSUMED',
        consumedEntryId: 'entry-network', deliveryMode: 'new-turn',
      },
    };
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const original = adapter.lastConnection!;
    original.submitPrompt.mockRejectedValueOnce(
      new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', '连接断开。', true),
    );

    await typeComposer('网络未知输入');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(original.submitPrompt).toHaveBeenCalledTimes(1));
    const commandId = original.submitPrompt.mock.calls[0]?.[0] as string;

    await waitFor(() => expect(adapter.connectCalls).toHaveLength(2));
    await waitFor(() => expect(adapter.lastConnection?.queryCommand).toHaveBeenCalledWith(commandId));
    expect(original.queryCommand).not.toHaveBeenCalled();
    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
    expect(await screen.findByText('输入已接收，执行已中断')).toBeTruthy();
  });

  it.each([
    ['HTTP_413', false, 'HTTP 请求正文超过 8 MiB。'],
    ['PROTOCOL_FRAME_OUT_OF_BOUNDS', true, '这次请求包含的数据过多。'],
  ] as const)('keeps the exact draft after definite upload rejection %s', async (
    code,
    retryable,
    reason,
  ) => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const active = adapter.lastConnection!;
    active.submitPrompt.mockRejectedValueOnce(new RuntimeApiError(code, reason, retryable));

    await typeComposer('保留\\"草稿');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('输入超过上传容量')).toBeTruthy();
    const pendingMessage = document.querySelector('[data-pending-message]') as HTMLElement;
    expect(within(pendingMessage).getByRole('status').textContent).toContain('发送失败');
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
    expect(screen.getAllByText(reason).length).toBeGreaterThan(0);
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toBe('保留\\"草稿');
    expect(active.submitPrompt).toHaveBeenCalledWith(
      expect.any(String),
      textPrompt('保留\\"草稿'),
      'bypass-permissions',
    );
    expect(active.queryCommand).not.toHaveBeenCalled();
    expect(adapter.connectCalls).toHaveLength(1);
    expect(screen.queryByText('提交状态未知')).toBeNull();
    expect(within(pendingMessage).getByRole('status').textContent).toContain(reason);
  });

  it('does not publish a late prompt receipt from an old session onto the new session', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [initialSession, { ...initialSession, id: 'session-2', title: '另一个会话', live: false }];
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    const receipt = deferred<CommandReceipt>();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const first = adapter.lastConnection!;
    first.submitPrompt.mockImplementationOnce(() => receipt.promise);

    await typeComposer('会话 A 的延迟输入');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(first.submitPrompt).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
    await screen.findByRole('heading', { name: '另一个会话' });

    const commandId = first.submitPrompt.mock.calls[0]![0];
    await act(async () => receipt.resolve({
      commandId, status: 'succeeded',
      promptDelivery: {
        queueItemId: 'queue-old-success', queueStatus: 'PENDING', deliveryMode: 'new-turn',
      },
    }));

    expect(screen.queryByText('输入已接受')).toBeNull();
    expect(screen.queryByText('会话 A 的延迟输入')).toBeNull();
    expect(adapter.connectCalls).toHaveLength(2);
  });

  it('does not reconnect the new session when an old-session prompt fails late', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [initialSession, { ...initialSession, id: 'session-2', title: '另一个会话', live: false }];
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    const receipt = deferred<CommandReceipt>();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const first = adapter.lastConnection!;
    first.submitPrompt.mockImplementationOnce(() => receipt.promise);

    await typeComposer('会话 A 的失败输入');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(first.submitPrompt).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
    await screen.findByRole('heading', { name: '另一个会话' });

    await act(async () => receipt.reject(
      new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', '会话 A 连接断开。', true),
    ));

    expect(adapter.connectCalls).toHaveLength(2);
    expect(adapter.lastConnection?.sessionId).toBe('session-2');
    expect(screen.queryByText('提交状态未知')).toBeNull();
    expect(screen.queryByText('会话 A 连接断开。')).toBeNull();
  });

  it('does not publish a late interaction decision from an old session', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [initialSession, { ...initialSession, id: 'session-2', title: '另一个会话', live: false }];
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
      interaction: {
        id: 'plan-old-session', kind: 'plan-draft', workflowId: 'workflow-old', workflowRevision: 1,
      },
    };
    const decision = deferred<CommandReceipt>();
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByText('检查契约');
    const first = adapter.lastConnection!;
    first.resolveInteraction.mockImplementationOnce(() => decision.promise);

    fireEvent.click(screen.getByRole('button', { name: '取消规划' }));
    await waitFor(() => expect(first.resolveInteraction).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
    await screen.findByRole('heading', { name: '另一个会话' });

    await act(async () => decision.resolve({
      commandId: 'command-old-decision', status: 'succeeded',
      planDraftDecision: 'cancel',
    }));

    expect(screen.queryByText('规划已取消')).toBeNull();
    expect(adapter.connectCalls).toHaveLength(2);
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
              status: 'completed', meta: '操作完成', resultText: '已读取 README.md · 1 行',
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
      completionAccepted: false,
      acceptedAt: '2026-08-30T12:00:00Z', terminalAt: '2026-08-30T12:01:00Z',
      summary: '### 研究结论\n\n页面符合契约。', color: 'blue',
      result: {
        id: 'result-complete', entryId: 'entry-result', summary: '### 研究结论\n\n页面符合契约。',
        outputPreview: '- 完整输出', diagnostics: [{ message: '目视检查通过' }],
      },
    }, {
      id: 'task-interrupted', label: '中断检查', role: '验证', objective: '验证重启边界。',
      status: 'interrupted', parentId: 'turn-2', batchId: 'batch-2', dependencyIds: [], color: 'amber',
      completionAccepted: false,
    }, {
      id: 'task-blocked', label: '等待产物', role: '整合', objective: '整合前置产物。',
      status: 'blocked', parentId: 'turn-1', batchId: 'batch-1', dependencyIds: ['task-interrupted'],
      dependencies: [{ id: 'task-interrupted', label: '中断检查', status: 'interrupted' }], color: 'violet',
      completionAccepted: false,
    }];

    render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    await waitFor(() => expect(adapter.listSessionTasks).toHaveBeenCalled());

    expect(screen.getByRole('button', { name: '项目能力' }).classList.contains('is-active')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '任务' }));
    expect(screen.getByRole('button', { name: '任务' }).classList.contains('is-active')).toBe(true);
    expect(await screen.findByText(/1 已完成 · 0 进行中 · 1 需留意/)).toBeTruthy();
    expect(screen.getByRole('button', { name: /中断检查/ })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /子任务组/ }));
    const dialog = await screen.findByRole('dialog', { name: /子任务组/ });
    expect(within(dialog).getByRole('button', { name: /等待产物/ })).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: /汇总研究/ }));
    const taskConversation = dialog.querySelector('.task-conversation');
    expect(taskConversation).toBeTruthy();
    expect(within(taskConversation as HTMLElement).getByText('主任务')).toBeTruthy();
    expect(taskConversation?.querySelectorAll('.user-turn')).toHaveLength(1);
    expect(taskConversation?.querySelector('.user-turn')?.textContent).toContain('### 汇总目标\n\n整理可见结果。');
    expect(within(dialog).queryByRole('heading', { name: '汇总目标' })).toBeNull();
    expect(screen.getByRole('heading', { name: '研究结论' })).toBeTruthy();
    expect(within(dialog).getByText('目视检查通过')).toBeTruthy();

    expect(within(dialog).queryByText('结果尚未加入主对话。')).toBeNull();
    expect(within(dialog).queryByRole('button', { name: '用这份结果继续' })).toBeNull();
    expect(within(dialog).queryByText(/不会重新运行子任务/)).toBeNull();
  });
});

describe('PR04 atomic queue action ownership', () => {
  const source = {
    queueItemId: 'queue-source', commandId: 'source-command', sequence: 1, status: 'pending' as const,
    deliveryMode: 'new-turn' as const, content: textPrompt('  keep\n原文  '), permission: 'read-only' as const,
    requestedPermission: 'ask-permissions' as const,
  };
  async function setup() {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(''), messages: [], queuedCount: 1, queuedPrompts: [source],
      control: { active_turns: [{ turn_id: 'turn-1', scope_kind: 'ROOT', status: 'RUNNING' }] } };
    const view = render(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('region', { name: '等待处理的输入' });
    return { adapter, active: adapter.lastConnection!, ...view };
  }
  const accepted = (commandId: string): CommandReceipt => ({
    commandId, status: 'pending', publicCode: 'PROMPT_STEER_QUEUED', promptDelivery: {
      queueItemId: 'replacement', queueStatus: 'PENDING', deliveryMode: 'steer',
    },
  });

  it('sends one text-free action, waits for ACK, then exactly replaces the optimistic card', async () => {
    const { active, container } = await setup();
    const pending = deferred<CommandReceipt>();
    const steer = vi.spyOn(active, 'steerQueuedPrompt').mockReturnValue(pending.promise);
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt');
    const send = within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' });
    fireEvent.click(send);
    fireEvent.click(send);
    expect(steer).toHaveBeenCalledTimes(1);
    const commandId = steer.mock.calls[0][0];
    expect(steer).toHaveBeenCalledWith(commandId, source.queueItemId, 'turn-1');
    expect(cancel).not.toHaveBeenCalled();
    expect(active.submitPrompt).not.toHaveBeenCalled();
    expect(screen.queryByRole('article', { name: '引导' })).toBeNull();
    pending.resolve(accepted(commandId));
    await screen.findByRole('article', { name: '引导' });
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
    expect(container.querySelector('.user-steer .prompt-content-body')?.textContent).toBe(
      promptContentTextProjection(source.content),
    );
    active.emit({ ...projection(''), queuedCount: 0, queuedPrompts: [], eventSequence: 3,
      messages: [{ id: 'canonical-steer', role: 'user', userKind: 'steer', turnId: 'turn-1',
        time: 'now', body: promptContentTextProjection(source.content), status: 'completed',
        inputSource: { commandId, queueItemId: 'replacement', deliveryMode: 'steer' } }],
    });
    await waitFor(() => expect(container.querySelector('[data-action-command-id]')).toBeNull());
    expect(screen.getAllByRole('article', { name: '引导' })).toHaveLength(1);
  });

  it('recovers a lost action ACK by querying its original command on the new connection', async () => {
    const { active, adapter } = await setup();
    const steer = vi.spyOn(active, 'steerQueuedPrompt').mockImplementation(async commandId => {
      adapter.queryCommandResult = accepted(commandId);
      throw new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'ACK lost', true);
    });
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    await screen.findByRole('article', { name: '引导' });
    expect(adapter.connectCalls).toHaveLength(2);
    expect(steer).toHaveBeenCalledTimes(1);
    expect(adapter.lastConnection!.queryCommand).toHaveBeenCalledWith(steer.mock.calls[0][0]);
    expect(adapter.lastConnection!.submitPrompt).not.toHaveBeenCalled();
  });

  it('freezes the current canonical ROOT after reload instead of a stale live snapshot ROOT', async () => {
    const { active } = await setup();
    const steer = vi.spyOn(active, 'steerQueuedPrompt').mockImplementation(async commandId => accepted(commandId));
    active.emit({ ...active.current(), activeTurnId: 'turn-old-snapshot',
      control: { active_turns: [{ turn_id: 'turn-successor', scope_kind: 'ROOT', status: 'RUNNING' }] },
      eventSequence: 9,
    });
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    expect(steer).toHaveBeenCalledWith(expect.any(String), source.queueItemId, 'turn-successor');
    await screen.findByRole('article', { name: '引导' });
  });

  it('does not send to the reloaded ROOT when canonical control has no RUNNING ROOT', async () => {
    const { active } = await setup();
    const steer = vi.spyOn(active, 'steerQueuedPrompt');
    active.emit({ ...active.current(), activeTurnId: 'turn-old-snapshot', control: {}, eventSequence: 10 });
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    expect(steer).not.toHaveBeenCalled();
    expect(screen.getByText('这条输入会按队列顺序自动处理。')).toBeTruthy();
    expect(screen.getByRole('region', { name: '等待处理的输入' })).toBeTruthy();
  });

  it('keeps the exact source after the completion fence rejects the action', async () => {
    const { active, container } = await setup();
    vi.spyOn(active, 'steerQueuedPrompt').mockImplementation(async commandId => ({
      commandId, status: 'rejected', publicCode: 'STEER_TARGET_CLOSED', publicMessage: '当前任务已结束；输入仍按队列顺序处理。',
    }));
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    await screen.findByText('当前任务已结束；输入仍按队列顺序处理。');
    expect(container.querySelector(
      '[data-queue-item-id="queue-source"] .prompt-content-body',
    )?.textContent).toBe(
      promptContentTextProjection(source.content),
    );
    expect(screen.queryByRole('article', { name: '引导' })).toBeNull();
    expect(active.submitPrompt).not.toHaveBeenCalled();
  });

  it('edits only after cancellation ACK and restores the requested permission', async () => {
    const { active } = await setup();
    const cancellation = deferred<CommandReceipt>();
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt').mockReturnValue(cancellation.promise);
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    const input = screen.getByLabelText('发送给 Pulsara');
    expect(input.textContent).toBe('');
    expect(screen.getByRole('region', { name: '等待处理的输入' })).toBeTruthy();
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    cancellation.resolve({ commandId: cancel.mock.calls[0][0], status: 'succeeded', publicCode: 'PROMPT_CANCELLED',
      promptDelivery: { queueItemId: source.queueItemId, queueStatus: 'CANCELLED', deliveryMode: 'new-turn' } });
    await waitFor(() => expect(composerIsDisabled(screen.getByLabelText('发送给 Pulsara')))
      .toBe(false));
    const restored = screen.getByLabelText('发送给 Pulsara');
    expect(restored.innerHTML).toContain('<br');
    expect(restored.textContent).toContain('keep');
    expect(restored.textContent).toContain('原文');
    expect(screen.getByRole('button', { name: /每次询问/ })).toBeTruthy();
    await waitFor(() => expect(document.activeElement)
      .toBe(screen.getByLabelText('发送给 Pulsara')));
    expect(active.submitPrompt).not.toHaveBeenCalled();
    fireEvent.keyDown(restored, { key: 'Enter' });
    await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledWith(
      expect.any(String),
      source.content,
      'ask-permissions',
    ));
  });

  it('hydrates every queued image before cancellation and restores exact typed nodes', async () => {
    const digest = `sha256:${'1'.repeat(64)}`;
    const imageSource = {
      ...source,
      queueItemId: 'queue-images',
      commandId: 'source-images',
      content: {
        parts: [
          { type: 'text' as const, text: 'before' },
          {
            type: 'image' as const,
            source: 'canonical' as const,
            digest,
            encodedBytes: 3,
            mediaType: 'image/png',
            width: 2,
            height: 1,
            refOrdinal: 0,
            owner: { kind: 'queue' as const, queueItemId: 'queue-images' },
          },
          { type: 'text' as const, text: '\nafter' },
          {
            type: 'image' as const,
            source: 'canonical' as const,
            digest,
            encodedBytes: 3,
            mediaType: 'image/png',
            width: 2,
            height: 1,
            refOrdinal: 1,
            owner: { kind: 'queue' as const, queueItemId: 'queue-images' },
          },
        ],
      },
    };
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [],
      queuedCount: 1,
      queuedPrompts: [imageSource],
      control: {
        active_turns: [{ turn_id: 'turn-1', scope_kind: 'ROOT', status: 'RUNNING' }],
      },
    };
    render(<PulsaraApp adapter={adapter} />);
    const queue = await screen.findByRole('region', { name: '等待处理的输入' });
    expect(within(queue).getAllByRole('button', { name: /^\[Figure [12]\]$/ }))
      .toHaveLength(2);
    const active = adapter.lastConnection!;
    const secondRead = deferred<Uint8Array>();
    const read = vi.spyOn(active, 'readPromptImage').mockImplementation(async (image) => {
      if (image.refOrdinal === 0) return new Uint8Array([1, 2, 3]);
      return secondRead.promise;
    });
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt');

    fireEvent.click(within(queue).getByRole('button', { name: '编辑' }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(read.mock.calls.map(([image]) => [image.owner, image.refOrdinal])).toEqual([
      [{ kind: 'queue', queueItemId: 'queue-images' }, 0],
      [{ kind: 'queue', queueItemId: 'queue-images' }, 1],
    ]);
    expect(cancel).not.toHaveBeenCalled();
    expect(screen.getByRole('region', { name: '等待处理的输入' })).toBeTruthy();
    expect(composerIsDisabled(screen.getByLabelText('发送给 Pulsara'))).toBe(true);

    secondRead.resolve(new Uint8Array([4, 5, 6]));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(composerIsDisabled(
      screen.getByLabelText('发送给 Pulsara'),
    )).toBe(false));
    const composer = screen.getByLabelText('发送给 Pulsara');
    expect(composer.querySelectorAll('img[data-prompt-asset-id]')).toHaveLength(2);
    expect(composer.textContent).toContain('before');
    expect(composer.textContent).toContain('after');
    await waitFor(() => expect(document.activeElement).toBe(composer));

    fireEvent.keyDown(composer, { key: 'Enter' });
    await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledWith(
      expect.any(String),
      {
        parts: [
          { type: 'text', text: 'before' },
          {
            type: 'image', source: 'local',
            bytes: new Uint8Array([1, 2, 3]), declaredMediaType: 'image/png',
          },
          { type: 'text', text: '\nafter' },
          {
            type: 'image', source: 'local',
            bytes: new Uint8Array([4, 5, 6]), declaredMediaType: 'image/png',
          },
        ],
      },
      'ask-permissions',
    ));
  });

  it('releases an unknown edit when its exact source has been canonically consumed', async () => {
    const { active, adapter } = await setup();
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt').mockImplementation(async () => {
      // FIFO won; the rejection was not persisted as an action command.
      throw new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'rejection ACK lost', true);
    });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    await waitFor(() => expect(adapter.connectCalls).toHaveLength(2));
    const next = adapter.lastConnection!;
    await waitFor(() => expect(next.queryCommand).toHaveBeenCalledWith(cancel.mock.calls[0][0]));
    const input = screen.getByLabelText('发送给 Pulsara');
    expect(composerIsDisabled(input)).toBe(true);
    // Identical text from a different submission is not evidence about source.
    next.emit({ ...next.current(), eventSequence: 10, messages: [{
      id: 'other', role: 'user', userKind: 'prompt', time: 'now',
      body: promptContentTextProjection(source.content),
      inputSource: { commandId: 'other-command', queueItemId: 'other-queue', deliveryMode: 'new-turn' },
    }] });
    await waitFor(() => expect(next.queryCommand.mock.calls.length).toBeGreaterThan(1));
    expect(composerIsDisabled(input)).toBe(true);
    next.emit({ ...next.current(), eventSequence: 20, queuedPrompts: [], queuedCount: 0, messages: [{
      id: 'consumed-source', turnId: 'turn-next', entrySequence: 20,
      role: 'user', userKind: 'prompt', time: 'now',
      body: promptContentTextProjection(source.content),
      inputSource: { commandId: source.commandId, queueItemId: source.queueItemId, deliveryMode: 'new-turn' },
    }] });
    await waitFor(() => expect(composerIsDisabled(input)).toBe(false));
    expect(input.textContent).toBe('');
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(next.submitPrompt).not.toHaveBeenCalled();
  });

  it('reserves skill insertion as well as typing while an edit is in flight', async () => {
    const { active } = await setup();
    const pending = deferred<CommandReceipt>();
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt').mockReturnValue(pending.promise);
    fireEvent.click(screen.getByRole('button', { name: '本轮选项' }));
    fireEvent.click(await screen.findByRole('button', { name: '选择技能' }));
    const skill = screen.getByRole('button', { name: /\$pdf/ });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    // Even an already-open menu must not bypass the composer reservation.
    fireEvent.click(skill);
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    pending.resolve({ commandId: cancel.mock.calls[0][0], status: 'succeeded', publicCode: 'PROMPT_CANCELLED',
      promptDelivery: { queueItemId: source.queueItemId, queueStatus: 'CANCELLED', deliveryMode: 'new-turn' } });
    await waitFor(() => expect(composerIsDisabled(screen.getByLabelText('发送给 Pulsara')))
      .toBe(false));
    const restored = screen.getByLabelText('发送给 Pulsara');
    expect(restored.innerHTML).toContain('<br');
    fireEvent.keyDown(restored, { key: 'Enter' });
    await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledWith(
      expect.any(String),
      source.content,
      'ask-permissions',
    ));
  });

  it('queries a rejected steer even if no pending snapshot ever contained the replacement', async () => {
    const { active } = await setup();
    const steer = vi.spyOn(active, 'steerQueuedPrompt').mockImplementation(async commandId => accepted(commandId));
    const query = vi.spyOn(active, 'queryCommand').mockImplementation(async commandId => accepted(commandId));
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    await screen.findByRole('article', { name: '引导' });
    const commandId = steer.mock.calls[0][0];
    query.mockImplementation(async id => id === source.commandId ? {
      commandId: id, status: 'succeeded', publicCode: 'PROMPT_CANCELLED',
      promptDelivery: { queueItemId: source.queueItemId, queueStatus: 'CANCELLED', deliveryMode: 'new-turn' },
    } : ({ commandId: id, status: 'rejected', publicCode: 'STEER_TARGET_TERMINAL',
      publicMessage: '目标任务已中断，引导未被接纳。',
      promptDelivery: { queueItemId: 'replacement', queueStatus: 'REJECTED', deliveryMode: 'steer' } }));
    active.emit({ ...active.current(), isRunning: false, control: { active_turns: [] },
      eventSequence: 10, messages: [], queuedPrompts: [], queuedCount: 0 });
    await waitFor(() => expect(screen.queryByRole('article', { name: '引导' })).toBeNull());
    expect(query).toHaveBeenCalledWith(commandId);
    expect(steer).toHaveBeenCalledTimes(1);
    expect(screen.getByText('目标任务已中断，引导未被接纳。')).toBeTruthy();
  });

  it('queries the exact consumed source when it is absent from visible history', async () => {
    const { active, adapter } = await setup();
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt').mockRejectedValue(
      new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'rejection ACK lost', true),
    );
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    await waitFor(() => expect(adapter.connectCalls).toHaveLength(2));
    const next = adapter.lastConnection!;
    await waitFor(() => expect(next.queryCommand).toHaveBeenCalledWith(cancel.mock.calls[0][0]));
    next.queryCommand.mockImplementation(async id => id === source.commandId ? {
      commandId: id, status: 'succeeded', publicCode: 'PROMPT_CONSUMED',
      promptDelivery: { queueItemId: source.queueItemId, queueStatus: 'CONSUMED', deliveryMode: 'new-turn' },
    } : undefined);
    next.emit({ ...next.current(), eventSequence: 20, messages: [], queuedPrompts: [], queuedCount: 0 });
    const input = screen.getByLabelText('发送给 Pulsara');
    await waitFor(() => expect(composerIsDisabled(input)).toBe(false));
    expect(next.queryCommand).toHaveBeenCalledWith(source.commandId);
    expect(input.textContent).toBe('');
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(next.submitPrompt).not.toHaveBeenCalled();
  });

  it('rechecks a newer cut after the original action query finishes without issuing concurrent queries', async () => {
    const { active } = await setup();
    const pendingQuery = deferred<CommandReceipt>();
    const steer = vi.spyOn(active, 'steerQueuedPrompt').mockImplementation(async id => accepted(id));
    const query = vi.spyOn(active, 'queryCommand').mockReturnValue(pendingQuery.promise);
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    await screen.findByRole('article', { name: '引导' });
    const commandId = steer.mock.calls[0][0];
    await waitFor(() => expect(query).toHaveBeenCalledWith(commandId));
    query.mockImplementation(async id => id === source.commandId ? {
      commandId: id, status: 'succeeded', publicCode: 'PROMPT_CANCELLED',
      promptDelivery: { queueItemId: source.queueItemId, queueStatus: 'CANCELLED', deliveryMode: 'new-turn' },
    } : {
      commandId: id, status: 'rejected', publicCode: 'STEER_TARGET_TERMINAL',
      publicMessage: '目标任务已中断，引导未被接纳。',
      promptDelivery: { queueItemId: 'replacement', queueStatus: 'REJECTED', deliveryMode: 'steer' },
    });
    active.emit({ ...active.current(), isRunning: false, control: { active_turns: [] },
      eventSequence: 10, messages: [], queuedPrompts: [], queuedCount: 0 });
    await waitFor(() => expect(query).toHaveBeenCalledWith(source.commandId));
    expect(query.mock.calls.filter(([id]) => id === commandId)).toHaveLength(1);
    expect(screen.getByRole('article', { name: '引导' })).toBeTruthy();
    pendingQuery.resolve(accepted(commandId));
    await waitFor(() => expect(screen.queryByRole('article', { name: '引导' })).toBeNull());
    expect(query.mock.calls.filter(([id]) => id === commandId)).toHaveLength(2);
    expect(screen.getByText('目标任务已中断，引导未被接纳。')).toBeTruthy();
    expect(steer).toHaveBeenCalledTimes(1);
  });
});

function pr05Projection(busy = false): RuntimeProjection {
  return { ...projection('PR05'), hostSessionId: 'host:pr05', interaction: {
    id: 'interaction:pr05', kind: 'tool-confirmation', prompt: 'Allow terminal?', options: ['ALLOW', 'DENY'],
    expiresAtUtc: '2099-01-01T00:00:00Z', decisionInProgress: busy,
  } };
}

it.each([
  [false, 'PROTOCOL_TRANSPORT_CLOSED'], [true, 'PROTOCOL_TRANSPORT_CLOSED'],
  [false, 'INTERACTION_OUTCOME_UNKNOWN'], [true, 'INTERACTION_OUTCOME_UNKNOWN'],
] as const)('PR05 original command survives unknown ACK and card remount, projected busy=%s, code=%s', async (busy, code) => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pr05Projection();
  render(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  const first = adapter.lastConnection!;
  const reconnects = code === 'PROTOCOL_TRANSPORT_CLOSED';
  first.resolveInteraction.mockRejectedValueOnce(new RuntimeApiError(code, 'lost ACK', reconnects));
  fireEvent.click(allow);
  await waitFor(() => expect(adapter.connectCalls).toHaveLength(reconnects ? 2 : 1));
  const submitted = first.resolveInteraction.mock.calls[0][1];
  expect(submitted.kind).toBe('tool');
  if (submitted.kind !== 'tool') throw new Error('expected tool command');
  expect(submitted.commandId).toMatch(/^command:web:/);
  const recovered = adapter.lastConnection!;
  await waitFor(() => expect(recovered.queryCommand).toHaveBeenCalledWith(submitted.commandId));
  expect(screen.getByRole('button', { name: /允许本次操作/ }).hasAttribute('disabled')).toBe(true);
  await act(async () => recovered.emit({ ...pr05Projection(busy), liveControlRevision: 4 }));
  fireEvent.click(screen.getByRole('button', { name: /允许本次操作/ }));
  if (recovered !== first) expect(recovered.resolveInteraction).not.toHaveBeenCalled();
  expect(first.resolveInteraction).toHaveBeenCalledTimes(1);
});

it('PR05 projection prevents deciding while another controller is writing or confirming', async () => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pr05Projection(true);
  render(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  expect(allow.hasAttribute('disabled')).toBe(true);
  expect(await screen.findByText('正在处理确认')).toBeTruthy();
  fireEvent.click(allow);
  expect(adapter.lastConnection!.resolveInteraction).not.toHaveBeenCalled();
});

it('PR05 late tool decision stays with A while B retains its draft and buttons', async () => {
  const adapter = new FakeAdapter();
  adapter.sessions = [initialSession, { ...initialSession, id: 'session-2', title: '另一个会话', live: false }];
  adapter.connectionValue = pr05Projection();
  const pending = deferred<CommandReceipt>();
  render(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  const first = adapter.lastConnection!;
  first.resolveInteraction.mockImplementationOnce(() => pending.promise);
  fireEvent.click(allow);
  const submitted = first.resolveInteraction.mock.calls[0][1];
  if (submitted.kind !== 'tool') throw new Error('expected tool');
  fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
  await screen.findByRole('heading', { name: '另一个会话' });
  const composer = await typeComposer('B private draft');
  await act(async () => pending.resolve({ commandId: submitted.commandId, status: 'succeeded', publicCode: 'INTERACTION_ALLOW' }));
  expect(composer.textContent).toBe('B private draft');
  expect(screen.queryByText('已允许本次操作')).toBeNull();
  expect(adapter.connectCalls).toHaveLength(2);
});

it('PR05 queries a lost HTTP ACK before waiting for connection cleanup', async () => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pr05Projection();
  render(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  const first = adapter.lastConnection!;
  const close = vi.spyOn(first, 'close').mockReturnValue(new Promise(() => {}));
  first.resolveInteraction.mockRejectedValueOnce(new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'HTTP ACK lost', true));
  first.queryCommand.mockImplementation(async commandId => ({ commandId, status: 'succeeded', publicCode: 'INTERACTION_ALLOW' }));
  fireEvent.click(allow);
  await screen.findByText('已允许本次操作');
  const submitted = first.resolveInteraction.mock.calls[0][1];
  if (submitted.kind !== 'tool') throw new Error('expected tool');
  expect(first.queryCommand).toHaveBeenCalledWith(submitted.commandId);
  expect(first.resolveInteraction).toHaveBeenCalledTimes(1);
  expect(adapter.connectCalls).toHaveLength(1);
  expect(close).not.toHaveBeenCalled();
});

it('PR05 confirmed non-acceptance permits a new explicit click on the same valid confirmation', async () => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pr05Projection();
  render(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  const active = adapter.lastConnection!;
  active.resolveInteraction.mockRejectedValueOnce(new RuntimeApiError('INTERACTION_NOT_ACCEPTED', 'confirmed rollback', false));
  fireEvent.click(allow);
  await screen.findByText('这项选择没有被接受');
  expect(active.queryCommand).not.toHaveBeenCalled();
  await waitFor(() => expect(allow.hasAttribute('disabled')).toBe(false));
  const first = active.resolveInteraction.mock.calls[0][1];
  active.resolveInteraction.mockImplementationOnce(async (_, resolution) => {
    if (resolution.kind !== 'tool') throw new Error('expected tool');
    return {commandId: resolution.commandId!, status: 'succeeded', publicCode: 'INTERACTION_ALLOW'};
  });
  fireEvent.click(allow);
  await screen.findByText('已允许本次操作');
  expect(active.resolveInteraction).toHaveBeenCalledTimes(2);
  const second = active.resolveInteraction.mock.calls[1][1];
  if (first.kind !== 'tool' || second.kind !== 'tool') throw new Error('expected tool');
  expect(second.commandId).not.toBe(first.commandId);
  expect(adapter.connectCalls).toHaveLength(1);
});

it.each([false, true])('PR05 later unknown command recovers independently of old unresolved query, pending=%s', async pending => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pr05Projection();
  render(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  const active = adapter.lastConnection!;
  const blocked = deferred<CommandReceipt | undefined>();
  const queries = new Map<string, number>();
  let firstCommand: string | undefined;
  active.resolveInteraction.mockRejectedValue(new RuntimeApiError('SERVER_OPERATION_FAILED', 'unknown', false));
  active.queryCommand.mockImplementation(commandId => {
    firstCommand ??= commandId;
    const count = (queries.get(commandId) ?? 0) + 1;
    queries.set(commandId, count);
    if (count === 1) return Promise.resolve(undefined); // Direct post-ACK read.
    if (commandId === firstCommand) return pending ? blocked.promise : Promise.resolve(undefined);
    return Promise.resolve({commandId, status: 'succeeded', publicCode: 'INTERACTION_ALLOW'});
  });
  fireEvent.click(allow);
  await waitFor(() => expect(queries.get(firstCommand!)).toBe(2));
  const next = pr05Projection();
  if (next.interaction?.kind !== 'tool-confirmation') throw new Error('expected confirmation');
  next.interaction = {...next.interaction, id: 'interaction:next'};
  next.liveControlRevision = 8;
  next.eventSequence = 8;
  await act(async () => active.emit(next));
  if (pending) expect(queries.get(firstCommand!)).toBe(2); // One read in flight per command/connection.
  const nextAllow = screen.getByRole('button', { name: /允许本次操作/ });
  await waitFor(() => expect(nextAllow.hasAttribute('disabled')).toBe(false));
  fireEvent.click(nextAllow);
  await screen.findByText('已允许本次操作');
  const second = active.resolveInteraction.mock.calls[1][1];
  if (second.kind !== 'tool') throw new Error('expected tool');
  expect(queries.get(second.commandId!)).toBe(2);
  expect(active.resolveInteraction).toHaveBeenCalledTimes(2);
  await act(async () => blocked.resolve(undefined));
  const oldQueries = queries.get(firstCommand!);
  await act(async () => active.emit({...next, eventSequence: 9, liveControlRevision: 9}));
  expect(queries.get(firstCommand!)).toBe(oldQueries); // Ended old intent is retired.
  expect(nextAllow.hasAttribute('disabled')).toBe(true); // Accepted is never re-enabled by an empty read.
});
