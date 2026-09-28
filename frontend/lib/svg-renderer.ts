const svgNamespace = 'http://www.w3.org/2000/svg';

export function startsWithSvg(source: string): boolean {
  return /^\s*(?:<\?xml\s[\s\S]*?\?>\s*)?<svg(?=[\s/>])/.test(source);
}

export async function renderSvg(source: string, signal: AbortSignal) {
  signal.throwIfAborted();
  const { default: createDOMPurify } = await import('dompurify');
  signal.throwIfAborted();
  // Browser XML parsing owns syntax validation; DOMPurify owns SVG sanitizing.
  // The detached document is never inserted into the application DOM.
  const document = new DOMParser().parseFromString(source, 'image/svg+xml');
  const root = document.documentElement;
  if (root.localName !== 'svg' || document.querySelector('parsererror')
    || (root.namespaceURI && root.namespaceURI !== svgNamespace)) {
    throw new Error('Invalid SVG document');
  }
  if (!root.namespaceURI) root.setAttribute('xmlns', svgNamespace);
  const viewBox = root.getAttribute('viewBox')?.trim().split(/[\s,]+/).map(Number);
  const length = (name: string) => {
    const value = root.getAttribute(name)?.trim() ?? '';
    return /^(?:\d+(?:\.\d*)?|\.\d+)(?:px)?$/.test(value) ? Number.parseFloat(value) : undefined;
  };
  let width = length('width');
  let height = length('height');
  if (!(width && height) && viewBox?.length === 4 && viewBox.every(Number.isFinite)) {
    [, , width, height] = viewBox;
  }
  if (width && height && Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
    root.setAttribute('width', String(width));
    root.setAttribute('height', String(height));
  } else {
    width = height = undefined;
  }
  // Use a separate instance so Mermaid's sanitizer configuration cannot leak here.
  const svg = createDOMPurify(window).sanitize(root.outerHTML, {
    USE_PROFILES: { svg: true, svgFilters: true },
    NAMESPACE: svgNamespace,
    PARSER_MEDIA_TYPE: 'application/xhtml+xml',
  });
  const clean = new DOMParser().parseFromString(svg, 'image/svg+xml');
  if (clean.documentElement.localName !== 'svg' || clean.querySelector('parsererror')) {
    throw new Error('SVG could not be prepared for preview');
  }
  signal.throwIfAborted();
  return { svg, width, height };
}
