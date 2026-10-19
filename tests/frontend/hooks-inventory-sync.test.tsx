import {act, cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {HooksPanel} from '../../frontend/components/hooks-panel';
import type {HookConfigText, HookSource, UserHooksApi} from '../../frontend/lib/hooks-api';

afterEach(() => {cleanup(); vi.restoreAllMocks();});
const source: HookSource = {path: '/fixture/hooks.json', source_disposition: 'COMPLETE', trust_state: 'TRUSTED',
  enabled: true, definition_digest: 'existing-definition', trusted_definition_digest: 'existing-definition',
  trusted_at: null, definition_count: 1, can_trust: true, exists: true, diagnostics: []};
const config = (commands: string[]): HookConfigText => ({exists: true, path: source.path,
  text: JSON.stringify({hooks: {SessionStart: commands.map(command => ({hooks: [{type: 'command', command}]}))}})});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => {resolve = done;});
  return {promise, resolve};
}
function fixture() {
  const api = {list: vi.fn<UserHooksApi['list']>().mockResolvedValue({sources: [source], details: []}),
    config: vi.fn<UserHooksApi['config']>().mockResolvedValue(config(['echo old'])),
    save: vi.fn<UserHooksApi['save']>().mockResolvedValue({status: 'APPLIED', message: null, pending_sessions: 0}),
    review: vi.fn<UserHooksApi['review']>().mockResolvedValue(source),
    trust: vi.fn<UserHooksApi['trust']>().mockResolvedValue({status: 'APPLIED', message: null, pending_sessions: 0})};
  const count = vi.fn();
  const props = {api, query: '', refresh: 0, inventory: {}, onNotify: vi.fn(), onCountChange: count};
  const view = render(<HooksPanel {...props}/>);
  return {api, props, view, count};
}
async function expand() {
  fireEvent.click(await screen.findByRole('button', {name: '查看 本地 Hooks'}));
  await screen.findByText('echo old');
}

it('refreshes expanded read-only rules after accepted inventory and source-list changes', async () => {
  const {api, props, view, count} = fixture();
  await expand();
  const pending = deferred<HookConfigText>();
  api.list.mockResolvedValue({sources: [{...source, definition_count: 2}], details: []});
  api.config.mockReturnValueOnce(pending.promise);
  view.rerender(<HooksPanel {...props} inventory={{changed: true}}/>);
  await screen.findByText('正在读取规则…');
  expect(screen.queryByText('echo old')).toBeNull();
  expect(screen.queryByText('暂无规则')).toBeNull();
  expect(count).toHaveBeenLastCalledWith(2);
  await act(async () => pending.resolve(config(['echo new', 'echo second'])));
  expect(await screen.findByText('echo new')).toBeTruthy();
  expect(screen.getByText('echo second')).toBeTruthy();
  api.config.mockResolvedValue(config(['echo refreshed']));
  view.rerender(<HooksPanel {...props} refresh={1}/>);
  expect(await screen.findByText('echo refreshed')).toBeTruthy();
  expect(screen.queryByText('echo new')).toBeNull();
});

it('ignores late read-only config responses after another source inventory is accepted', async () => {
  const {api, props, view} = fixture();
  await expand();
  const stale = deferred<HookConfigText>();
  api.config.mockReturnValueOnce(stale.promise);
  view.rerender(<HooksPanel {...props} inventory={{revision: 2}}/>);
  await waitFor(() => expect(api.config).toHaveBeenCalledTimes(2));
  const staleSignal = api.config.mock.calls[1][0]!;
  api.config.mockResolvedValue(config(['echo newest']));
  view.rerender(<HooksPanel {...props} inventory={{revision: 3}}/>);
  expect(await screen.findByText('echo newest')).toBeTruthy();
  expect(staleSignal.aborted).toBe(true);
  await act(async () => stale.resolve(config(['echo stale'])));
  expect(screen.queryByText('echo stale')).toBeNull();
  expect(screen.getByText('echo newest')).toBeTruthy();
});

it.each(['json', 'rule'] as const)('preserves an open %s draft and original CAS baseline while read-only rules refresh', async mode => {
  const {api, props, view} = fixture();
  const baseline = config(['echo old']);
  await expand();
  fireEvent.click(screen.getByRole('button', {name: mode === 'json' ? '查看和编辑 Hooks JSON' : '编辑规则 1'}));
  const dialog = await screen.findByRole('dialog');
  const editor = within(dialog).getByRole('textbox', {name: mode === 'json' ? 'Hook 配置 JSON' : '规则 1 的命令 1'});
  const draft = mode === 'json' ? config(['echo user draft']).text : 'echo user draft';
  fireEvent.change(editor, {target: {value: draft}});
  api.config.mockResolvedValue(config(['echo external']));
  api.list.mockResolvedValue({sources: [{...source, definition_digest: 'externally-updated'}], details: []});
  view.rerender(<HooksPanel {...props} inventory={{changed: true}}/>);
  await screen.findByText('echo external');
  expect((editor as HTMLTextAreaElement).value).toBe(draft);
  api.save.mockRejectedValue(new Error('配置已在其他位置修改'));
  fireEvent.click(within(dialog).getByRole('button', {name: '保存配置'}));
  await within(dialog).findByRole('alert');
  expect(api.save).toHaveBeenCalledWith(baseline, expect.stringContaining('echo user draft'));
  expect(api.save.mock.calls[0][1]).not.toContain('echo external');
  expect((editor as HTMLTextAreaElement).value).toBe(draft);
});

it.each(['source', 'list'] as const)('does not present stale rules as complete after a temporary %s failure', async failure => {
  const {api, props, view, count} = fixture();
  await expand();
  if (failure === 'source') {
    api.list.mockResolvedValue({sources: [{...source, source_disposition: 'UNAVAILABLE', trust_state: 'UNAVAILABLE', definition_count: 0}], details: []});
  } else {api.list.mockRejectedValue(new Error('来源暂时不可读取'));}
  view.rerender(<HooksPanel {...props} inventory={{failed: true}}/>);
  await waitFor(() => expect(screen.queryByText('echo old')).toBeNull());
  expect(screen.queryByText('暂无规则')).toBeNull();
  expect(screen.getAllByRole('alert').length).toBeGreaterThan(0);
  if (failure === 'list') expect(count).toHaveBeenLastCalledWith(undefined);
  expect(api.config).toHaveBeenCalledTimes(1);
  api.list.mockResolvedValue({sources: [source], details: []});
  api.config.mockResolvedValue(config(['echo recovered']));
  view.rerender(<HooksPanel {...props} inventory={{recovered: true}}/>);
  expect(await screen.findByText('echo recovered')).toBeTruthy();
});
