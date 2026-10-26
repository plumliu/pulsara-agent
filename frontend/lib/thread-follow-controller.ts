import { captureHistoryAnchor, hasHistorySelection, restoreHistoryAnchor, type HistoryReadingAnchor } from './history-dom';

// Product return zone, in CSS pixels. Visibility has its own rounding tolerance.
export const LATEST_FOLLOW_DISTANCE = 48;
export const TAIL_MEASUREMENT_TOLERANCE = 1.5;
type Intent = 'follow' | 'read';
type ViewProtection = { key: string; anchor: HistoryReadingAnchor; closing: boolean; pending: boolean };

/** The main scrollport's only position owner. The reader still owns data and
 * cancellation; this disposable controller owns intent and browser geometry. */
export class ThreadFollowController {
  intent: Intent = 'follow';
  anchor?: HistoryReadingAnchor;
  private views = new Map<string, ViewProtection>();
  private selecting = false;
  private native: 'up' | 'down' | 'drag' | undefined;
  private inputFrame?: number;
  private animationFrame?: number;
  private last?: { top: number; height: number; viewport: number };
  private writtenTop?: number;
  private disposed = false;
  private navigating = false;

  constructor(private root: () => HTMLElement | null, private containsLatest: () => boolean,
    private publishVisibility: (visible: boolean) => void, private onInput: () => void,
    private onSettled: () => void) {}

  get userScrolling() { return this.native !== undefined; }
  get hasViewProtection() { return this.views.size > 0; }
  get residenceAnchor() { return [...this.views.values()].at(-1)?.anchor ?? this.anchor; }
  get protectedEntryIds() { return [...new Set([this.anchor?.entryId, ...[...this.views.values()].map(view => view.anchor.entryId)].filter((id): id is string => Boolean(id)))]; }
  get canFollow() {
    const root = this.root();
    const active = root?.ownerDocument.activeElement;
    const editing = active instanceof HTMLElement && root?.contains(active)
      && Boolean(active.closest('input, textarea, select, [contenteditable="true"]'));
    return this.intent === 'follow' && this.containsLatest() && !this.native && !this.navigating
      && !this.views.size && !this.selecting && !editing && !(root && hasHistorySelection(root));
  }

  private tailDistance(root: HTMLElement) {
    const end = root.querySelector<HTMLElement>('[data-thread-tail-end]');
    const viewport = root.getBoundingClientRect();
    // The content marker excludes decorative padding; a missing/unlaid-out
    // marker falls back to the physical range until content is installed.
    const rect = end?.getBoundingClientRect();
    return viewport.height && rect && end!.getClientRects().length
      ? rect.bottom - (viewport.top + root.clientTop + root.clientHeight)
      : root.scrollHeight - root.scrollTop - root.clientHeight;
  }

  private publish() {
    const root = this.root();
    if (root) {
      this.publishVisibility(this.containsLatest() && this.tailDistance(root) <= TAIL_MEASUREMENT_TOLERANCE);
      this.last = { top: root.scrollTop, height: root.scrollHeight, viewport: root.clientHeight };
    }
  }

  private controls(root: HTMLElement, key: string) {
    return [...root.querySelectorAll<HTMLElement>('[data-disclosure-key]')]
      .filter(node => node.dataset.disclosureKey === key);
  }

  private animations(root: HTMLElement) {
    return (root.getAnimations?.({ subtree: true }) ?? [])
      .filter(animation => {
        const target = (animation.effect as KeyframeEffect | null)?.target;
        return target instanceof Element && target.matches('.animated-disclosure')
          && (animation.playState === 'running' || animation.pending);
      });
  }

  coordinate = () => {
    const root = this.root();
    if (!root || this.disposed) return;
    const animations = this.animations(root);
    for (const [key, view] of this.views) {
      const controls = this.controls(root, key);
      if (!view.pending && (!controls.length || controls.every(node => node.closest('[hidden], [inert]'))
        || (view.closing && controls.every(node => node.getAttribute('aria-expanded') === 'false') && !animations.length))) {
        this.views.delete(key);
      }
    }
    if (!this.native && !this.navigating) {
      const before = root.scrollTop;
      const view = [...this.views.values()].at(-1);
      if (this.canFollow) {
        if (Math.abs(root.scrollTop - Math.max(0, root.scrollHeight - root.clientHeight)) > .01) root.scrollTop = root.scrollHeight;
        this.anchor = undefined;
      } else {
        const anchor = view?.anchor ?? this.anchor;
        if (anchor) restoreHistoryAnchor(root, anchor);
      }
      if (root.scrollTop !== before) this.writtenTop = root.scrollTop;
    }
    this.publish();
    if (animations.length && this.animationFrame === undefined) {
      this.animationFrame = requestAnimationFrame(() => {
        this.animationFrame = undefined;
        this.coordinate();
        this.onSettled();
      });
    }
  };

  read() {
    this.native = undefined;
    if (this.inputFrame !== undefined) { cancelAnimationFrame(this.inputFrame); this.inputFrame = undefined; }
    this.intent = 'read';
    this.views.clear();
    const root = this.root();
    this.anchor = root ? captureHistoryAnchor(root) : undefined;
    this.publish();
  }

  input(direction: 'up' | 'down' | 'drag') {
    if (this.disposed) return;
    this.onInput();
    this.navigating = false;
    this.read();
    this.native = direction;
    this.writtenTop = undefined;
    if (this.inputFrame !== undefined) cancelAnimationFrame(this.inputFrame);
    if (direction !== 'drag') this.inputFrame = requestAnimationFrame(() => {
      this.inputFrame = undefined;
      this.endInput();
    });
  }

  endInput() {
    const direction = this.native;
    this.native = undefined;
    const root = this.root();
    if (!root || this.disposed) return;
    this.anchor = captureHistoryAnchor(root) ?? this.anchor;
    if (direction === 'down' && this.containsLatest() && !hasHistorySelection(root)
      && this.tailDistance(root) <= LATEST_FOLLOW_DISTANCE) this.intent = 'follow';
    this.coordinate();
    this.onSettled();
  }

  scroll() {
    const root = this.root();
    if (!root || this.disposed) return;
    const previous = this.last;
    const moved = previous ? root.scrollTop - previous.top : 0;
    const programmed = this.writtenTop !== undefined && Math.abs(root.scrollTop - this.writtenTop) < .01;
    this.writtenTop = undefined;
    const clamped = previous && (previous.height !== root.scrollHeight || previous.viewport !== root.clientHeight)
      && Math.abs(root.scrollTop - Math.max(0, root.scrollHeight - root.clientHeight)) < .01;
    if (!programmed && !clamped && (moved || this.tailDistance(root) > TAIL_MEASUREMENT_TOLERANCE)) {
      // Covers native scrollbar paths without DOM pointerdown. Unknown movement
      // yields to reading rather than letting the next token steal the viewport.
      const direction = moved > 0 ? 'down' : 'up';
      const dragging = this.native === 'drag';
      this.onInput();
      this.navigating = false;
      this.read();
      this.native = dragging ? 'drag' : direction;
      if (this.inputFrame !== undefined) cancelAnimationFrame(this.inputFrame);
      if (!dragging) this.inputFrame = requestAnimationFrame(() => { this.inputFrame = undefined; this.endInput(); });
    }
    if (this.native === 'down' && this.containsLatest() && !hasHistorySelection(root)
      && this.tailDistance(root) <= LATEST_FOLLOW_DISTANCE) {
      this.native = undefined;
      this.intent = 'follow';
      this.coordinate();
    } else {
      if (!this.canFollow) this.anchor = captureHistoryAnchor(root) ?? this.anchor;
      this.publish();
    }
  }

  focusChanged = () => {
    const root = this.root();
    const focused = root?.ownerDocument.activeElement;
    if (root && focused instanceof HTMLElement && root.contains(focused)
      && focused.closest('input, textarea, select, [contenteditable="true"]')) this.anchor ??= captureHistoryAnchor(root);
    this.coordinate();
  };

  selectionChanged() {
    const root = this.root();
    if (!root) return;
    const selecting = hasHistorySelection(root);
    if (selecting && !this.selecting) {
      this.onInput();
      this.navigating = false;
      this.anchor = captureHistoryAnchor(root);
    }
    this.selecting = selecting;
    this.coordinate();
    this.onSettled();
  }

  disclose(button: HTMLElement) {
    const root = this.root();
    const key = button.dataset.disclosureKey;
    if (!root || !key) return;
    if (button.getAttribute('aria-busy') === 'true') {
      const previous = this.views.get(key);
      if (previous?.closing) this.views.set(key, {...previous, closing: false, pending: false});
      else this.views.delete(key);
      return;
    }
    const run = button.closest<HTMLElement>('.conversation-run');
    const host = button.closest('[data-source-entry]');
    const lastProcessHost = [...run?.querySelectorAll('[data-source-entry]') ?? []]
      .filter(node => node.closest('.conversation-run__drawer')).at(-1);
    const tail = this.containsLatest() && run?.dataset.threadTail === 'true'
      && (button.hasAttribute('data-process-toggle') || host === lastProcessHost);
    if (this.intent === 'follow' && tail && !this.views.size && !this.selecting) return;
    const entryId = button.dataset.historyEntry ?? host?.getAttribute('data-source-entry');
    if (!entryId) return;
    this.views.set(key, { key, closing: button.getAttribute('aria-expanded') === 'true', pending: false,
      anchor: { entryId, disclosureKey: key, processEdge: button.dataset.processEdge,
        top: button.getBoundingClientRect().top - root.getBoundingClientRect().top } });
    this.anchor ??= captureHistoryAnchor(root);
  }

  pendingDisclosure(key: string | undefined, pending: boolean, failed = false, expected?: ViewProtection) {
    const view = key ? this.views.get(key) : undefined;
    if (!view || (expected && expected !== view)) return;
    view.pending = pending;
    if (failed) {
      if (view.closing) view.closing = false;
      else this.views.delete(view.key);
    }
    this.coordinate();
    return view;
  }

  beginNavigation() { this.navigating = true; }
  endNavigation() { this.navigating = false; this.coordinate(); }
  latest() {
    if (this.disposed || !this.containsLatest()) return;
    this.navigating = false;
    this.intent = 'follow';
    this.views.clear();
    this.anchor = undefined;
    this.native = undefined;
    this.coordinate();
  }
  center(target: HTMLElement) {
    const root = this.root();
    if (!root || this.disposed) return;
    target.scrollIntoView({ block: 'center', behavior: 'instant' });
    this.writtenTop = root.scrollTop;
    this.anchor = captureHistoryAnchor(root);
    this.publish();
  }
  activate() { this.disposed = false; }
  dispose() {
    this.disposed = true;
    if (this.inputFrame !== undefined) cancelAnimationFrame(this.inputFrame);
    if (this.animationFrame !== undefined) cancelAnimationFrame(this.animationFrame);
    this.inputFrame = this.animationFrame = undefined;
  }
}
