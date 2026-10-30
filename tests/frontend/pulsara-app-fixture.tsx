import { uiPreferences } from '../../frontend/lib/ui-preferences';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, vi } from 'vitest';
import { LocalMemoryApi } from '../../frontend/lib/memory-api';


import { RuntimeApiError } from '../../frontend/lib/runtime-adapter';
import type { CommandReceipt, BackgroundProcessLog, BackgroundProcessPage, ForkOutcome, RuntimeAdapter, RuntimeBootstrap, ModelCatalogReadModel, RuntimeConnection, RuntimeInteractionContent, RuntimeInteractionSummary, RuntimeProjection, UserControlQueryResult, UserControlCommandRef, CanonicalPromptContent, CanonicalPromptImagePart, EditablePromptContent, ModelCallBindingPayload } from '../../frontend/lib/runtime-adapter';
import type { CapabilitySnapshot, SessionSummary, SessionWorkspaceSelection, UserCapabilitySnapshot } from '../../frontend/lib/pulsara-types';
import { WELCOME_TYPEWRITER_PHRASES } from '../../frontend/components/welcome-typewriter';

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
  Object.defineProperty(window, 'pulsaraDesktop', { configurable: true, writable: true, value: { writeClipboardText: writeText } });
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

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true, value() { this.setAttribute('open', ''); },
  });
  // jsdom hides popovers but does not implement opening them.
  Object.defineProperty(HTMLElement.prototype, 'showPopover', {
    configurable: true, value() { this.style.display = 'block'; },
  });
  Object.defineProperty(HTMLElement.prototype, 'hidePopover', {
    configurable: true, value() { this.style.display = 'none'; },
  });
  // Each test starts with its own browser selection; remounts within a test
  // still exercise the production preference persistence.
  uiPreferences.removeItem('pulsara-active-session');
  uiPreferences.removeItem('pulsara-workspace-open');
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} });
});

afterEach(() => { delete window.pulsaraDesktop;
  cleanup();
  Reflect.deleteProperty(HTMLElement.prototype, 'showPopover');
  Reflect.deleteProperty(HTMLElement.prototype, 'hidePopover');
});

const bootstrap: RuntimeBootstrap = {
  application: { name: 'Pulsara', version: '0.1.0', transport: 'local' },
  protocol: { major: 3, minor: 0 },
  runtime: { status: 'ready', origin: 'http://localhost', database_state: 'ready' },
  database_state: 'ready',
  model_catalog_loading: false,
  model_catalog_recovering: false,
  model_catalog_error: null,
  local_settings: { input_suggestions_enabled: true, pulsara_home: '/Users/test/custom-pulsara-home',
    state: 'ready',
    postgres: { runtime_dsn: 'postgresql://pulsara@localhost/pulsara', admin_dsn: null },
    memory_retrieval: { embedding: null, embedding_enabled: false, history_enabled: true, rerank: null, decision: null, ranking_mode: 'off' },
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
  id: 'session:bwpogagzox7onyn52iumyctnkq',
  title: '准备发布',
  subtitle: '1 条记录',
  status: 'running',
  updatedAt: '刚刚',
  live: false,
  workspace: {
    id: 'workspace',
    name: 'pulsara_agent',
    path: '/tmp/pulsara_agent',
    kind: 'project',
  },
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
  sessionId: 'session:bwpogagzox7onyn52iumyctnkq',
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
      mcpCount: 1, hookCount: 3,
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
      turnId: 'turn-1',
      role: 'assistant',
      time: '现在',
      body,
      status: 'running',
    }],
    isRunning: true,
    queuedCount: 0,
    queuedPrompts: [],
    promptTransitions: [],
    canonicalInputs: [],
    planMode: false,
    activeTurnId: 'turn-1',
    control: {},
    liveControl: {},
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
  readonly availability = {path: '/tmp/project', outcome: 'AVAILABLE' as const, reason: null};
  readonly filePreview = {
    open: vi.fn(async () => { throw new Error('No preview fixture'); }),
    page: vi.fn(async () => { throw new Error('No page fixture'); }),
    close: vi.fn(async () => undefined),
    action: vi.fn(async () => undefined),
  };
  async importFiles(): Promise<import('../../frontend/lib/file-reference').ImportedPath> {
    throw new Error('Unexpected file import');
  }
  readonly role: 'controller' | 'observer';
  readonly generation: number;
  private value: RuntimeProjection;
  private observer?: (value: RuntimeProjection) => void;
  private listeners = new Set<() => void>();
  subscribe = vi.fn((listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  });
  readQueuePage = vi.fn(async (_cursor?: string, signal?: AbortSignal) => {
    signal?.throwIfAborted();
    return {items:this.value.queuedPrompts, nextCursor:null};
  });
  readQueueItem = vi.fn(async (queueItemId: string, signal?: AbortSignal) => {
    signal?.throwIfAborted();
    return this.value.queuedPrompts.find(item => item.queueItemId === queueItemId) ?? null;
  });
  publishRead(value: RuntimeProjection) {
    this.value = {...value, canonicalInputs: [...new Map([
      ...value.canonicalInputs ?? [], ...value.messages.filter(message => message.role === 'user' && message.inputSource),
    ].map(input => [input.id, input])).values()]};
    for (const listener of this.listeners) listener();
  }
  readHistory = vi.fn(async (_windowId?: string, _direction?: 'older' | 'newer', _signal?: AbortSignal) => {
    void _windowId; void _direction; void _signal; return this.value;
  });
  hydrateEntries = vi.fn(async (_ids: string[], _signal?: AbortSignal, _includeReasoning?: boolean) => {
    void _ids; void _signal; void _includeReasoning; return this.value;
  });
  locateEntry = vi.fn(async (_entryId: string, _signal?: AbortSignal) => {
    void _entryId; void _signal; return this.value;
  });
  readProcessEnd = vi.fn(async (_entryId: string, signal: AbortSignal) => {
    signal.throwIfAborted();
    return this.current();
  });
  locateAnnotationSource = vi.fn(async (_entryId: string, _signal?: AbortSignal) => {
    void _entryId; void _signal; return this.value;
  });
  closed = false;

  constructor(
    readonly sessionId: string,
    value = projection(),
    role: 'controller' | 'observer' = 'controller',
    private readonly interactionContent?: RuntimeInteractionContent,
    generation = 1,
    private readonly queryCommandResult?: CommandReceipt,
  ) {
    this.value = {...value, canonicalInputs: [...new Map([
      ...value.canonicalInputs ?? [], ...value.messages.filter(message => message.role === 'user' && message.inputSource),
    ].map(input => [input.id, input])).values()]};
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
    this.value = {...value, canonicalInputs: [...new Map([
      ...value.canonicalInputs ?? [], ...value.messages.filter(message => message.role === 'user' && message.inputSource),
    ].map(input => [input.id, input])).values()]};
    const observer = this.observer;
    this.observer = undefined;
    observer?.(this.value);
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

  async stopActiveTurn(_reference: UserControlCommandRef): Promise<CommandReceipt> {
    void _reference;
    return { commandId: 'command-3', status: 'succeeded' };
  }

  async terminateBackgroundProcess(): Promise<CommandReceipt> {
    return { commandId: 'command-terminate-process', status: 'succeeded' };
  }

  async queryControlCommand(_reference: UserControlCommandRef): Promise<UserControlQueryResult> {
    void _reference;
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

  async readVisualizationContent(): Promise<string> {
    throw new Error('No visualization fixture');
  }

  async readVisualizationThumbnail(): Promise<string> {
    return 'data:image/png;base64,';
  }

  async readPromptImage(image: CanonicalPromptImagePart): Promise<Uint8Array> {
    void image;
    throw new Error('No prompt image fixture');
  }

  async readPromptForEdit(content: CanonicalPromptContent): Promise<EditablePromptContent> {
    const parts: EditablePromptContent['parts'][number][] = [];
    for (const part of content.parts) {
      parts.push(part.type !== 'image'
        ? { ...part }
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
    commandId?: string,
  ): Promise<CommandReceipt> => {
    return {
      commandId: commandId ?? 'command-6', status: 'succeeded',
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

  queryCommand = vi.fn<RuntimeConnection['queryCommand']>(async (commandId) => {
    if (this.closed) {
      throw new RuntimeApiError('CONNECTION_CLOSED', '本地连接已经关闭。', true);
    }
    return this.queryCommandResult ? { ...this.queryCommandResult, commandId } : undefined;
  });

  async close() { this.closed = true; }
}

class FakeAdapter implements RuntimeAdapter {
  async ephemeralResults(sessionId: string): Promise<import('../../frontend/lib/runtime-adapter').SessionEphemeralResults> {
    return { session_id: sessionId, host_session_id: this.lastConnection?.current().hostSessionId ?? null,
      suggestion: { state: 'absent' as const, origin_turn_id: null, turn_id: null, final_entry_id: null, model_call_binding: null, text: null },
      title: { state: 'absent' as const } };
  }

  hooks = {list: vi.fn(async () => ({sources: [], details: []})), config: vi.fn(), save: vi.fn(), review: vi.fn(), trust: vi.fn()};
  userAvatar = vi.fn(async () => ({ configured: false }));
  uploadUserAvatar = vi.fn(async (_file: File) => { void _file; return { configured: true }; });
  clearUserAvatar = vi.fn(async () => ({ configured: false }));
  workspaceAvailability = vi.fn(async (_sessionId: string) => { void _sessionId; return {path: '/tmp/project', outcome: 'AVAILABLE' as const, reason: null}; });
  restoreWorkspace = vi.fn(async (_sessionId: string) => { void _sessionId; return {path: '/tmp/project', outcome: 'AVAILABLE' as const, reason: null}; });
  searchSessions = vi.fn(async () => ({ items: this.sessions.map(session => ({ session, matchKind: 'recent' as const, snippet: '' })), nextCursor: null }));
  renameSession = vi.fn(async (sessionId: string, title: string) => {
    this.sessions = this.sessions.map(item => item.id === sessionId ? { ...item, title } : item);
    return { session_id: sessionId, title };
  });
  archived: SessionSummary[] = [];
  listArchivedSessions = vi.fn(async () => this.archived);
  archiveSession = vi.fn(async (sessionId: string) => {
    this.archived.push(...this.sessions.filter(s => s.id === sessionId).map(s => ({ ...s, lifecycle: 'ARCHIVED' as const, live: false })));
    this.sessions = this.sessions.filter(s => s.id !== sessionId);
    return { status: 'ARCHIVED' as const, session_id: sessionId };
  });
  unarchiveSession = vi.fn(async (sessionId: string) => {
    this.sessions.push(...this.archived.filter(s => s.id === sessionId).map(s => ({ ...s, lifecycle: 'OPEN' as const })));
    this.archived = this.archived.filter(s => s.id !== sessionId);
    return { status: 'OPEN' as const, session_id: sessionId };
  });
  deleteSession = vi.fn(async (sessionId: string) => {
    this.archived = this.archived.filter(s => s.id !== sessionId);
    this.sessions = this.sessions.filter(session => session.id !== sessionId);
    return { status: 'DELETED' as const, session_id: sessionId };
  });
  deleteArchivedSession = vi.fn<RuntimeAdapter['deleteArchivedSession']>(async (sessionId: string) => {
    if (this.sessions.some(session => session.id === sessionId)) return {status: 'NOT_ARCHIVED', session_id: sessionId, cleanup_warning: null};
    const found = this.archived.some(session => session.id === sessionId);
    this.archived = this.archived.filter(session => session.id !== sessionId);
    return {status: found ? 'DELETED' : 'ABSENT', session_id: sessionId, cleanup_warning: null};
  });
  forkConversation = vi.fn(async (_sessionId: string, _entryId: string): Promise<ForkOutcome> => {
    void _sessionId; void _entryId;
    const childId = 'session:matqlubh75wb5jgabb4tqlneye';
    this.sessions = [{ ...initialSession, id: childId }, ...this.sessions];
    return { outcome: 'CREATED_AND_OPENED' as const, child_session_id: childId };
  });
  readSession = vi.fn(async (id: string) => this.sessions.find(session => session.id === id) ?? null);
  memory = new LocalMemoryApi();
  sessions = [initialSession];
  modelConfigurations = [...bootstrap.model_configurations];
  lastConnection?: FakeConnection;
  connectionValue?: RuntimeProjection;
  interactionContent?: RuntimeInteractionContent;
  connectionRole: 'controller' | 'observer' = 'controller';
  queryCommandResult?: CommandReceipt;
  connectionValues = new Map<string, RuntimeProjection>();
  connectCalls: Array<{ sessionId: string; takeover: boolean }> = [];
  pickWorkspaceDirectory = vi.fn(async (): Promise<string | null> => '/tmp/project');
  completeWorkspacePaths = vi.fn(async () => ({ directory: '/tmp/project', items: [], next_cursor: null }));
  createSession = vi.fn(async (selection: SessionWorkspaceSelection, modelCallBinding?: ModelCallBindingPayload) => {
    const created: SessionSummary = {
      id: 'session:hrluejkx3v2ajzf4ec6bb3ierm',
      title: '新会话',
      subtitle: '刚刚创建',
      status: 'waiting',
      updatedAt: '刚刚',
      live: true,
      ...(modelCallBinding ? { modelCallBinding } : {}),
      workspace: selection.kind === 'quick'
        ? { id: 'quick-workspace', name: '快速开始', path: '/tmp/pulsara-quick', kind: 'quick' }
        : { id: 'project-workspace', name: 'project', path: selection.path, kind: 'project' },
    };
    this.sessions = [created, ...this.sessions];
    return created;
  });

  inputSuggestionsEnabled = true;

  async setInputSuggestionsEnabled(enabled: boolean) {
    this.inputSuggestionsEnabled = enabled;
    return { enabled };
  }

  async bootstrap() {
    return { ...bootstrap, local_settings: { ...bootstrap.local_settings, input_suggestions_enabled: this.inputSuggestionsEnabled }, model_configurations: this.modelConfigurations };
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
            reasoning_wire_profiles: ['catalog_standard', 'provider_default'],
          }],
        }],
      }],
    };
  }

  async localSettings() {
    return {
      local_settings: { ...bootstrap.local_settings, input_suggestions_enabled: this.inputSuggestionsEnabled },
      model_configurations: this.modelConfigurations,
      database_state: bootstrap.database_state,
    };
  }

  async addModelConfiguration() {
    return { model_configuration: bootstrap.model_configurations[0], wire_shape_warning: false };
  }

  async modelConfiguration(): Promise<import('../../frontend/lib/runtime-adapter').ModelConfigurationDetail> {
    throw new Error('unexpected model configuration read');
  }

  async updateModelConfiguration(): Promise<Awaited<ReturnType<RuntimeAdapter['updateModelConfiguration']>>> {
    throw new Error('unexpected model configuration update');
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
  async saveMemoryModel() { return bootstrap.local_settings.memory_retrieval; }
  async clearMemoryModel() { return bootstrap.local_settings.memory_retrieval; }
  async testMemoryModel() { return { status: 'ready' as const, elapsed_ms: 10 }; }
  async setHistoryEnabled() { return bootstrap.local_settings.memory_retrieval; }
  async setEmbeddingEnabled() { return bootstrap.local_settings.memory_retrieval; }
  async setMemoryRanking() { return bootstrap.local_settings.memory_retrieval; }

  async updateModelCallBinding(sessionId: string, binding: NonNullable<SessionSummary['modelCallBinding']>) {
    this.sessions = this.sessions.map((session) => session.id === sessionId
      ? { ...session, modelCallBinding: binding }
      : session);
    return { modelCallBinding: binding, reasoningPreferenceReset: false };
  }

  async reopenRuntime(_sessionId: string) {
    void _sessionId;
    return { status: 'reopened' as const };
  }

  listSessions = vi.fn(async () => this.sessions.map((session) => ({ ...session })));

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
      this.connectionValues.get(sessionId) ?? (sessionId === 'session:hrluejkx3v2ajzf4ec6bb3ierm'
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

// Session interaction tests explicitly enter the workbench from the overview.
function renderWorkbench(ui: Parameters<typeof render>[0]) {
  const view = render(ui);
  fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
  return view;
}

export { isWelcomeHeading, deferred, textPrompt, stubClipboard, typeComposer, composerIsDisabled, bootstrap, initialSession, SOURCE_FIDELITY_MARKDOWN, SOURCE_FIDELITY_USER_TEXT, capabilitySnapshot, userCapabilitySnapshot, projection, FakeConnection, FakeAdapter, renderWorkbench };
