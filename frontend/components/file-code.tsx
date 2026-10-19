import { fileCodeLanguage, useCodeHighlight } from '../lib/code-highlight';

export function FileCode({ source, filename }: { source: string; filename: string }) {
  // Only the current preview page is highlighted, including partial lines at
  // page boundaries. No whole-file reads or cross-page parser state are needed.
  const pieces = useCodeHighlight(source, fileCodeLanguage(filename));
  return <div className="file-preview-code">
    <pre className="file-preview-line-numbers" aria-hidden="true">
      {source.split('\n').map((_, index) => index + 1).join('\n')}
    </pre>
    <pre><code>{pieces
      ? pieces.map((piece, index) => <span key={index} className={piece.type ? `code-card__token--${piece.type}` : undefined}>{piece.text}</span>)
      : source}</code></pre>
  </div>;
}
