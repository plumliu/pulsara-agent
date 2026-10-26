import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import type { CommandReceipt, SessionEphemeralResults } from '../../frontend/lib/runtime-adapter';
import { FakeAdapter, initialSession, projection, deferred, renderWorkbench, typeComposer } from './pulsara-app-fixture';

function finished() {
  return { ...projection('正式回答已经完成。'), hostSessionId: 'host-1', isRunning: false,
    activeTurnId: undefined, control: { latest_root_turn: { turn_id: 'turn-1', status: 'COMPLETED' } },
    messages: projection('正式回答已经完成。').messages.map(message => ({ ...message, assistantKind: 'terminal' as const, rootFinal: true, status: 'completed' as const })) };
}
function ready(): SessionEphemeralResults {
  return { session_id: initialSession.id, host_session_id: 'host-1', title: { state: 'absent' },
    suggestion: { state: 'ready', origin_turn_id: 'turn-1', turn_id: 'turn-1', final_entry_id: 'answer-1',
      model_call_binding: initialSession.modelCallBinding!, text: '如何验证刚才的结论？' } };
}

afterEach(() => vi.useRealTimers());

describe('PulsaraApp ephemeral integration', () => {
  it('keeps the final answer and composer usable while a complete suggestion arrives behind a draft', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = finished();
    const pending = deferred<SessionEphemeralResults>();
    const reads = vi.spyOn(adapter, 'ephemeralResults').mockImplementation(() => pending.promise);
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    expect((await screen.findAllByText('正式回答已经完成。')).length).toBeGreaterThan(0);
    await typeComposer('我先写点内容');
    await act(async () => pending.resolve(ready()));
    expect(screen.queryByText('如何验证刚才的结论？')).toBeNull();
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toBe('我先写点内容');
    expect(reads).toHaveBeenCalled();
  });

  it('refreshes a queued first-input title adopted after leaving the conversation page', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(), hostSessionId: 'host-1' };
    const reads = vi.spyOn(adapter, 'ephemeralResults');
    const metadata = vi.spyOn(adapter, 'listSessions');
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    await typeComposer('排队的第一份用户输入');
    fireEvent.keyDown(screen.getByLabelText('发送给 Pulsara'), { key: 'Enter' });
    await waitFor(() => expect(screen.getByLabelText('发送给 Pulsara').textContent).toBe(''));
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    await act(async () => {});
    // The title GET already observed absent before actual queued admission.
    expect(reads).toHaveBeenCalled();
    const count = reads.mock.calls.length;
    vi.useFakeTimers();
    fireEvent.focus(window); // Starts the ordinary metadata observer under this clock.
    await act(async () => {});
    const metadataCount = metadata.mock.calls.length;
    adapter.sessions = adapter.sessions.map(session => ({ ...session, title: '排队输入自动生成的标题' }));
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(metadata).toHaveBeenCalledTimes(metadataCount + 1);
    expect(reads).toHaveBeenCalledTimes(count);
    vi.useRealTimers();
    fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
    expect((await screen.findAllByText('排队输入自动生成的标题')).length).toBeGreaterThan(0);
  });

  it('discards the cached suggestion immediately when manual compaction is submitted', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = finished();
    vi.spyOn(adapter, 'ephemeralResults').mockImplementation(async () => ready());
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByText('如何验证刚才的结论？');
    const pending = deferred<CommandReceipt>();
    adapter.lastConnection!.compactContext = vi.fn(() => pending.promise);
    fireEvent.click(screen.getByRole('button', { name: '压缩上下文' }));
    expect(screen.queryByText('如何验证刚才的结论？')).toBeNull();
    await act(async () => pending.resolve({ commandId: 'compact', status: 'rejected', publicCode: 'COMPACTION_FAILED' }));
    expect(screen.queryByText('如何验证刚才的结论？')).toBeNull();
  });

  it('renders the completed suggestion and immediately invalidates it when a new prompt is submitted', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = finished();
    vi.spyOn(adapter, 'ephemeralResults').mockImplementation(async () => ready());
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByText('如何验证刚才的结论？');
    await typeComposer('验证一下');
    fireEvent.keyDown(screen.getByLabelText('发送给 Pulsara'), { key: 'Enter' });
    await waitFor(() => expect(screen.getByLabelText('发送给 Pulsara').textContent).toBe(''));
    expect(screen.queryByText('如何验证刚才的结论？')).toBeNull();
  });
});


it('turns suggestions off from model settings and does not resurrect the cached result on re-enable', async () => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = finished();
  vi.spyOn(adapter, 'ephemeralResults').mockImplementation(async () => ready());
  const save = vi.spyOn(adapter, 'setInputSuggestionsEnabled');
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByText('如何验证刚才的结论？');
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  fireEvent.click(await screen.findByRole('button', { name: '模型', exact: true }));
  const toggle = await screen.findByRole('switch', { name: '输入框建议' });
  await waitFor(() => expect(toggle).toHaveProperty('disabled', false));
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle.getAttribute('aria-checked')).toBe('false'));
  expect(save).toHaveBeenCalledExactlyOnceWith(false);
  await waitFor(() => expect(toggle).toHaveProperty('disabled', false));
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle.getAttribute('aria-checked')).toBe('true'));
  fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
  await screen.findByLabelText('发送给 Pulsara');
  expect(screen.queryByText('如何验证刚才的结论？')).toBeNull();
});
