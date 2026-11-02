// A physical browser-work queue, never a limit on the number of gallery items.
let queue: Promise<unknown> = Promise.resolve();
export function readThumbnailInOrder<T>(read: () => Promise<T>, signal: AbortSignal): Promise<T> {
  const pending = queue.then(() => { signal.throwIfAborted(); return read(); });
  queue = pending.catch(() => undefined);
  return pending;
}
