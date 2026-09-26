import { Node, mergeAttributes } from '@tiptap/core';
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from '@tiptap/react';
import { File, Folder, LoaderCircle } from 'lucide-react';
import { fileReference } from '../lib/file-reference';
import { FileReferenceChip } from './file-reference-chip';

function ReferenceView({ node, extension }: NodeViewProps) {
  const reference = fileReference(node.attrs.raw as string);
  return <NodeViewWrapper as="span" className="composer-file-node" contentEditable={false}>
    {reference ? <FileReferenceChip reference={reference} /> : <span className="file-import-card">
      {node.attrs.state === 'uploading' ? <LoaderCircle size={14} className="spin" />
        : node.attrs.kind === 'directory' ? <Folder size={14} /> : <File size={14} />}
      <span>{node.attrs.name}</span>
      <small>{node.attrs.state === 'failed' ? node.attrs.error : '正在导入…'}</small>
      {node.attrs.state === 'failed' && <button type="button" onClick={() => extension.options.retry(node.attrs.id)}>重试</button>}
      <button type="button" aria-label={`移除 ${node.attrs.name}`} onClick={() => extension.options.remove(node.attrs.id)}>×</button>
    </span>}
  </NodeViewWrapper>;
}

export const FileReferenceNode = Node.create({
  name: 'fileReference', group: 'inline', inline: true, atom: true, selectable: true,
  addOptions: (): { retry: (id: string) => void; remove: (id: string) => void } => ({ retry: () => {}, remove: () => {} }),
  addAttributes: () => ({
    id: { default: '' }, raw: { default: '' }, name: { default: '' },
    kind: { default: 'file' }, state: { default: 'ready' }, error: { default: '' },
  }),
  parseHTML: () => [],
  renderHTML: ({ node, HTMLAttributes }) => ['span', mergeAttributes(HTMLAttributes,
    { 'data-file-reference': node.attrs.raw }), node.attrs.raw || `[正在导入 ${node.attrs.name}]`],
  renderText: ({ node }) => node.attrs.raw || `[正在导入 ${node.attrs.name}]`,
  addNodeView: () => ReactNodeViewRenderer(ReferenceView),
});
