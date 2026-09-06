import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  LocalHttpRuntimeAdapter,
  mergeRuntimeTaskInventory,
  productVisibleText,
  selectPromptCommand,
  type RuntimeProjection,
} from './runtime-adapter';
import type { AgentTask } from './pulsara-types';

afterEach(() => vi.unstubAllGlobals());

it('requests Plugin discovery using only the source directory', async () => {
  const result = {candidates: []};
  const fetcher = vi.fn(async () => new Response(JSON.stringify(result), {status: 200}));
  vi.stubGlobal('fetch', fetcher);
  expect(await new LocalHttpRuntimeAdapter().previewPluginImport('/source')).toEqual(result);
  expect(fetcher).toHaveBeenCalledWith('/api/capabilities/plugins/preview-import', expect.objectContaining({
    method: 'POST', body: JSON.stringify({source_path: '/source'}),
  }));
});

it('preserves pending safe-point adoption from a user capability mutation', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    operation: { status: 'CREATED', success: true },
    adoption: { updated_sessions: 0, pending_sessions: 2, attention_sessions: 0 },
    capabilities: { skills: {}, mcp: {}, plugins: {} },
  }), { status: 200, headers: { 'Content-Type': 'application/json' } })));
  const result = await new LocalHttpRuntimeAdapter().createUserMcp({
    serverId: 'example', config: { transport: { type: 'streamable_http', endpoint: 'https://example.com/mcp' } },
    secretChanges: [],
  });
  expect(result.operation.success).toBe(true);
  expect(result.capabilities.adoption).toEqual({
    updatedSessions: 0, pendingSessions: 2, attentionSessions: 0,
  });
});

describe('selectPromptCommand', () => {
  it('joins tool results exactly across interrupted history, reordered results, and paging', async () => {
    const content = (text: string) => ({ kind: 'INLINE', inline_content: btoa(text) });
    const request = (id: string, sequence: number, call: string) => ({
      entry_id: id, turn_id: id === 'old' ? 'turn-old' : 'turn-new', entry_sequence: String(sequence),
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{ block_id: `${id}-block`, block_kind: 'TOOL_CALL', tool_call_id: call, tool_name: 'remember' }],
    });
    const result = (id: string, sequence: number, assistant: string, call: string, state: string) => ({
      entry_id: id, turn_id: 'turn-new', entry_sequence: String(sequence),
      entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT', content: content('plain text is not a success verdict'),
      tool_result: { assistant_entry_id: assistant, tool_call_id: call, result_state: state },
    });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', connection_generation: 1, session_id: 'session-1', role: 'controller',
      live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
      snapshot: { snapshot: {
        session_id: 'session-1', writer_generation: '1', event_sequence_cut: '7',
        entries: [
          request('old', 1, 'old-call'), request('a', 2, 'call-a'), request('b', 3, 'call-b'),
          result('result-b', 4, 'b', 'call-b', 'APPLICATION_ERROR'),
          result('result-a', 5, 'a', 'call-a', 'SUCCESS'),
          result('detached', 6, 'request-outside-page', 'other-call', 'APPLICATION_ERROR'),
        ], control: {},
      } }, live_control_snapshot: { snapshot: {} },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));
    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    const messages = connection.current().messages;
    expect(messages.find(m => m.id === 'old')?.traces?.[0]).toMatchObject({ status: 'cancelled' });
    expect(messages.find(m => m.id === 'old')?.traces?.[0].resultText).toBeUndefined();
    expect(messages.find(m => m.id === 'a')?.traces?.[0]).toMatchObject({ status: 'completed' });
    expect(messages.find(m => m.id === 'b')?.traces?.[0]).toMatchObject({ status: 'failed' });
    expect(messages.find(m => m.id === 'b')?.traces).toHaveLength(1);
    expect(messages.find(m => m.id === 'detached')?.traces?.[0]).toMatchObject({ status: 'failed' });
  });

  it('submits a normal prompt when there is no active turn', () => {
    expect(selectPromptCommand(false, false)).toBe('SUBMIT_PROMPT');
  });

  it('queues a new prompt instead of rewriting an active turn', () => {
    expect(selectPromptCommand(true, false)).toBe('SUBMIT_PROMPT');
  });

  it('uses the explicit steer command for an active turn', () => {
    expect(selectPromptCommand(true, true)).toBe('STEER_ACTIVE_TURN');
  });

  it('keeps an in-flight user steer distinct from an ordinary user turn', async () => {
    const content = (value: string) => ({
      kind: 'INLINE',
      inline_content: btoa(String.fromCharCode(...new TextEncoder().encode(value))),
    });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', connection_generation: 1,
      session_id: 'session-1', role: 'controller',
      live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
          entries: [{
            entry_id: 'prompt-1', turn_id: 'turn-1', entry_sequence: '1',
            entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content: content('完成这项工作'),
          }, {
            entry_id: 'steer-1', turn_id: 'turn-1', entry_sequence: '2',
            entry_kind: 'USER_STEER', scope_kind: 'ROOT', content: content('先检查真实页面'),
          }, {
            entry_id: 'accepted-result-1', turn_id: 'turn-2', entry_sequence: '3',
            entry_kind: 'INTER_AGENT_MESSAGE', scope_kind: 'ROOT',
            source_subagent_task_id: 'task-1',
            content: content('{"status":"accepted"}'),
          }],
          control: {},
        },
      },
      live_control_snapshot: { snapshot: {} },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().messages.map((message) => ({
      body: message.body,
      userKind: message.userKind,
    }))).toEqual([
      { body: '完成这项工作', userKind: 'prompt' },
      { body: '先检查真实页面', userKind: 'steer' },
      { body: '', userKind: 'subagent-completion' },
    ]);
    expect(connection.current().messages[2]?.sourceSubagentTaskId).toBe('task-1');
  });

  it('loads every older history page before exposing a resumed connection', async () => {
    const historyBodies: Array<Record<string, unknown>> = [];
    const entry = (sequence: number, body: string) => ({
      entry_id: `entry-${sequence}`,
      turn_id: `turn-${sequence}`,
      entry_sequence: String(sequence),
      entry_kind: 'USER_MESSAGE',
      content: { kind: 'INLINE_UTF8', inline_content: btoa(body) },
    });
    const responses = [
      {
        connection_id: 'connection-1',
        connection_generation: 1,
        session_id: 'session-1',
        role: 'controller',
        live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
        snapshot: {
          snapshot: {
            session_id: 'session-1',
            writer_generation: '1',
            event_sequence_cut: '0',
            entries: [entry(3, 'latest')],
            older_history_cursor: {
              session_id: 'session-1',
              cut_sequence: '3',
              entry_sequence: '3',
            },
            control: {},
          },
        },
        live_control_snapshot: { snapshot: {} },
      },
      {
        history_page: {
          entries: [entry(2, 'middle')],
          has_more: true,
          older_history_cursor: {
            session_id: 'session-1',
            cut_sequence: '3',
            entry_sequence: '2',
          },
        },
      },
      { history_page: { entries: [entry(1, 'oldest')], has_more: false } },
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/history') && init?.body) {
        historyBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      }
      const payload = responses.shift();
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().messages.map((message) => message.body)).toEqual([
      'oldest',
      'middle',
      'latest',
    ]);
    expect(historyBodies).toHaveLength(2);
    expect(historyBodies[0]).toMatchObject({
      maximum_entries: 256,
      maximum_serialized_bytes: 2 << 20,
      cursor: { entry_sequence: '3' },
    });
    expect(historyBodies[1]).toMatchObject({ cursor: { entry_sequence: '2' } });
  });

  it('treats the manual context action as an explicit forced request', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const responses = [
      {
        connection_id: 'connection-1', connection_generation: 1,
        session_id: 'session-1', role: 'controller',
        live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
        snapshot: {
          snapshot: {
            session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
            entries: [], control: {},
          },
        },
        live_control_snapshot: { snapshot: {} },
      },
      {
        command_outcome: {
          command_id: 'command:web:test', status: 'SUCCEEDED',
          target_id: 'turn-1', public_code: 'COMPACTED',
        },
      },
    ];
    vi.stubGlobal('crypto', { randomUUID: () => 'test' });
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.body) bodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      return new Response(JSON.stringify(responses.shift()), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    await connection.compactContext('turn-1');

    expect(bodies.at(-1)).toMatchObject({
      command_kind: 'COMPACT_CONTEXT', target_turn_id: 'turn-1', force: true,
    });
  });

  it('projects an adopted context boundary from canonical control after observation', async () => {
    const content = (value: string) => ({
      kind: 'INLINE',
      inline_content: btoa(String.fromCharCode(...new TextEncoder().encode(value))),
    });
    const responses = [{
      connection_id: 'connection-1', connection_generation: 1,
      session_id: 'session-1', role: 'controller',
      live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '2',
          entries: [{
            entry_id: 'entry-1', turn_id: 'turn-1', entry_sequence: '1',
            entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content: content('压缩前'),
          }],
          control: {},
        },
      },
      live_control_snapshot: { snapshot: {} },
    }, {
      observation: {
        through_event_sequence: '3',
        committed: [{
          projection_kind: 'CURRENT_CONTROL',
          current_control: {
            latest_context_compaction: {
              turn_id: 'turn-1',
              context_binding_revision_id: 'revision-1',
              source_through_sequence: '1',
              adopted_after_entry_sequence: '1',
              accepted_at_utc: '2026-09-01T10:00:00Z',
            },
          },
        }],
      },
    }];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect(connection.current().contextCompaction).toBeUndefined();
    expect(connection.current().messages[0]?.entrySequence).toBe(1);

    const observed = await connection.observe();

    expect(observed.contextCompaction).toEqual({
      contextBindingRevisionId: 'revision-1',
      turnId: 'turn-1',
      sourceThroughSequence: 1,
      adoptedAfterEntrySequence: 1,
      acceptedAt: '2026-09-01T10:00:00Z',
    });
  });

  it('projects process-local presentation notices from only the current observation', async () => {
    const responses = [{
      connection_id: 'connection-1', connection_generation: 1,
      session_id: 'session-1', role: 'controller',
      live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
          entries: [], control: {},
        },
      },
      live_control_snapshot: { snapshot: {} },
    }, {
      observation: {
        through_event_sequence: '0',
        presentation_notices: [
          '模型已切换。由于上下文长度变化，接下来的回答可能不如之前连贯。',
        ],
      },
    }, {
      observation: { through_event_sequence: '0' },
    }];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect((await connection.observe()).presentationNotices).toEqual([
      '模型已切换。由于上下文长度变化，接下来的回答可能不如之前连贯。',
    ]);
    expect((await connection.observe()).presentationNotices).toEqual([]);
  });

  it('projects provider reasoning and distinguishes full text from a summary', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', connection_generation: 1,
      session_id: 'session-1', role: 'controller',
      live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
          entries: [{
            entry_id: 'assistant-1', turn_id: 'turn-1', entry_sequence: '1',
            entry_kind: 'ASSISTANT_MESSAGE', scope_kind: 'ROOT',
            blocks: [{
              block_id: 'text-1', block_kind: 'TEXT',
              content: { kind: 'INLINE', inline_content: btoa('final answer') },
            }],
            reasoning_blocks: [
              {
                block_id: 'reasoning-1', ordinal: '0',
                presentation_kind: 'REASONING_PRESENTATION_SUMMARY',
                content: { kind: 'INLINE', inline_content: btoa('short provider summary') },
              },
              {
                block_id: 'reasoning-2', ordinal: '1',
                presentation_kind: 'REASONING_PRESENTATION_FULL',
                content: { kind: 'INLINE', inline_content: btoa('full provider reasoning') },
              },
            ],
          }],
          control: {},
        },
      },
      live_control_snapshot: { snapshot: {} },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().messages[0].assistantKind).toBe('terminal');
    expect(connection.current().messages[0].reasoning).toEqual([
      { id: 'reasoning-1', kind: 'summary', body: 'short provider summary' },
      { id: 'reasoning-2', kind: 'full', body: 'full provider reasoning' },
    ]);
  });

  it('keeps the provider presentation kind on a live reasoning stream', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', connection_generation: 1,
      session_id: 'session-1', role: 'controller',
      live_hello: {
        live_owner_epoch: '1', live_revision: '2',
        live_snapshot: {
          events: [
            {
              live_revision: '1', event_type: 'THINKING_START', draft_identity: 'draft-1',
              turn_id: 'turn-1', scope_kind: 'ROOT', block_id: 'reasoning-1',
              payload: { thinking_start: {
                block_identity: 'reasoning-1',
                presentation_kind: 'REASONING_PRESENTATION_SUMMARY',
              } },
            },
            {
              live_revision: '2', event_type: 'THINKING_DELTA', draft_identity: 'draft-1',
              turn_id: 'turn-1', scope_kind: 'ROOT', block_id: 'reasoning-1',
              payload: { thinking_delta: { block_identity: 'reasoning-1', delta: 'live summary' } },
            },
          ],
        },
      },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
          entries: [], control: { active_turns: [{ turn_id: 'turn-1', status: 'RUNNING' }] },
        },
      },
      live_control_snapshot: { snapshot: {} },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().messages[0].assistantKind).toBe('live');
    expect(connection.current().messages[0].reasoning).toEqual([
      { id: 'reasoning-1', kind: 'summary', body: 'live summary', active: true },
    ]);
  });

  it('projects the exact live tool name and terminal command when arguments close', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', connection_generation: 1,
      session_id: 'session-1', role: 'controller',
      live_hello: {
        live_owner_epoch: '1', live_revision: '2',
        live_snapshot: {
          events: [
            {
              live_revision: '1', event_type: 'TOOL_CALL_START', draft_identity: 'draft-1',
              turn_id: 'turn-1', scope_kind: 'ROOT', block_id: 'tool-1',
              payload: { tool_call_start: {
                block_identity: 'tool-1', tool_call_id: 'call-1', tool_name: 'terminal',
              } },
            },
            {
              live_revision: '2', event_type: 'TOOL_CALL_END', draft_identity: 'draft-1',
              turn_id: 'turn-1', scope_kind: 'ROOT', block_id: 'tool-1',
              payload: { tool_call_end: {
                block_identity: 'tool-1', tool_call_id: 'call-1', tool_name: 'terminal',
                arguments_json: JSON.stringify({ command: 'sleep 30' }),
              } },
            },
          ],
        },
      },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
          entries: [], control: { active_turns: [{ turn_id: 'turn-1', status: 'RUNNING' }] },
        },
      },
      live_control_snapshot: { snapshot: {} },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().messages[0].traces?.[0]).toMatchObject({
      toolName: 'terminal', title: '运行命令', subtitle: '等待执行结果', command: 'sleep 30',
    });
  });

  it('projects the root TODO and applies live updates without attaching it to a message', async () => {
    const responses = [
      {
        connection_id: 'connection-1', connection_generation: 1,
        session_id: 'session-1', role: 'controller',
        live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
        snapshot: {
          snapshot: {
            session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
            entries: [], control: { active_turns: [{ turn_id: 'turn-1', scope_kind: 'ROOT', status: 'RUNNING' }] },
          },
        },
        live_control_snapshot: {
          snapshot: {
            owner_epoch: '1', live_revision: '0',
            current_todos: [{
              todo_run_id: 'todo-child', scope_kind: 'SUBAGENT_TASK',
              scope_subagent_task_id: 'task-1', disposition: 'ACTIVE',
              ordered_items: [{ ordinal: 0, text: '子任务步骤', status: 'in_progress' }],
            }, {
              todo_run_id: 'todo-root', scope_kind: 'ROOT', disposition: 'ACTIVE',
              ordered_items: [
                { ordinal: 0, text: '检查契约', status: 'completed' },
                { ordinal: 1, text: '目视验证', status: 'pending' },
              ],
            }],
          },
        },
      },
      {
        observation: {
          through_event_sequence: '0', live_owner_epoch: '1', through_live_revision: '1',
          live: [{
            live_revision: '1', event_type: 'TODO_SNAPSHOT_UPDATED', turn_id: 'turn-1',
            scope_kind: 'ROOT', payload: { todo_snapshot_updated: {
              todo_run_id: 'todo-root', todo_revision: '2', disposition: 'ACTIVE',
              ordered_items: [
                { ordinal: 0, text: '检查契约', status: 'completed' },
                { ordinal: 1, text: '目视验证', status: 'in_progress' },
              ],
              pending_count: 0, in_progress_count: 1, completed_count: 1,
            } },
          }],
        },
      },
      {
        observation: {
          through_event_sequence: '0', live_owner_epoch: '1', through_live_revision: '2',
          live: [{
            live_revision: '2', event_type: 'TODO_SNAPSHOT_UPDATED', turn_id: 'turn-1',
            scope_kind: 'ROOT', payload: { todo_snapshot_updated: {
              todo_run_id: 'todo-root', todo_revision: '3', disposition: 'CLOSED',
              ordered_items: [], pending_count: 0, in_progress_count: 0, completed_count: 0,
            } },
          }],
        },
      },
    ];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().todo).toEqual({
      id: 'todo-root',
      items: [
        { id: 'todo-root:0', label: '检查契约', status: 'completed' },
        { id: 'todo-root:1', label: '目视验证', status: 'pending' },
      ],
    });
    expect(connection.current().messages).toEqual([]);

    const updated = await connection.observe();
    expect(updated.todo?.items.map((item) => item.status)).toEqual(['completed', 'in-progress']);
    expect(updated.messages).toEqual([]);

    const closed = await connection.observe();
    expect(closed.todo).toBeUndefined();
  });

  it('hydrates a large historical reasoning block through the content reader', async () => {
    const reasoning = 'large provider reasoning';
    const digest = `sha256:${'3'.repeat(64)}`;
    const requests: Array<Record<string, unknown>> = [];
    const responses = [
      {
        connection_id: 'connection-1', connection_generation: 1,
        session_id: 'session-1', role: 'controller',
        live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
        snapshot: {
          snapshot: {
            session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
            entries: [{
              entry_id: 'assistant-1', turn_id: 'turn-1', entry_sequence: '1',
              entry_kind: 'ASSISTANT_MESSAGE', scope_kind: 'ROOT',
              reasoning_blocks: [{
                block_id: 'assistant-1:provider-reasoning:0', ordinal: '0',
                presentation_kind: 'REASONING_PRESENTATION_FULL',
                content: {
                  kind: 'CANONICAL_BLOB', digest, size: String(reasoning.length),
                  media_type: 'text/plain', codec: 'utf-8',
                },
              }],
            }],
            control: {},
          },
        },
        live_control_snapshot: { snapshot: {} },
      },
      {
        content: {
          digest, complete_size: String(reasoning.length), offset_bytes: '0',
          content: btoa(reasoning), complete: true,
        },
      },
    ];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.body) requests.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      return new Response(JSON.stringify(responses.shift()), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().messages[0].reasoning?.[0].body).toBe(reasoning);
    expect(requests.at(-1)).toMatchObject({
      entry_id: 'assistant-1', block_id: 'assistant-1:provider-reasoning:0', offset_bytes: 0,
    });
  });

  it('projects successful orchestration results and exposes child execution', async () => {
    const content = (value: string) => ({ kind: 'INLINE', inline_content: btoa(value) });
    const toolRequest = (
      sequence: number,
      entryId: string,
      toolName: string,
      callId: string,
      scope = 'ROOT',
      taskId = '',
      argumentsJson = '{}',
    ) => ({
      entry_id: entryId,
      turn_id: scope === 'ROOT' ? 'turn-root' : `turn-${taskId}`,
      entry_sequence: String(sequence),
      entry_kind: 'ASSISTANT_TOOL_REQUEST',
      scope_kind: scope,
      scope_subagent_task_id: taskId,
      blocks: [{
        block_id: `block-${sequence}`,
        block_kind: 'TOOL_CALL',
        tool_call_id: callId,
        tool_name: toolName,
        tool_arguments_preview: btoa(argumentsJson),
      }],
    });
    const entries = [
      toolRequest(1, 'root-create', 'spawn_agent', 'call-create'),
      {
        entry_id: 'task-objective', turn_id: 'turn-task-1', entry_sequence: '2',
        entry_kind: 'USER_MESSAGE', scope_kind: 'SUBAGENT_TASK',
        scope_subagent_task_id: 'task-1', content: content('Read README.md.'),
      },
      {
        entry_id: 'task-guidance', turn_id: 'turn-task-1', entry_sequence: '3',
        entry_kind: 'INTER_AGENT_MESSAGE', scope_kind: 'SUBAGENT_TASK',
        scope_subagent_task_id: 'task-1', content: content('Also verify the visible heading.'),
      },
      {
        entry_id: 'create-result', turn_id: 'turn-root', entry_sequence: '4',
        tool_result: { assistant_entry_id: 'root-create', tool_call_id: 'call-create', result_state: 'SUCCESS' },
        entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
        content: content(JSON.stringify({ tasks: [{ task_id: 'task-1', status: 'active' }] })),
      },
      toolRequest(5, 'task-read', 'read_file', 'call-read', 'SUBAGENT_TASK', 'task-1'),
      {
        entry_id: 'read-result', turn_id: 'turn-task-1', entry_sequence: '6',
        tool_result: { assistant_entry_id: 'task-read', tool_call_id: 'call-read', result_state: 'SUCCESS' },
        entry_kind: 'TOOL_RESULT', scope_kind: 'SUBAGENT_TASK',
        scope_subagent_task_id: 'task-1',
        content: content(JSON.stringify({ status: 'ok', path: 'README.md', total_lines: 1 })),
      },
      {
        entry_id: 'task-answer', turn_id: 'turn-task-1', entry_sequence: '7',
        entry_kind: 'ASSISTANT_MESSAGE', scope_kind: 'SUBAGENT_TASK',
        scope_subagent_task_id: 'task-1',
        blocks: [{ block_id: 'task-text', block_kind: 'TEXT', content: content('# Pulsara') }],
      },
      toolRequest(8, 'root-wait', 'wait_agent', 'call-wait'),
      {
        entry_id: 'wait-result', turn_id: 'turn-root', entry_sequence: '9',
        tool_result: { assistant_entry_id: 'root-wait', tool_call_id: 'call-wait', result_state: 'SUCCESS' },
        entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
        content: content(JSON.stringify({ pending_task_ids: [], settled: [{ status: 'completed' }] })),
      },
      toolRequest(10, 'root-denied', 'create_agent_tasks', 'call-denied'),
      {
        entry_id: 'denied-result', turn_id: 'turn-root', entry_sequence: '11',
        tool_result: { assistant_entry_id: 'root-denied', tool_call_id: 'call-denied', result_state: 'PERMISSION_DENIED' },
        entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
        content: content('ROOT subagent orchestration requires bypass-permissions mode'),
      },
      toolRequest(
        12,
        'root-user-denied',
        'terminal',
        'call-user-denied',
        'ROOT',
        '',
        JSON.stringify({ command: 'printf "visible command"' }),
      ),
      {
        entry_id: 'user-denied-result', turn_id: 'turn-root', entry_sequence: '13',
        tool_result: { assistant_entry_id: 'root-user-denied', tool_call_id: 'call-user-denied', result_state: 'PERMISSION_DENIED' },
        entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
        content: content('tool execution denied by user'),
      },
      toolRequest(14, 'root-artifact-read', 'artifact_read', 'call-artifact-read'),
      {
        entry_id: 'artifact-read-result', turn_id: 'turn-root', entry_sequence: '15',
        tool_result: { assistant_entry_id: 'root-artifact-read', tool_call_id: 'call-artifact-read', result_state: 'SUCCESS' },
        entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
        content: content(JSON.stringify({ status: 'ok', content: 'retained page' })),
      },
      {
        entry_id: 'orphan-objective', turn_id: 'turn-orphan', entry_sequence: '16',
        entry_kind: 'USER_MESSAGE', scope_kind: 'SUBAGENT_TASK',
        scope_subagent_task_id: 'task-orphan', content: content('Interrupted before a final reply.'),
      },
      toolRequest(17, 'root-orphan-tool', 'wait_agent', 'call-orphan'),
    ];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1',
      connection_generation: 1,
      session_id: 'session-1',
      role: 'controller',
      live_hello: {
        live_owner_epoch: '1', live_revision: '1',
        live_snapshot: {
          events: [{
            live_revision: '1', event_type: 'TEXT_DELTA', draft_identity: 'stale-task-draft',
            turn_id: 'turn-task-1', scope_kind: 'SUBAGENT_TASK',
            scope_subagent_task_id: 'task-1', payload: { text_delta: { delta: 'stale live text' } },
          }],
        },
      },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
          entries,
          control: {
            subagent_tasks: [{
              task_id: 'task-1', parent_turn_id: 'turn-root', status: 'COMPLETED',
              label: '读取标题', display_role: '研究', objective: 'Read README.md.',
              result_summary: '# Pulsara',
            }, {
              task_id: 'task-2', parent_turn_id: 'turn-root', status: 'PENDING_START',
              label: '等待槽位', display_role: '研究', objective: 'Wait for capacity.',
            }],
          },
        },
      },
      live_control_snapshot: { snapshot: {} },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    const projected = connection.current();
    const create = projected.messages.find((message) => message.id === 'root-create');
    const wait = projected.messages.find((message) => message.id === 'root-wait');
    const denied = projected.messages.find((message) => message.id === 'root-denied');
    const userDenied = projected.messages.find((message) => message.id === 'root-user-denied');
    const artifactRead = projected.messages.find((message) => message.id === 'root-artifact-read');
    const orphanTool = projected.messages.find((message) => message.id === 'root-orphan-tool');

    expect(create?.assistantKind).toBe('tool-request');
    expect(create?.traces?.[0]).toMatchObject({ status: 'completed', meta: '操作完成' });
    expect(create?.traces?.[0].toolName).toBe('spawn_agent');
    expect(wait?.traces?.[0]).toMatchObject({ status: 'completed', meta: '操作完成' });
    expect(denied?.traces?.[0]).toMatchObject({ status: 'failed', subtitle: '已拒绝' });
    expect(userDenied?.traces?.[0]).toMatchObject({
      toolName: 'terminal', command: 'printf "visible command"',
      argumentsJson: JSON.stringify({ command: 'printf "visible command"' }),
      resultText: 'tool execution denied by user',
      status: 'failed', subtitle: '已拒绝', meta: '操作未完成',
    });
    expect(artifactRead?.traces?.[0]).toMatchObject({
      title: '读取保留内容', status: 'completed', meta: '操作完成',
    });
    expect(create?.subagentRuns?.find((run) => run.id === 'task-orphan')).toBeUndefined();
    expect(orphanTool?.traces?.[0]).toMatchObject({
      status: 'cancelled', subtitle: '已结束', meta: '操作未完成',
    });
    expect(orphanTool?.subagentRuns).toBeUndefined();
    expect(create?.subagentRuns?.[0]).toMatchObject({
      id: 'task-1', label: '读取标题', status: 'completed', objective: 'Read README.md.',
    });
    expect(create?.subagentRuns?.[0].activities).toHaveLength(3);
    expect(create?.subagentRuns?.[0].activities[0]).toMatchObject({
      kind: 'guidance', body: 'Also verify the visible heading.',
    });
    expect(create?.subagentRuns?.find((run) => run.id === 'task-2')).toMatchObject({
      label: '等待槽位', status: 'pending', activities: [],
    });
    expect(create?.subagentRuns?.[0].activities.find((activity) => activity.traces?.length)?.traces?.[0]).toMatchObject({
      title: '读取文件', status: 'completed', meta: '操作完成',
    });
    expect(projected.isRunning).toBe(false);
    expect(JSON.stringify(create?.subagentRuns)).not.toContain('stale live text');
  });

  it('keeps exact MCP meta-tool arguments and results for structured UI projection', async () => {
    const argumentsJson = JSON.stringify({ server_id: 'firecrawl' });
    const resultText = JSON.stringify({
      total_server_count: 1,
      servers: [{ server_id: 'firecrawl', public_status: 'READY', tool_count: 3 }],
    });
    const content = (value: string) => ({ kind: 'INLINE', inline_content: btoa(value) });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', connection_generation: 1,
      session_id: 'session-1', role: 'controller',
      live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '2',
          entries: [{
            entry_id: 'assistant-mcp-list', turn_id: 'turn-1', entry_sequence: '1',
            entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
            blocks: [{
              block_id: 'block-mcp-list', block_kind: 'TOOL_CALL',
              tool_call_id: 'call-mcp-list', tool_name: 'list_mcp_servers',
              tool_arguments_preview: btoa(argumentsJson),
            }],
          }, {
            entry_id: 'result-mcp-list', turn_id: 'turn-1', entry_sequence: '2',
            tool_result: { assistant_entry_id: 'assistant-mcp-list', tool_call_id: 'call-mcp-list', result_state: 'SUCCESS' },
            entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT', content: content(resultText),
          }],
          control: {
            tool_attempts: [{
              assistant_entry_id: 'assistant-mcp-list', tool_call_id: 'call-mcp-list',
              result_entry_id: 'result-mcp-list', result_state: 'SUCCESS',
            }],
          },
        },
      },
      live_control_snapshot: { snapshot: {} },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect(connection.current().messages[0]?.traces?.[0]).toMatchObject({
      toolName: 'list_mcp_servers',
      title: '浏览 MCP 服务',
      argumentsJson,
      resultText,
      status: 'completed',
    });
  });

  it('ignores a stale child live draft after the task leaves active control', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', connection_generation: 1,
      session_id: 'session-1', role: 'controller',
      live_hello: {
        live_owner_epoch: '1', live_revision: '1',
        live_snapshot: {
          events: [{
            live_revision: '1', event_type: 'TOOL_RESULT_START',
            draft_identity: 'stale-child-tool-result',
            turn_id: 'turn-task-1', scope_kind: 'SUBAGENT_TASK',
            scope_subagent_task_id: 'task-1',
            payload: { tool_result_start: { tool_call_id: 'call-1' } },
          }],
        },
      },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
          entries: [], control: {},
        },
      },
      live_control_snapshot: { snapshot: {} },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().isRunning).toBe(false);
    expect(connection.current().messages).toEqual([]);
  });
});

describe('LocalHttpRuntimeAdapter connection ownership', () => {
  it('sends the stable page identity with an explicit takeover request', async () => {
    let requestBody: unknown;
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      requestBody = init?.body ? JSON.parse(String(init.body)) : undefined;
      return new Response(JSON.stringify({
        connection_id: 'connection-takeover', connection_generation: 2,
        session_id: 'session-1', role: 'controller',
        live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
        snapshot: {
          snapshot: {
            session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
            entries: [], control: {},
          },
        },
        live_control_snapshot: { snapshot: {} },
      }), { status: 201, headers: { 'Content-Type': 'application/json' } });
    }));

    const connection = await new LocalHttpRuntimeAdapter(
      '00000000-0000-4000-8000-000000000001',
    ).connect('session-1', true);

    expect(requestBody).toEqual({
      browser_instance_id: '00000000-0000-4000-8000-000000000001',
      takeover: true,
    });
    expect(connection.role).toBe('controller');
  });
});

describe('subagent completion continuation', () => {
  it('carries the selected permission into the new root turn command', async () => {
    let commandBody: Record<string, unknown> | undefined;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/command')) {
        commandBody = JSON.parse(String(init?.body));
        return new Response(JSON.stringify({
          command_outcome: {
            command_id: commandBody?.command_id,
            status: 'SUCCEEDED',
            public_code: 'SUBAGENT_COMPLETION_DELIVERED',
          },
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify({
        connection_id: 'connection-result', connection_generation: 3,
        session_id: 'session-1', role: 'controller',
        live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
        snapshot: { snapshot: { session_id: 'session-1', writer_generation: '1', entries: [], control: {} } },
        live_control_snapshot: { snapshot: {} },
      }), { status: 201, headers: { 'Content-Type': 'application/json' } });
    }));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    await connection.acceptSubagentCompletion('task-1', 'read-only');

    expect(commandBody).toMatchObject({
      command_kind: 'ACCEPT_SUBAGENT_COMPLETION',
      subagent_task_id: 'task-1',
      requested_permission_mode: 'PERMISSION_MODE_READ_ONLY',
    });
  });
});

describe('session summaries', () => {
  it('keeps process-loaded state structured for the sidebar', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      sessions: [{
        id: 'session-loaded',
        title: '会话 loaded',
        subtitle: '13 条记录',
        lifecycle: 'OPEN',
        updated_at: '2026-08-30T11:00:00Z',
        live: true,
        task_counts: { total: 5, active: 1, waiting: 2, attention: 1 },
      }, {
        id: 'session-resumable',
        title: '会话 resumable',
        subtitle: '8 条记录',
        lifecycle: 'OPEN',
        updated_at: '2026-08-30T10:00:00Z',
        live: false,
      }],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const sessions = await new LocalHttpRuntimeAdapter().listSessions();

    expect(sessions.map((session) => ({
      id: session.id,
      subtitle: session.subtitle,
      live: session.live,
      taskCounts: session.taskCounts,
    }))).toEqual([
      {
        id: 'session-loaded', subtitle: '13 条记录', live: true,
        taskCounts: { total: 5, active: 1, waiting: 2, attention: 1 },
      },
      { id: 'session-resumable', subtitle: '8 条记录', live: false, taskCounts: undefined },
    ]);
  });
});

describe('session task inventory', () => {
  it('projects the durable page with every state, result field, and dependency', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => {
      void _input;
      return new Response(JSON.stringify({
      session_id: 'session-1',
      tasks: [{
        id: 'task-blocked', parent_turn_id: 'turn-1', batch_id: 'batch-1',
        task_key: 'verify', label: '验证结果', profile: 'verification_worker',
        display_role: '验证', context: { mode: 'LAST_N', last_n_turns: 4 },
        objective: '确认结果可以使用。', status: 'BLOCKED_DEPENDENCY_FAILED',
        terminal_public_detail: '前置任务未能完成。', completion_delivered: true,
        accepted_at: '2026-08-30T12:00:00Z', terminal_at: '2026-08-30T12:01:00Z',
        dependencies: [{
          task_id: 'task-failed', task_key: 'build', label: '生成结果',
          status: 'FAILED', result_summary: null,
        }],
        result: {
          id: 'result-1', entry_id: 'entry-1', source: 'EXPLICIT',
          summary: '保留的总结', output_preview: '输出摘录',
          diagnostics: [{ message: '前置检查失败' }],
        },
      }],
      total_count: 51, remaining_count: 50, next_cursor: 'page-2',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    vi.stubGlobal('fetch', fetchMock);

    const page = await new LocalHttpRuntimeAdapter().listSessionTasks('session-1');

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/sessions/session-1/tasks?limit=50');
    expect(page).toMatchObject({ totalCount: 51, remainingCount: 50, nextCursor: 'page-2' });
    expect(page.tasks[0]).toMatchObject({
      id: 'task-blocked', status: 'blocked', role: '验证',
      terminalPublicDetail: '前置任务未能完成。', completionDelivered: true,
      context: { mode: 'last-n', lastNTurns: 4 },
      dependencyIds: ['task-failed'],
      dependencies: [{ id: 'task-failed', status: 'failed', label: '生成结果' }],
      result: {
        id: 'result-1', entryId: 'entry-1', summary: '保留的总结',
        outputPreview: '输出摘录',
      },
    });
  });

  it('uses durable terminal truth over stale live progress while preserving live active progress', () => {
    const durable: AgentTask[] = [{
      id: 'task-1', label: '持久任务', role: '研究', objective: '完成检查',
      status: 'interrupted', dependencyIds: [], color: 'blue',
      completionDelivered: false,
      summary: '中断前的最后结果',
    }];
    const projection: RuntimeProjection = {
      messages: [], isRunning: true, queuedCount: 0, planMode: false,
      control: {}, liveControl: {}, todo: undefined,
      agentTasks: [{
        ...durable[0], status: 'running', progress: '仍在运行', summary: undefined,
      }],
      eventSequence: 1, liveOwnerEpoch: 1, liveRevision: 1,
      liveControlOwnerEpoch: 1, liveControlRevision: 1,
    };

    const merged = mergeRuntimeTaskInventory(projection, durable);

    expect(merged.agentTasks[0]).toMatchObject({
      status: 'interrupted', progress: undefined, summary: '中断前的最后结果',
    });
    expect(merged.messages).toEqual([]);
  });
});

describe('capability catalog adapter', () => {
  it('projects Skill activation facts and the complete ready MCP tool detail', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      session_id: 'session-1',
      workspace_path: '/tmp/project',
      skills: {
        status: 'ready',
        items: [{
          name: 'pdf', description: '处理 PDF', location: 'bundled_skills/pdf',
          source: 'bundled', configured: false, authoring_notes: [],
        }],
        issues: [], details: [], roots: [],
      },
      mcp: {
        servers: [{
          id: 'docs', name: '文档', status: 'READY', required: false,
          available_to_subagents: true, tool_count: 1, discovered_tool_count: 1,
          resource_count: 2, resource_template_count: 3, prompt_count: 4,
          instructions: '优先读取目录', has_failure: false,
          tools: [{
            name: 'docs_search', remote_name: 'search', description: '搜索文档',
            effect: 'READ_ONLY', available_to_subagents: true, parallel_safe: true,
          }],
        }],
        collisions: [],
      },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const capabilities = await new LocalHttpRuntimeAdapter().inspectCapabilities('session-1');

    expect(capabilities.skills.items[0]).toMatchObject({ name: 'pdf', source: 'bundled' });
    expect(capabilities.mcp.servers[0]).toMatchObject({
      id: 'docs', status: 'ready', availableToSubagents: true,
      resourceCount: 2, resourceTemplateCount: 3, promptCount: 4,
    });
    expect(capabilities.mcp.servers[0].tools[0]).toMatchObject({
      name: 'docs_search', remoteName: 'search', effect: 'read-only',
      availableToSubagents: true, parallelSafe: true,
    });
  });

  it('writes a path-based user Skill switch and projects its effective state', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      void input;
      void init;
      return new Response(JSON.stringify({
        operation: { status: 'DISABLED', success: true, message: '技能已关闭。' },
        capabilities: {
          roots: [
            { kind: 'agents', path: '/Users/test/.agents' },
            { kind: 'pulsara', path: '/Users/test/.pulsara' },
          ],
          skills: {
            status: 'ready',
            config_path: '/Users/test/.pulsara/skills.yaml',
            items: [{
              name: 'review', description: '审阅实现', location: '~/.agents/skills/review/SKILL.md',
              path: '/Users/test/.agents/skills/review/SKILL.md', enabled: false,
              root: 'agents', authoring_notes: [],
            }],
            issues: [], details: [], roots: [],
          },
          mcp: { config_path: '/Users/test/.pulsara/mcp.yaml', servers: [] },
          plugins: { status: 'ready', items: [], details: [] },
        },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await new LocalHttpRuntimeAdapter().setUserSkillEnabled(
      '/Users/test/.agents/skills/review/SKILL.md',
      false,
      'session-1',
    );

    expect(result.capabilities.skills).toMatchObject({
      configPath: '/Users/test/.pulsara/skills.yaml',
      items: [{ name: 'review', enabled: false }],
    });
    const [, init] = fetchMock.mock.calls[0]!;
    expect(JSON.parse(String(init?.body))).toEqual({
      path: '/Users/test/.agents/skills/review/SKILL.md',
      enabled: false,
      active_session_id: 'session-1',
    });
  });
});

describe('productVisibleText', () => {
  it('translates local runtime vocabulary without rewriting ordinary prose', () => {
    expect(productVisibleText(
      'ROOT subagent orchestration requires bypass-permissions mode; create_agent_tasks was rejected.',
    )).toBe('创建子任务需要在本轮选择“完全访问”权限; 创建子任务 已被拒绝.');
    expect(productVisibleText('The Linux kernel uses canonical paths.')).toBe(
      'The Linux kernel uses canonical paths.',
    );
    expect(productVisibleText('使用 read_file 和 terminal 工具。')).toBe(
      '使用 读取文件 和 终端。',
    );
  });
});
