import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { PresentationResult } from '../../frontend/components/presentation-result';
import { parsePresentationResult } from '../../frontend/lib/presentation-targets';

afterEach(cleanup);
it('keeps accepted targets actionable and rejected targets separate', () => {
  const target = { type: 'file' as const, path: '/project/guide.md' };
  const open = vi.fn();
  render(<PresentationResult result={{ targets: [target, { type: 'panel', name: 'capabilities' }], errors: [{ index: 1, message: '地址不支持' }] }} onOpen={open} />);
  expect(screen.getAllByRole('button')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: /guide.md/ }));
  expect(open).toHaveBeenCalledWith([target]);
  expect(screen.getByRole('listitem').textContent).toBe('第 2 项地址不支持');
});
it('renders only rejection details when the batch accepted nothing', () => {
  render(<PresentationResult result={{ targets: [], errors: [{ index: 0, message: '目标不存在' }] }} />);
  expect(screen.queryByRole('button')).toBeNull();
  expect(screen.getByText('目标不存在', { exact: false })).toBeTruthy();
});
it('adapts public file and host-bound process targets to native display requests', () => {
  const result = parsePresentationResult(JSON.stringify({targets:[
    {target:{kind:'PATH',path:'/project/a.md'}},
    {target:{kind:'PROCESS',process_id:'p1'},host_session_id:'host:one'},
  ]}));
  expect(result?.targets).toEqual([
    {type:'file',path:'/project/a.md'},
    {type:'terminal',process_id:'p1',host_session_id:'host:one'},
  ]);
  expect(parsePresentationResult(JSON.stringify({targets:[{target:{kind:'PROCESS',process_id:'p1'}}]}))).toBeUndefined();
  expect(parsePresentationResult(JSON.stringify({targets:[{type:'file',path:'/project/a.md'}]}))).toBeUndefined();
});

it('parses only closed Agent and Cell envelopes with their original ROOT and Host', () => {
  const agent = 'session:' + 'a'.repeat(26), cell = 'cell:' + 'a'.repeat(26);
  const targets = [
    { target: { kind: 'AGENT', agent_id: agent }, root_session_id: 'root:one' },
    { target: { kind: 'CELL', cell_id: cell }, root_session_id: 'root:one', host_session_id: 'host:one' },
    { target: { kind: 'PANEL', name: 'subagents' }, root_session_id: 'root:one' },
    { target: { kind: 'PANEL', name: 'cells' }, root_session_id: 'root:one', host_session_id: 'host:one' },
  ];
  expect(parsePresentationResult(JSON.stringify({ targets }))?.targets).toEqual([
    { type: 'agent', agent_id: agent, root_session_id: 'root:one' },
    { type: 'cell', cell_id: cell, root_session_id: 'root:one', host_session_id: 'host:one' },
    { type: 'panel', name: 'subagents', root_session_id: 'root:one' },
    { type: 'panel', name: 'cells', root_session_id: 'root:one', host_session_id: 'host:one' },
  ]);
  const rejected = [
    { target: { kind: 'AGENT', agent_id: agent } },
    { ...targets[0], host_session_id: 'host:one' },
    { target: { kind: 'CELL', cell_id: cell }, root_session_id: 'root:one' },
    { ...targets[1], target: { kind: 'CELL', cell_id: cell, view: 'graph' } },
    { ...targets[1], target: { kind: 'CELL', cell_id: 'cell:invented' } },
    { target: { kind: 'WORK', work_id: 'queue:' + 'a'.repeat(26) }, root_session_id: 'root:one' },
    { target: { kind: 'PANEL', name: 'cell_graphs' }, root_session_id: 'root:one' },
    { target: { kind: 'PANEL', name: 'subagents' } },
    { target: { kind: 'PANEL', name: 'cells' }, host_session_id: 'host:one' },
  ];
  for (const target of rejected) expect(parsePresentationResult(JSON.stringify({ targets: [target] }))).toBeUndefined();
  expect(parsePresentationResult(JSON.stringify({ targets: [], errors: [{ index: 0, message: 'bad', view: 'graph' }] }))).toBeUndefined();
});

it('uses the five exact panel labels and keeps object buttons actionable', () => {
  const open = vi.fn();
  render(<PresentationResult onOpen={open} result={{ targets: [
    { type: 'panel', name: 'capabilities' }, { type: 'panel', name: 'background' },
    { type: 'panel', name: 'subagents', root_session_id: 'one' },
    { type: 'panel', name: 'cells', root_session_id: 'one', host_session_id: 'host:one' },
    { type: 'panel', name: 'browser' },
    { type: 'agent', agent_id: 'session:agent', root_session_id: 'one' },
    { type: 'cell', cell_id: 'cell:one', root_session_id: 'one', host_session_id: 'host:one' },
  ] }} />);
  for (const name of ['能力', '进程', '子代理', 'cell', '网页']) expect(screen.getByRole('button', { name, exact: true })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'session:agent' }));
  expect(open).toHaveBeenCalledWith([{ type: 'agent', agent_id: 'session:agent', root_session_id: 'one' }]);
});
