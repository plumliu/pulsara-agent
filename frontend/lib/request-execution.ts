/** Execution of an accepted new-turn request; steer only has delivery state. */
export interface RequestExecution {
  status: 'queued' | 'running' | 'completed' | 'stopped';
  terminal_reason: string | null;
}

export function requestExecutionFromWire(value: {
  status?: string; terminal_reason?: string;
}): RequestExecution {
  const status = {
    REQUEST_EXECUTION_QUEUED: 'queued',
    REQUEST_EXECUTION_RUNNING: 'running',
    REQUEST_EXECUTION_COMPLETED: 'completed',
    REQUEST_EXECUTION_STOPPED: 'stopped',
  } as const;
  const parsed = status[value.status as keyof typeof status];
  if (!parsed) throw new Error('Invalid request execution status');
  const reason = value.terminal_reason || null;
  if ((parsed === 'stopped') !== (reason !== null)) {
    throw new Error('Invalid request execution terminal reason');
  }
  return { status: parsed, terminal_reason: reason };
}
