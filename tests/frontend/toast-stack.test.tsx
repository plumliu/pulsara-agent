import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { useLayoutEffect } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { ToastStack } from '../../frontend/components/overlays';

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('rehomes a toast when its modal detaches before the popover layout effect', async () => {
  const modal = document.createElement('dialog');
  modal.open = true;
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
