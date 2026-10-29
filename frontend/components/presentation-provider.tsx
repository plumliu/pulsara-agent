import { createContext, useCallback, useContext, useLayoutEffect, useRef, type ReactNode } from 'react';
import { compareSequence, type RuntimeConnection } from '../lib/runtime-adapter';
import { browserTitle, type BrowserTabRecord } from '../lib/browser-tabs';
import { presentationLabel, type PresentationTarget } from '../lib/presentation-targets';
import { FilePresentationContext } from './file-browser';
import { inspectorTab, useDesktopWorkspace, type PreparedWorkspaceTab, type WorkspaceTab } from './desktop-workspace';

export const PresentationContext = createContext<((targets: PresentationTarget[]) => void) | undefined>(undefined);

/** Disposable observation effects. No pending request survives its visible connection. */
export function PresentationProvider({ connection, visible, isOpen, children }: {
  connection?: RuntimeConnection; visible: boolean; isOpen: boolean; children: ReactNode;
}) {
  const workspace = useDesktopWorkspace();
  const prepareFile = useContext(FilePresentationContext);
  const current = useRef({ workspace, prepareFile });
  useLayoutEffect(() => { current.current = { workspace, prepareFile }; });
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
        if (target.type === 'file') {
          const prepare = current.current.prepareFile;
          if (!prepare) return;
          prepared.push(await prepare(target.path, valid));
        } else if (target.type === 'browser' || (target.type === 'panel' && target.name === 'browser')) {
          const browser = owner.browserTabs.prepare(owner.sessionId, target.type === 'browser' ? target.url : undefined, browsers);
          prepared.push({ release: () => {}, start: browser.start, adopt: () => {
            const record = browser.adopt();
            return { id: record.id, sessionId: record.sessionId, kind: 'browser', title: browserTitle(record), browser: record, onClose: () => owner.browserTabs.close(record.id) };
          } });
        } else {
          const tab = target.type === 'panel'
            ? inspectorTab(owner.sessionId, target.name, presentationLabel(target))
            : inspectorTab(owner.sessionId, target.type === 'terminal' ? 'process' : 'mcp', presentationLabel(target), target.type === 'terminal' ? target.process_id : target.runtime_server_id, target.host_session_id);
          prepared.push({ release: () => {}, adopt: () => tab });
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
      const valid = () => alive && !operation.signal.aborted && !request.signal.aborted && current.current.workspace?.visible === true && current.current.workspace?.interactionVersion() === interaction;
      void (async () => {
        const result = await request.read();
        if (!valid() || !result?.targets.length) return;
        // Host-bound targets retain the original Host. Never fill identity from active UI.
        if (result.targets.some(target => 'host_session_id' in target && target.host_session_id !== connection.current().hostSessionId)) return;
        if (compareSequence(request.sequence, latest.current) > 0) latest.current = request.sequence;
        await present(result.targets, valid, () => request.sequence === latest.current);
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
