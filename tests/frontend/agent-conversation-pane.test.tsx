import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AgentConversationView } from '../../frontend/components/workbench-view';
import type { AgentConversationReader, QueuedPrompt, RuntimeProjection } from '../../frontend/lib/runtime-adapter';
import { RuntimeApiError, RuntimeGapError } from '../../frontend/lib/runtime-adapter';
import { AgentConversationPane } from '../../frontend/components/agent-conversation-pane';
import type { CanonicalPromptImagePart } from '../../frontend/lib/prompt-content';
import { agentCellApi } from '../../frontend/lib/agent-cell-api';

const reader = {
  readHistory: vi.fn(), hydrateEntries: vi.fn(), readLatestHistory: vi.fn(),
  readProcessEnd: vi.fn(), retainHistory: vi.fn(), locateEntry: vi.fn(),
  readToolArtifact: vi.fn(), readPromptImage: vi.fn(), readVisualizationContent: vi.fn(),
  readVisualizationThumbnail: vi.fn(),
} as unknown as AgentConversationReader;
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('Agent conversation reader', () => {
  it('opens and releases a scoped read-only reader', async () => {
    vi.spyOn(agentCellApi, 'agent').mockResolvedValue({agent: {agent_id:'session:agent', title:'Researcher', current_work_id:null, queued_count:0, host_session_id:null}});
    const projection = {messages:[], canonicalInputs:[], queuedPrompts:[], queuedCount:0, isRunning:false} as unknown as RuntimeProjection;
    const close = vi.fn(async () => {});
    const scoped = { ...reader, view:'history', current:() => projection, snapshot:vi.fn(async () => projection),
      subscribe:() => () => {}, readQueuePage:vi.fn(async () => ({items:[], nextCursor:null})), close } as unknown as AgentConversationReader;
    const open = vi.fn(async () => scoped);
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={{openAgentReader:open, createAgentContentReader:() => reader}} readingState={{choices:new Map()}} onNotify={() => {}} />);
    await waitFor(() => expect(open).toHaveBeenCalledWith('session:root', 'session:agent', expect.any(AbortSignal)));
    expect(await screen.findByRole('region', {name:'Researcher 会话'})).toBeTruthy();
    view.unmount();
    await waitFor(() => expect(close).toHaveBeenCalledOnce());
    vi.restoreAllMocks();
  });
  it('reads a canonical Agent tool image only on disclosure and releases its scoped URL and reader on unmount', async () => {
    vi.spyOn(agentCellApi, 'agent').mockResolvedValue({agent:{agent_id:'session:agent', title:'Researcher', current_work_id:null, queued_count:0, host_session_id:null}});
    const image: CanonicalPromptImagePart = {type:'image',source:'canonical',owner:{kind:'entry',entryId:'agent:image-result'},
      refOrdinal:0,digest:`sha256:${'a'.repeat(64)}`,encodedBytes:4,mediaType:'image/png',width:2,height:1};
    const projection = {messages:[{id:'agent:image-request',turnId:'agent:turn',role:'assistant',assistantKind:'tool-request',time:'',body:'',status:'completed',
      traces:[{id:'agent:image-trace',kind:'read',toolName:'view_image',title:'读取图片',subtitle:'已完成',status:'completed',
        resultEntryId:'agent:image-result',resultText:'Image loaded.',resultContent:{parts:[image]}}]},
      {id:'agent:answer',turnId:'agent:turn',role:'assistant',assistantKind:'terminal',time:'',body:'图片已核对。',status:'completed',rootFinal:true}],
      canonicalInputs:[],queuedPrompts:[],queuedCount:0,isRunning:false} as unknown as RuntimeProjection;
    const close = vi.fn(async () => {});
    const wrongReader = vi.fn();
    const scoped = {...reader,view:'history',current:() => projection,snapshot:async () => projection,subscribe:() => () => {},
      readPromptImage:wrongReader,readQueuePage:async () => ({items:[],nextCursor:null}),close} as unknown as AgentConversationReader;
    let resolve!: (value: Uint8Array) => void;
    const bytes = new Promise<Uint8Array>(done => {resolve = done;});
    const readImage = vi.fn(() => bytes);
    const content = {...reader,readPromptImage:readImage};
    const open = vi.fn(async () => scoped);
    const create = vi.fn(() => content);
    const createUrl = vi.spyOn(URL,'createObjectURL');
    const revokeUrl = vi.spyOn(URL,'revokeObjectURL');
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={{openAgentReader:open,createAgentContentReader:create}} readingState={{choices:new Map()}} onNotify={() => {}} />);
    await screen.findByText('图片已核对。');
    expect(open).toHaveBeenCalledExactlyOnceWith('session:root','session:agent',expect.any(AbortSignal));
    expect(create).toHaveBeenCalledExactlyOnceWith('session:root','session:agent');
    expect(readImage).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole('button',{name:'展开处理过程'})));
    expect(readImage).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole('button',{name:'展开工具详情：view_image'}));
    await waitFor(() => expect(readImage).toHaveBeenCalledExactlyOnceWith(image));
    expect(wrongReader).not.toHaveBeenCalled();
    expect(screen.getByText('正在读取图片…')).toBeTruthy();
    await act(async () => {resolve(new Uint8Array([137,80,78,71]));await bytes;});
    const preview = screen.getByRole('button',{name:'放大图片'});
    const url = preview.querySelector('img')?.getAttribute('src');
    expect(url).toMatch(/^blob:/);
    expect(createUrl).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({type:'image/png',size:4}));
    view.unmount();
    expect(revokeUrl).toHaveBeenCalledExactlyOnceWith(url);
    expect(close).toHaveBeenCalledOnce();
  });
  it('removes the old live draft before a replacement Host opens, even at the same epoch', async () => {
    vi.useFakeTimers();
    let host = 'host:first';
    vi.spyOn(agentCellApi, 'agent').mockImplementation(async () => ({agent: {agent_id:'session:agent', title:'Researcher', current_work_id:null, queued_count:0, host_session_id:host}}));
    const projection = (body: string) => ({messages:[{id:'live:one', role:'assistant', assistantKind:'live', status:'running', time:'', body, turnId:'turn:one'}], canonicalInputs:[], queuedPrompts:[], queuedCount:0, isRunning:true, activeTurnId:'turn:one', liveOwnerEpoch:1}) as unknown as RuntimeProjection;
    const makeReader = (body: string) => ({...reader, view:'live', current:() => projection(body),
      subscribe:() => () => {}, observe:() => new Promise(() => {}),
      readQueuePage:async () => ({items:[],nextCursor:null}), close:vi.fn(async () => {})}) as unknown as AgentConversationReader;
    const old = makeReader('Old Host draft');
    const next = makeReader('New Host draft');
    let finish!: (value: AgentConversationReader) => void;
    const delayed = new Promise<AgentConversationReader>(resolve => { finish = resolve; });
    const open = vi.fn().mockResolvedValueOnce(old).mockReturnValueOnce(delayed);
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={{openAgentReader:open, createAgentContentReader:() => reader}} readingState={{choices:new Map()}} onNotify={() => {}} />);
    await act(async () => {});
    expect(screen.getByText('Old Host draft')).toBeTruthy();
    host = 'host:second';
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(screen.queryByText('Old Host draft')).toBeNull();
    await act(async () => { finish(next); await delayed; });
    expect(screen.getByText('New Host draft')).toBeTruthy();
    expect(old.close).toHaveBeenCalledOnce();
    view.unmount();
  });
  it.each([
    new RuntimeGapError('COMMITTED_GAP', 'gap'),
    new RuntimeGapError('LIVE_CONTROL_GAP', 'gap'),
    new RuntimeApiError('PROTOCOL_TRANSPORT_CLOSED', 'closed', true),
    new RuntimeApiError('LOCAL_RESOURCE_NOT_FOUND', 'missing connection', false),
  ])('reopens the read-only reader after an invalid observation: %s', async failure => {
    vi.spyOn(agentCellApi, 'agent').mockResolvedValue({agent:{agent_id:'session:agent', title:'Researcher', current_work_id:null, queued_count:0, host_session_id:'host:same'}});
    const projection = (body: string) => ({messages:[{id:'live:one',role:'assistant',assistantKind:'live',status:'running',time:'',body,turnId:'turn:one'}],canonicalInputs:[],queuedPrompts:[],queuedCount:0,isRunning:true,activeTurnId:'turn:one',liveOwnerEpoch:1}) as unknown as RuntimeProjection;
    let fail!: (cause: unknown) => void;
    const observation = new Promise<RuntimeProjection>((_resolve, reject) => { fail = reject; });
    const make = (body: string, observe: () => Promise<RuntimeProjection>) => ({...reader,view:'live',current:() => projection(body),subscribe:() => () => {},observe,
      readQueuePage:async () => ({items:[],nextCursor:null}),close:vi.fn(async () => {})}) as unknown as AgentConversationReader;
    const old = make('Old draft', () => observation);
    const next = make('Fresh draft', () => new Promise(() => {}));
    let finish!: (value: AgentConversationReader) => void;
    const reopened = new Promise<AgentConversationReader>(resolve => { finish = resolve; });
    const open = vi.fn().mockResolvedValueOnce(old).mockReturnValueOnce(reopened);
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={{openAgentReader:open,createAgentContentReader:() => reader}} readingState={{choices:new Map()}} onNotify={() => {}} />);
    expect(await screen.findByText('Old draft')).toBeTruthy();
    await act(async () => { fail(failure); });
    expect(screen.queryByText('Old draft')).toBeNull();
    await waitFor(() => expect(open).toHaveBeenCalledTimes(2));
    await act(async () => { finish(next); await reopened; });
    expect(screen.getByText('Fresh draft')).toBeTruthy();
    expect(old.close).toHaveBeenCalledOnce();
    view.unmount();
  });
  it('keeps the current content and retries the same reader after a temporary transport failure', async () => {
    vi.useFakeTimers();
    vi.spyOn(agentCellApi, 'agent').mockResolvedValue({agent:{agent_id:'session:agent', title:'Researcher', current_work_id:null, queued_count:0, host_session_id:'host:same'}});
    const projection = {messages:[{id:'live:one',role:'assistant',assistantKind:'live',status:'running',time:'',body:'Retained draft',turnId:'turn:one'}],canonicalInputs:[],queuedPrompts:[],queuedCount:0,isRunning:true,activeTurnId:'turn:one'} as unknown as RuntimeProjection;
    let fail!: (cause: unknown) => void;
    const first = new Promise<RuntimeProjection>((_resolve, reject) => { fail = reject; });
    const observe = vi.fn().mockReturnValueOnce(first).mockImplementation(() => new Promise(() => {}));
    const scoped = {...reader,view:'live',current:() => projection,subscribe:() => () => {},observe,
      readQueuePage:async () => ({items:[],nextCursor:null}),close:vi.fn(async () => {})} as unknown as AgentConversationReader;
    const open = vi.fn(async () => scoped);
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={{openAgentReader:open,createAgentContentReader:() => reader}} readingState={{choices:new Map()}} onNotify={() => {}} />);
    await act(async () => {});
    expect(screen.getByText('Retained draft')).toBeTruthy();
    await act(async () => { fail(new RuntimeApiError('LOCAL_TRANSPORT_UNAVAILABLE', 'offline', true)); });
    expect(screen.getByText('Retained draft')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(observe).toHaveBeenCalledTimes(2);
    expect(open).toHaveBeenCalledTimes(1);
    view.unmount();
  });
  it('finishes slow queue scans between successive observations without starving the queue', async () => {
    vi.spyOn(agentCellApi, 'agent').mockResolvedValue({agent:{agent_id:'session:agent', title:'Researcher', current_work_id:null, queued_count:1, host_session_id:'host:same'}});
    const projection = {messages:[],canonicalInputs:[],queuedPrompts:[],queuedCount:1,isRunning:true} as unknown as RuntimeProjection;
    const item = (text: string): QueuedPrompt => ({queueItemId:'queue:one',sequence:'1',status:'pending',sourceKind:'agent',inputOrigin:'AGENT_INSTRUCTION',deliveryMode:'new-turn',content:{parts:[{type:'text',text}]}});
    let finishFirst!: (value: {items:QueuedPrompt[];nextCursor:null}) => void;
    let finishSecond!: (value: {items:QueuedPrompt[];nextCursor:null}) => void;
    let finishObservation!: (value: RuntimeProjection) => void;
    const firstScan = new Promise<{items:QueuedPrompt[];nextCursor:null}>(resolve => { finishFirst = resolve; });
    const secondScan = new Promise<{items:QueuedPrompt[];nextCursor:null}>(resolve => { finishSecond = resolve; });
    const observation = new Promise<RuntimeProjection>(resolve => { finishObservation = resolve; });
    const scan = vi.fn().mockResolvedValueOnce({items:[],nextCursor:null}).mockReturnValueOnce(firstScan).mockReturnValueOnce(secondScan);
    const observe = vi.fn().mockResolvedValueOnce(projection).mockReturnValueOnce(observation).mockImplementation(() => new Promise(() => {}));
    const scoped = {...reader,view:'live',current:() => projection,subscribe:() => () => {},observe,readQueuePage:scan,close:async () => {}} as unknown as AgentConversationReader;
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={{openAgentReader:async () => scoped,createAgentContentReader:() => reader}} readingState={{choices:new Map()}} onNotify={() => {}} />);
    await waitFor(() => expect(scan).toHaveBeenCalledTimes(2));
    expect(observe).toHaveBeenCalledTimes(1);
    await act(async () => { finishFirst({items:[item('First scan')],nextCursor:null}); await firstScan; });
    expect(await screen.findByText('First scan')).toBeTruthy();
    await waitFor(() => expect(observe).toHaveBeenCalledTimes(2));
    await act(async () => { finishObservation(projection); await observation; });
    await waitFor(() => expect(scan).toHaveBeenCalledTimes(3));
    expect(observe).toHaveBeenCalledTimes(2);
    await act(async () => { finishSecond({items:[item('Second scan')],nextCursor:null}); await secondScan; });
    expect(await screen.findByText('Second scan')).toBeTruthy();
    await waitFor(() => expect(observe).toHaveBeenCalledTimes(3));
    view.unmount();
  });
  it('does not treat a queue read failure as an observation gap', async () => {
    vi.spyOn(agentCellApi, 'agent').mockResolvedValue({agent:{agent_id:'session:agent', title:'Researcher', current_work_id:null, queued_count:0, host_session_id:'host:same'}});
    const projection = {messages:[],canonicalInputs:[],queuedPrompts:[],queuedCount:0,isRunning:true} as unknown as RuntimeProjection;
    const observe = vi.fn(() => new Promise(() => {}));
    const scoped = {...reader,view:'live',current:() => projection,subscribe:() => () => {},observe,
      readQueuePage:async () => { throw new RuntimeGapError('COMMITTED_GAP', 'queue unavailable'); },close:vi.fn(async () => {})} as unknown as AgentConversationReader;
    const open = vi.fn(async () => scoped);
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={{openAgentReader:open,createAgentContentReader:() => reader}} readingState={{choices:new Map()}} onNotify={() => {}} />);
    await waitFor(() => expect(observe).toHaveBeenCalledOnce());
    expect(open).toHaveBeenCalledOnce();
    expect(scoped.close).not.toHaveBeenCalled();
    view.unmount();
  });
  it('locates a pending Work by queue identity and keeps it read-only', async () => {
    vi.spyOn(agentCellApi, 'agent').mockResolvedValue({agent:{agent_id:'session:agent', title:'Researcher', current_work_id:null, queued_count:1, host_session_id:null}});
    vi.spyOn(agentCellApi, 'work').mockResolvedValue({work:{agent_id:'session:agent', work_id:'queue:target', status:'queued'},inputs:[]});
    const item = {queueItemId:'queue:target', sequence: '10', status:'pending', sourceKind:'agent', inputOrigin:'AGENT_INSTRUCTION', deliveryMode:'new-turn', content:{parts:[{type:'text', text:'Located Work'}]}} as QueuedPrompt;
    const projection = {messages:[],canonicalInputs:[],queuedPrompts:[],queuedCount:1,isRunning:false} as unknown as RuntimeProjection;
    const scoped = {...reader, view:'history', current:() => projection, snapshot:async () => projection,
      subscribe:() => () => {}, readQueuePage:async () => ({items:[],nextCursor:null}), readQueueItem:async () => item,
      close:async () => {}} as unknown as AgentConversationReader;
    const scroll = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {configurable:true, value:scroll});
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={{openAgentReader:async () => scoped, createAgentContentReader:() => reader}} readingState={{choices:new Map()}} onNotify={() => {}}
      workLocation={{workId:'queue:target',requestId:1}} />);
    expect(await screen.findByText('Located Work')).toBeTruthy();
    await waitFor(() => expect(scroll).toHaveBeenCalled());
    expect(screen.queryByRole('button', {name:'删除'})).toBeNull();
    view.unmount();
    delete (HTMLElement.prototype as unknown as {scrollIntoView?: unknown}).scrollIntoView;
  });
  it('orders queue entries exactly beyond JavaScript safe integers', async () => {
    vi.spyOn(agentCellApi, 'agent').mockResolvedValue({agent:{agent_id:'session:agent', title:'Researcher', current_work_id:null, queued_count:2, host_session_id:null}});
    const item = (queueItemId: string, sequence: string, text: string): QueuedPrompt => ({queueItemId, sequence, status:'pending', sourceKind:'agent', inputOrigin:'AGENT_INSTRUCTION', deliveryMode:'new-turn', content:{parts:[{type:'text', text}]}});
    const projection = {messages:[],canonicalInputs:[],queuedPrompts:[],queuedCount:2,isRunning:false} as unknown as RuntimeProjection;
    const scoped = {...reader,view:'history',current:() => projection,snapshot:async () => projection,subscribe:() => () => {},
      readQueuePage:async () => ({items:[item('late','9007199254740993','Later'),item('early','9007199254740992','Earlier')],nextCursor:null}),close:async () => {}} as unknown as AgentConversationReader;
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={{openAgentReader:async () => scoped,createAgentContentReader:() => reader}} readingState={{choices:new Map()}} onNotify={() => {}} />);
    await screen.findByText('Earlier');
    expect([...view.container.querySelectorAll('.agent-conversation-queue article')].map(row => row.getAttribute('data-queue-item-id'))).toEqual(['early','late']);
    view.unmount();
  });
  it('shows a never-started terminal Work without adding it to pending inputs', async () => {
    vi.spyOn(agentCellApi, 'agent').mockResolvedValue({agent:{agent_id:'session:agent', title:'Researcher', current_work_id:null, queued_count:0, host_session_id:null}});
    vi.spyOn(agentCellApi, 'work').mockResolvedValue({work:{agent_id:'session:agent', work_id:'queue:stopped', status:'stopped', terminal_reason:'cancelled'},inputs:[]});
    const projection = {messages:[],canonicalInputs:[],queuedPrompts:[],queuedCount:0,isRunning:false} as unknown as RuntimeProjection;
    const scoped = {...reader, view:'history', current:() => projection, snapshot:async () => projection,
      subscribe:() => () => {}, readQueuePage:async () => ({items:[],nextCursor:null}), readQueueItem:async () => null,
      close:async () => {}} as unknown as AgentConversationReader;
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {configurable:true, value:vi.fn()});
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={{openAgentReader:async () => scoped, createAgentContentReader:() => reader}} readingState={{choices:new Map()}} onNotify={() => {}}
      workLocation={{workId:'queue:stopped',requestId:1}} />);
    expect(await screen.findByText('任务已结束')).toBeTruthy();
    expect(screen.queryByText('排队中')).toBeNull();
    view.unmount();
    delete (HTMLElement.prototype as unknown as {scrollIntoView?: unknown}).scrollIntoView;
  });
  it('repeats navigation to a started Work using its instruction entry', async () => {
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {configurable:true, value:vi.fn()});
    vi.spyOn(agentCellApi, 'agent').mockResolvedValue({agent:{agent_id:'session:agent', title:'Researcher', current_work_id:'queue:work', queued_count:0, host_session_id:null}});
    vi.spyOn(agentCellApi, 'work').mockResolvedValue({work:{agent_id:'session:agent', work_id:'queue:work', status:'running', consumed_entry_id:'entry:instruction', turn_id:'turn:one'},inputs:[]});
    const projection = {messages:[{id:'entry:instruction',role:'user',userKind:'prompt',inputOrigin:'AGENT_INSTRUCTION',time:'',body:'The instruction'}],canonicalInputs:[],queuedPrompts:[],queuedCount:0,isRunning:false} as unknown as RuntimeProjection;
    const locate = vi.fn(async () => projection);
    const scoped = {...reader, view:'history', current:() => projection, snapshot:async () => projection, locateEntry:locate,
      subscribe:() => () => {}, readQueuePage:async () => ({items:[],nextCursor:null}), close:async () => {}} as unknown as AgentConversationReader;
    const adapter = {openAgentReader:async () => scoped, createAgentContentReader:() => reader};
    const state = {choices:new Map<string,boolean>()};
    const notify = vi.fn();
    const view = render(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={adapter} readingState={state} onNotify={notify} workLocation={{workId:'queue:work',requestId:1}} />);
    await waitFor(() => expect(locate).toHaveBeenCalledTimes(1));
    view.rerender(<AgentConversationPane rootSessionId="session:root" agentSessionId="session:agent" agentName="Researcher"
      adapter={adapter} readingState={state} onNotify={notify} workLocation={{workId:'queue:work',requestId:2}} />);
    await waitFor(() => expect(locate).toHaveBeenCalledTimes(2));
    expect(notify).not.toHaveBeenCalled();
    view.unmount();
    delete (HTMLElement.prototype as unknown as {scrollIntoView?: unknown}).scrollIntoView;
  });
  it('does not restart frozen visualization reads when a live projection updates', async () => {
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
    vi.stubGlobal('IntersectionObserver', class {
      constructor(private callback: IntersectionObserverCallback) {}
      observe(target: Element) { this.callback([{target, isIntersecting:true, intersectionRect:{width:100,height:100}} as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
      disconnect() {}
    });
    let resolve!: (value: string) => void;
    const pending = new Promise<string>(done => { resolve = done; });
    const read = vi.fn(() => pending);
    const contentReader = {...reader, readVisualizationContent:read};
    const message = {id:'entry:answer',role:'assistant',assistantKind:'terminal',status:'completed',rootFinal:true,time:'',body:'```pulsara-viz\nchart.html\n```',visualizations:[{ordinal:0,state:'READY',contentDigest:`sha256:${'a'.repeat(64)}`,contentSize:16,sourceFilename:'chart.html'}]};
    const projection = {messages:[message],canonicalInputs:[],queuedPrompts:[],queuedCount:0,isRunning:false} as unknown as RuntimeProjection;
    const state = {choices:new Map<string,boolean>()};
    const notify = vi.fn();
    const view = render(<AgentConversationView sessionId="session:agent" agentName="Researcher" reader={reader} contentReader={contentReader}
      projection={projection} queue={[]} queueCursor={null} onMoreQueue={() => {}} readingState={state} onNotify={notify} />);
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    const signal = read.mock.calls[0][5] as AbortSignal;
    view.rerender(<AgentConversationView sessionId="session:agent" agentName="Researcher" reader={reader} contentReader={contentReader}
      projection={{...projection,messages:[{...message}] as RuntimeProjection['messages']}} queue={[]} queueCursor={null} onMoreQueue={() => {}} readingState={state} onNotify={notify} />);
    expect(read).toHaveBeenCalledTimes(1);
    expect(signal.aborted).toBe(false);
    await act(async () => { resolve('<p>saved</p>'); await pending; });
    view.unmount();
  });
  it('renders independent queued Work and the real sources without controls', () => {
    const projection = {
      messages: [
        {id:'instruction', role:'user', userKind:'prompt', inputOrigin:'AGENT_INSTRUCTION', time:'', body:'Investigate.'},
        {id:'material', role:'user', userKind:'prompt', inputOrigin:'AGENT_MATERIAL', time:'', body:'Prior context.'},
        {id:'message', role:'user', userKind:'steer', inputOrigin:'AGENT_MESSAGE', time:'', body:'Review this.',
          inputSource:{identityKind:'queue', queueItemId:'queue:steer', deliveryMode:'steer', sourceKind:'agent', sourceSessionId:'session:other'}},
      ], canonicalInputs:[], isRunning:false, queuedCount:2, queuedPrompts:[], todo:undefined,
    } as unknown as RuntimeProjection;
    const queue = [
      {queueItemId:'queue:one', sequence: '1', status:'pending', sourceKind:'agent', inputOrigin:'AGENT_INSTRUCTION', deliveryMode:'new-turn', content:{parts:[{type:'text', text:'First Work'}]}, waitingOn:['queue:dependency']},
      {queueItemId:'queue:two', sequence: '2', status:'pending', sourceKind:'agent', inputOrigin:'AGENT_INSTRUCTION', deliveryMode:'new-turn', content:{parts:[{type:'text', text:'Second Work'}]}},
    ] as QueuedPrompt[];
    const {container} = render(<AgentConversationView sessionId="session:agent" agentName="Researcher"
      reader={reader} contentReader={reader} projection={projection} queue={queue} queueCursor={null} onMoreQueue={() => {}}
      readingState={{choices:new Map()}} onNotify={() => {}} />);
    expect(screen.getByText('ROOT')).toBeTruthy();
    expect(screen.getByRole('article', {name:'任务材料'}).textContent).toContain('Prior context.');
    expect(screen.getByText('代理 session:other')).toBeTruthy();
    const cards = container.querySelectorAll('.agent-conversation-queue article');
    expect(cards).toHaveLength(2);
    expect(within(cards[0] as HTMLElement).getByText('等待依赖：queue:dependency')).toBeTruthy();
    expect(within(cards[1] as HTMLElement).getByText('Second Work')).toBeTruthy();
    expect(screen.queryByRole('button', {name:'发送'})).toBeNull();
    expect(screen.queryByRole('button', {name:'删除'})).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
  });
});
