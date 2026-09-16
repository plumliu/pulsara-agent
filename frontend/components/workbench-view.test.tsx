import { ToolResultDisplayContext } from '../lib/tool-result-display';
import type { ReactElement, PropsWithChildren } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { QueuedPrompt, QueuedPromptAction, ToolArtifactPage } from '../lib/runtime-adapter';
import { PromptDraftStore } from '../lib/prompt-draft';
import { promptContentTextProjection } from '../lib/prompt-content';
import { ConversationMessages, WorkbenchView } from './workbench-view';

function renderWithRawResults(ui: ReactElement) {
  return render(ui, { wrapper: ({ children }: PropsWithChildren) => (
    <ToolResultDisplayContext.Provider value={{ showBuiltinToolResults: true, onChange: () => {} }}>
      {children}
    </ToolResultDisplayContext.Provider>
  ) });
}

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

  it('keeps the same compact card when a local submission is accepted into the queue', () => {
    const view = render(<WorkbenchView {...props({ localSubmissions: [{
      sessionId: 'session-one', connectionGeneration: 1, commandId: item.commandId,
      content: item.content, deliveryMode: 'new-turn', status: 'sending',
      targetTurnId: 'internal-turn-id', permission: 'read-only', detail: '正在核对投递状态',
    }] })} />);
    const queue = screen.getByRole('region', { name: '等待处理的输入' });
    const card = queue.querySelector('article');
    const content = queue.querySelector('.queued-prompt-content');
    expect(queue.textContent).not.toMatch(/适用权限|目标轮次|internal-turn-id|正在核对投递状态|正在加入/);
    expect((within(queue).getByRole('button', { name: '编辑' }) as HTMLButtonElement).disabled).toBe(true);
    view.rerender(<WorkbenchView {...props({ queuedPrompts: [item] })} />);
    expect(queue.querySelector('article')).toBe(card);
    expect(queue.querySelector('.queued-prompt-content')).toBe(content);
    expect((within(queue).getByRole('button', { name: '编辑' }) as HTMLButtonElement).disabled).toBe(false);
    expect(queue.querySelector('.prompt-content-body')?.textContent).toBe(promptContentTextProjection(item.content));
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

  it.each([true, false])('animates editor shrink only after an accepted submission: %s', async (accepted) => {
    let settle!: (accepted: boolean) => void;
    const onSend = vi.fn(() => new Promise<boolean>(resolve => { settle = resolve; }));
    const view = render(<WorkbenchView {...props({ onSend })} />);
    act(() => promptDraftStore.insertText('session-one', '第一行\n第二行\n第三行'));
    const editor = view.container.querySelector('.composer-editor') as HTMLElement;
    editor.getBoundingClientRect = () => new DOMRect(0, 0, 720,
      promptDraftStore.summary('session-one').hasContent ? 140 : 52);
    const animate = vi.fn(() => ({ cancel: vi.fn() }) as unknown as Animation);
    editor.animate = animate;
    fireEvent.keyDown(screen.getByLabelText('发送给 Pulsara'), { key: 'Enter' });
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(animate).not.toHaveBeenCalled();
    expect(promptDraftStore.summary('session-one').hasContent).toBe(true);
    await act(async () => settle(accepted));
    expect(promptDraftStore.summary('session-one').hasContent).toBe(!accepted);
    if (accepted) {
      expect(animate).toHaveBeenCalledExactlyOnceWith([
        { height: '140px', overflow: 'hidden' },
        { height: '52px', overflow: 'hidden' },
      ], { duration: 240, easing: 'cubic-bezier(.2, .7, .2, 1)' });
    } else expect(animate).not.toHaveBeenCalled();
  });

  it.each([false, true])('restores Enter focus without stealing a later focus choice: %s', async (movedFocus) => {
    let accept!: (accepted: boolean) => void;
    const onSend = vi.fn(() => new Promise<boolean>(resolve => { accept = resolve; }));
    render(<WorkbenchView {...props({ onSend })} />);
    // Restored drafts can have revision zero, just like the replacement editor.
    act(() => promptDraftStore.restoreIfEmpty('session-one', textPrompt('继续测试焦点')));
    const oldEditor = screen.getByLabelText('发送给 Pulsara');
    act(() => oldEditor.focus());
    fireEvent.keyDown(oldEditor, { key: 'Enter' });
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    const otherControl = screen.getByRole('button', { name: '切换检查器' });
    if (movedFocus) act(() => otherControl.focus());
    await act(async () => accept(true));
    const newEditor = screen.getByLabelText('发送给 Pulsara');
    expect(newEditor).not.toBe(oldEditor);
    expect(promptDraftStore.summary('session-one').hasContent).toBe(false);
    await waitFor(() => expect(document.activeElement).toBe(movedFocus ? otherControl : newEditor));
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

describe('empty session welcome composer', () => {
  it('disables compaction for an empty session even with an unsent draft, but allows existing context', () => {
    const input = props({ isRunning: false });
    const view = render(<WorkbenchView {...input} />);
    const compact = () => screen.getByRole('button', { name: '压缩上下文' }) as HTMLButtonElement;
    expect(compact().disabled).toBe(true);
    act(() => promptDraftStore.insertText('session-one', '尚未发送'));
    expect(compact().disabled).toBe(true);
    fireEvent.click(compact());
    expect(input.onCompact).not.toHaveBeenCalled();
    view.rerender(<WorkbenchView {...input} messages={[{ id: 'user-one', role: 'user', body: '已发送', time: '现在' }]} />);
    expect(compact().disabled).toBe(false);
    view.rerender(<WorkbenchView {...input} initialContextBase={{ base_kind: 'SNAPSHOT', display_after_entry_sequence: 0 }} />);
    expect(compact().disabled).toBe(false);
    view.rerender(<WorkbenchView {...input} isRunning />);
    expect(compact().disabled).toBe(false);
    view.rerender(<WorkbenchView {...input} isRunning runtimeStatus="offline" />);
    expect(compact().disabled).toBe(true);
  });

  it('keeps the same focused editor when the first send opens the drawer, and retains a rejected draft', async () => {
    let settle!: (accepted: boolean) => void;
    const onSend = vi.fn(() => new Promise<boolean>(resolve => { settle = resolve; }));
    const view = render(<WorkbenchView {...props({ isRunning: false, onSend,
      initialContextBase: { base_kind: 'FULL_HISTORY', display_after_entry_sequence: 0 },
    })} />);
    expect(screen.getByRole('heading', { name: '有什么想做的？' })).toBeTruthy();
    const drawer = view.container.querySelector('.composer-drawer')!;
    expect(drawer.hasAttribute('inert')).toBe(true);
    const wrap = view.container.querySelector('.composer-wrap') as HTMLElement;
    wrap.getBoundingClientRect = () => new DOMRect(0,
      view.container.querySelector('.is-welcome') ? 280 : 650, 720, 70);
    const animate = vi.fn(() => ({ cancel: vi.fn() }) as unknown as Animation);
    wrap.animate = animate;
    act(() => promptDraftStore.insertText('session-one', '从这里开始'));
    const editor = screen.getByLabelText('发送给 Pulsara');
    act(() => editor.focus());
    expect(document.activeElement).toBe(editor);
    fireEvent.keyDown(editor, { key: 'Enter' });
    await waitFor(() => expect(onSend).toHaveBeenCalledOnce());
    expect(screen.getByLabelText('发送给 Pulsara')).toBe(editor);
    expect(editor.contains(document.activeElement)).toBe(true);
    expect(view.container.querySelector('.is-welcome')).toBeNull();
    expect(drawer.hasAttribute('inert')).toBe(false);
    expect(screen.queryByRole('heading', { name: '有什么想做的？' })).toBeNull();
    expect(animate).toHaveBeenCalledWith([
      { transform: 'translateY(-370px)' }, { transform: 'translateY(0)' },
    ], expect.objectContaining({ duration: 560 }));
    await act(async () => settle(false));
    expect(promptDraftStore.summary('session-one').text).toBe('从这里开始');
  });

  it('keeps options collapsed and gently guides missing model selection in two steps', () => {
    const view = render(<WorkbenchView {...props({ isRunning: false })} />);
    expect(screen.getByRole('button', { name: '输入选项' }).classList.contains('needs-selection')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '输入选项' }));
    expect(view.container.querySelector('.composer-drawer')?.hasAttribute('inert')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '输入选项' }));
    expect(view.container.querySelector('.composer-drawer')?.hasAttribute('inert')).toBe(true);
    view.rerender(<WorkbenchView {...props({ isRunning: false, modelCallBinding: null })} />);
    expect(view.container.querySelector('.composer-drawer')?.hasAttribute('inert')).toBe(true);
    const options = screen.getByRole('button', { name: '输入选项' });
    expect(options.classList.contains('needs-selection')).toBe(true);
    expect(view.container.querySelector('.welcome-heading__brand')).toBeNull();
    expect(view.container.querySelector('.composer-note')).toBeNull();
    fireEvent.click(options);
    expect(view.container.querySelector('.composer-drawer')?.hasAttribute('inert')).toBe(false);
    expect(options.classList.contains('needs-selection')).toBe(false);
    const model = screen.getByRole('button', { name: /选择模型/ });
    expect(model.classList.contains('needs-selection')).toBe(true);
    fireEvent.click(model);
    expect(model.classList.contains('needs-selection')).toBe(false);
    expect(model.getAttribute('aria-expanded')).toBe('true');
    view.rerender(<WorkbenchView {...props({ isRunning: false })} />);
    expect(view.container.querySelector('.needs-selection')).toBeNull();
  });

  it.each(['click', 'enter'])('opens options and model selection instead of submitting an unconfigured draft via %s', async (method) => {
    const input = props({ isRunning: false, modelCallBinding: null });
    const view = render(<WorkbenchView {...input} />);
    act(() => promptDraftStore.insertText('session-one', '先选择模型'));
    const send = screen.getByRole('button', { name: '发送' }) as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    if (method === 'click') fireEvent.click(send);
    else fireEvent.keyDown(screen.getByLabelText('发送给 Pulsara'), { key: 'Enter' });
    expect(view.container.querySelector('.composer-drawer')?.hasAttribute('inert')).toBe(false);
    expect(screen.getByRole('button', { name: /选择模型/ }).getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('heading', { name: '有什么想做的？' })).toBeTruthy();
    expect(promptDraftStore.summary('session-one').text).toBe('先选择模型');
    expect(input.onSend).not.toHaveBeenCalled();
    expect(input.onNotify).not.toHaveBeenCalled();
  });

  it('uses welcome only for empty sessions, not running or inherited conversations', () => {
    const view = render(<WorkbenchView {...props({ isRunning: false })} />);
    expect(view.container.querySelector('.is-welcome')).toBeTruthy();
    view.rerender(<WorkbenchView {...props()} />);
    expect(view.container.querySelector('.is-welcome')).toBeNull();
    view.rerender(<WorkbenchView {...props({ isRunning: false,
      initialContextBase: { base_kind: 'SNAPSHOT', display_after_entry_sequence: 0 },
    })} />);
    expect(view.container.querySelector('.is-welcome')).toBeNull();
  });
});

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

  it('hides builtin raw previews and artifact pages by default, retaining diffs and commands', async () => {
    const readArtifact = vi.fn(async () => ({ resultEntryId: 'result', text: 'retained output',
      offsetChars: 0, returnedChars: 15, totalChars: 15, hasMore: false }));
    const viewProps = props({ isRunning: true, onReadToolArtifact: readArtifact, messages: [{
      id: 'assistant', role: 'assistant', time: '现在', body: '', status: 'running', traces: [{
        id: 'edit', kind: 'edit', toolName: 'edit_file', title: '更新文件', subtitle: 'file',
        status: 'completed', resultText: JSON.stringify({ diff: '-old\n+new', secret: 'raw body' }),
        resultSummary: 'raw body', resultEntryId: 'result',
        artifact: { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'HEAD_TAIL' },
      }, {
        id: 'terminal', kind: 'terminal', toolName: 'terminal', title: '运行命令', subtitle: 'pwd',
        status: 'completed', command: 'pwd', resultText: 'raw stdout', resultSummary: 'raw stdout',
      }, {
        id: 'read', kind: 'read', toolName: 'read_file', title: '读取文件', subtitle: 'file',
        status: 'completed', resultText: 'raw file', resultSummary: 'raw file',
      }],
    }] });
    const display = (show: boolean) => <ToolResultDisplayContext.Provider value={{ showBuiltinToolResults: show, onChange: () => {} }}><WorkbenchView {...viewProps} /></ToolResultDisplayContext.Provider>;
    const view = render(display(false));
    const titles = [...view.container.querySelectorAll('.trace-summary-title strong')].map(node => node.textContent);
    expect(titles).toEqual(['修改文件', '运行命令', '读取文件']);
    fireEvent.click(screen.getByRole('button', { name: '展开工具详情：edit_file' }));
    fireEvent.click(screen.getByRole('button', { name: '展开工具详情：terminal' }));
    expect(screen.getByLabelText('文件差异').textContent).toBe('-old\n+new');
    expect(view.container.querySelector('.terminal-command')?.textContent).toContain('pwd');
    expect(screen.queryByRole('button', { name: '展开工具详情：read_file' })).toBeNull();
    expect(screen.queryByText('raw stdout')).toBeNull();
    expect(screen.queryByText('raw body')).toBeNull();
    expect(screen.queryByRole('region', { name: '工具原始结果' })).toBeNull();
    expect(screen.queryByRole('button', { name: '查看完整输出' })).toBeNull();
    expect(readArtifact).not.toHaveBeenCalled();

    view.rerender(display(true));
    expect(screen.getAllByRole('region', { name: '工具原始结果' })).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: '查看完整输出' }));
    expect(await screen.findByText('retained output')).toBeTruthy();
    view.rerender(display(false));
    expect(screen.queryByText('retained output')).toBeNull();
    expect(screen.queryByRole('region', { name: '完整工具输出' })).toBeNull();
    expect(screen.getByLabelText('文件差异')).toBeTruthy();
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
    renderWithRawResults(<WorkbenchView {...props({
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

    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
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
          id: 'trace-tool', kind: 'mcp', toolName: 'mcp__example__unknown', title: '未知工具', subtitle: '已完成',
          status: 'completed', resultText: '{"value":true}', resultEntryId: 'entry-result',
          artifact: { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'HEAD_TAIL' },
        }],
      }],
      onReadToolArtifact: readArtifact,
    })} />);
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    fireEvent.click(screen.getByRole('button', { name: '展开工具详情：mcp__example__unknown' }));
    fireEvent.click(screen.getByRole('button', { name: '查看完整输出' }));
    fireEvent.click(screen.getByRole('button', { name: '收起工具详情：mcp__example__unknown' }));
    resolve({
      resultEntryId: 'entry-result', text: 'late-artifact-page', offsetChars: 0,
      returnedChars: 18, totalChars: 18, hasMore: false,
    });
    await Promise.resolve();
    fireEvent.click(screen.getByRole('button', { name: '展开工具详情：mcp__example__unknown' }));
    await waitFor(() => expect(readArtifact).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('late-artifact-page')).toBeNull();
  });

  it('shows a large tool image only when expanded, without Figure links or raw details', () => {
    const readImage = vi.fn(async () => new Uint8Array([1, 2, 3]));
    render(<WorkbenchView {...props({
      isRunning: false,
      onReadPromptImage: readImage,
      messages: [{
        id: 'assistant-image', role: 'assistant', time: '现在', body: '', status: 'completed',
        traces: [{
          id: 'trace-image', kind: 'read', toolName: 'view_image', title: '读取图片',
          subtitle: '已完成', status: 'completed', resultText: '{"status":"image_attached"}',
          resultEntryId: 'entry-image', resultContent: { parts: [{
            type: 'image', source: 'canonical', digest: `sha256:${'a'.repeat(64)}`,
            encodedBytes: 3, mediaType: 'image/png', width: 12, height: 8,
            refOrdinal: 0, owner: { kind: 'entry', entryId: 'entry-image' },
          }] },
        }],
      }],
    })} />);

    expect(screen.queryByRole('region', { name: '工具读取的图片' })).toBeNull();
    expect(readImage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    fireEvent.click(screen.getByRole('button', { name: '展开工具详情：view_image' }));
    expect(screen.getByRole('region', { name: '工具读取的图片' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '放大图片' }).className).toBe('tool-image-preview');
    expect(screen.queryByText(/Figure/)).toBeNull();
    expect(screen.queryByRole('region', { name: '工具原始结果' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '收起工具详情：view_image' }));
    expect(screen.queryByRole('region', { name: '工具读取的图片' })).toBeNull();
  });
});

describe('completed reply process disclosure', () => {
  const progress = {
    id: 'progress', turnId: 'turn-one', entrySequence: 2, role: 'assistant' as const,
    assistantKind: 'tool-request' as const, time: '13:01', body: '先检查原始资料。',
    forkEligible: false, status: 'completed' as const,
    traces: [{ id: 'read-one', kind: 'terminal' as const, title: '读取文件', toolName: 'read_file', status: 'completed' as const, subtitle: '已读取', resultText: '文件内容'  }],
  };
  const steer = {
    id: 'steer', turnId: 'turn-one', entrySequence: 3, role: 'user' as const,
    userKind: 'steer' as const, time: '13:02', body: '请保留原来的配色。',
  };
  const final = {
    id: 'final', turnId: 'turn-one', entrySequence: 4, role: 'assistant' as const,
    assistantKind: 'terminal' as const, time: '13:03', body: '已完成，并保留原来的配色。',
    forkEligible: true, status: 'completed' as const,
  };
  const hiddenProgress = () => screen.getByText(progress.body).closest('.conversation-run__step')?.getAttribute('aria-hidden') === 'true';

  it('keeps streaming and steer in order, closes only on a confirmed final, and preserves manual expansion', () => {
    const view = renderWithRawResults(<WorkbenchView {...props({ messages: [progress, steer] })} />);
    expect(hiddenProgress()).toBe(false);
    const draft = { ...final, id: 'live-final', assistantKind: 'live' as const, status: 'running' as const, forkEligible: false };
    view.rerender(<WorkbenchView {...props({ messages: [progress, steer, draft] })} />);
    expect(hiddenProgress()).toBe(false);
    // A committed assistant text without terminal-final eligibility is not enough.
    view.rerender(<WorkbenchView {...props({ messages: [progress, steer, { ...final, forkEligible: false }] })} />);
    expect(hiddenProgress()).toBe(false);
    view.rerender(<WorkbenchView {...props({ isRunning: false, messages: [progress, steer, final] })} />);
    expect(hiddenProgress()).toBe(true);
    expect(screen.getByText(steer.body).closest('.conversation-run__step')?.getAttribute('aria-hidden')).toBe('false');
    expect(screen.getByText(progress.body).closest('[inert]')).toBeTruthy();
    expect(screen.getByText(steer.body).closest('[inert]')).toBeNull();
    expect(screen.getByText(final.body).closest('.conversation-run__step')).toBeNull();
    expect(screen.getByRole('button', { name: '复制回复' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '展开工具详情：read_file' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    expect(hiddenProgress()).toBe(false);
    expect(screen.getByRole('button', { name: '展开工具详情：read_file' })).toBeTruthy();
    const body = view.container.textContent!;
    expect(body.indexOf(progress.body)).toBeLessThan(body.indexOf(steer.body));
    expect(body.indexOf(steer.body)).toBeLessThan(body.indexOf(final.body));
    view.rerender(<WorkbenchView {...props({ isRunning: false, messages: [{ ...progress }, { ...steer }, { ...final }] })} />);
    expect(hiddenProgress()).toBe(false);
  });

  it.each(['full', 'summary'] as const)('loads history collapsed, preserves %s reasoning when expanded, and leaves standalone answers alone', (kind) => {
    const view = render(<WorkbenchView {...props({ isRunning: false, messages: [progress, {
      ...final, reasoning: [{ id: 'reason', kind, body: '核对完成。' }],
    }] })} />);
    expect(hiddenProgress()).toBe(true);
    expect(screen.queryByRole('button', { name: /展开思考/ })).toBeNull();
    expect(view.container.querySelectorAll('[data-memory-entry="final"]')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    fireEvent.click(screen.getByRole('button', { name: kind === 'summary' ? '展开思考摘要' : '展开思考' }));
    expect(screen.getByText('核对完成。').closest('.reasoning-row__body')).toBeTruthy();
    view.rerender(<WorkbenchView {...props({ isRunning: false, messages: [final] })} />);
    expect(screen.queryByRole('button', { name: /中间过程/ })).toBeNull();
    expect(screen.getByText(final.body)).toBeTruthy();
  });

  it('does not fold the next running turn with a previous completed one', () => {
    render(<WorkbenchView {...props({ messages: [progress, final,
      { id: 'next-user', turnId: 'turn-two', role: 'user', userKind: 'prompt', time: '13:04', body: '接着做' },
      { ...progress, id: 'next-progress', turnId: 'turn-two', body: '正在处理第二轮。' },
    ] })} />);
    expect(hiddenProgress()).toBe(true);
    expect(screen.getByText('正在处理第二轮。').closest('.conversation-run__step')?.getAttribute('aria-hidden')).toBe('false');
  });

  it('keeps interrupted history available without presenting an answer or a successful completion', () => {
    render(<WorkbenchView {...props({ isRunning: false, messages: [progress, steer] })} />);
    expect(hiddenProgress()).toBe(true);
    expect(screen.queryByText('处理过程')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    expect(hiddenProgress()).toBe(false);
    expect(screen.queryByRole('button', { name: '复制回复' })).toBeNull();
  });

  it('keeps compaction visible and opens a collapsed process for an explicit history location', () => {
    const common = {
      messages: [progress, steer, final], artifactOwnerKey: 'session-one',
      onReadToolArtifact: vi.fn(), onNotify: vi.fn(), contextCompactionIndex: 2,
    };
    const view = renderWithRawResults(<ConversationMessages {...common} />);
    expect(hiddenProgress()).toBe(true);
    expect(screen.getByRole('separator', { name: '上下文已压缩' })).toBeTruthy();
    expect(screen.getByText(final.body).closest('[hidden]')).toBeNull();
    view.rerender(<ConversationMessages {...common}
      focusMemoryEntry={{ sessionId: 'session-one', entryId: progress.id }} />);
    expect(hiddenProgress()).toBe(false);
    expect(screen.getByRole('button', { name: '展开工具详情：read_file' })).toBeTruthy();
  });

});
