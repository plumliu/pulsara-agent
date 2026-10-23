import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Download } from 'lucide-react';
import Lightbox from 'yet-another-react-lightbox';
import Zoom from 'yet-another-react-lightbox/plugins/zoom';
import 'yet-another-react-lightbox/styles.css';
import type { SavedImageData } from '../lib/runtime-adapter';

export type ReadSavedImage = (signal: AbortSignal) => Promise<SavedImageData>;

export function useSavedImage(read: ReadSavedImage, enabled: boolean) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ url?: string; width?: number; height?: number; error?: string }>({});
  const [request, setRequest] = useState({read, enabled, attempt});
  if (request.read !== read || request.enabled !== enabled || request.attempt !== attempt) {
    setRequest({read, enabled, attempt});
    setState({});
  }
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let url: string | undefined;
    void read(controller.signal).then(image => {
      if (controller.signal.aborted) return;
      url = URL.createObjectURL(new Blob([new Uint8Array(image.bytes)], { type: image.mediaType }));
      setState({ url, width: image.width, height: image.height });
    }).catch(error => {
      if (!controller.signal.aborted) setState({ error: error instanceof Error ? error.message : '图片暂时无法读取。' });
    });
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [read, enabled, attempt]);
  return { ...state, retry: () => setAttempt(value => value + 1) };
}

export function SavedImage({ read, title, inline = false, children, loading }: { read: ReadSavedImage; title: string; inline?: boolean; children?: ReactNode; loading?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const state = useSavedImage(read, inline || open);
  const opener = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  return <span className={inline ? 'saved-image' : 'saved-image-reference'}>
    <button ref={opener} type="button" className={inline ? 'saved-image__open' : 'file-reference__label markdown-image-reference'} onClick={() => setOpen(true)} aria-label={`查看图片：${title}`}>
      {/* Saved immutable blob URLs must not go through an image optimizer. */}
      {inline && state.url ? <img src={state.url} alt={title} width={state.width} height={state.height} /> : children ?? title}
    </button>
    {inline && !state.url && (state.error ? <span role="status">{state.error}<button onClick={state.retry}>重试</button></span> : loading ?? <span role="status">正在加载图片…</span>)}
    <Lightbox open={open} close={close} plugins={[Zoom]}
      portal={{ root: () => opener.current?.closest('dialog:modal') ?? document.body }}
      slides={state.url ? [{ src: state.url, width: state.width, height: state.height, alt: title, imageFit: 'contain' }] : [{ src: '', alt: title }]}
      controller={{ closeOnBackdropClick: true }} on={{ exited: () => opener.current?.focus() }}
      toolbar={{ buttons: [state.url ? <a key="download" className="yarl__button" href={state.url} download={title} aria-label="下载图片" title="下载图片"><Download size={32} aria-hidden="true" /></a> : null, 'close'] }}
      render={{ buttonPrev: () => null, buttonNext: () => null,
        slide: () => state.url ? undefined : <div role="status">{state.error ?? '正在读取图片…'}{state.error && <button onClick={state.retry}>重试</button>}</div> }} />
  </span>;
}
