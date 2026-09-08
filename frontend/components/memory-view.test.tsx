import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryView } from './memory-view';
import { LocalMemoryApi, MemoryApiError, type MemoryDetail, type MemoryFact, type MemoryRecord } from '../lib/memory-api';

afterEach(() => { cleanup(); vi.useRealTimers(); });
const fact: MemoryFact = { fact_id: 'memory:a', context_id: 'ctx:global', statement: '用户喜欢散步，雨天除外。', kind: 'USER_PROFILE', lifecycle: 'ACTIVE', recorded_at: '2026-09-05T00:00:00Z', updated_at: '2026-09-05T00:00:00Z', context_label: '跨对话' };
const confirmation: MemoryRecord[] = [{ type: 'HEADER', root: fact.fact_id, view: 'global', workspace_id: null, disposition: 'READY' }, { type: 'FACT_DELETE', fact }, { type: 'END', counts: { HEADER: 1, FACT_DELETE: 1 } }];
function setup() {
  const api = new LocalMemoryApi();
  vi.spyOn(api, 'projects').mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(api, 'catalog').mockResolvedValue({ items: [fact], next_cursor: null });
  vi.spyOn(api, 'detail').mockResolvedValue({ fact, formation: '在对话中记住', public_summary: '依据用户表达整理', source: null, relations: [], next_cursor: null });
  vi.spyOn(api, 'preview').mockResolvedValue(confirmation);
  vi.spyOn(api, 'delete').mockResolvedValue([{ type: 'HEADER', root: fact.fact_id, view: 'global', workspace_id: null, result: 'DELETED' }, { type: 'END', counts: { HEADER: 1 } }]);
  return api;
}

describe('MemoryView', () => {
  it.each(['before', 'after'] as const)('retains project errors arriving %s the catalog debounce', async timing => {
    vi.useFakeTimers();
    const api = setup();
    let rejectProjects!: (error: Error) => void;
    vi.mocked(api.projects).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectProjects = reject; }));
    render(<MemoryView api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
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
    render(<MemoryView api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
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
    await act(async () => { resolveDetail({ fact, formation: '', source: null, public_summary: '', relations: [], next_cursor: null }); });
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
    render(<MemoryView api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
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
    render(<StrictMode><MemoryView api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} /></StrictMode>);
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
    const initial: MemoryDetail = { fact, formation: '在对话中记住', public_summary: '', source: null, relations: [{ relation_id: 'relation:a', relative_role: 'BASED_ON', subject: fact, companion, recorded_at: fact.recorded_at, public_summary: '' }], next_cursor: null };
    vi.mocked(api.detail).mockResolvedValue(initial);
    vi.mocked(api.catalog).mockResolvedValue({ items: [fact, companion], next_cursor: null });
    const scroll = vi.fn();
    const previousScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView');
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll });
    try {
      render(<MemoryView api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
      const row = await screen.findByRole('button', { name: /用户喜欢散步/ });
      fireEvent.click(row);
      const panel = await screen.findByRole('complementary', { name: '记忆详情' });
      await within(panel).findByText(fact.statement);
      fireEvent.click(row);
      expect(api.detail).toHaveBeenCalledTimes(1);
      expect(panel.getAttribute('aria-busy')).toBe('false');

      let resolve!: (detail: MemoryDetail) => void;
      vi.mocked(api.detail).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
      fireEvent.click(within(panel).getByRole('button', { name: /散步时喜欢经过河边/ }));
      expect(screen.getByRole('complementary', { name: '记忆详情' })).toBe(panel);
      expect(within(panel).getByText(fact.statement)).toBeTruthy();
      expect(panel.getAttribute('aria-busy')).toBe('true');
      expect((within(panel).getByRole('button', { name: /删除记忆/ }) as HTMLButtonElement).disabled).toBe(true);
      const pendingRow = screen.getAllByRole('button', { name: /散步时喜欢经过河边/ }).find(button => button.classList.contains('memory-row'))!;
      fireEvent.click(pendingRow);
      expect(api.detail).toHaveBeenCalledTimes(2);
      await act(async () => resolve({ ...initial, fact: companion, relations: [] }));
      expect(screen.getByRole('complementary', { name: '记忆详情' })).toBe(panel);
      expect(within(panel).queryByText(fact.statement)).toBeNull();
      fireEvent.click(pendingRow);
      expect(api.detail).toHaveBeenCalledTimes(2);
      expect(api.catalog).toHaveBeenCalledTimes(1);
      expect(scroll).not.toHaveBeenCalled();
    } finally {
      if (previousScroll) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', previousScroll);
      else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
    }
  });
  it('retains the loaded detail on read failure, and ignores a response after closing', async () => {
    const api = setup();
    const companion = { ...fact, fact_id: 'memory:b', statement: '另一条记忆' };
    vi.mocked(api.catalog).mockResolvedValue({ items: [fact, companion], next_cursor: null });
    render(<MemoryView api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const panel = await screen.findByRole('complementary', { name: '记忆详情' });
    await within(panel).findByText(fact.statement);
    vi.mocked(api.detail).mockRejectedValueOnce(new Error('暂时无法读取'));
    fireEvent.click(screen.getByRole('button', { name: /另一条记忆/ }));
    await screen.findByRole('alert');
    expect(screen.getByRole('complementary', { name: '记忆详情' })).toBe(panel);
    expect(within(panel).getByText(fact.statement)).toBeTruthy();
    let resolve!: (detail: MemoryDetail) => void;
    vi.mocked(api.detail).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    fireEvent.click(screen.getByRole('button', { name: /另一条记忆/ }));
    fireEvent.click(screen.getByRole('button', { name: '关闭记忆详情' }));
    await act(async () => resolve({ fact: companion, formation: '', source: null, public_summary: '', relations: [], next_cursor: null }));
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
    render(<MemoryView api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    fireEvent.click(screen.getByRole('button', { name: /另一条记忆/ }));
    const panel = screen.getByRole('complementary', { name: '记忆详情' });
    const detail = { fact, formation: '', source: null, public_summary: '', relations: [], next_cursor: null };
    await act(async () => first(detail));
    expect(panel.getAttribute('aria-busy')).toBe('true');
    expect(within(panel).queryByText(fact.statement)).toBeNull();
    await act(async () => second({ ...detail, fact: companion }));
    expect(panel.getAttribute('aria-busy')).toBe('false');
    expect(within(panel).getByText(companion.statement)).toBeTruthy();
  });
  it('distinguishes an empty library, search results, and project selection', async () => {
    const api = setup();
    vi.mocked(api.catalog).mockResolvedValue({ items: [], next_cursor: null });
    render(<MemoryView api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    expect(await screen.findByRole('heading', { name: '还没有跨对话记忆' })).toBeTruthy();
    fireEvent.change(screen.getByRole('textbox', { name: '搜索记忆' }), { target: { value: '散步' } });
    expect(await screen.findByRole('heading', { name: '没有找到匹配的记忆' })).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: '项目' }));
    expect(await screen.findByRole('heading', { name: '选择一个项目' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /删除记忆/ })).toBeNull();
  });
  it.each(['database_not_configured', 'database_configured_unverified', 'database_unavailable', 'database_schema_action_required'] as const)('gates %s without reading memories', async databaseState => {
    const api = setup();
    render(<MemoryView api={api} databaseState={databaseState} onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    expect(screen.getByRole('button', { name: /前往本地服务设置/ })).toBeTruthy();
    expect(api.catalog).not.toHaveBeenCalled(); expect(api.projects).not.toHaveBeenCalled();
  });
  it('keeps details inside the page, confirms full effects, deletes exactly once', async () => {
    const api = setup();
    render(<MemoryView api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    const detail = await screen.findByRole('complementary', { name: '记忆详情' });
    expect(within(detail).queryByRole('button', { name: '在对话中查看' })).toBeNull();
    expect(within(detail).queryByRole('button', { name: '编辑' })).toBeNull();
    fireEvent.click(within(detail).getByRole('button', { name: /删除记忆/ }));
    await screen.findByRole('region', { name: '删除影响' });
    expect(screen.getByText('将删除')).toBeTruthy();
    expect(api.delete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledExactlyOnceWith(fact.fact_id, confirmation));
    expect(await screen.findByText(/记忆已删除。/)).toBeTruthy();
  });
  it('replaces drifted confirmation and prevents unresolved deletion', async () => {
    const api = setup();
    const fresh: MemoryRecord[] = [{ type: 'HEADER', root: fact.fact_id, view: 'global', workspace_id: null, disposition: 'NEEDS_RESOLUTION' }, { type: 'FACT_RESTORE', fact, planned_lifecycle: 'ACTIVE' }, { type: 'END', counts: { HEADER: 1, FACT_RESTORE: 1 } }];
    vi.mocked(api.delete).mockRejectedValue(new MemoryApiError('记忆发生变化', 409, fresh));
    render(<MemoryView api={api} databaseState="ready" onOpenSettings={vi.fn()} onOpenSource={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /用户喜欢散步/ }));
    fireEvent.click(await screen.findByRole('button', { name: /删除记忆/ }));
    fireEvent.click(await screen.findByRole('button', { name: '确认删除' }));
    await screen.findByRole('checkbox');
    expect((screen.getByRole('button', { name: '确认删除' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('alert').textContent).toContain('记忆发生变化');
  });
  it('clears loaded memories on database readiness loss', async () => {
    const api = setup(); const props = { api, onOpenSettings: vi.fn(), onOpenSource: vi.fn() };
    const view = render(<MemoryView {...props} databaseState="ready" />);
    await screen.findByRole('button', { name: /用户喜欢散步/ });
    view.rerender(<MemoryView {...props} databaseState="database_unavailable" />);
    expect(screen.queryByText(fact.statement)).toBeNull();
  });
});
