import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import type { ModelCallBindingPayload, RuntimeAdapter, RuntimeConnection, RuntimeProjection, SessionEphemeralResults } from './runtime-adapter';

type Context = { host: string; root: string; binding: string; queued: boolean; sequence: number; compacting: boolean; running: boolean };
type Slot = { context: Context; result?: SessionEphemeralResults['suggestion']; blocked: boolean };

function bindingValue(binding: ModelCallBindingPayload | null | undefined): string {
  // Compare complete typed values independent of JSON property insertion order.
  return JSON.stringify(binding ?? null, ['connection_id', 'reasoning', 'kind', 'value', 'enabled', 'tokens']);
}

function contextOf(projection: RuntimeProjection, binding: ModelCallBindingPayload | null | undefined): Context {
  return {
    host: projection.hostSessionId ?? '',
    root: projection.activeTurnId ?? projection.control.latest_root_turn?.turn_id ?? '',
    binding: bindingValue(binding),
    queued: projection.queuedCount > 0,
    sequence: projection.eventSequence,
    running: projection.isRunning,
    compacting: projection.liveControl.root_compaction_in_progress === true,
  };
}

/** App-owned, disposable memory. No storage, provider requests or draft ownership. */
export class SessionSuggestionCache {
  private slots = new Map<string, Slot>();
  private listeners = new Set<() => void>();
  private version = 0;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getVersion = () => this.version;
  private changed() { this.version += 1; for (const listener of this.listeners) listener(); }

  observe(sessionId: string, projection: RuntimeProjection, binding: ModelCallBindingPayload | null | undefined): Slot {
    const context = contextOf(projection, binding);
    const prior = this.slots.get(sessionId);
    if (prior && JSON.stringify(prior.context) === JSON.stringify(context)) return prior;
    const sameRoot = prior?.context.host === context.host && prior.context.root === context.root
      && prior.context.binding === context.binding;
    // A queued input consumed as steer keeps the ROOT identity. Its eventual
    // completion can own a fresh suggestion, while the replaced read owner
    // still rejects every response issued before submission/consumption.
    const settledRunningRoot = sameRoot && prior?.context.running && !context.running && !context.queued;
    const slot: Slot = { context, blocked: Boolean(sameRoot && !settledRunningRoot
      && (prior?.blocked || (!prior?.context.queued && context.queued))) };
    this.slots.set(sessionId, slot);
    this.changed();
    return slot;
  }

  invalidate(sessionId: string) {
    const prior = this.slots.get(sessionId);
    // Replacing the owner retires reads already in flight. A settled ROOT
    // cannot repopulate its old suggestion; a running ROOT may finish a steer.
    if (prior) this.slots.set(sessionId, { context: prior.context, blocked: true });
    this.changed();
  }

  invalidateAll() { for (const sessionId of this.slots.keys()) this.invalidate(sessionId); }

  forget(sessionId: string) { this.slots.delete(sessionId); this.changed(); }

  accept(sessionId: string, owner: Slot, value: SessionEphemeralResults): boolean {
    if (this.slots.get(sessionId) !== owner || owner.blocked || owner.context.queued
      || value.session_id !== sessionId || value.host_session_id !== owner.context.host) return false;
    const suggestion = value.suggestion;
    if (suggestion.state === 'ready' && (
      !suggestion.text || !suggestion.final_entry_id || !suggestion.turn_id
      || suggestion.turn_id !== owner.context.root
      || bindingValue(suggestion.model_call_binding) !== owner.context.binding
    )) return false;
    owner.result = suggestion;
    this.changed();
    return true;
  }

  text(sessionId: string, projection: RuntimeProjection, binding: ModelCallBindingPayload | null | undefined): string | undefined {
    const slot = this.slots.get(sessionId);
    if (!slot || slot.blocked || projection.isRunning || projection.queuedCount > 0
      || projection.liveControl.root_compaction_in_progress === true
      || JSON.stringify(slot.context) !== JSON.stringify(contextOf(projection, binding))) return undefined;
    return slot.result?.state === 'ready' ? slot.result.text ?? undefined : undefined;
  }
}

/** Reads only existing Host state. Neither opening a page nor polling generates work. */
export function useSessionEphemeralResults({ cache, adapter, connection, projection, binding, visible, suggestionsEnabled = true, refreshSessions }: {
  cache: SessionSuggestionCache;
  adapter: RuntimeAdapter;
  connection: RuntimeConnection | undefined;
  projection: RuntimeProjection;
  binding: ModelCallBindingPayload | null | undefined;
  visible: boolean;
  suggestionsEnabled?: boolean;
  refreshSessions: () => Promise<unknown>;
}) {
  useSyncExternalStore(cache.subscribe, cache.getVersion, cache.getVersion);
  useEffect(() => { if (!suggestionsEnabled) cache.invalidateAll(); }, [cache, suggestionsEnabled]);
  const titleLifetime = useMemo(() => ({ adapter, active: true }), [adapter]);
  const titleReads = useRef(new Map<string, { host: string; abort: AbortController; timer?: ReturnType<typeof setTimeout> }>());
  const adoptedTitles = useRef(new Map<string, string>());
  const refreshRef = useRef(refreshSessions);
  useEffect(() => { refreshRef.current = refreshSessions; }, [refreshSessions]);

  const refreshTitle = useCallback(async (sessionId: string, host: string) => {
    if (adoptedTitles.current.get(sessionId) === host) return;
    adoptedTitles.current.set(sessionId, host);
    try { await refreshRef.current(); }
    catch { adoptedTitles.current.delete(sessionId); }
  }, []);

  // Title observers belong to the user-level metadata owner, not the visible
  // conversation. Switching pages or sessions must not stop an observed title.
  const observeTitle = useCallback((sessionId: string, host: string) => {
    if (!titleLifetime.active) return;
    const previous = titleReads.current.get(sessionId);
    if (previous?.host === host) return;
    previous?.abort.abort();
    clearTimeout(previous?.timer);
    const owner = { host, abort: new AbortController(), timer: undefined as ReturnType<typeof setTimeout> | undefined };
    titleReads.current.set(sessionId, owner);
    const read = async () => {
      owner.timer = undefined;
      try {
        const result = await titleLifetime.adapter.ephemeralResults(sessionId, owner.abort.signal);
        if (owner.abort.signal.aborted || titleReads.current.get(sessionId) !== owner) return;
        if (result.session_id !== sessionId || result.host_session_id !== host) return;
        if (result.title.state === 'pending') {
          owner.timer = setTimeout(() => void read(), 650);
          return;
        }
        if (result.title.state === 'adopted') await refreshTitle(sessionId, host);
      } catch { /* Advisory results have no error UI or automatic recovery. */ }
      finally {
        if (!owner.timer && titleReads.current.get(sessionId) === owner) titleReads.current.delete(sessionId);
      }
    };
    void read();
  }, [titleLifetime, refreshTitle]);

  useEffect(() => {
    titleLifetime.active = true;
    const reads = titleReads.current;
    return () => {
      titleLifetime.active = false;
      for (const owner of reads.values()) { owner.abort.abort(); clearTimeout(owner.timer); }
      reads.clear();
    };
  }, [titleLifetime]);

  const host = projection.hostSessionId;
  const root = projection.activeTurnId ?? projection.control.latest_root_turn?.turn_id;
  const bindingKey = bindingValue(binding);
  const stableBinding = useMemo(() => JSON.parse(bindingKey) as ModelCallBindingPayload | null, [bindingKey]);
  const queued = projection.queuedCount > 0;
  const compacting = projection.liveControl.root_compaction_in_progress === true;
  useEffect(() => {
    if (!connection || !host) return;
    const current = connection.current();
    const title = titleReads.current.get(connection.sessionId);
    if (title && title.host !== host) {
      title.abort.abort();
      clearTimeout(title.timer);
      titleReads.current.delete(connection.sessionId);
    }
    cache.observe(connection.sessionId, current, stableBinding);
  }, [cache, connection, host, root, queued, compacting, stableBinding]);

  useEffect(() => {
    if (!visible || !connection || !host) return;
    const sessionId = connection.sessionId;
    const owner = cache.observe(sessionId, connection.current(), stableBinding);
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const read = async () => {
      try {
        const result = await adapter.ephemeralResults(sessionId, abort.signal);
        if (abort.signal.aborted || connection.current().hostSessionId !== host) return;
        if (result.session_id !== sessionId || result.host_session_id !== host) return;
        if (result.title.state === 'pending') observeTitle(sessionId, host);
        else if (result.title.state === 'adopted') void refreshTitle(sessionId, host);
        cache.observe(sessionId, connection.current(), stableBinding);
        if (suggestionsEnabled) cache.accept(sessionId, owner, result);
        if (suggestionsEnabled && (result.suggestion.state === 'pending'
          || (result.suggestion.state === 'awaiting_completion' && !connection.current().isRunning))) {
          timer = setTimeout(() => void read(), 650);
        }
      } catch { /* A missing ephemeral result never blocks the composer. */ }
    };
    void read();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [adapter, cache, connection, host, root, stableBinding, queued, compacting, projection.isRunning,
    projection.eventSequence, visible, suggestionsEnabled, observeTitle, refreshTitle]);

  return {
    suggestion: suggestionsEnabled && connection ? cache.text(connection.sessionId, projection, binding) : undefined,
    observeTitle,
  };
}
