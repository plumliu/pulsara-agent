# Inline visualizations and HTML review

Use a Markdown image link for a saved image, fenced SVG or Mermaid for a small static diagram, and self-contained HTML for useful interaction. Keep simple answers simple.

Write responsive `.html` or `.htm` files, preferably in `.pulsara/visualizations/`. Inline scripts, styles, fonts and data; network resources, CDN dependencies and companion files cannot load. Relative paths start at the session workspace, not the terminal's current directory. Absolute paths and `~` follow ordinary file access.

Optionally call `view_image` with the HTML path to inspect a screenshot of the current file. Screenshots use a 1200×800 viewport, light theme and reduced motion. Inspect the returned image and revise as needed; edits after that screenshot are unreviewed until another screenshot is taken. The returned `image_ref` can reread that screenshot; it does not reference HTML. Screenshot review does not publish the file or validate every interaction.

After creating the files, put one or more paths, one per line, in an explicitly closed top-level fenced block in a tool-free reply, between the paragraphs it illustrates. Use only paths inside the fence: no bullets, quotes, captions, JSON, HTML source or image references. For example, this reply contains a two-figure gallery followed by a separate single figure:

````markdown
The cost comparison is shown here:

```pulsara-visualization
.pulsara/visualizations/cost.html
.pulsara/visualizations/cost-breakdown.html
```

Beyond this point, option B costs less. The delivery schedule is shown separately:

```pulsara-visualization
.pulsara/visualizations/timeline.html
```

The first milestone is the critical dependency.
````

Publication reads and freezes the HTML. Editing or deleting its file afterward does not change older replies. A later reply with the same path publishes the current version. Each marker displays an independent gallery at its own position. One path shows a single figure; multiple paths share thumbnail navigation. Blank lines are ignored; no subscription or display ID is needed. A missing or unreadable file shows an error in place. Do not put actual display markers inside lists, blockquotes, or tool-bearing messages. Use an outer four-backtick fence to explain the marker syntax without displaying a file.

Independent galleries use separate body markers; related alternative views can share one marker containing multiple HTML paths. Only the selected figure in a gallery is shown, so use separate galleries when readers should see figures together. Pulsara supplies thumbnail navigation; do not recreate this outer navigation in every HTML file. Views sharing selection or controls should use one file: separate files do not share state, even within one gallery. Avoid title banners, explanatory padding and excessive whitespace when surrounding prose already provides context. Label axes, units and controls; preserve readability on narrow screens.

For a compact chart, mark exactly one visible container `data-pulsara-visualization-root`. If it fits the reference viewport, the host crops vertical whitespace and reduces the space occupied in the reply, while keeping the full body-column width and your internal horizontal spacing. Figures shorter than 46px keep their measured height without an inline zoom button; taller figures retain zoom, and maximized views always retain the exit control. Tall content uses the original page with native scrolling, within the inline height limit min(120dvh, 1200px). Without a marker, a unique visible `<main>` is automatically selected only when it contains all visible content with no overflowing descendants or ambiguous generated content. Automatic fitting is a layout heuristic: decorative shadows or strokes may be clipped. Automatic previews stay in page mode for the current mount after detected animations or interaction-driven content overflow, avoiding repeated layout shifts. Other unmarked layouts, and multiple, hidden or invalid explicit roots use page mode. Maximize displays a full viewport.

For an inline figure, keep `html`, `body`, and the outer layout container transparent by default so the surrounding conversation shows through. Do not wrap the whole figure in a large opaque panel solely to provide a background. Cards, plot surfaces, or a requested full-page design can retain a background where it improves readability. Keep text, axes, and controls legible in both light and dark themes; transparency is a default, not a requirement to remove every surface.

The optional read-only `window.PulsaraVisualization` is an environment API, not a visualization library. Its only methods are `getSnapshot()` and `subscribe(callback)`, which returns an unsubscribe function. There are no `mount`, `render`, or `animate` methods; create charts using ordinary HTML/SVG/canvas or an inlined library. A snapshot contains `theme` (`light`/`dark`), `visible`, `reducedMotion`, and `colors` (`background`, `foreground`, `muted`, `accent`, `border`, `soft`). Matching CSS variables are named `--pulsara-background`, etc. Read the initial snapshot explicitly: subscribing delivers later updates, not the initial value.

For CSS animations, a small environment adapter can set the play state; apply the same visibility rule to any JavaScript animation loop you own:

```css
html, body { background: transparent; }
body {
  margin: 0;
  color: var(--pulsara-foreground, #17191f);
}
[data-animated] { animation-play-state: var(--animation-state, paused); }
```

```js
const environment = window.PulsaraVisualization;
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
function applyEnvironment(state) {
  document.documentElement.style.setProperty(
    '--animation-state', state.visible && !state.reducedMotion ? 'running' : 'paused'
  );
}
if (environment) {
  applyEnvironment(environment.getSnapshot());
  environment.subscribe(applyEnvironment);
} else {
  const update = () => applyEnvironment({
    visible: !document.hidden, reducedMotion: reducedMotion.matches
  });
  update();
  reducedMotion.addEventListener('change', update);
  document.addEventListener('visibilitychange', update);
}
```

If your file creates and destroys its own components, retain and call the unsubscribe function when removing their listeners. Environment updates do not reload the iframe. Each gallery retains up to three recently visited pages; hidden retained pages receive `visible=false`. No arbitrary JavaScript state survives eviction or unmounting.

Provide ordinary browser fallbacks, for example `color: var(--pulsara-foreground, #17191f)`, and optional chaining for the API. Use `background: var(--pulsara-background, #fff)` on local surfaces that need an opaque background, not automatically on the entire page. A file must also work outside Pulsara. The API cannot read host files, call tools or send follow-up messages. Do not rely on network access or persisted widget parameters.

The example `examples/analyze_csv.py` writes a standalone HTML file and reports its path. Adapt it to the task; its layout is not a required schema.
