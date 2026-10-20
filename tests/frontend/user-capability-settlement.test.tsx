import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import { LocalHttpRuntimeAdapter } from '../../frontend/lib/runtime-adapter';
import type { UserCapabilitySnapshot } from '../../frontend/lib/pulsara-types';
import { deferred, FakeAdapter, userCapabilitySnapshot } from './pulsara-app-fixture';

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
const attention = { updatedSessions: 0, pendingSessions: 0, attentionSessions: 1 };
const attentionText = '1 个已打开会话需要稍后重试。';

async function openCapabilities(adapter: FakeAdapter) {
  adapter.sessions = [];
  render(<PulsaraApp adapter={adapter} />);
  fireEvent.click(await screen.findByRole('button', { name: '能力' }));
  await waitFor(() => expect(adapter.inspectUserCapabilities).toHaveBeenCalled());
  await waitFor(() => expect((screen.getByRole('button', { name: '刷新能力' }) as HTMLButtonElement).disabled).toBe(false));
}

it.each([650, 30_000])('serializes a slow explicit reload across the %ims cadence and a subsequent toolbar GET', async cadence => {
  const adapter = new FakeAdapter();
  adapter.inspectUserCapabilities.mockResolvedValue({ ...userCapabilitySnapshot, mcp: {
    ...userCapabilitySnapshot.mcp,
    servers: userCapabilitySnapshot.mcp.servers.map(server => ({ ...server, status: cadence === 650 ? 'connecting' : 'ready' })),
  } });
  await openCapabilities(adapter);
  vi.useFakeTimers();
  await act(async () => fireEvent.focus(window));
  const before = adapter.inspectUserCapabilities.mock.calls.length;
  const reload = deferred<UserCapabilitySnapshot>();
  adapter.refreshUserCapabilities.mockReturnValueOnce(reload.promise);
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '重新加载会话能力' })));
  await act(async () => vi.advanceTimersByTimeAsync(cadence * 2));
  fireEvent.click(screen.getByRole('button', { name: '刷新能力' })); // Disabled until POST settles.
  expect(adapter.inspectUserCapabilities).toHaveBeenCalledTimes(before);
  expect(adapter.refreshUserCapabilities).toHaveBeenCalledTimes(1);

  await act(async () => reload.resolve({ ...userCapabilitySnapshot, adoption: attention }));
  expect(screen.getByText(attentionText).closest('.toast')).toBeTruthy();
  expect(adapter.inspectUserCapabilities).toHaveBeenCalledTimes(before + 1);
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '刷新能力' })));
  expect(adapter.inspectUserCapabilities).toHaveBeenCalledTimes(before + 2);
  expect(screen.getByText(attentionText).closest('.toast')).toBeTruthy();
  await act(async () => vi.advanceTimersByTimeAsync(4200));
  expect(screen.queryByText(attentionText)).toBeNull();
  await act(async () => fireEvent.focus(window));
  expect(screen.queryByText(attentionText)).toBeNull();
  expect(adapter.refreshUserCapabilities).toHaveBeenCalledTimes(1);
});

it('settles one real HTTP reload across navigation and cancels queued old-page reads', async () => {
  const http = new LocalHttpRuntimeAdapter();
  const wire = { roots: [], skills: { status: 'ready', items: [], issues: [], roots: [] },
    plugins: { status: 'ready', items: [], details: [] }, mcp: { servers: [] } };
  const post = deferred<Response>();
  const fetcher = vi.fn<typeof fetch>(async (path, init) => {
    if (String(path) === '/api/capabilities/refresh') return post.promise;
    expect(String(path)).toBe('/api/capabilities');
    expect(init?.signal).toBeInstanceOf(AbortSignal);
    return new Response(JSON.stringify(wire), { status: 200 });
  });
  vi.stubGlobal('fetch', fetcher);
  const adapter = new FakeAdapter();
  adapter.inspectUserCapabilities.mockImplementation((signal?: AbortSignal) => http.inspectUserCapabilities(signal));
  adapter.refreshUserCapabilities.mockImplementation(() => http.refreshUserCapabilities());
  await openCapabilities(adapter);
  vi.useFakeTimers();
  await act(async () => fireEvent.focus(window));
  const before = fetcher.mock.calls.length;
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '重新加载会话能力' })));
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  await act(async () => fireEvent.focus(window));
  fireEvent.click(screen.getByRole('button', { name: '总览' }));
  fireEvent.click(screen.getByRole('button', { name: '能力' }));
  await act(async () => vi.advanceTimersByTimeAsync(20));
  await act(async () => fireEvent.focus(window));
  expect(fetcher).toHaveBeenCalledTimes(before + 1);
  const [path, request] = fetcher.mock.calls[before];
  expect(path).toBe('/api/capabilities/refresh');
  expect(request).toEqual(expect.objectContaining({ method: 'POST', body: '{}' }));
  expect(request?.signal).toBeUndefined();
  expect(request?.headers).not.toHaveProperty('X-Pulsara-Connection-Id');

  await act(async () => post.resolve(new Response(JSON.stringify({ ...wire, adoption: {
    updated_sessions: 0, pending_sessions: 0, attention_sessions: 1,
  } }), { status: 200 })));
  expect(screen.getByText(attentionText).closest('.toast')).toBeTruthy();
  expect(adapter.refreshUserCapabilities).toHaveBeenCalledTimes(1);
  expect((screen.getByRole('button', { name: '刷新能力' }) as HTMLButtonElement).disabled).toBe(false);
  expect(fetcher.mock.calls.filter(([url]) => String(url) === '/api/capabilities/refresh')).toHaveLength(1);
});

it('does not roll back an intervening mutation when an earlier reload settles', async () => {
  const adapter = new FakeAdapter();
  await openCapabilities(adapter);
  const reload = deferred<UserCapabilitySnapshot>();
  adapter.refreshUserCapabilities.mockReturnValueOnce(reload.promise);
  fireEvent.click(screen.getByRole('button', { name: '重新加载会话能力' }));
  await waitFor(() => expect(adapter.refreshUserCapabilities).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole('tab', { name: /技能/ }));
  fireEvent.click(screen.getByRole('switch', { name: '关闭 personal-pdf' }));
  await screen.findByRole('switch', { name: '开启 personal-pdf' });
  await act(async () => reload.resolve({ ...userCapabilitySnapshot, adoption: attention }));
  expect(screen.getByRole('switch', { name: '开启 personal-pdf' })).toBeTruthy();
  expect(screen.getByText(attentionText).closest('.toast')).toBeTruthy();
});

it('reports a real HTTP MCP mutation once in the existing toast without keeping a page banner', async () => {
  const http = new LocalHttpRuntimeAdapter();
  const pendingText = '2 个会话将在下次模型请求前的安全时机载入。';
  const fetcher = vi.fn<typeof fetch>(async (path, init) => {
    expect(String(path)).toBe('/api/capabilities/mcp/personal-docs');
    expect(init?.method).toBe('PUT');
    expect(JSON.parse(init!.body as string)).toEqual(expect.objectContaining({
      expected_identity: 'current-personal-docs', config: expect.objectContaining({ enabled: false }),
    }));
    return new Response(JSON.stringify({
      operation: { status: 'UPDATED', success: true, message: 'MCP 配置已保存。' },
      capabilities: { roots: [], skills: { status: 'ready', items: [], issues: [], roots: [] },
        plugins: { status: 'ready', items: [], details: [] }, mcp: { servers: [] } },
      adoption: { updated_sessions: 0, pending_sessions: 2, attention_sessions: 0 },
    }), { status: 200 });
  });
  vi.stubGlobal('fetch', fetcher);
  const adapter = new FakeAdapter();
  adapter.updateUserMcp.mockImplementation((input, expected) => http.updateUserMcp(input, expected));
  const connect = vi.spyOn(adapter, 'connect');
  await openCapabilities(adapter);
  fireEvent.click(screen.getByRole('tab', { name: /MCP/ }));
  vi.useFakeTimers();
  await act(async () => fireEvent.click(screen.getByRole('switch', { name: '关闭 个人文档' })));
  const toast = screen.getByRole('button', { name: /MCP 服务已关闭/ });
  expect(toast.textContent).toContain(pendingText);
  expect(document.querySelectorAll('.toast')).toHaveLength(1);
  expect(document.querySelector('.capability-page-notice')).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][1]?.headers).not.toHaveProperty('X-Pulsara-Connection-Id');
  expect(connect).not.toHaveBeenCalled();
  await act(async () => vi.advanceTimersByTimeAsync(4200));
  expect(document.querySelector('.toast')).toBeNull();
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '刷新能力' })));
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '总览' })));
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '能力' })));
  await act(async () => fireEvent.focus(window));
  expect(screen.queryByText(new RegExp(pendingText))).toBeNull();
  expect(document.querySelector('.toast')).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it('shows an explicit reload failure once as a toast without masking queued GET results or replaying the POST', async () => {
  const adapter = new FakeAdapter();
  await openCapabilities(adapter);
  vi.useFakeTimers();
  await act(async () => fireEvent.focus(window));
  const reload = deferred<UserCapabilitySnapshot>();
  adapter.refreshUserCapabilities.mockReturnValueOnce(reload.promise);
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '重新加载会话能力' })));
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  adapter.inspectUserCapabilities.mockRejectedValueOnce(new Error('目录暂时读取失败。'));
  await act(async () => reload.reject(new Error('显式能力重载失败，请稍后重试。')));
  expect(screen.getByText('显式能力重载失败，请稍后重试。').closest('.toast')).toBeTruthy();
  expect(screen.getByText('目录暂时读取失败。').closest('.capability-page-notice')).toBeTruthy();
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(adapter.refreshUserCapabilities).toHaveBeenCalledTimes(1);
  expect(screen.queryByText('显式能力重载失败，请稍后重试。')).toBeNull();
  expect(screen.queryByText('目录暂时读取失败。')).toBeNull();
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '重新加载会话能力' })));
  expect(adapter.refreshUserCapabilities).toHaveBeenCalledTimes(2);
  expect(screen.queryByText('显式能力重载失败，请稍后重试。')).toBeNull();
});

it('cancels an active toolbar GET on hide and releases loading after returning', async () => {
  const adapter = new FakeAdapter();
  await openCapabilities(adapter);
  const cancelled = deferred<void>();
  adapter.inspectUserCapabilities.mockImplementationOnce((signal?: AbortSignal) => new Promise((_resolve, reject) => {
    signal!.addEventListener('abort', () => { cancelled.resolve(); reject(signal!.reason); }, { once: true });
  }));
  fireEvent.click(screen.getByRole('button', { name: '刷新能力' }));
  await waitFor(() => expect(adapter.inspectUserCapabilities).toHaveBeenCalledTimes(2));
  const visibility = vi.spyOn(document, 'visibilityState', 'get');
  visibility.mockReturnValue('hidden');
  await act(async () => { fireEvent(document, new Event('visibilitychange')); await cancelled.promise; });
  visibility.mockReturnValue('visible');
  await act(async () => fireEvent(document, new Event('visibilitychange')));
  expect((screen.getByRole('button', { name: '刷新能力' }) as HTMLButtonElement).disabled).toBe(false);
  expect(adapter.inspectUserCapabilities).toHaveBeenCalledTimes(3);
});

function pluginInventory(): UserCapabilitySnapshot {
  const auth = { type: 'oauth', redirect_uri: 'http://127.0.0.1:12345/callback' };
  const connection = { serverId: 'docs', credentialOwner: { kind: 'plugin' as const, scope_key: 'user', plugin_id: 'personal-tools', server_id: 'docs' },
    config: { transport: { type: 'streamable_http', endpoint: 'https://same.example/mcp' }, auth },
    defaults: { transport: { type: 'streamable_http', endpoint: 'https://same.example/mcp' }, auth }, overlay: null };
  return { ...userCapabilitySnapshot, plugins: { ...userCapabilitySnapshot.plugins,
    items: userCapabilitySnapshot.plugins.items.map(plugin => ({ ...plugin, mcpConnections: [connection] })),
  } };
}

it.each(['same', 'changed', 'removed'] as const)('uses the last complete plugin observation after an attention gap: %s', async outcome => {
  const adapter = new FakeAdapter();
  const initial = pluginInventory();
  adapter.inspectUserCapabilities.mockResolvedValue(initial);
  adapter.pluginMcpAuthorization.mockResolvedValueOnce({ state: 'awaiting_user', error: null })
    .mockResolvedValue({ state: 'authorized', error: null });
  await openCapabilities(adapter);
  fireEvent.click(screen.getByRole('button', { name: '查看 Personal Tools' }));
  fireEvent.click(screen.getByRole('button', { name: '配置连接' }));
  vi.useFakeTimers();
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '登录授权' })));
  expect(within(screen.getByRole('dialog', { name: '编辑 MCP 服务' })).getByText('等待浏览器授权')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '取消', exact: true }));
  adapter.inspectUserCapabilities.mockResolvedValue({ ...initial, plugins: {
    status: 'attention', items: [], details: ['Temporary plugin inspection timeout'],
  } });
  await act(async () => fireEvent.focus(window));
  expect(screen.queryByText('Personal Tools')).toBeNull();
  const recovered: UserCapabilitySnapshot = outcome === 'same' ? initial : { ...initial, plugins: {
    ...initial.plugins,
    items: outcome === 'removed' ? [] : initial.plugins.items.map(plugin => ({ ...plugin,
      mcpConnections: plugin.mcpConnections!.map(connection => ({ ...connection,
        config: { ...connection.config, transport: { type: 'streamable_http', endpoint: 'https://changed.example/mcp' } },
      })),
    })),
  } };
  adapter.inspectUserCapabilities.mockResolvedValue(recovered);
  await act(async () => fireEvent.focus(window));
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(adapter.pluginMcpAuthorization).toHaveBeenCalledTimes(outcome === 'same' ? 2 : 1);
  if (outcome !== 'removed') {
    fireEvent.click(screen.getByRole('button', { name: '配置连接' }));
    const dialog = within(screen.getByRole('dialog', { name: '编辑 MCP 服务' }));
    if (outcome === 'same') expect(dialog.getByText('已授权')).toBeTruthy();
    else expect(dialog.queryByText('等待浏览器授权')).toBeNull();
  } else expect(screen.getByText('还没有安装用户插件。')).toBeTruthy();
});
