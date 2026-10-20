import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ActivityRail } from '../../frontend/components/activity-rail';

afterEach(cleanup);

it('keeps every desktop destination directly accessible', () => {
  const navigate = vi.fn();
  render(<ActivityRail activeView="workbench" onNavigate={navigate} />);
  for (const [name, view] of [['总览', 'overview'], ['会话', 'workbench'], ['能力', 'capabilities'], ['记忆', 'memory'], ['定时任务', 'scheduled'], ['设置', 'settings']]) {
    fireEvent.click(screen.getByRole('button', { name, exact: true }));
    expect(navigate).toHaveBeenLastCalledWith(view);
  }
  expect(screen.queryByRole('button', { name: '更多', exact: true })).toBeNull();
});

it('opens settings from the profile and marks the settings gear as current', () => {
  const navigate = vi.fn();
  const view = render(<ActivityRail activeView="memory" onNavigate={navigate} />);
  expect(screen.getByRole('button', { name: '记忆', exact: true }).getAttribute('aria-current')).toBe('page');
  fireEvent.click(screen.getByRole('button', { name: '用户设置', exact: true }));
  expect(navigate).toHaveBeenCalledExactlyOnceWith('settings');
  view.rerender(<ActivityRail activeView="settings" onNavigate={navigate} />);
  expect(screen.getByRole('button', { name: '设置', exact: true }).getAttribute('aria-current')).toBe('page');
  expect(screen.getByRole('button', { name: '记忆', exact: true }).hasAttribute('aria-current')).toBe(false);
});
