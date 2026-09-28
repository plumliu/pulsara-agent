import { useCallback, useEffect, useRef, useState } from 'react';

// Shared by path details and image previews; content and image loading stay with their owners.
export function usePromptHover<T extends HTMLElement = HTMLButtonElement>(maxWidth: number, onShow?: () => void) {
  const [position, setPosition] = useState<{ left: number; top: number; above: boolean }>();
  const trigger = useRef<T>(null);
  const details = useRef<HTMLSpanElement>(null);
  const closing = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const keep = () => clearTimeout(closing.current);
  const dismiss = useCallback(() => {
    clearTimeout(closing.current);
    setPosition(undefined);
  }, []);
  const show = () => {
    keep();
    const rect = trigger.current?.getBoundingClientRect();
    if (!rect) return;
    setPosition({ left: Math.max(8, Math.min(rect.left, window.innerWidth - maxWidth - 8)),
      top: rect.top > 180 ? rect.top : rect.bottom, above: rect.top > 180 });
    onShow?.();
  };
  const hide = () => { keep(); closing.current = setTimeout(dismiss, 120); };
  useEffect(() => () => clearTimeout(closing.current), []);
  useEffect(() => {
    if (!position) return;
    const close = (event: Event) => {
      if (event.target instanceof Node && (trigger.current?.contains(event.target) || details.current?.contains(event.target))) return;
      dismiss();
    };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') dismiss(); };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', escape);
    window.addEventListener('resize', close);
    document.addEventListener('scroll', close, true);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', escape);
      window.removeEventListener('resize', close);
      document.removeEventListener('scroll', close, true);
    };
  }, [position, dismiss]);
  return { trigger, details, position, show, hide, keep, dismiss };
}
