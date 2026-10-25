import '../app/styles/scheduled.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { desktopHost } from '../lib/desktop-host';
import { initializeUiPreferences } from '../lib/ui-preferences';
import PulsaraApp from '../app/pulsara-app';
import 'katex/dist/katex.min.css';
import 'yet-another-react-lightbox/styles.css';
import '../app/globals.css';
import '../app/styles/base.css';
import '../app/styles/shell.css';
import '../app/styles/workbench.css';
import '../app/styles/inspector.css';
import '../app/styles/overview.css';
import '../app/styles/settings.css';
import '../app/styles/capabilities.css';
import '../app/styles/memory.css';
import '../app/styles/overlays.css';
import '../app/styles/responsive.css';
import '../app/styles/desktop.css';

const root = document.getElementById('root');

if (!root) {
  throw new Error('Pulsara root element is missing');
}

function finishStartup() {
  void desktopHost()!.startupSettled().catch(error => {
    console.error('Could not dismiss startup cover', error);
  }).finally(() => {
    root!.inert = false;
  });
}

if (!desktopHost()) {
  root.innerHTML = '<main style="font:16px var(--sans);padding:48px;color:#625a49;background:#faf8f1;height:100vh"><h1>Pulsara</h1><p>请从 Pulsara 桌面应用打开工作台。</p></main>';
  root.inert = false;
} else void desktopHost()!.readUiPreferences().catch(error => {
  console.warn('Could not load UI preferences', error);
  return {};
}).then(saved => {
  initializeUiPreferences(saved);
  createRoot(root).render(
  <StrictMode>
    <PulsaraApp onStartupSettled={finishStartup} />
  </StrictMode>,
  );
});
