import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import { ToastStack } from '../../frontend/components/overlays';
import { deferred, FakeAdapter, initialSession, renderWorkbench, userCapabilitySnapshot } from './pulsara-app-fixture';

const titles = () => [...document.querySelectorAll('.toast strong')].map(node => node.textContent);
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('queues every toast from the bottom and keeps a persistent item in place through progress updates', () => {
  const persistent = { id: -1, title: '正在删除归档会话 0/1', onClick: vi.fn() };
  const old = { id: 1, title: '旧通知' };
  const view = render(<ToastStack toasts={[old, persistent]} onDismiss={vi.fn()} />);
  const persistentNode = screen.getByText(persistent.title).closest('button');
  const incoming = Array.from({ length: 6 }, (_, i) => ({ id: i + 2, title: `新通知 ${i + 1}` }));
  // The application appends the live progress summary to its DTO. Arrival order
  // still places each newly created notification after the existing summary.
  view.rerender(<ToastStack toasts={[old, ...incoming, { ...persistent, title: '归档会话删除结果 1/1' }]} onDismiss={vi.fn()} />);
  expect(titles()).toEqual(['旧通知', '归档会话删除结果 1/1', ...incoming.map(toast => toast.title)]);
  expect(screen.getByText('归档会话删除结果 1/1').closest('button')).toBe(persistentNode);
  view.rerender(<ToastStack toasts={[{ ...persistent, title: '归档会话删除结果 1/1' }]} onDismiss={vi.fn()} />);
  expect(titles()).toEqual(['归档会话删除结果 1/1']);
  expect(screen.getByText('归档会话删除结果 1/1').closest('button')).toBe(persistentNode);
});

it('smoothly drops the persistent item after a lower toast leaves without replaying its entrance', async () => {
  const records: Array<{ element: HTMLElement; frames: Keyframe[] }> = [];
  const previousAnimate = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'animate');
  Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: vi.fn(function(this: HTMLElement, frames: Keyframe[]) {
    records.push({ element: this, frames });
    return { cancel: vi.fn(), finished: Promise.resolve() };
  }) });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function(this: HTMLElement) {
    if (!this.classList.contains('toast-stack__item')) return new DOMRect(700, 500, 300, 70);
    const siblings = [...this.parentElement!.children];
    return new DOMRect(700, 700 - siblings.length * 80 + siblings.indexOf(this) * 80, 300, 70);
  });
  try {
    const old = { id: 1, title: '上方暂态' }, persistent = { id: -1, title: '常驻提示' }, newest = { id: 2, title: '下方暂态' };
    const view = render(<ToastStack toasts={[old, persistent, newest]} onDismiss={vi.fn()} />);
    const node = screen.getByText('常驻提示').closest('button')!;
    const wrapper = node.closest('.toast-stack__item');
    view.rerender(<ToastStack toasts={[old, persistent]} onDismiss={vi.fn()} />);
    await waitFor(() => expect(screen.queryByText('下方暂态')).toBeNull());
    expect(records.find(record => record.element === wrapper)?.frames).toEqual([
      { transform: 'translateY(-80px)' }, { transform: 'none' },
    ]);
    view.rerender(<ToastStack toasts={[persistent]} onDismiss={vi.fn()} />);
    await waitFor(() => expect(screen.queryByText('上方暂态')).toBeNull());
    expect(screen.getByText('常驻提示').closest('button')).toBe(node);
    expect(records.filter(record => record.element === node)).toHaveLength(1);
  } finally {
    cleanup();
    if (previousAnimate) Object.defineProperty(HTMLElement.prototype, 'animate', previousAnimate);
    else Reflect.deleteProperty(HTMLElement.prototype, 'animate');
  }
});

it('retains more than three real application notifications until their own expiry', async () => {
  const adapter = new FakeAdapter();
  adapter.sessions = [];
  render(<PulsaraApp adapter={adapter} />);
  fireEvent.click(await screen.findByRole('button', { name: '能力' }));
  await waitFor(() => expect(adapter.inspectUserCapabilities).toHaveBeenCalled());
  vi.useFakeTimers();
  for (let i = 0; i < 6; i++) {
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '重新加载会话能力' })));
  }
  expect(adapter.refreshUserCapabilities).toHaveBeenCalledTimes(6);
  expect(document.querySelectorAll('.toast')).toHaveLength(6);
  await act(async () => vi.advanceTimersByTimeAsync(4200));
  expect(document.querySelectorAll('.toast')).toHaveLength(0);
});

it('keeps the archive deletion notice in the same queue without expiry; swiping clears only the notice through completion', async () => {
  const adapter = new FakeAdapter();
  adapter.archived = [{ ...initialSession, id: 'archived-toast', title: '归档提示测试', lifecycle: 'ARCHIVED', live: false }];
  const pending = deferred<Awaited<ReturnType<typeof adapter.deleteArchivedSession>>>();
  adapter.deleteArchivedSession.mockImplementation(() => pending.promise);
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  fireEvent.click(await screen.findByRole('button', { name: '设置' }));
  fireEvent.click(screen.getByRole('button', { name: '已归档会话', exact: true }));
  await screen.findByText('归档提示测试');
  fireEvent.click(screen.getByRole('button', { name: '删除全部' }));
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '永久删除' }));
  const notice = await screen.findByRole('button', { name: /正在删除归档会话/ });
  fireEvent.click(screen.getByRole('button', { name: '能力' }));
  await waitFor(() => expect(adapter.inspectUserCapabilities).toHaveBeenCalled());
  vi.useFakeTimers();
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '重新加载会话能力' })));
  expect(notice.closest('.toast-stack')).toBe(screen.getByText('会话能力已重新加载').closest('.toast-stack'));
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(screen.getByRole('button', { name: /正在删除归档会话/ })).toBe(notice);
  expect(document.querySelectorAll('.toast')).toHaveLength(1);
  vi.stubGlobal('PointerEvent', MouseEvent);
  notice.setPointerCapture = vi.fn(); notice.hasPointerCapture = vi.fn(() => true); notice.releasePointerCapture = vi.fn();
  vi.spyOn(notice, 'getBoundingClientRect').mockReturnValue(new DOMRect(700, 500, 300, 70));
  fireEvent.pointerDown(notice, { button: 0, clientX: 710, clientY: 520 });
  fireEvent.pointerMove(notice, { clientX: 820, clientY: 520 });
  expect(screen.queryByRole('button', { name: /正在删除归档会话/ })).toBeNull();
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(adapter.deleteArchivedSession).toHaveBeenCalledTimes(1);
  await act(async () => pending.resolve({ status: 'DELETED', session_id: 'archived-toast', cleanup_warning: null }));
  expect(screen.queryByRole('button', { name: /归档会话删除结果/ })).toBeNull();
  expect(adapter.deleteArchivedSession).toHaveBeenCalledTimes(1);
});
