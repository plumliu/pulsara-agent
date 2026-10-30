# HTML theme CSS

Use this reference to style HTML visualizations with Pulsara's theme colors. It is self-contained; basic CSS styling does not require the environment API or a data-analysis workflow.

## Available variables

Pulsara injects these CSS custom properties on the HTML document's root element and updates them when the host theme changes. They are Pulsara-provided names, not browser standards. Use `var(--pulsara-name, fallback)` so the HTML also works outside Pulsara. The fallback values below match the host's default light palette; live values follow the current theme.

| Variable | Use | Fallback |
| --- | --- | --- |
| `--pulsara-foreground` | Main text, labels and diagram strokes | `#17191f` |
| `--pulsara-background` | Opaque cards, panels and controls | `#fdfbf6` |
| `--pulsara-muted` | Secondary text and annotations | `#7d7b74` |
| `--pulsara-accent` | Highlights, selected elements and emphasis | `#d79a42` |
| `--pulsara-border` | Borders, separators and grid lines | `rgba(29, 31, 38, 0.12)` |
| `--pulsara-soft` | Subtle fills and secondary surfaces | `#e9e5db` |

Keep the outer page background transparent unless the design needs a surface. Apply opaque colors to the elements that need them. These variables provide theme colors, not a complete categorical chart palette; choose additional series colors when needed and keep labels and marks legible in both themes.

## Example: a responsive card

```html
<style>
  html, body { margin: 0; background: transparent; }
  body {
    color: var(--pulsara-foreground, #17191f);
    font: 14px/1.5 system-ui, sans-serif;
  }
  .card {
    box-sizing: border-box;
    width: 100%;
    max-width: 36rem;
    padding: 1rem;
    border: 1px solid var(--pulsara-border, rgba(29, 31, 38, 0.12));
    border-radius: 12px;
    background: var(--pulsara-background, #fdfbf6);
  }
  .card h2 { margin: 0 0 .5rem; }
  .note { color: var(--pulsara-muted, #7d7b74); }
</style>
<section class="card" pulsara-viz-root>
  <h2>Experiment results</h2>
  <p>Eight of ten checks passed.</p>
  <p class="note">Two checks still need investigation.</p>
</section>
```

For a compact figure, `pulsara-viz-root` marks one visible containing element for host fitting. Keep its contents within its bounds.

## Example: selected controls

```css
button {
  padding: .5rem .75rem;
  color: var(--pulsara-foreground, #17191f);
  background: var(--pulsara-soft, #e9e5db);
  border: 1px solid var(--pulsara-border, rgba(29, 31, 38, 0.12));
  border-radius: 6px;
  font: inherit;
  cursor: pointer;
}
button[aria-pressed="true"] {
  border-color: var(--pulsara-accent, #d79a42);
  box-shadow: inset 0 -2px var(--pulsara-accent, #d79a42);
}
button:focus-visible {
  outline: 2px solid var(--pulsara-accent, #d79a42);
  outline-offset: 2px;
}
```

## Example: inline SVG

CSS variables also work in inline SVG styles. This diagram follows theme changes without JavaScript.

```html
<svg viewBox="0 0 320 100" style="width:100%;max-width:32rem"
     role="img" aria-label="Input flows to output">
  <style>
    .node { fill: var(--pulsara-soft, #e9e5db); }
    .edge { stroke: var(--pulsara-accent, #d79a42); stroke-width: 3; }
    .label { fill: var(--pulsara-foreground, #17191f); font: 14px system-ui; }
  </style>
  <rect class="node" x="10" y="25" width="100" height="50" rx="8" />
  <rect class="node" x="210" y="25" width="100" height="50" rx="8" />
  <path class="edge" d="M110 50 H200 M190 44 L200 50 L190 56" fill="none" />
  <text class="label" x="60" y="55" text-anchor="middle">Input</text>
  <text class="label" x="260" y="55" text-anchor="middle">Output</text>
</svg>
```

Inline SVG can inherit the containing document's variables; a separate SVG loaded through `<img>` cannot. Canvas drawing also needs explicit color values and a redraw to reflect theme changes. Only when JavaScript needs theme or playback state, consult [HTML environment](html-environment.md).
