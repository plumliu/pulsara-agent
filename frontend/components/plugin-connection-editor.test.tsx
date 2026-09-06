import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {PluginConnectionEditor} from './plugin-connection-editor';
import type {PluginMcpConnection, UserPluginCapability} from '../lib/pulsara-types';

afterEach(cleanup);
const plugin: UserPluginCapability = {
  id: 'docs', name: 'Docs', description: 'A fixture', enabled: false,
  packageInstallId: 'pkg-fixture', packageRoot: '/tmp/package', skillCount: 0, mcpCount: 1,
  effectiveSkillNames: [], effectiveMcpServerIds: [], details: [],
};
const connection: PluginMcpConnection = {
  serverId: 'search', credentialOwner: {kind: 'plugin', scope_key: 'user', plugin_id: 'docs', server_id: 'search'},
  defaults: {transport: {type: 'streamable_http', endpoint: 'https://default.example/mcp'}, auth: {type: 'none'}, public_headers: {}},
  config: {transport: {type: 'streamable_http', endpoint: 'https://instance.example/mcp'}, auth: {type: 'none'}, public_headers: {}},
  overlay: {local_server_id: 'search', transport_kind: 'streamable_http', endpoint: 'https://instance.example/mcp', public_headers: {}, environment: {}, secret_environment: {}, auth: {type: 'none'}},
};

describe('Plugin connection editor', () => {
  it('restores the immutable default only after a second explicit confirmation', async () => {
    const save = vi.fn(async () => true);
    const close = vi.fn();
    render(<PluginConnectionEditor plugin={plugin} connection={connection} onSave={save} onClose={close} onAuthorization={vi.fn()} />);
    fireEvent.click(screen.getByText('插件默认连接（只读）'));
    fireEvent.click(screen.getByRole('button', {name: '恢复插件默认连接'}));
    expect(save).not.toHaveBeenCalled();
    expect(screen.getByText(/恢复上方默认目的地和认证设置/)).toBeTruthy();
    expect(document.body.textContent).toContain('https://default.example/mcp');
    fireEvent.click(screen.getByRole('button', {name: '确认恢复默认连接'}));
    await waitFor(() => expect(save).toHaveBeenCalledWith({overlay: null, secretChanges: []}));
    expect(close).toHaveBeenCalledOnce();
  });

  it('does not expose executable or policy mutations through the connection editor', async () => {
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
      local_server_id: 'search', transport_kind: 'stdio', endpoint: null,
      public_headers: {}, environment: {REGION: 'west'}, secret_environment: {}, auth: {type: 'none'},
    }})));
  });
});
