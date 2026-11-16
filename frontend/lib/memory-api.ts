export type MemoryKind = 'FACT' | 'USER_PROFILE' | 'RESPONSE_PREFERENCE';
export interface MemorySelection { view: 'global' | 'project'; workspace_id: string | null }
export interface MemoryRecord {
  target: { kind: 'MEMORY'; memory_id: string };
  context_kind: 'GLOBAL' | 'CURRENT_PROJECT'; kind: MemoryKind;
  title: string; statement: string; recorded_at: string; updated_at: string;
}
export interface MemoryProject { workspace_id: string; label: string; root: string; last_activity_at: string | null }
export interface MemoryPage<T> { items: T[]; next_cursor: string | null }
export class MemoryApiError extends Error {
  constructor(message: string, public status: number, public code?: string) { super(message); }
}
function query(values: Record<string, string | number | null | undefined>) {
  const result = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value !== null && value !== undefined && value !== '') result.set(key, String(value)); });
  return `?${result}`;
}
export class LocalMemoryApi {
  private async request<T>(path: string, method = 'GET', value?: unknown): Promise<T> {
    const response = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store',
      ...(value === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) }) });
    const data = await response.json() as T & { error?: { message?: string; code?: string } };
    if (!response.ok) throw new MemoryApiError(data.error?.message ?? '记忆操作失败，请刷新后核对', response.status, data.error?.code);
    return data;
  }
  projects(cursor?: string) { return this.request<MemoryPage<MemoryProject>>(`/api/memories/projects${query({ cursor })}`); }
  catalog(selection: MemorySelection, filters: { kind?: string; search?: string; cursor?: string }) {
    return this.request<MemoryPage<MemoryRecord>>(`/api/memories${query({ ...selection, ...filters })}`);
  }
  detail(selection: MemorySelection, id: string) {
    return this.request<MemoryRecord>(`/api/memories/${encodeURIComponent(id)}${query({ ...selection })}`);
  }
  create(selection: MemorySelection, value: { kind: MemoryKind; title: string; statement: string }) {
    return this.request<MemoryRecord>('/api/memories', 'POST', { ...selection, ...value });
  }
  edit(selection: MemorySelection, record: MemoryRecord, changes: { statement?: string; destination?: MemorySelection }) {
    return this.request<{ record: MemoryRecord; changed: boolean }>(`/api/memories/${encodeURIComponent(record.target.memory_id)}`, 'PATCH',
      { ...selection, expected_updated_at: record.updated_at, ...changes });
  }
  delete(selection: MemorySelection, record: MemoryRecord) {
    return this.request<Record<string, never>>(`/api/memories/${encodeURIComponent(record.target.memory_id)}`, 'DELETE',
      { ...selection, expected_updated_at: record.updated_at });
  }
}
