import {act, cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {HooksPanel} from '../../frontend/components/hooks-panel';
import {LocalHttpRuntimeAdapter} from '../../frontend/lib/runtime-adapter';
import type {HookSource, UserHooksApi} from '../../frontend/lib/hooks-api';

afterEach(() => {cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals();});
const source: HookSource = {path: '/home/hooks.json', source_disposition: 'COMPLETE', trust_state: 'UNTRUSTED', enabled: true,
  definition_digest: 'current', trusted_definition_digest: null, trusted_at: null, definition_count: 1, can_trust: true,
  exists: true, diagnostics: [{code: 'ignored', message: 'Ignored unsupported handler'}], declaration_environment: {PLUGIN_ROOT: '/package'},
  definitions: [{event: 'PreToolUse', command: 'echo review', commandWindows: 'echo windows', matcher: 'Read', timeout: 4, async: false, additionalContextLimit: 10}]};
function fixture(overrides: Partial<UserHooksApi> = {}) {
  const api = {list: vi.fn().mockResolvedValue({sources: [source], details: []}), config: vi.fn().mockResolvedValue({exists: true, text: '{broken', path: source.path}),
    save: vi.fn().mockResolvedValue({status: 'APPLIED', message: null, pending_sessions: 1}), review: vi.fn().mockResolvedValue(source),
    trust: vi.fn().mockResolvedValue({status: 'APPLIED', message: null, pending_sessions: 1}), ...overrides};
  const notify = vi.fn();
  const props = {api, query: '', refresh: 0, onNotify: notify};
  const view = render(<HooksPanel {...props}/>);
  return {api, notify, props, view};
}
async function expand() {const row = await screen.findByText('本地 Hooks'); await act(async () => {fireEvent.click(row);});}

it('keeps prior rows during refresh and discards unmounted reads', async () => {
  let complete!: (value: unknown) => void;
  const {api, props, view} = fixture();
  await expand();
  api.list.mockImplementationOnce(() => new Promise(done => {complete = done;}));
  view.rerender(<HooksPanel {...props} refresh={1}/>);
  expect(screen.getByText('本地 Hooks')).toBeTruthy();
  expect(screen.queryByText('正在读取 Hooks…')).toBeNull();
  view.unmount();
  await act(async () => complete({sources: [], details: []}));
  expect(screen.queryByText('本地 Hooks')).toBeNull();
});

it('shows missing local config and seeds creation, never edits a plugin', async () => {
  const {api} = fixture({list: vi.fn().mockResolvedValue({sources: [{...source, exists: false, can_trust: false, definition_count: 0}, {...source, plugin_id: 'pony'}], details: []}),
    config: vi.fn().mockResolvedValue({exists: false, text: '', path: source.path})});
  await expand();
  expect(screen.getByText(/未配置/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name: '添加配置'}));
  expect((await screen.findByRole('combobox', {name: '触发事件'}) as HTMLSelectElement).value).toBe('SessionStart');
  expect((screen.getByRole('textbox', {name: '规则 1 的命令 1'}) as HTMLTextAreaElement).value).toBe('');
  fireEvent.click(screen.getByRole('button', {name: '取消'}));
  fireEvent.click(screen.getByText('pony'));
  expect(within(screen.getByText('pony').closest('article')!).queryByRole('button', {name: '查看和编辑 Hooks JSON'})).toBeNull();
  expect(api.save).not.toHaveBeenCalled();
});

it('preserves bad JSON draft and baseline on conflict until explicitly reloaded', async () => {
  const {api} = fixture({save: vi.fn().mockRejectedValue(new Error('配置已在其他位置修改'))});
  await expand(); fireEvent.click(screen.getByRole('button', {name: '查看和编辑 Hooks JSON'}));
  const editor = await screen.findByRole('textbox', {name: 'Hook 配置 JSON'});
  fireEvent.change(editor, {target: {value: '{"hooks":{}}'}});
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  await screen.findByRole('alert');
  expect((editor as HTMLTextAreaElement).value).toBe('{"hooks":{}}');
  expect(api.save).toHaveBeenCalledWith({exists: true, text: '{broken', path: source.path}, '{"hooks":{}}');
  api.config.mockResolvedValueOnce({exists: true, text: '{"hooks":{},"description":"new"}', path: source.path});
  fireEvent.click(screen.getByRole('button', {name: '重新读取，替换草稿'}));
  await waitFor(() => expect((editor as HTMLTextAreaElement).value).toContain('new'));
});

it('preserves a CRLF baseline and CRLF edits', async () => {
  const {api} = fixture({config: vi.fn().mockResolvedValue({exists: true, text: '{\r\n"hooks":{}\r\n}', path: source.path})});
  await expand(); fireEvent.click(screen.getByRole('button', {name: '查看和编辑 Hooks JSON'}));
  const editor = await screen.findByRole('textbox', {name: 'Hook 配置 JSON'});
  fireEvent.change(editor, {target: {value: '{\n"hooks":{},\n"description":"new"\n}'}});
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  await waitFor(() => expect(api.save).toHaveBeenCalledWith(expect.objectContaining({text: '{\r\n"hooks":{}\r\n}'}), '{\r\n"hooks":{},\r\n"description":"new"\r\n}'));
});

it('freezes a complete source review and requires acceptance, preserving disabled state', async () => {
  const {api, notify, props, view} = fixture({review: vi.fn().mockResolvedValue({...source, enabled: false})});
  await expand(); fireEvent.click(screen.getByRole('button', {name: '审阅并信任'}));
  const confirm = await screen.findByRole('button', {name: '确认并信任'});
  expect((confirm as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText('echo review')).toBeTruthy();
  expect(screen.getByText('echo windows')).toBeTruthy();
  expect(screen.queryByText('当前未发现匹配工具')).toBeNull();
  api.list.mockResolvedValueOnce({sources: [{...source, definition_digest: 'later'}], details: []});
  view.rerender(<HooksPanel {...props} refresh={1}/>);
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(confirm);
  await waitFor(() => expect(api.trust).toHaveBeenCalledWith({...source, enabled: false}));
  expect(notify).toHaveBeenCalledWith('Hooks 已更新', expect.stringContaining('已信任，来源仍关闭'), 'success');
});

it('blocks repeated submit, escape, close and cancel while settlement is pending', async () => {
  let complete!: (value: unknown) => void;
  const {api} = fixture({save: vi.fn(() => new Promise(done => {complete = done;})) as UserHooksApi['save']});
  await expand(); fireEvent.click(screen.getByRole('button', {name: '查看和编辑 Hooks JSON'}));
  const dialog = await screen.findByRole('dialog');
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  fireEvent.keyDown(dialog, {key: 'Escape'});
  fireEvent.click(screen.getByRole('button', {name: '取消'}));
  expect(api.save).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('dialog')).toBeTruthy();
  await act(async () => complete({status: 'APPLIED', message: null, pending_sessions: 0}));
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('retains the draft and exposes published-write attention without success', async () => {
  const {notify} = fixture({save: vi.fn().mockResolvedValue({status: 'ATTENTION', message: '配置已写入，但确认失败', pending_sessions: 0})});
  await expand(); fireEvent.click(screen.getByRole('button', {name: '查看和编辑 Hooks JSON'}));
  await screen.findByRole('dialog');
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  expect((await screen.findByText('配置已写入，但确认失败')).getAttribute('role')).toBe('alert');
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('{broken');
  expect(notify).not.toHaveBeenCalled();
});

it('isolates the shell, traps focus and restores the opener on cancel', async () => {
  const shell = document.createElement('main'); shell.className = 'pulsara-shell'; document.body.append(shell);
  try {
    fixture(); await expand();
    const opener = screen.getByRole('button', {name: '查看和编辑 Hooks JSON'}); opener.focus(); fireEvent.click(opener);
    const close = await screen.findByRole('button', {name: '关闭'});
    await waitFor(() => expect(shell.inert).toBe(true));
    expect(document.activeElement).toBe(screen.getByRole('textbox'));
    const save = screen.getByRole('button', {name: '保存配置'}); save.focus(); fireEvent.keyDown(save, {key: 'Tab'});
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(close, {key: 'Escape'});
    expect(shell.inert).toBeFalsy(); expect(document.activeElement).toBe(opener);
  } finally {shell.remove();}
});

it('uses exact raw baseline and submits only the minimal trust observation through the existing HTTP client', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({status: 'APPLIED', message: null, pending_sessions: 0}), {status: 200, headers: {'Content-Type': 'application/json'}}));
  vi.stubGlobal('fetch', fetch);
  const adapter = new LocalHttpRuntimeAdapter('test-hooks');
  await adapter.hooks.trust({...source, plugin_id: 'pony'});
  expect(fetch.mock.calls[0][0]).toBe('/api/capabilities/hooks/trust');
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({plugin_id: 'pony', definition_digest: 'current', trust: {enabled: true, trusted_definition_digest: null, trusted_at: null}, accepted: true});
  await adapter.hooks.save({exists: true, text: 'raw\r\n', path: '/ignored'}, 'new');
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({exists: true, expected_text: 'raw\r\n', text: 'new'});
});

it('preserves unchanged mixed newline bytes when editing a local substring', async () => {
  const baseline = '{\r\n"hooks":{},\n"description":"old"\r}';
  const {api} = fixture({config: vi.fn().mockResolvedValue({exists: true, text: baseline, path: source.path})});
  await expand(); fireEvent.click(screen.getByRole('button', {name: '查看和编辑 Hooks JSON'}));
  const editor = await screen.findByRole('textbox', {name: 'Hook 配置 JSON'});
  fireEvent.change(editor, {target: {value: baseline.replace(/\r\n?/g, '\n').replace('old', 'new')}});
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  await waitFor(() => expect(api.save).toHaveBeenCalledWith(expect.objectContaining({text: baseline}), baseline.replace('old', 'new')));
});

it('makes rows keyboard-expandable and keeps local and plugin local distinct', async () => {
  const {view} = fixture({list: vi.fn().mockResolvedValue({sources: [source, {...source, plugin_id: 'local'}], details: []})});
  const local = await screen.findByRole('button', {name: '查看 本地 Hooks'});
  const plugin = screen.getByRole('button', {name: '查看 local'});
  local.focus(); fireEvent.click(local);
  expect(local.getAttribute('aria-expanded')).toBe('true');
  expect(plugin.getAttribute('aria-expanded')).toBe('false');
  fireEvent.click(plugin);
  expect(local.getAttribute('aria-expanded')).toBe('false');
  expect(plugin.getAttribute('aria-expanded')).toBe('true');
  expect(view.container.querySelectorAll('.capability-list-item.is-expanded').length).toBe(1);
});

it('keeps trust and disabled-source semantics distinct in the compact status display', async () => {
  fixture({list: vi.fn().mockResolvedValue({sources: [{...source, enabled: false, trust_state: 'TRUSTED'}], details: []})});
  await screen.findByText('本地 Hooks');
  expect(screen.getByText('已信任')).toBeTruthy();
  expect(screen.getByText(/hooks.json.*已关闭/)).toBeTruthy();
  expect(screen.queryByText(/来源开启/)).toBeNull();
  expect(screen.queryByText(/保存配置与授予信任分别操作/)).toBeNull();
});

it('creates a Hook using event and command fields and requires a command before saving', async () => {
  const {api} = fixture({config: vi.fn().mockResolvedValue({exists: false, text: '', path: source.path})});
  await expand(); fireEvent.click(screen.getByRole('button', {name: '添加配置'}));
  const event = await screen.findByRole('combobox', {name: '触发事件'});
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  expect(api.save).not.toHaveBeenCalled();
  fireEvent.change(event, {target: {value: 'PreToolUse'}});
  fireEvent.change(screen.getByRole('textbox', {name: '规则 1 的命令 1'}), {target: {value: 'python /tmp/check.py'}});
  fireEvent.click(screen.getByText('匹配条件（可选）'));
  fireEvent.change(screen.getByLabelText('匹配条件'), {target: {value: 'read_file'}});
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1));
  expect(api.save.mock.calls[0][0]).toEqual({exists: false, text: '', path: source.path});
  expect(JSON.parse(api.save.mock.calls[0][1]).hooks.PreToolUse).toEqual([{matcher: 'read_file', hooks: [{type: 'command', command: 'python /tmp/check.py'}]}]);
  expect(api.trust).not.toHaveBeenCalled();
});

it('loads multiple commands and preserves unsupported fields when editing through the form', async () => {
  const baseline = '{"description":"keep","future":42,"hooks":{"PreToolUse":[{"matcher":"Read","hooks":[{"type":"command","command":"first","timeout":4,"statusMessage":"retain"},{"type":"command","command":"second","async":true}]}],"FutureEvent":true}}';
  const {api} = fixture({config: vi.fn().mockResolvedValue({exists: true, text: baseline, path: source.path})});
  await expand(); fireEvent.click(await screen.findByRole('button', {name: '编辑规则 1'}));
  const command = await screen.findByRole('textbox', {name: '规则 1 的命令 1'});
  expect((command as HTMLTextAreaElement).value).toBe('first');
  fireEvent.change(command, {target: {value: 'changed'}});
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  await waitFor(() => expect(api.save).toHaveBeenCalledWith({exists: true, text: baseline, path: source.path}, baseline.replace('"first"', '"changed"')));
});

it('appends one rule without rewriting existing rules or document metadata', async () => {
  const baseline = '{"metadata":12345678901234567890,"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"existing"}]}],"FutureEvent":true}}';
  const {api} = fixture({config: vi.fn().mockResolvedValue({exists: true, text: baseline, path: source.path})});
  await expand(); fireEvent.click(screen.getByRole('button', {name: '添加配置'}));
  await screen.findByRole('combobox', {name: '触发事件'});
  expect((screen.getByRole('textbox', {name: '规则 1 的命令 1'}) as HTMLTextAreaElement).value).toBe('');
  fireEvent.change(screen.getByRole('textbox', {name: '规则 1 的命令 1'}), {target: {value: 'added'}});
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1));
  expect(api.save.mock.calls[0][0].text).toBe(baseline);
  const saved = api.save.mock.calls[0][1];
  expect(saved).toContain('12345678901234567890');
  expect(saved).toContain('{"hooks":[{"type":"command","command":"existing"}]}');
  expect(JSON.parse(saved).hooks.SessionStart.map((group: {hooks: Array<{command:string}>}) => group.hooks[0].command)).toEqual(['existing', 'added']);
  expect(JSON.parse(saved).hooks.FutureEvent).toBe(true);
});

it('opens JSON from the source row without expanding and refreshes rules after saving it', async () => {
  const baseline = '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"old"}]}]}}';
  const next = '{"hooks":{"PreToolUse":[{"matcher":"Read","hooks":[{"type":"command","command":"new"}]}]}}';
  const api = fixture({config: vi.fn().mockResolvedValue({exists: true, text: baseline, path: source.path})}).api;
  const json = await screen.findByRole('button', {name: '查看和编辑 Hooks JSON'});
  expect(screen.getByText(source.path).closest('.capability-list-copy')).toBeTruthy();
  fireEvent.click(json);
  await screen.findByRole('dialog');
  expect(screen.getByRole('button', {name: '查看 本地 Hooks'}).getAttribute('aria-expanded')).toBe('false');
  fireEvent.click(screen.getByRole('button', {name: '取消'}));
  await expand();
  expect(await screen.findByText('old')).toBeTruthy();
  fireEvent.click(json);
  const editor = await screen.findByRole('textbox', {name: 'Hook 配置 JSON'});
  expect(screen.getByRole('button', {name: '查看 本地 Hooks'}).getAttribute('aria-expanded')).toBe('true');
  fireEvent.change(editor, {target: {value: next}});
  api.config.mockResolvedValue({exists: true, text: next, path: source.path});
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(await screen.findByText('new')).toBeTruthy();
  expect(screen.getByText('工具调用前')).toBeTruthy();
  expect(screen.getByText('匹配：Read')).toBeTruthy();
  expect(screen.queryByText('old')).toBeNull();
});

it('lists plugin commands without allowing managed package edits', async () => {
  fixture({list: vi.fn().mockResolvedValue({sources: [{...source, plugin_id: 'pony'}], details: []})});
  fireEvent.click(await screen.findByRole('button', {name: '查看 pony'}));
  expect(screen.getByText('echo review')).toBeTruthy();
  expect(screen.getByText('工具调用前')).toBeTruthy();
  expect(screen.queryByRole('button', {name: '添加配置'})).toBeNull();
  expect(screen.queryByRole('button', {name: '编辑规则 1'})).toBeNull();
  expect(screen.queryByRole('button', {name: '查看和编辑 Hooks JSON'})).toBeNull();
});

it('keeps the exact rule-list observation as the edit baseline after an external reorder', async () => {
  const baseline = '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"selected"}]}]}}';
  const {api} = fixture({config: vi.fn().mockResolvedValue({exists: true, text: baseline, path: source.path}), save: vi.fn().mockRejectedValue(new Error('配置已在其他位置修改'))});
  await expand();
  api.config.mockResolvedValue({exists: true, text: '{"hooks":{}}', path: source.path});
  fireEvent.click(screen.getByRole('button', {name: '编辑规则 1'}));
  fireEvent.change(await screen.findByRole('textbox', {name: '规则 1 的命令 1'}), {target: {value: 'edited'}});
  fireEvent.click(screen.getByRole('button', {name: '保存配置'}));
  expect(await screen.findByText('配置已在其他位置修改')).toBeTruthy();
  expect(api.save).toHaveBeenCalledWith({exists:true, text:baseline, path:source.path}, baseline.replace('selected', 'edited'));
  expect((screen.getByRole('textbox', {name: '规则 1 的命令 1'}) as HTMLTextAreaElement).value).toBe('edited');
});

it('discards stale expanded reads and keeps failures distinct from an empty rule list', async () => {
  let complete!: (value: unknown) => void;
  const {api, props, view} = fixture({config: vi.fn().mockImplementationOnce(() => new Promise(done => {complete = done;}))});
  await expand();
  api.config.mockRejectedValueOnce(new Error('文件暂时不可读'));
  view.rerender(<HooksPanel {...props} refresh={1}/>);
  expect(await screen.findByText('文件暂时不可读')).toBeTruthy();
  await act(async () => complete({exists:true, text:'{"hooks":{}}', path:source.path}));
  expect(screen.queryByText('暂无规则')).toBeNull();
  expect(screen.getByText('文件暂时不可读')).toBeTruthy();
});
