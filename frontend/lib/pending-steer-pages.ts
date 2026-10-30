import { useCallback, useEffect, useRef, useState } from 'react';
import { compareSequence, type AgentConversationReader, type QueuedPrompt, type RuntimeProjection } from './runtime-adapter';

type Reader = Pick<AgentConversationReader, 'readQueuePage' | 'readQueueItem' | 'readQueueStatus'>;
/** Disposable reading pages for the exact active lane; canonical queue rows remain authority. */
export function usePendingSteerPages(reader: Reader | undefined, projection: RuntimeProjection | undefined) {
  const target = projection?.pendingSteerPage?.targetTurnId;
  const [depth, setDepth] = useState(1);
  const [state, setState] = useState<{ reader: Reader; target: string; items: QueuedPrompt[]; next: string | null; error?: string; loading: boolean }>();
  const previous = useRef(state); previous.current = state;
  const generation = useRef(0);
  useEffect(() => { setDepth(1); }, [reader, target]);
  useEffect(() => {
    if (!reader || !target) return;
    const owner = ++generation.current; const controller = new AbortController();
    const old = previous.current?.reader === reader && previous.current.target === target ? previous.current : undefined;
    setState({ reader, target, items: old?.items ?? [], next: old?.next ?? projection?.pendingSteerPage?.nextCursor ?? null, loading: true });
    void (async () => {
      const found = new Map<string, QueuedPrompt>(); let cursor: string | undefined; let next: string | null = null;
      for (let index = 0; index < depth; index++) {
        const page = await reader.readQueuePage(cursor, controller.signal, {deliveryMode:'steer',targetTurnId:target});
        for (const item of page.items) if (item.deliveryMode === 'steer' && item.targetTurnId === target) found.set(item.queueItemId, item);
        next = page.nextCursor; if (!next) break; cursor = next;
      }
      for (const item of old?.items ?? []) if (!found.has(item.queueItemId)) {
        const status = await reader.readQueueStatus(item.queueItemId, controller.signal);
        if (status?.status !== 'PENDING' || status.deliveryMode !== 'steer' || status.targetTurnId !== target) continue;
        const exact = await reader.readQueueItem(item.queueItemId, controller.signal);
        if (exact?.deliveryMode === 'steer' && exact.targetTurnId === target) found.set(exact.queueItemId, exact);
      }
      if (!controller.signal.aborted && owner === generation.current) setState({reader,target,items:[...found.values()].sort((a,b)=>compareSequence(a.sequence,b.sequence)),next,loading:false});
    })().catch(error => { if (!controller.signal.aborted && owner === generation.current) setState({reader,target,items:old?.items ?? [],next:old?.next ?? projection?.pendingSteerPage?.nextCursor ?? null,loading:false,error:error instanceof Error?error.message:'暂时无法读取补充输入。'}); });
    return () => controller.abort();
  }, [reader, target, depth, projection?.eventSequence, projection?.pendingSteerPage?.nextCursor]);
  const current = state && state.reader === reader && state.target === target ? state : undefined;
  const more = useCallback(() => { if (!current?.loading) setDepth(value=>value+1); }, [current?.loading]);
  return { items:current?.items ?? [], nextCursor:current ? current.next : projection?.pendingSteerPage?.nextCursor ?? null, loading:current?.loading ?? false, error:current?.error, more };
}
