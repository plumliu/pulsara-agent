import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { pickLinuxWorkspaceDirectory, WorkspaceDirectoryPicker, type WorkspaceDirectoryApi, type WorkspaceDirectoryPage } from '../../frontend/components/workspace-directory-picker';

const root = '/home/user';
const page = (extra: Partial<WorkspaceDirectoryPage> = {}): WorkspaceDirectoryPage => ({root, path:root, parent:null, entries:[], next_cursor:null, ...extra});
function api(): WorkspaceDirectoryApi {
  return {list:vi.fn(async () => page()), create:vi.fn(async (parent, name) => ({path:`${parent}/${name}`})), select:vi.fn(async path => ({path}))};
}
beforeEach(() => { HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) { this.open = true; }); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('dragging the header stays in the viewport, releases capture and leaves action buttons clickable', async () => {
  vi.stubGlobal('PointerEvent', class extends MouseEvent {
    pointerId: number;
    constructor(type: string, init: PointerEventInit = {}) { super(type, init); this.pointerId = init.pointerId ?? 1; }
  });
  const operations = api(), finish = vi.fn();
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={finish} />);
  await screen.findByRole('status', {name:'所选文件夹'});
  const dialog = screen.getByRole('dialog'), header = dialog.querySelector('header')!;
  vi.spyOn(dialog, 'getBoundingClientRect').mockImplementation(() => new DOMRect(
    dialog.style.left ? parseFloat(dialog.style.left) : 200,
    dialog.style.top ? parseFloat(dialog.style.top) : 150, 500, 350));
  header.setPointerCapture = vi.fn();
  const pointer = (type: 'pointerDown' | 'pointerMove' | 'pointerUp' | 'pointerCancel', x: number, y: number) => {
    fireEvent[type](header, {button:0, pointerId:1, clientX:x, clientY:y});
  };
  pointer('pointerDown', 300, 170); pointer('pointerMove', 350, 220);
  expect([dialog.style.left, dialog.style.top]).toEqual(['250px', '200px']);
  pointer('pointerUp', 350, 220); pointer('pointerMove', 800, 700);
  expect([dialog.style.left, dialog.style.top]).toEqual(['250px', '200px']);
  pointer('pointerDown', 300, 220); pointer('pointerMove', -50, -50);
  expect([dialog.style.left, dialog.style.top]).toEqual(['0px', '0px']);
  pointer('pointerCancel', -50, -50); pointer('pointerMove', 1000, 1000);
  expect([dialog.style.left, dialog.style.top]).toEqual(['0px', '0px']);
  pointer('pointerDown', 50, 20); pointer('pointerMove', 10000, 10000);
  expect([dialog.style.left, dialog.style.top]).toEqual([`${innerWidth - 500}px`, `${innerHeight - 350}px`]);
  pointer('pointerUp', 10000, 10000);
  vi.stubGlobal('innerWidth', 600); vi.stubGlobal('innerHeight', 400);
  fireEvent(window, new Event('resize'));
  expect([dialog.style.left, dialog.style.top]).toEqual(['100px', '50px']);
  vi.mocked(header.setPointerCapture).mockClear();
  const refresh = screen.getByRole('button', {name:'刷新'});
  fireEvent.pointerDown(refresh.querySelector('svg')!, {button:0, pointerId:1});
  expect(header.setPointerCapture).not.toHaveBeenCalled();
  fireEvent.click(refresh);
  await waitFor(() => expect(operations.list).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole('button', {name:'关闭目录选择'}));
  expect(finish).toHaveBeenCalledWith(null);
});

test('the picker opens the existing home and selection is revalidated without creating a directory', async () => {
  const operations = api();
  const finish = vi.fn();
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={finish} />);
  await screen.findByRole('status', {name:'所选文件夹'});
  expect(operations.create).not.toHaveBeenCalled();
  expect(operations.select).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name:'选择文件夹'}));
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
  fireEvent.click(screen.getByRole('button', {name:'选择文件夹'}));
  await screen.findByRole('alert');
  expect(finish).not.toHaveBeenCalled();
});

test('a read failure exposes its cause and can be retried without creating a directory', async () => {
  const operations = api();
  vi.mocked(operations.list).mockRejectedValueOnce(new Error('目录读取失败'));
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={vi.fn()} />);
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '目录读取失败');
  expect(screen.queryByRole('button', {name:'取消', exact:true})).toBeNull();
  expect(screen.getByRole('button', {name:'关闭目录选择'})).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name:'重新读取'}));
  await screen.findByRole('status', {name:'所选文件夹'});
  expect(screen.queryByRole('alert')).toBeNull();
  expect(operations.create).not.toHaveBeenCalled();
});

test('a home read failure during browsing is shown without creating or replacing the home', async () => {
  const operations = api();
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={vi.fn()} />);
  await screen.findByRole('status', {name:'所选文件夹'});
  vi.mocked(operations.list).mockRejectedValueOnce(new Error('主目录暂时不可用'));
  fireEvent.click(screen.getByRole('button', {name:'刷新'}));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '主目录暂时不可用');
  expect(operations.create).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name:'刷新'}));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  await screen.findByRole('status', {name:'所选文件夹'});
});

test('single click selects a child and confirmation revalidates that child', async () => {
  const operations = api();
  vi.mocked(operations.list).mockResolvedValue(page({entries:[{name:'one',path:`${root}/one`}]}));
  const finish = vi.fn();
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={finish} />);
  fireEvent.click(await screen.findByRole('button', {name:'one'}));
  expect(operations.list).toHaveBeenCalledOnce();
  expect(operations.select).not.toHaveBeenCalled();
  expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value', `${root}/one`);
  fireEvent.click(screen.getByRole('button', {name:'选择文件夹'}));
  await waitFor(() => expect(finish).toHaveBeenCalledWith(`${root}/one`));
  expect(operations.select).toHaveBeenCalledWith(`${root}/one`, expect.any(AbortSignal));
});

test('double click enters a directory; back and forward restore the location without selecting it', async () => {
  const operations = api();
  vi.mocked(operations.list).mockImplementation(async path => path && path !== root ? page({path, parent:root}) : page({entries:[{name:'one',path:`${root}/one`}]}));
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={vi.fn()} />);
  const child = await screen.findByRole('button', {name:'one'});
  fireEvent.click(child); fireEvent.doubleClick(child);
  await waitFor(() => expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value', `${root}/one`));
  await waitFor(() => expect((screen.getByRole('button', {name:'后退'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', {name:'后退'}));
  await screen.findByRole('button', {name:'one'});
  expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value', root);
  fireEvent.click(screen.getByRole('button', {name:'前进'}));
  await waitFor(() => expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value', `${root}/one`));
  expect(operations.select).not.toHaveBeenCalled();
});

test('new folder opens an editor and only creates after confirmation', async () => {
  const operations = api();
  vi.mocked(operations.list).mockImplementation(async path => page(path ? {path,parent:root} : {}));
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name:'新建文件夹'}));
  fireEvent.change(screen.getByRole('textbox', {name:'新文件夹名称'}), {target:{value:'中文 项目'}});
  expect(operations.create).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name:'创建', exact:true}));
  await waitFor(() => expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value', `${root}/中文 项目`));
  expect(operations.create).toHaveBeenCalledWith(root,'中文 项目',expect.any(AbortSignal));
  expect(operations.select).not.toHaveBeenCalled();
});

test('native browse locates a validated Linux directory and final confirmation revalidates it', async () => {
  const operations = api();
  const path = `${root}/中文 项目`;
  operations.chooseNative = vi.fn(async () => ({path:'\\\\wsl.localhost\\Ubuntu\\home\\user\\中文 项目', linuxPath:path}));
  vi.mocked(operations.list).mockImplementation(async value => page(value ? {path:value,parent:root} : {}));
  const finish = vi.fn();
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={finish} />);
  fireEvent.click(await screen.findByRole('button', {name:'浏览…'}));
  await waitFor(() => expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value', path));
  expect(operations.list).toHaveBeenLastCalledWith(path, null, expect.any(AbortSignal));
  expect(operations.select).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name:'选择文件夹'}));
  await waitFor(() => expect(finish).toHaveBeenCalledWith(path));
  expect(operations.select).toHaveBeenCalledWith(path, expect.any(AbortSignal));
});

test('foreign native selections cover the toolbar and list; refresh clears the cover', async () => {
  const operations = api();
  const path = 'C:\\Users\\user\\Documents';
  operations.chooseNative = vi.fn(async () => ({path,linuxPath:null}));
  vi.mocked(operations.list).mockResolvedValue(page({entries:[{name:'one',path:`${root}/one`}]}));
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name:'浏览…'}));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', `该目录无效，请选择 ${root} 下的文件夹。`);
  expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value',path);
  expect(screen.getByRole('button', {name:'选择文件夹'})).toHaveProperty('disabled',true);
  fireEvent.click(screen.getByRole('button', {name:'选择文件夹'}));
  expect(operations.list).toHaveBeenCalledOnce();
  expect(operations.select).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', {name:'one'})).toBeNull();
  expect(screen.getByText('无法选择此目录')).toBeTruthy();
  expect(screen.queryByRole('button', {name:'新建文件夹'})).toBeNull();
  expect(screen.queryByRole('navigation', {name:'目录位置'})).toBeNull();
  expect(screen.getByRole('alert').parentElement).toBe(screen.getByRole('button', {name:'取消',exact:true}).parentElement);
  fireEvent.click(screen.getByRole('button', {name:'刷新'}));
  await screen.findByRole('button', {name:'one'});
  expect(screen.queryByRole('alert')).toBeNull();
  expect(screen.queryByText('无法选择此目录')).toBeNull();
  expect(screen.getByRole('button', {name:'选择文件夹'})).toHaveProperty('disabled',false);
  expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value',root);
});

test('Linux rejects an out-of-home native candidate; cancellation preserves the rejection and a valid browse clears it', async () => {
  const operations = api();
  operations.chooseNative = vi.fn().mockResolvedValueOnce({path:'\\\\wsl.localhost\\Ubuntu\\tmp',linuxPath:'/tmp'})
    .mockResolvedValueOnce(null).mockResolvedValueOnce({path:root,linuxPath:root});
  vi.mocked(operations.list).mockImplementation(async path => {
    if (path === '/tmp') throw new Error(`该目录无效，请选择 ${root} 下的文件夹。`);
    return page({entries:[{name:'one',path:`${root}/one`}]});
  });
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name:'浏览…'}));
  await screen.findByRole('alert');
  expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value','/tmp');
  expect(screen.getByRole('button', {name:'选择文件夹'})).toHaveProperty('disabled',true);
  expect(screen.queryByRole('button', {name:'one'})).toBeNull();
  expect(screen.getByText('无法选择此目录')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name:'浏览…'}));
  await waitFor(() => expect(screen.getByRole('button', {name:'浏览…'})).toHaveProperty('disabled',false));
  expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value','/tmp');
  expect(screen.getByRole('alert')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name:'浏览…'}));
  await waitFor(() => expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value',root));
  expect(screen.queryByRole('alert')).toBeNull();
  expect(screen.getByRole('button', {name:'选择文件夹'})).toHaveProperty('disabled',false);
});

test('cancelling native browse preserves the highlighted folder', async () => {
  const operations = api();
  operations.chooseNative = vi.fn(async () => null);
  vi.mocked(operations.list).mockResolvedValue(page({entries:[{name:'one',path:`${root}/one`}]}));
  render(<WorkspaceDirectoryPicker api={operations} signal={new AbortController().signal} finish={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name:'one'}));
  fireEvent.click(screen.getByRole('button', {name:'浏览…'}));
  await waitFor(() => expect(screen.getByRole('button', {name:'浏览…'})).toHaveProperty('disabled',false));
  expect(screen.getByRole('status', {name:'所选文件夹'})).toHaveProperty('value',`${root}/one`);
  expect(operations.list).toHaveBeenCalledOnce();
});

test('closing the picker discards a late native result without another Linux read or selection', async () => {
  const operations = api();
  let complete!: (value: {path:string;linuxPath:string}) => void;
  operations.chooseNative = vi.fn(() => new Promise(resolve => {complete = resolve;}));
  let picked!: Promise<string | null>;
  await act(async () => {picked = pickLinuxWorkspaceDirectory(operations);});
  fireEvent.click(screen.getByRole('button', {name:'浏览…'}));
  fireEvent.click(screen.getByRole('button', {name:'关闭目录选择'}));
  expect(await picked).toBeNull();
  await act(async () => {complete({path:root,linuxPath:root});});
  expect(operations.list).toHaveBeenCalledOnce();
  expect(operations.select).not.toHaveBeenCalled();
  expect(screen.queryByRole('dialog')).toBeNull();
});

test.each(['取消', '关闭目录选择'])('%s retires the mounted picker and aborts its user-level requests', async label => {
  const operations = api();
  let requestSignal: AbortSignal | undefined;
  vi.mocked(operations.list).mockImplementation((_path, _cursor, signal) => {
    requestSignal = signal;
    if (label === '取消') return Promise.resolve(page());
    return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason), {once:true}));
  });
  let picked!: Promise<string | null>;
  await act(async () => { picked = pickLinuxWorkspaceDirectory(operations); });
  expect(requestSignal?.aborted).toBe(false);
  await act(async () => { fireEvent.click(screen.getByRole('button', {name:label})); });
  expect(await picked).toBeNull();
  expect(requestSignal?.aborted).toBe(true);
  expect(screen.queryByRole('dialog')).toBeNull();
});
