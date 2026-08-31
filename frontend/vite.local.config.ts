import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/postcss';

const frontendRoot = fileURLToPath(new URL('.', import.meta.url));

export default defineConfig({
  root: fileURLToPath(new URL('./local', import.meta.url)),
  publicDir: fileURLToPath(new URL('./public', import.meta.url)),
  css: { postcss: { plugins: [tailwindcss()] } },
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    fs: { allow: [frontendRoot] },
  },
  build: {
    outDir: fileURLToPath(
      new URL('../src/pulsara_agent/web_app/static', import.meta.url),
    ),
    emptyOutDir: true,
    sourcemap: true,
  },
});
