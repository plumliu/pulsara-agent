import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { AnimatedPromptQueue } from '../../frontend/components/animated-prompt-queue';

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
