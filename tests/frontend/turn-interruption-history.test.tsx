import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ConversationMessages } from '../../frontend/components/workbench-view';
import { TurnInterruptionHistoryNotice } from '../../frontend/components/turn-interruption-notice';
import type { TurnInterruptionNotice } from '../../frontend/lib/runtime-adapter';

afterEach(cleanup);
const notice: TurnInterruptionNotice = {ownerKind:'EXECUTED_TURN',ownerId:'stopped',reason:'PROVIDER_REQUEST_FAILED',publicDetail:'rate_limited: retry later',terminalAtUtc:'2026-10-05T00:00:00Z',displayAfterEntrySequence:2,displayAfterMessageId:'partial'};

it('shows quoted details through focus, pinned click and Escape in readonly history', () => {
  render(<TurnInterruptionHistoryNotice notice={notice} />);
  const trigger = screen.getByRole('button',{name:'查看中断详情'});
  fireEvent.focus(trigger);
  expect(screen.getByRole('tooltip').textContent).toContain('rate_limited: retry later');
  fireEvent.click(trigger); fireEvent.mouseLeave(trigger); fireEvent.blur(trigger);
  expect(screen.getByRole('tooltip')).toBeTruthy();
  fireEvent.keyDown(document,{key:'Escape'});
  expect(screen.queryByRole('tooltip')).toBeNull();
  fireEvent.click(trigger);
  expect(screen.getByRole('tooltip')).toBeTruthy();
});

it('keeps each notice outside intermediate results while the next turn runs and completes', () => {
  const props={artifactOwnerKey:'session',onReadToolArtifact:vi.fn(),onNotify:vi.fn(),interruptionNotices:[notice],
    messages:[{id:'input',role:'user' as const,turnId:'stopped',time:'now',body:'question',entrySequence:1},
      {id:'partial',role:'assistant' as const,turnId:'stopped',assistantKind:'trajectory' as const,time:'now',body:'partial answer',entrySequence:2},
      {id:'next',role:'user' as const,turnId:'next-turn',time:'now',body:'continue',entrySequence:3}]};
  const view=render(<ConversationMessages {...props} isRunning={true}/>);
  const marker=screen.getByText('本轮回复已中断。').closest('.conversation-interruption')!;
  expect(marker.closest('.conversation-run')).toBeNull();
  expect(marker.compareDocumentPosition(screen.getByText('continue')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  view.rerender(<ConversationMessages {...props} isRunning={false}/>);
  expect(screen.getAllByText('本轮回复已中断。')).toHaveLength(1);
});

it('renders a standalone mounted notice even when its feedback entry has no visible message', () => {
  const view=render(<ConversationMessages messages={[]} interruptionNotices={[{...notice,displayAfterMessageId:undefined}]}
    artifactOwnerKey="feedback-only" onReadToolArtifact={vi.fn()} onNotify={vi.fn()} />);
  expect(screen.getAllByText('本轮回复已中断。')).toHaveLength(1);
  expect(view.container.querySelector('.conversation-interruption')?.closest('.conversation-run')).toBeNull();
});
