import type { DesktopBrowser } from '../../desktop/src/browser-contract';
export type { BrowserState, BrowserEvent, BrowserCommand, BrowserPresentation, BrowserLayout, BrowserHide, DesktopBrowser } from '../../desktop/src/browser-contract';

export interface CliStatus {
  state: 'ready' | 'missing' | 'outdated' | 'conflict' | 'error';
  path: string;
  existingTarget?: string;
  message?: string;
  managedBy: 'application' | 'package';
  shadowedBy?: string;
}
/** Narrow preload contract. Browser-based component tests deliberately have no host. */
export interface PulsaraDesktop {
  browser: DesktopBrowser;
  platform: 'darwin' | 'linux';
  startupSettled(): Promise<void>;
  readUiPreferences(): Promise<Record<string, string>>;
  writeUiPreference(input: { key: string; value: string | null }): Promise<void>;
  writeClipboardText(text: string): Promise<void>;
  minimize(): Promise<void>;
  toggleMaximize(): Promise<void>;
  closeWindow(): Promise<void>;
  cliStatus(): Promise<CliStatus>;
  repairCli(replaceConflict: boolean): Promise<CliStatus>;
  chooseFiles(): Promise<string[]>;
  chooseDirectory(): Promise<string | null>;
  pathForFile(file: File): string;
  openPath(input: { path: string; reveal: boolean }): Promise<void>;
  openExternal(url: string): Promise<void>;
}
declare global { interface Window { pulsaraDesktop?: PulsaraDesktop } }
export function desktopHost(): PulsaraDesktop | undefined { return typeof window === 'undefined' ? undefined : window.pulsaraDesktop; }

/** Product copy buttons use the trusted desktop host, never preview-page permissions. */
export async function writeClipboardText(text: string): Promise<void> {
  const host = desktopHost();
  if (!host) throw new Error("桌面剪贴板不可用。");
  await host.writeClipboardText(text);
}
