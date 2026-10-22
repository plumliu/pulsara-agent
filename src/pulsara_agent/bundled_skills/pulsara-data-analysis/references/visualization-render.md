# Figures, file browsing and HTML review

Choose text or tables for simple results, publish saved figures inline with `pulsara-visualization`, use fenced `svg` or `mermaid` for static diagrams, and self-contained HTML for interaction. SVG and Mermaid display as images; ordinary HTML code blocks display source. Use `$...$` for inline formulas or `$$` on separate lines for display math; `\(...\)` and `\[...\]` are also supported.

Always enclose SVG, Mermaid and other source code in closed triple-backtick fences with the correct language label. Never emit bare SVG/XML or diagram source.

For a static SVG diagram, put the SVG source directly in a `svg` fence:

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 40">
  <rect width="160" height="40" rx="8" fill="#536fd3"/>
</svg>
```

## Open current files

Link a deliverable with `[label](/absolute/path)`. The desktop opens its current contents in a right-side browsing tab. Links between local HTML and Markdown documents navigate in that tab with back/forward history. Use this for document packages, Skill references and files the user should revisit after edits. In conversation replies, Markdown image syntax `![label](/absolute/plot.png)` creates a clickable image reference; it does not display the full figure inline.

## Create and publish

Write responsive `.html`/`.htm` files, preferably in `.pulsara/visualizations/`. For simple visuals, consider a single HTML file. Local image paths may be absolute or relative to the file that references them. Embed animated SVG and SVG icons such as Lucide inside the HTML. Network resources are unavailable. Paths in publication fences resolve from the session workspace, not the terminal directory; absolute paths and `~` follow ordinary file access. Resources inside HTML/CSS resolve relative to the declaring file.

In a tool-free reply, place explicitly closed, top-level `pulsara-visualization` fences beside the relevant prose. Each fence contains only existing file paths or known `image:` references copied unchanged, one per line. The only supported file extensions are **`.png`, `.jpg`, `.jpeg`, `.webp` (static only), `.html` and `.htm`**. Do not put `.svg` paths or SVG source in these fences; use a fenced `svg` code block or embed the SVG in an HTML file instead. Supported images do not need an HTML wrapper or a prior `view_image` call:

Compare the costs:

```pulsara-visualization
.pulsara/visualizations/cost.html
.pulsara/visualizations/cost-breakdown.png
```

Each fence creates an independent gallery. One path shows one figure; multiple paths share host-provided thumbnail navigation. Group alternatives together; use separate fences to show figures together. Views with shared controls belong in one file. Keep display fences outside lists and blockquotes.

Prefer several compact HTML figures in separate fences beside their explanations over one long, internally scrolling dashboard. Several paths in one fence create gallery pages, not stacked inline figures. Keep shared filters or tightly coupled interactions together.

Publication freezes each image or HTML together with its supported local resources. Later edits or deletion leave earlier replies unchanged; publishing the path again captures its current contents. Missing or unreadable files show an error at their display position. Opening a published figure in a tab still shows that snapshot. A frozen HTML snapshot does not include an entire linked document package; provide a current-file link when the user needs cross-file browsing.

## Layout and review

Use concise labels, readable axes and units, and compact layouts that fit narrow screens. Keep outer backgrounds transparent by default, with opaque surfaces where readability or the requested design needs them. Preserve contrast in light and dark themes.

For compact content, mark exactly one visible container `data-pulsara-visualization-root`. Keep its content within its bounds for reliable fitting. Without a marker, a unique visible `main` containing all visible content may be fitted automatically; other layouts use page mode.

Use `view_image` directly for SVG files; do not convert them with system thumbnail tools. Inspect saved images or current HTML with `view_image(path=...)` when useful. This gives the model pixels independently of the user’s tabs; publishing a figure does not mean you have seen it. An HTML screenshot checks the captured appearance, not clicks or other interactions. Describe exactly what you inspected, and inspect again after edits before claiming the updated version was reviewed.

## Host environment

The optional read-only `window.PulsaraVisualization` supplies `getSnapshot()` and `subscribe(callback)`. Read the initial snapshot before subscribing to later updates; subscription returns an unsubscribe function. Snapshots contain `theme` (`light`/`dark`), `visible`, `reducedMotion`, and `colors` (`background`, `foreground`, `muted`, `accent`, `border`, `soft`). Colors are also available as `--pulsara-*` CSS variables. Render with HTML/SVG/canvas or an inlined library, with standalone fallbacks.

```css
html, body { background: transparent; }
body { margin: 0; color: var(--pulsara-foreground, #17191f); }
[data-animated] { animation-play-state: var(--animation-state, paused); }
```

```js
const environment = window.PulsaraVisualization;
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
  const update = () => apply({visible: !document.hidden, reducedMotion: false});
  update();
  document.addEventListener('visibilitychange', update);
}
```

Apply the same pause rule to JavaScript animations. Live displays keep `reducedMotion=false` regardless of OS motion preferences; do not disable live transitions with a reduced-motion media query. The separate still-image capture uses `reducedMotion=true` to request a stable frame. Hidden pages receive `visible=false`; reopening a page can reset its local state, so do not rely on persisted controls. The environment API provides display context only; it grants no host-file, tool or messaging access.

`examples/analyze_csv.py` provides an adaptable standalone HTML example.
