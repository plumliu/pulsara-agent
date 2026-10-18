import { contextBridge, ipcRenderer, webUtils } from 'electron';

// No Node primitives or generic IPC channel are exposed. Main validates frame and origin too.
if (process.isMainFrame) contextBridge.exposeInMainWorld('pulsaraDesktop', {
  platform: process.platform,
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
  pathForFile: (file: File) => webUtils.getPathForFile(file),
  openPath: (value: {path: string; reveal: boolean}) => ipcRenderer.invoke('desktop:open-path', value),
  openExternal: (url: string) => ipcRenderer.invoke('desktop:open-external', url),
  retryStartup: () => ipcRenderer.invoke('desktop:retry'),
  resetDatabase: () => ipcRenderer.invoke('desktop:reset-database'),
  showLogs: () => ipcRenderer.invoke('desktop:logs'),
  quit: () => ipcRenderer.invoke('desktop:quit'),
});
