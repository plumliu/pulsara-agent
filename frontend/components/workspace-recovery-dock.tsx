import { writeClipboardText } from '../lib/desktop-host';

import { Copy, Eye, FolderX, RefreshCw } from 'lucide-react';
import { useId, useState } from 'react';
import { createPortal } from 'react-dom';
import type { WorkspaceAvailability } from '../lib/runtime-adapter';
import { usePromptHover } from '../lib/prompt-hover';
import type { MarkdownNotify } from './markdown-body';

export function WorkspaceRecoveryDock({ isObserver, availability, busy, error, onTakeControl, onRestore, onRecheck, onNotify }: {
  isObserver: boolean;
  availability?: WorkspaceAvailability;
  busy?: boolean;
  error?: string;
  onTakeControl: () => void;
  onRestore?: () => Promise<void>;
  onRecheck?: () => Promise<void>;
  onNotify: MarkdownNotify;
}) {
  const { trigger, details, position, show, hide, keep } = usePromptHover(440);
  const [tooltipPosition, setTooltipPosition] = useState(position);
  // Keep the last placement while CSS fades the tooltip out.
  if (position && position !== tooltipPosition) setTooltipPosition(position);
  const pathId = useId();
  const missing = availability?.outcome === 'MISSING';
  const path = availability?.path;
  const pathNameIndex = path ? Math.max(0, path.lastIndexOf('/'), path.lastIndexOf('\\')) : 0;
  return <div className={`observer-dock ${isObserver ? 'observer-dock--observer' : 'readonly-dock'}`}>
    <span className="observer-dock__icon">{isObserver ? <Eye size={15} /> : <FolderX size={16} />}</span>
    <div className="readonly-dock__copy">
      <strong>{isObserver ? '此窗口仅供查看' : missing ? '工作目录已丢失' : availability?.outcome === 'UNAVAILABLE' ? '工作目录不可用' : '暂时无法继续'}</strong>
      {!isObserver && <>
        {missing && <small>历史对话仍可查看。继续时会在原路径创建空目录，原文件不会恢复。</small>}
        {availability?.outcome === 'UNAVAILABLE' && <small>{availability.reason}</small>}
        {path && <button ref={trigger} type="button" className="readonly-dock__path" aria-label="复制工作目录路径"
          aria-describedby={position ? pathId : undefined}
          onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}
          onClick={async () => {
            try { await writeClipboardText(path); onNotify('路径已复制', undefined, 'success'); }
            catch { onNotify('无法复制路径', '系统剪贴板暂时不可用，请重试', 'warning'); }
          }}>
          <span className="readonly-dock__path-text"><span>{path.slice(0, pathNameIndex)}</span><span>{path.slice(pathNameIndex)}</span></span>
          <Copy size={12} aria-hidden="true" />
        </button>}
      </>}
      {error && <small className="readonly-dock__error" role="alert">{error}</small>}
    </div>
    {!isObserver && <button type="button" className="readonly-dock__refresh" aria-label="重新检查工作目录" title="重新检查"
      disabled={busy} onClick={() => void onRecheck?.()}><RefreshCw size={14} aria-hidden="true" /></button>}
    {(isObserver || missing) && <button type="button" className="readonly-dock__continue" disabled={busy}
      onClick={() => { if (isObserver) onTakeControl(); else void onRestore?.(); }}>
      {busy ? '正在创建…' : isObserver ? '在此窗口继续' : '创建目录并继续'}
    </button>}
    {!isObserver && path && tooltipPosition && createPortal(<span ref={details} id={pathId} role="tooltip" className="readonly-dock__path-tooltip"
      data-open={Boolean(position)} data-side={tooltipPosition.above ? 'above' : 'below'} aria-hidden={!position} inert={!position}
      style={{ left: tooltipPosition.left, top: tooltipPosition.top, translate: tooltipPosition.above ? '0 calc(-100% - 6px)' : '0 6px' }}
      onMouseEnter={keep} onMouseLeave={hide}>{path}</span>, document.body)}
  </div>;
}
