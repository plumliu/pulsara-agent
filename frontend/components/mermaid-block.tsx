'use client';

import { useCallback, useSyncExternalStore } from 'react';
import { DiagramBlock } from './diagram-block';
import { renderMermaid } from '../lib/mermaid-renderer';
import type { MarkdownNotify } from './markdown-body';

const isDark = () => document.documentElement.dataset.theme === 'dark';
const serverTheme = () => false;
function subscribeTheme(notify: () => void) {
  const observer = new MutationObserver(notify);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  return () => observer.disconnect();
}

export function MermaidBlock({ source, streaming, onNotify }: {
  source: string;
  streaming: boolean;
  onNotify: MarkdownNotify;
}) {
  const dark = useSyncExternalStore(subscribeTheme, isDark, serverTheme);
  const renderImage = useCallback((value: string, signal: AbortSignal) => renderMermaid(value, dark, signal), [dark]);
  return <DiagramBlock source={source} streaming={streaming} onNotify={onNotify} format="Mermaid" renderImage={renderImage} />;
}
