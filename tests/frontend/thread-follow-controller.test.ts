import { afterEach, describe, expect, it, vi } from 'vitest';
import { ThreadFollowController } from '../../frontend/lib/thread-follow-controller';

const controllers: ThreadFollowController[] = [];
afterEach(() => { controllers.splice(0).forEach(controller => controller.dispose()); document.getSelection()?.removeAllRanges(); document.body.replaceChildren(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function setup(padding = 0) {
  const frames = new Map<number, FrameRequestCallback>(); let frameId = 0;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frames.set(++frameId, callback); return frameId; });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
  const root = document.createElement('div'); document.body.append(root);
  root.innerHTML = `<section class="conversation-run" data-thread-tail="false">
    <div class="animated-disclosure conversation-run__drawer"><div data-source-entry="old"><article><button data-disclosure-key="old-thought" aria-expanded="false">旧思考</button><p>旧正文</p></article></div></div>
    <button data-process-toggle data-disclosure-key="old-process" data-history-entry="old" data-process-edge="bottom" aria-expanded="false">旧过程</button>
  </section><section class="conversation-run" data-thread-tail="true">
    <div class="animated-disclosure conversation-run__drawer"><div data-source-entry="tail"><article><button data-disclosure-key="thought" aria-expanded="false">思考</button><button data-disclosure-key="tool" aria-expanded="false">工具</button><p>尾部正文</p></article></div></div>
    <button data-process-toggle data-disclosure-key="process" data-history-entry="tail" data-process-edge="bottom" aria-expanded="true">过程</button>
  </section><span data-thread-tail-end></span>`;
  const geometry = { height: 1000, viewport: 400, top: 600, latest: true };
  Object.defineProperties(root, { scrollHeight: {get: () => geometry.height}, clientHeight: {get: () => geometry.viewport},
    scrollTop: {get: () => geometry.top, set: value => {geometry.top = Math.max(0, Math.min(geometry.height - geometry.viewport, value));}} });
  vi.spyOn(root, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 0, 600, geometry.viewport));
  for (const node of root.querySelectorAll<HTMLElement>('button, article')) {
    const offset = node.closest('[data-thread-tail="true"]') ? 850 : 700;
    vi.spyOn(node, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, offset - geometry.top, 300, 24));
  }
  const end = root.querySelector<HTMLElement>('[data-thread-tail-end]')!;
  vi.spyOn(end, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, geometry.height - padding - geometry.top, 1, 0));
  vi.spyOn(end, 'getClientRects').mockReturnValue([new DOMRect()] as unknown as DOMRectList);
  const visible = vi.fn(), input = vi.fn(), settled = vi.fn();
  const controller = new ThreadFollowController(() => root, () => geometry.latest, visible, input, settled); controllers.push(controller);
  const button = (key: string) => root.querySelector<HTMLElement>(`[data-disclosure-key="${key}"]`)!;
  const toggle = (key: string) => { const node = button(key); controller.disclose(node); node.setAttribute('aria-expanded', String(node.getAttribute('aria-expanded') !== 'true')); controller.coordinate(); };
  const frame = () => { const current = [...frames.values()]; frames.clear(); current.forEach(callback => callback(0)); };
  controller.coordinate();
  return { root, controller, geometry, visible, input, settled, button, toggle, frame, frames };
}

describe('follow intent and actual visible tail', () => {
  it.each([.1, .25, 1, 1.5])('preserves fractional upward intent while hiding latest at %s CSS pixels', distance => {
    const {controller, geometry, visible, frame} = setup();
    controller.input('up'); geometry.top -= distance; controller.scroll(); frame();
    expect(controller.intent).toBe('read'); expect(visible).toHaveBeenLastCalledWith(true);
    geometry.height += 100; controller.coordinate();
    expect(geometry.top).toBe(600 - distance); expect(visible).toHaveBeenLastCalledWith(false);
  });
  it.each([47.9, 48, 48.1, 80])('snaps only on downward return within 48 CSS pixels: %s', distance => {
    const {controller, geometry, frame} = setup(); controller.input('up'); frame();
    geometry.top = 600 - distance; controller.scroll(); frame();
    controller.input('down'); frame();
    expect(controller.intent).toBe(distance <= 48 ? 'follow' : 'read');
    expect(geometry.top).toBe(distance <= 48 ? 600 : 600 - distance);
  });
  it('does not treat layout entering the return zone as downward input', () => {
    const {controller, geometry, frame} = setup(); controller.input('up'); frame();
    geometry.top = 520; controller.scroll(); frame();
    geometry.height = 950; controller.coordinate();
    expect(controller.intent).toBe('read'); expect(geometry.top).toBe(520);
    controller.input('down'); frame(); expect(geometry.top).toBe(550);
  });
  it('restores on a downward gesture at the physical end without a scroll event', () => {
    const {controller, geometry, frame} = setup(); controller.input('up'); frame(); controller.input('down'); frame();
    geometry.height += 30; controller.coordinate(); expect(geometry.top).toBe(630);
  });
  it('excludes decorative padding from visibility and measures CSS pixels independently of DPR', () => {
    vi.stubGlobal('devicePixelRatio', 3);
    const {controller, geometry, visible, frame} = setup(42);
    controller.input('up'); geometry.top = 558.5; controller.scroll(); frame();
    expect(visible).toHaveBeenLastCalledWith(true);
    geometry.top = 556; controller.scroll(); frame(); expect(visible).toHaveBeenLastCalledWith(false);
  });
  it('never calls an old band latest even at its physical bottom', () => {
    const {controller, geometry, visible, frame} = setup(); geometry.latest = false;
    controller.input('down'); frame(); expect(controller.canFollow).toBe(false); expect(visible).toHaveBeenLastCalledWith(false);
  });
  it('recognizes a native upward scrollbar move without pointerdown during resize', () => {
    const {controller, geometry, frame} = setup(); geometry.height -= 200; geometry.top = 399.75; controller.scroll(); frame();
    expect(controller.intent).toBe('read'); geometry.height += 100; controller.coordinate(); expect(geometry.top).toBe(399.75);
  });
  it('accepts browser clamping before resize delivery without changing follow intent', () => {
    const {controller, geometry} = setup(); geometry.height -= 200; geometry.top = 400; controller.scroll();
    geometry.viewport = 300; controller.coordinate(); expect(geometry.top).toBe(500); expect(controller.intent).toBe('follow');
  });
  it('does not write while a scrollbar is held before its first scroll event', () => {
    const {controller, geometry} = setup(); controller.input('drag'); geometry.height += 100; controller.coordinate();
    expect(geometry.top).toBe(600); controller.endInput(); expect(controller.intent).toBe('read');
  });
});

describe('semantic detail protection and animation ownership', () => {
  it.each(['thought', 'tool', 'process'])('keeps tail %s opening, streaming and closing continuously following', key => {
    const {controller, geometry, toggle} = setup(); toggle(key); geometry.height += 1200; controller.coordinate();
    expect(geometry.top).toBe(1800); expect(controller.canFollow).toBe(true);
    toggle(key); for (const height of [2000, 1500, 1100]) { geometry.height = height; controller.coordinate(); expect(geometry.top).toBe(height - 400); }
  });
  it('protects a visible old thought rather than treating the whole latest turn as tail', () => {
    const {controller, geometry, toggle} = setup(); toggle('old-thought'); geometry.height += 500; controller.coordinate();
    expect(controller.intent).toBe('follow'); expect(controller.canFollow).toBe(false); expect(geometry.top).toBe(600);
    toggle('old-thought'); expect(controller.canFollow).toBe(true); expect(geometry.top).toBe(1100);
  });
  it('closing a child retains its parent view until the parent closes', () => {
    const {controller, geometry, toggle} = setup(); toggle('old-process'); toggle('old-thought'); toggle('old-thought');
    geometry.height += 500; controller.coordinate(); expect(geometry.top).toBe(600); expect(controller.canFollow).toBe(false);
    toggle('old-process'); expect(controller.canFollow).toBe(true);
  });
  it('an upward gesture replaces the remembered follow intent before a detail closes', () => {
    const {controller, geometry, toggle, frame} = setup(); toggle('thought'); geometry.height += 1000; controller.coordinate();
    controller.input('up'); geometry.top -= 20; controller.scroll(); frame(); const top = geometry.top;
    toggle('thought'); geometry.height += 100; controller.coordinate(); expect(geometry.top).toBe(top); expect(controller.intent).toBe('read');
  });
  it('finds a replacement semantic control and releases invalid targets', () => {
    const {controller, root, toggle, button, geometry} = setup(); toggle('old-thought');
    const old = button('old-thought'), replacement = old.cloneNode(true) as HTMLElement;
    old.replaceWith(replacement); vi.spyOn(replacement, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 700 - geometry.top, 300, 24));
    geometry.height += 100; controller.coordinate(); expect(controller.canFollow).toBe(false); expect(geometry.top).toBe(600);
    root.querySelector('[data-thread-tail="false"]')!.remove(); controller.coordinate(); expect(controller.canFollow).toBe(true); expect(geometry.top).toBe(700);
  });
  it('keeps original protection when a closing read fails, and ignores stale read completion', () => {
    const {controller, toggle, button} = setup(); toggle('old-thought'); controller.disclose(button('old-thought'));
    const old = controller.pendingDisclosure('old-thought', true);
    controller.disclose(button('old-thought')); const current = controller.pendingDisclosure('old-thought', true);
    controller.pendingDisclosure('old-thought', false, true, old); expect(current).not.toBe(old);
    expect(controller.canFollow).toBe(false); controller.pendingDisclosure('old-thought', false, true, current);
    controller.coordinate(); expect(controller.canFollow).toBe(false);
  });
  it('coordinates every live drawer frame, then stops; unrelated infinite spinners never poll', () => {
    const {controller, root, geometry, frame, frames} = setup();
    const drawer = root.querySelector('.animated-disclosure')!;
    let active = true;
    Object.defineProperty(root, 'getAnimations', {configurable: true, value: () => active ? [{playState: 'running', effect: {target: drawer}}] : []});
    controller.coordinate(); geometry.height += 40; frame(); expect(geometry.top).toBe(640); expect(frames.size).toBe(1);
    active = false; frame(); expect(frames.size).toBe(0);
    Object.defineProperty(root, 'getAnimations', {configurable: true, value: () => [{playState: 'running', effect: {target: root.querySelector('p')}}]});
    controller.coordinate(); expect(frames.size).toBe(0);
  });
  it('selection retains intent, blocks streaming and restores after clearing even far from latest', () => {
    const {controller, root, geometry} = setup();
    vi.spyOn(Range.prototype, 'getClientRects').mockReturnValue([new DOMRect(0, 20, 100, 20)] as unknown as DOMRectList);
    const range = document.createRange(); range.selectNodeContents(root.querySelector('p')!); document.getSelection()!.addRange(range);
    controller.selectionChanged(); geometry.height += 500; controller.coordinate(); expect(geometry.top).toBe(600); expect(controller.intent).toBe('follow'); expect(controller.canFollow).toBe(false);
    document.getSelection()!.removeAllRanges(); controller.selectionChanged(); expect(geometry.top).toBe(1100);
  });
  it('disposed navigation/animation callbacks cannot write a successor session', () => {
    const {controller, geometry, frame} = setup(); controller.input('down'); controller.dispose(); geometry.height += 300;
    controller.coordinate(); controller.latest(); frame(); expect(geometry.top).toBe(600);
  });
});

it('keeps the programmed scroll event owned when new geometry arrives before resize delivery', () => {
  const {controller, geometry} = setup();
  geometry.height = 1100; controller.coordinate(); expect(geometry.top).toBe(700);
  // The event from that write is delivered after another token changes layout.
  geometry.height = 1200; controller.scroll(); controller.coordinate();
  expect(controller.intent).toBe('follow'); expect(geometry.top).toBe(800);
});
