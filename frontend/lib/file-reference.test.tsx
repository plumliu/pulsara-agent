import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { closeHistory } from '@tiptap/pm/history';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PromptDraftStore } from './prompt-draft';
import { fileReference, formatFileReference, splitFileReferences, type ImportedPath } from './file-reference';
import { PromptComposer } from '../components/prompt-composer';
import { PromptContentView } from '../components/prompt-content-view';

const imported = (name: string): ImportedPath => ({ path: `/tmp/imports/id/${name}`, name, bytes: 12, file_count: 1 });
const raw = (name: string) => formatFileReference(imported(name), false);
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const stores: PromptDraftStore[] = [];
function store() { const value = new PromptDraftStore(); stores.push(value); return value; }
afterEach(() => { cleanup(); stores.splice(0).forEach(value => value.destroy()); vi.restoreAllMocks(); });

describe('file reference path contract', () => {
  it('round trips JSON quoting, Unicode and brackets without folding ordinary paths', () => {
    for (const path of ['/tmp/测试 \\"】.pdf', 'C:\\Users\\a\\report.docx', '/tmp/a[b].xlsx']) {
      const text = formatFileReference({ ...imported('x'), path }, false);
      expect(fileReference(text)?.path).toBe(path);
      expect(splitFileReferences(`before${text}after`)).toEqual(['before', fileReference(text), 'after']);
    }
    for (const text of ['@/tmp/test.pdf', '/tmp/test.docx', 'read_file("/tmp/a.pdf")', raw('x').slice(0, -1)]) {
      expect(splitFileReferences(text)).toEqual([text]);
    }
  });

  it('keeps a mixed selection at the tail while imports finish in reverse order', async () => {
    const value = store();
    const a = deferred<ImportedPath>(); const b = deferred<ImportedPath>();
    value.setImporter(vi.fn((_session, files) => files[0].name === 'a.pdf' ? a.promise : b.promise));
    const editor = value.getEditor('s');
    value.insertText('s', 'before'); editor.commands.setTextSelection(2);
    const image = new File(['image'], 'a.png', { type: 'image/png' });
    Object.defineProperty(image, 'arrayBuffer', { value: async () => new Uint8Array([1, 2]).buffer });
    value.insertFiles('s', [new File(['pdf'], 'a.pdf'), image, new File(['docx'], 'b.docx')], 'end');
    value.insertText('s', 'after');
    await expect(value.capture('s')).rejects.toThrow('导入');
    b.resolve(imported('b.docx')); await new Promise(resolve => setTimeout(resolve, 0));
    expect(value.summary('s').pendingFiles).toBe(1);
    a.resolve(imported('a.pdf')); await new Promise(resolve => setTimeout(resolve, 0));
    const snapshot = await value.capture('s');
    expect(snapshot.content.parts).toEqual([
      { type: 'text', text: 'before' + raw('a.pdf') },
      { type: 'image', source: 'local', bytes: new Uint8Array([1, 2]), declaredMediaType: 'image/png' },
      { type: 'text', text: raw('b.docx') + 'after' },
    ]);
    expect(value.restoreIfEmpty('fork', snapshot.content)).toBe(true);
    expect((await value.capture('fork')).content).toEqual(snapshot.content);
    expect(value.getEditor('fork').getText()).toContain('/tmp/imports/id/a.pdf');
  });

  it('uses one directory reference including its images, then deletes and undoes atomically', async () => {
    const value = store(); const upload = vi.fn(async () => imported('directory'));
    value.setImporter(upload); const editor = value.getEditor('s');
    const file = new File(['image'], 'a.png', { type: 'image/png' });
    Object.defineProperty(file, 'webkitRelativePath', { value: 'directory/nested/a.png' });
    value.insertDirectory('s', [file]);
    await new Promise(resolve => setTimeout(resolve, 0));
    const snapshot = await value.capture('s');
    expect(upload).toHaveBeenCalledWith('s', [file], true, expect.any(AbortSignal));
    expect(snapshot.content.parts).toEqual([{ type: 'text', text: formatFileReference(imported('directory'), true) }]);
    editor.view.dispatch(closeHistory(editor.state.tr));
    editor.commands.setNodeSelection(1); editor.commands.deleteSelection();
    expect(value.summary('s').hasContent).toBe(false);
    editor.commands.undo();
    expect((await value.capture('s')).content).toEqual(snapshot.content);
  });

  it('shows failed import, retries in place, and exposes a copyable path in the card', async () => {
    const value = store(); const upload = vi.fn().mockRejectedValueOnce(new Error('磁盘不足')).mockResolvedValue(imported('report.pdf'));
    value.setImporter(upload);
    render(<PromptComposer store={value} sessionId="s" disabled={false} placeholder="input" onSubmit={() => {}} onNotify={() => {}} />);
    await act(async () => { value.insertFiles('s', [new File(['pdf'], 'report.pdf')]); });
    expect(value.summary('s').failedFiles).toBe(1);
    expect(screen.getByText('磁盘不足')).toBeTruthy();
    await expect(value.capture('s')).rejects.toThrow('导入');
    await act(async () => { fireEvent.click(screen.getByText('重试')); });
    expect(value.summary('s').failedFiles).toBe(0);
    expect(screen.getByRole('button', { name: 'PDF：report.pdf' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'PDF：report.pdf' }));
    expect(screen.getByText('/tmp/imports/id/report.pdf')).toBeTruthy();
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '复制路径' })); });
    expect(writeText).toHaveBeenCalledWith('/tmp/imports/id/report.pdf');
  });

  it('cancels removed uploads without resurrecting the card or changing a different session', async () => {
    const value = store(); const upload = deferred<ImportedPath>(); let signal!: AbortSignal;
    value.setImporter(async (_id, _files, _directory, nextSignal) => { signal = nextSignal; return upload.promise; });
    const editor = value.getEditor('s'); value.insertFiles('s', [new File(['pdf'], 'report.pdf')]);
    await Promise.resolve();
    editor.commands.selectAll(); editor.commands.deleteSelection();
    expect(signal.aborted).toBe(true);
    value.insertText('other', 'keep'); upload.resolve(imported('report.pdf'));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(value.summary('s').hasContent).toBe(false);
    expect((await value.capture('other')).content.parts).toEqual([{ type: 'text', text: 'keep' }]);
  });

  it('restores a retryable card when removal precedes the first upload microtask', async () => {
    const value = store(); const upload = vi.fn(async () => imported('report.pdf'));
    value.setImporter(upload); const editor = value.getEditor('s');
    value.insertFiles('s', [new File(['pdf'], 'report.pdf')]);
    editor.view.dispatch(closeHistory(editor.state.tr));
    editor.commands.selectAll(); editor.commands.deleteSelection();
    await Promise.resolve();
    expect(upload).not.toHaveBeenCalled();
    editor.commands.undo();
    expect(value.summary('s').failedFiles).toBe(1);
    expect(value.summary('s').pendingFiles).toBe(0);
    await expect(value.capture('s')).rejects.toThrow('导入');
  });

  it.each(['resolve', 'reject'] as const)('ignores a cancelled attempt that later %ss while its retry is pending', async outcome => {
    const value = store(); const first = deferred<ImportedPath>(); const second = deferred<ImportedPath>();
    value.setImporter(vi.fn().mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise));
    render(<PromptComposer store={value} sessionId="s" disabled={false} placeholder="input" onSubmit={() => {}} onNotify={() => {}} />);
    const editor = value.getEditor('s');
    await act(async () => { value.insertFiles('s', [new File(['pdf'], 'report.pdf')]); });
    await act(async () => {
      editor.view.dispatch(closeHistory(editor.state.tr));
      editor.commands.selectAll(); editor.commands.deleteSelection(); editor.commands.undo();
    });
    const retry = await screen.findByRole('button', { name: '重试' });
    await act(async () => { fireEvent.click(retry); });
    await act(async () => {
      if (outcome === 'resolve') first.resolve(imported('cancelled.pdf'));
      else first.reject(new Error('late aborted request'));
    });
    expect(value.summary('s').pendingFiles).toBe(1);
    expect(value.summary('s').failedFiles).toBe(0);
    await expect(value.capture('s')).rejects.toThrow('导入');
    await act(async () => { second.resolve(imported('current.pdf')); });
    expect((await value.capture('s')).content.parts).toEqual([{ type: 'text', text: raw('current.pdf') }]);
  });

  it('reconstructs history and queue cards from canonical text with exact surrounding prose', () => {
    for (const variant of ['message', 'queue'] as const) {
      const content = { parts: [{ type: 'text' as const, text: 'Before' + raw('report.pdf') + 'After /tmp/plain.pdf' }] };
      const view = render(<PromptContentView variant={variant} content={content} onReadImage={vi.fn()} />);
      expect(screen.getByRole('button', { name: 'PDF：report.pdf' })).toBeTruthy();
      const body = view.container.querySelector('.prompt-content-text')!;
      expect(body.firstChild?.textContent).toBe('Before');
      expect(body.lastChild?.textContent).toBe('After /tmp/plain.pdf');
      const selection = window.getSelection()!;
      const range = document.createRange(); range.selectNodeContents(body);
      selection.removeAllRanges(); selection.addRange(range);
      const setData = vi.fn();
      fireEvent.copy(body, { clipboardData: { setData } });
      expect(setData).toHaveBeenCalledWith('text/plain', content.parts[0].text);
      selection.removeAllRanges();
      view.unmount();
    }
  });
});

it('renders original path references across history and queue with their exact path markers', () => {
  const text = '查看 【本地文件："/tmp/资料/report.pdf"】 和 【本地文件夹："/tmp/资料"】';
  for (const variant of ['message', 'queue'] as const) {
    const view = render(<PromptContentView content={{ parts: [{ type: 'text', text }] }} variant={variant} onReadImage={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'PDF：report.pdf' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '目录：资料' })).toBeTruthy();
    const references = view.container.querySelectorAll('[data-file-reference]');
    expect([...references].map(node => node.getAttribute('data-file-reference'))).toEqual([
      '【本地文件："/tmp/资料/report.pdf"】', '【本地文件夹："/tmp/资料"】',
    ]);
    view.unmount();
  }
});
