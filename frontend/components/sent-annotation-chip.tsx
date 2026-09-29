'use client';

import { autoUpdate, computePosition, flip, offset, shift } from '@floating-ui/dom';
import { MessageSquare } from 'lucide-react';
import { useId, useLayoutEffect } from 'react';
import { createPortal } from 'react-dom';
import type { PromptAnnotationPart } from '../lib/prompt-content';
import { usePromptHover } from '../lib/prompt-hover';
import { AnnotationQuote } from './annotation-quote';

/** Read the submitted snapshot without entering the draft or resolving its source. */
export function SentAnnotationChip({ value, number }: { value: PromptAnnotationPart; number: number }) {
  const { trigger, details, position, show, hide, keep, dismiss } = usePromptHover<HTMLButtonElement, HTMLDivElement>(350);
  const id = useId();
  const open = Boolean(position);
  useLayoutEffect(() => {
    const reference = trigger.current;
    const floating = details.current;
    if (!open || !reference || !floating) return;
    let disposed = false;
    const update = () => {
      void computePosition(reference, floating, {
        placement: 'top-end', strategy: 'fixed',
        middleware: [offset(8), flip({ padding: 8 }), shift({ padding: 8, crossAxis: true })],
      }).then(({ x, y }) => {
        if (!disposed) Object.assign(floating.style, { left: `${x}px`, top: `${y}px`, visibility: 'visible' });
      });
    };
    const cleanup = autoUpdate(reference, floating, update);
    return () => { disposed = true; cleanup(); };
  }, [open, trigger, details]);

  return <>
    <span className={`annotation-chip annotation-chip--sent${open ? ' is-active' : ''}`}>
      <button ref={trigger} type="button" className="annotation-chip__open" aria-label={`查看批注 ${number}`}
        aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined}
        onMouseEnter={show} onMouseLeave={hide} onFocus={show}
        onBlur={event => { if (!details.current?.contains(event.relatedTarget)) hide(); }} onClick={show}
        onKeyDown={event => { if (event.key === 'ArrowDown' && open) { event.preventDefault(); details.current?.focus(); } }}>
        <MessageSquare size={13} aria-hidden="true" /><span className="annotation-chip__number">{number}</span>
        <span className="annotation-chip__summary">{value.comment?.trim() || value.quote}</span>
      </button>
    </span>
    {open && createPortal(<div ref={details} id={id} role="dialog" aria-labelledby={`${id}-title`} tabIndex={-1}
      className="sent-annotation-preview prompt-annotation" style={{ visibility: 'hidden' }}
      onMouseEnter={keep} onMouseLeave={hide} onFocusCapture={keep}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget) && !trigger.current?.contains(event.relatedTarget)) hide(); }}
      onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); trigger.current?.focus(); dismiss(); } }}>
      <div id={`${id}-title`} className="sent-annotation-preview__title">Annotation {number}</div>
      <AnnotationQuote quote={value.quote} />
      {value.comment && <p className="sent-annotation-preview__comment">{value.comment}</p>}
    </div>, document.body)}
  </>;
}
