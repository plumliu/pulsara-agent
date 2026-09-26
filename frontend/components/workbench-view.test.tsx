import { ToolResultDisplayContext } from '../lib/tool-result-display';
import type { ReactElement, PropsWithChildren } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { QueuedPrompt, QueuedPromptAction, ToolArtifactPage } from '../lib/runtime-adapter';
import { PromptDraftStore } from '../lib/prompt-draft';
import { promptContentTextProjection } from '../lib/prompt-content';
import { visualizationLayoutMessageType } from '../lib/visualization-frame';
import { ConversationMessages, WorkbenchView } from './workbench-view';
import { WELCOME_TYPEWRITER_PHRASES } from './welcome-typewriter';

const isWelcomeHeading = (name: string) => (WELCOME_TYPEWRITER_PHRASES as readonly string[]).includes(name);

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

describe('visualization occurrence layout', () => {
  it('crops only a measured root from its own iframe and falls back to the page', async () => {
    let notifyResize: ResizeObserverCallback | null = null;
    let availableWidth = 962;
    vi.stubGlobal('ResizeObserver', class {
      constructor(private readonly callback: ResizeObserverCallback) { notifyResize = callback; }
      observe() { this.callback([], this as unknown as ResizeObserver); }
      disconnect() {}
    });
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
      return this instanceof HTMLIFrameElement ? availableWidth - 2 : availableWidth;
    });
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this instanceof HTMLIFrameElement ? 560 : 0;
    });
    const html = '<!doctype html><html><body><main data-pulsara-visualization-root>Chart</main></body></html>';
    const read = vi.fn(async () => html);
    const view = render(<ConversationMessages
      messages={[{
        id: 'chart-answer', role: 'assistant', assistantKind: 'terminal', status: 'completed',
        time: '13:04', body: '图表如下。', visualizations: [{
          ordinal: 0, state: 'READY', visualizationRef: 'sha256:test', contentSize: html.length,
        }],
      }]}
      artifactOwnerKey="session-one" onReadToolArtifact={vi.fn()} onNotify={vi.fn()}
      onReadVisualization={read}
    />);
    const toggle = screen.getByRole('button', { name: '展开可视化 1' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(read).not.toHaveBeenCalled();
    expect(view.container.querySelector('iframe')).toBeNull();
    fireEvent.click(toggle);
    const panel = await waitFor(() => {
      const element = view.container.querySelector<HTMLElement>('.assistant-visualization[data-visualization-layout]');
      expect(element?.querySelector('iframe')).toBeTruthy();
      return element!;
    });
    const frame = panel.querySelector('iframe')!;
    await waitFor(() => expect(frame.style.width).toBe('960px'));
    const announce = (source: MessageEventSource, mode: string, rect?: object) => {
      act(() => window.dispatchEvent(new MessageEvent('message', {
        source, data: { type: visualizationLayoutMessageType, mode, rect },
      })));
    };
    announce(window, 'root', { x: 100, y: 20, width: 420, height: 300 });
    expect(panel.dataset.visualizationLayout).toBe('page');
    announce(frame.contentWindow!, 'root', { x: 100, y: 20, width: 420, height: 300 });
    expect(panel.dataset.visualizationLayout).toBe('root');
    expect(panel.style.width).toBe('422px');
    expect(frame.style.transform).toBe('translate(-100px, -20px)');
    fireEvent.click(screen.getByRole('button', { name: '收起可视化 1' }));
    expect(panel.classList.contains('is-expanded')).toBe(false);
    expect(panel.style.width).toBe('');
    expect(panel.querySelector('.animated-disclosure')?.getAttribute('aria-hidden')).toBe('true');
    expect(panel.querySelector('iframe')).toBe(frame);
    fireEvent.click(screen.getByRole('button', { name: '展开可视化 1' }));
    expect(panel.style.width).toBe('422px');
    expect(panel.querySelector('iframe')).toBe(frame);
    expect(read).toHaveBeenCalledOnce();
    availableWidth = 602;
    act(() => notifyResize?.([], {} as ResizeObserver));
    expect(panel.dataset.visualizationLayout).toBe('page');
    expect(frame.style.width).toBe('600px');
    announce(frame.contentWindow!, 'root', { x: 40, y: 20, width: 420, height: 300 });
    expect(panel.dataset.visualizationLayout).toBe('root');
    announce(frame.contentWindow!, 'root', { x: 100, y: 20, width: 900, height: 300 });
    expect(panel.dataset.visualizationLayout).toBe('page');
    announce(frame.contentWindow!, 'page');
    expect(panel.dataset.visualizationLayout).toBe('page');
  });
});
afterEach(() => {
  cleanup();
  promptDraftStore.destroy();
  vi.restoreAllMocks();
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
    onReopenRuntime: vi.fn(),
    runtimeReopenBusy: false,
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

describe('current-session runtime actions', () => {
  it('keeps runtime reopen in the header menu, separate from compaction', () => {
    const onReopenRuntime = vi.fn();
    render(<WorkbenchView {...props({ onReopenRuntime })} />);
    const trigger = screen.getByRole('button', { name: '更多会话操作' });
    expect(screen.getByRole('button', { name: '压缩上下文' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重新载入当前会话运行时' })).toBeNull();

    fireEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: /重新载入当前会话运行时/ }));
    expect(onReopenRuntime).toHaveBeenCalledOnce();
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
  });

  it('closes the runtime menu on Escape or outside click and disables it while reopening', () => {
    const view = render(<WorkbenchView {...props()} />);
    const trigger = screen.getByRole('button', { name: '更多会话操作' }) as HTMLButtonElement;
    fireEvent.click(trigger);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(trigger);

    fireEvent.click(trigger);
    fireEvent.pointerDown(document.body);
    expect(trigger.getAttribute('aria-expanded')).toBe('false');

    view.rerender(<WorkbenchView {...props({ runtimeReopenBusy: true })} />);
    expect(trigger.disabled).toBe(true);
  });
});

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

  it('keeps the same focused editor when the first send leaves welcome, and retains a rejected draft', async () => {
    let settle!: (accepted: boolean) => void;
    const onSend = vi.fn(() => new Promise<boolean>(resolve => { settle = resolve; }));
    const view = render(<WorkbenchView {...props({ isRunning: false, onSend,
      initialContextBase: { base_kind: 'FULL_HISTORY', display_after_entry_sequence: 0 },
    })} />);
    expect(screen.getByRole('heading', { name: isWelcomeHeading })).toBeTruthy();
    const wrap = view.container.querySelector('.composer-wrap') as HTMLElement;
    wrap.getBoundingClientRect = () => new DOMRect(0,
      view.container.querySelector('.is-welcome') ? 280 : 650, 720, 70);
    Object.defineProperty(wrap, 'offsetTop', { configurable: true,
      get: () => view.container.querySelector('.is-welcome') ? 280 : 650 });
    Object.defineProperty(view.container.querySelector('.workbench'), 'clientHeight', { configurable: true, value: 800 });
    const animate = vi.fn(() => ({ cancel: vi.fn() }) as unknown as Animation);
    wrap.animate = animate;
    act(() => promptDraftStore.insertText('session-one', '从这里开始'));
    const thread = view.container.querySelector('.thread-scroll') as HTMLElement;
    Object.defineProperties(thread, {
      scrollHeight: { configurable: true, value: 1200 },
      clientHeight: { configurable: true, value: 400 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    });
    fireEvent.scroll(thread);
    const latest = await screen.findByRole('button', { name: '回到最新' });
    await waitFor(() => expect(latest.style.bottom).toBe('532px'));
    const editor = screen.getByLabelText('发送给 Pulsara');
    act(() => editor.focus());
    expect(document.activeElement).toBe(editor);
    fireEvent.keyDown(editor, { key: 'Enter' });
    await waitFor(() => expect(onSend).toHaveBeenCalledOnce());
    expect(screen.getByLabelText('发送给 Pulsara')).toBe(editor);
    expect(editor.contains(document.activeElement)).toBe(true);
    expect(view.container.querySelector('.is-welcome')).toBeNull();
    // The composer moved without resizing; the old welcome offset must not linger.
    await waitFor(() => expect(latest.style.bottom).toBe('162px'));
    expect(screen.queryByRole('heading', { name: isWelcomeHeading })).toBeNull();
    expect(animate).toHaveBeenCalledWith([
      { transform: 'translateY(-370px)' }, { transform: 'translateY(0)' },
    ], expect.objectContaining({ duration: 560 }));
    await act(async () => settle(false));
    expect(promptDraftStore.summary('session-one').text).toBe('从这里开始');
  });

  it('offers model and permission directly in welcome and guides missing model selection', () => {
    const view = render(<WorkbenchView {...props({ isRunning: false })} />);
    expect(screen.getByRole('button', { name: 'test-model' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '只读' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '输入选项' })).toBeNull();
    view.rerender(<WorkbenchView {...props({ isRunning: false, modelCallBinding: null })} />);
    const model = screen.getByRole('button', { name: /选择模型/ });
    expect(model.classList.contains('needs-selection')).toBe(true);
    expect(view.container.querySelector('.composer-note')).toBeNull();
    fireEvent.click(model);
    expect(model.classList.contains('needs-selection')).toBe(false);
    expect(model.getAttribute('aria-expanded')).toBe('true');
    view.rerender(<WorkbenchView {...props({ isRunning: false })} />);
    expect(view.container.querySelector('.needs-selection')).toBeNull();
  });

  it.each(['click', 'enter'])('opens model selection instead of submitting an unconfigured draft via %s', async (method) => {
    const input = props({ isRunning: false, modelCallBinding: null });
    render(<WorkbenchView {...input} />);
    act(() => promptDraftStore.insertText('session-one', '先选择模型'));
    const send = screen.getByRole('button', { name: '发送' }) as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    if (method === 'click') fireEvent.click(send);
    else fireEvent.keyDown(screen.getByLabelText('发送给 Pulsara'), { key: 'Enter' });
    expect(screen.getByRole('button', { name: /选择模型/ }).getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('heading', { name: isWelcomeHeading })).toBeTruthy();
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
  it('positions latest above the composer, TODO and queue independently of welcome animation', async () => {
    let composerTop = 650;
    const visualOffset = -300;
    const offsetTop = vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get').mockImplementation(function(this: HTMLElement) {
      return this.classList.contains('composer-wrap') ? composerTop : 0;
    });
    const clientHeight = vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function(this: HTMLElement) {
      return this.classList.contains('workbench') ? 800 : 0;
    });
    const rect = (top: number, height: number): DOMRect => ({
      x: 0, y: top, top, bottom: top + height, left: 0, right: 800,
      width: 800, height, toJSON: () => ({}),
    });
    const geometry = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockImplementation(function box(this: HTMLElement) {
        if (this.classList.contains('workbench')) return rect(0, 800);
        if (this.classList.contains('composer-wrap')) return rect(composerTop + visualOffset, 800 - composerTop);
        if (this.classList.contains('todo-dock__trigger')) return rect(composerTop + visualOffset, 31);
        if (this.classList.contains('todo-dock__popover')) return rect(composerTop + visualOffset - 180, 164);
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

      fireEvent.click(todo);
      await waitFor(() => expect(latest.style.bottom).toBe('162px'));
      // Multiline draft growth changes the actual composer layout.
      composerTop = 530;
      fireEvent(window, new Event('resize'));
      await waitFor(() => expect(latest.style.bottom).toBe('282px'));
      view.rerender(<WorkbenchView {...props()} />);
      await waitFor(() => expect(latest.style.bottom).toBe('282px'));
      // Queue cards are inside the same composer wrapper and move its top.
      composerTop = 430;
      view.rerender(<WorkbenchView {...props({ queuedCount: 1, queuedPrompts: [{
        queueItemId: 'queued-one', commandId: 'queued-command', sequence: 1,
        status: 'pending', deliveryMode: 'new-turn', content: textPrompt('下一轮'), permission: 'read-only',
      }] })} />);
      await waitFor(() => expect(latest.style.bottom).toBe('382px'));
      composerTop = 720;
      view.rerender(<WorkbenchView {...props({ isObserver: true, canControl: false })} />);
      await waitFor(() => expect(latest.style.bottom).toBe('92px'));
    } finally {
      geometry.mockRestore();
      offsetTop.mockRestore();
      clientHeight.mockRestore();
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

  it('hides builtin raw previews and artifact pages by default, retaining diffs, commands, and terminal output', async () => {
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
    expect(screen.getByRole('region', { name: '命令输出' }).textContent).toContain('raw stdout');
    expect(screen.queryByRole('button', { name: '展开工具详情：read_file' })).toBeNull();
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

  it('shows a localized memory-relation tool card with a concise result', () => {
    const view = render(<WorkbenchView {...props({
      isRunning: false,
      messages: [{
        id: 'assistant-relation', role: 'assistant', time: '现在', body: '', status: 'completed',
        traces: [{
          id: 'relation', kind: 'artifact', toolName: 'mark_memory_relation',
          title: '使用工具', subtitle: '已完成', status: 'completed', resultState: 'SUCCESS',
          argumentsJson: JSON.stringify({ relation_kind: 'SUPERSEDES', source_memory_id: 'private-source', target_memory_id: 'private-target' }),
          resultText: JSON.stringify({ status: 'SAVED', relation_kind: 'SUPERSEDES', relation_id: 'private-relation' }),
        }],
      }],
    })} />);
    expect(view.container.querySelector('.trace-summary-title strong')?.textContent).toBe('标记记忆关系');
    expect(view.container.querySelector('.trace-summary-copy small')?.textContent).toBe('已标记取代关系');
    expect(view.container.querySelector('.trace-card')?.textContent).not.toMatch(/private-|mark_memory_relation/);
  });

  it('streams terminal output in an expanded card and unwraps only the final terminal envelope', () => {
    const terminalTrace = (resultText: string, status: 'running' | 'completed', resultState?: string) => ({
      id: 'terminal-stream', kind: 'terminal' as const, toolName: 'terminal', title: '运行命令',
      subtitle: 'printf', status, command: 'printf output', resultText, resultState,
    });
    const viewProps = (trace: ReturnType<typeof terminalTrace>) => props({
      isRunning: true,
      messages: [{
        id: 'assistant-terminal-stream', role: 'assistant' as const, time: '现在', body: '',
        status: 'running' as const, traces: [trace],
      }],
    });
    const display = (trace: ReturnType<typeof terminalTrace>) => (
      <ToolResultDisplayContext.Provider value={{ showBuiltinToolResults: false, onChange: () => {} }}>
        <WorkbenchView {...viewProps(trace)} />
      </ToolResultDisplayContext.Provider>
    );

    const view = render(display(terminalTrace('first', 'running')));
    fireEvent.click(screen.getByRole('button', { name: '展开工具详情：terminal' }));
    expect(screen.getByRole('region', { name: '命令输出' }).textContent).toContain('first');
    expect(screen.getByLabelText('命令仍在执行')).toBeTruthy();

    view.rerender(display(terminalTrace('first\nsecond', 'running')));
    const stream = screen.getByRole('region', { name: '命令输出' });
    expect(stream.textContent).toContain('first\nsecond');
    const scrollingOutput = stream.querySelector('pre')!;
    Object.defineProperties(scrollingOutput, {
      scrollHeight: { configurable: true, value: 1_000 },
      clientHeight: { configurable: true, value: 100 },
    });
    scrollingOutput.scrollTop = 0;
    view.rerender(display(terminalTrace('first\nsecond\nthird', 'running')));
    expect(scrollingOutput.scrollTop).toBe(1_000);

    scrollingOutput.scrollTop = 0;
    fireEvent.scroll(scrollingOutput);
    view.rerender(display(terminalTrace('first\nsecond\nthird\nfourth', 'running')));
    expect(scrollingOutput.scrollTop).toBe(0);

    const jsonPrintedByCommand = '{"exit_code":0,"output":"this is command output"}';
    view.rerender(display(terminalTrace(jsonPrintedByCommand, 'running')));
    expect(screen.getByRole('region', { name: '命令输出' }).textContent).toContain(jsonPrintedByCommand);

    const finalEnvelope = JSON.stringify({
      status: 'success', terminal_process_action: 'start', output: 'first\nsecond', exit_code: 0,
    });
    view.rerender(display(terminalTrace(finalEnvelope, 'completed', 'SUCCESS')));
    const output = screen.getByRole('region', { name: '命令输出' });
    expect(output.textContent).toContain('first\nsecond');
    expect(output.textContent).not.toContain('terminal_process_action');
    expect(screen.queryByLabelText('命令仍在执行')).toBeNull();
    expect(screen.queryByText('已转到后台运行')).toBeNull();
    expect(screen.queryByRole('region', { name: '工具原始结果' })).toBeNull();

    const backgroundEnvelope = JSON.stringify({
      status: 'running', terminal_process_action: 'start', output: 'first\nsecond',
      exit_code: -1, yielded_to_background: true,
    });
    view.rerender(display(terminalTrace(backgroundEnvelope, 'completed', 'SUCCESS')));
    const backgroundNotice = screen.getByText('已转到后台运行');
    expect(backgroundNotice.tagName).toBe('EM');
    expect(backgroundNotice.closest('[aria-label="命令输出"]')).toBeTruthy();
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
  it('keeps imported final replies visible without granting extra fork actions', () => {
    const { container } = render(<ConversationMessages
      messages={[
        { id: 'user-one', turnId: 'history-one', role: 'user', userKind: 'prompt', time: '09:37', body: 'hello?' },
        { id: 'tool-one', turnId: 'history-one', role: 'assistant', assistantKind: 'tool-request', time: '09:37', body: '', status: 'completed',
          traces: [{ id: 'read-one', kind: 'read', toolName: 'read_file', title: '读取文件', subtitle: '已完成', status: 'completed' }] },
        { id: 'final-one', turnId: 'history-one', role: 'assistant', assistantKind: 'terminal', entryOwnerKind: 'IMPORTED_HISTORY',
          rootFinal: true, forkEligible: false, time: '09:37', body: '历史最终回复', status: 'completed' },
        { id: 'user-two', turnId: 'history-two', role: 'user', userKind: 'prompt', time: '09:38', body: '继续' },
        { id: 'tool-two', turnId: 'history-two', role: 'assistant', assistantKind: 'tool-request', time: '09:38', body: '', status: 'completed',
          traces: [{ id: 'read-two', kind: 'read', toolName: 'read_file', title: '读取文件', subtitle: '已完成', status: 'completed' }] },
        { id: 'final-two', turnId: 'history-two', role: 'assistant', assistantKind: 'terminal', entryOwnerKind: 'IMPORTED_HISTORY',
          rootFinal: true, forkEligible: true, time: '09:38', body: '分叉锚点回复', status: 'completed' },
      ]}
      artifactOwnerKey="fork-session" onReadToolArtifact={vi.fn()} onNotify={vi.fn()}
    />);

    expect(screen.getAllByText('处理过程')).toHaveLength(2);
    expect(screen.queryByText('中间过程')).toBeNull();
    expect(screen.getByText('历史最终回复').closest('.conversation-run__step')).toBeNull();
    expect(screen.getByText('分叉锚点回复').closest('.conversation-run__step')).toBeNull();
    expect(container.querySelectorAll('.conversation-run__step[aria-hidden="true"]')).toHaveLength(2);
    expect(screen.getAllByRole('button', { name: '复制回复' })).toHaveLength(2);
    expect(screen.getAllByRole('button', { name: '从此处分叉' })).toHaveLength(1);
  });

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
    rootFinal: true, forkEligible: true, status: 'completed' as const,
  };
  const hiddenProgress = () => screen.getByText(progress.body).closest('.conversation-run__step')?.getAttribute('aria-hidden') === 'true';

  it('keeps streaming and steer in order, closes only on a confirmed final, and preserves manual expansion', () => {
    const view = renderWithRawResults(<WorkbenchView {...props({ messages: [progress, steer] })} />);
    expect(hiddenProgress()).toBe(false);
    const draft = { ...final, id: 'live-final', assistantKind: 'live' as const, status: 'running' as const, rootFinal: false, forkEligible: false };
    view.rerender(<WorkbenchView {...props({ messages: [progress, steer, draft] })} />);
    expect(hiddenProgress()).toBe(false);
    expect(screen.getByText(draft.body).closest('.assistant-markdown')?.classList.contains('assistant-markdown--pretty')).toBe(false);
    // A committed assistant text without terminal-final eligibility is not enough.
    view.rerender(<WorkbenchView {...props({ messages: [progress, steer, { ...final, rootFinal: false, forkEligible: false }] })} />);
    expect(hiddenProgress()).toBe(false);
    expect(screen.queryByRole('button', { name: '复制回复' })).toBeNull();
    view.rerender(<WorkbenchView {...props({ isRunning: false, messages: [progress, steer, final] })} />);
    expect(hiddenProgress()).toBe(true);
    expect(screen.getByText(final.body).closest('.assistant-markdown')?.classList.contains('assistant-markdown--pretty')).toBe(true);
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

describe('composer + menu', () => {
  it.each([false, true])('reserves queued edit against an open menu and late picker, directory=%s', async directory => {
    const upload = vi.fn(async () => ({ path: '/tmp/imports/id/report.pdf', name: 'report.pdf', bytes: 1, file_count: 1 }));
    promptDraftStore.setImporter(upload);
    const item: QueuedPrompt = { queueItemId: 'source', commandId: 'submitted', sequence: 1, status: 'pending', deliveryMode: 'new-turn', content: textPrompt('queued body'), permission: 'read-only' };
    const action: QueuedPromptAction = { commandId: 'edit-command', sessionId: 'session-one', connectionGeneration: 1, kind: 'edit', source: item, status: 'submitting', submittedAt: 'now' };
    const onNotify = vi.fn();
    const view = render(<WorkbenchView {...props({ queuedPrompts: [item], onNotify })} />);
    fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
    const input = screen.getByLabelText(directory ? '选择文件夹' : '选择文件');
    const pick = vi.spyOn(input, 'click');
    view.rerender(<WorkbenchView {...props({ queuedPrompts: [item], queueActions: [action], onNotify })} />);
    expect(screen.getByLabelText('发送给 Pulsara').getAttribute('contenteditable')).toBe('false');
    const menuItem = screen.getByRole('button', { name: directory ? '添加文件夹' : '添加文件' }) as HTMLButtonElement;
    expect(menuItem.disabled).toBe(true);
    fireEvent.click(menuItem);
    expect(pick).not.toHaveBeenCalled();
    const file = new File(['bytes'], 'report.pdf');
    Object.defineProperty(file, 'webkitRelativePath', { value: 'folder/report.pdf' });
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
      fireEvent.paste(screen.getByLabelText('发送给 Pulsara'), { clipboardData: { files: [file] } });
      fireEvent.drop(screen.getByLabelText('发送给 Pulsara'), { dataTransfer: { files: [file] } });
    });
    expect(promptDraftStore.summary('session-one').hasContent).toBe(false);
    expect(upload).not.toHaveBeenCalled();
    expect(onNotify).toHaveBeenCalledWith('文件未加入草稿', expect.any(String));
  });

  it('groups attachments, skills and planning and appends a picked file beyond the current cursor', async () => {
    promptDraftStore.setImporter(async (_session, files) => ({ path: `/tmp/imports/id/${files[0].name}`, name: files[0].name, bytes: 1, file_count: 1 }));
    const onSend = vi.fn(async () => true);
    const view = render(<WorkbenchView {...props({ isRunning: false, onSend })} />);
    const editor = promptDraftStore.getEditor('session-one');
    act(() => { promptDraftStore.insertText('session-one', 'before'); editor.commands.setTextSelection(2); });
    fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
    expect(screen.getByRole('button', { name: '添加文件' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '添加文件夹' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '选择技能' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '先规划' }));
    expect(view.container.querySelector('.composer-plan-status')?.textContent).toBe('本轮先规划');
    await act(async () => { fireEvent.change(screen.getByLabelText('选择文件'), { target: { files: [new File(['x'], 'report.pdf')] } }); });
    const content = (await promptDraftStore.capture('session-one')).content;
    expect(content.parts[0]).toMatchObject({ type: 'text', text: expect.stringMatching(/^before【本地文件/) });
    expect(screen.queryByRole('button', { name: '先规划' })).toBeNull();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })); });
    expect(onSend).toHaveBeenCalledWith(content, 'read-only', true);
    expect(view.container.querySelector('.composer-plan-status')).toBeNull();
  });
});

describe('workbench file drop boundary', () => {
  const transfer = (files: File[] = []) => ({ types: ['Files'], files, items: [], getData: () => '', dropEffect: 'none', effectAllowed: 'all' });
  const importFile = async (_session: string, files: readonly File[]) => ({ path: `/tmp/imports/${files[0].name}`, name: files[0].name, bytes: files[0].size, file_count: 1 });

  it.each([
    { insideEditor: false, mixed: false }, { insideEditor: true, mixed: false },
    { insideEditor: false, mixed: true }, { insideEditor: true, mixed: true },
  ])('rejects a directory drop once, before importing any entries: %j', async ({ insideEditor, mixed }) => {
    const upload = vi.fn(importFile);
    promptDraftStore.setImporter(upload);
    const onNotify = vi.fn();
    const view = render(<WorkbenchView {...props({ onNotify })} />);
    act(() => promptDraftStore.insertText('session-one', 'existing draft'));
    const directory = new File([], 'memos');
    const files = mixed ? [new File(['pdf'], 'report.pdf'), directory] : [directory];
    const dataTransfer = { ...transfer(files), items: files.map(file => ({
      kind: 'file', getAsFile: () => file,
      webkitGetAsEntry: () => ({ isDirectory: file === directory, isFile: file !== directory }),
    })) };
    const target = insideEditor ? screen.getByRole('textbox') : view.container.querySelector('.thread-scroll')!;
    fireEvent.dragEnter(target, { dataTransfer: transfer() });
    expect(fireEvent.drop(target, { dataTransfer })).toBe(false);
    await act(async () => {});
    expect(onNotify).toHaveBeenCalledExactlyOnceWith('不支持拖入文件夹', '本次拖入未添加。请使用 @ 选择目录，或粘贴完整路径。');
    expect(upload).not.toHaveBeenCalled();
    expect(promptDraftStore.summary('session-one').text).toBe('existing draft');
    expect(promptDraftStore.summary('session-one').pendingFiles).toBe(0);
    expect(promptDraftStore.summary('session-one').failedFiles).toBe(0);
    expect(screen.queryByText('松开以添加文件')).toBeNull();
  });

  it('accepts an empty extensionless file using its file entry metadata', async () => {
    const upload = vi.fn(importFile);
    promptDraftStore.setImporter(upload);
    const onNotify = vi.fn();
    render(<WorkbenchView {...props({ onNotify })} />);
    const file = new File([], 'memos');
    fireEvent.drop(screen.getByRole('region', { name: '会话工作台' }), { dataTransfer: {
      ...transfer([file]), items: [{ kind: 'file', webkitGetAsEntry: () => ({ isDirectory: false, isFile: true }) }],
    } });
    await waitFor(() => expect(upload).toHaveBeenCalledOnce());
    await waitFor(() => expect(promptDraftStore.summary('session-one').pendingFiles).toBe(0));
    expect(promptDraftStore.summary('session-one').text).toBe('【本地文件（只读副本，0 bytes）："/tmp/imports/memos"】');
    expect(onNotify).not.toHaveBeenCalled();
  });

  it('appends mixed files from the message area in order and focuses the existing draft without sending', async () => {
    const upload = vi.fn(importFile);
    promptDraftStore.setImporter(upload);
    const onSend = vi.fn(async () => true);
    const view = render(<WorkbenchView {...props({ onSend })} />);
    const editor = promptDraftStore.getEditor('session-one');
    act(() => { promptDraftStore.insertText('session-one', 'before'); editor.commands.setTextSelection(2); });
    const image = new File(['image'], 'paste.png', { type: 'image/png' });
    Object.defineProperty(image, 'arrayBuffer', { value: async () => new Uint8Array([1, 2]).buffer });
    const files = [new File(['pdf'], 'report.pdf'), image, new File(['doc'], 'draft.docx')];
    const messageArea = view.container.querySelector('.thread-scroll')!;
    fireEvent.dragEnter(messageArea, { dataTransfer: transfer() });
    expect(screen.getByText('松开以添加文件')).toBeTruthy();
    fireEvent.drop(messageArea, { dataTransfer: transfer(files) });
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(promptDraftStore.summary('session-one').pendingFiles).toBe(0));
    expect((await promptDraftStore.capture('session-one')).content.parts).toEqual([
      { type: 'text', text: 'before【本地文件（只读副本，3 bytes）："/tmp/imports/report.pdf"】' },
      { type: 'image', source: 'local', bytes: new Uint8Array([1, 2]), declaredMediaType: 'image/png' },
      { type: 'text', text: '【本地文件（只读副本，3 bytes）："/tmp/imports/draft.docx"】' },
    ]);
    expect(screen.queryByText('松开以添加文件')).toBeNull();
    expect(onSend).not.toHaveBeenCalled();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('textbox')));
  });

  it('lets Tiptap insert at the drop position once instead of also appending it', async () => {
    const upload = vi.fn(importFile);
    promptDraftStore.setImporter(upload);
    render(<WorkbenchView {...props()} />);
    const editor = promptDraftStore.getEditor('session-one');
    act(() => promptDraftStore.insertText('session-one', 'abcd'));
    vi.spyOn(editor.view, 'posAtCoords').mockReturnValue({ pos: 3, inside: -1 });
    fireEvent.drop(screen.getByRole('textbox'), { dataTransfer: transfer([new File(['pdf'], 'report.pdf')]), clientX: 1, clientY: 1 });
    await waitFor(() => expect(upload).toHaveBeenCalledOnce());
    await waitFor(() => expect(promptDraftStore.summary('session-one').pendingFiles).toBe(0));
    expect(promptDraftStore.summary('session-one').text).toBe('ab【本地文件（只读副本，3 bytes）："/tmp/imports/report.pdf"】cd');
  });

  it('keeps the hint stable across children and clears it on leaving or cancelling', () => {
    const view = render(<WorkbenchView {...props()} />);
    const region = screen.getByRole('region', { name: '会话工作台' });
    const child = screen.getByRole('heading', { name: 'PR03 会话' });
    const dataTransfer = transfer(); // OS drag metadata arrives before file bytes.
    fireEvent.dragEnter(region, { dataTransfer });
    fireEvent.dragEnter(child, { dataTransfer });
    fireEvent.dragLeave(region, { dataTransfer });
    expect(screen.getByText('松开以添加文件')).toBeTruthy();
    fireEvent.dragLeave(child, { dataTransfer });
    expect(screen.queryByText('松开以添加文件')).toBeNull();
    fireEvent.dragEnter(region, { dataTransfer });
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByText('松开以添加文件')).toBeNull();
    fireEvent.dragEnter(region, { dataTransfer });
    fireEvent.dragEnd(window);
    expect(view.container.querySelector('.is-file-dragging')).toBeNull();
  });

  it('excludes both sidebars, plain text, and internal drags', () => {
    const upload = vi.fn(importFile);
    promptDraftStore.setImporter(upload);
    render(<><aside aria-label="左侧栏" /><WorkbenchView {...props()} /><aside aria-label="右侧栏" /></>);
    const dataTransfer = transfer([new File(['pdf'], 'report.pdf')]);
    for (const name of ['左侧栏', '右侧栏']) {
      const sidebar = screen.getByRole('complementary', { name });
      fireEvent.dragEnter(sidebar, { dataTransfer });
      expect(screen.queryByText('松开以添加文件')).toBeNull();
      expect(fireEvent.drop(sidebar, { dataTransfer })).toBe(true);
    }
    const region = screen.getByRole('region', { name: '会话工作台' });
    const textTransfer = { ...transfer(), types: ['text/plain'], getData: () => 'normal text' };
    fireEvent.dragEnter(region, { dataTransfer: textTransfer });
    expect(fireEvent.drop(region, { dataTransfer: textTransfer })).toBe(true);
    fireEvent.dragStart(region, { dataTransfer });
    fireEvent.dragEnter(region, { dataTransfer });
    expect(screen.queryByText('松开以添加文件')).toBeNull();
    expect(fireEvent.drop(region, { dataTransfer })).toBe(true);
    expect(upload).not.toHaveBeenCalled();
    expect(promptDraftStore.summary('session-one').hasContent).toBe(false);
  });

  it.each([{ runtimeStatus: 'offline' as const }, { canControl: false, isObserver: true }])('does not import while unavailable: %j', async blockedProps => {
    const upload = vi.fn(importFile);
    promptDraftStore.setImporter(upload);
    render(<WorkbenchView {...props(blockedProps)} />);
    const region = screen.getByRole('region', { name: '会话工作台' });
    const dataTransfer = transfer([new File(['pdf'], 'report.pdf')]);
    fireEvent.dragEnter(region, { dataTransfer });
    expect(screen.queryByText('松开以添加文件')).toBeNull();
    fireEvent.dragOver(region, { dataTransfer });
    expect(dataTransfer.dropEffect).toBe('none');
    await act(async () => fireEvent.drop(region, { dataTransfer }));
    expect(upload).not.toHaveBeenCalled();
    expect(promptDraftStore.summary('session-one').hasContent).toBe(false);
  });

  it('rejects a drop if the session changes mid-drag, then allows a fresh gesture', async () => {
    const upload = vi.fn(importFile);
    promptDraftStore.setImporter(upload);
    const onNotify = vi.fn();
    const view = render(<WorkbenchView {...props({ onNotify })} />);
    const region = screen.getByRole('region', { name: '会话工作台' });
    const dataTransfer = transfer([new File(['pdf'], 'report.pdf')]);
    fireEvent.dragEnter(region, { dataTransfer });
    view.rerender(<WorkbenchView {...props({ onNotify, session: { ...props().session, id: 'session-two' } })} />);
    expect(screen.queryByText('松开以添加文件')).toBeNull();
    await act(async () => fireEvent.drop(region, { dataTransfer }));
    expect(upload).not.toHaveBeenCalled();
    expect(onNotify).toHaveBeenCalledWith('文件未加入草稿', expect.stringContaining('会话已切换'));
    expect(promptDraftStore.summary('session-one').hasContent).toBe(false);
    expect(promptDraftStore.summary('session-two').hasContent).toBe(false);
    fireEvent.dragEnter(region, { dataTransfer });
    fireEvent.drop(region, { dataTransfer });
    await waitFor(() => expect(upload).toHaveBeenCalledWith('session-two', expect.any(Array), false, expect.any(AbortSignal)));
  });
});
