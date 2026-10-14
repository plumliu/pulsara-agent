'use client';

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChevronRight, CircleAlert, FileCode2, LoaderCircle, Pencil, Plus, Save, ShieldCheck, Workflow, X } from 'lucide-react';
import type { HookConfigText, HookMutation, HookSource, UserHooksApi } from '../lib/hooks-api';
import type { MarkdownNotify } from './markdown-body';
import { HookSourceReview } from './hook-source-review';
import {HookConfigForm} from './hook-config-form';
import {applyHookRule, hookRuleDraft, readHookForm, type HookFormGroup} from '../lib/hook-config-form';
import events from '../lib/hook-form-events.json';

type HookDialogState = {config: HookConfigText; mode: 'json'} | {config: HookConfigText; mode: 'rule'; group?: HookFormGroup} | {source: HookSource};
const eventLabel = (event: string) => events.find(item => item.value === event)?.label ?? event;
const commandText = (value: unknown): string => value && typeof value === 'object' && 'command' in value && typeof value.command === 'string' ? value.command : '未支持的命令类型';

const trustLabels: Record<string, string> = {TRUSTED: '已信任', UNTRUSTED: '待信任', MODIFIED: '需重新审阅', UNAVAILABLE: '读取失败'};
const name = (source: HookSource) => source.plugin_id ?? '本地 Hooks';

function sourceStatus(source: HookSource) {
  if (source.exists === false) return {label: '未配置', style: 'status-configured'};
  if (source.source_disposition !== 'COMPLETE' || source.trust_state === 'UNAVAILABLE') return {label: '读取失败', style: 'status-failed'};
  return {label: trustLabels[source.trust_state] ?? '待信任', style: source.trust_state === 'TRUSTED' ? 'status-ready' : source.trust_state === 'MODIFIED' ? 'status-updating' : 'status-configured'};
}

// A textarea exposes LF; retain the original bytes outside the edited range.
function preserveNewlines(original: string, value: string): string {
  const normalized = original.replace(/\r\n?/g, '\n');
  if (normalized === value) return original;
  let start = 0, tail = 0;
  while (start < Math.min(normalized.length, value.length) && normalized[start] === value[start]) start++;
  while (tail < Math.min(normalized.length, value.length) - start && normalized[normalized.length - 1 - tail] === value[value.length - 1 - tail]) tail++;
  const rawOffset = (offset: number) => {
    let raw = 0;
    for (let i = 0; i < offset; i++, raw++) if (original[raw] === '\r' && original[raw + 1] === '\n') raw++;
    return raw;
  };
  const newline = original.match(/\r\n|\r|\n/)?.[0] ?? '\n';
  return original.slice(0, rawOffset(start)) + value.slice(start, value.length - tail).replace(/\n/g, newline) + original.slice(rawOffset(normalized.length - tail));
}

export function HooksPanel({api, query, refresh, inventory, onCountChange, onNotify}: {api: UserHooksApi; query: string; refresh: number; inventory?: object; onCountChange?: (count: number | undefined) => void; onNotify: MarkdownNotify}) {
  const [catalog, setCatalog] = useState<{sources: HookSource[]; details: string[]}>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [opening, setOpening] = useState(false);
  const [expanded, setExpanded] = useState<string>();
  const [dialog, setDialog] = useState<HookDialogState>();
  const [local, setLocal] = useState<{config?: HookConfigText; loading: boolean; error: string}>({loading: true, error: ''});
  const live = useRef(true);
  const openingRef = useRef(false);
  useEffect(() => {live.current = true; return () => {live.current = false;};}, []);
  useEffect(() => {
    const controller = new AbortController();
    void api.list(controller.signal).then(value => {if (!controller.signal.aborted) {
      setCatalog(value); setError('');
      onCountChange?.(value.details.length ? undefined : value.sources.reduce((total, source) => total + (source.enabled && source.source_disposition === 'COMPLETE' && source.trust_state === 'TRUSTED' ? source.definition_count : 0), 0));
    }}, cause => {
      if (!controller.signal.aborted) {setError(cause instanceof Error ? cause.message : '暂时无法读取 Hook 来源。'); onCountChange?.(undefined);}
    }).finally(() => {if (!controller.signal.aborted) setLoading(false);});
    return () => controller.abort();
  }, [api, refresh, attempt, inventory, onCountChange]);
  useEffect(() => {
    if (expanded !== 'local') return;
    const controller = new AbortController();
    void api.config(controller.signal).then(config => {
      if (!controller.signal.aborted) setLocal({config, loading: false, error: ''});
    }, cause => {
      if (!controller.signal.aborted) setLocal({loading: false, error: cause instanceof Error ? cause.message : '暂时无法读取规则。'});
    });
    return () => controller.abort();
  }, [api, expanded, refresh, attempt]);
  const open = async (source: HookSource, mode: 'json' | 'rule' | 'trust') => {
    if (openingRef.current) return;
    openingRef.current = true;
    setOpening(true);
    try {
      const next: HookDialogState = mode === 'trust' ? {source: await api.review(source.plugin_id)} : {config: await api.config(), mode};
      if (mode === 'rule' && 'config' in next && next.config.exists && readHookForm(next.config.text).error) throw new Error('配置无法载入，请通过 JSON 入口修正。');
      if (live.current) setDialog(next);
    } catch (cause) { if (live.current) onNotify('未能读取 Hooks', cause instanceof Error ? cause.message : '请刷新后重试。', 'warning'); }
    finally {openingRef.current = false; if (live.current) setOpening(false);}
  };
  const sources = catalog?.sources.filter(source => `${name(source)} ${source.path}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <>
    {error && <div className="capability-page-notice" role="alert"><CircleAlert size={15}/><span>{error}</span></div>}
    {catalog?.details.map((detail, i) => <div className="capability-page-notice" role="alert" key={i}><CircleAlert size={15}/><span>{detail}</span></div>)}
    {!catalog && loading && <div className="capability-page-empty" role="status"><LoaderCircle size={18}/>正在读取 Hooks…</div>}
    {sources?.map(source => {
      const key = source.plugin_id ? `plugin:${source.plugin_id}` : 'local';
      const isOpen = expanded === key;
      const status = sourceStatus(source);
      return <article className={`capability-list-item${isOpen ? ' is-expanded' : ''}`} key={key}>
        <div className={`capability-list-row capability-list-row--connection${!source.plugin_id ? ' hook-source-row' : ''}`} onClick={() => setExpanded(isOpen ? undefined : key)}>
          <span className="capability-list-icon"><Workflow size={18}/></span>
          <span className="capability-list-copy"><strong>{name(source)}</strong><small className="hook-source-path" title={source.path}>{source.path}{!source.enabled && ' · 已关闭'}</small></span>
          {!source.plugin_id && <button className="secondary-ghost hook-source-config" aria-label="查看和编辑 Hooks JSON" title="查看和编辑 JSON 配置" disabled={opening || source.editable === false} onClick={event => {event.stopPropagation(); void open(source, 'json');}}><FileCode2 size={14}/>配置</button>}
          <span className={`capability-live-status ${status.style}`}><i/>{status.label}</span>
          <button className="capability-row-more" aria-label={`查看 ${name(source)}`} aria-expanded={isOpen}><ChevronRight className={isOpen ? 'is-open' : ''} size={14}/></button>
        </div>
        <div className={`capability-drawer${isOpen ? ' is-open' : ''}`} aria-hidden={!isOpen} inert={!isOpen}><div className="capability-drawer__inner"><div className="capability-list-detail">
          <div className="capability-plugin-summary">
            <div className="capability-detail-stats">
              <span><strong>{source.definition_count}</strong> 钩子</span>
              <span>{source.plugin_id ? '来自插件' : '用户配置'}</span>
              {source.exists !== false && <span>{source.enabled ? '已开启' : '已关闭'}</span>}
            </div>
            <div className="capability-detail-actions">
              {!source.plugin_id && <button className="secondary-ghost" disabled={opening || source.editable === false || local.loading || Boolean(local.error) || Boolean(local.config && readHookForm(local.config.exists ? local.config.text : '{"hooks":{}}').error)} onClick={() => void open(source, 'rule')}><Plus size={13}/>添加配置</button>}
              {source.can_trust && <button className="secondary-ghost" disabled={opening} onClick={() => void open(source, 'trust')}><ShieldCheck size={13}/>审阅并信任</button>}
            </div>
          </div>
          {source.plugin_id ? <>
            <div className="capability-tool-grid">{source.definitions?.map((rule, index) => <div key={index}><code title={rule.event}>{eventLabel(rule.event)}</code><span className="hook-rule-command">{rule.command}{rule.matcher && <small>匹配：{rule.matcher}</small>}</span></div>)}</div>
          </> : isOpen && <>
            {local.loading && !local.config && <p role="status">正在读取规则…</p>}
            {local.error && <p className="capability-inline-error" role="alert">{local.error}</p>}
            {local.config && <LocalHookRules config={local.config} disabled={local.loading || opening || source.editable === false} onEdit={group => setDialog({config: local.config!, mode: 'rule', group})}/>}
          </>}
          {source.diagnostics.map((diagnostic, i) => <p className="capability-inline-error" key={i}>{diagnostic.message}</p>)}
        </div></div></div>
      </article>;
    })}
    {catalog && !sources?.length && !catalog.details.length && <div className="capability-page-empty">没有匹配的 Hook 来源。</div>}
    {dialog && <HookDialog api={api} initial={dialog} onClose={() => setDialog(undefined)} onSaved={(outcome) => {
      setAttempt(value => value + 1);
      const message = 'source' in dialog && !dialog.source.enabled ? '已信任，来源仍关闭。' : 'source' in dialog ? '已信任。' : '配置已保存，信任与来源开关保持原状态。';
      onNotify('Hooks 已更新', message + (outcome.pending_sessions ? '活动会话将在下次请求前的安全时机载入。' : ''), 'success');
    }}/>}
  </>;
}

function LocalHookRules({config, disabled, onEdit}: {config: HookConfigText; disabled: boolean; onEdit: (group: HookFormGroup) => void}) {
  const form = readHookForm(config.exists ? config.text : '{"hooks":{}}');
  if (form.error) return <p className="capability-inline-error">配置无法载入规则列表，请通过 JSON 入口修正。</p>;
  return <>
    <div className="capability-tool-grid hook-rule-list">{form.groups.map((group, index) => <div key={`${group.event}:${group.index}`}>
      <code title={group.event}>{eventLabel(group.event)}</code>
      <span className="hook-rule-command">{group.handlers.map((command, i) => <span key={i}>{commandText(command)}</span>)}{typeof group.value.matcher === 'string' && group.value.matcher && <small>匹配：{group.value.matcher}</small>}</span>
      <button className="capability-row-more" aria-label={`编辑规则 ${index + 1}`} title="编辑规则" disabled={disabled} onClick={() => onEdit(group)}><Pencil size={13}/></button>
    </div>)}</div>
    {!form.groups.length && !form.unrepresented && <p className="capability-detail-note">暂无规则</p>}
    {form.unrepresented && <p className="capability-detail-note">部分条目无法展示，请通过 JSON 入口查看。</p>}
  </>;
}

function HookDialog({api, initial, onClose, onSaved}: {api: UserHooksApi; initial: HookDialogState; onClose: () => void; onSaved: (outcome: HookMutation) => void}) {
  const [config, setConfig] = useState('config' in initial ? initial.config : undefined);
  const source = 'source' in initial ? initial.source : undefined;
  const rule = 'mode' in initial && initial.mode === 'rule';
  const group = rule ? initial.group : undefined;
  const [text, setText] = useState(rule ? hookRuleDraft(group) : config?.exists ? config.text : '{"hooks":{}}');
  const title = config ? rule ? group ? '编辑 Hook 配置' : '添加 Hook 配置' : '本地 Hooks JSON' : '审阅并信任 Hooks';
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const active = useRef(false);
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const application = document.querySelector<HTMLElement>('main.pulsara-shell');
    const wasInert = application?.inert ?? false;
    const opener = document.activeElement as HTMLElement | null;
    if (application) application.inert = true;
    (dialog.current?.querySelector<HTMLElement>(':is(input,select,textarea):not(:disabled)') ?? dialog.current?.querySelector<HTMLElement>('button'))?.focus();
    return () => {if (application) application.inert = wasInert; opener?.focus();};
  }, []);
  const submit = async () => {
    if (active.current || (!config && !reviewed)) return;
    if (config) {
      const invalid = [...(dialog.current?.querySelectorAll<HTMLElement>('input,select,textarea') ?? [])].find(control => !(control as HTMLElement & {checkValidity: () => boolean}).checkValidity());
      if (invalid) {
        for (let details = invalid.closest('details'); details; details = details.parentElement?.closest('details') ?? null) details.open = true;
        (invalid as HTMLElement & {reportValidity: () => boolean}).reportValidity();
        return;
      }
    }
    active.current = true; setBusy(true); setError('');
    try {
      const outcome = config ? await api.save(config, rule ? applyHookRule(config.exists ? config.text : '{"hooks":{}}', text, group) : text) : await api.trust(source!);
      if (outcome.status !== 'APPLIED') {setError(outcome.message ?? '已写入，请刷新确认。'); return;}
      onSaved(outcome); onClose();
    } catch (cause) {setError(cause instanceof Error ? cause.message : '操作未完成，请刷新确认。');}
    finally {active.current = false; setBusy(false);}
  };
  return createPortal(<div className="capability-add-panel" ref={dialog} role="dialog" aria-modal="true" aria-label={title} aria-busy={busy}
    onKeyDown={event => {
      if (event.key === 'Escape') {event.preventDefault(); event.stopPropagation(); if (!active.current) onClose();}
      if (event.key !== 'Tab') return;
      const controls = [...(dialog.current?.querySelectorAll<HTMLElement>(':is(button,input,select,textarea,summary):not(:disabled)') ?? [])];
      if (!controls.length) {event.preventDefault(); return;}
      const first = controls[0], last = controls.at(-1)!;
      if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last.focus();}
      else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first.focus();}
    }}>
    <div className="capability-add-panel__backdrop" onClick={() => {if (!active.current) onClose();}}/>
    <section><header className="hook-dialog-header"><h2>{title}</h2><button aria-label="关闭" disabled={busy} onClick={onClose}><X size={16}/></button></header>
      <div className="capability-dialog-body">
        {config ? <>
          <p className="capability-detail-note">{config.path}</p>
          {rule ? <HookConfigForm text={text} disabled={busy} onChange={setText}/> : <label className="capability-field"><span>hooks.json</span><textarea className="hook-config-editor" aria-label="Hook 配置 JSON" value={text} disabled={busy} spellCheck={false} onChange={event => {const value = event.target.value; setText(previous => preserveNewlines(previous, value));}}/><small>保存后更新规则列表；保存配置不会授予信任。</small></label>}
        </> : <>
          <HookSourceReview hook={source!}/>
          <label className="capability-choice"><input type="checkbox" checked={reviewed} disabled={busy} onChange={event => setReviewed(event.target.checked)}/><span>我已审阅并信任这些 Hook 定义</span></label>
        </>}
        {error && <p className="capability-inline-error" role="alert">{error}</p>}
        {error && config && !rule && <button className="secondary-ghost" disabled={busy} onClick={() => void (async () => {
          if (active.current) return;
          active.current = true;
          setBusy(true);
          try {const value = await api.config(); setConfig(value); setText(value.exists ? value.text : '{"hooks":{}}'); setError('');}
          catch (cause) {setError(cause instanceof Error ? cause.message : '无法重新读取。');}
          finally {active.current = false; setBusy(false);}
        })()}>重新读取，替换草稿</button>}
      </div>
      <footer><button className="secondary-action" disabled={busy} onClick={onClose}>取消</button><button className="capability-primary" disabled={busy || (!config && !reviewed)} onClick={() => void submit()}>{busy ? <LoaderCircle size={14}/> : <Save size={14}/>} {config ? '保存配置' : '确认并信任'}</button></footer>
    </section>
  </div>, document.body);
}
