import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { FileVideoContent, placeVideoAnnotations } from '../../frontend/components/file-video-content';
import { deriveIdentity } from '../../frontend/lib/identity';

const notify = vi.fn();
const props = { src: '/api/file-previews/video/content', name: 'clip.mp4', onNotify: notify, onRetry: vi.fn() };

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(function (this: HTMLMediaElement) {
    Object.defineProperty(this, 'paused', { value: true, configurable: true });
    this.dispatchEvent(new Event('pause'));
  });
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(function (this: HTMLMediaElement) {
    Object.defineProperty(this, 'paused', { value: false, configurable: true });
    this.dispatchEvent(new Event('play'));
    return Promise.resolve();
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function metadata(video: HTMLVideoElement, duration = 96) {
  Object.defineProperty(video, 'duration', { value: duration, configurable: true });
  Object.defineProperty(video, 'videoWidth', { value: 1024, configurable: true });
  Object.defineProperty(video, 'videoHeight', { value: 576, configurable: true });
  fireEvent.loadedMetadata(video);
}

it('plays the bound preview URL and seeks without empty analysis regions', () => {
  const saved = vi.fn();
  const view = render(<StrictMode><FileVideoContent {...props} initialState={{ time: 42 }} onPlaybackStateChange={saved} /></StrictMode>);
  const video = view.container.querySelector('video')!;
  expect(video.getAttribute('src')).toBe(props.src);
  expect(video.preload).toBe('metadata');
  expect(video.controls).toBe(false);
  metadata(video);
  fireEvent.seeked(video);
  expect(video.currentTime).toBe(42);
  const controls = within(view.container.querySelector('.video-player-controls') as HTMLElement);
  expect(controls.getAllByRole('button').slice(0, 3).map(button => button.getAttribute('aria-label')))
    .toEqual(['后退 5 秒', '播放视频', '前进 5 秒']);
  fireEvent.click(controls.getByRole('button', { name: '前进 5 秒' }));
  fireEvent.click(controls.getByRole('button', { name: '前进 5 秒' }));
  expect(video.currentTime).toBe(52);
  fireEvent.click(controls.getByRole('button', { name: '后退 5 秒' }));
  expect(video.currentTime).toBe(47);
  fireEvent.seeked(video);
  fireEvent.click(controls.getByRole('button', { name: '播放视频' }));
  expect(controls.getByRole('button', { name: '暂停视频' })).toBeDefined();
  fireEvent.click(controls.getByRole('button', { name: '暂停视频' }));
  fireEvent.change(screen.getByRole('slider', { name: '视频预览位置' }), { target: { value: '94' } });
  fireEvent.seeked(video);
  fireEvent.click(controls.getByRole('button', { name: '前进 5 秒' }));
  expect(video.currentTime).toBe(96);
  fireEvent.change(screen.getByRole('combobox', { name: '播放速度' }), { target: { value: '1.5' } });
  expect(video.playbackRate).toBe(1.5);
  fireEvent.change(screen.getByRole('slider', { name: '音量' }), { target: { value: '.5' } });
  expect(video.volume).toBe(.5);
  expect(screen.queryByRole('region', { name: '时间段批注列表' })).toBeNull();
  expect(view.container.querySelector('.video-reading-track')).toBeNull();
  expect(view.container.querySelector('.video-note-track')).toBeNull();
  expect(view.container.querySelector('.video-height-divider')).toBeNull();
  view.unmount();
  expect(video.getAttribute('src')).toBeNull();
  expect(saved.mock.calls.at(-1)?.[0]).toEqual({ time: 96, volume: .5, muted: false, rate: 1.5 });
});

it('can receive optional analysis without remounting media and removes it when cleared', () => {
  const note = { resultId: deriveIdentity('result', 'video-content-test'), from: 8, to: 24, text: '保留原片中的操作证据。' };
  const view = render(<FileVideoContent {...props} />);
  const video = view.container.querySelector('video')!;
  metadata(video);
  video.currentTime = 42;
  fireEvent.timeUpdate(video);
  view.rerender(<FileVideoContent {...props} activeSpan={{ from: 30, to: 58 }} annotations={[note]} />);
  expect(view.container.querySelector('video')).toBe(video);
  expect(video.currentTime).toBe(42);
  expect(screen.getByRole('button', { name: /定位用户预览到模型读取范围/ })).toBeDefined();
  fireEvent.click(screen.getByRole('button', { name: '查看批注 1，00:08–00:24，保留原片中的操作证据。' }));
  expect(video.currentTime).toBe(8);
  expect(screen.getAllByRole('button', { pressed: true })).toHaveLength(2);
  fireEvent.click(view.container.querySelector('.video-ticks')!);
  expect(screen.queryAllByRole('button', { pressed: true })).toHaveLength(0);
  view.rerender(<FileVideoContent {...props} annotations={[note]} />);
  expect(view.container.querySelector('.video-reading-track')).toBeNull();
  expect(screen.getByText(note.text)).toBeDefined();
  view.rerender(<FileVideoContent {...props} />);
  expect(view.container.querySelector('.video-notes')).toBeNull();
  expect(view.container.querySelector('video')).toBe(video);
});

it('does not invent a seek extent for unknown duration and exposes native decode failure', () => {
  const view = render(<FileVideoContent {...props} />);
  const video = view.container.querySelector('video')!;
  metadata(video, Infinity);
  expect((screen.getByRole('slider', { name: '视频预览位置' }) as HTMLInputElement).disabled).toBe(true);
  const controls = within(view.container.querySelector('.video-player-controls') as HTMLElement);
  expect((controls.getByRole('button', { name: '播放视频' }) as HTMLButtonElement).disabled).toBe(false);
  Object.defineProperty(video, 'error', { value: { code: 4 }, configurable: true });
  fireEvent.error(video);
  expect(screen.getByRole('alert').textContent).toContain('不支持这个视频的容器或编码');
  fireEvent.click(screen.getByRole('button', { name: '重试' }));
  expect(props.onRetry).toHaveBeenCalledOnce();
});

it('does not overwrite saved playback time when closed before metadata loads', () => {
  const saved = vi.fn();
  const view = render(<FileVideoContent {...props} initialState={{ time: 42 }} onPlaybackStateChange={saved} />);
  view.unmount();
  expect(saved).not.toHaveBeenCalled();
});

it('applies explicit gallery positions to an existing player without autoplay or remount', async () => {
  const view=render(<FileVideoContent {...props}/>);
  const video=view.container.querySelector('video')!; metadata(video);
  await video.play(); video.currentTime=44;
  view.rerender(<FileVideoContent {...props} previewPosition={{time:17}}/>);
  expect(view.container.querySelector('video')).toBe(video);
  expect(video.currentTime).toBe(17); expect(video.paused).toBe(true);
  video.currentTime=70;
  view.rerender(<FileVideoContent {...props} previewPosition={{time:17}}/>);
  expect(video.currentTime).toBe(17);
});

it('shares controls and analysis with hidden native audio while observing its visible container', () => {
  const observed: Element[] = [];
  let changed!: IntersectionObserverCallback;
  vi.stubGlobal('IntersectionObserver', class { constructor(callback: IntersectionObserverCallback) { changed = callback; } observe(element: Element) { observed.push(element); } disconnect() {} });
  const note = { resultId: deriveIdentity('result', 'audio-note'), from: 8, to: 24, text: '音频证据' };
  const view = render(<StrictMode><FileVideoContent {...props} kind="audio" annotations={[note]} /></StrictMode>);
  const audio = view.container.querySelector('audio')!;
  expect(audio.hidden).toBe(true);
  expect(view.container.querySelector('video, .video-player-viewport, .video-height-divider')).toBeNull();
  expect(screen.queryByRole('button', { name: '全屏视频' })).toBeNull();
  expect(observed.every(node => node === view.container.querySelector('.video-layout'))).toBe(true);
  Object.defineProperty(audio, 'duration', { value: 96, configurable: true });
  fireEvent.loadedMetadata(audio);
  fireEvent.click(screen.getByRole('button', { name: '播放音频' }));
  expect(audio.paused).toBe(false);
  changed([{ isIntersecting: false, intersectionRect: { width: 0, height: 0 } } as IntersectionObserverEntry], {} as IntersectionObserver);
  expect(audio.paused).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: /查看批注 1/ }));
  expect(audio.currentTime).toBe(8);
  expect(screen.queryByRole('button', { name: '定位来源调用' })).toBeNull();
  view.unmount();
  expect(audio.getAttribute('src')).toBeNull();
});

it('keeps an annotation outside the current duration readable and selected without seeking', () => {
  const note = { resultId: deriveIdentity('result', 'out-of-range-note'), from: 8, to: 120, text: '完整的原记录' };
  const view = render(<FileVideoContent {...props} annotations={[note]} />);
  const video = view.container.querySelector('video')!;
  metadata(video);
  video.currentTime = 40;
  fireEvent.click(screen.getByRole('button', { name: /查看批注 1/ }));
  expect(video.currentTime).toBe(40);
  expect(screen.getByText(note.text)).toBeDefined();
  expect(screen.getAllByRole('button', { pressed: true })).toHaveLength(2);
});

it.each(['video', 'audio'] as const)('follows actual %s playback and seeking after a clicked note, without seeking or pausing on automatic selection', async kind => {
  const notes = [
    { resultId: deriveIdentity('result', 'follow-first'), from: 4, to: 9, text: '开场' },
    { resultId: deriveIdentity('result', 'follow-second'), from: 12, to: 14, text: '问题' },
    { resultId: deriveIdentity('result', 'follow-third'), from: 16, to: 33, text: '执行过程' },
  ];
  const view = render(<FileVideoContent {...props} kind={kind} annotations={notes} />);
  const player = view.container.querySelector(kind)!;
  Object.defineProperty(player, 'duration', { value: 60, configurable: true });
  fireEvent.loadedMetadata(player);
  const cards = view.container.querySelectorAll<HTMLElement>('.video-note-card');
  const markers = view.container.querySelectorAll('.video-note-range');
  const list = view.container.querySelector<HTMLElement>('.video-note-list')!;
  Object.defineProperty(list, 'clientHeight', { value: 100, configurable: true });
  Object.defineProperty(cards[2], 'offsetTop', { value: 500, configurable: true });
  Object.defineProperty(cards[2], 'offsetHeight', { value: 80, configurable: true });
  fireEvent.click(cards[1]);
  fireEvent.seeked(player);
  await player.play();
  const advance = (time: number) => { player.currentTime = time; fireEvent.timeUpdate(player); };
  advance(14);
  expect(cards[1].getAttribute('aria-pressed')).toBe('true'); // Gap retains note 2.
  advance(18);
  expect(cards[1].getAttribute('aria-pressed')).toBe('false');
  expect(cards[2].getAttribute('aria-pressed')).toBe('true');
  expect(markers[2].getAttribute('aria-pressed')).toBe('true');
  expect(list.scrollTop).toBe(500);
  expect(player.currentTime).toBe(18);
  expect(player.paused).toBe(false);
  expect(view.container.querySelector(kind)).toBe(player);
  fireEvent.change(screen.getByRole('slider', { name: `${kind === 'audio' ? '音频' : '视频'}预览位置` }), { target: { value: '5' } });
  expect(cards[2].getAttribute('aria-pressed')).toBe('true'); // Wait for the native observation.
  fireEvent.seeked(player);
  expect(cards[0].getAttribute('aria-pressed')).toBe('true');
  player.pause();
  fireEvent.keyDown(view.container.querySelector('.video-layout')!, { key: 'Escape' });
  fireEvent.timeUpdate(player);
  fireEvent.pause(player);
  expect(screen.queryAllByRole('button', { pressed: true })).toHaveLength(0);
  advance(5.5);
  expect(cards[0].getAttribute('aria-pressed')).toBe('true');
  expect(player.paused).toBe(true);
});

it('keeps the selected note through overlaps and gaps, then selects a containing note when its range ends', () => {
  const notes = [[0, 10], [5, 15], [5, 15], [20, 22]].map(([from, to], index) => ({
    resultId: deriveIdentity('result', `follow-overlap-${index}`), from, to, text: `批注 ${index + 1}`,
  }));
  const view = render(<FileVideoContent {...props} annotations={notes} />);
  const player = view.container.querySelector('video')!;
  metadata(player);
  const cards = view.container.querySelectorAll('.video-note-card');
  const advance = (time: number, selected: number) => {
    player.currentTime = time; fireEvent.timeUpdate(player);
    expect(cards[selected].getAttribute('aria-pressed')).toBe('true');
    expect(view.container.querySelectorAll('.video-note-card.is-selected')).toHaveLength(1);
    expect(player.currentTime).toBe(time);
  };
  advance(2, 0);
  advance(6, 0);
  advance(10, 1);
  fireEvent.click(cards[2]);
  fireEvent.seeked(player);
  advance(12, 2);
  advance(15, 2);
  advance(21, 3);
  advance(25, 3);
  advance(7, 1); // Backward seek with equal starts uses the existing list order.
  advance(0, 0);
});

it('places dense overlaps without repeated index lookups and keeps every loaded annotation selectable', () => {
  const notes=Array.from({length:180},(_,index)=>({resultId:deriveIdentity('result',`overlap-${index}`),from:1,to:8,text:`完整批注 ${index}`}));
  const view=render(<FileVideoContent {...props} annotations={notes}/>);
  const video=view.container.querySelector('video')!;
  metadata(video);
  expect(view.container.querySelectorAll('.video-note-range')).toHaveLength(notes.length);
  expect(view.container.querySelectorAll('.video-note-card')).toHaveLength(notes.length);
  const last=screen.getByRole('button',{name:'查看批注 180，00:01–00:08，完整批注 179'});
  fireEvent.click(last);
  expect(video.currentTime).toBe(1);
  expect(last.getAttribute('aria-pressed')).toBe('true');
  const cards=view.container.querySelectorAll('.video-note-card');
  video.currentTime=4;fireEvent.timeUpdate(video);
  expect(view.container.querySelectorAll('.video-note-card')[179]).toBe(cards[179]);
});


it('uses ordered interval lanes with original numbering at large overlap and reuses the lowest free lane', () => {
  const notes=Array.from({length:10000},(_,i)=>({resultId:String(i),from:1,to:2,text:''}));
  const placement=placeVideoAnnotations(notes);
  expect(placement.map(item=>item.lane)).toEqual(Array.from({length:10000},(_,i)=>i));
  expect(placement.at(-1)?.ordinal).toBe(10000);
  const reused=placeVideoAnnotations([{...notes[0],from:0,to:10},{...notes[1],from:1,to:2},{...notes[2],from:11,to:12}]);
  expect(reused.map(item=>item.lane)).toEqual([0,1,0]);
});
