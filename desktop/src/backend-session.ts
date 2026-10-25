import { randomUUID, X509Certificate } from 'node:crypto';
import type { DownloadItem, Session } from 'electron';
import { setTimeout as delay } from 'node:timers/promises';

const pdfExtension = 'mhjfbmdgcfjbbpaeojofohoefgiehjai';
const pem = (value: string) => value.replaceAll('\r\n','\n').trim();

export interface BackendSession {
  readonly session: Session;
  waitUntilReachable(signal?: AbortSignal): Promise<void>;
  seal(): void;
  retire(settleOwners: () => Promise<void>): Promise<void>;
}

/** One immutable launch identity. Main retains Window/IPC/browser ownership. */
export function createBackendSession(options: {origin: string; certificatePem: string}, sessions: Pick<typeof import('electron').session,'fromPartition'> = require('electron').session): BackendSession {
  const endpoint = new URL(options.origin);
  if (endpoint.protocol !== 'https:' || endpoint.hostname !== '127.0.0.1' || endpoint.username || endpoint.password
      || endpoint.pathname !== '/' || endpoint.search || endpoint.hash || endpoint.origin !== options.origin) throw new Error('无效的 Linux HTTPS 工作台地址。');
  const certificate = pem(options.certificatePem);
  new X509Certificate(certificate);
  const ses = sessions.fromPartition('pulsara-workbench-'+randomUUID(),{cache:false});
  let sealed = false;
  let retiring: Promise<void> | undefined;
  let readiness: Promise<void> | undefined;
  const readinessCancellation = new AbortController();
  const downloads = new Map<DownloadItem,{done:Promise<void>;cancel:()=>void}>();
  ses.setCertificateVerifyProc((request,done) => {
    done(!sealed && request.hostname === endpoint.hostname && pem(request.certificate.data) === certificate ? 0 : -2);
  });
  ses.webRequest.onBeforeRequest((request,done) => {
    let allow = false;
    try {
      const url = new URL(request.url);
      allow = !sealed && (url.origin === endpoint.origin
        || (url.protocol === 'chrome-extension:' && url.hostname === pdfExtension)
        || (url.protocol === 'chrome:' && url.hostname === 'resources' && Boolean(request.frame?.url.startsWith('chrome-extension://'+pdfExtension+'/'))));
    } catch { /* A destroyed frame or malformed URL has no admission. */ }
    done({cancel:!allow});
  });
  ses.setPermissionRequestHandler((_contents,_permission,done) => done(false));
  ses.setPermissionCheckHandler(() => false);
  ses.on('will-download',(_event,item) => {
    let finished = false;
    let queued = false;
    let resolve!: () => void;
    let reject!: (error: unknown) => void;
    const done = new Promise<void>((yes,no) => { resolve=yes; reject=no; });
    void done.catch(() => {});
    const cancel = () => {
      if (finished || queued) return;
      queued=true;
      // Leave the native DownloadItem observer before mutating its lifecycle.
      setImmediate(() => { if (!finished) { try { item.cancel(); } catch (error) { reject(error); } } });
    };
    downloads.set(item,{done,cancel});
    item.once('done',() => { finished=true; downloads.delete(item); resolve(); });
    if (sealed) cancel();
  });
  const seal = () => { sealed=true; readinessCancellation.abort(); for (const value of downloads.values()) value.cancel(); };
  return {
    session:ses, seal,
    waitUntilReachable(signal) {
      if (sealed) return Promise.reject(readinessCancellation.signal.reason);
      if (signal?.aborted) return Promise.reject(signal.reason);
      readiness ??= (async () => {
        const cancellation = signal ? AbortSignal.any([readinessCancellation.signal,signal]) : readinessCancellation.signal;
        for (;;) {
          cancellation.throwIfAborted();
          let response;
          try {
            response = await ses.fetch(endpoint.href,{method:'HEAD',redirect:'error',signal:cancellation});
          } catch (error) {
            cancellation.throwIfAborted();
            if (!(error instanceof Error) || error.message !== 'net::ERR_CONNECTION_REFUSED') throw error;
            // WSL publishes Linux readiness before Windows localhost forwarding.
            // This is a read-only sampling cadence, never a startup lifetime cap.
            await delay(250,undefined,{signal:cancellation}).catch(error => { cancellation.throwIfAborted(); throw error; });
            continue;
          }
          await response.body?.cancel();
          if (!response.ok) throw new Error('Windows 工作台就绪检查失败：HTTP '+response.status);
          cancellation.throwIfAborted();
          return;
        }
      })();
      void readiness.catch(() => {});
      return readiness;
    },
    retire(settleOwners) {
      seal();
      retiring ??= Promise.resolve().then(async () => {
        const settled = await Promise.allSettled([settleOwners(), ...(readiness ? [readiness] : [])]);
        await ses.closeAllConnections();
        while (downloads.size) await Promise.all([...downloads.values()].map(value => value.done));
        await ses.clearStorageData();
        if (settled[0].status === 'rejected') throw settled[0].reason;
      });
      return retiring;
    },
  };
}
