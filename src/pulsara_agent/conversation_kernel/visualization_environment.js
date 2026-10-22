export default function installVisualizationEnvironment(initial) {
  // Shared by the embedded frame and the screenshot worker. Defaults match the
  // light application tokens and still-image capture mode. The live host sends
  // its actual computed tokens and reducedMotion=false, independent of the OS.
  const defaults = { theme: 'light', visible: true, reducedMotion: true, colors: {
    background: '#fdfbf6', foreground: '#17191f', muted: '#7d7b74', accent: '#d79a42', border: 'rgba(29, 31, 38, 0.12)', soft: '#e9e5db',
  } };
  const valid = value => value && ['light', 'dark'].includes(value.theme)
    && typeof value.visible === 'boolean' && typeof value.reducedMotion === 'boolean'
    && value.colors && Object.keys(defaults.colors).every(key => typeof value.colors[key] === 'string');
  const freeze = value => Object.freeze({ theme: value.theme, visible: value.visible, reducedMotion: value.reducedMotion,
    colors: Object.freeze(Object.fromEntries(Object.keys(defaults.colors).map(key => [key, value.colors[key]]))) });
  let current = freeze(valid(initial) ? initial : defaults);
  const subscribers = new Set();
  const apply = () => {
    const root = document.documentElement;
    root.style.colorScheme = current.theme;
    for (const [key, value] of Object.entries(current.colors)) root.style.setProperty(`--pulsara-${key}`, value);
  };
  Object.defineProperty(window, 'PulsaraVisualization', { value: Object.freeze({
    getSnapshot: () => current,
    subscribe: callback => { subscribers.add(callback); return () => subscribers.delete(callback); },
  }), writable: false, configurable: false });
  addEventListener('message', event => {
    if (event.source !== parent || event.data?.type !== 'pulsara-visualization-environment' || !valid(event.data.environment)) return;
    current = freeze(event.data.environment); apply();
    for (const callback of subscribers) { try { callback(current); } catch (error) { console.error(error); } }
  });
  apply();
  parent.postMessage({ type: 'pulsara-visualization-environment-ready' }, '*');
}
