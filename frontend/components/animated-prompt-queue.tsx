import { Children, useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactElement, type ReactNode } from 'react';

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
export function AnimatedPromptQueue({ children }: { children: ReactNode }) {
  const incoming = useMemo(() => Children.toArray(children) as ReactElement[], [children]);
  const [present, setPresent] = useState(incoming);
  useLayoutEffect(() => {
    // Preserve the same bubble when a local submission becomes canonical,
    // and retain removed bubbles solely until the exit animation settles.
    setPresent(current => [
      ...current.map(item => incoming.find(next => next.key === item.key) ?? item),
      ...incoming.filter(item => !current.some(previous => previous.key === item.key)),
    ]);
  }, [incoming]);
  const finishExit = useCallback((key: string) => {
    setPresent(current => current.filter(item => String(item.key) !== key));
  }, []);

  if (!present.length) return null;
  return <section className="composer-queue" aria-label="等待处理的输入">
    {present.map(item => <QueueBubble key={item.key} exiting={!incoming.some(next => next.key === item.key)} onExitComplete={finishExit}>
      {item}
    </QueueBubble>)}
  </section>;
}
