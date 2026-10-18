import { useEffect, useRef, useState, type PropsWithChildren } from 'react';

export function AnimatedDisclosure({
  open,
  className,
  contentClassName,
  unmountOnClose = false,
  children,
}: PropsWithChildren<{
  open: boolean;
  className?: string;
  contentClassName?: string;
  unmountOnClose?: boolean;
}>) {
  const [hasOpened, setHasOpened] = useState(open);
  const container = useRef<HTMLDivElement>(null);

  if (open && !hasOpened) setHasOpened(true);
  useEffect(() => {
    if (open || !unmountOnClose || !hasOpened) return;
    // CSS owns timing and reduced-motion. Await only this container's own
    // transitions; cancellation/no animation must release the subtree too.
    const animations = container.current?.getAnimations?.() ?? [];
    if (!animations.length) { setHasOpened(false); return; }
    let current = true;
    void Promise.allSettled(animations.map(animation => animation.finished)).then(() => {
      if (current) setHasOpened(false);
    });
    return () => { current = false; };
  }, [open, unmountOnClose, hasOpened]);

  return (
    <div
      ref={container}
      className={`animated-disclosure${open ? ' is-open' : ''}${className ? ` ${className}` : ''}`}
      aria-hidden={!open}
      inert={!open}
    >
      <div className={`animated-disclosure__content${contentClassName ? ` ${contentClassName}` : ''}`}>
        {(open || hasOpened) && children}
      </div>
    </div>
  );
}
