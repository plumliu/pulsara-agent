// The UI and disposable screenshot worker share the same document boundary.
export default function visualizationShell({ html, bootstrap = '', setup = '', sourceUrl, resourcePrefix }) {
  const frozenPolicy = "default-src 'none'; script-src 'unsafe-inline' data:; style-src 'unsafe-inline' data:; img-src data:; font-src data:; connect-src 'none'; worker-src 'none'; frame-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
  // Ordinary file previews retain their existing resource-server policy.
  const policy = sourceUrl === undefined ? frozenPolicy : frozenPolicy.replace("script-src 'unsafe-inline' data:", "script-src 'unsafe-inline'").replace("style-src 'unsafe-inline' data:", "style-src 'unsafe-inline'");
  const outerPolicy = resourcePrefix ? policy.replace("frame-src 'none'", `frame-src ${resourcePrefix}`) : policy;
  const inner = sourceUrl ?? `<!doctype html><meta http-equiv="Content-Security-Policy" content="${policy}">${bootstrap}${html.replace(/^\s*<!doctype[^>]*>/i, '')}`;
  const literal = JSON.stringify(inner).replace(/</g, '\\u003c');
  return `<!doctype html><meta http-equiv="Content-Security-Policy" content="${outerPolicy}"><style>html,body,iframe{margin:0;width:100%;height:100%;border:0;display:block;overflow:hidden}</style><iframe id="content" sandbox="allow-scripts" referrerpolicy="no-referrer" title="预览内容"></iframe><script>
const frame=document.getElementById('content');
${setup}
frame.${sourceUrl === undefined ? 'srcdoc' : 'src'}=${literal};
</script>`;
}
