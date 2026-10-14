'use client';
import { useEffect, useMemo, useRef, useState, type CSSProperties, type RefObject } from 'react';
import { visualizationFrameMeasurementScript, visualizationLayoutMessageType } from '../lib/visualization-frame';

import { environmentBootstrap, environmentMessageType, type VisualizationEnvironment } from '../lib/visualization-environment';

import visualizationShell from '../../src/pulsara_agent/conversation_kernel/visualization_shell.js';
const scriptValue = (value: string) => JSON.stringify(value).replace(/</g, '\\u003c');

type HtmlPreviewSource = { html: string; measure?: boolean; allowTextSelection?: boolean; environment?: VisualizationEnvironment } | { url: string };

// Visualization content is an interactive artifact, outside the reply's text
// selection. Keep editing inside its own controls functional.
const nonselectableContent = `<style>
html,body,body *{-webkit-user-select:none!important;user-select:none!important}
input,textarea,[contenteditable]:not([contenteditable="false"]),[contenteditable]:not([contenteditable="false"]) *{-webkit-user-select:text!important;user-select:text!important}
</style><script>
document.addEventListener('selectstart',event=>{
  const target=event.target instanceof Element?event.target:event.target?.parentElement;
  if(target?.closest('input,textarea') || target?.isContentEditable)return;
  event.preventDefault();
},true);
</script>`;

// A trusted outer document owns frame-src, so a script inside the untrusted
// inner document cannot navigate itself to the application or an external URL.
export function htmlPreviewShell(source: HtmlPreviewSource): string {
  const local = 'url' in source;
  const url = local ? new URL(source.url, window.location.origin) : undefined;
  if (url && (url.origin !== window.location.origin || !/^\/api\/file-previews\/[A-Za-z0-9_-]+\/resources\//.test(url.pathname))) {
    throw new Error('预览地址不属于当前本地文件。');
  }
  const prefix = url ? `${url.origin}${url.pathname.split('/resources/')[0]}/resources/` : undefined;
  const html = local ? '' : `${source.html}${source.measure ? visualizationFrameMeasurementScript : ''}`;
  const bootstrap = local ? '' : `${source.environment ? environmentBootstrap(source.environment) : ''}${source.allowTextSelection === false ? nonselectableContent : ''}`;
  const setup = `
const report=type=>parent.postMessage({type},'*');
let environment=${scriptValue(!local && source.environment ? JSON.stringify(source.environment) : 'null')};
environment=JSON.parse(environment);
const validEnv=e=>e && ['light','dark'].includes(e.theme) && typeof e.visible==='boolean' && typeof e.reducedMotion==='boolean' && e.colors && ['background','foreground','muted','accent','border','soft'].every(k=>typeof e.colors[k]==='string');
const sendEnvironment=()=>{if(environment)frame.contentWindow.postMessage({type:'${environmentMessageType}',environment},'*');};
addEventListener('securitypolicyviolation',()=>report('pulsara-preview-error'));
addEventListener('message',event=>{
  if(event.source===parent && event.data?.type==='${environmentMessageType}' && validEnv(event.data.environment)){
    const e=event.data.environment;
    environment={theme:e.theme,visible:e.visible,reducedMotion:e.reducedMotion,colors:Object.fromEntries(['background','foreground','muted','accent','border','soft'].map(k=>[k,e.colors[k]]))};
    sendEnvironment();return;
  }
  if(event.source!==frame.contentWindow || !event.data || typeof event.data!=='object')return;
  const d=event.data;
  if(d.type==='pulsara-visualization-environment-ready')sendEnvironment();
  if(d.type==='pulsara-preview-error')report(d.type);
  if(d.type==='${visualizationLayoutMessageType}'){
    if(!d.viewport || !Number.isFinite(d.viewport.width) || !Number.isFinite(d.viewport.height))return;
    const viewport={width:d.viewport.width,height:d.viewport.height};
    if(d.mode==='page')parent.postMessage({type:d.type,mode:'page',viewport},'*');
    if(d.mode==='root' && d.rect && ['x','y','width','height'].every(k=>Number.isFinite(d.rect[k])))
      parent.postMessage({type:d.type,mode:'root',viewport,rect:{x:d.rect.x,y:d.rect.y,width:d.rect.width,height:d.rect.height}},'*');
  }
});
frame.addEventListener('load',()=>{sendEnvironment();report('pulsara-preview-ready');});
`;
  return visualizationShell({ html, bootstrap, setup, sourceUrl: local ? source.url : undefined, resourcePrefix: prefix });
}

export function SandboxedHtmlPreview({ source, title, frameRef, style, onStatus }: {
  source: HtmlPreviewSource;
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
  const allowTextSelection = 'allowTextSelection' in source ? source.allowTextSelection : undefined;
  const [initialEnvironment] = useState(() => 'environment' in source ? source.environment : undefined);
  const environment = 'environment' in source ? source.environment : undefined;
  const environmentRef = useRef(environment);
  useEffect(() => {
    environmentRef.current = environment;
    if (environment) ref.current?.contentWindow?.postMessage({ type: environmentMessageType, environment }, '*');
  }, [environment, ref]);
  const shell = useMemo(() => htmlPreviewShell(url !== undefined ? { url } : { html: html ?? '', measure, allowTextSelection, environment: initialEnvironment }), [html, url, measure, allowTextSelection, initialEnvironment]);
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (event.source !== ref.current?.contentWindow) return;
      if (event.data?.type === 'pulsara-preview-error') onStatus?.('error');
      if (event.data?.type === 'pulsara-preview-ready') {
        if (environmentRef.current) ref.current?.contentWindow?.postMessage({type: environmentMessageType, environment: environmentRef.current}, '*');
        onStatus?.('ready');
      }
    };
    window.addEventListener('message', receive);
    return () => window.removeEventListener('message', receive);
  }, [onStatus, ref]);
  return <iframe ref={ref} title={title} sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={shell} style={style} />;
}
