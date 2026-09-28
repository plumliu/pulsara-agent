'use client';

import type { Element as HastElement, Parent as HastParent, Root as HastRoot } from 'hast';
import type { Root as MdastRoot } from 'mdast';
import { toText } from 'hast-util-to-text';
import { Image as ImageIcon } from 'lucide-react';
import rehypeKatex from 'rehype-katex';
import { createContext, useCallback, useContext, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent, type ReactNode } from 'react';
import ReactMarkdown, { defaultUrlTransform, type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math-extended';
import { SKIP, visitParents } from 'unist-util-visit-parents';
import { normalizeMathWithSource, rehypeSourceMapping } from '../lib/markdown-source';
import { markdownDiagramFormat } from '../lib/markdown-diagram';
import { MermaidBlock } from './mermaid-block';
import { DiagramBlock } from './diagram-block';
import { CodeBlock } from './code-block';
import { renderSvg, startsWithSvg } from '../lib/svg-renderer';
import { FileLinkContext } from './file-link-context';
import { classifyFileLink, markdownImageUrl } from '../lib/file-preview';

export type MarkdownNotify = (
  title: string,
  detail?: string,
  tone?: 'neutral' | 'success' | 'warning',
) => void;

interface MarkdownProps {
  body: string;
  onNotify: MarkdownNotify;
  streaming?: boolean;
  annotationSource?: boolean;
}

interface PointerOrigin {
  pointerId: number;
  target: HTMLElement;
  x: number;
  y: number;
  moved: boolean;
}

function elementClasses(element: HastElement): ReadonlyArray<string> {
  return Array.isArray(element.properties.className)
    ? element.properties.className.map(String)
    : [];
}

function remarkSvgBlocks() {
  return (tree: MdastRoot) => {
    visitParents(tree, 'html', (node, ancestors) => {
      const parent = ancestors.at(-1);
      if (!parent || (parent.type !== 'root' && parent.type !== 'blockquote' && parent.type !== 'listItem')) return;
      // Recognize only standalone SVG HTML blocks, never arbitrary raw HTML or
      // inline examples. XML validation and sanitizing happen in the renderer.
      if (!startsWithSvg(node.value) || !/(?:<\/svg>|^\s*<svg\b[^<>]*\/>)\s*$/.test(node.value)) return;
      const index = parent.children.indexOf(node);
      if (index >= 0) parent.children[index] = { type: 'code', lang: 'svg', value: node.value, position: node.position };
    });
  };
}

function rehypeCopyableMath() {
  return (tree: HastRoot) => {
    visitParents(tree, 'element', (element, ancestors) => {
      const classes = elementClasses(element);
      const display = classes.includes('math-display');
      if (!display && !classes.includes('math-inline')) return;

      const parent = ancestors.at(-1);
      if (!parent) return;

      let scope: HastElement = element;
      let scopeParent: HastParent | undefined = parent;
      if (display && parent.type === 'element' && parent.tagName === 'pre') {
        scope = parent;
        scopeParent = ancestors.at(-2);
      }
      if (!scopeParent) return;

      const index = scopeParent.children.indexOf(scope);
      if (index < 0) return;

      const source = toText(element, { whitespace: 'pre' }).trim();
      const wrapper: HastElement = {
        type: 'element',
        tagName: display ? 'div' : 'span',
        properties: {
          className: [
            'math-copy-target',
            display ? 'math-copy-target--display' : 'math-copy-target--inline',
          ],
          'data-math-source': source,
          ariaLabel: '复制 LaTeX 公式',
          role: 'button',
          tabIndex: 0,
        },
        position: scope.position ?? element.position,
        children: [scope],
      };
      scopeParent.children[index] = wrapper;
      return SKIP;
    });
  };
}

function findMathTarget(target: EventTarget | null): HTMLElement | null {
  return target instanceof Element
    ? target.closest<HTMLElement>('.math-copy-target')
    : null;
}

function selectionTouches(target: HTMLElement): boolean {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed) return false;
  return Boolean(
    (selection.anchorNode && target.contains(selection.anchorNode))
    || (selection.focusNode && target.contains(selection.focusNode)),
  );
}

function MarkdownCopySurface({
  children,
  inline = false,
  onNotify,
  annotationSource = false,
}: {
  children: ReactNode;
  inline?: boolean;
  onNotify: MarkdownNotify;
  annotationSource?: boolean;
}) {
  const pointerOrigin = useRef<PointerOrigin | undefined>(undefined);
  const imageOrigin = useRef<PointerOrigin | undefined>(undefined);
  const imageSelectionClick = useRef<HTMLElement | undefined>(undefined);

  const copy = useCallback(async (target: HTMLElement) => {
    const source = target.dataset.mathSource;
    if (source === undefined) return;
    try {
      await navigator.clipboard.writeText(source);
      onNotify('公式已复制', undefined, 'success');
    } catch {
      onNotify('无法复制公式', '浏览器没有授予剪贴板权限。', 'warning');
    }
  }, [onNotify]);

  const onPointerDown = useCallback((event: PointerEvent<HTMLElement>) => {
    imageSelectionClick.current = undefined;
    const image = annotationSource && event.target instanceof HTMLImageElement
      && event.target.closest('[data-source-atom]') ? event.target : undefined;
    imageOrigin.current = image && event.button === 0 ? {
      pointerId: event.pointerId, target: image, x: event.clientX, y: event.clientY, moved: false,
    } : undefined;
    const target = findMathTarget(event.target);
    pointerOrigin.current = target ? {
      pointerId: event.pointerId,
      target,
      x: event.clientX,
      y: event.clientY,
      moved: false,
    } : undefined;
  }, [annotationSource]);

  const onPointerMove = useCallback((event: PointerEvent<HTMLElement>) => {
    const image = imageOrigin.current;
    if (image && image.pointerId === event.pointerId && Math.hypot(event.clientX - image.x, event.clientY - image.y) > 5) image.moved = true;
    const origin = pointerOrigin.current;
    if (!origin || origin.pointerId !== event.pointerId || origin.moved) return;
    if (Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > 5) {
      origin.moved = true;
    }
  }, []);

  const onPointerUp = useCallback((event: PointerEvent<HTMLElement>) => {
    const origin = imageOrigin.current; imageOrigin.current = undefined;
    const selection = window.getSelection();
    // Chromium does not create a text Range for a drag confined to an image.
    // Give that whole-image gesture a native Range; cross-node selections keep
    // the browser's original endpoints and the shared strict source validator.
    if (origin?.moved && origin.pointerId === event.pointerId && event.target === origin.target && selection?.isCollapsed) {
      const range = document.createRange(); range.selectNode(origin.target);
      selection.removeAllRanges(); selection.addRange(range);
      imageSelectionClick.current = origin.target;
    }
  }, []);

  const onClick = useCallback((event: MouseEvent<HTMLElement>) => {
    const target = findMathTarget(event.target);
    if (!target) return;
    const origin = pointerOrigin.current;
    pointerOrigin.current = undefined;
    if ((origin?.target === target && origin.moved) || selectionTouches(target)) return;
    event.preventDefault();
    event.stopPropagation();
    void copy(target);
  }, [copy]);

  const onKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const target = findMathTarget(event.target);
    if (!target) return;
    event.preventDefault();
    event.stopPropagation();
    void copy(target);
  }, [copy]);

  const handlers = {
    onClickCapture: (event: MouseEvent<HTMLElement>) => {
      const image = imageSelectionClick.current; imageSelectionClick.current = undefined;
      if (image && event.target === image) { event.preventDefault(); event.stopPropagation(); }
    },
    onClick,
    onKeyDown,
    onPointerCancel: () => { pointerOrigin.current = undefined; imageOrigin.current = undefined; },
    onPointerDown,
    onPointerMove,
    onPointerUp,
  };
  return inline ? (
    <span className="markdown-copy-surface" {...handlers}>
      {children}
    </span>
  ) : (
    <div
      className="markdown-copy-surface"
      {...handlers}
    >
      {children}
    </div>
  );
}

export function normalizeMathMarkdown(value: string): string {
  return normalizeMathWithSource(value).text;
}

function markdownUrl(url: string, key: string, node: HastElement) {
  const fileAddress = (node.tagName === 'a' && key === 'href') || (node.tagName === 'img' && key === 'src');
  return fileAddress && classifyFileLink(url) === 'local' ? url : defaultUrlTransform(url);
}

const MarkdownAnchorContext = createContext(false);

function MarkdownLink({ href = '', children, onNotify }: { href?: string; children?: ReactNode; onNotify: MarkdownNotify }) {
  const context = useContext(FileLinkContext);
  const kind = classifyFileLink(href);
  const content = <MarkdownAnchorContext.Provider value={true}>{children}</MarkdownAnchorContext.Provider>;
  if (kind === 'external') return <a href={href} target="_blank" rel="noreferrer">{content}</a>;
  if (kind === 'anchor') return <a href={href}>{content}</a>;
  return <a href={kind === 'local' ? href : undefined} role="link" tabIndex={0}
    onClick={event => {
      event.preventDefault();
      if (kind === 'local' && context) context.open(href, event.currentTarget, context.basePreview);
      else onNotify('无法打开文件', kind === 'invalid' ? '无法识别这个文件地址。' : '此内容没有可用的会话文件连接。', 'warning');
    }} onKeyDown={event => { if (event.key === 'Enter' && !event.currentTarget.hasAttribute('href')) event.currentTarget.click(); }}>
    {content}
  </a>;
}

function MarkdownImage({ src, alt, onNotify, selectable }: { src?: string | Blob; alt?: string; onNotify: MarkdownNotify; selectable?: boolean }) {
  const context = useContext(FileLinkContext);
  const linked = useContext(MarkdownAnchorContext);
  const [failed, setFailed] = useState<string>();
  if (typeof src !== 'string') return <span>{alt ?? '图片'}</span>;
  const kind = classifyFileLink(src);
  const url = kind === 'external' ? src : context?.imagesUrl ? markdownImageUrl(src, context.imagesUrl) : undefined;
  const thumbnail = Boolean(url && failed !== url);
  const content = thumbnail
    // Raster resources and the existing external image policy; never raw SVG DOM.
    // eslint-disable-next-line @next/next/no-img-element
    ? <img draggable={selectable ? false : undefined} src={url} alt={alt ?? ''} loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(url)} />
    : kind === 'local' ? <><ImageIcon size={14} aria-hidden="true" /><span>{alt || src}</span></>
      : <span className="file-preview-image-placeholder" title={src}>[{alt || '图片'}：暂不可预览 — {src}]</span>;
  // Linked images keep their enclosing link's target; never nest buttons in links.
  if (kind !== 'local' || linked) return content;
  return <button type="button" className={thumbnail ? 'markdown-image-preview' : 'file-reference__label markdown-image-reference'}
    title={src} aria-label={`查看图片：${alt || src}`} onClick={event => {
      if (selectable && selectionTouches(event.currentTarget)) { event.preventDefault(); return; }
      event.preventDefault();
      event.stopPropagation();
      if (context) context.open(src, event.currentTarget, context.basePreview);
      else onNotify('无法打开图片', '此内容没有可用的会话文件连接。', 'warning');
    }}>{content}</button>;
}

export function MarkdownBody({ body, onNotify, streaming = false, annotationSource = false }: MarkdownProps) {
  const normalized = useMemo(() => normalizeMathWithSource(body), [body]);
  const sourceMapping = useMemo(() => () => annotationSource ? rehypeSourceMapping(normalized) : () => {}, [normalized, annotationSource]);
  const components = useMemo<Components>(() => ({
    a: ({ children, href }) => <MarkdownLink href={href} onNotify={onNotify}>{children}</MarkdownLink>,
    img: ({ src, alt, node }) => <span data-source-atom={node?.properties['data-source-atom']}><MarkdownImage src={src} alt={alt} onNotify={onNotify} selectable={annotationSource} /></span>,
    pre: ({ node, children, ...props }) => {
      const code = node?.children[0];
      if (code?.type === 'element' && code.tagName === 'code') {
        const language = elementClasses(code).find(name => name.startsWith('language-'))?.slice(9).toLowerCase();
        const source = toText(code, { whitespace: 'pre' }).replace(/\n$/, '');
        const format = markdownDiagramFormat(language, source);
        if (format === 'mermaid') return <div data-source-atom={node?.properties['data-source-atom']}><MermaidBlock source={source} streaming={streaming} onNotify={onNotify} /></div>;
        if (format === 'svg') {
          return <div data-source-atom={node?.properties['data-source-atom']}><DiagramBlock source={source} streaming={streaming} onNotify={onNotify} format="SVG" renderImage={renderSvg} /></div>;
        }
      }
      if (code?.type === 'element' && code.tagName === 'code') {
        const language = elementClasses(code).find(name => name.startsWith('language-'))?.slice(9).toLowerCase();
        const source = toText(code, { whitespace: 'pre' });
        return <CodeBlock source={source} language={language}
          sourceMap={annotationSource && typeof code.properties['data-source-text'] === 'string' ? code.properties['data-source-text'] : undefined}
          invalidSource={annotationSource && node?.properties['data-source-invalid'] !== undefined}
          streaming={streaming} onNotify={onNotify} />;
      }
      return <pre {...props}>{children}</pre>;
    },
  }), [onNotify, streaming, annotationSource]);
  return (
    <MarkdownCopySurface onNotify={onNotify} annotationSource={annotationSource}>
      <ReactMarkdown
        urlTransform={markdownUrl}
        remarkPlugins={[remarkGfm, remarkSvgBlocks, [remarkMath, { singleDollarTextMath: true }]]}
        rehypePlugins={[rehypeCopyableMath, sourceMapping, [rehypeKatex, { strict: false }]]}
        components={components}
      >
        {normalized.text}
      </ReactMarkdown>
    </MarkdownCopySurface>
  );
}

export function MarkdownInline({ body, onNotify }: MarkdownProps) {
  return (
    <MarkdownCopySurface inline onNotify={onNotify}>
      <ReactMarkdown
        urlTransform={markdownUrl}
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
        rehypePlugins={[rehypeCopyableMath, [rehypeKatex, { strict: false }]]}
        allowedElements={['a', 'strong', 'em', 'del', 'code', 'span']}
        unwrapDisallowed
        components={{
          a: ({ children, href }) => <MarkdownLink href={href} onNotify={onNotify}>{children}</MarkdownLink>,
        }}
      >
        {normalizeMathMarkdown(body)}
      </ReactMarkdown>
    </MarkdownCopySurface>
  );
}
