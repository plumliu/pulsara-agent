import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  CanonicalPromptContent,
  CanonicalPromptImagePart,
  EditablePromptContent,
} from '../lib/prompt-content';
import { PromptContentView } from './prompt-content-view';

function canonicalImage(
  refOrdinal: number,
  digestByte: string,
  owner: CanonicalPromptImagePart['owner'] = { kind: 'entry', entryId: 'entry-1' },
): CanonicalPromptImagePart {
  return {
    type: 'image',
    source: 'canonical',
    digest: `sha256:${digestByte.repeat(64)}`,
    encodedBytes: 3,
    mediaType: 'image/png',
    width: 12,
    height: 8,
    refOrdinal,
    owner,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('PromptContentView', () => {
  it('keeps sent annotations outside the bubble and opens frozen details without reading their source', async () => {
    const read = vi.fn();
    const quote = '<script>quoted text stays inert</script>\n完整引用';
    const content: CanonicalPromptContent = { parts: [
      { type: 'annotation', quote, comment: '解释一下这一段', source: { entry_id: 'unloaded', start: 0, end: quote.length } },
      { type: 'annotation', quote: '来源已不存在的引用', source: null },
      { type: 'text', text: '结合这两处说明。' },
    ] };
    const view = render(<PromptContentView content={content} variant="message" onReadImage={read}
      renderMessageBody={body => <div data-testid="bubble">{body}</div>} />);
    const bubble = screen.getByTestId('bubble');
    expect(bubble.textContent).toBe('结合这两处说明。');
    expect(view.container.querySelector('.sent-annotation-strip')?.nextElementSibling).toBe(bubble);
    expect(screen.queryByRole('dialog')).toBeNull();
    const trigger = screen.getByRole('button', { name: '查看批注 1' });
    await userEvent.hover(trigger);
    const preview = await screen.findByRole('dialog', { name: 'Annotation 1' });
    expect(preview.querySelector('blockquote')?.textContent).toBe(quote);
    expect(within(preview).getByText('解释一下这一段')).toBeTruthy();
    expect(preview.querySelector('textarea, input, [contenteditable], script')).toBeNull();
    expect(within(preview).queryByRole('button')).toBeNull();
    await userEvent.unhover(trigger);
    await userEvent.hover(preview);
    await new Promise(resolve => setTimeout(resolve, 150));
    expect(screen.getByRole('dialog', { name: 'Annotation 1' })).toBeTruthy();
    await userEvent.keyboard('{Escape}');
    await userEvent.click(screen.getByRole('button', { name: '查看批注 2' }));
    expect(screen.getByRole('dialog', { name: 'Annotation 2' }).querySelector('blockquote')?.textContent).toBe('来源已不存在的引用');
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(read).not.toHaveBeenCalled();
    expect(content.parts[0]).toMatchObject({ quote, comment: '解释一下这一段' });
  });

  it('keeps image numbering separate and avoids an empty body for an annotation-only message', () => {
    const annotation = { type: 'annotation' as const, quote: '仅有引用', source: null };
    const renderBody = vi.fn(body => <div data-testid="bubble">{body}</div>);
    const view = render(<PromptContentView content={{ parts: [annotation] }} variant="message" onReadImage={vi.fn()}
      renderMessageBody={renderBody} />);
    expect(renderBody).toHaveBeenLastCalledWith(null);
    expect(screen.getByRole('button', { name: '查看批注 1' })).toBeTruthy();
    view.rerender(<PromptContentView content={{ parts: [annotation, canonicalImage(0, 'a'), annotation, canonicalImage(1, 'b')] }}
      variant="message" onReadImage={vi.fn()} renderMessageBody={renderBody} />);
    expect(view.container.querySelector('.prompt-thumbnail-strip')?.nextElementSibling?.className).toBe('sent-annotation-strip');
    expect(within(screen.getByTestId('bubble')).getAllByRole('button', { name: /^打开 Figure [12]$/ })).toHaveLength(2);
    expect(screen.getByRole('button', { name: '查看批注 2' })).toBeTruthy();
  });

  it('reuses image loading and lightbox for a tool preview without Figure captions or status text', async () => {
    vi.stubGlobal('IntersectionObserver', undefined);
    const read = vi.fn(async () => Uint8Array.from([1, 2, 3]));
    const view = render(<PromptContentView
      content={{ parts: [{ type: 'text', text: 'Image loaded.' }, canonicalImage(0, 'a')] }}
      variant="tool" onReadImage={read}
    />);
    await waitFor(() => expect(view.container.querySelector('.tool-image-preview img')).toBeTruthy());
    expect(read).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Image loaded.')).toBeNull();
    expect(screen.queryByText(/Figure/)).toBeNull();
    const trigger = screen.getByRole('button', { name: '放大图片' });
    await userEvent.click(trigger);
    expect(screen.getByRole('dialog', { name: 'Lightbox' })).toBeTruthy();
    expect(document.querySelector('.yarl__slide_current .prompt-lightbox-caption')).toBeNull();
    expect(screen.queryByText(/Figure/)).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('derives queue Figure chips from occurrences without reading image payloads', async () => {
    const first = canonicalImage(0, 'a', { kind: 'queue', queueItemId: 'queue-1' });
    const second = canonicalImage(1, 'a', { kind: 'queue', queueItemId: 'queue-1' });
    const content: CanonicalPromptContent = {
      parts: [
        first,
        { type: 'text', text: 'literal [Figure 1] and ' },
        second,
        { type: 'text', text: 'tail' },
      ],
    };
    const read = vi.fn(async () => Uint8Array.from([1, 2, 3]));
    render(<PromptContentView content={content} variant="queue" onReadImage={read} />);

    expect(screen.getByText('2 张图片')).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /^打开 Figure [12]$/ })).toHaveLength(2);
    expect(document.querySelector('.prompt-content-text')?.textContent)
      .toBe('literal [Figure 1] and ');
    expect(read).not.toHaveBeenCalled();

    const trigger = screen.getByRole('button', { name: '打开 Figure 2' });
    await userEvent.hover(trigger);
    await waitFor(() => expect(read).toHaveBeenCalledWith(second));
    const tooltip = screen.getByRole('tooltip');
    expect(within(tooltip).getByRole('img', { name: 'Figure 2 缩略图' })).toBeTruthy();
    expect(screen.queryByRole('dialog', { name: 'Lightbox' })).toBeNull();
    await userEvent.click(trigger);
    await waitFor(() => expect(read).toHaveBeenCalledWith(second));
    expect(read).toHaveBeenCalledTimes(1);
    expect(document.querySelector('.yarl__slide_current .prompt-lightbox-caption')?.textContent).toBe('Figure 2');
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Lightbox' })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('shows one inline chip per queue occurrence, loading only on hover or activation', async () => {
    const images = [canonicalImage(0, 'a'), canonicalImage(1, 'b'), canonicalImage(2, 'a')];
    const content: CanonicalPromptContent = {
      parts: [images[0], { type: 'text', text: 'between' }, images[1], images[2]],
    };
    const read = vi.fn(async (image: CanonicalPromptImagePart) => Uint8Array.from([image.refOrdinal + 1]));
    const view = render(<PromptContentView content={content} variant="queue" onReadImage={read} />);
    expect(screen.getAllByRole('button', { name: /^打开 Figure [123]$/ })).toHaveLength(3);
    expect([...view.container.querySelectorAll('.prompt-image-chip')].map(item => item.textContent))
      .toEqual(['Figure 1', 'Figure 2', 'Figure 3']);
    expect(view.container.querySelector('.prompt-content-body')?.textContent).toBe('Figure 1betweenFigure 2Figure 3');
    expect(view.container.querySelector('img')).toBeNull();
    expect(view.container.querySelector('.prompt-thumbnail-strip')).toBeNull();
    expect(read).not.toHaveBeenCalled();
    const trigger = screen.getByRole('button', { name: '打开 Figure 2' });
    trigger.focus(); await userEvent.keyboard('{Enter}');
    await waitFor(() => expect(read).toHaveBeenCalledWith(images[1]));
    expect(document.querySelector('.yarl__slide_current .prompt-lightbox-caption')?.textContent).toBe('Figure 2');
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('keeps sent thumbnails above inline chips and shares their numbering and loaded images', async () => {
    class IdleIntersectionObserver {
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('IntersectionObserver', IdleIntersectionObserver);
    const images = [canonicalImage(0, 'a'), canonicalImage(1, 'b'), canonicalImage(2, 'a')];
    const read = vi.fn(async () => Uint8Array.from([1, 2, 3]));
    const view = render(<PromptContentView
      content={{ parts: [images[0], { type: 'text', text: 'between' }, images[1], images[2]] }}
      variant="message" onReadImage={read}
    />);
    const body = view.container.querySelector<HTMLElement>('.prompt-content-body')!;
    const strip = view.container.querySelector<HTMLElement>('.prompt-thumbnail-strip')!;
    expect(strip.nextElementSibling).toBe(body);
    expect(within(body).getAllByRole('button', { name: /^打开 Figure [123]$/ })).toHaveLength(3);
    expect(body.textContent).toBe('Figure 1betweenFigure 2Figure 3');
    const thumbnail = within(strip).getByRole('button', { name: '打开 Figure 1' });
    const more = within(strip).getByRole('button', { name: '+2 张' });
    expect(read).not.toHaveBeenCalled();

    await userEvent.click(thumbnail);
    await waitFor(() => expect(read).toHaveBeenCalledWith(images[0]));
    expect(document.querySelector('.yarl__slide_current .prompt-lightbox-caption')?.textContent).toBe('Figure 1');
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(document.activeElement).toBe(thumbnail));

    const chip = within(body).getByRole('button', { name: '打开 Figure 1' });
    chip.focus(); await userEvent.keyboard('{Enter}');
    expect(read).toHaveBeenCalledTimes(1);
    expect(document.querySelector('.yarl__slide_current .prompt-lightbox-caption')?.textContent).toBe('Figure 1');
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(document.activeElement).toBe(chip));

    await userEvent.click(more);
    await waitFor(() => expect(read).toHaveBeenCalledWith(images[1]));
    expect(read).toHaveBeenCalledTimes(2);
    expect(document.querySelector('.yarl__slide_current .prompt-lightbox-caption')?.textContent).toBe('Figure 2');
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
  });

  it('keeps a failed occurrence numbered and retries only that exact owner reference', async () => {
    const image = canonicalImage(0, 'd', { kind: 'queue', queueItemId: 'queue-fail' });
    const read = vi.fn()
      .mockRejectedValueOnce(new Error('原图读取失败'))
      .mockResolvedValueOnce(Uint8Array.from([1, 2, 3]));
    const view = render(<PromptContentView
      content={{ parts: [image] }}
      variant="queue"
      onReadImage={read}
    />);

    await userEvent.click(screen.getByRole('button', { name: '打开 Figure 1' }));
    expect(await screen.findByText('原图读取失败')).toBeTruthy();
    expect(view.container.querySelector('.prompt-image-chip > span')?.textContent).toBe('Figure 1');
    await userEvent.click(screen.getByRole('button', { name: '重新读取' }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(read.mock.calls[1]?.[0]).toBe(image);
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
  });

  it('does not turn observer refreshes into automatic retries after an opened image read fails', async () => {
    vi.stubGlobal('IntersectionObserver', undefined);
    const image = canonicalImage(0, 'f');
    const read = vi.fn()
      .mockRejectedValueOnce(new Error('原图读取失败'))
      .mockResolvedValueOnce(Uint8Array.from([1, 2, 3]));
    const view = render(<PromptContentView
      content={{ parts: [image] }}
      variant="message"
      onReadImage={read}
    />);

    await waitFor(() => expect(screen.getByText('读取失败')).toBeTruthy());
    expect(screen.getByText('重试')).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(1);
    view.rerender(<PromptContentView
      content={{ parts: [image] }}
      variant="message"
      onReadImage={async (part) => read(part)}
    />);
    await Promise.resolve();
    expect(read).toHaveBeenCalledTimes(1);

    const body = view.container.querySelector<HTMLElement>('.prompt-content-body')!;
    await userEvent.click(within(body).getByRole('button', { name: '打开 Figure 1' }));
    expect(await screen.findByText('原图读取失败')).toBeTruthy();
    expect(read).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole('button', { name: '重新读取' }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
  });

  it('does not create or install an object URL when an owner read finishes after unmount', async () => {
    const pending = deferred<Uint8Array>();
    const read = vi.fn(() => pending.promise);
    const create = vi.spyOn(URL, 'createObjectURL');
    const view = render(<PromptContentView
      content={{ parts: [canonicalImage(0, 'e')] }}
      variant="queue"
      onReadImage={read}
    />);
    await userEvent.click(screen.getByRole('button', { name: '打开 Figure 1' }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));

    view.unmount();
    pending.resolve(Uint8Array.from([3, 2, 1]));
    await pending.promise;
    await Promise.resolve();

    expect(create).not.toHaveBeenCalled();
  });

  it('keeps a local frozen candidate URL alive until its display owner unmounts', async () => {
    const create = vi.spyOn(URL, 'createObjectURL');
    const revoke = vi.spyOn(URL, 'revokeObjectURL');
    const content: EditablePromptContent = {
      parts: [{
        type: 'image',
        source: 'local',
        bytes: Uint8Array.from([6, 5, 4]),
        declaredMediaType: 'image/png',
      }],
    };
    const view = render(<PromptContentView
      content={content}
      variant="queue"
      onReadImage={async () => { throw new Error('not canonical'); }}
    />);
    await userEvent.click(screen.getByRole('button', { name: '打开 Figure 1' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(revoke).not.toHaveBeenCalled();

    view.unmount();
    expect(revoke).toHaveBeenCalledTimes(1);
  });
});
