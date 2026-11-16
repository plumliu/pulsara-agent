import { beforeEach, vi } from 'vitest';
import { initializeUiPreferences } from '../../frontend/lib/ui-preferences';
// jsdom has no layout; tests control whether the session menu anchor is clipped.
const sessionMenuClipping = vi.hoisted(() => ({ hidden: false }));
export { sessionMenuClipping };
vi.mock('@floating-ui/dom', async importOriginal => {
  const original = await importOriginal<typeof import('@floating-ui/dom')>();
  return { ...original, hide: (...args: Parameters<typeof original.hide>) => {
    const middleware = original.hide(...args);
    return { ...middleware, fn: (state: Parameters<typeof middleware.fn>[0]) =>
      state.elements.reference instanceof HTMLElement && state.elements.reference.matches('.session-row__menu > summary')
        ? { data: { referenceHidden: sessionMenuClipping.hidden } } : middleware.fn(state) };
  } };
});
beforeEach(() => initializeUiPreferences({}));

if (!Range.prototype.getClientRects) {
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
}

if (!Range.prototype.getBoundingClientRect) {
  Range.prototype.getBoundingClientRect = () => new DOMRect();
}

if (!document.elementFromPoint) {
  document.elementFromPoint = () => document.querySelector('[contenteditable="true"]');
}

// jsdom has no native scrolling; tests that verify movement supply their own spy.
if (!HTMLElement.prototype.scrollBy) {
  HTMLElement.prototype.scrollBy = () => undefined;
}

let objectUrlSequence = 0;
if (!URL.createObjectURL) {
  URL.createObjectURL = () => `blob:pulsara-test:${objectUrlSequence += 1}`;
}
if (!URL.revokeObjectURL) {
  URL.revokeObjectURL = () => undefined;
}
