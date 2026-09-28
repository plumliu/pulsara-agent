'use client';

import { ChevronDown, Code2, Copy, TextWrap } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import type { ShjLanguage, ShjToken } from '@speed-highlight/core';
import type { MarkdownNotify } from './markdown-body';

type Piece = { text: string; type?: ShjToken };
type LinePiece = Piece & { start: number; end: number };

function splitCodeLines(source: string, pieces?: Piece[]): LinePiece[][] {
  const lines: LinePiece[][] = [[]];
  let offset = 0;
  for (const piece of pieces ?? [{ text: source }]) {
    piece.text.split('\n').forEach((text, index) => {
      if (index) { offset++; lines.push([]); }
      if (!text) return;
      lines[lines.length - 1].push({ text, type: piece.type, start: offset, end: offset + text.length });
      offset += text.length;
    });
  }
  return lines;
}

const supported = new Set<ShjLanguage>([
  'asm', 'bash', 'bf', 'c', 'css', 'csv', 'diff', 'docker', 'git', 'go',
  'html', 'http', 'ini', 'java', 'js', 'jsdoc', 'json', 'log', 'lua',
  'make', 'md', 'pl', 'py', 'regex', 'rs', 'sql', 'toml', 'ts', 'xml', 'yaml',
]);
const aliases: Record<string, ShjLanguage> = {
  python: 'py', javascript: 'js', jsx: 'js', typescript: 'ts', tsx: 'ts',
  shell: 'bash', sh: 'bash', zsh: 'bash', yml: 'yaml', markdown: 'md',
  rust: 'rs', dockerfile: 'docker', plaintext: 'plain', text: 'plain', txt: 'plain',
};

function highlightLanguage(label: string): ShjLanguage | undefined {
  const name = label.toLowerCase();
  return aliases[name] ?? (supported.has(name as ShjLanguage) ? name as ShjLanguage : undefined);
}

export function CodeBlock({ source, language, sourceMap, invalidSource, onNotify, streaming = false }: {
  source: string;
  language?: string;
  sourceMap?: string;
  invalidSource?: boolean;
  onNotify: MarkdownNotify;
  streaming?: boolean;
}) {
  // HAST adds one display newline before the closing fence. Keep its source
  // mapping out of the visual rows; actual newlines stay between logical rows.
  const displaySource = source.replace(/\n$/, '');
  const label = language || 'TEXT';
  const syntax = highlightLanguage(label);
  const [highlighted, setHighlighted] = useState<{ source: string; language: ShjLanguage; pieces: Piece[] }>();
  const [expanded, setExpanded] = useState(false);
  const [wrapped, setWrapped] = useState(false);
  const mapping = useMemo(() => {
    if (!sourceMap) return undefined;
    const entries = JSON.parse(sourceMap) as number[][];
    return entries.length >= displaySource.length ? entries.slice(0, displaySource.length) : undefined;
  }, [sourceMap, displaySource]);
  useEffect(() => {
    if (!syntax || syntax === 'plain' || streaming || !displaySource) return;
    let live = true;
    const pieces: Piece[] = [];
    // The library only classifies text. React still owns every character and
    // each token retains its original source offsets for copy and annotations.
    void import('@speed-highlight/core').then(({ tokenize }) => tokenize(displaySource, syntax, (text, type) => {
      if (text) pieces.push({ text, type });
    })).then(() => {
      if (live && pieces.map(piece => piece.text).join('') === displaySource) setHighlighted({ source: displaySource, language: syntax, pieces });
    }).catch(() => undefined);
    return () => { live = false; };
  }, [displaySource, syntax, streaming]);

  const pieces = !streaming && highlighted?.source === displaySource && highlighted.language === syntax ? highlighted.pieces : undefined;
  const lines = useMemo(() => splitCodeLines(displaySource, pieces), [displaySource, pieces]);
  const canExpand = lines.length > 10;
  const mapped = sourceMap !== undefined;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(displaySource);
      onNotify('代码已复制', undefined, 'success');
    } catch {
      onNotify('无法复制代码', '浏览器没有授予剪贴板权限。', 'warning');
    }
  };

  return <section className="code-card" aria-label={`${label} 代码块`}
    data-source-invalid={invalidSource || (mapped && !mapping) ? '' : undefined}>
    <div className="code-card__toolbar" data-source-decoration="">
      <span><Code2 size={13} aria-hidden="true" />{label.toUpperCase()}</span>
      <div>
        <button type="button" aria-label={wrapped ? '关闭自动换行' : '按宽度换行'}
          aria-pressed={wrapped} title={wrapped ? '关闭自动换行' : '按宽度换行'}
          onClick={() => setWrapped(value => !value)}><TextWrap size={14} /></button>
        <button type="button" aria-label="复制代码" title="复制代码" onClick={() => void copy()}><Copy size={14} /></button>
        {canExpand && <button type="button" aria-label={expanded ? '收起代码' : '展开代码'}
          aria-expanded={expanded} title={expanded ? '收起代码' : '展开代码'} onClick={() => setExpanded(value => !value)}>
          <ChevronDown size={14} className={expanded ? 'is-expanded' : ''} />
        </button>}
      </div>
    </div>
    <pre className={`code-card__body${expanded ? ' is-expanded' : ''}${wrapped ? ' is-wrapped' : ''}`}>
      <code>
        {lines.map((line, lineIndex) => <span className="code-card__line" data-line={lineIndex + 1} key={lineIndex}>
          <span className="code-card__line-content">
            {line.map((piece, pieceIndex) => <span key={pieceIndex}
              className={piece.type ? `code-card__token--${piece.type}` : undefined}
              data-source-text={mapping ? JSON.stringify(mapping.slice(piece.start, piece.end)) : undefined}>{piece.text}</span>)}
          </span>
        </span>)}
      </code>
    </pre>
  </section>;
}
