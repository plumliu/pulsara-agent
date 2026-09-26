import Image from '@tiptap/extension-image';
import { NodeViewWrapper, ReactNodeViewRenderer, useEditorState, type NodeViewProps } from '@tiptap/react';
import { createContext, useContext } from 'react';
import type { PromptDraftImageStatus } from '../lib/prompt-draft';
import { PromptImageChip } from './prompt-image-chip';

export const DraftImagePreviewContext = createContext<{
  images: PromptDraftImageStatus[];
  imageUrl: (assetId: string) => string | undefined;
  open: (index: number, trigger: HTMLButtonElement) => void;
}>({ images: [], imageUrl: () => undefined, open: () => {} });

function DraftImageView({ node, editor, getPos }: NodeViewProps) {
  const preview = useContext(DraftImagePreviewContext);
  // Derive numbering from occurrences, including copies with the same asset ID.
  // Tiptap owns transaction subscriptions; no numbering is stored in the node.
  const number = useEditorState({ editor, selector: ({ editor: current }) => {
    const position = getPos();
    let count = 0;
    current.state.doc.descendants((candidate, offset) => {
      if (position !== undefined && offset <= position && candidate.type.name === 'image') count += 1;
    });
    return count;
  } });
  const status = preview.images.find(image => image.assetId === node.attrs.assetId);
  return <NodeViewWrapper as="span" className="composer-image-node" contentEditable={false} data-drag-handle>
    <PromptImageChip number={number} state={status?.state} reason={status?.reason}
      previewSrc={preview.imageUrl(node.attrs.assetId)}
      onClick={event => preview.open(number - 1, event.currentTarget)} />
  </NodeViewWrapper>;
}

export const PromptImageNode = Image.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      assetId: {
        default: null,
        parseHTML: () => null,
        renderHTML: attributes => typeof attributes.assetId === 'string'
          ? { 'data-prompt-asset-id': attributes.assetId } : {},
      },
    };
  },
  parseHTML: () => [],
  addNodeView: () => ReactNodeViewRenderer(DraftImageView),
}).configure({ inline: true, allowBase64: false, resize: false });
