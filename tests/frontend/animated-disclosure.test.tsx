import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnimatedDisclosure } from '../../frontend/components/animated-disclosure';

afterEach(cleanup);

describe('AnimatedDisclosure', () => {
  it('mounts lazily, then keeps content mounted for the closing animation', () => {
    const view = render(<AnimatedDisclosure open={false}><button>详情操作</button></AnimatedDisclosure>);
    const disclosure = view.container.querySelector('.animated-disclosure')!;

    expect(screen.queryByText('详情操作')).toBeNull();
    expect(disclosure.classList.contains('is-open')).toBe(false);
    expect(disclosure.getAttribute('aria-hidden')).toBe('true');
    expect(disclosure.hasAttribute('inert')).toBe(true);

    view.rerender(<AnimatedDisclosure open><button>详情操作</button></AnimatedDisclosure>);
    expect(disclosure.classList.contains('is-open')).toBe(true);
    expect(disclosure.getAttribute('aria-hidden')).toBe('false');
    expect(disclosure.hasAttribute('inert')).toBe(false);

    view.rerender(<AnimatedDisclosure open={false}><button>详情操作</button></AnimatedDisclosure>);
    expect(screen.getByText('详情操作')).toBeTruthy();
    expect(disclosure.classList.contains('is-open')).toBe(false);
    expect(disclosure.getAttribute('aria-hidden')).toBe('true');
    expect(disclosure.hasAttribute('inert')).toBe(true);
  });
});

it.each(['finish', 'cancel'])('releases expensive children after its closing transition %s', async outcome => {
  let settle!: () => void;
  const finished = new Promise<void>((resolve, reject) => {settle = outcome === 'finish' ? resolve : () => reject(new Error('cancelled'));});
  const view = render(<AnimatedDisclosure open unmountOnClose><p>expensive content</p></AnimatedDisclosure>);
  const getAnimations=vi.fn(()=>[{finished}]);
  Object.defineProperty(view.container.firstElementChild!, 'getAnimations', {value:getAnimations});
  view.rerender(<AnimatedDisclosure open={false} unmountOnClose><p>expensive content</p></AnimatedDisclosure>);
  expect(view.container.firstElementChild!.hasAttribute('inert')).toBe(true);
  expect(screen.getByText('expensive content')).toBeTruthy();
  expect(getAnimations).toHaveBeenCalledExactlyOnceWith();
  await act(async()=>settle());
  expect(screen.queryByText('expensive content')).toBeNull();
});
it('ignores an old closing transition after rapid reopen and releases immediately without animations', async () => {
  let settle!: () => void;
  const finished = new Promise<void>(resolve=>{settle=resolve;});
  const view=render(<AnimatedDisclosure open unmountOnClose><p>content</p></AnimatedDisclosure>);
  const getAnimations=vi.fn(()=>[{finished}]);
  Object.defineProperty(view.container.firstElementChild!, 'getAnimations',{value:getAnimations});
  view.rerender(<AnimatedDisclosure open={false} unmountOnClose><p>content</p></AnimatedDisclosure>);
  view.rerender(<AnimatedDisclosure open unmountOnClose><p>content</p></AnimatedDisclosure>);
  await act(async()=>settle());
  expect(screen.getByText('content')).toBeTruthy();
  getAnimations.mockReturnValue([]); // Already-settled or non-rendered containers have no active transitions.
  view.rerender(<AnimatedDisclosure open={false} unmountOnClose><p>content</p></AnimatedDisclosure>);
  expect(screen.queryByText('content')).toBeNull();
});
