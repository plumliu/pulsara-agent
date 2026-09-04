import rehypeKatex from 'rehype-katex';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math-extended';

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

export function MarkdownBody({ body }: { body: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
      rehypePlugins={[[rehypeKatex, { strict: false }]]}
      components={{
        a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
      }}
    >
      {normalizeMathMarkdown(body)}
    </ReactMarkdown>
  );
}

export function MarkdownInline({ body }: { body: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: true }]]}
      rehypePlugins={[[rehypeKatex, { strict: false }]]}
      allowedElements={['a', 'strong', 'em', 'del', 'code', 'span']}
      unwrapDisallowed
      components={{
        a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
      }}
    >
      {normalizeMathMarkdown(body)}
    </ReactMarkdown>
  );
}
