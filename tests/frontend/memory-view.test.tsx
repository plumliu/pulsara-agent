import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryView } from '../../frontend/components/memory-view';
import { LocalMemoryApi, MemoryApiError, type MemoryRecord } from '../../frontend/lib/memory-api';
const record: MemoryRecord = { target: { kind: 'MEMORY', memory_id: 'memory:aaaaaaaaaaaaaaaaaaaaaaaaaa' }, context_kind: 'GLOBAL', kind: 'FACT', title: '项目运行环境', statement: '项目使用Python 3.12。', recorded_at: '2026-11-16T01:00:00.123456Z', updated_at: '2026-11-16T01:00:00.123457Z' };
const notify = vi.fn();
afterEach(() => { cleanup(); vi.restoreAllMocks(); notify.mockReset(); });
function setup(online = true) {
  const api = new LocalMemoryApi();
  vi.spyOn(api, 'projects').mockResolvedValue({ items: [{ workspace_id: 'workspace:aaaaaaaaaaaaaaaaaaaaaaaaaa', label: 'Pulsara', root: '/projects/pulsara', last_activity_at: null }], next_cursor: null });
  vi.spyOn(api, 'catalog').mockResolvedValue({ items: [record], next_cursor: null });
  vi.spyOn(api, 'detail').mockResolvedValue(record);
  vi.spyOn(api, 'create').mockResolvedValue({ ...record, title: '新记忆' });
  vi.spyOn(api, 'edit').mockResolvedValue({ record, changed: false });
  vi.spyOn(api, 'delete').mockResolvedValue({});
  render(<MemoryView api={api} onNotify={notify} runtimeStatus={online ? 'online' : 'offline'} databaseState="ready" onReconnect={vi.fn()} onOpenSettings={vi.fn()} />);
  return api;
}
it('shows useful memory text and scope without technical identifiers or concurrency fields', async () => {
  setup();
  fireEvent.click(await screen.findByRole('button', { name: /项目运行环境/ }));
  const panel = await screen.findByRole('complementary', { name: '记忆详情' });
  expect(within(panel).getByText(record.title)).toBeTruthy();
  expect(within(panel).getByText(record.statement)).toBeTruthy();
  expect(screen.queryByText(record.target.memory_id)).toBeNull();
  expect(screen.queryByText(record.updated_at)).toBeNull();
});
it('creates a preference with its complete title and empty body', async () => {
  const api = setup(); await screen.findByRole('button', { name: /项目运行环境/ });
  fireEvent.click(screen.getByRole('button', { name: '新增记忆' }));
  fireEvent.change(screen.getByLabelText('类别'), { target: { value: 'RESPONSE_PREFERENCE' } });
  fireEvent.change(screen.getByLabelText('完整回答偏好'), { target: { value: '先给结论，再给例子' } });
  expect(screen.queryByLabelText('正文')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '保存记忆' }));
  await waitFor(() => expect(api.create).toHaveBeenCalledWith({ view: 'global', workspace_id: null }, { kind: 'RESPONSE_PREFERENCE', title: '先给结论，再给例子', statement: '' }));
});
it('edits only body and scope while retaining the displayed concurrency basis', async () => {
  const api = setup();
  fireEvent.click(await screen.findByRole('button', { name: /项目运行环境/ }));
  fireEvent.click(await screen.findByRole('button', { name: '编辑记忆' }));
  expect(screen.queryByLabelText('标题')).toBeNull(); expect(screen.queryByLabelText('类别')).toBeNull();
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: '  新正文\r\n第二行  ' } });
  fireEvent.change(screen.getByLabelText('保存范围'), { target: { value: 'workspace:aaaaaaaaaaaaaaaaaaaaaaaaaa' } });
  fireEvent.click(screen.getByRole('button', { name: '保存记忆' }));
  await waitFor(() => expect(api.edit).toHaveBeenCalledWith({ view: 'global', workspace_id: null }, record, { statement: '新正文\n第二行', destination: { view: 'project', workspace_id: 'workspace:aaaaaaaaaaaaaaaaaaaaaaaaaa' } }));
});
it('keeps the edit draft after a real concurrency conflict', async () => {
  const api = setup(); vi.mocked(api.edit).mockRejectedValue(new MemoryApiError('记忆已变化，请重新查询', 409));
  fireEvent.click(await screen.findByRole('button', { name: /项目运行环境/ }));
  fireEvent.click(await screen.findByRole('button', { name: '编辑记忆' }));
  fireEvent.change(screen.getByLabelText('正文'), { target: { value: '我的草稿' } });
  fireEvent.click(screen.getByRole('button', { name: '保存记忆' }));
  await waitFor(() => expect(notify).toHaveBeenCalledWith('记忆未能保存', '记忆已变化，请重新查询', 'warning'));
  expect((screen.getByLabelText('正文') as HTMLTextAreaElement).value).toBe('我的草稿'); expect(api.edit).toHaveBeenCalledOnce();
});
it('deletes the one selected memory after a plain confirmation', async () => {
  const api = setup(); fireEvent.click(await screen.findByRole('button', { name: /项目运行环境/ }));
  fireEvent.click(await screen.findByRole('button', { name: '删除记忆' }));
  fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
  await waitFor(() => expect(api.delete).toHaveBeenCalledWith({ view: 'global', workspace_id: null }, record));
});
it('does not load memory while the user-level service is offline', () => {
  const api = setup(false); expect(api.catalog).not.toHaveBeenCalled(); expect(api.projects).not.toHaveBeenCalled();
});
