import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import path from 'node:path';

function knownKey(key: string): boolean {
  return ['pulsara-theme', 'pulsara-active-session', 'pulsara-show-builtin-tool-results', 'pulsara-sidebar-open'].includes(key)
    || /^pulsara-workspace-(?:open|width):session:[a-z2-7]{26}$/.test(key);
}
function valid(key: string, value: unknown): value is string {
  if (key === 'pulsara-theme') return value === 'light' || value === 'dark';
  if (key === 'pulsara-active-session') return value === '' || (typeof value === 'string' && /^session:[a-z2-7]{26}$/.test(value));
  if (/^pulsara-workspace-width:session:[a-z2-7]{26}$/.test(key)) return typeof value === 'string' && Number.isFinite(Number(value)) && Number(value) >= 320;
  if (key === 'pulsara-sidebar-open' || key === 'pulsara-show-builtin-tool-results' || /^pulsara-workspace-open:session:[a-z2-7]{26}$/.test(key)) return value === 'true' || value === 'false';
  return false;
}

/** Disposable desktop presentation preferences; no runtime or model configuration. */
export class UiPreferences {
  private values: Record<string, string> = {};
  constructor(private readonly file: string) {
    try {
      const saved: unknown = JSON.parse(readFileSync(file, 'utf8'));
      if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
        for (const [key, value] of Object.entries(saved)) if (valid(key, value)) this.values[key] = value;
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') console.warn('Could not read UI preferences', error);
    }
  }
  read(): Record<string, string> { return { ...this.values }; }
  write(input: unknown): void {
    if (!input || typeof input !== 'object') throw new Error('无效的界面偏好。');
    const { key, value } = input as {key?: unknown; value?: unknown};
    if (typeof key !== 'string' || !knownKey(key) || (value !== null && !valid(key, value))) throw new Error('无效的界面偏好。');
    const next = { ...this.values };
    if (value === null) delete next[key]; else next[key] = value as string;
    mkdirSync(path.dirname(this.file), { recursive: true });
    writeFileSync(`${this.file}.tmp`, JSON.stringify(next), { mode: 0o600 });
    renameSync(`${this.file}.tmp`, this.file);
    this.values = next;
  }
}
