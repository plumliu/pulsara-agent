import { ArrowLeft, ArrowRight, Copy, ExternalLink, Globe, RotateCw, X } from 'lucide-react';
import { createElement, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { browserGuestSource } from '../../desktop/src/browser-contract';
import { desktopHost, writeClipboardText } from '../lib/desktop-host';
import { browserAddressUrl, type BrowserTabRecord, type BrowserTabs } from '../lib/browser-tabs';
import { useSessionCanvasWindow } from './session-canvas';

export function browserAddressActions(record: BrowserTabRecord, notify: (error: string) => void) {
  const url = record.state?.url;
  const host = desktopHost();
  if (!url || !host) return;
  const report = (error: unknown) => notify(error instanceof Error ? error.message : String(error));
  return {
    copy: () => { if (record.state?.url === url) void writeClipboardText(url).catch(report); },
    openExternal: () => { if (record.state?.url === url) void host.openExternal(url).catch(report); },
  };
}

export function BrowserPanel({ record, tabs, isOpen }: { record: BrowserTabRecord; tabs: BrowserTabs; isOpen: boolean }) {
  const state = record.state;
  const [draft, setDraft] = useState(state?.pendingUrl || state?.url || record.requestedUrl || '');
  const [editing, setEditing] = useState(false);
  const [message, setMessage] = useState('');
  const guest = useRef<HTMLElement>(null);
  const address = useRef<HTMLInputElement>(null);
  const focusBlank = useRef(record.focusAddressOnOpen === true);
  const host = desktopHost();
  const addressActions = browserAddressActions(record, setMessage);
  const actualUrl = state?.pendingUrl || state?.url || record.requestedUrl || '';
  const activate = useSessionCanvasWindow()?.activate;
  useEffect(() => { if (!editing) setDraft(actualUrl); }, [actualUrl, editing]);
  useEffect(() => {
    if (actualUrl) focusBlank.current = false;
    if (isOpen && focusBlank.current) { focusBlank.current = false; address.current?.focus(); }
  }, [isOpen, actualUrl]);
  const failed = state?.error?.message || (!state ? record.error : undefined);
  const notice = message || record.notice || (state ? record.error : undefined);
  useLayoutEffect(() => {
    const element = guest.current;
    if (!isOpen || !element || !activate) return;
    element.addEventListener('focus', activate);
    return () => element.removeEventListener('focus', activate);
  }, [activate, isOpen, state?.instanceId, state?.crashed]);
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
      <button type="button" title="复制地址" aria-label="复制网页地址" disabled={!addressActions} onClick={addressActions?.copy}><Copy size={14} /></button>
      <button type="button" title="在系统浏览器打开" aria-label="在系统浏览器打开" disabled={!addressActions} onClick={addressActions?.openExternal}><ExternalLink size={14} /></button>
    </form>
    {notice && <div className="browser-notice" role="status">{notice}</div>}
    {record.popup && <div className="browser-notice"><button type="button" onClick={() => { const url = record.popup; record.popup = undefined; if (url) tabs.open(url, { newTab: true }); }}>有新页面，点击打开</button></div>}
    <div className="browser-viewport">
      {state && !state.crashed && createElement('webview', {
        key: state.instanceId, ref: guest, className: 'browser-guest', 'aria-label': '嵌入网页',
        src: browserGuestSource(state.instanceId), hidden: !!failed || !actualUrl,
      })}
      {state?.crashed || failed ? <div className="browser-empty" role="status"><strong>{state?.crashed ? '网页已停止响应' : '无法打开网页'}</strong><p>{failed}</p><small>{state?.error?.url || actualUrl || draft}</small><button type="button" onClick={() => { if (!state && !draft) void tabs.create(record); else navigate(state?.error?.url || actualUrl || draft); }}>重试</button></div>
      : !actualUrl ? <div className="browser-empty"><Globe size={28}/><strong>打开网页</strong><p>在上方输入网址</p></div> : null}
    </div>
  </section>;
}
