import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MediaAnnotationView } from '../../frontend/components/media-annotation-view';
import type { FilePreview, FilePreviewApi } from '../../frontend/lib/file-preview';

const file:FilePreview={read_token:'source',path:'/project/media.wav',reading_root:'/project',name:'media.wav',kind:'audio',modified_ns:'1',size:1,notice:null,content_url:'/audio',document_url:null,images_url:null,can_open:false};
const note=(id:number)=>({resultId:`entry:${id}`,entryId:`entry:${id}`,sessionId:'session:source',from:1,to:2,text:`批注原文 ${id}`});
const api=():FilePreviewApi=>({open:vi.fn(),close:vi.fn(),snapshot:vi.fn(),page:vi.fn(),action:vi.fn(),annotations:vi.fn()});
const children=(notes:readonly {resultId:string;text:string}[])=><div>{notes.map(note=><p key={note.resultId}>{note.text}</p>)}</div>;
afterEach(()=>{cleanup();vi.clearAllMocks();});
it('reads one actual page at open and preserves all notes when the user requests further pages',async()=>{
  const files=api();
  vi.mocked(files.annotations).mockResolvedValueOnce({items:Array.from({length:25},(_,i)=>note(i)),next:'next-real-page'})
    .mockResolvedValueOnce({items:[note(24),note(25)],next:'last-page'})
    .mockResolvedValueOnce({items:[note(26)],next:null});
  render(<MediaAnnotationView file={file} api={files} revision="1">{children}</MediaAnnotationView>);
  await screen.findByText('批注原文 24');
  expect(files.annotations).toHaveBeenCalledExactlyOnceWith('source',null,expect.any(AbortSignal));
  expect(screen.queryByText('批注原文 25')).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'加载更多批注'}));
  await screen.findByText('批注原文 25');
  expect(files.annotations).toHaveBeenLastCalledWith('source','next-real-page',expect.any(AbortSignal));
  expect(screen.getAllByText('批注原文 24')).toHaveLength(1);
  fireEvent.click(screen.getByRole('button',{name:'加载更多批注'}));
  await screen.findByText('批注原文 26');
  expect(screen.getByText('批注原文 0')).toBeDefined();
  expect(screen.queryByRole('button',{name:'加载更多批注'})).toBeNull();
  expect(files.annotations).toHaveBeenCalledTimes(3);
});
it('retains loaded original text after a later page failure and retries the same real cursor',async()=>{
  const files=api();
  vi.mocked(files.annotations).mockResolvedValueOnce({items:[note(0)],next:'page-2'}).mockRejectedValueOnce(Error('下一页暂不可用')).mockResolvedValueOnce({items:[note(1)],next:null});
  render(<MediaAnnotationView file={file} api={files} revision="1">{children}</MediaAnnotationView>);
  await screen.findByText('批注原文 0');
  fireEvent.click(screen.getByRole('button',{name:'加载更多批注'}));
  await screen.findByText('下一页暂不可用');
  expect(screen.getByText('批注原文 0')).toBeDefined();
  fireEvent.click(screen.getByRole('button',{name:'重新读取批注'}));
  await screen.findByText('批注原文 1');
  expect(files.annotations).toHaveBeenNthCalledWith(3,'source','page-2',expect.any(AbortSignal));
});
it('aborts owned page reads on unmount without reading the next page',async()=>{
  const files=api();
  vi.mocked(files.annotations).mockResolvedValue({items:[note(0)],next:'later'});
  const view=render(<MediaAnnotationView file={file} api={files} revision="1">{children}</MediaAnnotationView>);
  await screen.findByText('批注原文 0');
  const signal=vi.mocked(files.annotations).mock.calls[0][2];
  view.unmount();
  await waitFor(()=>expect(signal.aborted).toBe(true));
  expect(files.annotations).toHaveBeenCalledOnce();
});
