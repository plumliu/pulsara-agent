'use client';

import {
  Archive, Bot, Check, ChevronLeft, ChevronRight, CircleAlert, Database, HardDrive, KeyRound,
  LoaderCircle, Moon, Palette, Pencil, Plus, RefreshCw,
  SlidersHorizontal, Sun, Trash2, X,
} from 'lucide-react';
import { useContext, useEffect, useMemo, useRef, useState } from 'react';
import type {
  LocalSettingsReadModel, ModelCatalogReadModel,
  CustomReasoningProfile, ModelConfigurationDetail, ModelConfigurationInput, ModelConfigurationSummary,
  ReasoningWireProfile,
  RuntimeAdapter, RuntimeBootstrap,
} from '../lib/runtime-adapter';
import type { RuntimeStatus, SessionSummary } from '../lib/pulsara-types';
import { ArchivedSessions } from './archived-sessions';
import { RuntimeApiError } from '../lib/runtime-adapter';

import { ToolResultDisplayContext } from '../lib/tool-result-display';

type SettingsSection = 'general' | 'models' | 'service' | 'archived';
type CredentialKind = 'embedding' | 'rerank';
type ModelConfigurationSource = 'models_dev' | 'user_declared';
type CustomReasoningKind = CustomReasoningProfile;

interface SettingsViewProps {
  sessionRevision: number;
  onSessionsChanged: () => Promise<void>;
  onDeleteSession: (session: SessionSummary) => void;
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
  { id: 'archived' as const, label: '已归档会话', icon: Archive },
];

const statusLabels: Record<RuntimeStatus, string> = {
  starting: '正在连接', online: '连接正常', reconnecting: '正在重连',
  offline: '连接中断', failed: '连接失败',
};

const minimumContextTokens = 256_000;
// Presentation batch only; every saved connection remains available across pages.
const modelPageSize = 5;

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

function formatModalities(values?: readonly string[] | null): string {
  if (values == null) return '未声明';
  const labels: Record<string, string> = {
    text: '文字', image: '图片', audio: '音频', video: '视频', pdf: 'PDF',
  };
  return values.length ? values.map((value) => labels[value] ?? value).join('、') : '无';
}

function connectionTitle(connection: ModelConfigurationSummary): string {
  return `${connection.route_name ?? connection.route_id} · ${connection.display_name ?? connection.model_id}`;
}

function connectionCredentialLabel(connection: ModelConfigurationSummary): string {
  if (connection.authentication === 'none') return '无需认证';
  return connection.credential_configured ? '密钥已配置' : '密钥未配置';
}

function parseEffortValues(value: string): string[] {
  return [...new Set(value.split(/[,，]/).map((item) => item.trim()).filter(Boolean))];
}

const effortReasoningProfiles = new Set<CustomReasoningKind>([
  'effort', 'thinking_effort', 'broad_compat',
]);
const chatOnlyReasoningProfiles = new Set<CustomReasoningKind>([
  'enable_thinking', 'thinking_type', 'thinking_effort', 'broad_compat',
]);

function reasoningProfileDescription(profile: ReasoningWireProfile): string {
  return {
    provider_default: '不发送推理控制字段，由服务端自行决定。',
    effort: '使用所选协议的标准 effort 字段。',
    toggle: 'Chat 发送 reasoning.enabled；Responses 映射为 high / none。',
    enable_thinking: '发送 enable_thinking 布尔值。',
    thinking_type: '发送 thinking.type 的 enabled / disabled。',
    thinking_effort: '同时发送 thinking.type 与 reasoning_effort；关闭时只发送 disabled。',
    broad_compat: '同时发送四种常见兼容字段；严格服务端可能拒绝未知字段。',
    catalog_standard: '按照目录声明的能力使用所选协议的标准字段。',
  }[profile];
}

function reasoningProfileLabel(profile: ReasoningWireProfile): string {
  return {
    provider_default: '由服务端决定',
    effort: '标准 effort',
    toggle: '推理开关',
    enable_thinking: 'enable_thinking 开关',
    thinking_type: 'thinking.type 开关',
    thinking_effort: 'thinking.type + effort',
    broad_compat: '多字段兼容组合',
    catalog_standard: '目录标准映射',
  }[profile];
}

function catalogReasoningProfileDescription(
  profile: ReasoningWireProfile,
  reasoningKind?: string,
): string {
  if (reasoningKind !== 'fixed_on') return reasoningProfileDescription(profile);
  return {
    toggle: '目录声明该模型固定开启推理；此连接固定发送启用形状，会话中不显示开关。',
    enable_thinking: '目录声明该模型固定开启推理；发送 enable_thinking: true，会话中不显示开关。',
    thinking_type: '目录声明该模型固定开启推理；发送 thinking.type: enabled，会话中不显示开关。',
  }[profile as 'toggle' | 'enable_thinking' | 'thinking_type']
    ?? reasoningProfileDescription(profile);
}

function reasoningProfilePayload(
  profile: CustomReasoningKind,
  values: string[],
): Extract<ModelConfigurationInput, { source: 'user_declared' }>['reasoning'] {
  if (profile === 'effort' || profile === 'thinking_effort' || profile === 'broad_compat') {
    return { kind: profile, values };
  }
  return { kind: profile };
}

function DashScopeCredentialRow({ adapter, kind, state, onChanged, onNotify }: {
  adapter: RuntimeAdapter;
  kind: CredentialKind;
  state: boolean;
  onChanged: (state: boolean) => void;
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
      onNotify('访问密钥未保存', error instanceof Error ? error.message : '请检查本机设置文件。', 'warning');
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
      onNotify('访问密钥未清除', error instanceof Error ? error.message : '请检查本机设置文件。', 'warning');
    } finally { setBusy(false); }
  };

  return <div className="credential-row">
    <span><strong>{title}</strong><small>{detail}</small></span>
    <span className={`credential-state credential-state--${state ? 'present' : 'missing'}`}><i />{state ? '已配置' : '未配置'}</span>
    <div className="credential-actions">{editing ? <>
      <input ref={input} type="password" autoComplete="new-password" placeholder="阿里云百炼 API key" aria-label={`${title} API key`} />
      <button disabled={busy} onClick={() => void save()}>{busy ? <LoaderCircle size={13} /> : <Check size={13} />}保存</button>
      <button disabled={busy} onClick={() => { if (input.current) input.current.value = ''; setEditing(false); }}>取消</button>
    </> : <>
      <button disabled={busy} onClick={() => setEditing(true)}><KeyRound size={13} />填写或更新</button>
      {state && <button className="subtle-danger" disabled={busy} onClick={() => void clear()}><Trash2 size={13} />清除</button>}
    </>}</div>
  </div>;
}

function DatabaseResetDialog({ target, busy, error, onClose, onConfirm }: {
  target: NonNullable<LocalSettingsReadModel['local_settings']['postgres']>;
  busy: boolean; error?: string; onClose: () => void; onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} className="database-reset-dialog" aria-labelledby="database-reset-title" onClose={onClose} onCancel={(event) => { if (busy) event.preventDefault(); }}>
    <h2 id="database-reset-title">重置 Pulsara 数据？</h2>
    <p>将永久删除目标数据库中的 Pulsara 会话、图片、任务和记忆，并重新初始化。此操作无法撤销。</p>
    <dl><dt>Runtime DSN</dt><dd>{target.runtime_dsn}</dd><dt>Admin DSN</dt><dd>{target.admin_dsn}</dd></dl>
    <p>模型配置、API key 和工作目录中的文件会保留。正在运行的数据服务将先关闭；若内核已经启动，完成后需要重启 Pulsara。</p>
    {error && <p role="alert" className="database-reset-error">{error}</p>}
    <footer><button autoFocus disabled={busy} onClick={onClose}>取消</button><button className="database-reset-confirm" disabled={busy} onClick={onConfirm}>{busy ? '正在重置…' : '确认清空并初始化'}</button></footer>
  </dialog>;
}

function ModelFormContainer({ editing, busy, onClose, children }: {
  editing: boolean; busy: boolean; onClose: () => void; children: React.ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (editing) dialog.current?.showModal(); }, [editing]);
  const content = <fieldset className="model-editor-fields" disabled={busy}>{children}</fieldset>;
  return editing ? <dialog ref={dialog} className="model-edit-dialog" aria-label="修改模型配置" onClose={onClose} onCancel={(event) => { if (busy) event.preventDefault(); else onClose(); }}>{content}</dialog> : content;
}

export function SettingsView({ theme, bootstrap, runtimeStatus, adapter, onThemeChange, onConfigurationChanged, onNotify, sessionRevision, onSessionsChanged, onDeleteSession }: SettingsViewProps) {
  const { showBuiltinToolResults, onChange: onToolResultDisplayChange } = useContext(ToolResultDisplayContext);
  const [section, setSection] = useState<SettingsSection>(bootstrap?.database_state === 'ready' ? 'general' : 'service');
  const [settings, setSettings] = useState<LocalSettingsReadModel | undefined>(bootstrap);
  const [catalog, setCatalog] = useState<ModelCatalogReadModel>();
  const [loading, setLoading] = useState(true);
  const [catalogRefreshing, setCatalogRefreshing] = useState(false);
  const [error, setError] = useState<string>();
  const [adding, setAdding] = useState(false);
  const [editingConfiguration, setEditingConfiguration] = useState<ModelConfigurationDetail & { id: string }>();
  const [loadingModelId, setLoadingModelId] = useState<string>();
  const [modelFormError, setModelFormError] = useState<string>();
  const [configurationSource, setConfigurationSource] = useState<ModelConfigurationSource>('models_dev');
  const [routeId, setRouteId] = useState('');
  const [modelId, setModelId] = useState('');
  const [wireApi, setWireApi] = useState<'' | 'openai_chat_completions' | 'openai_responses'>('');
  const [configurationName, setConfigurationName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [authentication, setAuthentication] = useState<'bearer_api_key' | 'none'>('bearer_api_key');
  const [contextTokens, setContextTokens] = useState('256000');
  const [maxOutputTokens, setMaxOutputTokens] = useState('8192');
  const [toolCall, setToolCall] = useState(true);
  const [inputModalities, setInputModalities] = useState<string[] | null>(['text']);
  const [catalogReasoningProfile, setCatalogReasoningProfile] = useState<ReasoningWireProfile>('catalog_standard');
  const [customReasoning, setCustomReasoning] = useState<CustomReasoningKind>('provider_default');
  const [effortValues, setEffortValues] = useState('low, medium, high');
  const [keyPresent, setKeyPresent] = useState(false);
  const modelKey = useRef<HTMLInputElement>(null);
  const [savingModel, setSavingModel] = useState(false);
  const [testingModel, setTestingModel] = useState(false);
  const [deleteCandidateId, setDeleteCandidateId] = useState<string>();
  const [deletingModelId, setDeletingModelId] = useState<string>();
  const [modelPage, setModelPage] = useState(0);
  const [runtimeDsn, setRuntimeDsn] = useState(bootstrap?.local_settings?.postgres?.runtime_dsn ?? '');
  const [adminDsn, setAdminDsn] = useState(bootstrap?.local_settings?.postgres?.admin_dsn ?? '');
  const [databaseBusy, setDatabaseBusy] = useState<'save' | 'check' | 'migrate' | 'reset'>();
  const [databaseMessage, setDatabaseMessage] = useState<string>();
  const [resetTarget, setResetTarget] = useState<NonNullable<LocalSettingsReadModel['local_settings']['postgres']>>();
  const [resetError, setResetError] = useState<string>();

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
  const modelPageCount = Math.max(1, Math.ceil(modelConfigurations.length / modelPageSize));
  const currentModelPage = Math.min(modelPage, modelPageCount - 1);
  const modelPageStart = currentModelPage * modelPageSize;
  const visibleModelConfigurations = modelConfigurations.slice(modelPageStart, modelPageStart + modelPageSize);
  const draftEndpoint = configurationSource === 'models_dev' ? selectedWire?.endpoint : baseUrl.trim();
  const canKeepModelKey = Boolean(editingConfiguration?.credential_configured && draftEndpoint
    && draftEndpoint.replace(/\/+$/, '') === editingConfiguration.base_url.replace(/\/+$/, ''));
  const incompatibleCustomProfile = wireApi === 'openai_responses' && chatOnlyReasoningProfiles.has(customReasoning);

  const changeModelPage = (page: number) => {
    setModelPage(page);
    setDeleteCandidateId(undefined);
  };

  const resetModelDraft = () => {
    if (modelKey.current) modelKey.current.value = '';
    setKeyPresent(false);
    setRouteId(''); setModelId(''); setWireApi('');
    setConfigurationName(''); setBaseUrl('');
    setAuthentication('bearer_api_key');
    setContextTokens('256000'); setMaxOutputTokens('8192');
    setToolCall(true); setCatalogReasoningProfile('catalog_standard');
    setCustomReasoning('provider_default');
    setInputModalities(['text']);
    setEffortValues('low, medium, high');
    setModelFormError(undefined);
  };

  const closeModelForm = () => {
    if (savingModel || testingModel) return;
    resetModelDraft(); setEditingConfiguration(undefined); setAdding(false);
  };

  const editModel = async (connection: ModelConfigurationSummary) => {
    setLoadingModelId(connection.id);
    try {
      const detail = await adapter.modelConfiguration(connection.id);
      const value = detail.configuration;
      resetModelDraft(); setDeleteCandidateId(undefined);
      setEditingConfiguration({ ...detail, id: connection.id });
      setConfigurationSource(value.source); setModelId(value.model_id); setWireApi(value.wire_api);
      if (value.source === 'models_dev') {
        setRouteId(value.route_id); setCatalogReasoningProfile(value.reasoning_wire_profile);
      } else {
        setConfigurationName(value.configuration_name); setBaseUrl(value.base_url);
        setAuthentication(value.authentication); setContextTokens(String(value.context_tokens));
        setMaxOutputTokens(String(value.max_output_tokens)); setToolCall(value.tool_call);
        setInputModalities(value.input_modalities); setCustomReasoning(value.reasoning.kind);
        if ('values' in value.reasoning) setEffortValues(value.reasoning.values.join(', '));
      }
      setAdding(true);
    } catch (loadError) {
      onNotify('模型配置无法读取', loadError instanceof Error ? loadError.message : '请刷新列表后重试。', 'warning');
    } finally { setLoadingModelId(undefined); }
  };

  const modelDraft = (): ModelConfigurationInput | undefined => {
    const apiKey = modelKey.current?.value || null;
    if (configurationSource === 'models_dev') {
      if (!routeId || !modelId || !wireApi || (!apiKey && !canKeepModelKey) || !selectedWire?.executable
        || !selectedWire.reasoning_wire_profiles?.includes(catalogReasoningProfile)) return undefined;
      return {
        source: 'models_dev', route_id: routeId, model_id: modelId, wire_api: wireApi,
        reasoning_wire_profile: catalogReasoningProfile, api_key: apiKey,
      };
    }
    const context = Number(contextTokens);
    const output = Number(maxOutputTokens);
    const efforts = parseEffortValues(effortValues);
    if (!configurationName.trim() || !baseUrl.trim() || !modelId.trim() || !wireApi || incompatibleCustomProfile
      || !Number.isInteger(context) || context < minimumContextTokens
      || !Number.isInteger(output) || output < 1 || output > context
      || (authentication === 'bearer_api_key' && !apiKey && !canKeepModelKey)
      || (effortReasoningProfiles.has(customReasoning) && !efforts.length)) return undefined;
    return {
      source: 'user_declared', configuration_name: configurationName.trim(),
      base_url: baseUrl.trim(), model_id: modelId.trim(), wire_api: wireApi,
      authentication, api_key: authentication === 'none' ? null : apiKey,
      context_tokens: context, max_output_tokens: output, tool_call: toolCall,
      input_modalities: inputModalities,
      reasoning: reasoningProfilePayload(customReasoning, efforts),
    };
  };

  const refreshCatalog = async () => {
    setCatalogRefreshing(true);
    try { setCatalog(await adapter.modelCatalog(true)); onNotify('模型目录已刷新', '选择器已经使用最新的 models.dev 快照。', 'success'); }
    catch (refreshError) { onNotify('模型目录未刷新', refreshError instanceof Error ? refreshError.message : '请稍后重试。', 'warning'); }
    finally { setCatalogRefreshing(false); }
  };

  const saveModel = async () => {
    const draft = modelDraft();
    if (!draft) return;
    setSavingModel(true); setModelFormError(undefined);
    const editingId = editingConfiguration?.id;
    try {
      const result = editingId
        ? await adapter.updateModelConfiguration(editingId, draft)
        : await adapter.addModelConfiguration(draft);
      const savedModels = editingId
        ? modelConfigurations.map((connection) => connection.id === editingId ? result.model_configuration : connection)
        : [...modelConfigurations, result.model_configuration];
      setSettings((current) => current ? { ...current, model_configurations: savedModels } : current);
      const savedIndex = savedModels.findIndex((connection) => connection.id === result.model_configuration.id);
      changeModelPage(Math.floor(Math.max(0, savedIndex) / modelPageSize));
      resetModelDraft(); setEditingConfiguration(undefined); setAdding(false);
      onNotify(editingId ? '模型配置已修改' : '模型配置已添加', connectionTitle(result.model_configuration), 'success');
      try {
        const nextSettings = await adapter.localSettings();
        setSettings(nextSettings);
        const refreshedIndex = nextSettings.model_configurations.findIndex((connection) => connection.id === result.model_configuration.id);
        changeModelPage(Math.floor(Math.max(0, refreshedIndex) / modelPageSize));
        await onConfigurationChanged();
      } catch { onNotify('配置已保存，但页面刷新失败', '请重新打开页面读取最新状态。', 'warning'); }
    } catch (saveError) {
      const detail = saveError instanceof Error ? saveError.message : '请检查所选模型与本机配置。';
      setModelFormError(detail);
      onNotify(editingId ? '模型配置未修改' : '模型配置未添加', detail, 'warning');
    } finally { setSavingModel(false); }
  };

  const testModel = async () => {
    const draft = modelDraft();
    if (!draft) return;
    setTestingModel(true); setModelFormError(undefined);
    try {
      if (editingConfiguration) await adapter.testModelConfiguration(draft, editingConfiguration.id);
      else await adapter.testModelConfiguration(draft);
      onNotify('连接测试通过', '所选 endpoint、模型与 API 协议完成了一次极短请求。', 'success');
    } catch (testError) {
      setModelFormError(testError instanceof Error ? testError.message : '请检查连接信息。');
      onNotify('连接测试未通过', testError instanceof Error ? testError.message : '请检查连接信息。', 'warning');
    } finally { setTestingModel(false); }
  };

  const deleteModel = async (connection: ModelConfigurationSummary) => {
    setDeletingModelId(connection.id);
    try {
      const result = await adapter.deleteModelConfiguration(connection.id);
      setSettings((current) => current ? {
        ...current,
        model_configurations: result.model_configurations,
      } : current);
      setModelPage((current) => Math.min(current, Math.max(0, Math.ceil(result.model_configurations.length / modelPageSize) - 1)));
      setDeleteCandidateId(undefined);
      onNotify(
        result.deleted ? '模型配置已删除' : '模型配置已经不存在',
        result.deleted
          ? '本机配置与其中的访问密钥已一并移除；已有会话不会自动改用其他模型。'
          : '页面已经刷新为当前配置。',
        'success',
      );
      try {
        await onConfigurationChanged();
      } catch (refreshError) {
        onNotify(
          '模型配置已删除，但页面刷新失败',
          refreshError instanceof Error ? refreshError.message : '重新打开设置页即可读取最新状态。',
          'warning',
        );
      }
    } catch (deleteError) {
      onNotify('模型配置未删除', deleteError instanceof Error ? deleteError.message : '请检查本机设置文件。', 'warning');
    } finally {
      setDeletingModelId(undefined);
    }
  };

  const updateDashScopeConfigured = (kind: CredentialKind, state: boolean) => setSettings((current) => current ? {
    ...current,
    local_settings: {
      ...current.local_settings,
      dashscope_credentials: kind === 'embedding'
        ? { ...current.local_settings.dashscope_credentials, embedding_configured: state }
        : { ...current.local_settings.dashscope_credentials, rerank_configured: state },
    },
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
    } catch (actionError) {
      setDatabaseMessage(actionError instanceof Error ? actionError.message : '数据库操作没有完成。');
      if (actionError instanceof RuntimeApiError && actionError.code === 'DATABASE_RESET_REQUIRED') {
        setSettings((current) => current ? { ...current, database_state: 'database_reset_required' } : current);
      }
    }
    finally { setDatabaseBusy(undefined); }
  };

  const resetDatabase = async () => {
    if (!resetTarget || databaseBusy || runtimeStatus !== 'online') return;
    setDatabaseBusy('reset'); setResetError(undefined);
    try {
      const result = await adapter.resetPostgres(resetTarget);
      setResetTarget(undefined);
      setDatabaseMessage(result.restart_required
        ? `数据库 ${result.database_name} 已重置。请重启 Pulsara 后继续使用。`
        : `数据库 ${result.database_name} 已重置并完成初始化。`);
      try {
        setSettings(await adapter.localSettings());
        await onConfigurationChanged();
      } catch {
        setDatabaseMessage(`数据库 ${result.database_name} 已重置，但页面状态未刷新。请重新打开页面检查；如果提示数据服务已关闭，请重启 Pulsara。`);
      }
    } catch (resetFailure) {
      setResetError(resetFailure instanceof Error ? resetFailure.message : '重置没有完成。');
    } finally { setDatabaseBusy(undefined); }
  };

  const databaseState = settings?.database_state ?? bootstrap?.database_state ?? 'database_not_configured';
  const databaseStateCopy = {
    ready: '数据服务已就绪', database_not_configured: '尚未配置 PostgreSQL',
    database_configured_unverified: '已保存，尚未检查 PostgreSQL',
    database_unavailable: '无法连接 PostgreSQL', database_schema_action_required: '需要初始化或升级数据库',
    database_reset_required: '现有数据与当前版本不兼容，需要重置',
    database_resetting: '正在重置数据', database_restart_required: '数据服务已关闭，请重启 Pulsara',
  }[databaseState];
  const selectedShapeWarning = Boolean(selectedModel?.wire_shape_hint && wireApi
    && ((selectedModel.wire_shape_hint === 'responses') !== (wireApi === 'openai_responses')));
  const parsedContextTokens = Number(contextTokens);
  const parsedMaxOutputTokens = Number(maxOutputTokens);
  const customDeclarationReady = Boolean(
    configurationName.trim() && baseUrl.trim() && modelId.trim() && wireApi
    && Number.isInteger(parsedContextTokens) && parsedContextTokens >= minimumContextTokens
    && Number.isInteger(parsedMaxOutputTokens) && parsedMaxOutputTokens >= 1
    && parsedMaxOutputTokens <= parsedContextTokens
    && (!effortReasoningProfiles.has(customReasoning) || parseEffortValues(effortValues).length),
  );
  const draftReady = configurationSource === 'models_dev'
    ? Boolean(routeId && modelId && wireApi && selectedWire?.executable && (keyPresent || canKeepModelKey)
      && selectedWire.reasoning_wire_profiles?.includes(catalogReasoningProfile))
    : customDeclarationReady && !incompatibleCustomProfile && (authentication === 'none' || keyPresent || canKeepModelKey);

  return <section className="surface-view settings-view">
    {resetTarget && <DatabaseResetDialog target={resetTarget} busy={databaseBusy === 'reset'} error={resetError} onClose={() => { if (!databaseBusy) setResetTarget(undefined); }} onConfirm={() => void resetDatabase()} />}
    <header className="page-header"><div><span className="page-kicker">本机设置</span><h1>设置</h1><p>配置模型、记忆检索与本机 PostgreSQL。访问密钥保存在权限受限的本机设置文件中。</p></div></header>
    <div className="settings-layout">
      <aside className="settings-nav">
        <div className="settings-profile"><span>PL</span><div><strong>本机用户</strong><small>无需账号登录</small></div></div>
        {navItems.map(({ id, label, icon: Icon }) => <button className={section === id ? 'is-active' : ''} key={id} onClick={() => setSection(id)}><Icon size={14} /><span>{label}</span><ChevronRight size={12} /></button>)}
        <div className="settings-version"><strong>Pulsara</strong><span>{bootstrap?.application.version ?? '本地版本'}</span><small>运行在这台 Mac 上</small></div>
      </aside>
      <div className="settings-content">
        {section === 'archived' && <ArchivedSessions adapter={adapter} revision={sessionRevision} onRestored={onSessionsChanged} onDelete={onDeleteSession} />}
        {error && <div className="settings-alert"><CircleAlert size={15} /><span>{error}</span><button onClick={() => void load()}>重试</button></div>}
        {loading && <div className="settings-loading"><LoaderCircle size={15} />正在读取本机设置…</div>}
        {section === 'general' && <section className="settings-group"><header><Palette size={16} /><div><h2>外观</h2><p>控制 Pulsara 在本机的呈现方式。</p></div></header>
          <SettingRow icon={theme === 'light' ? Sun : Moon} title="主题" detail="切换明暗外观"><div className="theme-picker"><button className={theme === 'light' ? 'is-active' : ''} onClick={() => onThemeChange('light')}><Sun size={12} /> 浅色</button><button className={theme === 'dark' ? 'is-active' : ''} onClick={() => onThemeChange('dark')}><Moon size={12} /> 深色</button></div></SettingRow>
          <SettingRow icon={SlidersHorizontal} title="显示内置工具原始结果" detail="显示原始文本、长结果预览和分页读取入口。仅影响界面，保存在当前浏览器。">
            <button type="button" role="switch" className="settings-switch" aria-label="显示内置工具原始结果" aria-checked={showBuiltinToolResults} onClick={() => onToolResultDisplayChange(!showBuiltinToolResults)}><span /></button>
          </SettingRow>
        </section>}
        {section === 'models' && <>
          <section className="settings-group model-settings-group"><header><Bot size={16} /><div><h2>模型配置</h2><p>每张卡片是一条可在会话中选择的独立连接。</p></div><button className="settings-header-action" disabled={Boolean(loadingModelId) || savingModel || testingModel} onClick={() => { if (adding) closeModelForm(); else { resetModelDraft(); setEditingConfiguration(undefined); setAdding(true); } }}><Plus size={13} />添加配置</button></header>
            {modelConfigurations.length ? <>
              <div className="model-card-list">{visibleModelConfigurations.map((connection) => <article className="model-config-card" key={connection.id}>
                <span className="model-config-card__icon"><Bot size={15} /></span>
                <span><strong title={connectionTitle(connection)}>{connectionTitle(connection)}</strong><small>{wireLabel(connection.wire_api)} · {formatTokens(connection.context_tokens)} 上下文</small><code title={connection.model_id}>{connection.model_id}</code></span>
                <div className="model-config-card__actions">
                  <span className={`credential-state credential-state--${connection.status === 'ready' ? 'present' : 'missing'}`}><i />{connection.status === 'ready' ? connectionCredentialLabel(connection) : '配置不可用'}</span>
                  {deleteCandidateId === connection.id ? <div className="model-delete-confirmation"><span>删除后，已有会话不会自动改用其他模型。</span><button disabled={deletingModelId === connection.id} onClick={() => setDeleteCandidateId(undefined)}>取消</button><button className="subtle-danger" disabled={deletingModelId === connection.id} onClick={() => void deleteModel(connection)}>{deletingModelId === connection.id ? <LoaderCircle size={13} /> : <Trash2 size={13} />}确认删除</button></div> : <div className="model-config-card__buttons"><button className="model-edit-trigger" aria-label={`修改模型配置 ${connectionTitle(connection)}`} disabled={Boolean(deletingModelId || loadingModelId) || savingModel || testingModel} onClick={() => void editModel(connection)}>{loadingModelId === connection.id ? <LoaderCircle size={13} /> : <Pencil size={13} />}修改</button><button className="model-delete-trigger subtle-danger" aria-label={`删除模型配置 ${connectionTitle(connection)}`} disabled={Boolean(deletingModelId || loadingModelId) || savingModel || testingModel} onClick={() => setDeleteCandidateId(connection.id)}><Trash2 size={13} />删除</button></div>}
                </div>
              </article>)}</div>
              {modelPageCount > 1 && <nav className="model-pagination" aria-label="模型配置分页">
                <span aria-live="polite">{modelPageStart + 1}–{modelPageStart + visibleModelConfigurations.length} / 共 {modelConfigurations.length} 项</span>
                <div><button disabled={currentModelPage === 0 || Boolean(deletingModelId)} onClick={() => changeModelPage(currentModelPage - 1)}><ChevronLeft size={13} />上一页</button><span>{currentModelPage + 1} / {modelPageCount}</span><button disabled={currentModelPage === modelPageCount - 1 || Boolean(deletingModelId)} onClick={() => changeModelPage(currentModelPage + 1)}>下一页<ChevronRight size={13} /></button></div>
              </nav>}
            </>
              : <div className="settings-empty"><Bot size={18} /><strong>还没有模型配置</strong><span>添加后，在每个会话的输入框下方显式选择要使用的连接。</span></div>}
          </section>
          {adding && <ModelFormContainer editing={Boolean(editingConfiguration)} busy={savingModel || testingModel} onClose={closeModelForm}><section className="settings-group model-add-panel"><header>{editingConfiguration ? <Pencil size={16} /> : <Plus size={16} />}<div><h2>{editingConfiguration ? '修改模型配置' : '添加模型配置'}</h2><p>保存与测试相互独立；只有测试会发送一次极短模型请求。</p></div>{configurationSource === 'models_dev' && <button className="settings-header-action" disabled={catalogRefreshing} onClick={() => void refreshCatalog()}><RefreshCw size={13} />刷新目录</button>}{editingConfiguration && <button className="settings-header-action" aria-label="关闭修改模型配置" onClick={closeModelForm}><X size={15} /></button>}</header>
            {editingConfiguration ? <div className="model-edit-notice"><strong>{configurationSource === 'models_dev' ? 'Models.dev 目录' : '自定义服务'}</strong><p>修改后会保留这条配置。更改模型、地址、协议、能力或推理请求形状后，请在相关会话空闲时重新载入运行时；已有会话不会自动切换，后续请求可能因配置不匹配而停止。</p></div> : <div className="model-source-picker" role="group" aria-label="模型配置来源"><button className={configurationSource === 'models_dev' ? 'is-active' : ''} onClick={() => { resetModelDraft(); setConfigurationSource('models_dev'); }}>Models.dev 目录</button><button className={configurationSource === 'user_declared' ? 'is-active' : ''} onClick={() => { resetModelDraft(); setConfigurationSource('user_declared'); }}>自定义服务</button></div>}
            {modelFormError && <p role="alert" className="settings-alert settings-alert--inside">{modelFormError}</p>}
            {configurationSource === 'models_dev' ? <>
              <div className="settings-form-grid">
                <label><span>提供方</span><select value={routeId} onChange={(event) => { setRouteId(event.target.value); setModelId(''); setWireApi(''); setCatalogReasoningProfile('catalog_standard'); }}><option value="">选择 Provider</option>{routeId && !selectedRoute && <option value={routeId}>{routeId} · 当前目录不可用</option>}{selectableRoutes.map((route) => <option key={route.route_id} value={route.route_id}>{route.display_name}</option>)}</select></label>
                <label><span>模型</span><select value={modelId} disabled={!selectedRoute} onChange={(event) => { setModelId(event.target.value); setWireApi(''); setCatalogReasoningProfile('catalog_standard'); }}><option value="">选择 Model ID</option>{modelId && !selectedModel && <option value={modelId}>{modelId} · 当前目录不可用</option>}{selectableModels.map((model) => <option key={model.model_id} value={model.model_id}>{model.display_name} · {model.model_id}</option>)}</select></label>
                <label><span>API 协议</span><select value={wireApi} disabled={!selectedModel || !hasExecutableWire} onChange={(event) => { setWireApi(event.target.value as typeof wireApi); setCatalogReasoningProfile('catalog_standard'); }}><option value="">选择 Chat 或 Responses</option>{selectedModel?.wire_apis.map((wire) => <option key={wire.wire_api} value={wire.wire_api} disabled={!wire.executable}>{wireLabel(wire.wire_api)}{wire.recommended ? ' · models.dev 建议' : ''}{wire.executable ? '' : ' · 暂不支持'}</option>)}</select></label>
                <label><span>Reasoning 请求形状</span><select aria-label="Reasoning 请求形状" value={catalogReasoningProfile} disabled={!selectedWire} onChange={(event) => setCatalogReasoningProfile(event.target.value as ReasoningWireProfile)}>{!selectedWire?.reasoning_wire_profiles?.includes(catalogReasoningProfile) && <option value={catalogReasoningProfile}>{reasoningProfileLabel(catalogReasoningProfile)} · 当前不可用</option>}{selectedWire?.reasoning_wire_profiles?.map((profile) => <option key={profile} value={profile}>{reasoningProfileLabel(profile)}</option>)}</select><small>{catalogReasoningProfileDescription(catalogReasoningProfile, selectedWire?.reasoning?.kind)}</small></label>
                <label><span>API key</span><input ref={modelKey} aria-label="API key" type="password" autoComplete="new-password" placeholder={canKeepModelKey ? '留空保留现有密钥' : '保存到本机配置'} onChange={(event) => setKeyPresent(Boolean(event.target.value))} />{editingConfiguration && <small>{canKeepModelKey ? '留空保留现有密钥，填写则替换。' : '服务地址已改变或没有现有密钥，请重新填写。'}</small>}</label>
              </div>
              {selectedModel && !hasExecutableWire && <p className="inline-warning"><CircleAlert size={13} />该提供方没有声明 OpenAI-compatible 接口，无法使用通用 Chat / Responses adapter。</p>}
              {selectedModel && <div className="model-confirmation"><strong>确认连接</strong><dl><div><dt>提供方</dt><dd>{selectedRoute?.display_name}</dd></div><div><dt>模型</dt><dd>{selectedModel.model_id}</dd></div><div><dt>协议</dt><dd>{selectedWire ? wireLabel(selectedWire.wire_api) : '未选择'}</dd></div><div><dt>推理形状</dt><dd>{selectedWire ? reasoningProfileLabel(catalogReasoningProfile) : '未选择'}</dd></div><div><dt>Endpoint</dt><dd>{selectedWire?.endpoint ?? '目录未提供'}</dd></div><div><dt>上下文</dt><dd>{formatTokens(selectedModel.context_tokens)}</dd></div><div><dt>输入模态</dt><dd>{formatModalities(selectedModel.input_modalities)}</dd></div><div><dt>输出模态</dt><dd>{formatModalities(selectedModel.output_modalities)}</dd></div></dl><p>模态与推理能力来自 models.dev；请求形状由你显式选择，Pulsara 不会根据服务商或模型名称猜测。</p>{selectedShapeWarning && <p className="inline-warning"><CircleAlert size={13} />所选协议与 models.dev 建议不同；仍可保存，调用失败时请返回这里添加另一配置。</p>}<p>Pulsara 当前只支持与 OpenAI Chat Completions 或 Responses 兼容的接口。模型出现在目录中不代表所选提供方一定支持你选择的 API 协议。</p><div className="form-actions"><button onClick={closeModelForm}>取消</button><button disabled={!draftReady || savingModel || testingModel} onClick={() => void testModel()}>{testingModel ? <LoaderCircle size={13} /> : <RefreshCw size={13} />}测试连接</button><button className="primary-action" disabled={!draftReady || savingModel || testingModel} onClick={() => void saveModel()}>{savingModel ? <LoaderCircle size={13} /> : <Check size={13} />}{editingConfiguration ? '保存修改' : '保存配置'}</button></div></div>}
            </> : <>
              <div className="settings-form-grid">
                <label><span>配置名称</span><input value={configurationName} onChange={(event) => setConfigurationName(event.target.value)} placeholder="例如：公司内网模型" /></label>
                <label><span>Base URL</span><input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://example.com/v1" /></label>
                <label><span>Model ID</span><input value={modelId} onChange={(event) => setModelId(event.target.value)} placeholder="实际发送给 endpoint 的模型 ID" /></label>
                <label><span>API 协议</span><select value={wireApi} onChange={(event) => { const value = event.target.value as typeof wireApi; setWireApi(value); if (value === 'openai_responses' && chatOnlyReasoningProfiles.has(customReasoning)) setCustomReasoning('provider_default'); }}><option value="">选择 Chat 或 Responses</option><option value="openai_chat_completions">Chat Completions</option><option value="openai_responses">Responses</option></select></label>
                <label><span>认证方式</span><select value={authentication} onChange={(event) => { const value = event.target.value as typeof authentication; setAuthentication(value); if (value === 'none' && modelKey.current) { modelKey.current.value = ''; setKeyPresent(false); } }}><option value="bearer_api_key">Bearer API key</option><option value="none">无需认证</option></select></label>
                {authentication === 'bearer_api_key' && <label><span>API key</span><input ref={modelKey} aria-label="API key" type="password" autoComplete="new-password" placeholder={canKeepModelKey ? '留空保留现有密钥' : '保存到本机配置'} onChange={(event) => setKeyPresent(Boolean(event.target.value))} />{editingConfiguration && <small>{canKeepModelKey ? '留空保留现有密钥，填写则替换。' : '服务地址或认证方式已改变，请填写密钥。'}</small>}</label>}
                <label><span>Context window</span><input type="number" min={minimumContextTokens} step="1" value={contextTokens} onChange={(event) => setContextTokens(event.target.value)} /></label>
                <label><span>最大输出长度</span><input type="number" min="1" step="1" value={maxOutputTokens} onChange={(event) => setMaxOutputTokens(event.target.value)} /></label>
                <label><span>Reasoning 请求形状</span><select aria-label="Reasoning 请求形状" value={incompatibleCustomProfile ? '' : customReasoning} onChange={(event) => setCustomReasoning(event.target.value as CustomReasoningKind)}>{incompatibleCustomProfile && <option value="" disabled>原请求形状不支持此协议，请重新选择</option>}<option value="provider_default">由服务端决定</option><option value="effort">标准 effort</option><option value="toggle">推理开关</option>{wireApi !== 'openai_responses' && <><option value="enable_thinking">enable_thinking 开关</option><option value="thinking_type">thinking.type 开关</option><option value="thinking_effort">thinking.type + effort</option><option value="broad_compat">多字段兼容组合</option></>}</select><small>{reasoningProfileDescription(customReasoning)}</small></label>
                {effortReasoningProfiles.has(customReasoning) && <label><span>Effort 列表</span><input value={effortValues} onChange={(event) => setEffortValues(event.target.value)} placeholder="low, medium, high" /></label>}
                <label className="model-boolean-field"><input type="checkbox" checked={toolCall} onChange={(event) => setToolCall(event.target.checked)} /><span>支持 tool calling</span></label>
                <label className="model-boolean-field"><input type="checkbox" checked={inputModalities?.includes('image') ?? false} onChange={(event) => { const checked = event.target.checked; setInputModalities((values) => checked ? [...(values ?? ['text']), 'image'] : (values ?? ['text']).filter((value) => value !== 'image')); }} /><span>支持图像输入</span></label>
              </div>
              <div className="model-confirmation"><strong>用户声明的连接事实</strong>{customDeclarationReady && <dl><div><dt>配置</dt><dd>{configurationName.trim()}</dd></div><div><dt>模型</dt><dd>{modelId.trim()}</dd></div><div><dt>协议</dt><dd>{wireApi ? wireLabel(wireApi) : '未选择'}</dd></div><div><dt>Endpoint</dt><dd>{baseUrl.trim()}</dd></div><div><dt>推理形状</dt><dd>{reasoningProfileLabel(customReasoning)}</dd></div><div><dt>上下文</dt><dd>{formatTokens(parsedContextTokens)}</dd></div><div><dt>输入模态</dt><dd>{formatModalities(inputModalities)}</dd></div><div><dt>认证</dt><dd>{authentication === 'none' ? '无需认证' : 'Bearer API key'}</dd></div></dl>}<p>这些能力由你直接声明，不会根据服务商或模型名称自动判断，也不会在失败后偷偷换参数重试。Pulsara 只使用通用 Chat / Responses adapter。</p><p>Context window 至少为 256,000。拿不准请求形状时请选择“由服务端决定”。</p><div className="form-actions"><button onClick={closeModelForm}>取消</button><button disabled={!draftReady || savingModel || testingModel} onClick={() => void testModel()}>{testingModel ? <LoaderCircle size={13} /> : <RefreshCw size={13} />}测试连接</button><button className="primary-action" disabled={!draftReady || savingModel || testingModel} onClick={() => void saveModel()}>{savingModel ? <LoaderCircle size={13} /> : <Check size={13} />}{editingConfiguration ? '保存修改' : '保存配置'}</button></div></div>
            </>}
          </section></ModelFormContainer>}
          <section className="settings-group"><header><KeyRound size={16} /><div><h2>记忆检索 · 阿里云百炼 DashScope</h2><p>可选的相关性增强；未配置不会阻止对话或基础记忆路径。</p></div></header>{settings && <div className="credential-list"><DashScopeCredentialRow adapter={adapter} kind="embedding" state={settings.local_settings.dashscope_credentials.embedding_configured} onChanged={(state) => updateDashScopeConfigured('embedding', state)} onNotify={onNotify} /><DashScopeCredentialRow adapter={adapter} kind="rerank" state={settings.local_settings.dashscope_credentials.rerank_configured} onChanged={(state) => updateDashScopeConfigured('rerank', state)} onNotify={onNotify} /></div>}</section>
        </>}
        {section === 'service' && <>
          <section className="settings-group"><header><Database size={16} /><div><h2>PostgreSQL</h2><p>会话数据平面使用本机 PostgreSQL；保存不会自动连接或迁移。</p></div></header>
            {settings?.local_settings.state === 'unavailable' && <div className="settings-alert settings-alert--inside"><CircleAlert size={15} /><span>本机设置文件无法读取。保存下面的完整 PostgreSQL 配置会覆盖并修复该文件。</span></div>}
            <div className="database-state-row"><span className={`database-state database-state--${databaseState}`}><i />{databaseStateCopy}</span><small>浏览器设置壳：{statusLabels[runtimeStatus]}</small></div>
            <div className="database-form"><label><span>Runtime DSN</span><input value={runtimeDsn} onChange={(event) => setRuntimeDsn(event.target.value)} placeholder="postgresql://pulsara:…@localhost:5432/pulsara" /></label><label><span>Admin DSN（可选）</span><input value={adminDsn} onChange={(event) => setAdminDsn(event.target.value)} placeholder="只用于显式初始化、升级或重置" /></label><p>初始化/升级将使用上方已保存的 Admin DSN 管理同一数据库，并按 Runtime DSN 验证运行角色。请先核对目标数据库与角色。</p>{databaseMessage && <div className="database-message">{databaseMessage}</div>}<div className="form-actions"><button className="primary-action" disabled={Boolean(databaseBusy) || !runtimeDsn.trim()} onClick={() => void saveDatabase()}>{databaseBusy === 'save' ? <LoaderCircle size={13} /> : <Check size={13} />}保存连接</button><button disabled={Boolean(databaseBusy) || !settings?.local_settings.postgres} onClick={() => void databaseAction('check')}>{databaseBusy === 'check' ? <LoaderCircle size={13} /> : <RefreshCw size={13} />}检查连接</button><button disabled={Boolean(databaseBusy) || !settings?.local_settings.postgres?.admin_dsn || databaseState === 'database_reset_required'} onClick={() => void databaseAction('migrate')}>{databaseBusy === 'migrate' ? <LoaderCircle size={13} /> : <Database size={13} />}初始化 / 升级</button><button className="subtle-danger" disabled={Boolean(databaseBusy) || runtimeStatus !== 'online' || !settings?.local_settings.postgres?.admin_dsn || databaseState === 'database_resetting'} onClick={() => { setResetError(undefined); setResetTarget(settings!.local_settings.postgres!); }}><Trash2 size={13} />重置数据…</button></div></div>
          </section>
          <section className="settings-group"><header><HardDrive size={16} /><div><h2>本地服务</h2><p>Pulsara 的设置与任务都由这台设备上的进程管理。</p></div></header><SettingRow icon={HardDrive} title="页面连接" detail="浏览器与本地设置服务"><span className={`connection-value connection-value--${runtimeStatus}`} role="status"><i aria-hidden="true" />{statusLabels[runtimeStatus]}</span></SettingRow></section>
        </>}
      </div>
    </div>
  </section>;
}
