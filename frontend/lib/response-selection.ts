import type { PromptAnnotationPart, PromptAnnotationSource } from './prompt-content';

const bodies = new WeakMap<HTMLElement, string>();
const bodyListeners = new Set<() => void>();

/** Resolve only after React has registered the actual, verified source body. */
export function waitForAnnotationBody(entryId: string, signal: AbortSignal): Promise<HTMLElement> {
  return new Promise((resolve, reject) => {
    const cleanup = () => { bodyListeners.delete(check); signal.removeEventListener('abort', abort); };
    const abort = () => { cleanup(); reject(new DOMException('定位已取消', 'AbortError')); };
    const check = () => {
      if (signal.aborted) { abort(); return; }
      const root = [...document.querySelectorAll<HTMLElement>('[data-annotation-entry]')]
        .find(node => node.dataset.annotationEntry === entryId && node.isConnected && bodies.has(node));
      if (root) { cleanup(); resolve(root); }
    };
    bodyListeners.add(check);
    signal.addEventListener('abort', abort, { once: true });
    check();
  });
}
export function registerAnnotationBody(element: HTMLElement, body: string): () => void {
  bodies.set(element, body);
  for (const listener of bodyListeners) listener();
  return () => { bodies.delete(element); };
}

function element(node: globalThis.Node | null): Element | null {
  return node instanceof Element ? node : node?.parentElement ?? null;
}
function intersects(range: Range, node: globalThis.Node): boolean {
  const other = document.createRange(); other.selectNode(node);
  return range.compareBoundaryPoints(Range.END_TO_START, other) < 0
    && range.compareBoundaryPoints(Range.START_TO_END, other) > 0;
}

/** Actual text-content intersection also handles element-container endpoints. */
function selectedTextOffsets(range: Range, text: Text): [number, number] | undefined {
  const contents = document.createRange(); contents.selectNodeContents(text);
  if (range.compareBoundaryPoints(Range.END_TO_START, contents) >= 0
    || range.compareBoundaryPoints(Range.START_TO_END, contents) <= 0) return;
  const from = range.startContainer === text ? range.startOffset : 0;
  const to = range.endContainer === text ? range.endOffset : text.length;
  return from < to ? [from, to] : undefined;
}

function atomIsSelected(range: Range, atom: HTMLElement): boolean {
  const walker = document.createTreeWalker(atom, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (element(node)?.closest('[data-source-decoration]')) continue;
    if (node instanceof Text && selectedTextOffsets(range, node)) return true;
    if (node instanceof Element && node.matches('img,svg,canvas,br') && intersects(range, node)) return true;
  }
  return false;
}

export interface MappedSelection { annotation: PromptAnnotationPart; range: Range; root: HTMLElement }

function emptyResponseBoundary(range: Range): boolean {
  const contents = range.cloneContents();
  if (contents.textContent?.trim()
    || contents.querySelector('[data-annotation-entry],[data-annotation-exclude],iframe,input,textarea,[contenteditable="true"],.execution-rail,.reasoning-disclosure,.subagent-group,.user-turn')) return false;
  // Only the surrounding reply controls may contribute non-text decoration.
  return [...contents.querySelectorAll('img,svg,canvas,video,audio,object,embed')]
    .every(node => node.closest('.assistant-heading,.conversation-run__toggle,.response-actions'));
}

export function mapResponseSelection(selection: Selection | null): MappedSelection | undefined {
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return;
  const range = selection.getRangeAt(0).cloneRange();
  const startRoot = element(range.startContainer)?.closest<HTMLElement>('[data-annotation-entry]');
  const endRoot = element(range.endContainer)?.closest<HTMLElement>('[data-annotation-entry]');
  if (startRoot && endRoot && startRoot !== endRoot) return;
  let root = startRoot ?? endRoot;
  if (!root) {
    const common = element(range.commonAncestorContainer);
    const owner = common?.closest('.conversation-run') ?? common?.closest('.assistant-turn') ?? common?.closest('.assistant-copy');
    const candidates = [...owner?.querySelectorAll<HTMLElement>('[data-annotation-entry]') ?? []]
      .filter(node => intersects(range, node));
    if (candidates.length !== 1) return;
    root = candidates[0];
  }
  if (!root) return;
  // Blank margins above and below a reply can place native endpoints on the
  // process toggle, heading or footer. Validate both boundary slices within the
  // same reply owner, then trim them symmetrically without changing Selection.
  if (!startRoot || !endRoot) {
    const owner = root.closest('.conversation-run') ?? root.closest('.assistant-turn') ?? root.closest('.assistant-copy');
    if (!owner?.contains(range.startContainer) || !owner.contains(range.endContainer)) return;
    if (!startRoot) {
      const leading = range.cloneRange(); leading.setEndBefore(root);
      if (!emptyResponseBoundary(leading)) return;
      range.setStart(root, 0);
    }
    if (!endRoot) {
      const trailing = range.cloneRange(); trailing.setStartAfter(root);
      if (!emptyResponseBoundary(trailing)) return;
      range.setEnd(root, root.childNodes.length);
    }
  }
  const start = element(range.startContainer);
  const end = element(range.endContainer);
  if (start?.closest('input,textarea,[contenteditable="true"],[data-source-decoration]')
    && end?.closest('input,textarea,[contenteditable="true"],[data-source-decoration]') === start.closest('input,textarea,[contenteditable="true"],[data-source-decoration]')) return;
  const body = bodies.get(root);
  if (body === undefined) return;
  if ([...root.querySelectorAll('[data-annotation-exclude], iframe')].some(node => intersects(range, node))) return;
  const segments: Array<[number, number]> = [];
  const seenAtoms = new Set<HTMLElement>();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = element(node);
    if (parent?.closest('[data-source-decoration]')) continue;
    const atom = parent?.closest<HTMLElement>('[data-source-atom]');
    if (atom) {
      if (!seenAtoms.has(atom) && atomIsSelected(range, atom)) {
        seenAtoms.add(atom);
        const [from, to] = atom.dataset.sourceAtom!.split(':').map(Number);
        segments.push([from, to]);
      }
      continue;
    }
    if (!(node instanceof Text)) continue;
    const offsets = selectedTextOffsets(range, node);
    if (!offsets) continue;
    // Coverage is complete: selected text with no exact source mapping fails
    // the entire range, including unknown/generated or unsupported content.
    if (parent?.closest('[data-source-invalid]')) return;
    const leaf = node.parentElement;
    if (!leaf?.hasAttribute('data-source-text') || leaf.childNodes.length !== 1) return;
    const mapping = JSON.parse(leaf.getAttribute('data-source-text')!) as number[][];
    const [from, to] = offsets;
    const first = mapping[from]; const last = mapping[to - 1];
    if (!first || !last) return;
    if (first[0] < last[1]) segments.push([first[0], last[1]]);
  }
  if (!segments.length) return;
  // Reordered or duplicated generated source is not a contiguous quote.
  for (let index = 1; index < segments.length; index++) if (segments[index][0] < segments[index - 1][1]) return;
  const from = segments[0][0]; const to = segments.at(-1)![1];
  if (!Number.isSafeInteger(from) || !Number.isSafeInteger(to) || from < 0 || to > body.length || from >= to) return;
  return { annotation: { type: 'annotation', quote: body.slice(from, to), source: { entry_id: root.dataset.annotationEntry!, start: from, end: to } }, range: range.cloneRange(), root };
}

/** Reconstruct source ranges without wrapping or rewriting rendered content. */
export function annotationSourceRange(root: HTMLElement, source: PromptAnnotationSource): Range | undefined {
  let result: Range | undefined;
  for (const node of root.querySelectorAll<HTMLElement>('[data-source-text],[data-source-atom]')) {
    if (node.parentElement?.closest('[data-source-atom]')) continue;
    const part = document.createRange();
    if (node.hasAttribute('data-source-atom')) {
      const [start, end] = node.dataset.sourceAtom!.split(':').map(Number);
      if (end <= source.start || start >= source.end) continue;
      part.selectNode(node);
    } else {
      if (!(node.firstChild instanceof Text) || node.childNodes.length !== 1) continue;
      const mapping = JSON.parse(node.dataset.sourceText!) as number[][];
      const from = mapping.findIndex(([start, end]) => start < source.end && end > source.start);
      const to = mapping.findLastIndex(([start, end]) => start < source.end && end > source.start);
      if (from < 0) continue;
      part.setStart(node.firstChild, from); part.setEnd(node.firstChild, to + 1);
    }
    if (!result) result = part;
    else result.setEnd(part.endContainer, part.endOffset);
  }
  return result;
}

export function showAnnotationHighlight(root: HTMLElement, source: PromptAnnotationSource, name: 'annotation-source' | 'annotation-active' | 'annotation-hover'): () => void {
  const range = annotationSourceRange(root, source);
  const css = globalThis.CSS as typeof CSS & { highlights?: Map<string, unknown> };
  const Highlight = (globalThis as typeof globalThis & { Highlight?: new (...ranges: Range[]) => unknown }).Highlight;
  const highlight = range && Highlight ? new Highlight(range) : undefined;
  if (highlight) css?.highlights?.set(name, highlight);
  const atoms = [...root.querySelectorAll<HTMLElement>('[data-source-atom]')].filter(node => {
    const [start, end] = node.dataset.sourceAtom!.split(':').map(Number);
    return start < source.end && end > source.start;
  });
  for (const node of atoms) node.classList.add(name);
  return () => {
    if (highlight && css?.highlights?.get(name) === highlight) css.highlights.delete(name);
    for (const node of atoms) node.classList.remove(name);
  };
}

export function highlightAnnotationSource(root: HTMLElement, source: PromptAnnotationSource): () => void {
  const body = bodies.get(root);
  const range = annotationSourceRange(root, source);
  if (body === undefined || source.start < 0 || source.end > body.length || source.start >= source.end
    || !range) throw new Error('引用原文范围无法在已读取正文中定位。');
  const thread = root.closest<HTMLElement>('.thread-scroll');
  const firstRect = [...range.getClientRects()].find(rect => rect.height > 0);
  if (thread && firstRect) {
    // Center the first quoted line, so even a quote spanning a long reply
    // starts inside the viewport. Native scroll events keep the reading anchor
    // in sync with this precise position after the entry-level locator settles.
    const viewportTop = thread.getBoundingClientRect().top + thread.clientTop;
    thread.scrollTop = Math.max(0, thread.scrollTop + firstRect.top - viewportTop
      - Math.max(0, thread.clientHeight - Math.min(firstRect.height, thread.clientHeight)) / 2);
  } else {
    const start = range.startContainer instanceof Element
      ? range.startContainer.childNodes[range.startOffset] ?? range.startContainer : range.startContainer;
    element(start)?.scrollIntoView({ block: 'center', behavior: 'instant' });
  }
  const cleanup = showAnnotationHighlight(root, source, 'annotation-source');
  const timer = window.setTimeout(cleanup, 3500);
  return () => { window.clearTimeout(timer); cleanup(); };
}
