'use client';

import { Code2, Copy, Maximize2, RotateCcw, Workflow } from 'lucide-react';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import Lightbox from 'yet-another-react-lightbox';
import Zoom from 'yet-another-react-lightbox/plugins/zoom';
import { renderMermaid, type MermaidImage } from '../lib/mermaid-renderer';
import type { MarkdownNotify } from './markdown-body';

const isDark = () => document.documentElement.dataset.theme === 'dark';
const serverTheme = () => false;
function subscribeTheme(notify: () => void) {
  const observer = new MutationObserver(notify);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  return () => observer.disconnect();
}

type Preview = { source: string; dark: boolean; attempt: number; signal: AbortSignal } & (
  | { kind: 'ready'; url: string; width?: number; height?: number }
  | { kind: 'failed' }
);

export function MermaidBlock({ source, streaming, onNotify }: {
  source: string;
  streaming: boolean;
  onNotify: MarkdownNotify;
}) {
  const dark = useSyncExternalStore(subscribeTheme, isDark, serverTheme);
  const [preview, setPreview] = useState<Preview>();
  const [attempt, setAttempt] = useState(0);
  const [showSource, setShowSource] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const expandButton = useRef<HTMLButtonElement>(null);
  const current = preview?.source === source && preview.dark === dark && preview.attempt === attempt && !preview.signal.aborted ? preview : undefined;
  const ready = !streaming && current?.kind === 'ready' ? current : undefined;
  const failed = !streaming && current?.kind === 'failed';

  useEffect(() => {
    if (streaming) return;
    const controller = new AbortController();
    let url: string | undefined;
    void renderMermaid(source, dark, controller.signal).then((image: MermaidImage) => {
      if (controller.signal.aborted) return;
      url = URL.createObjectURL(new Blob([image.svg], { type: 'image/svg+xml' }));
      setPreview({ source, dark, attempt, signal: controller.signal, kind: 'ready', url, width: image.width, height: image.height });
    }).catch(() => {
      if (!controller.signal.aborted) setPreview({ source, dark, attempt, signal: controller.signal, kind: 'failed' });
    });
    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [source, dark, streaming, attempt]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(source);
      onNotify('Mermaid 源码已复制', undefined, 'success');
    } catch {
      onNotify('无法复制源码', '浏览器没有授予剪贴板权限。', 'warning');
    }
  };

  return (
    <section className="mermaid-block" aria-label="Mermaid 图表">
      <div className="mermaid-block__toolbar">
        <span><Workflow size={13} /> Mermaid</span>
        <div>
          <button type="button" aria-label={showSource ? '显示图表' : '查看源码'} aria-pressed={showSource}
            onClick={() => setShowSource(value => !value)}>
            {showSource ? <Workflow size={13} /> : <Code2 size={13} />}{showSource ? '图表' : '源码'}
          </button>
          <button type="button" aria-label="复制 Mermaid 源码" title="复制源码" onClick={() => void copy()}><Copy size={13} /></button>
          <button ref={expandButton} type="button" aria-label="放大图表" title="放大图表" disabled={!ready} onClick={() => setExpanded(true)}><Maximize2 size={13} /></button>
        </div>
      </div>
      {(streaming || !current) && <div className="mermaid-block__status" role="status">{streaming ? '图表生成中…' : '正在绘制图表…'}</div>}
      {failed && <div className="mermaid-block__status" role="status">
        <span>暂时无法绘制，请查看源码。</span>
        <button type="button" onClick={() => setAttempt(value => value + 1)}><RotateCcw size={12} />重试</button>
      </div>}
      {showSource || !ready ? <pre><code className="language-mermaid">{source}</code></pre> : (
        <div className="mermaid-block__preview">
          {/* SVG Blob URLs are local generated images, not Next image-optimizer inputs. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={ready.url} width={ready.width} height={ready.height} alt="Mermaid 图表" />
        </div>
      )}
      <Lightbox open={expanded && Boolean(ready)} close={() => setExpanded(false)}
        slides={ready ? [{ src: ready.url, alt: 'Mermaid 图表', width: ready.width, height: ready.height }] : []}
        plugins={[Zoom]} carousel={{ finite: true }}
        controller={{ closeOnBackdropClick: true }}
        on={{ exited: () => expandButton.current?.focus() }}
      />
    </section>
  );
}
