import { fileURLToPath } from 'node:url';
import { build } from '../../frontend/node_modules/vite/dist/node/index.js';
import react from '../../frontend/node_modules/@vitejs/plugin-react/dist/index.js';

// TraceCard is private and has no expanded-result render slot. This local build
// substitutes that one slot in memory; it does not patch production source or
// copy its card/state machinery. Fail explicitly if the upstream seam changes.
const workbenchPath = fileURLToPath(new URL('../../frontend/components/workbench-view.tsx', import.meta.url));
const summaryPath = fileURLToPath(new URL('./subagent-tool-summary.tsx', import.meta.url));
const expandedResultSeam = '{expanded && (\n          <div className="terminal-output">';
const demoSubagentResult = {
  name: 'demo-subagent-expanded-result',
  enforce: 'pre',
  transform(source, id) {
    if (id !== workbenchPath) return undefined;
    if (source.split(expandedResultSeam).length !== 2) {
      throw new Error('Production TraceCard expanded-result seam changed; review the demo adapter.');
    }
    return {
      code: `import { DemoSubagentToolSummary, isDemoSubagentTool } from ${JSON.stringify(summaryPath)};\n`
        + source.replace(expandedResultSeam,
          '{expanded && isDemoSubagentTool(trace) && <DemoSubagentToolSummary trace={trace} />}\n'
          + '        {expanded && !isDemoSubagentTool(trace) && (\n          <div className="terminal-output">'),
      map: null,
    };
  },
};

// Reuse the installed production toolchain, but never its production output or
// settings. The frame isolates real Workbench CSS from the exploratory graph UI.
await build({
  configFile: false,
  envFile: false,
  root: fileURLToPath(new URL('../../frontend/', import.meta.url)),
  publicDir: false,
  base: './',
  plugins: [demoSubagentResult, react()],
  resolve: { dedupe: ['react', 'react-dom', 'lucide-react'] },
  css: { postcss: { plugins: [] } },
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    outDir: fileURLToPath(new URL('./production-assets/', import.meta.url)),
    emptyOutDir: true,
    lib: {
      entry: fileURLToPath(new URL('./production-workbench.tsx', import.meta.url)),
      formats: ['es'],
      fileName: 'workbench',
      cssFileName: 'workbench',
    },
  },
});
