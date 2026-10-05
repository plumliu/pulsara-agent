import type { PermissionMode } from './pulsara-types';
export type ScheduleRule = { contract: 'scheduled-rule:v1' } & (
  | { kind: 'once'; run_at_utc: string }
  | { kind: 'interval'; anchor_at_utc: string; seconds: number }
  | { kind: 'daily'; start_date: string; time: string }
  | { kind: 'weekly'; start_date: string; time: string; weekdays: number[] }
  | { kind: 'monthly'; start_date: string; time: string; day: number }
);
export interface ScheduledValues { name: string; prompt: string; schedule: ScheduleRule; timezone: string; permission_mode: PermissionMode }
export interface ScheduledTask extends ScheduledValues {
  id: string; session_id: string; status: 'ACTIVE' | 'PAUSED' | 'COMPLETED'; next_run_at: string | null;
  revision: number; pending_queue_item_id?: string | null; last_turn_status?: string | null;
  last_turn_id?: string | null; last_entry_id?: string | null; prepare_error?: string | null;
}
export interface ScheduledRunRequest { client_command_id: string; request_at_utc: string; task_id: string; session_id: string; expected_revision: number }
export interface ScheduledPage { tasks: ScheduledTask[]; next_cursor: string | null }
export class ScheduledApiError extends Error { constructor(message: string, public status: number) { super(message); } }
export class LocalScheduledTasksApi {
  private async request<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
    const response = await fetch(`/api/scheduled-tasks${path}`, { method, signal, credentials: 'same-origin', cache: 'no-store',
      ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) });
    const value = await response.json() as T & { error?: { message?: string } };
    if (!response.ok) throw new ScheduledApiError(value.error?.message ?? '定时任务操作失败', response.status);
    return value as T;
  }
  list(status = '', cursor?: string, signal?: AbortSignal) { const q = new URLSearchParams(); if (status) q.set('status', status); if (cursor) q.set('cursor', cursor); return this.request<ScheduledPage>(`?${q}`, 'GET', undefined, signal); }
  get(id: string, signal?: AbortSignal) { return this.request<ScheduledTask>(`/${encodeURIComponent(id)}`, 'GET', undefined, signal); }
  create(session_id: string, values: ScheduledValues) { return this.request<ScheduledTask>('', 'POST', { session_id, values }); }
  update(task: ScheduledTask, values: ScheduledValues) { return this.request<ScheduledTask>(`/${encodeURIComponent(task.id)}`, 'PATCH', { expected_revision: task.revision, values }); }
  action(task: ScheduledTask, action: 'pause' | 'resume' | 'delete') { return this.request<{task: ScheduledTask | null}>(`/${encodeURIComponent(task.id)}/${action}`, 'POST', { expected_revision: task.revision }); }
  runNow(request: ScheduledRunRequest) { return this.request<{queue_item_id: string; status: string}>(`/${encodeURIComponent(request.task_id)}/run-now`, 'POST', request); }
  preview(schedule: ScheduleRule, timezone: string, signal?: AbortSignal) { return this.request<{next_run_at: string; local_time_fold: 0 | 1 | null}>('/preview', 'POST', { schedule, timezone }, signal); }
  localOnce(run_at_local: string, timezone: string, signal?: AbortSignal) { return this.request<{schedule: ScheduleRule}>('/local-once', 'POST', { run_at_local, timezone }, signal); }
}
export function scheduleLabel(task: ScheduledValues): string {
  const s = task.schedule;
  if (s.kind === 'once') return `一次 · ${new Intl.DateTimeFormat('zh-CN', { timeZone: task.timezone, dateStyle: 'short', timeStyle: 'short' }).format(new Date(s.run_at_utc))}`;
  if (s.kind === 'interval') return `每 ${s.seconds % 3600 === 0 ? `${s.seconds / 3600} 小时` : s.seconds % 60 === 0 ? `${s.seconds / 60} 分钟` : `${s.seconds} 秒`}`;
  if (s.kind === 'daily') return `每天 ${s.time}`;
  if (s.kind === 'weekly') return `每周 ${s.weekdays.map(d => ['一','二','三','四','五','六','日'][d - 1]).join('、')} ${s.time}`;
  return `每月 ${s.day} 号 ${s.time}`;
}
