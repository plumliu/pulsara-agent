import type { WebContents } from 'electron';

/** The workbench owns user-triggered fullscreen; embedded documents keep their own boundary. */
export function installWorkbenchPermissions(contents: WebContents, origin: () => string | undefined) {
  const allowed = (requester: WebContents | null, permission: string, details: {isMainFrame: boolean; requestingUrl?: string}) => {
    const expected = origin();
    if (requester !== contents || permission !== 'fullscreen' || !details.isMainFrame || !expected || !details.requestingUrl) return false;
    try { return new URL(details.requestingUrl).origin === expected; }
    catch { return false; }
  };
  contents.session.setPermissionRequestHandler((requester, permission, callback, details) => callback(allowed(requester, permission, details)));
  contents.session.setPermissionCheckHandler((requester, permission, _requestingOrigin, details) => allowed(requester, permission, details));
}
