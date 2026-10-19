import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { RuntimeAdapter, RuntimeConnection } from '../lib/runtime-adapter';
import { desktopHost } from '../lib/desktop-host';

type Action = 'login' | 'status' | 'cancel' | 'logout';
type Status = { state: string; error: string | null };
type Request = (action: Action) => Promise<Status>;
type Watch = { timer?: ReturnType<typeof setTimeout>; returnOnSuccess: boolean };
const pending = (state: string) => state === 'connecting' || state === 'awaiting_user';
const labels: Record<string, string> = {
  idle: '未登录', needs_login: '需要重新登录', connecting: '正在准备授权',
  awaiting_user: '等待浏览器授权', authorized: '已授权', cancelled: '授权已取消', failed: '授权未完成',
  unavailable: '登录状态未更新',
};
export const mcpAuthorizationKey = (...owner: string[]) => JSON.stringify(owner);
export const McpAuthorizationContext = createContext<Record<string, Status>>({});

export function McpAuthorizationStatus({ owner }: { owner?: string }) {
  const statuses = useContext(McpAuthorizationContext);
  const result = owner ? statuses[owner] : undefined;
  return result ? <span className="mcp-authorization-status" role="status" data-state={result.state} title={result.error ?? undefined}>{labels[result.state] ?? result.state}</span> : null;
}

/** Advisory UI polling uses existing status reads; it never enables/reconnects an MCP. */
export function useMcpAuthorization(notify: (title: string, detail?: string, tone?: 'neutral' | 'success' | 'warning') => void) {
  const [statuses, setStatuses] = useState<Record<string, Status>>({});
  const watches = useRef(new Map<string, Watch>());
  useEffect(() => {
    const current = watches.current;
    return () => { for (const watch of current.values()) clearTimeout(watch.timer); current.clear(); };
  }, []);

  const invalidate = useCallback((owner: string) => {
    clearTimeout(watches.current.get(owner)?.timer);
    watches.current.delete(owner);
    setStatuses(values => {
      if (!(owner in values)) return values;
      const next = { ...values };
      delete next[owner];
      return next;
    });
  }, []);

  const run = useCallback(async (owner: string, action: Action, request: Request, options?: { returnOnSuccess?: boolean; signal?: AbortSignal }) => {
    if (options?.signal?.aborted) return;
    const previous = watches.current.get(owner);
    clearTimeout(previous?.timer);
    const watch: Watch = { returnOnSuccess: options?.returnOnSuccess === true || action === 'login' || (action === 'status' && Boolean(previous?.returnOnSuccess)) };
    watches.current.set(owner, watch);
    const current = () => !options?.signal?.aborted && watches.current.get(owner) === watch;
    const read = async (operation: Action, initial: boolean): Promise<void> => {
      if (!current()) return;
      try {
        const result = await request(operation);
        if (!current()) return;
        setStatuses(values => ({ ...values, [owner]: result }));
        if (pending(result.state)) {
          if (initial) notify('MCP 授权', labels[result.state]);
          // One status read per second while this user-started flow is pending.
          // There is no total-duration/retry cap or durable background job.
          watch.timer = setTimeout(() => void read('status', false), 1000);
          return;
        }
        watches.current.delete(owner);
        notify('MCP 授权', operation === 'logout' ? '本机授权已清除' : result.error || labels[result.state], result.state === 'authorized' ? 'success' : result.error ? 'warning' : 'neutral');
        if (result.state === 'authorized' && watch.returnOnSuccess) {
          void desktopHost()?.focus().catch(error => console.warn('Could not focus Pulsara after authorization', error));
        }
      } catch {
        if (!current()) return;
        watches.current.delete(owner);
        setStatuses(values => ({ ...values, [owner]: { state: 'unavailable', error: '请重试查看登录状态。' } }));
        notify('授权状态未更新', '请重试查看登录状态。', 'warning');
      }
    };
    await read(action, true);
  }, [notify]);
  return { statuses, run, invalidate };
}

/** Observe fresh host tool settlements, including model authorization without a UI login button. */
export function useModelMcpAuthorization(
  connection: RuntimeConnection | undefined,
  adapter: RuntimeAdapter,
  run: ReturnType<typeof useMcpAuthorization>['run'],
) {
  useEffect(() => {
    if (!connection || connection.role !== 'controller') return;
    const lifetime = new AbortController();
    const unsubscribe = connection.subscribeMcpAuthorization?.(request => {
      void (async () => {
        const result = await request.read();
        if (!result || lifetime.signal.aborted || request.signal.aborted) return;
        const target = result.authorization;
        const owner = target.owner;
        const key = owner.kind === 'plugin'
          ? mcpAuthorizationKey('plugin', target.package_install_id!, owner.server_id)
          : result.scope === 'WORKSPACE'
            ? mcpAuthorizationKey('project', connection.sessionId, owner.server_id, target.display_identity)
            : mcpAuthorizationKey('device', owner.server_id, target.display_identity);
        // The connection owns reading the fresh model result. After acceptance,
        // a USER authorization belongs to the user hook, including Plugin owners.
        // Every status read still checks the saved target's current identity.
        await run(key, 'status', () => adapter.modelMcpAuthorizationStatus(target), {
          returnOnSuccess: true,
          signal: result.scope === 'WORKSPACE'
            ? AbortSignal.any([lifetime.signal, request.signal])
            : undefined,
        });
      })().catch(() => { /* The ordinary status button remains available after a lost observation. */ });
    });
    return () => { lifetime.abort(); unsubscribe?.(); };
  }, [connection, adapter, run]);
}
