import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { pickLinuxWorkspaceDirectory, WorkspaceDirectoryPicker, type WorkspaceDirectoryApi, type WorkspaceDirectoryPage } from '../../frontend/components/workspace-directory-picker';

const root = '/home/user/workspaces';
const page = (extra: Partial<WorkspaceDirectoryPage> = {}): WorkspaceDirectoryPage => ({root, path:root, parent:null, entries:[], next_cursor:null, ...extra});
function api(): WorkspaceDirectoryApi {
  return {list:vi.fn(async () => page()), initialize:vi.fn(async () => ({path:root})), create:vi.fn(async (parent, name) => ({path:`${parent}/${name}`})), select:vi.fn(async path => ({path}))};
}
beforeEach(() => { HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) { this.open = true; }); });
afterEach(cleanup);

test('a missing root is created only by the explicit action, then selection is revalidated', async () => {
  const operations = api();
  vi.mocked(operations.list).mockRejectedValueOnce(Object.assign(new Error('工程根目录尚不存在'), {code:'WORKSPACE_ROOT_MISSING'}));
  const finish = vi.fn();
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={finish} />);
  fireEvent.click(await screen.findByRole('button', {name:'创建工程根目录'}));
  await screen.findByText(root);
  expect(operations.initialize).toHaveBeenCalledTimes(1);
  expect(operations.select).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name:'选择此目录'}));
  await waitFor(() => expect(finish).toHaveBeenCalledWith(root));
  expect(operations.select).toHaveBeenCalledWith(root, expect.any(AbortSignal));
});

test('paging retains existing entries and a rejected selection keeps the picker open', async () => {
  const operations = api();
  vi.mocked(operations.list).mockResolvedValueOnce(page({entries:[{name:'one',path:`${root}/one`}], next_cursor:'next'}))
    .mockResolvedValueOnce(page({entries:[{name:'two',path:`${root}/two`}]}));
  vi.mocked(operations.select).mockRejectedValueOnce(new Error('目录已被替换，请刷新'));
  const finish = vi.fn();
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={finish} />);
  fireEvent.click(await screen.findByRole('button', {name:'加载更多'}));
  await screen.findByRole('button', {name:'two'});
  expect(screen.getByRole('button', {name:'one'})).toBeTruthy();
  expect(operations.list).toHaveBeenLastCalledWith(root, 'next', expect.any(AbortSignal));
  fireEvent.click(screen.getByRole('button', {name:'选择此目录'}));
  await screen.findByRole('alert');
  expect(finish).not.toHaveBeenCalled();
});

test('cancel retires the mounted picker and aborts its in-flight user-level request', async () => {
  const operations = api();
  let requestSignal: AbortSignal | undefined;
  vi.mocked(operations.list).mockImplementation((_path, _cursor, signal) => {
    requestSignal = signal;
    return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason), {once:true}));
  });
  let picked!: Promise<string | null>;
  await act(async () => { picked = pickLinuxWorkspaceDirectory(operations); });
  expect(requestSignal?.aborted).toBe(false);
  await act(async () => { fireEvent.click(screen.getByRole('button', {name:'取消'})); });
  expect(await picked).toBeNull();
  expect(requestSignal?.aborted).toBe(true);
  expect(screen.queryByRole('dialog')).toBeNull();
});
