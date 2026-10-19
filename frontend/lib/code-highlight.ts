import { useEffect, useState } from 'react';
import type { ShjLanguage, ShjToken } from '@speed-highlight/core';

export type CodePiece = { text: string; type?: ShjToken };

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

export function highlightLanguage(label: string): ShjLanguage | undefined {
  const name = label.toLowerCase();
  return Object.hasOwn(aliases, name) ? aliases[name] : supported.has(name as ShjLanguage) ? name as ShjLanguage : undefined;
}

/** Tokenization is advisory; source text remains the authority for display. */
export function useCodeHighlight(source: string, language?: string, enabled = true): CodePiece[] | undefined {
  const syntax = language ? highlightLanguage(language) : undefined;
  const [highlighted, setHighlighted] = useState<{ source: string; language: ShjLanguage; pieces: CodePiece[] }>();
  useEffect(() => {
    if (!syntax || syntax === 'plain' || !enabled || !source) return;
    let live = true;
    const pieces: CodePiece[] = [];
    // Use the dependency's text tokens, never its HTML rendering API. Exact
    // source matching also preserves chat copy/annotation offsets and file bytes.
    void import('@speed-highlight/core').then(({ tokenize }) => tokenize(source, syntax, (text, type) => {
      if (text) pieces.push({ text, type });
    })).then(() => {
      if (live && pieces.map(piece => piece.text).join('') === source) setHighlighted({ source, language: syntax, pieces });
    }).catch(() => undefined);
    return () => { live = false; };
  }, [source, syntax, enabled]);
  return enabled && highlighted?.source === source && highlighted.language === syntax ? highlighted.pieces : undefined;
}

export function fileCodeLanguage(filename: string): ShjLanguage | undefined {
  const name = filename.split(/[\\/]/).at(-1)?.toLowerCase() ?? '';
  if (/^dockerfile(?:\.|$)/.test(name)) return 'docker';
  if (['makefile', 'gnumakefile'].includes(name)) return 'make';
  if (['.bashrc', '.bash_profile', '.zshrc', '.zprofile', '.profile'].includes(name)) return 'bash';
  if (/^\.env(?:\.|$)/.test(name)) return 'ini';
  const extension = name.includes('.') ? name.split('.').at(-1)! : '';
  const extensions: Record<string, ShjLanguage> = {
    mjs: 'js', cjs: 'js', mts: 'ts', cts: 'ts', jsonc: 'json',
    h: 'c', htm: 'html', svg: 'xml', pyw: 'py', pyi: 'py',
  };
  return Object.hasOwn(extensions, extension) ? extensions[extension] : highlightLanguage(extension);
}
