import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  BackgroundProcess,
  BackgroundProcessLog,
  CommandReceipt,
  UserControlCommandRef,
} from '../lib/runtime-adapter';
import { BackgroundTerminalPanel } from './background-terminal-panel';

afterEach(() => { vi.unstubAllGlobals(); cleanup(); });

const process: BackgroundProcess = {
  processId: 'process-a', command: 'printf long-output', cwd: '/tmp/probe',
  status: 'running', physicalState: 'RUNNING', ioMode: 'pipe', streamId: 'stream-a',
  outputCursor: '0', retainedFromCursor: '0', durationSeconds: 1, timedOut: false,
  originTurnId: 'turn-a',
};

describe('BackgroundTerminalPanel PR03 controls', () => {
  it('freezes one exact reference, shows retained output, and keeps partial failure visible', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'control-uuid' });
    const terminate = vi.fn(async (reference: UserControlCommandRef): Promise<CommandReceipt> => ({
      commandId: reference.commandId,
      status: 'failed',
      publicMessage: '进程已结束，但 monitor 关闭失败。',
      userControl: {
        accepted: true, execution: 'FINISHED',
        feedback: {
          canonicalStatus: 'ACCEPTED', inclusionStatus: 'INCLUDED', ownerAvailability: 'AVAILABLE',
          transportInvocationAttempted: true, transportInvocationSucceeded: true,
        },
      },
    }));
    render(<BackgroundTerminalPanel
      ownerKey="session-a:1:host-a"
      sessionId="session-a"
      hostSessionId="host-a"
      controlAdmissionDeadlineMs={42_000}
      canControl
      loadProcesses={vi.fn(async () => ({ processes: [process] }))}
      readLog={vi.fn(async () => ({
        process, output: 'line-1\nline-2', outputCursor: '2', retainedFromCursor: '0',
        gapBeforeOutput: false, truncatedByResponseBound: false, sourceCoverage: 'COMPLETE',
      }))}
      terminateProcess={terminate}
      queryControl={vi.fn(async () => ({ status: 'RESULT_UNAVAILABLE' as const }))}
    />);
    fireEvent.click((await screen.findByText('printf long-output')).closest('button')!);
    expect(await screen.findByText(/line-1/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
    await waitFor(() => expect(terminate).toHaveBeenCalledWith(expect.objectContaining({
      commandId: 'command:control:42000:control-uuid',
      sessionId: 'session-a', hostSessionId: 'host-a', targetId: 'process-a',
    })));
    expect(screen.getByText('进程已结束，但 monitor 关闭失败。')).toBeTruthy();
    expect(screen.getByText(/已编入原轮次模型请求/)).toBeTruthy();
  });

  it('invalidates a late log page when the owner changes', async () => {
    let resolve!: (value: BackgroundProcessLog) => void;
    const pending = new Promise<BackgroundProcessLog>((accept) => { resolve = accept; });
    const common = {
      controlAdmissionDeadlineMs: 42_000,
      canControl: false,
      loadProcesses: vi.fn(async () => ({ processes: [process] })),
      terminateProcess: vi.fn(),
      queryControl: vi.fn(),
    };
    const view = render(<BackgroundTerminalPanel
      ownerKey="session-a:1:host-a" sessionId="session-a" hostSessionId="host-a"
      {...common}
      readLog={vi.fn(async () => pending)}
    />);
    fireEvent.click((await screen.findByText('printf long-output')).closest('button')!);
    view.rerender(<BackgroundTerminalPanel
      ownerKey="session-a:2:host-b" sessionId="session-a" hostSessionId="host-b"
      {...common}
      readLog={vi.fn(async () => ({
        process, output: 'new-owner-output', outputCursor: '1', retainedFromCursor: '0',
        gapBeforeOutput: false, truncatedByResponseBound: false, sourceCoverage: 'COMPLETE',
      }))}
    />);
    resolve({
      process, output: 'stale-owner-output', outputCursor: '9', retainedFromCursor: '0',
      gapBeforeOutput: false, truncatedByResponseBound: false, sourceCoverage: 'COMPLETE',
    });
    await Promise.resolve();
    expect(screen.queryByText(/stale-owner-output/)).toBeNull();
  });

  it('invalidates a late log page when the process row is collapsed', async () => {
    let resolve!: (value: BackgroundProcessLog) => void;
    const pending = new Promise<BackgroundProcessLog>((accept) => { resolve = accept; });
    const readLog = vi.fn()
      .mockImplementationOnce(async () => pending)
      .mockImplementation(async () => new Promise<BackgroundProcessLog>(() => {}));
    render(<BackgroundTerminalPanel
      ownerKey="session-a:1:host-a"
      sessionId="session-a"
      hostSessionId="host-a"
      controlAdmissionDeadlineMs={42_000}
      canControl={false}
      loadProcesses={vi.fn(async () => ({ processes: [process] }))}
      readLog={readLog}
      terminateProcess={vi.fn()}
      queryControl={vi.fn()}
    />);
    const toggle = (await screen.findByText('printf long-output')).closest('button')!;
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    resolve({
      process, output: 'late-collapsed-output', outputCursor: '9', retainedFromCursor: '0',
      gapBeforeOutput: false, truncatedByResponseBound: false, sourceCoverage: 'COMPLETE',
    });
    await Promise.resolve();
    fireEvent.click(toggle);
    await waitFor(() => expect(readLog).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/late-collapsed-output/)).toBeNull();
  });
});
