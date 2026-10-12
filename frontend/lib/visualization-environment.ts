import install from '../../src/pulsara_agent/conversation_kernel/visualization_environment.js';
export interface VisualizationEnvironment {
  theme: 'light' | 'dark'; visible: boolean; reducedMotion: boolean;
  colors: Record<'background' | 'foreground' | 'muted' | 'accent' | 'border' | 'soft', string>;
}
export const environmentMessageType = 'pulsara-visualization-environment';
export function visualizationEnvironment(visible: boolean): VisualizationEnvironment {
  const style = getComputedStyle(document.documentElement);
  return { theme: document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light',
    visible: visible && document.visibilityState !== 'hidden', reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
    colors: { background: style.getPropertyValue('--paper-solid').trim(), foreground: style.getPropertyValue('--ink').trim(),
      muted: style.getPropertyValue('--muted').trim(), accent: style.getPropertyValue('--amber').trim(),
      border: style.getPropertyValue('--line').trim(), soft: style.getPropertyValue('--paper-deep').trim() } };
}
export function environmentBootstrap(environment?: VisualizationEnvironment): string {
  return `<script>(${install.toString()})(${JSON.stringify(environment ?? null).replace(/</g, '\\u003c')})</script>`;
}
