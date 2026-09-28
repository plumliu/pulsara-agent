import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PromptDraftStore } from '../lib/prompt-draft';
import type { PromptAnnotationPart } from '../lib/prompt-content';
import { MarkdownBody } from './markdown-body';
import { PromptComposer } from './prompt-composer';
import { AnnotationBody, ResponseAnnotations } from './response-annotations';

vi.mock('@floating-ui/dom', async importOriginal => ({
  ...await importOriginal<typeof import('@floating-ui/dom')>(),
  computePosition: vi.fn(async () => ({ x: 100, y: 100, middlewareData: {} })),
  autoUpdate: (_reference: unknown, _floating: unknown, update: () => void) => { update(); return () => {}; },
}));
const body = '第一段需要解释。\n\n第二段需要核对。';
const first: PromptAnnotationPart = { type: 'annotation', quote: '第一段需要解释。', source: { entry_id: 'entry:a', start: 0, end: 8 } };
const second: PromptAnnotationPart = { type: 'annotation', quote: '第二段需要核对。', source: { entry_id: 'entry:a', start: 10, end: 18 } };
let store: PromptDraftStore;
let highlights: Map<string, { ranges: Range[] }>;
function Harness({ sessionId = 'one', canAnnotate = true }: { sessionId?: string; canAnnotate?: boolean }) {
  return <ResponseAnnotations sessionId={sessionId} store={store} canAnnotate={canAnnotate} onNotify={() => {}}>
    <div className="workbench"><div className="thread-scroll"><AnnotationBody entryId="entry:a" body={body}>
      <MarkdownBody annotationSource body={body} onNotify={() => {}} />
    </AnnotationBody></div><PromptComposer sessionId={sessionId} store={store} disabled={false} placeholder="正文" onSubmit={() => {}} onNotify={() => {}} /></div>
    <button>其他区域</button>
  </ResponseAnnotations>;
}
beforeEach(() => {
  store = new PromptDraftStore(); highlights = new Map();
  vi.stubGlobal('CSS', { ...globalThis.CSS, highlights });
  vi.stubGlobal('Highlight', class { ranges: Range[]; constructor(...ranges: Range[]) { this.ranges = ranges; } });
  vi.spyOn(Range.prototype, 'getClientRects').mockReturnValue([new DOMRect(100, 100, 120, 20)] as unknown as DOMRectList);
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([new DOMRect(100, 100, 650, 200)] as unknown as DOMRectList);
});
afterEach(() => { cleanup(); store.destroy(); window.getSelection()?.removeAllRanges(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('draft annotation UI', () => {
  it('adds a selection to a compact chip and immediately focuses its floating comment editor', async () => {
    const { container } = render(<Harness />);
    const text = container.querySelector('[data-source-text]')!.firstChild!;
    const range = document.createRange(); range.setStart(text, 0); range.setEnd(text, 8);
    window.getSelection()!.addRange(range);
    fireEvent.pointerUp(text.parentElement!);
    fireEvent.click(await screen.findByRole('button', { name: '添加注释' }));
    const input = await screen.findByRole('textbox', { name: '批注内容' });
    expect(document.activeElement).toBe(input);
    expect(window.getSelection()!.isCollapsed).toBe(true);
    expect(highlights.get('annotation-active')?.ranges[0].toString()).toBe(first.quote);
    expect(container.querySelectorAll('.annotation-chip')).toHaveLength(1);
    expect(container.querySelector('textarea')).toBeNull(); // Editor is a floating portal, not a tall composer card.
    const inline = screen.getByRole('dialog');
    expect(inline.classList.contains('annotation-editor--inline')).toBe(true);
    expect(inline.querySelector('blockquote,button')).toBeNull();
    expect(inline.textContent).toBe('');
    expect((await store.capture('one')).content.parts).toEqual([first]);
    fireEvent.change(input, { target: { value: '这句话是什么意思？' } });
    expect(store.annotations('one')[0].value.comment).toBe('这句话是什么意思？');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByRole('dialog')).toBe(inline);
    fireEvent.pointerDown(screen.getByRole('button', { name: '其他区域' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(highlights.has('annotation-active')).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '编辑批注 1' }));
    const preview = await screen.findByRole('dialog');
    expect(preview.classList.contains('annotation-editor--preview')).toBe(true);
    expect(preview.querySelector('blockquote')?.textContent).toBe(first.quote);
    expect((screen.getByRole('textbox', { name: '批注内容' }) as HTMLTextAreaElement).value).toBe('这句话是什么意思？');
    expect(screen.queryByRole('button', { name: '定位批注原文' })).toBeNull();
    expect(screen.queryByRole('button', { name: '完成' })).toBeNull();
  });

  it('keeps edit highlighting while another marker is hovered and reopens comments from markers', async () => {
    store.addAnnotation('one', first); store.addAnnotation('one', second);
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: '编辑批注 1' }));
    const input = await screen.findByRole('textbox', { name: '批注内容' });
    fireEvent.change(input, { target: { value: '解释一下' } });
    const marker = await screen.findByRole('button', { name: '正文批注 2' });
    fireEvent.mouseEnter(marker);
    expect(highlights.get('annotation-hover')?.ranges[0].toString()).toBe(second.quote);
    expect(highlights.get('annotation-active')?.ranges[0].toString()).toBe(first.quote);
    fireEvent.mouseLeave(marker);
    expect(highlights.has('annotation-hover')).toBe(false);
    expect(highlights.has('annotation-active')).toBe(true);
    fireEvent.pointerDown(screen.getByRole('button', { name: '其他区域' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(await screen.findByRole('button', { name: '正文批注 1' }));
    const reopened = await screen.findByRole('textbox', { name: '批注内容' });
    expect((reopened as HTMLTextAreaElement).value).toBe('解释一下');
    fireEvent.change(reopened, { target: { value: '换个角度解释' } });
    fireEvent.keyDown(reopened, { key: 'z', metaKey: true });
    expect(store.annotations('one')[0].value.comment).toBe('解释一下');
    fireEvent.keyDown(reopened, { key: 'z', metaKey: true, shiftKey: true });
    expect(store.annotations('one')[0].value.comment).toBe('换个角度解释');
    fireEvent.keyDown(reopened, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(highlights.size).toBe(0);
  });

  it('tracks duplicate quotes through deletion and undo without leaking draft identities', async () => {
    const id1 = store.addAnnotation('one', first); const id2 = store.addAnnotation('one', first);
    expect(id1).not.toBe(id2);
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: '编辑批注 2' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '批注内容' }), { target: { value: '第二份引用' } });
    fireEvent.click(screen.getByRole('button', { name: '移除批注 1' }));
    await screen.findByRole('dialog', { name: '编辑批注 1' });
    expect(store.annotations('one')[0]).toMatchObject({ id: id2, number: 1, value: { comment: '第二份引用' } });
    act(() => { store.getEditor('one').commands.undo(); });
    await screen.findByRole('dialog', { name: '编辑批注 2' });
    const snapshot = await store.capture('one');
    expect(snapshot.content.parts).toEqual([first, { ...first, comment: '第二份引用' }]);
    expect(JSON.stringify(snapshot.content)).not.toContain(id2);
    act(() => { store.restoreIfEmpty('other', snapshot.content); });
    expect((await store.capture('other')).content).toEqual(snapshot.content);
    expect(store.annotations('other')[1].id).not.toBe(id2);
  });

  it('clears floating UI on session changes and successful submission', async () => {
    store.addAnnotation('one', first);
    const { rerender } = render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: '编辑批注 1' }));
    await screen.findByRole('dialog');
    rerender(<Harness sessionId="two" />);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('button', { name: '正文批注 1' })).toBeNull();
    expect(highlights.size).toBe(0);
    rerender(<Harness />);
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(await screen.findByRole('button', { name: '编辑批注 1' }));
    const snapshot = await store.capture('one');
    act(() => { expect(store.clearIfSnapshot('one', snapshot)).toBe(true); });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(highlights.size).toBe(0);
  });

  it('blocks annotation mutations when control is lost, while retaining source copy and the draft', async () => {
    store.addAnnotation('one', first);
    const { container, rerender } = render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: '编辑批注 1' }));
    fireEvent.change(await screen.findByRole('textbox', { name: '批注内容' }), { target: { value: '保留的评论' } });
    const before = await store.capture('one');
    rerender(<Harness canAnnotate={false} />);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(highlights.size).toBe(0);
    expect(screen.queryByRole('button', { name: '正文批注 1' })).toBeNull();
    const remove = screen.getByRole('button', { name: '移除批注 1' }) as HTMLButtonElement;
    expect(remove.disabled).toBe(true); fireEvent.click(remove);
    const text = container.querySelector('[data-source-text]')!.firstChild!;
    const range = document.createRange(); range.setStart(text, 0); range.setEnd(text, 8);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
    fireEvent.pointerUp(text.parentElement!);
    expect(screen.queryByRole('button', { name: '添加注释' })).toBeNull();
    const setData = vi.fn(); fireEvent.copy(text.parentElement!, { clipboardData: { setData } });
    expect(setData).toHaveBeenCalledWith('text/plain', first.quote);
    expect((await store.capture('one')).content).toEqual(before.content);
    rerender(<Harness />);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('button', { name: '添加注释' })).toBeNull();
    expect(highlights.size).toBe(0);
  });
});
