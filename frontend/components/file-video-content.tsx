import { releaseMedia, pauseMediaWhenHidden } from '../lib/video-media';
import { useCallback, useEffect, useEffectEvent, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Check, ChevronRight, ExternalLink, Film, Maximize2, Minimize2, MessageSquareText, Pause, Play, RotateCcw, RotateCw, Sparkles, Volume2, VolumeX } from 'lucide-react';
import type { MarkdownNotify } from './markdown-body';
import '../app/styles/video.css';

export interface VideoSpan { from: number; to: number }
export interface VideoAnnotation extends VideoSpan { resultId: string; text: string; openSource?: () => void }
export interface VideoPlaybackState { time: number; volume: number; muted: boolean; rate: number }
const emptyAnnotations: readonly VideoAnnotation[] = [];
const clock = (seconds: number) => {
  const value = Math.floor(Math.max(0, seconds));
  const base = `${String(Math.floor(value / 60) % 60).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
  return value >= 3600 ? `${Math.floor(value / 3600)}:${base}` : base;
};
const spanText = ({ from, to }: VideoSpan) => `${clock(from)}–${clock(to)}`;
const playbackState = (video: HTMLMediaElement): VideoPlaybackState => ({ time: video.currentTime, volume: video.volume, muted: video.muted, rate: video.playbackRate });

/** Interval partitioning, with the earliest-ending lane at the heap root.
 * Geometry is rebuilt only when loaded annotations change, never per play tick. */
export function placeVideoAnnotations(annotations: readonly VideoAnnotation[]) {
  type Lane = {end: number; lane: number};
  const active: Lane[] = [], available: Lane[] = [];
  const push = (heap: Lane[], value: Lane) => {
    heap.push(value);
    for (let child = heap.length - 1; child > 0; ) {
      const parent = Math.floor((child - 1) / 2);
      if (heap[parent].end <= heap[child].end) break;
      [heap[parent], heap[child]] = [heap[child], heap[parent]]; child = parent;
    }
  };
  const pop = (heap: Lane[]) => {
    const first = heap[0], tail = heap.pop()!;
    if (heap.length) {
      heap[0] = tail;
      for (let parent = 0; ;) {
        let child = parent * 2 + 1;
        if (child >= heap.length) break;
        if (child + 1 < heap.length && heap[child + 1].end < heap[child].end) child++;
        if (heap[parent].end <= heap[child].end) break;
        [heap[parent], heap[child]] = [heap[child], heap[parent]]; parent = child;
      }
    }
    return first;
  };
  let laneCount = 0;
  return annotations.map((note, index) => ({note, ordinal:index + 1})).sort((a,b) => a.note.from - b.note.from).map(item => {
    while (active.length && active[0].end <= item.note.from) {
      const {lane} = pop(active); push(available, {end:lane,lane});
    }
    const lane = available.length ? pop(available).lane : laneCount++;
    push(active, {end:item.note.to,lane});
    return {...item,lane};
  });
}

/** The file preview owner binds src and keys this component to its read lease.
 * Optional analysis is presentation data only; it never starts or restores work. */
export function FileVideoContent({ src, name, kind = 'video', poster, initialState, previewPosition, onPreviewPositioned, onPlaybackStateChange, onNotify, onRetry, activeSpan = null, annotations = emptyAnnotations }: {
  src: string;
  name: string;
  kind?: 'video' | 'audio';
  poster?: string;
  initialState?: Partial<VideoPlaybackState>;
  previewPosition?: {time: number};
  onPreviewPositioned?: () => void;
  onPlaybackStateChange?: (state: VideoPlaybackState) => void;
  onNotify: MarkdownNotify;
  onRetry: () => void;
  activeSpan?: VideoSpan | null;
  annotations?: readonly VideoAnnotation[];
}) {
  const media = useRef<HTMLMediaElement>(null);
  const audio = kind === 'audio';
  const Media = audio ? 'audio' : 'video';
  const mediaLabel = audio ? '音频' : '视频';
  const layout = useRef<HTMLDivElement>(null);
  const splitDrag = useRef<{ axis: 'x' | 'y'; point: number; size: number } | null>(null);
  const cards = useRef(new Map<string, HTMLButtonElement>());
  const markers = useRef(new Map<string, HTMLButtonElement>());
  const [duration, setDuration] = useState(0);
  const [time, setTime] = useState(initialState?.time ?? 0);
  const [requested, setRequested] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [muted, setMuted] = useState(initialState?.muted ?? false);
  const [volume, setVolume] = useState(initialState?.volume ?? 1);
  const [rate, setRate] = useState(initialState?.rate ?? 1);
  const [error, setError] = useState('');
  const [ready, setReady] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const hasNotes = annotations.length > 0;
  const [selected, setSelected] = useState<string | null>(null);
  const [buffered, setBuffered] = useState<VideoSpan[]>([]);
  const [aspect, setAspect] = useState(16 / 9);
  const [space, setSpace] = useState({ height: 0, width: 0, controls: 0, timeline: 0 });
  const [videoRatio, setVideoRatio] = useState<number | null>(null);
  const [preferredNoteWidth, setPreferredNoteWidth] = useState<number | null>(null);
  useLayoutEffect(() => {
    const element = layout.current;
    if (!element) return;
    const controls = element.querySelector<HTMLElement>('.video-player-controls')!;
    const timeline = element.querySelector<HTMLElement>('.video-timeline-section')!;
    const measure = () => {
      const next = { height: element.clientHeight, width: element.clientWidth,
        controls: controls.getBoundingClientRect().height, timeline: timeline.getBoundingClientRect().height };
      setSpace(previous => Object.keys(next).every(key => previous[key as keyof typeof next] === next[key as keyof typeof next]) ? previous : next);
    };
    const observer = new ResizeObserver(measure);
    [element, controls, timeline].forEach(node => observer.observe(node));
    measure();
    return () => observer.disconnect();
  }, []);
  // Layout depends only on outer geometry, the source aspect ratio and fixed
  // controls. Moving notes never remounts the media or feeds back into this fit.
  const dividerSize = 13;
  const playerInset = 32;
  const verticalRoom = Math.max(0, space.height - space.controls - space.timeline);
  const stackedRoom = Math.max(0, verticalRoom - (hasNotes ? dividerSize : 0));
  const minVideo = Math.min(120, stackedRoom * .4);
  // The stacked view needs only one compact note and its list padding, rather
  // than a fixed reading area that keeps the picture short as width increases.
  const maxVideo = hasNotes ? Math.max(minVideo, stackedRoom - Math.min(96, stackedRoom * .6)) : stackedRoom;
  const clampVideo = (value: number) => Math.max(minVideo, Math.min(maxVideo, value));
  const stackedHeight = audio ? 0 : stackedRoom ? clampVideo(videoRatio === null
    ? Math.max(0, space.width - playerInset) / aspect
    : stackedRoom * videoRatio) : 120;
  // 300px fits a readable note column; 360px fits the timeline and controls.
  // Only dock when the actual picture can keep its previous displayed size.
  const previousPictureWidth = Math.min(Math.max(0, space.width - playerInset), stackedHeight * aspect);
  const requiredViewerWidth = Math.max(360, previousPictureWidth + playerInset);
  const sideBySide = !audio && hasNotes && space.width >= requiredViewerWidth + dividerSize + 300;
  const minViewer = requiredViewerWidth;
  const maxViewer = Math.max(minViewer, space.width - dividerSize - 264);
  const noteWidth = Math.max(264, Math.min(space.width - dividerSize - minViewer, preferredNoteWidth ?? 300));
  const viewerWidth = space.width - dividerSize - noteWidth;
  const videoHeight = audio ? 0 : sideBySide ? Math.max(0, Math.min((viewerWidth - playerInset) / aspect, verticalRoom)) : stackedHeight;
  const splitSize = sideBySide ? viewerWidth : videoHeight;
  const splitMin = sideBySide ? minViewer : minVideo;
  const splitMax = sideBySide ? maxViewer : maxVideo;
  const resizeSplit = (value: number) => {
    if (sideBySide) setPreferredNoteWidth(space.width - dividerSize - Math.max(minViewer, Math.min(maxViewer, value)));
    else if (stackedRoom) setVideoRatio(clampVideo(value) / stackedRoom);
  };
  const saveOnUnmount = useEffectEvent((video: HTMLMediaElement) => {
    if (ready) onPlaybackStateChange?.(playbackState(video));
  });
  useEffect(() => {
    const video = media.current;
    const root = layout.current;
    if (video && !video.getAttribute('src')) video.src = src;
    const updateFullscreen = () => setFullscreen(document.fullscreenElement === root);
    document.addEventListener('fullscreenchange', updateFullscreen);
    const stopVisibility = video ? pauseMediaWhenHidden(video, root!) : undefined;
    return () => {
      document.removeEventListener('fullscreenchange', updateFullscreen);
      stopVisibility?.();
      if (video) {
        saveOnUnmount(video);
        releaseMedia(video);
      }
    };
  }, [src, kind]);
  const reportPosition = useEffectEvent(() => onPreviewPositioned?.());
  useEffect(() => {
    const video = media.current;
    if (!video || !previewPosition) return;
    video.pause();
    if (ready && Number.isFinite(video.duration)) {
      video.currentTime = Math.max(0, Math.min(video.duration, previewPosition.time));
      reportPosition();
    }
  }, [previewPosition, ready, duration]);
  const syncTime = () => { if (media.current) { setTime(media.current.currentTime); if (ready) onPlaybackStateChange?.(playbackState(media.current)); } };
  const seek = useCallback((value: number) => {
    const video = media.current;
    if (!video || !duration || error) return;
    const next = Math.max(0, Math.min(duration, value));
    if (Math.abs(video.currentTime - next) < .001) return;
    setRequested(next);
    video.currentTime = next;
  }, [duration, error]);
  const togglePlay = async () => {
    const video = media.current;
    if (!video || error) return;
    if (!video.paused) video.pause();
    else try { await video.play(); } catch (reason) {
      if (video.isConnected && !(reason instanceof DOMException && reason.name === 'AbortError')) {
        onNotify(`无法播放${mediaLabel}`, reason instanceof Error ? reason.message : '请重试。', 'warning');
      }
    }
  };
  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement === layout.current) await document.exitFullscreen();
      else await layout.current?.requestFullscreen();
    } catch (reason) { onNotify('无法进入全屏', reason instanceof Error ? reason.message : '请重试。', 'warning'); }
  };
  const showActive = activeSpan !== null && duration > 0;
  const showNoteTrack = hasNotes && duration > 0;
  // Lane placement is disposable geometry; note times and identities stay intact.
  const placed = useMemo(() => placeVideoAnnotations(annotations), [annotations]);
  const fraction = useCallback((value: number) => duration ? `${Math.min(100, Math.max(0, value / duration * 100))}%` : '0%', [duration]);
  const geometry = useCallback(({ from, to }: VideoSpan): CSSProperties => ({ left: fraction(from), width: fraction(to - from) }), [fraction]);
  const selectNote = useCallback((note: VideoAnnotation) => {
    setSelected(note.resultId);
    if (note.from >= 0 && note.to <= duration) seek(note.from);
    const card = cards.current.get(note.resultId);
    const list = card?.closest<HTMLElement>('.video-note-list');
    const row = card?.closest<HTMLElement>('.video-note-row');
    if (row && list && (row.offsetTop < list.scrollTop || row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight)) {
      list.scrollTop = row.offsetTop;
    }
    const marker = markers.current.get(note.resultId);
    const track = marker?.parentElement;
    if (marker && track && (marker.offsetTop < track.scrollTop || marker.offsetTop + marker.offsetHeight > track.scrollTop + track.clientHeight)) track.scrollTop = marker.offsetTop;
  }, [duration, seek]);
  const noteMarkers = useMemo(() => placed.map(({ note, lane, ordinal }) => <button key={note.resultId} ref={node => { if (node) markers.current.set(note.resultId, node); else markers.current.delete(note.resultId); }} className={`video-note-range${selected === note.resultId ? ' is-selected' : ''}`} style={{ ...geometry(note), gridRow: lane + 1 }} aria-label={`批注 ${ordinal}，${spanText(note)}，${note.text}`} aria-pressed={selected === note.resultId} title={`${spanText(note)} ${note.text}`} onClick={() => selectNote(note)}><span>{ordinal}</span></button>), [placed, selected, geometry, selectNote]);
  const noteCards = useMemo(() => annotations.map((note, index) => <div key={note.resultId} className="video-note-row"><button ref={node => { if (node) cards.current.set(note.resultId, node); else cards.current.delete(note.resultId); }} className={`video-note-card${selected === note.resultId ? ' is-selected' : ''}`} aria-label={`查看批注 ${index + 1}，${spanText(note)}，${note.text}`} aria-pressed={selected === note.resultId} onClick={() => selectNote(note)}><span className="video-note-number">{index + 1}</span><span className="video-note-body"><span className="video-note-meta"><b>{spanText(note)}</b>{selected === note.resultId && <Check size={13} />}</span><span>{note.text}</span></span><ChevronRight size={15} /></button>{selected === note.resultId && note.openSource && <button className="video-note-source" aria-label="定位来源调用" title="定位来源调用" onClick={note.openSource}><ExternalLink size={15}/></button>}</div>), [annotations, selected, selectNote]);
  return <div ref={layout} className={`video-layout${sideBySide ? ' is-side-by-side' : ''}${!hasNotes ? ' is-plain' : ''}${audio ? ' is-audio' : ''}`} aria-label={`${mediaLabel}：${name}`} onKeyDown={event => { if (event.key === 'Escape') setSelected(null); }} style={{ '--video-note-track-height': `${Math.max(32, space.height * .2)}px`, ...(!audio ? { '--video-frame-height': `${videoHeight}px`, '--video-note-width': `${noteWidth}px` } : {}) } as CSSProperties}>
    <div className="video-viewer">
    <div className={audio ? "audio-player-source" : "video-player-viewport"}>
      <Media ref={node => { media.current = node; }} hidden={audio} src={src} poster={poster} preload="metadata" playsInline muted={muted} onClick={() => void togglePlay()}
        onLoadedMetadata={event => {
          const video = event.currentTarget;
          setReady(true);
          if (video instanceof HTMLVideoElement && video.videoWidth > 0 && video.videoHeight > 0) setAspect(video.videoWidth / video.videoHeight);
          if (Number.isFinite(video.duration) && video.duration > 0) setDuration(video.duration);
          video.volume = volume; video.playbackRate = rate;
          const target = initialState?.time ?? 0;
          if (target > 0 && Number.isFinite(video.duration) && video.duration > 0) { setRequested(Math.min(target, video.duration)); video.currentTime = Math.min(target, video.duration); }
        }} onDurationChange={event => { const value = event.currentTarget.duration; if (Number.isFinite(value) && value > 0) setDuration(value); }}
        onTimeUpdate={syncTime} onSeeking={() => setWaiting(true)} onSeeked={() => { syncTime(); setRequested(null); setWaiting(false); }}
        onVolumeChange={event => { setVolume(event.currentTarget.volume); setMuted(event.currentTarget.muted); if (ready) onPlaybackStateChange?.(playbackState(event.currentTarget)); }}
        onRateChange={event => { setRate(event.currentTarget.playbackRate); if (ready) onPlaybackStateChange?.(playbackState(event.currentTarget)); }}
        onPlay={() => setPlaying(true)} onPause={() => { setPlaying(false); syncTime(); }} onEnded={() => setPlaying(false)} onWaiting={() => setWaiting(true)} onPlaying={() => setWaiting(false)} onCanPlay={() => setWaiting(false)}
        onProgress={event => { const ranges = event.currentTarget.buffered; setBuffered(Array.from({ length: ranges.length }, (_, index) => ({ from: ranges.start(index), to: ranges.end(index) }))); }}
        onError={event => setError(event.currentTarget.error?.code === 4 ? `当前播放引擎不支持这个${mediaLabel}的容器或编码。` : `${mediaLabel}未能加载，请刷新文件后重试。`)} />
      {!audio && !playing && !error && <button className="video-play-overlay" aria-label="播放视频" onClick={() => void togglePlay()}><Play size={24} fill="currentColor" /></button>}
      {error && <div className="video-media-error" role="alert"><Film size={25} /><p>{error}</p><button className="video-button" onClick={onRetry}>重试</button></div>}
      {waiting && !error && <span className="video-loading" role="status">{requested === null ? '缓冲中' : `定位到 ${clock(requested)}`}</span>}
    </div>
    <div className="video-player-controls">
      <button className="video-icon-button" disabled={!duration || !!error} aria-label="后退 5 秒" title="后退 5 秒" onClick={() => seek((media.current?.currentTime ?? time) - 5)}><RotateCcw size={17} /></button>
      <button className="video-icon-button video-primary-play" disabled={!ready || !!error} aria-label={`${playing ? '暂停' : '播放'}${mediaLabel}`} onClick={() => void togglePlay()}>{playing ? <Pause size={17} fill="currentColor" /> : <Play size={17} fill="currentColor" />}</button>
      <button className="video-icon-button" disabled={!duration || !!error} aria-label="前进 5 秒" title="前进 5 秒" onClick={() => seek((media.current?.currentTime ?? time) + 5)}><RotateCw size={17} /></button>
      <span className="video-time"><strong>{clock(time)}</strong><span>/</span>{duration ? clock(duration) : '--:--'}</span>
      <div className="video-volume"><button className="video-icon-button" aria-label={muted ? '取消静音' : '静音'} onClick={() => setMuted(value => !value)}>{muted || volume === 0 ? <VolumeX size={16} /> : <Volume2 size={16} />}</button><input aria-label="音量" type="range" min="0" max="1" step=".05" value={muted ? 0 : volume} onChange={event => { const value = Number(event.target.value); setVolume(value); setMuted(value === 0); if (media.current) media.current.volume = value; }} /></div>
      <select aria-label="播放速度" value={rate} onChange={event => { const value = Number(event.target.value); setRate(value); if (media.current) media.current.playbackRate = value; }}>{[.5, 1, 1.5, 2].map(value => <option key={value} value={value}>{value}×</option>)}</select>
      {!audio && <button className="video-icon-button" aria-label={fullscreen ? "退出全屏视频" : "全屏视频"} onClick={() => void toggleFullscreen()}>{fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}</button>}
    </div>
    <section className="video-timeline-section" aria-label={`Pulsara ${mediaLabel}时间轴`} onClick={event => {
      if (event.target instanceof Element && !event.target.closest('button, input, .video-track-label')) setSelected(null);
    }} onKeyDown={event => { if (event.key === 'Escape') setSelected(null); }}>
      <div className="video-timeline-grid" style={{ gridTemplateRows: `24px ${showActive ? '28px ' : ''}${showNoteTrack ? 'minmax(32px, auto) ' : ''}22px` }}>
        <div className="video-track-label video-preview-label"><span className="video-playhead-key" />预览</div>
        <div className="video-seek-track">
          <div className="video-seek-rail">{buffered.map((range, index) => <i key={index} style={geometry(range)} />)}<b style={{ width: fraction(time) }} /></div>
          <input aria-label={`${mediaLabel}预览位置`} aria-valuetext={`${clock(time)}，总时长 ${duration ? clock(duration) : '未知'}${requested !== null ? '，正在定位' : ''}`} type="range" min="0" max={duration || 1} step=".1" value={requested ?? time} disabled={!duration || !!error} onChange={event => seek(Number(event.target.value))} />
        </div>
        {showActive && <><div className="video-track-label"><Sparkles size={13} />模型</div>
        <div className="video-reading-track"><button className="video-reading-range" style={geometry(activeSpan!)} onClick={() => seek(activeSpan!.from)} aria-label={`定位用户预览到模型读取范围 ${spanText(activeSpan!)}`}><span>{spanText(activeSpan!)}</span></button></div></>}
        {showNoteTrack && <><div className="video-track-label video-annotation-label"><MessageSquareText size={13} />批注</div>
        <div className="video-note-track">{noteMarkers}</div></>}
        <div className="video-tick-spacer" /><div className="video-ticks">{[0, .25, .5, .75, 1].map(value => <span key={value} style={{ left: `${value * 100}%` }}>{duration ? clock(value * duration) : '--:--'}</span>)}</div>
        {duration > 0 && <div className="video-playhead-layer" aria-hidden="true"><i style={{ left: fraction(requested ?? time) }} /></div>}
      </div>
    </section>
    </div>
    {hasNotes && <>{!audio && <div className="video-height-divider" role="separator" tabIndex={0}
      aria-label={sideBySide ? '调整视频与批注区域宽度' : '调整视频与批注区域高度'} aria-orientation={sideBySide ? 'vertical' : 'horizontal'}
      aria-valuemin={Math.round(splitMin)} aria-valuemax={Math.round(splitMax)} aria-valuenow={Math.round(splitSize)} aria-valuetext={`视频${sideBySide ? '宽度' : '高度'} ${Math.round(splitSize)} 像素`}
      title={`${sideBySide ? '左右' : '上下'}拖动调整视频与批注；双击恢复默认`}
      onPointerDown={event => { if (event.button !== 0) return; event.preventDefault(); splitDrag.current = { axis: sideBySide ? 'x' : 'y', point: sideBySide ? event.clientX : event.clientY, size: splitSize }; event.currentTarget.setPointerCapture(event.pointerId); }}
      onPointerMove={event => { const drag = splitDrag.current; if (drag && drag.axis === (sideBySide ? 'x' : 'y')) resizeSplit(drag.size + (sideBySide ? event.clientX : event.clientY) - drag.point); }}
      onPointerUp={() => { splitDrag.current = null; }} onPointerCancel={() => { splitDrag.current = null; }} onLostPointerCapture={() => { splitDrag.current = null; }}
      onDoubleClick={() => { if (sideBySide) setPreferredNoteWidth(null); else setVideoRatio(null); }}
      onKeyDown={event => {
        const decrease = sideBySide ? 'ArrowLeft' : 'ArrowUp';
        const increase = sideBySide ? 'ArrowRight' : 'ArrowDown';
        if (![decrease, increase, 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        resizeSplit(event.key === 'Home' ? splitMin : event.key === 'End' ? splitMax : splitSize + (event.key === decrease ? -24 : 24));
      }}><span /></div>}
    <section className="video-notes" aria-label="时间段批注列表">
      <div className="video-note-list">{noteCards}</div>
    </section></>}
    </div>;
}
