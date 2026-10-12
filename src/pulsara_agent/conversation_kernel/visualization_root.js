// Shared by the sandboxed display and screenshot worker. Browser layout owns
// geometry; this is a content-layout heuristic, not a painted-pixel guarantee.
export default function visualizationRoot() {
  const visible = element => element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  const marked = document.querySelectorAll('[data-pulsara-visualization-root]');
  // An explicit but invalid selection must not silently select another root.
  if (marked.length) return marked.length === 1 && visible(marked[0]) ? marked[0] : null;
  if (document.getAnimations().some(animation => animation.pending || animation.playState === 'running')) return null;
  const mains = Array.from(document.querySelectorAll('main')).filter(visible);
  if (mains.length !== 1 || !document.body?.contains(mains[0])) return null;
  const root = mains[0];
  const bounds = root.getBoundingClientRect();
  if (bounds.width <= 0 || bounds.height <= 0) return null;
  const fits = rect => rect.left >= bounds.left && rect.top >= bounds.top
    && rect.right <= bounds.right && rect.bottom <= bounds.bottom;
  const rendered = rect => rect.width > 0 && rect.height > 0;
  const ignored = element => element.closest('head,script,style,template,noscript,meta,link');
  const nodes = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  // Include body/ancestor pseudo-content too. A DOM box cannot establish its
  // painted extent, so such ambiguous pages retain the full viewport.
  let node = document.documentElement;
  do {
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    if (!element || ignored(element)) continue;
    const style = getComputedStyle(element);
    if (!visible(element) && style.display !== 'contents') continue;
    if (node.nodeType === Node.TEXT_NODE) {
      if (!node.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      if (Array.from(range.getClientRects()).some(rect => rendered(rect) && (!root.contains(node) || !fits(rect)))) return null;
      continue;
    }
    if (element.shadowRoot || element.localName.includes('-')) return null;
    for (const pseudo of ['::before', '::after']) {
      const content = getComputedStyle(element, pseudo).content;
      if (content !== 'none' && content !== 'normal' && content !== '""') return null;
    }
    if (element === root || element.contains(root)) continue;
    const rects = Array.from(element.getClientRects()).filter(rendered);
    if (rects.length && (!root.contains(element) || rects.some(rect => !fits(rect)))) return null;
  } while ((node = nodes.nextNode()));
  return root;
}
