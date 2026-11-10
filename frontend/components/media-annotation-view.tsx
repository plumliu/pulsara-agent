import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { FilePreview, FilePreviewApi, MediaNote } from '../lib/file-preview';
import type { VideoAnnotation } from './file-video-content';

export function MediaAnnotationView({ file, api, revision, children }: {
  file: FilePreview;
  api: FilePreviewApi;
  revision: string | number;
  children: (notes: readonly VideoAnnotation[]) => ReactNode;
}) {
  const [notes, setNotes] = useState<readonly MediaNote[]>([]);
  const [status, setStatus] = useState('');
  const [failed, setFailed] = useState(false);
  const [next, setNext] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const more = useRef(() => {});
  const retry = useRef(() => {});
  useEffect(() => {
    const controller = new AbortController();
    const collected = new Map<string, MediaNote>();
    let fetching = false;
    let continuation: string | null = null;
    let requested: string | null = null;
    setNotes([]); setNext(null);
    const read = async (after: string | null) => {
      if (fetching || controller.signal.aborted) return;
      fetching = true; requested = after;
      setBusy(true); setStatus('正在读取历史批注…'); setFailed(false);
      try {
        const page = await api.annotations(file.read_token, after, controller.signal);
        if (controller.signal.aborted) return;
        for (const note of page.items) collected.set(note.resultId, note);
        continuation = page.next; setNext(page.next);
        setNotes([...collected.values()].sort((a, b) => a.from - b.from || a.to - b.to || a.resultId.localeCompare(b.resultId)));
        setStatus('');
      } catch (error) {
        if (!controller.signal.aborted) {
          setStatus(error instanceof Error ? error.message : '历史批注读取失败'); setFailed(true);
        }
      } finally {
        fetching = false;
        if (!controller.signal.aborted) setBusy(false);
      }
    };
    more.current = () => { if (continuation !== null) void read(continuation); };
    retry.current = () => { void read(requested); };
    void read(null);
    return () => { controller.abort(); };
  }, [api, file.read_token, revision]);
  return <>{children(notes)}
    {status && <div role="status">{status}{failed && <button onClick={() => retry.current()}>重新读取批注</button>}</div>}
    {next !== null && !failed && <button className="media-notes-more" disabled={busy} onClick={() => more.current()}>加载更多批注</button>}
  </>;
}
