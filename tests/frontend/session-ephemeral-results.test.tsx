import { act, cleanup, render, renderHook, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PromptComposer } from '../../frontend/components/prompt-composer';
import { PromptDraftStore } from '../../frontend/lib/prompt-draft';
import { SessionSuggestionCache, useSessionEphemeralResults } from '../../frontend/lib/session-ephemeral-results';
import { LocalHttpRuntimeAdapter, type ModelCallBindingPayload, type RuntimeAdapter, type RuntimeConnection, type RuntimeProjection, type SessionEphemeralResults } from '../../frontend/lib/runtime-adapter';

const binding: ModelCallBindingPayload = { connection_id: 'deepseek', reasoning: { kind: 'effort', value: 'high' } };
const projection = (root = 'turn-1', host = 'host-1'): RuntimeProjection => ({
  messages: [], canonicalInputs: [], isRunning: false, queuedCount: 0, queuedPrompts: [], promptTransitions: [],
  planMode: false, hostSessionId: host, control: { latest_root_turn: { turn_id: root, status: 'COMPLETED' } },
  liveControl: {}, agentTasks: [], eventSequence: 1, liveOwnerEpoch: 1, liveRevision: 1,
  liveControlOwnerEpoch: 1, liveControlRevision: 1,
});
const result = (sessionId = 'session-1'): SessionEphemeralResults => ({
  session_id: sessionId, host_session_id: 'host-1', title: { state: 'absent' },
  suggestion: { state: 'ready', origin_turn_id: 'turn-1', turn_id: 'turn-1', final_entry_id: 'answer-1',
    model_call_binding: binding, text: '如何验证这个假设？' },
});
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
function setup() {
  let current = projection();
  const connection = { sessionId: 'session-1', generation: 1, current: () => current } as RuntimeConnection;
  const read = vi.fn(async (_id: string, _signal?: AbortSignal): Promise<SessionEphemeralResults> => result());
  const cache = new SessionSuggestionCache();
  const props = { cache, adapter: { ephemeralResults: read } as unknown as RuntimeAdapter, connection,
    projection: current, binding, visible: true, refreshSessions: vi.fn(async () => {}) };
  return { cache, read, props, setProjection: (next: RuntimeProjection) => { current = next; return { ...props, projection: next }; } };
}
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('session ephemeral results', () => {
  it('receives a whole suggestion behind an existing draft and reveals it when the draft is cleared', async () => {
    const { props, read } = setup();
    const response = deferred<SessionEphemeralResults>();
    read.mockReturnValue(response.promise);
    const drafts = new PromptDraftStore();
    drafts.insertText('session-1', '已经写下的草稿');
    function Composer() {
      const { suggestion } = useSessionEphemeralResults(props);
      return <PromptComposer store={drafts} sessionId="session-1" disabled={false}
        placeholder={suggestion ?? '默认提示'} onSubmit={vi.fn()} onNotify={vi.fn()} />;
    }
    render(<Composer />);
    expect(screen.getByRole('textbox').textContent).toBe('已经写下的草稿');
    await act(async () => response.resolve(result()));
    expect(screen.queryByText('如何验证这个假设？')).toBeNull();
    expect(screen.getByRole('textbox').textContent).toBe('已经写下的草稿');
    act(() => drafts.getEditor('session-1').commands.clearContent());
    expect(screen.getByText('如何验证这个假设？')).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(1);
  });

  it('retains memory through navigation and uses the cached text while a returning read is pending', async () => {
    const { props, read } = setup();
    const hook = renderHook(useSessionEphemeralResults, { initialProps: props });
    await act(async () => {});
    expect(hook.result.current.suggestion).toBe('如何验证这个假设？');
    hook.rerender({ ...props, visible: false });
    const outstanding = deferred<SessionEphemeralResults>();
    read.mockReturnValue(outstanding.promise);
    hook.rerender({ ...props, connection: { ...props.connection }, visible: true });
    expect(hook.result.current.suggestion).toBe('如何验证这个假设？');
    await act(async () => outstanding.resolve(result()));
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('cancels reads on page exit and rejects late replies when another session is selected', async () => {
    const { props, read } = setup();
    const old = deferred<SessionEphemeralResults>();
    read.mockReturnValueOnce(old.promise).mockResolvedValueOnce({ ...result('session-2'), suggestion: { ...result().suggestion, text: '另一个会话的问题？' } });
    const hook = renderHook(useSessionEphemeralResults, { initialProps: props });
    const signal = read.mock.calls[0][1];
    hook.rerender({ ...props, connection: { ...props.connection, sessionId: 'session-2' } });
    await act(async () => {});
    expect(signal?.aborted).toBe(true);
    expect(hook.result.current.suggestion).toBe('另一个会话的问题？');
    await act(async () => old.resolve(result()));
    expect(hook.result.current.suggestion).toBe('另一个会话的问题？');
  });

  it('retires submitted and queued/steered results even if an old read arrives later', async () => {
    const { props, cache, read, setProjection } = setup();
    const old = deferred<SessionEphemeralResults>();
    read.mockReturnValueOnce(old.promise);
    const hook = renderHook(useSessionEphemeralResults, { initialProps: props });
    act(() => cache.invalidate('session-1'));
    await act(async () => old.resolve(result()));
    expect(hook.result.current.suggestion).toBeUndefined();
    hook.rerender(setProjection({ ...projection(), queuedCount: 1, eventSequence: 2 }));
    await act(async () => {});
    hook.rerender(setProjection({ ...projection(), eventSequence: 3 }));
    await act(async () => {});
    expect(hook.result.current.suggestion).toBeUndefined();
    const next = { ...result(), suggestion: { ...result().suggestion, origin_turn_id: 'turn-2', turn_id: 'turn-2', final_entry_id: 'answer-2', text: '下一轮的问题？' } };
    read.mockResolvedValue(next);
    hook.rerender(setProjection({ ...projection('turn-2'), eventSequence: 4 }));
    await act(async () => {});
    expect(hook.result.current.suggestion).toBe('下一轮的问题？');
  });

  it('shows a fresh same-ROOT suggestion after queued input is consumed as steer, rejecting the old read', async () => {
    const { cache, read, setProjection } = setup();
    const waiting = { ...result(), suggestion: { ...result().suggestion, state: 'awaiting_completion' as const, text: null } };
    const old = deferred<SessionEphemeralResults>();
    read.mockReturnValueOnce(old.promise).mockResolvedValue(waiting);
    const running = { ...projection(), isRunning: true, activeTurnId: 'turn-1',
      control: { latest_root_turn: { turn_id: 'turn-1', status: 'RUNNING' } } };
    const hook = renderHook(useSessionEphemeralResults, { initialProps: setProjection(running) });
    const oldSignal = read.mock.calls[0][1];
    act(() => cache.invalidate('session-1'));
    hook.rerender(setProjection({ ...running, queuedCount: 1, eventSequence: 2 }));
    await act(async () => {});
    act(() => cache.invalidate('session-1')); // Send the queued input as steer.
    hook.rerender(setProjection({ ...running, queuedCount: 0, eventSequence: 3 }));
    await act(async () => {});
    expect(hook.result.current.suggestion).toBeUndefined();
    read.mockResolvedValue({ ...result(), suggestion: { ...result().suggestion,
      final_entry_id: 'answer-after-steer', text: '加入补充要求后该如何验证？' } });
    hook.rerender(setProjection({ ...projection(), eventSequence: 4 }));
    await act(async () => {});
    expect(hook.result.current.suggestion).toBe('加入补充要求后该如何验证？');
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => old.resolve(result()));
    expect(hook.result.current.suggestion).toBe('加入补充要求后该如何验证？');
  });

  it('allows the last queued ROOT to complete when queue removal is observed after ROOT activation', async () => {
    const { props, read, setProjection } = setup();
    const hook = renderHook(useSessionEphemeralResults, { initialProps: props });
    await act(async () => {});
    hook.rerender(setProjection({ ...projection(), queuedCount: 1, eventSequence: 2 }));
    await act(async () => {});
    const next = { ...result(), suggestion: { ...result().suggestion, origin_turn_id: 'turn-2', turn_id: 'turn-2', final_entry_id: 'answer-2', text: '队列完成后的问题？' } };
    read.mockResolvedValue(next);
    hook.rerender(setProjection({ ...projection('turn-2'), isRunning: true, queuedCount: 1, eventSequence: 3 }));
    await act(async () => {});
    hook.rerender(setProjection({ ...projection('turn-2'), isRunning: true, queuedCount: 1, eventSequence: 4 }));
    await act(async () => {});
    hook.rerender(setProjection({ ...projection('turn-2'), eventSequence: 5 }));
    await act(async () => {});
    expect(hook.result.current.suggestion).toBe('队列完成后的问题？');
  });

  it('rejects an in-flight read after same-ROOT canonical advancement before React observes it', async () => {
    const { props, read, setProjection } = setup();
    const old = deferred<SessionEphemeralResults>();
    read.mockReturnValue(old.promise);
    const hook = renderHook(useSessionEphemeralResults, { initialProps: props });
    // A remote steer can advance canonical input without changing ROOT/queue.
    setProjection({ ...projection(), eventSequence: 2 });
    await act(async () => old.resolve(result()));
    expect(hook.result.current.suggestion).toBeUndefined();
  });

  it('fences the canonical-event/manual-compaction settlement race using the existing ROOT flag', async () => {
    const { props, read, setProjection } = setup();
    const hook = renderHook(useSessionEphemeralResults, { initialProps: props });
    await act(async () => {});
    expect(hook.result.current.suggestion).toBe('如何验证这个假设？');
    // Another controller compacts the completed ROOT. The canonical event may
    // arrive while the Host has not yet discarded its old ready slot.
    hook.rerender(setProjection({ ...projection(), eventSequence: 2,
      liveControl: { root_compaction_in_progress: true } }));
    await act(async () => {});
    expect(hook.result.current.suggestion).toBeUndefined();
    read.mockResolvedValue({ ...result(), suggestion: { ...result().suggestion, state: 'absent', text: null } });
    const count = read.mock.calls.length;
    // No new durable event: settling the existing live flag must still read.
    hook.rerender(setProjection({ ...projection(), eventSequence: 2,
      liveControl: { root_compaction_in_progress: false } }));
    await act(async () => {});
    expect(read).toHaveBeenCalledTimes(count + 1);
    expect(hook.result.current.suggestion).toBeUndefined();
  });

  it.each(['model', 'host', 'root', 'delete'] as const)('rejects a late response after %s invalidation', async reason => {
    const { props, cache, read, setProjection } = setup();
    const old = deferred<SessionEphemeralResults>();
    read.mockReturnValue(old.promise);
    const hook = renderHook(useSessionEphemeralResults, { initialProps: props });
    if (reason === 'model') hook.rerender({ ...props, binding: { ...binding, connection_id: 'other-model' } });
    if (reason === 'host') hook.rerender(setProjection(projection('turn-1', 'host-2')));
    if (reason === 'root') hook.rerender(setProjection(projection('turn-2')));
    if (reason === 'delete') act(() => cache.forget('session-1'));
    await act(async () => old.resolve(result()));
    expect(hook.result.current.suggestion).toBeUndefined();
  });

  it('observes awaiting completion and pending only, then stops after a complete result', async () => {
    vi.useFakeTimers();
    const { props, read } = setup();
    read.mockResolvedValueOnce({ ...result(), suggestion: { ...result().suggestion, state: 'awaiting_completion', text: null } })
      .mockResolvedValueOnce({ ...result(), suggestion: { ...result().suggestion, state: 'pending', text: null } })
      .mockResolvedValue(result());
    const hook = renderHook(useSessionEphemeralResults, { initialProps: props });
    await act(async () => {});
    expect(hook.result.current.suggestion).toBeUndefined();
    await act(async () => vi.advanceTimersByTimeAsync(650));
    expect(hook.result.current.suggestion).toBeUndefined();
    await act(async () => vi.advanceTimersByTimeAsync(650));
    expect(hook.result.current.suggestion).toBe('如何验证这个假设？');
    await act(async () => vi.advanceTimersByTimeAsync(6500));
    expect(read).toHaveBeenCalledTimes(3);
  });

  it('keeps observed title metadata refreshing after switching to another page/session', async () => {
    vi.useFakeTimers();
    const { props, read } = setup();
    read.mockResolvedValue({ ...result(), suggestion: { ...result().suggestion, state: 'absent', text: null }, title: { state: 'pending' } });
    const hook = renderHook(useSessionEphemeralResults, { initialProps: props });
    await act(async () => {});
    hook.rerender({ ...props, visible: false, connection: { ...props.connection, sessionId: 'session-2' } });
    read.mockResolvedValue({ ...result(), title: { state: 'adopted' } });
    await act(async () => vi.advanceTimersByTimeAsync(650));
    expect(props.refreshSessions).toHaveBeenCalledOnce();
    expect(read.mock.calls.at(-1)?.[0]).toBe('session-1');
    const count = read.mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(6500));
    expect(read).toHaveBeenCalledTimes(count);
  });

  it.each(['unmount', 'adapter-replacement'] as const)('rejects a late submission title callback after %s', async reason => {
    const { props, read } = setup();
    const hook = renderHook(useSessionEphemeralResults, { initialProps: { ...props, visible: false } });
    const lateObserve = hook.result.current.observeTitle;
    if (reason === 'unmount') hook.unmount();
    else hook.rerender({ ...props, visible: false, adapter: { ephemeralResults: vi.fn() } as unknown as RuntimeAdapter });
    act(() => lateObserve('session-1', 'host-1'));
    await act(async () => {});
    expect(read).not.toHaveBeenCalled();
  });

  it('aborts all observed title reads and stops their timers when the App unmounts', async () => {
    vi.useFakeTimers();
    const { props, read } = setup();
    const pending = deferred<SessionEphemeralResults>();
    read.mockReturnValue(pending.promise);
    const hook = renderHook(useSessionEphemeralResults, { initialProps: { ...props, visible: false } });
    act(() => hook.result.current.observeTitle('session-1', 'host-1'));
    const signal = read.mock.calls[0][1];
    hook.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.resolve({ ...result(), title: { state: 'pending' } }));
    await act(async () => vi.advanceTimersByTimeAsync(6500));
    expect(read).toHaveBeenCalledOnce();
  });

  it('performs a cancellable GET without activating or submitting to a session', async () => {
    const fetch = vi.fn(async (_url: string | URL | Request, _options?: RequestInit) => new Response(JSON.stringify(result()), { headers: { 'content-type': 'application/json' } }));
    vi.stubGlobal('fetch', fetch);
    const abort = new AbortController();
    const value = await new LocalHttpRuntimeAdapter().ephemeralResults('session-1', abort.signal);
    expect(value).toEqual(result());
    expect(fetch).toHaveBeenCalledOnce();
    expect(String(fetch.mock.calls[0][0])).toContain('/api/sessions/session-1/ephemeral-results');
    expect(fetch.mock.calls[0][1]?.method ?? 'GET').toBe('GET');
    expect(fetch.mock.calls[0][1]?.signal).toBe(abort.signal);
  });
});


it('clears all cached suggestions on disable and rejects an outstanding read after re-enable', async () => {
  const { props, cache, read } = setup();
  const other = cache.observe('session-2', projection(), binding);
  cache.accept('session-2', other, result('session-2'));
  const hook = renderHook(useSessionEphemeralResults, { initialProps: { ...props, suggestionsEnabled: true } });
  await act(async () => {});
  expect(hook.result.current.suggestion).toBe('如何验证这个假设？');
  const old = deferred<SessionEphemeralResults>();
  read.mockReturnValue(old.promise);
  hook.rerender({ ...props, connection: { ...props.connection }, suggestionsEnabled: true });
  hook.rerender({ ...props, suggestionsEnabled: false });
  expect(hook.result.current.suggestion).toBeUndefined();
  expect(cache.text('session-2', projection(), binding)).toBeUndefined();
  hook.rerender({ ...props, suggestionsEnabled: true });
  await act(async () => old.resolve(result()));
  expect(hook.result.current.suggestion).toBeUndefined();
});
