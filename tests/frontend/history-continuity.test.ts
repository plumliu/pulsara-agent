import {createHash, webcrypto} from 'node:crypto';
import {afterEach, expect, it, vi} from 'vitest';
import {CanonicalHistoryView, LocalHttpRuntimeAdapter, mergeRuntimeTaskInventory, type RuntimeConnection} from '../../frontend/lib/runtime-adapter';
const inline=(text:string)=>({inline_content:Buffer.from(text).toString('base64')});
const blob=(text:string)=>({digest:`sha256:${createHash('sha256').update(text).digest('hex')}`,size:String(Buffer.byteLength(text)),media_type:'text/plain',codec:'utf-8'});
const entry=(id:string,n:number,extra:object={})=>({entry_id:id,entry_sequence:String(n),turn_id:'turn:root',entry_kind:'ASSISTANT_MESSAGE',scope_kind:'ROOT',root_final:true,fork_eligible:true,blocks:[{block_id:`block:${id}`,block_kind:'TEXT',content:inline(id)}],...extra});
const cursor={session_id:'session:test',cut_sequence:'20',event_sequence_cut:'30',entry_sequence:'20'};
const snapshot=(entries:object[],older?:object,cut='20',eventCut='30')=>({session_id:'session:test',entry_sequence_cut:cut,event_sequence_cut:eventCut,writer_generation:'1',entries,older_history_cursor:older,control:{}});
const payload=(entries:object[],older?:object)=>({connection_id:'connection:test',connection_generation:1,session_id:'session:test',role:'controller',snapshot:{snapshot:snapshot(entries,older)},live_hello:{live_owner_epoch:'1'},live_control_snapshot:{snapshot:{}}});
const response=(v:unknown)=>new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}});
function deferred<T>(){let resolve!:(v:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return{promise,resolve};}
function context(body:any,extra:object={}){return{session_id:'session:test',cut_sequence:body.cut_sequence??'20',event_sequence_cut:body.event_sequence_cut??'30',entry_ids:body.entry_ids,groups:[{owner_id:'turn:root',owner_kind:'EXECUTED_TURN',initial_entry_id:'start'}],entry_contexts:body.entry_ids.map((id:string)=>({entry_id:id,owner_id:'turn:root',owner_kind:'EXECUTED_TURN',root_owner_id:'turn:root',segment_id:'segment:root',segment_start_entry_id:'start'})),tasks:[],batches:[],interruption_notices:[],compactions:[],...extra};}
function fixture(route:(url:string,body:any)=>unknown|Promise<unknown>){const calls:Array<{url:string;body:any}>=[];vi.stubGlobal('crypto',webcrypto);vi.stubGlobal('fetch',vi.fn(async(input:string,init:RequestInit={})=>{const url=String(input),body=JSON.parse(String(init.body??'{}'));calls.push({url,body});const v=await route(url,body);return v instanceof Response?v:response(v);}));return calls;}
const connect=async()=>await new LocalHttpRuntimeAdapter().connect('session:test') as RuntimeConnection;
const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
afterEach(()=>vi.unstubAllGlobals());


it.each([1, 3, 23])('keeps a short continuous conversation regardless of batch size %s', async batchSize => {
 let head=1; let accepted:object[]=[];
 fixture((url,body)=>{
  if(url.endsWith('/connections')) return payload([entry('e:1',1)]);
  if(url.endsWith('/context')) return context(body,{cut_sequence:String(head),event_sequence_cut:String(head+100)});
  if(url.endsWith('/observe')) return {observation:{through_event_sequence:String(head+100),committed:accepted.map(value=>({projection_kind:'IMMUTABLE_ENTRY',entry:value}))}};
  throw Error(url);
 });
 const reader=await connect();await flush();
 for(let start=2;start<=24;start+=batchSize){
  head=Math.min(start+batchSize-1,24);
  accepted=Array.from({length:head-start+1},(_,i)=>entry(`e:${start+i}`,start+i));
  await reader.observe();await flush();
  reader.retainHistory!({visibleEntryIds:['e:1'],pinnedEntryIds:[],followLatest:false});
  const p=reader.current();
  expect(p.messages.map(m=>m.id)).toEqual(Array.from({length:head},(_,i)=>`e:${i+1}`));
  expect(p.history!.windows).toHaveLength(1);
  expect(p.history!.atLatest).toBe(true);
 }
});

it.each([1, 7, 64])('keeps one visible band and a separate bounded tail across 600 entries, batch %s', async batchSize => {
 let head=1; let accepted:object[]=[];
 fixture((url,body)=>{
  if(url.endsWith('/connections')) return payload([entry('e:1',1)]);
  if(url.endsWith('/context')) return context(body,{cut_sequence:String(head),event_sequence_cut:String(head+1000)});
  if(url.endsWith('/observe')) return {observation:{through_event_sequence:String(head+1000),committed:accepted.map(value=>({projection_kind:'IMMUTABLE_ENTRY',entry:value}))}};
  throw Error(url);
 });
 const reader=await connect();await flush();
 for(let start=2;start<=600;start+=batchSize){
  head=Math.min(start+batchSize-1,600);
  accepted=Array.from({length:head-start+1},(_,i)=>entry(`e:${start+i}`,start+i));
  await reader.observe();await flush();
  reader.retainHistory!({visibleEntryIds:['e:1'],pinnedEntryIds:[],followLatest:false});
  const p=reader.current();
  expect(p.history!.windows).toHaveLength(1);
  expect(p.messages.map(m=>m.id)).toEqual(Array.from({length:p.messages.length},(_,i)=>`e:${i+1}`));
 }
 const owner=reader as unknown as {entries:Map<string,unknown>};
 expect(owner.entries.size).toBeLessThanOrEqual(257+batchSize*2);
 expect(owner.entries.has('e:300')).toBe(false);
 expect(owner.entries.has('e:600')).toBe(true);
 expect(reader.current().history!.atLatest).toBe(false);
 const latest=await reader.readLatestHistory!();
 expect(latest.history!.atLatest).toBe(true);
 expect(latest.messages.at(-1)?.id).toBe('e:600');
 const first=Number(latest.messages[0].id.slice(2));
 expect(latest.messages.map(m=>m.id)).toEqual(Array.from({length:601-first},(_,i)=>`e:${first+i}`));
 // The old DOM may report residence after the explicit switch. It cannot steal it back.
 reader.retainHistory!({visibleEntryIds:['e:1'],pinnedEntryIds:[],followLatest:false});
 expect(reader.current().messages.at(-1)?.id).toBe('e:600');
}, 15_000); // 600 sequential HTTP/context cycles also run under full-suite contention.

it('retains a selected convex range without interior holes and reclaims it after selection ends', async()=>{
 const range=(a:number,b:number)=>Array.from({length:b-a+1},(_,i)=>entry(`e:${a+i}`,a+i));
 const calls=fixture((url,body)=>{
  if(url.endsWith('/context'))return context(body);
  if(url.endsWith('/history')){const edge=Number(body.cursor.entry_sequence),start=Math.max(1,edge-100);return{history_page:{entries:range(start,edge-1),has_more:start>1,older_history_cursor:start>1?{...cursor,cut_sequence:'1000',entry_sequence:String(start)}:undefined}};}
  throw Error(url);
 });
 const reader=new CanonicalHistoryView('session:test',{path:'/missing',outcome:'MISSING',reason:null},snapshot(range(901,1000),{...cursor,cut_sequence:'1000',entry_sequence:'901'},'1000'));
 await reader.initialize();await flush();
 for(let i=0;i<9;i++){await reader.readHistory();await flush();}
 reader.retainHistory({visibleEntryIds:['e:550'],pinnedEntryIds:['e:100','e:900']});
 expect(reader.current().messages.map(m=>m.id)).toEqual(range(1,1000).map(e=>e.entry_id));
 reader.retainHistory({visibleEntryIds:['e:550'],pinnedEntryIds:[]});
 const p=reader.current();
 expect(p.messages.map(m=>m.id)).toEqual(range(401,700).map(e=>e.entry_id));
 expect(p.history!.windows).toHaveLength(1);
 expect(p.history!.atLatest).toBe(false);
 expect(calls.filter(c=>c.url.endsWith('/history'))).toHaveLength(9);
});

it('does not let late locate, failed annotation, or stale residence replace a successful navigation', async()=>{
 const late=deferred<unknown>();let lateBody:any;
 fixture((url,body)=>{
  if(url.endsWith('/connections'))return payload([entry('tail',1000)]);
  if(url.endsWith('/window'))return{session_id:'session:test',cut_sequence:'1000',event_sequence_cut:'2000',entries:[entry(body.anchor_entry_id,body.anchor_entry_id==='bad'?100:50,body.anchor_entry_id==='bad'?{entry_kind:'USER_MESSAGE'}:{})]};
  if(url.endsWith('/context')){if(body.entry_ids.includes('old')){lateBody=body;return late.promise;}return context(body,{cut_sequence:'1000',event_sequence_cut:'2000'});}
  throw Error(url);
 });
 const reader=await connect();await flush();
 const first=reader.locateEntry!('old',new AbortController().signal);
 const rejection=expect(first).rejects.toMatchObject({name:'AbortError'});
 await flush();
 expect(reader.current().messages.map(m=>m.id)).toEqual(['tail']);
 await reader.readLatestHistory!();
 late.resolve(context(lateBody,{cut_sequence:'1000',event_sequence_cut:'2000'}));
 await rejection;
 expect(reader.current().messages.map(m=>m.id)).toEqual(['tail']);
 await expect(reader.locateAnnotationSource('bad',new AbortController().signal)).rejects.toMatchObject({code:'ANNOTATION_SOURCE_MISSING'});
 expect(reader.current().messages.map(m=>m.id)).toEqual(['tail']);
 await reader.locateEntry!('old',new AbortController().signal);
 expect(reader.current().messages.map(m=>m.id)).toEqual(['old']);
 expect(reader.current().history!.atLatest).toBe(false);
 reader.retainHistory!({visibleEntryIds:['tail'],pinnedEntryIds:[],followLatest:true});
 expect(reader.current().messages.map(m=>m.id)).toEqual(['old']);
});

it('rejects an old DOM residence report even when navigation stays in the same connected band', async () => {
  const range = (a: number, b: number) => Array.from({length: b-a+1}, (_, i) => entry(`e:${a+i}`, a+i));
  fixture((url, body) => {
    if (url.endsWith('/context')) return context(body);
    if (url.endsWith('/history')) {
      const end = Number(body.cursor.entry_sequence), start = end - 100;
      return {history_page: {entries: range(start, end-1), has_more: start > 1,
        older_history_cursor: start > 1 ? {...cursor, cut_sequence: '500', entry_sequence: String(start)} : undefined}};
    }
    throw Error(url);
  });
  const reader = new CanonicalHistoryView('session:test', {path: '/missing', outcome: 'MISSING', reason: null},
    snapshot(range(401,500), {...cursor, cut_sequence: '500', entry_sequence: '401'}, '500'));
  await reader.initialize(); await flush();
  for (let i = 0; i < 4; i++) { await reader.readHistory(); await flush(); }
  const oldOwner = reader.current().history!.readingOwner;
  const located = await reader.locateEntry('e:350', new AbortController().signal);
  expect(located.history!.readingOwner).not.toBe(oldOwner);
  reader.retainHistory({readingOwner: oldOwner, visibleEntryIds: ['e:1'], pinnedEntryIds: [], anchorEntryId: 'e:1'});
  expect(reader.current().messages).toHaveLength(500);
  reader.retainHistory({readingOwner: located.history!.readingOwner, visibleEntryIds: ['e:350'], pinnedEntryIds: [], anchorEntryId: 'e:350'});
  expect(reader.current().messages[0].id).toBe('e:201');
  expect(reader.current().messages.at(-1)?.id).toBe('e:500');
});

it('keeps accepted input facts and task inventory current without injecting remote activities into an old reading band', async () => {
  const creator = entry('creator', 1, {entry_kind: 'ASSISTANT_TOOL_REQUEST', blocks: [
    {block_id: 'spawn', block_kind: 'TOOL_CALL', tool_call_id: 'spawn', tool_name: 'spawn_agent'},
  ]});
  const task = {id:'task:one', batchId:'batch:one', parentId:'turn:root', label:'实际子任务', role:'任务', objective:'分析', status:'running' as const, color:'blue', dependencyIds:[], completionAccepted:false};
  fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([
      entry('accepted', 999, {entry_kind: 'USER_MESSAGE', input_source: {command_id:'command:accepted', queue_item_id:'queue:accepted', delivery_mode:'NEW_TURN'}, blocks:[], content:{media_type:'application/vnd.pulsara.prompt+json',...inline(JSON.stringify({schema:'pulsara.prompt/v2',parts:[{type:'text',text:'已接受的输入'}]}))}}),
      entry('task-tail', 1000, {scope_kind:'SUBAGENT_TASK', scope_subagent_task_id:'task:one'}),
    ]);
    if (url.endsWith('/window')) return {session_id:'session:test',cut_sequence:'1000',event_sequence_cut:'2000',entries:[creator]};
    if (url.endsWith('/context')) return context(body, {batches:[{batch_id:'batch:one',parent_turn_id:'turn:root',creator_entry_id:'creator',creator_tool_call_id:'spawn',task_count:'1'}]});
    throw Error(url);
  });
  const reader = await connect(); await flush();
  const p = await reader.locateEntry!('creator', new AbortController().signal);
  expect(p.messages.map(m => m.id)).toEqual(['creator']);
  expect(p.canonicalInputs).toEqual([expect.objectContaining({id:'accepted',inputSource:expect.objectContaining({commandId:'command:accepted'}),contentState:'ready'})]);
  const merged = mergeRuntimeTaskInventory(p, [task]);
  expect(merged.messages[0].subagentRuns?.[0].activities).toEqual([]);
  expect(merged.subagentRuns?.[0].activities.map(activity => activity.id)).toEqual(['task-tail']);
  expect(merged.agentTasks).toEqual([task]);
});

it('hydrates the actual result host before committing a navigation, without reading unrelated entries', async () => {
  const text = '定位宿主正文', ref = blob(text), reasoning = '宿主思考', reasonRef = blob(reasoning);
  let fail = true;
  const calls = fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([entry('tail', 1000)]);
    if (url.endsWith('/context')) return context(body);
    if (url.endsWith('/window')) return {session_id:'session:test',cut_sequence:'1000',event_sequence_cut:'2000',entries:[
      entry('creator', 1, {entry_kind:'ASSISTANT_TOOL_REQUEST',root_final:false,content:ref,blocks:[{block_id:'call',block_kind:'TOOL_CALL',tool_call_id:'call',tool_name:'read_file'}],reasoning_blocks:[{block_id:'reason',content:reasonRef}]}),
      entry('result', 2, {entry_kind:'TOOL_RESULT',blocks:[],content:inline('结果'),tool_result:{assistant_entry_id:'creator',tool_call_id:'call',result_state:'SUCCESS'}}),
      entry('unrelated', 3, {blocks:[],content:blob('无关正文')}),
    ]};
    if (url.endsWith('/read-content')) {
      if (fail) return new Response('offline', {status:503});
      const value = body.block_id === 'reason' ? reasoning : text;
      const digest = body.block_id === 'reason' ? reasonRef.digest : ref.digest;
      return {content:{digest,complete_size:String(Buffer.byteLength(value)),offset_bytes:'0',content:Buffer.from(value).toString('base64'),complete:true}};
    }
    throw Error(url);
  });
  const reader = await connect(); await flush();
  const original = reader.current().history!.readingOwner;
  await expect(reader.locateEntry!('result', new AbortController().signal)).rejects.toThrow();
  expect(reader.current().history!.readingOwner).toBe(original);
  expect(reader.current().messages.map(m => m.id)).toEqual(['tail']);
  fail = false;
  const p = await reader.locateEntry!('result', new AbortController().signal);
  expect(p.messages.find(m => m.id === 'creator')).toMatchObject({contentState:'ready',reasoningContentState:'ready',sourceEntryIds:['creator','result']});
  expect(calls.filter(c => c.url.endsWith('/read-content')).every(c => c.body.entry_id === 'creator')).toBe(true);
});

it('uses the documented byte buffer before the entry-count buffer for large admitted entries', async () => {
  let head = 1;
  const large = (n: number) => entry(`e:${n}`, n, {blocks:[{block_id:`b:${n}`,block_kind:'TEXT',content:inline('x'.repeat(512 * 1024))}]});
  fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([large(1)]);
    if (url.endsWith('/context')) return context(body,{cut_sequence:String(head),event_sequence_cut:String(head+100)});
    if (url.endsWith('/observe')) return {observation:{through_event_sequence:String(head+100),committed:[{projection_kind:'IMMUTABLE_ENTRY',entry:large(head)}]}};
    throw Error(url);
  });
  const reader = await connect(); await flush();
  for (head = 2; head <= 12; head++) {
    await reader.observe(); await flush();
    reader.retainHistory!({visibleEntryIds:['e:1'],pinnedEntryIds:[],followLatest:false});
  }
  const p = reader.current();
  // Three base64 bodies exceed 2 MiB once metadata is counted; each buffer fits two.
  expect(p.messages.map(m => m.id)).toEqual(['e:1','e:2','e:3']);
  expect(p.history!.atLatest).toBe(false);
  const latest = await reader.readLatestHistory!();
  expect(latest.messages.map(m => m.id)).toEqual(['e:11','e:12']);
}, 15_000); // UTF-8/base64 accounting of twelve 512 KiB entries under full-suite contention.

it.each(['cancel', 'failure'])('keeps the old reading owner when return-latest ends in %s', async mode => {
  const delayed = deferred<unknown>(); let pendingBody:any; let observing = false;
  fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([entry('tail',1000)]);
    if (url.endsWith('/window')) return {session_id:'session:test',cut_sequence:'1000',event_sequence_cut:'2000',entries:[entry('old',1)]};
    if (url.endsWith('/observe')) {observing=true;return {observation:{through_event_sequence:'2001',committed:[{projection_kind:'IMMUTABLE_ENTRY',entry:entry('new-tail',1001)}]}};}
    if (url.endsWith('/context')) {
      if(observing && body.entry_ids.includes('new-tail')) {pendingBody=body;return mode==='cancel'?delayed.promise:new Response('offline',{status:503});}
      return context(body);
    }
    throw Error(url);
  });
  const reader=await connect(); await flush();
  await reader.locateEntry!('old',new AbortController().signal);
  const owner=reader.current().history!.readingOwner;
  await reader.observe(); await flush();
  const controller=new AbortController();
  const pending=reader.readLatestHistory!(controller.signal);
  const rejected=expect(pending).rejects.toThrow();
  if(mode==='cancel') {controller.abort();delayed.resolve(context(pendingBody));}
  await rejected; await flush();
  expect(reader.current().history!.readingOwner).toBe(owner);
  expect(reader.current().messages.map(m=>m.id)).toEqual(['old']);
});

it('selects a bounded process-end aperture even with 10,000 adjacent cached entries, then pages continuously', async () => {
  const range = (a: number, b: number) => Array.from({length:b-a+1}, (_,i) => entry(`e:${a+i}`, a+i,
    {root_final:a+i===10000,fork_eligible:a+i===10000}));
  const calls = fixture((url, body) => {
    if (url.endsWith('/context')) return context(body, {groups:[{owner_id:'turn:root',owner_kind:'EXECUTED_TURN',initial_entry_id:'e:1',final_entry_id:'e:10000'}]});
    if (url.endsWith('/window')) {
      const end = body.cursor ? Number(body.cursor.entry_sequence)+(body.cursor.direction==='newer'?128:-1) : 10000;
      const start = end-127;
      return {session_id:'session:test',cut_sequence:'10000',event_sequence_cut:'30',entries:range(start,end),
        older_cursor:{...cursor,cut_sequence:'10000',entry_sequence:String(start),direction:'older'},
        newer_cursor:end<10000?{...cursor,cut_sequence:'10000',entry_sequence:String(end),direction:'newer'}:undefined};
    }
    throw Error(url);
  });
  // The admission is deliberately preloaded here: the regression is selecting
  // a small view independently of already-connected cached canonical entries.
  const reader = new CanonicalHistoryView('session:test',{path:'/missing',outcome:'MISSING',reason:null},snapshot(range(1,10000),undefined,'10000'));
  await reader.initialize(); await flush();
  const oldOwner = reader.current().history!.readingOwner;
  const end = await reader.readProcessEnd('e:10000',new AbortController().signal);
  expect(end.messages.map(message=>message.id)).toEqual(range(9873,10000).map(item=>item.entry_id));
  expect(end.history!.readingOwner).not.toBe(oldOwner);
  expect(end.history!.atLatest).toBe(true);
  expect(end.messages[0]).toMatchObject({historyFinalEntryId:'e:10000',historyInitialEntryId:'e:1'});
  expect(calls.find(call=>call.url.endsWith('/window'))?.body).toEqual({anchor_entry_id:'e:10000',maximum_entries:128,maximum_serialized_bytes:2<<20});
  reader.retainHistory({readingOwner:oldOwner,visibleEntryIds:['e:1'],pinnedEntryIds:[]});
  expect(reader.current().messages).toHaveLength(128);
  await reader.readHistory(end.history!.windows[0].id,'older'); await flush();
  expect(reader.current().messages.map(message=>message.id)).toEqual(range(9745,10000).map(item=>item.entry_id));
  await reader.readHistory(undefined,'older'); await flush();
  expect(reader.current().messages.map(message=>message.id)).toEqual(range(9617,10000).map(item=>item.entry_id));
  await reader.readProcessEnd('e:10000',new AbortController().signal);
  expect(reader.current().messages).toHaveLength(128);
  expect(reader.current().history!.hasOlder).toBe(true);
  await reader.readHistory(undefined,'older'); await flush();
  expect(reader.current().messages.map(message=>message.id)).toEqual(range(9745,10000).map(item=>item.entry_id));
  expect(reader.current().history!.hasOlder).toBe(true);
  expect(calls.filter(call=>call.url.endsWith('/window') && call.body.anchor_entry_id)).toHaveLength(2);
  for(let i=0;i<6;i++){await reader.readHistory(undefined,'older');await flush();}
  reader.retainHistory({visibleEntryIds:['e:9200'],pinnedEntryIds:[],anchorEntryId:'e:9200'});
  const middle=reader.current();
  const first=Number(middle.messages[0].id.slice(2));
  const last=Number(middle.messages.at(-1)!.id.slice(2));
  expect(middle.messages.map(message=>message.id)).toEqual(range(first,last).map(item=>item.entry_id));
  expect(last).toBeLessThan(10000);
  expect(middle.history!.atLatest).toBe(false);
  expect(middle.history!.windows[0].hasNewer).toBe(true);
  await reader.readHistory(middle.history!.windows[0].id,'newer');await flush();
  expect(reader.current().messages.map(message=>message.id)).toEqual(range(first,last+128).map(item=>item.entry_id));
}, 20_000);

it.each(['cancel','superseded','context failure','not final'] as const)('keeps the old aperture when process-end navigation ends in %s', async mode => {
  const pending = deferred<unknown>(); let contextBody:any;
  const calls = fixture((url,body)=>{
    if(url.endsWith('/connections'))return payload([entry('old',1)]);
    if(url.endsWith('/window'))return{session_id:'session:test',cut_sequence:'3',event_sequence_cut:'30',entries:[entry('next',2),entry('final',3,{root_final:mode!=='not final',fork_eligible:false})]};
    if(url.endsWith('/context')){
      if(body.entry_ids.includes('final')){contextBody=body;return mode==='context failure'?new Response('offline',{status:503}):pending.promise;}
      return context(body);
    }
    throw Error(url);
  });
  const reader=await connect();await flush();
  const owner=reader.current().history!.readingOwner;
  const controller=new AbortController();
  const operation=reader.readProcessEnd!('final',controller.signal);
  const rejected=expect(operation).rejects.toThrow();
  await flush();
  expect(reader.current().messages.map(message=>message.id)).toEqual(['old']);
  if(mode==='cancel')controller.abort();
  if(mode==='superseded')await reader.locateEntry!('old',new AbortController().signal);
  if(contextBody)pending.resolve(context(contextBody));
  await rejected;await flush();
  expect(reader.current().messages.map(message=>message.id)).toEqual(['old']);
  if(mode!=='superseded')expect(reader.current().history!.readingOwner).toBe(owner);
  expect(calls.filter(call=>call.url.endsWith('/window'))).toHaveLength(1);
});

it('does not infer completion from a context final target after the page cut', async()=>{
  fixture((url,body)=>{
    if(url.endsWith('/connections'))return payload([entry('progress',1,{root_final:false,fork_eligible:false})]);
    if(url.endsWith('/context'))return context(body,{groups:[{owner_id:'turn:root',owner_kind:'EXECUTED_TURN',initial_entry_id:'start',final_entry_id:'future-final'}]});
    throw Error(url);
  });
  const reader=await connect();await flush();
  expect(reader.current().messages[0]).toMatchObject({historyFinalEntryId:'future-final',rootFinal:false,forkEligible:false});
});

it('extends an aperture with adjacent live tail but never inserts that tail into an old selected aperture',async()=>{
  let head=20;
  fixture((url,body)=>{
    if(url.endsWith('/connections'))return payload([entry('final',20)]);
    if(url.endsWith('/context'))return context(body,{cut_sequence:String(head),event_sequence_cut:String(head+100)});
    if(url.endsWith('/window'))return{session_id:'session:test',cut_sequence:'20',event_sequence_cut:'120',entries:[entry('final',20)]};
    if(url.endsWith('/observe'))return{observation:{through_event_sequence:String(head+100),committed:[{projection_kind:'IMMUTABLE_ENTRY',entry:entry(`e:${head}`,head)}]}};
    throw Error(url);
  });
  const reader=await connect();await flush();
  await reader.readProcessEnd!('final',new AbortController().signal);
  head=21;await reader.observe();await flush();
  expect(reader.current().messages.map(message=>message.id)).toEqual(['final','e:21']);
  await reader.readProcessEnd!('final',new AbortController().signal);
  expect(reader.current().history!.atLatest).toBe(false);
  head=22;await reader.observe();await flush();
  expect(reader.current().messages.map(message=>message.id)).toEqual(['final']);
});

it('reuses complete short turns across pages and compactions without narrowing a longer conversation or requesting the database', async () => {
  // frzaaaxz-shaped regression: total history exceeds a window, each turn fits.
  const range = Array.from({length:184},(_,i)=>entry(`short:${i+1}`,i+1,{turn_id:`turn:${Math.floor(i/23)}`,
    root_final:(i+1)%23===0,fork_eligible:(i+1)%23===0}));
  const calls=fixture((url,body)=>{
    if(url.endsWith('/context'))return context(body,{
      groups:Array.from({length:8},(_,i)=>({owner_kind:'EXECUTED_TURN',owner_id:`turn:${i}`,initial_entry_id:`short:${i*23+1}`,final_entry_id:`short:${(i+1)*23}`})),
      entry_contexts:body.entry_ids.map((id:string)=>{const n=Number(id.split(':')[1]), owner=`turn:${Math.floor((n-1)/23)}`;return{entry_id:id,owner_id:owner,owner_kind:'EXECUTED_TURN',root_owner_id:owner,segment_id:`epoch:${Math.floor(n/7)}`,segment_start_entry_id:`short:${Math.floor((n-1)/23)*23+1}`};}),
    });
    throw Error(`Unexpected read: ${url}`);
  });
  const reader=new CanonicalHistoryView('session:test',{path:'/missing',outcome:'MISSING',reason:null},snapshot(range,undefined,'184'));
  await reader.initialize();await flush();
  const reads=calls.length;
  for(const id of ['short:184','short:69','short:138','short:69','short:184']) {
    await reader.readProcessEnd(id,new AbortController().signal);
    expect(reader.current().history!.windows[0].entryIds).toEqual(range.map(item=>item.entry_id));
    expect(reader.current().history!.hasOlder).toBe(false);
  }
  expect(calls).toHaveLength(reads);
});

it.each(['missing start','over byte budget'] as const)('does not reuse a process with %s as a complete short turn',async mode=>{
  const entries=[entry('start',1,{root_final:false,fork_eligible:false,blocks:[{block_id:'start-text',block_kind:'TEXT',content:inline(mode==='over byte budget'?'x'.repeat(2<<20):'start')}]}),entry('final',2)];
  const calls=fixture((url,body)=>{
    if(url.endsWith('/context'))return context(body,{groups:[{owner_id:'turn:root',owner_kind:'EXECUTED_TURN',initial_entry_id:'start',final_entry_id:'final'}]});
    if(url.endsWith('/window'))return{session_id:'session:test',cut_sequence:'2',event_sequence_cut:'30',entries:[entry('final',2)],older_cursor:{...cursor,cut_sequence:'2',entry_sequence:'2',direction:'older'}};
    throw Error(url);
  });
  const reader=new CanonicalHistoryView('session:test',{path:'/missing',outcome:'MISSING',reason:null},snapshot(mode==='missing start'?entries.slice(1):entries,undefined,'2'));
  await reader.initialize();await flush();
  await reader.readProcessEnd('final',new AbortController().signal);
  expect(calls.filter(call=>call.url.endsWith('/window'))).toHaveLength(1);
  expect(reader.current().history!.hasOlder).toBe(true);
});

it.each(['current band','selected aperture'] as const)('keeps delayed older paging attached during cached short-turn toggles in a %s', async mode => {
  const pending = deferred<unknown>();
  const range = (first: number, last: number) => Array.from({length:last-first+1},(_,index)=>{
    const n=first+index;
    return entry(`e:${n}`,n,{turn_id:n<7?'turn:previous':'turn:short',root_final:n===6||n===8,fork_eligible:n===6||n===8});
  });
  const edge = (n: number) => ({...cursor,cut_sequence:'8',entry_sequence:String(n)});
  const olderPage = (first: number, last: number, window: boolean) => window
    ? {session_id:'session:test',cut_sequence:'8',event_sequence_cut:'30',entries:range(first,last),
      older_cursor:first>1?{...edge(first),direction:'older'}:undefined}
    : {history_page:{entries:range(first,last),has_more:first>1,older_history_cursor:first>1?edge(first):undefined}};
  const calls=fixture((url,body)=>{
    if(url.endsWith('/context'))return context(body,{
      groups:[
        {owner_kind:'EXECUTED_TURN',owner_id:'turn:previous',initial_entry_id:'e:1',final_entry_id:'e:6'},
        {owner_kind:'EXECUTED_TURN',owner_id:'turn:short',initial_entry_id:'e:7',final_entry_id:'e:8'},
      ],
      entry_contexts:body.entry_ids.map((id:string)=>{
        const previous=Number(id.slice(2))<7,owner=previous?'turn:previous':'turn:short';
        return {entry_id:id,owner_id:owner,owner_kind:'EXECUTED_TURN',root_owner_id:owner,
          segment_id:`${owner}:initial`,segment_start_entry_id:previous?'e:1':'e:7'};
      }),
    });
    if(url.endsWith('/window')&&body.anchor_entry_id)return olderPage(5,8,true);
    if(url.endsWith('/history')||url.endsWith('/window')) {
      if(body.cursor.entry_sequence==='5')return pending.promise;
      if(body.cursor.entry_sequence==='3')return olderPage(1,2,url.endsWith('/window'));
    }
    throw Error(`Unexpected read: ${url}`);
  });
  const reader=new CanonicalHistoryView('session:test',{path:'/missing',outcome:'MISSING',reason:null},
    snapshot(mode==='selected aperture'?range(8,8):range(5,8),edge(mode==='selected aperture'?8:5),'8'));
  await reader.initialize();await flush();
  if(mode==='selected aperture')await reader.readProcessEnd('e:8',new AbortController().signal);
  const owner=reader.current().history!.readingOwner;
  const reading=reader.readHistory(undefined,'older');
  await flush();
  expect(reader.current().history!.loading).toBe(true);
  const reads=calls.length;
  for(let toggle=0;toggle<2;toggle++) {
    await reader.readProcessEnd('e:8',new AbortController().signal);
    expect(reader.current().history!.readingOwner).toBe(owner);
    expect(reader.current().messages.map(message=>message.id)).toEqual(range(5,8).map(item=>item.entry_id));
  }
  expect(calls).toHaveLength(reads);
  pending.resolve(olderPage(3,4,mode==='selected aperture'));
  await reading;await flush();
  expect(reader.current().messages.map(message=>message.id)).toEqual(range(3,8).map(item=>item.entry_id));
  expect(reader.current().history!.readingOwner).toBe(owner);
  expect(reader.current().history!.hasOlder).toBe(true);
  await reader.readHistory(undefined,'older');await flush();
  expect(reader.current().messages.map(message=>message.id)).toEqual(range(1,8).map(item=>item.entry_id));
  expect(reader.current().history!.hasOlder).toBe(false);
  expect(calls.filter(call=>call.body.cursor).map(call=>call.body.cursor.entry_sequence)).toEqual(['5','3']);
});
