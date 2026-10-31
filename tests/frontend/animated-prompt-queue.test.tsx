import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { AnimatedPromptQueue, queuePreviewCapacity } from '../../frontend/components/animated-prompt-queue';

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function animations() {
  const running: Array<{ finish: () => void; cancel: ReturnType<typeof vi.fn> }> = [];
  const animate = vi.fn(() => {
    let finish!: () => void;
    const finished = new Promise<void>(resolve => { finish = resolve; });
    const cancel = vi.fn();
    running.push({ finish, cancel });
    return { finished, cancel };
  });
  vi.stubGlobal('matchMedia', () => ({ matches: false }));
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'animate');
  Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: animate });
  return { running, animate, restore: () => {
    if (original) Object.defineProperty(HTMLElement.prototype, 'animate', original);
    else Reflect.deleteProperty(HTMLElement.prototype, 'animate');
  } };
}

it('keeps a bubble after entrance finishes and preserves identity when submission content updates', async () => {
  const motion = animations();
  try {
    const view = render(<AnimatedPromptQueue><article key="input"><button>删除</button>正在提交</article></AnimatedPromptQueue>);
    const button = screen.getByRole('button', { name: '删除' });
    await act(async () => motion.running.forEach(animation => animation.finish()));
    expect(screen.getByRole('button', { name: '删除' })).toBe(button);
    const entranceCount = motion.animate.mock.calls.length;
    view.rerender(<AnimatedPromptQueue><article key="input"><button>删除</button>已排队</article></AnimatedPromptQueue>);
    expect(screen.getByText('已排队')).toBeTruthy();
    expect(screen.getByRole('button', { name: '删除' })).toBe(button);
    expect(motion.animate).toHaveBeenCalledTimes(entranceCount);
  } finally { cleanup(); motion.restore(); }
});

it('makes a removed bubble inert immediately, then releases its visual after exit settles', async () => {
  const motion = animations();
  try {
    const view = render(<AnimatedPromptQueue><article key="input"><button>删除</button>原文</article></AnimatedPromptQueue>);
    view.rerender(<AnimatedPromptQueue>{null}</AnimatedPromptQueue>);
    expect(screen.queryByRole('button', { name: '删除' })).toBeNull();
    expect(screen.getByText('原文').closest('[inert]')).toBeTruthy();
    await act(async () => motion.running.forEach(animation => animation.finish()));
    expect(screen.queryByText('原文')).toBeNull();
    expect(screen.queryByRole('region', { name: '等待处理的输入' })).toBeNull();
  } finally { cleanup(); motion.restore(); }
});

it('cancels an old exit when the same input is present again', async () => {
  const motion = animations();
  try {
    const item = <article key="input"><button>删除</button>原文</article>;
    const view = render(<AnimatedPromptQueue>{item}</AnimatedPromptQueue>);
    view.rerender(<AnimatedPromptQueue>{null}</AnimatedPromptQueue>);
    const previous = [...motion.running];
    view.rerender(<AnimatedPromptQueue>{item}</AnimatedPromptQueue>);
    await act(async () => previous.forEach(animation => animation.finish()));
    expect(screen.getByRole('button', { name: '删除' })).toBeTruthy();
    expect(screen.getByText('原文').closest('[inert]')).toBeNull();
  } finally { cleanup(); motion.restore(); }
});

it('removes immediately with reduced motion', () => {
  const motion = animations();
  vi.stubGlobal('matchMedia', () => ({ matches: true }));
  try {
    const view = render(<AnimatedPromptQueue><article key="input">原文</article></AnimatedPromptQueue>);
    view.rerender(<AnimatedPromptQueue>{null}</AnimatedPromptQueue>);
    expect(screen.queryByText('原文')).toBeNull();
    expect(motion.animate).not.toHaveBeenCalled();
  } finally { cleanup(); motion.restore(); }
});

const rows = (first: number, last: number) => Array.from({ length: last - first + 1 }, (_, index) => {
  const id = first + index;
  return <article key={id} data-id={id}>消息 {id}</article>;
});
const visibleIds = () => screen.getAllByRole('article').map(item => Number(item.dataset.id));

it('slides a full head/tail preview when a 51st input arrives, without animating omission as consumption', async () => {
  const motion = animations();
  try {
    const view = render(<AnimatedPromptQueue maxVisibleItems={5}>{rows(1, 50)}</AnimatedPromptQueue>);
    expect(visibleIds()).toEqual([1, 2, 3, 49, 50]);
    expect(screen.getByText('中间省略 45 条排队消息')).toBeTruthy();
    const last = screen.getByText('消息 50');
    view.rerender(<AnimatedPromptQueue maxVisibleItems={5}>{rows(1, 51)}</AnimatedPromptQueue>);
    expect(visibleIds()).toEqual([1, 2, 3, 50, 51]);
    expect(screen.getByText('中间省略 46 条排队消息')).toBeTruthy();
    expect(screen.getByText('消息 50')).toBe(last);
    expect(screen.queryByText('消息 49')).toBeNull();

    view.rerender(<AnimatedPromptQueue maxVisibleItems={5}>{rows(2, 51)}</AnimatedPromptQueue>);
    expect(visibleIds()).toEqual([2, 3, 4, 50, 51]);
    expect(screen.getByText('消息 1').closest('[inert]')).toBeTruthy();
    expect(screen.getByText('中间省略 45 条排队消息')).toBeTruthy();
    await act(async () => motion.running.forEach(animation => animation.finish()));
    expect(screen.queryByText('消息 1')).toBeNull();
  } finally { cleanup(); motion.restore(); }
});

it('rebalances on resize and removes the omission line when everything fits', () => {
  const view = render(<AnimatedPromptQueue maxVisibleItems={5}>{rows(1, 6)}</AnimatedPromptQueue>);
  expect(visibleIds()).toEqual([1, 2, 3, 5, 6]);
  expect(screen.getByText('中间省略 1 条排队消息')).toBeTruthy();
  view.rerender(<AnimatedPromptQueue maxVisibleItems={4}>{rows(1, 6)}</AnimatedPromptQueue>);
  expect(visibleIds()).toEqual([1, 2, 5, 6]);
  expect(screen.getByText('中间省略 2 条排队消息')).toBeTruthy();
  view.rerender(<AnimatedPromptQueue maxVisibleItems={6}>{rows(1, 6)}</AnimatedPromptQueue>);
  expect(visibleIds()).toEqual([1, 2, 3, 4, 5, 6]);
  expect(screen.queryByText(/省略/)).toBeNull();
});

it('does not present the end of an incomplete page as the real tail', () => {
  const view = render(<AnimatedPromptQueue maxVisibleItems={5} unloadedCount={50}>{rows(1, 50)}</AnimatedPromptQueue>);
  expect(visibleIds()).toEqual([1, 2, 3]);
  expect(screen.getByText('还有 97 条排队消息')).toBeTruthy();
  view.rerender(<AnimatedPromptQueue maxVisibleItems={5}>{rows(1, 100)}</AnimatedPromptQueue>);
  expect(visibleIds()).toEqual([1, 2, 3, 99, 100]);
  expect(screen.getByText('中间省略 95 条排队消息')).toBeTruthy();
});

it('uses the available height for a preview budget, retaining both ends in a small window', () => {
  expect(queuePreviewCapacity(324, 720)).toBe(5);
  expect(queuePreviewCapacity(324, 480)).toBe(3);
  expect(queuePreviewCapacity(0, 720)).toBe(2);
});
