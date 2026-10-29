import { afterEach, expect, it, vi } from 'vitest';
import { CanonicalHistoryView } from '../../frontend/lib/runtime-adapter';
import { parsePresentationResult } from '../../frontend/lib/presentation-targets';

const inline = (text: string) => ({kind: 'INLINE', inline_content: Buffer.from(text).toString('base64')});
afterEach(() => vi.unstubAllGlobals());

it('groups accepted program results under their exact source exec without extra conversation bubbles', () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    session_id: 'session-1', cut_sequence: '3', event_sequence_cut: '3', entry_ids: [],
    groups: [], entry_contexts: [], interruption_notices: [], compactions: [],
  }), {status: 200})));
  const entries = [
    {entry_id: 'request', turn_id: 'turn-1', entry_sequence: '1', entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{block_id: 'block', block_kind: 'TOOL_CALL', tool_call_id: 'exec-call', tool_name: 'exec'}]},
    {entry_id: 'child-result', turn_id: 'turn-1', entry_sequence: '2', entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT', content: inline('actual file result'),
      tool_result: {source_kind: 'CODE_MODE_CALL', source_exec_attempt_id: 'exec-attempt', invocation_id: 'child-call',
        attempt_id: 'child-attempt', tool_name: 'read_file', original_arguments_json: '{"path":"a.py"}', result_state: 'SUCCESS'}},
    {entry_id: 'exec-result', turn_id: 'turn-1', entry_sequence: '3', entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT', content: inline('{"state":"running"}'),
      tool_result: {source_kind: 'MODEL_TOOL_CALL', attempt_id: 'exec-attempt', assistant_entry_id: 'request', tool_call_id: 'exec-call', result_state: 'SUCCESS'}},
  ];
  const view = new CanonicalHistoryView('session-1', {path: '/tmp', outcome: 'AVAILABLE', reason: null}, {
    session_id: 'session-1', writer_generation: '1', event_sequence_cut: '3', entries,
    control: {},
  });
  const result = view.current();
  expect(result.messages).toHaveLength(1);
  expect(result.messages[0].traces?.[0]).toMatchObject({toolName: 'exec', attemptId: 'exec-attempt',
    programCalls: [{id: 'child-call', toolName: 'read_file', resultEntryId: 'child-result', resultText: 'actual file result'}]});
  expect(result.messages[0].sourceEntryIds).toContain('child-result');
  void view.close();
});

it('rejects removed task presentation targets and task panels', () => {
  expect(parsePresentationResult(JSON.stringify({targets: [{type: 'task', task_id: 'old'}]}))).toBeUndefined();
  expect(parsePresentationResult(JSON.stringify({targets: [{target: {kind: 'PANEL', name: 'tasks'}}]}))).toBeUndefined();
});
