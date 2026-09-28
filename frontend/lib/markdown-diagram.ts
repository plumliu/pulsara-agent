import { startsWithSvg } from './svg-renderer';

/** Rendering and source mapping must agree on which fences are atomic. */
export function markdownDiagramFormat(language: string | undefined, source: string): 'mermaid' | 'svg' | undefined {
  if (language === 'mermaid') return 'mermaid';
  if (language === 'svg' || ((!language || language === 'xml' || language === 'html') && startsWithSvg(source))) return 'svg';
}
