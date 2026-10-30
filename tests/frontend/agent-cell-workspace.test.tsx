import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { agentCellApi, AgentCellError, prepareSessionTarget, type CellGraphPage } from '../../frontend/lib/agent-cell-api';
import { AgentList, CellGraph, CellList, CellSource, mergeParticipants } from '../../frontend/components/agent-cell-workspace';
vi.mock('../../frontend/components/file-code', () => ({ FileCode: ({ source }: { source: string }) => <pre>{source}</pre> }));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });
const cell = { target: { kind: 'CELL' as const, cell_id: 'cell:a' }, state: 'running', has_unread_output: false, pending_calls: 0, source_exec_attempt_id: 'attempt:a' };
function page(section: 'works' | 'participants', items: CellGraphPage['items'], coverage: CellGraphPage['participation_coverage'] = 'complete'): CellGraphPage {
  return { cell, section, items, participation_coverage: coverage, next_cursor: null };
}
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
it('does not remove an Agent missing from a weak page until exact lookup confirms retirement', async () => {
  const row = { agent_id: 'agent:a', title: 'A', current_work_id: null, queued_count: 0 };
  vi.spyOn(agentCellApi, 'agents').mockResolvedValueOnce({ agents: [row], next_cursor: 'cursor' }).mockResolvedValue({ agents: [], next_cursor: null });
  vi.spyOn(agentCellApi, 'agent').mockResolvedValueOnce({ agent: row }).mockRejectedValueOnce(new AgentCellError(404, 'retired'));
  render(<AgentList root="root:a" onOpen={() => {}} />);
  expect(await screen.findByText('A')).toBeTruthy();
  fireEvent.click(screen.getByText('加载更多'));
  await waitFor(() => expect(agentCellApi.agent).toHaveBeenCalledTimes(1));
  expect(screen.getByText('A')).toBeTruthy();
  await new Promise(resolve => setTimeout(resolve, 3100));
  await waitFor(() => expect(screen.queryByText('A')).toBeNull());
});
it('checks an omitted Cell directly before removing it from a weakly paged list', async () => {
  vi.spyOn(agentCellApi, 'cells').mockResolvedValueOnce({ cells: [cell], next_cursor: 'cursor', has_more: true }).mockResolvedValue({ cells: [], next_cursor: null, has_more: false });
  vi.spyOn(agentCellApi, 'cell').mockRejectedValue(new AgentCellError(404, 'retired'));
  render(<CellList root="root:a" host="host:a" onOpen={() => {}} />);
  expect(await screen.findByText('cell:a')).toBeTruthy();
  fireEvent.click(screen.getByText('加载更多'));
  await waitFor(() => expect(agentCellApi.cell).toHaveBeenCalledWith('root:a', 'host:a', 'cell:a', expect.any(AbortSignal)));
  await waitFor(() => expect(screen.queryByText('cell:a')).toBeNull());
});
it('clears a Cell list and pagination when its bound Host is confirmed retired', async () => {
  vi.spyOn(agentCellApi, 'cells').mockResolvedValueOnce({ cells: [cell], next_cursor: 'cursor', has_more: true }).mockRejectedValue(new AgentCellError(404, 'retired'));
  render(<CellList root="root:a" host="host:a" onOpen={() => {}} />);
  expect(await screen.findByText('cell:a')).toBeTruthy();
  fireEvent.click(screen.getByText('加载更多'));
  expect(await screen.findByText('对象已不可用')).toBeTruthy();
  expect(screen.queryByText('cell:a')).toBeNull(); expect(screen.queryByText('加载更多')).toBeNull();
});
it('keeps a Cell list during a temporary Host service failure', async () => {
  vi.spyOn(agentCellApi, 'cells').mockResolvedValueOnce({ cells: [cell], next_cursor: 'cursor', has_more: true }).mockRejectedValue(new AgentCellError(503, '暂不可用'));
  render(<CellList root="root:a" host="host:a" onOpen={() => {}} />);
  expect(await screen.findByText('cell:a')).toBeTruthy();
  fireEvent.click(screen.getByText('加载更多'));
  expect(await screen.findByText('暂不可用')).toBeTruthy();
  expect(screen.getByText('cell:a')).toBeTruthy();
  expect(screen.queryByText('对象已不可用')).toBeNull();
});
it('merges growing participation kinds by the exact Agent and Work identities', () => {
  expect(mergeParticipants(
    [{ agent_id: 'agent:a', work_id: 'work:a', kinds: ['query'] }],
    [{ agent_id: 'agent:a', work_id: 'work:a', kinds: ['query', 'message'] }, { agent_id: 'agent:a', work_id: null, kinds: ['wait'] }],
  )).toEqual([
    { agent_id: 'agent:a', work_id: 'work:a', kinds: ['query', 'message'] },
    { agent_id: 'agent:a', work_id: null, kinds: ['wait'] },
  ]);
});
it('continues canonical source paging after the live graph closes', async () => {
  const late = deferred<Awaited<ReturnType<typeof agentCellApi.source>>>(); let closed = false;
  vi.spyOn(agentCellApi, 'source').mockResolvedValueOnce({ source_exec_attempt_id: 'attempt:a', source: { status: 'AVAILABLE', text: 'const x = 1;', offset_chars: 0, returned_chars: 12, total_chars: 18, has_more: true, next_offset_chars: 12 } }).mockReturnValueOnce(late.promise);
  const live = vi.spyOn(agentCellApi, 'cell');
  vi.spyOn(agentCellApi, 'graph').mockImplementation(async (_r, _h, _c, section) => closed ? { status: 'CLOSED', message: '实时工作流视图已关闭' } : page(section, []));
  render(<CellSource attempt="attempt:a" root="root:a" host="host:a" cell="cell:a" onGraph={() => {}} />);
  expect(await screen.findByText('const x = 1;')).toBeTruthy();
  expect(await screen.findByRole('button', { name: '查看工作流图' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '加载更多' })); closed = true;
  await new Promise(resolve => setTimeout(resolve, 3100));
  await waitFor(() => expect(screen.queryByRole('button', { name: '查看工作流图' })).toBeNull());
  expect(screen.getByText('const x = 1;')).toBeTruthy(); expect(live).not.toHaveBeenCalled();
  await act(async () => { late.resolve({ source_exec_attempt_id: 'attempt:a', source: { status: 'AVAILABLE', text: 'MORE', offset_chars: 12, returned_chars: 4, total_chars: 16, has_more: false, next_offset_chars: null } }); await late.promise; });
  expect(screen.getByText('const x = 1;MORE')).toBeTruthy();
  expect(agentCellApi.source).toHaveBeenNthCalledWith(2, 'root:a', 'attempt:a', 12, expect.any(AbortSignal));
});
it('expands exact predecessor Work and preserves the directed dependency between agents', async () => {
  vi.spyOn(agentCellApi, 'graph').mockImplementation(async (_r, _h, _c, section) => page(section, section === 'works' ? [{ agent_id: 'agent:b', work_id: 'work:b', status: 'running' }] : []));
  vi.spyOn(agentCellApi, 'work').mockImplementation(async (_r, id) => id === 'work:b'
    ? { work: { agent_id: 'agent:b', work_id: id, status: 'running' }, inputs: [{ kind: 'dependency', source_work_id: 'work:a', ordinal: 0 }] }
    : { work: { agent_id: 'agent:a', work_id: id, status: 'completed' }, inputs: [] });
  vi.spyOn(agentCellApi, 'agent').mockImplementation(async (_r, id) => ({ agent: { agent_id: id, title: id, current_work_id: null, queued_count: 0 } }));
  const select = vi.fn(); render(<CellGraph root="root:a" host="host:a" cell="cell:a" onAgent={() => {}} onWork={select} />);
  expect(await screen.findByText('work:b')).toBeTruthy();
  fireEvent.click(screen.getByTitle('展开前置工作'));
  expect(await screen.findByText('work:a')).toBeTruthy();
  await waitFor(() => expect(document.querySelectorAll('.agent-cell-graph-lines > path').length).toBe(1));
  fireEvent.click(screen.getByTitle('收起前置工作'));
  expect(screen.queryByText('work:a')).toBeNull();
  fireEvent.click(screen.getByTitle('展开前置工作'));
  expect(await screen.findByText('work:a')).toBeTruthy();
  fireEvent.click(screen.getByText('work:b')); expect(select).toHaveBeenCalledWith(expect.objectContaining({ work_id: 'work:b' }));
});
it('does not present incomplete observation as a complete empty graph', async () => {
  vi.spyOn(agentCellApi, 'graph').mockImplementation(async (_r, _h, _c, section) => page(section, [], 'incomplete'));
  render(<CellGraph root="root:a" host="host:a" cell="cell:a" onAgent={() => {}} onWork={() => {}} />);
  expect(await screen.findByText('部分参与信息暂不可用')).toBeTruthy();
  expect(screen.queryByText('暂无子代理编排')).toBeNull();
});
it('retains a participant name on temporary failure and clears it after confirmed retirement', async () => {
  vi.spyOn(agentCellApi, 'graph').mockImplementation(async (_r, _h, _c, section) => page(section, section === 'participants' ? [{ agent_id: 'agent:a', work_id: null, kinds: ['query'] }] : []));
  vi.spyOn(agentCellApi, 'agent').mockResolvedValueOnce({ agent: { agent_id: 'agent:a', title: 'Researcher', current_work_id: null, queued_count: 0 } }).mockRejectedValueOnce(new AgentCellError(503, 'temporary')).mockRejectedValue(new AgentCellError(404, 'retired'));
  render(<CellGraph root="root:a" host="host:a" cell="cell:a" onAgent={() => {}} onWork={() => {}} />);
  expect(await screen.findByText('Researcher')).toBeTruthy();
  await new Promise(resolve => setTimeout(resolve, 3100));
  expect(await screen.findByText('部分对象暂时无法刷新。')).toBeTruthy();
  expect(screen.getByText('Researcher')).toBeTruthy();
  await new Promise(resolve => setTimeout(resolve, 3100));
  await waitFor(() => expect(screen.queryByText('Researcher')).toBeNull());
}, 10000);
it('keeps the graph entry and avoids an empty conclusion when a filtered page has a continuation', async () => {
  vi.spyOn(agentCellApi, 'source').mockResolvedValue({ source_exec_attempt_id: 'attempt:a', source: { status: 'AVAILABLE', text: 'code', offset_chars: 0, returned_chars: 4, total_chars: 4, has_more: false, next_offset_chars: null } });
  vi.spyOn(agentCellApi, 'graph').mockImplementation(async (_r, _h, _c, section) => ({ ...page(section, []), next_cursor: section === 'participants' ? 'more' : null }));
  const source = render(<CellSource attempt="attempt:a" root="root:a" host="host:a" cell="cell:a" onGraph={() => {}} />);
  expect(await screen.findByRole('button', { name: '查看工作流图' })).toBeTruthy(); source.unmount();
  render(<CellGraph root="root:a" host="host:a" cell="cell:a" onAgent={() => {}} onWork={() => {}} />);
  expect(await screen.findByRole('button', { name: '加载更多' })).toBeTruthy();
  expect(screen.queryByText('暂无子代理编排')).toBeNull();
});
it('rescans expanded graph pages and discovers a later Work after the first page', async () => {
  let added = false;
  vi.spyOn(agentCellApi, 'graph').mockImplementation(async (_r, _h, _c, section, cursor) => {
    if (section === 'participants') return page(section, []);
    if (!cursor) return { ...page(section, [{ agent_id: 'agent:a', work_id: 'work:a', status: 'running' }]), next_cursor: 'next' };
    return page(section, added ? [{ agent_id: 'agent:b', work_id: 'work:b', status: 'running' }] : []);
  });
  vi.spyOn(agentCellApi, 'work').mockImplementation(async (_r, id) => ({ work: { agent_id: id === 'work:a' ? 'agent:a' : 'agent:b', work_id: id, status: 'running' }, inputs: [] }));
  vi.spyOn(agentCellApi, 'agent').mockImplementation(async (_r, id) => ({ agent: { agent_id: id, title: id, current_work_id: null, queued_count: 0 } }));
  render(<CellGraph root="root:a" host="host:a" cell="cell:a" onAgent={() => {}} onWork={() => {}} />);
  expect(await screen.findByText('work:a')).toBeTruthy(); fireEvent.click(screen.getByText('加载更多'));
  await waitFor(() => expect(screen.queryByText('加载更多')).toBeNull());
  added = true; await new Promise(resolve => setTimeout(resolve, 3100));
  expect(await screen.findByText('work:b')).toBeTruthy();
});
it('retries a Work from a later graph page after a transient detail failure', async () => {
  let attempts = 0;
  vi.spyOn(agentCellApi, 'graph').mockImplementation(async (_r, _h, _c, section, cursor) => {
    if (section === 'participants') return page(section, []);
    if (!cursor) return { ...page(section, [{ agent_id: 'agent:a', work_id: 'work:a', status: 'running' }]), next_cursor: 'next' };
    return page(section, [{ agent_id: 'agent:b', work_id: 'work:b', status: 'running' }]);
  });
  vi.spyOn(agentCellApi, 'work').mockImplementation(async (_r, id) => {
    if (id === 'work:b' && attempts++ === 0) throw new AgentCellError(503, 'unavailable');
    return { work: { agent_id: id === 'work:a' ? 'agent:a' : 'agent:b', work_id: id, status: 'running' }, inputs: [] };
  });
  vi.spyOn(agentCellApi, 'agent').mockImplementation(async (_r, id) => ({ agent: { agent_id: id, title: id, current_work_id: null, queued_count: 0 } }));
  render(<CellGraph root="root:a" host="host:a" cell="cell:a" onAgent={() => {}} onWork={() => {}} />);
  expect(await screen.findByText('work:a')).toBeTruthy(); fireEvent.click(screen.getByText('加载更多'));
  expect(await screen.findByText('部分对象暂时无法刷新。')).toBeTruthy();
  await new Promise(resolve => setTimeout(resolve, 3100));
  expect(await screen.findByText('work:b')).toBeTruthy();
});
it('does not resurrect a graph from a late predecessor read after Cell retirement', async () => {
  const late = deferred<Awaited<ReturnType<typeof agentCellApi.work>>>(); let isRetired = false;
  vi.spyOn(agentCellApi, 'graph').mockImplementation(async (_r, _h, _c, section) => {
    if (isRetired) return { status: 'CLOSED', message: '实时工作流视图已关闭' };
    return page(section, section === 'works' ? [{ agent_id: 'agent:b', work_id: 'work:b', status: 'running' }] : []);
  });
  vi.spyOn(agentCellApi, 'work').mockImplementation(async (_r, id) => id === 'work:a' ? late.promise : { work: { agent_id: 'agent:b', work_id: id, status: 'running' }, inputs: [{ kind: 'dependency', source_work_id: 'work:a', ordinal: 0 }] });
  vi.spyOn(agentCellApi, 'agent').mockImplementation(async (_r, id) => ({ agent: { agent_id: id, title: id, current_work_id: null, queued_count: 0 } }));
  render(<CellGraph root="root:a" host="host:a" cell="cell:a" onAgent={() => {}} onWork={() => {}} />);
  expect(await screen.findByText('work:b')).toBeTruthy();
  fireEvent.click(screen.getByTitle('展开前置工作'));
  isRetired = true;
  await new Promise(resolve => setTimeout(resolve, 3100));
  expect(await screen.findByText('实时工作流视图已关闭')).toBeTruthy();
  await act(async () => { late.resolve({ work: { agent_id: 'agent:a', work_id: 'work:a', status: 'completed' }, inputs: [] }); await late.promise; });
  expect(screen.queryByText('work:a')).toBeNull(); expect(screen.queryByText('work:b')).toBeNull();
});
it('discards a prepared target when its ROOT navigation context has changed', async () => {
  vi.spyOn(agentCellApi, 'agent').mockResolvedValue({ agent: { agent_id: 'agent:a', title: 'A', current_work_id: null, queued_count: 0 } });
  expect(await prepareSessionTarget({ kind: 'agent', rootSessionId: 'root:a', agentSessionId: 'agent:a' }, () => false)).toBeUndefined();
  expect(agentCellApi.agent).toHaveBeenCalledWith('root:a', 'agent:a');
});
it('binds a Cell list tab to the exact Host that supplied the request', async () => {
  vi.spyOn(agentCellApi, 'cells').mockResolvedValue({ cells: [], next_cursor: null, has_more: false });
  const old = await prepareSessionTarget({ kind: 'cells', rootSessionId: 'root:a', hostSessionId: 'host:old' }, () => true);
  const current = await prepareSessionTarget({ kind: 'cells', rootSessionId: 'root:a', hostSessionId: 'host:new' }, () => true);
  expect(old?.adopt()).toMatchObject({ kind: 'cells', sessionId: 'root:a', hostSessionId: 'host:old' });
  expect(current?.adopt()?.id).not.toBe(old?.adopt()?.id);
});

it('opens a source tab using the canonical attempt without live Cell authorization',async()=>{
 const source=vi.spyOn(agentCellApi,'source').mockResolvedValue({source_exec_attempt_id:'attempt:historical',source:{status:'AVAILABLE',text:'actual hook code',offset_chars:0,returned_chars:16,total_chars:16,has_more:false,next_offset_chars:null}});
 const live=vi.spyOn(agentCellApi,'cell');
 const prepared=await prepareSessionTarget({kind:'cell',rootSessionId:'root:a',sourceExecAttemptId:'attempt:historical'},()=>true);
 expect(source).toHaveBeenCalledWith('root:a','attempt:historical');expect(live).not.toHaveBeenCalled();
 expect(prepared?.adopt()).toMatchObject({kind:'cell',sourceExecAttemptId:'attempt:historical'});
 expect(prepared?.adopt()).not.toHaveProperty('hostSessionId','host:a');
});
it('preserves the live graph through a temporary refresh failure without calling it closed',async()=>{
 vi.useFakeTimers();let fail=false;
 vi.spyOn(agentCellApi,'graph').mockImplementation(async(_r,_h,_c,section)=>{if(fail)throw new AgentCellError(503,'network');return page(section,section==='participants'?[{agent_id:'agent:a',work_id:null,kinds:['query']}]:[]);});
 vi.spyOn(agentCellApi,'agent').mockResolvedValue({agent:{agent_id:'agent:a',title:'Researcher',current_work_id:null,queued_count:0}});
 render(<CellGraph root="root:a" host="host:a" cell="cell:a" onAgent={()=>{}} onWork={()=>{}}/>);
 await act(async()=>{});expect(screen.getByText('Researcher')).toBeTruthy();fail=true;
 await act(async()=>{await vi.advanceTimersByTimeAsync(3000);});
 expect(screen.getByText('刷新失败：network')).toBeTruthy();expect(screen.getByText('Researcher')).toBeTruthy();
 expect(screen.queryByText('实时工作流视图已关闭')).toBeNull();
});

it('opens historical source with the live graph locator of that exact source attempt only',async()=>{
 vi.spyOn(agentCellApi,'source').mockResolvedValue({source_exec_attempt_id:'attempt:live',live_cell:{host_session_id:'host:exact',cell_id:'cell:exact'},source:{status:'AVAILABLE',text:'live code',offset_chars:0,returned_chars:9,total_chars:9,has_more:false,next_offset_chars:null}});
 const prepared=await prepareSessionTarget({kind:'cell',rootSessionId:'root:a',sourceExecAttemptId:'attempt:live'},()=>true);
 expect(prepared?.adopt()).toMatchObject({kind:'cell',sourceExecAttemptId:'attempt:live',hostSessionId:'host:exact',entityId:'cell:exact'});
});
