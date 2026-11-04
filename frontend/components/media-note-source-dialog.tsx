import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import type { MediaNote } from '../lib/file-preview';
import type { AgentConversationReader, RuntimeAdapter, RuntimeProjection } from '../lib/runtime-adapter';
import type { MarkdownNotify } from './markdown-body';
import { AgentConversationView } from './workbench-view';
import { FileLinkContext, FileMediaContext } from './file-link-context';
import './agent-conversation-pane.css';

/** A disposable view of an existing canonical call; it never attaches a Host or
 * replaces the active session and its mounted media players. */
export function MediaNoteSourceDialog({note, adapter, onClose, onNotify}: {
  note: MediaNote;
  adapter: Pick<RuntimeAdapter, 'openHistoryReader'>;
  onClose: () => void;
  onNotify: MarkdownNotify;
}) {
  const [state, setState] = useState<{reader: AgentConversationReader; projection: RuntimeProjection}>();
  const [error, setError] = useState('');
  const close = useRef<HTMLButtonElement>(null);
  const [readingState] = useState(() => ({choices:new Map<string, boolean>()}));
  useEffect(() => {
    const opener = document.activeElement;
    close.current?.focus();
    return () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus({preventScroll:true}); };
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    let owned: AgentConversationReader | undefined;
    let unsubscribe: (() => void) | undefined;
    setState(undefined); setError('');
    void adapter.openHistoryReader(note.sessionId, abort.signal).then(async reader => {
      if (abort.signal.aborted) { await reader.close(); return; }
      owned = reader;
      unsubscribe = reader.subscribe(() => { if (!abort.signal.aborted) setState({reader,projection:reader.current()}); });
      setState({reader,projection:reader.current()});
    }).catch(cause => { if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : '来源会话暂时无法读取。'); });
    return () => { abort.abort(); unsubscribe?.(); void owned?.close(); };
  }, [adapter, note.sessionId, note.resultId]);
  const reader = state?.reader;
  const contentReader = useMemo(() => reader ? {
    readToolOutput:reader.readToolOutput.bind(reader), readPromptImage:reader.readPromptImage.bind(reader),
    readImageReference:reader.readImageReference.bind(reader), readVisualizationContent:reader.readVisualizationContent.bind(reader),
    readVisualizationThumbnail:reader.readVisualizationThumbnail.bind(reader),
  } : undefined, [reader]);
  return createPortal(<div className="session-search-overlay" onClick={event => {if(event.target === event.currentTarget) onClose();}}>
    <section className="session-search-dialog media-note-source-dialog" role="dialog" aria-modal="true" aria-label="批注来源调用" onKeyDown={event => {
      event.stopPropagation();
      if (event.key === 'Escape') {event.preventDefault();onClose();}
      if (event.key === 'Tab') {
        const nodes = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), [href], input, select, [tabindex="0"]')].filter(node => node.getClientRects().length);
        const first = nodes[0], last = nodes.at(-1);
        if (event.shiftKey && document.activeElement === first) {event.preventDefault();last?.focus();}
        else if (!event.shiftKey && document.activeElement === last) {event.preventDefault();first?.focus();}
      }
    }}>
      <header><strong>批注来源调用</strong><button ref={close} aria-label="关闭批注来源" onClick={onClose}><X size={18}/></button></header>
      {error ? <div role="alert">{error}</div> : state && contentReader ? <FileLinkContext.Provider value={undefined}><FileMediaContext.Provider value={undefined}>
        <AgentConversationView sessionId={note.sessionId} agentName="Pulsara" reader={state.reader} contentReader={contentReader} projection={state.projection}
          queue={[]} queueCursor={null} onMoreQueue={() => {}} readingState={readingState} onNotify={onNotify}
          entryLocation={{entryId:note.entryId, requestId:1}}/>
      </FileMediaContext.Provider></FileLinkContext.Provider> : <div role="status">正在读取来源调用…</div>}
    </section>
  </div>, document.body);
}
