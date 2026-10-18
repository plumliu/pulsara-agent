import { afterEach, expect, it, vi } from 'vitest';
import { writeClipboardText, type PulsaraDesktop } from '../../frontend/lib/desktop-host';

afterEach(() => { delete window.pulsaraDesktop; vi.restoreAllMocks(); });

it('copies exact text through the desktop host even when the browser API rejects access', async () => {
  const browserWrite = vi.fn().mockRejectedValue(new Error('denied'));
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: browserWrite } });
  const nativeWrite = vi.fn().mockResolvedValue(undefined);
  window.pulsaraDesktop = { writeClipboardText: nativeWrite } as unknown as PulsaraDesktop;
  const path = '/Users/example/中文 文档/SKILL.md';
  await writeClipboardText(path);
  expect(nativeWrite).toHaveBeenCalledExactlyOnceWith(path);
  expect(browserWrite).not.toHaveBeenCalled();
});

it('reports host write failures to callers for their existing failure feedback', async () => {
  const failure = new Error('clipboard unavailable');
  window.pulsaraDesktop = { writeClipboardText: vi.fn().mockRejectedValue(failure) } as unknown as PulsaraDesktop;
  await expect(writeClipboardText('text')).rejects.toBe(failure);
});

it('rejects without a desktop host', async () => {
  await expect(writeClipboardText('text')).rejects.toThrow('桌面剪贴板不可用');
});
