import { uiPreferences } from '../../frontend/lib/ui-preferences';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { LocalScheduledTasksApi } from '../../frontend/lib/scheduled-tasks-api';
import { promptContentTextProjection } from '../../frontend/lib/prompt-content';
import { RuntimeApiError } from '../../frontend/lib/runtime-adapter';
import type { CommandReceipt, LocalPromptSubmission, RuntimeAdapter, RuntimeBootstrap, RuntimeConnection, RuntimeProjection, WorkspaceAvailability } from '../../frontend/lib/runtime-adapter';
import type { AgentTask, CapabilitySnapshot, ProjectCapabilityMutationResult, SessionSummary, UserCapabilitySnapshot } from '../../frontend/lib/pulsara-types';

import PulsaraApp from '../../frontend/app/pulsara-app';
import { PromptDraftStore } from '../../frontend/lib/prompt-draft';

import { isWelcomeHeading, deferred, textPrompt, stubClipboard, typeComposer, composerIsDisabled, bootstrap, initialSession, SOURCE_FIDELITY_MARKDOWN, SOURCE_FIDELITY_USER_TEXT, capabilitySnapshot, userCapabilitySnapshot, projection, FakeConnection, FakeAdapter, renderWorkbench } from './pulsara-app-fixture';

describe('PulsaraApp', () => {
  it('keeps the original stop error when its command result is unavailable, without resending', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(), hostSessionId: 'host:stop-error', controlAdmissionDeadlineMs: 42_000,
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    const active = adapter.lastConnection!;
    const stop = vi.spyOn(active, 'stopActiveTurn').mockRejectedValue(
      new RuntimeApiError('BAD_REQUEST', '请求内容不正确。', false),
    );
    const query = vi.spyOn(active, 'queryControlCommand');
    fireEvent.click(screen.getByRole('button', { name: '停止本轮运行' }));
    expect(await screen.findByText(/请求内容不正确。 未查到原操作结果/)).toBeTruthy();
    expect(screen.queryByText(/原操作结果已不在当前 Host 的保留窗口/)).toBeNull();
    expect(stop).toHaveBeenCalledTimes(1);
    expect(query).toHaveBeenCalledTimes(1);
    expect(query).toHaveBeenCalledWith(stop.mock.calls[0][0]);
  });

  const archivedTargets = (count: number): SessionSummary[] => Array.from({length: count}, (_, index) => ({
    ...initialSession, id: `archived-${index + 1}`, title: `归档批次 ${index + 1}`, lifecycle: 'ARCHIVED', live: false,
  }));
  async function openArchived(adapter: FakeAdapter) {
    const view = renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    fireEvent.click(screen.getByRole('button', {name: '设置'}));
    fireEvent.click(screen.getByRole('button', {name: '已归档会话', exact: true}));
    await screen.findByText('归档批次 1');
    return view;
  }

  it('bulk deletes every frozen page with three requests, keeps progress during navigation, and excludes new archives', async () => {
    const adapter = new FakeAdapter();
    adapter.archived = archivedTargets(12);
    const held = new Map(adapter.archived.map(session => [session.id, deferred<Awaited<ReturnType<RuntimeAdapter['deleteArchivedSession']>>>()]));
    adapter.deleteArchivedSession.mockImplementation(async id => {
      const result = await held.get(id)!.promise;
      adapter.archived = adapter.archived.filter(session => session.id !== id);
      return result;
    });
    await openArchived(adapter);
    fireEvent.click(screen.getByRole('button', {name: '删除全部'}));
    expect(screen.getByRole('dialog').textContent).toContain('12 条已归档会话');
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: '取消'}));
    expect(adapter.deleteArchivedSession).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', {name: '删除全部'}));
    adapter.archived = [...adapter.archived, {...initialSession, id: 'new-archive', title: '后来归档', lifecycle: 'ARCHIVED', live: false}];
    const confirm = within(screen.getByRole('dialog')).getByRole('button', {name: '永久删除'});
    fireEvent.click(confirm); fireEvent.click(confirm);
    expect(adapter.deleteArchivedSession).toHaveBeenCalledTimes(3);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.querySelector<HTMLElement>('main.pulsara-shell')?.inert).toBeFalsy();
    await act(async () => held.get('archived-1')!.resolve({status: 'DELETED', session_id: 'archived-1', cleanup_warning: null}));
    await waitFor(() => expect(adapter.deleteArchivedSession).toHaveBeenCalledTimes(4));
    fireEvent.click(screen.getByRole('button', {name: '会话', exact: true}));
    expect(screen.getByRole('button', {name: /正在删除归档会话 · 已处理 1 \/ 12/})).toBeTruthy();
    fireEvent.click(screen.getByRole('button', {name: '设置'}));
    fireEvent.click(screen.getByRole('button', {name: '已归档会话', exact: true}));
    expect(screen.getByRole('button', {name: '删除全部'})).toHaveProperty('disabled', true);
    await act(async () => { for (const [id, pending] of held) pending.resolve({status: 'DELETED', session_id: id, cleanup_warning: null}); });
    const resultToast = await screen.findByRole('button', {name: /归档会话删除结果 · 已处理 12 \/ 12/});
    expect(resultToast.closest('.toast-stack')).not.toBeNull();
    expect(adapter.deleteArchivedSession.mock.calls.map(([id]) => id)).toEqual([...held.keys()]);
    expect(adapter.deleteSession).not.toHaveBeenCalled();
    await screen.findByText('后来归档');
    expect(screen.queryByRole('navigation', {name: '已归档会话分页'})).toBeNull();
    fireEvent.click(screen.getByRole('button', {name: /归档会话删除结果 ·/}));
    expect(within(screen.getByRole('list', {name: '归档删除结果'})).getAllByRole('listitem')).toHaveLength(5);
    expect(screen.getByText('查看各会话的删除结果。')).toBeTruthy();
    expect(screen.getByRole('dialog').querySelector('footer')).toBeNull();
    expect(screen.queryByRole('button', {name: '重试未完成项…'})).toBeNull();
    fireEvent.click(within(screen.getByRole('navigation', {name: '归档删除结果分页'})).getByRole('button', {name: '下一页'}));
    expect(screen.getByText('归档批次 6')).toBeTruthy();
    expect(screen.queryByRole('button', {name: /归档会话删除结果 ·/})).toBeNull();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: '关闭删除结果', exact: true}));
    fireEvent.click(screen.getByRole('button', {name: '会话', exact: true}));
    expect(screen.queryByRole('button', {name: /归档会话删除结果 ·/})).toBeNull();
  });

  it.each([false, true])('consumes completed results once viewed, including completion during viewing (close while running: %s)', async closeWhileRunning => {
    const adapter = new FakeAdapter();
    adapter.archived = archivedTargets(1);
    const pending = deferred<Awaited<ReturnType<RuntimeAdapter['deleteArchivedSession']>>>();
    adapter.deleteArchivedSession.mockImplementation(() => pending.promise);
    await openArchived(adapter);
    fireEvent.click(screen.getByRole('button', {name: '删除全部'}));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: '永久删除'}));
    const progress = await screen.findByRole('button', {name: /正在删除归档会话 ·/});
    expect(progress.closest('.toast-stack')).not.toBeNull();
    fireEvent.click(progress);
    expect(screen.queryByRole('button', {name: /正在删除归档会话 ·/})).toBeNull();
    if (closeWhileRunning) {
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: '关闭删除结果', exact: true}));
      expect(screen.getByRole('button', {name: /正在删除归档会话 ·/})).toBeTruthy();
    }
    await act(async () => pending.resolve({status: 'DELETED', session_id: 'archived-1', cleanup_warning: null}));
    if (closeWhileRunning) {
      fireEvent.click(await screen.findByRole('button', {name: /归档会话删除结果 · 已处理 1 \/ 1/}));
    } else {
      await screen.findByText('本次处理已结束');
    }
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: '关闭删除结果', exact: true}));
    expect(screen.queryByRole('button', {name: /归档会话删除结果 ·/})).toBeNull();
    expect(screen.queryByText('归档会话已处理')).toBeNull();
  });

  it('keeps partial results and quarantine warnings, forgets only confirmed absence, and retries only incomplete targets', async () => {
    const adapter = new FakeAdapter();
    adapter.archived = archivedTargets(6);
    const forget = vi.spyOn(PromptDraftStore.prototype, 'removeSession');
    adapter.deleteArchivedSession.mockImplementation(async id => {
      if (id === 'archived-4') throw new RuntimeApiError('SESSION_DELETE_BUSY', '会话正忙', true);
      if (id === 'archived-6') throw new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', '网络断开', true);
      if (id === 'archived-3') return {status: 'NOT_ARCHIVED', session_id: id, cleanup_warning: {code: 'SESSION_DELETE_QUARANTINED', message: '本条已跳过，请重启 Pulsara'}};
      return {status: id === 'archived-2' ? 'ABSENT' : 'DELETED', session_id: id === 'archived-5' ? 'wrong-id' : id, cleanup_warning: null};
    });
    await openArchived(adapter);
    fireEvent.click(screen.getByRole('button', {name: '删除全部'}));
    adapter.listSessions.mockRejectedValueOnce(new Error('refresh failed'));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: '永久删除'}));
    await screen.findByRole('button', {name: /归档会话删除结果 · 已处理 6 \/ 6/});
    expect(forget.mock.calls.map(([id]) => id)).toEqual(['archived-1', 'archived-2']);
    fireEvent.click(screen.getByRole('button', {name: /归档会话删除结果 ·/}));
    expect(screen.getByText('本条已跳过，请重启 Pulsara')).toBeTruthy();
    expect(screen.getByText('会话正忙')).toBeTruthy();
    expect(screen.getByText(/操作结果已保留，但会话列表/)).toBeTruthy();
    const retry = screen.getByRole('button', {name: '重试未完成项…'});
    expect(retry.parentElement?.tagName).toBe('HEADER');
    expect(retry.nextElementSibling).toBe(screen.getByRole('button', {name: '关闭删除结果'}));
    expect(screen.getByRole('dialog').querySelector('footer')).toBeNull();
    fireEvent.click(retry);
    expect(screen.getByRole('dialog').textContent).toContain('3 条已归档会话');
    adapter.deleteArchivedSession.mockImplementation(async id => ({status: 'DELETED', session_id: id, cleanup_warning: null}));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: '永久删除'}));
    await screen.findByRole('button', {name: /归档会话删除结果 · 已处理 3 \/ 3/});
    expect(adapter.deleteArchivedSession.mock.calls.slice(6).map(([id]) => id)).toEqual(['archived-4', 'archived-5', 'archived-6']);
    forget.mockRestore();
  });

  it('stops claiming unsent bulk targets on root unmount and never resumes them in a new app', async () => {
    const adapter = new FakeAdapter();
    adapter.archived = archivedTargets(8);
    const pending = deferred<Awaited<ReturnType<RuntimeAdapter['deleteArchivedSession']>>>();
    adapter.deleteArchivedSession.mockImplementation(() => pending.promise);
    const view = await openArchived(adapter);
    fireEvent.click(screen.getByRole('button', {name: '删除全部'}));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', {name: '永久删除'}));
    expect(adapter.deleteArchivedSession).toHaveBeenCalledTimes(3);
    view.unmount();
    await act(async () => pending.resolve({status: 'DELETED', session_id: 'archived-1', cleanup_warning: null}));
    expect(adapter.deleteArchivedSession).toHaveBeenCalledTimes(3);
    await openArchived(adapter);
    expect(screen.queryByRole('button', {name: /删除结果 ·/})).toBeNull();
    expect(adapter.deleteArchivedSession).toHaveBeenCalledTimes(3);
  });

  it('stays on overview while restoring a saved live session and opens it only on navigation', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [{ ...initialSession, live: true }];
    uiPreferences.setItem('pulsara-active-session', initialSession.id);
    const pending = deferred<RuntimeBootstrap>();
    vi.spyOn(adapter, 'bootstrap').mockImplementation(() => pending.promise);
    render(<PulsaraApp adapter={adapter} />);
    expect(screen.getByRole('button', { name: '总览', exact: true }).getAttribute('aria-current')).toBe('page');
    expect(screen.queryByRole('region', { name: '会话工作台' })).toBeNull();
    await act(async () => pending.resolve(bootstrap));
    await screen.findByText('本地服务已连接');
    expect(adapter.connectCalls.map(call => call.sessionId)).toEqual([initialSession.id]);
    expect(screen.getByRole('button', { name: '总览', exact: true }).getAttribute('aria-current')).toBe('page');
    expect(screen.queryByRole('region', { name: '会话工作台' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
    await screen.findByLabelText('发送给 Pulsara');
    expect(adapter.connectCalls).toHaveLength(1);
  });

  it('keeps startup failures on overview', async () => {
    const adapter = new FakeAdapter();
    const onStartupSettled = vi.fn();
    vi.spyOn(adapter, 'bootstrap').mockRejectedValue(new RuntimeApiError('START_FAILED', '本地服务启动失败', false));
    render(<PulsaraApp adapter={adapter} onStartupSettled={onStartupSettled} />);
    await screen.findByText('连接失败');
    expect(onStartupSettled).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: '总览', exact: true }).getAttribute('aria-current')).toBe('page');
    expect(screen.queryByRole('region', { name: '会话工作台' })).toBeNull();
    expect(adapter.connectCalls).toHaveLength(0);
  });

  it('does not override user navigation when startup completes', async () => {
    const adapter = new FakeAdapter();
    const pending = deferred<RuntimeBootstrap>();
    vi.spyOn(adapter, 'bootstrap').mockImplementation(() => pending.promise);
    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(screen.getByRole('button', { name: '设置', exact: true }));
    await act(async () => pending.resolve(bootstrap));
    await waitFor(() => expect(adapter.lastConnection).toBeDefined());
    expect(screen.getByRole('button', { name: '设置', exact: true }).getAttribute('aria-current')).toBe('page');
  });

  it('restores the selected session after remounting in the same browser', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions.push({ ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '历史会话' });
    const view = renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    fireEvent.click(screen.getByRole('button', { name: /历史会话.*可恢复/ }));
    await screen.findByRole('heading', { name: '历史会话' });
    expect(uiPreferences.getItem('pulsara-active-session')).toBe('session:hrluejkx3v2ajzf4ec6bb3ierm');
    view.unmount();

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '历史会话' });
    expect(adapter.connectCalls.map(call => call.sessionId)).toEqual([
      'session:bwpogagzox7onyn52iumyctnkq', 'session:hrluejkx3v2ajzf4ec6bb3ierm', 'session:hrluejkx3v2ajzf4ec6bb3ierm',
    ]);
  });

  it('shows a selected session loading page immediately, including while closing the old connection', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions.push({ ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '历史会话' });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    const closing = deferred<void>();
    vi.spyOn(adapter.lastConnection!, 'close').mockImplementation(() => closing.promise);
    const opening = deferred<void>();
    const connect = adapter.connect.bind(adapter);
    const connectSpy = vi.spyOn(adapter, 'connect').mockImplementation(async (...args) => {
      await opening.promise;
      return connect(...args);
    });

    fireEvent.click(screen.getByRole('button', { name: /历史会话.*可恢复/ }));
    expect(screen.getByRole('region', { name: '会话加载页' })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toContain('正在恢复会话');
    const selected = screen.getByRole('button', { name: /历史会话.*正在载入/ });
    expect(selected.getAttribute('aria-current')).toBe('page');
    expect(screen.getByText('我已经开始检查。').closest('[hidden]')).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: '发送给 Pulsara' })).toBeNull();
    expect(screen.queryByRole('complementary', { name: '会话标签工作区' })).toBeNull();
    expect(connectSpy).not.toHaveBeenCalled();
    fireEvent.click(selected);
    await act(async () => closing.resolve());
    expect(connectSpy).toHaveBeenCalledTimes(1);
    await act(async () => opening.resolve());
    expect(screen.queryByRole('region', { name: '会话加载页' })).toBeNull();
    expect(screen.getByRole('heading', { name: '历史会话' })).toBeTruthy();
    expect(screen.getByLabelText('发送给 Pulsara')).toBeTruthy();
    expect(screen.getByRole('complementary', { name: '会话标签工作区' })).toBeTruthy();
  });

  it('keeps the failed target visible and retries it instead of reopening the previous session', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions.push({ ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '历史会话' });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    const connectSpy = vi.spyOn(adapter, 'connect').mockRejectedValueOnce(new Error('暂时无法恢复会话'));
    fireEvent.click(screen.getByRole('button', { name: /历史会话.*可恢复/ }));
    expect((await screen.findByRole('alert')).textContent).toContain('暂时无法恢复会话');
    expect(screen.getByRole('heading', { name: '历史会话' })).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: '发送给 Pulsara' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await screen.findByRole('textbox', { name: '发送给 Pulsara' });
    expect(connectSpy.mock.calls.map(args => args[0])).toEqual(['session:hrluejkx3v2ajzf4ec6bb3ierm', 'session:hrluejkx3v2ajzf4ec6bb3ierm']);
    expect(screen.queryByText('暂时无法恢复会话')).toBeNull();
  });

  it('opens the actual connection while independent workspace refresh is still pending', async () => {
    const adapter = new FakeAdapter();
    const refresh = deferred<WorkspaceAvailability>();
    adapter.workspaceAvailability.mockImplementation(() => refresh.promise);
    renderWorkbench(<PulsaraApp adapter={adapter} />);

    await screen.findByLabelText('发送给 Pulsara');
    expect(adapter.lastConnection?.closed).toBe(false);
    expect(screen.queryByRole('region', { name: '会话加载页' })).toBeNull();
    expect(screen.queryByText('工作目录已丢失')).toBeNull();
    await act(async () => refresh.resolve({path: '/tmp/project', outcome: 'MISSING', reason: 'missing'}));
    expect(await screen.findByText('工作目录已丢失')).toBeTruthy();
  });

  it.each(['success', 'error-after-read'] as const)('ignores late %s after selecting a different session', async outcome => {
    const adapter = new FakeAdapter();
    adapter.sessions.push(
      { ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '历史会话' },
      { ...initialSession, id: 'session:agtwi5siitkjqcaje6pp3y6ixu', title: '另一会话' },
    );
    adapter.connectionValues.set('session:agtwi5siitkjqcaje6pp3y6ixu', projection('另一会话的正文'));
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    const opening = deferred<void>();
    const existence = deferred<SessionSummary | null>();
    const connect = adapter.connect.bind(adapter);
    let lateConnection: FakeConnection | undefined;
    vi.spyOn(adapter, 'connect').mockImplementationOnce(async (...args) => {
      await opening.promise;
      if (outcome === 'error-after-read') throw new Error('旧会话恢复失败');
      lateConnection = await connect(...args);
      return lateConnection;
    });
    if (outcome === 'error-after-read') adapter.readSession.mockImplementationOnce(() => existence.promise);
    fireEvent.click(screen.getByRole('button', { name: /历史会话.*可恢复/ }));
    if (outcome === 'error-after-read') {
      await act(async () => opening.resolve());
      expect(adapter.readSession).toHaveBeenCalledWith('session:hrluejkx3v2ajzf4ec6bb3ierm');
    } else {
      await act(async () => {});
    }
    fireEvent.click(screen.getByRole('button', { name: /另一会话.*可恢复/ }));
    await screen.findByText('另一会话的正文');
    await act(async () => {
      if (outcome === 'success') opening.resolve();
      else existence.resolve(adapter.sessions.find(session => session.id === 'session:hrluejkx3v2ajzf4ec6bb3ierm')!);
    });
    expect(screen.getByRole('heading', { name: '另一会话' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: '会话加载页' })).toBeNull();
    expect(screen.queryByText('旧会话恢复失败')).toBeNull();
    expect(screen.getByLabelText('发送给 Pulsara').getAttribute('contenteditable')).toBe('true');
    if (outcome === 'success') expect(lateConnection?.closed).toBe(true);
  });

  it('does not connect an abandoned selection after its previous connection finally closes', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions.push(
      { ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '历史会话' },
      { ...initialSession, id: 'session:agtwi5siitkjqcaje6pp3y6ixu', title: '另一会话' },
    );
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    const closing = deferred<void>();
    vi.spyOn(adapter.lastConnection!, 'close').mockImplementation(() => closing.promise);
    fireEvent.click(screen.getByRole('button', { name: /历史会话.*可恢复/ }));
    fireEvent.click(screen.getByRole('button', { name: /另一会话.*可恢复/ }));
    await screen.findByLabelText('发送给 Pulsara');
    await act(async () => closing.resolve());
    expect(adapter.connectCalls.map(call => call.sessionId)).toEqual(['session:bwpogagzox7onyn52iumyctnkq', 'session:agtwi5siitkjqcaje6pp3y6ixu']);
    expect(screen.getByRole('heading', { name: '另一会话' })).toBeTruthy();
  });

  it.each(['missing', 'deleted'] as const)('exits loading when its target is %s', async outcome => {
    const adapter = new FakeAdapter();
    adapter.sessions.push({ ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '历史会话' });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    const opening = deferred<void>();
    const connect = adapter.connect.bind(adapter);
    let lateConnection: FakeConnection | undefined;
    vi.spyOn(adapter, 'connect').mockImplementationOnce(async (...args) => {
      await opening.promise;
      if (outcome === 'missing') throw new Error('会话已不存在');
      lateConnection = await connect(...args);
      return lateConnection;
    });
    fireEvent.click(screen.getByRole('button', { name: /历史会话.*可恢复/ }));
    await act(async () => {});
    if (outcome === 'missing') {
      adapter.readSession.mockResolvedValueOnce(null);
    } else {
      fireEvent.click(screen.getByLabelText('历史会话 更多操作'));
      fireEvent.click(screen.getAllByRole('button', { name: '删除会话…' }).at(-1)!);
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^(停止并删除|永久删除)$/ }));
      await screen.findByText('会话已删除');
    }
    await act(async () => opening.resolve());
    expect(screen.queryByRole('region', { name: '会话加载页' })).toBeNull();
    expect(screen.queryByLabelText('历史会话 更多操作')).toBeNull();
    expect(screen.getByRole('heading', { name: '尚未选择会话' })).toBeTruthy();
    if (outcome === 'deleted') expect(lateConnection?.closed).toBe(true);
  });

  it('shows canonical ROOT interruption instead of treating idle as completed', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(), isRunning: false, activeTurnId: undefined,
      control: { latest_root_turn: {
        turn_id: 'turn-interrupted', status: 'INTERRUPTED',
        terminal_reason: 'FOREGROUND_EXECUTION_INTERRUPTED',
      } },
    };
    adapter.connectionValue.interruptionNotices = [{ownerKind: "EXECUTED_TURN", ownerId: "turn-interrupted", reason: "FOREGROUND_EXECUTION_INTERRUPTED", terminalAtUtc: "2026-10-05T00:00:00Z", displayAfterEntrySequence: 1, displayAfterMessageId: adapter.connectionValue.messages[0].id}];
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const notice = await screen.findByText('本轮回复已中断。');
    expect(notice.closest('.conversation-interruption')).toBeTruthy();
    expect(notice.closest('.runtime-banner')).toBeNull();
    const workbench = screen.getByRole('region', { name: '会话工作台' });
    expect(within(workbench).getByText('已中断')).toBeTruthy();
    expect(within(workbench).queryByText('FOREGROUND_EXECUTION_INTERRUPTED')).toBeNull();
  });

  it('opens the real workbench state supplied by its adapter', async () => {
    renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
    expect(await screen.findByRole('heading', { name: '准备发布' })).toBeTruthy();
    expect(screen.getByLabelText('发送给 Pulsara')).toBeTruthy();
    expect(await screen.findByLabelText('TODO清单')).toBeTruthy();
    expect(screen.getAllByText('TODO清单')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '收起TODO清单' }).textContent).toContain('第 2 / 2 步');
    expect(screen.getByText('检查实现')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '收起TODO清单' }));
    expect(screen.queryByLabelText('TODO清单')).toBeNull();
    expect(screen.getByRole('button', { name: '展开TODO清单' })).toBeTruthy();
    expect(screen.queryByText(/文件已更改|项变更/)).toBeNull();
    expect(screen.queryByRole('button', { name: '添加附件' })).toBeNull();
    expect(screen.getByRole('button', { name: '能力' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '记忆' })).toBeTruthy();
  });

  it('keeps the full session catalog in the composer and user-owned capabilities on the first-class page', async () => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);

    fireEvent.click(await screen.findByRole('button', { name: '添加文件、技能或规划' }));
    const skillButton = await screen.findByRole('button', { name: '选择技能' });
    fireEvent.click(skillButton);
    fireEvent.click(screen.getByRole('button', { name: /\/pdf/ }));
    expect((await screen.findByLabelText('技能：pdf')).getAttribute('data-skill-reference')).toBe('/pdf');
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toBe('pdf ');

    fireEvent.click(screen.getByRole('button', { name: '能力' }));
    expect(await screen.findByRole('heading', { name: '能力' })).toBeTruthy();
    expect(await screen.findByText('Personal Tools')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '查看 Personal Tools' }));
    expect(within(screen.getByText('Personal Tools').closest('article')!).getByText('钩子').textContent).toBe('3 钩子');
    expect(screen.queryByText('已启用；会话将在安全时机采用')).toBeNull();
    expect(screen.getByRole('switch', { name: '关闭 Personal Tools' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.queryByText('已用于当前打开的会话')).toBeNull();
    expect(screen.queryByText('/Users/test/.pulsara/plugins/personal-tools')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: /技能/ }));
    const skillSwitch = await screen.findByRole('switch', { name: '关闭 personal-pdf' });
    expect(skillSwitch.getAttribute('aria-checked')).toBe('true');
    fireEvent.click(skillSwitch);
    expect(adapter.setUserSkillEnabled).toHaveBeenCalledWith(
      '/Users/test/.agents/skills/personal-pdf/SKILL.md',
      false,
    );
    expect(await screen.findByRole('switch', { name: '开启 personal-pdf' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '用于下一轮' })).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: /MCP/ }));
    expect(await screen.findByText('个人文档')).toBeTruthy();
    expect(screen.queryByText('本地文档')).toBeNull();
    expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session:bwpogagzox7onyn52iumyctnkq');
    expect(adapter.inspectUserCapabilities).toHaveBeenCalledWith(expect.any(AbortSignal));
  });

  it('manages directory capabilities in the session inspector and keeps inherited rows read-only', async () => {
    const adapter = new FakeAdapter();
    const projectSkill: CapabilitySnapshot['skills']['items'][number] = {
      id: 'pulsara:project-review',
      name: 'project-review',
      description: '检查当前项目。',
      location: '.pulsara/skills/project-review/SKILL.md',
      path: '/tmp/pulsara_agent/.pulsara/skills/project-review/SKILL.md',
      source: 'workspace',
      editable: true,
      enabled: true,
      effective: true,
      configured: false,
      authoringNotes: [],
    };
    const projectMcp: CapabilitySnapshot['mcp']['servers'][number] = {
      ...capabilitySnapshot.mcp.servers[0],
      id: 'project-docs',
      name: '项目文档',
      source: 'workspace',
      editable: true,
      configIdentity: 'project-config-v1',
      enabled: true,
      configuredEnabled: true,
      transport: { kind: 'http', summary: 'https://example.com/mcp', detail: 'https://example.com/mcp' },
    };
    const projectSnapshot: CapabilitySnapshot = {
      ...capabilitySnapshot,
      skills: {
        ...capabilitySnapshot.skills,
        items: [projectSkill, ...capabilitySnapshot.skills.items],
      },
      mcp: {
        ...capabilitySnapshot.mcp,
        servers: [projectMcp, ...capabilitySnapshot.mcp.servers],
      },
    };
    adapter.inspectCapabilities.mockResolvedValue(projectSnapshot);
    adapter.setProjectSkillEnabled.mockResolvedValue({
      operation: { status: 'DISABLED', success: true, message: '项目技能已关闭。', details: [] },
      adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-provider-dispatch' },
      capabilities: {
        ...projectSnapshot,
        adoption: { scope: 'workspace', pending: true, when: 'next-provider-dispatch' },
        skills: {
          ...projectSnapshot.skills,
          items: projectSnapshot.skills.items.map((item) => (
            item.id === projectSkill.id ? { ...item, enabled: false } : item
          )),
        },
      },
    });

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session:bwpogagzox7onyn52iumyctnkq'));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));

    expect(await within(inspector).findByText('项目能力')).toBeTruthy();
    expect(within(inspector).getByText('同一目录的所有会话', { exact: false })).toBeTruthy();
    const projectSwitch = within(inspector).getByRole('switch', { name: '关闭 project-review' });
    fireEvent.click(projectSwitch);
    await waitFor(() => expect(adapter.setProjectSkillEnabled).toHaveBeenCalledWith(
      'session:bwpogagzox7onyn52iumyctnkq',
      'pulsara:project-review',
      false,
    ));
    await waitFor(() => expect(projectSwitch.getAttribute('aria-checked')).toBe('false'));
    expect(within(inspector).queryByText('更改已保存')).toBeNull();
    expect(within(inspector).queryByRole('switch', { name: /pdf/ })).toBeNull();

    fireEvent.click(within(inspector).getByRole('tab', { name: /MCP/ }));
    expect(await within(inspector).findByText('项目文档')).toBeTruthy();
    expect(within(inspector).getByRole('switch', { name: '关闭 项目文档' })).toBeTruthy();
    expect(within(inspector).queryByRole('switch', { name: /本地文档/ })).toBeNull();
  });

  it('keeps ordinary Skill overrides quiet and explains precedence on the info icon', async () => {
    const adapter = new FakeAdapter();
    const hiddenPath = '/Users/test/source/src/pulsara_agent/bundled_skills/pdf/SKILL.md';
    const hiddenWinner = '/Users/test/.pulsara/skills/pdf/SKILL.md';
    adapter.inspectCapabilities.mockResolvedValue({
      ...capabilitySnapshot,
      skills: {
        ...capabilitySnapshot.skills,
        issues: [{
          kind: 'shadowed',
          title: 'pdf 使用了优先级更高的版本',
          path: hiddenPath,
          details: [`当前使用：${hiddenWinner}`],
        }],
        details: ['internal producer unavailable'],
      },
    });

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));

    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalled());
    expect(within(inspector).queryByText(/pdf 使用了优先级更高的版本/)).toBeNull();
    const rules = within(inspector).getByRole('button', { name: '技能覆盖规则' });
    expect(screen.queryByRole('tooltip')).toBeNull();
    fireEvent.mouseEnter(rules);
    const tooltip = screen.getByRole('tooltip');
    expect(tooltip.parentElement).toBe(document.body);
    expect(rules.getAttribute('aria-describedby')).toBe(tooltip.id);
    expect(within(tooltip).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      '当前目录下的 .pulsara/skills', '当前目录下的 .agents/skills', '用户级 Pulsara 目录下的 skills', '家目录下的 .agents/skills', '当前目录的插件提供的技能', '用户级插件提供的技能', 'Pulsara 自带的技能',
    ]);
    expect(within(tooltip).getByText('优先使用靠前的版本，其余版本会保留。')).toBeTruthy();
    fireEvent.mouseLeave(rules);
    await waitFor(() => expect(screen.queryByRole('tooltip')).toBeNull());
    fireEvent.focus(rules);
    expect(screen.getByRole('tooltip')).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('tooltip')).toBeNull();
    expect(within(inspector).getByText(/部分技能目录暂时无法完整读取/)).toBeTruthy();
    expect(within(inspector).queryByText(hiddenPath, { exact: false })).toBeNull();
    expect(within(inspector).queryByText(hiddenWinner, { exact: false })).toBeNull();
    expect(within(inspector).queryByText(/internal producer unavailable/)).toBeNull();
    fireEvent.mouseEnter(rules);
    fireEvent.click(within(screen.getByRole('tooltip')).getByRole('link', { name: '用户级 Pulsara 目录' }));
    expect(screen.queryByRole('tooltip')).toBeNull();
    const homeSetting = screen.getByRole('region', { name: 'Pulsara 家目录' });
    expect(screen.getByRole('button', {name:'通用'}).classList.contains('is-active')).toBe(true);
    expect(screen.queryByRole('button', {name:'本地服务'})).toBeNull();
    expect(homeSetting.classList.contains('is-highlighted')).toBe(true);
    expect(document.activeElement).toBe(homeSetting);
    expect(within(homeSetting).queryByText('当前路径，只读')).toBeNull();
    const homePath = within(homeSetting).getByRole('textbox', { name: 'Pulsara 家目录' });
    expect(homePath).toHaveProperty('value', '/Users/test/custom-pulsara-home');
    expect(homePath).toHaveProperty('disabled', true);
    fireEvent.animationEnd(homeSetting);
    await waitFor(() => expect(homeSetting.classList.contains('is-highlighted')).toBe(false), { timeout: 3000 });
  });

  it('keeps disabled or shadowed skills out of the composer suggestions', async () => {
    const adapter = new FakeAdapter();
    adapter.inspectCapabilities.mockResolvedValue({
      ...capabilitySnapshot,
      skills: {
        ...capabilitySnapshot.skills,
        items: [{
          id: 'pulsara:disabled-review',
          name: 'disabled-review',
          description: '当前目录中已停用的技能。',
          location: '.pulsara/skills/disabled-review/SKILL.md',
          path: '/tmp/pulsara_agent/.pulsara/skills/disabled-review/SKILL.md',
          source: 'workspace',
          editable: true,
          enabled: false,
          effective: false,
          configured: false,
          authoringNotes: [],
        }],
      },
    });

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session:bwpogagzox7onyn52iumyctnkq'));
    await waitFor(() => expect(screen.queryByRole('button', { name: '选择技能' })).toBeNull());
  });

  it('renders the project capability dialog above the app and isolates its background', async () => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    const addButton = await within(inspector).findByRole('button', { name: '添加' });
    addButton.focus();
    fireEvent.click(addButton);

    const dialog = await screen.findByRole('dialog', { name: '导入技能' });
    expect(inspector.contains(dialog)).toBe(false);
    const application = document.querySelector<HTMLElement>('main.pulsara-shell');
    expect(application?.inert).toBe(true);
    fireEvent.keyDown(window, {key: 'Escape'});
    expect(application?.inert).toBe(false);
    fireEvent.click(within(inspector).getByRole('tab', { name: /MCP/ }));
    fireEvent.click(addButton);
    const mcpDialog = await screen.findByRole('dialog', { name: '添加 MCP 服务' });
    expect(inspector.contains(mcpDialog)).toBe(false);
    expect((within(mcpDialog).getByRole('checkbox', { name: '也向委派 Agent 提供' }) as HTMLInputElement).checked).toBe(false);

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '导入技能' })).toBeNull();
    expect(screen.queryByRole('dialog', { name: '添加 MCP 服务' })).toBeNull();
    expect(application?.inert).toBe(false);
    await waitFor(() => expect(document.activeElement).toBe(addButton));
  });

  it.each([
    ['skills', '添加', '导入技能', '技能'],
    ['mcp', '添加', '添加 MCP 服务', 'MCP'],
    ['mcp', '导入 MCP', '导入 MCP 配置', 'MCP'],
  ])('opens %s / %s with a scope notice linking to the matching user capability tab', async (kind, action, dialogName, label) => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    if (kind === 'mcp') fireEvent.click(await within(inspector).findByRole('tab', { name: /MCP/ }));
    fireEvent.click(await within(inspector).findByRole('button', { name: action, exact: true }));
    await screen.findByRole('dialog', { name: dialogName });
    expect(screen.getByText(`这里添加的${label}仅会应用到当前目录中`)).toBeTruthy();
    const link = screen.getByRole('link', { name: '能力页' });
    expect(link.parentElement?.textContent).toBe(`如果需要添加用户级${label}，请在能力页添加`);
    expect(link.closest('button')).toBeNull();
    fireEvent.click(link);
    await screen.findByRole('heading', { name: '能力' });
    const selected = screen.getByRole('tab', { name: new RegExp(`^${label} `) });
    expect(selected.getAttribute('aria-selected')).toBe('true');
    expect(screen.queryByRole('dialog', { name: dialogName })).toBeNull();
    expect(Boolean(document.querySelector<HTMLElement>('main.pulsara-shell')?.inert)).toBe(false);
    await waitFor(() => expect(adapter.inspectUserCapabilities).toHaveBeenCalled());
  });

  it('retargets the scope notice link after the user has already opened another capability tab', async () => {
    renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(await within(inspector).findByRole('button', { name: '添加', exact: true }));
    await screen.findByRole('dialog', { name: '导入技能' });
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: '能力', exact: true }));
    await screen.findByRole('heading', { name: '能力' });
    fireEvent.click(screen.getByRole('tab', { name: /^MCP / }));
    fireEvent.click(screen.getByRole('link', { name: '能力页' }));
    expect(screen.getByRole('tab', { name: /^技能 / }).getAttribute('aria-selected')).toBe('true');
  });

  it.each(['skill', 'mcp-import', 'mcp-create'] as const)(
    'submits %s from the sidebar to the selected directory, preserves rejected drafts, and permits retry', async entry => {
      const adapter = new FakeAdapter();
      const rejected = '同名能力已存在，请更换名称。';
      const skillCandidate = {sourcePath: '/tmp/sidebar-skill', name: 'sidebar-skill', description: '检查当前目录', valid: true, details: []};
      adapter.previewSkillImport.mockResolvedValue([skillCandidate]);
      vi.spyOn(adapter as RuntimeAdapter, 'previewMcpImport').mockResolvedValue([
        {server_id: 'sidebar-mcp', transport: 'stdio', fields: [], notices: [], issues: []},
      ]);
      const skillSuccess = await adapter.installSkill(initialSession.id, skillCandidate);
      adapter.installSkill.mockClear();
      adapter.installSkill.mockResolvedValueOnce({
        ...skillSuccess, installation: {...skillSuccess.installation, installed: false, message: rejected},
      }).mockResolvedValueOnce(skillSuccess);
      const mcpSuccess = {
        operation: {status: 'ADDED', success: true, message: '添加成功', details: []},
        adoption: {scope: 'workspace', pendingSessions: 1, when: 'next-provider-dispatch'},
        capabilities: capabilitySnapshot,
      } satisfies ProjectCapabilityMutationResult;
      const mcpFailure = {...mcpSuccess, operation: {...mcpSuccess.operation, success: false, message: rejected}};
      adapter.createProjectMcp.mockResolvedValueOnce(mcpFailure).mockResolvedValueOnce(mcpSuccess);
      adapter.importProjectMcp.mockResolvedValueOnce(mcpFailure).mockResolvedValueOnce(mcpSuccess);

      renderWorkbench(<PulsaraApp adapter={adapter} />);
      const inspector = await screen.findByLabelText('会话标签工作区');
      fireEvent.click(within(inspector).getByRole('button', {name: '项目能力'}));
      if (entry !== 'skill') fireEvent.click(await within(inspector).findByRole('tab', {name: /MCP/}));
      fireEvent.click(await within(inspector).findByRole('button', {name: entry === 'mcp-import' ? '导入 MCP' : '添加', exact: true}));
      const dialog = await screen.findByRole('dialog');
      let submit: HTMLElement;
      if (entry === 'skill') {
        adapter.pickWorkspaceDirectory.mockResolvedValueOnce(skillCandidate.sourcePath);
        fireEvent.click(within(dialog).getByRole('button', {name: '来源目录 选择目录'}));
        await within(dialog).findByRole('button', {name: `来源目录 ${skillCandidate.sourcePath}`});
        fireEvent.click(within(dialog).getByRole('button', {name: '读取预览'}));
        await within(dialog).findByLabelText('安装名称');
        submit = within(dialog).getByRole('button', {name: '安装所选技能'});
      } else if (entry === 'mcp-import') {
        fireEvent.change(within(dialog).getByLabelText('配置内容'), {target: {value: '{"mcpServers":{"sidebar-mcp":{"command":"fixture"}}}'}});
        fireEvent.click(within(dialog).getByRole('button', {name: '读取预览'}));
        await within(dialog).findByLabelText('选择服务（分别导入）');
        submit = within(dialog).getByRole('button', {name: '确认导入此服务'});
      } else {
        fireEvent.change(within(dialog).getByLabelText('服务 ID'), {target: {value: 'sidebar-mcp'}});
        fireEvent.change(within(dialog).getByLabelText('服务地址'), {target: {value: 'https://example.org/mcp'}});
        fireEvent.change(within(dialog).getByLabelText('认证方式'), {target: {value: 'bearer'}});
        fireEvent.change(within(dialog).getByLabelText('Token'), {target: {value: 'sidebar-test-secret'}});
        submit = within(dialog).getByRole('button', {name: '保存连接'});
      }
      fireEvent.click(submit);
      await screen.findByText(rejected);
      expect(screen.getByRole('dialog')).toBe(dialog);
      expect(within(dialog).queryByText('已安装')).toBeNull();
      expect(within(dialog).queryByRole('button', {name: '已导入'})).toBeNull();
      await waitFor(() => expect(submit).toHaveProperty('disabled', false));
      fireEvent.click(submit);

      if (entry === 'skill') {
        expect(await within(dialog).findByText('已安装')).toBeTruthy();
        expect(adapter.previewSkillImport).toHaveBeenCalledExactlyOnceWith(skillCandidate.sourcePath);
        expect(adapter.installSkill).toHaveBeenLastCalledWith(initialSession.id, skillCandidate);
        expect(adapter.installSkill).toHaveBeenCalledTimes(2);
      } else if (entry === 'mcp-import') {
        expect(await within(dialog).findByRole('button', {name: '已导入'})).toHaveProperty('disabled', true);
        expect(adapter.importProjectMcp).toHaveBeenCalledTimes(2);
        expect(adapter.importProjectMcp).toHaveBeenLastCalledWith(initialSession.id, {
          content: '{"mcpServers":{"sidebar-mcp":{"command":"fixture"}}}', shape: 'auto',
          selected_server_id: 'sidebar-mcp', classifications: {}, values: {},
        });
      } else {
        await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
        expect(adapter.createProjectMcp).toHaveBeenCalledTimes(2);
        expect(adapter.createProjectMcp).toHaveBeenLastCalledWith(initialSession.id, expect.objectContaining({
          serverId: 'sidebar-mcp', config: expect.objectContaining({transport: expect.objectContaining({endpoint: 'https://example.org/mcp'})}),
          secretChanges: [{binding: {owner: {kind: 'local', scope_key: capabilitySnapshot.credentialScopeKey, server_id: 'sidebar-mcp', plugin_id: null}, name: 'bearer'}, value: 'sidebar-test-secret'}],
        }));
      }
      expect(adapter.installUserSkill).not.toHaveBeenCalled();
      expect(adapter.importUserMcp).not.toHaveBeenCalled();
      expect(adapter.createUserMcp).not.toHaveBeenCalled();
    },
  );

  it('keeps the project capability dialog open while its mutation is saving', async () => {
    const adapter = new FakeAdapter();
    adapter.previewSkillImport.mockResolvedValue([{sourcePath: '/tmp/market-skill', name: 'market-skill', description: 'A portable skill', valid: true, details: []}]);
    let finishInstallation!: (value: Awaited<ReturnType<FakeAdapter['installSkill']>>) => void;
    adapter.installSkill.mockImplementation(() => new Promise((resolve) => {
      finishInstallation = resolve;
    }));

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    const addButton = await within(inspector).findByRole('button', { name: '添加' });
    fireEvent.click(addButton);
    const dialog = await screen.findByRole('dialog', { name: '导入技能' });
    adapter.pickWorkspaceDirectory.mockResolvedValueOnce('/tmp/market-skill');
    fireEvent.click(within(dialog).getByRole('button', { name: '来源目录 选择目录' }));
    await within(dialog).findByRole('button', { name: '来源目录 /tmp/market-skill' });
    fireEvent.click(within(dialog).getByRole('button', { name: '读取预览' }));
    await within(dialog).findByLabelText('安装名称');
    fireEvent.click(within(dialog).getByRole('button', { name: '安装所选技能' }));

    await waitFor(() => expect(dialog.getAttribute('aria-busy')).toBe('true'));
    expect((addButton as HTMLButtonElement).disabled).toBe(true);
    for (const closeButton of within(dialog).getAllByRole('button', { name: '关闭' })) {
      expect((closeButton as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(closeButton);
    }
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog', { name: '导入技能' })).toBeTruthy();
    expect(adapter.installSkill).toHaveBeenCalledWith('session:bwpogagzox7onyn52iumyctnkq', {sourcePath: '/tmp/market-skill', name: 'market-skill', description: 'A portable skill', valid: true, details: []});

    await act(async () => finishInstallation({
      installation: {
        status: 'INSTALLED',
        installed: true,
        message: '技能已经安装。',
        sourcePath: '/tmp/market-skill',
        destinationPath: '/tmp/pulsara_agent/.pulsara/skills/market-skill',
        details: [],
      },
      adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-provider-dispatch' },
      capabilities: capabilitySnapshot,
    }));
    expect(await within(dialog).findByText('已安装')).toBeTruthy();
    fireEvent.keyDown(window, {key: 'Escape'});
    expect(screen.queryByRole('dialog', { name: '导入技能' })).toBeNull();
  });

  it('refreshes project skills after model management settles without a cached pending adoption', async () => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session:bwpogagzox7onyn52iumyctnkq'));
    expect(screen.queryByRole('switch', {name: '关闭 boundary-probe'})).toBeNull();
    adapter.inspectCapabilities.mockResolvedValue({
      ...capabilitySnapshot,
      skills: {...capabilitySnapshot.skills, items: [{
        ...capabilitySnapshot.skills.items[0]!,
        id: 'boundary-probe', name: 'boundary-probe', source: 'workspace',
        editable: true, path: '/tmp/pulsara_agent/.pulsara/skills/boundary-probe/SKILL.md',
      }]},
    });
    adapter.inspectCapabilities.mockClear();
    await act(async () => adapter.lastConnection?.emit({
      ...projection('Skill installed'), isRunning: false, eventSequence: 2,
      messages: [{id: 'managed-skill', role: 'assistant', time: '现在', body: 'Skill installed',
        status: 'completed', traces: [{id: 'install', kind: 'artifact', toolName: 'manage_capability',
          title: '管理扩展能力', subtitle: '配置变更已应用', status: 'completed',
          resultText: '{"status":"APPLIED","adoption":{"status":"RELOADED"}}'}]}],
    }));
    expect(await within(inspector).findByRole('switch', {name: '关闭 boundary-probe'})).toBeTruthy();
    expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session:bwpogagzox7onyn52iumyctnkq');
  });

  it('refreshes a stale MCP snapshot after a rejected project mutation', async () => {
    const adapter = new FakeAdapter();
    const projectMcp: CapabilitySnapshot['mcp']['servers'][number] = {
      ...capabilitySnapshot.mcp.servers[0]!,
      id: 'project-docs',
      name: '项目文档',
      source: 'workspace',
      editable: true,
      configIdentity: 'approval-v1',
      transport: { kind: 'http', summary: 'https://example.com/mcp', detail: 'https://example.com/mcp' },
    };
    const projectSnapshot = {
      ...capabilitySnapshot,
      mcp: { ...capabilitySnapshot.mcp, servers: [projectMcp] },
    };
    adapter.inspectCapabilities.mockResolvedValue(projectSnapshot);
    adapter.setProjectMcpEnabled.mockRejectedValueOnce(new Error('项目能力已经变化，正在读取最新状态。'));

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(await within(inspector).findByRole('tab', { name: /MCP/ }));
    adapter.inspectCapabilities.mockClear();
    fireEvent.click(await within(inspector).findByRole('switch', { name: '关闭 项目文档' }));

    await waitFor(() => expect(adapter.setProjectMcpEnabled).toHaveBeenCalled());
    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session:bwpogagzox7onyn52iumyctnkq'));
  });

  it('shows a durable adoption warning and the actual production MCP failure reasons', async () => {
    const adapter = new FakeAdapter();
    adapter.inspectCapabilities.mockResolvedValue({
      ...capabilitySnapshot,
      adoption: {
        ...capabilitySnapshot.adoption,
        attention: 'PROJECT_MCP_ADOPTION_INCOMPLETE',
      },
      mcp: {
        ...capabilitySnapshot.mcp,
        servers: [
          {
            ...capabilitySnapshot.mcp.servers[0]!,
            id: 'protocol-failure',
            name: '协议样本',
            source: 'workspace',
            editable: true,
            configIdentity: 'project-config-protocol',
            status: 'failed',
            hasFailure: true,
            failureCategory: 'McpProtocolConformanceError',
          },
          {
            ...capabilitySnapshot.mcp.servers[0]!,
            id: 'transport-failure',
            name: '连接样本',
            source: 'workspace',
            editable: true,
            configIdentity: 'project-config-transport',
            status: 'failed-retryable',
            hasFailure: true,
            failureCategory: 'McpTransportOperationError',
          },
        ],
      },
    });

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    expect(await within(inspector).findByText('部分项目连接未载入')).toBeTruthy();
    fireEvent.click(within(inspector).getByRole('tab', { name: /MCP/ }));
    expect(await within(inspector).findByText(/服务返回的内容不符合当前 MCP 要求/)).toBeTruthy();
    expect(await within(inspector).findByText(/连接在通信时中断/)).toBeTruthy();
  });

  it('does not describe a general project capability adoption failure as an MCP-only problem', async () => {
    const adapter = new FakeAdapter();
    adapter.inspectCapabilities.mockResolvedValue({
      ...capabilitySnapshot,
      adoption: {
        ...capabilitySnapshot.adoption,
        attention: 'PROJECT_CAPABILITY_ADOPTION_FAILED',
      },
    });

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    expect(await within(inspector).findByText('项目能力配置需要处理')).toBeTruthy();
    expect(within(inspector).getByText(/技能与连接设置/)).toBeTruthy();
  });

  it('does not let an older capability inspection overwrite a completed mutation', async () => {
    const adapter = new FakeAdapter();
    const projectMcp: CapabilitySnapshot['mcp']['servers'][number] = {
      ...capabilitySnapshot.mcp.servers[0]!,
      id: 'project-docs',
      name: '项目文档',
      source: 'workspace',
      editable: true,
      configIdentity: 'approval-v1',
      enabled: true,
      configuredEnabled: true,
      effective: true,
      status: 'connecting',
      transport: {
        kind: 'http',
        summary: 'https://example.com/mcp',
        detail: 'https://example.com/mcp',
      },
    };
    const inspectingSnapshot: CapabilitySnapshot = {
      ...capabilitySnapshot,
      mcp: { ...capabilitySnapshot.mcp, servers: [projectMcp] },
    };
    let resolveOlderInspection!: (snapshot: CapabilitySnapshot) => void;
    const olderInspection = new Promise<CapabilitySnapshot>((resolve) => {
      resolveOlderInspection = resolve;
    });
    adapter.inspectCapabilities
      .mockResolvedValueOnce(inspectingSnapshot)
      .mockReturnValue(olderInspection);
    adapter.setProjectMcpEnabled.mockResolvedValue({
      operation: { status: 'DISABLED', success: true, message: '项目 MCP 已关闭。', details: [] },
      adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-provider-dispatch' },
      capabilities: {
        ...inspectingSnapshot,
        mcp: {
          ...inspectingSnapshot.mcp,
          servers: [{
            ...projectMcp,
            configIdentity: 'approval-v2',
            enabled: false,
            configuredEnabled: false,
            effective: false,
            status: 'disabled',
          }],
        },
      },
    });

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(await within(inspector).findByRole('tab', { name: /MCP/ }));
    expect(await within(inspector).findByText(/连接中/)).toBeTruthy();
    await waitFor(
      () => expect(adapter.inspectCapabilities).toHaveBeenCalledTimes(2),
      { timeout: 1800 },
    );

    fireEvent.click(within(inspector).getByRole('switch', { name: '关闭 项目文档' }));
    expect(await within(inspector).findByRole('switch', { name: '开启 项目文档' })).toBeTruthy();
    await act(async () => resolveOlderInspection(inspectingSnapshot));
    expect(within(inspector).getByRole('switch', { name: '开启 项目文档' })).toBeTruthy();
  });

  it('does not paint a completed capability mutation onto a newly selected session', async () => {
    const adapter = new FakeAdapter();
    const secondSession: SessionSummary = {
      ...initialSession,
      id: 'session:hrluejkx3v2ajzf4ec6bb3ierm',
      title: '另一个会话',
      live: false,
      workspace: {
        id: 'workspace',
        name: 'pulsara_agent',
        path: '/tmp/pulsara_agent',
        kind: 'project',
      },
    };
    adapter.sessions = [initialSession, secondSession];
    const projectSkill: CapabilitySnapshot['skills']['items'][number] = {
      id: 'pulsara:project-review',
      name: 'project-review',
      description: '检查当前项目。',
      location: '.pulsara/skills/project-review/SKILL.md',
      path: '/tmp/pulsara_agent/.pulsara/skills/project-review/SKILL.md',
      source: 'workspace',
      editable: true,
      enabled: true,
      effective: true,
      configured: false,
      authoringNotes: [],
    };
    const firstSnapshot: CapabilitySnapshot = {
      ...capabilitySnapshot,
      sessionId: 'session:bwpogagzox7onyn52iumyctnkq',
      skills: { ...capabilitySnapshot.skills, items: [projectSkill] },
    };
    adapter.inspectCapabilities.mockImplementation(async (sessionId: string) => (
      sessionId === 'session:bwpogagzox7onyn52iumyctnkq'
        ? firstSnapshot
        : { ...capabilitySnapshot, sessionId: 'session:hrluejkx3v2ajzf4ec6bb3ierm' }
    ));
    let finishMutation!: (value: ProjectCapabilityMutationResult) => void;
    adapter.setProjectSkillEnabled.mockImplementation(() => (
      new Promise<ProjectCapabilityMutationResult>((resolve) => { finishMutation = resolve; })
    ));

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(await within(inspector).findByRole('switch', { name: '关闭 project-review' }));
    fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
    await screen.findByRole('heading', { name: '另一个会话' });

    await act(async () => {
      finishMutation({
        operation: { status: 'DISABLED', success: true, message: '项目技能已关闭。', details: [] },
        adoption: { scope: 'workspace', pendingSessions: 1, when: 'next-provider-dispatch' },
        capabilities: {
          ...firstSnapshot,
          skills: { ...firstSnapshot.skills, items: [{ ...projectSkill, enabled: false }] },
        },
      });
    });
    await waitFor(() => expect(within(inspector).queryByText('project-review')).toBeNull());
  });

  it('does not let a rejected mutation from an old session retire the new session inspection', async () => {
    const adapter = new FakeAdapter();
    const secondSession: SessionSummary = {
      ...initialSession,
      id: 'session:hrluejkx3v2ajzf4ec6bb3ierm',
      title: '新的能力会话',
      live: false,
      workspace: {
        id: 'workspace',
        name: 'pulsara_agent',
        path: '/tmp/pulsara_agent',
        kind: 'project',
      },
    };
    adapter.sessions = [initialSession, secondSession];
    const projectMcp: CapabilitySnapshot['mcp']['servers'][number] = {
      ...capabilitySnapshot.mcp.servers[0]!,
      id: 'project-docs',
      name: '项目文档',
      source: 'workspace',
      editable: true,
      configIdentity: 'approval-v1',
      transport: { kind: 'http', summary: 'https://example.com/mcp', detail: 'https://example.com/mcp' },
    };
    const firstSnapshot: CapabilitySnapshot = {
      ...capabilitySnapshot,
      sessionId: 'session:bwpogagzox7onyn52iumyctnkq',
      mcp: { ...capabilitySnapshot.mcp, servers: [projectMcp] },
    };
    let finishSecondInspection!: (value: CapabilitySnapshot) => void;
    const secondInspection = new Promise<CapabilitySnapshot>((resolve) => {
      finishSecondInspection = resolve;
    });
    adapter.inspectCapabilities.mockImplementation((sessionId: string) => (
      sessionId === 'session:bwpogagzox7onyn52iumyctnkq' ? Promise.resolve(firstSnapshot) : secondInspection
    ));
    let rejectOldMutation!: (reason: Error) => void;
    adapter.setProjectMcpEnabled.mockImplementation(() => new Promise((_, reject) => {
      rejectOldMutation = reject;
    }));

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(await within(inspector).findByRole('tab', { name: /MCP/ }));
    fireEvent.click(await within(inspector).findByRole('switch', { name: '关闭 项目文档' }));
    await waitFor(() => expect(adapter.setProjectMcpEnabled).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: /新的能力会话/ }));
    await screen.findByRole('heading', { name: '新的能力会话' });
    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledWith('session:hrluejkx3v2ajzf4ec6bb3ierm'));
    await act(async () => rejectOldMutation(new Error('项目能力已经变化。')));
    await act(async () => finishSecondInspection({
      ...capabilitySnapshot,
      sessionId: 'session:hrluejkx3v2ajzf4ec6bb3ierm',
    }));

    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(await within(inspector).findByRole('button', { name: /继承的能力/ }));
    expect(await within(inspector).findByText('本地文档')).toBeTruthy();
    await waitFor(() => expect(within(inspector).queryByText('正在加载...')).toBeNull());
    expect(adapter.inspectCapabilities.mock.calls.filter(([sessionId]) => sessionId === 'session:bwpogagzox7onyn52iumyctnkq')).toHaveLength(1);
  });

  it('refreshes capability adoption after the next turn without a pending banner', async () => {
    const adapter = new FakeAdapter();
    adapter.inspectCapabilities
      .mockResolvedValueOnce({
        ...capabilitySnapshot,
        adoption: { ...capabilitySnapshot.adoption, pending: true },
      })
      .mockResolvedValue({
        ...capabilitySnapshot,
        adoption: { ...capabilitySnapshot.adoption, pending: false },
      });

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    await within(inspector).findByRole('button', { name: /继承的能力/ });
    expect(within(inspector).queryByText('更改已保存')).toBeNull();

    await act(async () => {
      adapter.lastConnection?.emit({
        ...projection('能力配置已采用。'),
        messages: [{
          id: 'assistant-settled',
          role: 'assistant',
          time: '现在',
          body: '能力配置已采用。',
          status: 'completed',
        }],
        isRunning: false,
        activeTurnId: undefined,
      });
    });

    await waitFor(() => expect(adapter.inspectCapabilities).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(within(inspector).queryByText('更改已保存')).toBeNull());
  });

  it('keeps refreshing a settling MCP connection until its status is final', async () => {
    const adapter = new FakeAdapter();
    let resolveReady!: (snapshot: CapabilitySnapshot) => void;
    const readySnapshot = new Promise<CapabilitySnapshot>((resolve) => {
      resolveReady = resolve;
    });
    adapter.inspectCapabilities
      .mockResolvedValueOnce({
        ...capabilitySnapshot,
        mcp: {
          ...capabilitySnapshot.mcp,
          servers: capabilitySnapshot.mcp.servers.map((server) => ({
            ...server,
            source: 'workspace' as const,
            editable: true,
            status: 'connecting' as const,
          })),
        },
      })
      .mockReturnValue(readySnapshot);

    const settledSnapshot: CapabilitySnapshot = {
        ...capabilitySnapshot,
        mcp: {
          ...capabilitySnapshot.mcp,
          servers: capabilitySnapshot.mcp.servers.map((server) => ({
            ...server,
            source: 'workspace' as const,
            editable: true,
            status: 'ready' as const,
          })),
        },
      };

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const inspector = await screen.findByLabelText('会话标签工作区');
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('button', { name: '项目能力' }));
    fireEvent.click(within(inspector).getByRole('tab', { name: /MCP/ }));

    expect(await within(inspector).findByText(/连接中/)).toBeTruthy();
    await waitFor(
      () => expect(adapter.inspectCapabilities).toHaveBeenCalledTimes(2),
      { timeout: 1800 },
    );
    resolveReady(settledSnapshot);
    expect(await within(inspector).findByText(/已连接/)).toBeTruthy();
  });

  it('keeps a second page stable as an observer until the user explicitly takes control', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionRole = 'observer';
    renderWorkbench(<PulsaraApp adapter={adapter} />);

    expect((await screen.findAllByText('此窗口仅供查看')).length).toBeGreaterThan(0);
    expect(screen.queryByText('这里仍会实时显示对话、思考和任务进展。')).toBeNull();
    expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
    expect(screen.queryByRole('button', { name: '压缩上下文' })).toBeNull();
    expect(screen.getAllByText('观察中').length).toBeGreaterThan(0);

    fireEvent.click(screen.getAllByRole('button', { name: '在此窗口继续' })[0]!);

    expect(await screen.findByLabelText('发送给 Pulsara')).toBeTruthy();
    expect(screen.getByRole('button', { name: '压缩上下文' })).toBeTruthy();
    expect(adapter.connectCalls).toContainEqual({ sessionId: 'session:bwpogagzox7onyn52iumyctnkq', takeover: true });
  });

  it('explains when manual context compaction cannot shrink the current context', async () => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    adapter.lastConnection!.compactContext = vi.fn(async (): Promise<CommandReceipt> => ({
      commandId: 'command-compact-not-needed',
      status: 'succeeded',
      publicCode: 'NOT_NEEDED',
      publicMessage: 'CONTEXT_ALREADY_COMPACT',
    }));

    fireEvent.click(screen.getByRole('button', { name: '压缩上下文' }));

    expect(await screen.findByText('无需整理上下文')).toBeTruthy();
    expect(screen.getByText('当前上下文已经较紧凑，本次整理无法进一步缩小。')).toBeTruthy();
    expect(screen.queryByRole('separator', { name: '上下文已压缩' })).toBeNull();
  });

  it('places the adopted context boundary between old and successor messages', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(),
      messages: [{
        id: 'before-compaction',
        entrySequence: 1,
        role: 'user',
        time: '10:00',
        body: '压缩前的消息',
      }, {
        id: 'after-compaction',
        entrySequence: 2,
        role: 'user',
        time: '10:01',
        body: '压缩后的消息',
      }],
      history: { initialReady: true, hasOlder: false, loading: false, windows: [], compactions: [{
        eventId: 'adoption-1', turnId: 'turn-1', adoptedAfterEntrySequence: 1, displayAfterEntryId: 'before-compaction',
      }] },
      contextCompaction: {
        contextBindingRevisionId: 'revision-1',
        turnId: 'turn-1',
        sourceThroughSequence: 1,
        adoptedAfterEntrySequence: 1,
        acceptedAt: '2026-09-01T10:00:30Z',
      },
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);

    const before = await screen.findByText('压缩前的消息');
    const divider = screen.getByRole('separator', { name: '上下文已压缩' });
    const after = screen.getByText('压缩后的消息');

    expect(before.compareDocumentPosition(divider) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(divider.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(divider.querySelectorAll('.context-compaction-divider__line')).toHaveLength(2);
  });

  it('marks real speaker turns without breaking a continuous reasoning and tool flow', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'user-prompt', turnId: 'turn-one', role: 'user', userKind: 'prompt', time: '18:10',
        body: '检查完整流程。', status: 'completed',
      }, {
        id: 'assistant-tool', turnId: 'turn-one', role: 'assistant', assistantKind: 'tool-request', forkEligible: false,
        time: '18:11', body: '', status: 'completed',
        reasoning: [{ id: 'reasoning-before-tool', kind: 'full', body: '先读取文件。' }],
        traces: [{
          id: 'trace-read', kind: 'read', toolName: 'read_file', title: '读取文件', subtitle: '已完成',
          status: 'completed', meta: '操作完成',
        }],
      }, {
        id: 'user-steer', turnId: 'turn-one', role: 'user', userKind: 'steer', time: '18:12',
        body: '也检查真实页面。', status: 'waiting',
      }, {
        id: 'completion-one', turnId: 'turn-one', role: 'user', userKind: 'subagent-completion', time: '18:12',
        body: '', status: 'completed', sourceSubagentTaskId: 'task-one',
        sourceSubagentLabel: 'reader', sourceSubagentRelation: 'current',
      }, {
        id: 'assistant-tool-after-steer', turnId: 'turn-one', role: 'assistant', assistantKind: 'tool-request', forkEligible: false,
        time: '18:12', body: '', status: 'completed',
        reasoning: [{ id: 'reasoning-after-steer', kind: 'full', body: '继续检查真实页面。' }],
      }, {
        id: 'assistant-final', turnId: 'turn-one', role: 'assistant', assistantKind: 'terminal', rootFinal: true, forkEligible: true,
        time: '18:13', body: '检查已经完成。', status: 'completed',
        reasoning: [{ id: 'reasoning-after-tool', kind: 'summary', body: '整理检查结果。' }],
      }, {
        id: 'user-prompt-two', turnId: 'turn-two', role: 'user', userKind: 'prompt', time: '18:14',
        body: '继续下一项。', status: 'completed',
      }, {
        id: 'assistant-final-two', turnId: 'turn-two', role: 'assistant', assistantKind: 'terminal', rootFinal: true, forkEligible: true, time: '18:15',
        body: '第二项也已经完成。', status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByText('检查已经完成。')).toBeTruthy();

    expect(container.querySelectorAll('.user-heading')).toHaveLength(2);
    for (const userHeading of container.querySelectorAll('.user-heading')) {
      expect(userHeading.textContent).toContain('你');
      expect(userHeading.firstElementChild?.tagName).toBe('STRONG');
      expect(userHeading.lastElementChild?.classList.contains('person-avatar')).toBe(true);
      expect(userHeading.lastElementChild?.getAttribute('aria-label')).toBe('用户头像');
    }
    expect(container.querySelectorAll('.user-steer')).toHaveLength(1);
    expect(screen.getByText('引导')).toBeTruthy();
    expect(screen.queryByText('你 · 引导')).toBeNull();
    expect(screen.queryByLabelText('reader 的结果已加入本轮对话')).toBeNull();
    expect(container.querySelectorAll('.assistant-heading')).toHaveLength(2);
    expect(container.querySelectorAll('.assistant-heading--run-start')).toHaveLength(1);
    expect(container.querySelectorAll('.assistant-heading--response')).toHaveLength(1);
    expect(container.querySelectorAll('.conversation-run > .assistant-heading--run-start')).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: '展开处理过程' }));
    expect(await screen.findByLabelText('reader 的结果已加入本轮对话')).toBeTruthy();
    expect(screen.getByText('读取文件')).toBeTruthy();
    expect(screen.getByRole('button', { name: '展开思考摘要' })).toBeTruthy();

    const finalTurn = screen.getByText('检查已经完成。').closest('.assistant-turn--response');
    expect(finalTurn?.closest('.conversation-run__step')).toBeNull();
    expect(finalTurn?.querySelector('.assistant-copy')).toBeTruthy();
  });

  it('shows a readable builtin name and keeps the command detail expandable', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-terminal', role: 'assistant', time: '18:11', body: '', status: 'completed',
        traces: [{
          id: 'trace-terminal', kind: 'terminal', toolName: 'terminal', title: '运行命令',
          subtitle: '已完成', status: 'completed', command: 'printf "visible command"',
          resultText: 'visible command', meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
    expect(screen.queryByText('terminal')).toBeNull();
    expect(screen.getByText('运行命令')).toBeTruthy();
    expect(container.querySelector('.terminal-command')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /terminal/ }));

    expect(container.querySelector('.terminal-command')?.textContent).toContain('printf "visible command"');
  });

  it('shows the real edit diff in a bounded output region without a redundant diff-copy button', async () => {
    const adapter = new FakeAdapter();
    const diff = ['@@ -1,40 +1,40 @@', '-old-001', '+new-001', ...Array.from(
      { length: 80 }, (_, index) => ` context-${String(index).padStart(3, '0')}`,
    )].join('\n');
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-edit-diff', role: 'assistant', time: '18:11', body: '', status: 'completed',
        traces: [{
          id: 'trace-edit-diff', kind: 'edit', toolName: 'edit_file', title: '更新文件',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          resultSummary: '操作已完成。', resultText: JSON.stringify({ status: 'success', diff }),
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
    fireEvent.click(await screen.findByRole('button', { name: /展开工具详情：edit_file/ }));

    const diffOutput = screen.getByLabelText('文件差异');
    expect(diffOutput.textContent).toBe(diff);
    expect(diffOutput.classList.contains('tool-result-diff')).toBe(true);
    expect(screen.queryByText('操作已完成。')).toBeNull();
    expect(screen.queryByRole('button', { name: '复制工具差异' })).toBeNull();
    expect(screen.queryByRole('button', { name: '复制工具原始结果' })).toBeNull();
    expect(container.querySelector('.tool-result-raw')).toBeNull();
  });

  it('does not render diff-shaped MCP data as a builtin file difference', async () => {
    const adapter = new FakeAdapter();
    const resultText = JSON.stringify({
      status: 'success', path: 'remote.txt', bytes_written: 42,
      diff: '@@ external report, not edit_file @@',
    });
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-mcp-diff', role: 'assistant', time: '18:11', body: '', status: 'completed',
        traces: [{
          id: 'trace-mcp-diff', kind: 'mcp', toolName: 'mcp__external__report', title: '使用工具',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          resultText, resultSummary: '@@ external report, not edit_file @@',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
    fireEvent.click(await screen.findByRole('button', { name: /展开工具详情：mcp__external__report/ }));

    expect(screen.queryByLabelText('文件差异')).toBeNull();
    expect(screen.getByLabelText('工具原始结果').textContent).toContain(resultText);
  });

  it('collapses retained artifact content independently from the tool card', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-artifact', role: 'assistant', time: '18:03', body: '', status: 'completed',
        traces: [{
          id: 'trace-artifact', kind: 'mcp', toolName: 'mcp__firecrawl__firecrawl_search', title: '搜索内容',
          subtitle: '已完成', status: 'completed', resultEntryId: 'result-1', resultText: '{"content":[]}',
          artifact: { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'COMPLETE' },
          meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
    const expand = await screen.findByRole('button', { name: /展开工具详情/ });
    fireEvent.click(expand);

    const collapse = screen.getByRole('button', { name: /收起工具详情/ });
    expect(collapse.textContent).not.toContain('收起详情');
    const artifactTrigger = screen.getByRole('button', { name: '查看完整输出' });
    expect(artifactTrigger.classList.contains('tool-artifact__trigger')).toBe(true);
    fireEvent.click(artifactTrigger);

    const end = await screen.findByRole('status', { name: '完整输出读取状态' });
    expect(end.textContent).toContain('已到末页');
    expect(screen.getByLabelText('完整工具输出').classList.contains('is-open')).toBe(true);
    expect(screen.getByRole('button', { name: '复制当前工具输出页' }).getAttribute('title')).toBe('复制当前页');

    fireEvent.click(screen.getByRole('button', { name: '收起完整输出' }));
    expect(screen.queryByRole('button', { name: '复制当前工具输出页' })).toBeNull();
    expect(screen.getByRole('button', { name: '查看完整输出' })).toBeTruthy();
    expect(screen.getByLabelText('工具原始结果')).toBeTruthy();

    fireEvent.click(collapse);
    expect(screen.queryByRole('region', { name: '工具原始结果' })).toBeNull();
    expect(screen.queryByLabelText('工具原始结果')).toBeNull();
  });

  it('invalidates an in-flight artifact page when the whole tool card is collapsed', async () => {
    const adapter = new FakeAdapter();
    const page = deferred<Awaited<ReturnType<RuntimeConnection['readToolArtifact']>>>();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-artifact-race', role: 'assistant', time: '18:03', body: '', status: 'completed',
        traces: [{
          id: 'trace-artifact-race', kind: 'mcp', toolName: 'mcp__firecrawl__firecrawl_search',
          status: 'completed',
          title: '搜索内容', subtitle: '已完成', resultEntryId: 'result-race', resultText: '{}',
          artifact: { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'COMPLETE' },
          meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
    const expand = await screen.findByRole('button', { name: /展开工具详情/ });
    const active = adapter.lastConnection!;
    active.readToolArtifact.mockImplementationOnce(() => page.promise);
    fireEvent.click(expand);
    fireEvent.click(screen.getByRole('button', { name: '查看完整输出' }));
    await waitFor(() => expect(active.readToolArtifact).toHaveBeenCalledWith('result-race', 0));
    fireEvent.click(screen.getByRole('button', { name: /收起工具详情/ }));

    await act(async () => page.resolve({
      resultEntryId: 'result-race', text: '不应落回的迟到页', offsetChars: 0,
      returnedChars: 9, totalChars: 9, hasMore: false,
    }));
    fireEvent.click(screen.getByRole('button', { name: /展开工具详情/ }));

    expect(screen.queryByText('不应落回的迟到页')).toBeNull();
    expect(screen.getByRole('button', { name: '查看完整输出' })).toBeTruthy();
  });

  it('does not apply an old-session artifact page to a reused trace identity', async () => {
    const adapter = new FakeAdapter();
    const oldPage = deferred<Awaited<ReturnType<RuntimeConnection['readToolArtifact']>>>();
    const secondSession: SessionSummary = {
      ...initialSession,
      id: 'session:hrluejkx3v2ajzf4ec6bb3ierm',
      title: '另一个会话',
      live: false,
    };
    adapter.sessions = [initialSession, secondSession];
    const artifactProjection = (resultText: string): RuntimeProjection => ({
      ...projection(''),
      messages: [{
        id: 'assistant-shared', role: 'assistant', time: '18:03', body: '', status: 'completed',
        traces: [{
          id: 'trace-shared', kind: 'mcp', toolName: 'mcp__firecrawl__firecrawl_search',
          status: 'completed',
          title: '搜索内容', subtitle: '已完成', resultEntryId: 'result-shared', resultText,
          artifact: { disposition: 'AVAILABLE', sourceCoverage: 'COMPLETE', displayKind: 'COMPLETE' },
          meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    });
    adapter.connectionValues.set('session:bwpogagzox7onyn52iumyctnkq', artifactProjection('{"session":"A"}'));
    adapter.connectionValues.set('session:hrluejkx3v2ajzf4ec6bb3ierm', artifactProjection('{"session":"B"}'));

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
    const expand = await screen.findByRole('button', { name: /展开工具详情/ });
    const first = adapter.lastConnection!;
    first.readToolArtifact.mockImplementationOnce(() => oldPage.promise);
    fireEvent.click(expand);
    fireEvent.click(screen.getByRole('button', { name: '查看完整输出' }));
    await waitFor(() => expect(first.readToolArtifact).toHaveBeenCalledWith('result-shared', 0));

    fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
    await screen.findByRole('heading', { name: '另一个会话' });
    const processToggle = screen.queryByRole('button', { name: '展开处理过程' });
    if (processToggle) fireEvent.click(processToggle);
    await act(async () => oldPage.resolve({
      resultEntryId: 'result-shared', text: '会话 A 的迟到 artifact', offsetChars: 0,
      returnedChars: 17, totalChars: 17, hasMore: false,
    }));
    const summary = screen.getByRole('button', { name: /工具详情：mcp__firecrawl__firecrawl_search/ });
    if (summary.getAttribute('aria-expanded') !== 'true') fireEvent.click(summary);

    expect(screen.queryByText('会话 A 的迟到 artifact')).toBeNull();
    expect(screen.getByRole('button', { name: '查看完整输出' })).toBeTruthy();
  });

  it.each([undefined, 'invalid JSON', '{"error":"SOURCE_OBSERVATION_UNAVAILABLE"}'])(
    'does not report an unavailable capability page as empty: %s', async (resultText) => {
      const adapter = new FakeAdapter();
      adapter.connectionValue = {
        ...projection(''), messages: [{
          id: 'assistant-query', role: 'assistant', time: '18:11', body: '', status: 'completed',
          traces: [{ id: 'query', kind: 'mcp', toolName: 'list_capabilities', title: '查看能力',
            subtitle: '查看能力', status: resultText === undefined ? 'running' : 'failed',
            argumentsJson: '{}', resultText, meta: '' }],
        }], isRunning: false, activeTurnId: undefined,
      };
      renderWorkbench(<PulsaraApp adapter={adapter} />);
      fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
      expect(screen.queryByText('观察到 0 项能力')).toBeNull();
    },
  );

  it('explains MCP meta-tool routing and identifies exact Skill documents', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-capabilities', role: 'assistant', time: '18:11', body: '', status: 'completed',
        traces: [{
          id: 'trace-reload', kind: 'artifact', toolName: 'reload_capabilities', title: '刷新能力',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          argumentsJson: '{}', resultText: '{"status":"RELOADED"}',
        }, {
          id: 'trace-skill', kind: 'read', toolName: 'read_file', title: '读取文件',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          argumentsJson: JSON.stringify({ path: '/opt/pulsara/bundled_skills/pdf/SKILL.md' }),
          resultText: JSON.stringify({ path: '/opt/pulsara/bundled_skills/pdf/SKILL.md', total_lines: 42 }),
        }, {
          id: 'trace-list', kind: 'mcp', toolName: 'list_capabilities', title: '查看能力',
          subtitle: '已完成', status: 'completed', meta: '操作完成', argumentsJson: '{}',
          resultText: JSON.stringify({
            total_count: 2, completeness: 'COMPLETE',
            items: [{ kind: 'MCP_SERVER', name: 'firecrawl', status: '连接已就绪。' }, {
              kind: 'MCP_SERVER', name: 'local-docs', status: '正在连接。',
            }],
          }) + '\n\n[TOOL OUTPUT ARTIFACT: full retained output is available as artifact_id=artifact:test.]',
        }, {
          id: 'trace-inspect', kind: 'mcp', toolName: 'inspect_capability', title: '查看能力详情',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          argumentsJson: JSON.stringify({ target: { kind: 'MCP_TOOL', server_id: 'firecrawl', tool_name: 'mcp__firecrawl__firecrawl_search' } }),
          resultText: JSON.stringify({
            target: { kind: 'MCP_TOOL', server_id: 'firecrawl', tool_name: 'mcp__firecrawl__firecrawl_search' },
            invocation: { mode: 'META' }, effect_kind: 'READ_ONLY',
            description: '搜索公开网页。', tool_ref: 'mcpref_firecrawl_search',
            input_schema: { type: 'object', properties: { query: { type: 'string' } } },
          }),
        }, {
          id: 'trace-use', kind: 'mcp', toolName: 'use_new_mcp_tool', title: '调用 MCP 工具',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
          argumentsJson: JSON.stringify({
            tool_ref: 'mcpref_firecrawl_search', arguments: { query: 'Pulsara' },
          }),
          resultText: JSON.stringify({ content: '搜索完成' }),
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
    expect(screen.getByText('刷新扩展能力')).toBeTruthy();
    expect(screen.queryByText('reload_capabilities')).toBeNull();
    expect(await screen.findByText('使用 pdf 技能')).toBeTruthy();
    expect(screen.queryByText('reload_plugins')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /list_capabilities/ }));
    expect(screen.getByText('观察到 2 项能力')).toBeTruthy();
    const listDetails = container.querySelectorAll('.mcp-trace-details')[0] as HTMLElement;
    expect(within(listDetails).getByText('firecrawl')).toBeTruthy();
    expect(within(listDetails).getByText('local-docs')).toBeTruthy();
    expect(within(listDetails).getByText('连接已就绪。')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /inspect_capability/ }));
    const inspectDetails = container.querySelectorAll('.mcp-trace-details')[1] as HTMLElement;
    expect(within(inspectDetails).getByText('所选能力')).toBeTruthy();
    expect(within(inspectDetails).getByText('mcp__firecrawl__firecrawl_search')).toBeTruthy();
    expect(within(inspectDetails).queryByText('query')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /use_new_mcp_tool/ }));
    const useDetails = container.querySelectorAll('.mcp-trace-details')[2] as HTMLElement;
    expect(within(useDetails).getByText('调用的工具')).toBeTruthy();
    expect(within(useDetails).getByText('mcp__firecrawl__firecrawl_search')).toBeTruthy();
    expect(within(useDetails).queryByText(/"query": "Pulsara"/)).toBeNull();
  });

  it('offers copy only for the terminal assistant response', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-intermediate', turnId: 'turn-copy', role: 'assistant', assistantKind: 'tool-request',
        time: '18:11', body: '我先检查文件，然后继续处理。', status: 'completed',
        traces: [{
          id: 'trace-intermediate', kind: 'read', toolName: 'read_file', title: '读取文件',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
        }],
      }, {
        id: 'assistant-terminal-response', turnId: 'turn-copy', role: 'assistant', assistantKind: 'terminal',
        rootFinal: true, forkEligible: true, time: '18:12', body: '最终结果已经准备好。', status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
    const intermediate = (await screen.findByText('我先检查文件，然后继续处理。')).closest('.assistant-copy');
    const terminal = screen.getByText('最终结果已经准备好。').closest('.assistant-copy');

    expect(intermediate).toBeTruthy();
    expect(terminal).toBeTruthy();
    expect(within(intermediate as HTMLElement).queryByRole('button', { name: '复制回复' })).toBeNull();
    expect(within(terminal as HTMLElement).getByRole('button', { name: '复制回复' })).toBeTruthy();
    expect(within(intermediate as HTMLElement).queryByText('18:11')).toBeNull();
    expect(within(terminal as HTMLElement).getByText('18:12', { selector: 'time' })).toBeTruthy();
    expect(screen.getAllByRole('button', { name: '复制回复' })).toHaveLength(1);
  });

  it('copies the exact source Markdown instead of rendered or normalized text', async () => {
    const writeText = vi.fn(async () => undefined);
    stubClipboard(writeText);
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-source', role: 'assistant', assistantKind: 'terminal',
        rootFinal: true, forkEligible: true, time: '18:12', body: SOURCE_FIDELITY_MARKDOWN, status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '复制回复' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledExactlyOnceWith(SOURCE_FIDELITY_MARKDOWN));
    expect(await screen.findByText('已复制回复')).toBeTruthy();
  });

  it('copies one formula as LaTeX and reports through the shared toast stack', async () => {
    const writeText = vi.fn(async () => undefined);
    stubClipboard(writeText);
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-formula', role: 'assistant', assistantKind: 'terminal',
        rootFinal: true, forkEligible: true, time: '18:12', body: '能量关系为 $E=mc^2$。', status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '复制 LaTeX 公式' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledExactlyOnceWith('E=mc^2'));
    expect(await screen.findByText('公式已复制')).toBeTruthy();
  });

  it('renders accepted user prompts and steers with source line breaks and safe long-token wrapping', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'user-source', role: 'user', userKind: 'prompt',
        time: '18:10', body: SOURCE_FIDELITY_USER_TEXT, status: 'completed',
      }, {
        id: 'user-steer-source', role: 'user', userKind: 'steer',
        time: '18:11', body: SOURCE_FIDELITY_USER_TEXT, status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const bodies = [
      container.querySelector<HTMLElement>('.user-turn:not(.user-steer) .user-message > p'),
      container.querySelector<HTMLElement>('.user-steer .user-message > p'),
    ];
    expect(bodies[0]).not.toBe(bodies[1]);

    for (const body of bodies) {
      expect(body).toBeTruthy();
      expect(body?.textContent).toBe(SOURCE_FIDELITY_USER_TEXT);
      expect(body?.style.whiteSpace).toBe('pre-wrap');
      expect(body?.style.overflowWrap).toBe('anywhere');
      expect(body?.querySelector('script')).toBeNull();
    }
  });

  it('keeps the existing copy failure feedback when clipboard permission is denied', async () => {
    const writeText = vi.fn(async () => { throw new Error('denied'); });
    stubClipboard(writeText);
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-source', role: 'assistant', assistantKind: 'terminal',
        rootFinal: true, forkEligible: true, time: '18:12', body: SOURCE_FIDELITY_MARKDOWN, status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '复制回复' }));

    expect(await screen.findByText('无法复制回复')).toBeTruthy();
    expect(screen.getByText('系统剪贴板暂时不可用，请重试')).toBeTruthy();
  });

  it('dismisses a session menu outside the menu and with Escape while preserving its actions', async () => {
    const user = userEvent.setup();
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const trigger = await screen.findByLabelText(`${initialSession.title} 更多操作`);
    const menu = trigger.closest('details')!;
    await user.click(trigger);
    expect(menu.open).toBe(true);
    await user.click(menu.querySelector('.session-row__menu-actions')!);
    expect(menu.open).toBe(true);
    await user.click(screen.getByRole('heading', { name: '准备发布' }));
    expect(menu.open).toBe(false);
    await user.click(trigger);
    await user.keyboard('{Escape}');
    expect(menu.open).toBe(false);
    expect(document.activeElement).toBe(trigger);
    await user.click(trigger);
    await user.click(screen.getByRole('button', { name: '删除会话…' }));
    expect(menu.open).toBe(false);
    expect(await screen.findByRole('dialog')).toBeTruthy();
  });

  it('archives only eligible sessions, restores from settings without opening, and deletes there', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [{ ...initialSession, canArchive: false }];
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByLabelText(`${initialSession.title} 更多操作`));
    expect((screen.getByRole('button', { name: '归档会话' }) as HTMLButtonElement).disabled).toBe(true);
    adapter.sessions = [{ ...initialSession, canArchive: true }];
    fireEvent.click(screen.getByLabelText(`${initialSession.title} 更多操作`));
    fireEvent.click(screen.getByLabelText(`${initialSession.title} 更多操作`));
    await waitFor(() => expect((screen.getByRole('button', { name: '归档会话' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: '归档会话' }));
    await screen.findByText('会话已归档');
    expect(screen.getByText('可在设置中取消归档；绑定的定时任务已删除，需要重新创建。')).toBeTruthy();
    expect(adapter.archiveSession).toHaveBeenCalledExactlyOnceWith(initialSession.id);
    expect(adapter.createSession).not.toHaveBeenCalled();
    expect(adapter.lastConnection?.closed).toBe(true);
    expect(screen.queryByLabelText(`${initialSession.title} 更多操作`)).toBeNull();
    const connectionsBefore = adapter.connectCalls.length;
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '已归档会话' }));
    fireEvent.click(await screen.findByRole('button', { name: '取消归档' }));
    await screen.findByText('还没有已归档会话');
    expect(adapter.unarchiveSession).toHaveBeenCalledExactlyOnceWith(initialSession.id);
    expect(adapter.connectCalls).toHaveLength(connectionsBefore);
    adapter.archived = [{ ...initialSession, lifecycle: 'ARCHIVED' }];
    adapter.sessions = [];
    fireEvent.click(screen.getByRole('button', { name: '刷新已归档会话' }));
    fireEvent.click(await screen.findByRole('button', { name: '永久删除…' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '永久删除' }));
    await screen.findByText('会话已删除');
    expect(adapter.deleteSession).toHaveBeenCalledExactlyOnceWith(initialSession.id);
    await screen.findByText('还没有已归档会话');
  });

  it('permanently deletes the selected session without creating a replacement', async () => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const menu = await screen.findByLabelText(`${initialSession.title} 更多操作`);
    fireEvent.click(menu);
    fireEvent.click(screen.getByRole('button', { name: '删除会话…' }));
    const dialog = screen.getByRole('dialog', { name: '删除这条会话？' });
    expect(dialog.getAttribute('aria-describedby')).toBe('session-delete-description');
    expect(dialog.textContent).toContain('会话、记录及绑定的定时任务将永久删除，无法撤销');
    const preserved = within(dialog).getByRole('list', { name: '仍会保留的内容' });
    expect(within(preserved).getAllByRole('listitem').map(item => item.textContent)).toEqual(['已保存的记忆', '工作目录']);
    fireEvent.click(within(dialog).getByRole('button', { name: '取消' }));
    expect(adapter.deleteSession).not.toHaveBeenCalled();
    fireEvent.click(menu);
    fireEvent.click(screen.getByRole('button', { name: '删除会话…' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^(停止并删除|永久删除)$/ }));
    await screen.findByText('会话已删除');
    expect(adapter.deleteSession).toHaveBeenCalledExactlyOnceWith(initialSession.id);
    expect(adapter.createSession).not.toHaveBeenCalled();
    expect(screen.queryByLabelText(`${initialSession.title} 更多操作`)).toBeNull();
    expect(adapter.lastConnection?.closed).toBe(true);
  });

  it('retains a session after an uncertain deletion and offers explicit confirmation retry', async () => {
    const adapter = new FakeAdapter();
    let reject!: (error: Error) => void;
    adapter.deleteSession.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByLabelText(`${initialSession.title} 更多操作`));
    fireEvent.click(screen.getByRole('button', { name: '删除会话…' }));
    const button = within(screen.getByRole('dialog')).getByRole('button', { name: /^(停止并删除|永久删除)$/ });
    fireEvent.click(button); fireEvent.click(button);
    expect(adapter.deleteSession).toHaveBeenCalledTimes(1);
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect((within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }) as HTMLButtonElement).disabled).toBe(true);
    reject(new Error('暂时无法确认删除结果'));
    await screen.findByRole('alert');
    expect(screen.getByLabelText(`${initialSession.title} 更多操作`)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '重试确认' }));
    await screen.findByText('会话已删除');
    expect(adapter.deleteSession).toHaveBeenCalledTimes(2);
  });

  it('deletes another session without clearing the current conversation', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions.push({ ...initialSession, id: 'session:other', title: '待删除会话' });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByLabelText('待删除会话 更多操作'));
    fireEvent.click(screen.getAllByRole('button', { name: '删除会话…' }).at(-1)!);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /^(停止并删除|永久删除)$/ }));
    await screen.findByText('会话已删除');
    expect(adapter.deleteSession).toHaveBeenCalledExactlyOnceWith('session:other');
    expect(adapter.lastConnection?.closed).toBe(false);
    expect(adapter.connectCalls.at(-1)?.sessionId).toBe(initialSession.id);
  });

  it('forks only server-eligible entries with a server-issued identity and leaves the parent running', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(''), isRunning: true, messages: [
      { id: 'old-intermediate', role: 'assistant', assistantKind: 'terminal', body: '中间正文', time: '18:10', status: 'completed', forkEligible: false },
      { id: 'canonical-anchor', role: 'assistant', assistantKind: 'terminal', body: '已结算最终回复', time: '18:12', status: 'completed', rootFinal: true, forkEligible: true },
    ] };
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const child = 'session:server-generated-child';
    adapter.forkConversation.mockImplementationOnce(async () => {
      await gate;
      adapter.sessions = [{ ...initialSession, id: child }, ...adapter.sessions];
      return { outcome: 'CREATED_AND_OPENED', child_session_id: child };
    });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const button = await screen.findByRole('button', { name: '从此处分叉' });
    expect(screen.getAllByRole('button', { name: '从此处分叉' })).toHaveLength(1);
    fireEvent.click(button); fireEvent.click(button);
    expect(button.getAttribute('aria-busy')).toBe('true');
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(adapter.forkConversation).toHaveBeenCalledExactlyOnceWith(initialSession.id, 'canonical-anchor');
    release();
    await screen.findByText('分叉已打开');
    expect(adapter.connectCalls.at(-1)?.sessionId).toBe(child);
  });

  it('refreshes the list after a lost Fork response without replaying or claiming a known outcome', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(''), initialContextBase: { base_kind: 'SNAPSHOT', display_after_entry_sequence: 0 }, messages: [
      { id: 'imported-anchor', role: 'assistant', assistantKind: 'terminal', body: '继承的回复', time: '18:12', status: 'completed', rootFinal: true, forkEligible: true, entryOwnerKind: 'IMPORTED_HISTORY' },
    ] };
    adapter.forkConversation.mockImplementationOnce(async () => {
      const child = 'session:server-created-before-response-loss';
      adapter.sessions = [{ ...initialSession, id: child }, ...adapter.sessions];
      throw new Error('response lost');
    });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const button = await screen.findByRole('button', { name: '从此处分叉' });
    expect(screen.getByRole('separator', { name: '已保留分叉点的有效上下文，压缩前记录请在原会话查看' })).toBeTruthy();
    fireEvent.click(button);
    await screen.findByText('尚未确认分叉结果');
    expect(adapter.forkConversation).toHaveBeenCalledTimes(1);
    expect(adapter.readSession).not.toHaveBeenCalled();
    expect(adapter.connectCalls.at(-1)?.sessionId).toBe(initialSession.id);
  });

  it('starts a Fork with empty draft and planning off while retaining the source draft', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(''), isRunning: false, messages: [
      { id: 'anchor', role: 'assistant', assistantKind: 'terminal', body: '可分叉回复', time: '18:12', status: 'completed', rootFinal: true, forkEligible: true },
    ] };
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const textbox = await screen.findByRole('textbox', { name: '发送给 Pulsara' });
    await userEvent.click(textbox);
    await userEvent.type(textbox, '父会话未发送草稿', { skipClick: true });
    fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
    fireEvent.click(screen.getByRole('button', { name: '先规划' }));
    fireEvent.click(screen.getByRole('button', { name: '从此处分叉' }));
    await screen.findByText('分叉已打开');
    expect(screen.getByRole('textbox', { name: '发送给 Pulsara' }).textContent).toBe('');
    fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
    expect(screen.getByRole('button', { name: '先规划' }).getAttribute('aria-pressed')).toBe('false');
    const source = screen.getAllByRole('button').find(button => button.textContent?.includes(initialSession.title) && button.textContent?.includes('已载入'));
    expect(source).toBeTruthy();
    fireEvent.click(source!);
    await waitFor(() => expect(adapter.connectCalls.at(-1)?.sessionId).toBe(initialSession.id));
    expect(screen.getByRole('textbox', { name: '发送给 Pulsara' }).textContent)
      .toBe('父会话未发送草稿');
    fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
    expect(screen.getByRole('button', { name: '本轮先规划' }).getAttribute('aria-pressed')).toBe('true');
  });

  it.each(['CREATED_OPEN_DEFERRED', 'NOT_CREATED'] as const)('keeps the parent selected for %s', async (outcome) => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(''), messages: [
      { id: 'anchor', role: 'assistant', assistantKind: 'terminal', body: '保留父会话', time: '18:12', status: 'completed', rootFinal: true, forkEligible: true },
    ] };
    adapter.forkConversation.mockResolvedValueOnce({ outcome, child_session_id: 'child' });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '从此处分叉' }));
    await screen.findByText(outcome === 'NOT_CREATED' ? '未创建分叉' : '分叉已创建，暂未打开');
    expect(adapter.connectCalls.at(-1)?.sessionId).toBe(initialSession.id);
    expect(adapter.forkConversation).toHaveBeenCalledTimes(1);
  });

  it('joins consecutive tool rails only when no visible content separates them', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [{
        id: 'assistant-edit', role: 'assistant', assistantKind: 'tool-request', turnId: 'turn-rails', historyGroupId: 'group-rails', historyWindowId: 'page-rails',
        time: '18:11', body: '我先修正测试。', status: 'completed',
        traces: [{
          id: 'trace-edit', kind: 'edit', toolName: 'edit_file', title: '更新文件',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
        }],
      }, {
        id: 'assistant-terminal', role: 'assistant', assistantKind: 'tool-request', turnId: 'turn-rails', historyGroupId: 'group-rails', historyWindowId: 'page-rails',
        time: '18:12', body: '', status: 'completed',
        traces: [{
          id: 'trace-terminal', kind: 'terminal', toolName: 'terminal', title: '运行命令',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
        }],
      }, {
        id: 'assistant-reasoning-boundary', role: 'assistant', assistantKind: 'tool-request', turnId: 'turn-rails', historyGroupId: 'group-rails', historyWindowId: 'page-rails',
        time: '18:13', body: '', status: 'completed',
        reasoning: [{ id: 'reasoning-boundary', kind: 'full', body: '我需要先分析结果。' }],
        traces: [{
          id: 'trace-after-reasoning', kind: 'terminal', toolName: 'terminal', title: '运行命令',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
    await screen.findByText('我先修正测试。');
    const turns = container.querySelectorAll('.assistant-turn');

    expect(turns).toHaveLength(3);
    expect(turns[0].classList.contains('assistant-turn--tool-chain-after')).toBe(true);
    expect(turns[1].classList.contains('assistant-turn--tool-chain-before')).toBe(true);
    expect(turns[1].classList.contains('assistant-turn--tool-chain-after')).toBe(false);
    expect(turns[2].classList.contains('assistant-turn--tool-chain-before')).toBe(false);
  });

  it('renders an accepted previous-root subtask completion without implying provider use', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      canonicalRootTurnIds: ['turn-source', 'turn-target'],
      agentTasks: [{ id: 'task-internal', parentId: 'turn-source', label: 'reader', role: '研究',
        objective: '读取结果', dependencyIds: [], status: 'completed', color: 'blue', completionAccepted: true }],
      messages: [{
        id: 'accepted-result', turnId: 'turn-target', role: 'user', userKind: 'subagent-completion', time: '18:14',
        body: '{"status":"accepted","task_id":"internal"}',
        sourceSubagentTaskId: 'task-internal',
        sourceSubagentLabel: 'reader',
        sourceSubagentRelation: 'previous',
        status: 'completed',
      }, {
        id: 'assistant-after-result', turnId: 'turn-target', role: 'assistant', time: '18:15',
        body: '我会基于子任务结果继续。', status: 'completed',
      }],
      isRunning: false,
      activeTurnId: undefined,
    };

    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开处理过程' }));
    expect(await screen.findByLabelText('上一轮 reader 的结果已加入本轮对话')).toBeTruthy();
    expect(screen.getByText('上一轮 reader 的结果已加入本轮对话')).toBeTruthy();
    expect(screen.queryByRole('tooltip')).toBeNull();
    fireEvent.mouseEnter(screen.getByRole('article', { name: '上一轮 reader 的结果已加入本轮对话' }));
    expect(screen.getByRole('tooltip').textContent).toContain('已记录到当前对话');
    expect(screen.queryByText(/用于当前处理|会结合这项工作的结果/)).toBeNull();
    expect(screen.queryByText(/"status":"accepted"/)).toBeNull();
    expect(container.querySelectorAll('.user-heading')).toHaveLength(0);
    expect(container.querySelectorAll('.conversation-run > .assistant-heading')).toHaveLength(1);
  });

  it('defaults builtin raw results off and remembers the settings switch across reloads', async () => {
    try {
      let view = renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
      await screen.findByRole('heading', { name: '准备发布' });
      fireEvent.click(screen.getByRole('button', { name: '设置' }));
      fireEvent.click(screen.getByRole('button', { name: '通用' }));
      const toggle = screen.getByRole('switch', { name: '显示内置工具原始结果' });
      expect(toggle.getAttribute('aria-checked')).toBe('false');
      fireEvent.click(toggle);
      expect(toggle.getAttribute('aria-checked')).toBe('true');
      expect(uiPreferences.getItem('pulsara-show-builtin-tool-results')).toBe('true');
      view.unmount();

      view = renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
      await screen.findByRole('heading', { name: '准备发布' });
      fireEvent.click(screen.getByRole('button', { name: '设置' }));
      fireEvent.click(screen.getByRole('button', { name: '通用' }));
      const restored = screen.getByRole('switch', { name: '显示内置工具原始结果' });
      expect(restored.getAttribute('aria-checked')).toBe('true');
      fireEvent.click(restored);
      expect(uiPreferences.getItem('pulsara-show-builtin-tool-results')).toBe('false');
      view.unmount();
    } finally {
      cleanup();
      vi.unstubAllGlobals();
    }
  });

  it('navigates between the major product surfaces', async () => {
    renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '总览' }));
    expect(screen.getByRole('heading', { level: 1, name: /准备好继续/ })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    expect(screen.getByRole('heading', { name: '设置' })).toBeTruthy();
  });

  it('replaces the entire composer until an active session exists', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [];
    render(<PulsaraApp adapter={adapter} />);

    await screen.findByRole('heading', { level: 1, name: /准备好继续/ });
    fireEvent.click(screen.getByRole('button', { name: '会话' }));

    expect(await screen.findByText('创建或选择会话后开始')).toBeTruthy();
    expect(screen.getByText('当前没有活动会话')).toBeTruthy();
    expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
    expect(screen.queryByRole('button', { name: '选择模型' })).toBeNull();
    expect(screen.queryByRole('button', { name: '先规划' })).toBeNull();
    expect(screen.queryByRole('button', { name: /完全访问/ })).toBeNull();

    const inspector = screen.getByRole('complementary', { name: '会话标签工作区' });
    fireEvent.click(within(inspector).getByRole('button', { name: '后台进程' }));
    expect(within(inspector).getByText('创建或选择会话后查看内容')).toBeTruthy();
    expect(within(inspector).queryByText('后台终端不可用')).toBeNull();
    expect(adapter.connectCalls).toHaveLength(0);

    fireEvent.click(screen.getByRole('button', { name: '创建会话' }));
    expect(screen.getByRole('heading', { name: '新建会话' })).toBeTruthy();
  });

  it.each([false, true])('disables all creation entries after a bootstrap failure (retryable=%s)', async (retryable) => {
    const adapter = new FakeAdapter();
    vi.spyOn(adapter, 'bootstrap').mockRejectedValue(new RuntimeApiError('START_FAILED', '本地服务启动失败', retryable));
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByText('本地服务启动失败');
    expect((screen.getByRole('button', { name: /新建会话/ }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '创建会话' }) as HTMLButtonElement).disabled).toBe(true);
    for (const modifier of ['ctrlKey', 'metaKey']) {
      fireEvent.keyDown(window, { key: 'n', [modifier]: true });
      expect(screen.queryByRole('dialog', { name: '新建会话' })).toBeNull();
    }
    fireEvent.keyDown(window, { key: 'k', metaKey: true });
    expect(screen.getByRole('dialog', { name: '搜索会话' })).toBeTruthy();
    expect(adapter.searchSessions).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog', { name: '命令面板' })).toBeNull();
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.click(screen.getByRole('button', { name: '总览' }));
    expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '记忆' }));
    expect(screen.getByRole('heading', { name: retryable ? '本地服务连接已中断' : '本地服务连接失败' })).toBeTruthy();
    expect(adapter.createSession).not.toHaveBeenCalled();
  });

  it('keeps creation disabled until bootstrap and the initial session list are ready', async () => {
    const adapter = new FakeAdapter();
    const onStartupSettled = vi.fn();
    let finishBootstrap!: (value: RuntimeBootstrap) => void;
    let finishList!: (value: SessionSummary[]) => void;
    vi.spyOn(adapter, 'bootstrap').mockImplementation(() => new Promise(resolve => { finishBootstrap = resolve; }));
    vi.spyOn(adapter, 'listSessions').mockImplementation(() => new Promise(resolve => { finishList = resolve; }));
    render(<PulsaraApp adapter={adapter} onStartupSettled={onStartupSettled} />);
    expect(onStartupSettled).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => finishBootstrap(bootstrap));
    expect(onStartupSettled).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('region', { name: '会话工作台' })).toBeNull();
    await act(async () => finishList([]));
    expect(onStartupSettled).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('region', { name: '会话工作台' })).toBeNull();
    expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.keyDown(window, { key: 'n', metaKey: true });
    expect(screen.getByRole('dialog', { name: '新建会话' })).toBeTruthy();
  });

  it('releases the global startup cover while the saved session restores independently', async () => {
    const adapter = new FakeAdapter();
    const onStartupSettled = vi.fn();
    const pending = deferred<void>();
    const connect = adapter.connect.bind(adapter);
    vi.spyOn(adapter, 'connect').mockImplementation(async (...args) => {
      await pending.promise;
      return connect(...args);
    });
    render(<PulsaraApp adapter={adapter} onStartupSettled={onStartupSettled} />);
    await waitFor(() => expect(adapter.connect).toHaveBeenCalled());
    expect(onStartupSettled).toHaveBeenCalledTimes(1);
    expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(false);
    await act(async () => pending.resolve());
    expect(onStartupSettled).toHaveBeenCalledTimes(1);
    expect((screen.getByRole('button', { name: '开始新任务' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('releases the startup cover for database setup instead of waiting for readiness forever', async () => {
    const adapter = new FakeAdapter();
    const onStartupSettled = vi.fn();
    vi.spyOn(adapter, 'bootstrap').mockResolvedValue({ ...bootstrap, database_state: 'database_not_configured' });
    render(<PulsaraApp adapter={adapter} onStartupSettled={onStartupSettled} />);
    await waitFor(() => expect(onStartupSettled).toHaveBeenCalledTimes(1));
    expect(adapter.connectCalls).toHaveLength(0);
    expect(screen.getByRole('heading', { name: '正在准备内置数据服务' })).toBeTruthy();
    expect(screen.getByRole('button', { name: /前往通用设置/ })).toBeTruthy();
  });

  it('keeps global session creation available while a selected session reconnects', async () => {
    const adapter = new FakeAdapter();
    const connect = adapter.connect.bind(adapter);
    let disconnect!: (error: Error) => void;
    let recover!: () => void;
    vi.spyOn(adapter, 'connect')
      .mockImplementationOnce(async (...args) => {
        const connection = await connect(...args);
        vi.spyOn(connection, 'observe').mockImplementation(() => new Promise((_resolve, reject) => { disconnect = reject; }));
        return connection;
      })
      .mockImplementationOnce(async (...args) => {
        await new Promise<void>(resolve => { recover = resolve; });
        return connect(...args);
      });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    const dialog = screen.getByRole('dialog', { name: '新建会话' });
    await act(async () => disconnect(new Error('connection lost')));
    expect((within(dialog).getByRole('button', { name: /^创建会话/ }) as HTMLButtonElement).disabled).toBe(false);
    expect(adapter.createSession).not.toHaveBeenCalled();
    await waitFor(() => expect(recover).toBeTypeOf('function'));
    await act(async () => recover());
    expect((within(dialog).getByRole('button', { name: /^创建会话/ }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('blocks the workbench and guides setup without touching session data in zero configuration', async () => {
    class ZeroConfigAdapter extends FakeAdapter {
      override listSessions = vi.fn(async (): Promise<SessionSummary[]> => {
        throw new Error('session data plane must not be called');
      });

      override async bootstrap(): Promise<RuntimeBootstrap> {
        return {
          ...bootstrap,
          runtime: {
            ...bootstrap.runtime,
            database_state: 'database_not_configured',
          },
          database_state: 'database_not_configured',
          local_settings: {
            state: 'ready',
            postgres: null,
            memory_retrieval: { embedding: null, embedding_enabled: false, history_enabled: true, rerank: null, decision: null, ranking_mode: 'off' },
          },
          model_configurations: [],
        };
      }

      override async localSettings() {
        return {
          local_settings: {
            state: 'ready' as const,
            postgres: null,
            memory_retrieval: { embedding: null, embedding_enabled: false, history_enabled: true, rerank: null, decision: null, ranking_mode: 'off' },
          },
          model_configurations: [],
          database_state: 'database_not_configured' as const,
        };
      }
    }

    const adapter = new ZeroConfigAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByRole('heading', { name: '正在准备内置数据服务' })).toBeTruthy();
    expect(screen.getByLabelText('PostgreSQL 配置引导')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /新建会话/ })).toBeNull();
    expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
    expect(adapter.listSessions).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: 'n', metaKey: true });
    expect(screen.queryByRole('dialog', { name: '新建会话' })).toBeNull();
    fireEvent.keyDown(window, { key: 'k', metaKey: true });
    expect(screen.getByRole('dialog', { name: '搜索会话' })).toBeTruthy();
    expect(adapter.searchSessions).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: 'Escape' });

    fireEvent.click(screen.getByRole('button', { name: /前往通用设置/ }));
    expect(await screen.findByRole('region', { name: '本地服务' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '检查数据' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '本地服务' })).toBeNull();
  });

  it('replaces overview session content with PostgreSQL setup guidance while unavailable', async () => {
    class UnavailableDatabaseAdapter extends FakeAdapter {
      override listSessions = vi.fn(async (): Promise<SessionSummary[]> => {
        throw new Error('session data plane must not be called');
      });

      override async bootstrap(): Promise<RuntimeBootstrap> {
        return {
          ...bootstrap,
          runtime: { ...bootstrap.runtime, database_state: 'database_unavailable' },
          database_state: 'database_unavailable',
        };
      }
    }

    const adapter = new UnavailableDatabaseAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: 'PostgreSQL 当前无法连接' });
    fireEvent.click(screen.getByRole('button', { name: '总览' }));

    expect(await screen.findByRole('heading', { level: 1, name: /先准备好/ })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'PostgreSQL 当前无法连接' })).toBeTruthy();
    expect(screen.queryByText('保存连接')).toBeNull();
    expect(screen.getByText('Pulsara 暂时无法连接应用管理的数据服务，请查看启动诊断并重试。')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: '最近会话' })).toBeNull();
    expect(screen.queryByRole('button', { name: '开始新任务' })).toBeNull();
    expect(adapter.listSessions).not.toHaveBeenCalled();
  });

  it('adds a catalog-backed model configuration without retaining the typed API key', async () => {
    const adapter = new FakeAdapter();
    const add = vi.spyOn(adapter, 'addModelConfiguration');
    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));

    await screen.findByRole('option', { name: 'Local Test' });
    fireEvent.change(screen.getByLabelText('提供方'), { target: { value: 'test' } });
    await screen.findByRole('option', { name: /test-model · test-model/ });
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'test-model' } });
    expect(screen.getByText('文字、图片、音频、视频、PDF')).toBeTruthy();
    expect(screen.getByText('文字、future-output')).toBeTruthy();
    expect(screen.getByText(/模态与推理能力来自 models.dev/)).toBeTruthy();
    await screen.findByRole('option', { name: 'Responses · models.dev 建议' });
    fireEvent.change(screen.getByLabelText('API 协议'), { target: { value: 'openai_responses' } });
    fireEvent.change(screen.getByLabelText('Reasoning 请求形状'), {
      target: { value: 'provider_default' },
    });
    const secret = 'front-end-secret-sentinel';
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: secret } });
    expect(screen.getByText('确认连接')).toBeTruthy();
    expect(container.textContent).toContain('只支持与 OpenAI Chat Completions 或 Responses 兼容的接口');
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));

    await waitFor(() => expect(add).toHaveBeenCalledWith({
      source: 'models_dev',
      route_id: 'test',
      model_id: 'test-model',
      wire_api: 'openai_responses',
      reasoning_wire_profile: 'provider_default',
      api_key: secret,
      context_window_tokens: null,
    }));
    await waitFor(() => expect(screen.queryByLabelText('API key')).toBeNull());
    expect(container.textContent).not.toContain(secret);
  });

  it('deletes one model configuration without silently rebinding its sessions', async () => {
    const adapter = new FakeAdapter();
    const remove = vi.spyOn(adapter, 'deleteModelConfiguration');
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));

    const deleteButton = await screen.findByRole('button', {
      name: /删除模型配置 Local Test · test-model/,
    });
    fireEvent.click(deleteButton);
    expect(screen.queryByText('删除后，已有会话不会自动改用其他模型。')).toBeNull();
    expect(remove).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(
      'model-connection:00000000000000000000000000000000',
    ));
    expect(await screen.findByText('还没有模型配置')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '会话' }));
    await waitFor(() => expect(
      document.querySelector('.mode-chip.model-chip')?.textContent,
    ).toContain('模型配置已删除'));
    expect(screen.getByText(/原模型配置已删除，请重新选择/)).toBeTruthy();
    expect(adapter.sessions[0].modelCallBinding?.connection_id).toBe(
      'model-connection:00000000000000000000000000000000',
    );
  });

  it('adds and independently tests a user-declared OpenAI-compatible model configuration', async () => {
    const adapter = new FakeAdapter();
    const add = vi.spyOn(adapter, 'addModelConfiguration');
    const test = vi.spyOn(adapter, 'testModelConfiguration');
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));
    fireEvent.click(screen.getByRole('button', { name: '自定义服务' }));

    fireEvent.change(screen.getByLabelText('配置名称'), { target: { value: 'Local Gateway' } });
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'http://127.0.0.1:9000/v1' } });
    fireEvent.change(screen.getByLabelText('Model ID'), { target: { value: 'local-model' } });
    fireEvent.change(screen.getByLabelText('API 协议'), { target: { value: 'openai_chat_completions' } });
    fireEvent.change(screen.getByLabelText('Reasoning 请求形状'), { target: { value: 'effort' } });
    fireEvent.change(screen.getByLabelText('Effort 列表'), { target: { value: 'low, high, high' } });
    const secret = 'custom-front-end-secret';
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: secret } });

    const imageInput = screen.getByLabelText('支持图像输入') as HTMLInputElement;
    expect(imageInput.checked).toBe(false);
    fireEvent.click(imageInput);
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }));
    await waitFor(() => expect(test).toHaveBeenCalledWith(expect.objectContaining({
      source: 'user_declared',
      configuration_name: 'Local Gateway',
      input_modalities: ['text', 'image'],
      base_url: 'http://127.0.0.1:9000/v1',
      model_id: 'local-model',
      wire_api: 'openai_chat_completions',
      authentication: 'bearer_api_key',
      api_key: secret,
      context_tokens: 256000,
      max_output_tokens: 8192,
      tool_call: true,
      reasoning: { kind: 'effort', values: ['low', 'high'] },
    })));
    expect(add).not.toHaveBeenCalled();

    const save = screen.getByRole('button', { name: '保存配置' }) as HTMLButtonElement;
    await waitFor(() => expect(save.disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(add).toHaveBeenCalledWith(expect.objectContaining({
      source: 'user_declared',
      input_modalities: ['text', 'image'],
      api_key: secret,
    })));
  });

  it('shows only request profiles valid for the selected wire protocol', async () => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));
    fireEvent.click(screen.getByRole('button', { name: '自定义服务' }));

    const protocol = screen.getByLabelText('API 协议');
    const profile = screen.getByLabelText('Reasoning 请求形状') as unknown as HTMLSelectElement;
    fireEvent.change(protocol, { target: { value: 'openai_responses' } });
    expect([...profile.options].map((option) => option.value)).toEqual([
      'provider_default', 'effort', 'toggle',
    ]);

    fireEvent.change(protocol, { target: { value: 'openai_chat_completions' } });
    expect([...profile.options].map((option) => option.value)).toEqual([
      'provider_default', 'effort', 'toggle', 'enable_thinking',
      'thinking_type', 'thinking_effort', 'broad_compat',
    ]);
    fireEvent.change(profile, { target: { value: 'broad_compat' } });
    expect(screen.getByText(/严格服务端可能拒绝未知字段/)).toBeTruthy();
    expect(screen.getByLabelText('Effort 列表')).toBeTruthy();
  });

  it('allows a user-declared no-auth target without rendering an API key field', async () => {
    const adapter = new FakeAdapter();
    const add = vi.spyOn(adapter, 'addModelConfiguration');
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));
    fireEvent.click(screen.getByRole('button', { name: '自定义服务' }));
    fireEvent.change(screen.getByLabelText('配置名称'), { target: { value: 'Local No Auth' } });
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'http://127.0.0.1:11434/v1' } });
    fireEvent.change(screen.getByLabelText('Model ID'), { target: { value: 'local-model' } });
    fireEvent.change(screen.getByLabelText('API 协议'), { target: { value: 'openai_responses' } });
    fireEvent.change(screen.getByLabelText('认证方式'), { target: { value: 'none' } });

    expect(screen.queryByLabelText('API key')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
    await waitFor(() => expect(add).toHaveBeenCalledWith(expect.objectContaining({
      source: 'user_declared',
      input_modalities: ['text'],
      authentication: 'none',
      api_key: null,
      reasoning: { kind: 'provider_default' },
    })));
  });

  it('keeps saving available after an independent connection test fails', async () => {
    const adapter = new FakeAdapter();
    const test = vi.spyOn(adapter, 'testModelConfiguration').mockRejectedValue(new Error('endpoint rejected test'));
    const add = vi.spyOn(adapter, 'addModelConfiguration');
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));
    fireEvent.click(screen.getByRole('button', { name: '自定义服务' }));
    fireEvent.change(screen.getByLabelText('配置名称'), { target: { value: 'Save Anyway' } });
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'https://example.test/v1' } });
    fireEvent.change(screen.getByLabelText('Model ID'), { target: { value: 'future-model' } });
    fireEvent.change(screen.getByLabelText('API 协议'), { target: { value: 'openai_chat_completions' } });
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'draft-key' } });

    fireEvent.click(screen.getByRole('button', { name: '测试连接' }));
    await waitFor(() => expect(test).toHaveBeenCalledOnce());
    const save = screen.getByRole('button', { name: '保存配置' });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(add).toHaveBeenCalledWith(expect.objectContaining({
      source: 'user_declared',
      api_key: 'draft-key',
    })));
  });

  it('sorts provider names and model IDs alphabetically in their selectors', async () => {
    const adapter = new FakeAdapter();
    const originalCatalog = adapter.modelCatalog.bind(adapter);
    vi.spyOn(adapter, 'modelCatalog').mockImplementation(async () => {
      const catalog = await originalCatalog();
      const baseModel = catalog.routes[0].models[0];
      return {
        ...catalog,
        routes: [{
          route_id: 'zulu',
          display_name: 'Zulu Provider',
          models: [],
        }, {
          route_id: 'alpha',
          display_name: 'alpha Provider',
          models: [
            { ...baseModel, model_id: 'zeta-model', display_name: 'Zeta Model' },
            { ...baseModel, model_id: 'Alpha-model', display_name: 'Alpha Model' },
            { ...baseModel, model_id: 'middle-model', display_name: 'Middle Model' },
          ],
        }, {
          route_id: 'bravo',
          display_name: 'Bravo Provider',
          models: [],
        }],
      };
    });

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));

    const providerSelect = await screen.findByLabelText('提供方') as unknown as HTMLSelectElement;
    expect([...providerSelect.options].slice(1).map((option) => option.value)).toEqual([
      'alpha', 'bravo', 'zulu',
    ]);

    fireEvent.change(providerSelect, { target: { value: 'alpha' } });
    const modelSelect = screen.getByLabelText('模型') as unknown as HTMLSelectElement;
    expect([...modelSelect.options].slice(1).map((option) => option.value)).toEqual([
      'Alpha-model', 'middle-model', 'zeta-model',
    ]);
  });

  it('keeps catalog routes visible when their wire adapters are not executable', async () => {
    const adapter = new FakeAdapter();
    const originalCatalog = adapter.modelCatalog.bind(adapter);
    vi.spyOn(adapter, 'modelCatalog').mockImplementation(async () => {
      const catalog = await originalCatalog();
      return {
        ...catalog,
        routes: [...catalog.routes, {
          route_id: 'catalog-only',
          display_name: 'Catalog Only Provider',
          models: [{
            model_id: 'catalog-only-model',
            display_name: 'Catalog Only Model',
            wire_dialect: 'provider_native' as const,
            context_tokens: 256000,
            input_tokens: 256000,
            output_tokens: 8192,
            tool_call: null,
            input_modalities: null,
            output_modalities: null,
            wire_shape_hint: null,
            wire_apis: [
              {
                wire_api: 'openai_chat_completions' as const,
                executable: false,
                reason: 'route_wire_adapter_unavailable',
                endpoint: null,
              },
              {
                wire_api: 'openai_responses' as const,
                executable: false,
                reason: 'route_wire_adapter_unavailable',
                endpoint: null,
              },
            ],
          }],
        }],
      };
    });

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    fireEvent.click(screen.getByRole('button', { name: /添加配置/ }));

    await screen.findByRole('option', { name: 'Catalog Only Provider' });
    fireEvent.change(screen.getByLabelText('提供方'), { target: { value: 'catalog-only' } });
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: 'catalog-only-model' } });

    expect(screen.getByText(/没有声明 OpenAI-compatible 接口/)).toBeTruthy();
    expect((screen.getByLabelText('API 协议') as unknown as HTMLSelectElement).disabled).toBe(true);
    expect(screen.getByRole('option', { name: 'Chat Completions · 暂不支持' })).toBeTruthy();
    expect(screen.getByRole('option', { name: 'Responses · 暂不支持' })).toBeTruthy();
  });

  it('collapses the inspector when leaving desktop width without forcing it open again', async () => {
    const media = new EventTarget();
    let matches = true;
    const removeListener = vi.spyOn(media, 'removeEventListener');
    vi.stubGlobal('matchMedia', vi.fn(() => Object.assign(media, { matches })));
    const view = renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
    try {
      await screen.findByRole('heading', { name: '准备发布' });
      const inspector = screen.getByRole('complementary', { name: '会话标签工作区' });
      expect(inspector.classList.contains('is-open')).toBe(false);
      fireEvent.click(screen.getByRole('button', { name: '切换检查器' }));
      expect(inspector.classList.contains('is-open')).toBe(true);
      matches = false;
      act(() => media.dispatchEvent(Object.assign(new Event('change'), { matches })));
      expect(inspector.classList.contains('is-open')).toBe(false);
      fireEvent.click(screen.getByRole('button', { name: '切换检查器' }));
      expect(inspector.classList.contains('is-open')).toBe(true);
      fireEvent.click(screen.getByRole('button', { name: '收起工作区' }));
      expect(inspector.classList.contains('is-open')).toBe(false);
      matches = true;
      act(() => media.dispatchEvent(Object.assign(new Event('change'), { matches })));
      expect(inspector.classList.contains('is-open')).toBe(false);
    } finally {
      view.unmount();
      vi.unstubAllGlobals();
    }
    expect(removeListener).toHaveBeenCalledWith('change', expect.any(Function));
  });

  it('opens the models settings section from the picker gear and keeps the regular settings entry unchanged', async () => {
    renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: 'test-model' }));
    expect(screen.queryByRole('button', { name: '管理模型配置' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '模型设置' }));
    expect(await screen.findByRole('heading', { name: '模型配置' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '模型' }).classList.contains('is-active')).toBe(true);
    expect(screen.queryByRole('heading', { name: '外观' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '总览' }));
    fireEvent.click(screen.getByRole('button', { name: '设置' }));
    expect(await screen.findByRole('heading', { name: '外观' })).toBeTruthy();
  });

  it('changes reasoning directly from the toolbar and dismisses its menu without resetting it', async () => {
    const adapter = new FakeAdapter();
    const update = vi.spyOn(adapter, 'updateModelCallBinding');
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /推理 medium/ }));
    fireEvent.click(screen.getByRole('button', { name: 'high' }));
    const trigger = await screen.findByRole('button', { name: /推理 high/ });
    expect(update).toHaveBeenCalledTimes(1);
    fireEvent.click(trigger);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(trigger);
    expect(screen.getAllByRole('button', { name: /推理 high/ })).toHaveLength(1);
    fireEvent.pointerDown(screen.getByRole('heading', { name: '准备发布' }));
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    const label = document.querySelector('.model-chip__label');
    expect(label?.textContent).toBe('test-model');
    expect(label?.closest('button')?.title).toBe('Local Test · test-model · Responses');
    expect(update).toHaveBeenCalledTimes(1);
  });

  it('keeps an exact reasoning selection until the user changes it again', async () => {
    const adapter = new FakeAdapter();
    const update = vi.spyOn(adapter, 'updateModelCallBinding');
    const first = renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });

    fireEvent.click(screen.getByRole('button', { name: /推理 medium/ }));
    expect(screen.queryByText('Token 预算')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'high' }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(
      'session:bwpogagzox7onyn52iumyctnkq',
      {
        connection_id: 'model-connection:00000000000000000000000000000000',
        reasoning: { kind: 'effort', value: 'high' },
      },
    ));
    expect(await screen.findByRole('button', { name: /推理 high/ })).toBeTruthy();

    first.unmount();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    expect(screen.getByRole('button', { name: /推理 high/ })).toBeTruthy();
    expect(update).toHaveBeenCalledTimes(1);
  });

  it('shows a warning and adopts the kernel default when a reasoning choice is stale', async () => {
    const adapter = new FakeAdapter();
    adapter.updateModelCallBinding = vi.fn(async (sessionId, binding) => {
      const accepted = {
        ...binding,
        reasoning: { kind: 'effort' as const, value: 'medium' },
      };
      adapter.sessions = adapter.sessions.map((session) => session.id === sessionId
        ? { ...session, modelCallBinding: accepted }
        : session);
      return { modelCallBinding: accepted, reasoningPreferenceReset: true };
    });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });

    fireEvent.click(screen.getByRole('button', { name: /推理 medium/ }));
    fireEvent.click(screen.getByRole('button', { name: 'high' }));

    expect(await screen.findByText('推理选项已更新')).toBeTruthy();
    expect(screen.getByText(/原选择已不再适用于该模型/)).toBeTruthy();
    expect(screen.getByRole('button', { name: /推理 medium/ })).toBeTruthy();
  });

  it('uses shared session presence and shows useful local configuration facts', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [
      initialSession,
      {
        id: 'session:xtpjbedcqqrduabsmnw7cmuehi',
        title: '后台检查',
        subtitle: '8 条记录',
        status: 'waiting',
        updatedAt: '2 分钟前',
        live: true,
      },
      {
        id: 'session:rwuinpszanxoixj2vggg73xnqi',
        title: '历史会话',
        subtitle: '21 条记录',
        status: 'completed',
        updatedAt: '昨天',
        live: false,
      },
    ];
    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });

    fireEvent.click(screen.getByRole('button', { name: '总览' }));

    expect(screen.getByRole('heading', { name: /^最近会话/ })).toBeTruthy();
    expect(screen.getByText('共 3 个会话')).toBeTruthy();
    expect(screen.queryByText('最近工作')).toBeNull();
    const runtimeHealth = container.querySelector('.runtime-health');
    expect(runtimeHealth?.textContent).toBe('本地服务已连接');
    expect(runtimeHealth?.querySelector('small')).toBeNull();
    expect(screen.queryByText('正在发生')).toBeNull();
    expect(screen.queryByText('本地运行')).toBeNull();
    expect(container.querySelector('.active-mission-card')).toBeNull();
    expect(container.querySelector('.activity-card')).toBeNull();
    expect(container.querySelectorAll('.recent-table .session-presence--current')).toHaveLength(0);
    expect(container.querySelectorAll('.recent-table .session-presence--loaded')).toHaveLength(2);
    expect(container.querySelectorAll('.recent-table .session-presence--resumable')).toHaveLength(1);
    expect(Array.from(container.querySelectorAll('.recent-table .table-state'), (node) => node.textContent))
      .toEqual(['已载入', '已载入', '可恢复']);
    const systemCard = container.querySelector('.system-card');
    expect(systemCard?.textContent).toContain('模型配置1 组可用');
    expect(systemCard?.textContent).toContain('记忆检索Embedding已关闭');
    expect(systemCard?.textContent).toContain('结果重排Rerank已关闭');
    expect(systemCard?.querySelector('footer')).toBeNull();
  });

  it('reports configured retrieval services and the exact model configuration count', async () => {
    const adapter = new FakeAdapter();
    adapter.modelConfigurations = [
      ...adapter.modelConfigurations,
      {
        ...adapter.modelConfigurations[0],
        id: 'model-connection:11111111111111111111111111111111',
        route_id: 'backup',
        model_id: 'backup-model',
        display_name: 'backup-model',
      },
    ];
    adapter.bootstrap = vi.fn(async () => ({
      ...bootstrap,
      local_settings: {
        ...bootstrap.local_settings,
        memory_retrieval: { embedding: { endpoint: 'https://provider.example/embeddings', model_id: 'embedding-fixture', shape: 'openai_embedding', authentication: 'none', credential_configured: false }, embedding_enabled: true, history_enabled: true, rerank: { endpoint: 'https://provider.example/rerank', model_id: 'rerank-fixture', shape: 'flat_rerank', authentication: 'none', credential_configured: false }, decision: null, ranking_mode: 'rerank' },
      },
      model_configurations: adapter.modelConfigurations,
    }));

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '总览' }));

    const systemCard = screen.getByRole('heading', { name: '当前配置' }).closest('.system-card');
    expect(systemCard?.textContent).toContain('模型配置2 组可用');
    expect(systemCard?.textContent).toContain('记忆检索embedding-fixture已开启');
    expect(systemCard?.textContent).toContain('结果重排Rerank已开启');
  });

  it('refreshes loaded session facts and distinguishes the current page connection', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [
      initialSession,
      {
        id: 'session:rwuinpszanxoixj2vggg73xnqi',
        title: '继续检查',
        subtitle: '13 条记录',
        status: 'completed',
        updatedAt: '5 分钟前',
        live: false,
      },
    ];
    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);

    await screen.findByRole('heading', { name: '准备发布' });
    expect(container.querySelector('.session-item.is-active')?.textContent).toBe('准备发布当前会话');
    expect(screen.getByText('可恢复')).toBeTruthy();
    expect(container.querySelectorAll('.session-presence--current')).toHaveLength(1);
    expect(container.querySelectorAll('.session-presence--loaded')).toHaveLength(0);
    expect(container.querySelectorAll('.session-presence--resumable')).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: /继续检查/ }));
    await screen.findByRole('heading', { name: '继续检查' });
    expect(container.querySelector('.session-item.is-active')?.textContent).toBe('继续检查当前会话');
    expect(screen.getByText('已载入')).toBeTruthy();
    expect(container.querySelectorAll('.session-presence--current')).toHaveLength(1);
    expect(container.querySelectorAll('.session-presence--loaded')).toHaveLength(1);
    expect(container.querySelectorAll('.session-presence--resumable')).toHaveLength(0);
    await waitFor(() => expect(adapter.listSessions).toHaveBeenCalledTimes(3));
  });

  it('groups sessions into collapsible project directories and a final quick-start section', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [{
      ...initialSession,
      workspace: { id: 'project-alpha', name: 'alpha', path: '/tmp/alpha', kind: 'project' },
    }, {
      id: 'session:gbfo5cwwh3xmufskza3dqhrvry', title: '继续 alpha', subtitle: '7 条记录', status: 'completed',
      updatedAt: '3 分钟前', live: false,
      workspace: { id: 'project-alpha', name: 'alpha', path: '/tmp/alpha', kind: 'project' },
    }, {
      id: 'session:2lgjrvv6npbfqodkifluiyaro4', title: '检查 beta', subtitle: '4 条记录', status: 'completed',
      updatedAt: '10 分钟前', live: false,
      workspace: { id: 'project-beta', name: 'beta', path: '/tmp/beta', kind: 'project' },
    }, {
      id: 'session:5hpvqyxq6idcedgszwpg2xfgvi', title: '快速排查', subtitle: '2 条记录', status: 'completed',
      updatedAt: '昨天', live: false,
      workspace: { id: 'quick-one', name: '快速开始', path: '/tmp/pulsara-quick', kind: 'quick' },
    }];

    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const directoryTree = screen.getByRole('region', { name: '会话目录' });
    expect(container.querySelector('.workspace-heading')).toBeNull();
    expect(within(directoryTree).getByRole('button', { name: '从目录中打开' }).getAttribute('aria-expanded')).toBe('true');
    expect(within(directoryTree).getByRole('button', { name: '快速开始' }).getAttribute('aria-expanded')).toBe('true');
    const alphaGroup = within(directoryTree).getByRole('region', { name: 'alpha 会话' });
    expect(within(alphaGroup).getByText('准备发布')).toBeTruthy();
    expect(within(alphaGroup).getByText('继续 alpha')).toBeTruthy();
    expect(within(directoryTree).getByText('快速排查')).toBeTruthy();
    expect(within(directoryTree).queryByText('3 分钟前')).toBeNull();
    expect(within(directoryTree).queryByText('10 分钟前')).toBeNull();
    expect(within(directoryTree).queryByText('昨天')).toBeNull();
    expect(directoryTree.querySelector('.session-section:last-child')?.classList.contains('session-section--quick')).toBe(true);

    fireEvent.click(within(alphaGroup).getByRole('button', { name: 'alpha' }));
    expect(within(alphaGroup).queryByText('准备发布')).toBeNull();
    expect(within(alphaGroup).queryByText('继续 alpha')).toBeNull();

    fireEvent.click(within(directoryTree).getByRole('button', { name: '从目录中打开' }));
    fireEvent.click(within(directoryTree).getByRole('button', { name: '快速开始' }));
    expect(within(directoryTree).queryByRole('region', { name: 'alpha 会话' })).toBeNull();
    expect(within(directoryTree).queryByText('快速排查')).toBeNull();
    expect(within(directoryTree).getByRole('button', { name: '从目录中打开' })).toBeTruthy();
    expect(within(directoryTree).getByRole('button', { name: '快速开始' })).toBeTruthy();
  });

  it('creates a scheduled quick session with its selected model while retaining the current chat connection', async () => {
    const adapter = new FakeAdapter();
    vi.spyOn(LocalScheduledTasksApi.prototype, 'listSessions').mockResolvedValue({sessions:[],next_cursor:null});
  const list = vi.spyOn(LocalScheduledTasksApi.prototype, 'list').mockResolvedValue({tasks:[], next_cursor:null});
    const preview = vi.spyOn(LocalScheduledTasksApi.prototype, 'preview').mockResolvedValue({next_run_at:'2026-10-06T00:00:00Z', local_time_fold:null});
    const create = vi.spyOn(LocalScheduledTasksApi.prototype, 'create').mockImplementation(async (session_id, values) => ({...values, session_id, id:'scheduled:test', revision:1, status:'PAUSED', next_run_at:null}));
    try {
      renderWorkbench(<PulsaraApp adapter={adapter} />);
      await screen.findByRole('heading', {name:'准备发布'});
      const connection = adapter.lastConnection;
      const connectCount = adapter.connectCalls.length;
      fireEvent.click(screen.getByRole('button', {name:'定时任务'}));
      fireEvent.click(await screen.findByRole('button', {name:'新建任务'}));
      fireEvent.change(screen.getByRole('combobox', {name:'供应商 / 模型'}), {target:{value:bootstrap.model_configurations[0].id}});
      fireEvent.change(screen.getByRole('combobox', {name:'推理强度'}), {target:{value:JSON.stringify({kind:'effort', value:'high'})}});
      fireEvent.change(screen.getByRole('textbox', {name:'名称'}), {target:{value:'每日检查'}});
      fireEvent.change(screen.getByRole('textbox', {name:'提示词'}), {target:{value:'检查工作区并报告'}});
      fireEvent.click(screen.getByRole('button', {name:'保存任务'}));
      await waitFor(() => expect(create).toHaveBeenCalledOnce());
      expect(adapter.createSession).toHaveBeenCalledExactlyOnceWith({kind:'quick'}, {connection_id:bootstrap.model_configurations[0].id, reasoning:{kind:'effort', value:'high'}});
      expect(create).toHaveBeenCalledWith('session:hrluejkx3v2ajzf4ec6bb3ierm', expect.objectContaining({timezone:'Asia/Shanghai', permission_mode:'bypass-permissions'}));
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(screen.getByRole('heading', {name:'定时任务'})).toBeTruthy();
      expect(adapter.connectCalls).toHaveLength(connectCount);
      expect(adapter.lastConnection).toBe(connection);
    } finally {list.mockRestore(); preview.mockRestore(); create.mockRestore();}
  });

  it('creates a quick-start session before asking for the first task', async () => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    expect(screen.getByRole('heading', { name: '新建会话' })).toBeTruthy();
    expect(screen.getByRole('radio', { name: /快速开始/ })).toBeTruthy();
    expect(screen.getByRole('radio', { name: /指定目录/ })).toBeTruthy();
    expect(screen.queryByText('想完成什么？')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
    await waitFor(() => expect(adapter.createSession).toHaveBeenCalledWith({ kind: 'quick' }));
    expect(await screen.findByRole('heading', { name: isWelcomeHeading })).toBeTruthy();
  });

  it('selects a directory from the sidebar and adds to its existing collapsed group', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [{ ...initialSession, workspace: {
      id: 'project-workspace', name: 'project', path: '/tmp/project', kind: 'project',
    } }];
    const picked = deferred<string | null>();
    adapter.pickWorkspaceDirectory.mockImplementationOnce(() => picked.promise);
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const tree = screen.getByRole('region', { name: '会话目录' });
    fireEvent.click(within(tree).getByRole('button', { name: 'project' }));
    fireEvent.click(within(tree).getByRole('button', { name: '从目录中打开' }));
    const add = within(tree).getByRole('button', { name: '选择目录并创建会话' });
    const connectionsBeforePicker = adapter.connectCalls.length;
    fireEvent.click(add);
    fireEvent.click(add);
    expect(adapter.pickWorkspaceDirectory).toHaveBeenCalledTimes(1);
    expect(adapter.pickWorkspaceDirectory).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(add.getAttribute('aria-busy')).toBe('false');
    expect(add.getAttribute('title')).toBe('等待选择目录');
    expect(adapter.connectCalls).toHaveLength(connectionsBeforePicker);
    expect(screen.getByRole('heading', { name: '准备发布' })).toBeTruthy();
    expect((within(tree).getByRole('button', { name: '创建快速开始会话' }) as HTMLButtonElement).disabled).toBe(true);
    expect(adapter.createSession).not.toHaveBeenCalled();
    await act(async () => picked.resolve('/tmp/project'));
    await waitFor(() => expect(adapter.createSession).toHaveBeenCalledExactlyOnceWith({ kind: 'project', path: '/tmp/project' }));
    const group = within(tree).getByRole('region', { name: 'project 会话' });
    expect(within(tree).getAllByRole('region', { name: 'project 会话' })).toHaveLength(1);
    expect(within(group).getByText('准备发布')).toBeTruthy();
    expect(within(group).getByText('新会话')).toBeTruthy();
    expect(within(group).getByRole('button', { name: 'project' }).getAttribute('aria-expanded')).toBe('true');
    expect(screen.queryByRole('dialog', { name: '新建会话' })).toBeNull();
    expect(adapter.connectCalls.at(-1)?.sessionId).toBe('session:hrluejkx3v2ajzf4ec6bb3ierm');
  });

  it.each(['project', 'quick'] as const)('creates a %s session directly from its sidebar plus', async kind => {
    const adapter = new FakeAdapter();
    adapter.sessions = [{ ...initialSession, workspace: {
      id: 'project-workspace', name: 'project', path: '/tmp/ project ', kind: 'project',
    } }];
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const tree = screen.getByRole('region', { name: '会话目录' });
    const toggleName = kind === 'quick' ? '快速开始' : 'project';
    fireEvent.click(within(tree).getByRole('button', { name: toggleName }));
    fireEvent.click(within(tree).getByRole('button', {
      name: kind === 'quick' ? '创建快速开始会话' : '在 project 中创建会话',
    }));
    await waitFor(() => expect(adapter.createSession).toHaveBeenCalledExactlyOnceWith(
      kind === 'quick' ? { kind: 'quick' } : { kind: 'project', path: '/tmp/ project ' },
    ));
    expect(adapter.pickWorkspaceDirectory).not.toHaveBeenCalled();
    expect(within(tree).getByRole('button', { name: toggleName }).getAttribute('aria-expanded')).toBe('true');
    expect(within(tree).getByText('新会话')).toBeTruthy();
    expect(screen.queryByRole('dialog', { name: '新建会话' })).toBeNull();
    expect(await screen.findByRole('heading', { name: isWelcomeHeading })).toBeTruthy();
  });

  it.each(['cancel', 'error'] as const)('keeps the current session when sidebar directory selection ends with %s', async outcome => {
    const adapter = new FakeAdapter();
    if (outcome === 'cancel') adapter.pickWorkspaceDirectory.mockResolvedValueOnce(null);
    else adapter.pickWorkspaceDirectory.mockRejectedValueOnce(new Error('无法打开系统目录窗口'));
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const add = screen.getByRole('button', { name: '选择目录并创建会话' }) as HTMLButtonElement;
    fireEvent.click(add);
    await waitFor(() => expect(add.disabled).toBe(false));
    expect(adapter.createSession).not.toHaveBeenCalled();
    expect(screen.getByRole('heading', { name: '准备发布' })).toBeTruthy();
    if (outcome === 'error') expect(screen.getByText('无法打开系统目录窗口')).toBeTruthy();
  });

  it('discards a late sidebar picker result after leaving the workbench', async () => {
    const adapter = new FakeAdapter();
    const picked = deferred<string | null>();
    adapter.pickWorkspaceDirectory.mockImplementationOnce(() => picked.promise);
    const view = renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '选择目录并创建会话' }));
    const signal = (adapter.pickWorkspaceDirectory.mock.calls[0] as unknown as [AbortSignal])[0];
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => picked.resolve('/tmp/project'));
    expect(adapter.createSession).not.toHaveBeenCalled();
  });

  it('blocks duplicate sidebar creates and restores the buttons after failure', async () => {
    const adapter = new FakeAdapter();
    const creation = deferred<SessionSummary>();
    adapter.createSession.mockImplementationOnce(() => creation.promise);
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const add = screen.getByRole('button', { name: '创建快速开始会话' }) as HTMLButtonElement;
    fireEvent.click(add);
    fireEvent.click(add);
    expect(adapter.createSession).toHaveBeenCalledTimes(1);
    expect(add.disabled).toBe(true);
    await act(async () => creation.reject(new Error('目录不可用')));
    await waitFor(() => expect(add.disabled).toBe(false));
    expect(screen.getByText('无法创建会话')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '准备发布' })).toBeTruthy();
  });

  it('keeps a pending global directory selection when only the selected session disconnects', async () => {
    const adapter = new FakeAdapter();
    const picked = deferred<string | null>();
    const recovery = deferred<void>();
    const connect = adapter.connect.bind(adapter);
    let disconnect!: (error: Error) => void;
    adapter.pickWorkspaceDirectory.mockImplementationOnce(() => picked.promise);
    vi.spyOn(adapter, 'connect')
      .mockImplementationOnce(async (...args) => {
        const connection = await connect(...args);
        vi.spyOn(connection, 'observe').mockImplementation(() => new Promise((_resolve, reject) => { disconnect = reject; }));
        return connection;
      })
      .mockImplementationOnce(async (...args) => { await recovery.promise; return connect(...args); });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '选择目录并创建会话' }));
    const signal = (adapter.pickWorkspaceDirectory.mock.calls[0] as unknown as [AbortSignal])[0];
    await act(async () => disconnect(new Error('connection lost')));
    expect(signal.aborted).toBe(false);
    expect(adapter.createSession).not.toHaveBeenCalled();
    await act(async () => picked.resolve('/tmp/project'));
    await waitFor(() => expect(adapter.createSession).toHaveBeenCalledWith({kind: 'project', path: '/tmp/project'}));
    await act(async () => recovery.resolve());
  });

  it('binds plan and permission choices to the next composer submission', async () => {
    const adapter = new FakeAdapter();
    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
    await screen.findByRole('heading', { name: isWelcomeHeading });

    fireEvent.click(screen.getByRole('button', { name: /选择模型/ }));
    fireEvent.click(screen.getByRole('button', { name: /Local Test · test-model/ }));
    await waitFor(() => expect(adapter.sessions[0].modelCallBinding?.connection_id).toBe(
      'model-connection:00000000000000000000000000000000',
    ));

    fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
    fireEvent.click(screen.getByRole('button', { name: '先规划' }));
    const permissionTrigger = screen.getByRole('button', { name: /完全访问/ });
    expect(permissionTrigger.classList.contains('is-danger')).toBe(true);
    fireEvent.click(permissionTrigger);
    expect(
      [...container.querySelectorAll('.permission-menu .permission-option-title')]
        .map((element) => element.textContent),
    ).toEqual(['只读', '每次询问', '接受编辑', '完全访问']);
    expect(
      container.querySelector('.permission-option--danger .permission-warning-icon'),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /只读/ }));
    await typeComposer('验证新的前端任务');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('验证新的前端任务')).toBeTruthy();
    expect(adapter.lastConnection?.enterPlan).toHaveBeenCalledWith('验证新的前端任务', 'read-only');
    expect(adapter.lastConnection?.submitPrompt).toHaveBeenCalledWith(
      expect.stringMatching(/^command:[a-z2-7]{25}[aeimquy4]$/), textPrompt('验证新的前端任务'), 'read-only',
    );
    expect(screen.getByRole('button', { name: /完全访问/ })).toBeTruthy();
  });

  it('leaves Enter to the input method while the composer is composing text', async () => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const composer = screen.getByLabelText('发送给 Pulsara');

    await userEvent.click(composer);
    fireEvent.compositionStart(composer);
    await userEvent.type(composer, 'biruzhey', { skipClick: true });
    expect(fireEvent.keyDown(
      composer,
      { key: 'Enter', code: 'Enter', isComposing: true },
    )).toBe(true);

    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
    expect(composer.textContent).toBe('biruzhey');

    fireEvent.compositionEnd(composer);
    expect(fireEvent.keyDown(
      composer,
      { key: 'Enter', code: 'Enter', keyCode: 229 },
    )).toBe(true);
    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
    expect(composer.textContent).toBe('biruzhey');

    expect(fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' })).toBe(false);
    await waitFor(() => expect(adapter.lastConnection?.submitPrompt).toHaveBeenCalledWith(
      expect.stringMatching(/^command:[a-z2-7]{25}[aeimquy4]$/), textPrompt('biruzhey'),
      'bypass-permissions',
    ));
  });

  it('creates a session for an explicitly selected directory', async () => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: /新建会话/ }));
    fireEvent.click(screen.getByRole('radio', { name: /指定目录/ }));
    const pathPreview = screen.getByRole('textbox', { name: '目录路径' }) as HTMLInputElement;
    expect(pathPreview.readOnly).toBe(true);
    expect(pathPreview.value).toBe('');
    expect((screen.getByRole('button', { name: /^创建会话/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
    await waitFor(() => expect(pathPreview.value).toBe('/tmp/project'));
    expect(adapter.pickWorkspaceDirectory).toHaveBeenCalledWith(expect.any(AbortSignal));
    fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
    await waitFor(() => expect(adapter.createSession).toHaveBeenCalledWith({ kind: 'project', path: '/tmp/project' }));
  });

  it('renders a plan draft as Markdown and resolves it through the real interaction surface', async () => {
    const adapter = new FakeAdapter();
    adapter.interactionContent = { kind: 'plan-draft', body: SOURCE_FIDELITY_MARKDOWN };
    adapter.connectionValue = {
      ...projection(''),
      messages: [],
      isRunning: false,
      activeTurnId: undefined,
      interaction: {
        id: 'plan-review-1',
        kind: 'plan-draft',
        workflowId: 'plan-1',
        workflowRevision: 2,
      },
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByText(/保留/)).toBeTruthy();
    expect(screen.getAllByText('read_file').length).toBeGreaterThan(0);
    expect(screen.getByLabelText('python 代码块').textContent).toContain('ROOT = "read-only"');
    expect(screen.queryByLabelText('TODO清单')).toBeNull();
    expect(screen.getByRole('button', { name: '展开TODO清单' }).hasAttribute('disabled')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: /批准并继续/ }));

    await waitFor(() => expect(adapter.lastConnection?.resolveInteraction).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'plan-review-1', kind: 'plan-draft' }),
      { kind: 'plan-draft', decision: 'approve' },
    ));
    expect(await screen.findByText('已创建按批准方案继续处理的任务。')).toBeTruthy();
  });

  it.each([
    ['cancel', '取消规划', { kind: 'plan-draft', decision: 'cancel' }],
    ['revise', '提交修改意见', {
      kind: 'plan-draft', decision: 'revise', revisionPrompt: {parts: [{type: 'text', text: '保留 `read_file` 与 ROOT'}]},
    }],
  ] as const)('keeps source plan text and interaction identity when choosing %s', async (
    _name,
    action,
    resolution,
  ) => {
    const adapter = new FakeAdapter();
    adapter.interactionContent = { kind: 'plan-draft', body: SOURCE_FIDELITY_MARKDOWN };
    const interaction = {
      id: 'plan-review-source', kind: 'plan-draft' as const,
      workflowId: 'plan-source', workflowRevision: 11,
    };
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined, interaction,
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByText(/ROOT = "read-only"/)).toBeTruthy();
    if (_name === 'revise') {
      fireEvent.click(screen.getByRole('button', { name: '提出修改' }));
      fireEvent.change(screen.getByPlaceholderText('说说你想调整的地方…'), {
        target: { value: resolution.revisionPrompt.parts[0].text },
      });
    }
    fireEvent.click(screen.getByRole('button', { name: action }));

    await waitFor(() => expect(adapter.lastConnection?.resolveInteraction).toHaveBeenCalledWith(
      interaction,
      resolution,
    ));
    expect(await screen.findByText(_name === 'revise'
      ? '已创建继续修订方案的任务。'
      : '未因本次取消启动这份方案的实施。你可以发送新任务。')).toBeTruthy();
  });

  it('shows exact duplicate queued bodies in queue order to an observer', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionRole = 'observer';
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: true, queuedCount: 2,
      queuedPrompts: [{
        queueItemId: 'queue-1', commandId: 'command-1', sequence: 1, status: 'pending',
        deliveryMode: 'steer', targetTurnId: 'turn-1', content: textPrompt('相同\n正文'), permission: 'read-only',
      }, {
        queueItemId: 'queue-2', commandId: 'command-2', sequence: 2, status: 'pending',
        deliveryMode: 'new-turn', content: textPrompt('相同\n正文'),
      }],
    };
    const { container } = renderWorkbench(<PulsaraApp adapter={adapter} />);

    const queue = await screen.findByRole('region', { name: '等待处理的输入' });
    expect([...queue.querySelectorAll('.prompt-content-body')].map((item) => item.textContent))
      .toEqual(['相同\n正文']);
    const steer = screen.getByRole('article', { name: '引导' });
    expect(within(steer).getByText('相同 正文')).toBeTruthy();
    expect(steer.closest('[data-queue-item-id]')?.getAttribute('data-queue-item-id')).toBe('queue-1');
    expect([...queue.querySelectorAll('article')].map((item) => item.dataset.queueItemId))
      .toEqual(['queue-2']);
    expect(queue.closest('.composer-wrap')).toBeTruthy();
    for (const name of ['发送', '编辑', '删除']) {
      expect(within(queue).queryByRole('button', { name })).toBeNull();
    }
    expect(within(queue).getByRole('button', { name: '展开' })).toBeTruthy();
    expect(container.querySelectorAll('.user-turn:not(.user-steer)')).toHaveLength(0);
    expect(container.querySelectorAll('.user-turn.user-steer')).toHaveLength(1);
  });

  it('keeps an observed queue item visible when it reaches a terminal state', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionRole = 'observer';
    adapter.queryCommandResult = {
      commandId: 'command-observed', status: 'rejected', publicCode: 'USER_CANCELLED',
      publicMessage: 'Observed queue item was cancelled.',
      promptDelivery: {
        queueItemId: 'queue-observed', queueStatus: 'CANCELLED', deliveryMode: 'steer',
      },
    };
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: true, eventSequence: 1,
      queuedCount: 1,
      queuedPrompts: [{
        queueItemId: 'queue-observed', commandId: 'command-observed', sequence: 1,
        status: 'pending', deliveryMode: 'steer', targetTurnId: 'turn-observed',
        content: textPrompt('观察者看到的队列正文'), permission: 'ask-permissions',
      }],
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    expect(await screen.findByText('观察者看到的队列正文')).toBeTruthy();
    const active = adapter.lastConnection!;

    active.emit({
      ...projection(''), messages: [], isRunning: true, eventSequence: 2,
      queuedCount: 0, queuedPrompts: [],
    });

    await waitFor(() => expect(active.queryCommand).toHaveBeenCalledWith('command-observed'));
    expect(await screen.findByText('队列已取消')).toBeTruthy();
    expect(screen.getByText('观察者看到的队列正文')).toBeTruthy();
    expect(screen.queryByText('目标轮次：turn-observed')).toBeNull();
    expect(screen.queryByText('适用权限：每次询问')).toBeNull();
    expect(screen.getByText('Observed queue item was cancelled.')).toBeTruthy();
  });

  it.each([
    ['cancelled', '队列已取消'],
    ['rejected', '队列已拒绝'],
  ] as const)('shows an initial-hydration %s terminal without a local submission', async (
    status,
    label,
  ) => {
    const adapter = new FakeAdapter();
    const transition: LocalPromptSubmission = {
      sessionId: 'session:bwpogagzox7onyn52iumyctnkq', connectionGeneration: 1,
      commandId: `command-${status}`, queueItemId: `queue-${status}`,
      contentUnavailable: true,
      deliveryMode: 'steer', targetTurnId: 'turn-race', permission: 'read-only',
      status, detail: `Queue ${status} before hydration completed.`,
    };
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: true, queuedCount: 0, queuedPrompts: [],
      promptTransitions: [transition],
    };

    renderWorkbench(<PulsaraApp adapter={adapter} />);

    expect(await screen.findByText(label)).toBeTruthy();
    expect(screen.getByText('正文未能在队列终止前完成读取。')).toBeTruthy();
    expect(screen.queryByText('目标轮次：turn-race')).toBeNull();
    expect(screen.queryByText('适用权限：只读')).toBeNull();
    expect(screen.getByText(`Queue ${status} before hydration completed.`)).toBeTruthy();
  });

  it('shows an idle submission in the conversation through pending receipt and canonical adoption', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const active = adapter.lastConnection!;
    const receipt = deferred<CommandReceipt>();
    active.submitPrompt.mockImplementationOnce(() => receipt.promise);
    await typeComposer('空闲时直接显示的输入');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledTimes(1));
    const commandId = active.submitPrompt.mock.calls[0]![0];
    const bubble = document.querySelector(`[data-pending-message="${commandId}"]`)!;
    expect(bubble.closest('.thread-column')).toBeTruthy();
    expect(bubble.textContent).toContain('空闲时直接显示的输入');
    expect(bubble.textContent).toContain('正在发送');
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
    await act(async () => receipt.resolve({ commandId, status: 'pending', promptDelivery: {
      queueItemId: 'idle-queue', queueStatus: 'PENDING', deliveryMode: 'new-turn',
    } }));
    act(() => active.emit({ ...projection(''), messages: [], eventSequence: 2, queuedCount: 1,
      queuedPrompts: [{ queueItemId: 'idle-queue', commandId, sequence: 1, status: 'pending',
        deliveryMode: 'new-turn', content: textPrompt('空闲时直接显示的输入'), permission: 'read-only' }],
    }));
    await waitFor(() => expect(document.querySelector(`[data-pending-message="${commandId}"]`)).toBe(bubble));
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
    expect(screen.queryByText(/1 条等待处理/)).toBeNull();
    act(() => active.emit({ ...projection(''), eventSequence: 3, messages: [{
      id: 'idle-entry', role: 'user', userKind: 'prompt', turnId: 'turn-1', time: '现在',
      body: '空闲时直接显示的输入',
      inputSource: { commandId, queueItemId: 'idle-queue', deliveryMode: 'new-turn' },
    }] }));
    await waitFor(() => expect(document.querySelector('[data-pending-message]')).toBeNull());
    expect(screen.getAllByText('空闲时直接显示的输入')).toHaveLength(1);
    expect(active.submitPrompt).toHaveBeenCalledTimes(1);
  });

  it('separates consumed input delivery from an interrupted turn outcome', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    adapter.lastConnection?.submitPrompt.mockImplementationOnce(async commandId => ({
      commandId,
      status: 'rejected',
      publicCode: 'TURN_INTERRUPTED',
      publicMessage: 'The turn was interrupted and will not be replayed.',
      promptDelivery: {
        queueItemId: 'queue-consumed', queueStatus: 'CONSUMED',
        consumedEntryId: 'entry-consumed', deliveryMode: 'new-turn',
      },
    }));

    await typeComposer('已经被消费的输入');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('输入已接收，执行已中断')).toBeTruthy();
    expect(screen.queryByText('输入被拒绝')).toBeNull();
    const bubble = document.querySelector('[data-pending-message]') as HTMLElement;
    expect(within(bubble).getByText('已接收 · 执行已中断')).toBeTruthy();
    expect(within(bubble).getByText('已经被消费的输入')).toBeTruthy();
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
  });

  it('keeps and reconciles a queued submission when its canonical pending row disappears', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: true, queuedCount: 0, queuedPrompts: [],
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const active = adapter.lastConnection!;
    active.submitPrompt.mockImplementationOnce(async commandId => ({
      commandId, status: 'pending',
      promptDelivery: {
        queueItemId: 'queue-terminal', queueStatus: 'PENDING', deliveryMode: 'new-turn',
      },
    }));
    active.queryCommand.mockImplementation(async commandId => ({
      commandId, status: 'rejected', publicCode: 'USER_CANCELLED',
      publicMessage: 'The queued prompt was cancelled.',
      promptDelivery: {
        queueItemId: 'queue-terminal', queueStatus: 'CANCELLED', deliveryMode: 'new-turn',
      },
    }));

    await typeComposer('随后被取消的输入');
    fireEvent.click(screen.getByRole('button', { name: '排队发送' }));
    await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledTimes(1));
    const commandId = active.submitPrompt.mock.calls[0]?.[0] as string;

    active.emit({
      ...projection(''), messages: [], isRunning: true, eventSequence: 2,
      queuedCount: 1,
      queuedPrompts: [{
        queueItemId: 'queue-terminal', commandId, sequence: 1, status: 'pending',
        deliveryMode: 'new-turn', content: textPrompt('随后被取消的输入'), permission: 'accept-edits',
      }],
    });
    await waitFor(() => expect(document.querySelector(
      '[data-queue-item-id="queue-terminal"] .prompt-content-body',
    )?.textContent).toBe('随后被取消的输入'));

    active.emit({
      ...projection(''), messages: [], isRunning: true, eventSequence: 3,
      queuedCount: 0, queuedPrompts: [],
    });

    await waitFor(() => expect(active.queryCommand).toHaveBeenCalledWith(commandId));
    expect(await screen.findByText('队列已取消')).toBeTruthy();
    expect(screen.getByText('The queued prompt was cancelled.')).toBeTruthy();
  });

  it('queries the original command on the replacement connection after transport failure', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    adapter.queryCommandResult = {
      commandId: 'resolved-by-query', status: 'rejected', publicCode: 'TURN_INTERRUPTED',
      promptDelivery: {
        queueItemId: 'queue-network', queueStatus: 'CONSUMED',
        consumedEntryId: 'entry-network', deliveryMode: 'new-turn',
      },
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const original = adapter.lastConnection!;
    original.submitPrompt.mockRejectedValueOnce(
      new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', '连接断开。', true),
    );

    await typeComposer('网络未知输入');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(original.submitPrompt).toHaveBeenCalledTimes(1));
    const commandId = original.submitPrompt.mock.calls[0]?.[0] as string;

    await waitFor(() => expect(adapter.connectCalls).toHaveLength(2));
    await waitFor(() => expect(adapter.lastConnection?.queryCommand).toHaveBeenCalledWith(commandId));
    expect(original.queryCommand).not.toHaveBeenCalled();
    expect(adapter.lastConnection?.submitPrompt).not.toHaveBeenCalled();
    expect(await screen.findByText('输入已接收，执行已中断')).toBeTruthy();
  });

  it.each([
    ['HTTP_413', false, 'HTTP 请求正文超过 8 MiB。'],
    ['PROTOCOL_FRAME_OUT_OF_BOUNDS', true, '这次请求包含的数据过多。'],
  ] as const)('keeps the exact draft after definite upload rejection %s', async (
    code,
    retryable,
    reason,
  ) => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const active = adapter.lastConnection!;
    active.submitPrompt.mockRejectedValueOnce(new RuntimeApiError(code, reason, retryable));

    await typeComposer('保留\\"草稿');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('输入超过上传容量')).toBeTruthy();
    const pendingMessage = document.querySelector('[data-pending-message]') as HTMLElement;
    expect(within(pendingMessage).getByRole('status').textContent).toContain('发送失败');
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
    expect(screen.getAllByText(reason).length).toBeGreaterThan(0);
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toBe('保留\\"草稿');
    expect(active.submitPrompt).toHaveBeenCalledWith(
      expect.any(String),
      textPrompt('保留\\"草稿'),
      'bypass-permissions',
    );
    expect(active.queryCommand).not.toHaveBeenCalled();
    expect(adapter.connectCalls).toHaveLength(1);
    expect(screen.queryByText('提交状态未知')).toBeNull();
    expect(within(pendingMessage).getByRole('status').textContent).toContain(reason);
  });

  it('does not publish a late prompt receipt from an old session onto the new session', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [initialSession, { ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '另一个会话', live: false }];
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    const receipt = deferred<CommandReceipt>();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const first = adapter.lastConnection!;
    first.submitPrompt.mockImplementationOnce(() => receipt.promise);

    await typeComposer('会话 A 的延迟输入');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(first.submitPrompt).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
    await screen.findByRole('heading', { name: '另一个会话' });

    const commandId = first.submitPrompt.mock.calls[0]![0];
    await act(async () => receipt.resolve({
      commandId, status: 'succeeded',
      promptDelivery: {
        queueItemId: 'queue-old-success', queueStatus: 'PENDING', deliveryMode: 'new-turn',
      },
    }));

    expect(screen.queryByText('输入已接受')).toBeNull();
    expect(screen.queryByText('会话 A 的延迟输入')).toBeNull();
    expect(adapter.connectCalls).toHaveLength(2);
  });

  it('does not reconnect the new session when an old-session prompt fails late', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [initialSession, { ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '另一个会话', live: false }];
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
    };
    const receipt = deferred<CommandReceipt>();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    const first = adapter.lastConnection!;
    first.submitPrompt.mockImplementationOnce(() => receipt.promise);

    await typeComposer('会话 A 的失败输入');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(first.submitPrompt).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
    await screen.findByRole('heading', { name: '另一个会话' });

    await act(async () => receipt.reject(
      new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', '会话 A 连接断开。', true),
    ));

    expect(adapter.connectCalls).toHaveLength(2);
    expect(adapter.lastConnection?.sessionId).toBe('session:hrluejkx3v2ajzf4ec6bb3ierm');
    expect(screen.queryByText('提交状态未知')).toBeNull();
    expect(screen.queryByText('会话 A 连接断开。')).toBeNull();
  });

  it('does not publish a late interaction decision from an old session', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [initialSession, { ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '另一个会话', live: false }];
    adapter.connectionValue = {
      ...projection(''), messages: [], isRunning: false, activeTurnId: undefined,
      interaction: {
        id: 'plan-old-session', kind: 'plan-draft', workflowId: 'workflow-old', workflowRevision: 1,
      },
    };
    const decision = deferred<CommandReceipt>();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByText('检查契约');
    const first = adapter.lastConnection!;
    first.resolveInteraction.mockImplementationOnce(() => decision.promise);

    fireEvent.click(screen.getByRole('button', { name: '取消规划' }));
    await waitFor(() => expect(first.resolveInteraction).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
    await screen.findByRole('heading', { name: '另一个会话' });

    await act(async () => decision.resolve({
      commandId: 'command-old-decision', status: 'succeeded',
      planDraftDecision: 'cancel',
    }));

    expect(screen.queryByText('规划已取消')).toBeNull();
    expect(adapter.connectCalls).toHaveLength(2);
  });

  it('opens a single worker group from the compact transcript without expanding child details', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection('正在并行检查。'),
      messages: [{
        id: 'assistant-subagents', turnId: 'turn-1',
        role: 'assistant',
        assistantKind: 'tool-request',
        forkEligible: false,
        time: '现在',
        body: '正在并行检查。',
        status: 'completed',
        traces: [{id:'spawn',kind:'mcp',toolName:'spawn_agent',title:'创建子任务',subtitle:'',status:'running'}],
      }],
      taskCreators: { 'task-readme': 'assistant-subagents' },
      subagentRuns: [{
          id: 'task-readme', parentId: 'turn-1', batchId: 'batch-1',
          label: '检查 README',
          role: '研究',
          objective: '读取 README.md 并报告一级标题。',
          status: 'completed',
          color: 'blue',
          summary: '# Pulsara',
          activities: [{
            id: 'activity-readme',
            time: '现在',
            body: '# Pulsara',
            status: 'completed',
            traces: [{
              id: 'trace-readme', kind: 'read', title: '读取文件', subtitle: '已完成',
              status: 'completed', meta: '操作完成', resultText: '已读取 README.md · 1 行',
            }],
          }],
        }],
      isRunning: false,
      activeTurnId: undefined,
    };
    adapter.taskInventory = [{id:'task-readme',label:'检查 README',role:'研究',objective:'读取 README.md 并报告一级标题。',status:'completed',color:'blue',parentId:'turn-1',batchId:'batch-1',dependencyIds:[],completionAccepted:true}];
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '展开处理过程' }));
    const summary = await screen.findByLabelText('任务执行');
    expect(summary.textContent).not.toContain('读取 README.md 并报告一级标题。');
    expect(summary.querySelector('.subagent-run__body')).toBeNull();
    fireEvent.click(within(summary).getByRole('button', { name: '查看任务：检查 README' }));
    const tab = await screen.findByRole('tab', {name: '检查 README'});
    expect(tab.getAttribute('aria-selected')).toBe('true');
    expect(adapter.readSessionTask).toHaveBeenCalledWith(initialSession.id, 'task-readme');
    await waitFor(() => expect(adapter.listSessionTasks).toHaveBeenCalledWith(initialSession.id, undefined, 'batch-1'));
    expect(await screen.findByRole('heading', {name:'任务对话'})).toBeTruthy();
    fireEvent.click(within(summary).getByRole('button', { name: '查看任务：检查 README' }));
    await waitFor(() => expect(adapter.readSessionTask).toHaveBeenCalledTimes(2));
    expect(screen.getAllByRole('tab', {name: '检查 README'})).toHaveLength(1);
  });

  it('keeps the complete durable child-task history inside its session', async () => {
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection('主任务继续运行。'),
      messages: [{
        id: 'assistant-root', turnId: 'turn-1', role: 'assistant', time: '现在',
        body: '主任务继续运行。', status: 'running',
        traces: [{
          id: 'spawn-task', kind: 'mcp', toolName: 'spawn_agent', title: '创建子任务',
          subtitle: '已完成', status: 'completed', meta: '操作完成',
        }],
      }],
      isRunning: false,
      activeTurnId: undefined,
      agentTasks: [],
    };
    adapter.taskInventory = [{
      id: 'task-complete', label: '汇总研究', role: '研究', profile: 'research_worker',
      objective: '### 汇总目标\n\n整理可见结果。', status: 'completed',
      parentId: 'turn-1', batchId: 'batch-1', taskKey: 'research',
      context: { mode: 'last-n', lastNTurns: 4 }, dependencyIds: [],
      completionAccepted: false,
      acceptedAt: '2026-08-30T12:00:00Z', terminalAt: '2026-08-30T12:01:00Z',
      summary: '### 研究结论\n\n页面符合契约。', color: 'blue',
      result: {
        id: 'result-complete', entryId: 'entry-result', summary: '### 研究结论\n\n页面符合契约。',
        outputPreview: '- 完整输出', diagnostics: [{ message: '目视检查通过' }],
      },
    }, {
      id: 'task-interrupted', label: '中断检查', role: '验证', objective: '验证重启边界。',
      status: 'interrupted', parentId: 'turn-2', batchId: 'batch-2', dependencyIds: [], color: 'amber',
      completionAccepted: false,
    }, {
      id: 'task-blocked', label: '等待产物', role: '整合', objective: '整合前置产物。',
      status: 'blocked', parentId: 'turn-1', batchId: 'batch-1', dependencyIds: ['task-interrupted'],
      dependencies: [{ id: 'task-interrupted', label: '中断检查', status: 'interrupted' }], color: 'violet',
      completionAccepted: false,
    }];

    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    await waitFor(() => expect(adapter.listSessionTaskGroups).toHaveBeenCalled());

    expect(screen.queryByRole('tab')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '任务' }));
    expect(screen.getByRole('button', { name: '任务' }).classList.contains('is-active')).toBe(true);
    expect(await screen.findByText(/1 已完成 · 0 进行中 · 1 需留意/)).toBeTruthy();
    expect(screen.getByRole('button', { name: /中断检查/ })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /任务组/ }));
    const dialog = await screen.findByRole('region', { name: /任务组/ });
    await waitFor(() => expect(adapter.listSessionTasks).toHaveBeenCalledWith(expect.anything(), undefined, 'batch-1'));
    expect(within(dialog).getByRole('button', { name: /等待产物/ })).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: /汇总研究/ }));
    const taskConversation = dialog.querySelector('.task-conversation');
    expect(taskConversation).toBeTruthy();
    expect(within(taskConversation as HTMLElement).getByText('主 Agent')).toBeTruthy();
    expect(taskConversation?.querySelectorAll('.user-turn')).toHaveLength(1);
    expect(taskConversation?.querySelector('.user-turn')?.textContent).toContain('### 汇总目标\n\n整理可见结果。');
    expect(within(dialog).queryByRole('heading', { name: '汇总目标' })).toBeNull();
    expect(screen.getByRole('heading', { name: '研究结论' })).toBeTruthy();
    expect(within(dialog).getByText('目视检查通过')).toBeTruthy();

    expect(within(dialog).queryByText('结果尚未加入主对话。')).toBeNull();
    expect(within(dialog).queryByRole('button', { name: '用这份结果继续' })).toBeNull();
    expect(within(dialog).queryByText(/不会重新运行子任务/)).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: '其他任务组依赖来源：中断检查' }));
    const detail = await screen.findByRole('complementary', { name: '中断检查 详情' });
    expect(within(detail).getByText('验证重启边界。')).toBeTruthy();
    expect(adapter.readSessionTask).toHaveBeenCalledWith('session:bwpogagzox7onyn52iumyctnkq', 'task-interrupted');
    const reads = adapter.readSessionTask.mock.calls.length;
    const activityReads = adapter.listSessionTaskActivities.mock.calls.length;
    const background = vi.spyOn(adapter.lastConnection!, 'listBackgroundProcesses');
    await act(async () => {
      const current = adapter.lastConnection!.current();
      adapter.lastConnection!.emit({ ...current, messages: current.messages.map(message => ({ ...message, body: '新的实时内容' })) });
    });
    expect(adapter.readSessionTask).toHaveBeenCalledTimes(reads);
    expect(adapter.listSessionTaskActivities).toHaveBeenCalledTimes(activityReads);
    expect(background).not.toHaveBeenCalled();
  });

  it('shows exact cross-group worker live activity without making its observations task authority', async () => {
    const adapter = new FakeAdapter();
    const source: AgentTask = { id: 'source', label: '事实核对', role: '研究', objective: '检查事实', status: 'running', batchId: 'prior',
      parentId: 'turn-1', dependencyIds: [], completionAccepted: false, color: 'blue' };
    adapter.taskInventory = [source, { ...source, id: 'follow', label: '综合审阅', status: 'waiting', batchId: 'next',
      dependencyIds: ['source'], dependencies: [{ id: 'source', label: '事实核对', status: 'running' }] }];
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', { name: '准备发布' });
    fireEvent.click(screen.getByRole('button', { name: '任务' }));
    fireEvent.click(await screen.findByRole('button', { name: /综合审阅/ }));
    fireEvent.click(await screen.findByRole('button', { name: '其他任务组依赖来源：事实核对' }));
    const detail = await screen.findByRole('complementary', { name: '事实核对 详情' });
    await waitFor(() => expect(adapter.listSessionTaskActivities).toHaveBeenLastCalledWith(
      'session:bwpogagzox7onyn52iumyctnkq', 'source', undefined,
    ));
    const reads = adapter.readSessionTask.mock.calls.length;
    const activityReads = adapter.listSessionTaskActivities.mock.calls.length;
    const emitWorker = async () => { await act(async () => {
      const current = adapter.lastConnection!.current();
      adapter.lastConnection!.emit({ ...current, subagentRuns: [{ ...source, activities: [{
        id: 'live:worker-entry', kind: 'work', body: '跨组 worker 正在核对资料', time: '现在', status: 'running',
        traces: [{ id: 'worker-read', kind: 'read', title: '读取产品事实', subtitle: '正在读取', toolName: 'read_file', status: 'running' }],
      }] }] });
    }); };
    await emitWorker();
    expect(await within(detail).findByText('跨组 worker 正在核对资料')).toBeTruthy();
    expect(within(detail).getByText('读取文件')).toBeTruthy();
    expect(adapter.readSessionTask).toHaveBeenCalledTimes(reads);
    expect(adapter.listSessionTaskActivities).toHaveBeenCalledTimes(activityReads);
    fireEvent.click(screen.getByRole('button', { name: '关闭 综合审阅' }));
    adapter.taskInventory[0] = { ...source, status: 'completed' };
    fireEvent.click(screen.getByRole('button', { name: /综合审阅/ }));
    fireEvent.click(await screen.findByRole('button', { name: '其他任务组依赖来源：事实核对' }));
    const completedDetail = await screen.findByRole('complementary', { name: '事实核对 详情' });
    await emitWorker();
    expect(within(completedDetail).getByText('已完成')).toBeTruthy();
    expect(within(completedDetail).queryByText('跨组 worker 正在核对资料')).toBeNull();
  });

  it('keeps a newer group page when the initial page returns late', async () => {
    const adapter = new FakeAdapter();
    const oldTask: AgentTask = {
      id: 'task-race', batchId: 'batch-race', label: '旧状态任务', role: '通用协作',
      objective: '检查页读取水位', status: 'running', parentId: 'turn-1',
      dependencyIds: [], completionAccepted: false, color: 'blue',
    };
    const newTask: AgentTask = { ...oldTask, label: '更新后任务', status: 'completed' };
    adapter.taskInventory = [oldTask];
    const counts = (completed: number) => ({
      pending: 0, active: 1 - completed, waiting: 0, completed,
      cancelled: 0, failed: 0, interrupted: 0, blocked: 0,
    });
    const oldPage = deferred<Awaited<ReturnType<typeof adapter.listSessionTaskGroups>>>();
    adapter.taskInventory = [newTask];
    adapter.listSessionTasks.mockResolvedValue({ tasks: [newTask], readEventSequence: 110, totalCount: 1, remainingCount: 0 });
    adapter.listSessionTaskGroups.mockResolvedValue({ groups: [{ id: 'batch-race', parentTurnId: 'turn-1', firstAcceptedAt: '', taskCount: 1, statusCounts: counts(1), singleTaskLabel: '更新后任务' }], readEventSequence: 110, totalCount: 1, remainingCount: 0 });
    adapter.listSessionTaskGroups.mockImplementationOnce(() => oldPage.promise);
    adapter.readSessionTaskGroup.mockResolvedValue({
      group: { id: 'batch-race', parentTurnId: 'turn-1', firstAcceptedAt: '',
        taskCount: 1, statusCounts: counts(1), singleTaskLabel: '更新后任务' },
      readEventSequence: 110, totalCount: 1,
    });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await waitFor(() => expect(adapter.listSessionTaskGroups).toHaveBeenCalled());
    await act(async () => adapter.lastConnection?.emit({
      ...projection(''), eventSequence: 110, taskInvalidations: ['task-race'], taskGroupInvalidations: ['batch-race'],
    }));
    await waitFor(() => expect(adapter.listSessionTaskGroups).toHaveBeenCalledTimes(2));
    expect(adapter.readSessionTask).not.toHaveBeenCalled();
    await act(async () => oldPage.resolve({
      groups: [{ id: 'batch-race', parentTurnId: 'turn-1', firstAcceptedAt: '',
        taskCount: 1, statusCounts: counts(0), singleTaskLabel: '旧状态任务' }],
      totalCount: 1, readEventSequence: 100, remainingCount: 0,
    }));
    fireEvent.click(screen.getByRole('button', { name: '任务' }));
    expect(await screen.findByRole('button', { name: /更新后任务/ })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /旧状态任务/ })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /更新后任务/ }));
    const dialog = await screen.findByRole('region', { name: '更新后任务' });
    expect(within(dialog).getByRole('button', { name: /更新后任务.*已完成/ })).toBeTruthy();
  });
  it('refreshes an open group after a same-connection snapshot and releases its rows on close', async () => {
    const adapter = new FakeAdapter();
    const oldTask: AgentTask = { id: 'snapshot-task', batchId: 'snapshot-batch', label: '快照任务', role: '通用协作', objective: 'inspect', status: 'running', parentId: 'turn-1', dependencyIds: [], completionAccepted: false, color: 'blue' };
    adapter.taskInventory = [oldTask];
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await waitFor(() => expect(adapter.listSessionTaskGroups).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: '任务' }));
    fireEvent.click(await screen.findByRole('button', { name: /快照任务/ }));
    let dialog = await screen.findByRole('region', { name: '快照任务' });
    expect(within(dialog).getByRole('button', { name: /快照任务.*进行中/ })).toBeTruthy();
    adapter.taskInventory = [{ ...oldTask, status: 'completed' }];
    await act(async () => adapter.lastConnection?.emit({ ...projection(''), eventSequence: 120, taskSnapshotRevision: 2 }));
    await waitFor(() => expect(within(dialog).getByRole('button', { name: /快照任务.*已完成/ })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: '关闭 快照任务' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: '快照任务' })).toBeNull());
    const reads = adapter.listSessionTasks.mock.calls.length;
    await act(async () => adapter.lastConnection?.emit({ ...projection(''), eventSequence: 121, taskSnapshotRevision: 2, taskInvalidations: ['snapshot-task'], taskGroupInvalidations: ['snapshot-batch', 'snapshot-batch'] }));
    await waitFor(() => expect(adapter.readSessionTaskGroup).toHaveBeenCalled());
    expect(adapter.listSessionTasks.mock.calls.length).toBe(reads);
    expect(adapter.readSessionTask).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /快照任务/ }));
    dialog = await screen.findByRole('region', { name: '快照任务' });
    expect(adapter.listSessionTasks.mock.calls.length).toBe(reads + 1);
  });

  it('does not let an older exact group read roll back the overview total', async () => {
    const adapter = new FakeAdapter();
    adapter.taskInventory = [{ id: 'a', batchId: 'batch-a', label: '总数检查', role: '通用协作', objective: 'read', status: 'running', parentId: 'turn-1', dependencyIds: [], completionAccepted: false, color: 'blue' }];
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await waitFor(() => expect(adapter.listSessionTaskGroups).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: '任务' }));
    await screen.findByRole('button', { name: /总数检查/ });
    const base = await adapter.readSessionTaskGroup('session:bwpogagzox7onyn52iumyctnkq', 'batch-a');
    const delayed = deferred<typeof base>();
    adapter.readSessionTaskGroup.mockImplementationOnce(() => delayed.promise);
    await act(async () => adapter.lastConnection?.emit({ ...projection(''), eventSequence: 110, taskGroupInvalidations: ['batch-a'] }));
    adapter.readSessionTaskGroup.mockResolvedValue({ ...base, totalCount: 2, readEventSequence: 120 });
    await act(async () => adapter.lastConnection?.emit({ ...projection(''), eventSequence: 120, taskGroupInvalidations: ['batch-a'] }));
    await screen.findByRole('button', { name: '加载更多任务组' });
    await act(async () => delayed.resolve({ ...base, totalCount: 1, readEventSequence: 110 }));
    expect(screen.getByRole('button', { name: '加载更多任务组' })).toBeTruthy();
  });

  it('refreshes the open group when an external dependency changes without its own transition', async () => {
    const adapter = new FakeAdapter();
    adapter.taskInventory = [
      { id: 'a', batchId: 'batch-a', label: '前置 A', role: '通用协作', objective: 'read', status: 'running', parentId: 'turn-1', dependencyIds: [], completionAccepted: false, color: 'blue' },
      { id: 'b', batchId: 'batch-b', label: '等待两个来源', role: '通用协作', objective: 'read', status: 'waiting', parentId: 'turn-1', dependencyIds: ['a', 'c'], completionAccepted: false, color: 'blue' },
    ];
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await waitFor(() => expect(adapter.listSessionTaskGroups).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: '任务' }));
    fireEvent.click(await screen.findByRole('button', { name: /等待两个来源/ }));
    await screen.findByRole('region', { name: '等待两个来源' });
    const reads = adapter.listSessionTasks.mock.calls.length;
    await act(async () => adapter.lastConnection?.emit({ ...projection(''), eventSequence: 130, taskInvalidations: ['a'], taskGroupInvalidations: ['batch-a'] }));
    await waitFor(() => expect(adapter.listSessionTasks.mock.calls.length).toBe(reads + 1));
    expect(adapter.listSessionTasks).toHaveBeenLastCalledWith('session:bwpogagzox7onyn52iumyctnkq', undefined, 'batch-b');
  });

  it('coalesces repeated more-page clicks and rejects a page from an old snapshot owner', async () => {
    const adapter = new FakeAdapter();
    const group = (id: string) => ({id, parentTurnId:'turn-1', firstAcceptedAt:'',taskCount:1,singleTaskLabel:id,statusCounts:{pending:1,active:0,waiting:0,completed:0,cancelled:0,failed:0,interrupted:0,blocked:0}});
    const delayed = deferred<Awaited<ReturnType<typeof adapter.listSessionTaskGroups>>>();
    const fresh = {...group('old-first'), singleTaskLabel:'fresh'};
    adapter.listSessionTaskGroups.mockResolvedValue({groups:[fresh],totalCount:1,remainingCount:0,readEventSequence:120});
    adapter.readSessionTaskGroup.mockResolvedValue({group:fresh,totalCount:1,readEventSequence:120});
    adapter.listSessionTaskGroups.mockResolvedValueOnce({groups:[group('old-first')],totalCount:2,remainingCount:1,nextCursor:'old-cursor',readEventSequence:100});
    adapter.listSessionTaskGroups.mockImplementationOnce(()=>delayed.promise);
    renderWorkbench(<PulsaraApp adapter={adapter}/>);
    fireEvent.click(await screen.findByRole('button',{name:'任务'}));
    const more = await screen.findByRole('button',{name:'加载更多任务组'});
    fireEvent.click(more);
    fireEvent.click(more);
    expect(adapter.listSessionTaskGroups).toHaveBeenCalledTimes(2);
    await act(async()=>adapter.lastConnection?.emit({...projection(''),eventSequence:120,taskSnapshotRevision:2}));
    await screen.findByRole('button',{name:/fresh/});
    await act(async()=>delayed.resolve({groups:[group('old-delayed')],totalCount:3,remainingCount:1,nextCursor:'another-old-cursor',readEventSequence:110}));
    expect(screen.queryByRole('button',{name:/old-delayed/})).toBeNull();
    expect(screen.queryByRole('button',{name:'加载更多任务组'})).toBeNull();
  });

  it('keeps newly appended groups reachable after exhausting more than one page', async () => {
    const adapter = new FakeAdapter();
    const group = (index: number) => ({ id: `batch-${index}`, parentTurnId: 'turn-1', firstAcceptedAt: '', taskCount: 1, singleTaskLabel: `分组 ${index}`, statusCounts: {pending: 1, active: 0, waiting: 0, completed: 0, cancelled: 0, failed: 0, interrupted: 0, blocked: 0} });
    let count = 64;
    adapter.listSessionTaskGroups.mockImplementation(async (_sessionId, cursor) => ({
      groups: Array.from({length: cursor ? count - 50 : 50}, (_, i) => group(i + (cursor ? 50 : 0))),
      totalCount: count, readEventSequence: count, remainingCount: cursor ? 0 : count - 50,
      nextCursor: cursor ? undefined : 'after-49',
    }));
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await waitFor(() => expect(adapter.listSessionTaskGroups).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: '任务' }));
    fireEvent.click(await screen.findByRole('button', { name: '加载更多任务组' }));
    await screen.findByRole('button', { name: /分组 63/ });
    expect(screen.queryByRole('button', { name: '加载更多任务组' })).toBeNull();
    count = 65;
    await act(async () => adapter.lastConnection?.emit({ ...projection(''), eventSequence: 65, taskGroupInvalidations: ['batch-64'] }));
    expect(await screen.findByRole('button', { name: /分组 64/ })).toBeTruthy();
    expect(adapter.listSessionTaskGroups).toHaveBeenLastCalledWith('session:bwpogagzox7onyn52iumyctnkq', 'after-49');
    expect(adapter.readSessionTask).not.toHaveBeenCalled();
  });

});

describe('PR04 atomic queue action ownership', () => {
  const source = {
    queueItemId: 'queue-source', commandId: 'source-command', sequence: 1, status: 'pending' as const,
    deliveryMode: 'new-turn' as const, content: textPrompt('  keep\n原文  '), permission: 'read-only' as const,
    requestedPermission: 'ask-permissions' as const,
  };
  async function setup() {
    const adapter = new FakeAdapter();
    adapter.connectionValue = { ...projection(''), messages: [], queuedCount: 1, queuedPrompts: [source],
      control: { active_turns: [{ turn_id: 'turn-1', scope_kind: 'ROOT', status: 'RUNNING' }] } };
    const view = renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('region', { name: '等待处理的输入' });
    return { adapter, active: adapter.lastConnection!, ...view };
  }
  const accepted = (commandId: string): CommandReceipt => ({
    commandId, status: 'pending', publicCode: 'PROMPT_STEER_QUEUED', promptDelivery: {
      queueItemId: 'replacement', queueStatus: 'PENDING', deliveryMode: 'steer',
    },
  });

  it('sends one text-free action, waits for ACK, then exactly replaces the optimistic card', async () => {
    const { active, container } = await setup();
    const pending = deferred<CommandReceipt>();
    const steer = vi.spyOn(active, 'steerQueuedPrompt').mockReturnValue(pending.promise);
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt');
    const send = within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' });
    fireEvent.click(send);
    fireEvent.click(send);
    expect(steer).toHaveBeenCalledTimes(1);
    const commandId = steer.mock.calls[0][0];
    expect(steer).toHaveBeenCalledWith(commandId, source.queueItemId, 'turn-1');
    expect(cancel).not.toHaveBeenCalled();
    expect(active.submitPrompt).not.toHaveBeenCalled();
    expect(screen.queryByRole('article', { name: '引导' })).toBeNull();
    pending.resolve(accepted(commandId));
    await screen.findByRole('article', { name: '引导' });
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
    expect(container.querySelector('.user-steer .prompt-content-body')?.textContent).toBe(
      promptContentTextProjection(source.content),
    );
    active.emit({ ...projection(''), queuedCount: 0, queuedPrompts: [], eventSequence: 3,
      messages: [{ id: 'canonical-steer', role: 'user', userKind: 'steer', turnId: 'turn-1',
        time: 'now', body: promptContentTextProjection(source.content), status: 'completed',
        inputSource: { commandId, queueItemId: 'replacement', deliveryMode: 'steer' } }],
    });
    await waitFor(() => expect(container.querySelector('[data-action-command-id]')).toBeNull());
    expect(screen.getAllByRole('article', { name: '引导' })).toHaveLength(1);
  });

  it('recovers a lost action ACK by querying its original command on the new connection', async () => {
    const { active, adapter } = await setup();
    const steer = vi.spyOn(active, 'steerQueuedPrompt').mockImplementation(async commandId => {
      adapter.queryCommandResult = accepted(commandId);
      throw new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'ACK lost', true);
    });
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    await screen.findByRole('article', { name: '引导' });
    expect(adapter.connectCalls).toHaveLength(2);
    expect(steer).toHaveBeenCalledTimes(1);
    expect(adapter.lastConnection!.queryCommand).toHaveBeenCalledWith(steer.mock.calls[0][0]);
    expect(adapter.lastConnection!.submitPrompt).not.toHaveBeenCalled();
  });

  it('freezes the current canonical ROOT after reload instead of a stale live snapshot ROOT', async () => {
    const { active } = await setup();
    const steer = vi.spyOn(active, 'steerQueuedPrompt').mockImplementation(async commandId => accepted(commandId));
    active.emit({ ...active.current(), activeTurnId: 'turn-old-snapshot',
      control: { active_turns: [{ turn_id: 'turn-successor', scope_kind: 'ROOT', status: 'RUNNING' }] },
      eventSequence: 9,
    });
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    expect(steer).toHaveBeenCalledWith(expect.any(String), source.queueItemId, 'turn-successor');
    await screen.findByRole('article', { name: '引导' });
  });

  it('does not send to the reloaded ROOT when canonical control has no RUNNING ROOT', async () => {
    const { active } = await setup();
    const steer = vi.spyOn(active, 'steerQueuedPrompt');
    active.emit({ ...active.current(), activeTurnId: 'turn-old-snapshot', control: {}, eventSequence: 10 });
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    expect(steer).not.toHaveBeenCalled();
    expect(screen.getByText('这条输入会按队列顺序自动处理。')).toBeTruthy();
    expect(screen.getByRole('region', { name: '等待处理的输入' })).toBeTruthy();
  });

  it('keeps the exact source after the completion fence rejects the action', async () => {
    const { active, container } = await setup();
    vi.spyOn(active, 'steerQueuedPrompt').mockImplementation(async commandId => ({
      commandId, status: 'rejected', publicCode: 'STEER_TARGET_CLOSED', publicMessage: '当前任务已结束；输入仍按队列顺序处理。',
    }));
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    await screen.findByText('当前任务已结束；输入仍按队列顺序处理。');
    expect(container.querySelector(
      '[data-queue-item-id="queue-source"] .prompt-content-body',
    )?.textContent).toBe(
      promptContentTextProjection(source.content),
    );
    expect(screen.queryByRole('article', { name: '引导' })).toBeNull();
    expect(active.submitPrompt).not.toHaveBeenCalled();
  });

  it('edits only after cancellation ACK and restores the requested permission', async () => {
    const { active } = await setup();
    const cancellation = deferred<CommandReceipt>();
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt').mockReturnValue(cancellation.promise);
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    const input = screen.getByLabelText('发送给 Pulsara');
    expect(input.textContent).toBe('');
    expect(screen.getByRole('region', { name: '等待处理的输入' })).toBeTruthy();
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    cancellation.resolve({ commandId: cancel.mock.calls[0][0], status: 'succeeded', publicCode: 'PROMPT_CANCELLED',
      promptDelivery: { queueItemId: source.queueItemId, queueStatus: 'CANCELLED', deliveryMode: 'new-turn' } });
    await waitFor(() => expect(composerIsDisabled(screen.getByLabelText('发送给 Pulsara')))
      .toBe(false));
    const restored = screen.getByLabelText('发送给 Pulsara');
    expect(restored.innerHTML).toContain('<br');
    expect(restored.textContent).toContain('keep');
    expect(restored.textContent).toContain('原文');
    expect(screen.getByRole('button', { name: /每次询问/ })).toBeTruthy();
    await waitFor(() => expect(document.activeElement)
      .toBe(screen.getByLabelText('发送给 Pulsara')));
    expect(active.submitPrompt).not.toHaveBeenCalled();
    fireEvent.keyDown(restored, { key: 'Enter' });
    await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledWith(
      expect.any(String),
      source.content,
      'ask-permissions',
    ));
  });

  it('hydrates every queued image before cancellation and restores exact typed nodes', async () => {
    const digest = `sha256:${'1'.repeat(64)}`;
    const imageSource = {
      ...source,
      queueItemId: 'queue-images',
      commandId: 'source-images',
      content: {
        parts: [
          { type: 'text' as const, text: 'before' },
          {
            type: 'image' as const,
            source: 'canonical' as const,
            digest,
            encodedBytes: 3,
            mediaType: 'image/png',
            width: 2,
            height: 1,
            refOrdinal: 0,
            owner: { kind: 'queue' as const, queueItemId: 'queue-images' },
          },
          { type: 'text' as const, text: '\nafter' },
          {
            type: 'image' as const,
            source: 'canonical' as const,
            digest,
            encodedBytes: 3,
            mediaType: 'image/png',
            width: 2,
            height: 1,
            refOrdinal: 1,
            owner: { kind: 'queue' as const, queueItemId: 'queue-images' },
          },
        ],
      },
    };
    const adapter = new FakeAdapter();
    adapter.connectionValue = {
      ...projection(''),
      messages: [],
      queuedCount: 1,
      queuedPrompts: [imageSource],
      control: {
        active_turns: [{ turn_id: 'turn-1', scope_kind: 'ROOT', status: 'RUNNING' }],
      },
    };
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    const queue = await screen.findByRole('region', { name: '等待处理的输入' });
    expect(within(queue).getAllByRole('button', { name: /^打开 Figure [12]$/ }))
      .toHaveLength(2);
    const active = adapter.lastConnection!;
    const secondRead = deferred<Uint8Array>();
    const read = vi.spyOn(active, 'readPromptImage').mockImplementation(async (image) => {
      if (image.refOrdinal === 0) return new Uint8Array([1, 2, 3]);
      return secondRead.promise;
    });
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt');

    fireEvent.click(within(queue).getByRole('button', { name: '编辑' }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(read.mock.calls.map(([image]) => [image.owner, image.refOrdinal])).toEqual([
      [{ kind: 'queue', queueItemId: 'queue-images' }, 0],
      [{ kind: 'queue', queueItemId: 'queue-images' }, 1],
    ]);
    expect(cancel).not.toHaveBeenCalled();
    expect(screen.getByRole('region', { name: '等待处理的输入' })).toBeTruthy();
    expect(composerIsDisabled(screen.getByLabelText('发送给 Pulsara'))).toBe(true);

    secondRead.resolve(new Uint8Array([4, 5, 6]));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(composerIsDisabled(
      screen.getByLabelText('发送给 Pulsara'),
    )).toBe(false));
    const composer = screen.getByLabelText('发送给 Pulsara');
    expect(within(composer).getAllByRole('button', { name: /^打开 Figure [12]$/ })).toHaveLength(2);
    expect(composer.textContent).toContain('before');
    expect(composer.textContent).toContain('after');
    await waitFor(() => expect(document.activeElement).toBe(composer));

    fireEvent.keyDown(composer, { key: 'Enter' });
    await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledWith(
      expect.any(String),
      {
        parts: [
          { type: 'text', text: 'before' },
          {
            type: 'image', source: 'local',
            bytes: new Uint8Array([1, 2, 3]), declaredMediaType: 'image/png',
          },
          { type: 'text', text: '\nafter' },
          {
            type: 'image', source: 'local',
            bytes: new Uint8Array([4, 5, 6]), declaredMediaType: 'image/png',
          },
        ],
      },
      'ask-permissions',
    ));
  });

  it('releases an unknown edit when its exact source has been canonically consumed', async () => {
    const { active, adapter } = await setup();
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt').mockImplementation(async () => {
      // FIFO won; the rejection was not persisted as an action command.
      throw new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'rejection ACK lost', true);
    });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    await waitFor(() => expect(adapter.connectCalls).toHaveLength(2));
    const next = adapter.lastConnection!;
    await waitFor(() => expect(next.queryCommand).toHaveBeenCalledWith(cancel.mock.calls[0][0]));
    const input = screen.getByLabelText('发送给 Pulsara');
    expect(composerIsDisabled(input)).toBe(true);
    // Identical text from a different submission is not evidence about source.
    next.emit({ ...next.current(), eventSequence: 10, messages: [{
      id: 'other', role: 'user', userKind: 'prompt', time: 'now',
      body: promptContentTextProjection(source.content),
      inputSource: { commandId: 'other-command', queueItemId: 'other-queue', deliveryMode: 'new-turn' },
    }] });
    await waitFor(() => expect(next.queryCommand.mock.calls.length).toBeGreaterThan(1));
    expect(composerIsDisabled(input)).toBe(true);
    next.emit({ ...next.current(), eventSequence: 20, queuedPrompts: [], queuedCount: 0, messages: [{
      id: 'consumed-source', turnId: 'turn-next', entrySequence: 20,
      role: 'user', userKind: 'prompt', time: 'now',
      body: promptContentTextProjection(source.content),
      inputSource: { commandId: source.commandId, queueItemId: source.queueItemId, deliveryMode: 'new-turn' },
    }] });
    await waitFor(() => expect(composerIsDisabled(input)).toBe(false));
    expect(input.textContent).toBe('');
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(next.submitPrompt).not.toHaveBeenCalled();
  });

  it('reserves skill insertion as well as typing while an edit is in flight', async () => {
    const { active } = await setup();
    const pending = deferred<CommandReceipt>();
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt').mockReturnValue(pending.promise);
    fireEvent.click(screen.getByRole('button', { name: '添加文件、技能或规划' }));
    await waitFor(() => expect((screen.getByRole('button', { name: '选择技能' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(await screen.findByRole('button', { name: '选择技能' }));
    const skill = screen.getByRole('button', { name: /\/pdf/ });
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    // Even an already-open menu must not bypass the composer reservation.
    fireEvent.click(skill);
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    pending.resolve({ commandId: cancel.mock.calls[0][0], status: 'succeeded', publicCode: 'PROMPT_CANCELLED',
      promptDelivery: { queueItemId: source.queueItemId, queueStatus: 'CANCELLED', deliveryMode: 'new-turn' } });
    await waitFor(() => expect(composerIsDisabled(screen.getByLabelText('发送给 Pulsara')))
      .toBe(false));
    const restored = screen.getByLabelText('发送给 Pulsara');
    expect(restored.innerHTML).toContain('<br');
    fireEvent.keyDown(restored, { key: 'Enter' });
    await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledWith(
      expect.any(String),
      source.content,
      'ask-permissions',
    ));
  });

  it('queries a rejected steer even if no pending snapshot ever contained the replacement', async () => {
    const { active } = await setup();
    const steer = vi.spyOn(active, 'steerQueuedPrompt').mockImplementation(async commandId => accepted(commandId));
    const query = vi.spyOn(active, 'queryCommand').mockImplementation(async commandId => accepted(commandId));
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    await screen.findByRole('article', { name: '引导' });
    const commandId = steer.mock.calls[0][0];
    query.mockImplementation(async id => id === source.commandId ? {
      commandId: id, status: 'succeeded', publicCode: 'PROMPT_CANCELLED',
      promptDelivery: { queueItemId: source.queueItemId, queueStatus: 'CANCELLED', deliveryMode: 'new-turn' },
    } : ({ commandId: id, status: 'rejected', publicCode: 'STEER_TARGET_TERMINAL',
      publicMessage: '目标任务已中断，引导未被接纳。',
      promptDelivery: { queueItemId: 'replacement', queueStatus: 'REJECTED', deliveryMode: 'steer' } }));
    active.emit({ ...active.current(), isRunning: false, control: { active_turns: [] },
      eventSequence: 10, messages: [], queuedPrompts: [], queuedCount: 0 });
    await waitFor(() => expect(screen.queryByRole('article', { name: '引导' })).toBeNull());
    expect(query).toHaveBeenCalledWith(commandId);
    expect(steer).toHaveBeenCalledTimes(1);
    expect(screen.getByText('目标任务已中断，引导未被接纳。')).toBeTruthy();
  });

  it('queries the exact consumed source when it is absent from visible history', async () => {
    const { active, adapter } = await setup();
    const cancel = vi.spyOn(active, 'cancelQueuedPrompt').mockRejectedValue(
      new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'rejection ACK lost', true),
    );
    fireEvent.click(screen.getByRole('button', { name: '编辑' }));
    await waitFor(() => expect(adapter.connectCalls).toHaveLength(2));
    const next = adapter.lastConnection!;
    await waitFor(() => expect(next.queryCommand).toHaveBeenCalledWith(cancel.mock.calls[0][0]));
    next.queryCommand.mockImplementation(async id => id === source.commandId ? {
      commandId: id, status: 'succeeded', publicCode: 'PROMPT_CONSUMED',
      promptDelivery: { queueItemId: source.queueItemId, queueStatus: 'CONSUMED', deliveryMode: 'new-turn' },
    } : undefined);
    next.emit({ ...next.current(), eventSequence: 20, messages: [], queuedPrompts: [], queuedCount: 0 });
    const input = screen.getByLabelText('发送给 Pulsara');
    await waitFor(() => expect(composerIsDisabled(input)).toBe(false));
    expect(next.queryCommand).toHaveBeenCalledWith(source.commandId);
    expect(input.textContent).toBe('');
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(next.submitPrompt).not.toHaveBeenCalled();
  });

  it('rechecks a newer cut after the original action query finishes without issuing concurrent queries', async () => {
    const { active } = await setup();
    const pendingQuery = deferred<CommandReceipt>();
    const steer = vi.spyOn(active, 'steerQueuedPrompt').mockImplementation(async id => accepted(id));
    const query = vi.spyOn(active, 'queryCommand').mockReturnValue(pendingQuery.promise);
    fireEvent.click(within(screen.getByRole('region', { name: '等待处理的输入' })).getByRole('button', { name: '发送' }));
    await screen.findByRole('article', { name: '引导' });
    const commandId = steer.mock.calls[0][0];
    await waitFor(() => expect(query).toHaveBeenCalledWith(commandId));
    query.mockImplementation(async id => id === source.commandId ? {
      commandId: id, status: 'succeeded', publicCode: 'PROMPT_CANCELLED',
      promptDelivery: { queueItemId: source.queueItemId, queueStatus: 'CANCELLED', deliveryMode: 'new-turn' },
    } : {
      commandId: id, status: 'rejected', publicCode: 'STEER_TARGET_TERMINAL',
      publicMessage: '目标任务已中断，引导未被接纳。',
      promptDelivery: { queueItemId: 'replacement', queueStatus: 'REJECTED', deliveryMode: 'steer' },
    });
    active.emit({ ...active.current(), isRunning: false, control: { active_turns: [] },
      eventSequence: 10, messages: [], queuedPrompts: [], queuedCount: 0 });
    await waitFor(() => expect(query).toHaveBeenCalledWith(source.commandId));
    expect(query.mock.calls.filter(([id]) => id === commandId)).toHaveLength(1);
    expect(screen.getByRole('article', { name: '引导' })).toBeTruthy();
    pendingQuery.resolve(accepted(commandId));
    await waitFor(() => expect(screen.queryByRole('article', { name: '引导' })).toBeNull());
    expect(query.mock.calls.filter(([id]) => id === commandId)).toHaveLength(2);
    expect(screen.getByText('目标任务已中断，引导未被接纳。')).toBeTruthy();
    expect(steer).toHaveBeenCalledTimes(1);
  });
});

function pr05Projection(busy = false): RuntimeProjection {
  return { ...projection('PR05'), hostSessionId: 'host:pr05', interaction: {
    id: 'interaction:pr05', kind: 'tool-confirmation', prompt: 'Allow terminal?', options: ['ALLOW', 'DENY'],
    expiresAtUtc: '2099-01-01T00:00:00Z', decisionInProgress: busy,
  } };
}

it.each([
  [false, 'PROTOCOL_TRANSPORT_CLOSED'], [true, 'PROTOCOL_TRANSPORT_CLOSED'],
  [false, 'INTERACTION_OUTCOME_UNKNOWN'], [true, 'INTERACTION_OUTCOME_UNKNOWN'],
] as const)('PR05 original command survives unknown ACK and card remount, projected busy=%s, code=%s', async (busy, code) => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pr05Projection();
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  const first = adapter.lastConnection!;
  const reconnects = code === 'PROTOCOL_TRANSPORT_CLOSED';
  first.resolveInteraction.mockRejectedValueOnce(new RuntimeApiError(code, 'lost ACK', reconnects));
  fireEvent.click(allow);
  await waitFor(() => expect(adapter.connectCalls).toHaveLength(reconnects ? 2 : 1));
  const submitted = first.resolveInteraction.mock.calls[0][1];
  expect(submitted.kind).toBe('tool');
  if (submitted.kind !== 'tool') throw new Error('expected tool command');
  expect(submitted.commandId).toMatch(/^command:[a-z2-7]{25}[aeimquy4]$/);
  const recovered = adapter.lastConnection!;
  await waitFor(() => expect(recovered.queryCommand).toHaveBeenCalledWith(submitted.commandId));
  expect(screen.getByRole('button', { name: /允许本次操作/ }).hasAttribute('disabled')).toBe(true);
  await act(async () => recovered.emit({ ...pr05Projection(busy), liveControlRevision: 4 }));
  fireEvent.click(screen.getByRole('button', { name: /允许本次操作/ }));
  if (recovered !== first) expect(recovered.resolveInteraction).not.toHaveBeenCalled();
  expect(first.resolveInteraction).toHaveBeenCalledTimes(1);
});

it('PR05 projection prevents deciding while another controller is writing or confirming', async () => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pr05Projection(true);
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  expect(allow.hasAttribute('disabled')).toBe(true);
  expect(await screen.findByText('正在处理确认')).toBeTruthy();
  fireEvent.click(allow);
  expect(adapter.lastConnection!.resolveInteraction).not.toHaveBeenCalled();
});

it('PR05 late tool decision stays with A while B retains its draft and buttons', async () => {
  const adapter = new FakeAdapter();
  adapter.sessions = [initialSession, { ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '另一个会话', live: false }];
  adapter.connectionValue = pr05Projection();
  const pending = deferred<CommandReceipt>();
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  const first = adapter.lastConnection!;
  first.resolveInteraction.mockImplementationOnce(() => pending.promise);
  fireEvent.click(allow);
  const submitted = first.resolveInteraction.mock.calls[0][1];
  if (submitted.kind !== 'tool') throw new Error('expected tool');
  fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
  await screen.findByRole('heading', { name: '另一个会话' });
  const composer = await typeComposer('B private draft');
  await act(async () => pending.resolve({ commandId: submitted.commandId, status: 'succeeded', publicCode: 'INTERACTION_ALLOW' }));
  expect(composer.textContent).toBe('B private draft');
  expect(screen.queryByText('已允许本次操作')).toBeNull();
  expect(adapter.connectCalls).toHaveLength(2);
});

it('PR05 queries a lost HTTP ACK before waiting for connection cleanup', async () => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pr05Projection();
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  const first = adapter.lastConnection!;
  const close = vi.spyOn(first, 'close').mockReturnValue(new Promise(() => {}));
  first.resolveInteraction.mockRejectedValueOnce(new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'HTTP ACK lost', true));
  first.queryCommand.mockImplementation(async commandId => ({ commandId, status: 'succeeded', publicCode: 'INTERACTION_ALLOW' }));
  fireEvent.click(allow);
  await screen.findByText('已允许本次操作');
  const submitted = first.resolveInteraction.mock.calls[0][1];
  if (submitted.kind !== 'tool') throw new Error('expected tool');
  expect(first.queryCommand).toHaveBeenCalledWith(submitted.commandId);
  expect(first.resolveInteraction).toHaveBeenCalledTimes(1);
  expect(adapter.connectCalls).toHaveLength(1);
  expect(close).not.toHaveBeenCalled();
});

it('PR05 confirmed non-acceptance permits a new explicit click on the same valid confirmation', async () => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pr05Projection();
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  const active = adapter.lastConnection!;
  active.resolveInteraction.mockRejectedValueOnce(new RuntimeApiError('INTERACTION_NOT_ACCEPTED', 'confirmed rollback', false));
  fireEvent.click(allow);
  await screen.findByText('这项选择没有被接受');
  expect(active.queryCommand).not.toHaveBeenCalled();
  await waitFor(() => expect(allow.hasAttribute('disabled')).toBe(false));
  const first = active.resolveInteraction.mock.calls[0][1];
  active.resolveInteraction.mockImplementationOnce(async (_, resolution) => {
    if (resolution.kind !== 'tool') throw new Error('expected tool');
    return {commandId: resolution.commandId!, status: 'succeeded', publicCode: 'INTERACTION_ALLOW'};
  });
  fireEvent.click(allow);
  await screen.findByText('已允许本次操作');
  expect(active.resolveInteraction).toHaveBeenCalledTimes(2);
  const second = active.resolveInteraction.mock.calls[1][1];
  if (first.kind !== 'tool' || second.kind !== 'tool') throw new Error('expected tool');
  expect(second.commandId).not.toBe(first.commandId);
  expect(adapter.connectCalls).toHaveLength(1);
});

it.each([false, true])('PR05 later unknown command recovers independently of old unresolved query, pending=%s', async pending => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = pr05Projection();
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  const allow = await screen.findByRole('button', { name: /允许本次操作/ });
  const active = adapter.lastConnection!;
  const blocked = deferred<CommandReceipt | undefined>();
  const queries = new Map<string, number>();
  let firstCommand: string | undefined;
  active.resolveInteraction.mockRejectedValue(new RuntimeApiError('SERVER_OPERATION_FAILED', 'unknown', false));
  active.queryCommand.mockImplementation(commandId => {
    firstCommand ??= commandId;
    const count = (queries.get(commandId) ?? 0) + 1;
    queries.set(commandId, count);
    if (count === 1) return Promise.resolve(undefined); // Direct post-ACK read.
    if (commandId === firstCommand) return pending ? blocked.promise : Promise.resolve(undefined);
    return Promise.resolve({commandId, status: 'succeeded', publicCode: 'INTERACTION_ALLOW'});
  });
  fireEvent.click(allow);
  await waitFor(() => expect(queries.get(firstCommand!)).toBe(2));
  const next = pr05Projection();
  if (next.interaction?.kind !== 'tool-confirmation') throw new Error('expected confirmation');
  next.interaction = {...next.interaction, id: 'interaction:next'};
  next.liveControlRevision = 8;
  next.eventSequence = 8;
  await act(async () => active.emit(next));
  if (pending) expect(queries.get(firstCommand!)).toBe(2); // One read in flight per command/connection.
  const nextAllow = screen.getByRole('button', { name: /允许本次操作/ });
  await waitFor(() => expect(nextAllow.hasAttribute('disabled')).toBe(false));
  fireEvent.click(nextAllow);
  await screen.findByText('已允许本次操作');
  const second = active.resolveInteraction.mock.calls[1][1];
  if (second.kind !== 'tool') throw new Error('expected tool');
  expect(queries.get(second.commandId!)).toBe(2);
  expect(active.resolveInteraction).toHaveBeenCalledTimes(2);
  await act(async () => blocked.resolve(undefined));
  const oldQueries = queries.get(firstCommand!);
  await act(async () => active.emit({...next, eventSequence: 9, liveControlRevision: 9}));
  expect(queries.get(firstCommand!)).toBe(oldQueries); // Ended old intent is retired.
  expect(nextAllow.hasAttribute('disabled')).toBe(true); // Accepted is never re-enabled by an empty read.
});


it.each(['sidebar', 'topbar', 'double-click'])('renames the session from %s and updates shared title displays', async (entry) => {
  const adapter = new FakeAdapter();
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByLabelText(`${initialSession.title} 更多操作`);
  if (entry === 'double-click') fireEvent.doubleClick(screen.getByRole('button', { name: new RegExp(initialSession.title + '当前会话') }));
  else if (entry === 'sidebar') fireEvent.click(screen.getByLabelText(`${initialSession.title} 更多操作`));
  else fireEvent.click(await screen.findByRole('button', { name: '更多会话操作' }));
  if (entry !== 'double-click') {
    const buttons = screen.getAllByRole('button', { name: '重命名' });
    fireEvent.click(buttons.at(-1)!);
  }
  const dialog = await screen.findByRole('dialog', { name: '重命名会话' });
  fireEvent.change(within(dialog).getByLabelText('会话标题'), { target: { value: '新的会话标题' } });
  fireEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  await waitFor(() => expect(screen.queryByRole('dialog', { name: '重命名会话' })).toBeNull());
  expect(adapter.renameSession).toHaveBeenCalledExactlyOnceWith(initialSession.id, '新的会话标题');
  expect(screen.getByLabelText('新的会话标题 更多操作')).toBeTruthy();
  expect(screen.getByRole('heading', { name: '新的会话标题' })).toBeTruthy();
});

it('replaces commands with session search and opens a result outside the loaded sidebar', async () => {
  const adapter = new FakeAdapter();
  const hidden = { ...initialSession, id: 'search-hidden', title: '搜索找到的旧会话', live: false };
  adapter.searchSessions.mockImplementation(async () => {
    adapter.sessions = [initialSession, hidden]; // Created elsewhere after the initial sidebar load.
    return { items: [{ session: hidden, matchKind: 'recent', snippet: '' }], nextCursor: null };
  });
  adapter.readSession.mockImplementation(async id => id === hidden.id ? hidden : initialSession);
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByLabelText(`${initialSession.title} 更多操作`);
  expect(screen.queryByRole('button', { name: '命令面板' })).toBeNull();
  expect(screen.queryByText(hidden.title)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /搜索会话/ }));
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(hidden.title) }));
  await waitFor(() => expect(adapter.connectCalls.some(call => call.sessionId === hidden.id)).toBe(true));
  expect(adapter.readSession).toHaveBeenCalledWith(hidden.id);
  expect(screen.queryByRole('dialog', { name: '搜索会话' })).toBeNull();
  expect(await screen.findByRole('heading', { name: hidden.title })).toBeTruthy();
  fireEvent.keyDown(window, { key: 'k', metaKey: true });
  expect(await screen.findByRole('dialog', { name: '搜索会话' })).toBeTruthy();
  fireEvent.keyDown(screen.getByLabelText('搜索关键词'), { key: 'Escape' });
  expect(screen.queryByRole('dialog', { name: '搜索会话' })).toBeNull();
});

describe('explicit runtime reopen owner', () => {
  it('submits before closing the controller and owns reconnect after a slow server detach', async () => {
    const adapter = new FakeAdapter();
    const connect = adapter.connect.bind(adapter);
    let disconnect!: (error: Error) => void;
    vi.spyOn(adapter, 'connect').mockImplementationOnce(async (...args) => {
      const connection = await connect(...args);
      vi.spyOn(connection, 'observe').mockImplementation(() => new Promise((_resolve, reject) => { disconnect = reject; }));
      return connection;
    });
    const accepted = deferred<{status:'reopened'}>();
    const reopen = vi.spyOn(adapter, 'reopenRuntime').mockImplementation(async () => {
      expect(adapter.lastConnection?.closed).toBe(false);
      return accepted.promise;
    });
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('heading', {name:'准备发布'});
    const original = adapter.lastConnection!;
    fireEvent.click(screen.getByRole('button', {name:'更多会话操作'}));
    fireEvent.click(screen.getByRole('button', {name:/重新加载会话/}));
    await waitFor(() => expect(reopen).toHaveBeenCalledOnce());
    await act(async () => {
      disconnect(new RuntimeApiError('DETACHED', 'connection detached', true));
      await new Promise(resolve => setTimeout(resolve, 550));
    });
    expect(adapter.connectCalls).toHaveLength(1);
    await act(async () => accepted.resolve({status:'reopened'}));
    await waitFor(() => expect(adapter.connectCalls).toHaveLength(2));
    expect(original.closed).toBe(true);
    expect((screen.getByRole('button', {name:'更多会话操作'}) as HTMLButtonElement).disabled).toBe(false);
  });
});

it('loads existing task groups for a cold history view without opening a runtime', async () => {
  const { CanonicalHistoryView } = await import('../../frontend/lib/runtime-adapter');
  const adapter = new FakeAdapter();
  const history = new CanonicalHistoryView('session:bwpogagzox7onyn52iumyctnkq', {outcome:'MISSING', path:'/tmp/project'},
    {session_id:'session:bwpogagzox7onyn52iumyctnkq', entries:[], control:{}, event_sequence_cut:'0'});
  vi.spyOn(adapter, 'connect').mockResolvedValue(history);
  vi.spyOn(adapter, 'workspaceAvailability').mockResolvedValue({outcome:'MISSING', path:'/tmp/project'});
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  expect(await screen.findByText('工作目录已丢失')).toBeTruthy();
  await waitFor(() => expect(adapter.listSessionTaskGroups).toHaveBeenCalledWith('session:bwpogagzox7onyn52iumyctnkq', undefined));
  expect(adapter.lastConnection).toBeUndefined();
  expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
});


it('opens the unique ACTIVE schedule from the session header and hides the alarm after it pauses', async () => {
  const task = { id: 'scheduled:header', session_id: initialSession.id, name: '会话活跃计划', prompt: '检查进展', status: 'ACTIVE' as const, revision: 1,
    next_run_at: '2026-10-13T00:00:00Z', timezone: 'Asia/Shanghai', permission_mode: 'bypass-permissions' as const,
    schedule: { contract: 'scheduled-rule:v1' as const, kind: 'daily' as const, start_date: '2026-10-13', time: '08:00' } };
  vi.spyOn(LocalScheduledTasksApi.prototype, 'listSessions').mockResolvedValue({sessions:[],next_cursor:null});
  const list = vi.spyOn(LocalScheduledTasksApi.prototype, 'list').mockResolvedValue({tasks:[task],next_cursor:null});
  try {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', { name: '查看此会话的活跃定时任务' }));
    expect(list).toHaveBeenCalledWith('ACTIVE', undefined, expect.any(AbortSignal), initialSession.id);
    const row = (await screen.findByRole('heading', { name: task.name })).closest('article')!;
    await waitFor(() => expect(document.activeElement).toBe(row));
    expect(row.classList.contains('is-highlighted')).toBe(true);
    list.mockResolvedValue({tasks:[],next_cursor:null});
    fireEvent.click(within(row).getByRole('button', { name: initialSession.title }));
    await screen.findByRole('heading', { name: initialSession.title });
    await waitFor(() => expect(screen.queryByRole('button', { name: '查看此会话的活跃定时任务' })).toBeNull());
  } finally { list.mockRestore(); }
});

it('publishes asynchronous reader readiness while observe is waiting and preserves the unsent draft', async () => {
  const adapter = new FakeAdapter();
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByText('我已经开始检查。');
  const active = adapter.lastConnection!;
  await typeComposer('保留草稿');
  await act(async () => active.publishRead(projection('正文读取完成')));
  expect(await screen.findByText('正文读取完成')).toBeTruthy();
  expect(screen.getByLabelText('发送给 Pulsara').textContent).toContain('保留草稿');
  expect(active.subscribe).toHaveBeenCalledTimes(1);
  expect(active.submitPrompt).not.toHaveBeenCalled();
  act(() => active.emit(projection('后续实时进展')));
  expect(await screen.findByText('后续实时进展')).toBeTruthy();
});

it.each(['loading', 'ready'] as const)('keeps known submitted text while grouping loads with a %s canonical body', async contentState => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = { ...projection(''), messages: [], isRunning: false, activeTurnId: undefined };
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByRole('heading', { name: '准备发布' });
  const active = adapter.lastConnection!;
  active.submitPrompt.mockImplementationOnce(async commandId => ({ commandId, status: 'pending', promptDelivery: {
    queueItemId: 'queue-body', queueStatus: 'PENDING', deliveryMode: 'new-turn',
  } }));
  await typeComposer('本地唯一的已知正文');
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledTimes(1));
  const commandId = active.submitPrompt.mock.calls[0][0];
  await waitFor(() => expect(screen.getByLabelText('发送给 Pulsara').textContent).toBe(''));
  await typeComposer('下一条尚未发送');
  const metadata: RuntimeProjection = { ...projection(''), eventSequence: 3, messages: [{
    id: 'entry-body', role: 'user', userKind: 'prompt', time: '现在', body: contentState === 'ready' ? '校验后的正式正文' : '', contentState, historyContextState:'loading',
    inputSource: { commandId, queueItemId: 'queue-body', deliveryMode: 'new-turn' },
  }] };
  act(() => active.emit(metadata));
  expect(await screen.findByText('本地唯一的已知正文')).toBeTruthy();
  expect(screen.getAllByText('本地唯一的已知正文')).toHaveLength(1);
  expect(screen.getByLabelText('发送给 Pulsara').textContent).toContain('下一条尚未发送');
  await act(async () => active.publishRead({ ...metadata, messages: [{ ...metadata.messages[0],
    body: '校验后的正式正文', promptContent: textPrompt('校验后的正式正文'), contentState: 'ready', historyContextState:'ready',
  }] }));
  expect(await screen.findByText('校验后的正式正文')).toBeTruthy();
  expect(screen.queryByText('本地唯一的已知正文')).toBeNull();
  expect(screen.getByLabelText('发送给 Pulsara').textContent).toContain('下一条尚未发送');
  expect(active.submitPrompt).toHaveBeenCalledTimes(1);
});

it.each(['command', 'queue'])('rejects mismatched %s receipts without fetching or replaying a consumed entry', async mismatch => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = { ...projection(''), messages: [], queuedCount: 1, queuedPrompts: [{
    queueItemId: 'queue-exact', commandId: 'command-exact', sequence: 1, status: 'pending',
    deliveryMode: 'new-turn', content: textPrompt('保留精确队列正文'),
  }] };
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByText('保留精确队列正文');
  const active = adapter.lastConnection!;
  const snapshot = vi.spyOn(active, 'snapshot');
  active.queryCommand.mockResolvedValue({ commandId: mismatch === 'command' ? 'wrong' : 'command-exact', status: 'succeeded',
    promptDelivery: { queueItemId: mismatch === 'queue' ? 'wrong' : 'queue-exact', queueStatus: 'CONSUMED',
      deliveryMode: 'new-turn', consumedEntryId: 'unrelated-entry' } });
  act(() => active.emit({ ...projection(''), messages: [], eventSequence: 2 }));
  await waitFor(() => expect(active.queryCommand).toHaveBeenCalledWith('command-exact'));
  expect(await screen.findByText('输入状态与原始输入身份不一致，请稍后重试核对。')).toBeTruthy();
  expect(screen.getByText('保留精确队列正文')).toBeTruthy();
  expect(active.locateEntry).not.toHaveBeenCalled();
  expect(snapshot).not.toHaveBeenCalled();
  expect(active.submitPrompt).not.toHaveBeenCalled();
});

it('loads the exact consumed entry without snapshot and cancels its read when the session changes', async () => {
  const adapter = new FakeAdapter();
  const second = { ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '另一个会话', live: false };
  adapter.sessions = [initialSession, second];
  adapter.connectionValues.set(second.id, projection('新会话的正文'));
  adapter.connectionValue = { ...projection(''), messages: [], queuedCount: 1, queuedPrompts: [{
    queueItemId: 'queue-consumed', commandId: 'command-consumed', sequence: 1, status: 'pending',
    deliveryMode: 'new-turn', content: textPrompt('待交接正文'),
  }] };
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByText('待交接正文');
  const active = adapter.lastConnection!;
  const snapshot = vi.spyOn(active, 'snapshot');
  const read = deferred<RuntimeProjection>();
  active.locateEntry.mockReturnValue(read.promise);
  active.queryCommand.mockResolvedValue({ commandId: 'command-consumed', status: 'succeeded', promptDelivery: {
    queueItemId: 'queue-consumed', queueStatus: 'CONSUMED', deliveryMode: 'new-turn', consumedEntryId: 'entry-exact',
  } });
  act(() => active.emit({ ...projection(''), messages: [], eventSequence: 2 }));
  await waitFor(() => expect(active.locateEntry).toHaveBeenCalledWith('entry-exact', expect.any(AbortSignal)));
  expect(snapshot).not.toHaveBeenCalled();
  const signal = active.locateEntry.mock.calls[0][1]!;
  act(() => active.emit(projection('读取正文期间仍有实时进展')));
  expect(await screen.findByText('读取正文期间仍有实时进展')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /另一个会话/ }));
  await screen.findByText('新会话的正文');
  expect(signal.aborted).toBe(true);
  await act(async () => { read.resolve(projection('迟到的旧正文')); active.publishRead(projection('旧订阅迟到')); });
  expect(screen.queryByText('迟到的旧正文')).toBeNull();
  expect(screen.queryByText('旧订阅迟到')).toBeNull();
  expect(screen.getByText('新会话的正文')).toBeTruthy();
  expect(active.submitPrompt).not.toHaveBeenCalled();
});

it('passes an explicit process reasoning read through the active session reader', async () => {
  const originalObserver = globalThis.IntersectionObserver;
  const observed: Array<{node: Element; notify: IntersectionObserverCallback}> = [];
  vi.stubGlobal('IntersectionObserver', class {
    constructor(private notify: IntersectionObserverCallback) {}
    observe(node: Element) { observed.push({node, notify:this.notify}); }
    disconnect() {}
  });
  try {
    const adapter = new FakeAdapter();
    const completed = projection('最后已核验的回复');
    adapter.connectionValue = {...completed, isRunning:false, activeTurnId:undefined, messages:[{
      ...completed.messages[0], assistantKind:'terminal', status:'completed', rootFinal:true, reasoningContentState:'unloaded',
    }]};
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByText('最后已核验的回复');
    const active = adapter.lastConnection!;
    expect(active.hydrateEntries).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button',{name:'展开处理过程'}));
    await waitFor(() => expect(active.readProcessEnd).toHaveBeenCalledWith('assistant-1', expect.any(AbortSignal)));
    await screen.findByRole('button', { name: '收起处理过程' });
    const thought = observed.find(item => item.node.getAttribute('data-reasoning-entry') === 'assistant-1') ?? observed[0];
    expect(thought).toBeTruthy();
    act(() => thought.notify([{target:thought.node,isIntersecting:true}] as IntersectionObserverEntry[],{} as IntersectionObserver));
    await waitFor(()=>expect(active.hydrateEntries).toHaveBeenCalledExactlyOnceWith(['assistant-1'],expect.any(AbortSignal),true));
    expect(active.submitPrompt).not.toHaveBeenCalled();
  } finally {
    vi.stubGlobal('IntersectionObserver',originalObserver);
  }
});


it('keeps the initial history context in the opening page while subscription and observe already run', async () => {
  const adapter = new FakeAdapter();
  const pending: RuntimeProjection = { ...projection('初始正文'), history: {
    initialReady: false, hasOlder: false, loading: true, windows: [], compactions: [],
  } };
  pending.messages = pending.messages.map(message => ({ ...message, historyContextState: 'loading' }));
  adapter.connectionValue = pending;
  const observe = vi.spyOn(FakeConnection.prototype, 'observe');
  try {
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByRole('region', { name: '会话加载页' });
    await waitFor(() => expect(observe).toHaveBeenCalledTimes(1));
    const active = adapter.lastConnection!;
    expect(active.subscribe).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('正在读取记录分组…')).toBeNull();
    expect(document.querySelector('[data-source-entry]')).toBeNull();
    expect(screen.queryByLabelText('发送给 Pulsara')).toBeNull();
    const ready: RuntimeProjection = { ...projection('初始正文'), history: { ...pending.history!, initialReady: true, loading: false } };
    await act(async () => active.publishRead(ready));
    expect(await screen.findByText('初始正文')).toBeTruthy();
    expect(screen.queryByRole('region', { name: '会话加载页' })).toBeNull();
    await typeComposer('后续刷新保留草稿');
    await act(async () => active.emit({ ...ready, history: { ...ready.history!, loading: true } }));
    expect(screen.queryByRole('region', { name: '会话加载页' })).toBeNull();
    expect(screen.getByLabelText('发送给 Pulsara').textContent).toContain('后续刷新保留草稿');
    expect(adapter.connectCalls).toHaveLength(1);
  } finally { observe.mockRestore(); }
});

it('retries only initial context from the opening error page and keeps the returning session draft', async () => {
  const adapter = new FakeAdapter();
  const other = { ...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '另一会话' };
  adapter.sessions.push(other);
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByText('我已经开始检查。');
  await typeComposer('恢复后保留的草稿');
  fireEvent.click(screen.getByRole('button', { name: /另一会话.*可恢复/ }));
  await waitFor(() => expect(adapter.lastConnection?.sessionId).toBe(other.id));
  await screen.findByLabelText('发送给 Pulsara');
  const failed: RuntimeProjection = { ...projection('重试后正文'), history: {
    initialReady: false, initialError: '首次分组读取失败', hasOlder: false, loading: false, windows: [], compactions: [],
  } };
  adapter.connectionValue = failed;
  fireEvent.click(screen.getByRole('button', { name: /准备发布/ }));
  const loading = await screen.findByRole('region', { name: '会话加载页' });
  await within(loading).findByText('首次分组读取失败');
  const active = adapter.lastConnection!;
  const ready: RuntimeProjection = { ...failed, history: { ...failed.history!, initialReady: true, initialError: undefined } };
  const read = deferred<RuntimeProjection>();
  active.readHistory.mockImplementationOnce(async () => read.promise);
  const connects = adapter.connectCalls.length;
  fireEvent.click(within(loading).getByRole('button', { name: '重试', exact: true }));
  await waitFor(() => expect(active.readHistory).toHaveBeenCalledExactlyOnceWith(undefined, undefined, expect.any(AbortSignal)));
  expect(active.subscribe).toHaveBeenCalledTimes(1);
  expect(adapter.connectCalls).toHaveLength(connects);
  await act(async () => read.resolve(ready));
  expect(await screen.findByText('重试后正文')).toBeTruthy();
  expect(screen.queryByRole('region', { name: '会话加载页' })).toBeNull();
  expect(screen.getByLabelText('发送给 Pulsara').textContent).toContain('恢复后保留的草稿');
  expect(adapter.connectCalls).toHaveLength(connects);
});

it('keeps independently opened group memberships and releases only the closed group', async () => {
  const adapter = new FakeAdapter();
  const base: AgentTask = { id: 'independent-a', batchId: 'batch-independent-a', label: '组甲', role: '研究', objective: '检查甲', status: 'running', parentId: 'turn-1', dependencyIds: [], completionAccepted: false, color: 'blue' };
  adapter.taskInventory = [base, { ...base, id: 'independent-b', batchId: 'batch-independent-b', label: '组乙', objective: '检查乙' }];
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByRole('heading', { name: '准备发布' });
  fireEvent.click(screen.getByRole('button', { name: '任务' }));
  fireEvent.click(await screen.findByRole('button', { name: /组甲/ }));
  await screen.findByRole('region', { name: '组甲' });
  fireEvent.click(screen.getByRole('tab', { name: '任务', exact: true }));
  fireEvent.click(await screen.findByRole('button', { name: /组乙/ }));
  await screen.findByRole('region', { name: '组乙' });
  const aReads = adapter.listSessionTasks.mock.calls.filter(call => call[2] === 'batch-independent-a').length;
  fireEvent.click(screen.getByRole('tab', { name: '组甲' }));
  await screen.findByRole('region', { name: '组甲' });
  expect(adapter.listSessionTasks.mock.calls.filter(call => call[2] === 'batch-independent-a')).toHaveLength(aReads);
  fireEvent.click(screen.getByRole('button', { name: '关闭 组乙' }));
  expect(screen.getByRole('region', { name: '组甲' })).toBeTruthy();
  adapter.taskInventory[0] = { ...base, status: 'completed' };
  await act(async () => adapter.lastConnection?.emit({ ...projection(''), eventSequence: 124, taskGroupInvalidations: ['batch-independent-a'] }));
  await waitFor(() => expect(within(screen.getByRole('region', { name: '组甲' })).getByRole('button', { name: /组甲.*已完成/ })).toBeTruthy());
  expect(screen.queryByRole('tab', { name: '组乙' })).toBeNull();
});


it('keeps desktop navigation visible when selecting or creating a session', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({matches:true, addEventListener:vi.fn(), removeEventListener:vi.fn()})));
  const adapter = new FakeAdapter();
  adapter.sessions.push({...initialSession,id:'session:desktop-layout',title:'Desktop other'});
  const view = renderWorkbench(<PulsaraApp adapter={adapter}/>);
  await screen.findByLabelText('发送给 Pulsara');
  expect(view.container.querySelector('.desktop-shell')?.classList.contains('sidebar-visible')).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:/Desktop other.*可恢复/}));
  await screen.findByRole('heading',{name:'Desktop other'});
  expect(view.container.querySelector('.desktop-shell')?.classList.contains('sidebar-visible')).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'创建快速开始会话'}));
  await screen.findByRole('heading',{name:'新会话'});
  expect(view.container.querySelector('.desktop-shell')?.classList.contains('sidebar-visible')).toBe(true);
});

it('starts new sessions with the workspace closed and remembers each session independently', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({matches:true, addEventListener:vi.fn(), removeEventListener:vi.fn()})));
  const adapter = new FakeAdapter();
  const other = { ...initialSession, id:'session:hk7ud7dvpyxqz4csc3z3hqksuq', title:'另一个会话' };
  adapter.sessions.push(other);
  let view = renderWorkbench(<PulsaraApp adapter={adapter}/>);
  await screen.findByRole('heading', {name:'准备发布'});
  const isOpen = () => view.container.querySelector('.desktop-workspace')!.classList.contains('is-open');
  expect(isOpen()).toBe(false);
  fireEvent.click(screen.getByRole('button', {name:'切换检查器'}));
  expect(isOpen()).toBe(true);
  fireEvent.click(screen.getByRole('button', {name:/另一个会话.*可恢复/}));
  await screen.findByRole('heading', {name:'另一个会话'});
  expect(isOpen()).toBe(false);
  fireEvent.click(screen.getByRole('button', {name:/准备发布.*已载入/}));
  await screen.findByRole('heading', {name:'准备发布'});
  expect(isOpen()).toBe(true);
  view.unmount();
  view = renderWorkbench(<PulsaraApp adapter={adapter}/>);
  await screen.findByRole('heading', {name:'准备发布'});
  expect(isOpen()).toBe(true);
  fireEvent.click(screen.getByRole('button', {name:'收起工作区'}));
  expect(uiPreferences.getItem(`pulsara-workspace-open:${initialSession.id}`)).toBe('false');
  fireEvent.click(screen.getByRole('button', {name:'创建快速开始会话'}));
  await screen.findByRole('heading', {name:'新会话'});
  expect(isOpen()).toBe(false);
  view.unmount();
});

it('remembers the left sidebar globally across sessions and remounts', async () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({matches:true, addEventListener:vi.fn(), removeEventListener:vi.fn()})));
  const adapter = new FakeAdapter();
  let view = renderWorkbench(<PulsaraApp adapter={adapter}/>);
  await screen.findByRole('heading', {name:'准备发布'});
  expect(view.container.querySelector('.sidebar-visible')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name:'切换会话侧栏'}));
  expect(view.container.querySelector('.sidebar-visible')).toBeNull();
  expect(uiPreferences.getItem('pulsara-sidebar-open')).toBe('false');
  view.unmount();
  view = renderWorkbench(<PulsaraApp adapter={adapter}/>);
  await screen.findByRole('heading', {name:'准备发布'});
  expect(view.container.querySelector('.sidebar-visible')).toBeNull();
  fireEvent.click(screen.getByRole('button', {name:'切换会话侧栏'}));
  expect(view.container.querySelector('.sidebar-visible')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name:'创建快速开始会话'}));
  await screen.findByRole('heading', {name:'新会话'});
  expect(view.container.querySelector('.sidebar-visible')).toBeTruthy();
  expect(uiPreferences.getItem('pulsara-sidebar-open')).toBe('true');
  view.unmount();
});

it('collapses the left sidebar at the drawer breakpoint but still allows manual reopening', async () => {
  const sidebarMedia = Object.assign(new EventTarget(), { matches: true });
  const remove = vi.spyOn(sidebarMedia, 'removeEventListener');
  vi.stubGlobal('matchMedia', vi.fn((query: string) => query === '(min-width: 1151px)' ? sidebarMedia : { matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const view = renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
  await screen.findByRole('heading', { name: '准备发布' });
  expect(view.container.querySelector('.sidebar-visible')).toBeTruthy();
  sidebarMedia.matches = false;
  act(() => sidebarMedia.dispatchEvent(Object.assign(new Event('change'), { matches: false })));
  expect(view.container.querySelector('.sidebar-visible')).toBeNull();
  expect(uiPreferences.getItem('pulsara-sidebar-open')).toBe('false');
  fireEvent.click(screen.getByRole('button', { name: '切换会话侧栏' }));
  expect(view.container.querySelector('.sidebar-visible')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '切换会话侧栏' }));
  sidebarMedia.matches = true;
  act(() => sidebarMedia.dispatchEvent(Object.assign(new Event('change'), { matches: true })));
  expect(view.container.querySelector('.sidebar-visible')).toBeNull();
  view.unmount();
  expect(remove).toHaveBeenCalledWith('change', expect.any(Function));
});


describe('user capability synchronization', () => {
  it('retires an earlier user GET when a successful mutation is adopted', async () => {
    const adapter = new FakeAdapter();
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', {name: '能力'}));
    await screen.findByText('Personal Tools');
    fireEvent.click(screen.getByRole('tab', {name: /技能/}));
    const stale = deferred<UserCapabilitySnapshot>();
    adapter.inspectUserCapabilities.mockImplementationOnce(() => stale.promise);
    fireEvent.click(screen.getByRole('button', {name: '刷新能力'}));
    await waitFor(() => expect(adapter.inspectUserCapabilities).toHaveBeenCalledTimes(2));
    adapter.inspectUserCapabilities.mockResolvedValue({...userCapabilitySnapshot, skills: {...userCapabilitySnapshot.skills,
      items: userCapabilitySnapshot.skills.items.map(skill => ({...skill, enabled: false})),
    }});
    fireEvent.click(screen.getByRole('switch', {name: '关闭 personal-pdf'}));
    await screen.findByRole('switch', {name: '开启 personal-pdf'});
    await act(async () => stale.resolve(userCapabilitySnapshot));
    expect(screen.getByRole('switch', {name: '开启 personal-pdf'})).toBeTruthy();
    expect((screen.getByRole('button', {name: '刷新能力'}) as HTMLButtonElement).disabled).toBe(false);
  });

  it('observes stable user services without any selected session and pauses while hidden', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [];
    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', {name: '能力'}));
    await screen.findByText('Personal Tools');
    fireEvent.click(screen.getByRole('tab', {name: /MCP/}));
    adapter.inspectUserCapabilities.mockResolvedValue({...userCapabilitySnapshot, mcp: {
      ...userCapabilitySnapshot.mcp,
      servers: userCapabilitySnapshot.mcp.servers.map(server => ({...server, status: 'failed-retryable' as const})),
    }});
    const visibility = vi.spyOn(document, 'visibilityState', 'get');
    vi.useFakeTimers();
    try {
      await act(async () => fireEvent.focus(window));
      expect(screen.getByText('需要重试')).toBeTruthy();
      const before = adapter.inspectUserCapabilities.mock.calls.length;
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(adapter.inspectUserCapabilities.mock.calls.length).toBe(before + 1);
      visibility.mockReturnValue('hidden');
      await act(async () => fireEvent(document, new Event('visibilitychange')));
      await act(async () => vi.advanceTimersByTimeAsync(60_000));
      expect(adapter.inspectUserCapabilities.mock.calls.length).toBe(before + 1);
      visibility.mockReturnValue('visible');
      await act(async () => fireEvent(document, new Event('visibilitychange')));
      expect(adapter.inspectUserCapabilities.mock.calls.length).toBe(before + 2);
      expect(adapter.connectCalls).toHaveLength(0);
    } finally {
      cleanup();
      vi.useRealTimers();
      visibility.mockRestore();
    }
  });

  it('keeps periodic reads serial and uses the existing fast settling cadence', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions = [];
    const connecting = {...userCapabilitySnapshot, mcp: {...userCapabilitySnapshot.mcp,
      servers: userCapabilitySnapshot.mcp.servers.map(server => ({...server, status: 'connecting' as const})),
    }};
    adapter.inspectUserCapabilities.mockResolvedValue(connecting);
    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', {name: '能力'}));
    await screen.findByText('Personal Tools');
    vi.useFakeTimers();
    try {
      await act(async () => fireEvent.focus(window));
      const before = adapter.inspectUserCapabilities.mock.calls.length;
      const pending = deferred<UserCapabilitySnapshot>();
      adapter.inspectUserCapabilities.mockReturnValueOnce(pending.promise);
      await act(async () => vi.advanceTimersByTimeAsync(650));
      expect(adapter.inspectUserCapabilities.mock.calls.length).toBe(before + 1);
      await act(async () => vi.advanceTimersByTimeAsync(10_000));
      expect(adapter.inspectUserCapabilities.mock.calls.length).toBe(before + 1);
      adapter.inspectUserCapabilities.mockResolvedValue(userCapabilitySnapshot);
      await act(async () => pending.resolve(userCapabilitySnapshot));
      await act(async () => vi.advanceTimersByTimeAsync(650));
      expect(adapter.inspectUserCapabilities.mock.calls.length).toBe(before + 1);
      await act(async () => vi.advanceTimersByTimeAsync(30_000 - 650));
      expect(adapter.inspectUserCapabilities.mock.calls.length).toBe(before + 2);
      fireEvent.click(screen.getByRole('button', {name: '总览'}));
      await act(async () => vi.advanceTimersByTimeAsync(60_000));
      expect(adapter.inspectUserCapabilities.mock.calls.length).toBe(before + 2);
    } finally { cleanup(); vi.useRealTimers(); }
  });

  it('adopts a user mutation after switching the selected session', async () => {
    const adapter = new FakeAdapter();
    adapter.sessions.push({...initialSession, id: 'session:hrluejkx3v2ajzf4ec6bb3ierm', title: '另一会话'});
    const mutation = deferred<Awaited<ReturnType<RuntimeAdapter['setUserSkillEnabled']>>>();
    adapter.setUserSkillEnabled.mockReturnValueOnce(mutation.promise);
    renderWorkbench(<PulsaraApp adapter={adapter} />);
    await screen.findByLabelText('发送给 Pulsara');
    fireEvent.click(screen.getByRole('button', {name: '能力'}));
    await screen.findByText('Personal Tools');
    fireEvent.click(screen.getByRole('tab', {name: /技能/}));
    fireEvent.click(screen.getByRole('switch', {name: '关闭 personal-pdf'}));
    fireEvent.click(screen.getByRole('button', {name: '会话', exact: true}));
    fireEvent.click(screen.getByRole('button', {name: /另一会话.*可恢复/}));
    await screen.findByRole('heading', {name: '另一会话'});
    fireEvent.click(screen.getByRole('button', {name: '能力'}));
    await screen.findByText('Personal Tools');
    adapter.inspectUserCapabilities.mockResolvedValue({...userCapabilitySnapshot, skills: {...userCapabilitySnapshot.skills,
      items: userCapabilitySnapshot.skills.items.map(skill => ({...skill, enabled: false})),
    }});
    await act(async () => mutation.resolve({operation: {status: 'DISABLED', success: true, message: '已关闭', details: []},
      capabilities: {...userCapabilitySnapshot, skills: {...userCapabilitySnapshot.skills,
        items: userCapabilitySnapshot.skills.items.map(skill => ({...skill, enabled: false})),
      }}}));
    fireEvent.click(screen.getByRole('tab', {name: /技能/}));
    expect(screen.getByRole('switch', {name: '开启 personal-pdf'})).toBeTruthy();
  });

  it.each(['save', 'external', 'remove'] as const)('retires old plugin authorization after %s replaces the connection', async source => {
    const adapter = new FakeAdapter();
    const auth = {type: 'oauth', redirect_uri: 'http://127.0.0.1:12345/callback'};
    const connection = {serverId: 'docs', credentialOwner: {kind: 'plugin' as const, scope_key: 'user', plugin_id: 'personal-tools', server_id: 'docs'},
      config: {transport: {type: 'streamable_http', endpoint: 'https://old.example/mcp'}, auth},
      defaults: {transport: {type: 'streamable_http', endpoint: 'https://old.example/mcp'}, auth}, overlay: null};
    const initial = {...userCapabilitySnapshot, plugins: {...userCapabilitySnapshot.plugins,
      items: userCapabilitySnapshot.plugins.items.map(plugin => ({...plugin, mcpConnections: [connection]})),
    }};
    const updated = {...initial, plugins: {...initial.plugins,
      items: source === 'remove' ? [] : initial.plugins.items.map(plugin => ({...plugin, mcpConnections: [{...connection,
        config: {...connection.config, transport: {type: 'streamable_http', endpoint: 'https://new.example/mcp'}},
        overlay: {endpoint: 'https://new.example/mcp'},
      }]})),
    }};
    adapter.inspectUserCapabilities.mockResolvedValue(initial);
    adapter.updatePluginConnection.mockResolvedValue({operation: {status: 'UPDATED', success: true, message: '已保存', details: []}, capabilities: updated});
    adapter.pluginMcpAuthorization.mockResolvedValue({state: 'authorized', error: null});
    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', {name: '能力'}));
    await screen.findByText('Personal Tools');
    const open = async () => {
      fireEvent.click(screen.getByRole('button', {name: '查看 Personal Tools'}));
      fireEvent.click(screen.getByRole('button', {name: '配置连接'}));
      await screen.findByRole('dialog', {name: '编辑 MCP 服务'});
    };
    await open();
    fireEvent.click(screen.getByRole('button', {name: '查看授权状态'}));
    await within(screen.getByRole('dialog', {name: '编辑 MCP 服务'})).findByText('已授权');
    adapter.inspectUserCapabilities.mockResolvedValue(updated);
    if (source === 'save') {
      fireEvent.change(screen.getByLabelText('服务地址'), {target: {value: 'https://new.example/mcp'}});
      fireEvent.click(screen.getByRole('button', {name: '保存连接'}));
    } else {
      fireEvent.click(screen.getByRole('button', {name: '取消', exact: true}));
      fireEvent.click(screen.getByRole('button', {name: '刷新能力'}));
    }
    await waitFor(() => expect(screen.queryByRole('dialog', {name: '编辑 MCP 服务'})).toBeNull());
    if (source !== 'remove') {
      // The drawer remains expanded after an edit; reopen its connection.
      fireEvent.click(await screen.findByRole('button', {name: '配置连接'}));
      await screen.findByRole('dialog', {name: '编辑 MCP 服务'});
      expect((screen.getByLabelText('服务地址') as HTMLInputElement).value).toBe('https://new.example/mcp');
      expect(within(screen.getByRole('dialog', {name: '编辑 MCP 服务'})).queryByText('已授权')).toBeNull();
    } else { expect(await screen.findByText('还没有安装用户插件。')).toBeTruthy(); }
  });

  it('keeps global memory, settings and creation available after selected-session failure', async () => {
    const adapter = new FakeAdapter();
    vi.spyOn(adapter, 'connect').mockRejectedValue(new RuntimeApiError('SESSION_BROKEN', '该会话连接失败', false));
    const projects = vi.spyOn(adapter.memory, 'projects').mockResolvedValue({items: [], next_cursor: null});
    vi.spyOn(adapter.memory, 'catalog').mockResolvedValue({items: [], next_cursor: null});
    const scheduled = vi.spyOn(LocalScheduledTasksApi.prototype, 'list').mockResolvedValue({tasks: [], next_cursor: null});
    render(<PulsaraApp adapter={adapter} />);
    await screen.findByText('本地服务已连接');
    expect((screen.getByRole('button', {name: '开始新任务'}) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole('button', {name: '记忆', exact: true}));
    await waitFor(() => expect(projects).toHaveBeenCalled());
    expect(screen.queryByText('本地服务连接失败')).toBeNull();
    fireEvent.click(screen.getByRole('button', {name: '定时任务', exact: true}));
    await waitFor(() => expect(scheduled).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', {name: '设置', exact: true}));
    expect((await screen.findByRole('button', {name: '重置数据'}) as HTMLButtonElement).disabled).toBe(false);
    scheduled.mockRestore();
  });

  it('retries failed global bootstrap from memory without opening the selected session', async () => {
    const adapter = new FakeAdapter();
    const boot = vi.spyOn(adapter, 'bootstrap').mockRejectedValueOnce(new RuntimeApiError('UNAVAILABLE', '暂时不可用', true));
    vi.spyOn(adapter.memory, 'projects').mockResolvedValue({items: [], next_cursor: null});
    vi.spyOn(adapter.memory, 'catalog').mockResolvedValue({items: [], next_cursor: null});
    render(<PulsaraApp adapter={adapter} />);
    fireEvent.click(await screen.findByRole('button', {name: '记忆', exact: true}));
    await screen.findByText('本地服务连接已中断');
    fireEvent.click(screen.getByRole('button', {name: '重新连接', exact: true}));
    await waitFor(() => expect(boot).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(adapter.memory.projects).toHaveBeenCalled());
    expect(adapter.connectCalls).toHaveLength(0);
    expect(screen.queryByText('本地服务连接已中断')).toBeNull();
  });
});

it('settles a consumed offscreen input before its resident tail retires, without replaying the local bubble on return', async () => {
  const adapter = new FakeAdapter();
  adapter.connectionValue = {...projection(''), messages:[], isRunning:false, activeTurnId:undefined};
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByLabelText('发送给 Pulsara');
  const active = adapter.lastConnection!;
  await typeComposer('远端已接受的旧输入');
  fireEvent.click(screen.getByRole('button',{name:'发送'}));
  await waitFor(() => expect(active.submitPrompt).toHaveBeenCalledOnce());
  const commandId = active.submitPrompt.mock.calls[0][0];
  const oldHistory = {readingOwner:{},atLatest:false,initialReady:true,hasOlder:false,loading:false,compactions:[],windows:[{
    id:'old-band',entryIds:['old'],firstEntryId:'old',lastEntryId:'old',hasOlder:false,hasNewer:true,loading:false,contextState:'ready' as const,
  }]};
  await act(async () => active.emit({...projection(''),eventSequence:20,isRunning:false,history:oldHistory,
    messages:[{id:'old',role:'assistant',assistantKind:'terminal',rootFinal:true,time:'',body:'正在翻阅的旧消息'}],
    canonicalInputs:[{id:'accepted-offscreen',inputSource:{commandId,queueItemId:'queue:accepted',deliveryMode:'new-turn'},contentState:'unloaded',historyContextState:'ready'}],
  }));
  expect(screen.getByText('正在翻阅的旧消息')).toBeTruthy();
  expect(screen.queryByText('远端已接受的旧输入')).toBeNull();
  // Subsequent retention has retired the already re-readable accepted input.
  const latest = {...projection('最新回复'),messages:[{id:'assistant-1',role:'assistant' as const,assistantKind:'terminal' as const,rootFinal:true,time:'',body:'最新回复'}],eventSequence:600,isRunning:false,canonicalInputs:[],
    history:{...oldHistory,readingOwner:{},atLatest:true,windows:[{...oldHistory.windows[0],id:'tail',entryIds:['assistant-1'],firstEntryId:'assistant-1',lastEntryId:'assistant-1',hasNewer:false}]}};
  Object.assign(active,{readLatestHistory:vi.fn(async () => latest)});
  fireEvent.click(screen.getByRole('button',{name:'回到最新'}));
  await screen.findByText('最新回复');
  expect(screen.queryByText('远端已接受的旧输入')).toBeNull();
  expect(document.querySelector('[data-pending-message]')).toBeNull();
});
