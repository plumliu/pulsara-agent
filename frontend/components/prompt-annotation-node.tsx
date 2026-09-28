import { Node } from '@tiptap/core';
import { NodeViewWrapper, ReactNodeViewRenderer, useEditorState, type NodeViewProps } from '@tiptap/react';
import { closeHistory } from '@tiptap/pm/history';
import { MessageSquare, X } from 'lucide-react';
import { useContext, useEffect, useLayoutEffect, useRef } from 'react';
import type { PromptAnnotationPart } from '../lib/prompt-content';
import { DraftAnnotationContext } from './annotation-interactions';

function AnnotationView({ node, editor, deleteNode }: NodeViewProps) {
  const value = node.attrs.value as PromptAnnotationPart;
  const id = node.attrs.draftId as string;
  const actions = useContext(DraftAnnotationContext);
  const ref = useRef<HTMLDivElement>(null);
  const previousPosition = useRef<{ x: number; y: number } | undefined>(undefined);
  const movement = useRef<Animation | undefined>(undefined);
  useLayoutEffect(() => {
    const chip = ref.current;
    const slot = chip?.closest<HTMLElement>('.node-annotation');
    const composer = slot?.closest<HTMLElement>('.tiptap');
    if (!chip || !slot || !composer) return;
    // Measure the untransformed Tiptap slot relative to its editor, so scrolling
    // or moving the composer cannot be mistaken for a chip changing position.
    const rect = slot.getBoundingClientRect(); const origin = composer.getBoundingClientRect();
    const next = { x: rect.left - origin.left, y: rect.top - origin.top };
    const previous = previousPosition.current; previousPosition.current = next;
    if (!previous || (previous.x === next.x && previous.y === next.y)) return;
    movement.current?.cancel();
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    movement.current = chip.animate?.([
      { transform: `translate(${previous.x - next.x}px, ${previous.y - next.y}px)` },
      { transform: 'translate(0, 0)' },
    ], { duration: 200, easing: 'cubic-bezier(.2, .7, .2, 1)' });
  });
  useEffect(() => () => movement.current?.cancel(), []);
  const number = useEditorState({ editor, selector: ({ editor }) => {
    let ordinal = 0; let result = 0;
    editor.state.doc.forEach(item => { if (item.type.name === 'annotation') { ordinal++; if (item.attrs.draftId === id) result = ordinal; } });
    return result;
  } });
  return <NodeViewWrapper ref={ref} className={`annotation-chip${actions.activeId === id ? ' is-active' : ''}`} contentEditable={false}
    data-draft-annotation={id} onMouseEnter={() => actions.preview(id)} onMouseLeave={() => actions.preview()}>
    <button type="button" className="annotation-chip__open" aria-label={`编辑批注 ${number}`} aria-expanded={actions.activeId === id} disabled={!actions.editable}
      onClick={event => actions.edit(id, event.currentTarget)}>
      <MessageSquare size={13} /><span className="annotation-chip__number">{number}</span>
      <span className="annotation-chip__summary">{value.comment?.trim() || value.quote}</span>
    </button>
    <button type="button" className="annotation-chip__remove" aria-label={`移除批注 ${number}`} disabled={!actions.editable}
      onClick={() => { if (actions.editable) { editor.view.dispatch(closeHistory(editor.state.tr)); deleteNode(); } }}><X size={12} /></button>
  </NodeViewWrapper>;
}

export const PromptAnnotationNode = Node.create({
  name: 'annotation', group: 'block', atom: true, selectable: true,
  addAttributes: () => ({ value: { default: null, rendered: false }, draftId: { default: '', rendered: false } }),
  parseHTML: () => [],
  renderHTML: ({ node }) => ['div', { 'data-prompt-annotation': '' }, (node.attrs.value as PromptAnnotationPart).quote],
  renderText: ({ node }) => (node.attrs.value as PromptAnnotationPart).quote,
  addNodeView: () => ReactNodeViewRenderer(AnnotationView),
});
