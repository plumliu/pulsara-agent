import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useLayoutEffect } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ToastStack } from '../../frontend/components/overlays';

// jsdom lacks showModal; mark only fixtures intended to represent native modals.
beforeEach(() => {
  const matches = HTMLElement.prototype.matches;
  vi.spyOn(HTMLElement.prototype, 'matches').mockImplementation(function (this: HTMLElement, selector: string) {
    return selector === ':modal' ? this instanceof HTMLDialogElement && this.open && this.hasAttribute('data-test-modal') : matches.call(this, selector);
  });
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each(['remove', 'close'])('preserves an existing toast and its animation when a modal %ss', async (action) => {
  const modal = document.createElement('dialog');
  modal.open = true;
  modal.setAttribute('data-test-modal', '');
  document.body.append(modal);
  const cancel = vi.fn();
  const animate = vi.fn(() => ({ cancel, finished: Promise.resolve() }));
  const originalAnimate = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'animate');
  Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: animate });
  const dismiss = vi.fn();
  try {
    const view = render(<ToastStack toasts={[{ id: 1, title: '连接测试通过' }]} onDismiss={dismiss} />);
    const toast = (await screen.findByText('连接测试通过')).closest('button')!;
    expect(modal.contains(toast)).toBe(true);
    expect(animate).toHaveBeenCalledOnce();
    await act(async () => { if (action === 'remove') modal.remove(); else modal.open = false; });
    await waitFor(() => expect(modal.contains(toast)).toBe(false));
    expect(screen.getByText('连接测试通过').closest('button')).toBe(toast);
    expect(animate).toHaveBeenCalledOnce();
    expect(cancel).not.toHaveBeenCalled();
    expect(dismiss).not.toHaveBeenCalled();
    fireEvent.click(toast);
    expect(dismiss).toHaveBeenCalledExactlyOnceWith(1);
    view.rerender(<ToastStack toasts={[]} onDismiss={dismiss} />);
    await waitFor(() => expect(screen.queryByText('连接测试通过')).toBeNull());
    expect(cancel).toHaveBeenCalledTimes(2);
  } finally {
    cleanup();
    modal.remove();
    if (originalAnimate) Object.defineProperty(HTMLElement.prototype, 'animate', originalAnimate);
    else Reflect.deleteProperty(HTMLElement.prototype, 'animate');
  }
});

it('rehomes a toast when its modal detaches before the popover layout effect', async () => {
  const modal = document.createElement('dialog');
  modal.open = true;
  modal.setAttribute('data-test-modal', '');
  document.body.append(modal);
  const showPopover = vi.fn(function (this: HTMLElement) {
    if (!this.isConnected) throw new DOMException('Disconnected popover', 'InvalidStateError');
  });
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} });
  const originalShow = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'showPopover');
  Object.defineProperty(HTMLElement.prototype, 'showPopover', { configurable: true, value: showPopover });
  function DetachModal() {
    useLayoutEffect(() => { modal.remove(); }, []);
    return null;
  }
  try {
    render(<><DetachModal /><ToastStack toasts={[{ id: 1, title: '会话已创建', tone: 'success' }]} onDismiss={() => {}} /></>);
    await screen.findByText('会话已创建');
    await waitFor(() => expect(showPopover).toHaveBeenCalledOnce());
    expect(showPopover.mock.contexts[0].isConnected).toBe(true);
    expect(document.querySelectorAll('.toast-stack')).toHaveLength(1);
  } finally {
    cleanup();
    modal.remove();
    if (originalShow) Object.defineProperty(HTMLElement.prototype, 'showPopover', originalShow);
    else Reflect.deleteProperty(HTMLElement.prototype, 'showPopover');
  }
});

it('ignores inline open dialogs and selects an actual modal behind them', async () => {
  const inline = document.createElement('dialog');
  inline.open = true;
  const modal = document.createElement('dialog');
  modal.open = true;
  modal.setAttribute('data-test-modal', '');
  document.body.append(inline, modal);
  try {
    render(<ToastStack toasts={[{ id: 1, title: '输入已接受' }]} onDismiss={() => {}} />);
    const toast = (await screen.findByText('输入已接受')).closest('button')!;
    expect(inline.contains(toast)).toBe(false);
    expect(modal.contains(toast)).toBe(true);
    await act(async () => { modal.open = false; });
    await waitFor(() => expect(modal.contains(toast)).toBe(false));
    expect(inline.contains(toast)).toBe(false);
    expect(document.body.contains(toast)).toBe(true);
  } finally {
    cleanup(); inline.remove(); modal.remove();
  }
});
