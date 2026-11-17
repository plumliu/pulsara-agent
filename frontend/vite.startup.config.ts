import {fileURLToPath} from 'node:url';
import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins:[react()],
  define:{'process.env.NODE_ENV':JSON.stringify('production')},
  build:{
    outDir:fileURLToPath(new URL('../desktop/assets/startup-ui', import.meta.url)),
    emptyOutDir:true,
    lib:{entry:fileURLToPath(new URL('./local/startup-toast.tsx', import.meta.url)), formats:['iife'], name:'PulsaraStartupToast', fileName:()=>'toast.js', cssFileName:'toast'},
  },
});
