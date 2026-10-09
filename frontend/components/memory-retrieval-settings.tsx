'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Bot, Brain, Check, LoaderCircle, Pencil, RefreshCw, Trash2, TriangleAlert, X } from 'lucide-react';
import { usePromptHover } from '../lib/prompt-hover';
import type { MemoryModelSlot, MemoryRetrievalSettings, RetrievalConnection,
  RetrievalModelInput, RetrievalShape, RuntimeAdapter } from '../lib/runtime-adapter';

const names = { embedding: 'Embedding', decision: 'Jev-like Decision', rerank: 'Rerank' };
const presets = {
  jev: { shape: 'system_one' as const, endpoint: 'https://api.typesafe.ai/v1/systemone' },
  openrouter: { shape: 'system_one' as const, endpoint: 'https://openrouter.ai/api/v1/systemone' },
  openai: { shape: 'openai_decisions' as const, endpoint: 'https://api.openai.com/v1/decisions' },
};
type Notify = (title: string, detail?: string, tone?: 'neutral' | 'success' | 'warning') => void;
const isConfigured = (connection: RetrievalConnection | null) => Boolean(connection && (connection.authentication === 'none' || connection.credential_configured));
const shapeLabels: Record<RetrievalShape, string> = {
  openai_embedding: 'Embedding · 1024 维', flat_rerank: '标准 Rerank', nested_rerank: '嵌套 Rerank',
  system_one: 'System One', openai_decisions: 'OpenAI Decisions',
};

function MissingModelWarning({ channel, connection }: { channel: 'Embedding' | '重排'; connection: RetrievalConnection | null }) {
  const { trigger, details, position, show, hide, keep } = usePromptHover(260);
  const id = useId();
  const text = `开关已打开，但${connection ? '尚未配置 API key' : '尚未配置模型'}，${channel === 'Embedding' ? '语义检索' : '重排'}暂未启用。`;
  return <>
    <button ref={trigger} type="button" className="retrieval-warning" aria-label={`${channel} 配置不完整`}
      aria-describedby={position ? id : undefined} onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide} onClick={show}>
      <TriangleAlert size={16} aria-hidden="true" />
    </button>
    {position && createPortal(<span ref={details} id={id} role="tooltip" className="retrieval-warning-tooltip"
      style={{ left: position.left, top: position.top, translate: position.above ? '0 -100%' : undefined }}
      onMouseEnter={keep} onMouseLeave={hide}>{text}</span>, document.body)}
  </>;
}

function RetrievalModelCard({ slot, connection, busy, deleting, onEdit, onDelete, onCancelDelete, onConfirmDelete }: {
  slot: MemoryModelSlot; connection: RetrievalConnection | null; busy: boolean; deleting: boolean;
  onEdit: () => void; onDelete: () => void; onCancelDelete: () => void; onConfirmDelete: () => void;
}) {
  const title = connection ? `${new URL(connection.endpoint).hostname} · ${connection.model_id}` : '未配置';
  return <div className="model-config-card">
    <span className="model-config-card__icon"><Bot size={15} /></span>
    <span><strong title={title}>{title}</strong>{connection && <>
      <small>{shapeLabels[connection.shape]}</small><code title={connection.model_id}>{connection.model_id}</code>
    </>}</span>
    <div className="model-config-card__actions">
      {connection && <span className={`credential-state credential-state--${isConfigured(connection) ? 'present' : 'missing'}`}>
        <i />{connection.authentication === 'none' ? '无需认证' : connection.credential_configured ? '密钥已配置' : '缺少密钥'}
      </span>}
      {deleting ? <div className="model-delete-confirmation">
        <button type="button" disabled={busy} onClick={onCancelDelete}>取消</button>
        <button type="button" className="subtle-danger" aria-label={`确认删除 ${names[slot]}`} disabled={busy} onClick={onConfirmDelete}><Trash2 size={13} />确认删除</button>
      </div> : <div className="model-config-card__buttons">
        <button type="button" className="model-edit-trigger" aria-label={`配置 ${names[slot]}`} disabled={busy} onClick={onEdit}><Pencil size={13} />{connection ? '修改' : '配置'}</button>
        {connection && <button type="button" className="model-delete-trigger subtle-danger" aria-label={`删除 ${names[slot]}`} disabled={busy} onClick={onDelete}><Trash2 size={13} />删除</button>}
      </div>}
    </div>
  </div>;
}

export function MemoryRetrievalSettingsPanel({ adapter, value, onChanged, onNotify }: {
  adapter: RuntimeAdapter; value: MemoryRetrievalSettings;
  onChanged: (value: MemoryRetrievalSettings) => void; onNotify: Notify;
}) {
  const [editor, setEditor] = useState<{ slot: MemoryModelSlot; activate: boolean }>();
  const [busy, setBusy] = useState(false);
  const [deleteCandidate, setDeleteCandidate] = useState<MemoryModelSlot>();
  // Drawer intent is local; only complete, server-accepted settings enable runtime calls.
  const [embeddingOpen, setEmbeddingOpen] = useState(value.embedding_enabled);
  const [rankingOpen, setRankingOpen] = useState(value.ranking_mode !== 'off');
  const [preferredRanking, setPreferredRanking] = useState<'decision' | 'rerank'>(() =>
    value.ranking_mode !== 'off' ? value.ranking_mode : isConfigured(value.decision) || !isConfigured(value.rerank) ? 'decision' : 'rerank');
  const adopt = (next: MemoryRetrievalSettings) => {
    if (next.ranking_mode !== 'off') setPreferredRanking(next.ranking_mode);
    onChanged(next);
  };
  const update = async (operation: () => Promise<MemoryRetrievalSettings>, accepted?: () => void) => {
    setBusy(true);
    try { adopt(await operation()); accepted?.(); }
    catch (error) { onNotify('设置未保存', error instanceof Error ? error.message : '请稍后重试。', 'warning'); }
    finally { setBusy(false); }
  };
  const toggleEmbedding = () => {
    const next = !embeddingOpen;
    if (!value.embedding_enabled && (!next || !isConfigured(value.embedding))) setEmbeddingOpen(next);
    else void update(() => adapter.setEmbeddingEnabled(next), () => setEmbeddingOpen(next));
  };
  const toggleRanking = () => {
    const next = !rankingOpen;
    const mode = next && isConfigured(value[preferredRanking]) ? preferredRanking : 'off';
    if (mode === value.ranking_mode) setRankingOpen(next);
    else void update(() => adapter.setMemoryRanking(mode), () => setRankingOpen(next));
  };
  const choose = (mode: 'decision' | 'rerank') => {
    const effective = isConfigured(value[mode]) ? mode : 'off';
    const selected = () => { setPreferredRanking(mode); setDeleteCandidate(undefined); };
    if (effective === value.ranking_mode) selected();
    else void update(() => adapter.setMemoryRanking(effective), selected);
  };
  const card = (slot: MemoryModelSlot) => <RetrievalModelCard key={slot} slot={slot} connection={value[slot]} busy={busy}
    deleting={deleteCandidate === slot} onDelete={() => setDeleteCandidate(slot)} onCancelDelete={() => setDeleteCandidate(undefined)}
    onConfirmDelete={() => void update(() => adapter.clearMemoryModel(slot), () => setDeleteCandidate(undefined))}
    onEdit={() => {
      setDeleteCandidate(undefined);
      const activate = slot === 'embedding' ? embeddingOpen && !value.embedding_enabled : rankingOpen && preferredRanking === slot && value.ranking_mode !== slot;
      setEditor({ slot, activate });
    }} />;
  return <section className="settings-group retrieval-settings">
    <header><Brain size={16} /><div><h2>记忆检索</h2><p>配置记忆召回使用的语义检索与重排模型。</p></div>{busy && <span className="retrieval-spinner"><LoaderCircle size={15} /></span>}</header>
    <section className="retrieval-channel" aria-labelledby="retrieval-embedding-heading">
    <div className="retrieval-channel-heading"><h3 id="retrieval-embedding-heading">Embedding</h3>
      <div className="retrieval-channel-controls">
        {embeddingOpen && !isConfigured(value.embedding) && <MissingModelWarning channel="Embedding" connection={value.embedding} />}
        <button type="button" role="switch" aria-label="启用 Embedding" aria-checked={embeddingOpen} className="settings-switch" disabled={busy} onClick={toggleEmbedding}><span /></button>
      </div>
    </div>
    {embeddingOpen && <div className="retrieval-drawer">{card('embedding')}</div>}
    </section>
    <section className="retrieval-channel" aria-labelledby="retrieval-ranking-heading">
    <div className="retrieval-channel-heading"><h3 id="retrieval-ranking-heading">重排</h3>
    <div className="retrieval-channel-controls">
      {rankingOpen && <fieldset className="retrieval-segments" aria-label="重排方式">
        {(['decision', 'rerank'] as const).map(mode => <label key={mode} className={preferredRanking === mode ? 'is-active' : ''}>
          <input type="radio" name="memory-ranking" value={mode} checked={preferredRanking === mode} disabled={busy} onChange={() => choose(mode)} />{names[mode]}
        </label>)}
      </fieldset>}
      {rankingOpen && !isConfigured(value[preferredRanking]) && <MissingModelWarning channel="重排" connection={value[preferredRanking]} />}
      <button type="button" role="switch" aria-label="启用重排" aria-checked={rankingOpen} className="settings-switch" disabled={busy} onClick={toggleRanking}><span /></button>
    </div>
    </div>
    {rankingOpen && <div className="retrieval-drawer">
    <div className="model-card-list">{card(preferredRanking)}</div></div>}
    </section>
    {editor && <MemoryModelDialog key={editor.slot} slot={editor.slot} activate={editor.activate} value={value} adapter={adapter} onChanged={adopt} onNotify={onNotify} onClose={() => setEditor(undefined)} />}
  </section>;
}

function MemoryModelDialog({ slot, activate, value, adapter, onChanged, onNotify, onClose }: {
  slot: MemoryModelSlot; activate: boolean; value: MemoryRetrievalSettings; adapter: RuntimeAdapter;
  onChanged: (value: MemoryRetrievalSettings) => void; onNotify: Notify; onClose: () => void;
}) {
  const original = value[slot];
  const dialog = useRef<HTMLDialogElement>(null);
  const [endpoint, setEndpoint] = useState(original?.endpoint ?? (slot === 'decision' ? presets.jev.endpoint : ''));
  const [model, setModel] = useState(original?.model_id ?? '');
  const [shape, setShape] = useState<RetrievalShape>(original?.shape ?? ({ embedding: 'openai_embedding', decision: 'system_one', rerank: 'flat_rerank' } as const)[slot]);
  const authentication = original?.authentication ?? 'bearer_api_key';
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState<'save' | 'test' | 'clear' | null>(null);
  const [error, setError] = useState<string>();
  const [pending, setPending] = useState<RetrievalModelInput>();
  useEffect(() => { dialog.current?.showModal(); }, []);
  const preset = Object.entries(presets).find(([, item]) => item.shape === shape && item.endpoint === endpoint)?.[0] ?? 'custom';
  const ready = Boolean(endpoint.trim() && model.trim() && (authentication === 'none' || key || original?.credential_configured));
  const input = (): RetrievalModelInput => ({
    connection: { endpoint: endpoint.trim(), model_id: model.trim(), shape, authentication },
    key_action: authentication === 'none' ? 'clear' : key ? 'replace' : 'keep',
    ...(authentication !== 'none' && key ? { api_key: key } : {}), activate,
  });
  const publish = async (payload: RetrievalModelInput) => {
    setBusy('save'); setError(undefined);
    try { onChanged(await adapter.saveMemoryModel(slot, payload)); onClose(); }
    catch (e) { setPending(undefined); setError(e instanceof Error ? e.message : '保存失败。'); }
    finally { setBusy(null); }
  };
  const save = async () => {
    const payload = input();
    if (slot !== 'embedding') { await publish(payload); return; }
    const before = value.embedding;
    const changed = !before || before.endpoint !== payload.connection.endpoint || before.model_id !== payload.connection.model_id || before.shape !== payload.connection.shape;
    if (!changed) { await publish(payload); return; }
    payload.confirm_reembed = true;
    payload.expected_embedding = before ? { endpoint: before.endpoint, model_id: before.model_id, shape: before.shape } : null;
    if (!before) {
      // Existing catalog owners tell us whether an initial embedding build has work.
      setBusy('save');
      try {
        const global = await adapter.memory.catalog({ view: 'global', workspace_id: null }, {});
        let hasMemory = global.items.length > 0;
        let cursor: string | undefined;
        while (!hasMemory) {
          const projects = await adapter.memory.projects(cursor);
          for (const project of projects.items) {
            const page = await adapter.memory.catalog({ view: 'project', workspace_id: project.workspace_id }, {});
            if (page.items.length) { hasMemory = true; break; }
          }
          if (hasMemory || !projects.next_cursor) break;
          cursor = projects.next_cursor;
        }
        if (!hasMemory) { await publish(payload); return; }
      } catch { /* If the catalog is unavailable, explain the possible initial build. */ }
      finally { setBusy(null); }
    }
    setPending(payload);
  };
  const test = async () => {
    setBusy('test'); setError(undefined);
    try {
      const result = await adapter.testMemoryModel(slot, input());
      const detail = `${Math.round(result.elapsed_ms)} ms · ${slot === 'embedding' ? `${result.dimensions} 维` : `${result.result_count} 条结果`}`;
      onNotify('连接正常', detail, 'success');
    } catch (e) { const message = e instanceof Error ? e.message : '连接失败。'; setError(message); onNotify('连接测试失败', message, 'warning'); }
    finally { setBusy(null); }
  };
  const clear = async () => {
    setBusy('clear'); setError(undefined);
    try { onChanged(await adapter.clearMemoryModel(slot)); onClose(); }
    catch (e) { setError(e instanceof Error ? e.message : '清除失败。'); }
    finally { setBusy(null); }
  };
  return <dialog ref={dialog} className="model-config-dialog retrieval-dialog" aria-labelledby="retrieval-dialog-title" onClose={onClose} onCancel={event => { if (busy) event.preventDefault(); }}>
    <section className="settings-group model-add-panel">
    <header><Pencil size={16} /><div><h2 id="retrieval-dialog-title">配置 {names[slot]}</h2></div><button type="button" className="settings-header-action" aria-label="关闭模型配置" disabled={Boolean(busy)} onClick={onClose}><X size={15} /></button></header>
    {pending ? <div className="retrieval-confirm"><p>{original ? `${value.embedding_enabled || activate ? '将' : '开启后将'}覆盖重建已有记忆向量，产生 API 费用。记忆内容保留；重建期间语义检索可能不完整。` : '首次启用将为已有记忆生成向量，产生 API 费用。'}</p>
      <div className="form-actions"><button disabled={Boolean(busy)} onClick={() => setPending(undefined)}>返回</button><button className="primary-action" disabled={Boolean(busy)} onClick={() => void publish(pending)}>{busy && <LoaderCircle size={13} />}确认保存{activate ? '并启用' : ''}</button></div>
    </div> : <><fieldset disabled={Boolean(busy)} className="retrieval-fields"><div className="settings-form-grid">
      {slot === 'decision' && <><label><span>连接预设</span><select value={preset} onChange={event => {
        const item = presets[event.target.value as keyof typeof presets];
        if (item) { setShape(item.shape); setEndpoint(item.endpoint); }
        else setEndpoint('');
      }}><option value="jev">Jev</option><option value="openrouter">OpenRouter</option><option value="openai">OpenAI</option><option value="custom">自定义</option></select></label>
        <label><span>协议</span><select value={shape} disabled={preset !== 'custom'} onChange={event => setShape(event.target.value as RetrievalShape)}><option value="system_one">System One</option><option value="openai_decisions">OpenAI Decisions</option></select></label></>}
      <label className="retrieval-wide"><span>请求地址</span><input value={endpoint} disabled={slot === 'decision' && preset !== 'custom'} onChange={event => setEndpoint(event.target.value)} type="url" placeholder="https://…" /></label>
      <label><span>模型 ID{slot === 'embedding' && <span className="retrieval-dimension">1024 维</span>}</span><input value={model} onChange={event => setModel(event.target.value)} /></label>
      {slot === 'rerank' && <label><span>请求格式</span><select value={shape} onChange={event => setShape(event.target.value as RetrievalShape)}><option value="flat_rerank">标准</option><option value="nested_rerank">嵌套</option></select></label>}
      {authentication === 'bearer_api_key' && <label><span>API key</span><input type="password" autoComplete="new-password" value={key} onChange={event => setKey(event.target.value)} placeholder={original?.credential_configured ? '留空保留现有密钥' : ''} /></label>}
    </div>{slot === 'rerank' && <div className="retrieval-details">
      <details><summary>格式示例</summary><pre>{JSON.stringify(shape === 'flat_rerank' ? { model: 'model-id', query: '用户问题', documents: ['候选记忆'] } : { model: 'model-id', input: { query: '用户问题', documents: ['候选记忆'] } }, null, 2)}</pre></details>
    </div>}</fieldset>
    {error && <p role="alert" className="retrieval-error">{error}</p>}
    <footer><span className="retrieval-fee">测试会发送请求，可能计费</span><div className="form-actions">
      {original && <button className="subtle-danger retrieval-clear" disabled={Boolean(busy)} onClick={() => void clear()}><Trash2 size={13} />清除配置</button>}
      <button disabled={Boolean(busy)} onClick={onClose}>取消</button><button disabled={!ready || Boolean(busy)} onClick={() => void test()}>{busy === 'test' ? <LoaderCircle size={13} /> : <RefreshCw size={13} />}测试连接</button>
      <button className="primary-action" disabled={!ready || Boolean(busy)} onClick={() => void save()}>{busy === 'save' ? <LoaderCircle size={13} /> : <Check size={13} />}{activate ? '保存并启用' : '保存'}</button>
    </div></footer></>}
    </section>
  </dialog>;
}
