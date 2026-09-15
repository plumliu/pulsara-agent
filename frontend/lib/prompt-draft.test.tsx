import { Slice } from '@tiptap/pm/model';
import { closeHistory } from '@tiptap/pm/history';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PromptComposer } from '../components/prompt-composer';
import type { EditablePromptContent } from './prompt-content';
import { PromptDraftStore } from './prompt-draft';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function imageFile(
  name: string,
  mediaType: string,
  bytes: readonly number[],
  read?: () => Promise<ArrayBuffer>,
): File {
  const file = new File([Uint8Array.from(bytes)], name, { type: mediaType });
  Object.defineProperty(file, 'arrayBuffer', {
    configurable: true,
    value: read ?? (async () => Uint8Array.from(bytes).buffer),
  });
  return file;
}

function imageBytes(content: EditablePromptContent): number[][] {
  return content.parts.flatMap((part) => part.type === 'image'
    ? [[...part.bytes]]
    : []);
}

function clipboardData(html: string, text = ''): DataTransfer {
  return {
    files: [] as unknown as FileList,
    items: [] as unknown as DataTransferItemList,
    types: ['text/html', 'text/plain'],
    getData: (type: string) => type === 'text/html' ? html : text,
  } as unknown as DataTransfer;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('PromptDraftStore', () => {
  it('serializes one paragraph in exact Text/Image order without trimming or labels', async () => {
    const store = new PromptDraftStore();
    store.getEditor('session');
    store.insertText('session', '  before');
    store.insertFiles('session', [imageFile('a.png', 'image/png', [1, 2, 3])]);
    store.insertText('session', '\\nafter [Figure 1]  ');
    store.insertFiles('session', [imageFile('a-again.png', 'image/png', [1, 2, 3])]);

    const snapshot = await store.capture('session');

    expect(snapshot.content.parts).toEqual([
      { type: 'text', text: '  before' },
      { type: 'image', source: 'local', bytes: Uint8Array.from([1, 2, 3]), declaredMediaType: 'image/png' },
      { type: 'text', text: '\\nafter [Figure 1]  ' },
      { type: 'image', source: 'local', bytes: Uint8Array.from([1, 2, 3]), declaredMediaType: 'image/png' },
    ]);
    expect(snapshot.content.parts.filter((part) => part.type === 'text')
      .map((part) => part.text).join('')).not.toContain('[Figure 2]');
    store.destroy();
  });

  it('accepts a pure image and replaces a selected text range with one image node', async () => {
    const store = new PromptDraftStore();
    const pure = store.getEditor('pure');
    store.insertFiles('pure', [imageFile('pure.webp', 'image/webp', [9, 8])]);
    expect((await store.capture('pure')).content.parts).toEqual([
      { type: 'image', source: 'local', bytes: Uint8Array.from([9, 8]), declaredMediaType: 'image/webp' },
    ]);

    const editor = store.getEditor('selection');
    store.insertText('selection', 'abcdef');
    editor.commands.setTextSelection({ from: 2, to: 5 });
    store.insertFiles('selection', [imageFile('selected.jpg', 'image/jpeg', [4, 5])]);
    expect((await store.capture('selection')).content.parts).toEqual([
      { type: 'text', text: 'a' },
      { type: 'image', source: 'local', bytes: Uint8Array.from([4, 5]), declaredMediaType: 'image/jpeg' },
      { type: 'text', text: 'ef' },
    ]);
    expect(pure.getJSON().content?.[0]?.content).toHaveLength(1);
    store.destroy();
  });

  it('keeps reserved multi-file positions when reads finish out of order', async () => {
    const store = new PromptDraftStore();
    store.getEditor('session');
    const first = deferred<ArrayBuffer>();
    const second = deferred<ArrayBuffer>();
    store.insertFiles('session', [
      imageFile('first.png', 'image/png', [1], () => first.promise),
      imageFile('second.png', 'image/png', [2], () => second.promise),
    ]);
    const capture = store.capture('session');

    second.resolve(Uint8Array.from([2]).buffer);
    await Promise.resolve();
    first.resolve(Uint8Array.from([1]).buffer);

    expect(imageBytes((await capture).content)).toEqual([[1], [2]]);
    store.destroy();
  });

  it('does not reinsert a deleted pending image and keeps another session isolated', async () => {
    const store = new PromptDraftStore();
    const pending = deferred<ArrayBuffer>();
    const editor = store.getEditor('first');
    store.insertFiles('first', [
      imageFile('late.png', 'image/png', [1], () => pending.promise),
      imageFile('kept.png', 'image/png', [2]),
    ]);
    editor.commands.deleteRange({ from: 1, to: 2 });
    store.getEditor('second');
    store.insertText('second', 'second draft');

    pending.resolve(Uint8Array.from([1]).buffer);
    await Promise.resolve();
    await Promise.resolve();

    expect(imageBytes((await store.capture('first')).content)).toEqual([[2]]);
    expect((await store.capture('second')).content.parts).toEqual([
      { type: 'text', text: 'second draft' },
    ]);
    store.destroy();
  });

  it('keeps failed image nodes visible and refuses partial serialization', async () => {
    const store = new PromptDraftStore();
    store.getEditor('session');
    store.insertText('session', 'keep me');
    store.insertFiles('session', [imageFile(
      'broken.png',
      'image/png',
      [1],
      async () => { throw new Error('browser read failed'); },
    )]);

    await expect(store.capture('session')).rejects.toThrow('browser read failed');
    expect(store.summary('session')).toMatchObject({ hasContent: true, hasImage: true, failedImages: 1 });
    expect(store.getEditor('session').getJSON().content?.[0]?.content?.map((node) => node.type))
      .toEqual(['text', 'image']);
    store.destroy();
  });

  it('uses the exact draft-session instance when a late acceptance tries to clear', async () => {
    const store = new PromptDraftStore();
    store.getEditor('session');
    store.insertText('session', 'old');
    const oldSnapshot = await store.capture('session');
    expect(store.clearIfSnapshot('session', oldSnapshot)).toBe(true);
    expect(store.restoreIfEmpty('session', { parts: [{ type: 'text', text: 'new' }] })).toBe(true);
    // Recreate the old numeric revision on a different session instance.
    store.insertText('session', '!');

    expect(store.summary('session').revision).toBe(oldSnapshot.revision);
    expect(store.clearIfSnapshot('session', oldSnapshot)).toBe(false);
    expect((await store.capture('session')).content.parts).toEqual([
      { type: 'text', text: '!new' },
    ]);
    store.destroy();
  });

  it('retains image bytes through delete undo and redo until the draft owner closes', async () => {
    const store = new PromptDraftStore();
    const revoked = vi.spyOn(URL, 'revokeObjectURL');
    const editor = store.getEditor('session');
    store.insertFiles('session', [imageFile('undo.png', 'image/png', [7, 6])]);
    await store.capture('session');
    editor.view.dispatch(closeHistory(editor.state.tr));
    editor.commands.deleteRange({ from: 1, to: 2 });
    expect(store.summary('session').hasImage).toBe(false);
    expect(editor.commands.undo()).toBe(true);
    expect(imageBytes((await store.capture('session')).content)).toEqual([[7, 6]]);
    expect(editor.commands.redo()).toBe(true);
    expect(store.summary('session').hasImage).toBe(false);
    expect(revoked).not.toHaveBeenCalled();
    store.destroy();
    expect(revoked).toHaveBeenCalledTimes(1);
  });
});

describe('PromptComposer clipboard and drag boundary', () => {
  function mount(store: PromptDraftStore, sessionId = 'session') {
    return render(<PromptComposer
      store={store}
      sessionId={sessionId}
      disabled={false}
      placeholder="prompt"
      onSubmit={() => undefined}
      onNotify={() => undefined}
    />);
  }

  it('keeps image input on paste/drop and undo/redo on editor shortcuts without toolbar controls', () => {
    const store = new PromptDraftStore();
    const view = mount(store);
    const composer = screen.getByLabelText('发送给 Pulsara');

    expect(screen.queryByRole('button', { name: '添加图片' })).toBeNull();
    expect(screen.queryByRole('button', { name: '撤销' })).toBeNull();
    expect(screen.queryByRole('button', { name: '重做' })).toBeNull();
    expect(view.container.querySelector('input[type="file"]')).toBeNull();

    store.insertText('session', 'shortcut');
    fireEvent.keyDown(composer, { key: 'z', code: 'KeyZ', ctrlKey: true });
    expect(store.summary('session').hasContent).toBe(false);
    fireEvent.keyDown(composer, { key: 'z', code: 'KeyZ', ctrlKey: true, shiftKey: true });
    expect(store.getEditor('session').getText()).toBe('shortcut');
    store.destroy();
  });

  it('copies an internal image occurrence through the actual paste handler', async () => {
    const store = new PromptDraftStore();
    store.getEditor('session');
    store.insertFiles('session', [imageFile('copy.png', 'image/png', [3, 4])]);
    await store.capture('session');
    mount(store);
    const editor = store.getEditor('session');
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', {
      value: clipboardData(editor.getHTML(), 'ignored fallback'),
    });

    fireEvent(screen.getByLabelText('发送给 Pulsara'), event);

    expect(event.defaultPrevented).toBe(true);
    expect(imageBytes((await store.capture('session')).content)).toEqual([[3, 4], [3, 4]]);
    store.destroy();
  });

  it('copies on an internal drop but leaves a normal moved drop to ProseMirror', async () => {
    const store = new PromptDraftStore();
    store.getEditor('session');
    store.insertFiles('session', [imageFile('drag.png', 'image/png', [5])]);
    await store.capture('session');
    mount(store);
    const editor = store.getEditor('session');
    const transfer = clipboardData(editor.getHTML());
    fireEvent.drop(screen.getByLabelText('发送给 Pulsara'), {
      dataTransfer: transfer,
      altKey: true,
      clientX: 0,
      clientY: 0,
    });
    expect(imageBytes((await store.capture('session')).content)).toEqual([[5], [5]]);

    const movedEvent = new Event('drop', { bubbles: true, cancelable: true }) as DragEvent;
    Object.defineProperty(movedEvent, 'dataTransfer', { value: transfer });
    let result: boolean | void = undefined;
    editor.view.someProp('handleDrop', (handler) => {
      result = handler(editor.view, movedEvent, Slice.empty, true);
      return true;
    });
    expect(result).toBe(false);
    expect(imageBytes((await store.capture('session')).content)).toEqual([[5], [5]]);
    store.destroy();
  });

  it('pastes external HTML and Markdown image syntax only as plain text', async () => {
    const store = new PromptDraftStore();
    mount(store);
    const source = '![remote](https://example.invalid/image.png)';
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', {
      value: clipboardData('<p><b>remote</b><img src="https://example.invalid/image.png"></p>', source),
    });

    fireEvent(screen.getByLabelText('发送给 Pulsara'), event);

    expect((await store.capture('session')).content.parts).toEqual([
      { type: 'text', text: source },
    ]);
    expect(store.summary('session').hasImage).toBe(false);
    store.destroy();
  });

  it('does not install a late file result after the composer store is destroyed', async () => {
    const store = new PromptDraftStore();
    const read = deferred<ArrayBuffer>();
    const revoked = vi.spyOn(URL, 'revokeObjectURL');
    store.getEditor('session');
    store.insertFiles('session', [imageFile('late.png', 'image/png', [8], () => read.promise)]);
    const objectUrl = store.imageStatuses('session')[0]?.assetId;
    mount(store);

    store.destroy();
    read.resolve(Uint8Array.from([8]).buffer);
    await act(async () => { await read.promise; });

    expect(objectUrl).toBeTruthy();
    expect(revoked).toHaveBeenCalledTimes(1);
  });
});
