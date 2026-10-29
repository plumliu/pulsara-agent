import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { CapabilityView as ControlledCapabilityView, type CapabilityTab } from '../../frontend/components/capability-view';
import { useState, type ComponentProps } from 'react';
import type { UserCapabilitySnapshot, UserMcpServerCapability } from '../../frontend/lib/pulsara-types';
import type {HookSource} from '../../frontend/lib/hooks-api';

function CapabilityView(props: Omit<ComponentProps<typeof ControlledCapabilityView>, 'tab' | 'onTabChange'>) {
  const [tab, setTab] = useState<CapabilityTab>('plugins');
  return <ControlledCapabilityView {...props} tab={tab} onTabChange={setTab} />;
}

afterEach(cleanup);

const server: UserMcpServerCapability = {
  id: 'docs', name: '文档服务', config: {}, currentIdentity: 'docs-config', enabled: true,
  status: 'configured', required: false, availableToSubagents: true,
  toolCount: 0, resourceCount: 0, resourceTemplateCount: 0, promptCount: 0,
  instructions: '', hasFailure: false, transport: { kind: 'http', summary: 'https://example.com/mcp' }, tools: [],
};
const snapshot: UserCapabilitySnapshot = {
  roots: [], skills: { status: 'ready', configPath: '', items: [], issues: [], details: [], roots: [] },
  plugins: { status: 'ready', items: [], details: [] }, mcp: { configPath: '', servers: [server] },
};
const callbacks = {
  hooks: {list: vi.fn().mockResolvedValue({sources: [], details: []}), config: vi.fn(), save: vi.fn(), review: vi.fn(), trust: vi.fn()},
  onPickDirectory: vi.fn(), onNotify: vi.fn(), onRefresh: vi.fn(), onOpenRoot: vi.fn(), onInstallSkill: vi.fn(),
  onPreviewSkills: vi.fn(), onInstallPlugin: vi.fn(), onPreviewPlugin: vi.fn(),
  onCreateMcp: vi.fn(), onTestMcp: vi.fn(), onPreviewMcpImport: vi.fn(), onImportMcp: vi.fn(),
  onEditMcp: vi.fn(), onRemoveMcp: vi.fn(), onMcpAuthorization: vi.fn(),
  onToggleSkill: vi.fn(), onRemoveSkill: vi.fn(), onToggleMcp: vi.fn(),
  onTogglePlugin: vi.fn(), onRemovePlugin: vi.fn(), onEditPluginConnection: vi.fn(), onPluginMcpAuthorization: vi.fn(),
};

it('distinguishes unread inventory from a connected server with genuinely zero tools', () => {
  const view = render(<CapabilityView {...callbacks} loading={false} snapshot={snapshot} />);
  fireEvent.click(screen.getByRole('tab', { name: 'MCP 1' }));
  fireEvent.click(screen.getByText('文档服务'));
  expect(screen.getByText('尚未读取工具列表')).toBeTruthy();
  const stats = view.container.querySelector('.capability-detail-stats')!;
  expect(stats.textContent).not.toContain('0 工具');
  expect(stats.textContent).not.toContain('0 资源');
  view.rerender(<CapabilityView {...callbacks} loading={false} snapshot={{ ...snapshot, mcp: { ...snapshot.mcp, servers: [{ ...server, status: 'ready' }] } }} />);
  expect(screen.queryByText('尚未读取工具列表')).toBeNull();
  expect(stats.textContent).toContain('0 工具');
});

it('shows tool counts and tool descriptions from the connected session', () => {
  render(<CapabilityView {...callbacks} loading={false} snapshot={{ ...snapshot, mcp: { ...snapshot.mcp, servers: [{ ...server, status: 'ready', toolCount: 28, tools: [{ name: 'mcp__docs__search', remoteName: 'search', description: '搜索文档', effect: 'read-only', availableToSubagents: true, parallelSafe: true }] }] } }} />);
  fireEvent.click(screen.getByRole('tab', { name: 'MCP 1' }));
  fireEvent.click(screen.getByText('文档服务'));
  expect(screen.getByText('28')).toBeTruthy();
  expect(screen.getByText('mcp__docs__search')).toBeTruthy();
  expect(screen.getByText('搜索文档')).toBeTruthy();
  expect(within(screen.getByRole('tablist')).getByRole('tab', { name: 'MCP 1' })).toBeTruthy();
});

it('places Hooks directly after skills and uses the same list and dialog styles', async () => {
  callbacks.hooks.list.mockResolvedValueOnce({sources: [{path: '/home/hooks.json', source_disposition: 'COMPLETE', trust_state: 'UNTRUSTED', enabled: true, definition_count: 0, can_trust: false, exists: false, diagnostics: []}], details: []});
  callbacks.hooks.config.mockResolvedValue({exists: false, text: '', path: '/home/hooks.json'});
  const view = render(<CapabilityView {...callbacks} loading={false} snapshot={snapshot}/>);
  await screen.findByRole('tab', {name: '钩子 0'});
  expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['插件 0', 'MCP 1', '技能 0', '钩子 0']);
  fireEvent.click(screen.getByRole('tab', {name: '钩子 0'}));
  fireEvent.click(await screen.findByText('本地 Hooks'));
  expect(view.container.querySelector('.capability-page-list .capability-list-item .capability-list-row')).toBeTruthy();
  await waitFor(() => expect((screen.getByRole('button', {name: '添加配置'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', {name: '添加配置'}));
  const modal = await screen.findByRole('dialog');
  expect(modal.classList.contains('capability-add-panel')).toBe(true);
  expect(modal.querySelector('.capability-dialog-body .capability-field')).toBeTruthy();
});

const hookSource: HookSource = {path:'/home/hooks.json', source_disposition:'COMPLETE', trust_state:'TRUSTED', enabled:true,
  definition_digest:'current', trusted_definition_digest:'current', trusted_at:'approved', definition_count:2, can_trust:true, exists:true, diagnostics:[]};

it('counts all runnable user Hooks before opening the tab and keeps the count independent of search', async () => {
  const hooks = {...callbacks.hooks, list:vi.fn().mockResolvedValue({sources:[hookSource,
    {...hookSource, plugin_id:'active', definition_count:3},
    {...hookSource, plugin_id:'untrusted', trust_state:'UNTRUSTED', definition_count:4},
    {...hookSource, plugin_id:'modified', trust_state:'MODIFIED', definition_count:5},
    {...hookSource, plugin_id:'disabled', enabled:false, definition_count:6},
    {...hookSource, plugin_id:'unavailable', source_disposition:'UNAVAILABLE', definition_count:7}],details:[]})};
  const view = render(<CapabilityView {...callbacks} hooks={hooks} loading={false} snapshot={snapshot}/>);
  const tab = await screen.findByRole('tab', {name:'钩子 5'});
  expect(screen.queryByRole('button', {name:'查看 本地 Hooks'})).toBeNull();
  fireEvent.click(tab);
  fireEvent.change(screen.getByPlaceholderText('搜索钩子'), {target:{value:'no match'}});
  expect(screen.getByRole('tab', {name:'钩子 5'})).toBeTruthy();
  expect(screen.getByText('没有匹配的 Hook 来源。')).toBeTruthy();
  hooks.list.mockResolvedValue({sources:[hookSource],details:[]});
  view.rerender(<CapabilityView {...callbacks} hooks={hooks} loading={false} snapshot={{...snapshot}}/>);
  await screen.findByRole('tab', {name:'钩子 2'});
});

it('does not present a partial or failed Hook observation as a zero active count', async () => {
  const hooks = {...callbacks.hooks, list:vi.fn().mockResolvedValue({sources:[hookSource],details:[]})};
  render(<CapabilityView {...callbacks} hooks={hooks} loading={false} snapshot={snapshot}/>);
  await screen.findByRole('tab', {name:'钩子 2'});
  hooks.list.mockResolvedValueOnce({sources:[hookSource],details:['插件来源暂时不可读']});
  fireEvent.click(screen.getByRole('button', {name:'刷新能力'}));
  await screen.findByRole('tab', {name:'钩子 —'});
  hooks.list.mockRejectedValueOnce(new Error('读取失败'));
  fireEvent.click(screen.getByRole('button', {name:'刷新能力'}));
  fireEvent.click(screen.getByRole('tab', {name:'钩子 —'}));
  await screen.findByText('读取失败');
  expect(screen.getByRole('tab', {name:'钩子 —'})).toBeTruthy();
});

it('updates the active count after a complete source is trusted', async () => {
  const source = {...hookSource, trust_state:'UNTRUSTED', definitions:[{event:'SessionStart', command:'echo first', matcher:'', async:false},{event:'Stop', command:'echo second', matcher:'', async:false}]};
  const hooks = {...callbacks.hooks, list:vi.fn().mockResolvedValue({sources:[source],details:[]}),
    config:vi.fn().mockResolvedValue({exists:false,text:'',path:source.path}), review:vi.fn().mockResolvedValue(source),
    trust:vi.fn().mockResolvedValue({status:'APPLIED',message:null,pending_sessions:0})};
  render(<CapabilityView {...callbacks} hooks={hooks} loading={false} snapshot={snapshot}/>);
  fireEvent.click(await screen.findByRole('tab', {name:'钩子 0'}));
  fireEvent.click(screen.getByText('本地 Hooks'));
  fireEvent.click(screen.getByRole('button', {name:'审阅并信任'}));
  const confirm = await screen.findByRole('button', {name:'确认并信任'});
  hooks.list.mockResolvedValue({sources:[{...source,trust_state:'TRUSTED'}],details:[]});
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(confirm);
  await screen.findByRole('tab', {name:'钩子 2'});
  expect(hooks.trust).toHaveBeenCalledWith(source);
});

it('groups toolbar actions while keeping display refresh separate from explicit reload', () => {
  const onRefresh = vi.fn(), onReload = vi.fn();
  const view = render(<CapabilityView {...callbacks} loading={false} snapshot={snapshot} onRefresh={onRefresh} onReload={onReload} />);
  const controls = view.container.querySelector('.capability-page-controls')! as HTMLElement;
  fireEvent.click(within(controls).getByRole('button', { name: '刷新能力' }));
  expect(onRefresh).toHaveBeenCalledOnce();
  expect(onReload).not.toHaveBeenCalled();
  fireEvent.click(within(controls).getByRole('button', { name: '重新加载会话能力' }));
  expect(onReload).toHaveBeenCalledOnce();
});


it('saves an adjusted MCP scope from enable review before enabling with the same policy', async () => {
  const overlay = {local_server_id: 'search', transport_kind: 'streamable_http', scope_policy: 'ROOT_ONLY', endpoint: null,
    public_headers: {}, environment: {}, secret_environment: {}, auth: {type: 'none'}};
  const connection = {serverId: 'search', credentialOwner: {kind: 'plugin' as const, scope_key: 'user', plugin_id: 'docs', server_id: 'search'},
    defaults: {}, config: {scope_policy: 'ROOT_ONLY', transport: {type: 'streamable_http', endpoint: 'https://example.org'}, auth: {type: 'none'}}, overlay};
  const plugin = {id: 'docs', name: 'Docs', description: '', enabled: false, packageInstallId: 'pkg', packageRoot: '/package',
    skillCount: 0, mcpCount: 1, hookCount: 0, effectiveSkillNames: [], effectiveMcpServerIds: [], details: [],
    mcpConnections: [connection], connectionReview: [{overlay, credentials: []}]};
  const edit = vi.fn(async () => true), enable = vi.fn(async () => true);
  render(<CapabilityView {...callbacks} onEditPluginConnection={edit} onTogglePlugin={enable} loading={false}
    snapshot={{...snapshot, plugins: {status: 'ready', items: [plugin], details: []}}} />);
  fireEvent.click(screen.getByRole('switch', {name: '开启 Docs'}));
  fireEvent.click(await screen.findByRole('checkbox', {name: 'search 为子代理也提供'}));
  expect(edit).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name: '确认启用'}));
  await waitFor(() => expect(enable).toHaveBeenCalledOnce());
  expect(edit).toHaveBeenCalledWith(expect.anything(), expect.anything(), {overlay: {...overlay, scope_policy: 'ROOT_AND_SUBAGENTS'}, secretChanges: []});
  expect(enable.mock.calls[0][0].connectionReview?.[0].overlay.scope_policy).toBe('ROOT_AND_SUBAGENTS');
  expect(edit.mock.invocationCallOrder[0]).toBeLessThan(enable.mock.invocationCallOrder[0]);
});
