import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useEffect, useState } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ToastStack } from '../../frontend/components/overlays';
import type { ToastMessage } from '../../frontend/lib/pulsara-types';

const motions: Array<{ frames: Keyframe[]; finish: () => void; cancel: ReturnType<typeof vi.fn> }> = [];
let originalAnimate: PropertyDescriptor | undefined;
beforeEach(() => {
  motions.length = 0;
  vi.stubGlobal('PointerEvent', class extends MouseEvent {
    pointerId: number;
    isPrimary: boolean;
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init); this.pointerId = init.pointerId ?? 1; this.isPrimary = init.isPrimary ?? true;
    }
  });
  originalAnimate = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'animate');
  Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: vi.fn((frames: Keyframe[]) => {
    let finish!: () => void;
    const finished = new Promise<void>(resolve => { finish = resolve; });
    const cancel = vi.fn();
    motions.push({ frames, finish, cancel });
    return { finished, cancel };
  }) });
});
afterEach(() => {
  cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  if (originalAnimate) Object.defineProperty(HTMLElement.prototype, 'animate', originalAnimate);
  else Reflect.deleteProperty(HTMLElement.prototype, 'animate');
});

function Harness({ action, dismiss, expires = false }: { action?: () => void; dismiss: (id: number) => void; expires?: boolean }) {
  const [toasts, setToasts] = useState<ToastMessage[]>([
    { id: 1, title: '测试通知', onClick: action }, { id: 2, title: '保留通知' },
  ]);
  useEffect(() => {
    if (!expires) return;
    const timer = window.setTimeout(() => setToasts(current => current.filter(toast => toast.id !== 1)), 4200);
    return () => window.clearTimeout(timer);
  }, [expires]);
  return <ToastStack toasts={toasts} onDismiss={id => { dismiss(id); setToasts(current => current.filter(toast => toast.id !== id)); }} />;
}

function target() {
  const toast = screen.getByText('测试通知').closest('button')!;
  const captured = new Set<number>();
  toast.setPointerCapture = vi.fn(id => { captured.add(id); });
  toast.hasPointerCapture = vi.fn(id => captured.has(id));
  toast.releasePointerCapture = vi.fn(id => { captured.delete(id); });
  vi.spyOn(toast, 'getBoundingClientRect').mockReturnValue(new DOMRect(700, 100, 300, 70));
  return toast;
}
function pointer(toast: HTMLElement, type: 'pointerDown' | 'pointerMove' | 'pointerUp', x: number, y = 100) {
  fireEvent[type](toast, { button: 0, pointerId: 1, isPrimary: true, clientX: x, clientY: y });
}
function expectRightExit() {
  const motion = motions.at(-1)!;
  expect(motion.frames.at(-1)?.opacity).toBe(0);
  expect(parseFloat(String(motion.frames.at(-1)?.translate))).toBeGreaterThan(window.innerWidth - 700);
  return motion;
}

it('crossing the drag threshold continues offscreen before pointer release and removes only that toast', async () => {
  const dismiss = vi.fn(), action = vi.fn();
  render(<Harness dismiss={dismiss} action={action} />);
  const toast = target();
  pointer(toast, 'pointerDown', 710);
  pointer(toast, 'pointerMove', 770);
  expect(toast.style.translate).toBe('60px 0');
  expect(dismiss).not.toHaveBeenCalled();
  pointer(toast, 'pointerMove', 810);
  expect(dismiss).toHaveBeenCalledExactlyOnceWith(1);
  expect(toast.disabled).toBe(true);
  const exit = expectRightExit();
  pointer(toast, 'pointerUp', 810);
  fireEvent.click(toast, { detail: 1 });
  expect(action).not.toHaveBeenCalled();
  expect(dismiss).toHaveBeenCalledTimes(1);
  await act(async () => exit.finish());
  expect(screen.queryByText('测试通知')).toBeNull();
  expect(screen.getByText('保留通知')).toBeTruthy();
});

it.each(['click', 'expiry'])('%s uses the same rightward exit and retains the node until the animation finishes', async cause => {
  const dismiss = vi.fn();
  vi.useFakeTimers();
  render(<Harness dismiss={dismiss} expires={cause === 'expiry'} />);
  const toast = target();
  if (cause === 'click') fireEvent.click(toast);
  else await act(async () => vi.advanceTimersByTimeAsync(4200));
  expect(toast.isConnected).toBe(true);
  expect(toast.disabled).toBe(true);
  const exit = expectRightExit();
  await act(async () => exit.finish());
  expect(screen.queryByText('测试通知')).toBeNull();
  expect(screen.getByText('保留通知')).toBeTruthy();
  expect(dismiss).toHaveBeenCalledTimes(cause === 'click' ? 1 : 0);
});

it('a short drag snaps back without clicking the action, and later mouse and keyboard activation still work', () => {
  const dismiss = vi.fn(), action = vi.fn();
  render(<Harness dismiss={dismiss} action={action} />);
  const toast = target();
  pointer(toast, 'pointerDown', 710);
  pointer(toast, 'pointerMove', 750);
  pointer(toast, 'pointerUp', 750);
  expect(toast.style.translate).toBe('0px 0');
  fireEvent.click(toast, { detail: 1 });
  expect(action).not.toHaveBeenCalled();
  fireEvent.click(toast, { detail: 0 });
  expect(action).toHaveBeenCalledTimes(1);
  pointer(toast, 'pointerDown', 710);
  pointer(toast, 'pointerUp', 712);
  fireEvent.click(toast, { detail: 1 });
  expect(action).toHaveBeenCalledTimes(2);
  expect(dismiss).not.toHaveBeenCalled();
});

it.each([[-120, 0], [20, 160]])('leftward or vertical movement does not dismiss or click (%i, %i)', (x, y) => {
  const dismiss = vi.fn(), action = vi.fn();
  render(<Harness dismiss={dismiss} action={action} />);
  const toast = target();
  pointer(toast, 'pointerDown', 710);
  pointer(toast, 'pointerMove', 710 + x, 100 + y);
  pointer(toast, 'pointerUp', 710 + x, 100 + y);
  fireEvent.click(toast, { detail: 1 });
  expect(toast.style.translate).toBe('0px 0');
  expect(dismiss).not.toHaveBeenCalled();
  expect(action).not.toHaveBeenCalled();
});

it.each(['pointerCancel', 'lostPointerCapture'] as const)('%s cancels an incomplete gesture', event => {
  const dismiss = vi.fn();
  render(<Harness dismiss={dismiss} />);
  const toast = target();
  pointer(toast, 'pointerDown', 710);
  pointer(toast, 'pointerMove', 750);
  fireEvent[event](toast, { pointerId: 1 });
  expect(toast.style.translate).toBe('0px 0');
  pointer(toast, 'pointerUp', 900);
  expect(dismiss).not.toHaveBeenCalled();
});

it('reduced motion dismisses without animation', () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true }));
  const dismiss = vi.fn();
  render(<Harness dismiss={dismiss} />);
  fireEvent.click(screen.getByText('测试通知').closest('button')!);
  expect(screen.queryByText('测试通知')).toBeNull();
  expect(motions).toHaveLength(0);
});

it('expiry during a drag releases capture and completes one exit without an extra dismissal', async () => {
  const dismiss = vi.fn();
  vi.useFakeTimers();
  render(<Harness dismiss={dismiss} expires />);
  const toast = target();
  pointer(toast, 'pointerDown', 710);
  pointer(toast, 'pointerMove', 750);
  await act(async () => vi.advanceTimersByTimeAsync(4200));
  expect(toast.releasePointerCapture).toHaveBeenCalledExactlyOnceWith(1);
  const exit = expectRightExit();
  pointer(toast, 'pointerUp', 900);
  await act(async () => exit.finish());
  expect(screen.queryByText('测试通知')).toBeNull();
  expect(dismiss).not.toHaveBeenCalled();
});
