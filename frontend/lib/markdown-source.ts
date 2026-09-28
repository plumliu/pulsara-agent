import { parse, preprocess, postprocess } from 'micromark';
import { gfm } from 'micromark-extension-gfm';
import { math } from 'micromark-extension-math-extended';
import { decodeString } from 'micromark-util-decode-string';
import type { Element, Root, RootContent } from 'hast';
import { toText } from 'hast-util-to-text';
import { markdownDiagramFormat } from './markdown-diagram';

export interface NormalizedMarkdown { text: string; boundaries: number[] }

/** Same presentation normalization, with explicit provenance for inserted lines. */
export function normalizeMathWithSource(source: string): NormalizedMarkdown {
  let text = '';
  const boundaries: number[] = [0];
  const append = (start: number, end: number) => {
    text += source.slice(start, end);
    for (let i = start; i < end; i++) boundaries.push(i + 1);
  };
  const insert = (value: string, at: number) => { text += value; for (let i = 0; i < value.length; i++) boundaries.push(at); };
  const plain = (start: number, end: number) => {
    const pattern = /^([ \t]{0,3})\$\$[ \t]*([^\n]+?)[ \t]*\$\$[ \t]*$/gm;
    let cursor = start;
    for (const match of source.slice(start, end).matchAll(pattern)) {
      const at = start + match.index!;
      const openingEnd = at + match[1].length + 2;
      const close = at + match[0].lastIndexOf('$$');
      let contentStart = openingEnd; let contentEnd = close;
      while (/\s/.test(source[contentStart] ?? '') && contentStart < close) contentStart++;
      while (contentEnd > contentStart && /\s/.test(source[contentEnd - 1])) contentEnd--;
      append(cursor, openingEnd); insert('\n' + match[1], contentStart);
      append(contentStart, contentEnd); insert('\n' + match[1], close);
      append(close, close + 2);
      cursor = at + match[0].length;
      boundaries[boundaries.length - 1] = cursor;
    }
    append(cursor, end);
  };
  const outsideCode = (start: number, end: number) => {
    let cursor = start;
    while (cursor < end) {
      const opening = source.indexOf('`', cursor);
      if (opening < 0 || opening >= end) { plain(cursor, end); break; }
      let width = 1; while (source[opening + width] === '`') width++;
      const close = source.indexOf('`'.repeat(width), opening + width);
      if (close < 0 || close >= end) { plain(cursor, end); break; }
      plain(cursor, opening); append(opening, close + width); cursor = close + width;
    }
  };
  const openingFence = /^ {0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)/gm;
  let cursor = 0;
  while (cursor < source.length) {
    openingFence.lastIndex = cursor;
    const opening = openingFence.exec(source);
    if (!opening) { outsideCode(cursor, source.length); break; }
    outsideCode(cursor, opening.index);
    const marker = opening[1];
    const closingFence = new RegExp(`^ {0,3}${marker[0]}{${marker.length},}[ \\t]*(?:\\n|$)`, 'gm');
    closingFence.lastIndex = opening.index + opening[0].length;
    const closing = closingFence.exec(source);
    const end = closing ? closing.index + closing[0].length : source.length;
    append(opening.index, end); cursor = end;
  }
  return { text, boundaries };
}

interface SourceUnit { value: string; start: number; end: number; flow?: boolean }

function linearUnits(value: string, start: number): SourceUnit[] {
  const result: SourceUnit[] = [];
  for (const character of value) {
    result.push({ value: character, start, end: start + character.length });
    start += character.length;
  }
  return result;
}

/** micromark owns syntax/decoding. This adapter tracks only emitted character boundaries. */
function sourceUnits(text: string): { units: SourceUnit[]; code: Map<number, SourceUnit[]> } {
  const events = postprocess(parse({ extensions: [gfm(), math({ singleDollarTextMath: true })] })
    .document().write(preprocess()(text, undefined, true)));
  const units: SourceUnit[] = [];
  const stack: string[] = [];
  const code = new Map<number, SourceUnit[]>();
  for (const [phase, token, context] of events) {
    if (phase === 'enter') { stack.push(token.type); continue; }
    const start = token.start.offset; const end = token.end.offset;
    if (['data', 'codeTextData', 'codeFlowValue', 'autolinkProtocol', 'autolinkEmail'].includes(token.type)) {
      const raw = text.slice(start, end);
      // sliceSerialize accounts for parser normalization (e.g. tab expansion).
      const value = context.sliceSerialize(token);
      if (raw.length === value.length) {
        let offset = start;
        for (const character of value) {
          units.push({ value: character, start: offset, end: offset + character.length, flow: token.type === 'codeFlowValue' }); offset += character.length;
        }
      } else {
        // A fence indent can consume part of a tab. micromark then emits its
        // remaining virtual spaces at the next source offset. Only that exact
        // tab maps to those spaces; subsequent code retains linear positions.
        const extra = value.length - raw.length;
        if (extra > 0 && text[start - 1] === '\t' && /^ +$/.test(value.slice(0, extra)) && value.slice(extra) === raw) {
          units.push({ value: value.slice(0, extra), start: start - 1, end: start, flow: token.type === 'codeFlowValue' });
          units.push(...linearUnits(raw, start).map(unit => ({ ...unit, flow: token.type === 'codeFlowValue' })));
        }
        // Unknown transformations fail the renderer equality check below.
      }
    } else if (token.type === 'characterReference' || token.type === 'characterEscape') {
      units.push({ value: decodeString(text.slice(start, end)), start, end });
    } else if (token.type === 'lineEnding') {
      units.push({ value: stack.includes('codeText') ? ' ' : context.sliceSerialize(token), start, end, flow: stack.includes('codeFenced') || stack.includes('codeIndented') });
    } else if (token.type === 'codeFenced' || token.type === 'codeIndented') {
      const content = units.filter(unit => unit.flow && unit.start >= start && unit.end <= end);
      // Match mdast-util-from-markdown's code exit normalization, using parser
      // tokens rather than searching for the rendered code in the source.
      if (token.type === 'codeFenced' && /^[\r\n]+$/.test(content[0]?.value ?? '')) content.shift();
      if (/^[\r\n]+$/.test(content.at(-1)?.value ?? '')) content.pop();
      code.set(start, content);
    }
    stack.pop();
  }
  return { units: units.sort((a, b) => a.start - b.start), code };
}

export function rehypeSourceMapping(normalized: NormalizedMarkdown) {
  const { units, code: codeUnits } = sourceUnits(normalized.text);
  const mappingOf = (selected: SourceUnit[]) => selected.flatMap(unit => Array.from({ length: unit.value.length }, () => [normalized.boundaries[unit.start], normalized.boundaries[unit.end]]));
  const position = (node: RootContent) => {
    const start = node.position?.start.offset; const end = node.position?.end.offset;
    if (start === undefined || end === undefined) return undefined;
    return { start, end, originalStart: normalized.boundaries[start], originalEnd: normalized.boundaries[end] };
  };
  return (tree: Root) => {
    const visit = (parent: Root | Element) => {
      parent.children = parent.children.map(node => {
        const pos = position(node);
        // ReactMarkdown displays raw HTML literally; preserve that same text,
        // with a linear source map, rather than leave its text uncovered.
        if (node.type === 'raw' && pos) {
          return { type: 'element', tagName: 'span', properties: { 'data-source-text': JSON.stringify(mappingOf(linearUnits(node.value, pos.start))) }, children: [{ type: 'text', value: node.value }] };
        }
        if (node.type === 'element') {
          const classes = Array.isArray(node.properties.className) ? node.properties.className.map(String) : [];
          const code = node.tagName === 'pre' ? node.children[0] : undefined;
          const codeClasses = code?.type === 'element' && Array.isArray(code.properties.className) ? code.properties.className.map(String) : [];
          const language = codeClasses.find(name => name.startsWith('language-'))?.slice(9).toLowerCase();
          const isDiagram = code?.type === 'element' && markdownDiagramFormat(language, toText(code, { whitespace: 'pre' }).replace(/\n$/, ''));
          if (pos && (node.tagName === 'img' || classes.includes('math-copy-target') || isDiagram)) {
            node.properties['data-source-atom'] = `${pos.originalStart}:${pos.originalEnd}`;
            return node;
          }
          if (node.properties.dataFootnoteRef || node.properties.dataFootnotes) {
            node.properties['data-source-invalid'] = ''; return node;
          }
          if (pos && node.tagName === 'pre' && code?.type === 'element' && code.children.length === 1 && code.children[0].type === 'text') {
            const selected = codeUnits.get(pos.start) ?? [];
            const emitted = selected.map(unit => unit.value).join('');
            const value = code.children[0].value;
            if (value !== emitted + (emitted ? '\n' : '')) { node.properties['data-source-invalid'] = ''; return node; }
            const mapping = mappingOf(selected);
            if (emitted) mapping.push([selected.at(-1) ? normalized.boundaries[selected.at(-1)!.end] : pos.originalEnd, selected.at(-1) ? normalized.boundaries[selected.at(-1)!.end] : pos.originalEnd]);
            code.properties['data-source-text'] = JSON.stringify(mapping);
            return node;
          }
          visit(node); return node;
        }
        if (node.type !== 'text') return node;
        // hast-util-to-jsx-runtime removes whitespace between table structural
        // elements. Keep it as text: a decoration span would survive that step
        // and create anonymous table boxes with independent column widths.
        if (!pos && !node.value.trim() && parent.type === 'element'
          && ['table', 'thead', 'tbody', 'tfoot', 'tr'].includes(parent.tagName)) return node;
        if (!pos) return { type: 'element', tagName: 'span', properties: { [node.value.trim() ? 'data-source-invalid' : 'data-source-decoration']: '' }, children: [node] };
        let low = 0; let high = units.length;
        while (low < high) { const mid = (low + high) >>> 1; if (units[mid].start < pos.start) low = mid + 1; else high = mid; }
        const selected: SourceUnit[] = [];
        for (let i = low; i < units.length && units[i].end <= pos.end; i++) selected.push(units[i]);
        let emitted = selected.map(unit => unit.value).join('');
        // HAST adds one display newline after a code block. It has no source.
        const syntheticNewline = parent.type === 'element' && parent.tagName === 'code' && node.value === emitted + '\n';
        if (syntheticNewline) emitted += '\n';
        if (emitted !== node.value) return { type: 'element', tagName: 'span', properties: { 'data-source-invalid': '' }, children: [node] };
        const mapping: number[][] = [];
        for (const unit of selected) for (let i = 0; i < unit.value.length; i++) mapping.push([
          normalized.boundaries[unit.start], normalized.boundaries[unit.end],
        ]);
        if (syntheticNewline) mapping.push([pos.originalEnd, pos.originalEnd]);
        if (parent.type === 'element' && parent.children.length === 1 && parent.tagName !== 'a') {
          parent.properties['data-source-text'] = JSON.stringify(mapping); return node;
        }
        return { type: 'element', tagName: 'span', properties: { 'data-source-text': JSON.stringify(mapping) }, children: [node] };
      }) as typeof parent.children;
    };
    visit(tree);
  };
}
