import { afterEach, describe, expect, it, vi } from 'vitest';
import { LocalMemoryApi, MemoryApiError, type MemoryRecord } from '../../frontend/lib/memory-api';
const record: MemoryRecord = { target: { kind: 'MEMORY', memory_id: 'memory:aaaaaaaaaaaaaaaaaaaaaaaaaa' }, context_kind: 'GLOBAL', kind: 'FACT', title: '项目版本', statement: 'Python 3.12', recorded_at: '2026-11-16T01:00:00.123456Z', updated_at: '2026-11-16T01:00:00.123457Z' };
afterEach(() => vi.unstubAllGlobals());
describe('current memory API', () => {
  it('sends the exact displayed microsecond timestamp and selected scope when editing', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ record, changed: false }), { status: 200 })); vi.stubGlobal('fetch', fetch);
    const api = new LocalMemoryApi();
    const selection = { view: 'global' as const, workspace_id: null };
    await api.edit(selection, record, { statement: 'new body' });
    const [path, init] = fetch.mock.calls[0];
    expect(path).toBe('/api/memories/memory%3Aaaaaaaaaaaaaaaaaaaaaaaaaaa');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body)).toEqual({ ...selection, statement: 'new body', expected_updated_at: record.updated_at });
  });
  it('creates independent records and deletes one exact displayed target', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(record), { status: 201 })); vi.stubGlobal('fetch', fetch);
    const api = new LocalMemoryApi(); const selection = { view: 'global' as const, workspace_id: null };
    await api.create(selection, { kind: 'FACT', title: record.title, statement: record.statement });
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ ...selection, kind: 'FACT', title: record.title, statement: record.statement });
    fetch.mockResolvedValueOnce(new Response('{}', { status: 200 }));
    await api.delete(selection, record);
    expect(fetch.mock.calls[1][1].method).toBe('DELETE');
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ ...selection, expected_updated_at: record.updated_at });
  });
  it('reports a conflict and never retries an edit automatically', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'MEMORY_CHANGED', message: '记忆已变化' } }), { status: 409 })); vi.stubGlobal('fetch', fetch);
    await expect(new LocalMemoryApi().edit({ view: 'global', workspace_id: null }, record, { statement: 'new' })).rejects.toBeInstanceOf(MemoryApiError);
    expect(fetch).toHaveBeenCalledOnce();
  });
});
