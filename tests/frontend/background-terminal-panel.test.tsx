import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  BackgroundProcess,
  BackgroundProcessLog,
  CommandReceipt,
  UserControlCommandRef,
  UserControlTarget,
} from '../../frontend/lib/runtime-adapter';
import { randomIdentity } from '../../frontend/lib/identity';
import { DesktopWorkspaceProvider } from '../../frontend/components/desktop-workspace';
import { BackgroundTerminalPanel } from '../../frontend/components/background-terminal-panel';

const prepareControl = async (target: UserControlTarget): Promise<UserControlCommandRef> => ({ ...target, commandId: randomIdentity('command'), admissionDeadlineMs: 42_000 });

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

const process: BackgroundProcess = {
  processId: 'process-a', command: 'printf long-output', cwd: '/tmp/probe',
  status: 'running', physicalState: 'RUNNING', ioMode: 'pipe', streamId: 'stream-a',
  outputCursor: '0', retainedFromCursor: '0', durationSeconds: 1, timedOut: false,
  originTurnId: 'turn-a',
};

it('shows a standalone process with output, without list expand or open controls', async () => {
  const readLog = vi.fn(async (): Promise<BackgroundProcessLog> => ({ process, output: 'detail output', outputCursor: '9', retainedFromCursor: '0', gapBeforeOutput: false, truncatedByResponseBound: false, sourceCoverage: 'COMPLETE' }));
  const view = render(<BackgroundTerminalPanel prepareControl={prepareControl} processId={process.processId} ownerKey="one" sessionId="one" hostSessionId="host" canControl={false}
    loadProcesses={async () => ({ processes: [process] })} readLog={readLog} terminateProcess={vi.fn()} queryControl={vi.fn()} onOpenProcess={vi.fn()} />);
  expect(await screen.findByText('detail output', {}, { timeout: 2500 })).toBeTruthy();
  expect(view.container.querySelector('.process-detail')).toBeTruthy();
  expect(screen.queryByRole('button', { name: '打开到新标签页' })).toBeNull();
  expect(screen.queryByRole('button', { name: '查看命令详情' })).toBeNull();
  expect(screen.queryByRole('button', { expanded: true })).toBeNull();
  expect(screen.getAllByText(process.command)).toHaveLength(1);
});

it('replaces the transition preview with the process owner result when the record is gone', async () => {
  let resolve!: (page: { processes: BackgroundProcess[] }) => void;
  const pending = new Promise<{ processes: BackgroundProcess[] }>(done => { resolve = done; });
  const view = render(<BackgroundTerminalPanel prepareControl={prepareControl} processId={process.processId} initialProcess={process} ownerKey="one" sessionId="one" canControl={false}
    loadProcesses={async () => pending} readLog={vi.fn()} terminateProcess={vi.fn()} queryControl={vi.fn()} onOpenProcess={vi.fn()} />);
  expect(view.container.querySelector('.process-detail')).toBeTruthy();
  expect(screen.getByText(process.command)).toBeTruthy();
  await act(async () => { resolve({ processes: [] }); });
  expect(await screen.findByText('原进程记录已不在当前保留范围。')).toBeTruthy();
  expect(view.container.querySelector('.process-detail')).toBeNull();
});

it('shows only the complete command on hover and opens the source card with a transition', async () => {
  const onOpen = vi.fn();
  const transition = vi.fn(({ update }: { update: () => void }) => { update(); return { ready: Promise.resolve(), finished: Promise.resolve() }; });
  const original = document.startViewTransition;
  Object.defineProperty(document, 'startViewTransition', { configurable: true, value: transition });
  try {
    render(<BackgroundTerminalPanel prepareControl={prepareControl} ownerKey="one" sessionId="one" hostSessionId="host" canControl={false} loadProcesses={async () => ({ processes: [process] })} readLog={vi.fn()} terminateProcess={vi.fn()} queryControl={vi.fn()} onOpenProcess={onOpen} />);
    const command = await screen.findByText(process.command);
    fireEvent.mouseEnter(command.closest('.background-process__identity')!);
    expect(screen.getByRole('tooltip').textContent).toBe(process.command);
    fireEvent.click(screen.getByRole('button', { name: /^printf long-output/ }));
    expect(onOpen).toHaveBeenCalledWith(process);
    expect(transition).toHaveBeenCalledWith(expect.objectContaining({ types: ['process-detail'] }));
    await waitFor(() => expect(command.closest<HTMLElement>('.background-process')!.style.viewTransitionName).toBe(''));
  } finally { Object.defineProperty(document, 'startViewTransition', { configurable: true, value: original }); }
});

it('opens a list entry directly and never reads output, including while its metadata is expanded', async () => {
  vi.useFakeTimers();
  const readLog = vi.fn();
  const loadProcesses = vi.fn(async () => ({ processes: [process] }));
  const onOpenProcess = vi.fn();
  const view = render(<BackgroundTerminalPanel prepareControl={prepareControl} ownerKey="one" sessionId="one" canControl={false}
    loadProcesses={loadProcesses} readLog={readLog} terminateProcess={vi.fn()} queryControl={vi.fn()} onOpenProcess={onOpenProcess} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(loadProcesses).toHaveBeenCalledTimes(3);
  expect(readLog).not.toHaveBeenCalled();
  const entry = screen.getByRole('button', { name: /^printf long-output/ });
  expect(entry.hasAttribute('aria-expanded')).toBe(false);
  expect(screen.queryByRole('button', { name: '打开到新标签页' })).toBeNull();
  expect(view.container.querySelector('.background-process__output')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '查看命令详情' }));
  expect(screen.getByText(process.cwd)).toBeTruthy();
  expect(screen.getByText(process.physicalState)).toBeTruthy();
  expect(onOpenProcess).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(readLog).not.toHaveBeenCalled();
  fireEvent.click(entry);
  expect(onOpenProcess).toHaveBeenCalledExactlyOnceWith(process);
  expect(readLog).not.toHaveBeenCalled();
});

describe('BackgroundTerminalPanel PR03 controls', () => {
  it('does not infer retention expiry from an unavailable control result or resend termination', async () => {
    const terminate = vi.fn(async (_reference: UserControlCommandRef): Promise<CommandReceipt> => {
      throw new Error('请求内容不正确。');
    });
    const query = vi.fn(async (_reference: UserControlCommandRef) => ({
      status: 'RESULT_UNAVAILABLE' as const,
    }));
    render(<BackgroundTerminalPanel prepareControl={prepareControl}
      ownerKey="session-a:1:host-a"
      sessionId="session-a"
      hostSessionId="host-a"
      canControl
      loadProcesses={vi.fn(async () => ({ processes: [process] }))}
      readLog={vi.fn()}
      terminateProcess={terminate}
      queryControl={query}
      onOpenProcess={vi.fn()}
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
    render(<BackgroundTerminalPanel prepareControl={prepareControl}
      ownerKey="session-a:1:host-a"
      sessionId="session-a"
      hostSessionId="host-a"
      canControl={false}
      loadProcesses={vi.fn(async () => ({ processes: [unknownExit] }))}
      readLog={vi.fn()}
      terminateProcess={vi.fn()}
      queryControl={vi.fn()}
      onOpenProcess={vi.fn()}
    />);
    expect(await screen.findByText('已结束 · 执行失败')).toBeTruthy();
    expect(screen.queryByText(/exit -1/)).toBeNull();
  });

  it('freezes one exact reference, shows retained output, and keeps partial failure visible', async () => {
    vi.stubGlobal('crypto', { getRandomValues: (bytes: Uint8Array) => bytes.fill(0) });
    const terminate = vi.fn(async (reference: UserControlCommandRef): Promise<CommandReceipt> => ({
      commandId: reference.commandId,
      status: 'failed',
      publicMessage: '进程已结束，但资源清理失败。',
      userControl: {
        accepted: true, execution: 'FINISHED',
      },
    }));
    const onOpenProcess = vi.fn();
    render(<BackgroundTerminalPanel prepareControl={prepareControl}
      processId={process.processId}
      ownerKey="session-a:1:host-a"
      sessionId="session-a"
      hostSessionId="host-a"
      canControl
      loadProcesses={vi.fn(async () => ({ processes: [process] }))}
      readLog={vi.fn(async () => ({
        process, output: 'line-1\nline-2', outputCursor: '2', retainedFromCursor: '0',
        gapBeforeOutput: false, truncatedByResponseBound: false, sourceCoverage: 'COMPLETE',
      }))}
      terminateProcess={terminate}
      queryControl={vi.fn(async () => ({ status: 'RESULT_UNAVAILABLE' as const }))}
      onOpenProcess={onOpenProcess}
    />);
    expect(await screen.findByText(/line-1/, {}, { timeout: 2500 })).toBeTruthy();
    expect(screen.getByRole('region', { name: '命令输出' }).textContent).toContain('line-1\nline-2');
    fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
    await waitFor(() => expect(terminate).toHaveBeenCalledWith(expect.objectContaining({
      commandId: 'command:aaaaaaaaaaaaaaaaaaaaaaaaaa', admissionDeadlineMs: 42000,
      sessionId: 'session-a', hostSessionId: 'host-a', targetId: 'process-a',
    })));
    expect(screen.getByText('进程已结束，但资源清理失败。')).toBeTruthy();
    expect(screen.queryByText(/已编入原轮次模型请求/)).toBeNull();
    expect(onOpenProcess).not.toHaveBeenCalled();
  });

  it('invalidates a late log page when the owner changes', async () => {
    let resolve!: (value: BackgroundProcessLog) => void;
    const pending = new Promise<BackgroundProcessLog>((accept) => { resolve = accept; });
    const common = {
      canControl: false,
      loadProcesses: vi.fn(async () => ({ processes: [process] })),
      terminateProcess: vi.fn(),
      queryControl: vi.fn(),
      onOpenProcess: vi.fn(),
    };
    const readLog = vi.fn(async () => pending);
    const view = render(<BackgroundTerminalPanel prepareControl={prepareControl}
      ownerKey="session-a:1:host-a" sessionId="session-a" hostSessionId="host-a"
      processId={process.processId}
      {...common}
      readLog={readLog}
    />);
    await waitFor(() => expect(readLog).toHaveBeenCalledOnce(), { timeout: 2500 });
    view.rerender(<BackgroundTerminalPanel prepareControl={prepareControl}
      ownerKey="session-a:2:host-b" sessionId="session-a" hostSessionId="host-b"
      processId={process.processId}
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
    expect(await screen.findByText('new-owner-output', {}, { timeout: 2500 })).toBeTruthy();
  });

  it('invalidates a late log page when returning from the detail page to the list', async () => {
    let resolve!: (value: BackgroundProcessLog) => void;
    const pending = new Promise<BackgroundProcessLog>((accept) => { resolve = accept; });
    const readLog = vi.fn(async () => pending);
    const common = {
      ownerKey: 'session-a:1:host-a', sessionId: 'session-a', hostSessionId: 'host-a',
      canControl: false, loadProcesses: vi.fn(async () => ({ processes: [process] })),
      readLog, terminateProcess: vi.fn(), queryControl: vi.fn(), onOpenProcess: vi.fn(),
    };
    const view = render(<BackgroundTerminalPanel prepareControl={prepareControl}
      {...common}
      processId={process.processId}
    />);
    await waitFor(() => expect(readLog).toHaveBeenCalledOnce(), { timeout: 2500 });
    view.rerender(<BackgroundTerminalPanel prepareControl={prepareControl} {...common} />);
    expect(await screen.findByRole('button', { name: /^printf long-output/ })).toBeTruthy();
    await act(async () => {
      resolve({
        process, output: 'late-detail-output', outputCursor: '9', retainedFromCursor: '0',
        gapBeforeOutput: false, truncatedByResponseBound: false, sourceCoverage: 'COMPLETE',
      });
    });
    expect(screen.queryByText(/late-detail-output/)).toBeNull();
    expect(view.container.querySelector('.background-process__output')).toBeNull();
    expect(readLog).toHaveBeenCalledOnce();
  });
});

it('keeps the same pending control after its view closes and reopens', async () => {
  const terminate = vi.fn(async (_reference:UserControlCommandRef):Promise<CommandReceipt> => {throw new Error('transport unknown');});
  const query = vi.fn(async (_reference:UserControlCommandRef)=>({status:'RESULT_UNAVAILABLE' as const}));
  const onOpenProcess = vi.fn();
  const common = {ownerKey:'session-a:1:host-a',sessionId:'session-a',hostSessionId:'host-a',canControl:true,
    loadProcesses:vi.fn(async()=>({processes:[process]})), readLog:vi.fn(),terminateProcess:terminate,queryControl:query,onOpenProcess};
  function Host({visible}:{visible:boolean}) { return <DesktopWorkspaceProvider sessionId="session-a" onShow={()=>{}}>{visible && <BackgroundTerminalPanel prepareControl={prepareControl} {...common}/>}</DesktopWorkspaceProvider>; }
  const view=render(<Host visible/>);
  fireEvent.click(await screen.findByRole('button',{name:'终止 printf long-output'}));
  await screen.findByText(/transport unknown/);
  const reference=terminate.mock.calls[0][0];
  view.rerender(<Host visible={false}/>);
  expect(terminate).toHaveBeenCalledOnce();
  expect(onOpenProcess).not.toHaveBeenCalled();
  view.rerender(<Host visible/>);
  await waitFor(()=>expect(query).toHaveBeenCalledWith(reference));
  expect(await screen.findByText('当前无法取得这次操作的结果；不会自动重发。')).toBeTruthy();
  expect(terminate).toHaveBeenCalledOnce();
});

it('prepares a fresh exact reference before every explicitly retried rejected control', async () => {
  const prepare = vi.fn(async (target: Omit<UserControlCommandRef, 'commandId' | 'admissionDeadlineMs'>) => ({
    ...target, commandId: randomIdentity('command'), admissionDeadlineMs: 99_000,
  }));
  const terminate = vi.fn(async (reference: UserControlCommandRef): Promise<CommandReceipt> => ({
    commandId: reference.commandId, status: 'rejected', publicCode: 'CONTROL_REQUEST_EXPIRED', publicMessage: '请求已过期',
  }));
  render(<BackgroundTerminalPanel ownerKey="session-a:host-a" sessionId="session-a" hostSessionId="host-a" canControl
    prepareControl={prepare} loadProcesses={async () => ({ processes: [process] })} readLog={vi.fn()}
    terminateProcess={terminate} queryControl={vi.fn()} onOpenProcess={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '终止 printf long-output' }));
  await waitFor(() => expect(terminate).toHaveBeenCalledTimes(1));
  expect(prepare).toHaveBeenCalledWith({ operation: 'TERMINATE_BACKGROUND_PROCESS', targetKind: 'BACKGROUND_PROCESS',
    sessionId: 'session-a', hostSessionId: 'host-a', targetId: process.processId });
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await waitFor(() => expect(terminate).toHaveBeenCalledTimes(2));
  expect(terminate.mock.calls[0][0].commandId).not.toBe(terminate.mock.calls[1][0].commandId);
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

const pendingReceipt = (reference: UserControlCommandRef): CommandReceipt => ({
  commandId: reference.commandId, status: 'pending', userControl: { accepted: true, execution: 'RUNNING' },
});
const finishedReceipt = (reference: UserControlCommandRef, publicMessage = '已确认终止原命令'): CommandReceipt => ({
  commandId: reference.commandId, status: 'succeeded', publicMessage,
  userControl: { accepted: true, execution: 'FINISHED' },
});
const panelProps = () => ({ ownerKey: 'session-a:host-a', sessionId: 'session-a', hostSessionId: 'host-a', canControl: true,
  prepareControl, loadProcesses: vi.fn(async () => ({ processes: [process] })), readLog: vi.fn(),
  terminateProcess: vi.fn(async (reference: UserControlCommandRef) => pendingReceipt(reference)),
  queryControl: vi.fn(async (_reference: UserControlCommandRef): Promise<import('../../frontend/lib/runtime-adapter').UserControlQueryResult> => ({ status: 'RESULT_UNAVAILABLE' })),
  onOpenProcess: vi.fn(),
});

it('joins double clicks during preparation and never sends a prepared target after its view leaves', async () => {
  const preparation = deferred<UserControlCommandRef>();
  const props = { ...panelProps(), prepareControl: vi.fn(() => preparation.promise) };
  const view = render(<BackgroundTerminalPanel {...props} />);
  const button = await screen.findByRole('button', { name: '终止 printf long-output' });
  fireEvent.click(button); fireEvent.click(button);
  expect(props.prepareControl).toHaveBeenCalledTimes(1);
  expect(props.terminateProcess).not.toHaveBeenCalled();
  view.rerender(<BackgroundTerminalPanel {...props} ownerKey="session-b:host-b" sessionId="session-b" hostSessionId="host-b" />);
  await act(async () => preparation.resolve(await prepareControl({ operation: 'TERMINATE_BACKGROUND_PROCESS',
    targetKind: 'BACKGROUND_PROCESS', sessionId: 'session-a', hostSessionId: 'host-a', targetId: process.processId })));
  expect(props.terminateProcess).not.toHaveBeenCalled();
});

it('allows a fresh preparation after a failure known to precede sending', async () => {
  const props = { ...panelProps(), prepareControl: vi.fn(prepareControl).mockRejectedValueOnce(new Error('快照读取失败')) };
  render(<BackgroundTerminalPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: '终止 printf long-output' }));
  expect(await screen.findByText('快照读取失败')).toBeTruthy();
  expect(props.terminateProcess).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await waitFor(() => expect(props.terminateProcess).toHaveBeenCalledTimes(1));
  expect(props.prepareControl).toHaveBeenCalledTimes(2);
});

it('keeps one shared query in flight across views and saves its terminal reply while hidden', async () => {
  vi.useFakeTimers();
  const result = deferred<import('../../frontend/lib/runtime-adapter').UserControlQueryResult>();
  const props = { ...panelProps(), queryControl: vi.fn(() => result.promise) };
  function Host({ visible, twice = false }: { visible: boolean; twice?: boolean }) {
    return <DesktopWorkspaceProvider sessionId="session-a" onShow={() => {}}>
      {visible && <BackgroundTerminalPanel {...props} />}
      {visible && twice && <BackgroundTerminalPanel {...props} />}
    </DesktopWorkspaceProvider>;
  }
  const view = render(<Host visible />);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await act(async () => {});
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(props.queryControl).toHaveBeenCalledTimes(1);
  view.rerender(<Host visible twice />);
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(props.queryControl).toHaveBeenCalledTimes(1);
  view.rerender(<Host visible={false} />);
  await act(async () => result.resolve({ status: 'FOUND', receipt: finishedReceipt(props.terminateProcess.mock.calls[0][0]) }));
  view.rerender(<Host visible />);
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(screen.getByText('已确认终止原命令')).toBeTruthy();
  expect(props.queryControl).toHaveBeenCalledTimes(1);
  expect(props.terminateProcess).toHaveBeenCalledTimes(1);
});

it('continues UNKNOWN queries after the process leaves the listing and later accepts FOUND', async () => {
  vi.useFakeTimers();
  const props = panelProps();
  props.queryControl.mockImplementationOnce(async () => ({ status: 'RESULT_UNAVAILABLE' }))
    .mockImplementation(async reference => ({ status: 'FOUND', receipt: finishedReceipt(reference) }));
  render(<BackgroundTerminalPanel {...props} />);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await act(async () => {});
  props.loadProcesses.mockResolvedValue({ processes: [] });
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(screen.getByText('当前无法取得这次操作的结果；不会自动重发。')).toBeTruthy();
  expect(screen.getByText('进程记录已不在保留范围')).toBeTruthy();
  expect(screen.queryByRole('button', { name: '再次请求停止' })).toBeNull();
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(screen.getByText('已确认终止原命令')).toBeTruthy();
  expect(props.queryControl).toHaveBeenCalledTimes(2);
  expect(props.terminateProcess).toHaveBeenCalledTimes(1);
});

it('keeps UNKNOWN as query-only unless the user explicitly requests another stop', async () => {
  vi.useFakeTimers();
  const oldResult = deferred<import('../../frontend/lib/runtime-adapter').UserControlQueryResult>();
  const props = panelProps();
  props.terminateProcess.mockRejectedValueOnce(new Error('回包丢失'));
  props.queryControl.mockImplementation(() => oldResult.promise);
  render(<BackgroundTerminalPanel {...props} />);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await act(async () => {});
  expect(props.terminateProcess).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: '再次请求停止' }));
  await act(async () => {});
  expect(props.terminateProcess).toHaveBeenCalledTimes(2);
  expect(screen.getByText('上次终止操作结果待确认')).toBeTruthy();
  await act(async () => oldResult.resolve({ status: 'FOUND', receipt: finishedReceipt(props.terminateProcess.mock.calls[0][0], '旧操作已确认') }));
  expect(screen.queryByText('旧操作已确认')).toBeNull();
  expect(screen.getByText('正在终止')).toBeTruthy();
  expect(props.terminateProcess.mock.calls[0][0].commandId).not.toBe(props.terminateProcess.mock.calls[1][0].commandId);
});

it('does not poll after the physical control result has finished', async () => {
  vi.useFakeTimers();
  const props = panelProps();
  props.terminateProcess.mockImplementationOnce(async reference => finishedReceipt(reference, '物理结果已确认'));
  render(<BackgroundTerminalPanel {...props} />);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await act(async () => {});
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(screen.getByText('物理结果已确认')).toBeTruthy();
  expect(props.queryControl).not.toHaveBeenCalled();
});

it('does not resurrect an operation whose result observation was explicitly ended', async () => {
  vi.useFakeTimers();
  const result = deferred<import('../../frontend/lib/runtime-adapter').UserControlQueryResult>();
  const props = { ...panelProps(), queryControl: vi.fn(() => result.promise) };
  render(<BackgroundTerminalPanel {...props} />);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await act(async () => {});
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  fireEvent.click(screen.getByRole('button', { name: '结束结果观察' }));
  await act(async () => result.resolve({ status: 'FOUND', receipt: finishedReceipt(props.terminateProcess.mock.calls[0][0]) }));
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(screen.queryByText('已确认终止原命令')).toBeNull();
  expect(props.queryControl).toHaveBeenCalledTimes(1);
});

it('retains a successful send response across view closure without querying or resending it', async () => {
  vi.useFakeTimers();
  const sent = deferred<CommandReceipt>();
  const props = { ...panelProps(), terminateProcess: vi.fn((_reference: UserControlCommandRef) => sent.promise) };
  function Host({ visible }: { visible: boolean }) {
    return <DesktopWorkspaceProvider sessionId="session-a" onShow={() => {}}>{visible && <BackgroundTerminalPanel {...props} />}</DesktopWorkspaceProvider>;
  }
  const view = render(<Host visible />);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await act(async () => {});
  view.rerender(<Host visible={false} />);
  await act(async () => sent.resolve(finishedReceipt(props.terminateProcess.mock.calls[0][0])));
  props.loadProcesses.mockRejectedValue(new Error('listing unavailable'));
  view.rerender(<Host visible />);
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(screen.getByText('已确认终止原命令')).toBeTruthy();
  expect(props.queryControl).not.toHaveBeenCalled();
  expect(props.terminateProcess).toHaveBeenCalledTimes(1);
});

it('does not restore released workspace records after a late query response', async () => {
  vi.useFakeTimers();
  const result = deferred<import('../../frontend/lib/runtime-adapter').UserControlQueryResult>();
  const props = { ...panelProps(), queryControl: vi.fn(() => result.promise) };
  const controlRef: { current: import('../../frontend/components/desktop-workspace').WorkspaceControl | null } = { current: null };
  function Host({ visible }: { visible: boolean }) {
    return <DesktopWorkspaceProvider sessionId="session-a" onShow={() => {}} controlRef={controlRef}>
      {visible && <BackgroundTerminalPanel {...props} />}
    </DesktopWorkspaceProvider>;
  }
  const view = render(<Host visible />);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await act(async () => {});
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  view.rerender(<Host visible={false} />);
  await act(async () => controlRef.current!.forgetSession('session-a'));
  await act(async () => result.resolve({ status: 'FOUND', receipt: finishedReceipt(props.terminateProcess.mock.calls[0][0]) }));
  view.rerender(<Host visible />);
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(screen.queryByText('已确认终止原命令')).toBeNull();
  expect(props.queryControl).toHaveBeenCalledTimes(1);
});

it('shares the one-second query interval across fast UNKNOWN replies, remounts and manual refresh', async () => {
  vi.useFakeTimers();
  const props = panelProps();
  function Host({ visible, twice = false }: { visible: boolean; twice?: boolean }) {
    return <DesktopWorkspaceProvider sessionId="session-a" onShow={() => {}}>
      {visible && <BackgroundTerminalPanel {...props} />}
      {visible && twice && <BackgroundTerminalPanel {...props} />}
    </DesktopWorkspaceProvider>;
  }
  const view = render(<Host visible />);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await act(async () => {});
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(props.queryControl).toHaveBeenCalledTimes(1);
  const reference = props.terminateProcess.mock.calls[0][0];
  expect(props.queryControl).toHaveBeenLastCalledWith(reference);
  await act(async () => vi.advanceTimersByTimeAsync(250));
  view.rerender(<Host visible={false} />);
  view.rerender(<Host visible twice />);
  await act(async () => {});
  for (const button of screen.getAllByRole('button', { name: '终止 printf long-output' })) fireEvent.click(button);
  await act(async () => {});
  expect(props.queryControl).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTimeAsync(749));
  fireEvent.click(screen.getAllByRole('button', { name: '终止 printf long-output' })[0]);
  await act(async () => {});
  expect(props.queryControl).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTimeAsync(1));
  fireEvent.click(screen.getAllByRole('button', { name: '终止 printf long-output' })[0]);
  await act(async () => {});
  expect(props.queryControl).toHaveBeenCalledTimes(2);
  expect(props.queryControl).toHaveBeenLastCalledWith(reference);
  expect(props.terminateProcess).toHaveBeenCalledTimes(1);
});

it('ends queries for an unavailable owner without claiming termination or blocking another process', async () => {
  vi.useFakeTimers();
  const props = panelProps();
  props.queryControl.mockResolvedValue({ status: 'OWNER_UNAVAILABLE' });
  render(<BackgroundTerminalPanel {...props} />);
  await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: '终止 printf long-output' }));
  await act(async () => {});
  const original = props.terminateProcess.mock.calls[0][0];
  await act(async () => vi.advanceTimersByTimeAsync(4000));
  expect(props.queryControl).toHaveBeenCalledExactlyOnceWith(original);
  expect(screen.getByText('原 Host 已失效；不会把这次操作改投新的 Host。')).toBeTruthy();
  expect(screen.getByText('运行中')).toBeTruthy();
  expect(screen.queryByText('终止操作已确认。')).toBeNull();
  expect(screen.queryByRole('button', { name: '终止 printf long-output' })).toBeNull();
  expect(screen.queryByRole('button', { name: '再次请求停止' })).toBeNull();
  const another = { ...process, processId: randomIdentity('process'), command: 'another command' };
  props.loadProcesses.mockResolvedValue({ processes: [process, another] });
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  fireEvent.click(screen.getByRole('button', { name: '终止 another command' }));
  await act(async () => {});
  expect(props.terminateProcess).toHaveBeenCalledTimes(2);
  expect(props.terminateProcess.mock.calls[1][0].targetId).toBe(another.processId);
  expect(props.queryControl).toHaveBeenCalledExactlyOnceWith(original);
});
