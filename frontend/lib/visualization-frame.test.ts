import { describe, expect, it } from 'vitest';
import {
  usableVisualizationRootRect,
  visualizationFrameMeasurementScript,
  visualizationLayoutMessageType,
} from './visualization-frame';

describe('visualization frame geometry', () => {
  it('accepts a fully visible root and rejects ambiguous or unsafe boxes', () => {
    const box = { x: 100, y: 25, width: 420, height: 300 };
    expect(usableVisualizationRootRect(box, 960, 560)).toEqual(box);
    expect(usableVisualizationRootRect({ ...box, width: 900 }, 960, 560)).toBeNull();
    expect(usableVisualizationRootRect({ ...box, y: -1 }, 960, 560)).toBeNull();
    expect(usableVisualizationRootRect({ ...box, height: Infinity }, 960, 560)).toBeNull();
    expect(usableVisualizationRootRect({ ...box, width: 0 }, 960, 560)).toBeNull();
    expect(usableVisualizationRootRect('not geometry', 960, 560)).toBeNull();
  });

  it('observes only the explicit marker and sends geometry, never content', () => {
    expect(visualizationFrameMeasurementScript).toContain('[data-pulsara-visualization-root]');
    expect(visualizationFrameMeasurementScript).toContain(visualizationLayoutMessageType);
    expect(visualizationFrameMeasurementScript).toContain('ResizeObserver');
    expect(visualizationFrameMeasurementScript).not.toContain('innerHTML');
  });
});
