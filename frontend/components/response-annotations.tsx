'use client';
import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { autoUpdate, computePosition, flip, hide, offset, shift } from '@floating-ui/dom';
import type { MappedSelection } from '../lib/response-selection';
import { createPortal } from 'react-dom';
import { MessageSquarePlus, CornerUpLeft, Trash2, X } from 'lucide-react';
import { closeHistory } from '@tiptap/pm/history';
import type { PromptAnnotationPart, PromptAnnotationSource } from '../lib/prompt-content';
import type { DraftAnnotation, PromptDraftStore } from '../lib/prompt-draft';
import { annotationSourceRange, highlightAnnotationSource, mapResponseSelection, registerAnnotationBody, showAnnotationHighlight } from '../lib/response-selection';
import type { MarkdownNotify } from './markdown-body';
import { AnnotationQuote } from './annotation-quote';
import { DraftAnnotationContext } from './annotation-interactions';

const AnnotationContext = createContext<(source: PromptAnnotationSource | null) => void>(() => {});

export function AnnotationBody({ entryId, body, children }: { entryId?: string; body: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (ref.current && entryId) return registerAnnotationBody(ref.current, body); }, [body, entryId]);
  return <div ref={ref} data-annotation-entry={entryId}>{children}</div>;
}

export function AnnotationCard({ value }: { value: PromptAnnotationPart }) {
  const locate = useContext(AnnotationContext);
  return <div className="prompt-annotation prompt-annotation--saved">
    <button type="button" className="prompt-annotation__source" onClick={() => locate(value.source)}><CornerUpLeft size={13} />{value.source ? '引用回复' : '来源不可定位'}</button>
    <AnnotationQuote quote={value.quote} />
    {value.comment && <p>{value.comment}</p>}
  </div>;
}

function SelectionMenu({ selection, onAdd, onHide }: { selection: MappedSelection; onAdd: () => void; onHide: () => void }) {
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const floating = ref.current;
    if (!floating) return;
    const boundary = selection.root.closest<HTMLElement>('.thread-scroll') ?? selection.root;
    const reference = {
      contextElement: selection.root,
      getBoundingClientRect: () => selection.range.getBoundingClientRect(),
      getClientRects: () => selection.range.getClientRects(),
    };
    let disposed = false;
    const update = () => {
      if (!selection.root.isConnected || !selection.range.getClientRects().length) { onHide(); return; }
      void computePosition(reference, floating, {
        placement: 'top', strategy: 'fixed',
        middleware: [offset(7), flip({ boundary, padding: 8 }), shift({ boundary, padding: 8, crossAxis: true }), hide({ boundary })],
      }).then(({ x, y, middlewareData }) => {
        if (disposed) return;
        // Keep a selected source available when scrolling it back into view.
        Object.assign(floating.style, { left: `${x}px`, top: `${y}px`, visibility: middlewareData.hide?.referenceHidden ? 'hidden' : 'visible' });
      });
    };
    const cleanup = autoUpdate(reference, floating, update);
    return () => { disposed = true; cleanup(); };
  }, [selection, onHide]);
  return <button ref={ref} type="button" className="annotation-selection-menu" style={{ visibility: 'hidden' }}
    onPointerDown={event => event.preventDefault()} onClick={onAdd}><MessageSquarePlus size={14} />添加注释</button>;
}

function sourceRoot(source: PromptAnnotationSource | null): HTMLElement | undefined {
  return source ? [...document.querySelectorAll<HTMLElement>('[data-annotation-entry]')].find(node => node.dataset.annotationEntry === source.entry_id) : undefined;
}

function sourceRect(root: HTMLElement, source: PromptAnnotationSource): DOMRect {
  const range = annotationSourceRange(root, source);
  return range?.getClientRects()[0] ?? root.getBoundingClientRect();
}

function AnnotationMarker({ item, siblings, active, onEdit, onPreview }: {
  item: DraftAnnotation; siblings: DraftAnnotation[]; active: boolean;
  onEdit: (id: string) => void; onPreview: (id?: string) => void;
}) {
  const ref = useRef<HTMLButtonElement>(null);
  // Reattach after history hydration or disclosure changes replace a source DOM.
  useLayoutEffect(() => {
    const floating = ref.current; const source = item.value.source; const root = sourceRoot(source);
    if (!floating || !root || !source) return;
    const boundary = root.closest<HTMLElement>('.thread-scroll') ?? root;
    const reference = { contextElement: root, getBoundingClientRect: () => {
      let top = -Infinity;
      for (const sibling of siblings) {
        top = Math.max(sourceRect(root, sibling.value.source!).top, top + 26);
        if (sibling.id === item.id) break;
      }
      return new DOMRect(root.getBoundingClientRect().right, top, 0, 20);
    } };
    let disposed = false;
    const update = () => {
      if (!root.isConnected || !root.getClientRects().length) { floating.style.visibility = 'hidden'; return; }
      void computePosition(reference, floating, { placement: 'right-start', strategy: 'fixed',
        middleware: [offset(8), shift({ boundary, padding: 6, crossAxis: true }), hide({ boundary })],
      }).then(({ x, y, middlewareData }) => {
        if (!disposed) Object.assign(floating.style, { left: `${x}px`, top: `${y}px`, visibility: middlewareData.hide?.referenceHidden ? 'hidden' : 'visible' });
      });
    };
    const cleanup = autoUpdate(reference, floating, update);
    return () => { disposed = true; cleanup(); };
  });
  return <button ref={ref} type="button" data-annotation-ui="" className={`annotation-marker${active ? ' is-active' : ''}`}
    style={{ visibility: 'hidden' }} aria-label={`正文批注 ${item.number}`} aria-pressed={active}
    onMouseEnter={() => onPreview(item.id)} onMouseLeave={() => onPreview()}
    onFocus={() => onPreview(item.id)} onBlur={() => onPreview()} onClick={() => onEdit(item.id)}>{item.number}</button>;
}

function AnnotationEditor({ item, anchor, store, sessionId, onClose }: {
  item: DraftAnnotation; anchor?: HTMLElement; store: PromptDraftStore; sessionId: string;
  onClose: (focus?: boolean) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const preview = Boolean(anchor);
  useLayoutEffect(() => {
    const editor = store.getEditor(sessionId);
    editor.view.dispatch(closeHistory(editor.state.tr));
    input.current?.focus({ preventScroll: true });
  }, [item.id, sessionId, store]);
  useLayoutEffect(() => {
    const node = input.current;
    if (node) {
      node.style.height = 'auto';
      node.style.height = `${Math.min(160, Math.max(24, node.scrollHeight + node.offsetHeight - node.clientHeight))}px`;
    }
  }, [item.value.comment]);
  useLayoutEffect(() => {
    const floating = ref.current;
    const root = sourceRoot(item.value.source);
    const target = anchor?.isConnected ? anchor : root;
    if (!floating || !target) return;
    const reference = { contextElement: target, getBoundingClientRect: () =>
      target === root && item.value.source ? sourceRect(root!, item.value.source) : target.getBoundingClientRect() };
    const boundary = target.closest<HTMLElement>('.thread-scroll, .workbench') ?? target;
    let disposed = false;
    const update = () => {
      if (!target.isConnected || !target.getClientRects().length) { floating.style.visibility = 'hidden'; return; }
      void computePosition(reference, floating, { placement: 'top-start', strategy: 'fixed',
        middleware: [offset(10), flip({ boundary, padding: 10 }), shift({ boundary, padding: 10, crossAxis: true }), hide({ boundary })],
      }).then(({ x, y, middlewareData }) => {
        if (!disposed) Object.assign(floating.style, { left: `${x}px`, top: `${y}px`, visibility: middlewareData.hide?.referenceHidden ? 'hidden' : 'visible' });
      });
    };
    const cleanup = autoUpdate(reference, floating, update);
    return () => { disposed = true; cleanup(); };
  });
  return <div ref={ref} role="dialog" aria-label={`编辑批注 ${item.number}`} data-annotation-ui=""
    className={`annotation-editor ${preview ? 'annotation-editor--preview prompt-annotation' : 'annotation-editor--inline'}`} style={{ visibility: 'hidden' }}>
    {preview && <><div className="annotation-editor__header"><b>Annotation {item.number}</b><div>
      <button type="button" aria-label="删除此批注" onClick={() => { store.removeAnnotation(sessionId, item.id); onClose(true); }}><Trash2 size={14} /></button>
      <button type="button" aria-label="关闭批注编辑" onClick={() => onClose(true)}><X size={15} /></button>
    </div></div>
    <AnnotationQuote quote={item.value.quote} /></>}
    <textarea ref={input} rows={1} aria-label="批注内容" placeholder="添加评论…" value={item.value.comment ?? ''}
      onChange={event => store.updateAnnotation(sessionId, item.id, event.target.value)}
      onKeyDown={event => {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
          event.preventDefault(); event.stopPropagation();
          if (event.shiftKey) store.getEditor(sessionId).commands.redo(); else store.getEditor(sessionId).commands.undo();
        }
      }} />
    {!preview && <button type="button" className="annotation-editor__delete" aria-label="删除此批注" title="删除此批注"
      onClick={() => { store.removeAnnotation(sessionId, item.id); onClose(true); }}><Trash2 size={14} /></button>}
  </div>;
}

export function ResponseAnnotations({ sessionId, store, canAnnotate, onLocate, onNotify, children }: {
  sessionId: string; store: PromptDraftStore; canAnnotate: boolean; children: ReactNode; onNotify: MarkdownNotify;
  onLocate?: (entryId: string, signal: AbortSignal) => Promise<void>;
}) {
  useSyncExternalStore(store.subscribe, store.getVersion, store.getVersion);
  const items = store.annotations(sessionId);
  const [menu, setMenu] = useState<{ sessionId: string; selection: MappedSelection }>();
  const [editing, setEditing] = useState<{ sessionId: string; id: string; anchor?: HTMLElement }>();
  const [hovered, setHovered] = useState<string>();
  const active = canAnnotate && editing?.sessionId === sessionId ? items.find(item => item.id === editing.id) : undefined;
  const preview = canAnnotate ? items.find(item => item.id === hovered) : undefined;
  const hideMenu = useCallback(() => setMenu(undefined), []);
  const closeEditor = useCallback((focus = false) => {
    setEditing(undefined); setHovered(undefined);
    if (focus) store.getEditor(sessionId).commands.focus('end', { scrollIntoView: false });
  }, [sessionId, store]);
  const edit = useCallback((id: string, anchor?: HTMLElement) => {
    if (!canAnnotate) return;
    hideMenu(); window.getSelection()?.removeAllRanges();
    setEditing({ sessionId, id, anchor });
  }, [sessionId, hideMenu, canAnnotate]);
  if (editing && (!active || editing.sessionId !== sessionId)) setEditing(undefined);
  if (menu && (!canAnnotate || menu.sessionId !== sessionId)) setMenu(undefined);
  if (hovered && (!canAnnotate || !preview)) setHovered(undefined);
  // Hover and active edit have separate lifetimes. Leaving a marker must never
  // clear the persistent highlight of the annotation currently being edited.
  useLayoutEffect(() => {
    const cleanups: Array<() => void> = [];
    for (const [item, name] of [[active, 'annotation-active'], [preview, 'annotation-hover']] as const) {
      const root = item && sourceRoot(item.value.source);
      if (root && item?.value.source) cleanups.push(showAnnotationHighlight(root, item.value.source, name));
    }
    return () => { cleanups.forEach(cleanup => cleanup()); };
  });
  const locateController = useRef<AbortController | undefined>(undefined);
  const locate = useCallback((source: PromptAnnotationSource | null) => {
    locateController.current?.abort();
    if (!source) { onNotify('来源不可定位', '引用内容仍然完整保留。'); return; }
    const controller = new AbortController(); locateController.current = controller;
    void (async () => {
      try {
        if (!sourceRoot(source)) onNotify('正在定位原文');
        if (onLocate) {
          await onLocate(source.entry_id, controller.signal);
          await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
        } else if (!sourceRoot(source)) throw new Error('来源消息暂时无法读取。');
        if (controller.signal.aborted) return;
        const root = sourceRoot(source);
        if (!root) throw new Error('当前会话中找不到来源消息。');
        highlightAnnotationSource(root, source);
      } catch (error) {
        if (!controller.signal.aborted) onNotify('无法定位原文', `${error instanceof Error ? error.message : '读取失败。'} 可再次点击引用重试。`, 'warning');
      }
    })();
  }, [onLocate, onNotify]);
  useEffect(() => {
    const update = () => {
      if (!canAnnotate) { setMenu(undefined); return; }
      const mapped = mapResponseSelection(window.getSelection());
      if (!mapped) { setMenu(undefined); return; }
      setMenu({ sessionId, selection: mapped });
    };
    const hide = () => setMenu(undefined);
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { hide(); closeEditor(); locateController.current?.abort(); }
    };
    const pointerdown = (event: PointerEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (!target?.closest('[data-annotation-ui], .annotation-chip, .annotation-selection-menu')) closeEditor();
    };
    const copy = (event: ClipboardEvent) => {
      if (event.defaultPrevented || !event.clipboardData) return;
      const target = event.target instanceof Element ? event.target : document.activeElement;
      if (target?.closest('input,textarea,[contenteditable="true"],[role="dialog"]')) return;
      const mapped = mapResponseSelection(window.getSelection());
      if (!mapped) return;
      event.clipboardData.setData('text/plain', mapped.annotation.quote); event.preventDefault();
    };
    const pointerup = (event: PointerEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('.annotation-selection-menu')) return;
      if (target?.closest('[data-annotation-entry]')) update(); else hide();
    };
    document.addEventListener('selectionchange', update);
    document.addEventListener('pointerdown', pointerdown);
    document.addEventListener('pointerup', pointerup);
    document.addEventListener('copy', copy);
    document.addEventListener('keydown', keydown);
    return () => {
      document.removeEventListener('selectionchange', update); document.removeEventListener('pointerup', pointerup);
      document.removeEventListener('pointerdown', pointerdown);
      document.removeEventListener('copy', copy); document.removeEventListener('keydown', keydown);
      locateController.current?.abort();
    };
  }, [sessionId, closeEditor, canAnnotate]);
  return <AnnotationContext.Provider value={locate}><DraftAnnotationContext.Provider value={{ editable: canAnnotate, activeId: active?.id, edit, preview: setHovered }}>
    {children}
    {typeof document !== 'undefined' && createPortal(<>
      {canAnnotate && items.filter(item => item.value.source).map(item => <AnnotationMarker key={item.id} item={item}
        siblings={items.filter(other => other.value.source?.entry_id === item.value.source!.entry_id).sort((a, b) => a.value.source!.start - b.value.source!.start || a.number - b.number)}
        active={item.id === active?.id} onEdit={id => edit(id)} onPreview={setHovered} />)}
      {active && <AnnotationEditor key={`${active.id}:${editing?.anchor ? 'preview' : 'inline'}`} item={active} anchor={editing?.anchor} store={store} sessionId={sessionId} onClose={closeEditor} />}
      {canAnnotate && menu?.sessionId === sessionId && <SelectionMenu selection={menu.selection} onHide={hideMenu} onAdd={() => {
        if (!canAnnotate) return;
        const current = mapResponseSelection(window.getSelection());
        if (!current || current.root !== menu.selection.root || current.annotation.quote !== menu.selection.annotation.quote
          || current.annotation.source?.start !== menu.selection.annotation.source?.start || current.annotation.source?.end !== menu.selection.annotation.source?.end) { hideMenu(); return; }
        const id = store.addAnnotation(sessionId, current.annotation);
        edit(id);
      }} />}
    </>, document.body)}
  </DraftAnnotationContext.Provider></AnnotationContext.Provider>;
}
