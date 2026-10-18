/** Browser-only semantic positions; these never represent canonical read coverage. */
export interface HistoryReadingAnchor {
  entryId: string;
  traceId?: string;
  sourceOffset?: number;
  top: number;
}

function visibleHost(element: HTMLElement): HTMLElement {
  return element.dataset.sourceEntry ? element.firstElementChild as HTMLElement ?? element : element;
}

export function findHistoryEntry(root: HTMLElement, entryId: string): HTMLElement | undefined {
  const trace = [...root.querySelectorAll<HTMLElement>('[data-result-entry]')]
    .find(node => node.dataset.resultEntry === entryId && !node.closest('[inert], [hidden]'));
  if (trace) return trace;
  const entries = [...root.querySelectorAll<HTMLElement>('[data-source-entry]')].filter(node => !node.closest('[inert], [hidden]'));
  const entry = entries.find(node => node.dataset.sourceEntry === entryId)
    ?? entries.find(node => node.dataset.sourceEntries?.split(' ').includes(entryId));
  return entry && visibleHost(entry);
}

export function waitForHistoryEntry(root: HTMLElement, entryId: string, signal: AbortSignal): Promise<HTMLElement> {
  return new Promise((resolve, reject) => {
    const observer = new MutationObserver(check);
    const cleanup = () => { observer.disconnect(); signal.removeEventListener('abort', abort); };
    const abort = () => { cleanup(); reject(new DOMException('定位已取消', 'AbortError')); };
    function check() {
      if (signal.aborted) { abort(); return; }
      const target = findHistoryEntry(root, entryId);
      if (target?.isConnected && !target.closest('[data-content-state="loading"], [data-content-state="unloaded"], [data-content-state="error"]')) { cleanup(); resolve(target); }
    }
    observer.observe(root, { childList: true, subtree: true, attributes: true });
    signal.addEventListener('abort', abort, { once: true });
    check();
  });
}

export function captureHistoryAnchor(root: HTMLElement): HistoryReadingAnchor | undefined {
  const viewport = root.getBoundingClientRect();
  // Prefer a text/trace subanchor over the outer message whose height can change.
  for (const selector of ['[data-source-text]', '[data-result-entry]', '[data-source-entry]']) {
    for (const candidate of root.querySelectorAll<HTMLElement>(selector)) {
      if (candidate.closest('[inert], [hidden]')) continue;
      const node = visibleHost(candidate);
      const rect = node.getBoundingClientRect();
      if (rect.bottom <= viewport.top || rect.top >= viewport.bottom || !rect.height) continue;
      const entryId = candidate.dataset.resultEntry ?? candidate.closest<HTMLElement>('[data-source-entry]')?.dataset.sourceEntry;
      if (!entryId) continue;
      const mapping = candidate.dataset.sourceText && JSON.parse(candidate.dataset.sourceText) as number[][];
      return { entryId, traceId: candidate.dataset.resultEntry, sourceOffset: mapping ? mapping[0]?.[0] : undefined, top: rect.top - viewport.top };
    }
  }
}

export function restoreHistoryAnchor(root: HTMLElement, anchor: HistoryReadingAnchor): number | undefined {
  let node = findHistoryEntry(root, anchor.traceId ?? anchor.entryId);
  if (!node) return;
  if (anchor.sourceOffset !== undefined) {
    const offset = anchor.sourceOffset;
    node = [...node.querySelectorAll<HTMLElement>('[data-source-text]')].find(candidate => {
      const mapping = JSON.parse(candidate.dataset.sourceText!) as number[][];
      return mapping.some(([start, end]) => start <= offset && offset < end);
    }) ?? node;
  }
  const delta = node.getBoundingClientRect().top - root.getBoundingClientRect().top - anchor.top;
  if (Math.abs(delta) > 0.5) root.scrollTop += delta;
  return root.scrollTop;
}
