import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ArchivedSessions } from '../../frontend/components/archived-sessions';
import type { RuntimeAdapter } from '../../frontend/lib/runtime-adapter';
import type { SessionSummary } from '../../frontend/lib/pulsara-types';

afterEach(cleanup);

function setup(count: number) {
  let items: SessionSummary[] = Array.from({ length: count }, (_, index) => ({
    id: `session-${index + 1}`, title: `归档会话 ${index + 1}`, subtitle: '',
    status: null, updatedAt: '刚刚', live: false, lifecycle: 'ARCHIVED',
  }));
  const listArchivedSessions = vi.fn(async () => items);
  const unarchiveSession = vi.fn(async (id: string) => {
    items = items.filter(item => item.id !== id);
    return { status: 'OPEN' as const, session_id: id };
  });
  const adapter = { listArchivedSessions, unarchiveSession } as unknown as RuntimeAdapter;
  const onRestored = vi.fn(async () => {});
  const onDelete = vi.fn();
  const props = { adapter, revision: 0, onRestored, onDelete };
  const view = render(<ArchivedSessions {...props} />);
  return { ...view, props, onRestored, onDelete, listArchivedSessions, unarchiveSession,
    remove: (id: string) => { items = items.filter(item => item.id !== id); } };
}

it('paginates all archived sessions in groups of five and keeps the page on refresh', async () => {
  const state = setup(12);
  await screen.findByText('归档会话 1');
  expect(screen.getAllByRole('listitem')).toHaveLength(5);
  expect(screen.getByText('1–5 / 共 12 项')).toBeTruthy();
  expect(screen.getByRole('button', { name: '上一页' })).toHaveProperty('disabled', true);
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  expect(screen.getByText('归档会话 6')).toBeTruthy();
  expect(screen.queryByText('归档会话 1')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  expect(screen.getAllByRole('listitem')).toHaveLength(2);
  expect(screen.getByText('11–12 / 共 12 项')).toBeTruthy();
  expect(screen.getByRole('button', { name: '下一页' })).toHaveProperty('disabled', true);
  fireEvent.click(screen.getByRole('button', { name: '刷新已归档会话' }));
  await screen.findByText('归档会话 11');
  expect(state.listArchivedSessions).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole('button', { name: '上一页' }));
  expect(screen.getByText('归档会话 6')).toBeTruthy();
});

it('returns to the previous page when restoring its last session and preserves delete targets', async () => {
  const state = setup(6);
  await screen.findByText('归档会话 1');
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  fireEvent.click(screen.getByRole('button', { name: '永久删除…' }));
  expect(state.onDelete).toHaveBeenCalledWith(expect.objectContaining({ id: 'session-6' }));
  fireEvent.click(screen.getByRole('button', { name: '取消归档' }));
  await screen.findByText('归档会话 1');
  await waitFor(() => expect(state.onRestored).toHaveBeenCalledOnce());
  expect(state.unarchiveSession).toHaveBeenCalledWith('session-6');
  expect(screen.getAllByRole('listitem')).toHaveLength(5);
  expect(screen.queryByRole('navigation', { name: '已归档会话分页' })).toBeNull();
});

it('clamps the page after a deletion revision and retains a failed restore for retry', async () => {
  const state = setup(6);
  await screen.findByText('归档会话 1');
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  state.remove('session-6');
  state.rerender(<ArchivedSessions {...state.props} revision={1} />);
  await screen.findByText('归档会话 1');
  state.unarchiveSession.mockRejectedValueOnce(new Error('恢复失败'));
  const row = screen.getAllByRole('listitem')[0];
  fireEvent.click(within(row).getByRole('button', { name: '取消归档' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '恢复失败');
  expect(screen.getByText('归档会话 1')).toBeTruthy();
  expect(within(row).getByRole('button', { name: '取消归档' })).toHaveProperty('disabled', false);
});

it('offers all pages as one snapshot and disables bulk deletion on stale or failed reads', async () => {
  const state = setup(12);
  const onDeleteAll = vi.fn();
  state.rerender(<ArchivedSessions {...state.props} onDeleteAll={onDeleteAll} />);
  await screen.findByText('归档会话 1');
  fireEvent.click(screen.getByRole('button', {name: '下一页'}));
  fireEvent.click(screen.getByRole('button', {name: '删除全部'}));
  expect(onDeleteAll.mock.calls[0][0]).toHaveLength(12);
  expect(onDeleteAll.mock.calls[0][0][0].id).toBe('session-1');
  state.listArchivedSessions.mockRejectedValueOnce(new Error('清单读取失败'));
  fireEvent.click(screen.getByRole('button', {name: '刷新已归档会话'}));
  expect(screen.getByRole('button', {name: '删除全部'})).toHaveProperty('disabled', true);
  await screen.findByText('清单读取失败');
  expect(screen.getByRole('button', {name: '删除全部'})).toHaveProperty('disabled', true);
  state.rerender(<ArchivedSessions {...state.props} onDeleteAll={onDeleteAll} deletionBusy />);
  expect(screen.getAllByRole('button', {name: '取消归档'}).every(button => (button as HTMLButtonElement).disabled)).toBe(true);
});
