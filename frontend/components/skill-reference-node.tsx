import { Node, mergeAttributes } from '@tiptap/core';
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from '@tiptap/react';
import { skillReference } from '../lib/skill-reference';
import { SkillReferenceChip } from './skill-reference-chip';

function SkillReferenceView({ node }: NodeViewProps) {
  const reference = skillReference(node.attrs.raw);
  return <NodeViewWrapper as="span" className="composer-skill-node" contentEditable={false}>
    {reference ? <SkillReferenceChip reference={reference} /> : node.attrs.raw}
  </NodeViewWrapper>;
}

export const SkillReferenceNode = Node.create({
  name: 'skillReference', group: 'inline', inline: true, atom: true, selectable: true,
  addAttributes: () => ({ raw: { default: '', rendered: false } }),
  parseHTML: () => [],
  renderHTML: ({ node, HTMLAttributes }) => ['span', mergeAttributes(HTMLAttributes,
    { 'data-skill-reference': node.attrs.raw }), node.attrs.raw],
  renderText: ({ node }) => node.attrs.raw,
  addNodeView: () => ReactNodeViewRenderer(SkillReferenceView),
});
