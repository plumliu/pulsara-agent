import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";
import {
  visualizationFrameMeasurementScript,
  visualizationLayoutMessageType,
} from "../lib/visualization-frame";

import {
  environmentBootstrap,
  environmentMessageType,
  type VisualizationEnvironment,
} from "../lib/visualization-environment";

import visualizationShell from "../../src/pulsara_agent/conversation_kernel/visualization_shell.js";
const scriptValue = (value: string) =>
  JSON.stringify(value).replace(/</g, "\\u003c");

type HtmlPreviewSource =
  | {
      html: string;
      measure?: boolean;
      allowTextSelection?: boolean;
      environment?: VisualizationEnvironment;
    }
  | { url: string };

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
  const local = "url" in source;
  const url = local ? new URL(source.url, window.location.origin) : undefined;
  if (
    url &&
    (url.origin !== window.location.origin ||
      !/^\/api\/file-previews\/[A-Za-z0-9_-]+\/resources\//.test(url.pathname))
  ) {
    throw new Error("预览地址不属于当前本地文件。");
  }
  const prefix = url
    ? `${url.origin}${url.pathname.split("/resources/")[0]}/resources/`
    : undefined;
  const html = local
    ? ""
    : `${source.html}${source.measure ? visualizationFrameMeasurementScript : ""}`;
  const bootstrap = local
    ? ""
    : `<script>addEventListener('click',event=>{const link=event.target.closest?.('a[href]');if(link && !link.getAttribute('href').startsWith('#')){event.preventDefault();event.stopImmediatePropagation();parent.postMessage({type:'pulsara-snapshot-target-unavailable'},'*');}},true);</script>${source.environment ? environmentBootstrap(source.environment) : ""}${source.allowTextSelection === false ? nonselectableContent : ""}`;
  const token = url?.pathname.split("/")[3];
  const setup = `
const report=type=>parent.postMessage({type},'*');
let environment=${scriptValue(!local && source.environment ? JSON.stringify(source.environment) : "null")};
environment=JSON.parse(environment);
const validEnv=e=>e && ['light','dark'].includes(e.theme) && typeof e.visible==='boolean' && typeof e.reducedMotion==='boolean' && e.colors && ['background','foreground','muted','accent','border','soft'].every(k=>typeof e.colors[k]==='string');
const sendEnvironment=()=>{if(environment)frame.contentWindow.postMessage({type:'${environmentMessageType}',environment},'*');};
addEventListener('securitypolicyviolation',()=>report('pulsara-preview-error'));
addEventListener('message',event=>{
  if(event.source===parent && event.data?.type==='pulsara-preview-restore'){frame.contentWindow.postMessage(event.data,'*');return;}
  if(event.source===parent && event.data?.type==='${environmentMessageType}' && validEnv(event.data.environment)){
    const e=event.data.environment;
    environment={theme:e.theme,visible:e.visible,reducedMotion:e.reducedMotion,colors:Object.fromEntries(['background','foreground','muted','accent','border','soft'].map(k=>[k,e.colors[k]]))};
    sendEnvironment();return;
  }
  if(event.source!==frame.contentWindow || !event.data || typeof event.data!=='object')return;
  const d=event.data;
  if(${JSON.stringify(local)} && d.token===${JSON.stringify(token)} && ['pulsara-preview-navigation','pulsara-preview-title','pulsara-preview-position'].includes(d.type)){parent.postMessage({...d,pulsaraUserActivated:navigator.userActivation?.isActive===true},'*');return;}
  if(d.type==='pulsara-snapshot-target-unavailable')report(d.type);
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
  return visualizationShell({
    html,
    bootstrap,
    setup,
    sourceUrl: local ? source.url : undefined,
    resourcePrefix: url?.href ?? prefix,
  });
}

export function SandboxedHtmlPreview({
  source,
  title,
  frameRef,
  style,
  onStatus,
  onNavigation,
  onTitle,
  onPosition,
  position,
  anchor,
}: {
  source: HtmlPreviewSource;
  title: string;
  frameRef?: RefObject<HTMLIFrameElement | null>;
  style?: CSSProperties;
  onStatus?: (status: "ready" | "error") => void;
  onNavigation?: (href: string, newTab: boolean) => void;
  onTitle?: (title: string) => void;
  onPosition?: (position: { top: number; left: number }) => void;
  position?: { top: number; left: number };
  anchor?: string;
}) {
  const [pendingWebpage, setPendingWebpage] = useState<{ href: string; newTab: boolean }>();
  const [blockedSnapshot, setBlockedSnapshot] = useState(false);
  const ownRef = useRef<HTMLIFrameElement>(null);
  const ref = frameRef ?? ownRef;
  const html = "html" in source ? source.html : undefined;
  const url = "url" in source ? source.url : undefined;
  const measure = "measure" in source && source.measure;
  const allowTextSelection =
    "allowTextSelection" in source ? source.allowTextSelection : undefined;
  const [initialEnvironment] = useState(() =>
    "environment" in source ? source.environment : undefined,
  );
  const environment = "environment" in source ? source.environment : undefined;
  const environmentRef = useRef(environment);
  useEffect(() => {
    environmentRef.current = environment;
    if (environment)
      ref.current?.contentWindow?.postMessage(
        { type: environmentMessageType, environment },
        "*",
      );
  }, [environment, ref]);
  const shell = useMemo(
    () =>
      htmlPreviewShell(
        url !== undefined
          ? { url }
          : {
              html: html ?? "",
              measure,
              allowTextSelection,
              environment: initialEnvironment,
            },
      ),
    [html, url, measure, allowTextSelection, initialEnvironment],
  );
  useLayoutEffect(() => {
    const receive = (event: MessageEvent) => {
      if (event.source !== ref.current?.contentWindow) return;
      if (
        url &&
        event.data?.token ===
          new URL(url, window.location.origin).pathname.split("/")[3]
      ) {
        if (
          event.data.type === "pulsara-preview-navigation" &&
          typeof event.data.href === "string"
        ) {
          let target: URL;
          try { target = new URL(event.data.href, new URL(url, window.location.origin)); } catch { return; }
          if (['http:', 'https:'].includes(target.protocol) && target.origin !== window.location.origin) {
            const box = ref.current?.getBoundingClientRect();
            const visible = box && box.width > 0 && box.height > 0 && box.bottom > 0 && box.top < innerHeight && box.right > 0 && box.left < innerWidth && !ref.current?.closest('[inert]') && getComputedStyle(ref.current!).visibility !== 'hidden';
            // Source+token authenticate the document, never its claimed userGesture.
            if (visible && event.data.pulsaraUserActivated === true) onNavigation?.(target.href, event.data.newTab === true);
            else setPendingWebpage({ href: target.href, newTab: event.data.newTab === true });
          } else onNavigation?.(event.data.href, event.data.newTab === true);
        }
        if (
          event.data.type === "pulsara-preview-title" &&
          typeof event.data.title === "string"
        )
          onTitle?.(event.data.title);
        if (
          event.data.type === "pulsara-preview-position" &&
          Number.isFinite(event.data.top) &&
          Number.isFinite(event.data.left)
        )
          onPosition?.({ top: event.data.top, left: event.data.left });
      }
      if (!url && event.data?.type === "pulsara-snapshot-target-unavailable")
        setBlockedSnapshot(true);
      if (event.data?.type === "pulsara-preview-error") onStatus?.("error");
      if (event.data?.type === "pulsara-preview-ready") {
        if (environmentRef.current)
          ref.current?.contentWindow?.postMessage(
            {
              type: environmentMessageType,
              environment: environmentRef.current,
            },
            "*",
          );
        ref.current?.contentWindow?.postMessage(
          { type: "pulsara-preview-restore", ...position, anchor },
          "*",
        );
        onStatus?.("ready");
      }
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [onStatus, ref, url, onNavigation, onTitle, onPosition, position, anchor]);
  useEffect(() => {
    if (anchor)
      ref.current?.contentWindow?.postMessage(
        { type: "pulsara-preview-restore", anchor },
        "*",
      );
  }, [anchor, ref]);
  useEffect(() => { setPendingWebpage(undefined); }, [url]);
  return (
    <>
      {pendingWebpage && <div className="file-preview-html-status"><span>{pendingWebpage.href}</span> <button type="button" onClick={() => { onNavigation?.(pendingWebpage.href, pendingWebpage.newTab); setPendingWebpage(undefined); }}>打开网页</button></div>}
      {blockedSnapshot && (
        <div role="status" className="file-preview-html-status">
          此快照未包含目标文档
        </div>
      )}
      <iframe
        ref={ref}
        title={title}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        srcDoc={shell}
        style={style}
      />
    </>
  );
}
