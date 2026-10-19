import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import {
  mcpAuthorizationKey, useMcpAuthorization, useModelMcpAuthorization,
} from '../../frontend/components/mcp-authorization';
import type { PulsaraDesktop } from '../../frontend/lib/desktop-host';
import type { McpAuthorizationRequest, McpAuthorizationResult } from '../../frontend/lib/mcp-authorization-result';
import type { RuntimeAdapter, RuntimeConnection } from '../../frontend/lib/runtime-adapter';

const waiting = { state: 'awaiting_user', error: null };
const authorized = { state: 'authorized', error: null };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); delete window.pulsaraDesktop; });

function connection(sessionId: string) {
  let listener: ((request: McpAuthorizationRequest) => void) | undefined;
  const subscribeMcpAuthorization = vi.fn((next: typeof listener) => {
    listener = next;
    return () => { listener = undefined; };
  });
  return {
    value: { sessionId, role: 'controller', subscribeMcpAuthorization } as unknown as RuntimeConnection,
    emit: (request: McpAuthorizationRequest) => listener?.(request),
  };
}

function settlement(scope: McpAuthorizationResult['scope'], kind: 'local' | 'plugin'): McpAuthorizationResult {
  return {
    scope,
    authorization: {
      owner: { kind, scope_key: 'saved-owner', server_id: 'docs', plugin_id: kind === 'plugin' ? 'example' : null },
      config_identity: 'saved-config', display_identity: 'display-config',
      package_install_id: kind === 'plugin' ? 'installed-package' : null,
    },
  };
}

function observe(initialConnection: RuntimeConnection, readStatus: ReturnType<typeof vi.fn>) {
  const notify = vi.fn();
  const adapter = { modelMcpAuthorizationStatus: readStatus } as unknown as RuntimeAdapter;
  return renderHook(({ selected }) => {
    const auth = useMcpAuthorization(notify);
    useModelMcpAuthorization(selected, adapter, auth.run);
    return auth;
  }, { initialProps: { selected: initialConnection } });
}

it.each(['local', 'plugin'] as const)('keeps an accepted USER %s authorization polling when the selected session changes', async kind => {
  const focus = vi.fn().mockResolvedValue(undefined);
  window.pulsaraDesktop = { focus } as unknown as PulsaraDesktop;
  const first = connection('session-a');
  const second = connection('session-b');
  const source = new AbortController();
  const target = settlement('USER', kind);
  const readStatus = vi.fn().mockResolvedValueOnce(waiting).mockResolvedValueOnce(authorized);
  const { result, rerender } = observe(first.value, readStatus);
  await act(async () => first.emit({ signal: source.signal, read: async () => target }));
  const key = kind === 'plugin'
    ? mcpAuthorizationKey('plugin', 'installed-package', 'docs')
    : mcpAuthorizationKey('device', 'docs', 'display-config');
  expect(result.current.statuses[key]).toEqual(waiting);

  rerender({ selected: second.value });
  source.abort(); // Closing A also aborts its result-reader presentation epoch.
  await act(async () => vi.advanceTimersByTimeAsync(1000));

  expect(readStatus.mock.calls).toEqual([[target.authorization], [target.authorization]]);
  expect(result.current.statuses[key]).toEqual(authorized);
  expect(focus).toHaveBeenCalledTimes(1);
});

it('does not accept a USER result whose source connection changed before reading finished', async () => {
  const first = connection('session-a');
  const second = connection('session-b');
  const readStatus = vi.fn().mockResolvedValue(authorized);
  const { result, rerender } = observe(first.value, readStatus);
  let finish!: (value: McpAuthorizationResult) => void;
  const read = new Promise<McpAuthorizationResult>(resolve => { finish = resolve; });
  act(() => first.emit({ signal: new AbortController().signal, read: () => read }));
  rerender({ selected: second.value });
  await act(async () => finish(settlement('USER', 'plugin')));
  expect(readStatus).not.toHaveBeenCalled();
  expect(result.current.statuses).toEqual({});
});

it.each(['local', 'plugin'] as const)('retires a WORKSPACE %s observation with its source connection', async kind => {
  const focus = vi.fn().mockResolvedValue(undefined);
  window.pulsaraDesktop = { focus } as unknown as PulsaraDesktop;
  const first = connection('session-a');
  const second = connection('session-b');
  const readStatus = vi.fn().mockResolvedValueOnce(waiting).mockResolvedValueOnce(authorized);
  const { rerender } = observe(first.value, readStatus);
  await act(async () => first.emit({ signal: new AbortController().signal, read: async () => settlement('WORKSPACE', kind) }));
  rerender({ selected: second.value });
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(readStatus).toHaveBeenCalledTimes(1);
  expect(focus).not.toHaveBeenCalled();
});

it('invalidates the saved owner status and ignores an in-flight response after a connection edit', async () => {
  const focus = vi.fn().mockResolvedValue(undefined);
  window.pulsaraDesktop = { focus } as unknown as PulsaraDesktop;
  let finish!: (value: typeof authorized) => void;
  const request = vi.fn().mockResolvedValueOnce(waiting)
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const { result } = renderHook(() => useMcpAuthorization(vi.fn()));
  const owner = mcpAuthorizationKey('plugin', 'installed-package', 'docs');
  await act(async () => result.current.run(owner, 'login', request));
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  act(() => result.current.invalidate(owner));
  expect(result.current.statuses[owner]).toBeUndefined();
  const currentRequest = vi.fn().mockResolvedValue(waiting);
  await act(async () => result.current.run(owner, 'status', currentRequest));
  await act(async () => finish(authorized));
  expect(result.current.statuses[owner]).toEqual(waiting);
  expect(focus).not.toHaveBeenCalled();
  act(() => result.current.invalidate(owner));
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(result.current.statuses[owner]).toBeUndefined();
  expect(currentRequest).toHaveBeenCalledTimes(1);
});

it('stops an accepted USER observation when the user authorization hook unmounts', async () => {
  const first = connection('session-a');
  const readStatus = vi.fn().mockResolvedValue(waiting);
  const { unmount } = observe(first.value, readStatus);
  await act(async () => first.emit({ signal: new AbortController().signal, read: async () => settlement('USER', 'local') }));
  unmount();
  await act(async () => vi.advanceTimersByTimeAsync(5000));
  expect(readStatus).toHaveBeenCalledTimes(1);
});
