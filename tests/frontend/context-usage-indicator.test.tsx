import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ContextUsageIndicator } from '../../frontend/components/context-usage-indicator';
import type { ContextUsagePreview } from '../../frontend/lib/runtime-adapter';

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} });
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const ready: ContextUsagePreview = {
  state: 'ready', connection_id: 'model-a', input_tokens: 58000,
  input_budget_tokens: 100000, budget_source: 'reported_input_anchor',
  compaction_expected: false, automatic_compaction_available: true,
};
const props = { sessionId: 'session-a', binding: { connection_id: 'model-a' }, revision: 1, isRunning: false };

describe('context usage indicator', () => {
  it('shows usage and opens the tooltip by keyboard or click', async () => {
    render(<ContextUsageIndicator {...props} readUsage={vi.fn(async () => ready)} />);
    const button = await screen.findByRole('button', { name: '上下文约占 58%' });
    fireEvent.click(button);
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('已用约 58,000 tokens')).toBeTruthy();
    expect(screen.getByText('可用输入额度 100,000 tokens')).toBeTruthy();
    fireEvent.keyDown(button, { key: 'Escape' });
    expect(button.getAttribute('aria-expanded')).toBe('false');
  });

  it('marks smaller-model overflow red and bounds the visual ring', async () => {
    const view = render(<ContextUsageIndicator {...props} readUsage={vi.fn(async () => ({
      ...ready, input_tokens: 110000, model_switch_pending: true, compaction_expected: true,
    }))} />);
    fireEvent.click(await screen.findByRole('button', { name: '上下文约占 110%，需要压缩' }));
    expect(view.container.firstElementChild?.getAttribute('data-tone')).toBe('danger');
    expect(view.container.querySelector('.context-usage__fill')?.getAttribute('stroke-dasharray')).toBe('100 100');
  });

  it('dismisses a hovered tooltip with Escape while the composer retains focus', async () => {
    render(<><input aria-label="消息" /><ContextUsageIndicator {...props} readUsage={vi.fn(async () => ready)} /></>);
    const button = await screen.findByRole('button', { name: /58%/ });
    const composer = screen.getByRole('textbox', { name: '消息' });
    composer.focus();
    fireEvent.mouseEnter(button);
    await screen.findByRole('tooltip');
    fireEvent.keyDown(composer, { key: 'Escape' });
    expect(screen.queryByRole('tooltip')).toBeNull();
    expect(document.activeElement).toBe(composer);
  });

  it('clears a previous measurement when the live session becomes unavailable', async () => {
    const readUsage = vi.fn().mockResolvedValueOnce(ready).mockResolvedValue({ state: 'unavailable', connection_id: null });
    const view = render(<ContextUsageIndicator {...props} readUsage={readUsage} />);
    await screen.findByRole('button', { name: /58%/ });
    view.rerender(<ContextUsageIndicator {...props} revision="host-closed" readUsage={readUsage} />);
    await screen.findByRole('button', { name: '上下文占用' });
    expect(view.container.firstElementChild?.getAttribute('data-tone')).toBe('unknown');
    expect(view.container.querySelector('.context-usage__fill')).toBeNull();
  });

  it('aborts old-model reads and rejects their late results after a model switch', async () => {
    let resolveOld!: (value: ContextUsagePreview) => void;
    const readUsage = vi.fn((signal: AbortSignal) => signal.aborted
      ? Promise.resolve(ready)
      : new Promise<ContextUsagePreview>(resolve => { resolveOld = resolve; }));
    const view = render(<ContextUsageIndicator {...props} readUsage={readUsage} />);
    await waitFor(() => expect(readUsage).toHaveBeenCalledOnce());
    const oldSignal = readUsage.mock.calls[0][0];
    const readNew = vi.fn(async () => ({ ...ready, connection_id: 'model-b', input_tokens: 32000, budget_source: 'heuristic' as const }));
    view.rerender(<ContextUsageIndicator {...props} binding={{ connection_id: 'model-b' }} readUsage={readNew} />);
    expect(oldSignal.aborted).toBe(true);
    await screen.findByRole('button', { name: '上下文约占 32%' });
    await act(async () => resolveOld(ready));
    expect(screen.queryByRole('button', { name: '上下文约占 58%' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '上下文约占 32%' }));
    expect(screen.getByText('已用约 32,000 tokens')).toBeTruthy();
  });

  it.each(['empty', 'updating', 'compacting', 'unavailable'] as const)('represents %s as unknown, never zero percent', async state => {
    const view = render(<ContextUsageIndicator {...props} readUsage={vi.fn(async () => ({ state, connection_id: 'model-a' }))} />);
    fireEvent.click(screen.getByRole('button', { name: '上下文占用' }));
    await waitFor(() => expect(screen.getByText('—')).toBeTruthy());
    expect(view.container.firstElementChild?.getAttribute('data-tone')).toBe('unknown');
    expect(view.container.querySelector('.context-usage__fill')).toBeNull();
    expect(screen.queryByText(/约 0%/)).toBeNull();
  });

  it('refreshes after turn completion and compaction revisions', async () => {
    const readUsage = vi.fn(async () => ready);
    const view = render(<ContextUsageIndicator {...props} readUsage={readUsage} />);
    await screen.findByRole('button', { name: /58%/ });
    view.rerender(<ContextUsageIndicator {...props} revision="2:true" isRunning readUsage={readUsage} />);
    await waitFor(() => expect(readUsage).toHaveBeenCalledTimes(2));
    view.rerender(<ContextUsageIndicator {...props} revision="3:false" readUsage={readUsage} />);
    await waitFor(() => expect(readUsage).toHaveBeenCalledTimes(3));
  });

  it('reads again when inspected, including usage settled after the visible reply', async () => {
    const readUsage = vi.fn().mockResolvedValueOnce(ready).mockResolvedValue({ ...ready, input_tokens: 60000 });
    render(<ContextUsageIndicator {...props} readUsage={readUsage} />);
    const button = await screen.findByRole('button', { name: /58%/ });
    fireEvent.mouseEnter(button);
    await screen.findByRole('button', { name: /60%/ });
    expect(screen.getByText('已用约 60,000 tokens')).toBeTruthy();
  });
});
