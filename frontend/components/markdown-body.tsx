import rehypeKatex from 'rehype-katex';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';

const openingMarkers = new Set(['(', '[']);

function escapedAt(value: string, index: number): boolean {
  let backslashes = 0;
  for (let cursor = index - 1; cursor >= 0 && value[cursor] === '\\'; cursor -= 1) {
    backslashes += 1;
  }
  return backslashes % 2 === 1;
}

function findSlashDelimiter(
  value: string,
  from: number,
  markers: ReadonlySet<string>,
): { index: number; marker: string } | undefined {
  for (let index = value.indexOf('\\', from); index >= 0; index = value.indexOf('\\', index + 1)) {
    const marker = value[index + 1];
    if (marker && markers.has(marker) && !escapedAt(value, index)) return { index, marker };
  }
  return undefined;
}

function normalizeSlashMath(value: string): string {
  let output = '';
  let cursor = 0;
  while (cursor < value.length) {
    const opening = findSlashDelimiter(value, cursor, openingMarkers);
    if (!opening) {
      output += value.slice(cursor);
      break;
    }
    const closingMarker = opening.marker === '(' ? ')' : ']';
    const closing = findSlashDelimiter(
      value,
      opening.index + 2,
      new Set([closingMarker]),
    );
    if (!closing) {
      output += value.slice(cursor);
      break;
    }

    output += value.slice(cursor, opening.index);
    const math = value.slice(opening.index + 2, closing.index);
    if (opening.marker === '(') {
      output += `$${math}$`;
    } else {
      const lineStart = value.lastIndexOf('\n', opening.index - 1) + 1;
      const lineEndCandidate = value.indexOf('\n', closing.index + 2);
      const lineEnd = lineEndCandidate < 0 ? value.length : lineEndCandidate;
      const needsLeadingBreak = value.slice(lineStart, opening.index).trim().length > 0;
      const needsTrailingBreak = value.slice(closing.index + 2, lineEnd).trim().length > 0;
      output += `${needsLeadingBreak ? '\n' : ''}$$\n${math.trim()}\n$$${needsTrailingBreak ? '\n' : ''}`;
    }
    cursor = closing.index + 2;
  }
  return output;
}

function normalizePlainMath(value: string): string {
  return normalizeSlashMath(value).replace(
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
