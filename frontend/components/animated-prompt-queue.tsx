import { Children, Fragment, useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactElement, type ReactNode } from 'react';

// A display budget, not a queue limit. The row estimate includes card padding
// and spacing; reserve one quiet line for the omitted middle.
export function queuePreviewCapacity(availableHeight: number, width: number): number {
  return Math.max(2, Math.floor((availableHeight - 24) / (width <= 480 ? 80 : 60)));
}

const timing = { duration: 240, easing: 'cubic-bezier(.2,.7,.2,1)' };
const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

function QueueBubble({ children, exiting, onExitComplete }: {
  children: ReactElement; exiting: boolean; onExitComplete: (key: string) => void;
}) {
  const row = useRef<HTMLDivElement>(null);
  const bubble = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const wrapper = row.current;
    const content = bubble.current;
    if (!wrapper || !content) return;
    if (!wrapper.animate || reducedMotion()) {
      if (exiting) onExitComplete(String(children.key));
      return;
    }
    // Same native animation mechanism and easing as ToastStack. Only a
    // removed input exits: there is no expiry, gesture, or queue mutation here.
    const height = wrapper.getBoundingClientRect().height;
    const layout = wrapper.animate(exiting
      ? [{ height: `${height}px` }, { height: '0px' }]
      : [{ height: '0px' }, { height: `${height}px` }], { ...timing, fill: exiting ? 'forwards' : 'none' });
    const surface = content.animate(exiting
      ? [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateX(32px) scale(.98)' }]
      : [{ opacity: 0, transform: 'translateY(12px) scale(.985)' }, { opacity: 1, transform: 'none' }], { ...timing, fill: exiting ? 'forwards' : 'none' });
    let current = true;
    if (exiting) void Promise.allSettled([layout.finished, surface.finished]).then(() => {
      if (current) onExitComplete(String(children.key));
    });
    return () => { current = false; layout.cancel(); surface.cancel(); };
  }, [exiting, children.key, onExitComplete]);

  return <div ref={row} className={`composer-queue__item${exiting ? ' is-exiting' : ''}`} aria-hidden={exiting || undefined} inert={exiting}>
    <div ref={bubble} className="composer-queue__item-content">{children}</div>
  </div>;
}

/** Short-lived visual presence only; the caller owns all queue semantics. */
export function AnimatedPromptQueue({ children, maxVisibleItems, unloadedCount = 0 }: {
  children: ReactNode; maxVisibleItems?: number; unloadedCount?: number;
}) {
  const incoming = useMemo(() => Children.toArray(children) as ReactElement[], [children]);
  const limit = Math.max(2, maxVisibleItems ?? incoming.length);
  const headCount = Math.ceil(limit / 2);
  const visible = useMemo(() => {
    // Until the remaining pages arrive, the last loaded row is not the tail.
    if (unloadedCount > 0) return incoming.slice(0, headCount);
    return incoming.length <= limit ? incoming
      : [...incoming.slice(0, headCount), ...incoming.slice(-Math.floor(limit / 2))];
  }, [incoming, limit, headCount, unloadedCount]);
  const omittedCount = incoming.length + unloadedCount - visible.length;
  const firstTailKey = omittedCount > 0 && unloadedCount === 0 ? visible[headCount]?.key : undefined;
  const [present, setPresent] = useState(visible);
  useLayoutEffect(() => {
    // Preserve the same bubble when a local submission becomes canonical,
    // retaining only actual departures for exit animation. Moving into the
    // omitted middle is a display change, never a consumption/cancel signal.
    setPresent(current => {
      const next = [...visible];
      const incomingKeys = new Set(incoming.map(item => item.key));
      current.forEach((item, index) => {
        if (incomingKeys.has(item.key)) return;
        const following = current.slice(index + 1).find(candidate => next.some(value => value.key === candidate.key));
        const position = following ? next.findIndex(value => value.key === following.key) : next.length;
        next.splice(position, 0, item);
      });
      return next;
    });
  }, [incoming, visible]);
  const finishExit = useCallback((key: string) => {
    setPresent(current => current.filter(item => String(item.key) !== key));
  }, []);

  if (!present.length && !omittedCount) return null;
  const omission = omittedCount > 0 ? <p className="composer-queue__omission">
    {unloadedCount > 0 ? `还有 ${omittedCount} 条排队消息` : `中间省略 ${omittedCount} 条排队消息`}
  </p> : null;
  return <section className="composer-queue" aria-label="等待处理的输入">
    {present.map(item => <Fragment key={item.key}>
      {item.key === firstTailKey && omission}
      <QueueBubble exiting={!incoming.some(next => next.key === item.key)} onExitComplete={finishExit}>
        {item}
      </QueueBubble>
    </Fragment>)}
    {unloadedCount > 0 && omission}
  </section>;
}
