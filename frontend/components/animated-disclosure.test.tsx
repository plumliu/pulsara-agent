import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { AnimatedDisclosure } from './animated-disclosure';

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
