import { cleanup, fireEvent, render, screen, waitFor, act } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MediaNoteSourceDialog } from '../../frontend/components/media-note-source-dialog';
import type { AgentConversationReader, RuntimeProjection } from '../../frontend/lib/runtime-adapter';

const viewer=vi.hoisted(()=>vi.fn());
vi.mock('../../frontend/components/workbench-view',()=>({AgentConversationView:(props:unknown)=>{viewer(props);return <div>Canonical source viewer</div>;}}));
const note={resultId:'entry:result',entryId:'entry:source-result',sessionId:'session:archived',from:1,to:2,text:'Original annotation'};
const projection={messages:[],canonicalInputs:[],queuedPrompts:[],queuedCount:0,isRunning:false} as unknown as RuntimeProjection;
function reader(){return {view:'history',sessionId:note.sessionId,current:()=>projection,subscribe:vi.fn(()=>vi.fn()),close:vi.fn(async()=>{}),readToolOutput:vi.fn(),readPromptImage:vi.fn(),readImageReference:vi.fn(),readVisualizationContent:vi.fn(),readVisualizationThumbnail:vi.fn()} as unknown as AgentConversationReader;}
afterEach(()=>{cleanup();vi.clearAllMocks();});
it('reviews the exact archived source in the existing viewer and keeps the mounted player untouched',async()=>{
  const source=reader(),openHistoryReader=vi.fn(async()=>source),close=vi.fn();
  const view=render(<><audio data-testid="playing" src="/bound/audio"/><MediaNoteSourceDialog note={note} adapter={{openHistoryReader}} onClose={close} onNotify={vi.fn()}/></>);
  const player=screen.getByTestId('playing');
  await screen.findByText('Canonical source viewer');
  expect(openHistoryReader).toHaveBeenCalledExactlyOnceWith(note.sessionId,expect.any(AbortSignal));
  expect(viewer.mock.calls.at(-1)?.[0]).toMatchObject({sessionId:note.sessionId,reader:source,entryLocation:{entryId:note.entryId}});
  fireEvent.click(screen.getByRole('button',{name:'关闭批注来源'}));
  expect(close).toHaveBeenCalledOnce();
  expect(screen.getByTestId('playing')).toBe(player);
  expect(player.getAttribute('src')).toBe('/bound/audio');
  view.unmount();
  await waitFor(()=>expect(source.close).toHaveBeenCalledOnce());
});
it('discards closed source intent and closes late readers without resurrecting their viewer',async()=>{
  let finish!:(value:AgentConversationReader)=>void;
  const pending=new Promise<AgentConversationReader>(resolve=>{finish=resolve;});
  const source=reader(),openHistoryReader=vi.fn(()=>pending);
  const view=render(<MediaNoteSourceDialog note={note} adapter={{openHistoryReader}} onClose={vi.fn()} onNotify={vi.fn()}/>);
  view.unmount();
  await act(async()=>{finish(source);await pending;});
  expect(source.close).toHaveBeenCalledOnce();
  expect(viewer).not.toHaveBeenCalled();
  expect(screen.queryByRole('dialog')).toBeNull();
});
it('shows denied source access without opening any conversation viewer',async()=>{
  render(<MediaNoteSourceDialog note={note} adapter={{openHistoryReader:vi.fn(async()=>{throw Error('来源无访问权限');})}} onClose={vi.fn()} onNotify={vi.fn()}/>);
  expect((await screen.findByRole('alert')).textContent).toContain('来源无访问权限');
  expect(viewer).not.toHaveBeenCalled();
});
