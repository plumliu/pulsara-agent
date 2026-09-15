'use client';

import type { Editor } from '@tiptap/core';
import { EditorContent } from '@tiptap/react';
import { useEffect, useRef, useState } from 'react';
import Lightbox from 'yet-another-react-lightbox';
import Zoom from 'yet-another-react-lightbox/plugins/zoom';
import { PromptDraftStore } from '../lib/prompt-draft';
import type { MarkdownNotify } from './markdown-body';

interface PromptComposerProps {
  store: PromptDraftStore;
  sessionId: string;
  disabled: boolean;
  placeholder: string;
  onSubmit: () => void;
  onNotify: MarkdownNotify;
}

export function PromptComposer({
  store,
  sessionId,
  disabled,
  placeholder,
  onSubmit,
  onNotify,
}: PromptComposerProps) {
  const editor: Editor = store.getEditor(sessionId);
  const [preview, setPreview] = useState<{ src: string; assetId: string }>();
  const composing = useRef(false);
  const disabledRef = useRef(disabled);
  const notifyRef = useRef(onNotify);
  const submitRef = useRef(onSubmit);
  const imageStatuses = store.imageStatuses(sessionId);

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
          if (event.key !== 'Enter') return false;
          if (composing.current || event.isComposing || event.keyCode === 229) return false;
          event.preventDefault();
          if (event.shiftKey) {
            editor.commands.setHardBreak();
          } else if (!disabledRef.current) {
            submitRef.current();
          }
          return true;
        },
        handlePaste: (_view, event) => {
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
          if (moved) return false;
          const transfer = event.dataTransfer;
          if (!transfer) return false;
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
        handleClickOn: (_view, _position, node) => {
          if (node.type.name !== 'image' || typeof node.attrs.assetId !== 'string') {
            return false;
          }
          const src = store.imageObjectUrl(sessionId, node.attrs.assetId);
          if (!src) return false;
          setPreview({ src, assetId: node.attrs.assetId });
          return true;
        },
      },
    });
  }, [editor, sessionId, store]);

  useEffect(() => {
    if (editor.isEditable !== !disabled) editor.setEditable(!disabled);
  }, [disabled, editor]);

  useEffect(() => {
    for (const status of imageStatuses) {
      const image = [...editor.view.dom.querySelectorAll<HTMLImageElement>(
        'img[data-prompt-asset-id]',
      )].find((candidate) => candidate.dataset.promptAssetId === status.assetId);
      if (!image) continue;
      image.dataset.readState = status.state;
      image.title = status.reason ?? '';
    }
  }, [editor, imageStatuses]);

  return (
    <div className="prompt-composer">
      <EditorContent editor={editor} />
      {editor.isEmpty && (
        <span className="prompt-composer__placeholder" aria-hidden="true">
          {placeholder}
        </span>
      )}
      {imageStatuses.some((status) => status.state !== 'ready') && (
        <div className="prompt-composer__image-status" role="status">
          {imageStatuses.map((status, index) => status.state === 'ready' ? null : (
            <span key={status.assetId}>
              图片 {index + 1}：{status.state === 'loading' ? '正在读取…' : status.reason}
            </span>
          ))}
        </div>
      )}
      <Lightbox
        open={Boolean(preview)}
        close={() => setPreview(undefined)}
        slides={preview ? [{ src: preview.src, alt: '草稿图片' }] : []}
        plugins={[Zoom]}
        carousel={{ finite: true }}
        controller={{ closeOnPullDown: true, closeOnBackdropClick: true }}
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
      '图片未加入草稿',
      error instanceof Error ? error.message : '请选择 PNG、JPEG 或 WebP 图片。',
    );
  }
}
