'use client';

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react';
import Lightbox from 'yet-another-react-lightbox';
import Zoom from 'yet-another-react-lightbox/plugins/zoom';
import type {
  CanonicalPromptImagePart,
  DisplayPromptContent,
  LocalPromptImagePart,
} from '../lib/prompt-content';
import { promptImageCount } from '../lib/prompt-content';

interface PromptContentViewProps {
  content: DisplayPromptContent;
  variant: 'message' | 'queue';
  onReadImage: (image: CanonicalPromptImagePart) => Promise<Uint8Array>;
}

type ImagePart = CanonicalPromptImagePart | LocalPromptImagePart;
type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; url: string }
  | { kind: 'failed'; reason: string };

const transparentPixel = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';

export function PromptContentView({
  content,
  variant,
  onReadImage,
}: PromptContentViewProps) {
  const images = useMemo(
    () => content.parts.filter((part): part is ImagePart => part.type === 'image'),
    [content],
  );
  const [expanded, setExpanded] = useState(false);
  const [lightboxIndex, setLightboxIndex] = useState<number>();
  const [states, setStates] = useState<Record<number, LoadState>>({});
  const statesRef = useRef(states);
  statesRef.current = states;
  const loading = useRef(new Set<number>());
  const urls = useRef(new Set<string>());
  const returnFocus = useRef<HTMLElement | null>(null);
  const mounted = useRef(false);

  useEffect(() => {
    const ownedUrls = urls.current;
    const ownedLoading = loading.current;
    mounted.current = true;
    return () => {
      mounted.current = false;
      for (const url of ownedUrls) URL.revokeObjectURL(url);
      ownedUrls.clear();
      ownedLoading.clear();
    };
  }, []);

  const load = useCallback(async (index: number, retry = false) => {
    const image = images[index];
    if (!image || loading.current.has(index)
      || statesRef.current[index]?.kind === 'ready'
      || (!retry && statesRef.current[index]?.kind === 'failed')) return;
    loading.current.add(index);
    setStates((current) => ({ ...current, [index]: { kind: 'loading' } }));
    try {
      const blob = image.source === 'local'
        ? new Blob([copyArrayBuffer(image.bytes)], { type: image.declaredMediaType })
        : new Blob([copyArrayBuffer(await onReadImage(image))], { type: image.mediaType });
      if (!mounted.current) return;
      const url = URL.createObjectURL(blob);
      if (!mounted.current) {
        URL.revokeObjectURL(url);
        return;
      }
      urls.current.add(url);
      setStates((current) => ({ ...current, [index]: { kind: 'ready', url } }));
    } catch (error) {
      if (!mounted.current) return;
      setStates((current) => ({
        ...current,
        [index]: {
          kind: 'failed',
          reason: error instanceof Error ? error.message : '图片读取失败。',
        },
      }));
    } finally {
      loading.current.delete(index);
    }
  }, [images, onReadImage]);

  const open = useCallback((index: number, trigger: HTMLElement) => {
    returnFocus.current = trigger;
    setLightboxIndex(index);
    void load(index);
  }, [load]);

  const body = (
    <div className="prompt-content-body">
      {renderPromptBody(content, (index, event) => open(index, event.currentTarget))}
    </div>
  );

  return (
    <>
      {variant === 'message' && images.length > 0 && (
        <PromptThumbnailStrip images={images} states={states} load={load} open={open} />
      )}
      {variant === 'queue' ? (
        <div className={`queued-prompt-content${expanded ? ' is-expanded' : ''}`}>
          {body}
          <div className="queued-prompt-content__meta">
            {images.length > 0 && <span>{promptImageCount(content)} 张图片</span>}
            <button type="button" onClick={() => setExpanded((value) => !value)}>
              {expanded ? '收起' : '展开'}
            </button>
          </div>
        </div>
      ) : body}
      <Lightbox
        open={lightboxIndex !== undefined}
        close={() => setLightboxIndex(undefined)}
        index={lightboxIndex ?? 0}
        slides={images.map((_, index) => ({
          src: states[index]?.kind === 'ready' ? states[index].url : transparentPixel,
          alt: `Figure ${index + 1}`,
          ...canonicalDimensions(images[index]),
          imageFit: 'contain' as const,
        }))}
        plugins={[Zoom]}
        controller={{ closeOnPullDown: true, closeOnBackdropClick: true }}
        on={{
          view: ({ index }) => {
            setLightboxIndex(index);
            void load(index);
          },
          exited: () => returnFocus.current?.focus(),
        }}
        render={{
          slide: ({ offset }) => {
            if (offset !== 0 || lightboxIndex === undefined) return undefined;
            const state = states[lightboxIndex];
            if (state?.kind === 'ready') return undefined;
            return (
              <div className="prompt-lightbox-state" role="status">
                <strong>Figure {lightboxIndex + 1}</strong>
                {state?.kind === 'failed' ? (
                  <>
                    <span>{state.reason}</span>
                    <button type="button" onClick={() => void load(lightboxIndex, true)}>重新读取</button>
                  </>
                ) : <span>正在读取原图…</span>}
              </div>
            );
          },
          slideFooter: ({ slide }) => (
            <div className="prompt-lightbox-caption">{slide.alt}</div>
          ),
        }}
      />
    </>
  );
}

function PromptThumbnailStrip({
  images,
  states,
  load,
  open,
}: {
  images: readonly ImagePart[];
  states: Record<number, LoadState>;
  load: (index: number) => Promise<void>;
  open: (index: number, trigger: HTMLElement) => void;
}) {
  const strip = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(images.length);

  useEffect(() => {
    const element = strip.current;
    if (!element) return;
    const update = () => {
      const capacity = Math.max(1, Math.floor((element.clientWidth - 54) / 82));
      setVisible(Math.min(images.length, capacity));
    };
    update();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [images.length]);

  return (
    <div ref={strip} className="prompt-thumbnail-strip" aria-label={`${images.length} 张图片`}>
      {images.slice(0, visible).map((_, index) => (
        <LazyThumbnail
          key={index}
          index={index}
          state={states[index]}
          load={load}
          open={open}
        />
      ))}
      {visible < images.length && (
        <button
          type="button"
          className="prompt-thumbnail-more"
          onClick={(event) => open(visible, event.currentTarget)}
        >+{images.length - visible} 张</button>
      )}
    </div>
  );
}

function LazyThumbnail({
  index,
  state,
  load,
  open,
}: {
  index: number;
  state?: LoadState;
  load: (index: number) => Promise<void>;
  open: (index: number, trigger: HTMLElement) => void;
}) {
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const element = button.current;
    if (!element) return;
    if (typeof IntersectionObserver === 'undefined') {
      void load(index);
      return undefined;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        void load(index);
        observer.disconnect();
      }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [index, load]);
  return (
    <button
      ref={button}
      type="button"
      className="prompt-thumbnail"
      aria-label={`打开 Figure ${index + 1}`}
      onClick={(event) => open(index, event.currentTarget)}
    >
      <span className={`prompt-thumbnail__image is-${state?.kind ?? 'loading'}`}>
        {state?.kind === 'ready'
          // Exact owner-scoped Blob URLs cannot pass through Next image optimization.
          // eslint-disable-next-line @next/next/no-img-element
          ? <img src={state.url} alt="" />
          : state?.kind === 'failed' ? '重试' : '…'}
      </span>
      <small>Figure {index + 1}</small>
    </button>
  );
}

function renderPromptBody(
  content: DisplayPromptContent,
  open: (index: number, event: ReactMouseEvent<HTMLButtonElement>) => void,
) {
  let imageIndex = 0;
  return content.parts.map((part, partIndex) => {
    if (part.type === 'text') {
      return <span key={partIndex} className="prompt-content-text">{part.text}</span>;
    }
    const current = imageIndex;
    imageIndex += 1;
    return (
      <button
        key={partIndex}
        type="button"
        className="prompt-figure-link"
        onClick={(event) => open(current, event)}
      >[Figure {current + 1}]</button>
    );
  });
}

function canonicalDimensions(image: ImagePart): { width?: number; height?: number } {
  return image.source === 'canonical'
    ? { width: image.width, height: image.height }
    : {};
}

function copyArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const result = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(result).set(bytes);
  return result;
}
