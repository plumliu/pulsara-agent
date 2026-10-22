import { afterEach, expect, it, vi } from 'vitest';
import { visualizationEnvironment } from '../../frontend/lib/visualization-environment';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each([true, false])('keeps live visualization motion enabled with OS reduced-motion=%s', reduced => {
  vi.stubGlobal('matchMedia', () => ({ matches: reduced }));
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  expect(visualizationEnvironment(true)).toMatchObject({ visible: true, reducedMotion: false });
  expect(visualizationEnvironment(false)).toMatchObject({ visible: false, reducedMotion: false });
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
  expect(visualizationEnvironment(true)).toMatchObject({ visible: false, reducedMotion: false });
});
