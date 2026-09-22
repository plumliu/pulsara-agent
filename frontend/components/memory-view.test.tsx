import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryView } from './memory-view';
import { LocalMemoryApi, MemoryApiError, type MemoryDetail, type MemoryFact, type MemoryRecord } from '../lib/memory-api';

afterEach(() => { cleanup(); vi.useRealTimers(); });
const fact: MemoryFact = { fact_id: 'memory:a', context_id: 'ctx:global', statement: '用户喜欢散步，雨天除外。', kind: 'USER_PROFILE', lifecycle: 'ACTIVE', recorded_at: '2026-09-05T00:00:00Z', updated_at: '2026-09-05T00:00:00Z', context_label: '全局记忆' };
const confirmation: MemoryRecord[] = [{ type: 'HEADER', root: fact.fact_id, view: 'global', workspace_id: null, disposition: 'READY' }, { type: 'FACT_DELETE', fact }, { type: 'END', counts: { HEADER: 1, FACT_DELETE: 1 } }];
function setup() {
  const api = new LocalMemoryApi();
  vi.spyOn(api, 'projects').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(api, 'catalog').mockResolvedValue({ items: [fact], next_cursor: null });
  vi.spyOn(api, 'detail').mockResolvedValue({ fact, formation: '由对话中的记忆工具直接保存', source: { availability: 'DELETED', locator: null }, relations: [], next_cursor: null });
  vi.spyOn(api, 'editStatement').mockResolvedValue({ fact, changed: false, user_edited_at: null });
  vi.spyOn(api, 'preview').mockResolvedValue(confirmation);
  vi.spyOn(api, 'delete').mockResolvedValue([{ type: 'HEADER', root: fact.fact_id, view: 'global', workspace_id: null, result: 'DELETED' }, { type: 'END', counts: { HEADER: 1 } }]);
  return api;
}

describe('MemoryView', () => {
  it('edits only the selected memory text and keeps source and relations intact', async () => {
    const api = setup();
    const source = { session_id: 'session:initial', turn_id: 'turn:initial', entry_id: 'entry:initial' };
    const companion = { ...fact, fact_id: 'memory:related', statement: '关联的旧安排' };
    vi.mocked(api.detail).mockResolvedValue({
      fact, formation: '', source: { availability: 'OPEN', locator: source }, user_edited_at: null,
      relations: [{ relation_id: 'relation:related', relative_role: 'UPDATES', subject: fact, companion, recorded_at: fact.recorded_at, owner: { write_tool: 'mark_memory_relation', source: { availability: 'DELETED', locator: null } } }],
      next_cursor: null,
    });
    const changed = { ...fact, statement: '用户更正：晴天沿河散步。', updated_at: '2026-09-05T01:00:00+00:00' };
    vi.mocked(api.editStatement).mockResolvedValue({ fact: changed, changed: true, user_edited_at: changed.updated_at });
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const panel = await screen.findByRole('complementary', { name: '记忆详情' });
    const actions = panel.querySelector<HTMLDivElement>('.memory-detail-actions')!;
    expect(Array.from(actions.querySelectorAll('button')).map(button => button.textContent)).toEqual(['在对话中查看', '编辑记忆', '删除记忆…']);
    fireEvent.click(within(panel).getByRole('button', { name: '编辑记忆' }));
    expect((within(panel).getByRole('textbox', { name: '记忆正文' }) as HTMLTextAreaElement).value).toBe(fact.statement);
    expect(within(panel).getByText(/已有依据、取代和冲突关系不会自动重判/)).toBeTruthy();
    expect((within(panel).getByRole('button', { name: '删除记忆…' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(panel).getByRole('textbox', { name: '记忆正文' }), { target: { value: `  ${changed.statement}  ` } });
    fireEvent.click(within(panel).getByRole('button', { name: '保存正文' }));
    await waitFor(() => expect(api.editStatement).toHaveBeenCalledWith(
      { view: 'global', workspace_id: null }, fact, changed.statement,
    ));
    await waitFor(() => expect(within(panel).getByText(changed.statement, { selector: 'p.memory-statement' })).toBeTruthy());
    expect(within(panel).getByText(/来源对话只记录最初保存的位置/)).toBeTruthy();
    expect(within(panel).getByRole('button', { name: '查看记忆：关联的旧安排' })).toBeTruthy();
    expect(screen.getByRole('button', { name: /用户更正：晴天沿河散步/ })).toBeTruthy();
    expect(within(panel).getByRole('button', { name: '在对话中查看' })).toBeTruthy();
  });
  it('keeps the edit draft after an optimistic concurrency conflict and cancels without saving', async () => {
    const api = setup();
    vi.mocked(api.editStatement).mockRejectedValue(new MemoryApiError('记忆已发生变化，请刷新详情后再编辑', 409));
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const panel = await screen.findByRole('complementary', { name: '记忆详情' });
    fireEvent.click(within(panel).getByRole('button', { name: '编辑记忆' }));
    fireEvent.change(within(panel).getByRole('textbox', { name: '记忆正文' }), { target: { value: '用户提出的新正文' } });
    fireEvent.click(within(panel).getByRole('button', { name: '保存正文' }));
    expect((await within(panel).findByRole('alert')).textContent).toContain('记忆已发生变化，请刷新详情后再编辑');
    expect((within(panel).getByRole('textbox', { name: '记忆正文' }) as HTMLTextAreaElement).value).toBe('用户提出的新正文');
    vi.mocked(api.editStatement).mockRejectedValueOnce(new Error('network interrupted'));
    fireEvent.click(within(panel).getByRole('button', { name: '保存正文' }));
    await waitFor(() => expect(within(panel).getByRole('alert').textContent).toContain('重新打开详情核对正文'));
    fireEvent.click(within(panel).getByRole('button', { name: '取消' }));
    expect(within(panel).getByText(fact.statement, { selector: 'p.memory-statement' })).toBeTruthy();
  });
  it.each(['starting', 'reconnecting', 'offline', 'failed'] as const)('shows a connection state instead of reading memories while %s', status => {
    const api = setup();
    const reconnect = vi.fn();
    render(<MemoryView api={api} databaseState={status === 'starting' ? undefined : 'ready'} runtimeStatus={status} onReconnect={reconnect} onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    expect(screen.getByRole('status').textContent).toContain('本地服务');
    if (status === 'offline' || status === 'failed') {
      fireEvent.click(screen.getByRole('button', { name: '重新连接' }));
      expect(reconnect).toHaveBeenCalledOnce();
    } else {
      expect(screen.queryByRole('button', { name: '重新连接' })).toBeNull();
    }
    expect(api.catalog).not.toHaveBeenCalled();
    expect(api.projects).not.toHaveBeenCalled();
  });
  it.each(['before', 'after'] as const)('retains project errors arriving %s the catalog debounce', async timing => {
    vi.useFakeTimers();
    const api = setup();
    let rejectProjects!: (error: Error) => void;
    vi.mocked(api.projects).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectProjects = reject; }));
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    if (timing === 'after') await act(async () => { await vi.advanceTimersByTimeAsync(180); });
    await act(async () => { rejectProjects(new Error('项目列表读取失败')); });
    expect(screen.queryByRole('alert')?.textContent).toBe('项目列表读取失败');
    await act(async () => { await vi.advanceTimersByTimeAsync(180); });
    expect(api.catalog).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('alert')?.textContent).toBe('项目列表读取失败');
    expect(screen.getByRole('button', { name: /用户喜欢散步/ })).toBeTruthy();
  });
  it('clears the previous query immediately and ignores its pending detail response', async () => {
    vi.useFakeTimers();
    const api = setup();
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(180); });
    let resolveDetail!: (value: MemoryDetail) => void;
    vi.mocked(api.detail).mockImplementationOnce(() => new Promise(resolve => { resolveDetail = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: /用户喜欢散步/ }));
    expect(screen.getByRole('complementary', { name: '记忆详情' })).toBeTruthy();
    const search = screen.getByRole('textbox', { name: '搜索记忆' });
    search.focus();
    fireEvent.change(search, { target: { value: '新查询' } });
    expect(screen.queryByRole('button', { name: /用户喜欢散步/ })).toBeNull();
    expect(screen.queryByRole('complementary', { name: '记忆详情' })).toBeNull();
    expect(screen.getByText('正在读取记忆…')).toBeTruthy();
    expect(document.activeElement).toBe(search);
    await act(async () => { resolveDetail({ fact, formation: '', source: { availability: 'DELETED', locator: null }, relations: [], next_cursor: null }); });
    expect(screen.queryByRole('complementary', { name: '记忆详情' })).toBeNull();
    vi.mocked(api.catalog).mockResolvedValueOnce({ items: [], next_cursor: null });
    await act(async () => { await vi.advanceTimersByTimeAsync(180); });
    expect(api.catalog).toHaveBeenLastCalledWith({ view: 'global', workspace_id: null }, { kind: '', lifecycle: 'active', search: '新查询' });
    expect(screen.getByRole('heading', { name: '没有找到匹配的记忆' })).toBeTruthy();
  });
  it('does not let an old catalog response overwrite the current query', async () => {
    vi.useFakeTimers();
    const api = setup();
    let resolveOld!: (value: { items: MemoryFact[]; next_cursor: null }) => void;
    vi.mocked(api.catalog).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(180); });
    fireEvent.change(screen.getByRole('textbox', { name: '搜索记忆' }), { target: { value: '新查询' } });
    vi.mocked(api.catalog).mockRejectedValueOnce(new Error('当前查询失败'));
    await act(async () => { await vi.advanceTimersByTimeAsync(180); });
    expect(screen.getByRole('alert').textContent).toBe('当前查询失败');
    await act(async () => { resolveOld({ items: [fact], next_cursor: null }); });
    expect(screen.queryByRole('button', { name: /用户喜欢散步/ })).toBeNull();
    expect(screen.getByRole('alert').textContent).toBe('当前查询失败');
  });
  it('keeps pagination bound to the committed query under StrictMode', async () => {
    vi.useFakeTimers();
    const api = setup();
    const companion = { ...fact, fact_id: 'memory:b', statement: '另一条记忆' };
    vi.mocked(api.catalog)
      .mockResolvedValueOnce({ items: [fact], next_cursor: 'next-page' })
      .mockResolvedValueOnce({ items: [companion], next_cursor: null });
    render(<StrictMode><MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} /></StrictMode>);
    await act(async () => { await vi.advanceTimersByTimeAsync(180); });
    expect(api.catalog).toHaveBeenCalledTimes(1);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '加载更多记忆' })); });
    expect(api.catalog).toHaveBeenCalledTimes(2);
    expect(api.catalog).toHaveBeenLastCalledWith({ view: 'global', workspace_id: null }, { kind: '', lifecycle: 'active', search: '', cursor: 'next-page' });
    expect(screen.getByRole('button', { name: /用户喜欢散步/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /另一条记忆/ })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '加载更多记忆' })).toBeNull();
  });
  it('keeps the same panel mounted while switching, and ignores repeated selection without scrolling', async () => {
    const api = setup();
    const companion = { ...fact, fact_id: 'memory:b', statement: '散步时喜欢经过河边。' };
    const initial: MemoryDetail = { fact, formation: '由对话中的记忆工具直接保存', source: { availability: 'DELETED', locator: null }, relations: [{ relation_id: 'relation:a', relative_role: 'BASED_ON', subject: fact, companion, recorded_at: fact.recorded_at, owner: { write_tool: 'remember', source: { availability: 'DELETED', locator: null } } }], next_cursor: null };
    vi.mocked(api.detail).mockResolvedValue(initial);
    vi.mocked(api.catalog).mockResolvedValue({ items: [fact, companion], next_cursor: null });
    const scroll = vi.fn();
    const previousScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView');
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll });
    try {
      render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
      const row = await screen.findByRole('button', { name: /用户喜欢散步/ });
      fireEvent.click(row);
      const panel = await screen.findByRole('complementary', { name: '记忆详情' });
      await within(panel).findByText(fact.statement, { selector: 'p.memory-statement' });
      fireEvent.click(row);
      expect(api.detail).toHaveBeenCalledTimes(1);
      expect(panel.getAttribute('aria-busy')).toBe('false');
      expect(scroll).not.toHaveBeenCalled();

      let resolve!: (detail: MemoryDetail) => void;
      vi.mocked(api.detail).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
      fireEvent.click(within(panel).getByRole('button', { name: /散步时喜欢经过河边/ }));
      expect(scroll).toHaveBeenCalledWith({ block: 'nearest' });
      expect(screen.getByRole('complementary', { name: '记忆详情' })).toBe(panel);
      expect(within(panel).getByText(fact.statement, { selector: 'p.memory-statement' })).toBeTruthy();
      expect(panel.getAttribute('aria-busy')).toBe('true');
      expect((within(panel).getByRole('button', { name: /删除记忆/ }) as HTMLButtonElement).disabled).toBe(true);
      const pendingRow = screen.getAllByRole('button', { name: /散步时喜欢经过河边/ }).find(button => button.classList.contains('memory-row'))!;
      fireEvent.click(pendingRow);
      expect(api.detail).toHaveBeenCalledTimes(2);
      await act(async () => resolve({ ...initial, fact: companion, relations: [] }));
      expect(screen.getByRole('complementary', { name: '记忆详情' })).toBe(panel);
      expect(within(panel).queryByText(fact.statement, { selector: 'p.memory-statement' })).toBeNull();
      fireEvent.click(pendingRow);
      expect(api.detail).toHaveBeenCalledTimes(2);
      expect(api.catalog).toHaveBeenCalledTimes(1);
      expect(scroll).toHaveBeenCalledTimes(1);
    } finally {
      if (previousScroll) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', previousScroll);
      else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
    }
  });
  it('locates a related superseded fact in the left list even when it is beyond the current page', async () => {
    const api = setup();
    const old = { ...fact, fact_id: 'memory:old', kind: 'FACT' as const, lifecycle: 'SUPERSEDED' as const, statement: '已被替代的旧记忆' };
    const other = { ...old, fact_id: 'memory:other', statement: '第一页的另一条旧记忆' };
    const initial: MemoryDetail = { fact, formation: '', source: { availability: 'DELETED', locator: null }, relations: [{ relation_id: 'relation:old', relative_role: 'UPDATES', subject: fact, companion: old, recorded_at: fact.recorded_at, owner: { write_tool: 'mark_memory_relation', source: { availability: 'DELETED', locator: null } } }], next_cursor: null };
    vi.mocked(api.catalog).mockImplementation(async (_selection, filters) => filters.lifecycle === 'updated'
      ? { items: [other], next_cursor: 'next-page' }
      : { items: [fact], next_cursor: null });
    vi.mocked(api.detail).mockImplementation(async (_selection, id) => id === fact.fact_id ? initial : { ...initial, fact: old, relations: [] });
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '关于你' }));
    fireEvent.change(screen.getByRole('textbox', { name: '搜索记忆' }), { target: { value: '散步' } });
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const panel = await screen.findByRole('complementary', { name: '记忆详情' });
    expect(within(panel).getByText(/事实 · 全局记忆 · 已被替代/)).toBeTruthy();
    fireEvent.click(within(panel).getByRole('button', { name: '查看记忆：已被替代的旧记忆' }));
    await waitFor(() => expect(within(panel).getByText(old.statement, { selector: 'p.memory-statement' })).toBeTruthy());
    expect(screen.getByRole('option', { name: '已被替代', selected: true })).toBeTruthy();
    expect(screen.getByRole('button', { name: '全部' }).getAttribute('aria-pressed')).toBe('true');
    expect((screen.getByRole('textbox', { name: '搜索记忆' }) as HTMLInputElement).value).toBe('');
    await waitFor(() => expect(api.catalog).toHaveBeenLastCalledWith({ view: 'global', workspace_id: null }, { kind: '', lifecycle: 'updated', search: '' }));
    const row = screen.getAllByRole('button', { name: /已被替代的旧记忆/ }).find(button => button.classList.contains('memory-row'))!;
    expect(row.classList.contains('is-selected')).toBe(true);
    expect(screen.getByRole('button', { name: '加载更多记忆' })).toBeTruthy();
    fireEvent.click(within(panel).getByRole('button', { name: '关闭记忆详情' }));
    expect(screen.queryByRole('button', { name: /已被替代的旧记忆/ })).toBeNull();
  });
  it('switches to the related project fact and selects it in that project list', async () => {
    const api = setup();
    const project = { workspace_id: 'ctx:project-a', label: '项目 A', root: '/work/a', last_activity_at: fact.recorded_at };
    const projectFact = { ...fact, fact_id: 'memory:project-a', context_id: project.workspace_id, context_label: project.label, kind: 'DECISION' as const, statement: '项目中的关联记忆' };
    const initial: MemoryDetail = { fact, formation: '', source: { availability: 'DELETED', locator: null }, relations: [{ relation_id: 'relation:project', relative_role: 'BASED_ON', subject: fact, companion: projectFact, recorded_at: fact.recorded_at, owner: { write_tool: 'remember', source: { availability: 'DELETED', locator: null } } }], next_cursor: null };
    vi.mocked(api.projects).mockResolvedValue({ items: [project], next_cursor: null });
    vi.mocked(api.catalog).mockImplementation(async (selection) => ({ items: selection.view === 'global' ? [fact] : [projectFact], next_cursor: null }));
    vi.mocked(api.detail).mockImplementation(async (_selection, id) => id === fact.fact_id ? initial : { ...initial, fact: projectFact, relations: [] });
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const panel = await screen.findByRole('complementary', { name: '记忆详情' });
    fireEvent.click(within(panel).getByRole('button', { name: '查看记忆：项目中的关联记忆' }));
    await waitFor(() => expect(within(panel).getByText(projectFact.statement, { selector: 'p.memory-statement' })).toBeTruthy());
    expect(screen.getByRole('tab', { name: '项目记忆' }).getAttribute('aria-selected')).toBe('true');
    expect(within(screen.getByRole('combobox', { name: '选择项目' })).getByRole('option', { name: /项目 A/, selected: true })).toBeTruthy();
    expect(screen.getByRole('button', { name: '全部' }).getAttribute('aria-pressed')).toBe('true');
    await waitFor(() => expect(api.catalog).toHaveBeenLastCalledWith({ view: 'project', workspace_id: project.workspace_id }, { kind: '', lifecycle: 'active', search: '' }));
    const row = screen.getAllByRole('button', { name: /项目中的关联记忆/ }).find(button => button.classList.contains('memory-row'))!;
    expect(row.classList.contains('is-selected')).toBe(true);
  });
  it('shows the relation owner separately from the fact creator', async () => {
    const api = setup();
    const onOpenSource = vi.fn();
    const creationSource = { session_id: 'session:fact', turn_id: 'turn:fact', entry_id: 'entry:fact' };
    const relationOwner = { session_id: 'session:relation', turn_id: 'turn:relation', entry_id: 'entry:relation' };
    vi.mocked(api.detail).mockResolvedValue({
      fact, formation: '由对话中的记忆工具直接保存', source: { availability: 'OPEN', locator: creationSource },
      relations: [{ relation_id: 'relation:later', relative_role: 'CONFLICTS_WITH', subject: fact, companion: { ...fact, fact_id: 'memory:other', statement: '另一条记忆' }, recorded_at: fact.recorded_at, owner: { write_tool: 'mark_memory_relation', source: { availability: 'OPEN', locator: relationOwner } } }],
      next_cursor: null,
    });
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={onOpenSource} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const panel = await screen.findByRole('complementary', { name: '记忆详情' });
    const actions = panel.querySelector<HTMLDivElement>('.memory-detail-actions')!;
    expect(within(actions).getByRole('button', { name: '删除记忆…' })).toBeTruthy();
    fireEvent.click(within(actions).getByRole('button', { name: '在对话中查看' }));
    expect(onOpenSource).toHaveBeenCalledWith(creationSource);
    expect(within(panel).queryByText('保存时引用')).toBeNull();
    expect(within(panel).queryByText('适用条件与例外')).toBeNull();
    expect(within(panel).queryByText('保存方式')).toBeNull();
    expect(within(panel).getByRole('heading', { name: '关系' })).toBeTruthy();
    const graph = within(panel).getByRole('region', { name: '记忆关系图' });
    expect(within(graph).getByText('当前记忆')).toBeTruthy();
    expect(within(graph).getByText('contradicts')).toBeTruthy();
    expect(graph.querySelector('.memory-graph-edge svg.lucide-arrow-right')).toBeTruthy();
    expect(within(panel).getByText('后续标定')).toBeTruthy();
    fireEvent.click(within(panel).getByRole('button', { name: '查看建立处' }));
    expect(onOpenSource).toHaveBeenCalledWith(relationOwner);
  });
  it.each([
    ['BASED_ON', 'is based on'],
    ['BASIS_FOR', 'is the basis of'],
    ['UPDATES', 'supersedes'],
    ['UPDATED_BY', 'is superseded by'],
    ['CONFLICTS_WITH', 'contradicts'],
  ] as const)('reads %s from the current card toward the related card', async (relative_role, phrase) => {
    const api = setup();
    vi.mocked(api.detail).mockResolvedValue({
      fact, formation: '', source: { availability: 'DELETED', locator: null },
      relations: [{ relation_id: 'relation:test', relative_role, subject: fact, companion: { ...fact, fact_id: 'memory:other', statement: '另一条记忆' }, recorded_at: fact.recorded_at, owner: { write_tool: 'remember', source: { availability: 'DELETED', locator: null } } }],
      next_cursor: null,
    });
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const graph = await screen.findByRole('region', { name: '记忆关系图' });
    expect(within(graph).getByText(phrase)).toBeTruthy();
    expect(graph.querySelectorAll('.memory-graph-edge svg.lucide-arrow-right')).toHaveLength(1);
    expect(within(graph).getByText('当前记忆')).toBeTruthy();
    expect(within(graph).getByRole('button', { name: '查看记忆：另一条记忆' })).toBeTruthy();
  });
  it('omits the generic usage note while retaining meaningful memory states', async () => {
    const api = setup();
    vi.mocked(api.catalog).mockResolvedValue({ items: [
      fact,
      { ...fact, fact_id: 'memory:old', lifecycle: 'SUPERSEDED' },
      { ...fact, fact_id: 'memory:conflict', needs_confirmation: true },
      { ...fact, fact_id: 'memory:preference', kind: 'RESPONSE_PREFERENCE' },
    ], next_cursor: null });
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    await screen.findAllByRole('button', { name: /用户喜欢散步/ });
    expect(screen.queryByText('相关时使用')).toBeNull();
    expect(screen.getByText('已被替代', { selector: '.memory-usage' })).toBeTruthy();
    expect(screen.getByText('需要确认')).toBeTruthy();
    expect(screen.getByText('通常随新一轮提供')).toBeTruthy();
  });
  it('retains the loaded detail on read failure, and ignores a response after closing', async () => {
    const api = setup();
    const companion = { ...fact, fact_id: 'memory:b', statement: '另一条记忆' };
    vi.mocked(api.catalog).mockResolvedValue({ items: [fact, companion], next_cursor: null });
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const panel = await screen.findByRole('complementary', { name: '记忆详情' });
    await within(panel).findByText(fact.statement, { selector: 'p.memory-statement' });
    vi.mocked(api.detail).mockRejectedValueOnce(new Error('暂时无法读取'));
    fireEvent.click(screen.getByRole('button', { name: /另一条记忆/ }));
    await screen.findByRole('alert');
    expect(screen.getByRole('complementary', { name: '记忆详情' })).toBe(panel);
    expect(within(panel).getByText(fact.statement, { selector: 'p.memory-statement' })).toBeTruthy();
    let resolve!: (detail: MemoryDetail) => void;
    vi.mocked(api.detail).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    fireEvent.click(screen.getByRole('button', { name: /另一条记忆/ }));
    fireEvent.click(screen.getByRole('button', { name: '关闭记忆详情' }));
    await act(async () => resolve({ fact: companion, formation: '', source: { availability: 'DELETED', locator: null }, relations: [], next_cursor: null }));
    expect(screen.queryByRole('complementary', { name: '记忆详情' })).toBeNull();
  });
  it('only accepts the latest selected detail when reads finish out of order', async () => {
    const api = setup();
    const companion = { ...fact, fact_id: 'memory:b', statement: '另一条记忆' };
    vi.mocked(api.catalog).mockResolvedValue({ items: [fact, companion], next_cursor: null });
    let first!: (detail: MemoryDetail) => void;
    let second!: (detail: MemoryDetail) => void;
    vi.mocked(api.detail)
      .mockImplementationOnce(() => new Promise(done => { first = done; }))
      .mockImplementationOnce(() => new Promise(done => { second = done; }));
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    fireEvent.click(screen.getByRole('button', { name: /另一条记忆/ }));
    const panel = screen.getByRole('complementary', { name: '记忆详情' });
    const detail: MemoryDetail = { fact, formation: '', source: { availability: 'DELETED', locator: null }, relations: [], next_cursor: null };
    await act(async () => first(detail));
    expect(panel.getAttribute('aria-busy')).toBe('true');
    expect(within(panel).queryByText(fact.statement, { selector: 'p.memory-statement' })).toBeNull();
    await act(async () => second({ ...detail, fact: companion }));
    expect(panel.getAttribute('aria-busy')).toBe('false');
    expect(within(panel).getByText(companion.statement, { selector: 'p.memory-statement' })).toBeTruthy();
  });
  it('distinguishes an empty library, search results, and project selection', async () => {
    const api = setup();
    vi.mocked(api.catalog).mockResolvedValue({ items: [], next_cursor: null });
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    expect(screen.getByRole('tab', { name: '全局记忆' }).getAttribute('aria-selected')).toBe('true');
    expect(await screen.findByRole('heading', { name: '还没有全局记忆' })).toBeTruthy();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索记忆' }), { target: { value: '散步' } });
    expect(await screen.findByRole('heading', { name: '没有找到匹配的记忆' })).toBeTruthy();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索记忆' }), { target: { value: '' } });
    fireEvent.change(screen.getByRole('combobox', { name: '记忆状态' }), { target: { value: 'updated' } });
    expect(await screen.findByRole('heading', { name: '暂无已被替代的记忆' })).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: '项目记忆' }));
    expect(await screen.findByRole('heading', { name: '选择一个项目' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /删除记忆/ })).toBeNull();
  });
  it('shows only memory-bearing projects and clears the selection after their last memory is deleted', async () => {
    const api = setup();
    const project = { workspace_id: 'ctx:project-a', label: '项目 A', root: '/work/a', last_activity_at: fact.recorded_at };
    const projectFact = { ...fact, fact_id: 'memory:project-a', context_id: project.workspace_id, context_label: project.label };
    const projectConfirmation: MemoryRecord[] = [
      { type: 'HEADER', root: projectFact.fact_id, view: 'project', workspace_id: project.workspace_id, disposition: 'READY' },
      { type: 'FACT_DELETE', fact: projectFact },
      { type: 'END', counts: { HEADER: 1, FACT_DELETE: 1 } },
    ];
    vi.mocked(api.projects)
      .mockResolvedValueOnce({ items: [project], next_cursor: null })
      .mockResolvedValueOnce({ items: [], next_cursor: null });
    vi.mocked(api.catalog).mockResolvedValue({ items: [projectFact], next_cursor: null });
    vi.mocked(api.detail).mockResolvedValue({ fact: projectFact, formation: '', source: { availability: 'DELETED', locator: null }, relations: [], next_cursor: null });
    vi.mocked(api.preview).mockResolvedValue(projectConfirmation);
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(screen.getByRole('tab', { name: '项目记忆' }));
    const selector = screen.getByRole('combobox', { name: '选择项目' });
    await waitFor(() => expect(within(selector).getByRole('option', { name: /项目 A/ })).toBeTruthy());
    expect(within(selector).queryByRole('option', { name: /未保存项目/ })).toBeNull();
    fireEvent.change(selector, { target: { value: project.workspace_id } });
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    fireEvent.click(await screen.findByRole('button', { name: /删除记忆/ }));
    fireEvent.click(await screen.findByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(api.projects).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(within(selector).getByRole('option', { name: '选择有记忆的项目', selected: true })).toBeTruthy());
    expect(within(selector).queryByRole('option', { name: /项目 A/ })).toBeNull();
    expect(within(selector).getByRole('option', { name: '选择有记忆的项目' })).toBeTruthy();
  });
  it.each(['database_not_configured', 'database_configured_unverified', 'database_unavailable', 'database_schema_action_required'] as const)('gates %s without reading memories', async databaseState => {
    const api = setup();
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState={databaseState} onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    expect(screen.getByRole('button', { name: /前往本地服务设置/ })).toBeTruthy();
    expect(api.catalog).not.toHaveBeenCalled(); expect(api.projects).not.toHaveBeenCalled();
  });
  it('keeps details in the page but confirms deletion in a separate dialog', async () => {
    const api = setup();
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const detail = await screen.findByRole('complementary', { name: '记忆详情' });
    expect(within(detail).getByText('暂无关联记忆')).toBeTruthy();
    expect(within(detail).queryByRole('button', { name: '在对话中查看' })).toBeNull();
    expect(within(detail).queryByRole('button', { name: '编辑' })).toBeNull();
    const actions = detail.querySelector<HTMLDivElement>('.memory-detail-actions')!;
    fireEvent.click(within(actions).getByRole('button', { name: /删除记忆/ }));
    const dialog = await screen.findByRole('dialog', { name: '删除这条记忆？' });
    expect(within(detail).queryByRole('dialog')).toBeNull();
    expect(dialog.parentElement?.parentElement).toBe(document.body);
    expect((within(actions).getByRole('button', { name: /删除记忆/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(dialog).getByText('你选择删除')).toBeTruthy();
    expect(within(dialog).queryByText('删除后无法撤销；已保存的对话原文不会改变。')).toBeNull();
    expect(within(dialog).getByText(/关于你 · 全局记忆/)).toBeTruthy();
    expect(within(dialog).queryByText(/跨对话/)).toBeNull();
    expect(within(dialog).getByLabelText('删除影响概览').textContent).toContain('删除记忆 1 条');
    expect(api.delete).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledExactlyOnceWith(fact.fact_id, confirmation));
    expect(screen.queryByRole('dialog', { name: '删除这条记忆？' })).toBeNull();
    expect(await screen.findByText(/记忆已删除。/)).toBeTruthy();
  });
  it('groups cascading deletion, restored facts, and relationship changes without lengthening details', async () => {
    const api = setup();
    const dependent = { ...fact, fact_id: 'memory:dependent', statement: '依赖这条记忆的决定', kind: 'DECISION' as const };
    const old = { ...fact, fact_id: 'memory:old', statement: '将重新生效的旧记忆', lifecycle: 'SUPERSEDED' as const };
    vi.mocked(api.preview).mockResolvedValue([
      { type: 'HEADER', root: fact.fact_id, view: 'global', workspace_id: null, disposition: 'READY' },
      { type: 'FACT_DELETE', fact }, { type: 'FACT_DELETE', fact: dependent },
      { type: 'RELATION_EFFECT', relation_id: 'relation:old', effect: 'REMOVED', subject: fact, companion: old, relative_role: 'UPDATES', recorded_at: fact.recorded_at },
      { type: 'FACT_RESTORE', fact: old, planned_lifecycle: 'ACTIVE' },
      { type: 'END', counts: { HEADER: 1, FACT_DELETE: 2, RELATION_EFFECT: 1, FACT_RESTORE: 1 } },
    ]);
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const detail = await screen.findByRole('complementary', { name: '记忆详情' });
    fireEvent.click(within(detail).getByRole('button', { name: /删除记忆/ }));
    const dialog = await screen.findByRole('dialog', { name: '删除这条记忆？' });
    expect(within(dialog).getByLabelText('删除影响概览').textContent).toMatch(/删除记忆 2 条.*重新生效 1 条.*关系变化 1 项/);
    expect(within(dialog).getByRole('heading', { name: /还会一并删除/ })).toBeTruthy();
    expect(within(dialog).getByText(dependent.statement)).toBeTruthy();
    expect(within(dialog).getByRole('heading', { name: /旧记忆将重新生效/ })).toBeTruthy();
    const restoration = within(dialog).getByRole('heading', { name: /旧记忆将重新生效/ }).closest('section')!;
    expect(within(restoration).getByText(old.statement)).toBeTruthy();
    expect(within(restoration).getByText(/已被替代/)).toBeTruthy();
    const relations = within(dialog).getByText('查看 1 项关系变化').closest('details')!;
    expect(relations.open).toBe(false);
    fireEvent.click(within(dialog).getByText('查看 1 项关系变化'));
    expect(relations.open).toBe(true);
    expect(within(relations).getByText('新记忆')).toBeTruthy();
    expect(within(relations).getByText('旧记忆')).toBeTruthy();
    expect(within(relations).queryByText('与')).toBeNull();
    expect(dialog.querySelector('footer span')).toBeNull();
    expect(within(detail).queryByText(dependent.statement)).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: '取消' }));
    expect(screen.queryByRole('dialog', { name: '删除这条记忆？' })).toBeNull();
    expect(api.delete).not.toHaveBeenCalled();
  });
  it('closes the deletion dialog with Escape without deleting the memory', async () => {
    const api = setup();
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    fireEvent.click(await screen.findByRole('button', { name: /删除记忆/ }));
    const dialog = await screen.findByRole('dialog', { name: '删除这条记忆？' });
    expect(document.activeElement).toBe(dialog);
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '删除这条记忆？' })).toBeNull();
    expect(screen.getByRole('complementary', { name: '记忆详情' })).toBeTruthy();
    expect(api.delete).not.toHaveBeenCalled();
  });
  it('replaces drifted confirmation and prevents unresolved deletion', async () => {
    const api = setup();
    const old = { ...fact, fact_id: 'memory:old', statement: '可能恢复的旧记忆', lifecycle: 'SUPERSEDED' as const };
    const fresh: MemoryRecord[] = [{ type: 'HEADER', root: fact.fact_id, view: 'global', workspace_id: null, disposition: 'NEEDS_RESOLUTION' }, { type: 'FACT_DELETE', fact }, { type: 'FACT_RESTORE', fact: old, planned_lifecycle: 'ACTIVE' }, { type: 'RESTORATION_CONFLICT', subject: old, companion: fact, reason: 'ACTIVE_SEMANTIC_COLLISION', group: 'group:old' }, { type: 'END', counts: { HEADER: 1, FACT_DELETE: 1, FACT_RESTORE: 1, RESTORATION_CONFLICT: 1 } }];
    vi.mocked(api.delete).mockRejectedValue(new MemoryApiError('记忆发生变化', 409, fresh));
    render(<MemoryView runtimeStatus="online" onReconnect={vi.fn()} api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    fireEvent.click(await screen.findByRole('button', { name: /删除记忆/ }));
    fireEvent.click(await screen.findByRole('button', { name: '确认删除' }));
    const dialog = await screen.findByRole('dialog', { name: '删除这条记忆？' });
    expect(within(dialog).getByRole('checkbox')).toBeTruthy();
    expect(dialog.querySelector<HTMLButtonElement>('.memory-delete-confirm')?.disabled).toBe(true);
    expect(within(dialog).getByRole('alert').textContent).toContain('记忆发生变化');
    expect(within(dialog).getByText('目前不能直接删除')).toBeTruthy();
    let rejectPreview!: (error: Error) => void;
    vi.mocked(api.preview).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectPreview = reject; }));
    fireEvent.click(within(dialog).getByRole('checkbox'));
    fireEvent.click(within(dialog).getByRole('button', { name: '重新计算影响' }));
    await waitFor(() => expect(api.preview).toHaveBeenLastCalledWith({ view: 'global', workspace_id: null }, fact.fact_id, [old.fact_id]));
    expect(screen.getByRole('dialog', { name: '删除这条记忆？' })).toBe(dialog);
    expect(dialog.querySelector<HTMLButtonElement>('.memory-delete-confirm')?.disabled).toBe(true);
    await act(async () => rejectPreview(new Error('重算失败')));
    expect(within(dialog).getByRole('alert').textContent).toContain('重算失败');
    expect((within(dialog).getByRole('button', { name: '确认删除' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole('button', { name: '重新计算影响' }));
    await waitFor(() => expect((within(dialog).getByRole('button', { name: '确认删除' }) as HTMLButtonElement).disabled).toBe(false));
  });
  it('clears loaded memories on database readiness loss', async () => {
    const api = setup(); const props = { api, runtimeStatus: 'online' as const, onReconnect: vi.fn(), onOpenSettings: vi.fn(), onOpenSource: vi.fn() };
    const view = render(<MemoryView {...props} databaseState="ready" />);
    await screen.findByRole('button', { name: /用户喜欢散步/ });
    view.rerender(<MemoryView {...props} databaseState="database_unavailable" />);
    expect(screen.queryByText(fact.statement)).toBeNull();
  });
});
