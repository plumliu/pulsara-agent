import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { McpEditor } from './mcp-editor';
import type { UserMcpServerCapability } from '../lib/pulsara-types';

afterEach(cleanup);
const binding = { owner: { kind: 'local', scope_key: 'user', server_id: 'docs', plugin_id: null }, name: 'bearer' };
const server: UserMcpServerCapability = {
  id: 'docs', name: '文档', enabled: true, status: 'configured', required: false,
  availableToSubagents: false, toolCount: 0, resourceCount: 0, resourceTemplateCount: 0, promptCount: 0,
  instructions: '', hasFailure: false, transport: { kind: 'http', summary: 'https://example.org/mcp' }, tools: [], currentIdentity: 'current-docs',
  config: { display_name: '文档', enabled: true, transport: { type: 'streamable_http', endpoint: 'https://example.org/mcp', network_policy: 'PUBLIC_ONLY' }, auth: { type: 'bearer', reference: { source: 'managed', binding } }, exposure_policy: { include_tool_names: ['search'] }, default_tool_timeout_ms: 45000 },
};

describe('MCP connection editor', () => {
  it('keeps advanced checkbox labels clickable and saves their exact policy values', async () => {
    const save = vi.fn(async () => true);
    render(<McpEditor server={server} onSave={save} onClose={vi.fn()} />);
    fireEvent.click(screen.getByText('更多连接选项'));
    for (const name of ['允许本机 HTTP 测试地址', '服务明确支持无状态请求', '将此服务标记为必需', '服务明确支持并行工具调用']) {
      const input = screen.getByLabelText(name) as HTMLInputElement;
      expect(input.checked).toBe(false);
      fireEvent.click(input.closest('label')!);
      expect(input.checked).toBe(true);
    }
    expect(screen.getByLabelText('无状态请求并发数')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', {name: '保存连接'}));
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({
      config: expect.objectContaining({
        required: true, supports_parallel_tool_calls: true,
        transport: expect.objectContaining({allow_http_localhost: true, proved_stateless: true}),
      }),
    })));
  });

  it('distinguishes an oversized tool definition from failed authorization', async () => {
    render(<McpEditor server={server} onSave={vi.fn()} onClose={vi.fn()} onTest={vi.fn(async () => ({status: 'schema_bound_exceeded' as const, tools: 0, resources: 0, resource_templates: 0, prompts: 0}))} />);
    fireEvent.click(screen.getByRole('button', {name: '测试连接'}));
    await waitFor(() => expect(screen.getByText(/工具定义超出资源限制/)).toBeTruthy());
    expect(screen.getByText(/不是登录授权失败/)).toBeTruthy();
    expect((screen.getByRole('button', {name: '保存连接'}) as HTMLButtonElement).disabled).toBe(false);
  });
  it('selects an OAuth client secret environment reference without reading the environment', async () => {
    const save = vi.fn(async () => true);
    render(<McpEditor server={server} onSave={save} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('认证方式'), {target: {value: 'oauth'}});
    fireEvent.change(screen.getByLabelText('客户端 ID（可选）'), {target: {value: 'client'}});
    fireEvent.change(screen.getByLabelText('客户端密钥来源'), {target: {value: 'environment'}});
    fireEvent.change(screen.getByLabelText('客户端密钥环境变量名'), {target: {value: 'DOCS_OAUTH_SECRET'}});
    fireEvent.click(screen.getByRole('button', {name: '保存连接'}));
    await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({
      config: expect.objectContaining({auth: expect.objectContaining({type: 'oauth', client_id: 'client', client_secret: {source: 'environment', name: 'DOCS_OAUTH_SECRET'}})}),
      secretChanges: [],
    })));
  });

  it('edits native policies without inventing a separate request template', async () => {
    const save = vi.fn(async () => true);
    render(<McpEditor server={server} onSave={save} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('单次工具超时（毫秒）'), {target: {value: '30000'}});
    fireEvent.change(screen.getByLabelText('隐藏的工具名（每行一个）'), {target: {value: 'write\ndelete'}});
    fireEvent.change(screen.getByLabelText('默认工具作用'), {target: {value: 'READ_ONLY'}});
    fireEvent.change(screen.getByLabelText('单独设置工具超时（JSON：工具名 → 毫秒）'), {target: {value: '{"search": 15000}'}});
    fireEvent.click(screen.getByRole('button', {name: '保存连接'}));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save).toHaveBeenCalledWith(expect.objectContaining({
      config: expect.objectContaining({default_tool_timeout_ms: 30000,
        per_tool_timeout_ms: {search: 15000}, effect_policy: {default_effect: 'READ_ONLY'},
        exposure_policy: {include_tool_names: ['search'], exclude_tool_names: ['write', 'delete']}}),
      secretChanges: [],
    }));
  });

  it('switches a saved bearer to an environment reference without copying its value', async () => {
    const save = vi.fn(async () => true);
    render(<McpEditor server={server} onSave={save} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('Token 来源'), {target: {value: 'environment'}});
    fireEvent.change(screen.getByLabelText('Token 环境变量名'), {target: {value: 'DOCS_TOKEN'}});
    expect(screen.queryByLabelText('Token')).toBeNull();
    fireEvent.click(screen.getByRole('button', {name: '保存连接'}));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save).toHaveBeenCalledWith(expect.objectContaining({
      config: expect.objectContaining({auth: {type: 'bearer', reference: {source: 'environment', name: 'DOCS_TOKEN'}}}),
      secretChanges: [],
    }));
  });

  it('submits private credentials separately from the complete non-secret connection', async () => {
    const save = vi.fn(async () => true);
    render(<McpEditor onSave={save} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('服务 ID'), { target: { value: 'docs' } });
    fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'https://example.org/mcp' } });
    fireEvent.change(screen.getByLabelText('认证方式'), { target: { value: 'bearer' } });
    fireEvent.change(screen.getByLabelText('Token'), { target: { value: 'test-private-value' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    const input = save.mock.calls[0] as unknown as [{ config: object; secretChanges: unknown[] }];
    expect(JSON.stringify(input[0].config)).not.toContain('test-private-value');
    expect(input[0].secretChanges).toEqual([{ binding, value: 'test-private-value' }]);
    expect(document.body.textContent).not.toContain('test-private-value');
  });

  it('retains untouched policies and credentials without a read-back secret value', async () => {
    const save = vi.fn(async () => true);
    render(<McpEditor server={server} onSave={save} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: '新名称' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ config: expect.objectContaining({ display_name: '新名称', auth: server.config.auth, exposure_policy: server.config.exposure_policy, default_tool_timeout_ms: 45000 }), secretChanges: [] }));
  });

  it('requires explicit credential reuse confirmation for a different destination', async () => {
    const save = vi.fn(async () => true);
    render(<McpEditor server={server} onSave={save} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'https://new.example.org/mcp' } });
    expect((screen.getByRole('button', { name: '保存连接' }) as HTMLButtonElement).disabled).toBe(true);
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText('我确认将保留的凭据用于新的连接目标。'));
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  });

  it('switches to NoAuth without silently preserving the prior bearer binding', async () => {
    const save = vi.fn(async () => true);
    render(<McpEditor server={server} onSave={save} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('认证方式'), { target: { value: 'none' } });
    fireEvent.click(screen.getByRole('button', { name: '保存连接' }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ config: expect.objectContaining({ auth: { type: 'none' } }), secretChanges: [] }));
  });
});
