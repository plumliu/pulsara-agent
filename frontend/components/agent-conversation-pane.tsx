import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { AgentCellError, agentCellApi } from '../lib/agent-cell-api';
import type { AgentConversationReader, QueuedPrompt, RuntimeAdapter, RuntimeProjection } from '../lib/runtime-adapter';
import { agentReaderRequiresReopen, compareSequence } from '../lib/runtime-adapter';
import { FileLinkContext } from './file-link-context';
import { VisualizationOwnerContext } from './inline-visualization';
import type { MarkdownNotify } from './markdown-body';
import { AgentConversationView, type AgentReadingState } from './workbench-view';
import './agent-conversation-pane.css';

export type { AgentReadingState } from './workbench-view';

export function AgentConversationPane({ rootSessionId, agentSessionId, agentName, adapter, readingState, onNotify, workLocation }: {
  rootSessionId: string; agentSessionId: string; agentName: string;
  adapter: Pick<RuntimeAdapter, 'openAgentReader' | 'createAgentContentReader'>;
  readingState: AgentReadingState; onNotify: MarkdownNotify;
  workLocation?: { workId: string; requestId: number };
}) {
  const fileLinks = useContext(FileLinkContext);
  const contentReader = useMemo(() => adapter.createAgentContentReader(rootSessionId, agentSessionId), [adapter, rootSessionId, agentSessionId]);
  const links = useMemo(() => fileLinks ? { ...fileLinks, imageOwnerKey: agentSessionId,
    readImageReference: contentReader.readImageReference } : undefined, [fileLinks, agentSessionId, contentReader]);
  const checkOwner = useCallback(async (signal: AbortSignal) => {
    try { await agentCellApi.agent(rootSessionId, agentSessionId, signal); return true; }
    catch (error) { if (error instanceof AgentCellError && (error.status === 404 || error.status === 410)) return false; throw error; }
  }, [rootSessionId, agentSessionId]);
  const [reader, setReader] = useState<AgentConversationReader>();
  const [readerEpoch, setReaderEpoch] = useState(0);
  const [projection, setProjection] = useState<RuntimeProjection>();
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState<string>();
  const [observeVersion, setObserveVersion] = useState(0);
  const [queue, setQueue] = useState<QueuedPrompt[]>([]);
  const queueRef = useRef<QueuedPrompt[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [entryLocation, setEntryLocation] = useState<{entryId: string; requestId: number}>();
  const [terminalWork, setTerminalWork] = useState<{workId: string; status: string; reason?: string | null}>();
  const [queueLocation, setQueueLocation] = useState<{workId: string; requestId: number}>();
  const pane = useRef<HTMLElement>(null);
  const readerRef = useRef<AgentConversationReader | undefined>(undefined);
  const invalidateReader = useRef<(owner: AgentConversationReader) => void>(() => {});
  const pageCount = useRef(1);
  const queueRevision = useRef(0);
  const queueRequests = useRef(new Set<AbortController>());

  const refreshQueue = useCallback(async (owner: AgentConversationReader, count: number, signal: AbortSignal) => {
    const revision = ++queueRevision.current;
    let next: string | undefined;
    const items = new Map<string, QueuedPrompt>();
    for (let index = 0; index < count; index++) {
      const page = await owner.readQueuePage(next, signal);
      for (const item of page.items) items.set(item.queueItemId, item);
      next = page.nextCursor ?? undefined;
      if (!next) break;
    }
    for (const old of queueRef.current) {
      if (items.has(old.queueItemId)) continue;
      const current = await owner.readQueueItem(old.queueItemId, signal);
      if (current) items.set(current.queueItemId, current);
    }
    if (!signal.aborted && readerRef.current === owner && revision === queueRevision.current) {
      const merged = [...items.values()].sort((a, b) => compareSequence(a.sequence, b.sequence));
      queueRef.current = merged;
      setQueue(merged);
      setCursor(next ?? null);
    }
  }, []);

  useEffect(() => {
    let disposed = false;
    let currentHost: string | null | undefined;
    let opening = false;
    let closing: Promise<void> | undefined;
    const abort = new AbortController();
    const probe = async () => {
      if (opening || disposed) return;
      opening = true;
      try {
        if (closing) await closing;
        if (disposed || abort.signal.aborted) return;
        const { agent } = await agentCellApi.agent(rootSessionId, agentSessionId, abort.signal);
        const host = agent.host_session_id ?? null;
        if (disposed) return;
        setUnavailable(false);
        if (readerRef.current && currentHost === host) {
          if (readerRef.current.view === 'history') {
            await readerRef.current.snapshot();
            if (!disposed && readerRef.current) {
              setProjection(readerRef.current.current());
              await refreshQueue(readerRef.current, pageCount.current, abort.signal);
              setError(undefined);
            }
          }
          return;
        }
        const previous = readerRef.current;
        if (previous) {
          readerRef.current = undefined;
          queueRevision.current++;
          for (const request of queueRequests.current) request.abort();
          queueRequests.current.clear();
          setReader(undefined); setProjection(undefined); setQueue([]); setCursor(null);
          queueRef.current = [];
          closing = previous.close().catch(() => {});
          await closing;
          closing = undefined;
          if (disposed || abort.signal.aborted) return;
        }
        currentHost = host;
        const next = await adapter.openAgentReader(rootSessionId, agentSessionId, abort.signal);
        if (disposed) { void next.close(); return; }
        readerRef.current = next; currentHost = host;
        queueRef.current = [];
        setReader(next); setReaderEpoch(value => value + 1); setProjection(next.current()); setQueue([]); setCursor(null); setError(undefined); pageCount.current = 1;
      } catch (cause) {
        if (disposed || abort.signal.aborted) return;
        if (cause instanceof AgentCellError && (cause.status === 404 || cause.status === 410)) {
          setUnavailable(true); setError(undefined);
          const previous = readerRef.current; readerRef.current = undefined;
          queueRef.current = [];
          setReader(undefined); setProjection(undefined); setQueue([]); setCursor(null);
          if (previous) void previous.close();
        } else setError(cause instanceof Error ? cause.message : '暂时无法读取子代理。');
      } finally { opening = false; }
    };
    invalidateReader.current = owner => {
      if (disposed || readerRef.current !== owner) return;
      readerRef.current = undefined;
      queueRevision.current++;
      for (const request of queueRequests.current) request.abort();
      queueRequests.current.clear();
      queueRef.current = [];
      setReader(undefined); setProjection(undefined); setQueue([]); setCursor(null);
      currentHost = undefined;
      closing = owner.close().catch(() => {});
      void closing.then(() => { closing = undefined; if (!disposed) void probe(); });
    };
    void probe();
    const timer = window.setInterval(() => void probe(), 3000);
    return () => {
      disposed = true; abort.abort(); window.clearInterval(timer); queueRevision.current++;
      invalidateReader.current = () => {};
      for (const request of queueRequests.current) request.abort();
      queueRequests.current.clear();
      const previous = readerRef.current; readerRef.current = undefined;
      if (previous) void previous.close();
    };
  }, [rootSessionId, agentSessionId, adapter, readingState, refreshQueue]);

  useEffect(() => {
    if (unavailable) { readingState.choices.clear(); delete readingState.anchor; }
  }, [unavailable, readingState]);

  useEffect(() => {
    if (!reader) return;
    const abort = new AbortController();
    let retry: number | undefined;
    const publish = () => {
      if (!abort.signal.aborted && readerRef.current === reader) setProjection(reader.current());
    };
    const unsubscribe = reader.subscribe(publish);
    const refreshVisibleQueue = async () => {
      try { await refreshQueue(reader, pageCount.current, abort.signal); }
      catch (cause) {
        if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : '排队任务暂时无法读取。');
      }
    };
    if (reader.view === 'history') void refreshVisibleQueue();
    if (reader.view === 'live') void (async () => {
      await refreshVisibleQueue();
      while (!abort.signal.aborted && readerRef.current === reader) {
        try {
          await reader.observe(abort.signal);
          publish();
          if (!abort.signal.aborted) setError(undefined);
        } catch (cause) {
          if (!abort.signal.aborted) {
            setError(cause instanceof Error ? cause.message : '观察暂时不可用。');
            if (agentReaderRequiresReopen(cause)) invalidateReader.current(reader);
            else retry = window.setTimeout(() => setObserveVersion(value => value + 1), 3000);
          }
          break;
        }
        if (!abort.signal.aborted && readerRef.current === reader) await refreshVisibleQueue();
      }
    })();
    return () => { abort.abort(); if (retry !== undefined) window.clearTimeout(retry); unsubscribe(); };
  }, [reader, refreshQueue, observeVersion]);

  useEffect(() => {
    if (!reader || !workLocation) return;
    const abort = new AbortController();
    setEntryLocation(undefined); setTerminalWork(undefined); setQueueLocation(undefined);
    void (async () => {
      try {
        let {work} = await agentCellApi.work(rootSessionId, workLocation.workId, abort.signal);
        if (abort.signal.aborted || readerRef.current !== reader) return;
        if (work.agent_id !== agentSessionId) throw new Error('任务不属于此子代理。');
        if (!work.consumed_entry_id) {
          const item = await reader.readQueueItem(workLocation.workId, abort.signal);
          if (abort.signal.aborted || readerRef.current !== reader) return;
          if (item) {
            const merged = [...queueRef.current.filter(value => value.queueItemId !== item.queueItemId), item].sort((a, b) => compareSequence(a.sequence, b.sequence));
            queueRef.current = merged; setQueue(merged);
            setQueueLocation(workLocation); return;
          }
          ({work} = await agentCellApi.work(rootSessionId, workLocation.workId, abort.signal));
        }
        if (abort.signal.aborted || readerRef.current !== reader) return;
        if (work.agent_id !== agentSessionId) throw new Error('任务不属于此子代理。');
        if (work.consumed_entry_id) setEntryLocation({entryId: work.consumed_entry_id, requestId: workLocation.requestId});
        else if (work.status === 'stopped' || work.status === 'completed') {
          setTerminalWork({workId: workLocation.workId, status: work.status, reason: work.terminal_reason});
          setQueueLocation(workLocation);
        } else throw new Error('任务状态正在变化，请重试。');
      } catch (cause) {
        if (!abort.signal.aborted) onNotify('无法定位任务', cause instanceof Error ? cause.message : '请重试。', 'warning');
      }
    })();
    return () => abort.abort();
  }, [rootSessionId, agentSessionId, reader, workLocation?.workId, workLocation?.requestId, onNotify]);

  useEffect(() => {
    if (!queueLocation) return;
    const frame = requestAnimationFrame(() => {
      const target = [...pane.current?.querySelectorAll<HTMLElement>('[data-queue-item-id]') ?? []]
        .find(item => item.dataset.queueItemId === queueLocation.workId);
      target?.scrollIntoView({block:'nearest'});
      target?.focus({preventScroll:true});
    });
    return () => cancelAnimationFrame(frame);
  }, [queueLocation, queue, terminalWork]);

  const more = useCallback(() => {
    if (!reader) return;
    pageCount.current++;
    const controller = new AbortController();
    queueRequests.current.add(controller);
    void refreshQueue(reader, pageCount.current, controller.signal).catch(cause => {
      if (!controller.signal.aborted) onNotify('无法读取排队任务', cause instanceof Error ? cause.message : '请重试。', 'warning');
    }).finally(() => queueRequests.current.delete(controller));
  }, [reader, refreshQueue, onNotify]);

  if (unavailable) return <div className="agent-pane-status" role="status">子代理已不可用。</div>;
  if (!reader || !projection) return <div className="agent-pane-status" role="status">{error ?? '正在读取子代理…'}</div>;
  return <FileLinkContext.Provider value={links}>
    <VisualizationOwnerContext.Provider value={checkOwner}>
    <section ref={pane} className="agent-conversation-pane" aria-label={`${agentName} 会话`}>
      <header><strong>{agentName}</strong>{error && <span role="status">暂时无法刷新：{error}</span>}</header>
      <AgentConversationView key={readerEpoch} sessionId={agentSessionId} agentName={agentName} reader={reader} contentReader={contentReader} projection={projection}
        queue={queue} queueCursor={cursor} onMoreQueue={more} readingState={readingState} onNotify={onNotify}
        entryLocation={entryLocation} terminalWork={terminalWork} />
    </section>
    </VisualizationOwnerContext.Provider>
  </FileLinkContext.Provider>;
}
