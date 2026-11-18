import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import type { PulsaraDesktop } from '../../frontend/lib/desktop-host';
import { RuntimeApiError, type CommandReceipt, type SessionCloseWork } from '../../frontend/lib/runtime-adapter';
import { deferred, FakeAdapter, initialSession, projection, renderWorkbench, textPrompt } from './pulsara-app-fixture';

const second = {
  ...initialSession,
  id: 'session:agtwi5siitkjqcaje6pp3y6ixu',
  title: '另一会话',
};

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

  it('waits for the selected session to stop before releasing its tab and retains its draft on reopen', async () => {
    const { adapter, first, firstConnection } = await start();
    await userEvent.type(within(first).getByLabelText('发送给 Pulsara'), 'draft after close');
    const work: SessionCloseWork = {
      requiresConfirmation: true, hasTerminalProcesses: true, observationId: 'operation:agtwi5siitkjqcaje6pp3y6ixu',
    };
    adapter.sessionCloseWork.mockResolvedValue(work);
    const stopped = deferred<{ status: 'stopped' }>();
    adapter.stopSessionCloseWork.mockReturnValue(stopped.promise);

    const close = () => userEvent.click(screen.getByRole('button', { name: '关闭会话 准备发布' }));
    await close();
    let dialog = await screen.findByRole('dialog', { name: /关闭「准备发布」/ });
    expect(within(dialog).getByText(/后台终端会继续运行/)).toBeTruthy();
    await userEvent.click(within(dialog).getByRole('button', { name: '继续运行' }));
    expect(adapter.stopSessionCloseWork).not.toHaveBeenCalled();
    expect(first.hidden).toBe(false);

    await close();
    dialog = await screen.findByRole('dialog', { name: /关闭「准备发布」/ });
    await userEvent.click(within(dialog).getByRole('button', { name: '停止并关闭' }));
    expect(adapter.stopSessionCloseWork).toHaveBeenCalledWith(initialSession.id, work);
    expect(within(dialog).getByRole('status').textContent).toMatch(/等待会话任务停止/);
    expect(first.isConnected).toBe(true);
    expect(firstConnection.closed).toBe(false);
    await act(async () => stopped.resolve({ status: 'stopped' }));
    await waitFor(() => expect(first.isConnected).toBe(false));
    expect(firstConnection.closed).toBe(true);

    await userEvent.click(screen.getByRole('button', { name: /准备发布/ }));
    await screen.findByRole('tab', { name: /准备发布/ });
    const reopened = document.getElementById(initialSession.id)!;
    expect(within(reopened).getByLabelText('发送给 Pulsara').textContent).toBe('draft after close');
  });

  it('retains the same window and connection when a confirmed stop fails', async () => {
    const { adapter, first, firstConnection } = await start();
    adapter.sessionCloseWork.mockResolvedValue({
      requiresConfirmation: true, hasTerminalProcesses: false, observationId: 'operation:agtwi5siitkjqcaje6pp3y6ixu',
    });
    adapter.stopSessionCloseWork.mockRejectedValueOnce(new RuntimeApiError('STOP_UNCONFIRMED', '暂时无法确认任务已经停止。', true));
    await userEvent.click(screen.getByRole('button', { name: '关闭会话 准备发布' }));
    const dialog = await screen.findByRole('dialog', { name: /关闭「准备发布」/ });
    await userEvent.click(within(dialog).getByRole('button', { name: '停止并关闭' }));
    expect((await within(dialog).findByRole('alert')).textContent).toBe('暂时无法确认任务已经停止。');
    expect(first.isConnected).toBe(true);
    expect(firstConnection.closed).toBe(false);
    await userEvent.click(within(dialog).getByRole('button', { name: '保留会话' }));
    expect(screen.getByRole('tab', { name: /准备发布/ }).getAttribute('aria-selected')).toBe('true');
  });

  it('keeps a reconnected tab when the previous connection returns a stop result', async () => {
    const { adapter, first, firstConnection } = await start();
    adapter.sessionCloseWork.mockResolvedValue({
      requiresConfirmation: true, hasTerminalProcesses: false, observationId: 'operation:agtwi5siitkjqcaje6pp3y6ixu',
    });
    const stopped = deferred<{ status: 'stopped' }>();
    adapter.stopSessionCloseWork.mockReturnValue(stopped.promise);
    await userEvent.click(screen.getByRole('button', { name: '关闭会话 准备发布' }));
    const dialog = await screen.findByRole('dialog', { name: /关闭「准备发布」/ });
    await userEvent.click(within(dialog).getByRole('button', { name: '停止并关闭' }));

    vi.spyOn(firstConnection, 'observe').mockRejectedValue(new Error('transport disconnected'));
    await act(async () => firstConnection.emit({ ...projection('断开前的回复'), eventSequence: 2 }));
    await waitFor(() => expect(adapter.connectCalls).toHaveLength(2));
    const replacement = adapter.lastConnection!;
    await act(async () => stopped.resolve({ status: 'stopped' }));

    expect((await within(dialog).findByRole('alert')).textContent).toBe('连接已改变，请重新关闭会话。');
    expect(first.isConnected).toBe(true);
    expect(replacement.closed).toBe(false);
  });

  it('retains attachment ownership across focus changes and discards a picker from a closed tab', async () => {
    const { adapter, first } = await start();
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
    adapter.sessionCloseWork.mockResolvedValue({ requiresConfirmation: false, hasTerminalProcesses: false, observationId: null });
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
    const { adapter, first, firstConnection } = await start();
    const receipt = deferred<CommandReceipt>();
    firstConnection.submitPrompt.mockReturnValueOnce(receipt.promise);
    await userEvent.type(within(first).getByLabelText('发送给 Pulsara'), 'pending input');
    await userEvent.click(within(first).getByRole('button', { name: '排队发送' }));
    await waitFor(() => expect(firstConnection.submitPrompt).toHaveBeenCalled());
    adapter.sessionCloseWork.mockResolvedValue({ requiresConfirmation: false, hasTerminalProcesses: false, observationId: null });
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

  it('lets an observer detach without inspecting or stopping delegated work', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionRole = 'observer';
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('tab', { name: /准备发布/ });
    const connection = adapter.lastConnection!;
    await userEvent.click(screen.getByRole('button', { name: '关闭会话 准备发布' }));
    await waitFor(() => expect(connection.closed).toBe(true));
    expect(screen.queryByRole('tab', { name: /准备发布/ })).toBeNull();
    expect(adapter.sessionCloseWork).not.toHaveBeenCalled();
    expect(adapter.stopSessionCloseWork).not.toHaveBeenCalled();
  });
});
