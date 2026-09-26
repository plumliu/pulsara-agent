import { ImageIcon, LoaderCircle, TriangleAlert } from 'lucide-react';
import { useId, type MouseEventHandler } from 'react';
import { createPortal } from 'react-dom';
import { usePromptHover } from '../lib/prompt-hover';

export function PromptImageChip({ number, state, reason, previewSrc, onPreview, onClick }: {
  number: number;
  state?: 'loading' | 'ready' | 'failed';
  reason?: string;
  previewSrc?: string;
  onPreview?: () => void;
  onClick: MouseEventHandler<HTMLButtonElement>;
}) {
  const { trigger, details, position, show, hide, keep, dismiss } = usePromptHover(208, onPreview);
  const tooltipId = useId();
  return <><button ref={trigger} type="button" className={`prompt-image-chip${state ? ` is-${state}` : ''}`}
    aria-label={`打开 Figure ${number}`} aria-busy={state === 'loading'}
    aria-describedby={position ? tooltipId : undefined}
    onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}
    onClick={event => { dismiss(); onClick(event); }}>
    <ImageIcon size={14} aria-hidden="true" /><span>Figure {number}</span>
    {state === 'loading' && <><LoaderCircle size={12} className="spin" aria-hidden="true" /><small>读取中…</small></>}
    {state === 'failed' && <><TriangleAlert size={12} aria-hidden="true" /><small>读取失败</small></>}
  </button>
    {position && createPortal(<span ref={details} id={tooltipId} className="prompt-image-tooltip" role="tooltip"
      style={{ left: position.left, top: position.top, translate: position.above ? '0 -100%' : undefined }}
      onMouseEnter={keep} onMouseLeave={hide}>
      {state === 'failed' ? <span className="prompt-image-tooltip__state">{reason ?? '图片读取失败，点击查看详情'}</span>
        : previewSrc
          // The image owner supplies its existing Blob URL; the preview owns no extra copy.
          // eslint-disable-next-line @next/next/no-img-element
          ? <img src={previewSrc} alt={`Figure ${number} 缩略图`} />
          : <span className="prompt-image-tooltip__state"><LoaderCircle size={16} className="spin" aria-hidden="true" />正在读取图片…</span>}
    </span>, document.body)}
  </>;
}
