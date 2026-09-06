import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {PluginImporter} from './plugin-importer';
import type {PluginImportPreview, PluginImportDiscovery} from '../lib/pulsara-types';

afterEach(cleanup);

const preview: PluginImportPreview = {
  name: 'example', source_format: 'cursor', skills: ['research'], hooks: [], notices: [],
  mcp: [{server_id: 'search', transport: 'streamable_http', issues: [], notices: [], fields: [
    {target: 'endpoint', private: false, template_literals: false, variables: [{name: 'URL', has_default: false, environment: false}]},
    {target: 'header:Authorization', private: true, template_literals: true, variables: [{name: 'KEY', has_default: false, environment: false}]},
  ]}],
};
const discovery = (items = [preview]): PluginImportDiscovery => ({candidates: items.map(item => ({source_format: item.source_format, manifest: `.${item.source_format}-plugin/plugin.json`, preview: item, error: null}))});

it('automatically previews the single format, fills public parameters, and installs disabled without packaging keys', async () => {
  const inspect = vi.fn(async () => discovery()), install = vi.fn(async () => true), close = vi.fn();
  render(<PluginImporter onPreview={inspect} onInstall={install} onClose={close} />);
  fireEvent.change(screen.getByLabelText('本地目录'), {target: {value: '/source'}});
  expect(screen.queryByLabelText('发行格式')).toBeNull();
  expect((screen.getByText('安装') as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByText('读取预览'));
  await screen.findByText('技能：research');
  expect(inspect).toHaveBeenCalledWith('/source');
  expect(screen.queryByRole('radio')).toBeNull();
  expect(install).not.toHaveBeenCalled();
  expect(screen.queryByLabelText('KEY')).toBeNull();
  expect(screen.getByText('KEY：安装后在连接设置中填写密钥。')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('URL'), {target: {value: 'https://example.org/mcp'}});
  fireEvent.click(screen.getByText('安装'));
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
  expect(install).toHaveBeenCalledWith('/source', {source_format: 'cursor', classifications: {}, public_values: {URL: 'https://example.org/mcp'}});
});

it('invalidates a preview when the source changes', async () => {
  const inspect = vi.fn(async () => discovery()), install = vi.fn(async () => true);
  render(<PluginImporter onPreview={inspect} onInstall={install} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('本地目录'), {target: {value: '/source'}});
  fireEvent.click(screen.getByText('读取预览'));
  await screen.findByText('技能：research');
  fireEvent.change(screen.getByLabelText('本地目录'), {target: {value: '/another'}});
  expect(screen.queryByText('技能：research')).toBeNull();
  expect((screen.getByText('安装') as HTMLButtonElement).disabled).toBe(true);
  expect(install).not.toHaveBeenCalled();
});

it('retains the form when installation fails, and never installs from a failed preview', async () => {
  const inspect = vi.fn(async () => {throw new Error('不支持的组件');}), install = vi.fn(async () => false);
  render(<PluginImporter onPreview={inspect} onInstall={install} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('本地目录'), {target: {value: '/source'}});
  fireEvent.click(screen.getByText('读取预览'));
  await screen.findByRole('alert');
  expect((screen.getByText('安装') as HTMLButtonElement).disabled).toBe(true);
  expect(install).not.toHaveBeenCalled();
});

it('requires selection for multiple distributions and clears parameters when switching', async () => {
  const codex = {...preview, source_format: 'codex' as const, skills: ['other'], mcp: []};
  const inspect = vi.fn(async () => discovery([preview, codex])), install = vi.fn(async () => true);
  render(<PluginImporter onPreview={inspect} onInstall={install} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('本地目录'), {target: {value: '/source'}});
  fireEvent.click(screen.getByText('读取预览'));
  await screen.findByRole('radio', {name: 'Cursor'});
  const componentLists = screen.getAllByText('查看组件');
  expect(componentLists).toHaveLength(2);
  for (const summary of componentLists) {
    const disclosure = summary.parentElement as HTMLDetailsElement;
    expect(disclosure.open).toBe(false);
    fireEvent.click(summary);
    expect(disclosure.open).toBe(true);
  }
  expect(screen.getByText(/技能：research；MCP：search/)).toBeTruthy();
  expect(screen.getByText(/技能：other；MCP：无/)).toBeTruthy();
  expect((screen.getByText('安装') as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('radio', {name: 'Codex'}) as HTMLInputElement).checked).toBe(false);
  const dialog = screen.getByRole('dialog', {name: '安装插件'});
  const body = dialog.querySelector('.capability-dialog-body');
  expect(body?.contains(screen.getByRole('group', {name: '选择发行版'}))).toBe(true);
  expect(body?.contains(screen.getByText('安装'))).toBe(false);
  expect(dialog.querySelector('footer')?.contains(screen.getByText('安装'))).toBe(true);
  fireEvent.click(screen.getByRole('radio', {name: 'Cursor'}));
  fireEvent.change(screen.getByLabelText('URL'), {target: {value: 'https://example.org/mcp'}});
  fireEvent.click(screen.getByRole('radio', {name: 'Codex'}));
  expect(screen.queryByLabelText('URL')).toBeNull();
  fireEvent.click(screen.getByText('安装'));
  await waitFor(() => expect(install).toHaveBeenCalledWith('/source', {source_format: 'codex', classifications: {}, public_values: {}}));
});

it('requires preview for native packages too and retains a failed install', async () => {
  const native = {...preview, source_format: 'native' as const, mcp: []};
  const install = vi.fn(async () => false), close = vi.fn();
  render(<PluginImporter onPreview={async () => discovery([native])} onInstall={install} onClose={close} />);
  fireEvent.change(screen.getByLabelText('本地目录'), {target: {value: '/source'}});
  expect((screen.getByText('安装') as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByText('读取预览'));
  await screen.findByText('技能：research');
  fireEvent.click(screen.getByText('安装'));
  await screen.findByRole('alert');
  expect(install).toHaveBeenCalledWith('/source', {source_format: 'native', classifications: {}, public_values: {}});
  expect(close).not.toHaveBeenCalled();
});

it('shows an invalid sibling without selecting or hiding it', async () => {
  const result = discovery([{...preview, mcp: []}]);
  result.candidates.push({source_format: 'claude', manifest: '.claude-plugin/plugin.json', preview: null, error: '不支持 apps'});
  render(<PluginImporter onPreview={async () => result} onInstall={vi.fn()} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('本地目录'), {target: {value: '/source'}});
  fireEvent.click(screen.getByText('读取预览'));
  await screen.findByText('不支持 apps');
  expect((screen.getByRole('radio', {name: 'Claude'}) as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole('radio', {name: 'Cursor'}) as HTMLInputElement).checked).toBe(false);
  expect((screen.getByText('安装') as HTMLButtonElement).disabled).toBe(true);
});
