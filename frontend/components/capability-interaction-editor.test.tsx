import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {CapabilityInteractionEditor} from './capability-interaction-editor';
import type {RuntimeInteractionResolution} from '../lib/runtime-adapter';

afterEach(cleanup);

it('uses the shared connection editor and submits private values once, separately from the public candidate', async () => {
  const resolve = vi.fn<(value: RuntimeInteractionResolution) => Promise<boolean>>(async () => true);
  render(<CapabilityInteractionEditor onResolve={resolve} form={{action: 'ADD_LOCAL_MCP', scope: 'USER',
    prefill: {server_id: 'docs', config: {transport: {type: 'streamable_http', endpoint: 'https://example.org/mcp'}}},
    credential_owner: {kind: 'local', scope_key: 'user', server_id: 'docs', plugin_id: null}}} />);
  expect((screen.getByLabelText('服务 ID') as HTMLInputElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('认证方式'), {target: {value: 'bearer'}});
  fireEvent.change(screen.getByLabelText('Token'), {target: {value: 'form-private-fixture'}});
  fireEvent.click(screen.getByRole('button', {name: '保存连接'}));
  await waitFor(() => expect(resolve).toHaveBeenCalledTimes(1));
  const resolution = resolve.mock.calls[0][0];
  expect(resolution.kind).toBe('capability');
  if (resolution.kind !== 'capability') throw new Error('wrong form resolution');
  expect(resolution.decision).toBe('SUBMIT');
  expect(JSON.stringify(resolution.submission?.config)).not.toContain('form-private-fixture');
  expect(JSON.stringify(resolution.submission?.secret_changes)).toContain('form-private-fixture');
  expect(document.body.textContent).not.toContain('form-private-fixture');
});

it('does not turn a shared editor post-save close into a second CANCEL', async () => {
  let complete!: (value: boolean) => void;
  const resolve = vi.fn(() => new Promise<boolean>(done => { complete = done; }));
  render(<CapabilityInteractionEditor onResolve={resolve} form={{action: 'ADD_LOCAL_MCP', scope: 'USER',
    prefill: {server_id: 'docs', config: {transport: {type: 'streamable_http', endpoint: 'https://example.org/mcp'}}},
    credential_owner: {kind: 'local', scope_key: 'user', server_id: 'docs', plugin_id: null}}} />);
  fireEvent.click(screen.getByRole('button', {name: '保存连接'}));
  expect(resolve).toHaveBeenCalledTimes(1);
  await act(async () => { complete(true); });
  expect(resolve).toHaveBeenCalledTimes(1);
  expect(resolve.mock.calls[0]).toEqual([expect.objectContaining({decision: 'SUBMIT'})]);
});

it('shows normalized Plugin components and requires an explicit review before enable', async () => {
  const resolve = vi.fn(async () => true);
  render(<CapabilityInteractionEditor onResolve={resolve} form={{action: 'SET_PLUGIN_ENABLED', scope: 'USER',
    prefill: {plugin_id: 'example', enabled: true}, plugin: {skills: [{name: 'research', description: 'Investigate sources'}],
      mcp: [{server_id: 'docs', config: {transport: {type: 'streamable_http', endpoint: 'https://example.org/mcp'}, auth: {type: 'bearer'}}, credentials: [{name: 'bearer', present: true}]}], hooks: [{event: 'SessionStart', command: 'node hooks/start.js'}]}}} />);
  const confirm = screen.getByRole('button', {name: '确认并继续'}) as HTMLButtonElement;
  expect(confirm.disabled).toBe(true);
  expect(screen.getByText(/Investigate sources/)).toBeTruthy();
  expect(screen.getByText(/MCP · docs/)).toBeTruthy();
  expect(screen.getByText('https://example.org/mcp')).toBeTruthy();
  expect(screen.getByText('认证：Bearer Token')).toBeTruthy();
  expect(screen.getByText('bearer：已配置')).toBeTruthy();
  expect(screen.getByText('node hooks/start.js')).toBeTruthy();
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(confirm);
  await waitFor(() => expect(resolve).toHaveBeenCalledWith({kind: 'capability', decision: 'SUBMIT', submission: {enable_review_accepted: true}}));
});

it('cancels without a boolean ALLOW and without submitting a draft', async () => {
  const resolve = vi.fn(async () => true);
  render(<CapabilityInteractionEditor onResolve={resolve} form={{action: 'REMOVE_LOCAL_MCP', scope: 'USER', prefill: {server_id: 'docs'}}} />);
  fireEvent.click(screen.getByRole('button', {name: '取消'}));
  await waitFor(() => expect(resolve).toHaveBeenCalledWith({kind: 'capability', decision: 'CANCEL'}));
});
