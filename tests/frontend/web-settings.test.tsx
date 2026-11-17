import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { WebSettingsPanel } from '../../frontend/components/web-settings';
import { LocalHttpRuntimeAdapter, type RuntimeAdapter, type WebSettingsStatus } from '../../frontend/lib/runtime-adapter';
import { randomIdentity } from '../../frontend/lib/identity';

const candidate = { model_connection_id: randomIdentity('connection'), label: 'DeepSeek · deepseek-chat' };
const free: WebSettingsStatus = { mode: 'free', connection: null, deepseek_candidate: candidate };
const account = (provider: 'deepseek' | 'firecrawl' | 'tavily' | 'exa' = 'tavily'): WebSettingsStatus => ({
  ...free, mode: 'account', connection: { provider, has_api_key: true, credential_source: 'api_key', model_connection_id: null, model_connection_label: null },
});
beforeEach(() => { Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function(this: HTMLDialogElement) { this.setAttribute('open', ''); } }); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function setup(value: WebSettingsStatus = free) {
  const changed = vi.fn(), notify = vi.fn();
  const read = vi.fn(async () => value);
  const save = vi.fn(async (_input: unknown) => account());
  const auto = vi.fn(async () => ({ ...account('deepseek'), connection: { ...account('deepseek').connection!, credential_source: 'model_connection' as const, model_connection_id: candidate.model_connection_id, model_connection_label: candidate.label } }));
  const clear = vi.fn(async () => free);
  const adapter = { webSettings: read, saveWebSettings: save, useDeepSeekWebConnection: auto, clearWebSettings: clear } as unknown as RuntimeAdapter;
  render(<WebSettingsPanel adapter={adapter} value={value} onChanged={changed} onNotify={notify} />);
  const open = async () => {
    fireEvent.click(screen.getByRole('button', { name: value.connection ? '修改' : '配置账户' }));
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
    return within(screen.getByRole('dialog'));
  };
  return { changed, notify, read, save, auto, clear, open };
}

it('uses free mode quietly without reading or adopting available model credentials', () => {
  const { read, auto, save, notify } = setup();
  expect(screen.getByText('自动免费')).toBeTruthy();
  expect(screen.getByText('无需配置即可搜索和读取网页')).toBeTruthy();
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(read).not.toHaveBeenCalled(); expect(auto).not.toHaveBeenCalled();
  expect(save).not.toHaveBeenCalled(); expect(notify).not.toHaveBeenCalled();
});

it.each(['deepseek', 'firecrawl', 'tavily', 'exa'])('saves an explicitly entered %s account with a write-only key', async provider => {
  const { open, save, changed, auto } = setup();
  const dialog = await open();
  expect(dialog.getAllByRole('option').map(item => item.textContent)).toEqual(['DeepSeek', 'Firecrawl', 'Tavily', 'Exa']);
  const input = dialog.getByLabelText('API key') as HTMLInputElement;
  expect(input.type).toBe('password'); expect(input.value).toBe('');
  fireEvent.change(dialog.getByLabelText('供应商'), { target: { value: provider } });
  fireEvent.change(input, { target: { value: 'explicit-test-secret' } });
  fireEvent.click(dialog.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(save).toHaveBeenCalledExactlyOnceWith({ provider, key_action: 'replace', api_key: 'explicit-test-secret' }));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
  expect(auto).not.toHaveBeenCalled(); expect(screen.queryByRole('dialog')).toBeNull();
});

it('keeps a direct key only for the same supplier and clears drafted keys when switching', async () => {
  const { open, save } = setup(account());
  const dialog = await open();
  const input = dialog.getByLabelText('API key') as HTMLInputElement;
  expect(input.value).toBe(''); expect(input.placeholder).toBe('留空保留现有密钥');
  fireEvent.change(input, { target: { value: 'must-not-cross-providers' } });
  fireEvent.change(dialog.getByLabelText('供应商'), { target: { value: 'exa' } });
  expect(input.value).toBe(''); expect((dialog.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(dialog.getByLabelText('供应商'), { target: { value: 'tavily' } });
  fireEvent.click(dialog.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(save).toHaveBeenCalledExactlyOnceWith({ provider: 'tavily', key_action: 'keep' }));
});

it('adopts the first official DeepSeek connection only after the explicit auto action', async () => {
  const { open, auto, save, changed } = setup();
  const dialog = await open();
  const button = dialog.getByRole('button', { name: '自动配置 DeepSeek' });
  expect(auto).not.toHaveBeenCalled();
  fireEvent.click(button);
  await waitFor(() => expect(auto).toHaveBeenCalledOnce());
  await waitFor(() => expect(changed).toHaveBeenCalledWith(expect.objectContaining({ connection: expect.objectContaining({ credential_source: 'model_connection', model_connection_label: candidate.label }) })));
  expect(save).not.toHaveBeenCalled(); expect(screen.queryByRole('dialog')).toBeNull();
});

it('does not offer auto configuration when no official connection is available', async () => {
  const { open, auto } = setup({ ...free, deepseek_candidate: null });
  const dialog = await open();
  expect((dialog.getByRole('button', { name: '自动配置 DeepSeek' }) as HTMLButtonElement).disabled).toBe(true);
  expect((dialog.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  expect(auto).not.toHaveBeenCalled();
});

it('shows a credential reference without issuing direct-key keep and can reset to free', async () => {
  const value: WebSettingsStatus = { ...account('deepseek'), connection: { ...account('deepseek').connection!, credential_source: 'model_connection', model_connection_id: candidate.model_connection_id, model_connection_label: candidate.label } };
  const { open, save, clear, changed } = setup(value);
  const dialog = await open();
  expect((dialog.getByLabelText('API key') as HTMLInputElement).value).toBe('');
  expect((dialog.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(dialog.getByRole('button', { name: '使用自动免费' }));
  await waitFor(() => expect(clear).toHaveBeenCalledOnce());
  await waitFor(() => expect(changed).toHaveBeenCalledWith(free));
  expect(save).not.toHaveBeenCalled();
});

it('retains the dialog and concrete failure if account save fails', async () => {
  const { open, save, changed } = setup();
  save.mockRejectedValueOnce(new Error('配置保存失败，请重试'));
  const dialog = await open();
  fireEvent.change(dialog.getByLabelText('API key'), { target: { value: 'test-value' } });
  fireEvent.click(dialog.getByRole('button', { name: '保存' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Error: 配置保存失败，请重试');
  expect(screen.getByRole('dialog')).toBeTruthy(); expect(changed).not.toHaveBeenCalled();
});

it('calls user-level settings endpoints without requiring an active session', async () => {
  const fetchMock = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(free)));
  vi.stubGlobal('fetch', fetchMock);
  const adapter = new LocalHttpRuntimeAdapter();
  expect(await adapter.webSettings()).toEqual(free);
  await adapter.saveWebSettings({ provider: 'tavily', key_action: 'replace', api_key: 'test-key' });
  await adapter.saveWebSettings({ provider: 'tavily', key_action: 'keep' });
  await adapter.useDeepSeekWebConnection();
  await adapter.clearWebSettings();
  expect(fetchMock.mock.calls.map(call => [String(call[0]), call[1]?.method ?? 'GET', call[1]?.body])).toEqual([
    ['/api/local-settings/web', 'GET', undefined],
    ['/api/local-settings/web', 'PUT', JSON.stringify({ provider: 'tavily', key_action: 'replace', api_key: 'test-key' })],
    ['/api/local-settings/web', 'PUT', JSON.stringify({ provider: 'tavily', key_action: 'keep' })],
    ['/api/local-settings/web/use-deepseek', 'POST', '{}'],
    ['/api/local-settings/web', 'DELETE', undefined],
  ]);
});

it('can retry a failed settings read without opening an uninitialized account form', async () => {
  const { open, read } = setup(account());
  read.mockRejectedValueOnce(new Error('服务暂不可用'));
  const dialog = await open();
  expect(dialog.getByRole('alert').textContent).toContain('服务暂不可用');
  expect((dialog.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  expect((dialog.getByLabelText('供应商') as HTMLSelectElement).disabled || dialog.getByLabelText('供应商').closest('fieldset')?.disabled).toBe(true);
  fireEvent.click(dialog.getByRole('button', { name: '重试读取配置' }));
  await waitFor(() => expect(dialog.queryByRole('status')).toBeNull());
  expect((dialog.getByLabelText('供应商') as HTMLSelectElement).value).toBe('tavily');
  expect((dialog.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(false);
  expect(dialog.queryByRole('alert')).toBeNull();
});

it('explains an unavailable saved reference truthfully and can select another available candidate', async () => {
  const missing: WebSettingsStatus = { ...free, connection: { provider: 'deepseek', has_api_key: false, credential_source: 'model_connection', model_connection_id: randomIdentity('connection'), model_connection_label: null } };
  const { open, auto, changed } = setup(missing);
  expect(screen.getByText('自动免费')).toBeTruthy();
  expect(screen.getByText('账户密钥不可用 · 当前免费使用')).toBeTruthy();
  const dialog = await open();
  expect(dialog.getByText('已保存的模型连接不可用，当前使用自动免费。可自动配置其他连接，或填写独立密钥。')).toBeTruthy();
  fireEvent.click(dialog.getByRole('button', { name: '自动配置 DeepSeek' }));
  await waitFor(() => expect(auto).toHaveBeenCalledOnce());
  await waitFor(() => expect(changed).toHaveBeenCalledWith(expect.objectContaining({ mode: 'account' })));
});

it('lets a failed save retry and protects an unsettled save from dismissal', async () => {
  const { open, save, changed } = setup();
  save.mockRejectedValueOnce(new Error('暂时写入失败'));
  const dialog = await open();
  fireEvent.change(dialog.getByLabelText('API key'), { target: { value: 'test-key' } });
  fireEvent.click(dialog.getByRole('button', { name: '保存' }));
  await screen.findByRole('alert');
  let resolveSave!: (value: WebSettingsStatus) => void;
  save.mockImplementationOnce(() => new Promise(resolve => { resolveSave = resolve; }));
  fireEvent.click(dialog.getByRole('button', { name: '保存' }));
  expect((dialog.getByRole('button', { name: '关闭网页服务配置' }) as HTMLButtonElement).disabled).toBe(true);
  expect((dialog.getByRole('button', { name: '取消' }) as HTMLButtonElement).disabled).toBe(true);
  const cancel = new Event('cancel', { bubbles: false, cancelable: true });
  screen.getByRole('dialog').dispatchEvent(cancel);
  expect(cancel.defaultPrevented).toBe(true);
  resolveSave(account());
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('can close the modal and reopen it with a fresh settings read', async () => {
  const { open, read, save, auto } = setup();
  let dialog = await open();
  fireEvent.click(dialog.getByRole('button', { name: '关闭网页服务配置' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  dialog = await open();
  expect(read).toHaveBeenCalledTimes(2);
  fireEvent.click(dialog.getByRole('button', { name: '取消' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(save).not.toHaveBeenCalled(); expect(auto).not.toHaveBeenCalled();
});
