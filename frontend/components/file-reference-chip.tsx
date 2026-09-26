'use client';

import { Check, Copy, File, FileText, Folder, Presentation, Sheet, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import { createPortal } from 'react-dom';
import { fileAppearance, type FileReference } from '../lib/file-reference';
import { usePromptHover } from '../lib/prompt-hover';

export function FileReferenceChip({ reference }: { reference: FileReference }) {
  const { trigger, details, position, show, hide, keep } = usePromptHover(340);
  const [copyState, setCopyState] = useState<'copied' | 'failed'>();
  const appearance = fileAppearance(reference.name, reference.kind === 'directory');
  const Icon = { directory: Folder, pdf: FileText, word: FileText,
    slides: Presentation, sheet: Sheet, file: File }[appearance.type] ?? File;
  const dot = reference.name.lastIndexOf('.');
  const hasExtension = reference.kind === 'file' && dot > 0;
  const copyLabel = copyState === 'copied' ? '已复制路径' : copyState === 'failed' ? '复制失败，可选择路径手动复制' : '复制路径';
  const CopyIcon = copyState === 'copied' ? Check : copyState === 'failed' ? TriangleAlert : Copy;
  return <span className={`file-reference file-reference--${appearance.type}`} data-file-reference={reference.raw}>
    <button ref={trigger} type="button" className="file-reference__label" aria-expanded={Boolean(position)}
      aria-label={`${appearance.label}：${reference.name}`}
      onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide} onClick={show}>
      <Icon size={14} aria-hidden="true" />
      <span className="file-reference__filename">
        <span className="file-reference__name">{hasExtension ? reference.name.slice(0, dot) : reference.name}</span>
        {hasExtension && <span className="file-reference__extension">{reference.name.slice(dot)}</span>}
      </span>
    </button>
    {position && createPortal(<span ref={details} className="file-reference__details" role="group" aria-label="文件引用详情"
      style={{ left: position.left, top: position.top, translate: position.above ? '0 -100%' : undefined }}
      onMouseEnter={keep} onMouseLeave={hide} onFocus={keep} onBlur={hide}>
      <span className="file-reference__path">{reference.path}</span>
      <button type="button" className="file-reference__copy" aria-label={copyLabel} title={copyLabel} onClick={async () => {
        try { await navigator.clipboard.writeText(reference.path); setCopyState('copied'); }
        catch { setCopyState('failed'); }
      }}><CopyIcon size={13} aria-hidden="true" /></button>
    </span>, document.body)}
  </span>;
}
