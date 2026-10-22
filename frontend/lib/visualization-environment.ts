import install from '../../src/pulsara_agent/conversation_kernel/visualization_environment.js';
import installPlayback from '../../src/pulsara_agent/conversation_kernel/visualization_playback.js';
export interface VisualizationEnvironment {
  theme: 'light' | 'dark'; visible: boolean; reducedMotion: boolean;
  colors: Record<'background' | 'foreground' | 'muted' | 'accent' | 'border' | 'soft', string>;
}
export const environmentMessageType = 'pulsara-visualization-environment';
export function visualizationEnvironment(visible: boolean): VisualizationEnvironment {
  const style = getComputedStyle(document.documentElement);
  return { theme: document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light',
    // Live playback always has motion. The separate still-image worker owns
    // reducedMotion=true for capture; it is not a user/OS preference anymore.
    visible: visible && document.visibilityState !== 'hidden', reducedMotion: false,
    colors: { background: style.getPropertyValue('--paper-solid').trim(), foreground: style.getPropertyValue('--ink').trim(),
      muted: style.getPropertyValue('--muted').trim(), accent: style.getPropertyValue('--amber').trim(),
      border: style.getPropertyValue('--line').trim(), soft: style.getPropertyValue('--paper-deep').trim() } };
}
export function environmentBootstrap(environment?: VisualizationEnvironment): string {
  const initial = JSON.stringify(environment ?? null).replace(/</g, '\\u003c');
  return `<script>(${install.toString()})(${initial}, (${installPlayback.toString()})(${Boolean(environment?.visible)}))</script>`;
}
