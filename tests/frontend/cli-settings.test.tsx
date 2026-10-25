import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { CliSettings } from '../../frontend/components/cli-settings';
import type { CliStatus, PulsaraDesktop } from '../../frontend/lib/desktop-host';

afterEach(() => { cleanup(); delete window.pulsaraDesktop; });
const conflict: CliStatus = { state:'conflict', path:'/home/me/.local/bin/pulsara', existingTarget:'/uv/pulsara', managedBy:'application' };
it('inspects on opening and replaces only after the explicit action', async () => {
  const cliStatus=vi.fn().mockResolvedValue(conflict), repairCli=vi.fn().mockResolvedValue({...conflict,state:'ready'});
  window.pulsaraDesktop={cliStatus,repairCli} as unknown as PulsaraDesktop;
  render(<CliSettings/>);
  const disclosure = await screen.findByRole('button', {name:'已有同名命令'});
  expect(disclosure.getAttribute('aria-expanded')).toBe('false');
  fireEvent.click(disclosure);
  expect(screen.getByRole('region', {name:'命令行冲突详情'})).toBeTruthy();
  expect(disclosure.getAttribute('aria-expanded')).toBe('true');
  expect(cliStatus).toHaveBeenCalledOnce();
  fireEvent.click(disclosure);
  expect(screen.queryByRole('region', {name:'命令行冲突详情'})).toBeNull();
  expect(repairCli).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'重新检查'}));
  await vi.waitFor(()=>expect(cliStatus).toHaveBeenCalledTimes(2));
  expect(repairCli).not.toHaveBeenCalled();
  await vi.waitFor(()=>expect((screen.getByRole('button',{name:'重置入口'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button',{name:'重置入口'}));
  await screen.findByText('在终端中使用 pulsara。');
  expect(repairCli).toHaveBeenCalledWith(true);
});
it('keeps system package repair with the package manager', async () => {
  window.pulsaraDesktop={cliStatus:vi.fn().mockResolvedValue({...conflict,managedBy:'package'})} as unknown as PulsaraDesktop;
  render(<CliSettings/>);
  fireEvent.click(await screen.findByRole('button', {name:'已有同名命令'}));
  expect(screen.getByText('解决命令冲突后，重新安装 Pulsara。')).toBeTruthy();
  expect(screen.queryByRole('button',{name:'重置入口'})).toBeNull();
});

it('does not infer a terminal PATH problem from a registered app entry', async () => {
  window.pulsaraDesktop={cliStatus:vi.fn().mockResolvedValue({state:'ready',path:conflict.path,managedBy:'application'})} as unknown as PulsaraDesktop;
  const {container}=render(<CliSettings/>);
  await screen.findByText('在终端中使用 pulsara。');
  expect(container.querySelector('.settings-cli-notice')).toBeNull();
});

it('scopes a different PATH command to the app environment', async () => {
  window.pulsaraDesktop={cliStatus:vi.fn().mockResolvedValue({state:'ready',path:conflict.path,managedBy:'application',shadowedBy:'/other/pulsara'})} as unknown as PulsaraDesktop;
  const {container}=render(<CliSettings/>);
  await screen.findByText('在终端中使用 pulsara。');
  expect(container.querySelector('.settings-cli-notice')?.textContent).toBe('应用检测到同名命令：/other/pulsara。在终端运行 command -v pulsara 查看当前命令。');
});
