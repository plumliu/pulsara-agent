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
    const roots = document.querySelectorAll('[data-pulsara-visualization-root]');
    observeRoot(roots.length === 1 ? roots[0] : null);
    if (roots.length !== 1) {
      parent.postMessage({ type, mode: 'page' }, '*');
      return;
    }
    const root = roots[0];
    if (!root.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) {
      parent.postMessage({ type, mode: 'page' }, '*');
      return;
    }
    const rect = root.getBoundingClientRect();
    parent.postMessage({ type, mode: 'root', rect: {
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
  viewportHeight: number,
): VisualizationRootRect | null {
  if (!value || typeof value !== 'object') return null;
  const rect = value as Record<string, unknown>;
  const { x, y, width, height } = rect;
  if (![x, y, width, height].every((part) => typeof part === 'number' && Number.isFinite(part))) return null;
  const checked = { x, y, width, height } as VisualizationRootRect;
  if (checked.width < 1 || checked.height < 1) return null;
  if (checked.x < 0 || checked.y < 0) return null;
  if (checked.x + checked.width > viewportWidth || checked.y + checked.height > viewportHeight) return null;
  return checked;
}
