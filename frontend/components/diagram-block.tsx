'use client';

import { Code2, Copy, Image as ImageIcon, Maximize2, RotateCcw, Workflow } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import Lightbox from 'yet-another-react-lightbox';
import Zoom from 'yet-another-react-lightbox/plugins/zoom';
import type { MarkdownNotify } from './markdown-body';

type DiagramImage = { svg: string; width?: number; height?: number };
type Renderer = (source: string, signal: AbortSignal) => Promise<DiagramImage>;
type Preview = { source: string; renderer: Renderer; attempt: number; signal: AbortSignal } & (
  | { kind: 'ready'; url: string; width?: number; height?: number }
  | { kind: 'failed' }
);

export function DiagramBlock({ source, streaming, onNotify, format, renderImage }: {
  source: string;
  streaming: boolean;
  onNotify: MarkdownNotify;
  format: 'Mermaid' | 'SVG';
  renderImage: Renderer;
}) {
  const [preview, setPreview] = useState<Preview>();
  const [attempt, setAttempt] = useState(0);
  const [showSource, setShowSource] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const expandButton = useRef<HTMLButtonElement>(null);
  const current = preview?.source === source && preview.renderer === renderImage && preview.attempt === attempt && !preview.signal.aborted ? preview : undefined;
  const ready = !streaming && current?.kind === 'ready' ? current : undefined;
  const failed = !streaming && current?.kind === 'failed';
  const Icon = format === 'Mermaid' ? Workflow : ImageIcon;

  useEffect(() => {
    if (streaming) return;
    const controller = new AbortController();
    let url: string | undefined;
    void renderImage(source, controller.signal).then(image => {
      if (controller.signal.aborted) return;
      url = URL.createObjectURL(new Blob([image.svg], { type: 'image/svg+xml' }));
      setPreview({ source, renderer: renderImage, attempt, signal: controller.signal, kind: 'ready', url, width: image.width, height: image.height });
    }).catch(() => {
      if (!controller.signal.aborted) setPreview({ source, renderer: renderImage, attempt, signal: controller.signal, kind: 'failed' });
    });
    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [source, renderImage, streaming, attempt]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(source);
      onNotify(`${format} 源码已复制`, undefined, 'success');
    } catch {
      onNotify('无法复制源码', '浏览器没有授予剪贴板权限。', 'warning');
    }
  };

  return <section className={`mermaid-block${format === 'SVG' ? ' svg-block' : ''}`} aria-label={`${format} 图表`}>
    <div className="mermaid-block__toolbar" data-source-decoration="">
      <span><Icon size={13} /> {format}</span>
      <div>
        <button type="button" aria-label={showSource ? '显示图表' : '查看源码'} aria-pressed={showSource}
          onClick={() => setShowSource(value => !value)}>
          {showSource ? <Icon size={13} /> : <Code2 size={13} />}{showSource ? '图表' : '源码'}
        </button>
        <button type="button" aria-label={`复制 ${format} 源码`} title="复制源码" onClick={() => void copy()}><Copy size={13} /></button>
        <button ref={expandButton} type="button" aria-label="放大图表" title="放大图表" disabled={!ready} onClick={() => setExpanded(true)}><Maximize2 size={13} /></button>
      </div>
    </div>
    {(streaming || !current) && <div className="mermaid-block__status" data-source-decoration="" role="status">{streaming ? '图表生成中…' : '正在绘制图表…'}</div>}
    {failed && <div className="mermaid-block__status" data-source-decoration="" role="status">
      <span>暂时无法绘制，请查看源码。</span>
      <button type="button" onClick={() => setAttempt(value => value + 1)}><RotateCcw size={12} />重试</button>
    </div>}
    {showSource || !ready ? <pre><code className={`language-${format.toLowerCase()}`}>{source}</code></pre> : <div className="mermaid-block__preview">
      {/* Renderers supply sanitized SVG, displayed only as an image. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img draggable={false} src={ready.url} width={ready.width} height={ready.height} alt={`${format} 图表`}
        onError={() => setPreview(previous => previous === ready ? { ...previous, kind: 'failed' } : previous)} />
    </div>}
    <Lightbox open={expanded && Boolean(ready)} close={() => setExpanded(false)}
      slides={ready ? [{ src: ready.url, alt: `${format} 图表`, width: ready.width, height: ready.height }] : []}
      plugins={[Zoom]} carousel={{ finite: true }}
      controller={{ closeOnBackdropClick: true }}
      on={{ exited: () => expandButton.current?.focus() }}
    />
  </section>;
}
