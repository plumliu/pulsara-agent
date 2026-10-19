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

afterEach(()=>{cleanup();delete window.pulsaraDesktop;});
function deferred<T>() { let resolve!: (value:T)=>void;const promise=new Promise<T>(yes=>{resolve=yes;});return {resolve,promise}; }
function fixture() {
  let listener: ((r:PresentationRequest)=>void)|undefined;
  const connection={sessionId:'one',role:'controller',generation:1,current:()=>({hostSessionId:'host:one'}),subscribePresentation:(fn:(r:PresentationRequest)=>void)=>{listener=fn;return()=>{listener=undefined;};}} as unknown as RuntimeConnection;
  const file=(path:string):FilePreview=>({path:decodeURIComponent(new URL(path).pathname),name:'a.md',kind:'text',reading_root:'/project',read_token:crypto.randomUUID(),size:0,notice:null,content_url:'',document_url:null,images_url:null,can_open:false});
  const api:FilePreviewApi={open:vi.fn(async path=>file(path)),close:vi.fn(async()=>{}),page:vi.fn(),action:vi.fn()};
  const create=vi.fn(async ({sessionId,tabId})=>({instanceId:crypto.randomUUID(),sessionId,tabId,url:'',loading:false}));
  const command=vi.fn(async()=>{});
  window.pulsaraDesktop={browser:{create,command,close:vi.fn(async()=>{}),clear:vi.fn(async()=>{}),onEvent:()=>()=>{}}} as unknown as PulsaraDesktop;
  const show=vi.fn();
  function Probe(){const w=useDesktopWorkspace()!;const manual=useContext(PresentationContext);const files=useContext(FileLinkContext);return <><button onClick={event=>files?.open("/project/user.md",event.currentTarget)}>user file</button><pre data-testid="tabs">{JSON.stringify(w.tabs.map(tab=>({id:tab.id,kind:tab.kind,entity:tab.entityId,host:tab.hostSessionId})))}</pre><output data-testid="active">{w.activeId}</output><button onClick={()=>w.tabs[0]&&w.activate(w.tabs[0].id)}>select first</button><button onClick={()=>w.tabs[0]&&w.close(w.tabs[0].id)}>close first</button><button onClick={()=>manual?.([{type:'terminal',process_id:'old',host_session_id:'host:old'}])}>history</button></>;}
  function Harness({visible=true,open=false,session='one'}:{visible?:boolean;open?:boolean;session?:string}){return <DesktopWorkspaceProvider sessionId={session} visible={visible} onShow={show}><FilePreviewProvider api={api} ownerKey={session} sessionId={session} onNotify={()=>{}}><PresentationProvider connection={connection} visible={visible} isOpen={open}><Probe/></PresentationProvider></FilePreviewProvider></DesktopWorkspaceProvider>;}
  const view=render(<Harness/>);
  const send=(targets:PresentationTarget[],sequence='1')=>act(()=>listener?.({sequence,signal:new AbortController().signal,read:async()=>({targets})}));
  return {api,file,create,command,show,view,Harness,send,tabs:()=>JSON.parse(screen.getByTestId('tabs').textContent!)};
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
  const f=fixture();await f.send([{type:'panel',name:'tasks'}]);await waitFor(()=>expect(f.tabs()).toHaveLength(1));
  f.view.rerender(<f.Harness open/>);
  const pending=deferred<FilePreview>();vi.mocked(f.api.open).mockReturnValueOnce(pending.promise);
  await f.send([{type:'file',path:'/project/a.md'}],'2');
  f.view.rerender(<f.Harness open={false}/>);await act(async()=>pending.resolve(f.file('file:///project/a.md')));
  expect(f.tabs()).toHaveLength(1);expect(f.show).toHaveBeenCalledOnce();expect(f.api.close).toHaveBeenCalledOnce();
});
it('does not let an older slow batch steal focus, but retains its valid file tab',async()=>{
  const f=fixture();const pending=deferred<FilePreview>();vi.mocked(f.api.open).mockReturnValueOnce(pending.promise);
  await f.send([{type:'file',path:'/project/a.md'}]);await f.send([{type:'panel',name:'tasks'}],'2');
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
  await f.send([{type:'panel',name:'tasks'},{type:'file',path:'/project/missing'}]);
  await waitFor(()=>expect(f.tabs()).toHaveLength(2));expect(screen.getByTestId('active').textContent).toBe(f.tabs()[1].id);expect(f.show).toHaveBeenCalledOnce();
});
it('keeps valid siblings when WHATWG rejects one browser target',async()=>{
  const f=fixture();await f.send([{type:'file',path:'/project/a.md'},{type:'browser',url:'https://%'},{type:'panel',name:'tasks'}]);
  await waitFor(()=>expect(f.tabs()).toHaveLength(3));expect(f.create).not.toHaveBeenCalled();expect(screen.getByTestId('active').textContent).toBe(f.tabs()[2].id);
});
it('releases directory tokens and still publishes an error tab with its siblings',async()=>{
  const f=fixture();vi.mocked(f.api.open).mockResolvedValueOnce({...f.file('file:///project/docs'),kind:'directory'});
  await f.send([{type:'file',path:'/project/docs'},{type:'panel',name:'tasks'}]);
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
