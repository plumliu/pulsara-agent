import { desktopHost } from './desktop-host';

// A renderer cache of the desktop-owned preferences, independent of the runtime's port.
let values: Record<string, string> = {};
export function initializeUiPreferences(saved: Record<string, string>): void { values = { ...saved }; }
function write(key: string, value: string | null): void {
  if ((values[key] ?? null) === value) return;
  if (value === null) delete values[key]; else values[key] = value;
  void desktopHost()?.writeUiPreference?.({ key, value }).catch(error => console.warn('Could not save UI preference', error));
}
export const uiPreferences = {
  getItem: (key: string): string | null => values[key] ?? null,
  setItem: (key: string, value: string): void => write(key, value),
  removeItem: (key: string): void => write(key, null),
};
