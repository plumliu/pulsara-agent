import { WorkspaceLoading } from './workspace-loading';
import {
  AlertTriangle,
  ChevronDown,
  CircleStop,
  Info,
  LoaderCircle,
  RefreshCw,
  TerminalSquare,
  SquareArrowOutUpRight,
} from 'lucide-react';
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { createPortal, flushSync } from 'react-dom';
import { usePromptHover } from '../lib/prompt-hover';
import { createUserControlCommandRef } from '../lib/runtime-adapter';
import { useDesktopWorkspace } from './desktop-workspace';
import { AnimatedDisclosure } from './animated-disclosure';
import type {
  BackgroundProcess,
  BackgroundProcessLog,
  BackgroundProcessPage,
  CommandReceipt,
  UserControlCommandRef,
  UserControlQueryResult,
} from '../lib/runtime-adapter';

interface ProcessView {
  process: BackgroundProcess;
  expanded: boolean;
  details: boolean;
  output: string;
  cursor?: string;
  reading: boolean;
  error?: string;
  control?: CommandReceipt;
  reference?: UserControlCommandRef;
}

const terminal = (status: string) => status !== 'running';

function ProcessIdentity({ command, status, expanded, onToggle }: { command: string; status: string; expanded?: boolean; onToggle?: () => void }) {
  const { trigger, details, position, show, hide, keep } = usePromptHover<HTMLDivElement>(440);
  const [placement, setPlacement] = useState(position);
  if (position && position !== placement) setPlacement(position);
  const id = useId();
  const copy = <><strong>{command}</strong><small>{status}</small></>;
  return <div ref={trigger} className="background-process__identity" onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
    {onToggle ? <button type="button" className="background-process__toggle" aria-expanded={expanded} aria-describedby={position ? id : undefined} onClick={onToggle}><ChevronDown size={13} /><span>{copy}</span></button>
      : <div className="process-detail__identity" tabIndex={0} aria-describedby={position ? id : undefined}><TerminalSquare size={16} /><span>{copy}</span></div>}
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

const controlQueryMessage = (status: UserControlQueryResult['status']): string => (
  status === 'OWNER_UNAVAILABLE'
    ? '原 Host 已失效；不会把这次操作改投新的 Host。'
    : '当前无法取得这次操作的结果；不会自动重发。'
);

export function BackgroundTerminalPanel({
  processId: selectedProcessId,
  onOpenProcess,
  ownerKey,
  sessionId,
  hostSessionId,
  controlAdmissionDeadlineMs,
  canControl,
  loadProcesses,
  readLog,
  terminateProcess,
  queryControl,
}: {
  processId?: string;
  onOpenProcess?: (process: BackgroundProcess) => void;
  ownerKey: string;
  sessionId: string;
  hostSessionId?: string;
  controlAdmissionDeadlineMs?: number;
  canControl: boolean;
  loadProcesses: (cursor?: string) => Promise<BackgroundProcessPage>;
  readLog: (processId: string, cursor?: string) => Promise<BackgroundProcessLog>;
  terminateProcess: (reference: UserControlCommandRef) => Promise<CommandReceipt>;
  queryControl: (reference: UserControlCommandRef) => Promise<UserControlQueryResult>;
}) {
  const [items, setItems] = useState<Map<string, ProcessView>>(new Map());
  const itemsRef = useRef(items);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const revision = useRef(0);
  const readRevisions = useRef(new Map<string, number>());
  const operations = useRef({ loadProcesses, readLog, terminateProcess, queryControl });
  const workspace = useDesktopWorkspace();
  const localControls = useRef(new Map<string, Pick<ProcessView, 'control' | 'reference' | 'error'>>());
  const controlOwner = JSON.stringify([sessionId, hostSessionId]);
  if (workspace && !workspace.backgroundControls.has(controlOwner)) workspace.backgroundControls.set(controlOwner, new Map());
  const controls = useMemo(() => ({ current: workspace?.backgroundControls.get(controlOwner) ?? localControls.current }), [workspace?.backgroundControls, controlOwner]);
  const controlReady = Boolean(
    canControl && hostSessionId && controlAdmissionDeadlineMs,
  );
  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  useEffect(() => {
    operations.current = { loadProcesses, readLog, terminateProcess, queryControl };
  }, [loadProcesses, queryControl, readLog, terminateProcess]);

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
          const retainedControl = controls.current.get(process.processId);
          next.set(process.processId, existing ? { ...existing, process } : {
            process, expanded: Boolean(selectedProcessId), details: Boolean(selectedProcessId), output: '', reading: false,
            ...retainedControl,
          });
        }
        return next;
      });
      for (const process of processes) {
        const retained = controls.current.get(process.processId);
        if (!retained?.reference) continue;
        try {
          const query = await operations.current.queryControl(retained.reference);
          if (requestRevision !== revision.current) continue;
          if (query.status !== 'FOUND' || !query.receipt) {
            const message = controlQueryMessage(query.status);
            controls.current.set(process.processId, {
              reference: retained.reference,
              control: retained.control,
              error: message,
            });
            update(process.processId, (value) => ({ ...value, error: message }));
            continue;
          }
          const control = query.receipt;
          controls.current.set(process.processId, {
            reference: retained.reference,
            control,
          });
          setItems((current) => {
            const value = current.get(process.processId);
            if (!value) return current;
            const next = new Map(current);
            next.set(process.processId, {
              ...value,
              control,
              error: undefined,
            });
            return next;
          });
        } catch {
          if (requestRevision !== revision.current) return;
        }
      }
      setError(undefined);
    } catch (caught) {
      if (requestRevision === revision.current) setError(
        caught instanceof Error ? caught.message : '后台命令暂时无法读取。',
      );
    } finally {
      if (requestRevision === revision.current) setLoading(false);
    }
  }, [update, selectedProcessId, controls]);

  useEffect(() => {
    revision.current += 1;
    readRevisions.current.clear();
    setItems(new Map());
    setLoading(true);
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
  }, [ownerKey, refresh]);

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
    const ownerRevision = revision.current;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const expanded = [...itemsRef.current.values()].filter((item) => item.expanded && !item.reading);
      for (const item of expanded) await read(item.process.processId, item.cursor);
      if (ownerRevision === revision.current) timer = setTimeout(poll, 1000);
    };
    timer = setTimeout(poll, 1000);
    return () => { if (timer) clearTimeout(timer); };
  }, [ownerKey, read]);

  const stop = async (process: BackgroundProcess) => {
    if (controls.current.has(process.processId)) { await refresh(); return; }
    const controlMemory = controls.current;
    const requestRevision = revision.current;
    const reference = createUserControlCommandRef(
      { hostSessionId, controlAdmissionDeadlineMs },
      sessionId,
      'TERMINATE_BACKGROUND_PROCESS',
      'BACKGROUND_PROCESS',
      process.processId,
    );
    controlMemory.set(process.processId, { reference });
    update(process.processId, (value) => ({ ...value, reference, error: undefined }));
    try {
      const receipt = await operations.current.terminateProcess(reference);
      if (requestRevision !== revision.current) return;
      controlMemory.set(process.processId, { reference, control: receipt });
      update(process.processId, (value) => ({ ...value, control: receipt, reference }));
      if (controlNeedsPolling(receipt)) {
        const poll = async () => {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          if (requestRevision !== revision.current) return;
          try {
            const query = await operations.current.queryControl(reference);
            if (requestRevision !== revision.current) return;
            if (query.status !== 'FOUND' || !query.receipt) {
              const message = controlQueryMessage(query.status);
              controlMemory.set(process.processId, { reference, control: receipt, error: message });
              update(process.processId, (value) => ({ ...value, error: message }));
              return;
            }
            const result = query.receipt;
            controlMemory.set(process.processId, { reference, control: result });
            update(process.processId, (value) => ({ ...value, control: result, error: undefined }));
            if (controlNeedsPolling(result)) void poll();
          } catch (caught) {
            if (requestRevision !== revision.current) return;
            const message = caught instanceof Error ? caught.message : '控制状态暂时无法查询。';
            controlMemory.set(process.processId, { reference, control: receipt, error: message });
            update(process.processId, (value) => ({ ...value, error: message }));
          }
        };
        void poll();
      }
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : '终止请求没有完成。';
      controlMemory.set(process.processId, { reference, error: message });
      if (requestRevision === revision.current) update(process.processId, (value) => ({
        ...value, reference, error: `${message} 结果待按原操作身份确认。`,
      }));
    }
  };

  const openDetail = (process: BackgroundProcess, source: HTMLButtonElement) => {
    if (!onOpenProcess) return;
    if (!document.startViewTransition) { onOpenProcess(process); return; }
    const card = source.closest<HTMLElement>('.background-process')!;
    const identity = card.querySelector<HTMLElement>('.background-process__identity')!;
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
  if (!items.size) return <div className="inspector-empty"><TerminalSquare size={18} /><span>{selectedProcessId ? '原进程记录已不在当前保留范围。' : '当前没有已交付的后台命令'}</span></div>;

  return <div className={selectedProcessId ? 'process-detail-panel' : 'background-terminal-panel'}>
    {[...items.values()].map((item) => <article className={`background-process${selectedProcessId ? ' process-detail' : ''}${item.expanded ? ' is-expanded' : ''}`} key={item.process.processId}>
      <header>
        <ProcessIdentity command={item.process.command} status={item.control?.status === 'pending' ? '正在终止' : processStatusText(item.process)} expanded={item.expanded} onToggle={selectedProcessId ? undefined : () => {
          if (item.expanded) {
            readRevisions.current.set(
              item.process.processId,
              (readRevisions.current.get(item.process.processId) ?? 0) + 1,
            );
          }
          update(item.process.processId, (value) => ({
            ...value,
            expanded: !value.expanded,
            reading: item.expanded ? false : value.reading,
          }));
          if (!item.expanded && !item.output) void read(item.process.processId);
        }} />
        {!selectedProcessId && onOpenProcess && <button type="button" className="background-process__open" aria-label="打开到新标签页" title="打开到新标签页" onClick={event => openDetail(item.process, event.currentTarget)}><SquareArrowOutUpRight size={13} /><span>打开到新标签页</span></button>}
        {!selectedProcessId && <button type="button" className="background-process__info" aria-label="查看命令详情" aria-pressed={item.details} onClick={() => update(item.process.processId, (value) => ({ ...value, details: !value.details }))}><Info size={13} /></button>}
        {controlReady && !terminal(item.process.status) && <button type="button" className="background-process__stop" aria-label={`终止 ${item.process.command}`} disabled={Boolean(item.reference && (!item.control || controlNeedsPolling(item.control)))} onClick={() => void stop(item.process)}>{item.control?.status === 'pending' ? <LoaderCircle className="is-spinning" size={13} /> : <CircleStop size={13} />}</button>}
      </header>
      <div className={selectedProcessId ? 'process-detail__body' : undefined}>
      <AnimatedDisclosure open={Boolean(selectedProcessId) || item.details} className="background-process__details">
        <dl><div><dt>来源</dt><dd>{`Agent ${item.process.originTurnId}`}</dd></div><div><dt>目录</dt><dd>{item.process.cwd}</dd></div><div><dt>物理状态</dt><dd>{item.process.physicalState}</dd></div></dl>
      </AnimatedDisclosure>
      {(item.error || item.control?.status === 'failed' || item.control?.status === 'rejected') && <p className="background-process__attention"><AlertTriangle size={12} /> {item.error || item.control?.publicMessage || '终止操作只完成了一部分。'}</p>}
      {item.control?.userControl?.feedback && <p className="background-process__feedback">反馈：{item.control.userControl.feedback.canonicalStatus === 'ACCEPTED' ? '已记录' : item.control.userControl.feedback.canonicalStatus === 'NOT_REQUIRED' ? '本次无需记录' : '记录状态待确认'} · {item.control.userControl.feedback.inclusionStatus === 'INCLUDED' ? '已编入原轮次模型请求' : item.control.userControl.feedback.inclusionStatus === 'TARGET_ENDED_BEFORE_INCLUSION' ? '原轮次在纳入前结束' : item.control.userControl.feedback.inclusionStatus === 'NOT_APPLICABLE' ? '不适用模型请求' : '请求纳入状态待确认'}{item.control.userControl.feedback.transportInvocationAttempted ? ` · 本地请求${item.control.userControl.feedback.transportInvocationSucceeded === false ? '发起失败' : '已发起'}` : ''}</p>}
      <AnimatedDisclosure open={item.expanded} className="background-process__output-disclosure">
        <div className="background-process__output" aria-label="后台命令输出">
          {!selectedProcessId && <div className="background-process__command" role="region" aria-label="命令原文"><span aria-hidden="true">$</span> {item.process.command}</div>}
          <section className="background-process__result" aria-label="命令输出">
            <strong>输出</strong>
            <pre>{item.output || (item.reading ? '正在读取…' : '暂无输出')}</pre>
            {item.reading && <LoaderCircle className="is-spinning" size={12} />}
          </section>
        </div>
      </AnimatedDisclosure>
      </div>
    </article>)}
  </div>;
}
