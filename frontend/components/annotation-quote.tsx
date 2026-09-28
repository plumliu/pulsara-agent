import { useLayoutEffect, useRef, useState } from 'react';

/** Presentation state only; the exact quote remains in the draft/canonical part. */
export function AnnotationQuote({ quote }: { quote: string }) {
  const ref = useRef<HTMLQuoteElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node || expanded) return;
    const measure = () => setOverflowing(node.scrollHeight > node.clientHeight);
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(measure);
    observer?.observe(node);
    return () => observer?.disconnect();
  }, [quote, expanded]);
  return <>
    <blockquote ref={ref} className={expanded ? 'is-expanded' : undefined}>{quote}</blockquote>
    {(overflowing || expanded) && <button type="button" className="prompt-annotation__expand" aria-expanded={expanded}
      onClick={() => setExpanded(value => !value)}>{expanded ? '收起' : '更多'}</button>}
  </>;
}
