import { afterEach, describe, expect, it, vi } from 'vitest';
import { decodeMemoryStream, LocalMemoryApi, MemoryApiError, type MemoryFact } from './memory-api';

afterEach(() => vi.unstubAllGlobals());

const header = { type: 'HEADER', root: 'fact:你好', view: 'global', workspace_id: null, disposition: 'READY' };
const end = { type: 'END', counts: { HEADER: 1 } };
const line = (value: unknown) => `${JSON.stringify(value)}\n`;

function response(text: string) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({
    start(controller) {
      // Split even multibyte characters and JSON tokens across physical chunks.
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      controller.close();
    },
  }));
}

describe('memory confirmation stream', () => {
  it('requires and preserves the complete ordered confirmation', async () => {
    expect(await decodeMemoryStream(response(line(header) + line(end)))).toEqual([header, end]);
  });

  it.each([
    line(header),
    line(header) + JSON.stringify(end),
    line(header) + line({ type: 'END', counts: { HEADER: 2 } }),
    line(header) + line(end) + line(header),
    line(end),
    line(header) + line({ type: 'UNKNOWN' }) + line(end),
  ])('rejects incomplete or inconsistent product records', async (text) => {
    await expect(decodeMemoryStream(response(text))).rejects.toThrow();
  });
});

describe('memory text edit', () => {
  it('sends only the statement, selected scope and exact observed version', async () => {
    const fact: MemoryFact = {
      fact_id: 'memory:one', context_id: 'ctx:global', kind: 'FACT',
      statement: 'initial', lifecycle: 'ACTIVE', recorded_at: '2026-09-05T00:00:00Z',
      updated_at: '2026-09-05T00:01:00+00:00',
    };
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ fact: { ...fact, statement: 'new text' }, changed: true, user_edited_at: fact.updated_at }), { status: 200 }));
    vi.stubGlobal('fetch', fetcher);
    const api = new LocalMemoryApi();
    expect((await api.editStatement({ view: 'global', workspace_id: null }, fact, 'new text')).changed).toBe(true);
    expect(fetcher).toHaveBeenCalledOnce();
    const [path, init] = fetcher.mock.calls[0];
    expect(path).toBe('/api/memories/memory%3Aone/statement');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body)).toEqual({
      view: 'global', workspace_id: null, statement: 'new text',
      expected_updated_at: fact.updated_at,
    });
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ error: { message: '记忆已变化' } }), { status: 409 }));
    await expect(api.editStatement({ view: 'global', workspace_id: null }, fact, 'newer')).rejects.toEqual(new MemoryApiError('记忆已变化', 409));
  });
});
