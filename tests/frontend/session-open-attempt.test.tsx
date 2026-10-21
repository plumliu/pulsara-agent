import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {expect, it, vi} from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import {uiPreferences} from '../../frontend/lib/ui-preferences';
import {RuntimeApiError} from '../../frontend/lib/runtime-adapter';
import {deferred, FakeAdapter, initialSession, projection, renderWorkbench, typeComposer} from './pulsara-app-fixture';

function selectSession(title = initialSession.title) {
  const target = screen.getAllByRole('button').find(button => button.querySelector('strong')?.textContent === title);
  expect(target).toBeTruthy();
  fireEvent.click(target!);
}

it('shares the original background attempt with early navigation without publishing a premature active session', async () => {
  const adapter = new FakeAdapter(), pending = deferred<void>();
  const native = adapter.connect.bind(adapter);
  let close: ReturnType<typeof vi.spyOn> | undefined;
  const connect = vi.spyOn(adapter, 'connect').mockImplementation(async (...args) => {
    await pending.promise;
    const next = await native(...args);
    close = vi.spyOn(next, 'close');
    return next;
  });
  const startup = vi.fn();
  const view = render(<PulsaraApp adapter={adapter} onStartupSettled={startup}/>);
  await waitFor(() => expect(startup).toHaveBeenCalledTimes(1));
  expect(connect.mock.calls).toEqual([[initialSession.id, false]]);
  expect(screen.getByRole('button', {name:'总览',exact:true}).getAttribute('aria-current')).toBe('page');
  selectSession();
  await act(async () => {});
  expect(connect.mock.calls).toEqual([[initialSession.id, false]]);
  expect(screen.queryByRole('textbox', {name:'发送给 Pulsara'})).toBeNull();
  expect(adapter.lastConnection).toBeUndefined();
  expect(uiPreferences.getItem('pulsara-active-session')).toBeNull();
  expect(screen.getByRole('button', {current:'page',name:/准备发布/}).querySelector('small')?.textContent).toBe('正在载入');
  await act(async () => pending.resolve());
  expect((await screen.findByRole('textbox', {name:'发送给 Pulsara'})).getAttribute('contenteditable')).toBe('true');
  expect(connect).toHaveBeenCalledTimes(1);
  expect(close).not.toHaveBeenCalled();
  view.unmount();
  expect(close).toHaveBeenCalledTimes(1);
});

it('preserves different-target cancellation and closes a late superseded background connection', async () => {
  const adapter = new FakeAdapter(), pending = deferred<void>();
  const other = {...initialSession,id:'session:hrluejkx3v2ajzf4ec6bb3ierm',title:'另一会话'};
  adapter.sessions.push(other);
  const native = adapter.connect.bind(adapter);
  let close: ReturnType<typeof vi.spyOn> | undefined;
  const connect = vi.spyOn(adapter, 'connect').mockImplementation(async (...args) => {
    if (args[0] === initialSession.id) await pending.promise;
    const next = await native(...args);
    if (args[0] === initialSession.id) close = vi.spyOn(next, 'close');
    return next;
  });
  render(<PulsaraApp adapter={adapter}/>);
  await waitFor(() => expect(connect).toHaveBeenCalledTimes(1));
  selectSession(other.title);
  await screen.findByRole('textbox', {name:'发送给 Pulsara'});
  await act(async () => pending.resolve());
  expect(close).toHaveBeenCalledTimes(1);
  expect(connect.mock.calls).toEqual([[initialSession.id,false],[other.id,false]]);
  expect(screen.getByRole('heading', {name:other.title})).toBeTruthy();
});

it('allows a new retry after a failed shared attempt settles', async () => {
  const adapter = new FakeAdapter(), pending = deferred<void>();
  const native = adapter.connect.bind(adapter);
  const connect = vi.spyOn(adapter, 'connect').mockImplementationOnce(async () => {
    await pending.promise;
    throw new Error('恢复暂不可用');
  }).mockImplementation(native);
  render(<PulsaraApp adapter={adapter}/>);
  await waitFor(() => expect(connect).toHaveBeenCalledTimes(1));
  selectSession();
  expect(connect).toHaveBeenCalledTimes(1);
  await act(async () => pending.resolve());
  expect((await screen.findByRole('alert')).textContent).toContain('恢复暂不可用');
  selectSession();
  await screen.findByRole('textbox', {name:'发送给 Pulsara'});
  expect(connect).toHaveBeenCalledTimes(2);
});

it('still closes the actual late connection after unmounting a shared attempt', async () => {
  const adapter = new FakeAdapter(), pending = deferred<void>();
  const native = adapter.connect.bind(adapter);
  let close: ReturnType<typeof vi.spyOn> | undefined;
  const connect = vi.spyOn(adapter, 'connect').mockImplementation(async (...args) => {
    await pending.promise;
    const next = await native(...args);
    close = vi.spyOn(next, 'close');
    return next;
  });
  const view = render(<PulsaraApp adapter={adapter}/>);
  await waitFor(() => expect(connect).toHaveBeenCalledTimes(1));
  selectSession();
  expect(connect).toHaveBeenCalledTimes(1);
  view.unmount();
  await act(async () => pending.resolve());
  expect(close).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('textbox', {name:'发送给 Pulsara'})).toBeNull();
});

it.each(['takeover','reconnecting'] as const)('keeps a later normal open distinct from an in-flight %s intent', async intent => {
  const adapter = new FakeAdapter();
  adapter.connectionRole = intent === 'takeover' ? 'observer' : 'controller';
  adapter.connectionValue = {...projection(''), messages:[], isRunning:false, activeTurnId:undefined};
  renderWorkbench(<PulsaraApp adapter={adapter}/>);
  if (intent === 'takeover') await screen.findAllByRole('button', {name:'在此窗口继续'});
  else await screen.findByRole('textbox', {name:'发送给 Pulsara'});
  const first = adapter.lastConnection!;
  const pending = deferred<void>(), native = adapter.connect.bind(adapter);
  const opened: Awaited<ReturnType<typeof native>>[] = [];
  const connect = vi.spyOn(adapter, 'connect').mockImplementation(async (...args) => {
    await pending.promise;
    const next = await native(...args);
    opened.push(next);
    return next;
  });
  if (intent === 'takeover') fireEvent.click(screen.getAllByRole('button', {name:'在此窗口继续'})[0]);
  else {
    vi.spyOn(first, 'submitPrompt').mockRejectedValueOnce(new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE','lost ACK',true));
    await typeComposer('只发送一次');
    fireEvent.click(screen.getByRole('button', {name:'发送',exact:true}));
  }
  await waitFor(() => expect(connect).toHaveBeenCalledTimes(1));
  selectSession();
  await waitFor(() => expect(connect).toHaveBeenCalledTimes(2));
  expect(connect.mock.calls).toEqual([[initialSession.id,intent === 'takeover'],[initialSession.id,false]]);
  await act(async () => pending.resolve());
  await screen.findByRole('textbox', {name:'发送给 Pulsara'});
  expect(opened[0].closed).toBe(true);
  expect(opened[1].closed).toBe(false);
  if (intent === 'reconnecting') expect(first.submitPrompt).toHaveBeenCalledTimes(1);
});
