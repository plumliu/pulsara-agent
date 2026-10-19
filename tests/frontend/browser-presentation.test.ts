import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { presentBrowser } from '../../frontend/lib/browser-presentation';
import type { BrowserHide, BrowserLayout, BrowserPresentation, DesktopBrowser } from '../../frontend/lib/desktop-host';

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}
const disposers: Array<() => void> = [];
const pending: Array<ReturnType<typeof deferred>> = [];
let frames: Map<number, FrameRequestCallback>;
let frameId = 0;
async function mutations() { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); }
async function frame() {
  const current = [...frames.values()]; frames.clear();
  for (const callback of current) callback(0);
  await mutations();
}
function geometry(element: Element, x: number, y: number, width: number, height: number) {
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue(new DOMRect(x, y, width, height));
}
function fixture() {
  document.body.innerHTML = '<main class="desktop-shell"><aside class="desktop-workspace"><div class="workspace-resizer"></div><div id="slot"></div></aside></main>';
  const pane = document.querySelector('.desktop-workspace')!;
  const resizer = document.querySelector('.workspace-resizer')!;
  const slot = document.querySelector<HTMLElement>('#slot')!;
  geometry(pane, 600, 40, 400, 700); geometry(resizer, 600, 40, 11, 700); geometry(slot, 606, 100, 394, 600);
  const activate = vi.fn((_input: BrowserPresentation) => Promise.resolve());
  const layout = vi.fn((_input: BrowserLayout) => Promise.resolve());
  const hide = vi.fn((_input: BrowserHide) => { const operation = deferred(); pending.push(operation); return operation.promise; });
  // Only the three presentation bridge methods participate in these DOM tests.
  const host = { activate, layout, hide } as unknown as DesktopBrowser;
  const errors = vi.fn();
  const mount = (id = 'native-one') => {
    const cleanup = presentBrowser(host, id, slot, errors);
    let disposed = false;
    const dispose = () => { if (!disposed) { disposed = true; cleanup(); } };
    disposers.push(dispose); return dispose;
  };
  return { slot, resizer, activate, layout, hide, errors, mount };
}
function modal() {
  const element = document.createElement('section');
  element.setAttribute('role', 'dialog'); element.setAttribute('aria-modal', 'true');
  element.innerHTML = '<input aria-label="Dialog input">';
  geometry(element, 500, 150, 300, 300);
  document.body.append(element);
  return element;
}

beforeEach(() => {
  frames = new Map();
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { const id = ++frameId; frames.set(id, callback); return id; });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
});
afterEach(async () => {
  for (const dispose of disposers.splice(0)) dispose();
  for (const operation of pending.splice(0)) operation.resolve();
  await mutations();
  document.body.replaceChildren(); document.documentElement.style.removeProperty('--browser-toast-right');
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe('native browser presentation coordination', () => {
  it('measures the actual drag hit target and stays idle without a transition', async () => {
    const ui = fixture(); ui.mount(); await mutations();
    expect(ui.layout).toHaveBeenLastCalledWith(expect.objectContaining({ bounds: { x: 611, y: 100, width: 389, height: 600 }, visible: true }));
    expect(frames.size).toBe(0);
    ui.resizer.setAttribute('style', 'display:none');
    window.dispatchEvent(new Event('resize')); await frame();
    expect(ui.layout).toHaveBeenLastCalledWith(expect.objectContaining({ bounds: { x: 606, y: 100, width: 394, height: 600 } }));
    expect(frames.size).toBe(0);
  });

  it('gates every nested modal arriving before hide resolves and restores only after the last modal closes', async () => {
    const ui = fixture(); ui.mount(); await mutations();
    const first = modal(); await mutations();
    expect(first.style.visibility).toBe('hidden'); expect(ui.hide).toHaveBeenCalledTimes(1);
    const second = modal(); await mutations();
    expect(second.style.visibility).toBe('hidden'); expect(ui.hide).toHaveBeenCalledTimes(1);
    first.remove(); await mutations();
    const layoutsBefore = ui.layout.mock.calls.length;
    pending[0].resolve(); await mutations(); await frame();
    expect(second.style.visibility).toBe('');
    expect(ui.layout).toHaveBeenCalledTimes(layoutsBefore);
    second.remove(); await mutations(); await frame();
    expect(ui.layout).toHaveBeenCalledTimes(layoutsBefore + 1);
    expect(ui.layout).toHaveBeenLastCalledWith(expect.objectContaining({ visible: true }));
  });

  it('does not race a new visible layout against an unresolved hide when a modal closes quickly', async () => {
    const ui = fixture(); ui.mount(); await mutations();
    const overlay = modal(); await mutations();
    const layoutsBefore = ui.layout.mock.calls.length;
    overlay.remove(); await mutations(); window.dispatchEvent(new Event('resize')); await frame();
    expect(ui.layout).toHaveBeenCalledTimes(layoutsBefore);
    pending[0].resolve(); await mutations(); await frame();
    expect(ui.layout).toHaveBeenCalledTimes(layoutsBefore + 1);
  });

  it('keeps a shared overlay gated until both the old cleanup and new display owner have hidden', async () => {
    const ui = fixture(); const old = ui.mount('old'); await mutations();
    const overlay = modal(); await mutations();
    old();
    expect(overlay.style.visibility).toBe('hidden');
    ui.mount('new'); await mutations();
    expect(ui.hide).toHaveBeenCalledTimes(3);
    const layoutsBefore = ui.layout.mock.calls.length;
    pending[2].resolve(); await mutations(); await frame();
    expect(overlay.style.visibility).toBe('hidden');
    pending[0].resolve(); await mutations();
    expect(overlay.style.visibility).toBe('hidden');
    pending[1].resolve(); await mutations(); await frame();
    expect(overlay.style.visibility).toBe('');
    expect(ui.layout).toHaveBeenCalledTimes(layoutsBefore);
    overlay.remove(); await mutations(); await frame();
    expect(ui.layout).toHaveBeenLastCalledWith(expect.objectContaining({ instanceId: 'new', visible: true }));
  });

  it('never publishes layout after cleanup even if activation resolves late', async () => {
    const ui = fixture(); const activation = deferred();
    ui.activate.mockImplementation(() => activation.promise);
    const cleanup = ui.mount(); cleanup(); activation.resolve();
    await mutations(); window.dispatchEvent(new Event('resize')); await frame();
    expect(ui.layout).not.toHaveBeenCalled();
    expect(ui.hide).toHaveBeenCalledWith(ui.activate.mock.calls[0][0]);
  });

  it('restores the gated dialog input after hide, without stealing a later explicit focus choice', async () => {
    const ui = fixture(); ui.mount(); await mutations();
    const first = modal(); const input = first.querySelector('input')!; input.focus(); await mutations();
    // jsdom does not blur controls when CSS visibility changes; reproduce the
    // browser's resulting body focus, while retaining real DOM focus operations.
    input.blur(); pending[0].resolve(); await mutations();
    expect(document.activeElement).toBe(input);
    first.remove(); await mutations(); await frame();
    const second = modal(); second.querySelector('input')!.focus(); await mutations();
    const other = document.createElement('button'); document.body.append(other); other.focus();
    pending[1].resolve(); await mutations();
    expect(document.activeElement).toBe(other);
  });

  it('restores the most recent dialog focus when nested overlays were both gated', async () => {
    const ui = fixture(); ui.mount(); await mutations();
    const first = modal(); const firstInput = first.querySelector('input')!;
    firstInput.focus(); await mutations(); firstInput.blur();
    const second = modal(); const secondInput = second.querySelector('input')!;
    secondInput.focus(); await mutations(); secondInput.blur();
    pending[0].resolve(); await mutations();
    expect(document.activeElement).toBe(secondInput);
  });
});
