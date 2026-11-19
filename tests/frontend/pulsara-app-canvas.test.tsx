import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import type { PulsaraDesktop } from '../../frontend/lib/desktop-host';
import { CanonicalHistoryView, RuntimeApiError, type CommandReceipt, type RuntimeAdapter, type SessionTabCloseOutcome } from '../../frontend/lib/runtime-adapter';
import { deriveIdentity } from '../../frontend/lib/identity';
import { deferred, FakeAdapter, initialSession, projection, renderWorkbench, textPrompt } from './pulsara-app-fixture';

const second = {
  ...initialSession,
  id: 'session:agtwi5siitkjqcaje6pp3y6ixu',
  title: '另一会话',
};
const closeFirst = () => screen.getByRole('button', { name: '关闭会话 准备发布' });
const closeMenu = () => screen.findByRole('dialog', { name: /关闭「准备发布」/ });
const formId = deriveIdentity('interaction', 'tab-close', 'original');
const nextFormId = deriveIdentity('interaction', 'tab-close', 'next');

async function start() {
  const adapter = new FakeAdapter();
  adapter.sessions.push(second);
  adapter.connectionValues.set(initialSession.id, projection('第一段正在输出'));
  adapter.connectionValues.set(second.id, projection('第二段正在输出'));
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByRole('tab', { name: /准备发布/ });
  const first = document.getElementById(initialSession.id)!;
  return { adapter, first, firstConnection: adapter.lastConnection! };
}

describe('production session canvas', () => {
  it('keeps both streams and window DOM alive while another composer is composing and during user navigation', async () => {
    const { adapter, first, firstConnection } = await start();
    const firstComposer = within(first).getByLabelText('发送给 Pulsara');
    await userEvent.type(firstComposer, 'first draft');

    await userEvent.click(screen.getByRole('button', { name: /另一会话/ }));
    const secondTab = await screen.findByRole('tab', { name: /另一会话/ });
    expect(secondTab.closest('.desktop-titlebar')).toBeTruthy();
    const tabOrder = () => screen.getAllByRole('tab').map(tab => tab.getAttribute('aria-controls'));
    const originalOrder = tabOrder();
    const secondWindow = document.getElementById(second.id)!;
    const secondConnection = adapter.lastConnection!;
    const composer = within(secondWindow).getByLabelText('发送给 Pulsara');
    expect(first.hidden).toBe(true);
    expect(firstConnection.closed).toBe(false);

    await userEvent.click(composer);
    fireEvent.compositionStart(composer);
    await userEvent.type(composer, 'zhongwen', { skipClick: true });
    await act(async () => {
      firstConnection.emit({
        ...projection('后台输出仍在接收'), eventSequence: 2,
        interaction: { id: 'approval', kind: 'tool-confirmation', expiresAtUtc: '2099-01-01T00:00:00Z',
          decisionInProgress: false, prompt: '审批后台操作', options: ['allow', 'deny'] },
      });
      secondConnection.emit({ ...projection('前台输出仍在接收'), eventSequence: 2 });
    });
    expect(within(screen.getByRole('tab', { name: /准备发布/ })).getByLabelText('等待审批')).toBeTruthy();
    expect(document.activeElement).toBe(composer);
    expect(fireEvent.keyDown(composer, { key: 'Enter', isComposing: true })).toBe(true);
    expect(secondConnection.submitPrompt).not.toHaveBeenCalled();
    fireEvent.compositionEnd(composer);
    expect(within(secondWindow).getByText('前台输出仍在接收')).toBeTruthy();

    await userEvent.click(screen.getByRole('tab', { name: /准备发布/ }));
    expect(first.hidden).toBe(false);
    expect(secondWindow.hidden).toBe(false);
    expect(document.getElementById(initialSession.id)).toBe(first);
    expect(within(first).getByLabelText('发送给 Pulsara')).toBe(firstComposer);
    expect(firstComposer.textContent).toBe('first draft');
    expect(within(first).getByText('后台输出仍在接收')).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: '记忆' }));
    expect(screen.queryByRole('tab', { name: /另一会话/ })).toBeNull();
    await act(async () => secondConnection.emit({ ...projection('切页期间的输出'), eventSequence: 3 }));
    await userEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
    expect(tabOrder()).toEqual(originalOrder);
    expect(within(secondWindow).getByText('切页期间的输出')).toBeTruthy();
    expect(within(secondWindow).getByLabelText('发送给 Pulsara')).toBe(composer);
    expect(composer.textContent).toBe('zhongwen');
    await userEvent.click(within(secondWindow).getByRole('button', { name: '排队发送' }));
    await waitFor(() => expect(secondConnection.submitPrompt).toHaveBeenCalledWith(
      expect.stringMatching(/^command:/), textPrompt('zhongwen'), 'bypass-permissions',
    ));
    expect(firstConnection.submitPrompt).not.toHaveBeenCalled();
    expect(adapter.connectCalls.map(call => call.sessionId)).toEqual([initialSession.id, second.id]);
  });

  it('keeps the placed window and its draft while another open fails', async () => {
    const { adapter, first, firstConnection } = await start();
    const composer = within(first).getByLabelText('发送给 Pulsara');
    await userEvent.type(composer, 'preserved');
    const opening = deferred<never>();
    vi.spyOn(adapter, 'connect').mockImplementationOnce(() => opening.promise);
    await userEvent.click(screen.getByRole('button', { name: /另一会话/ }));
    expect(first.hidden).toBe(false);
    expect(screen.queryByRole('tab', { name: /另一会话/ })).toBeNull();
    await act(async () => opening.reject(new RuntimeApiError('TEST_OPEN_FAILED', '会话连接暂时不可用。', true)));
    expect(within(screen.getByRole('button', { name: /另一会话/ })).getByText('会话连接暂时不可用。')).toBeTruthy();
    expect(screen.getByRole('tab', { name: /准备发布/ }).getAttribute('aria-selected')).toBe('true');
    expect(first.hidden).toBe(false);
    expect(firstConnection.closed).toBe(false);
    expect(composer.textContent).toBe('preserved');
  });

  it('keeps reading changes from a minimized conversation out of history residency', async () => {
    const { first, firstConnection } = await start();
    const retainHistory = vi.fn();
    Object.assign(firstConnection, { retainHistory });
    await act(async () => firstConnection.emit({ ...projection('收起之前'), eventSequence: 2 }));
    await waitFor(() => expect(retainHistory).toHaveBeenCalled());

    await userEvent.click(screen.getByRole('button', { name: /另一会话/ }));
    await screen.findByRole('tab', { name: /另一会话/ });
    expect(first.hidden).toBe(true);
    retainHistory.mockClear();
    await act(async () => firstConnection.emit({ ...projection('收起期间仍然接收消息'), eventSequence: 3 }));
    await act(async () => { await new Promise(requestAnimationFrame); });
    expect(retainHistory).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('tab', { name: /准备发布/ }));
    await waitFor(() => expect(retainHistory).toHaveBeenCalled());
    expect(within(first).getByText('收起期间仍然接收消息')).toBeTruthy();
  });

  it('waits for close admission without stopping work and restores the draft in a reopened tab', async () => {
    const { adapter, first, firstConnection } = await start();
    const stop = vi.spyOn(firstConnection, 'stopActiveTurn');
    await userEvent.type(within(first).getByLabelText('发送给 Pulsara'), 'draft after close');
    const closed = deferred<SessionTabCloseOutcome>();
    adapter.closeSessionTab.mockReturnValue(closed.promise);
    await userEvent.click(closeFirst());
    expect(adapter.closeSessionTab).toHaveBeenCalledExactlyOnceWith(firstConnection, {formIds: [], otherClients: false});
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(first.isConnected).toBe(true);
    expect(firstConnection.closed).toBe(false);
    expect(stop).not.toHaveBeenCalled();
    await act(async () => closed.resolve({ status: 'closed' }));
    await waitFor(() => expect(first.isConnected).toBe(false));
    expect(firstConnection.closed).toBe(true);

    await userEvent.click(screen.getByRole('button', { name: /准备发布/ }));
    await screen.findByRole('tab', { name: /准备发布/ });
    const reopened = document.getElementById(initialSession.id)!;
    expect(reopened).not.toBe(first);
    expect(within(reopened).getByLabelText('发送给 Pulsara').textContent).toBe('draft after close');
    expect(adapter.connectCalls.at(-1)).toMatchObject({sessionId: initialSession.id, reopenTab: true});
  });

  it('anchors the confirmation under its own close button and keeps the tab on Escape or outside click', async () => {
    const { adapter, first, firstConnection } = await start();
    adapter.closeSessionTab.mockResolvedValue({status: 'confirmation_required', formIds: [], otherClients: true, visibleForm: false});
    vi.spyOn(closeFirst(), 'getBoundingClientRect').mockReturnValue(new DOMRect(200, 8, 24, 24));
    await userEvent.click(closeFirst());
    let menu = await closeMenu();
    expect(closeFirst().getAttribute('aria-controls')).toBe(menu.id);
    expect(menu.hasAttribute('aria-modal')).toBe(false);
    await waitFor(() => expect(menu.style.top).toBe('38px'));
    expect(within(menu).getByText('此会话也在另一处打开。关闭此处标签后，另一处仍可查看会话。')).toBeTruthy();
    expect(within(menu).getByRole('button', { name: '关闭此处标签' })).toBeTruthy();
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(closeFirst());
    await userEvent.click(closeFirst());
    menu = await closeMenu();
    fireEvent.pointerDown(document.body);
    expect(menu.isConnected).toBe(false);
    expect(adapter.closeSessionTab).toHaveBeenCalledTimes(2);
    expect(first.isConnected).toBe(true);
    expect(firstConnection.closed).toBe(false);
  });

  it('requires fresh confirmation when the server reports a new form or another client', async () => {
    const { adapter, first, firstConnection } = await start();
    adapter.closeSessionTab
      .mockResolvedValueOnce({status: 'confirmation_required', formIds: [formId], otherClients: false, visibleForm: true})
      .mockResolvedValueOnce({status: 'confirmation_required', formIds: [formId, nextFormId], otherClients: true, visibleForm: false});
    await userEvent.click(closeFirst());
    const menu = await closeMenu();
    expect(within(menu).getByText('关闭标签会取消尚未完成的能力配置表单。')).toBeTruthy();
    expect(within(menu).getByRole('button', { name: '返回表单' })).toBeTruthy();
    await userEvent.click(within(menu).getByRole('button', { name: '关闭标签' }));
    expect(await within(menu).findByText('关闭此处标签会取消尚未完成的能力配置表单。另一处仍可查看会话。')).toBeTruthy();
    expect(within(menu).getByRole('button', { name: '保留标签' })).toBeTruthy();
    expect(adapter.closeSessionTab).toHaveBeenCalledTimes(2);
    expect(adapter.closeSessionTab).toHaveBeenLastCalledWith(firstConnection, {formIds: [formId], otherClients: false});
    expect(first.isConnected).toBe(true);
    expect(firstConnection.closed).toBe(false);
    await userEvent.click(within(menu).getByRole('button', { name: '关闭此处标签' }));
    expect(adapter.closeSessionTab).toHaveBeenLastCalledWith(firstConnection, {formIds: [formId, nextFormId], otherClients: true});
    await waitFor(() => expect(first.isConnected).toBe(false));
  });

  it('returns to a minimized tab when choosing to keep its visible form', async () => {
    const { adapter, first } = await start();
    await userEvent.click(screen.getByRole('button', { name: /另一会话/ }));
    await screen.findByRole('tab', { name: /另一会话/ });
    expect(first.hidden).toBe(true);
    adapter.closeSessionTab.mockResolvedValue({status: 'confirmation_required', formIds: [formId], otherClients: false, visibleForm: true});
    await userEvent.click(closeFirst());
    await userEvent.click(within(await closeMenu()).getByRole('button', { name: '返回表单' }));
    expect(first.hidden).toBe(false);
    expect(adapter.closeSessionTab).toHaveBeenCalledOnce();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('keeps the tab and connection when the confirmed close request fails', async () => {
    const { adapter, first, firstConnection } = await start();
    adapter.closeSessionTab
      .mockResolvedValueOnce({status: 'confirmation_required', formIds: [formId], otherClients: false, visibleForm: false})
      .mockRejectedValueOnce(new RuntimeApiError('CLOSE_FAILED', '暂时无法关闭标签。', true));
    await userEvent.click(closeFirst());
    const menu = await closeMenu();
    await userEvent.click(within(menu).getByRole('button', { name: '关闭标签' }));
    expect((await within(menu).findByRole('alert')).textContent).toBe('暂时无法关闭标签。');
    expect(first.isConnected).toBe(true);
    expect(firstConnection.closed).toBe(false);
    await userEvent.click(within(menu).getByRole('button', { name: '保留标签' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('does not reconnect an observation detached before its close response arrives', async () => {
    const { adapter, first, firstConnection } = await start();
    const closed = deferred<SessionTabCloseOutcome>();
    adapter.closeSessionTab.mockReturnValue(closed.promise);
    await userEvent.click(closeFirst());

    vi.spyOn(firstConnection, 'observe').mockRejectedValue(new RuntimeApiError('DETACHED', 'connection detached', true));
    await act(async () => {
      firstConnection.emit({ ...projection('断开前的回复'), eventSequence: 2 });
      await new Promise(resolve => setTimeout(resolve, 500));
    });
    expect(adapter.connectCalls).toHaveLength(1);
    expect(first.isConnected).toBe(true);
    await act(async () => closed.resolve({ status: 'closed' }));
    await waitFor(() => expect(first.isConnected).toBe(false));
    expect(adapter.connectCalls).toHaveLength(1);
  });

  it('queues an explicit reopen of the same tab behind its close response', async () => {
    const { adapter, first, firstConnection } = await start();
    const closed = deferred<SessionTabCloseOutcome>();
    adapter.closeSessionTab.mockReturnValue(closed.promise);
    await userEvent.click(closeFirst());
    await userEvent.click(screen.getByRole('button', { name: /准备发布.*当前会话/ }));
    expect(adapter.connectCalls).toHaveLength(1);
    expect(first.isConnected).toBe(true);
    await act(async () => closed.resolve({ status: 'closed' }));
    await waitFor(() => expect(adapter.connectCalls).toHaveLength(2));
    await screen.findByRole('tab', { name: /准备发布/ });
    expect(document.getElementById(initialSession.id)).not.toBe(first);
    expect(firstConnection.closed).toBe(true);
    expect(adapter.lastConnection?.closed).toBe(false);
    expect(adapter.connectCalls.at(-1)).toMatchObject({reopenTab: true});
  });

  it('waits for reopening and shows saved history with native restart when the old Host could not close', async () => {
    const { adapter, first } = await start();
    await userEvent.click(closeFirst());
    await waitFor(() => expect(first.isConnected).toBe(false));

    const restored = deferred<CanonicalHistoryView>();
    const connect = vi.spyOn(adapter as RuntimeAdapter, 'connect').mockImplementationOnce(() => restored.promise);
    const restartWorkbench = vi.fn(async () => {});
    window.pulsaraDesktop = { platform: 'darwin', restartWorkbench } as unknown as PulsaraDesktop;
    try {
      await userEvent.click(screen.getByRole('button', { name: /准备发布/ }));
      expect(connect).toHaveBeenCalledExactlyOnceWith(initialSession.id, false, true);
      expect(screen.queryByRole('tab', { name: /准备发布/ })).toBeNull();
      const history = new CanonicalHistoryView(initialSession.id, { path: '/tmp/project', outcome: 'AVAILABLE', reason: null },
        { session_id: initialSession.id, entries: [], event_sequence_cut: '1' }, 'ROOT', undefined, undefined, true, true);
      vi.spyOn(history, 'current').mockReturnValue({
        ...projection(''), isRunning: false, activeTurnId: undefined,
        messages: [{ id: 'saved', role: 'assistant', assistantKind: 'terminal', rootFinal: true, status: 'completed', time: '', body: '已保存的历史正文' }],
      });
      vi.spyOn(history, 'retainHistory').mockReturnValue(undefined);
      vi.spyOn(history, 'readQueuePage').mockResolvedValue({ items: [], nextCursor: null });
      await act(async () => restored.resolve(history));
      await screen.findByRole('tab', { name: /准备发布/ });
      expect(await screen.findByText('已保存的历史正文')).toBeTruthy();
      expect(screen.getByText('暂时无法接入会话，已显示保存的历史。')).toBeTruthy();
      expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
      expect(connect).toHaveBeenCalledTimes(1);
      await userEvent.click(screen.getByRole('button', { name: '重启工作台' }));
      expect(restartWorkbench).toHaveBeenCalledOnce();
      await userEvent.click(closeFirst());
      await waitFor(() => expect(screen.queryByRole('tab', { name: /准备发布/ })).toBeNull());
      expect(adapter.closeSessionTab).toHaveBeenCalledOnce();
    } finally {
      delete window.pulsaraDesktop;
    }
  });

  it('continues querying an original approval decision across a close and reopen', async () => {
    const { adapter, first, firstConnection } = await start();
    const approval = {...projection('调用需要批准'), hostSessionId: 'host:fixture', interaction: {
      kind: 'tool-confirmation' as const, id: formId, decisionInProgress: false,
      expiresAtUtc: '2099-01-01T00:00:00Z', prompt: '在工作区内创建文件：/tmp/project/note.txt', options: ['ALLOW', 'DENY'],
    }};
    const original = deferred<CommandReceipt>();
    firstConnection.resolveInteraction.mockReturnValueOnce(original.promise);
    await act(async () => firstConnection.emit(approval));
    await userEvent.click(await screen.findByRole('button', { name: '允许本次操作' }));
    const submitted = firstConnection.resolveInteraction.mock.calls[0][1];
    if (submitted.kind !== 'tool') throw new Error('Expected the original tool decision');
    await userEvent.click(closeFirst());
    await waitFor(() => expect(first.isConnected).toBe(false));

    adapter.connectionValues.set(initialSession.id, approval);
    await userEvent.click(screen.getByRole('button', { name: /准备发布/ }));
    await screen.findByRole('tab', { name: /准备发布/ });
    const reopened = adapter.lastConnection!;
    await waitFor(() => expect(reopened.queryCommand).toHaveBeenCalledWith(submitted.commandId));
    expect((await screen.findByRole('button', { name: '允许本次操作' }) as HTMLButtonElement).disabled).toBe(true);
    expect(reopened.resolveInteraction).not.toHaveBeenCalled();
    await act(async () => {
      original.reject(new RuntimeApiError('DETACHED', '连接已断开。', true));
      reopened.emit({ ...approval, interaction: undefined, liveControlRevision: 2 });
    });
    expect(await screen.findByText('决定结果未确认')).toBeTruthy();
    expect(reopened.closed).toBe(false);
  });

  it('retains attachment ownership across focus changes and discards a picker from a closed tab', async () => {
    const { first } = await start();
    const picked = deferred<string[]>();
    const pickedAfterClose = deferred<string[]>();
    const chooseFiles = vi.fn().mockReturnValueOnce(picked.promise).mockReturnValueOnce(pickedAfterClose.promise);
    window.pulsaraDesktop = { platform: 'darwin', chooseFiles } as unknown as PulsaraDesktop;
    const openPicker = async (window: HTMLElement) => {
      await userEvent.click(within(window).getByRole('button', { name: '添加文件、技能或规划' }));
      await userEvent.click(within(window).getByRole('button', { name: '添加文件' }));
    };

    await openPicker(first);
    await userEvent.click(screen.getByRole('button', { name: /另一会话/ }));
    await screen.findByRole('tab', { name: /另一会话/ });
    const secondWindow = document.getElementById(second.id)!;
    const secondComposer = within(secondWindow).getByLabelText('发送给 Pulsara');
    await userEvent.type(secondComposer, 'separate draft');
    await act(async () => picked.resolve(['/tmp/project/first-session.txt']));
    expect(document.activeElement).toBe(secondComposer);
    expect(secondComposer.textContent).toBe('separate draft');

    await userEvent.click(screen.getByRole('tab', { name: /准备发布/ }));
    expect(within(first).getByLabelText('发送给 Pulsara').textContent).toContain('first-session.txt');
    await openPicker(first);
    await userEvent.click(screen.getByRole('button', { name: '关闭会话 准备发布' }));
    await waitFor(() => expect(first.isConnected).toBe(false));
    await act(async () => pickedAfterClose.resolve(['/tmp/project/after-close.txt']));
    await userEvent.click(screen.getByRole('button', { name: /准备发布/ }));
    await screen.findByRole('tab', { name: /准备发布/ });
    const reopened = document.getElementById(initialSession.id)!;
    expect(within(reopened).getByLabelText('发送给 Pulsara').textContent).toContain('first-session.txt');
    expect(within(reopened).getByLabelText('发送给 Pulsara').textContent).not.toContain('after-close.txt');
  });

  it.each([false, true])('clears only the submitted draft snapshot when acknowledgment arrives after closing, edited=%s', async edited => {
    const { first, firstConnection } = await start();
    const receipt = deferred<CommandReceipt>();
    firstConnection.submitPrompt.mockReturnValueOnce(receipt.promise);
    await userEvent.type(within(first).getByLabelText('发送给 Pulsara'), 'pending input');
    await userEvent.click(within(first).getByRole('button', { name: '排队发送' }));
    await waitFor(() => expect(firstConnection.submitPrompt).toHaveBeenCalled());
    await userEvent.click(screen.getByRole('button', { name: '关闭会话 准备发布' }));
    await waitFor(() => expect(first.isConnected).toBe(false));

    await userEvent.click(screen.getByRole('button', { name: /准备发布/ }));
    await screen.findByRole('tab', { name: /准备发布/ });
    const reopened = document.getElementById(initialSession.id)!;
    const composer = () => within(reopened).getByLabelText('发送给 Pulsara');
    expect(composer().textContent).toBe('pending input');
    if (edited) await userEvent.type(composer(), ' with edits');
    const draftBeforeReceipt = composer().textContent;

    const [commandId] = firstConnection.submitPrompt.mock.calls[0];
    await act(async () => receipt.resolve({ commandId, status: 'succeeded', promptDelivery: {
      queueItemId: 'queue:agtwi5siitkjqcaje6pp3y6ixu', queueStatus: 'PENDING', deliveryMode: 'new-turn',
    } }));
    expect(composer().textContent).toBe(edited ? draftBeforeReceipt : '');
  });

  it('submits the close with the observer connection and keeps work untouched', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionRole = 'observer';
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('tab', { name: /准备发布/ });
    const connection = adapter.lastConnection!;
    const stop = vi.spyOn(connection, 'stopActiveTurn');
    adapter.closeSessionTab.mockResolvedValueOnce({status: 'confirmation_required', formIds: [], otherClients: true, visibleForm: false});
    await userEvent.click(closeFirst());
    await userEvent.click(within(await closeMenu()).getByRole('button', {name: '关闭此处标签'}));
    await waitFor(() => expect(connection.closed).toBe(true));
    expect(screen.queryByRole('tab', { name: /准备发布/ })).toBeNull();
    expect(adapter.closeSessionTab).toHaveBeenNthCalledWith(1, connection, {formIds: [], otherClients: false});
    expect(adapter.closeSessionTab).toHaveBeenNthCalledWith(2, connection, {formIds: [], otherClients: true});
    expect(stop).not.toHaveBeenCalled();
  });
});
