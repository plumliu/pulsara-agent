import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { AsrSettingsPanel } from '../../frontend/components/asr-settings';
import type { RuntimeAdapter } from '../../frontend/lib/runtime-adapter';

beforeEach(() => { Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function(this: HTMLDialogElement) { this.setAttribute('open', ''); } }); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('edits one independent connection with three fields, explicit testing and one cache cleanup action', async () => {
  const save = vi.fn(async (_input: unknown) => ({ endpoint: 'https://asr.test/full/path', model_id: 'speech-model', credential_configured: true }));
  const test = vi.fn(async () => ({ status: 'ready', granularity: 'segment', elapsed_ms: 125 }));
  const clear = vi.fn(async () => ({ deleted_blocks: 0, message: '暂无可清理的旧转写缓存' }));
  const changed = vi.fn(); const notify = vi.fn();
  const adapter = { saveAsrModel: save, testAsrModel: test, clearOldAsrCache: clear } as unknown as RuntimeAdapter;
  render(<AsrSettingsPanel adapter={adapter} value={null} onChanged={changed} onNotify={notify} />);
  expect(screen.queryByRole('switch')).toBeNull();
  expect(screen.queryByRole('combobox')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '清理旧转写缓存' }));
  await waitFor(() => expect(clear).toHaveBeenCalledOnce());
  expect(test).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '配置' }));
  expect(screen.getByRole('dialog')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('请求地址'), { target: { value: 'https://asr.test/full/path' } });
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'speech-model' } });
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'draft-secret' } });
  fireEvent.click(screen.getByRole('button', { name: '测试连接' }));
  await waitFor(() => expect(test).toHaveBeenCalledOnce());
  expect(save).not.toHaveBeenCalled();
  expect(notify).toHaveBeenCalledWith('转写连接测试通过', '段级时间戳 · 125 ms', 'success');
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(save).toHaveBeenCalledOnce());
  expect(save.mock.calls[0]).toEqual([{ connection: { endpoint: 'https://asr.test/full/path', model_id: 'speech-model' }, key_action: 'replace', api_key: 'draft-secret' }]);
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
});
