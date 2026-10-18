import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { CliSettings } from '../../frontend/components/cli-settings';
import type { CliStatus, PulsaraDesktop } from '../../frontend/lib/desktop-host';

afterEach(() => { cleanup(); delete window.pulsaraDesktop; });
const conflict: CliStatus = { state:'conflict', path:'/home/me/.local/bin/pulsara', existingTarget:'/uv/pulsara', managedBy:'application', directoryOnPath:false };
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
  await screen.findByText('使用 pulsara 命令打开或管理应用。');
  expect(repairCli).toHaveBeenCalledWith(true);
});
it('keeps system package repair with the package manager', async () => {
  window.pulsaraDesktop={cliStatus:vi.fn().mockResolvedValue({...conflict,managedBy:'package'})} as unknown as PulsaraDesktop;
  render(<CliSettings/>);
  fireEvent.click(await screen.findByRole('button', {name:'已有同名命令'}));
  expect(screen.getByText('处理同名冲突后，通过 deb 重新安装 Pulsara。')).toBeTruthy();
  expect(screen.queryByRole('button',{name:'重置入口'})).toBeNull();
});
