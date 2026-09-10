'use client';

import type { Element as HastElement, Parent as HastParent, Root as HastRoot } from 'hast';
import { toText } from 'hast-util-to-text';
import rehypeKatex from 'rehype-katex';
import { useCallback, useRef, type KeyboardEvent, type MouseEvent, type PointerEvent, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math-extended';
import { SKIP, visitParents } from 'unist-util-visit-parents';

export type MarkdownNotify = (
  title: string,
  detail?: string,
  tone?: 'neutral' | 'success' | 'warning',
) => void;

interface MarkdownProps {
  body: string;
  onNotify: MarkdownNotify;
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
}: {
  children: ReactNode;
  inline?: boolean;
  onNotify: MarkdownNotify;
}) {
  const pointerOrigin = useRef<PointerOrigin | undefined>(undefined);

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
    const target = findMathTarget(event.target);
    pointerOrigin.current = target ? {
      pointerId: event.pointerId,
      target,
      x: event.clientX,
      y: event.clientY,
      moved: false,
    } : undefined;
  }, []);

  const onPointerMove = useCallback((event: PointerEvent<HTMLElement>) => {
    const origin = pointerOrigin.current;
    if (!origin || origin.pointerId !== event.pointerId || origin.moved) return;
    if (Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > 5) {
      origin.moved = true;
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
    onClick,
    onKeyDown,
    onPointerCancel: () => { pointerOrigin.current = undefined; },
    onPointerDown,
    onPointerMove,
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

function normalizePlainMath(value: string): string {
  return value.replace(
    /^([ \t]{0,3})\$\$[ \t]*([^\n]+?)[ \t]*\$\$[ \t]*$/gm,
    (_match, indent: string, math: string) => `${indent}$$\n${indent}${math.trim()}\n${indent}$$`,
  );
}

function normalizeOutsideInlineCode(value: string): string {
  let output = '';
  let cursor = 0;
  while (cursor < value.length) {
    const opening = value.indexOf('`', cursor);
    if (opening < 0) {
      output += normalizePlainMath(value.slice(cursor));
      break;
    }
    let width = 1;
    while (value[opening + width] === '`') width += 1;
    const delimiter = '`'.repeat(width);
    const closing = value.indexOf(delimiter, opening + width);
    if (closing < 0) {
      output += normalizePlainMath(value.slice(cursor));
      break;
    }
    output += normalizePlainMath(value.slice(cursor, opening));
    output += value.slice(opening, closing + width);
    cursor = closing + width;
  }
  return output;
}

export function normalizeMathMarkdown(value: string): string {
  const openingFence = /^ {0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)/gm;
  let output = '';
  let cursor = 0;
  while (cursor < value.length) {
    openingFence.lastIndex = cursor;
    const opening = openingFence.exec(value);
    if (!opening) {
      output += normalizeOutsideInlineCode(value.slice(cursor));
      break;
    }
    output += normalizeOutsideInlineCode(value.slice(cursor, opening.index));
    const marker = opening[1] ?? '```';
    const closingFence = new RegExp(
      `^ {0,3}${marker[0]}{${marker.length},}[ \\t]*(?:\\n|$)`,
      'gm',
    );
    closingFence.lastIndex = opening.index + opening[0].length;
    const closing = closingFence.exec(value);
    const end = closing ? closing.index + closing[0].length : value.length;
    output += value.slice(opening.index, end);
    cursor = end;
  }
  return output;
}

export function MarkdownBody({ body, onNotify }: MarkdownProps) {
  return (
    <MarkdownCopySurface onNotify={onNotify}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
        rehypePlugins={[rehypeCopyableMath, [rehypeKatex, { strict: false }]]}
        components={{
          a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
        }}
      >
        {normalizeMathMarkdown(body)}
      </ReactMarkdown>
    </MarkdownCopySurface>
  );
}

export function MarkdownInline({ body, onNotify }: MarkdownProps) {
  return (
    <MarkdownCopySurface inline onNotify={onNotify}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
        rehypePlugins={[rehypeCopyableMath, [rehypeKatex, { strict: false }]]}
        allowedElements={['a', 'strong', 'em', 'del', 'code', 'span']}
        unwrapDisallowed
        components={{
          a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
        }}
      >
        {normalizeMathMarkdown(body)}
      </ReactMarkdown>
    </MarkdownCopySurface>
  );
}
