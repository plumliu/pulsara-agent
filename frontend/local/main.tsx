import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import PulsaraApp from '../app/pulsara-app';
import '../app/globals.css';
import '../app/styles/base.css';
import '../app/styles/shell.css';
import '../app/styles/workbench.css';
import '../app/styles/inspector.css';
import '../app/styles/overview.css';
import '../app/styles/settings.css';
import '../app/styles/overlays.css';
import '../app/styles/responsive.css';

const root = document.getElementById('root');

if (!root) {
  throw new Error('Pulsara root element is missing');
}

createRoot(root).render(
  <StrictMode>
    <PulsaraApp />
  </StrictMode>,
);
