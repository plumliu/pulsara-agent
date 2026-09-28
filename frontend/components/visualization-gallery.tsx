'use client';

import { ChevronLeft, ChevronRight, Eye, ImageOff, Images, Maximize2, Minimize2, RotateCcw } from 'lucide-react';
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { VisualizationOccurrence } from '../lib/pulsara-types';
import { usableVisualizationRootRect, visualizationLayoutMessageType, type VisualizationRootRect } from '../lib/visualization-frame';
import { AnimatedDisclosure } from './animated-disclosure';
import { SandboxedHtmlPreview } from './sandboxed-html-preview';

type ReadVisualization = (entry: string, ordinal: number, digest: string, size: number) => Promise<string>;
export type ReadVisualizationThumbnail = (entry: string, ordinal: number, digest: string, size: number, signal: AbortSignal) => Promise<string>;

// UI browser residency, not an artifact count limit: the current page and two
// recent pages retain arbitrary script state. Older pages can always reopen.
const residentFrames = 3;
const visualizationName = (item: VisualizationOccurrence) => item.sourceFilename || `可视化 ${item.ordinal + 1}`;
let thumbnailQueue: Promise<unknown> = Promise.resolve();
function readThumbnailInOrder(read: () => Promise<string>, signal: AbortSignal): Promise<string> {
  const pending = thumbnailQueue.then(() => { signal.throwIfAborted(); return read(); });
  thumbnailQueue = pending.catch(() => undefined);
  return pending;
}

function Thumbnail({ item, entryId, enabled, onRead }: {
  item: VisualizationOccurrence; entryId: string; enabled: boolean; onRead?: ReadVisualizationThumbnail;
}) {
  const [image, setImage] = useState<string>();
  const [failed, setFailed] = useState(false);
  const { ordinal, visualizationRef: digest, contentSize: size, state } = item;
  useEffect(() => {
    if (!enabled || image || failed || !onRead || state !== 'READY' || !digest || !size) return;
    const controller = new AbortController();
    void readThumbnailInOrder(() => onRead(entryId, ordinal, digest, size, controller.signal), controller.signal)
      .then(value => { if (!controller.signal.aborted) setImage(value); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [enabled, image, failed, onRead, entryId, ordinal, digest, size, state]);
  return <span className="visualization-gallery__thumbnail-image" aria-hidden="true">
    {/* Local derived PNGs need no image proxy or external optimizer. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    {image ? <img src={image} alt="" draggable={false} onError={() => { setImage(undefined); setFailed(true); }} />
      : state === 'FAILED' || failed ? <ImageOff size={19} /> : <Images size={19} />}
  </span>;
}

function GalleryPage({ entryId, item, onRead, active }: {
  entryId: string; item: VisualizationOccurrence; onRead: ReadVisualization; active: boolean;
}) {
  const [result, setResult] = useState<{ html?: string; error?: string }>({});
  const [attempt, setAttempt] = useState(0);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [measurement, setMeasurement] = useState<{ rect: VisualizationRootRect | null; width: number; height: number }>();
  const pageRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const pendingRead = useRef<{ args: unknown[]; promise: Promise<string> } | null>(null);
  const { ordinal, visualizationRef: digest, contentSize, state } = item;
  useEffect(() => {
    if (state !== 'READY' || !digest || !contentSize) return;
    let live = true;
    const args = [entryId, ordinal, digest, contentSize, onRead, attempt];
    if (!pendingRead.current || args.some((value, i) => value !== pendingRead.current!.args[i])) {
      pendingRead.current = { args, promise: onRead(entryId, ordinal, digest, contentSize) };
    }
    void pendingRead.current.promise.then(
      html => { if (live) setResult({ html }); },
      () => { if (live) setResult({ error: '已保存的可视化暂时无法读取。' }); },
    );
    return () => { live = false; };
  }, [entryId, ordinal, digest, contentSize, state, onRead, attempt]);
  useLayoutEffect(() => {
    const element = pageRef.current;
    if (!element) return;
    const measure = () => {
      const width = element.clientWidth, height = element.clientHeight;
      // Closing a disclosure must not resize its live HTML to a zero viewport.
      if (!width || !height) return;
      setSize(old => old.width === width && old.height === height ? old : { width, height });
    };
    const observer = new ResizeObserver(measure);
    observer.observe(element); measure();
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      const frame = frameRef.current;
      if (!frame || event.source !== frame.contentWindow || event.data?.type !== visualizationLayoutMessageType) return;
      const rect = event.data.mode === 'root'
        ? usableVisualizationRootRect(event.data.rect, frame.clientWidth, frame.clientHeight) : null;
      setMeasurement({ rect, width: frame.clientWidth, height: frame.clientHeight });
    };
    window.addEventListener('message', receive);
    return () => window.removeEventListener('message', receive);
  }, []);
  // A root measured at an old viewport must not crop a newly resized page.
  const rect = measurement?.width === size.width && measurement?.height === size.height ? measurement.rect : null;
  const error = state === 'FAILED' ? item.failureDetail ?? '可视化未能生成。'
    : !digest || !contentSize ? '可视化引用不完整。' : result.error;
  return <div ref={pageRef} className={`visualization-gallery__page${active ? ' is-active' : ''}`}
    aria-hidden={!active} inert={!active} data-visualization-ordinal={ordinal} data-visualization-layout={rect ? 'root' : 'page'}>
    {error ? <div className="visualization-gallery__status" role="status"><ImageOff size={22} /><p>{error}</p>
      {result.error && <button onClick={() => { setResult({}); setAttempt(value => value + 1); }}><RotateCcw size={14} />重试</button>}
    </div> : result.html === undefined ? <div className="visualization-gallery__status" role="status">正在加载可视化…</div>
      : <div className="visualization-gallery__crop" style={rect ? { width: rect.width, height: rect.height } : undefined}>
        <SandboxedHtmlPreview frameRef={frameRef} title={visualizationName(item)}
          source={{ html: result.html, measure: true, allowTextSelection: false }}
          style={{ width: size.width || '100%', height: size.height || '100%',
            transform: rect ? `translate(${-rect.x}px, ${-rect.y}px)` : undefined }} />
      </div>}
  </div>;
}

export function VisualizationGallery({ entryId, items, onRead, onReadThumbnail }: {
  entryId: string; items: VisualizationOccurrence[]; onRead: ReadVisualization; onReadThumbnail?: ReadVisualizationThumbnail;
}) {
  const [expanded, setExpanded] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const [selected, setSelected] = useState(items[0]?.ordinal);
  const [resident, setResident] = useState<number[]>([]);
  const [visible, setVisible] = useState<Set<number>>(new Set());
  const dialogRef = useRef<HTMLDialogElement>(null);
  const stripRef = useRef<HTMLDivElement>(null);
  const enlargeRef = useRef<HTMLButtonElement>(null);
  const shrinkRef = useRef<HTMLButtonElement>(null);
  const contentId = useId();
  const index = Math.max(0, items.findIndex(item => item.ordinal === selected));
  const current = items[index];
  const ordinal = current?.ordinal;
  if (expanded && ordinal !== undefined && !resident.includes(ordinal)) {
    setResident(old => [...old.filter(value => value !== ordinal), ordinal].slice(-residentFrames));
  }
  const select = (next: number) => {
    const item = items[next];
    if (!item) return;
    setSelected(item.ordinal);
    setResident(old => [...old.filter(value => value !== item.ordinal), item.ordinal].slice(-residentFrames));
  };
  useEffect(() => {
    if (!expanded || !stripRef.current) return;
    const observer = new IntersectionObserver(entries => setVisible(old => {
      const next = new Set(old);
      for (const entry of entries) {
        const ordinal = Number((entry.target as HTMLElement).dataset.ordinal);
        if (entry.isIntersecting) next.add(ordinal); else next.delete(ordinal);
      }
      return next;
    }), { root: stripRef.current });
    for (const button of stripRef.current.children) observer.observe(button);
    return () => observer.disconnect();
  }, [expanded, items.length]);
  useEffect(() => {
    const strip = stripRef.current;
    const button = strip?.querySelector<HTMLElement>('[aria-pressed="true"]');
    if (!strip || !button) return;
    // Scroll only the filmstrip, never scroll the conversation/page into view.
    const left = button.offsetLeft, right = left + button.offsetWidth;
    if (left < strip.scrollLeft) strip.scrollLeft = left;
    else if (right > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = right - strip.clientWidth;
  }, [selected, expanded]);
  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (maximized) {
      dialog.close();
      dialog.showModal();
      shrinkRef.current?.focus({ preventScroll: true });
    } else if (dialog.matches(':modal')) {
      dialog.close();
      dialog.show();
      enlargeRef.current?.focus({ preventScroll: true });
    }
  }, [maximized]);
  if (!current) return null;
  return <section className={`assistant-visualization${expanded ? ' is-expanded' : ''}`} aria-label="可视化图集">
    <button className="assistant-visualization__toggle" aria-expanded={expanded} aria-controls={contentId}
      aria-label={`${expanded ? '收起' : '展开'}可视化图集`} onClick={() => setExpanded(value => !value)}>
      <Eye size={15} /><span>可视化 · 共 {items.length} 张</span><small>{expanded ? '收起' : '展开'}</small>
      <ChevronRight size={14} className="assistant-visualization__chevron" />
    </button>
    <div id={contentId}>
      <AnimatedDisclosure open={expanded}>
        <div className={`visualization-gallery__slot${items.length > 1 ? ' has-thumbnails' : ''}`}>
          <dialog ref={dialogRef} open className={`visualization-gallery__body${maximized ? ' is-maximized' : ''}`}
            aria-label="可视化查看器" onCancel={event => { event.preventDefault(); setMaximized(false); }}
            onClick={event => {
              if (!maximized || event.target !== event.currentTarget) return;
              const r = event.currentTarget.getBoundingClientRect();
              if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) setMaximized(false);
            }}>
            <header className="visualization-gallery__toolbar">
              <span aria-live="polite" title={visualizationName(current)}>{visualizationName(current)}</span>
              <nav aria-label="可视化操作">
                {items.length > 1 && <>
                  <button aria-label="上一张可视化" disabled={index === 0} onClick={() => select(index - 1)}><ChevronLeft size={16} /></button>
                  <span className="visualization-gallery__counter">{index + 1} / {items.length}</span>
                  <button aria-label="下一张可视化" disabled={index === items.length - 1} onClick={() => select(index + 1)}><ChevronRight size={16} /></button>
                </>}
                {maximized ? <button ref={shrinkRef} aria-label="退出放大" title="退出放大" onClick={() => setMaximized(false)}><Minimize2 size={16} /></button>
                  : <button ref={enlargeRef} aria-label="放大可视化" title="放大可视化" onClick={() => setMaximized(true)}><Maximize2 size={16} /></button>}
              </nav>
            </header>
            <div className="visualization-gallery__stage">
              {items.filter(item => resident.includes(item.ordinal)).map(item => <GalleryPage
                key={`${item.ordinal}:${item.visualizationRef ?? item.state}`} entryId={entryId} item={item} onRead={onRead} active={item.ordinal === ordinal} />)}
            </div>
            {items.length > 1 && <div ref={stripRef} className="visualization-gallery__strip" role="group" aria-label="可视化缩略图"
              onKeyDown={event => {
                const button = (event.target as HTMLElement).closest<HTMLButtonElement>('button[data-ordinal]');
                if (!button) return;
                const at = items.findIndex(item => item.ordinal === Number(button.dataset.ordinal));
                const next = event.key === 'ArrowRight' ? Math.min(at + 1, items.length - 1)
                  : event.key === 'ArrowLeft' ? Math.max(0, at - 1) : event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : null;
                if (next === null) return;
                event.preventDefault(); select(next);
                (stripRef.current?.children[next] as HTMLElement)?.focus({ preventScroll: true });
              }}>
              {items.map((item, at) => <button key={`${item.ordinal}:${item.visualizationRef ?? item.state}`} data-ordinal={item.ordinal}
                aria-label={`查看可视化 ${item.ordinal + 1}${item.state === 'FAILED' ? '（无法显示）' : ''}`}
                title={visualizationName(item)}
                aria-pressed={item.ordinal === ordinal} onClick={() => select(at)}>
                <Thumbnail entryId={entryId} item={item} enabled={expanded && visible.has(item.ordinal)} onRead={onReadThumbnail} />
                <span>{visualizationName(item)}</span>
              </button>)}
            </div>}
          </dialog>
        </div>
      </AnimatedDisclosure>
    </div>
  </section>;
}
