import { act, fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import { SessionSearchDialog } from '../../frontend/components/session-search-dialog';
import type { RuntimeAdapter } from '../../frontend/lib/runtime-adapter';
import { FakeAdapter, renderWorkbench } from './pulsara-app-fixture';

it.each([
  ['k', { ctrlKey: true }], ['n', { ctrlKey: true }],
  ['k', { metaKey: true }], ['n', { metaKey: true }],
])('keeps ordinary command shortcut %s %j active', async (key, state) => {
  renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
  const composer = await screen.findByLabelText('发送给 Pulsara');
  expect(fireEvent.keyDown(composer, { key, ...state })).toBe(false);
  expect(screen.getByRole('dialog', { name: key === 'k' ? '搜索会话' : '新建会话' })).toBeTruthy();
});

it.each([
  ['k', { ctrlKey: true, altKey: true }],
  ['n', { ctrlKey: true, altKey: true }],
  ['k', { ctrlKey: true, isComposing: true }],
  ['n', { ctrlKey: true, isComposing: true }],
  ['k', { ctrlKey: true, keyCode: 229 }],
  ['n', { ctrlKey: true, keyCode: 229 }],
])('does not consume Ctrl+%s with extra Alt or composition state %j', async (key, state) => {
  renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
  const composer = await screen.findByLabelText('发送给 Pulsara');
  expect(fireEvent.keyDown(composer, { key, ...state })).toBe(true);
  expect(screen.queryByRole('dialog', { name: '搜索会话' })).toBeNull();
  expect(screen.queryByRole('dialog', { name: '新建会话' })).toBeNull();
});

it('keeps session search open during an input composition', () => {
  const onClose = vi.fn();
  render(<SessionSearchDialog adapter={{} as RuntimeAdapter} available={false} onOpen={vi.fn()} onClose={onClose} />);
  const input = screen.getByLabelText('搜索关键词');
  fireEvent.compositionStart(input);
  expect(fireEvent.keyDown(input, { key: 'k', ctrlKey: true })).toBe(true);
  expect(onClose).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input);
  fireEvent.keyDown(input, { key: 'k', ctrlKey: true });
  expect(onClose).toHaveBeenCalledOnce();
});

it('does not turn a mapped AltGraph character into a shortcut by physical key code', async () => {
  renderWorkbench(<PulsaraApp adapter={new FakeAdapter()} />);
  const composer = await screen.findByLabelText('发送给 Pulsara');
  // A mapped AltGr+N produces the layout character, not key="n". Chromium
  // normalizes its Ctrl+Alt flags to AltGraph for this printable mapping.
  const event = new KeyboardEvent('keydown', {
    key: 'ń', code: 'KeyN', modifierAltGraph: true, bubbles: true, cancelable: true,
  });
  expect(event.getModifierState('AltGraph')).toBe(true);
  act(() => { composer.dispatchEvent(event); });
  expect(event.defaultPrevented).toBe(false);
  expect(screen.queryByRole('dialog', { name: '新建会话' })).toBeNull();
});

it('does not close session search for Ctrl+Alt+K, while Ctrl+K still closes it', () => {
  const onClose = vi.fn();
  render(<SessionSearchDialog adapter={{} as RuntimeAdapter} available={false} onOpen={vi.fn()} onClose={onClose} />);
  const input = screen.getByLabelText('搜索关键词');
  expect(fireEvent.keyDown(input, { key: 'k', ctrlKey: true, altKey: true })).toBe(true);
  expect(onClose).not.toHaveBeenCalled();
  expect(fireEvent.keyDown(input, { key: 'k', ctrlKey: true })).toBe(false);
  expect(onClose).toHaveBeenCalledOnce();
});
