import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import type { CliStatus, PulsaraDesktop } from '../../frontend/lib/desktop-host';
import { FakeAdapter } from './pulsara-app-fixture';

afterEach(() => { delete window.pulsaraDesktop; });

it('keeps Linux system controls in the global titlebar when switching pages', async () => {
  const minimize = vi.fn(), maximize = vi.fn(), close = vi.fn();
  window.pulsaraDesktop = { platform: 'linux', minimize, toggleMaximize: maximize, closeWindow: close } as unknown as PulsaraDesktop;
  const view = render(<PulsaraApp adapter={new FakeAdapter()} />);
  const shell = view.container.querySelector('.desktop-shell')!;
  expect(shell.getAttribute('data-platform')).toBe('linux');
  const titlebar = shell.querySelector('header.desktop-titlebar')!;
  const controls = within(titlebar).getByRole('group', { name: '窗口控制' });
  expect(titlebar.parentElement).toBe(shell);
  fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
  await screen.findByRole('textbox', { name: '发送给 Pulsara' });
  expect(screen.getByRole('group', { name: '窗口控制' })).toBe(controls);
  const tabs = within(titlebar).getByRole('tablist', { name: '已载入会话' });
  expect(controls.contains(tabs)).toBe(false);
  const sessionTitlebar = shell.querySelector('.session-canvas-window:not([hidden]) > header')!;
  expect(sessionTitlebar.contains(controls)).toBe(false);
  fireEvent.doubleClick(sessionTitlebar);
  expect(maximize).not.toHaveBeenCalled();
  expect(within(sessionTitlebar).getByRole('button', { name: '铺满会话窗口' })).toBeTruthy();
  fireEvent.click(within(controls).getByRole('button', { name: '最小化' }));
  fireEvent.click(within(controls).getByRole('button', { name: '最大化或还原' }));
  fireEvent.click(screen.getByRole('button', { name: '记忆', exact: true }));
  expect(within(titlebar).getByText('Pulsara')).toBeTruthy();
  expect(within(titlebar).queryByRole('tablist', { name: '已载入会话' })).toBeNull();
  expect(within(titlebar).getByRole('group', { name: '窗口控制' })).toBe(controls);
  fireEvent.click(within(controls).getByRole('button', { name: '关闭窗口' }));
  expect(minimize).toHaveBeenCalledOnce();
  expect(maximize).toHaveBeenCalledOnce();
  expect(close).toHaveBeenCalledOnce();
});

it('retains macOS native controls without rendering Linux replacements', async () => {
  window.pulsaraDesktop = { platform: 'darwin' } as PulsaraDesktop;
  const view = render(<PulsaraApp adapter={new FakeAdapter()} />);
  fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
  await screen.findByRole('textbox', { name: '发送给 Pulsara' });
  const shell = view.container.querySelector('.desktop-shell')!;
  expect(shell.getAttribute('data-platform')).toBe('darwin');
  const titlebar = shell.querySelector('header.desktop-titlebar')!;
  expect(titlebar.parentElement).toBe(shell);
  expect(screen.queryByRole('group', { name: '窗口控制' })).toBeNull();
  const toggle = within(titlebar).getByRole('button', { name: '切换会话侧栏' });
  const tab = within(titlebar).getByRole('tab', { name: /准备发布/ });
  const initiallyOpen = shell.classList.contains('sidebar-visible');
  fireEvent.click(toggle);
  expect(shell.classList.contains('sidebar-visible')).toBe(!initiallyOpen);
  expect(toggle.closest('[inert]')).toBeNull();
  fireEvent.click(toggle);
  expect(shell.classList.contains('sidebar-visible')).toBe(initiallyOpen);
  expect(within(titlebar).getByRole('button', { name: '切换会话侧栏' })).toBe(toggle);
  expect(within(titlebar).getByRole('tab', { name: /准备发布/ })).toBe(tab);
});

it('uses Windows native controls on conversation and settings pages', async () => {
  window.pulsaraDesktop = {
    platform: 'win32',
    cliStatus: async (): Promise<CliStatus> => ({
      state: 'ready', path: '/home/test/.local/bin/pulsara', managedBy: 'application',
    }),
  } as PulsaraDesktop;
  const view = render(<PulsaraApp adapter={new FakeAdapter()} />);
  const shell = view.container.querySelector('.desktop-shell')!;
  expect(shell.getAttribute('data-platform')).toBe('win32');
  const titlebar = shell.querySelector('header.desktop-titlebar')!;
  expect(screen.queryByRole('group', { name: '窗口控制' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '会话', exact: true }));
  await screen.findByRole('textbox', { name: '发送给 Pulsara' });
  expect(within(titlebar).getByRole('tablist', { name: '已载入会话' })).toBeTruthy();
  expect(screen.queryByRole('group', { name: '窗口控制' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '设置', exact: true }));
  await screen.findByLabelText('命令行路径');
  expect(within(titlebar).getByText('Pulsara')).toBeTruthy();
  expect(within(titlebar).queryByRole('tablist', { name: '已载入会话' })).toBeNull();
  expect(screen.queryByRole('group', { name: '窗口控制' })).toBeNull();
});
