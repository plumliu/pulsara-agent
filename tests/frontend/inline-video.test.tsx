import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { InlineMediaPage, MediaThumbnail, videoReferenceMediaType } from '../../frontend/components/inline-video';
import { FileMediaContext } from '../../frontend/components/file-link-context';
import type { FilePreviewApi } from '../../frontend/lib/file-preview';
import { videoPoster } from '../../frontend/lib/video-media';

const item = {ordinal:1, state:'READY' as const, contentDigest:`sha256:${'a'.repeat(64)}`, contentSize:32, sourceFilename:'demo.mp4', mediaType:videoReferenceMediaType};
const preview = {read_token:'video', path:'/project/demo.mp4', reading_root:'/project', name:'demo.mp4', kind:'video' as const, modified_ns:'1234567890123456789', size:100, notice:null, content_url:'/api/file-previews/video/content', document_url:null, images_url:null, can_open:false};
let observed: {target:Element; callback:IntersectionObserverCallback}[];
beforeEach(() => {
  observed = [];
  vi.stubGlobal('IntersectionObserver', class {constructor(private callback:IntersectionObserverCallback) {} observe(target:Element) { observed.push({target,callback:this.callback}); } disconnect() {}});
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
});
afterEach(() => {cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks();});
function visibility(visible:boolean) {
  act(() => { for (const {target, callback} of observed) callback([{target,isIntersecting:visible,intersectionRect:{width:visible?100:0,height:visible?100:0}} as IntersectionObserverEntry], {} as IntersectionObserver); });
}
function api():FilePreviewApi {return {snapshot:vi.fn(),open:vi.fn(async () => preview),close:vi.fn(async () => {}),page:vi.fn(),action:vi.fn()};}
const read = async () => JSON.stringify({path:'/project/demo.mp4'});

it('lazily opens native video, pauses on hide, expands with actual time and releases on unmount', async () => {
  const files = api(), open = vi.fn(), onRead = vi.fn(read);
  const view = render(<StrictMode><FileMediaContext.Provider value={{api:files,ownerKey:'owner',sessionId:'session',open}}><InlineMediaPage entryId="entry" item={item} onRead={onRead} active/></FileMediaContext.Provider></StrictMode>);
  expect(onRead).not.toHaveBeenCalled();
  visibility(true);
  await waitFor(() => expect(view.container.querySelector('video')).toBeTruthy());
  const video = view.container.querySelector('video')!;
  expect(video.controls).toBe(true);
  expect(video.preload).toBe('metadata');
  expect(video.autoplay).toBe(false);
  Object.defineProperty(video,'readyState',{value:2,configurable:true}); video.currentTime = 17;
  fireEvent.click(screen.getByRole('button',{name:'扩大视频'}));
  expect(open).toHaveBeenCalledWith('/project/demo.mp4',17);
  const pause = vi.mocked(video.pause); pause.mockClear();
  visibility(false); expect(pause).toHaveBeenCalled();
  visibility(true); expect(view.container.querySelector('video')).toBe(video);
  view.unmount();
  expect(files.close).toHaveBeenCalledWith('video');
  expect(video.getAttribute('src')).toBeNull();
  expect(video.load).toHaveBeenCalled();
});

it('releases a late preview without mounting it after its owner is gone', async () => {
  const files = api(); let settle!:(value:typeof preview)=>void;
  vi.mocked(files.open).mockImplementation(() => new Promise(resolve => {settle=resolve;}));
  const view=render(<FileMediaContext.Provider value={{api:files,ownerKey:'owner',sessionId:'session',snapshot:vi.fn(),open:vi.fn()}}><InlineMediaPage entryId="entry" item={item} onRead={read} active/></FileMediaContext.Provider>);
  visibility(true); await waitFor(() => expect(files.open).toHaveBeenCalled());
  view.unmount();
  await act(async () => {settle(preview);});
  expect(files.close).toHaveBeenCalledExactlyOnceWith('video');
});

it('keeps thumbnail failure independent of playback and always marks the item as video', async () => {
  const files=api(), onRead=vi.fn(read);
  vi.mocked(files.open).mockRejectedValue(new Error('no thumbnail'));
  const view=render(<FileMediaContext.Provider value={{api:files,ownerKey:'owner',sessionId:'session',snapshot:vi.fn(),open:vi.fn()}}><MediaThumbnail entryId="entry" item={item} onRead={onRead} enabled={false}/></FileMediaContext.Provider>);
  expect(onRead).not.toHaveBeenCalled();
  view.rerender(<FileMediaContext.Provider value={{api:files,ownerKey:'owner',sessionId:'session',snapshot:vi.fn(),open:vi.fn()}}><MediaThumbnail entryId="entry" item={item} onRead={onRead} enabled/></FileMediaContext.Provider>);
  await waitFor(() => expect(files.open).toHaveBeenCalled());
  expect(view.container.querySelector('.inline-visualization__video-play')).toBeTruthy();
  expect(view.container.querySelector('video')).toBeNull();
});

it.each([[8,1],[1,0.5],[Infinity,0]])('captures one frame for duration %s, then frees the native decoder', async (duration, time) => {
  const native=document.createElement.bind(document); let media:HTMLVideoElement|undefined;
  vi.spyOn(document,'createElement').mockImplementation((tag:string, options?:ElementCreationOptions) => {
    const node=native(tag,options); if(tag==='video') media=node as HTMLVideoElement; return node;
  });
  const drawImage=vi.fn(); vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue({drawImage} as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype,'toDataURL').mockReturnValue('data:image/png;base64,poster');
  const abort=new AbortController(); const result=videoPoster('/video',abort.signal);
  Object.defineProperties(media!,{duration:{value:duration},readyState:{value:2},videoWidth:{value:1920},videoHeight:{value:1080}});
  media!.dispatchEvent(new Event('loadedmetadata'));
  await Promise.resolve();
  expect(media!.currentTime).toBe(time);
  if(time) media!.dispatchEvent(new Event('seeked'));
  expect(await result).toBe('data:image/png;base64,poster');
  expect(drawImage).toHaveBeenCalledOnce();
  expect(media!.getAttribute('src')).toBeNull(); expect(media!.pause).toHaveBeenCalled(); expect(media!.load).toHaveBeenCalled();
});

it('cancels frame extraction while metadata is still pending', async () => {
  const abort=new AbortController(); const result=videoPoster('/video',abort.signal);
  abort.abort(); await expect(result).rejects.toMatchObject({name:'AbortError'});
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled(); expect(HTMLMediaElement.prototype.load).toHaveBeenCalled();
});

it('canceled slow thumbnail opens do not block later items and release late leases', async () => {
  const files=api(); let settle!:(value:typeof preview)=>void;
  vi.mocked(files.open).mockImplementationOnce(() => new Promise(resolve => {settle=resolve;})).mockRejectedValueOnce(new Error('second thumbnail unavailable'));
  const mount = () => render(<FileMediaContext.Provider value={{api:files,ownerKey:'owner',sessionId:'session',snapshot:vi.fn(),open:vi.fn()}}><MediaThumbnail entryId="entry" item={item} onRead={read} enabled/></FileMediaContext.Provider>);
  const first=mount(); await waitFor(() => expect(files.open).toHaveBeenCalledTimes(1));
  first.unmount(); mount();
  await waitFor(() => expect(files.open).toHaveBeenCalledTimes(2));
  await act(async () => {settle(preview);});
  expect(files.close).toHaveBeenCalledWith('video');
});

it('renders audio with native compact controls, static thumbnail and independently owned release', async () => {
  const files = api(), open = vi.fn(), onRead = vi.fn(async () => JSON.stringify({path:'/project/speech.wav'}));
  const audioItem = {...item, mediaType:'application/vnd.pulsara.audio-reference+json', sourceFilename:'speech.wav'};
  vi.mocked(files.open).mockResolvedValue({...preview, kind:'audio', path:'/project/speech.wav'});
  const thumbnail = render(<FileMediaContext.Provider value={{api:files,ownerKey:'owner',sessionId:'session',open}}><MediaThumbnail entryId="entry" item={audioItem} onRead={onRead} enabled/></FileMediaContext.Provider>);
  expect(onRead).not.toHaveBeenCalled();
  expect(files.open).not.toHaveBeenCalled();
  expect(thumbnail.container.querySelector('video,audio,img')).toBeNull();
  thumbnail.unmount();
  const view = render(<StrictMode><FileMediaContext.Provider value={{api:files,ownerKey:'owner',sessionId:'session',open}}><InlineMediaPage entryId="entry" item={audioItem} onRead={onRead} active/></FileMediaContext.Provider></StrictMode>);
  visibility(true);
  await waitFor(() => expect(view.container.querySelector('audio')).toBeTruthy());
  const audio = view.container.querySelector('audio')!;
  expect(audio.controls).toBe(true);
  expect(audio.autoplay).toBe(false);
  expect(view.container.querySelector('video')).toBeNull();
  Object.defineProperty(audio,'readyState',{value:2,configurable:true}); audio.currentTime=11;
  fireEvent.click(screen.getByRole('button',{name:'扩大音频'}));
  expect(open).toHaveBeenCalledWith('/project/speech.wav',11);
  vi.mocked(audio.pause).mockClear();
  visibility(false); expect(audio.pause).toHaveBeenCalled();
  view.unmount();
  expect(audio.getAttribute('src')).toBeNull();
  expect(files.close).toHaveBeenCalledExactlyOnceWith('video');
});
