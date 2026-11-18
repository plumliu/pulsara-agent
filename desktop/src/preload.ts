import { contextBridge, ipcRenderer, webUtils } from 'electron';
import type { BrowserCommand, BrowserEvent, DesktopBrowser } from './browser-contract';

const browserListeners = new Set<(event: BrowserEvent) => void>();
const browser: DesktopBrowser = {
  create: (input: { tabId: string }) => ipcRenderer.invoke('desktop:browser-create', input),
  command: (input: BrowserCommand) => ipcRenderer.invoke('desktop:browser-command', input),
  close: (instanceId: string) => ipcRenderer.invoke('desktop:browser-close', instanceId),
  onEvent: (listener: (event: BrowserEvent) => void) => {
    browserListeners.add(listener);
    return () => { browserListeners.delete(listener); };
  },
};

// No Node primitives or generic IPC channel are exposed. Main validates frame and origin too.
if (process.isMainFrame) ipcRenderer.on('desktop:browser-event', (_event, value: BrowserEvent) => {
  for (const listener of browserListeners) listener(value);
});

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
  restartWorkbench: () => ipcRenderer.invoke('desktop:restart-workbench'),
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
  resetLocalData: (confirmed: boolean) => ipcRenderer.invoke('desktop:reset-local-data', confirmed),
  resetDatabase: () => ipcRenderer.invoke('desktop:reset-database'),
  showLogs: () => ipcRenderer.invoke('desktop:logs'),
  quit: () => ipcRenderer.invoke('desktop:quit'),
});
