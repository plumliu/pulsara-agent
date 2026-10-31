import type { ReactNode, RefObject, UIEventHandler } from 'react';
import { FileCode } from './file-code';

/** Shared file-reading presentation; each source retains its own read owner. */
export function FilePreviewPanel({ label, className, toolbar, notice, children }: {
  label: string;
  className?: string;
  toolbar?: ReactNode;
  notice?: ReactNode;
  children: ReactNode;
}) {
  return <section className={`file-browser-panel${className ? ` ${className}` : ''}`} aria-label={label}>
    {toolbar && <header className="file-preview-header">{toolbar}</header>}
    {notice}
    <div className="file-preview-body">{children}</div>
  </section>;
}

export function FileTextContent({ source, filename, markdown, scrollerRef, onScroll }: {
  source: string;
  filename: string;
  markdown?: ReactNode;
  scrollerRef?: RefObject<HTMLDivElement | null>;
  onScroll?: UIEventHandler<HTMLDivElement>;
}) {
  return <div className={`file-preview-text${markdown ? ' is-markdown assistant-markdown' : ''}`} ref={scrollerRef} onScroll={onScroll}>
    {!source ? <p className="file-preview-empty">空文件</p>
      : markdown ?? <FileCode source={source} filename={filename} />}
  </div>;
}
