/** Native media owns decoding; each caller releases only its own element. */
export function releaseMedia(video: HTMLMediaElement) {
  video.pause();
  video.removeAttribute('src');
  video.querySelectorAll('source').forEach(source => source.removeAttribute('src'));
  video.load();
}

export async function videoPoster(src: string, signal: AbortSignal): Promise<string> {
  const video = document.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.preload = 'metadata';
  const wait = (event: string) => new Promise<void>((resolve, reject) => {
    const clear = () => { video.removeEventListener(event, done); video.removeEventListener('error', fail); signal.removeEventListener('abort', abort); };
    const done = () => { clear(); resolve(); };
    const fail = () => { clear(); reject(new Error('视频封面暂不可用。')); };
    const abort = () => { clear(); reject(signal.reason); };
    video.addEventListener(event, done, {once:true});
    video.addEventListener('error', fail, {once:true});
    signal.addEventListener('abort', abort, {once:true});
    if (signal.aborted) abort();
  });
  try {
    signal.throwIfAborted();
    const metadata = wait('loadedmetadata');
    video.src = src;
    await metadata;
    const time = Number.isFinite(video.duration) && video.duration > 0 ? Math.min(1, video.duration / 2) : 0;
    if (time > 0) { const sought = wait('seeked'); video.currentTime = time; await sought; }
    if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) await wait('loadeddata');
    signal.throwIfAborted();
    if (!video.videoWidth || !video.videoHeight) throw new Error('视频没有可用画面。');
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, 320 / video.videoWidth, 214 / video.videoHeight);
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    const context = canvas.getContext('2d');
    if (!context) throw new Error('无法绘制视频封面。');
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/png');
  } finally { releaseMedia(video); }
}

/** Sidebar residency may outlive its visibility; hiding never resumes playback. */
export function pauseMediaWhenHidden(video: HTMLMediaElement, container: Element) {
  let visible = true;
  const hidden = () => { if (!visible || document.visibilityState === 'hidden') video.pause(); };
  const observer = typeof IntersectionObserver === 'undefined' ? undefined : new IntersectionObserver(entries => {
    const entry = entries.at(-1);
    visible = Boolean(entry?.isIntersecting && entry.intersectionRect.width > 0 && entry.intersectionRect.height > 0);
    hidden();
  }, {threshold: Number.EPSILON});
  observer?.observe(container);
  document.addEventListener('visibilitychange', hidden);
  video.addEventListener('play', hidden);
  hidden();
  return () => { observer?.disconnect(); document.removeEventListener('visibilitychange', hidden); video.removeEventListener('play', hidden); };
}
