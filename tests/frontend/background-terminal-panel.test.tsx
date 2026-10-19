import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  BackgroundProcess,
  BackgroundProcessLog,
  CommandReceipt,
  UserControlCommandRef,
} from '../../frontend/lib/runtime-adapter';
import { DesktopWorkspaceProvider } from '../../frontend/components/desktop-workspace';
import { BackgroundTerminalPanel } from '../../frontend/components/background-terminal-panel';

afterEach(() => { vi.unstubAllGlobals(); cleanup(); });

const process: BackgroundProcess = {
  processId: 'process-a', command: 'printf long-output', cwd: '/tmp/probe',
  status: 'running', physicalState: 'RUNNING', ioMode: 'pipe', streamId: 'stream-a',
  outputCursor: '0', retainedFromCursor: '0', durationSeconds: 1, timedOut: false,
  originTurnId: 'turn-a',
};

it('shows a standalone process with output, without list expand or open controls', async () => {
  const readLog = vi.fn(async (): Promise<BackgroundProcessLog> => ({ process, output: 'detail output', outputCursor: '9', retainedFromCursor: '0', gapBeforeOutput: false, truncatedByResponseBound: false, sourceCoverage: 'COMPLETE' }));
  const view = render(<BackgroundTerminalPanel processId={process.processId} command={process.command} ownerKey="one" sessionId="one" hostSessionId="host" canControl={false}
    loadProcesses={async () => ({ processes: [process] })} readLog={readLog} terminateProcess={vi.fn()} queryControl={vi.fn()} onOpenProcess={vi.fn()} />);
  expect(await screen.findByText('detail output', {}, { timeout: 2500 })).toBeTruthy();
  expect(view.container.querySelector('.process-detail')).toBeTruthy();
  expect(screen.queryByRole('button', { name: '打开到新标签页' })).toBeNull();
  expect(screen.queryByRole('button', { name: '查看命令详情' })).toBeNull();
  expect(view.container.querySelector('.background-process__toggle')).toBeNull();
  expect(screen.getAllByText(process.command)).toHaveLength(1);
});

it('shows only the complete command on hover and opens the source card with a transition', async () => {
  const onOpen = vi.fn();
  const transition = vi.fn(({ update }: { update: () => void }) => { update(); return { ready: Promise.resolve(), finished: Promise.resolve() }; });
  const original = document.startViewTransition;
  Object.defineProperty(document, 'startViewTransition', { configurable: true, value: transition });
  try {
    render(<BackgroundTerminalPanel ownerKey="one" sessionId="one" hostSessionId="host" canControl={false} loadProcesses={async () => ({ processes: [process] })} readLog={vi.fn()} terminateProcess={vi.fn()} queryControl={vi.fn()} onOpenProcess={onOpen} />);
    const command = await screen.findByText(process.command);
    fireEvent.mouseEnter(command.closest('.background-process__identity')!);
    expect(screen.getByRole('tooltip').textContent).toBe(process.command);
    fireEvent.click(screen.getByRole('button', { name: '打开到新标签页' }));
    expect(onOpen).toHaveBeenCalledWith(process);
    expect(transition).toHaveBeenCalledWith(expect.objectContaining({ types: ['process-detail'] }));
    await waitFor(() => expect(command.closest<HTMLElement>('.background-process')!.style.viewTransitionName).toBe(''));
  } finally { Object.defineProperty(document, 'startViewTransition', { configurable: true, value: original }); }
});

describe('BackgroundTerminalPanel PR03 controls', () => {
  it('does not infer retention expiry from an unavailable control result or resend termination', async () => {
    const terminate = vi.fn(async (_reference: UserControlCommandRef): Promise<CommandReceipt> => {
      throw new Error('请求内容不正确。');
    });
    const query = vi.fn(async (_reference: UserControlCommandRef) => ({
      status: 'RESULT_UNAVAILABLE' as const,
    }));
    render(<BackgroundTerminalPanel
      ownerKey="session-a:1:host-a"
      sessionId="session-a"
      hostSessionId="host-a"
      controlAdmissionDeadlineMs={42_000}
      canControl
      loadProcesses={vi.fn(async () => ({ processes: [process] }))}
      readLog={vi.fn()}
      terminateProcess={terminate}
      queryControl={query}
    />);
    fireEvent.click(await screen.findByRole('button', { name: '终止 printf long-output' }));
    expect(await screen.findByText(/请求内容不正确/)).toBeTruthy();
    expect(await screen.findByText('当前无法取得这次操作的结果；不会自动重发。', {}, { timeout: 2500 })).toBeTruthy();
    expect(screen.queryByText(/原操作结果已不在/)).toBeNull();
    expect(terminate).toHaveBeenCalledTimes(1);
    expect(query).toHaveBeenCalledWith(terminate.mock.calls[0][0]);
  });

  it('does not present the unknown-exit placeholder as a real exit code', async () => {
    const unknownExit: BackgroundProcess = {
      ...process, processId: 'process-unknown', command: 'unknown exit',
      status: 'error', physicalState: 'PHYSICALLY_JOINED', exitCode: -1,
    };
    render(<BackgroundTerminalPanel
      ownerKey="session-a:1:host-a"
      sessionId="session-a"
      hostSessionId="host-a"
      controlAdmissionDeadlineMs={42_000}
      canControl={false}
      loadProcesses={vi.fn(async () => ({ processes: [unknownExit] }))}
      readLog={vi.fn()}
      terminateProcess={vi.fn()}
      queryControl={vi.fn()}
    />);
    expect(await screen.findByText('已结束 · 执行失败')).toBeTruthy();
    expect(screen.queryByText(/exit -1/)).toBeNull();
  });

  it('freezes one exact reference, shows retained output, and keeps partial failure visible', async () => {
    vi.stubGlobal('crypto', { getRandomValues: (bytes: Uint8Array) => bytes.fill(0) });
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
    expect(screen.getByRole('region', { name: '命令原文' }).textContent).toContain('$ printf long-output');
    expect(screen.getByRole('region', { name: '命令输出' }).textContent).toContain('line-1\nline-2');
    fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
    await waitFor(() => expect(terminate).toHaveBeenCalledWith(expect.objectContaining({
      commandId: 'command:aaaaaaaaaaaaaaaaaaaaaaaaaa', admissionDeadlineMs: 42000,
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

it('keeps the same pending control after its view closes and reopens', async () => {
  const terminate = vi.fn(async (_reference:UserControlCommandRef):Promise<CommandReceipt> => {throw new Error('transport unknown');});
  const query = vi.fn(async (_reference:UserControlCommandRef)=>({status:'RESULT_UNAVAILABLE' as const}));
  const common = {ownerKey:'session-a:1:host-a',sessionId:'session-a',hostSessionId:'host-a',controlAdmissionDeadlineMs:42000,canControl:true,
    loadProcesses:vi.fn(async()=>({processes:[process]})), readLog:vi.fn(),terminateProcess:terminate,queryControl:query};
  function Host({visible}:{visible:boolean}) { return <DesktopWorkspaceProvider sessionId="session-a" onShow={()=>{}}>{visible && <BackgroundTerminalPanel {...common}/>}</DesktopWorkspaceProvider>; }
  const view=render(<Host visible/>);
  fireEvent.click(await screen.findByRole('button',{name:'终止 printf long-output'}));
  await screen.findByText(/transport unknown/);
  const reference=terminate.mock.calls[0][0];
  view.rerender(<Host visible={false}/>);
  expect(terminate).toHaveBeenCalledOnce();
  view.rerender(<Host visible/>);
  await waitFor(()=>expect(query).toHaveBeenCalledWith(reference));
  expect(await screen.findByText('当前无法取得这次操作的结果；不会自动重发。')).toBeTruthy();
  expect(terminate).toHaveBeenCalledOnce();
});
