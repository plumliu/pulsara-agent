import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import { RuntimeApiError, type RuntimeBootstrap } from '../../frontend/lib/runtime-adapter';
import type { SessionSummary } from '../../frontend/lib/pulsara-types';
import {
  bootstrap, deferred, FakeAdapter, initialSession, isWelcomeHeading,
  renderWorkbench, typeComposer,
} from './pulsara-app-fixture';

async function openRetrievalSettings() {
  fireEvent.click(screen.getByRole('button', { name: '设置', exact: true }));
  fireEvent.click(await screen.findByRole('button', { name: '模型', exact: true }));
  return screen.findByRole('switch', { name: '启用历史会话检索' });
}

function writableRetrieval(adapter: FakeAdapter) {
  let value = bootstrap.local_settings.memory_retrieval;
  vi.spyOn(adapter, 'setHistoryEnabled').mockImplementation(async enabled => (
    value = { ...value, history_enabled: enabled }
  ));
}

it.each(['database_unavailable', 'database_restart_required', 'error'] as const)(
  'rejects obsolete global %s after a newer successful refresh, without an active chat', async outcome => {
    const adapter = new FakeAdapter();
    adapter.sessions = [];
    writableRetrieval(adapter);
    const stale = deferred<RuntimeBootstrap>();
    const boot = vi.spyOn(adapter, 'bootstrap').mockResolvedValue(bootstrap)
      .mockResolvedValueOnce(bootstrap).mockImplementationOnce(() => stale.promise);
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByText('本地服务已连接');
    const toggle = await openRetrievalSettings();
    fireEvent.click(toggle);
    await waitFor(() => expect(boot).toHaveBeenCalledTimes(2));
    await waitFor(() => expect((toggle as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(toggle);
    await waitFor(() => expect(boot).toHaveBeenCalledTimes(3));
    fireEvent.click(screen.getByRole('button', { name: '总览', exact: true }));
    await screen.findByText('本地服务已连接');
    await act(async () => {
      if (outcome === 'error') stale.reject(new RuntimeApiError('UNAVAILABLE', '旧失败', true));
      else stale.resolve({ ...bootstrap, database_state: outcome });
    });
    expect(screen.getByText('本地服务已连接')).toBeTruthy();
    expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole('region', { name: 'PostgreSQL 配置引导' })).toBeNull();
    expect(adapter.connectCalls).toHaveLength(0);
  },
);

it.each(['success', 'error'] as const)('a superseded startup %s cannot publish or restore an old selected chat', async outcome => {
  const adapter = new FakeAdapter();
  writableRetrieval(adapter);
  const stale = deferred<RuntimeBootstrap>();
  vi.spyOn(adapter, 'bootstrap').mockResolvedValue(bootstrap).mockImplementationOnce(() => stale.promise);
  const settled = vi.fn();
  render(<PulsaraApp adapter={adapter} onStartupSettled={settled} />);
  const toggle = await openRetrievalSettings();
  fireEvent.click(toggle);
  await waitFor(() => expect(settled).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: '总览', exact: true }));
  await screen.findByText('本地服务已连接');
  await act(async () => {
    if (outcome === 'error') stale.reject(new RuntimeApiError('UNAVAILABLE', '旧启动失败', true));
    else stale.resolve({ ...bootstrap, database_state: 'database_restart_required' });
  });
  expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(false);
  expect(screen.queryByRole('region', { name: 'PostgreSQL 配置引导' })).toBeNull();
  expect(adapter.connectCalls).toHaveLength(0);
  expect(settled).toHaveBeenCalledOnce();
});

it('a stale restart response never closes the existing live connection', async () => {
  const adapter = new FakeAdapter();
  writableRetrieval(adapter);
  const stale = deferred<RuntimeBootstrap>();
  const boot = vi.spyOn(adapter, 'bootstrap').mockResolvedValue(bootstrap)
    .mockResolvedValueOnce(bootstrap).mockImplementationOnce(() => stale.promise);
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByRole('heading', { name: initialSession.title });
  const connection = adapter.lastConnection!;
  const toggle = await openRetrievalSettings();
  fireEvent.click(toggle);
  await waitFor(() => expect(boot).toHaveBeenCalledTimes(2));
  await waitFor(() => expect((toggle as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(toggle);
  await waitFor(() => expect(boot).toHaveBeenCalledTimes(3));
  await act(async () => stale.resolve({ ...bootstrap, database_state: 'database_restart_required' }));
  fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
  expect(await screen.findByRole('heading', { name: initialSession.title })).toBeTruthy();
  expect(connection.closed).toBe(false);
  expect(adapter.lastConnection).toBe(connection);
});

it('an accepted restart cleanup cannot detach a new connection after its old close settles', async () => {
  const adapter = new FakeAdapter();
  writableRetrieval(adapter);
  const boot = vi.spyOn(adapter, 'bootstrap').mockResolvedValue(bootstrap)
    .mockResolvedValueOnce(bootstrap)
    .mockResolvedValueOnce({ ...bootstrap, database_state: 'database_restart_required' });
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByRole('heading', { name: initialSession.title });
  const old = adapter.lastConnection!;
  const close = deferred<void>();
  vi.spyOn(old, 'close').mockImplementation(() => close.promise);
  const toggle = await openRetrievalSettings();
  fireEvent.click(toggle);
  await waitFor(() => expect(old.close).toHaveBeenCalled());
  await waitFor(() => expect((toggle as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(toggle);
  await waitFor(() => expect(boot).toHaveBeenCalledTimes(3));
  fireEvent.click(screen.getByRole('button', { name: '总览', exact: true }));
  await screen.findByText('本地服务已连接');
  fireEvent.click(screen.getByRole('button', { name: new RegExp(initialSession.title) }));
  await screen.findByRole('heading', { name: initialSession.title });
  const replacement = adapter.lastConnection!;
  expect(replacement).not.toBe(old);
  await act(async () => close.resolve());
  expect(screen.getByRole('heading', { name: initialSession.title })).toBeTruthy();
  expect(replacement.closed).toBe(false);
  expect(adapter.lastConnection).toBe(replacement);
});

it('a newer current list can finish global recovery while the superseded list remains pending', async () => {
  const adapter = new FakeAdapter();
  adapter.sessions = [];
  writableRetrieval(adapter);
  render(<PulsaraApp adapter={adapter} />);
  await screen.findByText('本地服务已连接');
  const toggle = await openRetrievalSettings();
  const stale = deferred<SessionSummary[]>();
  const lists = vi.spyOn(adapter, 'listSessions').mockImplementationOnce(() => stale.promise);
  fireEvent.click(toggle);
  await waitFor(() => expect(lists).toHaveBeenCalled());
  fireEvent.focus(window);
  await waitFor(() => expect(lists.mock.calls.length).toBeGreaterThan(1));
  fireEvent.click(screen.getByRole('button', { name: '总览', exact: true }));
  await screen.findByText('本地服务已连接');
  expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(false);
  await act(async () => stale.reject(new RuntimeApiError('UNAVAILABLE', '过期列表失败', true)));
  expect(screen.getByText('本地服务已连接')).toBeTruthy();
  expect(adapter.connectCalls).toHaveLength(0);
});

it('a focus list issued before creation cannot close the new connection or erase its draft', async () => {
  const adapter = new FakeAdapter();
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByRole('heading', { name: initialSession.title });
  const before = [...adapter.sessions];
  const stale = deferred<SessionSummary[]>();
  const lists = vi.spyOn(adapter, 'listSessions').mockImplementationOnce(() => stale.promise);
  fireEvent.focus(window);
  await waitFor(() => expect(lists).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('button', { name: '创建快速开始会话' }));
  await waitFor(() => expect(adapter.createSession).toHaveBeenCalledOnce());
  await screen.findByRole('heading', { name: isWelcomeHeading });
  const connection = adapter.lastConnection!;
  const composer = await typeComposer('keep this new draft');
  await act(async () => stale.resolve(before));
  expect(screen.queryByText('当前没有活动会话')).toBeNull();
  expect(connection.closed).toBe(false);
  expect(screen.getByLabelText('发送给 Pulsara')).toBe(composer);
  expect(composer.textContent).toContain('keep this new draft');
  expect(screen.getByLabelText('新会话 更多操作')).toBeTruthy();
  expect(adapter.sessions[0].id).toBe(connection.sessionId);
});

it('latest list ownership preserves a newer server title while still accepting real external deletion', async () => {
  const adapter = new FakeAdapter();
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByRole('heading', { name: initialSession.title });
  const connection = adapter.lastConnection!;
  const stale = deferred<SessionSummary[]>();
  const lists = vi.spyOn(adapter, 'listSessions').mockImplementationOnce(() => stale.promise);
  fireEvent.focus(window);
  await waitFor(() => expect(lists).toHaveBeenCalled());
  adapter.sessions = [{ ...initialSession, title: '另一个窗口的新标题' }];
  fireEvent.focus(window);
  await screen.findByRole('heading', { name: '另一个窗口的新标题' });
  await act(async () => stale.resolve([initialSession]));
  expect(screen.getByRole('heading', { name: '另一个窗口的新标题' })).toBeTruthy();
  adapter.sessions = [];
  fireEvent.focus(window);
  await screen.findByText('当前没有活动会话');
  expect(connection.closed).toBe(true);
});

it('a canonical rename invalidates old lists across a later session selection', async () => {
  const adapter = new FakeAdapter();
  const other = { ...initialSession, id: 'session:other', title: '另一个会话' };
  adapter.sessions = [initialSession, other];
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByRole('heading', { name: initialSession.title });
  const stale = deferred<SessionSummary[]>();
  const lists = vi.spyOn(adapter, 'listSessions').mockImplementationOnce(() => stale.promise);
  fireEvent.focus(window);
  await waitFor(() => expect(lists).toHaveBeenCalled());
  fireEvent.doubleClick(screen.getByRole('button', { name: new RegExp(initialSession.title + '当前会话') }));
  const dialog = await screen.findByRole('dialog', { name: '重命名会话' });
  fireEvent.change(within(dialog).getByLabelText('会话标题'), { target: { value: '新标题' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  await screen.findByRole('heading', { name: '新标题' });
  fireEvent.click(screen.getByRole('button', { name: /^另一个会话/ }));
  await screen.findByRole('heading', { name: other.title });
  await act(async () => stale.resolve([initialSession, other]));
  expect(screen.getByLabelText('新标题 更多操作')).toBeTruthy();
  expect(screen.getByRole('heading', { name: other.title })).toBeTruthy();
});

it('a canonical archive cannot be undone by an older global list', async () => {
  const adapter = new FakeAdapter();
  adapter.sessions = [{ ...initialSession, canArchive: true }];
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByRole('heading', { name: initialSession.title });
  const stale = deferred<SessionSummary[]>();
  const lists = vi.spyOn(adapter, 'listSessions').mockImplementationOnce(() => stale.promise);
  fireEvent.focus(window);
  await waitFor(() => expect(lists).toHaveBeenCalled());
  fireEvent.click(screen.getByLabelText(`${initialSession.title} 更多操作`));
  fireEvent.click(await screen.findByRole('button', { name: '归档会话' }));
  await screen.findByText('会话已归档');
  await act(async () => stale.resolve([{ ...initialSession, canArchive: true }]));
  expect(screen.queryByLabelText(`${initialSession.title} 更多操作`)).toBeNull();
  expect(adapter.sessions).toHaveLength(0);
});
