import {act, cleanup, fireEvent, screen, waitFor, within} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import PulsaraApp from '../../frontend/app/pulsara-app';
import type {RuntimeInteractionContent, RuntimeInteractionSummary} from '../../frontend/lib/runtime-adapter';
import {deferred, FakeAdapter, FakeConnection, projection, renderWorkbench} from './pulsara-app-fixture';

afterEach(() => {cleanup(); vi.restoreAllMocks(); vi.useRealTimers();});
const owner = {kind: 'local', scope_key: 'user', server_id: 'docs', plugin_id: null};
const config = {display_name: '文档服务', enabled: true, transport: {type: 'streamable_http', endpoint: 'https://docs.example/mcp'}, auth: {type: 'none'}};
async function open(content: RuntimeInteractionContent, summary?: RuntimeInteractionSummary) {
  const adapter = new FakeAdapter();
  adapter.interactionContent = content;
  const kind = content.kind;
  adapter.connectionValue = {...projection('请查看下面的内容。'), hostSessionId: 'host:cards', interaction: summary ?? (kind === 'plan-question' || kind === 'plan-draft'
    ? {id: 'interaction:card', kind, workflowId: 'workflow:card', workflowRevision: 7}
    : {id: 'interaction:card', kind: 'capability-form', prompt: '', options: []})};
  renderWorkbench(<PulsaraApp adapter={adapter} />);
  await screen.findByText('请查看下面的内容。');
  return adapter;
}
const capability = (action: string, prefill: Record<string, unknown> = {server_id: 'docs', config}, extras = {}) => ({kind: 'capability-form' as const, form: {action, scope: 'USER', prefill, credential_owner: owner, ...extras}});

it('saves an inline MCP through the conversation owner once and keeps the Token outside public config', async () => {
  const adapter = await open(capability('ADD_LOCAL_MCP'));
  const region = within(await screen.findByRole('region', {name: '连接 文档服务'}));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(document.querySelector('.capability-add-panel__backdrop')).toBeNull();
  expect(screen.queryByText('需要你的配置')).toBeNull();
  fireEvent.change(region.getByLabelText('认证方式'), {target: {value: 'bearer'}});
  fireEvent.change(region.getByLabelText('Token', {exact: true}), {target: {value: 'regression-token'}});
  fireEvent.click(region.getByRole('button', {name: '显示密钥'}));
  expect(region.getByLabelText('Token', {exact: true}).getAttribute('type')).toBe('text');
  fireEvent.click(region.getByRole('button', {name: '保存并继续'}));
  await waitFor(() => expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledTimes(1));
  const [interaction, result] = adapter.lastConnection!.resolveInteraction.mock.calls[0];
  expect(interaction).toEqual(expect.objectContaining({id: 'interaction:card', kind: 'capability-form'}));
  expect(result).toEqual({kind: 'capability', decision: 'SUBMIT', submission: {
    config: {...config, public_headers: {}, scope_policy: 'ROOT_ONLY', transport: {...config.transport, allow_http_localhost: false, network_policy: 'PUBLIC_ONLY', stateless_http_asserted: false}, auth: {type: 'bearer', reference: {source: 'managed', binding: {owner, name: 'bearer'}}}},
    secret_changes: [{binding: {owner, name: 'bearer'}, value: 'regression-token'}], retain_credentials_confirmed: false,
  }});
  expect(JSON.stringify(result.kind === 'capability' && result.submission?.config)).not.toContain('regression-token');
  fireEvent.click(region.getByRole('button', {name: '取消'}));
  expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledTimes(1);
});

it('retains the credential destination consent in an inline update', async () => {
  const prior = {...config, auth: {type: 'bearer', reference: {source: 'managed', binding: {owner, name: 'bearer'}}}};
  const adapter = await open(capability('UPDATE_LOCAL_MCP', {server_id: 'docs', config: prior}));
  const region = within(await screen.findByRole('region', {name: '编辑 文档服务 连接'}));
  fireEvent.change(region.getByLabelText('服务地址'), {target: {value: 'https://new.example/mcp'}});
  const save = region.getByRole('button', {name: '保存并继续'});
  expect(save.hasAttribute('disabled')).toBe(true);
  fireEvent.click(region.getByRole('checkbox', {name: '我确认将保留的凭据用于新的连接目标。'}));
  fireEvent.click(save);
  await waitFor(() => expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledTimes(1));
  expect(adapter.lastConnection!.resolveInteraction.mock.calls[0][1]).toEqual(expect.objectContaining({submission: expect.objectContaining({retain_credentials_confirmed: true, secret_changes: [], config: expect.objectContaining({auth: prior.auth, transport: expect.objectContaining({endpoint: 'https://new.example/mcp'})})})}));
});

it('saves a real stdio candidate from the inline editor without converting it to an HTTP connection', async () => {
  const adapter = await open(capability('ADD_LOCAL_MCP'));
  const region = within(await screen.findByRole('region', {name: '连接 文档服务'}));
  fireEvent.change(region.getByLabelText('连接方式'), {target: {value: 'stdio'}});
  fireEvent.change(region.getByLabelText('命令', {exact: true}), {target: {value: 'uv'}});
  fireEvent.change(region.getByLabelText('参数（JSON 数组）'), {target: {value: '["run","server.py"]'}});
  fireEvent.change(region.getByLabelText('工作目录（相对项目）'), {target: {value: '/tmp/mcp'}});
  fireEvent.click(region.getByRole('button', {name: '保存并继续'}));
  await waitFor(() => expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledTimes(1));
  expect(adapter.lastConnection!.resolveInteraction.mock.calls[0][1]).toEqual(expect.objectContaining({submission: expect.objectContaining({config: expect.objectContaining({transport: {type: 'stdio', command: 'uv', args: ['run', 'server.py'], cwd: '/tmp/mcp', env: {}, secret_env: {}}})})}));
});

it('keeps plugin overrides and private connection inputs in the inline plugin path', async () => {
  const pluginOwner = {...owner, kind: 'plugin', plugin_id: 'research'};
  const adapter = await open(capability('CONFIGURE_PLUGIN_MCP_CONNECTION', {server_id: 'docs', config}, {
    plugin: {name: '资料工具包', enabled: true, package_install_id: 'package:research'},
    connection: {server_id: 'docs', defaults: config, config, overlay: null, credential_owner: pluginOwner,
      connection_inputs: [{name: 'API_KEY', title: '服务密钥', private: true, required: true}]},
  }));
  const region = within(await screen.findByRole('region', {name: '配置 文档服务 连接'}));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.queryByText('只修改本机的连接配置')).toBeNull();
  fireEvent.change(region.getByLabelText('服务密钥', {exact: true}), {target: {value: 'plugin-private'}});
  fireEvent.change(region.getByLabelText('服务地址'), {target: {value: 'https://override.example/mcp'}});
  fireEvent.click(region.getByRole('button', {name: '保存并继续'}));
  await waitFor(() => expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledTimes(1));
  const result = adapter.lastConnection!.resolveInteraction.mock.calls[0][1];
  expect(result).toEqual(expect.objectContaining({submission: expect.objectContaining({overlay: expect.objectContaining({local_server_id: 'docs', endpoint: 'https://override.example/mcp'}), secret_changes: [{binding: {owner: pluginOwner, name: 'input:API_KEY'}, value: 'plugin-private'}]})}));
  expect(JSON.stringify(result.kind === 'capability' && result.submission?.overlay)).not.toContain('plugin-private');
});

it('keeps plugin review acceptance and OAuth cancellation on their original capability resolutions', async () => {
  const adapter = await open(capability('SET_PLUGIN_ENABLED', {plugin_id: 'research', enabled: true}, {plugin: {name: '资料工具包', skills: [{name: '资料检索', description: '保留引用'}], mcp: [], hooks: []}}));
  const enable = await screen.findByRole('button', {name: '启用插件'});
  expect(enable.hasAttribute('disabled')).toBe(true);
  fireEvent.click(screen.getByRole('checkbox', {name: '我已查看这些能力，并信任插件来源。'}));
  fireEvent.click(enable);
  await waitFor(() => expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledWith(expect.anything(), {kind: 'capability', decision: 'SUBMIT', submission: {enable_review_accepted: true}}));
  cleanup();
  const oauth = await open(capability('AUTHORIZE_MCP'));
  await screen.findByRole('button', {name: '前往登录'});
  expect(screen.queryByText(/接下来会打开|无需把登录信息|连接你的页面与工作区/)).toBeNull();
  expect(screen.queryByText('N', {exact: true})).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: '取消', exact: true}));
  await waitFor(() => expect(oauth.lastConnection!.resolveInteraction).toHaveBeenCalledWith(expect.anything(), {kind: 'capability', decision: 'CANCEL'}));
});

it.each(['allow', 'deny'] as const)('submits permission %s once through the session command owner', async decision => {
  const interaction = {id: 'interaction:permission', kind: 'tool-confirmation' as const, prompt: 'Allow write_file?', options: ['ALLOW', 'DENY'], expiresAtUtc: '2099-01-01T00:00:00Z', decisionInProgress: false};
  const adapter = await open({kind: 'tool-confirmation', prompt: interaction.prompt, options: interaction.options}, interaction);
  await screen.findByRole('heading', {name: '允许修改文件？'});
  const button = screen.getByRole('button', {name: decision === 'allow' ? '允许本次操作' : '拒绝', exact: true});
  const pending = deferred<Awaited<ReturnType<NonNullable<typeof adapter.lastConnection>['resolveInteraction']>>>();
  adapter.lastConnection!.resolveInteraction.mockReturnValueOnce(pending.promise);
  fireEvent.click(button); fireEvent.click(button);
  await waitFor(() => expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledTimes(1));
  expect(screen.getByRole('button', {name: '允许本次操作'}).hasAttribute('disabled')).toBe(true);
  expect(adapter.lastConnection!.resolveInteraction.mock.calls[0]).toEqual([interaction, {kind: 'tool', decision, commandId: expect.stringMatching(/^command:/)}]);
  const submitted = adapter.lastConnection!.resolveInteraction.mock.calls[0][1];
  if (submitted.kind !== 'tool') throw new Error('Expected tool decision');
  await act(async () => pending.resolve({commandId: submitted.commandId, publicCode: `INTERACTION_${decision.toUpperCase()}`, status: 'succeeded'}));
});

it.each(['expired', 'processing'] as const)('disables both permission choices while %s', async state => {
  const interaction = {id: 'interaction:permission', kind: 'tool-confirmation' as const, prompt: 'Allow terminal?', options: ['ALLOW', 'DENY'], expiresAtUtc: state === 'expired' ? '2000-01-01T00:00:00Z' : '2099-01-01T00:00:00Z', decisionInProgress: state === 'processing'};
  const adapter = await open({kind: 'tool-confirmation', prompt: interaction.prompt, options: interaction.options}, interaction);
  await screen.findByRole('heading', {name: '允许运行命令？'});
  expect(screen.getByRole('button', {name: '拒绝'}).hasAttribute('disabled')).toBe(true);
  expect(screen.getByRole('button', {name: '允许本次操作'}).hasAttribute('disabled')).toBe(true);
  expect(adapter.lastConnection!.resolveInteraction).not.toHaveBeenCalled();
});

const question = {kind: 'plan-question' as const, question: '按什么方式整理？', options: [{ordinal: 5, label: '按主题', description: '方便查找', recommended: true}], allowFreeText: true};
it.each(['option', 'text'] as const)('answers a plan question using %s without adding a placeholder contract', async method => {
  const adapter = await open(question);
  await screen.findByText(question.question);
  const card = document.querySelector('.interaction-card')!;
  expect(within(card as HTMLElement).queryByText('Plan Mode')).toBeNull();
  const input = screen.getByRole('textbox', {name: '也可以写下你的想法'});
  expect(input.getAttribute('placeholder')).toBe('写下你的想法…');
  expect(screen.getByRole('button', {name: '提交回答'}).hasAttribute('disabled')).toBe(true);
  if (method === 'option') fireEvent.click(screen.getByRole('button', {name: /按主题/}));
  else {fireEvent.change(input, {target: {value: ' 保留时间顺序 '}});fireEvent.click(screen.getByRole('button', {name: '提交回答'}));}
  await waitFor(() => expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledTimes(1));
  expect(adapter.lastConnection!.resolveInteraction.mock.calls[0][1]).toEqual(method === 'option' ? {kind: 'plan-question-option', optionOrdinal: 5} : {kind: 'plan-question-text', text: '保留时间顺序'});
});

it.each(['approve', 'revise', 'cancel'] as const)('preserves the Markdown plan and %s decision through the conversation', async decision => {
  const adapter = await open({kind: 'plan-draft', body: '## 文档整理方案\n\n保留 **原始内容**，更新 `docs/`。\n\n1. 阅读文档\n2. 检查引用'});
  await screen.findByRole('heading', {name: '文档整理方案'});
  expect(document.querySelector('.plan-draft-body strong')?.textContent).toBe('原始内容');
  expect(document.querySelector('.plan-draft-body code')?.textContent).toBe('docs/');
  expect(within(document.querySelector('.interaction-card')! as HTMLElement).queryByText('Plan Mode')).toBeNull();
  if (decision === 'revise') {
    fireEvent.click(screen.getByRole('button', {name: '提出修改'}));
    expect(screen.getByRole('button', {name: '提交修改意见'}).hasAttribute('disabled')).toBe(true);
    fireEvent.click(screen.getByRole('button', {name: '返回方案'}));
    fireEvent.click(screen.getByRole('button', {name: '提出修改'}));
    fireEvent.change(screen.getByPlaceholderText('说说你想调整的地方…'), {target: {value: '保留会议记录原文'}});
  }
  fireEvent.click(screen.getByRole('button', {name: decision === 'approve' ? '批准并继续' : decision === 'cancel' ? '取消规划' : '提交修改意见'}));
  await waitFor(() => expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledTimes(1));
  expect(adapter.lastConnection!.resolveInteraction.mock.calls[0]).toEqual([expect.objectContaining({workflowId: 'workflow:card', workflowRevision: 7}), {kind: 'plan-draft', decision, ...(decision === 'revise' ? {revisionPrompt: {parts: [{type: 'text', text: '保留会议记录原文'}]}} : {})}]);
});


it('restores an inline plugin connection only after reviewing the default and confirming', async () => {
  const adapter = await open(capability('CONFIGURE_PLUGIN_MCP_CONNECTION', {server_id: 'docs'}, {
    plugin: {name: '资料工具包', enabled: true, package_install_id: 'package:research'},
    connection: {server_id: 'docs', defaults: config, config: {...config, transport: {...config.transport, endpoint: 'https://override.example/mcp'}}, overlay: {endpoint: 'https://override.example/mcp'}, credential_owner: {...owner, kind: 'plugin', plugin_id: 'research'}},
  }));
  await screen.findByRole('region', {name: '配置 文档服务 连接'});
  fireEvent.click(screen.getByText('插件默认连接（只读）'));
  fireEvent.click(screen.getByRole('button', {name: '恢复插件默认连接'}));
  expect(adapter.lastConnection!.resolveInteraction).not.toHaveBeenCalled();
  expect(screen.getByText(/恢复默认连接后，不再使用的本机凭据和授权也会清除/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name: '确认恢复默认连接'}));
  await waitFor(() => expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledTimes(1));
  expect(adapter.lastConnection!.resolveInteraction.mock.calls[0][1]).toEqual({kind: 'capability', decision: 'SUBMIT', submission: {overlay: null, secret_changes: [], retain_credentials_confirmed: false}});
});

it('keeps the Hook command and trust gate in the conversation card', async () => {
  const adapter = await open(capability('TRUST_HOOK_SOURCE', {hook_source: {path: '/project/.pulsara/hooks.json', definitions: [{event: 'PreToolUse', command: 'python scripts/check_documents.py', matcher: 'write_file', timeout: 10, async: false, additionalContextLimit: 2000}]}}));
  await screen.findByText('python scripts/check_documents.py');
  expect(screen.getByText('/project/.pulsara/hooks.json')).toBeTruthy();
  const submit = screen.getByRole('button', {name: '信任并继续'});
  expect(submit.hasAttribute('disabled')).toBe(true);
  fireEvent.click(screen.getByRole('checkbox', {name: '我已检查这些命令，允许它们运行。'}));
  fireEvent.click(submit);
  await waitFor(() => expect(adapter.lastConnection!.resolveInteraction).toHaveBeenCalledWith(expect.anything(), {kind: 'capability', decision: 'SUBMIT', submission: {hook_review_accepted: true}}));
});

it.each(['loaded', 'failed'] as const)('settles the production card read as %s without exposing decision buttons early', async outcome => {
  const pending = deferred<RuntimeInteractionContent>();
  vi.spyOn(FakeConnection.prototype, 'readInteraction').mockReturnValueOnce(pending.promise);
  const adapter = await open({kind: 'plan-draft', body: '## 已加载方案'});
  const card = within(document.querySelector('.interaction-card')! as HTMLElement);
  expect(card.getByText('正在准备内容…')).toBeTruthy();
  expect(card.queryByRole('button', {name: '批准并继续'})).toBeNull();
  await act(async () => {if (outcome === 'loaded') pending.resolve({kind: 'plan-draft', body: '## 已加载方案'});else pending.reject(new Error('read failed'));});
  if (outcome === 'loaded') expect(await card.findByRole('heading', {name: '已加载方案'})).toBeTruthy();
  else {expect(await card.findByRole('alert')).toHaveProperty('textContent', '内容暂时无法读取，请等待更新后重试。');expect(card.queryByRole('button', {name: '批准并继续'})).toBeNull();}
  expect(adapter.lastConnection!.resolveInteraction).not.toHaveBeenCalled();
});

it('retains the composer draft when the server rejects a new message during plan review', async () => {
  const {typeComposer, textPrompt} = await import('./pulsara-app-fixture');
  const adapter = await open({kind: 'plan-draft', body: '## Review this plan'});
  await screen.findByRole('heading', {name: 'Review this plan'});
  adapter.lastConnection!.submitPrompt.mockImplementation(async commandId => ({commandId, status: 'rejected', publicCode: 'PLAN_REVIEW_PENDING', publicMessage: '请先批准、修改或取消当前方案，再发送新消息。'}));
  const editor = await typeComposer('keep my blocked input');
  fireEvent.keyDown(editor, {key: 'Enter', code: 'Enter'});
  await screen.findByText('输入被拒绝');
  expect(editor.textContent).toBe('keep my blocked input');
  expect(screen.queryByText('发送状态待确认')).toBeNull();
  expect(adapter.lastConnection!.queryCommand).not.toHaveBeenCalled();
  expect(adapter.lastConnection!.submitPrompt).toHaveBeenCalledWith(expect.stringMatching(/^command:/), textPrompt('keep my blocked input'), expect.anything());
  expect(screen.getByRole('button', {name: '批准并继续'})).toBeTruthy();
});

it.each(['tool-confirmation', 'plan-question', 'capability-form'] as const)('keeps ordinary new inputs queued during %s', async kind => {
  const {typeComposer} = await import('./pulsara-app-fixture');
  const content: RuntimeInteractionContent = kind === 'tool-confirmation'
    ? {kind, prompt: 'Allow write_file?', options: ['ALLOW', 'DENY']}
    : kind === 'plan-question' ? question : capability('AUTHORIZE_MCP');
  const adapter = await open(content, kind === 'tool-confirmation' ? {id: 'permission:queue', kind, prompt: 'Allow write_file?', options: ['ALLOW', 'DENY'], expiresAtUtc: '2099-01-01T00:00:00Z', decisionInProgress: false} : undefined);
  await screen.findByText(kind === 'plan-question' ? question.question : kind === 'tool-confirmation' ? 'Pulsara 想修改工作目录中的文件。' : '前往登录');
  adapter.lastConnection!.submitPrompt.mockImplementation(async commandId => ({commandId, status: 'pending', publicCode: 'PROMPT_QUEUED', promptDelivery: {queueItemId: 'queue:blocked', queueStatus: 'PENDING', deliveryMode: 'new-turn'}}));
  const editor = await typeComposer('queue after current operation');
  fireEvent.keyDown(editor, {key: 'Enter', code: 'Enter'});
  await waitFor(() => expect(adapter.lastConnection!.submitPrompt).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByLabelText('发送给 Pulsara').textContent).toBe(''));
  expect(screen.queryByText('发送状态待确认')).toBeNull();
  expect(adapter.lastConnection!.resolveInteraction).not.toHaveBeenCalled();
});
