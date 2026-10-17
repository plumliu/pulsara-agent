'use client';
import { Images, Maximize2, Minimize2, ImageOff, RotateCcw } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import type { VisualizationOccurrence } from '../lib/pulsara-types';
import { visualizationEnvironment, type VisualizationEnvironment } from '../lib/visualization-environment';
import { usableVisualizationRootRect, visualizationLayoutMessageType, type VisualizationRootRect } from '../lib/visualization-frame';
import { SavedImage, useSavedImage } from './saved-image';
import { SandboxedHtmlPreview } from './sandboxed-html-preview';

export type ReadVisualization = (entry: string, ordinal: number, digest: string, size: number, item?: VisualizationOccurrence, signal?: AbortSignal) => Promise<string | Uint8Array>;
export type ReadVisualizationThumbnail = (entry: string, ordinal: number, digest: string, size: number, signal: AbortSignal) => Promise<string>;
export interface InlineVisualizationContext { entryId: string; items: VisualizationOccurrence[]; onRead: ReadVisualization; onReadThumbnail?: ReadVisualizationThumbnail }

function GalleryPage({ entryId, item, onRead, active, maximized, zoomControl }: {entryId: string; item: VisualizationOccurrence; onRead: ReadVisualization; active: boolean; maximized: boolean; zoomControl: ReactNode}) {
  const [result, setResult] = useState<{html?: string; error?: string}>({});
  const [attempt, setAttempt] = useState(0);
  const [size, setSize] = useState({width: 0, height: 0});
  const [measurement, setMeasurement] = useState<{rect: VisualizationRootRect | null; width: number; height: number}>();
  const [environment, setEnvironment] = useState<VisualizationEnvironment>();
  const page = useRef<HTMLDivElement>(null);
  const probe = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const pending = useRef<{args: unknown[]; promise: Promise<string | Uint8Array>} | undefined>(undefined);
  const {ordinal, contentDigest: digest, contentSize, state, mediaType, codec} = item;
  useEffect(() => {
    if (state !== 'READY' || !digest || !contentSize) return;
    let live = true;
    const controller = new AbortController();
    const args = [entryId, ordinal, digest, contentSize, onRead, attempt];
    if (!pending.current || args.some((v, i) => v !== pending.current!.args[i])) pending.current = {args, promise: onRead(entryId, ordinal, digest, contentSize, {ordinal, contentDigest: digest, contentSize, state, mediaType, codec}, controller.signal)};
    void pending.current.promise.then(html => {if (live) setResult(typeof html === "string" ? {html} : {error: "可视化类型不匹配。"});}, () => {if (live) setResult({error: '已保存的可视化暂时无法读取。'});});
    return () => {live = false; controller.abort(); pending.current = undefined;};
  }, [entryId, ordinal, digest, contentSize, state, mediaType, codec, onRead, attempt]);
  useLayoutEffect(() => {
    if (!probe.current) return;
    const measure = () => {
      const width = probe.current!.clientWidth, height = probe.current!.clientHeight;
      if (width && height) setSize(old => old.width === width && old.height === height ? old : {width, height});
    };
    const observer = new ResizeObserver(measure); observer.observe(probe.current); measure();
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.data?.type !== visualizationLayoutMessageType) return;
      const viewport = event.data.viewport;
      if (!viewport || viewport.width !== size.width || viewport.height !== size.height) return;
      const rect = event.data.mode === 'root' ? usableVisualizationRootRect(event.data.rect, viewport.width) : null;
      setMeasurement({rect, ...viewport});
    };
    window.addEventListener('message', receive); return () => window.removeEventListener('message', receive);
  }, [size]);
  useEffect(() => {
    let visible = true;
    const update = () => setEnvironment(visualizationEnvironment(visible && active));
    const intersection = new IntersectionObserver(entries => {visible = Boolean(entries[0]?.isIntersecting); update();});
    if (page.current) intersection.observe(page.current);
    const mutation = new MutationObserver(update); mutation.observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme', 'class', 'style']});
    const media = matchMedia('(prefers-reduced-motion: reduce)'); media.addEventListener('change', update);
    document.addEventListener('visibilitychange', update); update();
    return () => {intersection.disconnect(); mutation.disconnect(); media.removeEventListener('change', update); document.removeEventListener('visibilitychange', update);};
  }, [active]);
  const selectedRoot = !maximized && measurement?.width === size.width && measurement?.height === size.height ? measurement.rect : null;
  const rect = selectedRoot && selectedRoot.y + selectedRoot.height <= size.height ? selectedRoot : null;
  // Enclose the measured bounds in whole CSS pixels. A fractional iframe
  // translation plus an exact-height clip can lose the bottom raster row.
  const cropTop = rect ? Math.floor(rect.y) : 0;
  const cropHeight = rect ? Math.ceil(rect.y + rect.height) - cropTop : undefined;
  // Below 8px + 30px button + 8px, omit inline zoom instead of padding the chart.
  // Maximized/page layouts have no cropped rect and always retain their control.
  const compact = Boolean(rect && rect.height < 46);
  const error = state === 'FAILED' ? item.failureDetail ?? '可视化未能生成。' : !digest || !contentSize ? '可视化引用不完整。' : result.error;
  const errorText = item.failureCode === 'HTML_NOT_FOUND' ? '未找到 HTML 文件' : error;
  return <div ref={page} className="inline-visualization__page" hidden={!active} aria-hidden={!active} inert={!active} data-visualization-ordinal={ordinal}>
      <div className="inline-visualization__viewport-probe" ref={probe}/>
      <div className="inline-visualization__stage" data-visualization-compact={compact || undefined} data-visualization-layout={rect ? 'root' : selectedRoot ? 'root-scroll' : 'page'} style={{height: error ? undefined : cropHeight ?? (size.height || 'var(--visualization-height)')}}>
        {error ? <div className="inline-visualization__status" role="status" title={error}>
            <span className="inline-visualization__status-icon" aria-hidden="true"><ImageOff size={20}/></span>
            <div className="inline-visualization__status-copy"><strong>可视化无法显示</strong><p>{errorText}</p>
              {result.error && <button onClick={() => {setResult({}); setAttempt(v => v + 1);}}><RotateCcw size={14}/>重试</button>}
            </div>
          </div>
          : result.html === undefined || !environment ? <div role="status">正在加载可视化…</div>
          : <div className="inline-visualization__crop" style={rect ? {height: cropHeight} : undefined}>
            <SandboxedHtmlPreview frameRef={frame} title={item.sourceFilename || '可视化'} source={{html: result.html, measure: true, allowTextSelection: false, environment}}
              style={{width: size.width || '100%', height: size.height || '100%', transform: rect ? `translateY(${-cropTop}px)` : undefined}}/>
          </div>}
        {active && !compact && <div className="inline-visualization__zoom-anchor">{zoomControl}</div>}
      </div>
  </div>;
}

function useVisualizationImageReader(entryId: string, item: VisualizationOccurrence, onRead: ReadVisualization) {
  const { ordinal, state, contentDigest, contentSize, mediaType, codec, imageRef, width, height } = item;
  // Runtime projections rebuild item objects even when saved content is unchanged.
  // Keep the read stable across those projections, including while bytes are pending.
  return useCallback(async (signal: AbortSignal) => {
    const expected = { ordinal, state, contentDigest, contentSize, mediaType, codec, imageRef, width, height };
    const bytes = await onRead(entryId, ordinal, contentDigest!, contentSize!, expected, signal);
    if (!(bytes instanceof Uint8Array)) throw new Error('图片内容类型不匹配。');
    return { bytes, mediaType: mediaType!, width: width!, height: height! };
  }, [entryId, ordinal, state, contentDigest, contentSize, mediaType, codec, imageRef, width, height, onRead]);
}

function ImagePage({ entryId, item, onRead }: { entryId: string; item: VisualizationOccurrence; onRead: ReadVisualization }) {
  const read = useVisualizationImageReader(entryId, item, onRead);
  return <div className="inline-visualization__page" data-visualization-ordinal={item.ordinal}>
    <SavedImage read={read} title={item.sourceFilename || '图片'} inline />
  </div>;
}

function ImageThumbnail({ entryId, item, enabled, onRead }: { entryId: string; item: VisualizationOccurrence; enabled: boolean; onRead: ReadVisualization }) {
  const read = useVisualizationImageReader(entryId, item, onRead);
  const image = useSavedImage(read, enabled);
  // Saved immutable blob URLs must not go through an image optimizer.
  // eslint-disable-next-line @next/next/no-img-element
  return <span className="inline-visualization__thumbnail-image" aria-hidden="true">{image.url ? <img src={image.url} alt="" draggable={false} /> : image.error ? <ImageOff size={19}/> : <Images size={19}/>}</span>;
}

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
  const { ordinal, contentDigest: digest, contentSize: size, state } = item;
  useEffect(() => {
    if (!enabled || image || failed || !onRead || state !== 'READY' || !digest || !size) return;
    const controller = new AbortController();
    void readThumbnailInOrder(() => onRead(entryId, ordinal, digest, size, controller.signal), controller.signal)
      .then(value => { if (!controller.signal.aborted) setImage(value); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [enabled, image, failed, onRead, entryId, ordinal, digest, size, state]);
  return <span className="inline-visualization__thumbnail-image" aria-hidden="true">
    {/* Local derived PNGs need no image proxy or external optimizer. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    {image ? <img src={image} alt="" draggable={false} onError={() => { setImage(undefined); setFailed(true); }} />
      : state === 'FAILED' || failed ? <ImageOff size={19} /> : <Images size={19} />}
  </span>;
}


export function InlineVisualization({entryId, items, onRead, onReadThumbnail}: InlineVisualizationContext) {
  const [selected, setSelected] = useState(items[0]?.ordinal);
  const [resident, setResident] = useState<number[]>(() => items.length ? [items[0].ordinal] : []);
  const [maximized, setMaximized] = useState(false);
  const [visible, setVisible] = useState<Set<number>>(new Set());
  const dialog = useRef<HTMLDialogElement>(null);
  const strip = useRef<HTMLDivElement>(null);
  const control = useRef<HTMLButtonElement>(null);
  const index = Math.max(0, items.findIndex(item => item.ordinal === selected));
  const current = items[index];
  const select = (next: number) => {
    const item = items[next];
    if (!item) return;
    setSelected(item.ordinal);
    if (item.imageRef) setMaximized(false);
    else setResident(old => [...old.filter(value => value !== item.ordinal), item.ordinal].slice(-residentFrames));
  };
  useEffect(() => {
    if (!strip.current) return;
    const observer = new IntersectionObserver(entries => setVisible(old => {
      const next = new Set(old);
      for (const entry of entries) {
        const ordinal = Number((entry.target as HTMLElement).dataset.ordinal);
        if (entry.isIntersecting) next.add(ordinal); else next.delete(ordinal);
      }
      return next;
    }), {root: strip.current});
    for (const button of strip.current.children) observer.observe(button);
    return () => observer.disconnect();
  }, [items.length]);
  useEffect(() => {
    const element = strip.current;
    const button = element?.querySelector<HTMLElement>('[aria-pressed="true"]');
    if (!element || !button) return;
    const reveal = () => {
      const left = button.offsetLeft, right = left + button.offsetWidth;
      const top = button.offsetTop, bottom = top + button.offsetHeight;
      if (left < element.scrollLeft) element.scrollLeft = left;
      else if (right > element.scrollLeft + element.clientWidth) element.scrollLeft = right - element.clientWidth;
      if (top < element.scrollTop) element.scrollTop = top;
      else if (bottom > element.scrollTop + element.clientHeight) element.scrollTop = bottom - element.clientHeight;
    };
    const observer = new ResizeObserver(reveal); observer.observe(element); reveal();
    return () => observer.disconnect();
  }, [selected]);
  useLayoutEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (maximized) {element.close(); element.showModal(); control.current?.focus({preventScroll: true});}
    else if (element.matches(':modal')) {element.close(); element.show(); control.current?.focus({preventScroll: true});}
  }, [maximized]);
  if (!current) return null;
  return <section className="inline-visualization" aria-label="可视化图集">
    <dialog ref={dialog} open className={`${maximized ? 'is-maximized' : ''}${items.length > 1 ? ' has-thumbnails' : ''}`}
      onCancel={event => {event.preventDefault(); setMaximized(false);}}
      onClick={event => {
        if (!maximized || event.target !== event.currentTarget) return;
        const r = event.currentTarget.getBoundingClientRect();
        if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) setMaximized(false);
      }}>
      <div className="inline-visualization__figure">
        {items.filter(item => !item.imageRef && resident.includes(item.ordinal)).map(item => <GalleryPage
          key={`${entryId}:${item.ordinal}:${item.contentDigest ?? item.state}`} entryId={entryId} item={item} onRead={onRead} active={item.ordinal === current.ordinal} maximized={maximized} zoomControl={
        <button ref={control} className="inline-visualization__zoom" title={visualizationName(current)}
          aria-label={maximized ? '退出放大' : '放大可视化'} onClick={() => setMaximized(v => !v)}>
          {maximized ? <Minimize2 size={16}/> : <Maximize2 size={16}/>}
        </button>
        }/>)}

        {current.imageRef && <ImagePage key={`${entryId}:${current.ordinal}:${current.contentDigest}`} entryId={entryId} item={current} onRead={onRead}/>}
      </div>
      {items.length > 1 && <aside className="inline-visualization__controls">
      <div ref={strip} className="inline-visualization__strip" role="group" aria-label="可视化缩略图"
        onKeyDown={event => {
          const button = (event.target as HTMLElement).closest<HTMLButtonElement>('button[data-ordinal]');
          if (!button) return;
          const at = items.findIndex(item => item.ordinal === Number(button.dataset.ordinal));
          const next = (event.key === 'ArrowRight' || event.key === 'ArrowDown') ? Math.min(at + 1, items.length - 1)
            : (event.key === 'ArrowLeft' || event.key === 'ArrowUp') ? Math.max(0, at - 1) : event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : null;
          if (next === null) return;
          event.preventDefault(); select(next);
          (strip.current?.children[next] as HTMLElement)?.focus({preventScroll: true});
        }}>
        {items.map((item, at) => <button key={`${item.ordinal}:${item.contentDigest ?? item.state}`} data-ordinal={item.ordinal}
          aria-label={`查看可视化 ${at + 1}${item.state === 'FAILED' ? '（无法显示）' : ''}`} title={visualizationName(item)}
          aria-pressed={item.ordinal === current.ordinal} onClick={() => select(at)}>
          <>{item.imageRef ? <ImageThumbnail entryId={entryId} item={item} enabled={visible.has(item.ordinal)} onRead={onRead}/> : <Thumbnail entryId={entryId} item={item} enabled={visible.has(item.ordinal)} onRead={onReadThumbnail}/>}</>
        </button>)}
      </div>
      </aside>}
    </dialog>
  </section>;
}
