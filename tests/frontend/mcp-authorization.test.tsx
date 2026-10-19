import type { RuntimeAdapter, RuntimeConnection } from '../../frontend/lib/runtime-adapter';
import type { McpAuthorizationRequest } from '../../frontend/lib/mcp-authorization-result';
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { mcpAuthorizationKey, useMcpAuthorization, useModelMcpAuthorization } from '../../frontend/components/mcp-authorization';
import type { PulsaraDesktop } from '../../frontend/lib/desktop-host';

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); delete window.pulsaraDesktop; });
const waiting = { state: 'awaiting_user', error: null };
const authorized = { state: 'authorized', error: null };

it('refreshes the same connection and focuses the desktop only after final authorization', async () => {
  const focus = vi.fn().mockResolvedValue(undefined);
  window.pulsaraDesktop = { focus } as unknown as PulsaraDesktop;
  const request = vi.fn().mockResolvedValueOnce(waiting).mockResolvedValueOnce(waiting).mockResolvedValueOnce(authorized);
  const notify = vi.fn();
  const { result } = renderHook(() => useMcpAuthorization(notify));
  const owner = mcpAuthorizationKey('device', 'example', 'saved-config');
  await act(async () => result.current.run(owner, 'login', request));
  expect(result.current.statuses[owner]).toEqual(waiting);
  expect(focus).not.toHaveBeenCalled();
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(focus).not.toHaveBeenCalled();
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(result.current.statuses[owner]).toEqual(authorized);
  expect(request.mock.calls).toEqual([['login'], ['status'], ['status']]);
  expect(focus).toHaveBeenCalledTimes(1);
  expect(notify).toHaveBeenLastCalledWith('MCP 授权', '已授权', 'success');
  await act(async () => vi.advanceTimersByTimeAsync(10000));
  expect(request).toHaveBeenCalledTimes(3);
});

it('shows failed authorization without focusing the app; a manual status read does not steal focus', async () => {
  const focus = vi.fn().mockResolvedValue(undefined);
  window.pulsaraDesktop = { focus } as unknown as PulsaraDesktop;
  const request = vi.fn().mockResolvedValueOnce(waiting).mockResolvedValueOnce({ state: 'failed', error: '未获许可' }).mockResolvedValueOnce(authorized);
  const { result } = renderHook(() => useMcpAuthorization(vi.fn()));
  await act(async () => result.current.run('owner', 'login', request));
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(result.current.statuses.owner.state).toBe('failed');
  await act(async () => result.current.run('owner', 'status', request));
  expect(result.current.statuses.owner).toEqual(authorized);
  expect(focus).not.toHaveBeenCalled();
});

it('does not adopt an old polling response after logout', async () => {
  const focus = vi.fn().mockResolvedValue(undefined);
  window.pulsaraDesktop = { focus } as unknown as PulsaraDesktop;
  let finish!: (value: typeof authorized) => void;
  const request = vi.fn().mockResolvedValueOnce(waiting)
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValueOnce({ state: 'idle', error: null });
  const { result } = renderHook(() => useMcpAuthorization(vi.fn()));
  await act(async () => result.current.run('owner', 'login', request));
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  await act(async () => result.current.run('owner', 'logout', request));
  await act(async () => finish(authorized));
  expect(result.current.statuses.owner.state).toBe('idle');
  expect(focus).not.toHaveBeenCalled();
});

it('reports an interrupted status read without leaving a permanent waiting label', async () => {
  const request = vi.fn().mockResolvedValueOnce(waiting).mockRejectedValueOnce(new Error('offline'));
  const { result } = renderHook(() => useMcpAuthorization(vi.fn()));
  await act(async () => result.current.run('owner', 'login', request));
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(result.current.statuses.owner.state).toBe('unavailable');
  await act(async () => vi.advanceTimersByTimeAsync(10000));
  expect(request).toHaveBeenCalledTimes(2);
});

it('keeps same-name connections in different scopes separate and stops polling on unmount', async () => {
  const device = mcpAuthorizationKey('device', 'example', 'config');
  const project = mcpAuthorizationKey('project', 'session', 'example', 'config');
  const first = vi.fn().mockResolvedValue(waiting);
  const second = vi.fn().mockResolvedValue(authorized);
  const { result, unmount } = renderHook(() => useMcpAuthorization(vi.fn()));
  await act(async () => result.current.run(device, 'login', first));
  await act(async () => result.current.run(project, 'status', second));
  expect(result.current.statuses[device]).toEqual(waiting);
  expect(result.current.statuses[project]).toEqual(authorized);
  unmount();
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(first).toHaveBeenCalledTimes(1);
});

it.each([
  ['USER', 'local', mcpAuthorizationKey('device', 'notion', 'display')],
  ['WORKSPACE', 'local', mcpAuthorizationKey('project', 'session-test', 'notion', 'display')],
  ['USER', 'plugin', mcpAuthorizationKey('plugin', 'package-test', 'notion')],
  ['WORKSPACE', 'plugin', mcpAuthorizationKey('plugin', 'package-test', 'notion')],
])('refreshes model authorization for %s %s and focuses without issuing another login', async (scope, kind, key) => {
  const focus = vi.fn().mockResolvedValue(undefined);
  window.pulsaraDesktop = { focus } as unknown as PulsaraDesktop;
  let receive!: (request: McpAuthorizationRequest) => void;
  const connection = { role: 'controller', sessionId: 'session-test', subscribeMcpAuthorization: vi.fn(listener => { receive = listener; return vi.fn(); }) } as unknown as RuntimeConnection;
  const readStatus = vi.fn().mockResolvedValue(authorized);
  const adapter = { modelMcpAuthorizationStatus: readStatus } as unknown as RuntimeAdapter;
  const { result } = renderHook(() => {
    const auth = useMcpAuthorization(vi.fn());
    useModelMcpAuthorization(connection, adapter, auth.run);
    return auth;
  });
  const target = { owner: { kind, scope_key: 'scope', server_id: 'notion', plugin_id: kind === 'plugin' ? 'plugin-test' : null }, config_identity: 'config', display_identity: 'display', package_install_id: kind === 'plugin' ? 'package-test' : null };
  await act(async () => receive({ signal: new AbortController().signal, read: async () => ({ scope, authorization: target }) } as McpAuthorizationRequest));
  expect(readStatus).toHaveBeenCalledWith(target);
  expect(result.current.statuses[key]).toEqual(authorized);
  expect(focus).toHaveBeenCalledTimes(1);
});

it('ignores a model completion after its connection closes, and does not focus for a cleared grant', async () => {
  const focus = vi.fn().mockResolvedValue(undefined);
  window.pulsaraDesktop = { focus } as unknown as PulsaraDesktop;
  const request = vi.fn().mockResolvedValue({ state: 'idle', error: null });
  const { result } = renderHook(() => useMcpAuthorization(vi.fn()));
  const lifetime = new AbortController();
  lifetime.abort();
  await act(async () => result.current.run('owner', 'status', request, {returnOnSuccess: true, signal: lifetime.signal}));
  expect(request).not.toHaveBeenCalled();
  await act(async () => result.current.run('owner', 'status', request, {returnOnSuccess: true}));
  expect(focus).not.toHaveBeenCalled();
  expect(result.current.statuses.owner.state).toBe('idle');
});
