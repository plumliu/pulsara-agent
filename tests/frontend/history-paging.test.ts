import {createHash, webcrypto} from 'node:crypto';
import {afterEach, expect, it, vi} from 'vitest';
import {CanonicalHistoryView, LocalHttpRuntimeAdapter, type RuntimeConnection} from '../../frontend/lib/runtime-adapter';
const inline=(text:string)=>({inline_content:Buffer.from(text).toString('base64')});
const blob=(text:string)=>({digest:`sha256:${createHash('sha256').update(text).digest('hex')}`,size:String(Buffer.byteLength(text)),media_type:'text/plain',codec:'utf-8'});
const entry=(id:string,n:number,extra:object={})=>({entry_id:id,entry_sequence:String(n),turn_id:'turn:root',entry_kind:'ASSISTANT_MESSAGE',scope_kind:'ROOT',root_final:true,fork_eligible:true,blocks:[{block_id:`block:${id}`,block_kind:'TEXT',content:inline(id)}],...extra});
const cursor={session_id:'session:test',cut_sequence:'20',event_sequence_cut:'30',entry_sequence:'20'};
const snapshot=(entries:object[],older?:object,cut='20',eventCut='30')=>({session_id:'session:test',entry_sequence_cut:cut,event_sequence_cut:eventCut,writer_generation:'1',entries,older_history_cursor:older,control:{}});
const payload=(entries:object[],older?:object)=>({connection_id:'connection:test',connection_generation:1,session_id:'session:test',role:'controller',snapshot:{snapshot:snapshot(entries,older)},live_hello:{live_owner_epoch:'1'},live_control_snapshot:{snapshot:{}}});
const response=(v:unknown)=>new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}});
function deferred<T>(){let resolve!:(v:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return{promise,resolve};}
function context(body:any,extra:object={}){return{session_id:'session:test',cut_sequence:body.cut_sequence??'20',event_sequence_cut:body.event_sequence_cut??'30',entry_ids:body.entry_ids,groups:[{owner_id:'turn:root',owner_kind:'EXECUTED_TURN',initial_entry_id:'start'}],entry_contexts:body.entry_ids.map((id:string)=>({entry_id:id,owner_id:'turn:root',owner_kind:'EXECUTED_TURN',segment_id:'segment:root',segment_start_entry_id:'start'})),tasks:[],batches:[],interruption_notices:[],compactions:[],...extra};}
function fixture(route:(url:string,body:any)=>unknown|Promise<unknown>){const calls:Array<{url:string;body:any}>=[];vi.stubGlobal('crypto',webcrypto);vi.stubGlobal('fetch',vi.fn(async(input:string,init:RequestInit={})=>{const url=String(input),body=JSON.parse(String(init.body??'{}'));calls.push({url,body});const v=await route(url,body);return v instanceof Response?v:response(v);}));return calls;}
const connect=async()=>await new LocalHttpRuntimeAdapter().connect('session:test') as RuntimeConnection;
const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
afterEach(()=>vi.unstubAllGlobals());

it('retires read pages around the viewport and rereads either gap with the original paired cut', async () => {
 const pair={session_id:'session:test',cut_sequence:'1000',event_sequence_cut:'300'};
 const range=(first:number,last:number)=>Array.from({length:last-first+1},(_,i)=>entry(`e:${first+i}`,first+i));
 const calls=fixture((url,body)=>{
  if(url.endsWith('/context'))return context(body);
  if(url.endsWith('/history')){const edge=Number(body.cursor.entry_sequence),first=Math.max(1,edge-100);return{history_page:{entries:range(first,edge-1),has_more:first>1,older_history_cursor:first>1?{...pair,entry_sequence:String(first)}:undefined}};}
  if(url.endsWith('/window')){const edge=Number(body.cursor.entry_sequence);return{...pair,entries:body.cursor.direction==='newer'?range(edge+1,Math.min(edge+128,1000)):range(Math.max(1,edge-100),edge-1),older_cursor:body.cursor.direction==='older'&&edge>101?{...pair,entry_sequence:String(edge-100),direction:'older'}:undefined};}
  throw Error(url);
 });
 const reader=new CanonicalHistoryView('session:test',{path:'/missing',outcome:'MISSING',reason:null},snapshot(range(901,1000),{...pair,entry_sequence:'901'},'1000','300'));
 await reader.initialize();await flush();
 for(let i=0;i<9;i++){await reader.readHistory();await flush();}
 expect(reader.current().messages).toHaveLength(1000);
 let p=reader.retainHistory({visibleEntryIds:['e:550'],pinnedEntryIds:[]})!;
 expect(p.messages).toHaveLength(300);
 expect(p.messages.map(m=>m.id)).not.toContain('e:1000');
 expect(p.history!.windows).toHaveLength(1);
 expect(p.history!.atLatest).toBe(false);
 expect(p.messages.map(m=>m.id)).not.toContain('e:1');
 const middle=p.history!.windows.find(w=>w.entryIds.includes('e:550'))!;
 expect(middle).toMatchObject({hasOlder:true,hasNewer:true});
 await reader.readHistory(middle.id,'older');await flush();
 expect(calls.at(-2)?.body.cursor ?? calls.at(-1)?.body.cursor).toBeDefined();
 const older=calls.filter(c=>c.url.endsWith('/window')).at(-1)!;
 expect(older.body.cursor).toEqual({...pair,entry_sequence:'401',direction:'older'});
 p=await reader.readHistory(reader.current().history!.windows.find(w=>w.entryIds.includes('e:550'))!.id,'newer');await flush();
 expect(calls.filter(c=>c.url.endsWith('/window')).at(-1)!.body.cursor).toEqual({...pair,entry_sequence:'700',direction:'newer'});
 expect(p.history!.windows).toHaveLength(1);
 expect(p.messages.at(-1)?.id).toBe('e:1000');
});

it('unloads re-readable resident bodies but retains visible and selected bodies, then coalesces exact rereads', async () => {
 const text='可回读正文',content=blob(text);const full={...content,...inline(text)};
 const calls=fixture((url,body)=>{if(url.endsWith('/context'))return context(body);if(url.endsWith('/read-content'))return{content:{digest:content.digest,complete_size:content.size,offset_bytes:'0',content:Buffer.from(text).toString('base64'),complete:true}};throw Error(url);});
 const reader=new CanonicalHistoryView('session:test',{path:'/missing',outcome:'MISSING',reason:null},snapshot([1,2,3].map(n=>entry(`e:${n}`,n,{content:n===1?{...full}:{...content},blocks:[]}))));
 await reader.initialize();await flush();
 await reader.hydrateEntries(['e:2']);
 reader.retainHistory({visibleEntryIds:['e:1'],pinnedEntryIds:['e:2']});
 let p=reader.current();
 expect(p.messages.map(m=>m.contentState)).toEqual(['ready','ready','unloaded']);
 expect(await reader.readCanonicalEntryContent('e:1',content.digest,Number(content.size))).toBe(text);
 expect(calls.filter(c=>c.url.endsWith('/read-content'))).toHaveLength(1);
 await Promise.all([reader.hydrateEntries(['e:3']),reader.hydrateEntries(['e:3'])]);
 expect(calls.filter(c=>c.url.endsWith('/read-content'))).toHaveLength(2);
 expect(await reader.readCanonicalEntryContent('e:3',content.digest,Number(content.size))).toBe(text);
 expect(calls.filter(c=>c.url.endsWith('/read-content'))).toHaveLength(2);
 p=reader.retainHistory({visibleEntryIds:['e:1'],pinnedEntryIds:[]})!;
 expect(p.messages.find(m=>m.id==='e:2')?.contentState).toBe('unloaded');
 expect(p.messages.find(m=>m.id==='e:1')?.contentState).toBe('ready');
});

it.each(['late','failure'])('uses a complete server tail pair after %s context and multiple retired live pages', async mode => {
 let head=20;
 const range=(first:number,last:number)=>Array.from({length:last-first+1},(_,i)=>entry(`e:${first+i}`,first+i));
 const late=deferred<unknown>();let lateBody:any;
 const forward=deferred<unknown>();let forwardBody:any;
 const calls=fixture((url,body)=>{
  if(url.endsWith('/connections'))return payload([entry('e:20',20)]);
  if(url.endsWith('/context')){if(body.entry_ids.includes('e:600')&&!lateBody){lateBody=body;return mode==='late'?late.promise:new Response('failed',{status:503});}return context(body,{cut_sequence:String(head),event_sequence_cut:String(head+1000)});}
  if(url.endsWith('/observe'))return{observation:{through_event_sequence:String(head+1000),committed:range(head===100?21:head-99,head).map(value=>({projection_kind:'IMMUTABLE_ENTRY',entry:value}))}};
  if(url.endsWith('/window')){if(!forwardBody){forwardBody=body;return forward.promise;}return{session_id:'session:test',cut_sequence:'600',event_sequence_cut:'1600',entries:range(329,456)};}
  throw Error(url);
 });
 const reader=await connect();await flush();
 for(head of [100,200,300,400,500,600]){await reader.observe();await flush();}
 const interest={visibleEntryIds:['e:20'],pinnedEntryIds:[]};
 reader.retainHistory!(interest);
 expect(reader.current().messages).toHaveLength(581); // Tail context still owns the bridge.
 if(mode==='late')late.resolve(context(lateBody,{cut_sequence:'600',event_sequence_cut:'1600'}));
 else await reader.readHistory!(reader.current().history!.windows[0].id,'newer');
 await flush();
 let p=reader.retainHistory!(interest)!;
 expect(p.messages.map(m=>m.id)).not.toContain('e:300');
 expect(p.history!.windows).toHaveLength(1);
 const old=p.history!.windows[0];
 const first=reader.readHistory!(old.id,'newer');await flush();
 reader.retainHistory!(interest); // Stable cursor identity must accept the pending read.
 const edge=Number(forwardBody.cursor.entry_sequence),end=Math.min(edge+128,600);
 forward.resolve({session_id:'session:test',cut_sequence:'600',event_sequence_cut:'1600',entries:range(edge+1,end),newer_cursor:end<600?{session_id:'session:test',cut_sequence:'600',event_sequence_cut:'1600',entry_sequence:String(end),direction:'newer'}:undefined});
 await first;await flush();
 expect(calls.filter(c=>c.url.endsWith('/window'))[0].body.cursor).toEqual({session_id:'session:test',cut_sequence:'600',event_sequence_cut:'1600',entry_sequence:'200',direction:'newer'});
 p=reader.current();
 expect(p.history!.atLatest).toBe(false);
 expect(p.messages.at(-1)?.id).toBe('e:328');
 await reader.readHistory!(p.history!.windows[0].id,'newer'); await flush();
 p=reader.current();
 expect(p.messages.some(m=>m.id==='e:149')).toBe(true);
 expect(p.history!.windows).toHaveLength(1);
 expect(p.eventSequence).toBe(1600);
});

it('releases repeated offscreen accepted live and queue handoffs without dropping uncommitted drafts',async()=>{
 let n=20;
 const calls=fixture((url,body)=>{
  if(url.endsWith('/connections'))return payload([entry('e:20',20)]);
  if(url.endsWith('/context'))return context(body,{cut_sequence:String(n),event_sequence_cut:String(n+100)});
  if(url.endsWith('/observe'))return{observation:{through_event_sequence:String(n+100),committed:[{projection_kind:'IMMUTABLE_ENTRY',entry:entry(`e:${n}`,n)}]}};
  throw Error(url);
 });
 const reader=await connect();await flush();
 // These are actual process-local handoff slots, seeded after acceptance to
 // isolate retention from the separately covered live-frame parser.
 const owner=reader as unknown as {drafts:Map<string,any>;liveResults:Map<string,any>;knownQueuedContent:Map<string,any>;entries:Map<string,any>};
 owner.drafts.set('uncommitted',{id:'uncommitted',turnId:'turn:root',scopeKind:'ROOT',taskId:'',body:'still running',reasoning:[],traces:[]});
 for(n=21;n<=300;n++){
  await reader.observe();await flush();
  const id=`e:${n}`,ref=blob(`reply:${n}`),canonical=owner.entries.get(id);
  canonical.blocks=[{block_id:`block:${id}`,block_kind:'TEXT',content:ref}];
  canonical.input_source={command_id:`command:${n}`,queue_item_id:`queue:${n}`};
  owner.knownQueuedContent.set(`queue:${n}`,{parts:[]});
  owner.drafts.set(`d:${n}`,{id:`d:${n}`,committedEntryId:id,turnId:'turn:root',scopeKind:'ROOT',taskId:'',body:'accepted live bytes',reasoning:[],traces:[]});
  owner.liveResults.set(`r:${n}`,{id:`r:${n}`,committedEntryId:id,turnId:'turn:root',scopeKind:'ROOT',taskId:'',text:'accepted result',hasText:true,ended:true});
  reader.retainHistory!({visibleEntryIds:['e:20'],pinnedEntryIds:[]});
 }
 expect(owner.drafts.size).toBe(1);expect(owner.drafts.has('uncommitted')).toBe(true);
 expect(owner.liveResults.size).toBe(0);expect(owner.knownQueuedContent.size).toBe(0);
 expect(reader.current().messages.filter(m=>!m.id.startsWith('live:')).length).toBeLessThanOrEqual(129);
 expect(calls.filter(c=>c.url.endsWith('/read-content'))).toHaveLength(0);
});

it('keeps a refreshed snapshot tail while the observation cursor and a late context replay older acceptances',async()=>{
 let end=40;const late=deferred<unknown>();let lateBody:any;
 const range=(first:number,last:number)=>Array.from({length:last-first+1},(_,i)=>entry(`e:${first+i}`,first+i));
 const calls=fixture((url,body)=>{
  if(url.endsWith('/connections'))return payload([entry('e:20',20)]);
  if(url.endsWith('/history-snapshot'))return{snapshot:{snapshot:snapshot(range(190,200),undefined,'200','400')}};
  if(url.endsWith('/context')){if(body.entry_ids.includes('e:40')){lateBody=body;return late.promise;}return context(body,{cut_sequence:'200',event_sequence_cut:'400'});}
  if(url.endsWith('/observe'))return{observation:{through_event_sequence:String(end+100),committed:range(end-19,end).map(value=>({projection_kind:'IMMUTABLE_ENTRY',entry:value}))}};
  throw Error(url);
 });
 const reader=await connect();await flush();reader.retainHistory!({visibleEntryIds:['e:20'],pinnedEntryIds:[],followLatest:false});await reader.snapshot();await flush();
 for(end of [40,60,80]){await reader.observe();await flush();}
 late.resolve(context(lateBody,{cut_sequence:'200',event_sequence_cut:'400'}));await flush();
 reader.retainHistory!({visibleEntryIds:['e:20'],pinnedEntryIds:[]});
 const p=reader.current();
 expect(p.messages.map(m=>m.id)).toEqual(range(20,80).map(e=>e.entry_id));
 expect(p.history!.atLatest).toBe(false);
 expect((await reader.readLatestHistory!()).messages.at(-1)?.id).toBe('e:200');
 expect(p.eventSequence).toBe(180);
 expect(calls.filter(c=>c.url.endsWith('/observe')).map(c=>c.body.after_event_sequence)).toEqual(['30','140','160']);
});

it('opens only tail and observes while context and exact body are blocked',async()=>{
 const ctx=deferred<unknown>(),bytes=deferred<unknown>(),text='较长正文🙂',content=blob(text);
 const calls=fixture((url,body)=>{if(url.endsWith('/connections'))return payload([entry('tail',20,{blocks:[{block_id:'block:tail',block_kind:'TEXT',content}]})],cursor);if(url.endsWith('/context'))return ctx.promise;if(url.endsWith('/read-content'))return bytes.promise;if(url.endsWith('/observe'))return{observation:{through_event_sequence:'31',committed:[{projection_kind:'CURRENT_CONTROL',current_control:{active_turns:[{turn_id:'turn:new',status:'RUNNING'}]},control_read_event_sequence:'35'}]}};throw Error(url);});
 const reader=await connect();expect(reader.current().history!.initialReady).toBe(false);expect(reader.current().messages[0]).toMatchObject({id:'tail',contentState:'unloaded',historyContextState:'loading'});expect(calls.filter(c=>c.url.endsWith('/history')||c.url.endsWith('/read-content'))).toHaveLength(0);
 const hydration=reader.hydrateEntries!(['tail']);expect((await reader.observe()).activeTurnId).toBe('turn:new');expect(reader.current().messages[0].contentState).toBe('loading');
 bytes.resolve({content:{digest:content.digest,complete_size:content.size,offset_bytes:'0',content:Buffer.from(text).toString('base64'),complete:true}});await hydration;ctx.resolve(context(calls.find(c=>c.url.endsWith('/context'))!.body));await flush();expect(reader.current().history!.initialReady).toBe(true);expect(reader.current().messages[0]).toMatchObject({body:text,contentState:'ready',historyGroupId:'segment:root'});expect(calls.every(c=>!c.url.endsWith('/command'))).toBe(true);
});
it.each(['0','9007199254740993'])('reads just one older page with exact event cut %s',async cut=>{
 const c={...cursor,event_sequence_cut:cut};const calls=fixture((url,body)=>{if(url.endsWith('/connections'))return payload([entry('tail',20)],c);if(url.endsWith('/context'))return context(body);if(url.endsWith('/history'))return{history_page:{entries:[entry('old',19)],has_more:true,older_history_cursor:{...c,entry_sequence:'19'}}};throw Error(url);});
 const reader=await connect();await flush();await reader.readHistory!();const reads=calls.filter(c=>c.url.endsWith('/history'));expect(reads).toHaveLength(1);expect(reads[0].body.cursor).toEqual(c);expect(reader.current().messages.map(m=>m.id)).toEqual(['old','tail']);expect(reader.current().eventSequence).toBe(30);expect(reader.current().history!.hasOlder).toBe(true);
});
it('locates a bounded anchor directly and keeps an unloaded gap until forward pages join',async()=>{
 const calls=fixture((url,body)=>{if(url.endsWith('/connections'))return payload([entry('tail',20)]);if(url.endsWith('/context'))return context(body);if(url.endsWith('/window')&&body.anchor_entry_id)return{session_id:'session:test',cut_sequence:'20',event_sequence_cut:'30',entries:[entry('old',2)],newer_cursor:{...cursor,entry_sequence:'2',direction:'newer'}};if(url.endsWith('/window'))return{session_id:'session:test',cut_sequence:'20',event_sequence_cut:'30',entries:Array.from({length:18},(_,i)=>entry(`fill:${i}`,i+3))};throw Error(url);});
 const reader=await connect();let p=await reader.locateEntry!('old',new AbortController().signal);expect(p.history!.windows).toHaveLength(1);expect(p.messages.map(m=>m.id)).toEqual(['old']);expect(p.history!.atLatest).toBe(false);expect(calls.filter(c=>c.url.endsWith('/history'))).toHaveLength(0);p=await reader.readHistory!(p.history!.windows[0].id,'newer');expect(p.history!.windows).toHaveLength(1);expect(p.messages.find(m=>m.id==='tail')?.body).toBe('tail');
});
it('keeps result semantic identity when an older assistant becomes its render host',async()=>{
 const result=entry('result',20,{entry_kind:'TOOL_RESULT',blocks:[],content:inline('完成'),tool_result:{assistant_entry_id:'creator',tool_call_id:'call:1',result_state:'SUCCESS'}});
 fixture((url,body)=>{if(url.endsWith('/connections'))return payload([result],cursor);if(url.endsWith('/context'))return context(body);if(url.endsWith('/history'))return{history_page:{entries:[entry('creator',19,{entry_kind:'ASSISTANT_TOOL_REQUEST',blocks:[{block_id:'call:1',block_kind:'TOOL_CALL',tool_call_id:'call:1',tool_name:'read_file'}]})],has_more:false}};throw Error(url);});
 const reader=await connect();await flush();expect(reader.current().messages[0].id).toBe('result');const p=await reader.readHistory!();expect(p.messages).toHaveLength(1);expect(p.messages[0]).toMatchObject({id:'creator',sourceEntryIds:['creator','result'],traces:[{resultEntryId:'result',resultText:'完成'}]});expect(p.history!.windows[0].lastEntryId).toBe('creator');
});
it('keeps body integrity errors local and retries in read-only history without a Host',async()=>{
 const text='来源中文',content=blob(text);let fail=true;const calls=fixture((url,body)=>{if(url.endsWith('/context'))return context(body);if(url.endsWith('/read-content'))return{content:{digest:content.digest,complete_size:content.size,offset_bytes:'0',content:Buffer.from(fail?'错误乱码':text).toString('base64'),complete:true}};throw Error(url);});
 const reader=new CanonicalHistoryView('session:test',{path:'/missing',outcome:'MISSING',reason:null},snapshot([entry('old',20,{blocks:[{block_id:'block:old',block_kind:'TEXT',content}]})]));await reader.initialize();await flush();expect((await reader.hydrateEntries(['old'])).messages[0].contentState).toBe('error');fail=false;expect((await reader.hydrateEntries(['old'])).messages[0]).toMatchObject({body:text,contentState:'ready'});expect(calls.every(c=>c.url.includes('/history/'))).toBe(true);
});
it('serializes snapshot installation after observe while keeping control read head separate',async()=>{
 const delta=deferred<unknown>();const calls=fixture((url,body)=>{if(url.endsWith('/connections'))return payload([entry('tail',20)]);if(url.endsWith('/context'))return context(body);if(url.endsWith('/observe'))return delta.promise;if(url.endsWith('/history-snapshot'))return{snapshot:{snapshot:{...snapshot([entry('newtail',21)],undefined,'21','32'),control:{active_turns:[{turn_id:'stale',status:'RUNNING'}]}}}};throw Error(url);});
 const reader=await connect(),pending=reader.observe();await flush();const snap=reader.snapshot();await flush();expect(calls.some(c=>c.url.endsWith('/history-snapshot'))).toBe(false);delta.resolve({observation:{through_event_sequence:'31',committed:[{projection_kind:'CURRENT_CONTROL',current_control:{active_turns:[{turn_id:'fresh',status:'RUNNING'}]},control_read_event_sequence:'35'}]}});await pending;await snap;expect(reader.current().activeTurnId).toBe('fresh');expect(reader.current().eventSequence).toBe(31);expect(reader.current().messages.map(m=>m.id)).toEqual(['tail','newtail']);
});
it('accepts a still-owned late old page across tail refresh and leaves observation cursor alone',async()=>{
 const old=deferred<unknown>();const calls=fixture((url,body)=>{if(url.endsWith('/connections'))return payload([entry('tail',20)],cursor);if(url.endsWith('/context'))return context(body);if(url.endsWith('/history'))return old.promise;if(url.endsWith('/history-snapshot'))return{snapshot:{snapshot:snapshot([entry('newtail',21)],undefined,'21','32')}};throw Error(url);});
 const reader=await connect();await flush();const pending=reader.readHistory!();await flush();await reader.snapshot();old.resolve({history_page:{entries:[entry('old',19)],has_more:false}});await pending;expect(reader.current().messages.map(m=>m.id)).toEqual(['old','tail','newtail']);expect(reader.current().eventSequence).toBe(30);expect(calls.filter(c=>c.url.endsWith('/history'))).toHaveLength(1);
});
it('cancels one citation waiter while its shared exact body read and observer continue',async()=>{
 const bytes=deferred<unknown>(),text='精确引用',content=blob(text);const calls=fixture((url,body)=>{if(url.endsWith('/connections'))return payload([entry('tail',20,{blocks:[{block_id:'block:tail',block_kind:'TEXT',content}]})]);if(url.endsWith('/context'))return context(body);if(url.endsWith('/read-content'))return bytes.promise;if(url.endsWith('/observe'))return{observation:{through_event_sequence:'31'}};throw Error(url);});
 const reader=await connect();await flush();const abort=new AbortController(),first=reader.locateAnnotationSource!('tail',abort.signal),second=reader.hydrateEntries!(['tail']);await flush();abort.abort();await expect(first).rejects.toMatchObject({name:'AbortError'});expect((await reader.observe()).eventSequence).toBe(31);bytes.resolve({content:{digest:content.digest,complete_size:content.size,offset_bytes:'0',content:Buffer.from(text).toString('base64'),complete:true}});await second;expect(reader.current().messages[0].body).toBe(text);expect(calls.filter(c=>c.url.endsWith('/read-content'))).toHaveLength(1);expect(calls.some(c=>c.url.endsWith('/command'))).toBe(false);
});
