import { act, fireEvent, screen, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import { deriveIdentity } from '../../frontend/lib/identity';
import { FakeAdapter, initialSession, projection, renderWorkbench } from './pulsara-app-fixture';

const { workbenchRender } = vi.hoisted(() => ({ workbenchRender: vi.fn<(id: string) => void>() }));

vi.mock('../../frontend/components/workbench-view', async importOriginal => {
  const actual = await importOriginal<typeof import('../../frontend/components/workbench-view')>();
  return {
    ...actual,
    WorkbenchView: (props: Parameters<typeof actual.WorkbenchView>[0]) => {
      workbenchRender(props.session.id);
      return <actual.WorkbenchView {...props} />;
    },
  };
});

it('isolates unaffected workbenches when switching tabs and recalling a minimized tab', async () => {
  const adapter = new FakeAdapter();
  const [second, third, fourth, fifth, sixth] = ['乙', '丙', '丁', '戊', '己'].map(name => ({
    ...initialSession,
    id: deriveIdentity('session', 'canvas-render', name),
    title: `${name}会话`,
  }));
  adapter.sessions.push(second, third, fourth, fifth, sixth);
  const tab = (title: string) => screen.getByRole('tab', { name: new RegExp(title) });
  const sessionWindow = (id: string) => document.getElementById(id) as HTMLElement;
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByRole('tab', { name: /准备发布/ });
  const firstConnection = adapter.lastConnection!;
  const sidebar = within(screen.getByRole('region', { name: '会话目录' }));

  for (const session of [second, third, fourth, fifth, sixth]) {
    await act(async () => fireEvent.click(sidebar.getByRole('button', { name: new RegExp(session.title) })));
    expect(tab(session.title).getAttribute('aria-selected')).toBe('true');
  }
  expect(sessionWindow(initialSession.id).hidden).toBe(true);
  expect(sessionWindow(second.id).hidden).toBe(true);

  workbenchRender.mockClear();
  await act(async () => fireEvent.click(tab(fourth.title)));
  expect(tab(fourth.title).getAttribute('aria-selected')).toBe('true');
  expect(workbenchRender).not.toHaveBeenCalledWith(initialSession.id);
  expect(workbenchRender).not.toHaveBeenCalledWith(second.id);
  expect(workbenchRender).not.toHaveBeenCalledWith(third.id);
  expect(workbenchRender).not.toHaveBeenCalledWith(fifth.id);

  workbenchRender.mockClear();
  await act(async () => fireEvent.click(tab(second.title)));
  expect(sessionWindow(second.id).hidden).toBe(false);
  expect(sessionWindow(third.id).hidden).toBe(true);
  expect(workbenchRender).not.toHaveBeenCalledWith(initialSession.id);
  expect(workbenchRender).not.toHaveBeenCalledWith(fifth.id);
  expect(workbenchRender).not.toHaveBeenCalledWith(sixth.id);

  await act(async () => firstConnection.emit({ ...projection('收起后收到的输出'), eventSequence: 2 }));
  await act(async () => fireEvent.click(tab(initialSession.title)));
  expect(within(sessionWindow(initialSession.id)).getByText('收起后收到的输出')).toBeTruthy();
  expect(adapter.connectCalls).toHaveLength(6);
});
