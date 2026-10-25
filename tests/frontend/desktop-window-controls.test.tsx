import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import type { PulsaraDesktop } from '../../frontend/lib/desktop-host';
import { FakeAdapter } from './pulsara-app-fixture';

it('keeps Linux window controls outside session drag regions when switching pages', async () => {
  const minimize = vi.fn(), maximize = vi.fn(), close = vi.fn();
  window.pulsaraDesktop = { platform: 'linux', minimize, toggleMaximize: maximize, closeWindow: close } as unknown as PulsaraDesktop;
  const view = render(<PulsaraApp adapter={new FakeAdapter()} />);
  expect(view.container.querySelector('.desktop-shell')?.getAttribute('data-platform')).toBe('linux');
  const controls = screen.getByRole('group', { name: '窗口控制' });
  const shell = view.container.querySelector('.desktop-shell')!;
  expect(controls.parentElement).toBe(shell);
  expect(shell.lastElementChild).toBe(controls);
  fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
  await screen.findByLabelText('发送给 Pulsara');
  expect(screen.getByRole('group', { name: '窗口控制' })).toBe(controls);
  expect(controls.closest('header')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '最小化' }));
  fireEvent.click(screen.getByRole('button', { name: '最大化或还原' }));
  fireEvent.click(screen.getByRole('button', { name: '总览', exact: true }));
  fireEvent.click(screen.getByRole('button', { name: '关闭窗口' }));
  expect(minimize).toHaveBeenCalledOnce();
  expect(maximize).toHaveBeenCalledOnce();
  expect(close).toHaveBeenCalledOnce();
});

it('retains macOS native controls without rendering Linux replacements', async () => {
  window.pulsaraDesktop = { platform: 'darwin' } as PulsaraDesktop;
  const view = render(<PulsaraApp adapter={new FakeAdapter()} />);
  fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
  await screen.findByLabelText('发送给 Pulsara');
  expect(view.container.querySelector('.desktop-titlebar.is-macos')).toBeTruthy();
  expect(screen.queryByRole('group', { name: '窗口控制' })).toBeNull();
});

it('uses Windows native controls on both overview and conversation pages', async () => {
  window.pulsaraDesktop = { platform: 'win32' } as PulsaraDesktop;
  const view = render(<PulsaraApp adapter={new FakeAdapter()} />);
  expect(view.container.querySelector('.desktop-shell')?.getAttribute('data-platform')).toBe('win32');
  expect(screen.queryByRole('group', { name: '窗口控制' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
  await screen.findByLabelText('发送给 Pulsara');
  expect(screen.queryByRole('group', { name: '窗口控制' })).toBeNull();
  expect(view.container.querySelector('.desktop-titlebar.is-macos')).toBeNull();
});
