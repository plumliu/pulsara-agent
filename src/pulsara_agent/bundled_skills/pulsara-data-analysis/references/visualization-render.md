# Inline visualizations and HTML review

Choose text or tables for simple results, Markdown image links for saved figures, fenced `svg` or `mermaid` for static diagrams, and self-contained HTML for interaction. SVG and Mermaid display as images; ordinary HTML code blocks display source. Use `$...$` for inline formulas or `$$` on separate lines for display math; `\(...\)` and `\[...\]` are also supported.

## Create and publish

Write responsive `.html`/`.htm` files, preferably in `.pulsara/visualizations/`. For simple visuals, consider a single HTML file. Local image paths may be absolute or relative to the file that references them. Use inline SVG for icons such as Lucide. Network resources are unavailable. Paths in publication fences resolve from the session workspace, not the terminal directory; absolute paths and `~` follow ordinary file access. Resources inside HTML/CSS resolve relative to the declaring file.

In a tool-free reply, place explicitly closed, top-level `pulsara-visualization` fences beside the relevant prose. Each fence contains only existing HTML paths, one per line:

````markdown
Compare the costs:

```pulsara-visualization
.pulsara/visualizations/cost.html
.pulsara/visualizations/cost-breakdown.html
```
````

Each fence creates an independent gallery. One path shows one figure; multiple paths share host-provided thumbnail navigation. Group alternatives together; use separate fences to show figures together. Views with shared controls belong in one file. Keep actual display fences outside lists and blockquotes; wrap syntax examples in a longer outer fence as above.

Publication freezes each HTML together with its supported local resources. Later edits or deletion leave earlier replies unchanged; publishing the path again captures its current contents. Missing or unreadable files show an error at their display position.

## Layout and review

Use concise labels, readable axes and units, and compact layouts that fit narrow screens. Keep outer backgrounds transparent by default, with opaque surfaces where readability or the requested design needs them. Preserve contrast in light and dark themes.

For compact content, mark exactly one visible container `data-pulsara-visualization-root`. Keep its content within its bounds for reliable fitting. Without a marker, a unique visible `main` containing all visible content may be fitted automatically; other layouts use page mode.

Optionally inspect the current HTML with `view_image(path=...)`. Its screenshot is a visual check, not publication or proof that every interaction works; inspect again after editing when claiming the updated version was reviewed.

## Host environment

The optional read-only `window.PulsaraVisualization` supplies `getSnapshot()` and `subscribe(callback)`. Read the initial snapshot before subscribing to later updates; subscription returns an unsubscribe function. Snapshots contain `theme` (`light`/`dark`), `visible`, `reducedMotion`, and `colors` (`background`, `foreground`, `muted`, `accent`, `border`, `soft`). Colors are also available as `--pulsara-*` CSS variables. Render with HTML/SVG/canvas or an inlined library, with standalone fallbacks.

```css
html, body { background: transparent; }
body { margin: 0; color: var(--pulsara-foreground, #17191f); }
[data-animated] { animation-play-state: var(--animation-state, paused); }
```

```js
const environment = window.PulsaraVisualization;
const motion = matchMedia('(prefers-reduced-motion: reduce)');
function apply(state) {
  document.documentElement.style.setProperty(
    '--animation-state', state.visible && !state.reducedMotion ? 'running' : 'paused'
  );
}
if (environment) {
  apply(environment.getSnapshot());
  const unsubscribe = environment.subscribe(apply);
  // Call unsubscribe when removing the component that owns this listener.
} else {
  const update = () => apply({visible: !document.hidden, reducedMotion: motion.matches});
  update();
  motion.addEventListener('change', update);
  document.addEventListener('visibilitychange', update);
}
```

Apply the same pause rule to JavaScript animations. Hidden pages receive `visible=false`; reopening a page can reset its local state, so do not rely on persisted controls. The environment API provides display context only; it grants no host-file, tool or messaging access.

`examples/analyze_csv.py` provides an adaptable standalone HTML example.
