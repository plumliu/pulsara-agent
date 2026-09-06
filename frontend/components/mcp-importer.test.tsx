import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {McpImporter} from './mcp-importer';

afterEach(cleanup);

it('keeps endpoint variables public and forwards only an explicit loopback choice', async () => {
  const onImport = vi.fn(async () => true);
  const onPreview = vi.fn(async () => [{server_id: 'local', transport: 'streamable_http' as const, notices: [], issues: [],
    fields: [{target: 'endpoint', private: false, template_literals: false,
      variables: [{name: 'HOST', has_default: false, environment: false, file_path: null}]}]}]);
  render(<McpImporter onPreview={onPreview} onImport={onImport} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('配置内容'), {target: {value: '{"url":"http://${HOST}/mcp"}'}});
  fireEvent.click(screen.getByRole('button', {name: '读取预览'}));
  const input = await screen.findByLabelText('HOST（待填写）');
  expect(input.getAttribute('type')).toBe('text');
  const loopback = screen.getByRole('checkbox', {name: '允许本机 HTTP 地址（仅用于本机服务）'});
  expect((loopback as HTMLInputElement).checked).toBe(false);
  fireEvent.change(input, {target: {value: '127.0.0.1:54130'}});
  fireEvent.click(loopback);
  fireEvent.click(screen.getByRole('button', {name: '确认导入此服务'}));
  await waitFor(() => expect(onImport).toHaveBeenCalledWith(expect.objectContaining({
    selected_server_id: 'local', allow_http_localhost: true, values: {HOST: '127.0.0.1:54130'},
  })));
});
