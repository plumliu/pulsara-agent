# HTML display environment

Read this reference when an HTML figure needs host theme, animation or screenshot integration. It applies to any HTML illustration or interactive view; it does not require a data-analysis workflow.

## Layout and resources

Create responsive HTML with legible labels and controls. Keep outer backgrounds transparent by default, with opaque surfaces where the design or contrast calls for them. For compact content, mark exactly one visible containing element with `pulsara-viz-root` and keep the figure within its bounds. The host also attempts automatic fitting for ordinary layouts; other content uses page mode.

HTML can use inline SVG, Canvas, ordinary elements or a locally included library. Network resources are unavailable. Local resource paths resolve relative to the HTML or CSS file that declares them. Publishing freezes the HTML and supported local resources; a current-file link is appropriate for a document package with cross-file navigation.

## Theme and animation

The optional read-only `window.PulsaraVisualization` exposes `getSnapshot()` and `subscribe(callback)`. Read the initial snapshot before subscribing; `subscribe` returns an unsubscribe function. Snapshots contain `theme` (`light`/`dark`), `visible`, `reducedMotion`, and `colors` (`background`, `foreground`, `muted`, `accent`, `border`, `soft`). Matching CSS variables are available as `--pulsara-*`. Supply ordinary browser fallbacks for use outside Pulsara.

```css
html, body { background: transparent; }
body { margin: 0; color: var(--pulsara-foreground, #17191f); }
[data-animated] { animation-play-state: var(--animation-state, paused); }
```

When a figure needs environment updates:

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

The live host pauses ordinary `requestAnimationFrame`, function timers, CSS/WAAPI and SVG animations while hidden, without requiring a subscription. Use rAF timestamps or `performance.now()` for elapsed animation time: their activity clock excludes host pauses, while `Date` remains real wall time. Live displays use `reducedMotion=false` independently of OS preferences. Still-image capture uses `reducedMotion=true`; render a stable frame in that mode. Hidden pages receive `visible=false`; retained pages keep their controls, while reopening an unloaded page can reset local state. The environment API supplies display context, not host-file, tool or messaging access.

## Inspect current content

Use `view_image` with a PATH target for a local image or current HTML, or copy an IMAGE_REF target for a saved conversation image. The returned pixels are for the model, independently of the user's tabs; publishing does not require prior inspection. An HTML screenshot captures appearance at that moment. Check interactions separately when they matter, and inspect changed content before describing it as reviewed.
