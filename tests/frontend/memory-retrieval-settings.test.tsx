import { useState } from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { MemoryRetrievalSettingsPanel } from '../../frontend/components/memory-retrieval-settings';
import type { MemoryRetrievalSettings, RuntimeAdapter, RetrievalModelInput, MemoryModelSlot } from '../../frontend/lib/runtime-adapter';

beforeEach(() => { Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function(this: HTMLDialogElement) { this.setAttribute('open', ''); } }); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const empty: MemoryRetrievalSettings = { embedding: null, embedding_enabled: false, decision: null, rerank: null, ranking_mode: 'off' };
const conn = (slot: MemoryModelSlot) => ({ endpoint: `https://provider.example/${slot}`, model_id: `${slot}-fixture`, shape: slot === 'embedding' ? 'openai_embedding' as const : slot === 'decision' ? 'system_one' as const : 'flat_rerank' as const, authentication: 'none' as const, credential_configured: false });

function setup(initial = empty) {
  let saved = initial;
  const notify = vi.fn();
  const save = vi.fn(async (slot: MemoryModelSlot, input: RetrievalModelInput) => {
    saved = { ...saved, [slot]: { ...input.connection, credential_configured: input.key_action === 'replace' } };
    if (input.activate) saved = slot === 'embedding' ? { ...saved, embedding_enabled: true } : { ...saved, ranking_mode: slot };
    return saved;
  });
  const test = vi.fn(async () => ({ status: 'ready' as const, elapsed_ms: 15, result_count: 2, dimensions: 1024 }));
  const setEmbedding = vi.fn(async (enabled: boolean) => (saved = { ...saved, embedding_enabled: enabled }));
  const setRanking = vi.fn(async (mode: 'off' | 'decision' | 'rerank') => (saved = { ...saved, ranking_mode: mode }));
  const clear = vi.fn(async (slot: MemoryModelSlot) => {
    saved = { ...saved, [slot]: null };
    if (slot === 'embedding') saved = { ...saved, embedding_enabled: false };
    else if (saved.ranking_mode === slot) saved = { ...saved, ranking_mode: 'off' };
    return saved;
  });
  const adapter = { saveMemoryModel: save, testMemoryModel: test, setEmbeddingEnabled: setEmbedding,
    setMemoryRanking: setRanking, clearMemoryModel: clear,
    memory: { catalog: async () => ({ items: [], next_cursor: null }), projects: async () => ({ items: [], next_cursor: null }) },
  } as unknown as RuntimeAdapter;
  function Harness() { const [value, setValue] = useState(initial); return <MemoryRetrievalSettingsPanel adapter={adapter} value={value} onChanged={setValue} onNotify={notify} />; }
  const rendered = render(<Harness />);
  return { ...rendered, save, test, setEmbedding, setRanking, clear, notify, saved: () => saved };
}

it('shows matching channel headings and two collapsed switches with one concise subtitle', () => {
  const { container } = setup();
  expect(screen.getByRole('switch', { name: '启用 Embedding' }).getAttribute('aria-checked')).toBe('false');
  expect(screen.getByRole('switch', { name: '启用重排' }).getAttribute('aria-checked')).toBe('false');
  expect(screen.getByRole('heading', { name: 'Embedding' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: '重排' })).toBeTruthy();
  expect(screen.queryAllByRole('radio')).toHaveLength(0);
  expect(screen.queryAllByRole('button', { name: /^配置 / })).toHaveLength(0);
  expect(container.querySelectorAll('small,p')).toHaveLength(1);
  expect(screen.queryByText('添加配置')).toBeNull();
});

it('an unconfigured drawer opens with a warning; cancelling configuration keeps runtime Off', () => {
  const { setRanking, save } = setup();
  fireEvent.click(screen.getByRole('switch', { name: '启用重排' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByRole('switch', { name: '启用重排' }).getAttribute('aria-checked')).toBe('true');
  expect(screen.getByRole('group', { name: '重排方式' })).toBeTruthy();
  expect(screen.getByRole('button', { name: '重排 配置不完整' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '配置 Jev-like Decision' }));
  expect(screen.getByRole('dialog', { name: '配置 Jev-like Decision' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '取消' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByRole('switch', { name: '启用重排' }).getAttribute('aria-checked')).toBe('true');
  expect(screen.getByRole('button', { name: '重排 配置不完整' })).toBeTruthy();
  expect(setRanking).not.toHaveBeenCalled(); expect(save).not.toHaveBeenCalled();
});

it('OpenRouter preset uses System One; Save and Enable commits one slot and selection', async () => {
  const { save, setRanking, saved } = setup();
  fireEvent.click(screen.getByRole('switch', { name: '启用重排' }));
  fireEvent.click(screen.getByRole('button', { name: '配置 Jev-like Decision' }));
  expect((screen.getByLabelText('协议') as HTMLSelectElement).disabled).toBe(true);
  expect((screen.getByLabelText('协议') as HTMLSelectElement).value).toBe('system_one');
  expect((screen.getByLabelText('请求地址') as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText('请求地址') as HTMLInputElement).value).toBe('https://api.typesafe.ai/v1/systemone');
  fireEvent.change(screen.getByLabelText('连接预设'), { target: { value: 'openai' } });
  expect((screen.getByLabelText('协议') as HTMLSelectElement).value).toBe('openai_decisions');
  expect((screen.getByLabelText('协议') as HTMLSelectElement).disabled).toBe(true);
  expect((screen.getByLabelText('请求地址') as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText('请求地址') as HTMLInputElement).value).toBe('https://api.openai.com/v1/decisions');
  fireEvent.change(screen.getByLabelText('连接预设'), { target: { value: 'openrouter' } });
  expect((screen.getByLabelText('请求地址') as HTMLInputElement).value).toBe('https://openrouter.ai/api/v1/systemone');
  expect((screen.getByLabelText('请求地址') as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText('协议') as HTMLSelectElement).value).toBe('system_one');
  expect((screen.getByLabelText('协议') as HTMLSelectElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'openai/gpt-6-luna-decisions' } });
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'fixture-key' } });
  fireEvent.click(screen.getByRole('button', { name: '保存并启用' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(save).toHaveBeenCalledOnce(); expect(setRanking).not.toHaveBeenCalled();
  expect(saved().ranking_mode).toBe('decision');
  expect(saved().decision?.model_id).toBe('openai/gpt-6-luna-decisions');
  expect(screen.queryByRole('button', { name: '重排 配置不完整' })).toBeNull();
});

it('disabling embedding and ranking preserves all three configured model slots', async () => {
  const { saved } = setup({ embedding: conn('embedding'), embedding_enabled: true, decision: conn('decision'), rerank: conn('rerank'), ranking_mode: 'decision' });
  fireEvent.click(screen.getByRole('switch', { name: '启用 Embedding' }));
  await waitFor(() => expect(saved().embedding_enabled).toBe(false));
  fireEvent.click(screen.getByRole('switch', { name: '启用重排' }));
  await waitFor(() => expect(saved().ranking_mode).toBe('off'));
  expect(saved().embedding?.model_id).toBe('embedding-fixture');
  expect(saved().decision?.model_id).toBe('decision-fixture'); expect(saved().rerank?.model_id).toBe('rerank-fixture');
});

it('editing an active model keeps its selection; test and close notify once', async () => {
  const { save, test, notify, saved } = setup({ ...empty, decision: conn('decision'), ranking_mode: 'decision' });
  fireEvent.click(screen.getByRole('button', { name: '配置 Jev-like Decision' }));
  fireEvent.click(screen.getByRole('button', { name: '测试连接' }));
  await waitFor(() => expect(test).toHaveBeenCalledOnce());
  await waitFor(() => expect(notify).toHaveBeenCalledOnce());
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'replacement' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(save.mock.calls[0][1].activate).toBe(false); expect(saved().ranking_mode).toBe('decision');
  expect(saved().decision?.model_id).toBe('replacement'); expect(notify).toHaveBeenCalledOnce();
});

it('embedding replacement requires confirmation; cancel leaves config and switch unchanged', async () => {
  const initial = { ...empty, embedding: conn('embedding'), embedding_enabled: true };
  const { save, saved } = setup(initial);
  fireEvent.click(screen.getByRole('button', { name: '配置 Embedding' }));
  fireEvent.change(screen.getByLabelText(/模型 ID/), { target: { value: 'new-embedding' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect(screen.getByText(/覆盖重建已有记忆向量/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '返回' }));
  expect(save).not.toHaveBeenCalled(); expect(saved()).toEqual(initial);
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  fireEvent.click(screen.getByRole('button', { name: '确认保存' }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0][1]).toMatchObject({ confirm_reembed: true, expected_embedding: {
    endpoint: initial.embedding.endpoint, model_id: initial.embedding.model_id, shape: 'openai_embedding' }, activate: false });
});

it('rerank examples remain folded without advanced settings; clearing disables runtime and keeps the drawer open', async () => {
  const { clear, saved } = setup({ ...empty, rerank: conn('rerank'), ranking_mode: 'rerank' });
  fireEvent.click(screen.getByRole('button', { name: '配置 Rerank' }));
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByText('格式示例').closest('details')?.open).toBe(false);
  expect(within(dialog).queryByText('高级设置')).toBeNull();
  expect(within(dialog).queryByLabelText('认证')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '清除配置' }));
  await waitFor(() => expect(clear).toHaveBeenCalledOnce());
  expect(saved().rerank).toBeNull(); expect(saved().ranking_mode).toBe('off');
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(screen.getByRole('switch', { name: '启用重排' }).getAttribute('aria-checked')).toBe('true');
  expect(screen.getByRole('button', { name: '重排 配置不完整' })).toBeTruthy();
});

it('custom decision preset clears the preset URL and can save a custom endpoint', async () => {
  const { saved } = setup();
  fireEvent.click(screen.getByRole('switch', { name: '启用重排' }));
  fireEvent.click(screen.getByRole('button', { name: '配置 Jev-like Decision' }));
  fireEvent.change(screen.getByLabelText('连接预设'), { target: { value: 'custom' } });
  expect((screen.getByLabelText('请求地址') as HTMLInputElement).value).toBe('');
  expect((screen.getByLabelText('请求地址') as HTMLInputElement).disabled).toBe(false);
  expect((screen.getByLabelText('连接预设') as HTMLSelectElement).value).toBe('custom');
  expect((screen.getByLabelText('协议') as HTMLSelectElement).disabled).toBe(false);
  fireEvent.change(screen.getByLabelText('协议'), { target: { value: 'openai_decisions' } });
  expect((screen.getByLabelText('协议') as HTMLSelectElement).value).toBe('openai_decisions');
  fireEvent.change(screen.getByLabelText('协议'), { target: { value: 'system_one' } });
  fireEvent.change(screen.getByLabelText('请求地址'), { target: { value: 'https://provider.example/custom/systemone' } });
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'decision-fixture' } });
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'fixture-key' } });
  fireEvent.click(screen.getByRole('button', { name: '保存并启用' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(saved().decision?.endpoint).toBe('https://provider.example/custom/systemone');
});

it('a failed configured switch mutation retains the original selection and reports once', async () => {
  const { setRanking, notify, saved } = setup({ ...empty, decision: conn('decision') });
  setRanking.mockRejectedValueOnce(new Error('保存失败'));
  fireEvent.click(screen.getByRole('switch', { name: '启用重排' }));
  await waitFor(() => expect(notify).toHaveBeenCalledOnce());
  expect(saved().ranking_mode).toBe('off');
  expect(screen.getByRole('switch', { name: '启用重排' }).getAttribute('aria-checked')).toBe('false');
  expect(screen.queryByRole('group', { name: '重排方式' })).toBeNull();
});

it('configured channels expand on enable, hide on disable and retain their configuration', async () => {
  const { saved } = setup({ ...empty, embedding: conn('embedding'), decision: conn('decision'), rerank: conn('rerank') });
  expect(screen.queryByRole('button', { name: '配置 Embedding' })).toBeNull();
  fireEvent.click(screen.getByRole('switch', { name: '启用 Embedding' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '配置 Embedding' })).toBeTruthy());
  fireEvent.click(screen.getByRole('switch', { name: '启用重排' }));
  await waitFor(() => expect(screen.getByRole('group', { name: '重排方式' })).toBeTruthy());
  expect(screen.getAllByRole('radio').map(element => element.getAttribute('value'))).toEqual(['decision', 'rerank']);
  expect((screen.getByRole('radio', { name: 'Jev-like Decision' }) as HTMLInputElement).checked).toBe(true);
  expect(screen.getByRole('button', { name: '配置 Jev-like Decision' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: '配置 Rerank' })).toBeNull();
  expect(screen.getByText('decision-fixture')).toBeTruthy();
  expect(screen.queryByText('rerank-fixture')).toBeNull();
  fireEvent.click(screen.getByRole('radio', { name: 'Rerank' }));
  await waitFor(() => expect(saved().ranking_mode).toBe('rerank'));
  expect(screen.getByRole('button', { name: '配置 Rerank' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: '配置 Jev-like Decision' })).toBeNull();
  expect(screen.getByText('rerank-fixture')).toBeTruthy();
  expect(screen.queryByText('decision-fixture')).toBeNull();
  fireEvent.click(screen.getByRole('switch', { name: '启用重排' }));
  await waitFor(() => expect(screen.queryByRole('group', { name: '重排方式' })).toBeNull());
  expect(screen.queryByRole('button', { name: '配置 Rerank' })).toBeNull();
  expect(saved().decision).toEqual(conn('decision')); expect(saved().rerank).toEqual(conn('rerank'));
  fireEvent.click(screen.getByRole('switch', { name: '启用重排' }));
  await waitFor(() => expect(saved().ranking_mode).toBe('rerank'));
  expect((screen.getByRole('radio', { name: 'Rerank' }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByRole('switch', { name: '启用 Embedding' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: '配置 Embedding' })).toBeNull());
  expect(saved().embedding).toEqual(conn('embedding'));
});

it('enabling with only Rerank configured uses it; selecting missing Decision disables the previous path', async () => {
  const { saved, setRanking } = setup({ ...empty, rerank: conn('rerank') });
  fireEvent.click(screen.getByRole('switch', { name: '启用重排' }));
  await waitFor(() => expect(saved().ranking_mode).toBe('rerank'));
  expect(setRanking).toHaveBeenCalledExactlyOnceWith('rerank');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect((screen.getByRole('radio', { name: 'Rerank' }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByRole('radio', { name: 'Jev-like Decision' }));
  await waitFor(() => expect(saved().ranking_mode).toBe('off'));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect((screen.getByRole('radio', { name: 'Jev-like Decision' }) as HTMLInputElement).checked).toBe(true);
  expect(screen.getByRole('button', { name: '重排 配置不完整' })).toBeTruthy();
  expect(saved().rerank).toEqual(conn('rerank'));
});

it('an empty Embedding drawer warns on hover and keyboard focus, saves and enables atomically', async () => {
  const { setEmbedding, saved, save } = setup();
  fireEvent.click(screen.getByRole('switch', { name: '启用 Embedding' }));
  const warning = screen.getByRole('button', { name: 'Embedding 配置不完整' });
  expect(screen.queryByRole('tooltip')).toBeNull();
  fireEvent.mouseEnter(warning);
  expect(screen.getByRole('tooltip').textContent).toBe('开关已打开，但尚未配置模型，语义检索暂未启用。');
  expect(warning.getAttribute('aria-describedby')).toBe(screen.getByRole('tooltip').id);
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(screen.queryByRole('tooltip')).toBeNull();
  fireEvent.focus(warning);
  expect(screen.getByRole('tooltip')).toBeTruthy();
  fireEvent.blur(warning);
  await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull());
  expect(saved().embedding_enabled).toBe(false);
  expect(setEmbedding).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '配置 Embedding' }));
  fireEvent.change(screen.getByLabelText('请求地址'), { target: { value: 'https://provider.example/embedding' } });
  fireEvent.change(screen.getByLabelText(/模型 ID/), { target: { value: 'embedding-fixture' } });
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'fixture-key' } });
  fireEvent.click(screen.getByRole('button', { name: '保存并启用' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(save).toHaveBeenCalledOnce(); expect(save.mock.calls[0][1].activate).toBe(true);
  expect(setEmbedding).not.toHaveBeenCalled(); expect(saved().embedding_enabled).toBe(true);
  expect(screen.queryByRole('button', { name: 'Embedding 配置不完整' })).toBeNull();
});

it.each(['embedding', 'decision', 'rerank'] as const)('deleting active %s keeps the drawer open with a warning and does not delete until confirmed', async slot => {
  const initial = { ...empty, embedding: conn('embedding'), decision: conn('decision'), rerank: conn('rerank'),
    embedding_enabled: slot === 'embedding', ranking_mode: slot === 'embedding' ? 'off' as const : slot };
  const { clear, saved } = setup(initial);
  const label = { embedding: 'Embedding', decision: 'Jev-like Decision', rerank: 'Rerank' }[slot];
  fireEvent.click(screen.getByRole('button', { name: `删除 ${label}` }));
  expect(clear).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '取消' }));
  expect(saved()).toEqual(initial);
  fireEvent.click(screen.getByRole('button', { name: `删除 ${label}` }));
  fireEvent.click(screen.getByRole('button', { name: `确认删除 ${label}` }));
  await waitFor(() => expect(saved()[slot]).toBeNull());
  expect(clear).toHaveBeenCalledExactlyOnceWith(slot);
  expect(slot === 'embedding' ? saved().embedding_enabled : saved().ranking_mode).toBe(slot === 'embedding' ? false : 'off');
  const channel = slot === 'embedding' ? 'Embedding' : '重排';
  expect(screen.getByRole('switch', { name: `启用${slot === 'embedding' ? ' ' : ''}${channel}` }).getAttribute('aria-checked')).toBe('true');
  expect(screen.getByRole('button', { name: `${channel} 配置不完整` })).toBeTruthy();
  expect(screen.getByRole('button', { name: `配置 ${label}` })).toBeTruthy();
  expect(screen.getByRole('button', { name: `配置 ${label}` }).closest('.model-config-card')?.querySelector('strong')?.textContent).toBe('未配置');
  fireEvent.click(screen.getByRole('button', { name: `配置 ${label}` }));
  expect(screen.getByRole('button', { name: '保存并启用' })).toBeTruthy();
});

it('model rows reuse main model title, protocol, ID, credential and edit/delete layout', () => {
  setup({ ...empty, embedding: { ...conn('embedding'), authentication: 'bearer_api_key', credential_configured: true }, embedding_enabled: true });
  const row = screen.getByRole('button', { name: '配置 Embedding' }).closest('.model-config-card')!;
  expect(row.querySelector('strong')?.textContent).toBe('provider.example · embedding-fixture');
  expect(row.querySelector('small')?.textContent).toBe('Embedding · 1024 维');
  expect(row.querySelector('code')?.textContent).toBe('embedding-fixture');
  expect(row.querySelector('.credential-state--present')?.textContent).toBe('密钥已配置');
  expect(within(row as HTMLElement).getByRole('button', { name: '删除 Embedding' })).toBeTruthy();
});

it('missing required credentials warn but do not send an enable request', () => {
  const { setEmbedding } = setup({ ...empty, embedding: { ...conn('embedding'), authentication: 'bearer_api_key' } });
  fireEvent.click(screen.getByRole('switch', { name: '启用 Embedding' }));
  fireEvent.focus(screen.getByRole('button', { name: 'Embedding 配置不完整' }));
  expect(screen.getByRole('tooltip').textContent).toBe('开关已打开，但尚未配置 API key，语义检索暂未启用。');
  expect(setEmbedding).not.toHaveBeenCalled();
});

it('deleting the selected type retains the other configuration and switching back enables it', async () => {
  const { saved } = setup({ ...empty, decision: conn('decision'), rerank: conn('rerank'), ranking_mode: 'decision' });
  expect(screen.queryByRole('button', { name: '删除 Rerank' })).toBeNull();
  fireEvent.click(screen.getByRole('radio', { name: 'Rerank' }));
  await waitFor(() => expect(saved().ranking_mode).toBe('rerank'));
  fireEvent.click(screen.getByRole('button', { name: '删除 Rerank' }));
  fireEvent.click(screen.getByRole('button', { name: '确认删除 Rerank' }));
  await waitFor(() => expect(saved().rerank).toBeNull());
  expect(saved().decision).toEqual(conn('decision'));
  expect(saved().ranking_mode).toBe('off');
  expect(screen.getByRole('button', { name: '重排 配置不完整' })).toBeTruthy();
  fireEvent.click(screen.getByRole('radio', { name: 'Jev-like Decision' }));
  await waitFor(() => expect(saved().ranking_mode).toBe('decision'));
  expect(saved().ranking_mode).toBe('decision');
  expect(screen.queryByRole('button', { name: '重排 配置不完整' })).toBeNull();
});

it('failed selection of a missing backend retains the previous actual path and selector', async () => {
  const { setRanking, notify, saved } = setup({ ...empty, rerank: conn('rerank'), ranking_mode: 'rerank' });
  setRanking.mockRejectedValueOnce(new Error('关闭失败'));
  fireEvent.click(screen.getByRole('radio', { name: 'Jev-like Decision' }));
  await waitFor(() => expect(notify).toHaveBeenCalledOnce());
  expect(saved().ranking_mode).toBe('rerank');
  expect((screen.getByRole('radio', { name: 'Rerank' }) as HTMLInputElement).checked).toBe(true);
  expect(screen.queryByRole('button', { name: '重排 配置不完整' })).toBeNull();
});
