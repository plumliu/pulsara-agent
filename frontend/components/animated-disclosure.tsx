'use client';

import { useState, type PropsWithChildren } from 'react';

export function AnimatedDisclosure({
  open,
  className,
  contentClassName,
  children,
}: PropsWithChildren<{
  open: boolean;
  className?: string;
  contentClassName?: string;
}>) {
  const [hasOpened, setHasOpened] = useState(open);

  if (open && !hasOpened) setHasOpened(true);

  return (
    <div
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
