/** Browser-only semantic positions; these never represent canonical read coverage. */
export interface HistoryReadingAnchor {
  entryId: string;
  traceId?: string;
  sourceOffset?: number;
  header?: boolean;
  processEdge?: string;
  top: number;
}

export function hasHistorySelection(root: HTMLElement): boolean {
  const selection = root.ownerDocument.getSelection();
  if (!selection || selection.isCollapsed) return false;
  for (let index = 0; index < selection.rangeCount; index++) {
    if (selection.getRangeAt(index).intersectsNode(root)) return true;
  }
  return false;
}

/** Same 200px preparation margin as immutable-body hydration. Selection and
 * focus are browser-owned interactions and may temporarily span many pages. */
export function collectHistoryResidence(root: HTMLElement): {visibleEntryIds: string[]; pinnedEntryIds: string[]} {
  const visible = new Set<string>(), pinned = new Set<string>();
  const viewport = root.getBoundingClientRect();
  if (!viewport.height || root.closest('[hidden], [inert]')) return {visibleEntryIds: [], pinnedEntryIds: []};
  const selection = root.ownerDocument.getSelection();
  const ids = (node: HTMLElement) => [node.dataset.sourceEntry, ...node.dataset.sourceEntries?.split(' ') ?? [], node.dataset.resultEntry, node.dataset.historyEntry].filter((id): id is string => Boolean(id));
  for (const node of root.querySelectorAll<HTMLElement>('[data-source-entry], [data-result-entry], [data-history-entry]')) {
    if (node.closest('[hidden], [inert]')) continue;
    const rect = node.getBoundingClientRect();
    if (rect.height && rect.bottom >= viewport.top - 200 && rect.top <= viewport.bottom + 200) for (const id of ids(node)) visible.add(id);
    if (!node.hasAttribute('data-process-toggle') && node.contains(root.ownerDocument.activeElement)) for (const id of ids(node)) pinned.add(id);
    if (selection && !selection.isCollapsed) for (let index = 0; index < selection.rangeCount; index++) {
      if (selection.getRangeAt(index).intersectsNode(node)) for (const id of ids(node)) pinned.add(id);
    }
  }
  const focused = root.ownerDocument.activeElement;
  if (focused instanceof HTMLElement && root.contains(focused) && !focused.closest('[hidden], [inert], [data-process-toggle]')) {
    // The run's own disclosure precedes its message hosts. Keep its semantic
    // first host while focused, rather than every entry in an expanded run.
    const host = focused.closest<HTMLElement>('[data-source-entry]')
      ?? focused.closest('.conversation-run')?.querySelector<HTMLElement>('[data-source-entry]');
    if (host) for (const id of ids(host)) pinned.add(id);
  }
  return {visibleEntryIds: [...visible], pinnedEntryIds: [...pinned]};
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
  for (const selector of ['[data-source-text]', '[data-result-entry]', '[data-source-entry]', '[data-history-entry]']) {
    for (const candidate of root.querySelectorAll<HTMLElement>(selector)) {
      if (candidate.closest('[inert], [hidden]')) continue;
      const node = visibleHost(candidate);
      const rect = node.getBoundingClientRect();
      if (rect.bottom <= viewport.top || rect.top >= viewport.bottom || !rect.height) continue;
      const entryId = candidate.dataset.historyEntry ?? candidate.dataset.resultEntry ?? candidate.closest<HTMLElement>('[data-source-entry]')?.dataset.sourceEntry;
      if (!entryId) continue;
      const mapping = candidate.dataset.sourceText && JSON.parse(candidate.dataset.sourceText) as number[][];
      return { entryId, traceId: candidate.dataset.resultEntry, sourceOffset: mapping ? mapping[0]?.[0] : undefined,
        ...(candidate.dataset.historyEntry ? {header: true, processEdge: candidate.dataset.processEdge} : {}), top: rect.top - viewport.top };
    }
  }
}

export function restoreHistoryAnchor(root: HTMLElement, anchor: HistoryReadingAnchor): number | undefined {
  const headers = anchor.header ? [...root.querySelectorAll<HTMLElement>('[data-history-entry]')].filter(node => !node.closest('[hidden], [inert]') && (!anchor.processEdge || node.dataset.processEdge === anchor.processEdge)) : [];
  let node = anchor.header ? headers.find(node => node.dataset.historyEntry === anchor.entryId)
    ?? headers.find(node => node.dataset.historyEntryAliases?.split(' ').includes(anchor.entryId))
    : findHistoryEntry(root, anchor.traceId ?? anchor.entryId);
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
