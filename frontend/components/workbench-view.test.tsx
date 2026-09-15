import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { QueuedPrompt, QueuedPromptAction, ToolArtifactPage } from '../lib/runtime-adapter';
import { PromptDraftStore } from '../lib/prompt-draft';
import { promptContentTextProjection } from '../lib/prompt-content';
import { WorkbenchView } from './workbench-view';

let promptDraftStore: PromptDraftStore;
beforeEach(() => {
  promptDraftStore = new PromptDraftStore();
});
afterEach(() => {
  cleanup();
  promptDraftStore.destroy();
  vi.unstubAllGlobals();
});

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

describe('PR04 composer queue hard cut', () => {
  const item: QueuedPrompt = {
    queueItemId: 'source', commandId: 'submission', sequence: 1, status: 'pending',
    deliveryMode: 'new-turn', content: textPrompt('  original\n原文  '), permission: 'read-only',
    requestedPermission: 'ask-permissions',
  };
  const acceptedAction = (kind: QueuedPromptAction['kind']): QueuedPromptAction => ({
    sessionId: 'session-one', connectionGeneration: 1, commandId: 'action', kind,
    source: item,
    restoredContent: kind === 'edit' ? textPrompt(promptContentTextProjection(item.content)) : undefined,
    targetTurnId: kind === 'send' ? 'turn-one' : undefined,
    submittedAt: '2026-09-12T05:00:00Z', status: 'accepted', receipt: {
      commandId: 'action', status: kind === 'send' ? 'pending' : 'succeeded',
      promptDelivery: { queueItemId: kind === 'send' ? 'replacement' : 'source',
        queueStatus: kind === 'send' ? 'PENDING' : 'CANCELLED', deliveryMode: kind === 'send' ? 'steer' : 'new-turn' },
    },
  });

  it('keeps the source busy until accepted then shows one exact steer, replaced by its canonical entry', () => {
    const action = acceptedAction('send');
    const view = render(<WorkbenchView {...props({ queuedPrompts: [item], queueActions: [{ ...action, status: 'submitting' }] })} />);
    expect(screen.queryByRole('article', { name: '引导' })).toBeNull();
    const queue = screen.getByRole('region', { name: '等待处理的输入' });
    for (const name of ['发送', '编辑', '删除']) {
      expect((within(queue).getByRole('button', { name }) as HTMLButtonElement).disabled)
        .toBe(true);
    }
    view.rerender(<WorkbenchView {...props({ queuedPrompts: [item], queueActions: [action] })} />);
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
    expect(screen.getAllByRole('article', { name: '引导' })).toHaveLength(1);
    expect(view.container.querySelector('.user-steer .prompt-content-body')?.textContent)
      .toBe(promptContentTextProjection(item.content));
    expect(screen.queryByText(/模型已收到|模型已读/)).toBeNull();
    view.rerender(<WorkbenchView {...props({ queueActions: [action], messages: [{
      id: 'entry', turnId: 'turn-one', role: 'user', userKind: 'steer', body: promptContentTextProjection(item.content),
      time: '13:00', status: 'completed', inputSource: { commandId: 'action', queueItemId: 'replacement', deliveryMode: 'steer' },
    }] })} />);
    expect(screen.getAllByRole('article', { name: '引导' })).toHaveLength(1);
    expect(view.container.querySelector('[data-action-command-id]')).toBeNull();
  });

  it('does not deduplicate another command with identical text', () => {
    render(<WorkbenchView {...props({ queueActions: [acceptedAction('send')], messages: [{
      id: 'other-entry', turnId: 'turn-one', role: 'user', userKind: 'steer', body: promptContentTextProjection(item.content),
      time: '12:59', status: 'completed', inputSource: { commandId: 'other-command', queueItemId: 'other-queue', deliveryMode: 'steer' },
    }] })} />);
    expect(screen.getAllByRole('article', { name: '引导' })).toHaveLength(2);
  });

  it('refuses edit with any draft and preserves both texts without sending cancellation', () => {
    const onQueueAction = vi.fn(async () => undefined);
    render(<WorkbenchView {...props({ queuedPrompts: [item], onQueueAction })} />);
    act(() => promptDraftStore.insertText('session-one', 'existing draft'));
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    expect(onQueueAction).not.toHaveBeenCalled();
    expect(promptDraftStore.summary('session-one').text).toBe('existing draft');
    expect(screen.getByRole('region', { name: '等待处理的输入' })
      .querySelector('.prompt-content-body')?.textContent)
      .toBe(promptContentTextProjection(item.content));
  });

  it('restores the complete original and requested permission only after exact edit cancellation', async () => {
    const onPermissionChange = vi.fn();
    const action = acceptedAction('edit');
    const onQueueActionHandled = () => view.rerender(<WorkbenchView {...props({ queueActions: [{ ...action, handled: true }], onPermissionChange, onQueueActionHandled })} />);
    const view = render(<WorkbenchView {...props({ queuedPrompts: [item], queueActions: [{ ...action, status: 'submitting' }], onPermissionChange })} />);
    const input = screen.getByLabelText('发送给 Pulsara');
    expect(promptDraftStore.summary('session-one').text).toBe('');
    expect(input.getAttribute('contenteditable')).toBe('false');
    view.rerender(<WorkbenchView {...props({ queueActions: [action], onPermissionChange, onQueueActionHandled })} />);
    await waitFor(() => expect(promptDraftStore.summary('session-one').text)
      .toBe(promptContentTextProjection(item.content)));
    expect(onPermissionChange).toHaveBeenCalledWith('ask-permissions');
    await waitFor(() => expect(document.activeElement)
      .toBe(screen.getByLabelText('发送给 Pulsara')));
    const restoredEditor = promptDraftStore.getEditor('session-one');
    expect(restoredEditor.state.selection.from).toBe(
      restoredEditor.state.doc.content.size - 1,
    );
  });

  it('keeps a rejected delete in place and moves successful delete focus to the next row', async () => {
    const next = { ...item, queueItemId: 'next', commandId: 'next-command', sequence: 2 };
    const action = acceptedAction('delete');
    const view = render(<WorkbenchView {...props({ queuedPrompts: [item, next], queueActions: [{ ...action, status: 'rejected' }] })} />);
    expect(view.container.querySelectorAll('.composer-queue article')).toHaveLength(2);
    view.rerender(<WorkbenchView {...props({ queuedPrompts: [item, next], queueActions: [action] })} />);
    expect(view.container.querySelectorAll('.composer-queue article')).toHaveLength(1);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: '删除' })));
  });

  it('does not reuse a pending edit from another session', () => {
    render(<WorkbenchView {...props({ queueActions: [{ ...acceptedAction('edit'), sessionId: 'another-session' }] })} />);
    expect(promptDraftStore.summary('session-one').text).toBe('');
  });

  it('keeps an unexpected draft editable while retaining an accepted edit for later restoration', async () => {
    const view = render(<WorkbenchView {...props()} />);
    const input = screen.getByLabelText('发送给 Pulsara');
    act(() => promptDraftStore.insertText('session-one', 'another draft'));
    // A restored connection can reveal an accepted edit after a local draft exists.
    const action = acceptedAction('edit');
    const onQueueActionHandled = vi.fn();
    view.rerender(<WorkbenchView {...props({ queueActions: [action], onQueueActionHandled })} />);
    expect(input.getAttribute('contenteditable')).toBe('true');
    expect(promptDraftStore.summary('session-one').text).toBe('another draft');
    expect(screen.getByRole('region', { name: '等待处理的输入' })
      .querySelector('.prompt-content-body')?.textContent)
      .toBe(promptContentTextProjection(item.content));
    expect(screen.getByRole('status').textContent).toContain('请先处理当前草稿');
    expect(onQueueActionHandled).not.toHaveBeenCalled();
    const current = await promptDraftStore.capture('session-one');
    act(() => { promptDraftStore.clearIfSnapshot('session-one', current); });
    await waitFor(() => expect(promptDraftStore.summary('session-one').text)
      .toBe(promptContentTextProjection(item.content)));
    expect(onQueueActionHandled).toHaveBeenCalledWith(action.commandId);
  });
  it.each([{}, { metaKey: true }, { ctrlKey: true }])(
    'keeps modified Enter on the existing new-turn submit path %j',
    async (modifiers) => {
    const onSend = vi.fn(async () => true);
    render(<WorkbenchView {...props({ onSend })} />);
    act(() => promptDraftStore.insertText('session-one', '  exact\ninput  '));
    fireEvent.keyDown(screen.getByLabelText('发送给 Pulsara'), { key: 'Enter', ...modifiers });
    await waitFor(() => expect(onSend).toHaveBeenCalledWith(
      textPrompt('  exact\ninput  '), 'read-only', false,
    ));
  });

  it('places exact duplicate inputs in composer with three accessible flat actions', () => {
    const queuedPrompts = ['one', 'two'].map((id, index) => ({
      queueItemId: id, commandId: `command-${id}`, sequence: index + 1,
      status: 'pending' as const, deliveryMode: 'new-turn' as const,
      content: textPrompt('same\n原文'), permission: 'read-only' as const,
    }));
    const view = render(<WorkbenchView {...props({ queuedPrompts, queuedCount: 2 })} />);
    const queue = screen.getByRole('region', { name: '等待处理的输入' });
    expect(queue.closest('.composer-wrap')).toBeTruthy();
    expect(view.container.querySelector('.thread-scroll .prompt-queue')).toBeNull();
    expect(within(queue).getAllByRole('button', { name: '发送' })).toHaveLength(2);
    expect(within(queue).getAllByRole('button', { name: '编辑' })).toHaveLength(2);
    expect(within(queue).getAllByRole('button', { name: '删除' })).toHaveLength(2);
    expect(queue.querySelectorAll('button svg')).toHaveLength(6);
  });
});

const model = {
  id: 'model-one',
  source: 'models_dev' as const,
  route_id: 'test',
  route_name: 'Test',
  wire_api: 'openai_responses' as const,
  model_id: 'test-model',
  display_name: 'test-model',
  base_url: 'http://localhost',
  status: 'ready' as const,
  authentication: 'none' as const,
  credential_configured: true,
  reasoning: { kind: 'provider_default' as const },
  default_reasoning: null,
};

function props(overrides: Partial<ComponentProps<typeof WorkbenchView>> = {}): ComponentProps<typeof WorkbenchView> {
  return {
    workspace: { id: 'workspace-one', name: 'PR03', path: '/tmp/pr03', kind: 'project' },
    session: {
      id: 'session-one', title: 'PR03 会话', subtitle: '', status: 'running', updatedAt: '刚刚', live: true,
      modelCallBinding: { connection_id: model.id, reasoning: null },
    },
    messages: [],
    activePlanMode: false,
    isRunning: true,
    inspectorOpen: false,
    queuedCount: 0,
    queuedPrompts: [],
    localSubmissions: [],
    queueActions: [],
    onQueueAction: vi.fn(async () => undefined),
    onQueueActionHandled: vi.fn(),
    runtimeStatus: 'online',
    canCreateSession: true,
    modelConfigurations: [model],
    modelCallBinding: { connection_id: model.id, reasoning: null },
    canControl: true,
    isObserver: false,
    permission: 'read-only',
    skills: [],
    focusTaskRevision: 0,
    focusTaskHighlighted: false,
    onFork: vi.fn(async () => undefined),
    onReconnect: vi.fn(),
    onTakeControl: vi.fn(),
    onOpenSidebar: vi.fn(),
    onNewSession: vi.fn(),
    onToggleInspector: vi.fn(),
    onOpenModelSettings: vi.fn(),
    onModelCallBindingChange: vi.fn(async () => undefined),
    onSend: vi.fn(async () => true),
    onStop: vi.fn(),
    onCompact: vi.fn(async () => undefined),
    onReadInteraction: vi.fn(),
    onResolveInteraction: vi.fn(),
    artifactOwnerKey: 'session-one:host-one:entry-result',
    onReadToolArtifact: vi.fn(),
    onReadPromptImage: vi.fn(async () => new Uint8Array()),
    promptDraftStore,
    onNotify: vi.fn(),
    onPermissionChange: vi.fn(),
    ...overrides,
  };
}

describe('WorkbenchView PR03 control and raw-result contract', () => {
  it('keeps the bounded editor, TODO, and latest controls in one composer frame', async () => {
    let composerTop = 650;
    const rect = (top: number, height: number): DOMRect => ({
      x: 0, y: top, top, bottom: top + height, left: 0, right: 800,
      width: 800, height, toJSON: () => ({}),
    });
    const geometry = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockImplementation(function box(this: HTMLElement) {
        if (this.classList.contains('workbench')) return rect(0, 800);
        if (this.classList.contains('composer-wrap')) return rect(composerTop, 800 - composerTop);
        if (this.classList.contains('todo-dock__trigger')) return rect(composerTop - 16, 31);
        if (this.classList.contains('todo-dock__popover')) return rect(composerTop - 180, 164);
        return rect(0, 0);
      });
    try {
      const view = render(<WorkbenchView {...props({
        todo: {
          id: 'todo-one',
          items: [{ id: 'todo-one:0', label: '验证输入框高度', status: 'in-progress' }],
        },
      })} />);
      const composer = screen.getByLabelText('发送给 Pulsara');
      const thread = view.container.querySelector('.thread-scroll') as HTMLDivElement;
      Object.defineProperties(thread, {
        scrollHeight: { configurable: true, value: 1200 },
        clientHeight: { configurable: true, value: 400 },
        scrollTop: { configurable: true, writable: true, value: 0 },
      });
      fireEvent.scroll(thread);

      act(() => promptDraftStore.insertText('session-one', '第一行\n第二行'));
      expect(composer.classList.contains('composer-prosemirror')).toBe(true);
      const todo = screen.getByRole('button', { name: '收起TODO清单' });
      expect(todo.closest('.composer-frame')).toBe(composer.closest('.composer-frame'));
      const latest = await screen.findByRole('button', { name: '回到最新' });
      await waitFor(() => expect(latest.style.bottom).toBe('342px'));

      composerTop = 530;
      expect(composer.closest('.prompt-composer')).toBeTruthy();
    } finally {
      geometry.mockRestore();
    }
  });

  it('labels STOP narrowly and preserves the existing producer/observer split', () => {
    const stop = vi.fn();
    const view = render(<WorkbenchView {...props({ onStop: stop })} />);
    const button = screen.getByRole('button', { name: '停止本轮运行' });
    expect(button.getAttribute('title')).toContain('子任务、排队输入和后台命令不会自动取消');
    fireEvent.click(button);
    expect(stop).toHaveBeenCalledTimes(1);

    view.rerender(<WorkbenchView {...props({ isObserver: true, canControl: false, onStop: stop })} />);
    expect(screen.queryByRole('button', { name: '停止本轮运行' })).toBeNull();
    expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
    expect(screen.getByText('这个会话正在另一个窗口中操作')).toBeTruthy();
  });

  it('renders current, previous, and earlier ROOT completion acceptance precisely', () => {
    render(<WorkbenchView {...props({
      isRunning: false,
      messages: [{
        id: 'accepted-current', role: 'user', userKind: 'subagent-completion', time: '18:01', body: '',
        sourceSubagentTaskId: 'task-current', sourceSubagentLabel: 'builder', sourceSubagentRelation: 'current',
      }, {
        id: 'accepted-previous', role: 'user', userKind: 'subagent-completion', time: '18:02', body: '',
        sourceSubagentTaskId: 'task-previous', sourceSubagentLabel: 'reader', sourceSubagentRelation: 'previous',
      }, {
        id: 'accepted-earlier', role: 'user', userKind: 'subagent-completion', time: '18:03', body: '',
        sourceSubagentTaskId: 'task-earlier', sourceSubagentLabel: 'reviewer', sourceSubagentRelation: 'earlier',
      }],
    })} />);

    expect(screen.getByLabelText('builder 的结果已加入本轮对话')).toBeTruthy();
    expect(screen.getByLabelText('上一轮 reader 的结果已加入本轮对话')).toBeTruthy();
    expect(screen.getByLabelText('此前 reviewer 的结果已加入本轮对话')).toBeTruthy();
    expect(screen.queryByText(/主任务会结合|用于当前处理|模型已收到/)).toBeNull();
  });

  it('keeps exact raw text/copy, exact edit diff, and paginates the retained artifact', async () => {
    const raw = '{"diff":"--- a/file\\n+++ b/file\\n@@ -1 +1 @@\\n-old\\n+new","literal":"N| 不清洗"}';
    const writeText = vi.fn(async () => undefined);
    stubClipboard(writeText);
    const pages: ToolArtifactPage[] = [{
      resultEntryId: 'entry-result', text: 'artifact-page-one', offsetChars: 0,
      returnedChars: 17, totalChars: 34, nextOffsetChars: 17, hasMore: true,
    }, {
      resultEntryId: 'entry-result', text: 'artifact-page-two', offsetChars: 17,
      returnedChars: 17, totalChars: 34, hasMore: false,
    }];
    const readArtifact = vi.fn(async (_entryId: string, offset: number) => pages[offset === 0 ? 0 : 1]!);
    render(<WorkbenchView {...props({
      isRunning: false,
      messages: [{
        id: 'assistant-one', role: 'assistant', time: '现在', body: '文件已处理。', status: 'completed',
        traces: [{
          id: 'trace-edit', kind: 'edit', toolName: 'edit_file', title: '更新文件', subtitle: '已完成',
          status: 'completed', meta: '操作完成', resultText: raw, resultEntryId: 'entry-result',
          artifact: { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'HEAD_TAIL' },
        }],
      }],
      onReadToolArtifact: readArtifact,
    })} />);

    fireEvent.click(screen.getByRole('button', { name: '展开工具详情：edit_file' }));
    const rawRegion = screen.getByRole('region', { name: '工具原始结果' });
    expect(within(rawRegion).getByText(raw).textContent).toBe(raw);
    expect(screen.getByLabelText('文件差异').textContent).toBe('--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new');
    fireEvent.click(within(rawRegion).getByRole('button', { name: '复制工具原始结果' }));
    expect(writeText).toHaveBeenCalledWith(raw);

    fireEvent.click(screen.getByRole('button', { name: '查看完整输出' }));
    expect(await screen.findByText('artifact-page-one')).toBeTruthy();
    expect(readArtifact).toHaveBeenCalledWith('entry-result', 0);
    fireEvent.click(screen.getByRole('button', { name: '读取下一页' }));
    expect(await screen.findByText('artifact-page-two')).toBeTruthy();
    expect(readArtifact).toHaveBeenCalledWith('entry-result', 17);
    expect(screen.getByRole('status', { name: '完整输出读取状态' }).textContent).toContain('已到末页');
  });

  it('does not apply an artifact page after the whole tool card is collapsed', async () => {
    let resolve!: (page: ToolArtifactPage) => void;
    const pending = new Promise<ToolArtifactPage>((accept) => { resolve = accept; });
    const readArtifact = vi.fn(async () => pending);
    render(<WorkbenchView {...props({
      isRunning: false,
      messages: [{
        id: 'assistant-one', role: 'assistant', time: '现在', body: '完成。', status: 'completed',
        traces: [{
          id: 'trace-tool', kind: 'mcp', toolName: 'mcp_unknown', title: '未知工具', subtitle: '已完成',
          status: 'completed', resultText: '{"value":true}', resultEntryId: 'entry-result',
          artifact: { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'HEAD_TAIL' },
        }],
      }],
      onReadToolArtifact: readArtifact,
    })} />);
    fireEvent.click(screen.getByRole('button', { name: '展开工具详情：mcp_unknown' }));
    fireEvent.click(screen.getByRole('button', { name: '查看完整输出' }));
    fireEvent.click(screen.getByRole('button', { name: '收起工具详情：mcp_unknown' }));
    resolve({
      resultEntryId: 'entry-result', text: 'late-artifact-page', offsetChars: 0,
      returnedChars: 18, totalChars: 18, hasMore: false,
    });
    await Promise.resolve();
    fireEvent.click(screen.getByRole('button', { name: '展开工具详情：mcp_unknown' }));
    await waitFor(() => expect(readArtifact).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('late-artifact-page')).toBeNull();
  });
});
