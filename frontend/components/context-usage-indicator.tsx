'use client';

import { autoUpdate, computePosition, flip, offset, shift } from '@floating-ui/dom';
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { ContextUsagePreview, ModelCallBindingPayload } from '../lib/runtime-adapter';

interface Props {
  sessionId: string;
  binding: ModelCallBindingPayload;
  revision: number | string;
  isRunning: boolean;
  readUsage: (signal: AbortSignal) => Promise<ContextUsagePreview>;
}

export function ContextUsageIndicator({ sessionId, binding, revision, isRunning, readUsage }: Props) {
  const id = useId();
  const key = `${sessionId}:${JSON.stringify(binding)}`;
  const [observation, setObservation] = useState<{ key: string; value: ContextUsagePreview }>();
  const [expanded, setExpanded] = useState(false);
  const [inspectionRevision, setInspectionRevision] = useState(0);
  const trigger = useRef<HTMLButtonElement>(null);
  const details = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    // Older responses cannot repaint a newly selected model or session.
    Promise.resolve().then(() => readUsage(controller.signal)).then(value => {
      if (!controller.signal.aborted && (value.connection_id === binding.connection_id
        || (value.connection_id == null && value.state !== 'ready'))) {
        setObservation({ key, value });
      }
    }).catch(() => {
      if (!controller.signal.aborted) setObservation({ key, value: { state: 'unavailable', connection_id: binding.connection_id } });
    });
    return () => controller.abort();
  }, [key, binding.connection_id, revision, isRunning, readUsage, inspectionRevision]);

  useEffect(() => {
    if (!expanded) return;
    const dismiss = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setExpanded(false);
    };
    document.addEventListener('keydown', dismiss);
    return () => document.removeEventListener('keydown', dismiss);
  }, [expanded]);

  const value = observation?.key === key ? observation.value : undefined;
  const used = value?.input_tokens;
  const budget = value?.input_budget_tokens;
  const known = value?.state === 'ready' && used != null && budget != null && budget > 0;
  const percentage = known ? Math.round(used / budget * 100) : undefined;
  const needsCompaction = Boolean(known && value?.compaction_expected);
  const tone = needsCompaction ? 'danger' : known ? 'normal' : 'unknown';
  const label = percentage === undefined ? '上下文占用' : `上下文约占 ${percentage}%${needsCompaction ? '，需要压缩' : ''}`;

  const inspect = () => {
    if (!expanded) setInspectionRevision(current => current + 1);
    setExpanded(true);
  };

  useLayoutEffect(() => {
    const reference = trigger.current;
    const floating = details.current;
    if (!expanded || !reference || !floating) return;
    let disposed = false;
    const update = () => {
      void computePosition(reference, floating, {
        placement: 'top-end', strategy: 'fixed',
        middleware: [offset(10), flip({ padding: 8 }), shift({ padding: 8, crossAxis: true })],
      }).then(({ x, y }) => {
        if (!disposed) Object.assign(floating.style, { left: `${x}px`, top: `${y}px`, visibility: 'visible' });
      });
    };
    const cleanup = autoUpdate(reference, floating, update);
    return () => { disposed = true; cleanup(); };
  }, [expanded, percentage]);

  return <div className="context-usage" data-tone={tone} data-expanded={expanded}>
    <button ref={trigger} type="button" className="context-usage__button" aria-label={label}
      aria-describedby={expanded ? id : undefined} aria-expanded={expanded}
      onMouseEnter={inspect} onMouseLeave={() => setExpanded(false)}
      onFocus={inspect} onClick={inspect}
      onBlur={() => setExpanded(false)}>
      <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
        <circle className="context-usage__track" cx="12" cy="12" r="8" />
        {known && <circle className="context-usage__fill" cx="12" cy="12" r="8" pathLength="100"
          strokeDasharray={`${Math.min(100, Math.max(0, percentage!))} 100`} transform="rotate(-90 12 12)" />}
        {!known && <circle className="context-usage__dot" cx="12" cy="12" r="1.5" />}
      </svg>
    </button>
    {expanded && createPortal(<div ref={details} id={id} className="context-usage__tooltip" role="tooltip"
      data-tone={tone} style={{ visibility: 'hidden' }}>
      <div className="context-usage__heading"><strong>上下文占用</strong><span>{percentage === undefined ? '—' : `约 ${percentage}%`}</span></div>
      {known && <p className="context-usage__amount">
        <span>已用约 {used.toLocaleString('zh-CN')} tokens</span>
        <span>可用输入额度 {budget.toLocaleString('zh-CN')} tokens</span>
      </p>}
    </div>, document.body)}
  </div>;
}
