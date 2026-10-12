import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ActivityRail } from '../../frontend/components/activity-rail';

afterEach(cleanup);

function toggleMore(state: 'open' | 'closed') {
  const event = new Event('toggle');
  Object.defineProperty(event, 'newState', { value: state });
  // jsdom lacks the native popover action; mirror its visibility and toggle event.
  const target = document.getElementById(screen.getByRole('button', { name: '更多', exact: true }).getAttribute('aria-controls')!)!;
  target.style.display = state === 'open' ? 'block' : 'none';
  fireEvent(target, event);
}

it('groups all four secondary destinations and closes after navigation', () => {
  const navigate = vi.fn();
  render(<ActivityRail activeView="workbench" onNavigate={navigate} />);
  const more = screen.getByRole('button', { name: '更多', exact: true });
  expect(more.getAttribute('aria-expanded')).toBe('false');
  toggleMore('open');
  const group = screen.getByRole('group', { name: '更多功能' });
  expect(within(group).getAllByRole('button').map(button => button.textContent)).toEqual(['总览', '能力', '记忆', '定时任务']);
  expect(more.getAttribute('aria-expanded')).toBe('true');
  fireEvent.click(within(group).getByRole('button', { name: '定时任务' }));
  expect(navigate).toHaveBeenCalledExactlyOnceWith('scheduled');
  expect(more.getAttribute('aria-expanded')).toBe('false');
  expect(within(group).queryAllByRole('button')).toHaveLength(0);
});

it('keeps settings directly accessible and marks the active secondary destination', () => {
  const navigate = vi.fn();
  render(<ActivityRail activeView="memory" onNavigate={navigate} />);
  const more = screen.getByRole('button', { name: '更多', exact: true });
  expect(more.classList.contains('is-active')).toBe(true);
  toggleMore('open');
  expect(within(screen.getByRole('group', { name: '更多功能' })).getByRole('button', { name: '记忆' }).getAttribute('aria-current')).toBe('page');
  toggleMore('closed');
  expect(more.getAttribute('aria-expanded')).toBe('false');
  expect(more.classList.contains('is-active')).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '设置', exact: true }));
  expect(navigate).toHaveBeenCalledExactlyOnceWith('settings');
});
