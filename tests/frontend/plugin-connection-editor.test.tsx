import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {PluginConnectionEditor} from '../../frontend/components/plugin-connection-editor';
import type {PluginMcpConnection, UserPluginCapability} from '../../frontend/lib/pulsara-types';

afterEach(cleanup);
const plugin: UserPluginCapability = {
  id: 'docs', name: 'Docs', description: 'A fixture', enabled: false,
  packageInstallId: {visibility: 'VISIBLE', value: 'package:aaaaaaaaaaaaaaaaaaaaaaaaaa'}, packageRoot: '/tmp/package', skillCount: 0, mcpCount: 1, hookCount: 3,
  effectiveSkillNames: [], effectiveMcpServerIds: [], details: [],
};
const connection: PluginMcpConnection = {
  serverId: 'search', credentialOwner: {kind: 'plugin', scope_key: 'user', plugin_id: 'docs', server_id: 'search'},
  defaults: {transport: {type: 'streamable_http', endpoint: 'https://default.example/mcp'}, auth: {type: 'none'}, public_headers: {}},
  config: {transport: {type: 'streamable_http', endpoint: 'https://instance.example/mcp'}, auth: {type: 'none'}, public_headers: {}},
  overlay: {local_server_id: 'search', transport_kind: 'streamable_http', endpoint: 'https://instance.example/mcp', public_headers: {}, environment: {}, secret_environment: {}, auth: {type: 'none'}},
};

describe('Plugin connection editor', () => {
  it('keeps a withheld external block distinct from empty editable configuration', () => {
    const save = vi.fn(async () => true);
    const close = vi.fn();
    const config = {...connection.config, public_headers: undefined, withheld_fields: ['public_headers']};
    render(<PluginConnectionEditor plugin={plugin} connection={{...connection, config}} onSave={save} onClose={close} />);
    expect(screen.getByText('部分配置因凭据保护无法显示，当前不能保存或测试此配置。')).toBeTruthy();
    const button = screen.getByRole('button', {name: '保存连接'}) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', {name: '取消'}));
    expect(close).toHaveBeenCalledOnce();
  });

  it('restores the immutable default only after a second explicit confirmation', async () => {
    const save = vi.fn(async () => true);
    const close = vi.fn();
    render(<PluginConnectionEditor plugin={plugin} connection={connection} onSave={save} onClose={close} onAuthorization={vi.fn()} />);
    fireEvent.click(screen.getByText('插件默认连接（只读）'));
    fireEvent.click(screen.getByRole('button', {name: '恢复插件默认连接'}));
    expect(save).not.toHaveBeenCalled();
    expect(screen.getByText(/恢复默认连接后，不再使用的本机凭据和授权也会清除/)).toBeTruthy();
    expect(document.body.textContent).toContain('https://default.example/mcp');
    fireEvent.click(screen.getByRole('button', {name: '确认恢复默认连接'}));
    await waitFor(() => expect(save).toHaveBeenCalledWith({overlay: null, secretChanges: []}));
    expect(close).toHaveBeenCalledOnce();
  });

  it('does not expose executable or effect policy mutations through the connection editor', async () => {
    const save = vi.fn(async () => true);
    const stdio = {...connection, config: {transport: {type: 'stdio', command: 'npx', args: ['-y', 'fixture'], cwd: '.', env: {}}, auth: {type: 'none'}}, overlay: null};
    render(<PluginConnectionEditor plugin={plugin} connection={{...stdio, defaults: stdio.config}} onSave={save} onClose={vi.fn()} onAuthorization={vi.fn()} />);
    for (const name of ['命令', '参数（JSON 数组）', '工作目录（相对项目）']) {
      expect((screen.getByLabelText(name) as HTMLInputElement).disabled).toBe(true);
    }
    expect(screen.queryByLabelText('默认工具作用')).toBeNull();
    fireEvent.change(screen.getByLabelText('普通环境变量（JSON 对象，不填写密钥）'), {target: {value: '{"REGION":"west"}'}});
    fireEvent.click(screen.getByRole('button', {name: '保存连接'}));
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({overlay: {
      local_server_id: 'search', transport_kind: 'stdio', scope_policy: 'ROOT_ONLY', endpoint: null,
      public_headers: {}, environment: {REGION: 'west'}, secret_environment: {}, auth: {type: 'none'},
    }})));
  });
});


it('uses the common MCP scope toggle and saves its selected policy', async () => {
  const save = vi.fn(async () => true);
  render(<PluginConnectionEditor plugin={plugin} connection={{...connection, config: {...connection.config, scope_policy: 'ROOT_ONLY'}}} onSave={save} onClose={vi.fn()} />);
  const scope = screen.getByRole('checkbox', {name: '也向委派 Agent 提供'});
  expect((scope as HTMLInputElement).checked).toBe(false);
  fireEvent.click(scope);
  fireEvent.click(screen.getByRole('button', {name: '保存连接'}));
  await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({overlay: expect.objectContaining({scope_policy: 'ROOT_AND_SUBAGENTS'})})));
});
