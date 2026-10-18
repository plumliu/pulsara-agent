import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/postcss';

export default defineConfig({
  root: fileURLToPath(new URL('./local', import.meta.url)),
  publicDir: fileURLToPath(new URL('./public', import.meta.url)),
  css: { postcss: { plugins: [tailwindcss()] } },
  plugins: [react()],
  build: {
    outDir: fileURLToPath(
      new URL('../src/pulsara_agent/desktop_app/static', import.meta.url),
    ),
    emptyOutDir: true,
    sourcemap: true,
  },
});
