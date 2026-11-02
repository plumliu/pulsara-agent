import { useContext, useEffect, useEffectEvent, useRef, useState, type RefObject } from 'react';
import { Film, Maximize2, Play, RotateCcw } from 'lucide-react';
import type { VisualizationOccurrence } from '../lib/pulsara-types';
import type { FilePreview } from '../lib/file-preview';
import { fileUrl } from '../lib/file-preview';
import { releaseVideo, videoPoster } from '../lib/video-media';
import { readThumbnailInOrder } from '../lib/visualization-thumbnails';
import { FileMediaContext } from './file-link-context';
import type { ReadVisualization } from './inline-visualization';

export const videoReferenceMediaType = 'application/vnd.pulsara.video-reference+json';
export const isVideoVisualization = (item: VisualizationOccurrence) => item.mediaType === videoReferenceMediaType;

async function readPath(entryId: string, item: VisualizationOccurrence, onRead: ReadVisualization, signal: AbortSignal) {
  if (item.state !== 'READY' || !item.contentDigest || !item.contentSize) throw new Error(item.failureDetail || '视频引用不完整。');
  const value = await onRead(entryId, item.ordinal, item.contentDigest, item.contentSize, item, signal);
  signal.throwIfAborted();
  if (typeof value !== 'string') throw new Error('视频引用类型不匹配。');
  const reference: unknown = JSON.parse(value);
  if (!reference || typeof reference !== 'object' || Object.keys(reference).length !== 1 || !('path' in reference)
    || typeof reference.path !== 'string' || !reference.path.startsWith('/') || Array.from(reference.path).some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) throw new Error('视频路径无效。');
  return reference.path;
}

function useVisible(ref: RefObject<HTMLDivElement | null>, active: boolean) {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    let intersecting = false;
    const update = () => setVisible(active && intersecting && document.visibilityState !== 'hidden');
    const observer = new IntersectionObserver(entries => {
      const entry = entries.at(-1);
      intersecting = Boolean(entry?.isIntersecting && entry.intersectionRect.width > 0 && entry.intersectionRect.height > 0);
      update();
    }, {threshold: Number.EPSILON});
    if (ref.current) observer.observe(ref.current);
    document.addEventListener('visibilitychange', update);
    update();
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', update); };
  }, [ref, active]);
  return visible && active;
}

export type VideoSource = Pick<FilePreview, 'path' | 'modified_ns'>;
const sameSource = (a: VideoSource, b: VideoSource) => a.path === b.path && a.modified_ns === b.modified_ns;

type Props = {entryId: string; item: VisualizationOccurrence; onRead: ReadVisualization};

export function InlineVideoPage({entryId, item, onRead, active, onSource}: Props & {active: boolean; onSource?: (ordinal: number, source: VideoSource) => void}) {
  const context = useContext(FileMediaContext);
  const page = useRef<HTMLDivElement>(null);
  const media = useRef<HTMLVideoElement>(null);
  const visible = useVisible(page, active);
  const [started, setStarted] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{file?: FilePreview; error?: string}>({});
  const [mediaError, setMediaError] = useState(false);
  const {ordinal, state, contentDigest, contentSize, mediaType} = item;
  const reportSource = useEffectEvent((file: FilePreview) => onSource?.(ordinal, file));
  const api = context?.api;
  const ownerKey = context?.ownerKey;
  const sessionId = context?.sessionId;
  useEffect(() => { if (visible) setStarted(true); }, [visible]);
  useEffect(() => {
    if (!started) return;
    const abort = new AbortController();
    let file: FilePreview | undefined;
    setResult({}); setMediaError(false);
    void (async () => {
      if (!api) throw new Error('视频没有可用的文件连接。');
      const path = await readPath(entryId, {ordinal, state, contentDigest, contentSize, mediaType}, onRead, abort.signal);
      const opened = await api.open(fileUrl(path));
      if (abort.signal.aborted) { void api.close(opened.read_token).catch(() => undefined); return; }
      file = opened;
      if (file.kind !== 'video' || !file.content_url) throw new Error('此文件不是可预览的视频。');
      setResult({file});
      reportSource(file);
    })().catch(error => {
      if (file) { void api?.close(file.read_token).catch(() => undefined); file = undefined; }
      if (!abort.signal.aborted) setResult({error: error instanceof Error ? error.message : '视频暂不可用。'});
    });
    return () => { abort.abort(); if (file) void api?.close(file.read_token).catch(() => undefined); };
  }, [started, entryId, ordinal, state, contentDigest, contentSize, mediaType, onRead, api, ownerKey, sessionId, attempt]);
  useEffect(() => {
    const video = media.current;
    if (!video || mediaError) return;
    // StrictMode replays setup/cleanup on the same DOM node.
    if (!video.getAttribute('src') && result.file?.content_url) video.src = result.file.content_url;
    return () => releaseVideo(video);
  }, [result.file, mediaError]);
  useEffect(() => { if (!visible) media.current?.pause(); }, [visible]);
  const error = result.error || (mediaError ? '视频无法播放，文件或编码可能不受支持。' : undefined);
  return <div ref={page} className="inline-visualization__page inline-visualization__video-page" hidden={!active} inert={!active} aria-hidden={!active} data-visualization-ordinal={ordinal}>
    {error ? <div className="inline-visualization__status" role="status"><Film size={20}/><div className="inline-visualization__status-copy"><strong>视频无法显示</strong><p>{error}</p><button onClick={() => setAttempt(value => value + 1)}><RotateCcw size={14}/>重试</button></div></div>
      : result.file ? <video ref={media} controls playsInline preload="metadata" src={result.file.content_url!} aria-label={item.sourceFilename || '视频'}
        onPlay={() => {if (!visible) media.current?.pause();}} onError={() => setMediaError(true)}/>
        : <div role="status">正在加载视频…</div>}
    {active && result.file && !error && <div className="inline-visualization__zoom-anchor"><button className="inline-visualization__zoom" aria-label="扩大视频" title="在侧栏打开视频" onClick={() => {
      const video = media.current;
      video?.pause();
      const time = video && video.readyState >= HTMLMediaElement.HAVE_METADATA && Number.isFinite(video.currentTime) ? video.currentTime : undefined;
      context?.open(result.file!.path, time);
    }}><Maximize2 size={16}/></button></div>}
  </div>;
}

export function VideoThumbnail({entryId, item, onRead, enabled, source}: Props & {enabled: boolean; source?: VideoSource}) {
  const context = useContext(FileMediaContext);
  const [image, setImage] = useState<VideoSource & {url: string}>();
  const [failed, setFailed] = useState(false);
  const {ordinal, state, contentDigest, contentSize, mediaType} = item;
  const api = context?.api;
  const ownerKey = context?.ownerKey;
  const sessionId = context?.sessionId;
  const currentImage = image && (!source || sameSource(image, source)) ? image.url : undefined;
  useEffect(() => { setFailed(false); }, [source?.path, source?.modified_ns]);
  useEffect(() => {
    if (!enabled || currentImage || failed || !api) return;
    const abort = new AbortController();
    void readThumbnailInOrder(async () => {
      // Same per-operation deadline as HTML thumbnails; no gallery-size bound.
      const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(30_000)]);
      const path = await readPath(entryId, {ordinal, state, contentDigest, contentSize, mediaType}, onRead, signal);
      // A canceled slow open must not hold the shared queue. Its late lease
      // still belongs to this operation and is released when it arrives.
      const work = (async () => {
        const file = await api.open(fileUrl(path));
        try {
          signal.throwIfAborted();
          if (file.kind !== 'video' || !file.content_url) throw new Error('视频封面暂不可用。');
          return {url: await videoPoster(file.content_url, signal), path: file.path, modified_ns: file.modified_ns};
        } finally { void api.close(file.read_token).catch(() => undefined); }
      })();
      let cancel!: () => void;
      const canceled = new Promise<never>((_resolve, reject) => {
        cancel = () => reject(signal.reason);
        signal.addEventListener('abort', cancel, {once:true});
        if (signal.aborted) cancel();
      });
      try { return await Promise.race([work, canceled]); }
      finally { signal.removeEventListener('abort', cancel); }
    }, abort.signal).then(value => {if (!abort.signal.aborted) setImage(value);}).catch(() => {if (!abort.signal.aborted) setFailed(true);});
    return () => abort.abort();
  }, [enabled, currentImage, failed, api, ownerKey, sessionId, entryId, ordinal, state, contentDigest, contentSize, mediaType, onRead]);
  return <span className="inline-visualization__thumbnail-image inline-visualization__video-thumbnail" aria-hidden="true">
    {currentImage ? <img src={currentImage} alt="" draggable={false}/> : <Film size={20}/>}
    <span className="inline-visualization__video-play"><Play size={14} fill="currentColor"/></span>
  </span>;
}
