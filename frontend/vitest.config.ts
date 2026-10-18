import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  server: {
    fs: { allow: [fileURLToPath(new URL('../', import.meta.url))] },
  },
  resolve: {
    // Tests and mocks outside frontend share this root's dependency instances.
    dedupe: [
      'react',
      'react-dom',
      'react-easy-crop',
      'vitest',
      '@testing-library/react',
      '@testing-library/user-event',
      '@tiptap/pm',
      '@floating-ui/dom',
    ],
  },
  test: {
    include: ['../tests/frontend/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    // jsdom owns window storage. Node 25's native Web Storage otherwise masks
    // its per-document implementation in Vitest's populated global object.
    execArgv: ['--no-experimental-webstorage'],
    css: false,
    setupFiles: ['../tests/frontend/setup.ts'],
  },
});
