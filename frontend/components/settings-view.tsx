'use client';

import {
  Bot, Check, ChevronRight, CircleAlert, Database, HardDrive, KeyRound,
  Laptop, LoaderCircle, Moon, Palette, Plus, RefreshCw, ShieldCheck,
  SlidersHorizontal, Sun, Trash2,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type {
  LocalCredentialState, LocalSettingsReadModel, ModelCatalogReadModel,
  ModelConfigurationSummary, RuntimeAdapter, RuntimeBootstrap,
} from '../lib/runtime-adapter';
import type { RuntimeStatus } from '../lib/pulsara-types';

type SettingsSection = 'general' | 'models' | 'service';
type CredentialKind = 'embedding' | 'rerank';

interface SettingsViewProps {
  theme: 'light' | 'dark';
  bootstrap?: RuntimeBootstrap;
  runtimeStatus: RuntimeStatus;
  adapter: RuntimeAdapter;
  onThemeChange: (theme: 'light' | 'dark') => void;
  onConfigurationChanged: () => Promise<void>;
  onNotify: (title: string, detail?: string, tone?: 'neutral' | 'success' | 'warning') => void;
}

const navItems = [
  { id: 'general' as const, label: '通用', icon: SlidersHorizontal },
  { id: 'models' as const, label: '模型', icon: Bot },
  { id: 'service' as const, label: '本地服务', icon: HardDrive },
];

const statusLabels: Record<RuntimeStatus, string> = {
  starting: '正在连接', online: '连接正常', reconnecting: '正在重连',
  offline: '连接中断', failed: '连接失败',
};

const credentialLabels: Record<LocalCredentialState, string> = {
  PRESENT: '已配置', MISSING: '未配置', DENIED: '访问被拒绝',
  UNAVAILABLE: '钥匙串不可用',
};

const alphabeticalCollator = new Intl.Collator('en', {
  numeric: true,
  sensitivity: 'base',
});

function SettingRow({ icon: Icon, title, detail, children }: {
  icon: typeof Sun; title: string; detail: string; children: React.ReactNode;
}) {
  return <div className="setting-row"><span className="setting-row__icon"><Icon size={15} /></span><span className="setting-row__copy"><strong>{title}</strong><small>{detail}</small></span><div className="setting-row__control">{children}</div></div>;
}

function formatTokens(value?: number | null): string {
  return value ? new Intl.NumberFormat('zh-CN').format(value) : '限制未知';
}

function wireLabel(value: string): string {
  return value === 'openai_responses' ? 'Responses' : 'Chat Completions';
}

function connectionTitle(connection: ModelConfigurationSummary): string {
  return `${connection.route_name ?? connection.route_id} · ${connection.display_name ?? connection.model_id}`;
}

function DashScopeCredentialRow({ adapter, kind, state, onChanged, onNotify }: {
  adapter: RuntimeAdapter;
  kind: CredentialKind;
  state: LocalCredentialState;
  onChanged: (state: LocalCredentialState) => void;
  onNotify: SettingsViewProps['onNotify'];
}) {
  const input = useRef<HTMLInputElement>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const title = kind === 'embedding' ? 'Embedding · text-embedding-v4 · 1024 维' : 'Reranker · qwen3-rerank';
  const detail = kind === 'embedding' ? '增强记忆的语义相关性' : '优化显式记忆搜索的排序';

  const save = async () => {
    const apiKey = input.current?.value ?? '';
    if (!apiKey) return;
    setBusy(true);
    try {
      const next = await adapter.putDashScopeCredential(kind, apiKey);
      if (input.current) input.current.value = '';
      setEditing(false);
      onChanged(next);
      onNotify('访问密钥已保存', `${title} 将在下一次相关操作中使用。`, 'success');
    } catch (error) {
      if (input.current) input.current.value = '';
      onNotify('访问密钥未保存', error instanceof Error ? error.message : '请检查本机钥匙串。', 'warning');
    } finally { setBusy(false); }
  };

  const clear = async () => {
    setBusy(true);
    try {
      const next = await adapter.deleteDashScopeCredential(kind);
      onChanged(next);
      setEditing(false);
      onNotify('访问密钥已清除', `${title} 会回退到不依赖该远程通道的记忆路径。`, 'success');
    } catch (error) {
      onNotify('访问密钥未清除', error instanceof Error ? error.message : '请检查本机钥匙串。', 'warning');
    } finally { setBusy(false); }
  };

  return <div className="credential-row">
    <span><strong>{title}</strong><small>{detail}</small></span>
    <span className={`credential-state credential-state--${state.toLowerCase()}`}><i />{credentialLabels[state]}</span>
    <div className="credential-actions">{editing ? <>
      <input ref={input} type="password" autoComplete="new-password" placeholder="阿里云百炼 API key" aria-label={`${title} API key`} />
      <button disabled={busy} onClick={() => void save()}>{busy ? <LoaderCircle size={13} /> : <Check size={13} />}保存</button>
      <button disabled={busy} onClick={() => { if (input.current) input.current.value = ''; setEditing(false); }}>取消</button>
    </> : <>
      <button disabled={busy} onClick={() => setEditing(true)}><KeyRound size={13} />填写或更新</button>
      {state === 'PRESENT' && <button className="subtle-danger" disabled={busy} onClick={() => void clear()}><Trash2 size={13} />清除</button>}
    </>}</div>
  </div>;
}

export function SettingsView({ theme, bootstrap, runtimeStatus, adapter, onThemeChange, onConfigurationChanged, onNotify }: SettingsViewProps) {
  const [section, setSection] = useState<SettingsSection>(bootstrap?.database_state === 'ready' ? 'general' : 'service');
  const [settings, setSettings] = useState<LocalSettingsReadModel | undefined>(bootstrap);
  const [catalog, setCatalog] = useState<ModelCatalogReadModel>();
  const [loading, setLoading] = useState(true);
  const [catalogRefreshing, setCatalogRefreshing] = useState(false);
  const [error, setError] = useState<string>();
  const [adding, setAdding] = useState(false);
  const [routeId, setRouteId] = useState('');
  const [modelId, setModelId] = useState('');
  const [wireApi, setWireApi] = useState<'' | 'openai_chat_completions' | 'openai_responses'>('');
  const modelKey = useRef<HTMLInputElement>(null);
  const [savingModel, setSavingModel] = useState(false);
  const [runtimeDsn, setRuntimeDsn] = useState(bootstrap?.local_settings?.postgres?.runtime_dsn ?? '');
  const [adminDsn, setAdminDsn] = useState(bootstrap?.local_settings?.postgres?.admin_dsn ?? '');
  const [databaseBusy, setDatabaseBusy] = useState<'save' | 'check' | 'migrate'>();
  const [databaseMessage, setDatabaseMessage] = useState<string>();

  const load = async () => {
    setLoading(true); setError(undefined);
    try {
      const [nextSettings, nextCatalog] = await Promise.all([adapter.localSettings(), adapter.modelCatalog()]);
      setSettings(nextSettings); setCatalog(nextCatalog);
      setRuntimeDsn(nextSettings.local_settings.postgres?.runtime_dsn ?? '');
      setAdminDsn(nextSettings.local_settings.postgres?.admin_dsn ?? '');
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '本机设置暂时无法读取。');
    } finally { setLoading(false); }
  };

  useEffect(() => {
    let active = true;
    void Promise.all([adapter.localSettings(), adapter.modelCatalog()])
      .then(([nextSettings, nextCatalog]) => {
        if (!active) return;
        setSettings(nextSettings);
        setCatalog(nextCatalog);
        setRuntimeDsn(nextSettings.local_settings.postgres?.runtime_dsn ?? '');
        setAdminDsn(nextSettings.local_settings.postgres?.admin_dsn ?? '');
      })
      .catch((loadError: unknown) => {
        if (!active) return;
        setError(loadError instanceof Error ? loadError.message : '本机设置暂时无法读取。');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [adapter]);

  const selectableRoutes = useMemo(() => [...(catalog?.routes ?? [])].sort((left, right) => (
    alphabeticalCollator.compare(left.display_name, right.display_name)
    || alphabeticalCollator.compare(left.route_id, right.route_id)
  )), [catalog]);
  const selectedRoute = selectableRoutes.find((route) => route.route_id === routeId);
  const selectableModels = useMemo(() => [...(selectedRoute?.models ?? [])].sort((left, right) => (
    alphabeticalCollator.compare(left.model_id, right.model_id)
  )), [selectedRoute]);
  const selectedModel = selectableModels.find((model) => model.model_id === modelId);
  const selectedWire = selectedModel?.wire_apis.find((wire) => wire.wire_api === wireApi);
  const hasExecutableWire = selectedModel?.wire_apis.some((wire) => wire.executable) ?? false;
  const modelConfigurations = settings?.model_configurations ?? [];

  const refreshCatalog = async () => {
    setCatalogRefreshing(true);
    try { setCatalog(await adapter.modelCatalog(true)); onNotify('模型目录已刷新', '选择器已经使用最新的 models.dev 快照。', 'success'); }
    catch (refreshError) { onNotify('模型目录未刷新', refreshError instanceof Error ? refreshError.message : '请稍后重试。', 'warning'); }
    finally { setCatalogRefreshing(false); }
  };

  const saveModel = async () => {
    const apiKey = modelKey.current?.value ?? '';
    if (!routeId || !modelId || !wireApi || !apiKey || !selectedWire?.executable) return;
    setSavingModel(true);
    try {
      const result = await adapter.addModelConfiguration({ route_id: routeId, model_id: modelId, wire_api: wireApi, api_key: apiKey });
      if (modelKey.current) modelKey.current.value = '';
      setSettings(await adapter.localSettings());
      setAdding(false); setRouteId(''); setModelId(''); setWireApi('');
      await onConfigurationChanged();
      onNotify('模型配置已添加', connectionTitle(result.model_configuration), 'success');
    } catch (saveError) {
      if (modelKey.current) modelKey.current.value = '';
      onNotify('模型配置未添加', saveError instanceof Error ? saveError.message : '请检查所选模型与本机钥匙串。', 'warning');
    } finally { setSavingModel(false); }
  };

  const updateCredentialState = (kind: CredentialKind, state: LocalCredentialState) => setSettings((current) => current ? {
    ...current,
    local_settings: { ...current.local_settings, dashscope_credentials: { ...current.local_settings.dashscope_credentials, [kind]: state } },
  } : current);

  const saveDatabase = async () => {
    if (!runtimeDsn.trim()) return;
    setDatabaseBusy('save'); setDatabaseMessage(undefined);
    try {
      const next = await adapter.savePostgres(runtimeDsn.trim(), adminDsn.trim() || null);
      setSettings(next);
      setDatabaseMessage(next.restart_required ? '已保存，重启 Pulsara 后连接。' : '已保存。现在可以检查连接或初始化数据库。');
      await onConfigurationChanged();
    } catch (saveError) { setDatabaseMessage(saveError instanceof Error ? saveError.message : '连接信息未保存。'); }
    finally { setDatabaseBusy(undefined); }
  };

  const databaseAction = async (kind: 'check' | 'migrate') => {
    setDatabaseBusy(kind); setDatabaseMessage(undefined);
    try {
      const result = kind === 'check' ? await adapter.checkPostgres() : await adapter.migratePostgres();
      const databaseName = typeof result.database_name === 'string' ? `数据库 ${result.database_name}` : '已保存的数据库';
      setDatabaseMessage(kind === 'check' ? `${databaseName} 连接与结构检查通过。` : `${databaseName} 已完成初始化或升级。`);
      setSettings(await adapter.localSettings()); await onConfigurationChanged();
    } catch (actionError) { setDatabaseMessage(actionError instanceof Error ? actionError.message : '数据库操作没有完成。'); }
    finally { setDatabaseBusy(undefined); }
  };

  const databaseState = settings?.database_state ?? bootstrap?.database_state ?? 'database_not_configured';
  const databaseStateCopy = {
    ready: '数据服务已就绪', database_not_configured: '尚未配置 PostgreSQL',
    database_configured_unverified: '已保存，尚未检查 PostgreSQL',
    database_unavailable: '无法连接 PostgreSQL', database_schema_action_required: '需要初始化或升级数据库',
  }[databaseState];
  const selectedShapeWarning = Boolean(selectedModel?.wire_shape_hint && wireApi
    && ((selectedModel.wire_shape_hint === 'responses') !== (wireApi === 'openai_responses')));

  return <section className="surface-view settings-view">
    <header className="page-header"><div><span className="page-kicker">本机设置</span><h1>设置</h1><p>配置模型、记忆检索与本机 PostgreSQL。访问密钥只保存在 macOS 钥匙串。</p></div></header>
    <div className="settings-layout">
      <aside className="settings-nav">
        <div className="settings-profile"><span>PL</span><div><strong>本机用户</strong><small>无需账号登录</small></div></div>
        {navItems.map(({ id, label, icon: Icon }) => <button className={section === id ? 'is-active' : ''} key={id} onClick={() => setSection(id)}><Icon size={14} /><span>{label}</span><ChevronRight size={12} /></button>)}
        <div className="settings-version"><strong>Pulsara</strong><span>{bootstrap?.application.version ?? '本地版本'}</span><small>运行在这台 Mac 上</small></div>
      </aside>
      <div className="settings-content">
        {error && <div className="settings-alert"><CircleAlert size={15} /><span>{error}</span><button onClick={() => void load()}>重试</button></div>}
        {loading && <div className="settings-loading"><LoaderCircle size={15} />正在读取本机设置…</div>}
        {section === 'general' && <section className="settings-group"><header><Palette size={16} /><div><h2>外观</h2><p>控制 Pulsara 在本机的呈现方式。</p></div></header>
          <SettingRow icon={theme === 'light' ? Sun : Moon} title="主题" detail="切换明暗外观"><div className="theme-picker"><button className={theme === 'light' ? 'is-active' : ''} onClick={() => onThemeChange('light')}><Sun size={12} /> 浅色</button><button className={theme === 'dark' ? 'is-active' : ''} onClick={() => onThemeChange('dark')}><Moon size={12} /> 深色</button></div></SettingRow>
        </section>}
        {section === 'models' && <>
          <section className="settings-group model-settings-group"><header><Bot size={16} /><div><h2>模型配置</h2><p>每张卡片是一条可在会话中选择的独立连接。</p></div><button className="settings-header-action" onClick={() => setAdding((value) => !value)}><Plus size={13} />添加配置</button></header>
            {modelConfigurations.length ? <div className="model-card-list">{modelConfigurations.map((connection) => <article className="model-config-card" key={connection.id}><span className="model-config-card__icon"><Bot size={15} /></span><span><strong>{connectionTitle(connection)}</strong><small>{wireLabel(connection.wire_api)} · {formatTokens(connection.context_tokens)} 上下文 · API key {credentialLabels[connection.credential_state]}</small><code>{connection.model_id}</code></span><span className={`credential-state credential-state--${connection.credential_state.toLowerCase()}`}><i />{connection.status === 'ready' ? credentialLabels[connection.credential_state] : '目录中不可用'}</span></article>)}</div>
              : <div className="settings-empty"><Bot size={18} /><strong>还没有模型配置</strong><span>添加后，在每个会话的输入框下方显式选择要使用的连接。</span></div>}
          </section>
          {adding && <section className="settings-group model-add-panel"><header><Plus size={16} /><div><h2>添加模型配置</h2><p>目录来自 models.dev；保存不会向模型发送测试请求。</p></div><button className="settings-header-action" disabled={catalogRefreshing} onClick={() => void refreshCatalog()}><RefreshCw size={13} />刷新目录</button></header>
            <div className="settings-form-grid">
              <label><span>提供方</span><select value={routeId} onChange={(event) => { setRouteId(event.target.value); setModelId(''); setWireApi(''); }}><option value="">选择 Provider</option>{selectableRoutes.map((route) => <option key={route.route_id} value={route.route_id}>{route.display_name}</option>)}</select></label>
              <label><span>模型</span><select value={modelId} disabled={!selectedRoute} onChange={(event) => { setModelId(event.target.value); setWireApi(''); }}><option value="">选择 Model ID</option>{selectableModels.map((model) => <option key={model.model_id} value={model.model_id}>{model.display_name} · {model.model_id}</option>)}</select></label>
              <label><span>API 协议</span><select value={wireApi} disabled={!selectedModel || !hasExecutableWire} onChange={(event) => setWireApi(event.target.value as typeof wireApi)}><option value="">选择 Chat 或 Responses</option>{selectedModel?.wire_apis.map((wire) => <option key={wire.wire_api} value={wire.wire_api} disabled={!wire.executable}>{wireLabel(wire.wire_api)}{wire.recommended ? ' · models.dev 建议' : ''}{wire.executable ? '' : ' · 暂不支持'}</option>)}</select></label>
              <label><span>API key</span><input ref={modelKey} type="password" autoComplete="new-password" placeholder="只写入 macOS 钥匙串" /></label>
            </div>
            {selectedModel && !hasExecutableWire && <p className="inline-warning"><CircleAlert size={13} />该模型仍保留在 models.dev 目录中，但 Pulsara 尚未为这个提供方注册可执行的 Chat / Responses wire adapter。</p>}
            {selectedModel && selectedWire && <div className="model-confirmation"><strong>确认连接</strong><dl><div><dt>提供方</dt><dd>{selectedRoute?.display_name}</dd></div><div><dt>模型</dt><dd>{selectedModel.model_id}</dd></div><div><dt>协议</dt><dd>{wireLabel(selectedWire.wire_api)}</dd></div><div><dt>Endpoint</dt><dd>{selectedWire.endpoint ?? '目录未提供'}</dd></div><div><dt>上下文</dt><dd>{formatTokens(selectedModel.context_tokens)}</dd></div></dl>{selectedShapeWarning && <p className="inline-warning"><CircleAlert size={13} />所选协议与 models.dev 建议不同；仍可保存，调用失败时请返回这里添加另一配置。</p>}<p>Pulsara 当前只支持与 OpenAI Chat Completions 或 Responses 兼容的接口。模型出现在目录中不代表所选提供方一定支持你选择的 API 协议；若调用失败，请返回这里更换配置。</p><div className="form-actions"><button onClick={() => { if (modelKey.current) modelKey.current.value = ''; setAdding(false); }}>取消</button><button className="primary-action" disabled={savingModel || !selectedWire.executable} onClick={() => void saveModel()}>{savingModel ? <LoaderCircle size={13} /> : <Check size={13} />}保存配置</button></div></div>}
          </section>}
          <section className="settings-group"><header><KeyRound size={16} /><div><h2>记忆检索 · 阿里云百炼 DashScope</h2><p>可选的相关性增强；未配置不会阻止对话或基础记忆路径。</p></div></header>{settings && <div className="credential-list"><DashScopeCredentialRow adapter={adapter} kind="embedding" state={settings.local_settings.dashscope_credentials.embedding} onChanged={(state) => updateCredentialState('embedding', state)} onNotify={onNotify} /><DashScopeCredentialRow adapter={adapter} kind="rerank" state={settings.local_settings.dashscope_credentials.rerank} onChanged={(state) => updateCredentialState('rerank', state)} onNotify={onNotify} /></div>}</section>
        </>}
        {section === 'service' && <>
          <section className="settings-group"><header><Database size={16} /><div><h2>PostgreSQL</h2><p>会话数据平面使用本机 PostgreSQL；保存不会自动连接或迁移。</p></div></header>
            {settings?.local_settings.state === 'unavailable' && <div className="settings-alert settings-alert--inside"><CircleAlert size={15} /><span>本机设置文件无法读取。保存下面的完整 PostgreSQL 配置会覆盖并修复该文件。</span></div>}
            <div className="database-state-row"><span className={`database-state database-state--${databaseState}`}><i />{databaseStateCopy}</span><small>浏览器设置壳：{statusLabels[runtimeStatus]}</small></div>
            <div className="database-form"><label><span>Runtime DSN</span><input value={runtimeDsn} onChange={(event) => setRuntimeDsn(event.target.value)} placeholder="postgresql://pulsara:…@localhost:5432/pulsara" /></label><label><span>Admin DSN（可选）</span><input value={adminDsn} onChange={(event) => setAdminDsn(event.target.value)} placeholder="只用于显式初始化或升级" /></label><p>初始化/升级将使用上方已保存的 Admin DSN 管理同一数据库，并按 Runtime DSN 验证运行角色。请先核对目标数据库与角色。</p>{databaseMessage && <div className="database-message">{databaseMessage}</div>}<div className="form-actions"><button className="primary-action" disabled={Boolean(databaseBusy) || !runtimeDsn.trim()} onClick={() => void saveDatabase()}>{databaseBusy === 'save' ? <LoaderCircle size={13} /> : <Check size={13} />}保存连接</button><button disabled={Boolean(databaseBusy) || !settings?.local_settings.postgres} onClick={() => void databaseAction('check')}>{databaseBusy === 'check' ? <LoaderCircle size={13} /> : <RefreshCw size={13} />}检查连接</button><button disabled={Boolean(databaseBusy) || !settings?.local_settings.postgres?.admin_dsn} onClick={() => void databaseAction('migrate')}>{databaseBusy === 'migrate' ? <LoaderCircle size={13} /> : <Database size={13} />}初始化 / 升级</button></div></div>
          </section>
          <section className="settings-group"><header><HardDrive size={16} /><div><h2>本地服务</h2><p>Pulsara 的设置与任务都由这台设备上的进程管理。</p></div></header><SettingRow icon={HardDrive} title="页面连接" detail="浏览器与本地设置服务"><span className={runtimeStatus === 'online' ? 'healthy-value' : ''}>{runtimeStatus === 'online' && <i />} {statusLabels[runtimeStatus]}</span></SettingRow><SettingRow icon={Laptop} title="数据位置" detail="配置与会话数据保存在本机"><span className="storage-value">本地</span></SettingRow><SettingRow icon={ShieldCheck} title="登录方式" detail="仅允许本机同源页面访问"><span className="storage-value">无需账号</span></SettingRow></section>
        </>}
      </div>
    </div>
  </section>;
}
