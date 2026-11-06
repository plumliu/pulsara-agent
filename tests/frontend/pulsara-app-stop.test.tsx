import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import type { CommandReceipt, UserControlCommandRef } from '../../frontend/lib/runtime-adapter';
import { RuntimeApiError } from '../../frontend/lib/runtime-adapter';
import { deferred, FakeAdapter, FakeConnection, initialSession, projection, renderWorkbench } from './pulsara-app-fixture';

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

async function start() {
  const adapter = new FakeAdapter();
  adapter.connectionValue = { ...projection(), hostSessionId: 'host:stop' };
  adapter.sessions.push({...initialSession, id: 'session:agtwi5siitkjqcaje6pp3y6ixu', title: '另一会话'});
  adapter.connectionValues.set('session:agtwi5siitkjqcaje6pp3y6ixu', {...projection('另一会话的正文'), hostSessionId: 'host:other'});
  const view = renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByLabelText('发送给 Pulsara');
  return { adapter, active: adapter.lastConnection!, view };
}
const clickStop = () => fireEvent.click(screen.getByRole('button', {name: '停止本轮运行'}));
const receipt = (reference: UserControlCommandRef, status: CommandReceipt['status'], publicCode?: string): CommandReceipt => ({
  commandId: reference.commandId, targetId: reference.targetId, status, publicCode,
});

describe('exact stop command lifecycle', () => {
  it('prepares a new reference without a cached deadline and coalesces preparation double clicks', async () => {
    const {active} = await start();
    const prepared = deferred<UserControlCommandRef>();
    const prepare = active.prepareUserControlCommand.getMockImplementation()!;
    active.prepareUserControlCommand.mockImplementationOnce(() => prepared.promise);
    const stop = vi.spyOn(active, 'stopActiveTurn');
    clickStop(); clickStop();
    expect(active.prepareUserControlCommand).toHaveBeenCalledTimes(1);
    expect(stop).not.toHaveBeenCalled();
    const target = active.prepareUserControlCommand.mock.calls[0][0];
    await act(async () => prepared.resolve(await prepare(target)));
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1));
    expect(stop.mock.calls[0][0]).toMatchObject({sessionId: active.sessionId, hostSessionId: 'host:stop', targetId: 'turn-1'});
  });

  it('queries a normally accepted stop through its final failure', async () => {
    const {active} = await start();
    vi.spyOn(active, 'stopActiveTurn').mockImplementation(async ref => receipt(ref, 'pending'));
    const query = vi.spyOn(active, 'queryControlCommand').mockImplementation(async ref => ({status: 'FOUND', receipt: {...receipt(ref, 'failed', 'CONTROL_FAILED'), publicMessage: '物理收尾尚未确认'}}));
    clickStop();
    expect(await screen.findByText('物理收尾尚未确认', {}, {timeout: 3000})).toBeTruthy();
    expect(query).toHaveBeenCalledTimes(1);
  });

  it('keeps an unavailable result queryable and only creates another command on explicit retry', async () => {
    const {active} = await start();
    const stop = vi.spyOn(active, 'stopActiveTurn').mockImplementation(async ref => receipt(ref, 'pending'));
    const query = vi.spyOn(active, 'queryControlCommand').mockResolvedValue({status: 'RESULT_UNAVAILABLE'});
    clickStop();
    await screen.findByRole('button', {name: '再次请求停止'}, {timeout: 3000});
    clickStop();
    expect(stop).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', {name: '再次请求停止'}));
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(2));
    expect(stop.mock.calls[0][0].commandId).not.toBe(stop.mock.calls[1][0].commandId);
    expect(query.mock.calls[0][0]).toEqual(stop.mock.calls[0][0]);
  });

  it('shows the actual rejection and allows another explicit stop', async () => {
    const {active} = await start();
    const stop = vi.spyOn(active, 'stopActiveTurn').mockImplementation(async ref => receipt(ref, 'rejected', 'CONTROL_REQUEST_EXPIRED'));
    clickStop();
    expect(await screen.findByText('本次停止请求已过期，请重新请求停止。')).toBeTruthy();
    expect(screen.queryByText('没有可停止的运行')).toBeNull();
    clickStop();
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(2));
  });

  it('prepares and settles the exact manual compaction command', async () => {
    const {active} = await start();
    await act(async () => active.emit({...active.current(), isRunning: false, activeTurnId: undefined, manualCompactionCommandId: 'command:manual'}));
    const rootStop = vi.spyOn(active, 'stopActiveTurn');
    const manualStop = vi.spyOn(active, 'stopContextCompaction').mockImplementation(async ref => receipt(ref, 'pending'));
    vi.spyOn(active, 'queryControlCommand').mockImplementation(async ref => ({status: 'FOUND', receipt: receipt(ref, 'succeeded', 'CONTROL_ALREADY_TERMINAL')}));
    clickStop();
    await screen.findByText('原目标已结束。');
    expect(rootStop).not.toHaveBeenCalled();
    expect(manualStop.mock.calls[0][0]).toMatchObject({operation: 'STOP_CONTEXT_COMPACTION', targetKind: 'MANUAL_COMPACTION', targetId: 'command:manual'});
  });

  it('does not dispatch a prepared stop after switching sessions', async () => {
    const {active} = await start();
    const prepared = deferred<UserControlCommandRef>();
    const prepare = active.prepareUserControlCommand.getMockImplementation()!;
    active.prepareUserControlCommand.mockImplementationOnce(() => prepared.promise);
    const stop = vi.spyOn(active, 'stopActiveTurn');
    clickStop();
    const target = active.prepareUserControlCommand.mock.calls[0][0];
    fireEvent.click(screen.getByRole('button', {name: /另一会话.*可恢复/}));
    await screen.findByText('另一会话的正文');
    await act(async () => prepared.resolve(await prepare(target)));
    expect(stop).not.toHaveBeenCalled();
  });

  it.each(['controller', 'observer'] as const)('stores a late final receipt and presents it on %s return', async role => {
    const {adapter, active} = await start();
    const ending = deferred<CommandReceipt>();
    const stop = vi.spyOn(active, 'stopActiveTurn').mockImplementation(() => ending.promise);
    clickStop();
    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole('button', {name: /另一会话.*可恢复/}));
    await screen.findByText('另一会话的正文');
    await act(async () => ending.resolve({...receipt(stop.mock.calls[0][0], 'failed'), publicMessage: '旧会话停止确认失败'}));
    expect(screen.queryByText('旧会话停止确认失败')).toBeNull();
    adapter.connectionRole = role;
    fireEvent.click(screen.getByRole('button', {name: /准备发布.*已载入/}));
    expect(await screen.findByText('旧会话停止确认失败')).toBeTruthy();
  });

  it('does not resurrect a result after the user ends observation', async () => {
    const {active} = await start();
    const ending = deferred<CommandReceipt>();
    const stop = vi.spyOn(active, 'stopActiveTurn').mockImplementation(() => ending.promise);
    clickStop();
    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole('button', {name: '结束结果观察'}));
    await act(async () => ending.resolve({...receipt(stop.mock.calls[0][0], 'failed'), publicMessage: '不应复活的结果'}));
    expect(screen.queryByText('不应复活的结果')).toBeNull();
    expect(screen.queryByRole('button', {name: '结束结果观察'})).toBeNull();
  });

  it('continues querying UNKNOWN at the read cadence until the original command is found', async () => {
    const {active} = await start();
    const stop = vi.spyOn(active, 'stopActiveTurn').mockImplementation(async ref => receipt(ref, 'pending'));
    const query = vi.spyOn(active, 'queryControlCommand').mockResolvedValueOnce({status: 'RESULT_UNAVAILABLE'})
      .mockImplementation(async ref => ({status: 'FOUND', receipt: receipt(ref, 'succeeded', 'ROOT_STOPPED')}));
    clickStop();
    await screen.findByRole('button', {name: '再次请求停止'});
    for (let eventSequence = 2; eventSequence < 15; eventSequence++) await act(async () => active.emit({...active.current(), eventSequence}));
    expect(query).toHaveBeenCalledTimes(1);
    expect(await screen.findByText('本轮运行已停止。', {}, {timeout: 3000})).toBeTruthy();
    expect(query).toHaveBeenCalledTimes(2);
    expect(stop).toHaveBeenCalledTimes(1);
    expect(query.mock.calls[1][0]).toEqual(stop.mock.calls[0][0]);
  });

  it('queries the original command after a same Host reconnect without sending another stop', async () => {
    const {adapter, active} = await start();
    const stop = vi.spyOn(active, 'stopActiveTurn').mockRejectedValue(new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'ACK丢失', true));
    const query = vi.spyOn(FakeConnection.prototype, 'queryControlCommand').mockImplementation(async ref => ({status: 'FOUND', receipt: receipt(ref, 'succeeded', 'ROOT_STOPPED')}));
    clickStop();
    await screen.findByText('本轮运行已停止。');
    expect(adapter.connectCalls).toHaveLength(2);
    expect(query.mock.calls[0][0]).toEqual(stop.mock.calls[0][0]);
    expect(adapter.lastConnection!.prepareUserControlCommand).not.toHaveBeenCalled();
    expect(stop).toHaveBeenCalledOnce();
  });

  it('does not let an old pending command disable stopping a later target', async () => {
    const {active} = await start();
    const stop = vi.spyOn(active, 'stopActiveTurn').mockImplementation(async ref => receipt(ref, 'pending'));
    vi.spyOn(active, 'queryControlCommand').mockImplementation(async ref => ({status: 'FOUND', receipt: receipt(ref, 'pending')}));
    clickStop();
    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
    await act(async () => active.emit({...active.current(), activeTurnId: 'turn:next'}));
    clickStop();
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(2));
    expect(stop.mock.calls.map(call => call[0].targetId)).toEqual(['turn-1', 'turn:next']);
  });

  it('releases a preparation failure and prepares a fresh command on the next click', async () => {
    const {active} = await start();
    active.prepareUserControlCommand.mockRejectedValueOnce(new Error('准备读取失败'));
    const stop = vi.spyOn(active, 'stopActiveTurn');
    clickStop();
    await screen.findByText('准备读取失败');
    expect(stop).not.toHaveBeenCalled();
    clickStop();
    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
    expect(active.prepareUserControlCommand).toHaveBeenCalledTimes(2);
  });

  it('keeps the clicked target when a successor appears during preparation', async () => {
    const {active} = await start();
    const prepared = deferred<UserControlCommandRef>();
    const prepare = active.prepareUserControlCommand.getMockImplementation()!;
    active.prepareUserControlCommand.mockImplementationOnce(() => prepared.promise);
    const stop = vi.spyOn(active, 'stopActiveTurn');
    clickStop();
    const target = active.prepareUserControlCommand.mock.calls[0][0];
    await act(async () => active.emit({...active.current(), activeTurnId: 'turn:successor'}));
    await act(async () => prepared.resolve(await prepare(target)));
    await waitFor(() => expect(stop).toHaveBeenCalledOnce());
    expect(stop.mock.calls[0][0].targetId).toBe('turn-1');
  });

  it('ends the old observation on a changed Host without querying or stopping the replacement', async () => {
    const {adapter, active} = await start();
    adapter.connectionValue = {...active.current(), hostSessionId: 'host:replacement'};
    const stop = vi.spyOn(active, 'stopActiveTurn').mockRejectedValue(new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'ACK丢失', true));
    const query = vi.spyOn(FakeConnection.prototype, 'queryControlCommand');
    clickStop();
    await screen.findByText('原会话运行实例已失效；不会把停止操作改投新的实例。');
    expect(adapter.connectCalls).toHaveLength(2);
    expect(query).not.toHaveBeenCalled();
    expect(adapter.lastConnection!.prepareUserControlCommand).not.toHaveBeenCalled();
    expect(stop).toHaveBeenCalledOnce();
  });

  it('presents owner loss instead of the older accepted receipt', async () => {
    const {active} = await start();
    vi.spyOn(active, 'stopActiveTurn').mockImplementation(async ref => ({...receipt(ref, 'pending', 'CONTROL_ACCEPTED'), publicMessage: '旧的已受理提示'}));
    vi.spyOn(active, 'queryControlCommand').mockResolvedValue({status: 'OWNER_UNAVAILABLE'});
    clickStop();
    expect(await screen.findByText('原 Host 已失效；不会把这次操作改投新的 Host。')).toBeTruthy();
    expect(screen.queryByText('旧的已受理提示')).toBeNull();
  });

  it('keeps a slow query single-flight across scheduled polls and releases it on unmount', async () => {
    const {active, view} = await start();
    vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout', 'performance']});
    vi.spyOn(active, 'stopActiveTurn').mockImplementation(async ref => receipt(ref, 'pending'));
    const answer = deferred<Awaited<ReturnType<typeof active.queryControlCommand>>>();
    const query = vi.spyOn(active, 'queryControlCommand').mockImplementation(() => answer.promise);
    await act(async () => clickStop());
    expect(query).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(3500));
    expect(query).toHaveBeenCalledOnce();
    view.unmount();
    await act(async () => answer.resolve({status: 'FOUND', receipt: {...receipt(query.mock.calls[0][0], 'failed'), publicMessage: '已释放的结果'}}));
    expect(screen.queryByText('已释放的结果')).toBeNull();
    expect(query).toHaveBeenCalledOnce();
  });
});
