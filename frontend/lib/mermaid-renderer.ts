import type { Mermaid } from 'mermaid';

let library: Promise<Mermaid> | undefined;
let pending: Promise<unknown> = Promise.resolve();

export interface MermaidImage {
  svg: string;
  width?: number;
  height?: number;
}

function loadMermaid(): Promise<Mermaid> {
  library ??= import('mermaid').then(module => module.default).catch(error => {
    library = undefined;
    throw error;
  });
  return library;
}

export function renderMermaid(source: string, dark: boolean, signal: AbortSignal): Promise<MermaidImage> {
  // Mermaid queues render(), but initialize() changes global configuration outside
  // that queue. Keep each theme + render together, including across theme changes.
  const result = pending.then(async () => {
    signal.throwIfAborted();
    const mermaid = await loadMermaid();
    await document.fonts?.ready;
    signal.throwIfAborted();
    const style = getComputedStyle(document.documentElement);
    const color = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback;
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      suppressErrorRendering: true,
      htmlLabels: false,
      theme: 'base',
      layout: 'dagre',
      // Mermaid 12 widens even short labels to 120px; a decision diamond's
      // height is derived from that width. Dagre honors the flowchart spacing
      // knobs (the bundled ELK adapter doesn't), keeping chat diagrams compact.
      flowchart: { minNodeWidth: 0, padding: 8, nodeSpacing: 28, rankSpacing: 28 },
      fontFamily: color('--sans', 'Arial, sans-serif'),
      secure: [...(mermaid.mermaidAPI.defaultConfig.secure ?? []), 'htmlLabels', 'themeCSS', 'theme', 'themeVariables', 'fontFamily'],
      themeVariables: {
        darkMode: dark,
        background: color('--paper-raised', dark ? '#202228' : '#faf8f2'),
        primaryColor: color('--paper-deep', dark ? '#2b2d34' : '#e9e5db'),
        primaryTextColor: color('--ink-soft', dark ? '#dad5cb' : '#32343d'),
        primaryBorderColor: color('--muted', dark ? '#aaa79f' : '#7d7b74'),
        secondaryColor: color('--blue-soft', dark ? '#29324f' : '#e4e9fa'),
        tertiaryColor: color('--amber-soft', dark ? '#41351f' : '#f5e6ca'),
        lineColor: color('--muted', dark ? '#aaa79f' : '#7d7b74'),
        textColor: color('--ink-soft', dark ? '#dad5cb' : '#32343d'),
        fontSize: '14px',
      },
    });
    const container = document.createElement('div');
    container.className = 'mermaid-measure';
    container.setAttribute('aria-hidden', 'true');
    document.body.appendChild(container);
    try {
      const { svg } = await mermaid.render(`mermaid-${crypto.randomUUID()}`, source, container);
      signal.throwIfAborted();
      // SVG stays in an image document, never in the application DOM. Explicit
      // dimensions preserve the viewBox aspect ratio in the image and Lightbox.
      const root = new DOMParser().parseFromString(svg, 'image/svg+xml').documentElement;
      if (root.localName !== 'svg') throw new Error('Mermaid did not return an SVG image');
      // The same image is shown over the dark Lightbox backdrop. Keep labels
      // outside nodes readable there as well as in the inline paper card.
      root.style.backgroundColor = color('--paper-raised', dark ? '#202228' : '#faf8f2');
      const viewBox = root.getAttribute('viewBox')?.trim().split(/[\s,]+/).map(Number);
      const width = viewBox?.[2];
      const height = viewBox?.[3];
      if (width && height && Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
        root.setAttribute('width', String(width));
        root.setAttribute('height', String(height));
        return { svg: root.outerHTML, width, height };
      }
      return { svg: root.outerHTML };
    } finally {
      container.remove();
    }
  });
  pending = result.catch(() => undefined);
  return result;
}
