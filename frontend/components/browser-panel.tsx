import { ArrowLeft, ArrowRight, Copy, ExternalLink, Globe, RotateCw, X } from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { desktopHost, writeClipboardText } from '../lib/desktop-host';
import { browserAddressUrl, type BrowserTabRecord, type BrowserTabs } from '../lib/browser-tabs';
import { presentBrowser } from '../lib/browser-presentation';

export function BrowserPanel({ record, tabs, isOpen }: { record: BrowserTabRecord; tabs: BrowserTabs; isOpen: boolean }) {
  const state = record.state;
  const [draft, setDraft] = useState(state?.pendingUrl || state?.url || record.requestedUrl || '');
  const [editing, setEditing] = useState(false);
  const [message, setMessage] = useState('');
  const slot = useRef<HTMLDivElement>(null);
  const address = useRef<HTMLInputElement>(null);
  const focusBlank = useRef(record.focusAddressOnOpen === true);
  const host = desktopHost();
  const actualUrl = state?.pendingUrl || state?.url || record.requestedUrl || '';
  const hasDocument = Boolean(state?.url || state?.pendingUrl);
  useEffect(() => { if (!editing) setDraft(actualUrl); }, [actualUrl, editing]);
  useEffect(() => { if (!actualUrl && isOpen && focusBlank.current) address.current?.focus(); if (actualUrl) focusBlank.current = false; }, [record.id, isOpen, actualUrl]);
  const failed = state?.error?.message || (!state ? record.error : undefined);
  const notice = message || record.notice || (state ? record.error : undefined);
  useLayoutEffect(() => {
    if (!host?.browser || !state?.instanceId || state.crashed || !slot.current || failed || !hasDocument) return;
    return presentBrowser(host.browser, state.instanceId, slot.current, setMessage);
  }, [host, state?.instanceId, state?.crashed, failed, hasDocument]);
  const command = (kind: 'back' | 'forward' | 'reload' | 'stop') => {
    setMessage('');
    if (state) void host?.browser.command({ instanceId: state.instanceId, kind }).catch(error => setMessage(String(error.message || error)));
  };
  const navigate = (url: string) => { setEditing(false); setMessage(''); void tabs.navigate(record, url); };
  return <section className="browser-panel" aria-label="网页查看器">
    <form className="browser-toolbar" onSubmit={event => { event.preventDefault(); try { navigate(browserAddressUrl(draft)); } catch (error) { setMessage((error as Error).message); } }}>
      <button type="button" title="后退" aria-label="网页后退" disabled={!state?.canGoBack || state.crashed} onClick={() => command('back')}><ArrowLeft size={14} /></button>
      <button type="button" title="前进" aria-label="网页前进" disabled={!state?.canGoForward || state.crashed} onClick={() => command('forward')}><ArrowRight size={14} /></button>
      <button type="button" title={state?.loading ? '停止' : '刷新'} aria-label={state?.loading ? '停止网页加载' : '刷新网页'} disabled={!state || state.crashed} onClick={() => command(state?.loading ? 'stop' : 'reload')}>{state?.loading ? <X size={14} /> : <RotateCw size={14} />}</button>
      <input ref={address} aria-label="网页地址" spellCheck={false} value={editing ? draft : actualUrl || draft} onFocus={() => { setDraft(actualUrl || draft); setEditing(true); }} onChange={event => { setDraft(event.target.value); setEditing(true); }} onBlur={() => setEditing(false)} onKeyDown={event => { if (event.key === 'Escape') { setDraft(actualUrl); setEditing(false); event.currentTarget.blur(); } }} />
      <button type="button" title="复制地址" aria-label="复制网页地址" disabled={!state?.url} onClick={() => { void writeClipboardText(state?.url || '').catch(error => setMessage(error.message)); }}><Copy size={14} /></button>
      <button type="button" title="在系统浏览器打开" aria-label="在系统浏览器打开" disabled={!state?.url} onClick={() => { void host?.openExternal(state?.url || '').catch(error => setMessage(error.message)); }}><ExternalLink size={14} /></button>
    </form>
    {notice && <div className="browser-notice" role="status">{notice}</div>}
    {record.popup && <div className="browser-notice"><button type="button" onClick={() => { const url = record.popup; record.popup = undefined; if (url) tabs.open(url, { newTab: true }); }}>有新页面，点击打开</button></div>}
    <div ref={slot} className="browser-viewport">
      {state?.crashed || failed ? <div className="browser-empty" role="status"><strong>{state?.crashed ? '网页已停止响应' : '无法打开网页'}</strong><p>{failed}</p><small>{state?.error?.url || actualUrl || draft}</small><button type="button" onClick={() => { if (!state && !draft) void tabs.create(record); else navigate(state?.error?.url || actualUrl || draft); }}>重试</button></div>
      : !actualUrl ? <div className="browser-empty"><Globe size={28}/><strong>打开网页</strong><p>在上方输入网址。</p></div> : null}
    </div>
  </section>;
}
