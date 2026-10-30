import { act, renderHook, waitFor, cleanup } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { usePendingSteerPages } from '../../frontend/lib/pending-steer-pages';
import type { QueuedPrompt, RuntimeProjection } from '../../frontend/lib/runtime-adapter';
afterEach(cleanup);
const item=(sequence:number,target='turn:a'):QueuedPrompt=>({queueItemId:`queue:${sequence}`,sequence:String(sequence),status:'pending',deliveryMode:'steer',targetTurnId:target,inputOrigin:'CELL_OUTPUT',sourceKind:'cell',sourceExecAttemptId:'attempt:a',content:{parts:[{type:'text',text:String(sequence)}]}});
const projection=(eventSequence:number,target='turn:a')=>({eventSequence,queuedPrompts:[],canonicalInputs:[],pendingSteerPage:{targetTurnId:target,totalCount:130,nextCursor:'128'}} as unknown as RuntimeProjection);
it('pages more than 128 exact active inputs without counting or loading them as future turns',async()=>{
 const rows=Array.from({length:130},(_,i)=>item(i+1));let consumed=false;
 const reader={readQueuePage:vi.fn(async(cursor?:string,_signal?:AbortSignal,lane?:{targetTurnId:string})=>{expect(lane).toEqual({deliveryMode:'steer',targetTurnId:'turn:a'});return cursor?{items:rows.slice(128),nextCursor:null}:{items:consumed?[]:rows.slice(0,128),nextCursor:consumed?null:'128'};}),readQueueItem:vi.fn(async()=>null),readQueueStatus:vi.fn(async()=>({queueItemId:'unused',status:'CONSUMED' as const,deliveryMode:'steer' as const,targetTurnId:'turn:a',consumedEntryId:'entry:outside-window'}))};
 const {result,rerender}=renderHook(({cut})=>usePendingSteerPages(reader,projection(cut)),{initialProps:{cut:1}});
 await waitFor(()=>expect(result.current.items).toHaveLength(128));expect(result.current.nextCursor).toBe('128');
 act(()=>result.current.more());await waitFor(()=>expect(result.current.items).toHaveLength(130));expect(result.current.nextCursor).toBeNull();
 expect(result.current.items.map(row=>row.sequence)).toEqual(rows.map(row=>row.sequence));
 consumed=true;rerender({cut:2});await waitFor(()=>expect(result.current.items).toHaveLength(0));
 expect(reader.readQueueStatus).toHaveBeenCalled();expect(reader.readQueueItem).not.toHaveBeenCalled();
});
it('does not leak a retired exact turn page into its successor',async()=>{
 let resolve!:(value:{items:QueuedPrompt[];nextCursor:null})=>void;
 const reader={readQueuePage:vi.fn().mockReturnValueOnce(new Promise(done=>{resolve=done;})).mockResolvedValue({items:[item(2,'turn:b')],nextCursor:null}),readQueueItem:vi.fn(),readQueueStatus:vi.fn()};
 const {result,rerender}=renderHook(({target})=>usePendingSteerPages(reader,projection(1,target)),{initialProps:{target:'turn:a'}});
 rerender({target:'turn:b'});await waitFor(()=>expect(result.current.items.map(row=>row.targetTurnId)).toEqual(['turn:b']));
 await act(async()=>resolve({items:[item(1,'turn:a')],nextCursor:null}));expect(result.current.items.map(row=>row.targetTurnId)).toEqual(['turn:b']);
});
