import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SettingsView } from './settings-view';
import type { LocalSettingsReadModel, RuntimeAdapter } from '../lib/runtime-adapter';

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute('open', ''); } });
});
afterEach(() => { cleanup(); Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal"); vi.restoreAllMocks(); });

function setup() {
  const target = { runtime_dsn: 'postgresql://runtime@localhost:5432/pulsara', admin_dsn: 'postgresql://admin@localhost:5432/pulsara' };
  let settings: LocalSettingsReadModel = {
    local_settings: { postgres: target, dashscope_credentials: { embedding_configured: false, rerank_configured: false } },
    database_state: 'database_reset_required', model_configurations: [],
  };
  const reset = vi.fn(async () => {
    settings = { ...settings, database_state: 'ready' };
    return { database_name: 'pulsara', restart_required: false };
  });
  const refresh = vi.fn(async () => {});
  const adapter = {
    localSettings: async () => settings,
    modelCatalog: async () => ({ status: 'ready', routes: [] }),
    resetPostgres: reset,
  } as unknown as RuntimeAdapter;
  render(<SettingsView adapter={adapter} theme="light" runtimeStatus="online" onThemeChange={() => {}} onConfigurationChanged={refresh} onNotify={() => {}} />);
  return { reset, refresh, target };
}

it('shows the saved target, cancels without deletion, and resets only after confirmation', async () => {
  const { reset, refresh, target } = setup();
  const opener = await screen.findByRole('button', { name: '重置数据…' });
  await waitFor(() => expect((screen.getByRole('button', { name: '初始化 / 升级' }) as HTMLButtonElement).disabled).toBe(true));
  fireEvent.change(screen.getByLabelText('Runtime DSN'), { target: { value: 'postgresql://wrong@localhost/other' } });
  fireEvent.click(opener);
  let dialog = screen.getByRole('dialog', { name: '重置 Pulsara 数据？' });
  expect(within(dialog).getByText(target.runtime_dsn)).toBeTruthy();
  expect(reset).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button', { name: '取消' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(reset).not.toHaveBeenCalled();
  fireEvent.click(opener);
  dialog = screen.getByRole('dialog', { name: '重置 Pulsara 数据？' });
  fireEvent.click(within(dialog).getByRole('button', { name: '确认清空并初始化' }));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  expect(reset).toHaveBeenCalledExactlyOnceWith(target);
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.getByText('数据库 pulsara 已重置并完成初始化。')).toBeTruthy();
});

it('keeps a failed reset visible without automatically retrying', async () => {
  const { reset } = setup();
  reset.mockRejectedValue(new Error('目标已变更，请重新确认。'));
  const opener = await screen.findByRole('button', { name: '重置数据…' });
  await waitFor(() => expect((opener as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(opener);
  fireEvent.click(screen.getByRole('button', { name: '确认清空并初始化' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '目标已变更，请重新确认。');
  expect(reset).toHaveBeenCalledOnce();
  expect(screen.getByRole('dialog')).toBeTruthy();
});
