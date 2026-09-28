'use client';
import { useEffect, useMemo, useRef, type CSSProperties, type RefObject } from 'react';
import { visualizationFrameMeasurementScript, visualizationLayoutMessageType } from '../lib/visualization-frame';

const isolatedPolicy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; worker-src 'none'; frame-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
const scriptValue = (value: string) => JSON.stringify(value).replace(/</g, '\\u003c');

// A trusted outer document owns frame-src, so a script inside the untrusted
// inner document cannot navigate itself to the application or an external URL.
export function htmlPreviewShell(source: { html: string; measure?: boolean } | { url: string }): string {
  const local = 'url' in source;
  const url = local ? new URL(source.url, window.location.origin) : undefined;
  if (url && (url.origin !== window.location.origin || !/^\/api\/file-previews\/[A-Za-z0-9_-]+\/resources\//.test(url.pathname))) {
    throw new Error('预览地址不属于当前本地文件。');
  }
  const prefix = url ? `${url.origin}${url.pathname.split('/resources/')[0]}/resources/` : undefined;
  const policy = prefix ? isolatedPolicy.replace("frame-src 'none'", `frame-src ${prefix}`) : isolatedPolicy;
  const inner = local ? source.url : `<!doctype html><meta http-equiv="Content-Security-Policy" content="${isolatedPolicy}">${source.html.replace(/^\s*<!doctype[^>]*>/i, '')}${source.measure ? visualizationFrameMeasurementScript : ''}`;
  return `<!doctype html><meta http-equiv="Content-Security-Policy" content="${policy}"><style>html,body,iframe{margin:0;width:100%;height:100%;border:0;display:block;overflow:hidden}</style><iframe id="content" sandbox="allow-scripts" referrerpolicy="no-referrer" title="预览内容"></iframe><script>
const frame=document.getElementById('content');
const report=type=>parent.postMessage({type},'*');
addEventListener('securitypolicyviolation',()=>report('pulsara-preview-error'));
addEventListener('message',event=>{
  if(event.source!==frame.contentWindow || !event.data || typeof event.data!=='object')return;
  const d=event.data;
  if(d.type==='pulsara-preview-error')report(d.type);
  if(d.type==='${visualizationLayoutMessageType}'){
    if(d.mode==='page')parent.postMessage({type:d.type,mode:'page'},'*');
    if(d.mode==='root' && d.rect && ['x','y','width','height'].every(k=>Number.isFinite(d.rect[k])))
      parent.postMessage({type:d.type,mode:'root',rect:{x:d.rect.x,y:d.rect.y,width:d.rect.width,height:d.rect.height}},'*');
  }
});
frame.addEventListener('load',()=>report('pulsara-preview-ready'));
frame.${local ? 'src' : 'srcdoc'}=${scriptValue(inner)};
</script>`;
}

export function SandboxedHtmlPreview({ source, title, frameRef, style, onStatus }: {
  source: { html: string; measure?: boolean } | { url: string };
  title: string;
  frameRef?: RefObject<HTMLIFrameElement | null>;
  style?: CSSProperties;
  onStatus?: (status: 'ready' | 'error') => void;
}) {
  const ownRef = useRef<HTMLIFrameElement>(null);
  const ref = frameRef ?? ownRef;
  const html = 'html' in source ? source.html : undefined;
  const url = 'url' in source ? source.url : undefined;
  const measure = 'measure' in source && source.measure;
  const shell = useMemo(() => htmlPreviewShell(url !== undefined ? { url } : { html: html ?? '', measure }), [html, url, measure]);
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (event.source !== ref.current?.contentWindow) return;
      if (event.data?.type === 'pulsara-preview-error') onStatus?.('error');
      if (event.data?.type === 'pulsara-preview-ready') onStatus?.('ready');
    };
    window.addEventListener('message', receive);
    return () => window.removeEventListener('message', receive);
  }, [onStatus, ref]);
  return <iframe ref={ref} title={title} sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={shell} style={style} />;
}
