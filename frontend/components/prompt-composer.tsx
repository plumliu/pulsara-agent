'use client';

import type { Editor } from '@tiptap/core';
import { EditorContent } from '@tiptap/react';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import Lightbox from 'yet-another-react-lightbox';
import Zoom from 'yet-another-react-lightbox/plugins/zoom';
import { PromptDraftStore } from '../lib/prompt-draft';
import { rejectDirectoryDrop } from '../lib/file-drop';
import type { MarkdownNotify } from './markdown-body';
import { DraftImagePreviewContext } from './prompt-image-node';
import type { CompleteWorkspacePaths } from '../lib/file-reference';
import type { SkillCapability } from '../lib/pulsara-types';
import { usePromptSuggestions } from './prompt-suggestions';

interface PromptComposerProps {
  store: PromptDraftStore;
  sessionId: string;
  disabled: boolean;
  placeholder: string;
  onSubmit: () => void;
  onNotify: MarkdownNotify;
  skills?: readonly SkillCapability[];
  onCompletePaths?: CompleteWorkspacePaths;
}

export function PromptComposer({
  store,
  sessionId,
  disabled,
  placeholder,
  onSubmit,
  onNotify,
  skills = [],
  onCompletePaths,
}: PromptComposerProps) {
  const editor: Editor = store.getEditor(sessionId);
  useSyncExternalStore(store.subscribe, store.getVersion, store.getVersion);
  const [preview, setPreview] = useState<number>();
  const previewTrigger = useRef<HTMLButtonElement | null>(null);
  const composing = useRef(false);
  const disabledRef = useRef(disabled);
  const notifyRef = useRef(onNotify);
  const submitRef = useRef(onSubmit);
  const imageStatuses = store.imageStatuses(sessionId);
  const suggestionKeyDown = usePromptSuggestions(editor, disabled, { skills, completePaths: onCompletePaths });

  useEffect(() => {
    disabledRef.current = disabled;
    notifyRef.current = onNotify;
    submitRef.current = onSubmit;
  }, [disabled, onNotify, onSubmit]);

  useEffect(() => {
    editor.setOptions({
      editorProps: {
        attributes: {
          class: 'composer-prosemirror',
          role: 'textbox',
          'aria-label': '发送给 Pulsara',
          'aria-multiline': 'true',
        },
        handleDOMEvents: {
          compositionstart: () => {
            composing.current = true;
            return false;
          },
          compositionend: () => {
            composing.current = false;
            return false;
          },
          blur: () => {
            composing.current = false;
            return false;
          },
        },
        handleKeyDown: (_view, event) => {
          if (composing.current || event.isComposing || event.keyCode === 229) return false;
          if (suggestionKeyDown.current(_view, event)) { event.preventDefault(); return true; }
          if (event.key !== 'Enter') return false;
          event.preventDefault();
          if (event.shiftKey) {
            editor.commands.setHardBreak();
          } else if (!disabledRef.current) {
            submitRef.current();
          }
          return true;
        },
        handlePaste: (_view, event) => {
          if (disabledRef.current) { event.preventDefault(); return true; }
          const transfer = event.clipboardData;
          if (!transfer) return false;
          const files = clipboardFiles(transfer);
          if (files.length > 0) {
            event.preventDefault();
            insertFiles(store, sessionId, files, notifyRef.current);
            return true;
          }
          if (store.insertInternalHtml(sessionId, transfer.getData('text/html'))) {
            event.preventDefault();
            return true;
          }
          const text = transfer.getData('text/plain');
          if (text || transfer.types.includes('text/html')) {
            event.preventDefault();
            if (text) store.insertText(sessionId, text);
            return true;
          }
          return false;
        },
        handleDrop: (view, event, _slice, moved) => {
          if (disabledRef.current) { event.preventDefault(); return true; }
          if (moved) return false;
          const transfer = event.dataTransfer;
          if (!transfer) return false;
          if (rejectDirectoryDrop(transfer, notifyRef.current)) {
            event.preventDefault();
            return true;
          }
          const files = [...transfer.files];
          if (files.length > 0) {
            event.preventDefault();
            const position = view.posAtCoords({
              left: event.clientX,
              top: event.clientY,
            })?.pos;
            insertFiles(store, sessionId, files, notifyRef.current, position);
            return true;
          }
          const position = view.posAtCoords({
            left: event.clientX,
            top: event.clientY,
          })?.pos;
          if (store.insertInternalHtml(
            sessionId,
            transfer.getData('text/html'),
            position,
          )) {
            event.preventDefault();
            return true;
          }
          if (transfer.types.includes('text/html')
            || transfer.types.includes('text/uri-list')) {
            event.preventDefault();
            const text = transfer.getData('text/plain')
              || transfer.getData('text/uri-list');
            if (text) {
              if (position !== undefined) editor.commands.setTextSelection(position);
              store.insertText(sessionId, text);
            }
            return true;
          }
          return false;
        },
      },
    });
  }, [editor, sessionId, store, suggestionKeyDown]);

  useEffect(() => {
    if (editor.isEditable !== !disabled) editor.setEditable(!disabled);
  }, [disabled, editor]);

  return (
    <div className="prompt-composer">
      <DraftImagePreviewContext.Provider value={{ images: imageStatuses,
        imageUrl: assetId => store.imageObjectUrl(sessionId, assetId), open: (index, trigger) => {
        previewTrigger.current = trigger;
        setPreview(index);
      } }}>
        <EditorContent editor={editor} />
      </DraftImagePreviewContext.Provider>
      {editor.isEmpty && (
        <span className="prompt-composer__placeholder" aria-hidden="true">
          {placeholder}
        </span>
      )}
      <Lightbox
        open={preview !== undefined}
        close={() => setPreview(undefined)}
        index={preview ?? 0}
        slides={imageStatuses.map((image, index) => ({
          src: store.imageObjectUrl(sessionId, image.assetId) ?? '', alt: `Figure ${index + 1}`,
        }))}
        plugins={[Zoom]}
        carousel={{ finite: true }}
        controller={{ closeOnPullDown: true, closeOnBackdropClick: true }}
        on={{ view: ({ index }) => setPreview(index), exited: () => {
          if (previewTrigger.current?.isConnected) previewTrigger.current.focus();
          else if (!editor.isDestroyed) editor.commands.focus();
        } }}
        render={{ slideFooter: ({ slide }) => <div className="prompt-lightbox-caption">{slide.alt}</div> }}
      />
    </div>
  );
}

function clipboardFiles(data: DataTransfer): File[] {
  return [...data.items]
    .filter((item) => item.kind === 'file')
    .map((item) => item.getAsFile())
    .filter((item): item is File => item !== null);
}

function insertFiles(
  store: PromptDraftStore,
  sessionId: string,
  files: readonly File[],
  onNotify: PromptComposerProps['onNotify'],
  position?: number,
): void {
  try {
    store.insertFiles(sessionId, files, position);
  } catch (error) {
    onNotify(
      '文件未加入草稿',
      error instanceof Error ? error.message : '请重新选择文件。',
    );
  }
}
