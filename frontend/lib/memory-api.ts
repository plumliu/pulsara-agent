export type MemoryKind = 'FACT' | 'USER_PROFILE' | 'RESPONSE_PREFERENCE' | 'DECISION';
export interface MemorySelection { view: 'global' | 'project'; workspace_id: string | null }
export interface MemoryFact {
  fact_id: string; context_id: string; kind: MemoryKind; lifecycle: 'ACTIVE' | 'SUPERSEDED';
  statement: string; recorded_at: string; updated_at: string;
  context_label?: string; needs_confirmation?: boolean;
}
export interface MemoryProject { workspace_id: string; label: string; root: string; last_activity_at: string }
export interface MemoryPage<T> { items: T[]; next_cursor: string | null }
export interface MemorySource { session_id: string; turn_id: string; entry_id: string }
export interface MemorySourceProjection { availability: 'OPEN' | 'CLOSED' | 'DELETED'; locator: MemorySource | null }
export interface MemoryRelation {
  relation_id: string; subject: MemoryFact; companion: MemoryFact;
  relative_role: 'BASED_ON' | 'BASIS_FOR' | 'UPDATES' | 'UPDATED_BY' | 'CONFLICTS_WITH';
  recorded_at: string; owner: { write_tool: 'remember' | 'mark_memory_relation'; source: MemorySourceProjection };
}
export interface MemoryDetail {
  fact: MemoryFact; formation: string;
  user_edited_at?: string | null;
  source: MemorySourceProjection;
  relations: MemoryRelation[]; next_cursor: string | null;
}
export type MemoryRecord =
  | ({ type: 'HEADER'; root: string; disposition?: 'READY' | 'NEEDS_RESOLUTION'; result?: 'DELETED' } & MemorySelection)
  | { type: 'ADDITIONAL_ROOT'; fact_id: string }
  | { type: 'FACT_DELETE'; fact: MemoryFact }
  | { type: 'FACT_RESTORE'; fact: MemoryFact; planned_lifecycle: 'ACTIVE' }
  | ({ type: 'RELATION_EFFECT'; effect: 'REMOVED' | 'BECOMES_ACTIVE_CONFLICT' } & Omit<MemoryRelation, 'owner'>)
  | { type: 'RESTORATION_CONFLICT'; subject: MemoryFact; companion: MemoryFact | null; reason: string; group: string }
  | { type: 'END'; counts: Record<string, number> };

export class MemoryApiError extends Error {
  constructor(message: string, public status: number, public preview?: MemoryRecord[]) { super(message); }
}

function query(values: Record<string, string | number | null | undefined>) {
  const result = new URLSearchParams();
  Object.entries(values).forEach(([k, v]) => { if (v !== null && v !== undefined && v !== '') result.set(k, String(v)); });
  return `?${result}`;
}

export async function decodeMemoryStream(response: Response): Promise<MemoryRecord[]> {
  if (!response.body) throw new Error('记忆确认未完整返回');
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  const records: MemoryRecord[] = [];
  const counts: Record<string, number> = {};
  let pending = ''; let ended = false; let lastOrder = -1;
  const order = ['HEADER', 'ADDITIONAL_ROOT', 'FACT_DELETE', 'RELATION_EFFECT', 'FACT_RESTORE', 'RESTORATION_CONFLICT', 'END'];
  const consume = () => {
    let index: number;
    while ((index = pending.indexOf('\n')) >= 0) {
      const line = pending.slice(0, index); pending = pending.slice(index + 1);
      if (ended || !line) throw new Error('记忆确认顺序无效');
      const record = JSON.parse(line) as MemoryRecord;
      const rank = order.indexOf(record.type);
      if (rank < 0 || rank < lastOrder || (record.type === 'HEADER' && records.length > 0)) throw new Error('记忆确认记录无效');
      lastOrder = rank;
      if (!records.length && record.type !== 'HEADER') throw new Error('缺少记忆确认头');
      if (record.type === 'END') {
        if (Object.keys(counts).length !== Object.keys(record.counts).length || Object.entries(counts).some(([k, v]) => record.counts[k] !== v)) throw new Error('记忆确认未完整返回');
        ended = true;
      } else counts[record.type] = (counts[record.type] ?? 0) + 1;
      records.push(record);
    }
  };
  try {
    while (true) {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const { value, done } = await Promise.race([
        reader.read(),
        new Promise<never>((_, reject) => { timer = setTimeout(() => { void reader.cancel(); reject(new Error('记忆确认传输中断，请重新预览')); }, 30_000); }),
      ]).finally(() => clearTimeout(timer));
      if (done) break;
      pending += decoder.decode(value, { stream: true }); consume();
    }
    pending += decoder.decode(); consume();
    if (pending || !ended) throw new Error('记忆操作结果未完整返回，请刷新核对后重试');
    return records;
  } finally { await reader.cancel(); reader.releaseLock(); }
}

export class LocalMemoryApi {
  private async read<T>(path: string): Promise<T> {
    const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
    const data = await response.json() as T & { error?: { message?: string } };
    if (!response.ok) throw new MemoryApiError(data.error?.message ?? '记忆读取失败', response.status);
    return data;
  }
  projects(cursor?: string) { return this.read<MemoryPage<MemoryProject>>(`/api/memories/projects${query({ cursor })}`); }
  catalog(selection: MemorySelection, filters: { kind?: string; lifecycle?: string; search?: string; cursor?: string }) {
    return this.read<MemoryPage<MemoryFact>>(`/api/memories${query({ ...selection, ...filters })}`);
  }
  detail(selection: MemorySelection, id: string, cursor?: string) {
    return this.read<MemoryDetail>(`/api/memories/${encodeURIComponent(id)}${query({ ...selection, cursor })}`);
  }
  async editStatement(selection: MemorySelection, fact: MemoryFact, statement: string) {
    const response = await fetch(`/api/memories/${encodeURIComponent(fact.fact_id)}/statement`, {
      method: 'PATCH', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...selection, statement, expected_updated_at: fact.updated_at }),
    });
    const result = await response.json() as { fact: MemoryFact; user_edited_at: string | null; changed: boolean; error?: { message?: string } };
    if (!response.ok) throw new MemoryApiError(result.error?.message ?? '记忆编辑失败，请刷新详情后重试', response.status);
    return result;
  }
  private async send(id: string, preview: boolean, records: MemoryRecord[]) {
    // Blob parts preserve record boundaries without an aggregate JSON string.
    // Browser ReadableStream uploads require HTTP/2; this loopback server is HTTP/1.1.
    const body = new Blob(records.map(r => `${JSON.stringify(r)}\n`), { type: 'application/x-ndjson' });
    const response = await fetch(`/api/memories/${encodeURIComponent(id)}${preview ? '/deletion-preview' : ''}`, {
      method: preview ? 'POST' : 'DELETE', body, credentials: 'same-origin',
    });
    if (!response.headers.get('Content-Type')?.includes('application/x-ndjson')) {
      const data = await response.json() as { error?: { message?: string } };
      throw new MemoryApiError(data.error?.message ?? '记忆操作失败', response.status);
    }
    const result = await decodeMemoryStream(response);
    if (!response.ok) throw new MemoryApiError('记忆已发生变化，请重新查看并确认', response.status, result);
    return result;
  }
  preview(selection: MemorySelection, id: string, additional: string[] = []) {
    const roots = [...new Set(additional)].filter(v => v !== id).sort();
    const records: MemoryRecord[] = [{ type: 'HEADER', ...selection, root: id }, ...roots.map(fact_id => ({ type: 'ADDITIONAL_ROOT' as const, fact_id }))];
    records.push({ type: 'END', counts: { HEADER: 1, ...(roots.length ? { ADDITIONAL_ROOT: roots.length } : {}) } });
    return this.send(id, true, records);
  }
  delete(id: string, confirmation: MemoryRecord[]) { return this.send(id, false, confirmation); }
}
