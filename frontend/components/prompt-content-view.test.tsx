import { cleanup, render, screen, waitFor } from '@testing-library/react';
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
  it('derives queue Figure links from occurrences without reading image payloads', async () => {
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
    expect(screen.getAllByRole('button', { name: /\[Figure [12]\]/ })).toHaveLength(2);
    expect(document.querySelector('.prompt-content-text')?.textContent)
      .toBe('literal [Figure 1] and ');
    expect(read).not.toHaveBeenCalled();

    const trigger = screen.getByRole('button', { name: '[Figure 2]' });
    await userEvent.click(trigger);
    await waitFor(() => expect(read).toHaveBeenCalledWith(second));
    expect(read).toHaveBeenCalledTimes(1);
    expect(await screen.findByText('Figure 2')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Lightbox' })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('keeps the full occurrence numbering when a narrow sent-message strip shows +N', async () => {
    class IdleIntersectionObserver {
      observe() {}
      disconnect() {}
      unobserve() {}
      takeRecords(): IntersectionObserverEntry[] { return []; }
      readonly root = null;
      readonly rootMargin = '0px';
      readonly thresholds = [0];
    }
    vi.stubGlobal('IntersectionObserver', IdleIntersectionObserver);
    const images = [canonicalImage(0, 'a'), canonicalImage(1, 'b'), canonicalImage(2, 'c')];
    const content: CanonicalPromptContent = {
      parts: [images[0], { type: 'text', text: 'one' }, images[1], images[2]],
    };
    const read = vi.fn(async (image: CanonicalPromptImagePart) => (
      Uint8Array.from([image.refOrdinal + 1])
    ));
    const view = render(<PromptContentView content={content} variant="message" onReadImage={read} />);

    expect(screen.getAllByRole('button', { name: /\[Figure [123]\]/ })).toHaveLength(3);
    const more = await screen.findByRole('button', { name: '+2 张' });
    expect(read).not.toHaveBeenCalled();
    await userEvent.click(more);
    await waitFor(() => expect(read).toHaveBeenCalledWith(images[1]));
    expect(await screen.findByText('Figure 2')).toBeTruthy();
    expect([...view.container.querySelectorAll('.prompt-figure-link')]
      .map((item) => item.textContent)).toEqual(['[Figure 1]', '[Figure 2]', '[Figure 3]']);
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

    await userEvent.click(screen.getByRole('button', { name: '[Figure 1]' }));
    expect(await screen.findByText('原图读取失败')).toBeTruthy();
    expect(view.container.querySelector('.prompt-figure-link')?.textContent).toBe('[Figure 1]');
    await userEvent.click(screen.getByRole('button', { name: '重新读取' }));
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(read.mock.calls[1]?.[0]).toBe(image);
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
  });

  it('does not turn observer refreshes into automatic retries after a thumbnail read fails', async () => {
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

    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('重试')).toBeTruthy();
    view.rerender(<PromptContentView
      content={{ parts: [image] }}
      variant="message"
      onReadImage={async (part) => read(part)}
    />);
    await Promise.resolve();
    expect(read).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole('button', { name: '打开 Figure 1' }));
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
    await userEvent.click(screen.getByRole('button', { name: '[Figure 1]' }));
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
    await userEvent.click(screen.getByRole('button', { name: '[Figure 1]' }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(revoke).not.toHaveBeenCalled();

    view.unmount();
    expect(revoke).toHaveBeenCalledTimes(1);
  });
});
