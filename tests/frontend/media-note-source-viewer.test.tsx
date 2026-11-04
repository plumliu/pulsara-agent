import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MediaNoteSourceDialog } from '../../frontend/components/media-note-source-dialog';
import type { AgentConversationReader, RuntimeProjection } from '../../frontend/lib/runtime-adapter';

afterEach(()=>{cleanup();vi.unstubAllGlobals();});
it('locates the exact canonical result in the existing conversation/process viewer',async()=>{
  vi.stubGlobal('ResizeObserver',class {observe(){}disconnect(){}});
  vi.stubGlobal('matchMedia',()=>({matches:false,addEventListener(){},removeEventListener(){}}));
  const note={resultId:'entry:result-source',entryId:'entry:result-source',sessionId:'session:archived',from:1,to:2,text:'Original annotation'};
  const projection={messages:[{id:'entry:tool-call',turnId:'turn:source',role:'assistant',assistantKind:'tool-request',time:'',body:'',status:'completed',traces:[{id:'call:source',kind:'tool',toolName:'annotate_media',title:'annotate_media',subtitle:'已完成',status:'completed',resultEntryId:note.entryId,resultText:'{"media_annotation":"added"}'}]},{id:'entry:answer',turnId:'turn:source',role:'assistant',assistantKind:'terminal',time:'',body:'Source conversation retained.',status:'completed',rootFinal:true}],canonicalInputs:[],queuedPrompts:[],queuedCount:0,isRunning:false} as unknown as RuntimeProjection;
  const locateEntry=vi.fn(async()=>projection);
  const reader={view:'history',sessionId:note.sessionId,current:()=>projection,subscribe:()=>()=>{},close:vi.fn(async()=>{}),locateEntry,readHistory:vi.fn(),hydrateEntries:vi.fn(),readLatestHistory:vi.fn(),readProcessEnd:vi.fn(),retainHistory:vi.fn(),readToolOutput:vi.fn(),readPromptImage:vi.fn(),readImageReference:vi.fn(),readVisualizationContent:vi.fn(),readVisualizationThumbnail:vi.fn()} as unknown as AgentConversationReader;
  render(<MediaNoteSourceDialog note={note} adapter={{openHistoryReader:vi.fn(async()=>reader)}} onClose={vi.fn()} onNotify={vi.fn()}/>);
  await screen.findByText('Source conversation retained.');
  await waitFor(()=>expect(locateEntry).toHaveBeenCalledWith(note.entryId,expect.any(AbortSignal)));
  await screen.findByText('annotate_media');
  expect(document.querySelector('[data-result-entry="entry:result-source"]')).toBeTruthy();
});
