import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { MarkdownBody } from '../../frontend/components/markdown-body';
import { InlineVisualization } from '../../frontend/components/inline-visualization';
import { visualizationLayoutMessageType } from '../../frontend/lib/visualization-frame';
import examples from '../fixtures/visualization-markers.json';

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {constructor(private callback: () => void) {} observe() {this.callback();} disconnect() {}});
  vi.stubGlobal('IntersectionObserver', class {observe() {} disconnect() {}});
  vi.stubGlobal('matchMedia', () => ({matches: false, addEventListener() {}, removeEventListener() {}}));
});
afterEach(() => {cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks();});
const item = {ordinal: 0, state: 'READY' as const, visualizationRef: 'sha256:'+'a'.repeat(64), contentSize: 16, sourceFilename: 'chart.html'};
const marker = '```pulsara-visualization\nchart.html\n```';

it('agrees with the backend marker corpus', () => {
  for (const example of examples) {
    const read = vi.fn();
    const items = Array.from({length: 10}, (_, ordinal) => ({ordinal, state: 'FAILED' as const, failureDetail: String(ordinal)}));
    const view = render(<MarkdownBody body={example.body} onNotify={() => {}} visualization={{entryId:'a', items, onRead:read}}/>);
    expect(view.container.querySelectorAll('.inline-visualization'), example.name).toHaveLength(example.groups?.length ?? example.paths.length);
    let offset = 0;
    const starts = (example.groups ?? example.paths.map(() => 1)).map(count => {const start=offset;offset+=count;return start;});
    expect([...view.container.querySelectorAll('.inline-visualization__page')].map(page => Number(page.getAttribute('data-visualization-ordinal'))), example.name).toEqual(starts);
    expect(read).not.toHaveBeenCalled(); view.unmount();
  }
});
it('renders only bound top-level assistant markers in place, including failures', async () => {
  const read = vi.fn(async () => '<h1>frozen</h1>');
  const body = 'before\n\n```pulsara-visualization\n\n```\n\nbetween\n\n'+marker+'\n\nafter';
  const view = render(<MarkdownBody body={body} onNotify={() => {}} visualization={{entryId: 'a', items: [{ordinal:0,state:'FAILED',failureDetail:'bad path'}, {...item,ordinal:1}], onRead:read}}/>);
  await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
  const content = view.container.textContent!;
  expect(content.indexOf('before')).toBeLessThan(content.indexOf('bad path'));
  const chart = view.container.querySelector('iframe')!;
  expect(screen.getByText('between').compareDocumentPosition(chart) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(chart.compareDocumentPosition(screen.getByText('after')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(view.container.querySelector('button[title="chart.html"]')).toBeTruthy();
  expect(read).toHaveBeenCalledWith('a',1,item.visualizationRef,16);
  expect(view.container.querySelectorAll('iframe')).toHaveLength(1);
});
it('keeps user and completed unbound markers ordinary and never reads during streaming', () => {
  const read = vi.fn();
  const view = render(<MarkdownBody body={marker} onNotify={() => {}}/>);
  expect(view.container.querySelector('iframe')).toBeNull();
  view.rerender(<MarkdownBody body={marker} streaming onNotify={() => {}} visualization={{entryId:'a',items:[],onRead:read}}/>);
  expect(screen.getByRole('status').textContent).toContain('正在准备');
  view.rerender(<MarkdownBody body={marker} onNotify={() => {}} visualization={{entryId:'a',items:[],onRead:read}}/>);
  expect(view.container.querySelector('[role=status]')).toBeNull(); expect(read).not.toHaveBeenCalled();
});
it('displays repeated paths independently and retries the saved occurrence', async () => {
  const read = vi.fn().mockRejectedValueOnce(Error('offline')).mockResolvedValue('<p>saved</p>');
  const view = render(<MarkdownBody body={marker+'\n\n'+marker} onNotify={() => {}} visualization={{entryId:'a',items:[item,{...item,ordinal:1}],onRead:read}}/>);
  await waitFor(() => expect(screen.getByText('已保存的可视化暂时无法读取。')).toBeTruthy());
  fireEvent.click(screen.getByRole('button',{name:'重试'}));
  await waitFor(() => expect(view.container.querySelectorAll('iframe')).toHaveLength(2));
  expect(read).toHaveBeenLastCalledWith('a',0,item.visualizationRef,16);
});
it('updates environment without replacing srcdoc and rejects stale viewport reports', async () => {
  vi.spyOn(HTMLElement.prototype,'clientWidth','get').mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype,'clientHeight','get').mockReturnValue(500);
  const view = render(<InlineVisualization entryId="a" items={[item]} onRead={async () => '<h1>frozen</h1>'}/>);
  await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
  const frame = view.container.querySelector('iframe')!; const html = frame.srcdoc;
  await act(async () => {document.documentElement.dataset.theme='dark';});
  expect(frame.srcdoc).toBe(html);
  const send = (width: number) => window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,data:{type:visualizationLayoutMessageType,mode:'root',viewport:{width,height:500},rect:{x:14,y:16,width:300,height:100}}}));
  act(() => {send(700);}); expect(view.container.querySelector('[data-visualization-layout]')?.getAttribute('data-visualization-layout')).toBe('page');
  act(() => {send(800);}); expect(view.container.querySelector('[data-visualization-layout]')?.getAttribute('data-visualization-layout')).toBe('root');
  expect((view.container.querySelector('.inline-visualization__stage') as HTMLElement).style.height).toBe('100px');
  expect(frame.style.height).toBe('500px');
  expect(frame.style.width).toBe('800px');
  expect(frame.style.transform).toBe('translateY(-16px)');
  expect((view.container.querySelector('.inline-visualization__crop') as HTMLElement).style.width).toBe('');
});

it('preserves the iframe when its containing MarkdownBody receives equivalent new props', async () => {
  const read = vi.fn(async () => '<input value="keep me">'); const notify = vi.fn();
  const view = render(<MarkdownBody body={marker} onNotify={notify} visualization={{entryId:'a',items:[item],onRead:read}}/>);
  await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
  const original = view.container.querySelector('iframe');
  view.rerender(<MarkdownBody body={marker} onNotify={notify} visualization={{entryId:'a',items:[{...item}],onRead:read}}/>);
  expect(view.container.querySelector('iframe')).toBe(original);
  expect(read).toHaveBeenCalledTimes(1);
});

it('renders independent galleries and switches pages without reloading retained frames', async () => {
  const read = vi.fn(async () => '<input value="keep me">');
  const items = Array.from({length: 3}, (_, ordinal) => ({...item,ordinal,sourceFilename:`${ordinal}.html`}));
  const view = render(<MarkdownBody body={'```pulsara-visualization\n0.html\n1.html\n```\n\nBetween\n\n```pulsara-visualization\n2.html\n```'} onNotify={() => {}} visualization={{entryId:'a',items,onRead:read}}/>);
  await waitFor(() => expect(view.container.querySelectorAll('iframe')).toHaveLength(2));
  const original = view.container.querySelector('iframe');
  const other = view.container.querySelectorAll('iframe')[1];
  expect(screen.queryByRole('button',{name:'下一张可视化'})).toBeNull();
  expect(screen.getAllByRole('group',{name:'可视化缩略图'})).toHaveLength(1);
  fireEvent.click(screen.getByRole('button',{name:'查看可视化 2'}));
  await waitFor(() => expect(view.container.querySelectorAll('iframe')).toHaveLength(3));
  expect(view.container.querySelector('[data-visualization-ordinal="0"]')?.hasAttribute('hidden')).toBe(true);
  expect(view.container.querySelector('[data-visualization-ordinal="0"]')?.hasAttribute('inert')).toBe(true);
  expect(view.container.querySelector('[data-visualization-ordinal="2"] iframe')).toBe(other);
  fireEvent.click(screen.getByRole('button',{name:'查看可视化 1'}));
  expect(view.container.querySelector('[data-visualization-ordinal="0"] iframe')).toBe(original);
  expect(read).toHaveBeenCalledTimes(3);
});

it('keeps failures selectable and keyboard navigation uses gallery-local positions', async () => {
  const read = vi.fn(async () => '<p>saved</p>');
  const view = render(<InlineVisualization entryId="a" items={[{ordinal:4,state:'FAILED',failureDetail:'missing file'},{...item,ordinal:5},{...item,ordinal:6}]} onRead={read}/>);
  expect(screen.getByText('missing file')).toBeTruthy();
  const first = screen.getByRole('button',{name:'查看可视化 1（无法显示）'});
  fireEvent.keyDown(first,{key:'End'});
  await waitFor(() => expect(view.container.querySelector('[data-visualization-ordinal="6"] iframe')).toBeTruthy());
  expect(screen.getByRole('button',{name:'查看可视化 3'}).getAttribute('aria-pressed')).toBe('true');
  fireEvent.keyDown(document.activeElement!,{key:'Home'});
  expect(screen.getByText('missing file')).toBeTruthy();
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(first,{key:'ArrowDown'});
  expect(screen.getByRole('button',{name:'查看可视化 2'}).getAttribute('aria-pressed')).toBe('true');
  fireEvent.keyDown(document.activeElement!,{key:'ArrowUp'});
  expect(document.activeElement).toBe(first);
  expect(read).toHaveBeenCalledWith('a',6,item.visualizationRef,16);
});

it('limits retained frames per gallery without limiting navigable items', async () => {
  const read = vi.fn(async (_entry: string, _ordinal: number) => '<input>');
  const view = render(<InlineVisualization entryId="a" items={Array.from({length:5},(_,ordinal)=>({...item,ordinal}))} onRead={read}/>);
  await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
  for (let index=1;index<5;index++) {
    fireEvent.click(screen.getByRole('button',{name:`查看可视化 ${index + 1}`}));
    await waitFor(() => expect(view.container.querySelector(`[data-visualization-ordinal="${index}"] iframe`)).toBeTruthy());
    expect(view.container.querySelectorAll('iframe').length).toBeLessThanOrEqual(3);
  }
  expect(view.container.querySelector('[data-visualization-ordinal="0"]')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'查看可视化 1'}));
  await waitFor(() => expect(view.container.querySelector('[data-visualization-ordinal="0"] iframe')).toBeTruthy());
  expect(read.mock.calls.filter(call=>call[1]===0)).toHaveLength(2);
});

it('sends hidden environment to inactive pages and restores visibility on selection', async () => {
  const read = vi.fn(async () => '<input>');
  const view = render(<InlineVisualization entryId="a" items={[item,{...item,ordinal:1}]} onRead={read}/>);
  await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
  const frame = view.container.querySelector('iframe')!;
  const post = vi.spyOn(frame.contentWindow!,'postMessage');
  fireEvent.click(screen.getByRole('button',{name:'查看可视化 2'}));
  await waitFor(() => expect(post).toHaveBeenCalledWith(expect.objectContaining({environment:expect.objectContaining({visible:false})}),'*'));
  fireEvent.click(screen.getByRole('button',{name:'查看可视化 1'}));
  await waitFor(() => expect(post).toHaveBeenLastCalledWith(expect.objectContaining({environment:expect.objectContaining({visible:true})}),'*'));
  expect(view.container.querySelector('iframe')).toBe(frame);
});

it('lazily loads actual PNG thumbnails and keeps thumbnail failures independent of pages', async () => {
  type Observed = {callback: IntersectionObserverCallback; elements: Element[]; root?: Element | Document | null};
  const observed: Observed[]=[];
  vi.stubGlobal('IntersectionObserver',class {
    record: Observed;
    constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {this.record={callback,elements:[],root:options?.root};observed.push(this.record);}
    observe(element: Element) {this.record.elements.push(element);} disconnect() {}
  });
  const read = vi.fn(async () => '<p>saved</p>');
  const thumb = vi.fn().mockResolvedValueOnce('data:image/png;base64,UE5H').mockRejectedValueOnce(Error('no thumbnail'));
  const view = render(<InlineVisualization entryId="a" items={[item,{...item,ordinal:1}]} onRead={read} onReadThumbnail={thumb}/>);
  expect(thumb).not.toHaveBeenCalled();
  const strip = observed.find(value=>value.root)!;
  act(()=>strip.callback(strip.elements.map(target=>({target,isIntersecting:true} as IntersectionObserverEntry)),{} as IntersectionObserver));
  await waitFor(()=>expect(thumb).toHaveBeenCalledTimes(2));
  await waitFor(()=>expect(view.container.querySelectorAll('.inline-visualization__thumbnail-image img')).toHaveLength(1));
  expect(thumb).toHaveBeenNthCalledWith(1,'a',0,item.visualizationRef,16,expect.any(AbortSignal));
  fireEvent.click(screen.getByRole('button',{name:'查看可视化 2'}));
  await waitFor(()=>expect(view.container.querySelector('[data-visualization-ordinal="1"] iframe')).toBeTruthy());
});

it('keeps tall roots valid and scrollable without translating or resizing the reference iframe', async () => {
  vi.spyOn(HTMLElement.prototype,'clientWidth','get').mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype,'clientHeight','get').mockReturnValue(720);
  const view = render(<InlineVisualization entryId="a" items={[item]} onRead={async () => '<main>long chart</main>'}/>);
  await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
  const frame = view.container.querySelector('iframe')!;
  act(() => {window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,data:{type:visualizationLayoutMessageType,mode:'root',viewport:{width:800,height:720},rect:{x:20,y:100,width:700,height:1500}}}));});
  expect(view.container.querySelector('[data-visualization-layout]')?.getAttribute('data-visualization-layout')).toBe('root-scroll');
  expect((view.container.querySelector('.inline-visualization__stage') as HTMLElement).style.height).toBe('720px');
  expect(frame.style.height).toBe('720px');
  expect(frame.style.transform).toBe('');
  const crop = view.container.querySelector('.inline-visualization__crop') as HTMLElement;
  expect(crop.style.height).toBe('');
  expect(crop.style.width).toBe('');
});

it('omits zoom below its 46px footprint and responds to changing root height', async () => {
  vi.spyOn(HTMLElement.prototype,'clientWidth','get').mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype,'clientHeight','get').mockReturnValue(500);
  const view = render(<InlineVisualization entryId="a" items={[item]} onRead={async () => '<main>short</main>'}/>);
  await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
  const frame = view.container.querySelector('iframe')!;
  for (const height of [8, 20, 45, 46, 165, 20]) {
    act(() => {window.dispatchEvent(new MessageEvent('message',{source:frame.contentWindow,data:{type:visualizationLayoutMessageType,mode:'root',viewport:{width:800,height:500},rect:{x:0,y:0,width:800,height}}}));});
    expect(Boolean(screen.queryByRole('button',{name:'放大可视化'}))).toBe(height >= 46);
    const stage = view.container.querySelector('.inline-visualization__stage') as HTMLElement;
    expect(stage.style.height).toBe(`${height}px`);
    expect(stage.getAttribute('data-visualization-compact')).toBe(height < 46 ? 'true' : null);
    expect((view.container.querySelector('.inline-visualization__crop') as HTMLElement).style.height).toBe(`${height}px`);
    expect(frame.style.height).toBe('500px');
  }
});

it('keeps maximized exit available when switching to a short gallery page', async () => {
  vi.spyOn(HTMLElement.prototype,'clientWidth','get').mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype,'clientHeight','get').mockReturnValue(500);
  const modal = new Set<HTMLDialogElement>();
  const matches = Element.prototype.matches;
  vi.spyOn(Element.prototype,'matches').mockImplementation(function (selector) {return selector === ':modal' ? modal.has(this as HTMLDialogElement) : matches.call(this,selector);});
  const view = render(<InlineVisualization entryId="a" items={[item,{...item,ordinal:1}]} onRead={async () => '<main>chart</main>'}/>);
  // jsdom does not implement native dialog methods; keep the stub on this fixture.
  const dialog = view.container.querySelector('dialog')!;
  Object.assign(dialog, {showModal: () => modal.add(dialog), close: () => modal.delete(dialog), show: () => {}});
  await waitFor(() => expect(view.container.querySelector('iframe')).toBeTruthy());
  fireEvent.click(screen.getByRole('button',{name:'查看可视化 2'}));
  await waitFor(() => expect(view.container.querySelectorAll('iframe')).toHaveLength(2));
  const short = view.container.querySelectorAll('iframe')[1];
  act(() => {window.dispatchEvent(new MessageEvent('message',{source:short.contentWindow,data:{type:visualizationLayoutMessageType,mode:'root',viewport:{width:800,height:500},rect:{x:0,y:0,width:800,height:20}}}));});
  expect(screen.queryByRole('button',{name:'放大可视化'})).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'查看可视化 1'}));
  fireEvent.click(screen.getByRole('button',{name:'放大可视化'}));
  fireEvent.click(screen.getByRole('button',{name:'查看可视化 2'}));
  fireEvent.click(screen.getByRole('button',{name:'退出放大'}));
  expect(view.container.querySelector('dialog')?.classList.contains('is-maximized')).toBe(false);
  expect(screen.queryByRole('button',{name:'放大可视化'})).toBeNull();
});
