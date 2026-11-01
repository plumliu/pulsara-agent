import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { PromptComposer } from '../../frontend/components/prompt-composer';
import { PromptDraftStore } from '../../frontend/lib/prompt-draft';
import type { McpPrompt, PreviewMcpPrompt } from '../../frontend/lib/mcp-prompts';

const stores: PromptDraftStore[] = [];
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} });
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} });
});
afterEach(() => { cleanup(); stores.forEach(s => s.destroy()); stores.length = 0; vi.unstubAllGlobals(); });
const target = {kind: 'MCP_PROMPT' as const, server_id: 'docs', name: 'build'};
function setup(args: McpPrompt['arguments'] = [], preview?: PreviewMcpPrompt) {
  const store = new PromptDraftStore(); stores.push(store);
  const prompt: McpPrompt = {target, name: 'build', description: '', server_name: 'Docs', arguments: args};
  const list = vi.fn(async () => ({state: 'ready' as const, items: [prompt], next_offset: null}));
  const request = vi.fn(preview ?? (async () => ({state: 'ready' as const, text: 'Build this\nexactly.'})));
  const submit = vi.fn();
  const props = {store, sessionId: 'one', disabled: false, placeholder: '输入', onSubmit: submit,
    onNotify: vi.fn(), skills: [], onListMcpPrompts: list, onPreviewMcpPrompt: request};
  const view = render(<PromptComposer {...props} />);
  const editor = store.getEditor('one');
  const type = (text: string) => act(() => {editor.commands.insertContent({type: 'text', text});});
  const choose = async (text = '/build') => {
    type(text);
    const option = await screen.findByRole('option', {name: '/docs/build'});
    expect(option.textContent).toContain('MCP Prompt');
    fireEvent.keyDown(screen.getByRole('textbox', {name: '发送给 Pulsara'}), {key: 'Enter'});
  };
  return {...view, store, editor, type, choose, list, request, submit, props};
}
it('inserts a no-argument prompt as plain text, preserving surrounding text and a single undo', async () => {
  const x = setup();
  x.type('before  after');
  act(() => {x.editor.commands.setTextSelection(8);});
  await x.choose();
  await waitFor(() => expect(x.store.summary('one').text).toBe('before Build this\nexactly. after'));
  expect(x.request).toHaveBeenCalledWith(target, {}, expect.any(AbortSignal));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(x.submit).not.toHaveBeenCalled();
  const content = await x.store.capture('one');
  expect(JSON.stringify(content.content)).not.toContain('MCP_PROMPT');
  act(() => {x.editor.commands.undo();});
  expect(x.store.summary('one').text).toBe('before /build after');
});
it('keeps the entire hierarchical slash active and queries the server path', async () => {
  const x = setup();
  await x.choose('/docs/build');
  expect(x.list).toHaveBeenLastCalledWith('docs/build', 0, expect.any(AbortSignal));
  await waitFor(() => expect(x.store.summary('one').text).toBe('Build this\nexactly.'));
});
it('shows fields and a preview area immediately and automatically fetches exact multiline arguments', async () => {
  const x = setup([{name: 'code', description: 'Source code', required: true}, {name: 'language', description: '', required: false}]);
  await x.choose();
  const panel = await screen.findByRole('dialog', {name: '提示词 build'});
  expect(panel.textContent).toContain('提示词预览');
  expect(panel.textContent).toContain('请填写 code');
  expect(x.request).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', {name: /生成预览|更新预览/})).toBeNull();
  const code = screen.getByLabelText('code（必填）');
  fireEvent.change(code, {target: {value: '  first\nsecond  '}});
  await screen.findByText(/Build this\s+exactly\./);
  expect(x.request).toHaveBeenCalledWith(target, {code: '  first\nsecond  '}, expect.any(AbortSignal));
  fireEvent.keyDown(code, {key: 'Enter'});
  expect(x.submit).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name: '插入输入框'}));
  expect(x.store.summary('one').text).toBe('Build this\nexactly.');
});
it('invalidates the old preview immediately and ignores a late cancelled response', async () => {
  let resolve!: (value: {state: 'ready'; text: string}) => void;
  const x = setup([{name: 'topic', description: '', required: false}], async () => new Promise(r => { resolve = r; }));
  await x.choose();
  await waitFor(() => expect(x.request).toHaveBeenCalledOnce());
  const signal = x.request.mock.calls[0][2];
  fireEvent.change(screen.getByLabelText('topic'), {target: {value: 'new'}});
  expect((screen.getByRole('button', {name: '插入输入框'}) as HTMLButtonElement).disabled).toBe(true);
  expect(signal.aborted).toBe(true);
  fireEvent.click(screen.getByRole('button', {name: '取消'}));
  await act(async () => {resolve({state: 'ready', text: 'stale'});});
  expect(x.store.summary('one').text).toBe('/build');
  expect(screen.queryByText('stale')).toBeNull();
});
it('closes on session switch or approval and never inserts unsupported partial results', async () => {
  const x = setup([], async () => ({state: 'unsupported_content', message: '此提示词包含暂不支持的消息内容。'}));
  await x.choose();
  await screen.findByText('此提示词包含暂不支持的消息内容。');
  expect(x.store.summary('one').text).toBe('/build');
  x.rerender(<PromptComposer {...x.props} interactionOpen />);
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(x.submit).not.toHaveBeenCalled();
});
it('waits until IME composition ends before requesting a preview', async () => {
  const x = setup([{name: 'topic', description: '', required: true}]); await x.choose();
  const input = screen.getByLabelText('topic（必填）');
  fireEvent.compositionStart(input); fireEvent.change(input, {target: {value: '中文'}});
  await act(async () => {await new Promise(r => setTimeout(r, 350));});
  expect(x.request).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input);
  await waitFor(() => expect(x.request).toHaveBeenCalledWith(target, {topic: '中文'}, expect.any(AbortSignal)));
});
