import type { Editor, Range } from '@tiptap/core';
import { closeHistory } from '@tiptap/pm/history';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Info, LoaderCircle, MessageSquareText, X } from 'lucide-react';
import { createPortal } from 'react-dom';
import { InteractionCardHeader } from './interaction-card-header';
import type { McpPrompt, McpPromptPreview, PreviewMcpPrompt } from '../lib/mcp-prompts';

interface Selection { prompt: McpPrompt; range: Range; doc: Editor['state']['doc'] }
type Preview = McpPromptPreview | {state: 'loading'} | {state: 'incomplete'; fields: string[]};

export function useMcpPromptPanel(editor: Editor, unavailable: boolean, previewPrompt?: PreviewMcpPrompt) {
  const [selection, setSelection] = useState<Selection>();
  const [args, setArgs] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<Preview>({state: 'loading'});
  const [composing, setComposing] = useState(false);
  const [retry, setRetry] = useState<object>({});
  const current = useRef(selection);
  current.current = selection;
  const requestPreview = useRef(previewPrompt);
  requestPreview.current = previewPrompt;
  const close = useCallback(() => {
    current.current = undefined;
    setSelection(undefined);
  }, []);
  const insert = useCallback((value: Selection, text: string) => {
    if (editor.isDestroyed || current.current !== value || editor.state.doc !== value.doc) { close(); return; }
    const content = text.split('\n').flatMap((line, index) => [
      ...(index ? [{type: 'hardBreak'}] : []), ...(line ? [{type: 'text', text: line}] : []),
    ]);
    editor.view.dispatch(closeHistory(editor.state.tr));
    editor.chain().insertContentAt(value.range, content).focus().run();
    editor.view.dispatch(closeHistory(editor.state.tr));
    close();
  }, [close, editor]);
  const select = useCallback((prompt: McpPrompt, range: Range) => {
    if (unavailable || !requestPreview.current || editor.isDestroyed) return;
    const value = {prompt, range, doc: editor.state.doc};
    current.current = value;
    setArgs({}); setPreview({state: 'loading'}); setSelection(value);
  }, [editor, unavailable]);
  useEffect(() => { if (unavailable) close(); }, [unavailable, close]);
  useEffect(() => {
    const changed = () => { if (current.current && editor.state.doc !== current.current.doc) close(); };
    editor.on('update', changed);
    return () => { editor.off('update', changed); };
  }, [editor, close]);
  useEffect(() => {
    if (!selection || unavailable) return;
    const controller = new AbortController();
    const missing = selection.prompt.arguments.filter(a => a.required && !args[a.name]?.trim()).map(a => a.name);
    if (missing.length) { setPreview({state: 'incomplete', fields: missing}); return; }
    setPreview({state: 'loading'});
    if (composing) return;
    const timer = window.setTimeout(() => {
      const values = Object.fromEntries(selection.prompt.arguments
        .filter(a => a.required || args[a.name] !== undefined && args[a.name] !== '')
        .map(a => [a.name, args[a.name] ?? '']));
      void requestPreview.current!(selection.prompt.target, values, controller.signal).then(result => {
        if (controller.signal.aborted || current.current !== selection) return;
        if (result.state === 'ready' && !selection.prompt.arguments.length) insert(selection, result.text);
        else setPreview(result);
      }, error => {
        if (!controller.signal.aborted && current.current === selection)
          setPreview({state: 'failed', message: error instanceof Error ? error.message : '提示词读取失败。'});
      });
    }, selection.prompt.arguments.length ? 300 : 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [selection, args, composing, retry, unavailable, insert]);
  const change = (name: string, value: string) => {
    setPreview({state: 'loading'});
    setArgs(current => ({...current, [name]: value}));
  };
  const closeAndFocus = () => { close(); if (!editor.isDestroyed) editor.commands.focus(); };
  const panel = selection && !unavailable ? selection.prompt.arguments.length === 0 ? <section
    className="interaction-card mcp-prompt-instant" role="status" aria-busy={preview.state === 'loading'}>
    <span>{preview.state === 'loading' ? '正在读取提示词…' : 'message' in preview ? preview.message : ''}</span>
    <div className="interaction-actions"><button type="button" onClick={closeAndFocus}>取消</button>
      {preview.state !== 'loading' && <button type="button" onClick={() => setRetry({})}>重试</button>}
    </div></section> : <section className="interaction-card mcp-prompt-panel" role="dialog"
    aria-modal="false" aria-label={`提示词 ${selection.prompt.name}`} aria-busy={preview.state === 'loading'}
    onKeyDown={event => { event.stopPropagation(); if (event.key === 'Escape') { event.preventDefault(); closeAndFocus(); } }}>
    <InteractionCardHeader icon={MessageSquareText} title={selection.prompt.name} tone="blue" />
    <button type="button" className="mcp-prompt-panel__close" aria-label="关闭提示词" onClick={closeAndFocus}><X size={17} /></button>
    <div className="mcp-prompt-panel__body">
      <div className="mcp-prompt-panel__source">{selection.prompt.server_name}</div>
      {selection.prompt.arguments.map((argument, index) => <div className="mcp-prompt-field" key={argument.name}>
        <label>{argument.name}{argument.required ? '（必填）' : ''}
          <textarea value={args[argument.name] ?? ''} autoFocus={index === 0} rows={1}
            onCompositionStart={() => {setComposing(true); setPreview({state: 'loading'});}}
            onCompositionEnd={() => setComposing(false)}
            onChange={event => { event.currentTarget.style.height = 'auto'; event.currentTarget.style.height = `${event.currentTarget.scrollHeight}px`; change(argument.name, event.target.value); }} />
        </label>
        {argument.description && <details><summary aria-label={`${argument.name}说明`}><Info size={15} /></summary><p>{argument.description}</p></details>}
      </div>)}
      <div className="mcp-prompt-panel__preview-title">提示词预览</div>
      <div className="plan-draft-body mcp-prompt-panel__preview" role={preview.state === 'ready' ? undefined : 'status'}>
        {preview.state === 'ready' ? <div className="mcp-prompt-panel__text">{preview.text}</div>
          : preview.state === 'incomplete' ? `请填写 ${preview.fields.join('、')}`
          : preview.state === 'loading' ? <span className="mcp-prompt-panel__loading"><LoaderCircle size={16} />正在更新…</span>
          : <span className="mcp-prompt-panel__error">{preview.message}</span>}
      </div>
    </div>
    <div className="interaction-actions interaction-actions--plan">
      <button type="button" onClick={closeAndFocus}>取消</button>
      {!['ready', 'loading', 'incomplete'].includes(preview.state) && <button type="button" onClick={() => setRetry({})}>重试</button>}
      {selection.prompt.arguments.length > 0 && <button type="button" className="is-primary"
        disabled={preview.state !== 'ready' || composing} onClick={() => {if (preview.state === 'ready') insert(selection, preview.text);}}><Check size={14} />插入输入框</button>}
    </div>
  </section> : null;
  const host = !editor.isDestroyed ? editor.view.dom.closest('.composer-frame')?.querySelector('[data-prompt-panel-host]') : null;
  return {select, active: Boolean(selection) && !unavailable, panel: panel && host ? createPortal(panel, host) : panel};
}
