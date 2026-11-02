import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { FileVideoContent } from '../../frontend/components/file-video-content';
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
