import type { DesktopBrowser } from './desktop-host';

const overlaySelector = '[role="dialog"], dialog, .menu-popover, .mobile-scrim, .yarl__root';
const gates = new WeakMap<HTMLElement, { owners: Set<object>; visibility: string; focus?: HTMLElement }>();
const intersects = (a: DOMRect, b: DOMRect) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

/** Native children cannot be clipped or covered by DOM. Track this one display slot. */
export function presentBrowser(host: DesktopBrowser, instanceId: string, slot: HTMLElement, onError: (message: string) => void) {
  const presentationId = crypto.randomUUID();
  let alive = true, activated = false, frame = 0, hidden = false, hideConfirmed = false, hidePending = false;
  let last = '';
  const transitions = new Set<string>();
  const overlays = new Set<Element>(document.querySelectorAll(overlaySelector));
  const gated = new Set<HTMLElement>();
  const gateOwner = {};
  const pane = slot.closest('.desktop-workspace');
  const shell = slot.closest('.desktop-shell');
  const releaseGates = () => {
    let restoreFocus: HTMLElement | undefined;
    for (const element of gated) {
      const gate = gates.get(element); if (!gate) continue;
      gate.owners.delete(gateOwner);
      if (!gate.owners.size) {
        element.style.visibility = gate.visibility; gates.delete(element);
        if (element.isConnected && gate.focus?.isConnected) restoreFocus = gate.focus;
      }
    }
    gated.clear();
    if (alive && restoreFocus && (document.activeElement === document.body || document.activeElement === restoreFocus)) restoreFocus.focus({ preventScroll: true });
  };
  const fail = (error: unknown) => { if (alive) onError(error instanceof Error ? error.message : String(error)); };
  function update() {
    if (!alive || !activated) return;
    const rect = slot.getBoundingClientRect();
    const paneRect = pane?.getBoundingClientRect();
    // Use the real hit target, including responsive layouts where it is absent.
    const handle = pane?.querySelector('.workspace-resizer');
    const handleRight = handle && getComputedStyle(handle).display !== 'none' ? handle.getBoundingClientRect().right : rect.left;
    const x = Math.max(rect.left, handleRight);
    const bounds = { x, y: rect.top, width: Math.max(0, rect.right - x), height: rect.height };
    const viewport = new DOMRect(x, rect.top, bounds.width, bounds.height);
    let occluded = false;
    for (const element of overlays) {
      if (!element.isConnected) { overlays.delete(element); continue; }
      if (element instanceof HTMLDialogElement && !element.open) continue;
      const style = getComputedStyle(element);
      if (style.display === 'none' || (style.visibility === 'hidden' && !gates.has(element as HTMLElement)) || style.pointerEvents === 'none') continue;
      const box = element.getBoundingClientRect();
      if (!box.width || !box.height) continue;
      if (element.getAttribute('aria-modal') === 'true' || element.matches('dialog[open],.yarl__root') || intersects(box, viewport)) {
        occluded = true;
        if (!hideConfirmed && element instanceof HTMLElement && !gated.has(element)) {
          const gate = gates.get(element) ?? { owners: new Set<object>(), visibility: element.style.visibility, focus: document.activeElement instanceof HTMLElement && element.contains(document.activeElement) ? document.activeElement : undefined };
          gate.owners.add(gateOwner); gates.set(element, gate); gated.add(element); element.style.visibility = 'hidden';
        }
      }
    }
    const visible = !occluded && bounds.width > 0 && bounds.height > 0 && x < innerWidth && rect.bottom > 0 && (paneRect?.width ?? 0) > 0;
    document.documentElement.style.setProperty('--browser-toast-right', visible ? `${Math.max(18, innerWidth - x + 12)}px` : '18px');
    if (occluded) {
      if (!hidden) {
        hidden = true; hidePending = true; last = '';
        void host.hide({ instanceId, presentationId }).then(() => {
          if (!alive) return;
          hidePending = false; hideConfirmed = true; releaseGates(); schedule();
        }).catch(fail);
      }
      return;
    }
    if (hidePending) return;
    hidden = false; hideConfirmed = false;
    const key = JSON.stringify({ bounds, visible });
    if (key === last) return;
    last = key;
    void host.layout({ instanceId, presentationId, bounds, visible }).catch(fail);
  }
  function tick() { frame = 0; update(); if (transitions.size) schedule(); }
  function schedule() { if (alive && !frame) frame = requestAnimationFrame(tick); }
  const observer = new ResizeObserver(schedule);
  observer.observe(slot); if (pane) observer.observe(pane);
  const mutations = new MutationObserver(records => {
    // Examine changed branches only; no per-frame DOM scan or global geometry polling.
    for (const record of records) {
      if (record.type === 'attributes' && record.target instanceof Element && record.target.matches(overlaySelector)) overlays.add(record.target);
      for (const node of record.addedNodes) if (node instanceof Element) {
        if (node.matches(overlaySelector)) overlays.add(node);
        for (const element of node.querySelectorAll(overlaySelector)) overlays.add(element);
      }
    }
    update();
  });
  mutations.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['open', 'class'] });
  const transitionStart = (event: Event) => {
    const transition = event as TransitionEvent;
    if (transition.target !== pane && transition.target !== shell) return;
    transitions.add(`${transition.target === pane ? 'pane' : 'shell'}:${transition.propertyName}`); schedule();
  };
  const transitionEnd = (event: Event) => {
    const transition = event as TransitionEvent;
    if (transition.target !== pane && transition.target !== shell) return;
    transitions.delete(`${transition.target === pane ? 'pane' : 'shell'}:${transition.propertyName}`); schedule();
  };
  document.addEventListener('transitionrun', transitionStart);
  document.addEventListener('transitionend', transitionEnd);
  document.addEventListener('transitioncancel', transitionEnd);
  window.addEventListener('resize', schedule);
  window.addEventListener('scroll', schedule, true);
  void host.activate({ instanceId, presentationId }).then(() => { if (alive) { activated = true; update(); } }).catch(fail);
  return () => {
    alive = false; cancelAnimationFrame(frame); observer.disconnect(); mutations.disconnect();
    document.removeEventListener('transitionrun', transitionStart); document.removeEventListener('transitionend', transitionEnd); document.removeEventListener('transitioncancel', transitionEnd);
    window.removeEventListener('resize', schedule); window.removeEventListener('scroll', schedule, true);
    document.documentElement.style.removeProperty('--browser-toast-right');
    void host.hide({ instanceId, presentationId }).then(releaseGates).catch(fail);
  };
}
