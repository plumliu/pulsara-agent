import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { PresentationResult } from '../../frontend/components/presentation-result';
import { parsePresentationTargets } from '../../frontend/lib/presentation-targets';

afterEach(cleanup);
it('keeps actual accepted targets actionable independently of public failure prose', () => {
  const target = { type: 'file' as const, path: '/project/guide.md' };
  const open = vi.fn();
  render(<PresentationResult result={{ targets: [target] }} onOpen={open} />);
  fireEvent.click(screen.getByRole('button', { name: /guide.md/ }));
  expect(open).toHaveBeenCalledWith([target]);
});
it('adapts closed accepted PATH, URL and Agent facts', () => {
  const agent = 'session:' + 'a'.repeat(26);
  expect(parsePresentationTargets([
    {target:{kind:'PATH',path:'/project/a.md'}},
    {target:{kind:'URL',url:'https://example.com'}},
    {target:{kind:'AGENT',agent_id:agent},root_session_id:agent},
  ])?.targets).toEqual([
    {type:'file',path:'/project/a.md'},
    {type:'browser',url:'https://example.com'},
    {type:'agent',agent_id:agent,root_session_id:agent},
  ]);
  expect(parsePresentationTargets([])?.targets).toEqual([]);
  for (const item of [
    {target:{kind:'PATH',path:'relative.md'}},
    {target:{kind:'AGENT',agent_id:agent}},
    {target:{kind:'AGENT',agent_id:'invented'},root_session_id:agent},
    {target:{kind:'CELL',cell_id:'cell:'+'a'.repeat(26)}},
    {target:{kind:'URL',url:'https://example.com',extra:true}},
  ]) expect(parsePresentationTargets([item])).toBeUndefined();
  expect(parsePresentationTargets('{"targets":[]}')).toBeUndefined();
});
it('retains internal panel buttons used by application navigation', () => {
  const open=vi.fn();
  render(<PresentationResult onOpen={open} result={{targets:[{type:'panel',name:'capabilities'}]}} />);
  fireEvent.click(screen.getByRole('button',{name:'能力'}));
  expect(open).toHaveBeenCalledWith([{type:'panel',name:'capabilities'}]);
});
