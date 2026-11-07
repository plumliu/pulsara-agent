import { fileURLToPath } from 'node:url';
import { readdirSync, readFileSync } from 'node:fs';
import { defineConfig, searchForWorkspaceRoot } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/postcss';

export default defineConfig({
  root: fileURLToPath(new URL('./local', import.meta.url)),
  publicDir: fileURLToPath(new URL('./public', import.meta.url)),
  server: { fs: { allow: [searchForWorkspaceRoot(fileURLToPath(new URL('./', import.meta.url))), fileURLToPath(new URL('../desktop/assets/fonts/', import.meta.url))] } },
  css: { postcss: { plugins: [tailwindcss()] } },
  resolve: { alias: { '@noble/hashes/sha256': fileURLToPath(new URL('./node_modules/@noble/hashes/esm/sha256.js', import.meta.url)) } },
  plugins: [react(), {
    name: 'bundled-font-notices',
    generateBundle() {
      const directory = new URL('../desktop/assets/fonts/licenses/', import.meta.url);
      for (const name of readdirSync(directory)) {
        const file = new URL(name, directory);
        this.addWatchFile(fileURLToPath(file));
        this.emitFile({ type: 'asset', fileName: `assets/font-licenses/${name}`, source: readFileSync(file) });
      }
    },
  }],
  build: {
    outDir: fileURLToPath(
      new URL('../src/pulsara_agent/desktop_app/static', import.meta.url),
    ),
    emptyOutDir: true,
    sourcemap: true,
  },
});
