import { describe, expect, it } from 'vitest';
import {
  usableVisualizationRootRect,
  visualizationFrameMeasurementScript,
  visualizationLayoutMessageType,
} from '../../frontend/lib/visualization-frame';

describe('visualization frame geometry', () => {
  it('accepts a fully visible root and rejects ambiguous or unsafe boxes', () => {
    const box = { x: 100, y: 25, width: 420, height: 300 };
    expect(usableVisualizationRootRect(box, 960)).toEqual(box);
    expect(usableVisualizationRootRect({ ...box, width: 900 }, 960)).toBeNull();
    expect(usableVisualizationRootRect({ ...box, y: -1 }, 960)).toBeNull();
    expect(usableVisualizationRootRect({ ...box, height: Infinity }, 960)).toBeNull();
    expect(usableVisualizationRootRect({ ...box, width: 0 }, 960)).toBeNull();
    expect(usableVisualizationRootRect('not geometry', 960)).toBeNull();
    expect(usableVisualizationRootRect({ ...box, height: 1500 }, 960)?.height).toBe(1500);
  });

  it('selects a crop target and sends geometry, never content', () => {
    expect(visualizationFrameMeasurementScript).toContain('[data-pulsara-visualization-root]');
    expect(visualizationFrameMeasurementScript).toContain(visualizationLayoutMessageType);
    expect(visualizationFrameMeasurementScript).toContain('ResizeObserver');
    expect(visualizationFrameMeasurementScript).not.toContain('innerHTML');
  });
});
