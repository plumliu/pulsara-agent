import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { VisualizationOccurrence } from '../lib/pulsara-types';
import { visualizationLayoutMessageType } from '../lib/visualization-frame';
import { VisualizationGallery } from './visualization-gallery';

const items = (count: number): VisualizationOccurrence[] => Array.from({ length: count }, (_, ordinal) => ({ ordinal, state: 'READY', visualizationRef: `sha256:${ordinal}`, contentSize: 16 }));
const read = vi.fn(async (_entry: string, ordinal: number) => `<h1>Chart ${ordinal}</h1>`);
let intersections: IntersectionObserverCallback;
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { constructor(private callback: ResizeObserverCallback) {} observe() { this.callback([], this as unknown as ResizeObserver); } disconnect() {} });
  vi.stubGlobal('IntersectionObserver', class { constructor(callback: IntersectionObserverCallback) { intersections = callback; } observe() {} disconnect() {} });
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(700);
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(420);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); read.mockClear(); });
const open = () => fireEvent.click(screen.getByRole('button', { name: '展开可视化图集' }));
const next = () => fireEvent.click(screen.getByRole('button', { name: '下一张可视化' }));

it('labels the count and uses exact subscribed filenames, including duplicate names', async () => {
  const group = items(3);
  group[0].sourceFilename = '客户结构 <完整 & 复核>.html';
  group[1].sourceFilename = group[0].sourceFilename;
  const view = render(<VisualizationGallery entryId="a" items={group} onRead={read} />);
  expect(screen.getByText('可视化 · 共 3 张')).toBeTruthy();
  open();
  await waitFor(() => expect(view.container.querySelector('iframe')?.title).toBe(group[0].sourceFilename));
  expect(view.container.querySelector('.visualization-gallery__toolbar > span')?.textContent).toBe(group[0].sourceFilename);
  expect(screen.getByRole('button', {name:'查看可视化 1'}).title).toBe(group[0].sourceFilename);
  next();
  expect(screen.getByRole('button', {name:'查看可视化 2'}).getAttribute('aria-pressed')).toBe('true');
  next();
  expect(view.container.querySelector('.visualization-gallery__toolbar > span')?.textContent).toBe('可视化 3');
});

it('renders no control for an empty group; one item needs no filmstrip', async () => {
  const view = render(<VisualizationGallery entryId="a" items={[]} onRead={read} />);
  expect(view.container.innerHTML).toBe('');
  view.rerender(<VisualizationGallery entryId="a" items={items(1)} onRead={read} />);
  open(); await waitFor(() => expect(read).toHaveBeenCalledOnce());
  expect(screen.queryByRole('group')).toBeNull();
  expect(screen.queryByRole('button', { name: '下一张可视化' })).toBeNull();
});

it('keeps recent frame identity across switching and folding', async () => {
  const view = render(<VisualizationGallery entryId="a" items={items(4)} onRead={read} />);
  expect(read).not.toHaveBeenCalled(); open();
  const first = await waitFor(() => { const frame = view.container.querySelector('iframe'); expect(frame).toBeTruthy(); return frame; });
  next(); await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole('button', { name: '上一张可视化' }));
  expect(view.container.querySelector('.is-active iframe')).toBe(first);
  fireEvent.click(screen.getByRole('button', { name: '收起可视化图集' })); open();
  expect(view.container.querySelector('.is-active iframe')).toBe(first);
  expect(read).toHaveBeenCalledTimes(2);
});

it('can navigate a thousand results with at most three resident HTML frames', async () => {
  const view = render(<VisualizationGallery entryId="a" items={items(1000)} onRead={read} />);
  open();
  await waitFor(() => expect(read).toHaveBeenCalledOnce());
  for (let i = 0; i < 12; i++) { next(); await waitFor(() => expect(read).toHaveBeenCalledTimes(i + 2)); }
  expect(view.container.querySelectorAll('iframe')).toHaveLength(3);
  const firstThumb = screen.getByRole('button', { name: '查看可视化 1' });
  fireEvent.keyDown(firstThumb, { key: 'End' });
  await waitFor(() => expect(read).toHaveBeenLastCalledWith('a', 999, 'sha256:999', 16));
  expect(screen.getByRole('button', { name: '下一张可视化' }).hasAttribute('disabled')).toBe(true);
  fireEvent.keyDown(screen.getByRole('button', { name: '查看可视化 1000' }), { key: 'Home' });
  await waitFor(() => expect(read).toHaveBeenLastCalledWith('a', 0, 'sha256:0', 16));
  expect(view.container.querySelectorAll('iframe')).toHaveLength(3);
}, 15_000);

it('isolates failed occurrences, missing references and retryable read errors', async () => {
  const reader = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue('<h1>Recovered</h1>');
  const group = items(3); group[1] = { ordinal: 1, state: 'FAILED', failureDetail: '找不到文件' }; group[2].visualizationRef = undefined;
  const view = render(<VisualizationGallery entryId="a" items={group} onRead={reader} />); open();
  fireEvent.click(await screen.findByRole('button', { name: '重试' }));
  await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
  next(); expect(screen.getByText('找不到文件')).toBeTruthy();
  next(); expect(screen.getByText('可视化引用不完整。')).toBeTruthy();
  expect(reader).toHaveBeenCalledTimes(2);
});

it('does not let an evicted slow read overwrite the current page', async () => {
  let finish!: (value: string) => void;
  const reader = vi.fn((_entry: string, ordinal: number) => ordinal === 0 ? new Promise<string>(resolve => { finish = resolve; }) : Promise.resolve(`<h1>${ordinal}</h1>`));
  const view = render(<VisualizationGallery entryId="a" items={items(4)} onRead={reader} />); open();
  next(); next(); next();
  await act(async () => { finish('<h1>Late</h1>'); });
  expect(view.container.querySelector('.is-active')?.getAttribute('data-visualization-ordinal')).toBe('3');
  expect(view.container.querySelector('[data-visualization-ordinal="0"]')).toBeNull();
  expect(view.container.querySelectorAll('iframe')).toHaveLength(3);
});

it('only accepts geometry from its own frame and never resizes the outer gallery', async () => {
  const view = render(<VisualizationGallery entryId="a" items={items(1)} onRead={read} />); open();
  const frame = await waitFor(() => { const el = view.container.querySelector('iframe'); expect(el).toBeTruthy(); return el!; });
  const report = (source: MessageEventSource, rect: unknown) => act(() => window.dispatchEvent(new MessageEvent('message', { source, data: { type: visualizationLayoutMessageType, mode: 'root', rect } })));
  report(window, { x: 10, y: 10, width: 200, height: 100 });
  expect(view.container.querySelector('[data-visualization-layout="root"]')).toBeNull();
  report(frame.contentWindow!, { x: 10, y: 10, width: 200, height: 100 });
  expect(view.container.querySelector('[data-visualization-layout="root"]')).toBeTruthy();
  expect((view.container.querySelector('.assistant-visualization') as HTMLElement).style.width).toBe('');
  expect(frame.style.transform).toBe('translate(-10px, -10px)');
  for (const rect of [null, { x: -1, y: 0, width: 10, height: 20 }, { x: 0, y: 0, width: 800, height: 100 }, { x: 0, y: 0, width: Infinity, height: 100 }]) {
    report(frame.contentWindow!, rect);
    expect(view.container.querySelector('[data-visualization-layout="root"]')).toBeNull();
  }
});

it('requests only visible thumbnails and cancels unstarted work when folded', async () => {
  let finish!: (image: string) => void;
  const thumb = vi.fn(() => new Promise<string>(resolve => { finish = resolve; }));
  const view = render(<VisualizationGallery entryId="a" items={items(30)} onRead={read} onReadThumbnail={thumb} />); open();
  expect(thumb).not.toHaveBeenCalled();
  const buttons = [...view.container.querySelectorAll('[data-ordinal]')];
  act(() => intersections(buttons.slice(0, 3).map(target => ({ target, isIntersecting: true })) as IntersectionObserverEntry[], {} as IntersectionObserver));
  await waitFor(() => expect(thumb).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: '收起可视化图集' }));
  await act(async () => { finish('data:image/png;base64,AAAA'); });
  expect(thumb).toHaveBeenCalledOnce();
  expect(view.container.querySelector('img')).toBeNull();
});
