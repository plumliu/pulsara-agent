import { createHash, webcrypto } from 'node:crypto';
import { afterEach, expect, it, vi } from 'vitest';
import { LocalHttpRuntimeAdapter } from '../../frontend/lib/runtime-adapter';

// Independent review regressions: assertions cover observable history, not
// private reader fields. The fake HTTP owner never dispatches model work.
const session = 'session:review';
const inline = (text: string) => ({ inline_content: Buffer.from(text).toString('base64') });
const blob = (text: string) => ({ digest: `sha256:${createHash('sha256').update(text).digest('hex')}`, size: String(Buffer.byteLength(text)), media_type: 'text/plain', codec: 'utf-8' });
const entry = (id: string, sequence: number, extra: object = {}) => ({
  entry_id: id, entry_sequence: String(sequence), turn_id: 'turn:root',
  entry_kind: 'ASSISTANT_MESSAGE', scope_kind: 'ROOT', root_final: false,
  blocks: [{ block_id: `block:${id}`, block_kind: 'TEXT', content: inline(id) }], ...extra,
});
const cursor = { session_id: session, cut_sequence: '20', event_sequence_cut: '30', entry_sequence: '20' };
function payload(entries: object[], older?: object, control: object = {}) {
  return {
    connection_id: 'connection:review', connection_generation: 1, session_id: session, role: 'controller',
    snapshot: { snapshot: { session_id: session, entry_sequence_cut: '20', event_sequence_cut: '30', writer_generation: '1', entries, older_history_cursor: older, control } },
    live_hello: { live_owner_epoch: '1' }, live_control_snapshot: { snapshot: {} },
  };
}
type Request = { entry_ids?: string[]; boundary_event_ids?: string[]; cut_sequence?: string; event_sequence_cut?: string; anchor_entry_id?: string; [key: string]: any };
function context(body: Request, extra: object = {}) {
  return {
    session_id: session, cut_sequence: body.cut_sequence ?? '21', event_sequence_cut: body.event_sequence_cut ?? '35',
    entry_ids: body.entry_ids ?? [], groups: [], tasks: [], batches: [], interruption_notices: [], compactions: [],
    entry_contexts: (body.entry_ids ?? []).map(id => ({ entry_id: id, owner_kind: 'EXECUTED_TURN', owner_id: 'turn:root', root_owner_id: 'turn:root', segment_id: 'turn:root:initial', segment_start_entry_id: 'start' })),
    ...extra,
  };
}
const marker = (id: string, edge: number, eventSequence: number) => ({ event_id: id, event_sequence: String(eventSequence), turn_id: 'turn:root', context_binding_revision_id: `binding:${id}`, source_through_sequence: '1', adopted_after_entry_sequence: String(edge), accepted_at_utc: '2026-10-18T00:00:00Z' });
const occurrence = (id: string, sequence: number) => ({ event_type: 'COMPACTION_ADOPTED', event_id: id, event_sequence: String(sequence), projection_kind: 'CURRENT_CONTROL' });
const unavailable = (message: string) => new Response(JSON.stringify({ error: { code: 'HISTORY_TEMPORARILY_UNAVAILABLE', message, retryable: true } }), { status: 503 });
function fixture(route: (url: string, body: Request) => unknown | Promise<unknown>) {
  const calls: Array<{ url: string; body: Request }> = [];
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('fetch', vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = String(input), body = JSON.parse(String(init.body ?? '{}')) as Request;
    calls.push({ url, body });
    const value = await route(url, body);
    if (value instanceof Response) return value;
    return new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  return calls;
}
const connect = () => new LocalHttpRuntimeAdapter().connect(session);
afterEach(() => vi.unstubAllGlobals());

it('retains both adoption boundaries when one observation carries two different edges and only the latest control', async () => {
  const calls = fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([entry('edge20', 20)]);
    if (url.endsWith('/observe')) return { observation: { through_event_sequence: '33', committed: [
      { ...occurrence('adoption:first', 31), control_read_event_sequence: '35', current_control: { latest_context_compaction: { context_binding_revision_id: 'binding:adoption:second', adopted_after_entry_sequence: '21' } } },
      { projection_kind: 'IMMUTABLE_ENTRY', event_sequence: '32', entry: entry('edge21', 21) },
      occurrence('adoption:second', 33),
    ] } };
    if (url.endsWith('/context') && body.boundary_event_ids) return context(body, { boundary_entries: body.boundary_event_ids.map(id => ({ event_id: id, entry_id: id === 'adoption:first' ? 'edge20' : 'edge21' })) });
    if (url.endsWith('/context')) return context(body, { compactions: body.event_sequence_cut === '30' ? [] : body.entry_ids!.map(id => id === 'edge20' ? marker('adoption:first', 20, 31) : marker('adoption:second', 21, 33)) });
    throw Error(url);
  });
  const reader = await connect();
  await vi.waitFor(() => expect(reader.current().messages[0].historyContextState).toBe('ready'));
  await reader.observe();
  await vi.waitFor(() => expect(reader.current().history!.compactions.map(item => item.eventId).sort()).toEqual(['adoption:first', 'adoption:second']));
  expect(reader.current().eventSequence).toBe(33);
  expect(calls.some(call => call.url.endsWith('/history') || call.url.endsWith('/command'))).toBe(false);
});

it('enriches a later-loaded old page with an already-observed boundary without changing its pagination cut', async () => {
  const calls = fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([entry('tail', 20)], cursor);
    if (url.endsWith('/observe')) return { observation: { through_event_sequence: '31', committed: [occurrence('adoption:old', 31)] } };
    if (url.endsWith('/context') && body.boundary_event_ids) return context(body, { boundary_entries: [{ event_id: 'adoption:old', entry_id: 'old18' }] });
    if (url.endsWith('/context')) return context(body, { compactions: body.entry_ids?.includes('old18') && body.event_sequence_cut === undefined ? [marker('adoption:old', 18, 31)] : [] });
    if (url.endsWith('/history')) return { history_page: { entries: [entry('old18', 18), entry('old19', 19)], has_more: true, older_history_cursor: { ...cursor, entry_sequence: '18' } } };
    throw Error(url);
  });
  const reader = await connect();
  await vi.waitFor(() => expect(reader.current().messages[0].historyContextState).toBe('ready'));
  await reader.observe();
  await vi.waitFor(() => expect(calls.some(call => call.body.boundary_event_ids)).toBe(true));
  await reader.readHistory!();
  await vi.waitFor(() => expect(reader.current().history!.compactions.map(item => item.eventId)).toEqual(['adoption:old']));
  const oldContexts = calls.filter(call => call.url.endsWith('/context') && call.body.entry_ids?.includes('old18'));
  expect(oldContexts.map(call => call.body.event_sequence_cut)).toEqual(['30', undefined]);
  expect(calls.filter(call => call.url.endsWith('/history'))[0].body.cursor).toEqual(cursor);
  expect(calls.filter(call => call.url.endsWith('/context') && call.body.entry_ids?.includes('tail'))).toHaveLength(1);
  expect(reader.current().eventSequence).toBe(31);
});

it('retries a failed non-first page context in a connected window without reading history again', async () => {
  let fail = true;
  const calls = fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([entry('first', 19)]);
    if (url.endsWith('/observe')) return { observation: { through_event_sequence: '31', committed: [{ projection_kind: 'IMMUTABLE_ENTRY', entry: entry('second', 20) }] } };
    if (url.endsWith('/context')) {
      if (body.entry_ids?.includes('second') && fail) return unavailable('second context failed');
      return context(body);
    }
    throw Error(url);
  });
  const reader = await connect();
  await vi.waitFor(() => expect(reader.current().messages[0].historyContextState).toBe('ready'));
  await reader.observe();
  await vi.waitFor(() => expect(reader.current().history!.error).toContain('second context failed'));
  expect(reader.current().history!.windows).toHaveLength(1);
  fail = false;
  await reader.readHistory!(reader.current().history!.windows[0].id);
  expect(reader.current().messages.find(message => message.id === 'second')?.historyContextState).toBe('ready');
  expect(calls.filter(call => call.url.endsWith('/context') && call.body.entry_ids?.includes('first'))).toHaveLength(1);
  expect(calls.filter(call => call.url.endsWith('/context') && call.body.entry_ids?.includes('second'))).toHaveLength(2);
  expect(calls.some(call => call.url.endsWith('/history'))).toBe(false);
});

it('keeps the fresh-context requirement after a refresh failure and an explicit retry', async () => {
  let failFresh = true;
  const calls = fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([entry('edge', 20)]);
    if (url.endsWith('/observe')) return { observation: { through_event_sequence: '31', committed: [occurrence('adoption:retry', 31)] } };
    if (url.endsWith('/context') && body.boundary_event_ids) return context(body, { boundary_entries: [{ event_id: 'adoption:retry', entry_id: 'edge' }] });
    if (url.endsWith('/context')) {
      if (body.event_sequence_cut === undefined && failFresh) return unavailable('fresh context failed');
      return context(body, { compactions: body.event_sequence_cut === undefined ? [marker('adoption:retry', 20, 31)] : [] });
    }
    throw Error(url);
  });
  const reader = await connect();
  await vi.waitFor(() => expect(reader.current().messages[0].historyContextState).toBe('ready'));
  await reader.observe();
  await vi.waitFor(() => expect(reader.current().history!.error).toContain('fresh context failed'));
  failFresh = false;
  await reader.readHistory!();
  expect(reader.current().history!.compactions.map(item => item.eventId)).toEqual(['adoption:retry']);
  expect(calls.filter(call => call.url.endsWith('/context') && call.body.entry_ids?.includes('edge')).map(call => call.body.event_sequence_cut)).toEqual(['30', undefined, undefined]);
  expect(reader.current().eventSequence).toBe(31);
});

it('does not let a late original-cut context satisfy a newer occurrence refresh', async () => {
  let resolveInitial!: (value: unknown) => void;
  const initial = new Promise(resolve => { resolveInitial = resolve; });
  const calls = fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([entry('edge', 20)]);
    if (url.endsWith('/observe')) return { observation: { through_event_sequence: '31', committed: [occurrence('adoption:late', 31)] } };
    if (url.endsWith('/context') && body.boundary_event_ids) return context(body, { boundary_entries: [{ event_id: 'adoption:late', entry_id: 'edge' }] });
    if (url.endsWith('/context') && body.event_sequence_cut === '30') return initial;
    if (url.endsWith('/context')) return context(body, { compactions: [marker('adoption:late', 20, 31)] });
    throw Error(url);
  });
  const reader = await connect();
  await reader.observe();
  expect(reader.current().eventSequence).toBe(31);
  expect(reader.current().messages[0].historyContextState).toBe('loading');
  resolveInitial(context({ entry_ids: ['edge'], cut_sequence: '20', event_sequence_cut: '30' }));
  await vi.waitFor(() => expect(reader.current().history!.compactions.map(item => item.eventId)).toEqual(['adoption:late']));
  expect(reader.current().messages[0].historyContextState).toBe('ready');
  expect(calls.filter(call => call.url.endsWith('/context') && call.body.entry_ids?.includes('edge')).map(call => call.body.event_sequence_cut)).toEqual(['30', undefined]);
});

it('still consumes an adoption occurrence crossed by a same-entry tail snapshot', async () => {
  const calls = fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([entry('edge', 20)]);
    if (url.endsWith('/history-snapshot')) return { snapshot: { snapshot: { session_id: session, entry_sequence_cut: '20', event_sequence_cut: '31', entries: [entry('edge', 20)], control: {} } } };
    if (url.endsWith('/observe')) return { observation: { through_event_sequence: '31', committed: [occurrence('adoption:snapshot', 31)] } };
    if (url.endsWith('/context') && body.boundary_event_ids) return context(body, { boundary_entries: [{ event_id: 'adoption:snapshot', entry_id: 'edge' }] });
    if (url.endsWith('/context')) return context(body, { compactions: body.event_sequence_cut === undefined ? [marker('adoption:snapshot', 20, 31)] : [] });
    throw Error(url);
  });
  const reader = await connect();
  await vi.waitFor(() => expect(reader.current().messages[0].historyContextState).toBe('ready'));
  await reader.snapshot();
  expect(reader.current().eventSequence).toBe(30);
  await reader.observe();
  await vi.waitFor(() => expect(reader.current().history!.compactions.map(item => item.eventId)).toEqual(['adoption:snapshot']));
  expect(calls.find(call => call.url.endsWith('/observe'))!.body.after_event_sequence).toBe('30');
  expect(reader.current().eventSequence).toBe(31);
});

it('keeps a disconnected tool result and its gap until reading the intervening region', async () => {
  const creator = entry('creator', 2, { entry_kind: 'ASSISTANT_TOOL_REQUEST', blocks: [{ block_id: 'call:one', block_kind: 'TOOL_CALL', tool_call_id: 'call:one', tool_name: 'read_file' }] });
  const result = entry('result', 20, { entry_kind: 'TOOL_RESULT', blocks: [], content: inline('完成'), tool_result: { assistant_entry_id: 'creator', tool_call_id: 'call:one', result_state: 'SUCCESS' } });
  fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([result]);
    if (url.endsWith('/context')) return context(body);
    if (url.endsWith('/window') && body.anchor_entry_id) return { session_id: session, cut_sequence: '20', event_sequence_cut: '30', entries: [creator], newer_cursor: { ...cursor, entry_sequence: '2', direction: 'newer' } };
    if (url.endsWith('/window')) return { session_id: session, cut_sequence: '20', event_sequence_cut: '30', entries: Array.from({ length: 17 }, (_, index) => entry(`between:${index}`, index + 3)) };
    throw Error(url);
  });
  const reader = await connect();
  const located = await reader.locateEntry!('creator', new AbortController().signal);
  expect(located.messages.map(message => message.id)).toEqual(['creator']);
  expect(located.history!.atLatest).toBe(false);
  expect(located.messages[0].traces?.[0].resultText).toBeUndefined();
  expect(located.history!.windows.map(window => window.firstEntryId)).toEqual(['creator']);
  const connected = await reader.readHistory!(located.history!.windows[0].id, 'newer');
  expect(connected.history!.windows).toHaveLength(1);
  expect(connected.messages.some(message => message.id === 'result')).toBe(false);
  expect(connected.messages.find(message => message.id === 'creator')?.sourceEntryIds).toEqual(['creator', 'result']);
});

it('preserves a reconnected pending queue body until its exact canonical entry is verified', async () => {
  const text = '重连时已知的输入🙂';
  const encoded = JSON.stringify({ schema: 'pulsara.prompt/v2', parts: [{ type: 'text', text }] });
  const reference = { ...blob(encoded), media_type: 'application/vnd.pulsara.prompt+json' };
  const queued = { queue_item_id: 'queue:one', command_id: 'command:one', queue_sequence: '1', status: 'PENDING', delivery_mode: 'NEW_TURN', content: { ...reference, ...inline(encoded) } };
  const consumed = entry('input', 21, { entry_kind: 'USER_MESSAGE', blocks: [], content: reference, input_source: { command_id: queued.command_id, queue_item_id: queued.queue_item_id, delivery_mode: 'NEW_TURN' } });
  const calls = fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([], undefined, { prompt_queue: [queued], prompt_queue_total_count: '1' });
    if (url.endsWith('/context')) return context(body);
    if (url.endsWith('/observe')) return { observation: { through_event_sequence: '32', committed: [{ projection_kind: 'IMMUTABLE_ENTRY', entry: consumed }, { projection_kind: 'CURRENT_CONTROL', control_read_event_sequence: '32', current_control: { prompt_queue: [], prompt_queue_total_count: '0' } }] } };
    if (url.endsWith('/read-content')) return { content: { digest: reference.digest, complete_size: reference.size, offset_bytes: '0', content: Buffer.from(encoded).toString('base64'), complete: true } };
    throw Error(url);
  });
  const reader = await connect();
  await vi.waitFor(() => expect(reader.current().queuedPrompts[0]?.content?.parts).toEqual([{ type: 'text', text }]));
  const metadata = await reader.observe();
  expect(metadata.queuedPrompts).toEqual([]);
  expect(metadata.messages[0]).toMatchObject({ id: 'input', contentState: 'unloaded', pendingContent: { parts: [{ type: 'text', text }] } });
  expect(metadata.messages[0].body).toBe('');
  const verified = await reader.hydrateEntries!(['input']);
  expect(verified.messages[0]).toMatchObject({ contentState: 'ready', body: text });
  expect(verified.messages[0].pendingContent).toBeUndefined();
  expect(calls.some(call => call.url.endsWith('/command') || call.url.endsWith('/history-snapshot'))).toBe(false);
});


it('retains installed entry context and body throughout a deferred context refresh and its failure', async () => {
  let resolveRefresh!: (value: unknown) => void;
  const refresh = new Promise(resolve => { resolveRefresh = resolve; });
  const calls = fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([entry('edge', 20)], cursor);
    if (url.endsWith('/observe')) return { observation: { through_event_sequence: '31', committed: [occurrence('adoption:refresh', 31)] } };
    if (url.endsWith('/context') && body.boundary_event_ids) return context(body, { boundary_entries: [{ event_id: 'adoption:refresh', entry_id: 'edge' }] });
    if (url.endsWith('/context') && body.event_sequence_cut === '30') return context(body);
    if (url.endsWith('/context')) return refresh;
    throw Error(url);
  });
  const reader = await connect();
  await vi.waitFor(() => expect(reader.current().history!.initialReady).toBe(true));
  const installed = reader.current().messages[0];
  expect(installed).toMatchObject({ id: 'edge', body: 'edge', contentState: 'ready', historyContextState: 'ready', historyGroupId: 'turn:root:initial' });
  await reader.observe();
  await vi.waitFor(() => expect(calls.filter(call => call.url.endsWith('/context') && call.body.entry_ids?.includes('edge'))).toHaveLength(2));
  const pending = reader.current();
  expect(pending.history!.initialReady).toBe(true);
  expect(pending.history!.windows[0].contextState).toBe('loading');
  expect(pending.messages[0]).toMatchObject({ body: installed.body, historyGroupId: installed.historyGroupId, historyContextState: 'ready' });
  resolveRefresh(unavailable('refresh temporarily failed'));
  await vi.waitFor(() => expect(reader.current().history!.windows[0].contextState).toBe('error'));
  expect(reader.current().history!.windows[0].contextError).toContain('refresh temporarily failed');
  expect(reader.current().messages[0]).toMatchObject({ body: installed.body, historyGroupId: installed.historyGroupId, historyContextState: 'ready' });
  expect(reader.current().messages[0].historyContextError).toBeUndefined();
  expect(calls.some(call => call.url.endsWith('/history'))).toBe(false);
});

it('publishes initial context retry loading and joins a second retry without consuming the older cursor', async () => {
  let resolveRetry!: (value: unknown) => void;
  const retry = new Promise(resolve => { resolveRetry = resolve; });
  let attempts = 0;
  const calls = fixture((url, body) => {
    if (url.endsWith('/connections')) return payload([entry('tail', 20)], cursor);
    if (url.endsWith('/context')) {
      attempts += 1;
      return attempts === 1 ? unavailable('initial context failed') : retry;
    }
    if (url.endsWith('/history')) return { history_page: { entries: [entry('old', 19)], has_more: false } };
    throw Error(url);
  });
  const reader = await connect();
  await vi.waitFor(() => expect(reader.current().history!.initialError).toContain('initial context failed'));
  const publications: Array<{ initialError?: string; contextState?: string }> = [];
  const unsubscribe = reader.subscribe!(() => {
    const history = reader.current().history!;
    publications.push({ initialError: history.initialError, contextState: history.windows[0].contextState });
  });
  const first = reader.readHistory!();
  expect(publications.at(-1)).toEqual({ initialError: undefined, contextState: 'loading' });
  const second = reader.readHistory!();
  expect(reader.current().history!.initialReady).toBe(false);
  expect(reader.current().messages[0].historyGroupId).toBeUndefined();
  expect(reader.current().messages[0].historyContextState).toBe('loading');
  expect(calls.filter(call => call.url.endsWith('/context'))).toHaveLength(2);
  expect(calls.filter(call => call.url.endsWith('/history'))).toHaveLength(0);
  resolveRetry(context({ entry_ids: ['tail'], cut_sequence: '20', event_sequence_cut: '30' }));
  await Promise.all([first, second]);
  expect(reader.current().history!.initialReady).toBe(true);
  expect(reader.current().history!.hasOlder).toBe(true);
  expect(reader.current().messages.map(message => message.id)).toEqual(['tail']);
  expect(calls.filter(call => call.url.endsWith('/history'))).toHaveLength(0);
  unsubscribe();
});
