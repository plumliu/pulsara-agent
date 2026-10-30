import { agentCellApi, AgentCellError } from '../../frontend/lib/agent-cell-api';
import { FileLinkContext } from '../../frontend/components/file-link-context';
import { useContext } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { DesktopWorkspaceProvider, useDesktopWorkspace } from '../../frontend/components/desktop-workspace';
import { FilePreviewProvider } from '../../frontend/components/file-browser';
import { PresentationContext, PresentationProvider } from '../../frontend/components/presentation-provider';
import type { RuntimeConnection } from '../../frontend/lib/runtime-adapter';
import type { PresentationRequest, PresentationTarget } from '../../frontend/lib/presentation-targets';
import type { FilePreviewApi, FilePreview } from '../../frontend/lib/file-preview';
import type { PulsaraDesktop } from '../../frontend/lib/desktop-host';

afterEach(()=>{cleanup();vi.restoreAllMocks();delete window.pulsaraDesktop;});
function deferred<T>() { let resolve!: (value:T)=>void;const promise=new Promise<T>(yes=>{resolve=yes;});return {resolve,promise}; }
function fixture() {
  let listener: ((r:PresentationRequest)=>void)|undefined;
  let hostSessionId = 'host:one';
  const connection={sessionId:'one',role:'controller',generation:1,current:()=>({hostSessionId}),subscribePresentation:(fn:(r:PresentationRequest)=>void)=>{listener=fn;return()=>{listener=undefined;};}} as unknown as RuntimeConnection;
  const file=(path:string):FilePreview=>({path:decodeURIComponent(new URL(path).pathname),name:'a.md',kind:'text',reading_root:'/project',read_token:crypto.randomUUID(),size:0,notice:null,content_url:'',document_url:null,images_url:null,can_open:false});
  const api:FilePreviewApi={open:vi.fn(async path=>file(path)),close:vi.fn(async()=>{}),page:vi.fn(),action:vi.fn()};
  const create=vi.fn(async ({tabId})=>({instanceId:crypto.randomUUID(),tabId,url:'',loading:false}));
  const command=vi.fn(async()=>{});
  window.pulsaraDesktop={browser:{create,command,close:vi.fn(async()=>{}),clear:vi.fn(async()=>{}),onEvent:()=>()=>{}}} as unknown as PulsaraDesktop;
  const show=vi.fn();
  const notify=vi.fn();
  const closeGroup=vi.fn();
  function Probe(){const w=useDesktopWorkspace()!;const manual=useContext(PresentationContext);const files=useContext(FileLinkContext);return <><button onClick={event=>files?.open("/project/user.md",event.currentTarget)}>user file</button><pre data-testid="tabs">{JSON.stringify(w.tabs.map(tab=>({id:tab.id,kind:tab.kind,entity:tab.entityId,host:tab.hostSessionId,error:!!tab.render})))}</pre><output data-testid="active">{w.activeId}</output><button onClick={()=>w.tabs[0]&&w.activate(w.tabs[0].id)}>select first</button><button onClick={()=>w.tabs[0]&&w.close(w.tabs[0].id)}>close first</button><button onClick={()=>manual?.([{type:'terminal',process_id:'old',host_session_id:'host:old'}])}>history</button><button onClick={()=>manual?.([{type:'cell',cell_id:'cell:old',root_session_id:'one',host_session_id:'host:old'},{type:'file',path:'/project/history.md'},{type:'browser',url:'https://example.test/history'}])}>cell history</button></>;}
  function Harness({visible=true,open=false,session='one'}:{visible?:boolean;open?:boolean;session?:string}){return <DesktopWorkspaceProvider sessionId={session} visible={visible} onShow={show}><FilePreviewProvider api={api} ownerKey={session} sessionId={session} onNotify={()=>{}}><PresentationProvider onNotify={notify} connection={connection} visible={visible} isOpen={open}><Probe/></PresentationProvider></FilePreviewProvider></DesktopWorkspaceProvider>;}
  const view=render(<Harness/>);
  const send=(targets:PresentationTarget[],sequence='1')=>act(()=>listener?.({sequence,signal:new AbortController().signal,read:async()=>({targets})}));
  return {api,file,create,command,show,notify,view,Harness,send,setHost:(host:string)=>{hostSessionId=host;},tabs:()=>JSON.parse(screen.getByTestId('tabs').textContent!)};
}
it('publishes mixed targets once, skips tool errors, deduplicates and selects the last accepted target',async()=>{
  const f=fixture();
  await f.send([{type:'file',path:'/project/a #?.md'},{type:'browser',url:'https://example.test'},{type:'file',path:'/project/a #?.md'},{type:'browser',url:'https://example.test/'}]);
  await waitFor(()=>expect(f.tabs()).toHaveLength(2));
  expect(f.show).toHaveBeenCalledOnce();expect(f.create).toHaveBeenCalledOnce();
  expect(f.api.open).toHaveBeenCalledWith('file:///project/a%20%23%3F.md',undefined,undefined);
  expect(f.api.close).toHaveBeenCalledOnce();expect(screen.getByTestId('active').textContent).toBe(f.tabs()[1].id);
  await f.send([], '2');expect(f.show).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByText('history'));await waitFor(()=>expect(f.tabs()).toHaveLength(3));expect(f.tabs()[2].host).toBe('host:old');
});
it('releases a late file and never creates a browser after leaving and returning to the same session',async()=>{
  const f=fixture();const pending=deferred<FilePreview>();vi.mocked(f.api.open).mockReturnValueOnce(pending.promise);
  await f.send([{type:'file',path:'/project/a.md'},{type:'browser',url:'https://example.test'}]);
  f.view.rerender(<f.Harness session="two"/>);f.view.rerender(<f.Harness/>);
  await act(async()=>pending.resolve(f.file('file:///project/a.md')));
  expect(f.tabs()).toHaveLength(0);expect(f.create).not.toHaveBeenCalled();expect(f.api.close).toHaveBeenCalledOnce();
});
it('keeps user choice and drops pending preparation when the pane is collapsed',async()=>{
  const f=fixture();await f.send([{type:'panel',name:'capabilities'}]);await waitFor(()=>expect(f.tabs()).toHaveLength(1));
  f.view.rerender(<f.Harness open/>);
  const pending=deferred<FilePreview>();vi.mocked(f.api.open).mockReturnValueOnce(pending.promise);
  await f.send([{type:'file',path:'/project/a.md'}],'2');
  f.view.rerender(<f.Harness open={false}/>);await act(async()=>pending.resolve(f.file('file:///project/a.md')));
  expect(f.tabs()).toHaveLength(1);expect(f.show).toHaveBeenCalledOnce();expect(f.api.close).toHaveBeenCalledOnce();
});
it('does not let an older slow batch steal focus, but retains its valid file tab',async()=>{
  const f=fixture();const pending=deferred<FilePreview>();vi.mocked(f.api.open).mockReturnValueOnce(pending.promise);
  await f.send([{type:'file',path:'/project/a.md'}]);await f.send([{type:'panel',name:'capabilities'}],'2');
  await waitFor(()=>expect(f.tabs()).toHaveLength(1));const selected=screen.getByTestId('active').textContent;
  await act(async()=>pending.resolve(f.file('file:///project/a.md')));
  expect(f.tabs()).toHaveLength(2);expect(screen.getByTestId('active').textContent).toBe(selected);expect(f.show).toHaveBeenCalledOnce();
});
it('deduplicates concurrent file batches at publication and preserves the selected file',async()=>{
  const f=fixture();const first=deferred<FilePreview>(),second=deferred<FilePreview>();vi.mocked(f.api.open).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  await f.send([{type:'file',path:'/project/a.md'}]);await f.send([{type:'file',path:'/project/a.md'}],'2');
  await act(async()=>second.resolve(f.file('file:///project/a.md')));const id=f.tabs()[0].id;
  await act(async()=>first.resolve(f.file('file:///project/a.md')));
  expect(f.tabs()).toHaveLength(1);expect(f.tabs()[0].id).toBe(id);expect(f.api.close).toHaveBeenCalledOnce();
});
it('shows an accepted file error without aborting other targets',async()=>{
  const f=fixture();vi.mocked(f.api.open).mockRejectedValueOnce(new Error('missing'));
  await f.send([{type:'panel',name:'capabilities'},{type:'file',path:'/project/missing'}]);
  await waitFor(()=>expect(f.tabs()).toHaveLength(2));expect(screen.getByTestId('active').textContent).toBe(f.tabs()[1].id);expect(f.show).toHaveBeenCalledOnce();
});
it('keeps valid siblings when WHATWG rejects one browser target',async()=>{
  const f=fixture();await f.send([{type:'file',path:'/project/a.md'},{type:'browser',url:'https://%'},{type:'panel',name:'capabilities'}]);
  await waitFor(()=>expect(f.tabs()).toHaveLength(3));expect(f.create).not.toHaveBeenCalled();expect(screen.getByTestId('active').textContent).toBe(f.tabs()[2].id);
});
it('releases directory tokens and still publishes an error tab with its siblings',async()=>{
  const f=fixture();vi.mocked(f.api.open).mockResolvedValueOnce({...f.file('file:///project/docs'),kind:'directory'});
  await f.send([{type:'file',path:'/project/docs'},{type:'panel',name:'capabilities'}]);
  await waitFor(()=>expect(f.tabs()).toHaveLength(2));expect(f.api.close).toHaveBeenCalledOnce();
});

it('a user file click supersedes a slow automatic batch before it can regain focus',async()=>{
  const f=fixture();const pending=deferred<FilePreview>();vi.mocked(f.api.open).mockReturnValueOnce(pending.promise);
  await f.send([{type:'file',path:'/project/automatic.md'}]);
  fireEvent.click(screen.getByText('user file'));await waitFor(()=>expect(f.tabs()).toHaveLength(1));
  const selected=screen.getByTestId('active').textContent;
  await act(async()=>pending.resolve(f.file('file:///project/automatic.md')));
  expect(f.tabs()).toHaveLength(1);expect(screen.getByTestId('active').textContent).toBe(selected);expect(f.api.close).toHaveBeenCalledOnce();
});
it('a late ordinary file stays in the background after the user collapses the pane',async()=>{
  const f=fixture();f.view.rerender(<f.Harness open/>);
  const pending=deferred<FilePreview>();vi.mocked(f.api.open).mockReturnValueOnce(pending.promise);
  fireEvent.click(screen.getByText('user file'));f.view.rerender(<f.Harness open={false}/>);
  await act(async()=>pending.resolve(f.file('file:///project/user.md')));
  expect(f.tabs()).toHaveLength(1);expect(f.show).not.toHaveBeenCalled();
});

it('creates one blank webpage per batch without navigation, and a fresh one on later calls', async () => {
  const f = fixture();
  await f.send([{ type: 'panel', name: 'browser' }, { type: 'panel', name: 'browser' }]);
  await waitFor(() => expect(f.create).toHaveBeenCalledOnce());
  expect(f.tabs()).toHaveLength(1);
  expect(f.tabs()[0].kind).toBe('browser');
  expect(f.command).not.toHaveBeenCalled();
  await f.send([{ type: 'panel', name: 'browser' }], '2');
  await waitFor(() => expect(f.create).toHaveBeenCalledTimes(2));
  expect(f.tabs()).toHaveLength(2);
  expect(screen.getByTestId('active').textContent).toBe(f.tabs()[1].id);
});


it('uses shared preparation for Agent, Cell and their panels, retaining the last successful target', async () => {
  const agent = vi.spyOn(agentCellApi, 'agent').mockResolvedValue({ agent: { agent_id: 'session:agent', title: 'Researcher', current_work_id: null, queued_count: 0 } });
  const cell = vi.spyOn(agentCellApi, 'cell').mockRejectedValue(new AgentCellError(404, 'retired'));
  const agents = vi.spyOn(agentCellApi, 'agents').mockResolvedValue({ agents: [], next_cursor: null });
  const cells = vi.spyOn(agentCellApi, 'cells').mockResolvedValue({ cells: [], next_cursor: null, has_more: false });
  const f = fixture();
  await f.send([
    { type: 'agent', agent_id: 'session:agent', root_session_id: 'one' },
    { type: 'cell', cell_id: 'cell:retired', root_session_id: 'one', host_session_id: 'host:one' },
    { type: 'panel', name: 'subagents', root_session_id: 'one' },
    { type: 'panel', name: 'cells', root_session_id: 'one', host_session_id: 'host:one' },
  ]);
  await waitFor(() => expect(f.tabs()).toHaveLength(3));
  expect(agent).toHaveBeenCalledWith('one', 'session:agent');
  expect(cell).toHaveBeenCalledWith('one', 'host:one', 'cell:retired');
  expect(agents).toHaveBeenCalledWith('one'); expect(cells).toHaveBeenCalledWith('one', 'host:one');
  expect(f.tabs().map((tab: {kind:string}) => tab.kind)).toEqual(['agent', 'subagents', 'cells']);
  expect(screen.getByTestId('active').textContent).toBe(f.tabs()[2].id);
  expect(f.show).toHaveBeenCalledOnce();
  expect(f.notify).toHaveBeenCalledWith('无法打开目标', 'cell:retired：retired', 'warning');
});

it('skips stale ROOT and Host targets individually without rebinding or dropping valid siblings', async () => {
  const agent = vi.spyOn(agentCellApi, 'agent'); const cell = vi.spyOn(agentCellApi, 'cell');
  const f = fixture();
  await f.send([
    { type: 'agent', agent_id: 'session:agent', root_session_id: 'old' },
    { type: 'cell', cell_id: 'cell:one', root_session_id: 'one', host_session_id: 'host:old' },
    { type: 'panel', name: 'capabilities' },
  ]);
  await waitFor(() => expect(f.tabs()).toHaveLength(1));
  expect(agent).not.toHaveBeenCalled(); expect(cell).not.toHaveBeenCalled();
  expect(f.tabs()[0].kind).toBe('capabilities');
});

it('cancels the entire pending object batch when ROOT changes, even if the user returns', async () => {
  const pending = deferred<Awaited<ReturnType<typeof agentCellApi.agent>>>();
  vi.spyOn(agentCellApi, 'agent').mockReturnValue(pending.promise);
  const cells = vi.spyOn(agentCellApi, 'cells');
  const f = fixture();
  await f.send([
    { type: 'agent', agent_id: 'session:agent', root_session_id: 'one' },
    { type: 'panel', name: 'cells', root_session_id: 'one', host_session_id: 'host:one' },
  ]);
  f.view.rerender(<f.Harness session="two" />); f.view.rerender(<f.Harness />);
  await act(async () => pending.resolve({ agent: { agent_id: 'session:agent', title: null, current_work_id: null, queued_count: 0 } }));
  expect(f.tabs()).toHaveLength(0); expect(cells).not.toHaveBeenCalled(); expect(f.show).not.toHaveBeenCalled(); expect(f.notify).not.toHaveBeenCalled();
});

it('does not publish prepared siblings after user navigation supersedes an object read', async () => {
  const pending = deferred<Awaited<ReturnType<typeof agentCellApi.agent>>>();
  vi.spyOn(agentCellApi, 'agent').mockReturnValue(pending.promise);
  const f = fixture();
  await f.send([
    { type: 'panel', name: 'capabilities' },
    { type: 'agent', agent_id: 'session:agent', root_session_id: 'one' },
  ]);
  fireEvent.click(screen.getByText('user file')); await waitFor(() => expect(f.tabs()).toHaveLength(1));
  await act(async () => pending.resolve({ agent: { agent_id: 'session:agent', title: null, current_work_id: null, queued_count: 0 } }));
  expect(f.tabs()).toHaveLength(1); expect(f.tabs()[0].kind).toBe('file');
});


it('does not notify a late object failure after the user leaves the ROOT', async () => {
  let reject!: (error: Error) => void;
  vi.spyOn(agentCellApi, 'agent').mockReturnValue(new Promise((_, fail) => { reject = fail; }));
  const f = fixture();
  await f.send([{ type: 'agent', agent_id: 'session:agent', root_session_id: 'one' }]);
  f.view.rerender(<f.Harness session="two" />);
  await act(async () => reject(new AgentCellError(404, 'retired')));
  expect(f.notify).not.toHaveBeenCalled(); expect(f.tabs()).toHaveLength(0);
});


it.each(['cell', 'cells'] as const)('drops the whole automatic batch when Host changes while preparing %s', async kind => {
  const pending = deferred<Awaited<ReturnType<typeof agentCellApi.cells>>>();
  const cells = vi.spyOn(agentCellApi, 'cells').mockReturnValue(pending.promise);
  const single = vi.spyOn(agentCellApi, 'cell').mockImplementation(async () => {
    await pending.promise;
    return { cell: { target: { kind: 'CELL', cell_id: 'cell:one' }, state: 'completed', has_unread_output: false, pending_calls: 0 } };
  });
  const f = fixture();
  await f.send([
    { type: 'panel', name: 'capabilities' },
    kind === 'cell' ? { type: 'cell', cell_id: 'cell:one', root_session_id: 'one', host_session_id: 'host:one' }
      : { type: 'panel', name: 'cells', root_session_id: 'one', host_session_id: 'host:one' },
    { type: 'browser', url: 'https://example.test' },
  ]);
  expect(kind === 'cell' ? single : cells).toHaveBeenCalledOnce();
  f.setHost('host:replacement');
  await act(async () => pending.resolve({ cells: [], next_cursor: null, has_more: false }));
  expect(f.tabs()).toHaveLength(0); expect(f.show).not.toHaveBeenCalled(); expect(f.create).not.toHaveBeenCalled(); expect(f.notify).not.toHaveBeenCalled();
});

it('manual old Cell history rechecks its original Host and still opens file and URL siblings', async () => {
  const cell = vi.spyOn(agentCellApi, 'cell').mockRejectedValue(new AgentCellError(404, '原 Host 已不可用'));
  const f = fixture(); f.setHost('host:replacement');
  fireEvent.click(screen.getByText('cell history'));
  await waitFor(() => expect(f.tabs()).toHaveLength(2));
  expect(cell).toHaveBeenCalledOnce(); expect(cell).toHaveBeenCalledWith('one', 'host:old', 'cell:old');
  expect(f.tabs().map((tab: {kind:string}) => tab.kind)).toEqual(['file', 'browser']);
  expect(f.notify).toHaveBeenCalledWith('无法打开目标', 'cell:old：原 Host 已不可用', 'warning');
});

it('published tool webpages are user tabs that survive the calling ROOT being replaced or absent', async () => {
  const f = fixture();
  await f.send([{type:'browser',url:'https://example.test'}]);
  await waitFor(() => expect(f.tabs()).toHaveLength(1));
  const id = f.tabs()[0].id;
  expect(f.create).toHaveBeenCalledWith({tabId:id});
  f.view.rerender(<f.Harness session="two" />);
  expect(f.tabs()).toHaveLength(1); expect(f.tabs()[0].id).toBe(id);
  expect(screen.getByTestId('active').textContent).toBe(id);
  f.view.rerender(<f.Harness session="" />);
  expect(f.tabs()).toHaveLength(1); expect(f.tabs()[0].id).toBe(id); expect(f.create).toHaveBeenCalledOnce();
});
