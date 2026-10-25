import { contextBridge, ipcRenderer, webUtils } from 'electron';
import type { BrowserCommand, BrowserEvent, BrowserHide, BrowserLayout, BrowserPresentation, DesktopBrowser } from './browser-contract';

const browser: DesktopBrowser = {
  create: (input: { sessionId: string; tabId: string }) => ipcRenderer.invoke('desktop:browser-create', input),
  command: (input: BrowserCommand) => ipcRenderer.invoke('desktop:browser-command', input),
  close: (instanceId: string) => ipcRenderer.invoke('desktop:browser-close', instanceId),
  forgetSession: (sessionId: string) => ipcRenderer.invoke('desktop:browser-forget-session', sessionId),
  clear: () => ipcRenderer.invoke('desktop:browser-clear'),
  activate: (input: BrowserPresentation) => ipcRenderer.invoke('desktop:browser-activate', input),
  layout: (input: BrowserLayout) => ipcRenderer.invoke('desktop:browser-layout', input),
  hide: (input: BrowserHide) => ipcRenderer.invoke('desktop:browser-hide', input),
  onEvent: (listener: (event: BrowserEvent) => void) => {
    const receive = (_event: Electron.IpcRendererEvent, value: BrowserEvent) => listener(value);
    ipcRenderer.on('desktop:browser-event', receive);
    return () => { ipcRenderer.removeListener('desktop:browser-event', receive); };
  },
};

// No Node primitives or generic IPC channel are exposed. Main validates frame and origin too.
if (process.isMainFrame) contextBridge.exposeInMainWorld('pulsaraDesktop', {
  browser,
  platform: process.platform,
  startupSettled: () => ipcRenderer.invoke('desktop:startup-settled'),
  focus: () => ipcRenderer.invoke('desktop:focus'),
  readUiPreferences: () => ipcRenderer.invoke('desktop:read-ui-preferences'),
  writeUiPreference: (value: {key: string; value: string | null}) => ipcRenderer.invoke('desktop:write-ui-preference', value),
  writeClipboardText: (text: string) => ipcRenderer.invoke('desktop:write-clipboard-text', text),
  minimize: () => ipcRenderer.invoke('desktop:minimize'),
  toggleMaximize: () => ipcRenderer.invoke('desktop:maximize'),
  closeWindow: () => ipcRenderer.invoke('desktop:close'),
  cliStatus: () => ipcRenderer.invoke('desktop:cli-status'),
  repairCli: (replaceConflict: boolean) => ipcRenderer.invoke('desktop:repair-cli', replaceConflict),
  chooseFiles: () => ipcRenderer.invoke('desktop:choose-files'),
  chooseDirectory: () => ipcRenderer.invoke('desktop:choose-directory'),
  chooseWorkspaceDirectory: () => ipcRenderer.invoke('desktop:choose-workspace-directory'),
  pathForFile: (file: File) => {
    const path = webUtils.getPathForFile(file);
    return process.platform === 'win32' && path ? ipcRenderer.sendSync('desktop:backend-path', path) : path;
  },
  wslDiscover: () => ipcRenderer.invoke('desktop:wsl-discover'),
  wslInspect: (distro: string) => ipcRenderer.invoke('desktop:wsl-inspect', distro),
  wslSelect: (selection: {distro: string; user: string}) => ipcRenderer.invoke('desktop:wsl-select', selection),
  openPath: (value: {path: string; reveal: boolean}) => ipcRenderer.invoke('desktop:open-path', value),
  openExternal: (url: string) => ipcRenderer.invoke('desktop:open-external', url),
  retryStartup: () => ipcRenderer.invoke('desktop:retry'),
  resetDatabase: () => ipcRenderer.invoke('desktop:reset-database'),
  showLogs: () => ipcRenderer.invoke('desktop:logs'),
  quit: () => ipcRenderer.invoke('desktop:quit'),
});
