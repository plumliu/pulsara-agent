import { PresentationContext } from '../../frontend/components/presentation-provider';
import type { PulsaraDesktop } from '../../frontend/lib/desktop-host';
import { createHash, webcrypto } from 'node:crypto';
import { LocalHttpRuntimeAdapter } from '../../frontend/lib/runtime-adapter';
import { ToolResultDisplayContext } from '../../frontend/lib/tool-result-display';
import type { ReactElement, PropsWithChildren } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { QueuedPrompt, QueuedPromptAction, ToolArtifactPage } from '../../frontend/lib/runtime-adapter';
import { PromptDraftStore } from '../../frontend/lib/prompt-draft';
import { promptContentTextProjection } from '../../frontend/lib/prompt-content';
import { ConversationMessages, WorkbenchView } from '../../frontend/components/workbench-view';
import { WELCOME_TYPEWRITER_PHRASES } from '../../frontend/components/welcome-typewriter';

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
  it('uses a replaced visualization reader for unchanged response markup', async () => {
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
    vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} });
    vi.stubGlobal('matchMedia', () => ({matches:false,addEventListener() {},removeEventListener() {}}));
    const first = vi.fn().mockRejectedValue(Error('offline'));
    const latest = vi.fn(async () => '<p>restored</p>');
    const messages = [{id:'chart-answer',role:'assistant' as const,assistantKind:'terminal' as const,
      status:'completed' as const,time:'13:04',body:'```pulsara-visualization\nchart.html\n```',
      visualizations:[{ordinal:0,state:'READY' as const,contentDigest:`sha256:${'a'.repeat(64)}`,contentSize:15}]}];
    const notify = vi.fn(), readTool = vi.fn();
    const view = render(<ConversationMessages messages={messages} artifactOwnerKey="session-one"
      onReadToolArtifact={readTool} onNotify={notify} onReadVisualization={first}/>);
    await screen.findByText('已保存的可视化暂时无法读取。');
    view.rerender(<ConversationMessages messages={messages} artifactOwnerKey="session-one"
      onReadToolArtifact={readTool} onNotify={notify} onReadVisualization={latest}/>);
    // The existing gallery owner restarts its read when its reader changes.
    await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
    expect(first).toHaveBeenCalledTimes(1);
    expect(latest).toHaveBeenCalledTimes(1);
  });

  it('renders bound occurrences at the body marker, reads only selected HTML, and reuses resident frames', async () => {
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
    vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} });
    vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
    const read = vi.fn(async () => '<h1>Chart</h1>');
    const visualizations = [0, 1].map(ordinal => ({
      ordinal, state: 'READY' as const, contentDigest: `sha256:${String(ordinal).repeat(64)}`, contentSize: 14,
    }));
    const view = render(<ConversationMessages canFork={true}
      messages={[{ id: 'chart-answer', role: 'assistant', assistantKind: 'terminal', status: 'completed',
        time: '13:04', body: '图表如下。\n\n```pulsara-visualization\nfirst.html\nsecond.html\n```\n\n图表之后的说明。', visualizations }]}
      artifactOwnerKey="session-one" onReadToolArtifact={vi.fn()} onNotify={vi.fn()} onReadVisualization={read} />);
    expect(screen.getAllByRole('region', { name: '可视化图集' })).toHaveLength(1);
    await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
    expect(read).toHaveBeenCalledOnce();
    expect(read).toHaveBeenCalledWith('chart-answer', 0, visualizations[0].contentDigest, 14, expect.objectContaining({ contentDigest: expect.any(String) }), expect.any(AbortSignal));
    const frame = view.container.querySelector('iframe');
    const gallery = screen.getByRole('region', { name: '可视化图集' });
    expect(screen.getByText('图表如下。').compareDocumentPosition(gallery) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(gallery.compareDocumentPosition(screen.getByText('图表之后的说明。')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole('button', { name: '查看可视化 1' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: '查看可视化 2' }).getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(screen.getByRole('button', { name: '查看可视化 2' }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(read).toHaveBeenLastCalledWith('chart-answer', 1, visualizations[1].contentDigest, 14, expect.objectContaining({ contentDigest: expect.any(String) }), expect.any(AbortSignal));
    expect(view.container.querySelector('[data-visualization-ordinal="0"]')?.hasAttribute('hidden')).toBe(true);
    expect(view.container.querySelector('[data-visualization-ordinal="0"]')?.hasAttribute('inert')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '查看可视化 1' }));
    expect(view.container.querySelector('[data-visualization-ordinal="0"] iframe')).toBe(frame);
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('does not append a gallery or read occurrences when the assistant body has no marker', () => {
    const read = vi.fn();
    const view = render(<ConversationMessages canFork={true}
      messages={[{ id: 'unmarked-answer', role: 'assistant', assistantKind: 'terminal', status: 'completed',
        time: '13:04', body: '只有文字说明。', visualizations: [{
          ordinal: 0, state: 'READY', contentDigest: `sha256:${'a'.repeat(64)}`, contentSize: 14,
        }] }]}
      artifactOwnerKey="session-one" onReadToolArtifact={vi.fn()} onNotify={vi.fn()} onReadVisualization={read} />);
    expect(screen.getByText('只有文字说明。')).toBeTruthy();
    expect(screen.queryByRole('region', { name: '可视化图集' })).toBeNull();
    expect(view.container.querySelector('iframe')).toBeNull();
    expect(read).not.toHaveBeenCalled();
  });
});
afterEach(() => {
  delete window.pulsaraDesktop;
  cleanup();
  promptDraftStore.destroy();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function textPrompt(text: string) {
  return { parts: [{ type: 'text' as const, text }] };
}

function stubClipboard(writeText: (value: string) => Promise<void>) {
  Object.defineProperty(window, 'pulsaraDesktop', { configurable: true, writable: true, value: { writeClipboardText: writeText } });
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

  it('settles accepted layout when clear and queued restore leave hasContent true in one batch', async () => {
    const originalClear = promptDraftStore.clearIfSnapshot.bind(promptDraftStore);
    vi.spyOn(promptDraftStore, 'clearIfSnapshot').mockImplementation((sessionId, snapshot) => {
      const cleared = originalClear(sessionId, snapshot);
      if (cleared) expect(promptDraftStore.restoreIfEmpty(sessionId, textPrompt('下一条恢复的输入'))).toBe(true);
      return cleared;
    });
    const view = render(<WorkbenchView {...props({ onSend: vi.fn(async () => true) })} />);
    act(() => promptDraftStore.restoreIfEmpty('session-one', textPrompt('原来的多行输入')));
    const editor = view.container.querySelector('.composer-editor') as HTMLElement;
    editor.getBoundingClientRect = () => new DOMRect(0, 0, 720,
      promptDraftStore.summary('session-one').text === '原来的多行输入' ? 140 : 52);
    const animate = vi.fn(() => ({cancel:vi.fn()}) as unknown as Animation);
    editor.animate = animate;
    act(() => screen.getByLabelText('发送给 Pulsara').focus());
    fireEvent.keyDown(screen.getByLabelText('发送给 Pulsara'), {key:'Enter'});
    await waitFor(() => expect(animate).toHaveBeenCalledExactlyOnceWith([
      {height:'140px',overflow:'hidden'}, {height:'52px',overflow:'hidden'},
    ], {duration:240,easing:'cubic-bezier(.2, .7, .2, 1)'}));
    expect(promptDraftStore.summary('session-one').hasContent).toBe(true);
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toBe('下一条恢复的输入');
    await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText('发送给 Pulsara')));
  });

  it.each([false, true])('does not replay an accepted layout after switching away and back (late acceptance: %s)', async late => {
    let accept!: (value: boolean) => void;
    const shared = props({onSend:vi.fn(() => new Promise<boolean>(resolve => {accept=resolve;}))});
    const second = {...shared,session:{...shared.session,id:'session-two',title:'另一个会话'}};
    const view = render(<WorkbenchView {...shared} />);
    act(() => promptDraftStore.restoreIfEmpty('session-one', textPrompt('原来的多行输入')));
    const wrapper = view.container.querySelector('.composer-editor') as HTMLElement;
    wrapper.getBoundingClientRect = () => new DOMRect(0,0,720,
      promptDraftStore.summary('session-one').hasContent ? 140 : 52);
    const animate = vi.fn(() => ({cancel:vi.fn()}) as unknown as Animation);
    wrapper.animate = animate;
    const focus = vi.spyOn(promptDraftStore,'focus');
    act(() => screen.getByLabelText('发送给 Pulsara').focus());
    fireEvent.keyDown(screen.getByLabelText('发送给 Pulsara'),{key:'Enter'});
    await waitFor(() => expect(shared.onSend).toHaveBeenCalledOnce());
    if (late) {
      view.rerender(<WorkbenchView {...second} />);
      act(() => screen.getByLabelText('发送给 Pulsara').focus());
    }
    await act(async () => accept(true));
    if (late) {
      expect(focus).not.toHaveBeenCalled();
      expect(animate).not.toHaveBeenCalled();
    } else {
      await waitFor(() => expect(focus).toHaveBeenCalledOnce());
      expect(animate).toHaveBeenCalledOnce();
      view.rerender(<WorkbenchView {...second} />);
    }
    focus.mockClear(); animate.mockClear();
    view.rerender(<WorkbenchView {...shared} />);
    expect(promptDraftStore.summary('session-one').hasContent).toBe(false);
    expect(focus).not.toHaveBeenCalled();
    expect(animate).not.toHaveBeenCalled();
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
    onNewSession: vi.fn(),
    onToggleInspector: vi.fn(),
    onOpenModelSettings: vi.fn(),
    onModelCallBindingChange: vi.fn(async () => undefined),
    onSend: vi.fn(async () => true),
    onStop: vi.fn(),
    onCompact: vi.fn(async () => undefined),
    onRenameSession: vi.fn(),
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

describe('prompt delivery and preparation', () => {
  const submission = {
    sessionId: 'session-one', connectionGeneration: 1, commandId: 'command:start',
    queueItemId: 'queue:start', content: { parts: [{ type: 'text' as const, text: '开始处理这条消息' }] },
    deliveryMode: 'new-turn' as const, permission: 'read-only' as const, displayAsMessage: true,
  };

  it('separates confirmed delivery from preparation, then replaces the placeholder with real activity', () => {
    const view = render(<WorkbenchView {...props({ isRunning: false, localSubmissions: [{ ...submission, status: 'sending' }] })} />);
    expect(screen.getByText('正在发送…')).toBeTruthy();
    expect(screen.queryByLabelText('Pulsara 处理状态')).toBeNull();
    view.rerender(<WorkbenchView {...props({ isRunning: false, localSubmissions: [{ ...submission, status: 'synchronizing' }] })} />);
    expect(screen.getByText('已送达').closest('.user-turn')).toBeTruthy();
    expect(within(screen.getByLabelText('Pulsara 处理状态')).getByText('准备开始…')).toBeTruthy();
    expect(screen.queryByText('正在开始…')).toBeNull();
    const user = { id: 'entry:start', role: 'user' as const, turnId: 'turn:start', body: '开始处理这条消息', time: '12:00' };
    view.rerender(<WorkbenchView {...props({ activeTurnId: 'turn:start', messages: [user] })} />);
    expect(screen.getAllByText('开始处理这条消息')).toHaveLength(1);
    expect(within(screen.getByLabelText('Pulsara 处理状态')).getByText('正在处理…')).toBeTruthy();
    view.rerender(<WorkbenchView {...props({ activeTurnId: 'turn:start', messages: [user,
      { id: 'live:start', role: 'assistant', turnId: 'turn:start', body: '收到', time: '', status: 'running' },
    ] })} />);
    expect(screen.queryByLabelText('Pulsara 处理状态')).toBeNull();
    expect(screen.getByText('收到')).toBeTruthy();
  });

  it.each([0, 1, 4])('keeps known input layout through canonical hydration after %i replies', priorReplies => {
    const previous = Array.from({ length: priorReplies }, (_, index) => ({
      id: `answer:${index}`, turnId: `previous:${index}`, role: 'assistant' as const,
      assistantKind: 'terminal' as const, rootFinal: true, body: '之前的回复', time: '12:00',
      historyContextState: 'ready' as const, historyWindowId: 'tail',
    }));
    const localSubmissions = [{ ...submission, status: 'queued' as const }];
    const base = props({ messages: previous, isRunning: false, localSubmissions });
    const view = render(<WorkbenchView {...base} />);
    const waiting = screen.getByLabelText('Pulsara 处理状态');
    const user = { id: 'entry:start', turnId: 'turn:start', role: 'user' as const,
      userKind: 'prompt' as const, body: '', time: '12:01', historyWindowId: 'tail',
      inputSource: { commandId: submission.commandId, queueItemId: submission.queueItemId, deliveryMode: 'new-turn' as const },
      pendingContent: submission.content };
    for (const phase of ['context', 'unloaded', 'loading', 'ready'] as const) {
      const contextState = phase === 'context' ? 'loading' as const : 'ready' as const;
      const history = { hasOlder: false, loading: false, windows: [{
        id: 'tail', hasOlder: false, hasNewer: false, loading: false, contextState,
        entryIds: [...previous.map(message => message.id), user.id],
      }] };
      view.rerender(<WorkbenchView {...base} isRunning activeTurnId="turn:start" history={history}
        messages={[...previous, { ...user, historyContextState: contextState,
          contentState: phase === 'context' ? 'unloaded' : phase,
          promptContent: phase === 'ready' ? submission.content : undefined }]} />);
      expect(screen.getByLabelText('Pulsara 处理状态')).toBe(waiting);
      expect(screen.getAllByText('开始处理这条消息')).toHaveLength(1);
      expect(view.container.querySelector('.history-boundary, .history-content-status')).toBeNull();
      expect(screen.queryByText('正在读取记录分组…')).toBeNull();
      expect(screen.queryByText('已保存，正文读取中…')).toBeNull();
      expect(view.container.querySelector('[data-source-entry="entry:start"] > .user-turn')).toBeTruthy();
    }
    expect(screen.getByText('12:01')).toBeTruthy();
    expect(screen.queryByText('本地已知内容')).toBeNull();
  });

  it('retains canonical read errors and retry for locally known input', () => {
    const read = vi.fn(async () => {});
    const base = props({ isRunning: true, activeTurnId: 'turn:start', onHydrateEntries: read });
    const user = { id: 'entry:start', turnId: 'turn:start', role: 'user' as const, body: '', time: '',
      pendingContent: submission.content, historyContextState: 'ready' as const,
      contentState: 'error' as const, contentError: '正文读取失败' };
    const view = render(<WorkbenchView {...base} messages={[user]} />);
    expect(screen.getByText('开始处理这条消息')).toBeTruthy();
    expect(screen.getByText('正文读取失败')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '重试正文' }));
    expect(read).toHaveBeenCalledWith(['entry:start'], false);
    view.rerender(<WorkbenchView {...base} messages={[{ ...user, contentState: 'unloaded', historyContextState: 'error' }]} />);
    expect(screen.getByText('记录分组读取失败，请重试此页。')).toBeTruthy();
  });

  it('still shows tail loading for an input whose content is not locally known', () => {
    const history = { hasOlder: false, loading: false, windows: [{
      id: 'tail', hasOlder: false, hasNewer: false, loading: false,
      contextState: 'loading' as const, entryIds: ['other-input'],
    }] };
    render(<WorkbenchView {...props({ isRunning: true, activeTurnId: 'other-turn', history,
      localSubmissions: [{ ...submission, status: 'queued' }],
      messages: [{ id: 'other-input', turnId: 'other-turn', role: 'user', body: '', time: '',
        historyWindowId: 'tail', historyContextState: 'loading', contentState: 'unloaded',
        inputSource: { commandId: 'other-command', queueItemId: 'other-queue', deliveryMode: 'new-turn' } }],
    })} />);
    expect(screen.getByText('正在加载…')).toBeTruthy();
    expect(screen.getAllByText('开始处理这条消息')).toHaveLength(1);
    expect(screen.queryByText('本地已知内容')).toBeNull();
  });

  it.each(['unknown', 'rejected', 'cancelled'] as const)('does not imply preparation for %s delivery', status => {
    render(<WorkbenchView {...props({ isRunning: false, localSubmissions: [{ ...submission, status }] })} />);
    expect(screen.queryByText('已送达')).toBeNull();
    expect(screen.queryByLabelText('Pulsara 处理状态')).toBeNull();
  });

  it('does not start another assistant for queued or disconnected submissions', () => {
    const localSubmissions = [{ ...submission, status: 'queued' as const }];
    const view = render(<WorkbenchView {...props({ localSubmissions })} />);
    expect(screen.queryByText('准备开始…')).toBeNull();
    view.rerender(<WorkbenchView {...props({ isRunning: false, runtimeStatus: 'reconnecting', localSubmissions })} />);
    expect(screen.queryByText('准备开始…')).toBeNull();
  });
});

describe('current-session runtime actions', () => {
  it('opens rename from the header menu and closes the menu', () => {
    const onRenameSession = vi.fn();
    render(<WorkbenchView {...props({ onRenameSession })} />);
    const trigger = screen.getByRole('button', { name: '更多会话操作' });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('button', { name: '重命名给你的会话起个名字' }));
    expect(onRenameSession).toHaveBeenCalledOnce();
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
  });

  it('keeps runtime reopen in the header menu, separate from compaction', () => {
    const onReopenRuntime = vi.fn();
    render(<WorkbenchView {...props({ onReopenRuntime })} />);
    const trigger = screen.getByRole('button', { name: '更多会话操作' });
    expect(screen.getByRole('button', { name: '压缩上下文' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重新加载会话' })).toBeNull();

    fireEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: /重新加载会话/ }));
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
    expect(button.getAttribute('title')).toContain('任务、排队输入和后台命令不会自动取消');
    fireEvent.click(button);
    expect(stop).toHaveBeenCalledTimes(1);

    view.rerender(<WorkbenchView {...props({ isObserver: true, canControl: false, onStop: stop })} />);
    expect(screen.queryByRole('button', { name: '停止本轮运行' })).toBeNull();
    expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
    expect(screen.getByText('此窗口仅供查看')).toBeTruthy();
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

    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
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
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
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
    const { container } = render(<ConversationMessages canFork={true}
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
    expect(container.querySelectorAll('.conversation-run__step')).toHaveLength(0);
    expect(container.querySelectorAll('.trace-card')).toHaveLength(0);
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
  // Collapsed processes must unmount their complex bodies, not merely hide them.
  const hiddenProgress = () => screen.queryByText(progress.body) === null;

  it('keeps one executing heading across live reasoning and committed tool cycles', () => {
    const committed = { ...progress, historyGroupId: 'root-process', historyWindowId: 'tail',
      entryOwnerKind: 'EXECUTED_TURN' as const };
    const draft = (id: string) => ({ id, turnId: 'turn-one', role: 'assistant' as const,
      assistantKind: 'live' as const, time: '', body: '', status: 'running' as const,
      reasoning: [{ id: `${id}-reason`, kind: 'full' as const, body: `思考 ${id}`, active: true }] });
    const base = { activeTurnId: 'turn-one', isRunning: true,
      artifactOwnerKey: 'session-one', onReadToolArtifact: vi.fn(), onNotify: vi.fn(),
      history: { hasOlder: false, loading: false, windows: [{ id: 'tail',
        firstEntryId: 'progress', lastEntryId: 'progress', hasOlder: false, hasNewer: false, loading: false }] } };
    const view = render(<ConversationMessages {...base} messages={[committed, draft('thinking-one')]} />);
    const heading = view.container.querySelector('.assistant-heading');
    const check = () => {
      expect(view.container.querySelectorAll('.assistant-heading')).toHaveLength(1);
      expect(view.container.querySelector('.assistant-heading')).toBe(heading);
      expect(within(heading as HTMLElement).getByText('正在执行')).toBeTruthy();
      expect(view.container.querySelectorAll('.conversation-run')).toHaveLength(1);
    };
    check();
    const tool = { ...committed, id: 'next-tool', body: '', traces: [{ ...progress.traces[0], id: 'read-two' }] };
    view.rerender(<ConversationMessages {...base} messages={[committed, tool]} />);
    check();
    view.rerender(<ConversationMessages {...base} messages={[committed, tool, draft('thinking-two')]} />);
    check();
    expect(screen.getByText('思考 thinking-two')).toBeTruthy();
    view.rerender(<ConversationMessages {...base} isRunning={false} messages={[committed, tool,
      { ...final, historyGroupId: 'root-process', historyWindowId: 'tail' }]} />);
    expect(view.container.querySelectorAll('.assistant-heading')).toHaveLength(1);
    expect(screen.queryByText('正在执行')).toBeNull();
    expect(screen.getByText(final.body)).toBeTruthy();
  });

  it('renders the reader’s old band with only a bottom continuation boundary', () => {
    const view = render(<ConversationMessages activeTurnId="turn-one" isRunning
      messages={[{ ...progress, historyGroupId: 'root-process', historyWindowId: 'older' }]}
      history={{ atLatest: false, hasOlder: false, loading: false, windows: [{ id: 'older',
        firstEntryId: 'progress', lastEntryId: 'progress', hasOlder: false, hasNewer: true, loading: false }] }}
      artifactOwnerKey="session-one" onReadToolArtifact={vi.fn()} onNotify={vi.fn()} />);
    expect(view.container.querySelectorAll('.conversation-run')).toHaveLength(1);
    expect(screen.getByText('后面还有消息')).toBeTruthy();
    expect(screen.queryByText('后续记录尚未加载')).toBeNull();
  });

  it.each([1, 3])('folds %s leading completion notifications without an earlier assistant step', count => {
    const notices = Array.from({ length: count }, (_, i) => ({
      id: `completion-${i}`, turnId: 'turn-one', role: 'user' as const,
      userKind: 'subagent-completion' as const, time: '13:02', body: '', sourceSubagentLabel: `任务 ${i}`,
    }));
    render(<ConversationMessages messages={[
      { id: 'prompt', turnId: 'turn-one', role: 'user', userKind: 'prompt', time: '13:01', body: '继续汇总' },
      ...notices, final,
    ]} artifactOwnerKey="session-one" onReadToolArtifact={vi.fn()} onNotify={vi.fn()} />);
    expect(screen.getByText('继续汇总').closest('.conversation-run')).toBeNull();
    expect(screen.queryAllByRole('article', { name: /的结果已加入本轮对话/ })).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    expect(screen.getAllByRole('article', { name: /的结果已加入本轮对话/ })).toHaveLength(count);
    expect(screen.getByText(final.body).closest('.conversation-run__step')).toBeNull();
  });

  it('folds completion notifications into the process and portals their help outside clipped rows', () => {
    const completion = { id: 'completion', turnId: 'turn-one', role: 'user' as const,
      userKind: 'subagent-completion' as const, time: '13:02', body: '', sourceSubagentLabel: '事实核对' };
    const view = render(<ConversationMessages messages={[progress, completion, steer, final]}
      artifactOwnerKey="session-one" onReadToolArtifact={vi.fn()} onNotify={vi.fn()} />);
    expect(screen.queryByRole('article', { name: '事实核对 的结果已加入本轮对话' })).toBeNull();
    expect(screen.getByText(steer.body).closest('[inert]')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    const card = screen.getByRole('article', { name: '事实核对 的结果已加入本轮对话' });
    fireEvent.mouseEnter(card);
    const tooltip = screen.getByRole('tooltip');
    expect(tooltip.parentElement).toBe(document.body);
    expect(card.getAttribute('aria-describedby')).toBe(tooltip.id);
    expect(view.container.querySelector('[role="tooltip"]')).toBeNull();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('tooltip')).toBeNull();
    fireEvent.focus(card);
    expect(screen.getByRole('tooltip')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '收起中间过程' }));
    expect(screen.queryByRole('tooltip')).toBeNull();
    expect(card.isConnected).toBe(false);
    expect(screen.getByText(final.body).closest('.conversation-run__step')).toBeNull();
  });

  it('keeps streaming and steer in order, closes only on a confirmed final, and preserves manual expansion', () => {
    const view = renderWithRawResults(<WorkbenchView {...props({ activeTurnId: 'turn-one', messages: [progress, steer] })} />);
    expect(hiddenProgress()).toBe(false);
    const draft = { ...final, id: 'live-final', assistantKind: 'live' as const, status: 'running' as const, rootFinal: false, forkEligible: false };
    view.rerender(<WorkbenchView {...props({ activeTurnId: 'turn-one', messages: [progress, steer, draft] })} />);
    expect(hiddenProgress()).toBe(false);
    const streamingLayout = screen.getByText(draft.body).closest('.assistant-markdown')?.className;
    expect(screen.getByText(draft.body).closest('.assistant-copy')?.querySelector('.response-actions')).toBeNull();
    // A committed assistant text without terminal-final eligibility is not enough.
    view.rerender(<WorkbenchView {...props({ activeTurnId: 'turn-one', messages: [progress, steer, { ...final, rootFinal: false, forkEligible: false }] })} />);
    expect(hiddenProgress()).toBe(false);
    expect(screen.queryByRole('button', { name: '复制回复' })).toBeNull();
    view.rerender(<WorkbenchView {...props({ isRunning: false, messages: [progress, steer, final] })} />);
    expect(hiddenProgress()).toBe(true);
    expect(screen.getByText(final.body).closest('.assistant-markdown')?.className).toBe(streamingLayout);
    expect(screen.getByText(steer.body).closest('.conversation-run__step')?.hasAttribute('inert')).toBe(false);
    expect(screen.queryByText(progress.body)).toBeNull();
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
    expect(view.container.querySelectorAll('[data-source-entry="final"]')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    fireEvent.click(screen.getByRole('button', { name: kind === 'summary' ? '展开思考摘要' : '展开思考' }));
    expect(screen.getByText('核对完成。').closest('.reasoning-row__body')).toBeTruthy();
    view.rerender(<WorkbenchView {...props({ isRunning: false, messages: [final] })} />);
    expect(screen.queryByRole('button', { name: /中间过程/ })).toBeNull();
    expect(screen.getByText(final.body)).toBeTruthy();
  });

  it('does not fold the next running turn with a previous completed one', () => {
    render(<WorkbenchView {...props({ activeTurnId: 'turn-two', messages: [progress, final,
      { id: 'next-user', turnId: 'turn-two', role: 'user', userKind: 'prompt', time: '13:04', body: '接着做' },
      { ...progress, id: 'next-progress', turnId: 'turn-two', body: '正在处理第二轮。' },
    ] })} />);
    expect(hiddenProgress()).toBe(true);
    expect(screen.getByText('正在处理第二轮。').closest('.conversation-run__step')?.hasAttribute('inert')).toBe(false);
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
      messages: [{ ...progress, historyGroupId: 'before-adoption' }, { ...steer, historyGroupId: 'before-adoption' }, { ...final, historyGroupId: 'after-adoption' }], artifactOwnerKey: 'session-one',
      onReadToolArtifact: vi.fn(), onNotify: vi.fn(), history: { hasOlder: false, loading: false, windows: [], compactions: [{ eventId: 'adoption', adoptedAfterEntrySequence: 3, displayAfterEntryId: 'steer' }] },
    };
    const view = renderWithRawResults(<ConversationMessages {...common} />);
    expect(hiddenProgress()).toBe(true);
    expect(screen.getByRole('separator', { name: '上下文已压缩' })).toBeTruthy();
    expect(screen.getByText(final.body).closest('[hidden]')).toBeNull();
    view.rerender(<ConversationMessages {...common}
      focusSourceEntry={{ sessionId: 'session-one', entryId: progress.id }} />);
    expect(hiddenProgress()).toBe(false);
    expect(screen.getByRole('button', { name: '展开工具详情：read_file' })).toBeTruthy();
  });

});

describe('composer + menu', () => {
  it('opens skills inside the same menu and can return or insert a skill', () => {
    render(<WorkbenchView {...props({ skills: [{ name: 'review', description: 'Review a project', location: '/tmp/review/SKILL.md', scope: 'user', configured: false }] })} />);
    fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
    const menu = screen.getByRole('dialog', { name: '添加内容与模式' });
    fireEvent.click(screen.getByRole('button', { name: '选择技能' }));
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(within(menu).getByText('/review')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '添加文件' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '返回添加菜单' }));
    expect(within(menu).getByRole('button', { name: '添加文件' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '选择技能' }));
    fireEvent.click(within(menu).getByText('/review').closest('button')!);
    expect(screen.queryByRole('dialog', { name: '添加内容与模式' })).toBeNull();
    expect(promptDraftStore.summary('session-one').text).toContain('review');
  });
  it.each([false, true])('reserves queued edit against an open menu and late picker, directory=%s', async directory => {
    const upload = vi.fn(async () => ({ path: '/tmp/imports/id/report.pdf', name: 'report.pdf', bytes: 1 }));
    promptDraftStore.setImporter(upload);
    const item: QueuedPrompt = { queueItemId: 'source', commandId: 'submitted', sequence: 1, status: 'pending', deliveryMode: 'new-turn', content: textPrompt('queued body'), permission: 'read-only' };
    const action: QueuedPromptAction = { commandId: 'edit-command', sessionId: 'session-one', connectionGeneration: 1, kind: 'edit', source: item, status: 'submitting', submittedAt: 'now' };
    const onNotify = vi.fn();
    const view = render(<WorkbenchView {...props({ queuedPrompts: [item], onNotify })} />);
    fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
    let resolvePicker!: (value: string | string[]) => void;
    const pick = vi.fn(() => new Promise<string | string[]>(resolve => { resolvePicker = resolve; }));
    window.pulsaraDesktop = { chooseFiles: pick, chooseDirectory: pick } as unknown as PulsaraDesktop;
    view.rerender(<WorkbenchView {...props({ queuedPrompts: [item], queueActions: [action], onNotify })} />);
    expect(screen.getByLabelText('发送给 Pulsara').getAttribute('contenteditable')).toBe('false');
    const menuItem = screen.getByRole('button', { name: directory ? '添加文件夹' : '添加文件' }) as HTMLButtonElement;
    expect(menuItem.disabled).toBe(true);
    fireEvent.click(menuItem);
    expect(pick).not.toHaveBeenCalled();
    view.rerender(<WorkbenchView {...props({ queuedPrompts: [item], onNotify })} />);
    fireEvent.click(screen.getByRole('button', { name: directory ? '添加文件夹' : '添加文件' }));
    expect(pick).toHaveBeenCalledOnce();
    view.rerender(<WorkbenchView {...props({ queuedPrompts: [item], queueActions: [action], onNotify })} />);
    const file = new File(['bytes'], 'report.pdf');
    Object.defineProperty(file, 'webkitRelativePath', { value: 'folder/report.pdf' });
    await act(async () => {
      resolvePicker(directory ? '/tmp/folder' : ['/tmp/report.pdf']);
      fireEvent.paste(screen.getByLabelText('发送给 Pulsara'), { clipboardData: { files: [file] } });
      fireEvent.drop(screen.getByLabelText('发送给 Pulsara'), { dataTransfer: { files: [file] } });
    });
    expect(promptDraftStore.summary('session-one').hasContent).toBe(false);
    expect(upload).not.toHaveBeenCalled();
    expect(onNotify).toHaveBeenCalledWith('文件未加入草稿', expect.any(String), 'warning');
  });

  it('groups attachments, skills and planning and appends a picked file beyond the current cursor', async () => {
    window.pulsaraDesktop = { chooseFiles: vi.fn(async () => ['/tmp/original/report.pdf']) } as unknown as PulsaraDesktop;
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
    fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '添加文件' })); });
    const content = (await promptDraftStore.capture('session-one')).content;
    expect(content.parts[0]).toMatchObject({ type: 'text', text: expect.stringContaining('/tmp/original/report.pdf') });
    expect(screen.queryByRole('button', { name: '先规划' })).toBeNull();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })); });
    expect(onSend).toHaveBeenCalledWith(content, 'read-only', true);
    expect(view.container.querySelector('.composer-plan-status')).toBeNull();
  });
});

describe('workbench file drop boundary', () => {
  const transfer = (files: File[] = []) => ({ types: ['Files'], files, items: [], getData: () => '', dropEffect: 'none', effectAllowed: 'all' });
  const importFile = async (_session: string, files: readonly File[]) => ({ path: `/tmp/imports/${files[0].name}`, name: files[0].name, bytes: files[0].size });

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
    await waitFor(() => expect(upload).toHaveBeenCalledWith('session-two', expect.any(Array), expect.any(AbortSignal)));
  });
});

describe('shared workspace read-only dock', () => {
  it.each([true, false])('shows and copies the full recovery path and keeps refresh separate from creation (clipboard available: %s)', async clipboardAvailable => {
    const path = '/private/var/folders/example/temporary/pulsara-visualization-production-test/workspaces/quick-20261014-123544-461f6478';
    const copy = vi.fn(async () => { if (!clipboardAvailable) throw new Error('denied'); });
    stubClipboard(copy);
    const notify = vi.fn(), restore = vi.fn(), recheck = vi.fn();
    const base = props({canControl:false, isRunning:false,
      workspaceAvailability:{outcome:'MISSING', path}, onNotify:notify,
      onRestoreWorkspace:restore, onRecheckWorkspace:recheck});
    const view = render(<WorkbenchView {...base} />);
    const pathButton = screen.getByRole('button', {name:'复制工作目录路径'});
    fireEvent.mouseEnter(pathButton);
    expect(screen.getByRole('tooltip').textContent).toBe(path);
    expect(pathButton.getAttribute('aria-describedby')).toBe(screen.getByRole('tooltip').id);
    fireEvent.click(pathButton);
    await waitFor(() => expect(notify).toHaveBeenCalledWith(
      clipboardAvailable ? '路径已复制' : '无法复制路径',
      clipboardAvailable ? undefined : '系统剪贴板暂时不可用，请重试',
      clipboardAvailable ? 'success' : 'warning',
    ));
    expect(copy).toHaveBeenCalledExactlyOnceWith(path);
    fireEvent.click(screen.getByRole('button', {name:'重新检查工作目录'}));
    expect(recheck).toHaveBeenCalledOnce();
    expect(restore).not.toHaveBeenCalled();
    view.rerender(<WorkbenchView {...base} workspaceRecoveryBusy />);
    expect(screen.getByRole('button', {name:'重新检查工作目录'})).toHaveProperty('disabled', true);
    expect(screen.getByRole('button', {name:'正在创建…'})).toHaveProperty('disabled', true);
  });

  it('retains historical text and drafts while blocking fork and chat writes', () => {
    promptDraftStore.restoreIfEmpty('session-one', textPrompt('keep my draft'));
    const restore = vi.fn();
    const original = props({ canControl: false, isRunning: false,
      workspaceAvailability: {outcome:'MISSING', path:'/tmp/pr03'},
      onRestoreWorkspace: restore,
      messages:[{id:'answer', role:'assistant', time:'现在', body:'saved history',
        status:'completed', assistantKind:'terminal', rootFinal:true, forkEligible:true}],
    });
    const view = render(<WorkbenchView {...original} />);
    expect(screen.getByText('saved history')).toBeTruthy();
    expect(screen.getByText('工作目录已丢失')).toBeTruthy();
    expect(screen.getByText('目录丢失')).toBeTruthy();
    expect(screen.queryByRole('button', {name:'从这里开始新对话'})).toBeNull();
    expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
    fireEvent.click(screen.getByRole('button', {name:'创建目录并继续'}));
    expect(restore).toHaveBeenCalledOnce();
    view.rerender(<WorkbenchView {...original} canControl={true} workspaceAvailability={{outcome:'AVAILABLE', path:'/tmp/pr03'}} />);
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toContain('keep my draft');
  });

  it('requires takeover before offering creation and never creates an unavailable path', () => {
    const takeover = vi.fn(), restore = vi.fn(), recheck = vi.fn();
    const base = props({canControl:false, isObserver:true,
      workspaceAvailability:{outcome:'MISSING', path:'/tmp/pr03'},
      onTakeControl:takeover, onRestoreWorkspace:restore, onRecheckWorkspace:recheck});
    const view = render(<WorkbenchView {...base} />);
    expect(screen.getByText('观察中')).toBeTruthy();
    expect(screen.queryByText('目录丢失')).toBeNull();
    expect(screen.queryByRole('button',{name:'创建目录并继续'})).toBeNull();
    fireEvent.click(screen.getByRole('button',{name:'在此窗口继续'}));
    expect(takeover).toHaveBeenCalledOnce();
    expect(restore).not.toHaveBeenCalled();
    view.rerender(<WorkbenchView {...base} isObserver={false}
      workspaceAvailability={{outcome:'UNAVAILABLE', path:'/tmp/pr03', reason:'路径是文件'}} />);
    expect(screen.getByText('目录不可用')).toBeTruthy();
    expect(screen.getByText('路径是文件')).toBeTruthy();
    expect(screen.queryByRole('button',{name:'创建目录并继续'})).toBeNull();
    fireEvent.click(screen.getByRole('button',{name:'重新检查工作目录'}));
    expect(recheck).toHaveBeenCalledOnce();
    expect(restore).not.toHaveBeenCalled();
  });
});


it('closes composer menus on control loss without reopening them when control returns', () => {
  const base = props({ canControl: true });
  const view = render(<WorkbenchView {...base} />);
  fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
  expect(screen.getByRole('dialog', { name: '添加内容与模式' })).toBeTruthy();

  view.rerender(<WorkbenchView {...base} canControl={false} />);
  expect(screen.queryByRole('dialog', { name: '添加内容与模式' })).toBeNull();
  view.rerender(<WorkbenchView {...base} />);
  expect(screen.queryByRole('dialog', { name: '添加内容与模式' })).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
  expect(screen.getByRole('dialog', { name: '添加内容与模式' })).toBeTruthy();
});

describe('ROOT context compaction progress', () => {
  it.each([false, true])('shows live compaction for controller and observer windows (observer=%s)', isObserver => {
    render(<WorkbenchView {...props({ isObserver, canControl: !isObserver, contextCompactionInProgress: true })} />);
    const status = screen.getByRole('status', { name: '上下文正在压缩' });
    expect(status.getAttribute('aria-busy')).toBe('true');
    expect(status.querySelector('.context-compaction-divider__spinner')).toBeTruthy();
    expect(screen.queryByRole('separator', { name: '上下文已压缩' })).toBeNull();
    if (!isObserver) {
      const button = screen.getByRole('button', { name: '压缩中…' }) as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      expect(button.getAttribute('aria-busy')).toBe('true');
      expect(button.classList.contains('is-compacting')).toBe(true);
      expect(button.textContent).toBe('压缩中…');
    }
  });

  it('replaces live progress with only the adopted canonical boundary', () => {
    const base = { artifactOwnerKey: 'session-one', onReadToolArtifact: vi.fn(), onNotify: vi.fn(), messages: [{ id: 'q', role: 'user' as const, time: '', body: '问题' }] };
    const view = render(<ConversationMessages {...base} contextCompactionInProgress />);
    expect(screen.getByRole('status', { name: '上下文正在压缩' })).toBeTruthy();
    view.rerender(<ConversationMessages {...base} history={{ hasOlder: false, loading: false, windows: [], compactions: [{ eventId: 'adoption', adoptedAfterEntrySequence: 1, displayAfterEntryId: 'q' }] }} />);
    expect(screen.queryByRole('status', { name: '上下文正在压缩' })).toBeNull();
    expect(screen.getAllByRole('separator', { name: '上下文已压缩' })).toHaveLength(1);
    // A second attempt leaves the old adopted marker intact.
    view.rerender(<ConversationMessages {...base} history={{ hasOlder: false, loading: false, windows: [], compactions: [{ eventId: 'adoption', adoptedAfterEntrySequence: 1, displayAfterEntryId: 'q' }] }} contextCompactionInProgress />);
    expect(screen.getAllByRole('separator', { name: '上下文已压缩' })).toHaveLength(1);
    expect(screen.getByRole('status', { name: '上下文正在压缩' })).toBeTruthy();
  });

  it('removes failed or cancelled progress without creating a completed boundary', () => {
    const base = { artifactOwnerKey: 'session-one', onReadToolArtifact: vi.fn(), onNotify: vi.fn(), messages: [] };
    const view = render(<ConversationMessages {...base} contextCompactionInProgress />);
    view.rerender(<ConversationMessages {...base} />);
    expect(screen.queryByRole('status', { name: '上下文正在压缩' })).toBeNull();
    expect(screen.queryByRole('separator', { name: '上下文已压缩' })).toBeNull();
  });
});


it('keeps child details and visualization reads in the sidebar, with compact navigation in the parent', () => {
  const body = '```pulsara-visualization\nchart.html\n```';
  const read = vi.fn();
  const present = vi.fn();
  const view = render(<PresentationContext.Provider value={present}><ConversationMessages messages={[{
    id: 'parent-call', role: 'assistant', status: 'completed', time: 'now', body: '', subagentRuns: [{
      id: 'task-1', batchId: 'batch-1', label: 'worker', role: '研究', objective: '只在任务详情显示的目标', color: 'blue', status: 'running', summary: '结果' + body,
      activities: [{ id: 'worker-entry', time: '', status: 'running', body, reasoning: [{id:'thinking', kind:'full', body:'任务内部思考'}],
        visualizations: [{ordinal:0,state:'READY',contentDigest:'sha256:'+'a'.repeat(64),contentSize:15,mediaType:'text/html',codec:'utf-8'}] }],
    }],
  }]} artifactOwnerKey="session" onReadToolArtifact={vi.fn()} onNotify={vi.fn()} onReadVisualization={read}/></PresentationContext.Provider>);
  fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
  const group = screen.getByLabelText('任务执行');
  const node = within(group).getByRole('button', { name: '查看任务：worker' });
  expect(node.classList.contains('trace-card__summary')).toBe(true);
  expect(group.textContent).toContain('0 / 1 已结束');
  expect(group.textContent).not.toMatch(/只在任务详情|任务内部思考|chart.html/);
  expect(node.hasAttribute('aria-expanded')).toBe(false);
  fireEvent.click(node);
  expect(present).toHaveBeenLastCalledWith([{ type: 'task', task_id: 'task-1' }]);
  fireEvent.click(within(group).getByRole('button', { name: '打开任务组' }));
  expect(present).toHaveBeenCalledTimes(2);
  expect(read).not.toHaveBeenCalled();
  expect(view.container.querySelector('.subagent-run__body, .subagent-guidance, .inline-visualization')).toBeNull();
});

it('keeps distinct canonical task groups separate and updates their compact progress', () => {
  const present = vi.fn();
  const runs = [{ id: 'a', batchId: 'group-one', label: '甲', role: '研究', status: 'running' as const, color: 'blue' as const, objective: '', activities: [] },
    { id: 'b', batchId: 'group-one', label: '乙', role: '研究', status: 'completed' as const, color: 'blue' as const, objective: '', activities: [] },
    { id: 'c', batchId: 'group-two', label: '丙', role: '研究', status: 'pending' as const, color: 'blue' as const, objective: '', activities: [] }];
  const display = (completed = false) => <PresentationContext.Provider value={present}><ConversationMessages
    messages={[{ id:'parent',role:'assistant',time:'',body:'',subagentRuns: runs.map(run => completed ? {...run,status:'completed' as const} : run) }]}
    artifactOwnerKey="session" onReadToolArtifact={vi.fn()} onNotify={vi.fn()} /></PresentationContext.Provider>;
  const view = render(display());
  fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
  expect(screen.getAllByLabelText('任务执行')).toHaveLength(2);
  expect(screen.getByText('1 / 2 已结束')).toBeTruthy();
  const node = screen.getByRole('button', { name: '查看任务：乙' });
  fireEvent.click(node);
  expect(present).toHaveBeenCalledWith([{type:'task',task_id:'b'}]);
  view.rerender(display(true));
  expect(screen.getByRole('button', { name: '查看任务：乙' })).toBe(node);
  expect(screen.getByText('2 / 2 已结束')).toBeTruthy();
});

describe('bounded history reading and DOM locations', () => {
  const answer = (id: string, group = 'root-one') => ({ id, turnId: group, historyGroupId: group,
    historyWindowId: 'tail', role: 'assistant' as const, assistantKind: 'terminal' as const,
    time: '', body: `正文 ${id}`, rootFinal: true, status: 'completed' as const, contentState: 'ready' as const });
  const history = { hasOlder: true, loading: false,
    windows: [{ id: 'tail', firstEntryId: 'answer', lastEntryId: 'answer', hasOlder: true, hasNewer: false, loading: false }] };

  it('starts with the tail and merges repeated explicit page demands without walking older pages', async () => {
    let finish!: () => void;
    const read = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    const base = props({ isRunning: false, messages: [answer('answer')], history, onLoadHistory: read });
    const view = render(<WorkbenchView {...base} />);
    expect(read).not.toHaveBeenCalled();
    const button = screen.getByRole('button', { name: '加载更早记录' });
    fireEvent.click(button); fireEvent.click(button);
    await waitFor(() => expect(read).toHaveBeenCalledExactlyOnceWith('tail', 'older'));
    await act(async () => finish());
    view.rerender(<WorkbenchView {...base} messages={[answer('older', 'root-old'), answer('answer')]}
      history={{ ...history, windows: [{ ...history.windows[0], firstEntryId: 'older' }] }} />);
    expect(read).toHaveBeenCalledOnce();
    expect(screen.getByText('正文 older')).toBeTruthy();
  });

  it('places paging only at the two edges of the single displayed reading band', async () => {
    const read = vi.fn(async () => {});
    const windows = [{ id: 'anchor', firstEntryId: 'old', lastEntryId: 'old', hasOlder: true, hasNewer: true, loading: false }];
    const view = render(<WorkbenchView {...props({ isRunning: false, messages: [{ ...answer('old', 'root-old'), historyWindowId: 'anchor' }],
      history: { ...history, atLatest: false, windows }, onLoadHistory: read })} />);
    expect(screen.queryByText('中间记录尚未加载')).toBeNull();
    expect(screen.queryByText('后续记录尚未加载')).toBeNull();
    const band = view.container.querySelector('[data-source-entry="old"]')!;
    expect(screen.getByRole('region', {name: '较早记录'}).compareDocumentPosition(band) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(band.compareDocumentPosition(screen.getByRole('region', {name: '后续消息'})) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '继续向后阅读' }));
    await waitFor(() => expect(read).toHaveBeenCalledWith('anchor', 'newer'));
    fireEvent.click(screen.getByRole('button', { name: '加载更早记录' }));
    await waitFor(() => expect(read).toHaveBeenCalledWith('anchor', 'older'));
  });

  it('shows one loading status at the older boundary and restores the keyboard page action afterwards', async () => {
    const read = vi.fn(async () => {});
    const messages = [{ ...answer('middle'), assistantKind: 'tool-request' as const, rootFinal: false,
      body: '中间步骤', historyEarlierUnloaded: true }, answer('answer')];
    const base = { artifactOwnerKey: 'session-one', onReadToolArtifact: vi.fn(), onNotify: vi.fn(), messages, onLoadHistory: read };
    const view = render(<ConversationMessages {...base} history={history} />);
    expect(screen.queryByText('本轮较早过程尚未加载')).toBeNull();
    view.rerender(<ConversationMessages {...base} history={{ ...history, loading: true, windows: [{ ...history.windows[0], loading: true }] }} />);
    expect(screen.getAllByText('正在加载…')).toHaveLength(1);
    expect(screen.queryByText('较早记录尚未加载')).toBeNull();
    expect(screen.queryByText('本轮较早过程尚未加载')).toBeNull();
    expect(screen.queryByRole('button', { name: '加载更早记录' })).toBeNull();
    // The entries request has ended, but its context is still pending. Keep the
    // existing final body and known user content; aggregate the new entry shells.
    view.rerender(<ConversationMessages {...base} messages={[
      { ...answer('pending-one'), historyContextState: 'loading' },
      { ...answer('pending-two'), historyContextState: 'loading' },
      { id: 'known-pending', historyWindowId: 'tail', role: 'user', time: '', body: '',
        historyContextState: 'loading', contentState: 'unloaded', pendingContent: { parts: [{ type: 'text', text: '本地仍可读' }] } },
      ...messages,
    ]} history={{ ...history, windows: [{ ...history.windows[0], loading: false, contextState: 'loading' }] }} />);
    expect(screen.getAllByText('正在加载…')).toHaveLength(1);
    expect(screen.queryByText('正在读取记录分组…')).toBeNull();
    expect(screen.queryByText('本轮较早过程尚未加载')).toBeNull();
    expect(screen.getByText('正文 answer')).toBeTruthy();
    expect(screen.getByText('本地仍可读')).toBeTruthy();
    expect(view.container.querySelector('[data-source-entry="pending-one"]')).toBeNull();
    view.rerender(<ConversationMessages {...base} history={history} />);
    expect(screen.queryByText('正在加载…')).toBeNull();
    expect(screen.queryByText('本轮较早过程尚未加载')).toBeNull();
    const button = screen.getByRole('button', { name: '加载更早记录' });
    button.focus();
    await userEvent.keyboard('{Enter}');
    expect(read).toHaveBeenCalledExactlyOnceWith('tail', 'older');
  });

  it('keeps the real group disclosure identity when an earlier part of the same turn is prepended', () => {
    const process = { ...answer('middle'), assistantKind: 'tool-request' as const, rootFinal: false, body: '中间步骤', historyEarlierUnloaded: true };
    const base = { artifactOwnerKey: 'session-one', onReadToolArtifact: vi.fn(), onNotify: vi.fn() };
    const view = render(<ConversationMessages {...base} messages={[process, answer('answer')]} />);
    expect(screen.queryByText('中间步骤')).toBeNull();
    expect(screen.queryByText('本轮较早过程尚未加载')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    const host = view.container.querySelector('.conversation-run');
    view.rerender(<ConversationMessages {...base} messages={[{ ...process, id: 'earlier', body: '较早步骤' }, process, answer('answer')]} />);
    expect(view.container.querySelector('.conversation-run')).toBe(host);
    expect(screen.getByText('较早步骤')).toBeTruthy();
    expect(screen.getByRole('button', { name: '收起中间过程' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '收起中间过程' }));
    expect(screen.queryByText('较早步骤')).toBeNull();
    expect(screen.queryByText('中间步骤')).toBeNull();
  });

  it('waits for a committed source host after the reader completes, and cancels an older location', async () => {
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { value: scroll, configurable: true });
    const read = vi.fn(async () => {});
    const target = { sessionId: 'session-one', entryId: 'old' };
    const base = props({ isRunning: false, messages: [answer('answer')], onLocateEntry: read });
    const view = render(<WorkbenchView {...base} focusSourceEntry={target} />);
    await waitFor(() => expect(read).toHaveBeenCalledWith('old', expect.any(AbortSignal)));
    expect(scroll).not.toHaveBeenCalled();
    const newerTarget = { sessionId: 'session-one', entryId: 'newer' };
    view.rerender(<WorkbenchView {...base} focusSourceEntry={newerTarget} />);
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    view.rerender(<WorkbenchView {...base} focusSourceEntry={newerTarget} messages={[answer('old', 'old-root'), answer('newer', 'newer-root'), answer('answer')]} />);
    await waitFor(() => expect(scroll).toHaveBeenCalledOnce());
    const scrolled = scroll.mock.instances[0] as HTMLElement;
    expect(scrolled.textContent).toContain('正文 newer');
    expect(read.mock.calls[0][1].aborted).toBe(true);
    delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
  });

  it('requests only intersecting bodies, keeps failures local, and preserves known input until verification', async () => {
    let notify!: IntersectionObserverCallback;
    const observed: Element[] = [];
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: IntersectionObserverCallback) { notify = callback; }
      observe(node: Element) { observed.push(node); }
      disconnect() {}
    });
    const read = vi.fn(async () => {});
    const message = { id: 'input', role: 'user' as const, time: '', body: '', contentState: 'unloaded' as const,
      inputSource: { commandId: 'command', queueItemId: 'queue', deliveryMode: 'new-turn' as const } };
    const local: import('../../frontend/lib/runtime-adapter').LocalPromptSubmission = {
      sessionId: 'session-one', connectionGeneration: 1, commandId: 'command', queueItemId: 'queue',
      deliveryMode: 'new-turn', status: 'synchronizing', displayAsMessage: true, content: textPrompt('保留本地原文'),
    };
    const base = props({ isRunning: false, messages: [message], localSubmissions: [local], onHydrateEntries: read });
    const view = render(<WorkbenchView {...base} />);
    expect(read).not.toHaveBeenCalled();
    expect(screen.getAllByText('保留本地原文')).toHaveLength(1);
    expect(view.container.querySelector('[data-pending-message]')).toBeNull();
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
    act(() => notify([{ target: observed[0], isIntersecting: true }] as IntersectionObserverEntry[], {} as IntersectionObserver));
    await waitFor(() => expect(read).toHaveBeenCalledExactlyOnceWith(['input'], false));
    view.rerender(<WorkbenchView {...base} messages={[{ ...message, historyContextState: 'loading' }]} />);
    expect(screen.getAllByText('保留本地原文')).toHaveLength(1);
    expect(screen.queryByText('正在读取记录分组…')).toBeNull();
    expect(view.container.querySelector('[data-source-entry="input"]')?.getAttribute('data-content-state')).toBe('loading');
    expect(view.container.querySelector('[data-source-entry="input"] > .user-turn')).toBeTruthy();
    view.rerender(<WorkbenchView {...base} messages={[{ ...message, contentState: 'error', contentError: '读取失败' }]} />);
    expect(screen.getAllByText('保留本地原文')).toHaveLength(1);
    expect(screen.getByLabelText('发送给 Pulsara')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '重试正文' }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  });

  it('does not request a final answer’s reasoning until its process is explicitly expanded', async () => {
    const observed: Array<{ node: Element; notify: IntersectionObserverCallback }> = [];
    vi.stubGlobal('IntersectionObserver', class {
      notify: IntersectionObserverCallback;
      constructor(callback: IntersectionObserverCallback) { this.notify = callback; }
      observe(node: Element) { observed.push({ node, notify: this.notify }); }
      disconnect() {}
    });
    const read = vi.fn(async () => {});
    render(<WorkbenchView {...props({ isRunning: false, messages: [{ ...answer('answer'), reasoningContentState: 'unloaded' }], onHydrateEntries: read })} />);
    expect(screen.getByText('正文 answer')).toBeTruthy();
    expect(read).not.toHaveBeenCalled();
    expect(observed).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    expect(observed).toHaveLength(1);
    act(() => observed[0].notify([{ target: observed[0].node, isIntersecting: true }] as IntersectionObserverEntry[], {} as IntersectionObserver));
    await waitFor(() => expect(read).toHaveBeenCalledExactlyOnceWith(['answer'], true));
  });

  it('keeps a tail containing only task records reachable without pretending the conversation is empty', () => {
    const read = vi.fn(async () => {});
    const view = render(<WorkbenchView {...props({ isRunning: false, messages: [], onLoadHistory: read,
      history: { ...history, windows: [{ ...history.windows[0], firstEntryId: undefined, lastEntryId: undefined, entryIds: ['task-entry'] }] } })} />);
    expect(screen.getByText('此区域有已保存记录，暂无主对话正文。')).toBeTruthy();
    expect(screen.getByRole('button', { name: '加载更早记录' })).toBeTruthy();
    expect(view.container.querySelector('.is-welcome')).toBeNull();
    expect(read).not.toHaveBeenCalled();
  });

  it('keeps reading intent when new output arrives and follows only after an explicit return', () => {
    const base = props({ isRunning: false, messages: [answer('answer')] });
    const view = render(<WorkbenchView {...base} />);
    const thread = view.container.querySelector('.thread-scroll')! as HTMLElement;
    Object.defineProperty(thread, 'scrollHeight', { value: 2000, configurable: true });
    Object.defineProperty(thread, 'clientHeight', { value: 500, configurable: true });
    thread.scrollTop = 600;
    fireEvent.wheel(thread, { deltaY: -100 });
    fireEvent.scroll(thread);
    view.rerender(<WorkbenchView {...base} messages={[answer('answer'), answer('new', 'new-root')]} />);
    expect(thread.scrollTop).toBe(600);
    fireEvent.click(screen.getByRole('button', { name: '回到最新' }));
    expect(thread.scrollTop).toBe(2000);
    expect(screen.queryByRole('button', { name: '回到最新' })).toBeNull();
  });

  it('places only context-backed compaction boundaries, never a latest summary outside the loaded window', () => {
    const base = props({ isRunning: false, messages: [answer('answer')], history,
      contextCompaction: { contextBindingRevisionId: 'binding', turnId: 'earlier', sourceThroughSequence: 1, adoptedAfterEntrySequence: 2, acceptedAt: '' } });
    const view = render(<WorkbenchView {...base} />);
    expect(screen.queryByRole('separator', { name: '上下文已压缩' })).toBeNull();
    view.rerender(<WorkbenchView {...base} history={{ ...history, compactions: [{ eventId: 'adoption', adoptedAfterEntrySequence: 2, displayBeforeEntryId: 'answer' }] }} />);
    expect(screen.getAllByRole('separator', { name: '上下文已压缩' })).toHaveLength(1);
  });
});

describe('independent interruption timeline rows', () => {
  const notice = { ownerKind: 'EXECUTED_TURN' as const, ownerId: 'stopped-root', reason: 'USER_STOPPED',
    terminalAtUtc: '2026-10-18T00:00:00Z', displayAfterEntrySequence: 20, displayAfterEntryId: 'feedback' };
  const window = { id: 'feedback-window', firstEntryId: undefined, lastEntryId: undefined,
    entryIds: ['feedback'], hasOlder: true, hasNewer: true, loading: false };

  it('shows a feedback-only page in its own window with both paging directions', async () => {
    const read = vi.fn(async () => {});
    render(<WorkbenchView {...props({ isRunning: false, messages: [], interruptionNotices: [notice], onLoadHistory: read,
      history: { hasOlder: true, loading: false, windows: [window] } })} />);
    expect(screen.getByText('本轮回复已中断。')).toBeTruthy();
    expect(screen.getAllByRole('button', { name: '加载更早记录' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '继续向后阅读' }));
    await waitFor(() => expect(read).toHaveBeenCalledWith('feedback-window', 'newer'));
  });

  it('orders an independent notice by its exact retained window without attaching to an unrelated ROOT', () => {
    const messages = [
      { id: 'before', role: 'user' as const, body: '之前ROOT', time: '', entrySequence: 10, historyWindowId: 'before-window' },
      { id: 'after', role: 'user' as const, body: '之后ROOT', time: '', entrySequence: 30, historyWindowId: 'after-window' },
    ];
    const view = render(<ConversationMessages messages={messages} interruptionNotices={[notice]}
      artifactOwnerKey="one" onReadToolArtifact={vi.fn()} onNotify={vi.fn()}
      history={{ hasOlder: false, loading: false, windows: [
        { ...window, id: 'before-window', entryIds: ['before'], firstEntryId: 'before', lastEntryId: 'before', hasOlder: false },
        window,
        { ...window, id: 'after-window', entryIds: ['after'], firstEntryId: 'after', lastEntryId: 'after', hasNewer: false },
      ] }} />);
    const before = screen.getByText('之前ROOT'); const after = screen.getByText('之后ROOT'); const marker = screen.getByText('本轮回复已中断。');
    expect(before.compareDocumentPosition(marker) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(marker.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(marker.closest('[data-source-entry]')?.getAttribute('data-source-entry')).toBe('feedback');
    expect(view.container.querySelectorAll('[data-interruption-owner]')).toHaveLength(1);
  });
});

describe('activity and pending-input body demands', () => {
  it('does not hydrate child history from the parent task summary', () => {
    const observed: Element[] = [];
    vi.stubGlobal('IntersectionObserver', class { observe(node: Element) { observed.push(node); } disconnect() {} });
    const read = vi.fn(async () => {});
    const present = vi.fn();
    render(<PresentationContext.Provider value={present}><WorkbenchView {...props({ isRunning: false, onHydrateEntries: read, messages: [{
      id: 'final-task', role: 'assistant', assistantKind: 'terminal', time: '', body: '任务完成', rootFinal: true, contentState: 'ready',
      subagentRuns: [{ id: 'worker', label: '审查worker', role: 'builder', color: 'blue', status: 'completed', objective: '核对', activities: [{
        id: 'worker-entry', time: '', body: '', contentState: 'unloaded', sourceEntryIds: ['worker-entry'], reasoningContentState: 'unloaded',
      }] }],
    }] })} /></PresentationContext.Provider>);
    fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
    fireEvent.click(screen.getByRole('button', { name: '查看任务：审查worker' }));
    expect(present).toHaveBeenCalledWith([{type:'task',task_id:'worker'}]);
    expect(observed).toHaveLength(0);
    expect(read).not.toHaveBeenCalled();
  });

  it('retains reader-held known queue text during canonical and context hydration with no local submission', () => {
    render(<WorkbenchView {...props({ isRunning: false, localSubmissions: [], messages: [{
      id: 'consumed-entry', role: 'user', time: '', body: '', contentState: 'loading', historyContextState: 'loading',
      pendingContent: textPrompt('已知队列原文'), inputSource: { commandId: 'old-command', queueItemId: 'old-queue', deliveryMode: 'new-turn' },
    }] })} />);
    expect(screen.getAllByText('已知队列原文')).toHaveLength(1);
    expect(screen.getByText('本地已知内容')).toBeTruthy();
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
  });
});

it.each(['wheel', 'Home'])('restores the semantic anchor after an edge gesture with no scroll event: %s', async gesture => {
  const message = { id: 'kept', role: 'user' as const, time: '', body: '保持位置', historyWindowId: 'tail' };
  const history = { hasOlder: true, loading: false, windows: [{ id: 'tail', firstEntryId: 'kept', lastEntryId: 'kept', entryIds: ['kept'], hasOlder: true, hasNewer: false, loading: false }] };
  const base = props({ isRunning: false, messages: [message], history, onLoadHistory: vi.fn(async () => {}) });
  const view = render(<WorkbenchView {...base} />);
  const thread = view.container.querySelector('.thread-scroll')! as HTMLElement;
  vi.spyOn(thread, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 600, 500));
  const source = screen.getByText('保持位置').closest('article')!;
  let sourceTop = 20;
  vi.spyOn(source, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, sourceTop, 600, 80));
  if (gesture === 'wheel') fireEvent.wheel(thread, { deltaY: -100 });
  else fireEvent.keyDown(thread, { key: 'Home' });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '加载更早记录' })); });
  sourceTop = 220;
  view.rerender(<WorkbenchView {...base} messages={[{ ...message, id: 'earlier', body: '更早内容' }, message]}
    history={{ ...history, windows: [{ ...history.windows[0], firstEntryId: 'earlier', entryIds: ['earlier', 'kept'] }] }} />);
  expect(thread.scrollTop).toBe(200);
});

describe('independent history context errors', () => {
  it('keeps messages and composer usable and retries boundary metadata even without an older page', async () => {
    const retry = vi.fn(async () => {});
    const reconnect = vi.fn();
    const history = { hasOlder: false, loading: false, error: '历史边界查询暂时失败', windows: [{
      id: 'tail', firstEntryId: 'answer', lastEntryId: 'answer', entryIds: ['answer'],
      hasOlder: false, hasNewer: false, loading: false,
    }] };
    const base = props({ isRunning: false, history, onLoadHistory: retry, onReconnect: reconnect,
      messages: [{ id: 'answer', role: 'assistant', assistantKind: 'terminal', rootFinal: true, time: '', body: '正常回复仍然可读' }] });
    const view = render(<WorkbenchView {...base} />);
    expect(screen.getByText('正常回复仍然可读')).toBeTruthy();
    expect(screen.getByLabelText('发送给 Pulsara').getAttribute('contenteditable')).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe('历史边界查询暂时失败');
    fireEvent.click(screen.getByRole('button', { name: '重试历史上下文' }));
    await waitFor(() => expect(retry).toHaveBeenCalledExactlyOnceWith(undefined, 'older'));
    expect(reconnect).not.toHaveBeenCalled();
    view.rerender(<WorkbenchView {...base} history={{ ...history, error: undefined }} />);
    expect(screen.queryByRole('region', { name: '历史上下文读取失败' })).toBeNull();
    expect(screen.getByText('正常回复仍然可读')).toBeTruthy();
  });

  it('keeps a window error in its existing retry region without duplicating a global error', () => {
    render(<ConversationMessages messages={[{ id: 'answer', role: 'user', time: '', body: '正文' }]}
      artifactOwnerKey="one" onReadToolArtifact={vi.fn()} onNotify={vi.fn()} onLoadHistory={vi.fn(async () => {})}
      history={{ hasOlder: false, loading: false, error: '此页上下文读取失败', windows: [{
        id: 'tail', firstEntryId: 'answer', lastEntryId: 'answer', entryIds: ['answer'],
        hasOlder: false, hasNewer: false, loading: false, error: '此页上下文读取失败',
      }] }} />);
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '重试历史上下文' })).toBeNull();
  });
});

describe('source location disclosure demand', () => {
  const final = { id: 'final', role: 'assistant' as const, assistantKind: 'terminal' as const,
    historyGroupId: 'root', historyWindowId: 'tail', turnId: 'root', time: '', body: '最终回复正文', rootFinal: true,
    contentState: 'ready' as const, reasoningContentState: 'unloaded' as const, sourceEntryIds: ['final'] };
  const process = { id: 'process', role: 'assistant' as const, assistantKind: 'tool-request' as const,
    historyGroupId: 'root', historyWindowId: 'tail', turnId: 'root', time: '', body: '',
    contentState: 'unloaded' as const, sourceEntryIds: ['process', 'tool-result'],
    traces: [{ id: 'tool-call', kind: 'read' as const, title: '读取文件', subtitle: '已完成', toolName: 'read_file', status: 'completed' as const, resultEntryId: 'tool-result' }] };

  it('locates a final answer without expanding or hydrating its earlier tools and reasoning', async () => {
    const observe = vi.fn();
    vi.stubGlobal('IntersectionObserver', class { observe = observe; disconnect() {} });
    const hydrate = vi.fn(async () => {});
    const read = vi.fn(async () => {});
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { value: scroll, configurable: true });
    const view = render(<WorkbenchView {...props({ isRunning: false, messages: [process, final], onHydrateEntries: hydrate,
      onLocateEntry: read, focusSourceEntry: { sessionId: 'session-one', entryId: 'final' } })} />);
    await waitFor(() => expect(scroll).toHaveBeenCalledOnce());
    expect(read).toHaveBeenCalledWith('final', expect.any(AbortSignal));
    expect(screen.getByText('最终回复正文')).toBeTruthy();
    expect(screen.getByRole('button', { name: '展开中间过程' })).toBeTruthy();
    expect(view.container.querySelector('[data-source-entry="process"]')).toBeNull();
    expect(view.container.querySelector('[data-source-entry="final:process"]')).toBeNull();
    expect(observe).not.toHaveBeenCalled();
    expect(hydrate).not.toHaveBeenCalled();
    delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
  });

  it('still expands the process for an exact tool-result alias and preserves that choice when locating the final body', () => {
    const base = { messages: [{ ...process, contentState: 'ready' as const }, { ...final, reasoningContentState: 'ready' as const }],
      artifactOwnerKey: 'one', onReadToolArtifact: vi.fn(), onNotify: vi.fn() };
    const view = render(<ConversationMessages {...base} focusSourceEntry={{ sessionId: 'one', entryId: 'tool-result' }} />);
    expect(screen.getByRole('button', { name: '收起中间过程' })).toBeTruthy();
    expect(view.container.querySelector('[data-result-entry="tool-result"]')).toBeTruthy();
    view.rerender(<ConversationMessages {...base} focusSourceEntry={{ sessionId: 'one', entryId: 'final' }} />);
    expect(screen.getByRole('button', { name: '收起中间过程' })).toBeTruthy();
  });

  it('still expands tasks attached to a final answer when the task itself is the target', () => {
    const view = render(<ConversationMessages messages={[{ ...final, reasoningContentState: 'ready', subagentRuns: [{
      id: 'worker', label: '目标worker', role: 'reviewer', objective: '核对', status: 'completed', color: 'blue', activities: [],
    }] }]} artifactOwnerKey="one" onReadToolArtifact={vi.fn()} onNotify={vi.fn()}
      focusTaskId="worker" focusTaskHighlighted focusTaskRevision={1} />);
    expect(screen.getByRole('button', { name: '收起中间过程' })).toBeTruthy();
    expect(view.container.querySelector('[data-task-id="worker"]')).toBeTruthy();
  });
});

it('keeps the same expanded tool card when its committed result arrives and rejects an old result read', async () => {
  let resolveOld!: (page: ToolArtifactPage) => void;
  const read = vi.fn(() => new Promise<ToolArtifactPage>(resolve => { resolveOld = resolve; }));
  const display = (resultEntryId?: string) => <WorkbenchView {...props({
    activeTurnId: 'turn-one', onReadToolArtifact: read, messages: [{
      id: 'tool-request', turnId: 'turn-one', role: 'assistant', time: '', body: '', assistantKind: 'tool-request',
      traces: [{ id: 'call', kind: 'mcp', toolName: 'mcp__demo__read', title: '读取', subtitle: '',
        status: resultEntryId ? 'completed' : 'running', resultEntryId, resultText: resultEntryId ?? 'streamed output',
        artifact: resultEntryId ? { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'HEAD_TAIL' } : undefined,
      }],
    }],
  })} />;
  const view = render(display());
  fireEvent.click(screen.getByRole('button', { name: '展开工具详情：mcp__demo__read' }));
  const button = screen.getByRole('button', { name: '收起工具详情：mcp__demo__read' });
  view.rerender(display('result-one'));
  expect(screen.getByRole('button', { name: '收起工具详情：mcp__demo__read' })).toBe(button);
  fireEvent.click(screen.getByRole('button', { name: '查看完整输出' }));
  view.rerender(display('result-two'));
  await act(async () => resolveOld({ resultEntryId: 'result-one', text: 'stale result page',
    offsetChars: 0, returnedChars: 17, totalChars: 17, hasMore: false }));
  expect(screen.queryByText('stale result page')).toBeNull();
  expect(screen.getByRole('button', { name: '收起工具详情：mcp__demo__read' })).toBe(button);
  expect(screen.getByRole('region', { name: '工具原始结果' }).textContent).toContain('result-two');
});

it('keeps a streamed final visible until exact canonical grouping and body reads finish', async () => {
  vi.stubGlobal('crypto', webcrypto);
  const text = '这段最终回复应当连续显示，直到正式记录准备好。';
  const bytes = Buffer.from(text);
  const digest = `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
  let releaseContext!: () => void, releaseBody!: () => void;
  const contextGate = new Promise<void>(resolve => { releaseContext = resolve; });
  const bodyGate = new Promise<void>(resolve => { releaseBody = resolve; });
  const previous = [{ entry_id: 'request-before-final', turn_id: 'turn-final', entry_sequence: '1',
    entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
    blocks: [{ block_id: 'tool-block', block_kind: 'TOOL_CALL', tool_call_id: 'tool-before-final', tool_name: 'mcp__demo__read' }] },
  { entry_id: 'result-before-final', turn_id: 'turn-final', entry_sequence: '2', entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
    content: { kind: 'INLINE', inline_content: Buffer.from('tool completed').toString('base64') },
    tool_result: { assistant_entry_id: 'request-before-final', tool_call_id: 'tool-before-final', result_state: 'SUCCESS' } }];
  const entry = { entry_id: 'final-entry', turn_id: 'turn-final', entry_sequence: '3',
    entry_kind: 'ASSISTANT_MESSAGE', scope_kind: 'ROOT', root_final: true, fork_eligible: true,
    blocks: [{ block_id: 'text-final', block_kind: 'TEXT', content: {
      kind: 'CANONICAL_BLOB', digest, size: String(bytes.length), codec: 'utf-8', media_type: 'text/plain',
    } }] };
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    let value: unknown;
    if (String(url).endsWith('/connections')) value = {
      connection_id: 'connection-final', connection_generation: 1, session_id: 'session-one', role: 'controller',
      snapshot: { snapshot: { session_id: 'session-one', writer_generation: '1', event_sequence_cut: '2',
        entries: previous, control: { active_turns: [{ turn_id: 'turn-final', scope_kind: 'ROOT', status: 'RUNNING' }] } } },
      live_hello: { live_owner_epoch: '1', live_revision: '1', live_snapshot: { events: [{
        live_revision: '1', event_type: 'TEXT_DELTA', draft_identity: 'draft-final',
        turn_id: 'turn-final', scope_kind: 'ROOT', payload: { text_delta: { delta: text } },
      }] } }, live_control_snapshot: { snapshot: {} },
    };
    else if (String(url).endsWith('/observe')) value = { observation: {
      through_event_sequence: '3', live_owner_epoch: '1', through_live_revision: '2',
      committed: [{ projection_kind: 'IMMUTABLE_ENTRY', entry },
        { projection_kind: 'CURRENT_CONTROL', current_control: { active_turns: [] } }],
      settlements: [{ kind: 'LIVE_GENERATION_COMMITTED', draft_identity: 'draft-final', committed_entry_id: 'final-entry' }],
    } };
    else if (String(url).endsWith('/history/context')) {
      const ids = JSON.parse(String(init?.body)).entry_ids as string[];
      if (ids.includes('final-entry')) await contextGate;
      value = { session_id: 'session-one', cut_sequence: '3', event_sequence_cut: '3', entry_ids: ids,
        groups: [{ owner_kind: 'EXECUTED_TURN', owner_id: 'turn-final', initial_entry_id: 'request-before-final' }],
        entry_contexts: ids.map(id => ({ entry_id: id, owner_kind: 'EXECUTED_TURN', owner_id: 'turn-final',
          root_owner_id: 'turn-final', segment_id: 'turn-final', segment_start_entry_id: 'request-before-final' })),
        tasks: [], batches: [], interruption_notices: [], compactions: [], };
    } else if (String(url).endsWith('/read-content')) {
      await bodyGate;
      value = { content: { digest, complete_size: String(bytes.length), offset_bytes: '0', content: bytes.toString('base64'), complete: true } };
    } else throw new Error(`Unexpected request: ${url}`);
    return new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  const connection = await new LocalHttpRuntimeAdapter().connect('session-one');
  await waitFor(() => expect(connection.current().messages[0].historyContextState).toBe('ready'));
  const display = () => <WorkbenchView {...props({ ...connection.current(),
    onHydrateEntries: async (ids, reasoning) => { await connection.hydrateEntries!(ids, undefined, reasoning); },
  })} />;
  const view = render(display());
  const unsubscribe = connection.subscribe(() => view.rerender(display()));
  try {
    expect(screen.getByText(text)).toBeTruthy();
    expect(screen.getByRole('button', { name: '展开工具详情：mcp__demo__read' })).toBeTruthy();
    await act(async () => { await connection.observe(); view.rerender(display()); });
    expect(connection.current().isRunning).toBe(false);
    expect(screen.getAllByText(text)).toHaveLength(1);
    expect(screen.queryByRole('button', { name: '复制回复' })).toBeNull();
    expect(screen.queryByRole('button', { name: '展开工具详情：mcp__demo__read' })).toBeNull();
    expect(view.container.querySelectorAll('.conversation-run')).toHaveLength(1);
    await act(async () => { releaseContext(); await contextGate; });
    await waitFor(() => expect(connection.current().messages.find(message => message.id === 'final-entry')?.historyContextState).toBe('ready'));
    expect(screen.getAllByText(text)).toHaveLength(1);
    await act(async () => { releaseBody(); await bodyGate; });
    await waitFor(() => expect(screen.getByRole('button', { name: '复制回复' })).toBeTruthy());
    expect(screen.getAllByText(text)).toHaveLength(1);
    expect(connection.current().messages.find(message => message.id === 'final-entry')).not.toHaveProperty('pendingAssistant');
  } finally { releaseContext(); releaseBody(); unsubscribe(); }
});

it('keeps tail-follow aligned while queue animations resize the viewport between stream updates', () => {
  const observations = new Map<Element, () => void>();
  vi.stubGlobal('ResizeObserver', class {
    constructor(private callback: () => void) {}
    observe(node: Element) { observations.set(node, this.callback); }
    disconnect() {} unobserve() {}
  });
  const view = render(<WorkbenchView {...props({ activeTurnId: 'turn-one', messages: [{
    id: 'prompt', turnId: 'turn-one', role: 'user', time: '', body: '开始处理',
  }] })} />);
  const thread = view.container.querySelector('.thread-scroll')! as HTMLElement;
  let viewportHeight = 500, top = 1500;
  Object.defineProperties(thread, {
    scrollHeight: { value: 2000, configurable: true },
    clientHeight: { get: () => viewportHeight },
    scrollTop: { get: () => top, set: value => { top = Math.max(0, Math.min(2000 - viewportHeight, value)); } },
  });
  const resizeViewport = () => observations.get(thread)?.();
  // Queue admission animates the composer without changing the message column.
  viewportHeight = 380;
  act(resizeViewport);
  expect(top).toBe(1620);
  expect(screen.queryByRole('button', { name: '回到最新' })).toBeNull();
  // The first thinking delta arrives while the same queue animation is active.
  view.rerender(<WorkbenchView {...props({ activeTurnId: 'turn-one', messages: [
    { id: 'prompt', turnId: 'turn-one', role: 'user', time: '', body: '开始处理' },
    { id: 'live', turnId: 'turn-one', role: 'assistant', assistantKind: 'live',
      time: '', body: '', status: 'running', reasoning: [{ id: 'reason', kind: 'full', body: '先检查', active: true }] },
  ] })} />);
  viewportHeight = 320;
  act(resizeViewport);
  expect(top).toBe(1680);
  // A reader who scrolls away keeps that intent through later viewport changes.
  fireEvent.wheel(thread, { deltaY: -100 });
  top = 900;
  fireEvent.scroll(thread);
  viewportHeight = 500;
  act(resizeViewport);
  expect(top).toBe(900);
  expect(screen.getByRole('button', { name: '回到最新' })).toBeTruthy();
});

it('restores explicit process, tool and reasoning choices after their history page is reread', () => {
 const messages: ComponentProps<typeof WorkbenchView>['messages']=[
  {id:'process',historyGroupId:'segment:one',role:'assistant',assistantKind:'tool-request',turnId:'turn-one',time:'',body:'处理中',reasoning:[{id:'reason',kind:'full',body:'思考正文'}],traces:[{id:'tool',kind:'mcp',toolName:'mcp__demo__read',title:'读取',subtitle:'完成',status:'completed',resultText:'tool body'}]},
  {id:'answer',historyGroupId:'segment:one',role:'assistant',assistantKind:'terminal',turnId:'turn-one',rootFinal:true,time:'',body:'最终正文'},
 ];
 const base=props({messages,isRunning:false});
 const view=render(<WorkbenchView {...base}/>);
 fireEvent.click(screen.getByRole('button',{name:'展开中间过程'}));
 fireEvent.click(screen.getByRole('button',{name:'展开工具详情：mcp__demo__read'}));
 fireEvent.click(screen.getByRole('button',{name:'展开思考'}));
 view.rerender(<WorkbenchView {...base} messages={[]}/>);
 expect(screen.queryByText('tool body')).toBeNull();
 view.rerender(<WorkbenchView {...base} messages={messages.map(message=>({...message,historyWindowId:'reread-window'}))}/>);
 expect(screen.getByRole('button',{name:'收起中间过程'})).toBeTruthy();
 expect(screen.getByRole('button',{name:'收起工具详情：mcp__demo__read'})).toBeTruthy();
 expect(screen.getByRole('button',{name:'收起思考'})).toBeTruthy();
 view.rerender(<WorkbenchView {...base} session={{...base.session,id:'other-session'}}/>);
 expect(screen.getByRole('button',{name:'展开中间过程'})).toBeTruthy();
});

it('reports viewport working-set interests through the existing thread scroll owner', async () => {
 const retain=vi.fn();
 const view=render(<WorkbenchView {...props({onRetainHistory:retain,isRunning:false,messages:[{id:'visible',role:'user',body:'可见正文',time:''},{id:'far',role:'user',body:'远处正文',time:''}]})}/>);
 const thread=view.container.querySelector<HTMLElement>('.thread-scroll')!;
 vi.spyOn(thread,'getBoundingClientRect').mockReturnValue(new DOMRect(0,0,600,400));
 for(const node of thread.querySelectorAll<HTMLElement>('[data-source-entry]')) vi.spyOn(node,'getBoundingClientRect').mockReturnValue(new DOMRect(0,node.dataset.sourceEntry==='visible'?20:4000,600,80));
 fireEvent.scroll(thread);
 await waitFor(()=>expect(retain).toHaveBeenCalledWith(expect.objectContaining({visibleEntryIds:['visible'],pinnedEntryIds:[]})));
 retain.mockClear();
 view.rerender(<WorkbenchView {...props({onRetainHistory:retain,isRunning:false,messages:[
  {id:'collapsed:one',turnId:'same',historyGroupId:'same',role:'assistant',body:'hidden one',time:''},
  {id:'collapsed:two',turnId:'same',historyGroupId:'same',role:'assistant',body:'hidden two',time:''},
 ]})}/>);
 const header=thread.querySelector<HTMLElement>('[data-history-entry]')!;
 expect(header.dataset.historyEntryAliases).toBe('collapsed:one collapsed:two');
 expect(thread.querySelector('[data-source-entry="collapsed:one"]')).toBeNull();
 vi.spyOn(header,'getBoundingClientRect').mockReturnValue(new DOMRect(0,20,600,30));
 fireEvent.scroll(thread);
 await waitFor(()=>expect(retain).toHaveBeenCalledWith(expect.objectContaining({visibleEntryIds:['collapsed:one'],pinnedEntryIds:[]})));
});

it('keeps repeated tool-call and live reasoning IDs isolated by their canonical assistant owner on reread', () => {
 const makeMessages=(owner:string):ComponentProps<typeof WorkbenchView>['messages']=>[
  {id:`process:${owner}`,historyGroupId:`segment:${owner}`,role:'assistant',assistantKind:'tool-request',turnId:`turn:${owner}`,time:'',body:'处理中',reasoning:[{id:'reasoning:0',kind:'full',body:'思考正文'}],traces:[{id:'call_1',kind:'mcp',toolName:'mcp__demo__read',title:'读取',subtitle:'完成',status:'completed',resultText:'tool body'}]},
  {id:`answer:${owner}`,historyGroupId:`segment:${owner}`,role:'assistant',assistantKind:'terminal',turnId:`turn:${owner}`,rootFinal:true,time:'',body:'最终正文'},
 ];
 const base=props({isRunning:false,messages:makeMessages('a')});
 const view=render(<WorkbenchView {...base}/>);
 fireEvent.click(screen.getByRole('button',{name:'展开中间过程'}));
 fireEvent.click(screen.getByRole('button',{name:'展开工具详情：mcp__demo__read'}));
 fireEvent.click(screen.getByRole('button',{name:'展开思考'}));
 view.rerender(<WorkbenchView {...base} messages={makeMessages('b')}/>);
 fireEvent.click(screen.getByRole('button',{name:'展开中间过程'}));
 expect(screen.getByRole('button',{name:'展开工具详情：mcp__demo__read'})).toBeTruthy();
 expect(screen.getByRole('button',{name:'展开思考'})).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'展开工具详情：mcp__demo__read'}));
 fireEvent.click(screen.getByRole('button',{name:'收起工具详情：mcp__demo__read'}));
 view.rerender(<WorkbenchView {...base} messages={makeMessages('a')}/>);
 expect(screen.getByRole('button',{name:'收起工具详情：mcp__demo__read'})).toBeTruthy();
 expect(screen.getByRole('button',{name:'收起思考'})).toBeTruthy();
 view.rerender(<WorkbenchView {...base} messages={makeMessages('b')}/>);
 expect(screen.getByRole('button',{name:'展开工具详情：mcp__demo__read'})).toBeTruthy();
 expect(screen.getByRole('button',{name:'展开思考'})).toBeTruthy();
});

it('keeps manual process and tool expansion at the clicked position, then resumes explicit tail-follow', () => {
  let resizeColumn: () => void = () => {};
  vi.stubGlobal('ResizeObserver', class {
    constructor(private callback: () => void) {}
    observe(node: Element) {if (node.classList.contains('thread-column')) resizeColumn=this.callback;}
    disconnect() {} unobserve() {}
  });
  const messages: ComponentProps<typeof WorkbenchView>['messages']=[
    {id:'process',role:'assistant',assistantKind:'tool-request',turnId:'turn-one',time:'',body:'处理中',traces:[
      {id:'tool',kind:'mcp',toolName:'mcp__demo__read',title:'读取',subtitle:'完成',status:'completed',resultText:'tool body'},
    ]},
    {id:'answer',role:'assistant',assistantKind:'terminal',turnId:'turn-one',rootFinal:true,time:'',body:'最终正文'},
  ];
  const base=props({messages,isRunning:false});
  const view=render(<WorkbenchView {...base}/>);
  const thread=view.container.querySelector('.thread-scroll')! as HTMLElement;
  let height=1000, top=600;
  Object.defineProperties(thread,{scrollHeight:{get:()=>height},clientHeight:{get:()=>400},scrollTop:{get:()=>top,set:value=>{top=Math.max(0,Math.min(height-400,value));},configurable:true}});
  vi.spyOn(thread,'getBoundingClientRect').mockReturnValue(new DOMRect(0,0,600,400));
  const process=screen.getByRole('button',{name:'展开中间过程'});
  vi.spyOn(process,'getBoundingClientRect').mockImplementation(()=>new DOMRect(0,740-top,600,24));
  fireEvent.click(process);
  height=1500;act(()=>resizeColumn());
  expect(top).toBe(600);
  expect(process.getBoundingClientRect().top).toBe(140);
  const tool=screen.getByRole('button',{name:'展开工具详情：mcp__demo__read'});
  vi.spyOn(tool,'getBoundingClientRect').mockImplementation(()=>new DOMRect(0,800-top,600,24));
  fireEvent.click(tool);
  height=1800;act(()=>resizeColumn());
  expect(top).toBe(600);
  // A resize-induced scroll event must not silently re-enable following.
  fireEvent.scroll(thread);height=1900;act(()=>resizeColumn());
  expect(top).toBe(600);
  fireEvent.click(screen.getByRole('button',{name:'回到最新'}));
  expect(top).toBe(1500);
  height=2100;act(()=>resizeColumn());
  expect(top).toBe(1700);
});

it('retains the same process content through exit without starting hidden hydration', async () => {
  const observed: Array<{node:Element,notify:IntersectionObserverCallback}>=[];
  vi.stubGlobal('IntersectionObserver',class {
    constructor(private notify:IntersectionObserverCallback) {}
    observe(node:Element){observed.push({node,notify:this.notify});} disconnect(){}
  });
  const hydrate=vi.fn(async()=>{});
  const view=render(<WorkbenchView {...props({isRunning:false,onHydrateEntries:hydrate,messages:[
    {id:'reasoning',role:'assistant',assistantKind:'progress',turnId:'turn-one',time:'',body:'进展正文',contentState:'ready',reasoningContentState:'unloaded'},
    {id:'final',role:'assistant',assistantKind:'terminal',turnId:'turn-one',rootFinal:true,time:'',body:'最终正文'},
  ]})}/>);
  fireEvent.click(screen.getByRole('button',{name:'展开中间过程'}));
  const host=view.container.querySelector('[data-source-entry="reasoning"]')!;
  const closing=host.closest('.animated-disclosure')!;
  let settle!:()=>void;
  const finished=new Promise<void>(resolve=>{settle=resolve;});
  Object.defineProperty(closing,'getAnimations',{value:()=>[{finished}]});
  expect(host.textContent).toContain('已保存，正文读取中');
  fireEvent.click(screen.getByRole('button',{name:'收起中间过程'}));
  expect(host.isConnected).toBe(true);
  expect(host.textContent).toContain('已保存，正文读取中');
  act(()=>observed.forEach(item=>item.notify([{target:item.node,isIntersecting:true}] as IntersectionObserverEntry[],{} as IntersectionObserver)));
  expect(hydrate).not.toHaveBeenCalled();
  await act(async()=>settle());
  expect(host.isConnected).toBe(false);
  expect(screen.getByText('最终正文')).toBeTruthy();
});

it('keeps tail following when clicking a file reference popover', () => {
  let resizeColumn: () => void = () => {};
  vi.stubGlobal('ResizeObserver', class {
    constructor(private callback: () => void) {}
    observe(node: Element) { if (node.classList.contains('thread-column')) resizeColumn = this.callback; }
    disconnect() {} unobserve() {}
  });
  const view = render(<WorkbenchView {...props({ isRunning: false, messages: [
    { id: 'file-user', role: 'user', time: '', body: '【本地文件："/project/notes.md"】看看这个文件', promptContent: { parts: [{ type: 'text', text: '【本地文件："/project/notes.md"】看看这个文件' }] } },
    { id: 'file-answer', role: 'assistant', assistantKind: 'terminal', rootFinal: true, time: '', body: '已找到。' },
  ] })} />);
  const thread = view.container.querySelector('.thread-scroll')! as HTMLElement;
  let height = 1000, top = 600;
  Object.defineProperties(thread, {
    scrollHeight: { get: () => height }, clientHeight: { get: () => 400 },
    scrollTop: { get: () => top, set: value => { top = Math.max(0, Math.min(height - 400, value)); } },
  });
  fireEvent.click(screen.getByRole('button', { name: '文件：notes.md' }));
  expect(screen.getByRole('group', { name: '文件引用详情' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: '回到最新' })).toBeNull();
  height = 1200; act(() => resizeColumn());
  expect(top).toBe(800);
});

it('resumes tail following on a downward gesture at the bottom even without a scroll event', async () => {
  const view = render(<WorkbenchView {...props({ isRunning: false, messages: [
    { id: 'process-edge', role: 'assistant', assistantKind: 'progress', turnId: 'turn-edge', time: '', body: '处理中' },
    { id: 'answer-edge', role: 'assistant', assistantKind: 'terminal', turnId: 'turn-edge', rootFinal: true, time: '', body: '正文' },
  ] })} />);
  const thread = view.container.querySelector('.thread-scroll')! as HTMLElement;
  let top = 600;
  Object.defineProperties(thread, {
    scrollHeight: { get: () => 1000 }, clientHeight: { get: () => 400 },
    scrollTop: { get: () => top, set: value => { top = Math.max(0, Math.min(600, value)); } },
  });
  // A real in-flow disclosure pauses following even if it adds no height.
  fireEvent.click(screen.getByRole('button', { name: '展开中间过程' }));
  expect(screen.getByRole('button', { name: '回到最新' })).toBeTruthy();
  fireEvent.wheel(thread, { deltaY: 100 });
  await waitFor(() => expect(screen.queryByRole('button', { name: '回到最新' })).toBeNull());
  expect(top).toBe(600);
});

it('keeps tail-follow while scrolling a contained plan document, including its edges', async () => {
  const view = render(<WorkbenchView {...props({isRunning: false, interaction: {id: 'plan-scroll', kind: 'plan-draft', workflowId: 'workflow', workflowRevision: 1}, onReadInteraction: async () => ({kind: 'plan-draft', body: '## Long plan\n\n1. first\n2. second'})})} />);
  await screen.findByRole('heading', {name: 'Long plan'});
  const thread = view.container.querySelector('.thread-scroll')! as HTMLElement;
  const plan = view.container.querySelector('.plan-draft-body')! as HTMLElement;
  plan.style.overflowY = 'auto'; plan.style.overscrollBehaviorY = 'contain';
  Object.defineProperties(thread, {scrollHeight: {value: 2000, configurable: true}, clientHeight: {value: 500, configurable: true}});
  Object.defineProperties(plan, {scrollHeight: {value: 1000, configurable: true}, clientHeight: {value: 300, configurable: true}});
  thread.scrollTop = 1500;
  for (const top of [200, 0]) {
    plan.scrollTop = top;
    fireEvent.wheel(plan.querySelector('li')!, {deltaY: -100});
    fireEvent.scroll(plan);
    expect(screen.queryByRole('button', {name: '回到最新'})).toBeNull();
    expect(thread.scrollTop).toBe(1500);
  }
  fireEvent.click(screen.getByRole('button', {name: '提出修改'}));
  fireEvent.keyDown(screen.getByRole('textbox', {name: '你想调整哪里？'}), {key: 'ArrowUp'});
  expect(screen.queryByRole('button', {name: '回到最新'})).toBeNull();
  fireEvent.wheel(thread, {deltaY: -100});
  expect(screen.getByRole('button', {name: '回到最新'})).toBeTruthy();
});

it('retains the preparation layout until a live draft actually contains activity', () => {
  const base = props({isRunning:true,activeTurnId:'turn-empty',messages:[]});
  const view = render(<WorkbenchView {...base} />);
  const waiting = screen.getByRole('article', {name:'Pulsara 处理状态'});
  view.rerender(<WorkbenchView {...base} messages={[{id:'live-empty',turnId:'turn-empty',role:'assistant',assistantKind:'live',status:'running',time:'',body:''}]} />);
  expect(screen.getByRole('article', {name:'Pulsara 处理状态'})).toBe(waiting);
  expect(view.container.querySelector('.conversation-run')).toBeNull();
  view.rerender(<WorkbenchView {...base} messages={[{id:'live-empty',turnId:'turn-empty',role:'assistant',assistantKind:'live',status:'running',time:'',body:'开始分析'}]} />);
  expect(screen.queryByRole('article', {name:'Pulsara 处理状态'})).toBeNull();
  expect(screen.getByText('开始分析')).toBeTruthy();
  expect(view.container.querySelector('.response-actions')).toBeNull();
});

it('preserves thought beside commentary throughout canonical reasoning hydration', () => {
  const reason = [{id:'reason',kind:'full' as const,body:'核对数据口径'}];
  const message = {id:'entry',turnId:'turn-one',role:'assistant' as const,assistantKind:'progress' as const,status:'completed' as const,
    time:'',body:'接下来分析趋势',historyContextState:'ready' as const,contentState:'ready' as const,reasoningContentState:'unloaded' as const,
    pendingAssistant:{body:'接下来分析趋势',reasoning:reason}};
  const base = props({isRunning:true,activeTurnId:'turn-one'});
  const view = render(<WorkbenchView {...base} messages={[message]} />);
  expect(screen.getByText('核对数据口径')).toBeTruthy();
  expect(screen.getByText(message.body)).toBeTruthy();
  expect(screen.queryByText('已保存，正文读取中…')).toBeNull();
  const run = view.container.querySelector('.conversation-run');
  view.rerender(<WorkbenchView {...base} messages={[{...message,pendingAssistant:undefined,reasoning:reason,reasoningContentState:'ready'}]} />);
  expect(screen.getByText('核对数据口径')).toBeTruthy();
  expect(view.container.querySelector('.conversation-run')).toBe(run);
});

it('keeps old-band bottom distinct from latest and leaves the old band in place when return-latest fails', async () => {
  const latest = vi.fn().mockRejectedValue(new Error('暂时无法读取'));
  const notify = vi.fn();
  const history = {atLatest:false, readingOwner:{}, hasOlder:false, loading:false, windows:[{
    id:'old',entryIds:['old'],firstEntryId:'old',lastEntryId:'old',hasOlder:false,hasNewer:true,loading:false,
  }]};
  const view = render(<WorkbenchView {...props({history,isRunning:true,activeTurnId:'running',contextCompactionInProgress:true,
    messages:[{id:'old',role:'user',time:'',body:'旧位置'}],onReadLatestHistory:latest,onNotify:notify,
    localSubmissions:[{sessionId:'session-one',connectionGeneration:1,commandId:'sending',status:'sending',displayAsMessage:true,deliveryMode:'new-turn',content:textPrompt('远处待发送输入')}],
  })} />);
  const thread = view.container.querySelector<HTMLElement>('.thread-scroll')!;
  Object.defineProperty(thread,'scrollHeight',{value:1000,configurable:true});
  Object.defineProperty(thread,'clientHeight',{value:500,configurable:true});
  thread.scrollTop = 500;
  fireEvent.wheel(thread,{deltaY:100}); fireEvent.scroll(thread);
  expect(screen.getByRole('button',{name:'回到最新'})).toBeTruthy();
  expect(screen.queryByText('远处待发送输入')).toBeNull();
  expect(screen.queryByLabelText('Pulsara 处理状态')).toBeNull();
  expect(view.container.querySelector('.context-compaction-divider--progress')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'回到最新'}));
  await waitFor(() => expect(notify).toHaveBeenCalledWith('无法回到最新','暂时无法读取','warning'));
  expect(screen.getByText('旧位置')).toBeTruthy();
  expect(thread.scrollTop).toBe(500);
  expect(screen.getByRole('button',{name:'回到最新'})).toBeTruthy();
});

it('keeps forward loading and retry at the bottom edge only', async () => {
  const load = vi.fn(async () => {});
  const window = {id:'old',entryIds:['old'],hasOlder:true,hasNewer:true,loading:true,boundaryDirection:'newer' as const};
  const base = {messages:[{id:'old',role:'user' as const,body:'旧位置',time:''}],artifactOwnerKey:'session',onReadToolArtifact:vi.fn(),onNotify:vi.fn(),onLoadHistory:load};
  const view = render(<ConversationMessages {...base} history={{hasOlder:true,loading:true,windows:[window]}} />);
  expect(screen.getAllByText('正在加载…')).toHaveLength(1);
  expect(within(screen.getByRole('region',{name:'后续消息'})).getByRole('status')).toBeTruthy();
  view.rerender(<ConversationMessages {...base} history={{hasOlder:true,loading:false,windows:[{...window,loading:false,error:'后续读取失败'}]}} />);
  expect(within(screen.getByRole('region',{name:'较早记录'})).queryByRole('alert')).toBeNull();
  fireEvent.click(within(screen.getByRole('region',{name:'后续消息'})).getByRole('button',{name:'重试'}));
  await waitFor(() => expect(load).toHaveBeenCalledExactlyOnceWith('old','newer'));
});

it('preserves the selected response DOM when cache-page identity changes around the reading band', () => {
  const messages = [{id:'answer',historyGroupId:'group',historyWindowId:'first-page',turnId:'turn',role:'assistant' as const,assistantKind:'terminal' as const,rootFinal:true,time:'',body:'选中的正文保持不变。'}];
  const base = props({messages,isRunning:false});
  const view = render(<WorkbenchView {...base} />);
  const element = screen.getByText('选中的正文保持不变。');
  const range = document.createRange(); range.selectNodeContents(element);
  window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
  view.rerender(<WorkbenchView {...base} messages={messages.map(message => ({...message,historyWindowId:'retained-page'}))} />);
  expect(screen.getByText('选中的正文保持不变。')).toBe(element);
  expect(window.getSelection()!.toString()).toBe('选中的正文保持不变。');
  window.getSelection()!.removeAllRanges();
});
