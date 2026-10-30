import { closeHistory } from '@tiptap/pm/history';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { PromptDraftStore } from '../../frontend/lib/prompt-draft';
import { formatWorkspaceReference, type CompleteWorkspacePaths, type WorkspacePathPage } from '../../frontend/lib/file-reference';
import type { SkillCapability } from '../../frontend/lib/pulsara-types';
import { PromptComposer } from '../../frontend/components/prompt-composer';

const stores: PromptDraftStore[] = [];
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} });
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} });
});
afterEach(() => { cleanup(); stores.forEach(store => store.destroy()); stores.length = 0; vi.unstubAllGlobals(); });
const skills = ['find-skills', 'find-files', 'pdf'].map(name => ({ name, description: 'find description', enabled: true, effective: true } as SkillCapability));
function setup(completePaths?: CompleteWorkspacePaths) {
  const store = new PromptDraftStore();
  stores.push(store);
  const onSubmit = vi.fn();
  const props = { store, sessionId: 'one', disabled: false, placeholder: '输入', onSubmit, onNotify: vi.fn(), skills, onCompletePaths: completePaths };
  const view = render(<PromptComposer {...props} />);
  const editor = store.getEditor('one');
  const type = (text: string) => act(() => { editor.commands.insertContent({ type: 'text', text }); });
  const key = (key: string, extra = {}) => fireEvent.keyDown(screen.getByRole('textbox'), { key, ...extra });
  return { ...view, store, editor, type, key, onSubmit, props };
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { resolve, promise }; }
const directory = { kind: 'directory' as const, name: '文档 folder', path: '/workspace/文档 folder', relative_path: '文档 folder' };
const document = { kind: 'file' as const, name: 'draft.pdf', path: '/workspace/文档 folder/draft.pdf', relative_path: '文档 folder/draft.pdf' };
const page = (items: WorkspacePathPage['items'], next_cursor: string | null = null): WorkspacePathPage => ({ directory: '/workspace', items, next_cursor });

it('opens skill candidates with a bare slash and never requests file completion', async () => {
  const lookup = vi.fn<CompleteWorkspacePaths>();
  const {type, key, store} = setup(lookup);
  type('/');
  await screen.findByRole('option', {name:'find-skills'});
  expect(screen.getByRole('option', {name:'pdf'})).toBeTruthy();
  key('Enter');
  expect(store.summary('one').text).toBe('/find-skills ');
  expect(lookup).not.toHaveBeenCalled();
});

it.each(['$find', 'folder/pdf', 'https://example.com/pdf', 'C:/pdf', '/中文/pdf', '/pdf.txt', '@/pdf'])('does not offer skill completion inside %s', async raw => {
  const {type, store} = setup();
  await act(async () => {type(raw);});
  expect(screen.queryByRole('listbox', {name:'技能候选'})).toBeNull();
  expect(store.summary('one').text).toBe(raw);
});

it('matches only skill name prefixes and replaces the active range, preserving undo and surrounding text', async () => {
  const { editor, type, key, onSubmit, store } = setup();
  type('前文  后文');
  act(() => editor.commands.setTextSelection(4));
  type('/find');
  await screen.findByRole('option', { name: 'find-skills' });
  expect(screen.queryByRole('option', { name: 'pdf' })).toBeNull();
  act(() => editor.view.dispatch(closeHistory(editor.state.tr)));
  key('ArrowDown');
  key('Enter');
  await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull());
  expect(onSubmit).not.toHaveBeenCalled();
  expect(store.summary('one').text).toBe('前文 /find-files 后文');
  expect(screen.getByLabelText('技能：find-files')).toBeTruthy();
  act(() => editor.commands.undo());
  expect(store.summary('one').text).toBe('前文 /find 后文');
  key('Escape');
  key('Enter');
  expect(onSubmit).toHaveBeenCalledOnce();
});

it('drills into directories with the arrow and inserts an original path with Tab', async () => {
  const lookup = vi.fn<CompleteWorkspacePaths>().mockResolvedValueOnce(page([directory])).mockResolvedValueOnce(page([document]));
  const { type, key, store, onSubmit } = setup(lookup);
  type('@文');
  await screen.findByRole('option', { name: /文档 folder/ });
  key('ArrowRight');
  await screen.findByRole('option', { name: 'draft.pdf' });
  expect(lookup).toHaveBeenLastCalledWith('文档 folder/', null, expect.any(AbortSignal));
  key('Tab');
  expect(onSubmit).not.toHaveBeenCalled();
  const raw = `${formatWorkspaceReference(document)} `;
  expect((await store.capture('one')).content.parts).toEqual([{ type: 'text', text: raw }]);
  expect(raw).not.toContain('只读副本');
  store.insertText('restored', raw);
  expect(store.getEditor('restored').getJSON().content?.[0]?.content?.[0]?.type).toBe('fileReference');
  expect((await store.capture('restored')).content.parts).toEqual([{ type: 'text', text: raw }]);
});

it('browses a paginated directory selection and commits its reference with Space', async () => {
  const lookup = vi.fn<CompleteWorkspacePaths>().mockResolvedValueOnce(page([document], 'draft.pdf')).mockResolvedValueOnce(page([directory]));
  const { type, key, store } = setup(lookup);
  type('@');
  await screen.findByRole('option', { name: '加载更多' });
  key('ArrowDown');
  key('Enter');
  await screen.findByRole('option', { name: /文档 folder/ });
  expect(lookup).toHaveBeenLastCalledWith('', 'draft.pdf', expect.any(AbortSignal));
  key('Enter');
  expect(store.summary('one').text).toBe('@文档 folder/');
  key(' ');
  expect(store.summary('one').text).toBe(`${formatWorkspaceReference(directory)} `);
});

it('discards stale queries and cancels current requests on disable and session switch', async () => {
  const old = deferred<WorkspacePathPage>();
  const lookup = vi.fn<CompleteWorkspacePaths>().mockImplementationOnce(() => old.promise).mockResolvedValueOnce(page([document]));
  const { type, key, rerender, props, store } = setup(lookup);
  type('@d');
  await waitFor(() => expect(lookup).toHaveBeenCalledOnce());
  type('r');
  await screen.findByRole('option', { name: 'draft.pdf' });
  expect(lookup.mock.calls[0][2].aborted).toBe(true);
  await act(async () => old.resolve(page([directory])));
  expect(screen.queryByRole('button', { name: '进入 文档 folder' })).toBeNull();
  key('Escape');
  expect(store.summary('one').text).toBe('@dr');
  expect(screen.queryByRole('listbox')).toBeNull();
  type(' @fresh');
  await waitFor(() => expect(lookup).toHaveBeenCalledTimes(3));
  rerender(<PromptComposer {...props} disabled />);
  expect(lookup.mock.calls[2][2].aborted).toBe(true);
  expect(screen.queryByRole('listbox')).toBeNull();
  rerender(<PromptComposer {...props} sessionId="two" />);
  expect(store.summary('two').text).toBe('');
});

it('keeps IME Enter and Shift+Enter from selecting or submitting', async () => {
  const { type, key, store, onSubmit } = setup();
  type('/find');
  await screen.findByRole('option', { name: 'find-skills' });
  fireEvent.compositionStart(screen.getByRole('textbox'));
  key('Enter', { isComposing: true, keyCode: 229 });
  expect(store.summary('one').text).toBe('/find');
  expect(onSubmit).not.toHaveBeenCalled();
  fireEvent.compositionEnd(screen.getByRole('textbox'));
  // ProseMirror suppresses Safari's immediate post-composition Enter for 500 ms.
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 510)); });
  key('Enter', { shiftKey: true });
  expect(store.summary('one').text).toBe('/find\n');
  expect(onSubmit).not.toHaveBeenCalled();
});

it('keeps errors retryable, hides empty results, and does not interpret email text as a trigger', async () => {
  const lookup = vi.fn<CompleteWorkspacePaths>().mockRejectedValueOnce(new Error('unavailable')).mockResolvedValueOnce(page([]));
  const { type, key, onSubmit } = setup(lookup);
  type('user@example.com');
  expect(screen.queryByRole('listbox')).toBeNull();
  type(' @missing/');
  await screen.findByRole('button', { name: '重试' });
  key('Enter');
  await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull());
  expect(screen.queryByRole('status')).toBeNull();
  expect(screen.getByRole('textbox').getAttribute('aria-controls')).toBeNull();
  expect(key('Tab')).toBe(true);
  key('Enter');
  expect(onSubmit).toHaveBeenCalledOnce();
});

it('restores skill candidates after editing an unmatched prefix without swallowing Enter while hidden', async () => {
  const { editor, type, key, onSubmit, store } = setup();
  type('/findzzz');
  await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull());
  expect(screen.queryByRole('status')).toBeNull();
  key('Enter');
  expect(onSubmit).toHaveBeenCalledOnce();
  expect(store.summary('one').text).toBe('/findzzz');
  act(() => editor.commands.deleteRange({ from: 6, to: 9 }));
  await screen.findByRole('option', { name: 'find-skills' });
  key('Enter');
  expect(onSubmit).toHaveBeenCalledOnce();
  expect(store.summary('one').text).toBe('/find-skills ');
});

it('aborts pagination when the query changes and ignores a late page', async () => {
  const more = deferred<WorkspacePathPage>();
  const lookup = vi.fn<CompleteWorkspacePaths>().mockResolvedValueOnce(page([document], 'draft.pdf'))
    .mockImplementationOnce(() => more.promise).mockResolvedValueOnce(page([]));
  const { type } = setup(lookup);
  type('@');
  await screen.findByRole('button', { name: '加载更多' });
  fireEvent.click(screen.getByRole('button', { name: '加载更多' }));
  expect(lookup).toHaveBeenCalledTimes(2);
  type('new');
  await waitFor(() => expect(lookup).toHaveBeenCalledTimes(3));
  await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull());
  expect(lookup.mock.calls[1][2].aborted).toBe(true);
  await act(async () => more.resolve(page([directory])));
  expect(screen.queryByRole('option')).toBeNull();
});


it.each(['Tab', 'Enter', 'click'])('enters a directory with %s and continues selecting its child file', async choice => {
  const lookup = vi.fn<CompleteWorkspacePaths>().mockResolvedValueOnce(page([directory])).mockResolvedValue(page([document]));
  const { type, key, store, onSubmit } = setup(lookup);
  type('@文');
  await screen.findByRole('option', { name: /文档 folder/ });
  if (choice === 'click') fireEvent.click(screen.getByRole('button', { name: '文档 folder' }));
  else key(choice);
  expect(store.summary('one').text).toBe('@文档 folder/');
  expect(screen.queryByRole('button', { name: '目录：文档 folder' })).toBeNull();
  await screen.findByRole('option', { name: 'draft.pdf' });
  expect(lookup).toHaveBeenLastCalledWith('文档 folder/', null, expect.any(AbortSignal));
  key('Enter');
  expect(store.summary('one').text).toBe(`${formatWorkspaceReference(document)} `);
  expect(onSubmit).not.toHaveBeenCalled();
});

it('commits the chosen directory before child lookup finishes and ignores late candidates', async () => {
  const children = deferred<WorkspacePathPage>();
  const lookup = vi.fn<CompleteWorkspacePaths>().mockResolvedValueOnce(page([directory])).mockImplementationOnce(() => children.promise);
  const { type, key, store, editor } = setup(lookup);
  type('前文 @文');
  await screen.findByRole('option', { name: /文档 folder/ });
  key('Tab');
  await waitFor(() => expect(lookup).toHaveBeenCalledTimes(2));
  act(() => editor.view.dispatch(closeHistory(editor.state.tr)));
  expect(key(' ')).toBe(false);
  expect(store.summary('one').text).toBe(`前文 ${formatWorkspaceReference(directory)} `);
  await act(async () => children.resolve(page([document])));
  expect(screen.queryByRole('listbox')).toBeNull();
  expect(lookup.mock.calls[1][2].aborted).toBe(true);
  act(() => editor.commands.undo());
  expect(store.summary('one').text).toBe('前文 @文档 folder/');
});

it('does not consume spaces in an edited child query or IME', async () => {
  const lookup = vi.fn<CompleteWorkspacePaths>().mockImplementation(async query => page(query === '' ? [directory] : []));
  const { type, key, store } = setup(lookup);
  type('@');
  await screen.findByRole('option', { name: /文档 folder/ });
  key('Enter');
  await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull());
  expect(key(' ', { isComposing: true, keyCode: 229 })).toBe(true);
  expect(store.summary('one').text).toBe('@文档 folder/');
  type('child');
  expect(key(' ')).toBe(true);
  expect(store.summary('one').text).toBe('@文档 folder/child');
});


it('commits an empty directory with Space after its candidate popup has disappeared', async () => {
  const lookup = vi.fn<CompleteWorkspacePaths>().mockResolvedValueOnce(page([directory])).mockResolvedValue(page([]));
  const { type, key, store } = setup(lookup);
  type('@');
  await screen.findByRole('option', { name: /文档 folder/ });
  key('Tab');
  await waitFor(() => expect(lookup).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull());
  key(' ');
  expect(store.summary('one').text).toBe(`${formatWorkspaceReference(directory)} `);
});

it('accepts the current follow-up with Tab as an editable draft, without submitting, and restores it on undo', () => {
  const { props, rerender, store, editor, key, onSubmit } = setup();
  rerender(<PromptComposer {...props} suggestion="旧问题？" />);
  rerender(<PromptComposer {...props} suggestion="这些约束发生冲突时，我应该按什么标准取舍？" />);
  expect(key('Tab')).toBe(false);
  expect(store.summary('one').text).toBe('这些约束发生冲突时，我应该按什么标准取舍？');
  expect(onSubmit).not.toHaveBeenCalled();
  expect(screen.queryByText('这些约束发生冲突时，我应该按什么标准取舍？', { selector: '.prompt-composer__placeholder' })).toBeNull();
  act(() => { editor.commands.undo(); });
  expect(store.summary('one').hasContent).toBe(false);
  expect(screen.getByText('这些约束发生冲突时，我应该按什么标准取舍？', { selector: '.prompt-composer__placeholder' })).toBeTruthy();
});

it('preserves normal Tab navigation without an available follow-up, during composition, or with a draft', () => {
  const { props, rerender, store, type, key } = setup();
  expect(key('Tab')).toBe(true);
  rerender(<PromptComposer {...props} suggestion="我的问题？" />);
  for (const modifier of ['shiftKey', 'ctrlKey', 'altKey', 'metaKey', 'isComposing']) {
    expect(key('Tab', { [modifier]: true })).toBe(true);
  }
  expect(store.summary('one').hasContent).toBe(false);
  rerender(<PromptComposer {...props} suggestion="我的问题？" disabled />);
  expect(key('Tab')).toBe(true);
  rerender(<PromptComposer {...props} suggestion="我的问题？" />);
  type('我的草稿');
  expect(key('Tab')).toBe(true);
  expect(store.summary('one').text).toBe('我的草稿');
  expect(screen.queryByText('我的问题？')).toBeNull();
});

it('keeps one body Markdown context across an inline image when deciding skill candidates',async()=>{
 const {editor,type}=setup();
 act(()=>editor.commands.insertContent([{type:'text',text:'```js\n'},{type:'image',attrs:{assetId:'context-only'}},{type:'text',text:'\n'}]));
 type('/pd');await new Promise(resolve=>setTimeout(resolve,30));expect(screen.queryByRole('option')).toBeNull();
 act(()=>editor.commands.insertContent({type:'text',text:'\n```\n'}));type('/pd');expect(await screen.findByRole('option',{name:'pdf'})).toBeTruthy();
});
