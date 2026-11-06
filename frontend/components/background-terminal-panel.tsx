import { WorkspaceLoading } from './workspace-loading';
import {
  AlertTriangle,
  CircleStop,
  Info,
  LoaderCircle,
  RefreshCw,
  TerminalSquare,
} from 'lucide-react';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal, flushSync } from 'react-dom';
import { usePromptHover } from '../lib/prompt-hover';
import { useDesktopWorkspace, type BackgroundControlAttempt, type BackgroundControlMemory, type BackgroundControlRecord } from './desktop-workspace';
import { AnimatedDisclosure } from './animated-disclosure';
import type {
  BackgroundProcess,
  BackgroundProcessLog,
  BackgroundProcessPage,
  CommandReceipt,
  UserControlCommandRef,
  UserControlTarget,
  UserControlQueryResult,
} from '../lib/runtime-adapter';

interface ProcessView {
  process: BackgroundProcess;
  details: boolean;
  output: string;
  cursor?: string;
  reading: boolean;
  error?: string;
  available?: boolean;
}

const terminal = (status: string) => status !== 'running';

function ProcessIdentity({ command, status, onOpen }: { command: string; status: string; onOpen?: (source: HTMLButtonElement) => void }) {
  const { trigger, details, position, show, hide, keep } = usePromptHover<HTMLDivElement>(440);
  const [placement, setPlacement] = useState(position);
  if (position && position !== placement) setPlacement(position);
  const id = useId();
  const copy = <><strong>{command}</strong><small>{status}</small></>;
  return <div ref={trigger} className="background-process__identity" onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
    {onOpen ? <button type="button" className="background-process__link" aria-describedby={position ? id : undefined} onClick={event => onOpen(event.currentTarget)}><span className="background-process__icon" aria-hidden="true"><TerminalSquare size={15} /></span><span>{copy}</span></button>
      : <div className="process-detail__identity" tabIndex={0} aria-describedby={position ? id : undefined}><span className="background-process__icon" aria-hidden="true"><TerminalSquare size={15} /></span><span>{copy}</span></div>}
    {placement && createPortal(<span ref={details} id={id} role="tooltip" className="process-command-tooltip" data-open={Boolean(position)} aria-hidden={!position} inert={!position}
      style={{ left: placement.left, top: placement.top, translate: placement.above ? '0 calc(-100% - 6px)' : '0 6px' }} onMouseEnter={keep} onMouseLeave={hide}>{command}</span>, document.body)}
  </div>;
}

const processStatusText = (process: BackgroundProcess): string => {
  if (process.status === 'running') return '运行中';
  if (process.status === 'timeout' || process.timedOut) return '已结束 · 命令超时';
  if (process.status === 'killed') return '已结束 · 已停止';
  if (process.exitCode === -1) {
    return process.status === 'error' ? '已结束 · 执行失败' : '已结束 · 退出状态未知';
  }
  return `已结束${process.exitCode === undefined ? '' : ` · exit ${process.exitCode}`}`;
};

const controlNeedsPolling = (receipt?: CommandReceipt): boolean => Boolean(
  receipt
  && (
    receipt.status === 'pending'
    || receipt.userControl?.execution === 'RUNNING'
    || receipt.userControl?.feedback?.canonicalStatus === 'PENDING'
    || receipt.userControl?.feedback?.inclusionStatus === 'PENDING'
  )
  && receipt.userControl?.feedback?.ownerAvailability !== 'UNAVAILABLE',
);

const attemptNeedsPolling = (attempt: BackgroundControlAttempt): boolean => !attempt.ownerUnavailable
  && (Boolean(attempt.unknown) || !attempt.control || controlNeedsPolling(attempt.control));

function ProcessFeedback({ receipt }: { receipt: CommandReceipt }) {
  const feedback = receipt.userControl!.feedback!;
  return <p className="background-process__feedback">反馈：{feedback.canonicalStatus === 'ACCEPTED' ? '已记录' : feedback.canonicalStatus === 'NOT_REQUIRED' ? '本次无需记录' : '记录状态待确认'} · {feedback.inclusionStatus === 'INCLUDED' ? '已编入原轮次模型请求' : feedback.inclusionStatus === 'TARGET_ENDED_BEFORE_INCLUSION' ? '原轮次在纳入前结束' : feedback.inclusionStatus === 'NOT_APPLICABLE' ? '不适用模型请求' : '请求纳入状态待确认'}{feedback.transportInvocationAttempted ? ` · 本地请求${feedback.transportInvocationSucceeded === false ? '发起失败' : '已发起'}` : ''}</p>;
}

const controlQueryMessage = (status: UserControlQueryResult['status']): string => (
  status === 'OWNER_UNAVAILABLE'
    ? '原 Host 已失效；不会把这次操作改投新的 Host。'
    : '当前无法取得这次操作的结果；不会自动重发。'
);

export function BackgroundTerminalPanel({
  processId: selectedProcessId,
  initialProcess,
  onOpenProcess,
  ownerKey,
  sessionId,
  hostSessionId,
  prepareControl,
  canControl,
  loadProcesses,
  readLog,
  terminateProcess,
  queryControl,
}: {
  processId?: string;
  initialProcess?: BackgroundProcess;
  onOpenProcess: (process: BackgroundProcess) => void;
  ownerKey: string;
  sessionId: string;
  hostSessionId?: string;
  prepareControl: (target: UserControlTarget) => Promise<UserControlCommandRef>;
  canControl: boolean;
  loadProcesses: (cursor?: string) => Promise<BackgroundProcessPage>;
  readLog: (processId: string, cursor?: string) => Promise<BackgroundProcessLog>;
  terminateProcess: (reference: UserControlCommandRef) => Promise<CommandReceipt>;
  queryControl: (reference: UserControlCommandRef) => Promise<UserControlQueryResult>;
}) {
  const seed = initialProcess?.processId === selectedProcessId ? initialProcess : undefined;
  const [items, setItems] = useState<Map<string, ProcessView>>(() => new Map(seed ? [[seed.processId, { process: seed, details: true, output: '', reading: false }]] : []));
  const itemsRef = useRef(items);
  const [loading, setLoading] = useState(!seed);
  const [error, setError] = useState<string>();
  const revision = useRef(0);
  const readRevisions = useRef(new Map<string, number>());
  const operations = useRef({ loadProcesses, readLog, prepareControl, terminateProcess, queryControl });
  const workspace = useDesktopWorkspace();
  const controlOwner = JSON.stringify([sessionId, hostSessionId]);
  const localControls = useRef<{ owner: string; records: BackgroundControlMemory }>({ owner: controlOwner, records: new Map() });
  if (localControls.current.owner !== controlOwner) localControls.current = { owner: controlOwner, records: new Map() };
  if (workspace && !workspace.backgroundControls.has(controlOwner)) workspace.backgroundControls.set(controlOwner, new Map());
  const controls = workspace?.backgroundControls.get(controlOwner) ?? localControls.current.records;
  const memoryRetained = useCallback(() => workspace ? workspace.backgroundControls.get(controlOwner) === controls : localControls.current.records === controls,
    [workspace, controlOwner, controls]);
  const controlReady = Boolean(canControl && hostSessionId);
  const currentOwner = useRef({ ownerKey, controls, canControl });
  currentOwner.current = { ownerKey, controls, canControl };
  const [, renderControls] = useState(0);
  const redraw = useCallback(() => renderControls(value => value + 1), []);
  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  useEffect(() => {
    operations.current = { loadProcesses, readLog, prepareControl, terminateProcess, queryControl };
  }, [loadProcesses, queryControl, readLog, prepareControl, terminateProcess]);

  const update = useCallback((processId: string, change: (value: ProcessView) => ProcessView) => {
    setItems((current) => {
      const value = current.get(processId);
      if (!value) return current;
      const next = new Map(current);
      next.set(processId, change(value));
      return next;
    });
  }, []);

  const refresh = useCallback(async () => {
    const requestRevision = revision.current;
    // Query records, not the process listing: a retained operation can outlive
    // its process row. The shared attempt owns concurrency across view remounts.
    if (canControl && memoryRetained()) for (const record of controls.values()) {
      for (const attempt of record.attempts.values()) {
        if (!attemptNeedsPolling(attempt) || attempt.sending || attempt.query
          || (attempt.lastQueryStarted !== undefined && performance.now() - attempt.lastQueryStarted < 1000)) continue;
        attempt.lastQueryStarted = performance.now();
        const queryControl = operations.current.queryControl;
        const retained = () => memoryRetained() && controls.get(record.process.processId) === record
          && record.attempts.get(attempt.reference.commandId) === attempt;
        const query = Promise.resolve().then(async () => {
          try {
            const result = await queryControl(attempt.reference);
            if (!retained()) return;
            if (result.status === 'FOUND' && result.receipt) {
              if (result.receipt.commandId !== attempt.reference.commandId) throw new Error('控制查询返回了其他操作的结果。');
              attempt.control = result.receipt;
              attempt.unknown = false;
              attempt.error = undefined;
            } else {
              attempt.ownerUnavailable = result.status === 'OWNER_UNAVAILABLE';
              attempt.unknown = true;
              attempt.error = controlQueryMessage(result.status);
            }
          } catch (caught) {
            if (!retained()) return;
            attempt.unknown = true;
            attempt.error = caught instanceof Error ? caught.message : '控制状态暂时无法查询。';
          } finally {
            // A released attempt is never reinserted by a late response.
            if (retained()) {
              attempt.query = undefined;
              if (record.currentCommandId !== attempt.reference.commandId && !attemptNeedsPolling(attempt)) {
                record.attempts.delete(attempt.reference.commandId);
              }
              if (requestRevision === revision.current) redraw();
            }
          }
        });
        attempt.query = query;
      }
    }
    try {
      const processes: BackgroundProcess[] = [];
      const seen = new Set<string>();
      let cursor: string | undefined;
      do {
        const page = await operations.current.loadProcesses(cursor);
        processes.push(...page.processes.filter(process => !selectedProcessId || process.processId === selectedProcessId));
        cursor = page.nextCursor;
        if (cursor) {
          if (seen.has(cursor)) throw new Error('后台命令分页游标发生循环。');
          seen.add(cursor);
        }
      } while (cursor);
      if (requestRevision !== revision.current) return;
      setItems((current) => {
        const next = new Map<string, ProcessView>();
        for (const process of processes) {
          const existing = current.get(process.processId);
          const record = controls.get(process.processId);
          if (record) record.process = process;
          next.set(process.processId, existing ? { ...existing, process, available: true } : {
            process, details: Boolean(selectedProcessId), output: '', reading: false, available: true,
          });
        }
        for (const record of controls.values()) {
          const id = record.process.processId;
          if ((!selectedProcessId || selectedProcessId === id) && !next.has(id)) {
            const existing = current.get(id);
            next.set(id, { process: record.process, details: Boolean(selectedProcessId), output: '', reading: false,
              ...existing, available: false });
          }
        }
        return next;
      });
      setError(undefined);
    } catch (caught) {
      if (requestRevision === revision.current) setError(
        caught instanceof Error ? caught.message : '后台命令暂时无法读取。',
      );
    } finally {
      if (requestRevision === revision.current) { setLoading(false); redraw(); }
    }
  }, [canControl, selectedProcessId, controls, memoryRetained, redraw]);

  useEffect(() => {
    revision.current += 1;
    readRevisions.current.clear();
    // Carry only the source card's metadata into the transition's first frame.
    // The ordinary process owner still refreshes its state and reads its output.
    const retained = new Map<string, ProcessView>();
    for (const record of controls.values()) if (!selectedProcessId || record.process.processId === selectedProcessId) {
      retained.set(record.process.processId, { process: record.process, details: Boolean(selectedProcessId),
        output: '', reading: false, available: false });
    }
    if (seed && !retained.has(seed.processId)) retained.set(seed.processId,
      { process: seed, details: true, output: '', reading: false });
    setItems(retained);
    setLoading(retained.size === 0);
    const ownerRevision = revision.current;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      await refresh();
      if (ownerRevision === revision.current) timer = setTimeout(poll, 1000);
    };
    void poll();
    return () => {
      revision.current += 1;
      if (timer) clearTimeout(timer);
    };
  }, [ownerKey, refresh, seed, controls, selectedProcessId]);

  const read = useCallback(async (processId: string, cursor?: string) => {
    const requestRevision = revision.current;
    const readRevision = (readRevisions.current.get(processId) ?? 0) + 1;
    readRevisions.current.set(processId, readRevision);
    update(processId, (value) => ({ ...value, reading: true, error: undefined }));
    try {
      const page = await operations.current.readLog(processId, cursor);
      if (requestRevision !== revision.current || readRevisions.current.get(processId) !== readRevision) return;
      update(processId, (value) => ({
        ...value,
        process: page.process,
        output: cursor && !page.gapBeforeOutput
          ? value.output + page.output
          : page.output,
        cursor: page.outputCursor,
        error: page.gapBeforeOutput
          ? '较早输出已不在保留范围，下面从当前可用位置继续。'
          : page.truncatedByResponseBound
            ? '本次只读取了部分可用输出。'
            : undefined,
        reading: false,
      }));
    } catch (caught) {
      if (requestRevision !== revision.current || readRevisions.current.get(processId) !== readRevision) return;
      update(processId, (value) => ({
        ...value,
        reading: false,
        error: caught instanceof Error ? caught.message : '输出暂时无法读取。',
      }));
    }
  }, [update]);

  useEffect(() => {
    if (!selectedProcessId) return;
    const ownerRevision = revision.current;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const item = itemsRef.current.get(selectedProcessId);
      if (item && item.available !== false && !item.reading) await read(item.process.processId, item.cursor);
      if (ownerRevision === revision.current) timer = setTimeout(poll, 1000);
    };
    timer = setTimeout(poll, 1000);
    return () => { if (timer) clearTimeout(timer); };
  }, [ownerKey, selectedProcessId, read, seed]);

  const stop = async (process: BackgroundProcess, explicitlyRetryUnknown = false) => {
    if (!controlReady || !hostSessionId || !memoryRetained()) return;
    let record = controls.get(process.processId);
    if (record?.preparing) return;
    const current = record?.currentCommandId ? record.attempts.get(record.currentCommandId) : undefined;
    if (current?.sending || (current && !current.unknown
      && (current.control?.status === 'pending' || current.control?.userControl?.execution === 'RUNNING'))) return;
    if (current?.unknown && !explicitlyRetryUnknown) { void refresh(); return; }
    if (!record) {
      record = { process, preparing: false, attempts: new Map() };
      controls.set(process.processId, record);
    }
    const capturedRecord = record;
    const retained = () => memoryRetained() && controls.get(process.processId) === capturedRecord;
    const requestRevision = revision.current;
    const capturedOperations = operations.current;
    record.preparing = true;
    record.prepareError = undefined;
    redraw();
    let reference: UserControlCommandRef;
    try {
      reference = await capturedOperations.prepareControl({ operation: 'TERMINATE_BACKGROUND_PROCESS',
        sessionId, hostSessionId, targetKind: 'BACKGROUND_PROCESS', targetId: process.processId });
      if (!retained()) return;
      if (requestRevision !== revision.current || currentOwner.current.controls !== controls
        || currentOwner.current.ownerKey !== ownerKey || !currentOwner.current.canControl) return;
    } catch (caught) {
      if (retained()) record.prepareError = caught instanceof Error ? caught.message : '终止请求暂时无法准备。';
      return;
    } finally {
      if (retained()) {
        record.preparing = false;
        if (requestRevision === revision.current) redraw();
      }
    }
    // Retain only still-unresolved previous operations. Their feedback may
    // continue after a new, explicitly requested physical termination.
    for (const [id, previous] of record.attempts) if (!attemptNeedsPolling(previous)) record.attempts.delete(id);
    const attempt: BackgroundControlAttempt = { reference, sending: true };
    record.currentCommandId = reference.commandId;
    record.attempts.set(reference.commandId, attempt);
    redraw();
    const attemptRetained = () => retained() && record.attempts.get(reference.commandId) === attempt;
    try {
      const receipt = await capturedOperations.terminateProcess(reference);
      if (!attemptRetained()) return;
      if (receipt.commandId !== reference.commandId) throw new Error('终止请求返回了其他操作的结果。');
      attempt.control = receipt;
      attempt.unknown = false;
    } catch (caught) {
      if (!attemptRetained()) return;
      attempt.unknown = true;
      attempt.error = `${caught instanceof Error ? caught.message : '终止请求没有完成。'} 结果待按原操作身份确认。`;
    } finally {
      if (attemptRetained()) {
        attempt.sending = false;
        if (requestRevision === revision.current) redraw();
      }
    }
  };

  const endObservation = (record: BackgroundControlRecord) => {
    if (controls.get(record.process.processId) !== record) return;
    controls.delete(record.process.processId);
    redraw();
    void refresh();
  };

  const openDetail = (process: BackgroundProcess, source: HTMLButtonElement) => {
    if (!document.startViewTransition) { onOpenProcess(process); return; }
    const card = source.closest<HTMLElement>('.background-process')!;
    const identity = card.querySelector<HTMLElement>('header')!;
    const overview = card.parentElement!;
    card.style.viewTransitionName = 'process-card';
    identity.style.viewTransitionName = 'process-identity';
    overview.style.viewTransitionName = 'process-overview';
    const transition = document.startViewTransition({ types: ['process-detail'], update: () => flushSync(() => onOpenProcess(process)) });
    void transition.ready.catch(() => undefined);
    void transition.finished.finally(() => {
      for (const element of [card, identity, overview]) element.style.removeProperty('view-transition-name');
    }).catch(() => undefined);
  };


  if (loading && items.size === 0) return <WorkspaceLoading/>;
  if (error && items.size === 0) return <div className="task-inventory-notice task-inventory-notice--error"><AlertTriangle size={15} /><span><strong>后台终端不可用</strong><small>{error}</small></span><button onClick={() => void refresh()}><RefreshCw size={11} /> 重试</button></div>;
  if (!items.size) return <div className="inspector-empty"><TerminalSquare size={18} /><span>{selectedProcessId ? '原进程记录已不在当前保留范围。' : '暂无后台进程'}</span></div>;

  return <div className={selectedProcessId ? 'process-detail-panel' : 'background-terminal-panel'}>
    {[...items.values()].map((item) => {
      const record = controls.get(item.process.processId);
      const attempt = record?.currentCommandId ? record.attempts.get(record.currentCommandId) : undefined;
      const pending = Boolean(record?.preparing || attempt?.sending || (!attempt?.unknown
        && (attempt?.control?.status === 'pending' || attempt?.control?.userControl?.execution === 'RUNNING')));
      const canStop = controlReady && !attempt?.ownerUnavailable && item.available !== false && !terminal(item.process.status);
      return <article className={`background-process${selectedProcessId ? ' process-detail' : ''}`} key={item.process.processId}>
      <header>
        <ProcessIdentity command={item.process.command} status={pending ? '正在终止' : item.available === false ? '进程记录已不在保留范围' : processStatusText(item.process)} onOpen={selectedProcessId ? undefined : source => openDetail(item.process, source)} />
        {!selectedProcessId && <button type="button" className="background-process__info" aria-label="查看命令详情" aria-pressed={item.details} onClick={() => update(item.process.processId, (value) => ({ ...value, details: !value.details }))}><Info size={13} /></button>}
        {canStop && <button type="button" className="background-process__stop" aria-label={`终止 ${item.process.command}`} disabled={pending} onClick={() => void stop(item.process)}>{pending ? <LoaderCircle className="is-spinning" size={13} /> : <CircleStop size={13} />}</button>}
      </header>
      <div className={selectedProcessId ? 'process-detail__body' : undefined}>
      <AnimatedDisclosure open={Boolean(selectedProcessId) || item.details} className="background-process__details">
        <dl><div><dt>来源</dt><dd>{`Agent ${item.process.originTurnId}`}</dd></div><div><dt>目录</dt><dd>{item.process.cwd}</dd></div><div><dt>物理状态</dt><dd>{item.process.physicalState}</dd></div></dl>
      </AnimatedDisclosure>
      {(item.error || record?.prepareError) && <p className="background-process__attention"><AlertTriangle size={12} /> {item.error || record?.prepareError}</p>}
      {record && [...record.attempts.values()].map(controlAttempt => <div key={controlAttempt.reference.commandId}>
        {controlAttempt.reference.commandId !== record.currentCommandId && <small>上次终止操作{controlAttempt.unknown ? '结果待确认' : '反馈仍在确认'}</small>}
        {(controlAttempt.error || controlAttempt.control?.status === 'failed' || controlAttempt.control?.status === 'rejected') && <p className="background-process__attention"><AlertTriangle size={12} /> {controlAttempt.error || controlAttempt.control?.publicMessage || '终止操作未能确认完成。'}</p>}
        {controlAttempt.control?.status === 'succeeded' && <p>{controlAttempt.control.publicMessage || (controlAttempt.control.publicCode === 'CONTROL_ALREADY_TERMINAL' ? '原进程已结束。' : '终止操作已确认。')}</p>}
        {controlAttempt.control?.userControl?.feedback && <ProcessFeedback receipt={controlAttempt.control} />}
      </div>)}
      {attempt?.unknown && canStop && <button type="button" disabled={pending} onClick={() => void stop(item.process, true)}>再次请求停止</button>}
      {record && !record.preparing && !attempt?.sending && [...record.attempts.values()].some(attemptNeedsPolling)
        && <button type="button" onClick={() => endObservation(record)}>结束结果观察</button>}
      {selectedProcessId && <div className="background-process__output" aria-label="后台命令输出">
          <section className="background-process__result" aria-label="命令输出">
            <strong>输出</strong>
            <pre>{item.output || (item.reading ? '正在读取…' : '暂无输出')}</pre>
            {item.reading && <LoaderCircle className="is-spinning" size={12} />}
          </section>
        </div>}
      </div>
    </article>; })}
  </div>;
}
