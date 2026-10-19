import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { PresentationResult } from '../../frontend/components/presentation-result';

afterEach(cleanup);
it('keeps accepted targets actionable and rejected targets separate', () => {
  const target = { type: 'file' as const, path: '/project/guide.md' };
  const open = vi.fn();
  render(<PresentationResult result={{ targets: [target, { type: 'panel', name: 'tasks' }], errors: [{ index: 1, message: '地址不支持' }] }} onOpen={open} />);
  expect(screen.getAllByRole('button')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: /guide.md/ }));
  expect(open).toHaveBeenCalledWith([target]);
  expect(screen.getByRole('listitem').textContent).toBe('第 2 项地址不支持');
});
it('renders only rejection details when the batch accepted nothing', () => {
  render(<PresentationResult result={{ targets: [], errors: [{ index: 0, message: '目标不存在' }] }} />);
  expect(screen.queryByRole('button')).toBeNull();
  expect(screen.getByText('目标不存在', { exact: false })).toBeTruthy();
});
