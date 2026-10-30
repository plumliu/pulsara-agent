import { createContext, useCallback, useContext, useLayoutEffect, useRef, type ReactNode } from 'react';
import { compareSequence, type RuntimeConnection } from '../lib/runtime-adapter';
import { prepareSessionTarget, type SessionNavigation } from '../lib/agent-cell-api';
import { browserTitle, type BrowserTabRecord } from '../lib/browser-tabs';
import { presentationLabel, type PresentationTarget } from '../lib/presentation-targets';
import type { MarkdownNotify } from './markdown-body';
import { FilePresentationContext } from './file-browser';
import { inspectorTab, useDesktopWorkspace, type PreparedWorkspaceTab, type WorkspaceTab } from './desktop-workspace';

export const PresentationContext = createContext<((targets: PresentationTarget[]) => void) | undefined>(undefined);

/** Disposable observation effects. No pending request survives its visible connection. */
export function PresentationProvider({ connection, visible, isOpen, onNotify, children }: {
  connection?: RuntimeConnection; visible: boolean; isOpen: boolean; onNotify: MarkdownNotify; children: ReactNode;
}) {
  const workspace = useDesktopWorkspace();
  const prepareFile = useContext(FilePresentationContext);
  const current = useRef({ workspace, prepareFile, onNotify });
  useLayoutEffect(() => { current.current = { workspace, prepareFile, onNotify }; });
  const pending = useRef(new Set<AbortController>());
  const latest = useRef('0');
  const cancel = useCallback(() => { for (const operation of pending.current) operation.abort(); pending.current.clear(); }, []);
  useLayoutEffect(() => { if (!isOpen) { current.current.workspace?.interact(); cancel(); } }, [isOpen, cancel]);
  useLayoutEffect(() => () => cancel(), [connection, visible, workspace?.sessionId, cancel]);

  const present = useCallback(async (targets: PresentationTarget[], valid: () => boolean, foreground: () => boolean) => {
    const owner = current.current.workspace;
    if (!owner || !targets.length || !valid()) return;
    const prepared: PreparedWorkspaceTab[] = [];
    const browsers = new Map<string, BrowserTabRecord>();
    try {
      // Sequential file preparation preserves the existing per-operation read bound.
      for (const target of targets) {
        if (!valid()) return;
        try {
          if ('root_session_id' in target && target.root_session_id !== owner.sessionId) continue;
          if (target.type === 'agent' || target.type === 'cell' || (target.type === 'panel' && (target.name === 'subagents' || target.name === 'cells'))) {
            const intent: SessionNavigation = target.type === 'agent'
              ? { kind: 'agent', rootSessionId: target.root_session_id, agentSessionId: target.agent_id }
              : target.type === 'cell'
                ? { kind: 'cell', rootSessionId: target.root_session_id, hostSessionId: target.host_session_id, cellId: target.cell_id }
                : target.name === 'cells'
                  ? { kind: 'cells', rootSessionId: target.root_session_id, hostSessionId: target.host_session_id }
                  : { kind: 'subagents', rootSessionId: target.root_session_id };
            const tab = await prepareSessionTarget(intent, valid);
            if (tab) prepared.push(tab);
          } else if (target.type === 'file') {
            const prepare = current.current.prepareFile;
            if (!prepare) continue;
            prepared.push(await prepare(target.path, valid));
          } else if (target.type === 'browser' || (target.type === 'panel' && target.name === 'browser')) {
            const browser = owner.browserTabs.prepare( target.type === 'browser' ? target.url : undefined, browsers);
            prepared.push({ release: () => {}, start: browser.start, adopt: () => {
              const record = browser.adopt();
              return { id: record.id, kind: 'browser', title: browserTitle(record), browser: record, onClose: () => owner.browserTabs.close(record.id) };
            } });
          } else if (target.type === 'panel' && (target.name === 'capabilities' || target.name === 'background')) {
            const tab = inspectorTab(owner.sessionId, target.name, presentationLabel(target));
            prepared.push({ release: () => {}, adopt: () => tab });
          } else if (target.type === 'terminal' || target.type === 'mcp') {
            const tab = inspectorTab(owner.sessionId, target.type === 'terminal' ? 'process' : 'mcp', presentationLabel(target), target.type === 'terminal' ? target.process_id : target.runtime_server_id, target.host_session_id);
            prepared.push({ release: () => {}, adopt: () => tab });
          }
        } catch (error) {
          if (valid()) current.current.onNotify('无法打开目标', `${presentationLabel(target)}：${error instanceof Error ? error.message : '暂时无法读取。'}`, 'warning');
        }
      }
      if (!valid()) return;
      const batch = prepared.map(item => item.adopt()).filter((tab): tab is WorkspaceTab => !!tab);
      owner.publish(batch, foreground());
      for (const item of prepared) item.start?.();
    } finally { for (const item of prepared) item.release(); }
  }, []);

  useLayoutEffect(() => {
    latest.current = '0';
    if (!visible || !connection || connection.role !== 'controller' || workspace?.sessionId !== connection.sessionId) return;
    let alive = true;
    const unsubscribe = connection.subscribePresentation?.(request => {
      const operation = new AbortController(); pending.current.add(operation);
      const interaction = current.current.workspace?.interactionVersion();
      let boundHost: string | undefined;
      const valid = () => alive && !operation.signal.aborted && !request.signal.aborted && current.current.workspace?.sessionId === connection.sessionId && current.current.workspace?.visible === true && current.current.workspace?.interactionVersion() === interaction && (boundHost === undefined || connection.current().hostSessionId === boundHost);
      void (async () => {
        const result = await request.read();
        if (!valid() || !result?.targets.length) return;
        // Host-bound targets retain the original Host. Never fill identity from active UI.
        const targets = result.targets.filter(target => (!('host_session_id' in target) || target.host_session_id === connection.current().hostSessionId) && (!('root_session_id' in target) || target.root_session_id === connection.sessionId));
        const hostTarget = targets.find(target => 'host_session_id' in target);
        if (hostTarget && 'host_session_id' in hostTarget) boundHost = hostTarget.host_session_id;
        if (compareSequence(request.sequence, latest.current) > 0) latest.current = request.sequence;
        await present(targets, valid, () => request.sequence === latest.current);
      })().catch(() => { /* Advisory failure stays available as a manual history entry. */ })
        .finally(() => pending.current.delete(operation));
    });
    return () => { alive = false; cancel(); unsubscribe?.(); };
  }, [connection, visible, workspace?.sessionId, cancel, present]);

  const manual = useCallback((targets: PresentationTarget[]) => {
    const owner = current.current.workspace;
    if (!owner?.visible) return;
    cancel();
    owner.interact();
    const operation = new AbortController(); pending.current.add(operation);
    const interaction = owner.interactionVersion();
    const valid = () => !operation.signal.aborted && current.current.workspace?.sessionId === owner.sessionId && current.current.workspace?.visible === true && owner.interactionVersion() === interaction;
    void present(targets, valid, () => true).catch(() => {}).finally(() => pending.current.delete(operation));
  }, [cancel, present]);
  return <PresentationContext.Provider value={manual}>{children}</PresentationContext.Provider>;
}
