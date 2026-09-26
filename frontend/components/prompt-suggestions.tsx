'use client';

import type { Editor } from '@tiptap/core';
import { shift } from '@floating-ui/dom';
import { PluginKey } from '@tiptap/pm/state';
import type { EditorView } from '@tiptap/pm/view';
import { ReactRenderer } from '@tiptap/react';
import { Suggestion, exitSuggestion, type SuggestionProps } from '@tiptap/suggestion';
import { BookOpenText, ChevronRight, File, Folder } from 'lucide-react';
import { forwardRef, useEffect, useId, useImperativeHandle, useRef, useState } from 'react';
import { formatWorkspaceReference, type CompleteWorkspacePaths, type WorkspacePathCandidate } from '../lib/file-reference';
import type { SkillCapability } from '../lib/pulsara-types';

type Candidate = { kind: 'skill'; name: string } | WorkspacePathCandidate;
type Selection = { item: Candidate; browse?: boolean };
interface Page { rows: Candidate[]; cursor: string | null; error?: string }
interface PopupProps extends SuggestionProps<Page, Selection> {
  kind: 'skill' | 'path';
  load: (query: string, cursor: string | null, signal: AbortSignal) => Promise<Page>;
}
interface PopupHandle { onKeyDown: (event: KeyboardEvent) => boolean }

const Popup = forwardRef<PopupHandle, PopupProps>(function Popup(props, ref) {
  return <CandidatePopup key={`${props.range.from}:${props.text}:${props.loading}`} {...props} ref={ref} />;
});

const CandidatePopup = forwardRef<PopupHandle, PopupProps>(function CandidatePopup(props, ref) {
  const { editor, command, loading, kind, query, load } = props;
  const [page, setPage] = useState<Page>(props.items[0] ?? { rows: [], cursor: null });
  const [selected, setSelected] = useState(0);
  const [pending, setPending] = useState(false);
  const request = useRef<AbortController | null>(null);
  const list = useRef<HTMLDivElement>(null);
  const id = useId();
  const hasAction = Boolean(page.cursor || page.error);
  const count = page.rows.length + Number(hasAction);
  const active = Math.min(selected, Math.max(0, count - 1));
  const visible = loading || pending || count > 0;

  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => {
    if (!visible) return;
    const textbox = editor.view.dom;
    textbox.setAttribute('aria-controls', id);
    textbox.setAttribute('aria-autocomplete', 'list');
    if (count) textbox.setAttribute('aria-activedescendant', `${id}-${active}`);
    else textbox.removeAttribute('aria-activedescendant');
    list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: 'nearest' });
    return () => {
      textbox.removeAttribute('aria-controls');
      textbox.removeAttribute('aria-autocomplete');
      textbox.removeAttribute('aria-activedescendant');
    };
  }, [active, count, editor, id, visible]);

  const more = async () => {
    if (request.current || loading) return;
    const controller = new AbortController();
    request.current = controller;
    setPending(true);
    const next = await load(query, page.cursor, controller.signal);
    if (controller.signal.aborted) return;
    setPage(current => next.error ? { ...current, error: next.error } : {
      rows: [...current.rows, ...next.rows.filter(row => !current.rows.some(existing =>
        existing.kind === 'skill' || row.kind === 'skill' ? existing.name === row.name : existing.path === row.path))],
      cursor: next.cursor,
    });
    request.current = null;
    setPending(false);
  };
  const choose = () => {
    if (loading) return;
    const item = page.rows[active];
    if (item) command({ item });
    else if (hasAction) void more();
  };
  useImperativeHandle(ref, () => ({ onKeyDown: event => {
    if (!visible) return false;
    if (event.isComposing || event.keyCode === 229 || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return false;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (count) setSelected((active + (event.key === 'ArrowDown' ? 1 : -1) + count) % count);
      return true;
    }
    if (event.key === 'Enter' || event.key === 'Tab') { choose(); return true; }
    const item = page.rows[active];
    if (event.key === 'ArrowRight' && item?.kind === 'directory') {
      command({ item, browse: true });
      return true;
    }
    return false;
  } }));

  if (!visible) return null;
  return <div className="prompt-candidates" onMouseDown={event => event.preventDefault()}>
    <div ref={list} id={id} role="listbox" aria-label={kind === 'skill' ? '技能候选' : '文件和目录候选'} aria-busy={loading || pending}>
      {page.rows.map((item, index) => {
        const Icon = item.kind === 'skill' ? BookOpenText : item.kind === 'directory' ? Folder : File;
        return <div key={item.kind === 'skill' ? item.name : item.path} id={`${id}-${index}`} role="option"
          aria-selected={active === index} className="prompt-candidates__row" onMouseEnter={() => setSelected(index)}>
          <button type="button" tabIndex={-1} onClick={() => command({ item })} title={item.kind === 'skill' ? item.name : item.path}>
            <Icon size={14} aria-hidden="true" /><span>{item.name}</span>
          </button>
          {item.kind === 'directory' && <button type="button" tabIndex={-1} className="prompt-candidates__browse"
            aria-label={`进入 ${item.name}`} title="进入目录 →" onClick={() => command({ item, browse: true })}><ChevronRight size={14} /></button>}
        </div>;
      })}
      {hasAction && <div id={`${id}-${page.rows.length}`} role="option" aria-selected={active === page.rows.length}
        className="prompt-candidates__row" onMouseEnter={() => setSelected(page.rows.length)}>
        <button type="button" tabIndex={-1} disabled={pending} onClick={() => void more()}>{pending ? '加载中…' : page.error ? '重试' : '加载更多'}</button>
      </div>}
    </div>
    {page.error && <div className="prompt-candidates__status" role="status">{page.error}</div>}
    {loading && !page.rows.length && !page.error && <div className="prompt-candidates__status" role="status">加载中…</div>}
  </div>;
});

interface Options { skills: readonly SkillCapability[]; completePaths?: CompleteWorkspacePaths }

/** Suggestion owns matching, async cancellation and Floating UI. We supply rows and existing nodes. */
export function usePromptSuggestions(editor: Editor, disabled: boolean, options: Options) {
  const current = useRef(options);
  const keydown = useRef<(view: EditorView, event: KeyboardEvent) => boolean>(() => false);
  useEffect(() => { current.current = options; }, [options]);
  useEffect(() => {
    if (disabled) return;
    const plugins = (['skill', 'path'] as const).map(kind => {
      const key = new PluginKey(`prompt-${kind}-candidates`);
      const load = async (query: string, cursor: string | null, signal: AbortSignal): Promise<Page> => {
        if (kind === 'skill') return { rows: current.current.skills
          .filter(skill => skill.name.toLowerCase().startsWith(query.toLowerCase()))
          .map(skill => ({ kind: 'skill' as const, name: skill.name })), cursor: null };
        try {
          const page = await current.current.completePaths!(query, cursor, signal);
          return { rows: page.items, cursor: page.next_cursor };
        } catch {
          return { rows: [], cursor: null, error: '无法读取目录，请检查路径或重试。' };
        }
      };
      const plugin = Suggestion<Page, Selection>({
        editor, pluginKey: key, char: kind === 'skill' ? '$' : '@',
        allowSpaces: kind === 'path', allowedPrefixes: null,
        debounce: kind === 'path' ? 100 : 0,
        placement: 'top-start', offset: { mainAxis: 6 },
        // A narrow viewport can fit neither alignment at a mid-line cursor.
        floatingUi: { strategy: 'fixed', middleware: [shift({ padding: 8 })] },
        allow: ({ state, range }) => {
          if (!editor.isEditable || (kind === 'path' && !current.current.completePaths)) return false;
          const before = state.doc.textBetween(Math.max(0, range.from - 1), range.from, '\n', '\ufffc');
          if (/[A-Za-z0-9_/@.$-]/.test(before)) return false;
          const query = state.doc.textBetween(range.from + 1, range.to);
          return kind === 'skill' ? /^[a-z0-9-]*$/i.test(query) : !/(?:^|\s)\$|[\u0000-\u001f\u007f]/.test(query);
        },
        items: async ({ query, signal }) => [await load(query, null, signal)],
        command: ({ editor, range, props: { item, browse } }) => {
          if (!editor.isEditable) return;
          if (browse && item.kind === 'directory') {
            editor.chain().focus().insertContentAt(range, { type: 'text', text: `@${item.relative_path}/` }).run();
            return;
          }
          const node = item.kind === 'skill'
            ? { type: 'skillReference', attrs: { raw: `$${item.name}` } }
            : { type: 'fileReference', attrs: { raw: formatWorkspaceReference(item), name: item.name, kind: item.kind, state: 'ready' } };
          const suffix = editor.state.doc.textBetween(range.to, Math.min(range.to + 1, editor.state.doc.content.size), '\n', '\ufffc');
          editor.chain().focus().insertContentAt(range, [node, ...(/^\s/.test(suffix) ? [] : [{ type: 'text', text: ' ' }])]).run();
        },
        render: () => {
          let component: ReactRenderer<PopupHandle> | undefined;
          let unmount: (() => void) | undefined;
          const propsFor = (props: SuggestionProps<Page, Selection>) => ({ ...props, kind, load });
          return {
            onStart: props => {
              component = new ReactRenderer(Popup, { props: propsFor(props), editor });
              component.element.classList.add('prompt-candidates-anchor');
              unmount = props.mount(component.element);
            },
            onUpdate: props => component?.updateProps(propsFor(props)),
            onExit: () => { unmount?.(); component?.destroy(); component = undefined; },
            onKeyDown: ({ view, event }) => view.composing ? false : component?.ref?.onKeyDown(event) ?? false,
          };
        },
      });
      editor.registerPlugin(plugin);
      return { plugin, key };
    });
    // Direct editor props run before plugin props: let suggestions consume Enter first.
    keydown.current = (view, event) => plugins.some(({ plugin }) => Boolean(plugin.props.handleKeyDown?.call(plugin, view, event)));
    return () => {
      keydown.current = () => false;
      for (const { key } of plugins) {
        if (!editor.isDestroyed) { exitSuggestion(editor.view, key); editor.unregisterPlugin(key); }
      }
    };
  }, [disabled, editor]);
  return keydown;
}
