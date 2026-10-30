import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import PulsaraApp from '../../frontend/app/pulsara-app';
import { RuntimeApiError, type RuntimeBootstrap } from '../../frontend/lib/runtime-adapter';
import {
  bootstrap, deferred, FakeAdapter, projection, renderWorkbench, typeComposer,
} from './pulsara-app-fixture';

function loadingBootstrap(): RuntimeBootstrap {
  return {
    ...bootstrap,
    model_catalog_loading: true,
    model_configurations: bootstrap.model_configurations.map(model => ({ ...model, status: 'unavailable' })),
  };
}

const pause = (milliseconds: number) => new Promise(resolve => setTimeout(resolve, milliseconds));

describe('initial model catalog observation', () => {
  it('releases startup while loading, preserves actions and draft, then enables the saved model automatically', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(), isRunning: false, activeTurnId: undefined };
    const pending = deferred<RuntimeBootstrap>();
    const read = vi.spyOn(adapter, 'bootstrap')
      .mockResolvedValueOnce(loadingBootstrap())
      .mockImplementationOnce(() => pending.promise)
      .mockResolvedValue(bootstrap);
    const startup = vi.fn();
    renderWorkbench(<PulsaraApp adapter={adapter} onStartupSettled={startup} />);
    await screen.findByText('模型目录正在载入，请稍候。');
    await waitFor(() => expect(startup).toHaveBeenCalledTimes(1));
    await typeComposer('等目录准备后发送');
    expect((screen.getByRole('button', { name: '发送', exact: true }) as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    const sessionReads = adapter.listSessions.mock.calls.length;
    expect((screen.getByRole('button', { name: /新建会话/ }) as HTMLButtonElement).disabled).toBe(false);
    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
    await act(async () => { pending.resolve(bootstrap); });
    await waitFor(() => expect((screen.getByRole('button', { name: '发送', exact: true }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toContain('等目录准备后发送');
    expect(screen.queryByText('模型目录正在载入，请稍候。')).toBeNull();
    expect(adapter.listSessions.mock.calls.length).toBe(sessionReads);
    fireEvent.click(screen.getByRole('button', { name: '发送', exact: true }));
    await waitFor(() => expect(adapter.lastConnection?.submitPrompt).toHaveBeenCalledTimes(1));
    await act(async () => { await pause(350); });
    expect(read).toHaveBeenCalledTimes(2);
    expect(startup).toHaveBeenCalledTimes(1);
  });

  it('stops observing a settled failure without retrying the catalog or scanning sessions', async () => {
    const adapter = new FakeAdapter();
    const failed = { ...loadingBootstrap(), model_catalog_loading: false };
    const read = vi.spyOn(adapter, 'bootstrap').mockResolvedValueOnce(loadingBootstrap()).mockResolvedValue(failed);
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByText('模型目录正在载入，请稍候。');
    await waitFor(() => expect(screen.queryByText('模型目录正在载入，请稍候。')).toBeNull());
    await act(async () => { await pause(350); });
    expect(read).toHaveBeenCalledTimes(2);
    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
  });

  it('observes background recovery and enables the model without a restart', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(), isRunning: false, activeTurnId: undefined };
    const recovering = { ...loadingBootstrap(), model_catalog_loading: false, model_catalog_recovering: true };
    const read = vi.spyOn(adapter, 'bootstrap').mockResolvedValueOnce(recovering).mockResolvedValue(bootstrap);
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    await typeComposer('恢复后发送');
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2), { timeout: 3500 });
    await waitFor(() => expect((screen.getByRole('button', { name: '发送', exact: true }) as HTMLButtonElement).disabled).toBe(false));
  });

  it('resumes catalog observation after a failed poll and successful service reconnect', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(), isRunning: false, activeTurnId: undefined };
    const reconnect = deferred<RuntimeBootstrap>();
    const read = vi.spyOn(adapter, 'bootstrap')
      .mockResolvedValueOnce(loadingBootstrap())
      .mockRejectedValueOnce(new RuntimeApiError('TRANSIENT_NETWORK', 'temporary bootstrap failure', true))
      .mockImplementationOnce(() => reconnect.promise)
      .mockResolvedValue(bootstrap);
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByText('模型目录正在载入，请稍候。');
    await typeComposer('恢复后发送');
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: '记忆', exact: true }));
    fireEvent.click(await screen.findByRole('button', { name: '重新连接', exact: true }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(3));
    // A pending foreground reconnect must not acquire competing background reads.
    await act(async () => { await pause(350); });
    expect(read).toHaveBeenCalledTimes(3);
    await act(async () => { reconnect.resolve(loadingBootstrap()); });
    await waitFor(() => expect(read).toHaveBeenCalledTimes(4));
    fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
    await waitFor(() => expect((screen.getByRole('button', { name: '发送', exact: true }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toContain('恢复后发送');
    expect(screen.queryByText('模型目录正在载入，请稍候。')).toBeNull();
    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
    const sessionReads = adapter.listSessions.mock.calls.length;
    await act(async () => { await pause(350); });
    expect(read).toHaveBeenCalledTimes(4);
    expect(adapter.listSessions.mock.calls.length).toBe(sessionReads);
  });

  it('clears the pending observation timer on unmount', async () => {
    // Keep the first poll pending even when other test workers delay rendering.
    vi.useFakeTimers();
    try {
      const adapter = new FakeAdapter();
      const read = vi.spyOn(adapter, 'bootstrap').mockResolvedValue(loadingBootstrap());
      const view = renderWorkbench(<PulsaraApp adapter={adapter} />);
      await act(async () => { await Promise.resolve(); });
      expect(screen.getByText('模型目录正在载入，请稍候。')).toBeTruthy();
      expect(read).toHaveBeenCalledTimes(1);
      view.unmount();
      await act(async () => { await vi.advanceTimersByTimeAsync(350); });
      expect(read).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
