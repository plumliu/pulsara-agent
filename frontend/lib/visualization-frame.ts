import visualizationRoot from '../../src/pulsara_agent/conversation_kernel/visualization_root.js';

export interface VisualizationRootRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export const visualizationLayoutMessageType = 'pulsara-visualization-layout';

// The browser owns HTML layout. This script reports only geometry from the
// sandboxed document; it never sends source bytes or accepts commands.
export const visualizationFrameMeasurementScript = `<script>
(() => {
  const type = '${visualizationLayoutMessageType}';
  let pending = false;
  let interactionPending = false;
  let hadAutomaticCrop = false;
  let automaticPage = false;
  let measuredViewport = null;
  let observedRoot = null;
  const resize = new ResizeObserver(schedule);
  const observeRoot = (root) => {
    if (root === observedRoot) return;
    if (observedRoot) resize.unobserve(observedRoot);
    observedRoot = root;
    if (root) resize.observe(root);
  };
  const report = () => {
    pending = false;
    const viewport = {width: innerWidth, height: innerHeight};
    // Overflow protection belongs to the viewport that produced it. A retained
    // iframe must reconsider cropping after zoom or responsive layout changes.
    if (!measuredViewport || measuredViewport.width !== viewport.width
      || measuredViewport.height !== viewport.height) {
      automaticPage = false;
      hadAutomaticCrop = false;
      interactionPending = false;
      measuredViewport = viewport;
    }
    const explicit = document.querySelector('[data-pulsara-visualization-root]');
    // Live layout follows actual bounds, not animation presence. Color and
    // contained hover effects must not expand the host's display window.
    const candidate = (${visualizationRoot.toString()})(true);
    const box = candidate?.getBoundingClientRect();
    const fits = box && box.width >= 1 && box.height >= 1 && box.x >= 0 && box.y >= 0
      && box.right <= viewport.width && box.bottom <= viewport.height;
    // A hidden gallery page has no visible candidate; that is not overflow.
    if (!explicit && document.documentElement.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) {
      if (interactionPending && hadAutomaticCrop && !fits) automaticPage = true;
      if (fits) hadAutomaticCrop = true;
    }
    interactionPending = false;
    const root = !explicit && automaticPage ? null : candidate;
    observeRoot(root);
    if (!root) {
      parent.postMessage({ type, mode: 'page', viewport }, '*');
      return;
    }
    if (!root.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) {
      parent.postMessage({ type, mode: 'page', viewport }, '*');
      return;
    }
    const rect = root.getBoundingClientRect();
    parent.postMessage({ type, mode: 'root', viewport, rect: {
      x: rect.x, y: rect.y, width: rect.width, height: rect.height,
    } }, '*');
  };
  function schedule() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(report);
  }
  const observe = () => {
    resize.observe(document.documentElement);
    const mutations = new MutationObserver(schedule);
    mutations.observe(document.documentElement, {
      attributes: true, childList: true, characterData: true, subtree: true,
    });
    schedule();
  };
  const interaction = () => {interactionPending = true; schedule();};
  for (const type of ['pointerover', 'pointerout', 'pointerdown', 'pointerup', 'focusin', 'focusout',
    'input', 'change', 'toggle', 'scroll', 'animationstart', 'animationend', 'animationcancel',
    'transitionrun', 'transitionstart', 'transitionend', 'transitioncancel']) {
    document.addEventListener(type, interaction, true);
  }
  window.addEventListener('resize', schedule);
  window.addEventListener('load', schedule);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', observe, { once: true });
  } else {
    observe();
  }
  if (document.fonts) document.fonts.ready.then(schedule);
})();
</script>`;

export function usableVisualizationRootRect(
  value: unknown,
  viewportWidth: number,
): VisualizationRootRect | null {
  if (!value || typeof value !== 'object') return null;
  const rect = value as Record<string, unknown>;
  const { x, y, width, height } = rect;
  if (![x, y, width, height].every((part) => typeof part === 'number' && Number.isFinite(part))) return null;
  const checked = { x, y, width, height } as VisualizationRootRect;
  if (checked.width < 1 || checked.height < 1) return null;
  if (checked.x < 0 || checked.y < 0) return null;
  if (checked.x + checked.width > viewportWidth) return null;
  return checked;
}
