import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ToastStack } from '../../frontend/components/overlays';
import type { ToastMessage } from '../../frontend/lib/pulsara-types';

const openedPopovers = new WeakSet<HTMLElement>();
const showPopover = vi.fn(function (this: HTMLElement) {
  if (!this.isConnected) throw new DOMException('Disconnected popover', 'InvalidStateError');
  openedPopovers.add(this);
  this.style.display = 'block';
});
const hidePopover = vi.fn(function (this: HTMLElement) {
  if (!this.isConnected) throw new DOMException('Disconnected popover', 'InvalidStateError');
  openedPopovers.delete(this);
  this.style.display = 'none';
});

beforeEach(() => {
  const nativeMatches = HTMLElement.prototype.matches;
  vi.spyOn(HTMLElement.prototype, 'matches').mockImplementation(function (this: HTMLElement, selector: string) {
    return selector === ':popover-open' ? openedPopovers.has(this) : nativeMatches.call(this, selector);
  });
  Object.defineProperty(HTMLElement.prototype, 'showPopover', { configurable: true, value: showPopover });
  Object.defineProperty(HTMLElement.prototype, 'hidePopover', { configurable: true, value: hidePopover });
});
afterEach(() => {
  cleanup();
  Reflect.deleteProperty(HTMLElement.prototype, 'showPopover');
  Reflect.deleteProperty(HTMLElement.prototype, 'hidePopover');
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

function SavingDialog({ existingToast = false }: { existingToast?: boolean }) {
  const [editing, setEditing] = useState(true);
  const [toasts, setToasts] = useState<ToastMessage[]>(existingToast ? [{ id: 1, title: '连接正常', tone: 'success' }] : []);
  return <>
    {editing && <dialog open aria-label="修改模型配置"><button onClick={() => {
      setEditing(false);
      setToasts([{ id: 2, title: '模型配置已保存', tone: 'success' }]);
    }}>保存修改</button></dialog>}
    <ToastStack toasts={toasts} onDismiss={id => setToasts(current => current.filter(toast => toast.id !== id))} />
  </>;
}

it.each([false, true])('keeps the save toast visible when its modal is removed in the same commit (existing toast: %s)', async existingToast => {
  render(<SavingDialog existingToast={existingToast} />);
  if (existingToast) await waitFor(() => expect(showPopover).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('button', { name: '保存修改' }));
  await waitFor(() => {
    expect(screen.queryByRole('dialog')).toBeNull();
    const toast = screen.getByRole('button', { name: '模型配置已保存' });
    expect(toast.isConnected).toBe(true);
    expect(openedPopovers.has(toast.parentElement!)).toBe(true);
  });
  fireEvent.click(screen.getByRole('button', { name: '模型配置已保存' }));
  expect(screen.queryByText('模型配置已保存')).toBeNull();
});

it('moves an active toast into an open modal and back when the modal closes', async () => {
  const dialog = document.createElement('dialog');
  document.body.append(dialog);
  render(<ToastStack toasts={[{ id: 1, title: '连接正常', tone: 'success' }]} onDismiss={() => {}} />);
  const expectOpenToastIn = (parent: HTMLElement) => {
    const toast = screen.getByRole('button', { name: '连接正常' });
    expect(parent.contains(toast)).toBe(true);
    expect(openedPopovers.has(toast.parentElement!)).toBe(true);
  };
  dialog.setAttribute('open', '');
  await waitFor(() => expectOpenToastIn(dialog));
  dialog.removeAttribute('open');
  await waitFor(() => {
    expect(dialog.querySelector('.toast-stack')).toBeNull();
    expectOpenToastIn(document.body);
  });
  dialog.remove();
});
