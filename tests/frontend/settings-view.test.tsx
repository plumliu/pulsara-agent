import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SettingsView } from '../../frontend/components/settings-view';
import type { LocalSettingsReadModel, ModelCatalogReadModel, ModelConfigurationDetail, ModelConfigurationInput, ModelConfigurationSummary, RuntimeAdapter } from '../../frontend/lib/runtime-adapter';

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute('open', ''); } });
});
afterEach(() => { cleanup(); Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal"); vi.restoreAllMocks(); });

function modelConfiguration(index: number): ModelConfigurationSummary {
  return {
    id: `connection-${index}`, source: 'user_declared', route_id: 'test', route_name: 'Test',
    display_name: `Model ${index}`, model_id: `model-${index}`, base_url: 'http://localhost:8000/v1',
    wire_api: 'openai_chat_completions', authentication: 'none', credential_configured: false,
    status: 'ready', context_tokens: 256_000, input_modalities: ['text', 'image'],
    reasoning: { kind: 'selectable', effort: { values: ['low', 'high'] } }, reasoning_wire_profile: 'effort',
  };
}

function setup(configurations: ModelConfigurationSummary[] = [], detail?: ModelConfigurationDetail, catalog: ModelCatalogReadModel = { status: 'ready', routes: [] }) {
  let settings: LocalSettingsReadModel = {
    local_settings: { pulsara_home: '/custom/pulsara-home', memory_retrieval: { embedding: null, embedding_enabled: false, history_enabled: true, rerank: null, decision: null, ranking_mode: 'off' } },
    database_state: 'database_reset_required', model_configurations: configurations,
  };
  const reset = vi.fn(async () => {
    settings = { ...settings, database_state: 'ready' };
    return { database_name: 'pulsara', restart_required: false };
  });
  const refresh = vi.fn(async () => {});
  const notify = vi.fn();
  const check = vi.fn(async () => ({database_name:'pulsara'}));
  const deleteModel = vi.fn(async (id: string) => {
    settings = { ...settings, model_configurations: settings.model_configurations.filter((connection) => connection.id !== id) };
    return { deleted: true, model_configurations: settings.model_configurations };
  });
  const addModel = vi.fn(async (_input: ModelConfigurationInput) => {
    const connection = modelConfiguration(settings.model_configurations.length + 1);
    settings = { ...settings, model_configurations: [...settings.model_configurations, connection] };
    return { model_configuration: connection, wire_shape_warning: false };
  });
  const readModel = vi.fn(async () => {
    if (!detail) throw new Error('unexpected edit');
    return detail;
  });
  const updateModel = vi.fn(async (id: string, input: ModelConfigurationInput) => {
    const current = settings.model_configurations.find((item) => item.id === id)!;
    const connection = { ...current, model_id: input.model_id, route_name: input.source === 'user_declared' ? input.configuration_name : current.route_name };
    settings = { ...settings, model_configurations: settings.model_configurations.map((item) => item.id === id ? connection : item) };
    return { model_configuration: connection, wire_shape_warning: false };
  });
  const probeModel = vi.fn(async () => ({ status: 'ready' as const }));
  const discoverModels = vi.fn(async () => ({ model_ids: ['alpha-model', 'beta-model', 'org/alpha-large'] }));
  const adapter = {
    localSettings: async () => settings,
    modelCatalog: async () => catalog,
    resetPostgres: reset,
    checkPostgres: check,
    deleteModelConfiguration: deleteModel,
    addModelConfiguration: addModel,
    modelConfiguration: readModel,
    updateModelConfiguration: updateModel,
    testModelConfiguration: probeModel,
    discoverModels,
  } as unknown as RuntimeAdapter;
  render(<SettingsView adapter={adapter} theme="light" runtimeStatus="online" onThemeChange={() => {}} onConfigurationChanged={refresh} onNotify={notify} sessionRevision={0} onSessionsChanged={refresh} onDeleteSession={() => {}} />);
  return { reset, refresh, notify, check, deleteModel, addModel, readModel, updateModel, probeModel, discoverModels };
}

function customDetail(inputModalities: string[] | null = null): ModelConfigurationDetail {
  return {
    base_url: 'https://private.example/v1', credential_configured: true,
    configuration: {
      source: 'user_declared', configuration_name: 'Saved Custom', base_url: 'https://private.example/v1',
      model_id: 'custom-model', wire_api: 'openai_chat_completions', authentication: 'bearer_api_key', api_key: null,
      context_tokens: 300_000, max_output_tokens: 16_384, tool_call: false, input_modalities: inputModalities,
      reasoning: { kind: 'broad_compat', values: ['none', 'medium', 'high'] },
    },
  };
}

it.each([{ modalities: null }, { modalities: ['text', 'image', 'audio', 'future-input'] }])('edits exact custom declarations without losing modalities ($modalities) or exposing the key', async ({ modalities }) => {
  const detail = customDetail(modalities);
  const { readModel, updateModel, addModel, probeModel } = setup([modelConfiguration(1)], detail);
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  fireEvent.click(await screen.findByRole('button', { name: '修改模型配置 Test · Model 1' }));
  const dialog = await screen.findByRole('dialog', { name: '修改模型配置' });
  expect(readModel).toHaveBeenCalledExactlyOnceWith('connection-1');
  expect(within(dialog).getByLabelText('配置名称')).toHaveProperty('value', 'Saved Custom');
  expect(within(dialog).getByLabelText('上下文窗口大小')).toHaveProperty('value', '300000');
  expect(within(dialog).queryByText('最大输出长度')).toBeNull();
  expect(within(dialog).getByLabelText('支持 tool calling')).toHaveProperty('checked', false);
  expect(within(dialog).getByLabelText('Reasoning 请求形状')).toHaveProperty('value', 'broad_compat');
  expect(within(dialog).getByLabelText('Effort 列表')).toHaveProperty('value', 'none, medium, high');
  expect(within(dialog).getByLabelText('API key')).toHaveProperty('value', '');
  expect(within(dialog).queryByRole('group', { name: '模型配置来源' })).toBeNull();
  fireEvent.change(within(dialog).getByLabelText('配置名称'), { target: { value: 'Edited' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '测试连接' }));
  await waitFor(() => expect(probeModel).toHaveBeenCalledExactlyOnceWith({ ...detail.configuration, configuration_name: 'Edited' }, 'connection-1'));
  expect(updateModel).not.toHaveBeenCalled();
  await waitFor(() => expect((within(dialog).getByRole('button', { name: '保存修改' }).closest('fieldset') as HTMLFieldSetElement).disabled).toBe(false));
  fireEvent.click(within(dialog).getByRole('button', { name: '保存修改' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(updateModel).toHaveBeenCalledExactlyOnceWith('connection-1', { ...detail.configuration, configuration_name: 'Edited' });
  expect(addModel).not.toHaveBeenCalled();
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(screen.getByText('Edited · Model 1')).toBeTruthy();
});

it('requires a fresh key for a changed endpoint, preserves failed drafts and cancels without saving', async () => {
  const { updateModel, probeModel } = setup([modelConfiguration(1)], customDetail());
  updateModel.mockRejectedValueOnce(new Error('写入失败，请重试。'));
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  fireEvent.click(await screen.findByRole('button', { name: '修改模型配置 Test · Model 1' }));
  const dialog = await screen.findByRole('dialog', { name: '修改模型配置' });
  fireEvent.change(within(dialog).getByLabelText('Base URL'), { target: { value: 'https://another.example/v1' } });
  expect(within(dialog).getByRole('button', { name: '保存修改' })).toHaveProperty('disabled', true);
  expect(within(dialog).getByRole('button', { name: '测试连接' })).toHaveProperty('disabled', true);
  fireEvent.change(within(dialog).getByLabelText('API key'), { target: { value: 'new-key-sentinel' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '保存修改' }));
  expect(await within(dialog).findByRole('alert')).toHaveProperty('textContent', '写入失败，请重试。');
  expect(within(dialog).getByLabelText('Base URL')).toHaveProperty('value', 'https://another.example/v1');
  expect(dialog.textContent).not.toContain('new-key-sentinel');
  fireEvent.click(within(dialog).getByRole('button', { name: '取消' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(updateModel).toHaveBeenCalledTimes(1);
  expect(probeModel).not.toHaveBeenCalled();
});

it('edits a catalog profile using the saved selection and matching catalog choices', async () => {
  const detail: ModelConfigurationDetail = {
    base_url: 'https://catalog.example/v1', credential_configured: true,
    configuration: { source: 'models_dev', context_window_tokens: null, route_id: 'test', model_id: 'catalog-model', wire_api: 'openai_chat_completions', reasoning_wire_profile: 'thinking_type', api_key: null },
  };
  const catalog: ModelCatalogReadModel = { status: 'ready', routes: [{ route_id: 'test', display_name: 'Catalog Test', models: [{
    model_id: 'catalog-model', display_name: 'Catalog Model', wire_dialect: 'openai_compatible', context_tokens: 256_000, input_tokens: 256_000, output_tokens: 8192, tool_call: true, input_modalities: ['text'], output_modalities: ['text'], wire_shape_hint: null,
    wire_apis: [{ wire_api: 'openai_chat_completions', executable: true, reason: null, endpoint: detail.base_url, reasoning: { kind: 'fixed_on' }, reasoning_wire_profiles: ['catalog_standard', 'thinking_type'] }],
  }] }] };
  const { updateModel } = setup([{ ...modelConfiguration(1), source: 'models_dev' }], detail, catalog);
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  fireEvent.click(await screen.findByRole('button', { name: '修改模型配置 Test · Model 1' }));
  const dialog = await screen.findByRole('dialog', { name: '修改模型配置' });
  expect(within(dialog).getByLabelText('提供方')).toHaveProperty('value', 'test');
  expect(within(dialog).getByLabelText('模型')).toHaveProperty('value', 'catalog-model');
  expect(within(dialog).getByLabelText('Reasoning 请求形状')).toHaveProperty('value', 'thinking_type');
  expect(within(dialog).queryByLabelText('Effort 列表')).toBeNull();
  fireEvent.change(within(dialog).getByLabelText('Reasoning 请求形状'), { target: { value: 'catalog_standard' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '保存修改' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(updateModel).toHaveBeenCalledExactlyOnceWith('connection-1', { ...detail.configuration, reasoning_wire_profile: 'catalog_standard' });
});

it('edits a catalog context allowance within its limits without changing output limits', async () => {
  const detail: ModelConfigurationDetail = {
    base_url: 'https://catalog.example/v1', credential_configured: true,
    configuration: { source: 'models_dev', context_window_tokens: 512_000, route_id: 'test', model_id: 'catalog-model', wire_api: 'openai_chat_completions', reasoning_wire_profile: 'catalog_standard', api_key: null },
  };
  const catalog: ModelCatalogReadModel = { status: 'ready', routes: [{ route_id: 'test', display_name: 'Test', models: [{
    model_id: 'catalog-model', display_name: 'Catalog Model', wire_dialect: 'openai_compatible', context_tokens: 1_000_000, input_tokens: 1_000_000, output_tokens: 384_000, tool_call: true, input_modalities: ['text'], output_modalities: ['text'], wire_shape_hint: null,
    wire_apis: [{ wire_api: 'openai_chat_completions', executable: true, reason: null, endpoint: detail.base_url, reasoning: { kind: 'provider_default' }, reasoning_wire_profiles: ['catalog_standard'] }],
  }] }] };
  const { updateModel } = setup([{ ...modelConfiguration(1), source: 'models_dev' }], detail, catalog);
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  fireEvent.click(await screen.findByRole('button', { name: '修改模型配置 Test · Model 1' }));
  const dialog = await screen.findByRole('dialog', { name: '修改模型配置' });
  const allowance = within(dialog).getByLabelText('上下文窗口大小');
  expect(allowance).toHaveProperty('value', '512000');
  expect(allowance.getAttribute('min')).toBe('256000');
  expect(allowance.getAttribute('max')).toBe('1000000');
  expect(within(dialog).queryByText('最大输出长度')).toBeNull();
  for (const invalid of ['255999', '1000001', '256000.5']) {
    fireEvent.change(allowance, { target: { value: invalid } });
    expect(within(dialog).getByRole('button', { name: '保存修改' })).toHaveProperty('disabled', true);
  }
  fireEvent.change(allowance, { target: { value: '256000' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '保存修改' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(updateModel).toHaveBeenCalledExactlyOnceWith('connection-1', { ...detail.configuration, context_window_tokens: 256000 });
});

it('paginates all saved models in compact summaries and clears pending deletion when changing pages', async () => {
  const { deleteModel } = setup(Array.from({ length: 11 }, (_, index) => modelConfiguration(index + 1)));
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  expect(await screen.findByText('Test · Model 1')).toBeTruthy();
  expect(screen.getAllByRole('article')).toHaveLength(5);
  expect(screen.getByText('1–5 / 共 11 项')).toBeTruthy();
  expect(screen.queryByText(/输入：|推理请求：/)).toBeNull();
  expect(screen.getByRole('button', { name: '上一页' })).toHaveProperty('disabled', true);
  fireEvent.click(screen.getByRole('button', { name: '删除模型配置 Test · Model 1' }));
  expect(screen.getByRole('button', { name: '确认删除' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  expect(screen.getByText('6–10 / 共 11 项')).toBeTruthy();
  expect(screen.queryByText('Test · Model 1')).toBeNull();
  for (let index = 6; index <= 10; index += 1) expect(screen.getByText(`Test · Model ${index}`)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  expect(screen.getByText('11–11 / 共 11 项')).toBeTruthy();
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(screen.getByText('Test · Model 11')).toBeTruthy();
  expect(screen.getByRole('button', { name: '下一页' })).toHaveProperty('disabled', true);
  fireEvent.click(screen.getByRole('button', { name: '上一页' }));
  fireEvent.click(screen.getByRole('button', { name: '上一页' }));
  expect(screen.queryByRole('button', { name: '确认删除' })).toBeNull();
  expect(deleteModel).not.toHaveBeenCalled();
});

it('returns to the preceding page after deleting the only model on the last page', async () => {
  const { deleteModel } = setup(Array.from({ length: 6 }, (_, index) => modelConfiguration(index + 1)));
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  await screen.findByText('Test · Model 1');
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  fireEvent.click(screen.getByRole('button', { name: '删除模型配置 Test · Model 6' }));
  fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
  expect(await screen.findByText('Test · Model 1')).toBeTruthy();
  expect(deleteModel).toHaveBeenCalledExactlyOnceWith('connection-6');
  expect(screen.getAllByRole('article')).toHaveLength(5);
  expect(screen.queryByRole('navigation', { name: '模型配置分页' })).toBeNull();
  expect(screen.queryByText('Test · Model 6')).toBeNull();
});

it('shows the page containing a newly saved model while retaining capability controls in the form', async () => {
  const { addModel } = setup(Array.from({ length: 5 }, (_, index) => modelConfiguration(index + 1)));
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  await screen.findByText('Test · Model 1');
  fireEvent.click(screen.getByRole('button', { name: '添加配置' }));
  const dialog = await screen.findByRole('dialog', { name: '添加模型配置' });
  expect(within(dialog).queryByText('最大输出长度')).toBeNull();
  fireEvent.click(within(dialog).getByRole('button', { name: '自定义服务' }));
  expect(within(dialog).queryByText('最大输出长度')).toBeNull();
  fireEvent.change(within(dialog).getByLabelText('配置名称'), { target: { value: 'Test' } });
  fireEvent.change(within(dialog).getByLabelText('Base URL'), { target: { value: 'http://localhost:8000/v1' } });
  fireEvent.change(within(dialog).getByLabelText('Model ID'), { target: { value: 'model-6' } });
  fireEvent.change(within(dialog).getByLabelText('API 协议'), { target: { value: 'openai_chat_completions' } });
  fireEvent.change(within(dialog).getByLabelText('认证方式'), { target: { value: 'none' } });
  fireEvent.change(within(dialog).getByLabelText('Reasoning 请求形状'), { target: { value: 'effort' } });
  fireEvent.click(within(dialog).getByLabelText('支持图像输入'));
  fireEvent.click(within(dialog).getByRole('button', { name: '保存配置' }));
  expect(await screen.findByText('Test · Model 6')).toBeTruthy();
  expect(screen.getByText('6–6 / 共 6 项')).toBeTruthy();
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(screen.queryByRole('heading', { name: '添加模型配置' })).toBeNull();
  expect(addModel).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
    max_output_tokens: 8192, input_modalities: ['text', 'image'], reasoning: { kind: 'effort', values: ['low', 'medium', 'high'] },
  }));
});

it('closes the add modal without saving and resets its draft when reopened', async () => {
  const { addModel } = setup();
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  fireEvent.click(await screen.findByRole('button', { name: '添加配置' }));
  const dialog = await screen.findByRole('dialog', { name: '添加模型配置' });
  fireEvent.click(within(dialog).getByRole('button', { name: '自定义服务' }));
  fireEvent.change(within(dialog).getByLabelText('配置名称'), { target: { value: 'Unsaved' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '关闭添加模型配置' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(addModel).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '添加配置' }));
  const reopened = await screen.findByRole('dialog', { name: '添加模型配置' });
  expect(within(reopened).getByLabelText('配置名称')).toHaveProperty('value', '');
  fireEvent.click(within(reopened).getByRole('button', { name: '取消' }));
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('keeps local service in General with only database actions and home', async () => {
  const { check, notify } = setup();
  expect(screen.queryByRole('button', {name:'本地服务'})).toBeNull();
  expect(screen.queryByText('页面连接')).toBeNull();
  const card = await screen.findByRole('region', {name:'本地服务'});
  expect(within(card).getByText('PostgreSQL')).toBeTruthy();
  expect(within(card).getAllByRole('button').map(button => button.textContent)).toEqual(['检查数据', '重置数据']);
  fireEvent.click(within(card).getByRole('button', {name:'检查数据'}));
  await waitFor(() => expect(check).toHaveBeenCalledOnce());
  expect(notify).toHaveBeenCalledWith('数据检查通过', '数据库 pulsara 连接与结构检查通过。', 'success');
});

it('shows the managed target without editable DSNs and resets only after confirmation', async () => {
  const { reset, refresh, notify } = setup();
  const opener = await screen.findByRole('button', { name: '重置数据' });
  expect(screen.queryByRole('button', { name: '初始化 / 升级' })).toBeNull();
  expect(screen.queryByLabelText('Runtime DSN')).toBeNull();
  expect(screen.queryByLabelText('Admin DSN')).toBeNull();
  fireEvent.click(opener);
  let dialog = screen.getByRole('dialog', { name: '重置 Pulsara 数据？' });
  expect(within(dialog).getByText('目标：此应用管理的专用 PostgreSQL 数据库。')).toBeTruthy();
  expect(reset).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button', { name: '取消' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(reset).not.toHaveBeenCalled();
  fireEvent.click(opener);
  dialog = screen.getByRole('dialog', { name: '重置 Pulsara 数据？' });
  fireEvent.click(within(dialog).getByRole('button', { name: '确认清空并初始化' }));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  expect(reset).toHaveBeenCalledExactlyOnceWith();
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(notify).toHaveBeenCalledWith('数据已重置', '数据库 pulsara 已重置并完成初始化。', 'success');
});

it('keeps a failed reset visible without automatically retrying', async () => {
  const { reset } = setup();
  reset.mockRejectedValue(new Error('目标已变更，请重新确认。'));
  const opener = await screen.findByRole('button', { name: '重置数据' });
  await waitFor(() => expect((opener as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(opener);
  fireEvent.click(screen.getByRole('button', { name: '确认清空并初始化' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '目标已变更，请重新确认。');
  expect(reset).toHaveBeenCalledOnce();
  expect(screen.getByRole('dialog')).toBeTruthy();
});


it('shows the effective Pulsara Home beneath PostgreSQL in General', async () => {
  setup();
  fireEvent.click(screen.getByRole('button', { name: '通用' }));
  const home = screen.getByRole('region', { name: 'Pulsara 家目录' });
  const path = within(home).getByRole('textbox', { name: 'Pulsara 家目录' });
  await waitFor(() => expect(path).toHaveProperty('value', '/custom/pulsara-home'));
  expect(path).toHaveProperty('disabled', true);
  path.focus();
  expect(document.activeElement).not.toBe(path);
  expect(within(home).queryByText('当前路径，只读')).toBeNull();
  expect(within(home).getByText('保存用户级技能、MCP、插件和模型连接。')).toBeTruthy();
  expect(within(home).queryByRole('button')).toBeNull();
  expect(home.classList.contains('is-highlighted')).toBe(false);
  const serviceCard = home.parentElement!;
  expect(within(serviceCard).getByRole('heading', { name: '本地服务' })).toBeTruthy();
  expect(within(serviceCard).getByText('PostgreSQL').closest('.setting-row')?.nextElementSibling).toBe(home);
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  expect(screen.queryByRole('region', { name: 'Pulsara 家目录' })).toBeNull();
});

async function openCustomForm() {
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  fireEvent.click(await screen.findByRole('button', { name: '添加配置' }));
  const dialog = await screen.findByRole('dialog', { name: '添加模型配置' });
  fireEvent.click(within(dialog).getByRole('button', { name: '自定义服务' }));
  return dialog;
}

it('discovers with just a base URL and supports substring search, keyboard and mouse selection, and manual IDs', async () => {
  const { discoverModels, addModel } = setup();
  const dialog = await openCustomForm();
  const fetch = within(dialog).getByRole('button', { name: '获取模型' });
  const input = within(dialog).getByRole('combobox', { name: 'Model ID' });
  expect(fetch).toHaveProperty('disabled', true);
  fireEvent.change(within(dialog).getByLabelText('Base URL'), { target: { value: 'https://gateway.example/v1' } });
  expect(fetch).toHaveProperty('disabled', false);
  fireEvent.click(fetch);
  await within(dialog).findByRole('option', { name: 'beta-model' });
  expect(discoverModels).toHaveBeenCalledExactlyOnceWith({
    base_url: 'https://gateway.example/v1', authentication: 'bearer_api_key', api_key: null, connection_id: null,
  }, expect.any(AbortSignal));
  expect(input).toHaveProperty('value', '');
  fireEvent.change(input, { target: { value: 'ALPHA' } });
  expect(within(within(dialog).getByRole('listbox', { name: '模型候选列表' })).getAllByRole('option')).toHaveLength(2);
  fireEvent.keyDown(input, { key: 'ArrowDown' });
  fireEvent.keyDown(input, { key: 'ArrowDown' });
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(input).toHaveProperty('value', 'org/alpha-large');
  expect(input.getAttribute('aria-expanded')).toBe('false');
  fireEvent.change(input, { target: { value: 'beta' } });
  fireEvent.click(within(dialog).getByRole('option', { name: 'beta-model' }));
  expect(input).toHaveProperty('value', 'beta-model');
  fireEvent.change(input, { target: { value: 'manual-unlisted' } });
  expect(within(dialog).getByText('没有匹配的模型，可继续手动填写。')).toBeTruthy();
  fireEvent.keyDown(input, { key: 'Escape' });
  expect(dialog.hasAttribute('open')).toBe(true);
  expect(input.getAttribute('aria-expanded')).toBe('false');
  fireEvent.change(within(dialog).getByLabelText('配置名称'), { target: { value: 'Custom' } });
  fireEvent.change(within(dialog).getByLabelText('认证方式'), { target: { value: 'none' } });
  fireEvent.change(within(dialog).getByLabelText('API 协议'), { target: { value: 'openai_responses' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '保存配置' }));
  await waitFor(() => expect(addModel).toHaveBeenCalledWith(expect.objectContaining({ model_id: 'manual-unlisted' })));
});

it.each(['error', 'empty', 'pending'])('allows manual saving with a %s discovery result', async (outcome) => {
  const { discoverModels, addModel } = setup();
  if (outcome === 'error') discoverModels.mockRejectedValueOnce(new Error('HTTP 401，请填写密钥。'));
  if (outcome === 'empty') discoverModels.mockResolvedValueOnce({ model_ids: [] });
  if (outcome === 'pending') discoverModels.mockImplementationOnce(() => new Promise(() => {}));
  const dialog = await openCustomForm();
  fireEvent.change(within(dialog).getByLabelText('Base URL'), { target: { value: 'https://gateway.example/v1' } });
  fireEvent.change(within(dialog).getByLabelText('配置名称'), { target: { value: 'Custom' } });
  fireEvent.change(within(dialog).getByLabelText('API 协议'), { target: { value: 'openai_responses' } });
  fireEvent.change(within(dialog).getByLabelText('API key'), { target: { value: 'test-secret' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '获取模型' }));
  if (outcome === 'error') await within(dialog).findByText(/HTTP 401.*仍可手动填写并保存/);
  if (outcome === 'empty') await within(dialog).findByText('服务返回了空列表，可手动填写 Model ID。');
  fireEvent.change(within(dialog).getByLabelText('Model ID'), { target: { value: 'typed-model' } });
  expect(within(dialog).getByRole('button', { name: '保存配置' })).toHaveProperty('disabled', false);
  fireEvent.click(within(dialog).getByRole('button', { name: '保存配置' }));
  await waitFor(() => expect(addModel).toHaveBeenCalledWith(expect.objectContaining({ model_id: 'typed-model', api_key: 'test-secret' })));
  expect(discoverModels).toHaveBeenCalledWith(expect.objectContaining({ api_key: 'test-secret' }), expect.any(AbortSignal));
});

it.each(['Base URL', 'API key', '认证方式', 'close'])('discards an in-flight result after changing %s', async (field) => {
  const { discoverModels } = setup();
  let resolve!: (value: { model_ids: string[] }) => void;
  discoverModels.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  const dialog = await openCustomForm();
  fireEvent.change(within(dialog).getByLabelText('Base URL'), { target: { value: 'https://old.example/v1' } });
  fireEvent.change(within(dialog).getByLabelText('API key'), { target: { value: 'old-secret' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '获取模型' }));
  if (field === 'close') {
    fireEvent.click(within(dialog).getByRole('button', { name: '关闭添加模型配置' }));
    fireEvent.click(screen.getByRole('button', { name: '添加配置' }));
    fireEvent.click(screen.getByRole('button', { name: '自定义服务' }));
  } else {
    fireEvent.change(within(dialog).getByLabelText(field), { target: { value: field === '认证方式' ? 'none' : field === 'Base URL' ? 'https://new.example/v1' : 'new-secret' } });
  }
  const signal = (discoverModels.mock.calls[0] as unknown as [unknown, AbortSignal])[1];
  expect(signal.aborted).toBe(true);
  resolve({ model_ids: ['stale-model'] });
  await waitFor(() => expect(screen.queryByRole('button', { name: '获取中…' })).toBeNull());
  expect(screen.queryByRole('option', { name: 'stale-model' })).toBeNull();
  expect(screen.queryByText(/已获取 1 个模型/)).toBeNull();
});

it('queries an edited connection without reading the saved key into the renderer', async () => {
  const { discoverModels } = setup([modelConfiguration(1)], customDetail());
  fireEvent.click(screen.getByRole('button', { name: '模型' }));
  fireEvent.click(await screen.findByRole('button', { name: '修改模型配置 Test · Model 1' }));
  const dialog = await screen.findByRole('dialog', { name: '修改模型配置' });
  fireEvent.click(within(dialog).getByRole('button', { name: '获取模型' }));
  await waitFor(() => expect(discoverModels).toHaveBeenCalledWith(expect.objectContaining({ api_key: null, connection_id: 'connection-1' }), expect.any(AbortSignal)));
  expect(within(dialog).getByLabelText('API key')).toHaveProperty('value', '');
});
