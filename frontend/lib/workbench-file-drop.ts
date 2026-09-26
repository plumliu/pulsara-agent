import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react';
import type { PromptDraftStore } from './prompt-draft';
import { rejectDirectoryDrop } from './file-drop';

interface Options {
  sessionId: string;
  store: PromptDraftStore;
  blocked: () => boolean;
  notify: (title: string, message: string) => void;
}

function hasFiles(transfer: DataTransfer): boolean {
  // During dragover the browser hides file bytes but exposes the Files type.
  return Array.from(transfer.types ?? []).includes('Files') || transfer.files.length > 0;
}

/** The workbench owns drops outside the editor; Tiptap keeps its precise drop position. */
export function useWorkbenchFileDrop({ sessionId, store, blocked, notify }: Options) {
  const [dragSession, setDragSession] = useState<string | null>(null);
  const owner = useRef<string | null>(null);
  const depth = useRef(0);
  const internal = useRef(false);
  const reset = useCallback(() => {
    owner.current = null;
    depth.current = 0;
    internal.current = false;
    setDragSession(null);
  }, []);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') reset(); };
    window.addEventListener('dragend', reset);
    window.addEventListener('drop', reset);
    window.addEventListener('blur', reset);
    window.addEventListener('keydown', escape);
    return () => {
      window.removeEventListener('dragend', reset);
      window.removeEventListener('drop', reset);
      window.removeEventListener('blur', reset);
      window.removeEventListener('keydown', escape);
    };
  }, [reset]);

  const isExternal = (event: DragEvent<HTMLElement>) => !internal.current
    && event.currentTarget.contains(event.target as Node) && hasFiles(event.dataTransfer);
  const unavailable = () => !sessionId || blocked() || (owner.current !== null && owner.current !== sessionId);
  const begin = () => {
    if (owner.current !== null) return;
    owner.current = sessionId;
    setDragSession(sessionId);
  };

  return {
    dragging: dragSession === sessionId && Boolean(sessionId) && !blocked(),
    handlers: {
      onDragStartCapture: () => { reset(); internal.current = true; },
      onDragEnterCapture: (event: DragEvent<HTMLElement>) => {
        if (!isExternal(event)) return;
        depth.current += 1;
        begin();
      },
      onDragOverCapture: (event: DragEvent<HTMLElement>) => {
        if (!isExternal(event)) return;
        begin();
        event.preventDefault();
        event.dataTransfer.dropEffect = unavailable() ? 'none' : 'copy';
        if (unavailable()) event.stopPropagation();
      },
      onDragLeaveCapture: (event: DragEvent<HTMLElement>) => {
        if (!isExternal(event)) return;
        depth.current = Math.max(0, depth.current - 1);
        if (!depth.current) reset();
      },
      onDropCapture: (event: DragEvent<HTMLElement>) => {
        if (!isExternal(event)) return;
        const rejected = unavailable();
        reset();
        if (!rejected && rejectDirectoryDrop(event.dataTransfer, notify)) {
          event.preventDefault();
          event.stopPropagation();
          return;
        }
        if (!rejected && store.getEditor(sessionId).view.dom.contains(event.target as Node)) {
          return; // The editor receives this drop exactly once, at its native position.
        }
        event.preventDefault();
        event.stopPropagation();
        if (rejected) {
          notify('文件未加入草稿', '会话已切换或输入框暂不可编辑，请重新拖入。');
          return;
        }
        const files = Array.from(event.dataTransfer.files);
        if (!files.length) return;
        try { store.insertFiles(sessionId, files, 'end'); }
        catch (error) { notify('文件未加入草稿', error instanceof Error ? error.message : '请重新拖入文件。'); }
      },
    },
  };
}
