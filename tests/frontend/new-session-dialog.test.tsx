import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { NewSessionDialog } from '../../frontend/components/overlays';

afterEach(cleanup);

function setup(onPickDirectory: (path: string, signal: AbortSignal) => Promise<string | null>) {
  const onCreate = vi.fn(async () => true);
  const view = render(<NewSessionDialog open canCreateSession
    onClose={() => {}} onCreate={onCreate} onPickDirectory={onPickDirectory} />);
  fireEvent.click(screen.getByRole('radio', { name: /指定目录/ }));
  return { ...view, onCreate, preview: screen.getByRole('textbox', { name: '目录路径' }) as HTMLInputElement };
}

it('requires an explicit directory selection and preserves it on cancellation or failure', async () => {
  const pick = vi.fn<(path: string, signal: AbortSignal) => Promise<string | null>>()
    .mockResolvedValueOnce(null).mockRejectedValueOnce(new Error('无法打开系统窗口'))
    .mockResolvedValueOnce('/tmp/文件夹 "quoted" ').mockResolvedValueOnce(null);
  const { preview, onCreate } = setup(pick);
  expect(preview.readOnly).toBe(true);
  expect(preview.value).toBe('');
  expect((screen.getByRole('button', { name: /^创建会话/ }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
  expect(onCreate).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
  await screen.findByRole('button', { name: '选择目录' });
  expect(preview.value).toBe('');
  expect(pick).toHaveBeenCalledWith('', expect.any(AbortSignal));
  expect(screen.queryByRole('alert')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
  expect((await screen.findByRole('alert')).textContent).toBe('无法打开系统窗口');
  expect(preview.value).toBe('');
  fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
  await waitFor(() => expect(preview.value).toBe('/tmp/文件夹 "quoted" '));
  expect(screen.queryByRole('alert')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
  await screen.findByRole('button', { name: '选择目录' });
  expect(preview.value).toBe('/tmp/文件夹 "quoted" ');
  fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
  expect(onCreate).toHaveBeenCalledWith({ kind: 'project', path: '/tmp/文件夹 "quoted" ' });
});

it('blocks duplicate selection and submission while pending and ignores results after closing', async () => {
  let resolve!: (path: string) => void;
  const pick = vi.fn<(path: string, signal: AbortSignal) => Promise<string>>()
    .mockImplementation(() => new Promise<string>((done) => { resolve = done; }));
  const { unmount, onCreate } = setup(pick);
  fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
  const choosing = screen.getByRole('button', { name: '正在选择…' }) as HTMLButtonElement;
  expect(choosing.disabled).toBe(true);
  fireEvent.click(choosing);
  fireEvent.click(screen.getByRole('button', { name: /^创建会话/ }));
  expect(pick).toHaveBeenCalledTimes(1);
  expect(onCreate).not.toHaveBeenCalled();
  unmount();
  expect(pick.mock.calls[0][1].aborted).toBe(true);
  const next = setup(async () => null);
  await act(async () => resolve('/tmp/late-selection'));
  expect(next.preview.value).toBe('');
});
