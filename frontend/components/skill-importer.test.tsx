import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SkillImporter } from './skill-importer';

afterEach(cleanup);

it('previews without installing, supplements description, and settles candidates independently', async () => {
  const onPreview = vi.fn(async () => [
    { sourcePath: '/source/.opencode/skills/one', name: 'one', description: '', valid: false, details: ['缺少描述'] },
    { sourcePath: '/source/skills/two', name: 'two', description: 'Second skill', valid: true, details: [] },
  ]);
  const onInstall = vi.fn(async (input) => input.name === 'two');
  render(<SkillImporter onPreview={onPreview} onInstall={onInstall} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('来源目录'), { target: { value: '/source' } });
  fireEvent.click(screen.getByText('读取预览'));
  await screen.findByText('缺少描述');
  expect(onInstall).not.toHaveBeenCalled();
  expect((screen.getByText('安装所选技能') as HTMLButtonElement).disabled).toBe(false);
  fireEvent.change(screen.getAllByLabelText('用途描述')[0], { target: { value: '用户确认的用途' } });
  fireEvent.click(screen.getByText('安装所选技能'));
  await screen.findByText('已安装');
  expect(onInstall.mock.calls.map(([input]) => input.name)).toEqual(['one', 'two']);
  fireEvent.click(screen.getByText('安装所选技能'));
  await waitFor(() => expect(onInstall).toHaveBeenCalledTimes(3));
  expect(onInstall.mock.calls[2][0].name).toBe('one');
});

it('keeps installing valid candidates after missing fields or a rejected installation', async () => {
  const onInstall = vi.fn(async (input) => {
    if (input.name === 'broken') throw new Error('来源发生变化，请重新预览。');
    return true;
  });
  render(<SkillImporter onPreview={async () => [
    { sourcePath: '/missing', name: 'missing', description: '', valid: false, details: [] },
    { sourcePath: '/broken', name: 'broken', description: 'Broken', valid: true, details: [] },
    { sourcePath: '/good', name: 'good', description: 'Good', valid: true, details: [] },
  ]} onInstall={onInstall} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('来源目录'), { target: { value: '/source' } });
  fireEvent.click(screen.getByText('读取预览'));
  await screen.findAllByLabelText('用途描述');
  fireEvent.click(screen.getByText('安装所选技能'));
  await screen.findByText('已安装');
  expect(onInstall.mock.calls.map(([input]) => input.name)).toEqual(['broken', 'good']);
  expect(screen.getByText('请补充安装名称和用途描述后重试。')).toBeTruthy();
  expect(screen.getByText('来源发生变化，请重新预览。')).toBeTruthy();
});
