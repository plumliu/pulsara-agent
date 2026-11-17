import {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {randomIdentity} from '../../desktop/src/identity-core.mjs';
import {ToastStack} from '../components/overlays';
import type {ToastMessage} from '../lib/pulsara-types';
import '../app/styles/overlays.css';

function StartupToasts() {
  const [toasts, setToasts] = useState<ToastMessage<string>[]>([]);
  useEffect(() => {
    const receive = (event: Event) => {
      const title = (event as CustomEvent).detail;
      if (typeof title === 'string') setToasts([{id: randomIdentity('presentation'), title, tone:'warning'}]);
    };
    window.addEventListener('pulsara-startup-toast', receive);
    return () => window.removeEventListener('pulsara-startup-toast', receive);
  }, []);
  return <ToastStack toasts={toasts} onDismiss={() => setToasts([])} />;
}
createRoot(document.getElementById('startup-toasts')!).render(<StartupToasts />);
