import { cleanup, render } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { BrandMark } from './brand-mark';

afterEach(cleanup);

it.each([
  { compact: false, size: 48 },
  { compact: true, size: 36 },
])('uses the approved decorative icon with compact=$compact', ({ compact, size }) => {
  const { container } = render(<BrandMark compact={compact} />);
  const image = container.querySelector('img')!;

  expect(image.getAttribute('src')).toBe('/assets/pulsara-icon.png');
  expect(image.getAttribute('alt')).toBe('');
  expect(image.getAttribute('aria-hidden')).toBe('true');
  expect(image.draggable).toBe(false);
  expect(image.width).toBe(size);
  expect(image.height).toBe(size);
  expect(image.classList.contains('is-compact')).toBe(compact);
});
