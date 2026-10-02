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
    css: false,
    setupFiles: ['../tests/frontend/setup.ts'],
  },
});
