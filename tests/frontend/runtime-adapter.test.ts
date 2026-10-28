import type { PulsaraDesktop } from '../../frontend/lib/desktop-host';
import { webcrypto } from 'node:crypto';
import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import {
  createUserControlCommandRef,
  LocalHttpRuntimeAdapter,
  CanonicalHistoryView,
  mergeRuntimeTaskInventory,
  projectAgentTaskConversation,
  type RuntimeProjection,
} from '../../frontend/lib/runtime-adapter';
import type { AgentTask } from '../../frontend/lib/pulsara-types';

const connectedWorkspaceAvailability = {path: '/tmp/project', outcome: 'AVAILABLE' as const, reason: null};
let historyOwnerFixture: Record<string, unknown> = {};
afterEach(() => { delete window.pulsaraDesktop; vi.unstubAllGlobals(); historyOwnerFixture = {}; });

// Page-context requests are an independent owner read, not another frame in
// a command/content fixture's ordered response queue. Capture the fixture's
// canonical metadata and return its explicit per-entry owner context here.
function stubRuntimeFetch(fetcher: typeof fetch) {
  const entries = new Map<string, Record<string, unknown>>();
  const tasks = new Map<string, Record<string, unknown>>();
  const collect = (value: unknown) => {
    if (!value || typeof value !== 'object') return;
    if (Array.isArray(value)) { value.forEach(collect); return; }
    const item = value as Record<string, unknown>;
    if (typeof item.entry_id === 'string' && item.entry_kind) entries.set(item.entry_id, item);
    if (typeof item.task_id === 'string' && typeof item.parent_turn_id === 'string') tasks.set(item.task_id, item);
    Object.values(item).forEach(collect);
  };
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path.endsWith('/history/context')) {
      const request = JSON.parse(String(init?.body));
      const ids = request.entry_ids as string[];
      const selected = ids.map(id => entries.get(id) ?? {entry_id: id, turn_id: id});
      const owners = [...new Set(selected.map(entry => String(entry.turn_id ?? entry.entry_id)))];
      return new Response(JSON.stringify({
        session_id: decodeURIComponent(path.split('/sessions/')[1].split('/')[0]),
        cut_sequence: String(request.cut_sequence ?? '1000'), event_sequence_cut: String(request.event_sequence_cut ?? '1000'),
        entry_ids: ids,
        groups: owners.map((id, index) => ({owner_kind:'EXECUTED_TURN',owner_id:id,previous_root_owner_id:owners[index - 1],initial_entry_id:String(selected.find(entry => String(entry.turn_id ?? entry.entry_id) === id)!.entry_id)})),
        entry_contexts: selected.map(entry => ({entry_id:entry.entry_id,owner_kind:'EXECUTED_TURN',owner_id:entry.turn_id ?? entry.entry_id,
          root_owner_id:entry.turn_id ?? entry.entry_id,segment_id:entry.turn_id ?? entry.entry_id,segment_start_entry_id:entry.entry_id})),
        tasks: [...tasks.values()].map(task => ({task_id:task.task_id,parent_turn_id:task.parent_turn_id,parent_initial_entry_id:task.parent_turn_id,batch_id:task.batch_id})), batches: [], interruption_notices: [], compactions: [],
        ...historyOwnerFixture,
      }), {status:200});
    }
    const response = await fetcher(input, init);
    try { collect(await response.clone().json()); } catch { /* Error fixtures retain their actual response. */ }
    return response;
  });
}


it('conditionally deletes a cold archived session and retains cleanup warnings', async () => {
  const payload = {status: 'NOT_ARCHIVED', session_id: 'session:cold/a', cleanup_warning: {code: 'SESSION_DELETE_QUARANTINED', message: '请重启 Pulsara'}};
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {status: 200}));
  stubRuntimeFetch(fetchMock);
  expect(await new LocalHttpRuntimeAdapter().deleteArchivedSession('session:cold/a')).toEqual(payload);
  expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session%3Acold%2Fa/archived', expect.objectContaining({
    method: 'DELETE', body: JSON.stringify({confirm_permanent_delete: true}), credentials: 'same-origin',
  }));
  expect(fetchMock.mock.calls[0][1].headers).not.toHaveProperty('X-Pulsara-Connection-Id');
});

it('reads a disposable context preview with cancellation and an encoded session identity', async () => {
  const payload = { state: 'ready', connection_id: 'selected-model', input_tokens: 1000, input_budget_tokens: 2000 };
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }));
  stubRuntimeFetch(fetchMock);
  const controller = new AbortController();
  expect(await new LocalHttpRuntimeAdapter().contextUsage('session:usage/a', controller.signal)).toEqual(payload);
  expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session%3Ausage%2Fa/context-usage', expect.objectContaining({ signal: controller.signal }));
});

it('bootstraps the app without assigning a working directory', async () => {
  const payload = {
    application: { name: 'Pulsara', version: '0.1.0', transport: 'localhost-http+terminal-v3' },
    protocol: { major: 3, minor: 0 },
    runtime: { status: 'ready', origin: 'http://127.0.0.1:8765', database_state: 'ready' },
    local_settings: {}, model_configurations: [],
  };
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }));
  stubRuntimeFetch(fetchMock);
  const bootstrap = await new LocalHttpRuntimeAdapter().bootstrap();
  expect(bootstrap).toEqual(payload);
  expect(bootstrap).not.toHaveProperty('workspace');
  expect(fetchMock).toHaveBeenCalledWith('/api/app/bootstrap', expect.any(Object));
});

it('keeps the macOS native directory picker and distinguishes cancellation without Linux directory requests', async () => {
  const fetchMock = vi.fn();
  const selected = "/Users/pulsara/中文 项目's ";
  const chooseDirectory = vi.fn().mockResolvedValueOnce(selected).mockResolvedValueOnce(null);
  window.pulsaraDesktop = { platform: 'darwin', chooseDirectory } as unknown as PulsaraDesktop;
  stubRuntimeFetch(fetchMock);
  const adapter = new LocalHttpRuntimeAdapter();
  const controller = new AbortController();
  expect(await adapter.pickWorkspaceDirectory(controller.signal)).toBe(selected);
  expect(chooseDirectory).toHaveBeenCalledExactlyOnceWith();
  expect(await adapter.pickWorkspaceDirectory()).toBeNull();
  expect(fetchMock).not.toHaveBeenCalled();
});

it('Windows native browse flows through the user-level directory API before final selection', async () => {
  const root = '/home/user', chosen = `${root}/中文 项目`;
  const chooseWorkspaceDirectory = vi.fn(async () => ({path:'\\\\wsl.localhost\\Ubuntu\\home\\user\\中文 项目',linuxPath:chosen}));
  window.pulsaraDesktop = {platform:'win32',chooseWorkspaceDirectory} as unknown as PulsaraDesktop;
  const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype,'showModal');
  Object.defineProperty(HTMLDialogElement.prototype,'showModal',{configurable:true,value:function (this: HTMLDialogElement) {this.open = true;}});
  const controller = new AbortController();
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input),'http://localhost');
    if (url.pathname.endsWith('/select')) return new Response(String(init?.body),{status:200});
    const path = url.searchParams.get('path') ?? root;
    return new Response(JSON.stringify({root,path,parent:path===root?null:root,entries:[],next_cursor:null}),{status:200});
  });
  stubRuntimeFetch(fetchMock);
  let picked!: Promise<string | null>;
  try {
    await act(async () => {picked = new LocalHttpRuntimeAdapter().pickWorkspaceDirectory(controller.signal);});
    fireEvent.click(await screen.findByRole('button',{name:'浏览…'}));
    await waitFor(() => expect(screen.getByRole('status',{name:'所选文件夹'})).toHaveProperty('value',chosen));
    expect(chooseWorkspaceDirectory).toHaveBeenCalledExactlyOnceWith();
    expect(fetchMock.mock.calls.map(([url])=>String(url))).toContain('/api/workspace-directories?path=%2Fhome%2Fuser%2F%E4%B8%AD%E6%96%87+%E9%A1%B9%E7%9B%AE');
    await act(async () => {fireEvent.click(screen.getByRole('button',{name:'选择文件夹'}));});
    expect(await picked).toBe(chosen);
    expect(fetchMock).toHaveBeenLastCalledWith('/api/workspace-directories/select',expect.objectContaining({method:'POST',body:JSON.stringify({path:chosen})}));
  } finally {
    await act(async () => {controller.abort();});
    if (originalShowModal) Object.defineProperty(HTMLDialogElement.prototype,'showModal',originalShowModal);
    else Reflect.deleteProperty(HTMLDialogElement.prototype,'showModal');
  }
});

it('does not open the macOS picker when its selection owner was already cancelled', async () => {
  const fetchMock = vi.fn();
  const chooseDirectory = vi.fn();
  window.pulsaraDesktop = { platform: 'darwin', chooseDirectory } as unknown as PulsaraDesktop;
  stubRuntimeFetch(fetchMock);
  const controller = new AbortController();
  controller.abort();
  await expect(new LocalHttpRuntimeAdapter().pickWorkspaceDirectory(controller.signal))
    .rejects.toMatchObject({ name: 'AbortError' });
  expect(chooseDirectory).not.toHaveBeenCalled();
  expect(fetchMock).not.toHaveBeenCalled();
});

it('discards a late macOS native picker result after its selection owner is cancelled', async () => {
  const fetchMock = vi.fn();
  let complete!: (path: string) => void;
  const chooseDirectory = vi.fn(() => new Promise<string>(resolve => { complete = resolve; }));
  window.pulsaraDesktop = { platform: 'darwin', chooseDirectory } as unknown as PulsaraDesktop;
  stubRuntimeFetch(fetchMock);
  const controller = new AbortController();
  const selection = new LocalHttpRuntimeAdapter().pickWorkspaceDirectory(controller.signal);
  expect(chooseDirectory).toHaveBeenCalledExactlyOnceWith();
  controller.abort();
  complete('/Users/pulsara/late 中文 workspace');
  await expect(selection).rejects.toMatchObject({ name: 'AbortError' });
  expect(fetchMock).not.toHaveBeenCalled();
});

const SOURCE_FIDELITY_TEXT = [
  '',
  '中文与 `read_file`、edit_file、ROOT、Kernel、HostSession 保持原样。',
  'path=/tmp/ROOT/edit_file.py url=https://example.test/Kernel?q=read-only',
  '```python',
  'from api import read_file, edit_file',
  'ROOT = "read-only"',
  'mode = "bypass-permissions"',
  'exit_code = 0',
  '```',
  '{"tool":"edit_file","owner":"HostSession","status":"read-only"}',
  '',
].join('\n');

function inventoryTask(id: string, label: string, status: AgentTask['status'], parentId: string, objective = ''): AgentTask {
  return {
    id, label, role: '通用协作', objective, status, parentId,
    dependencyIds: [], completionAccepted: status === 'completed', color: 'blue',
  };
}

function inlineContent(value: string) {
  const bytes = new TextEncoder().encode(value);
  return inlineBytes(bytes);
}

function promptBody(...texts: string[]): string {
  return JSON.stringify({ schema: 'pulsara.prompt/v2', parts: texts.map((text) => ({ type: 'text', text })) });
}

function inlinePrompt(...texts: string[]) {
  return { ...inlineContent(promptBody(...texts)), media_type: 'application/vnd.pulsara.prompt+json', codec: 'utf-8' };
}

function textPromptContent(text: string) {
  return { parts: [{ type: 'text', text }] };
}

function blobPrompt(text: string) {
  return { ...blobContent(promptBody(text)), media_type: 'application/vnd.pulsara.prompt+json' };
}

function inlineBytes(bytes: Uint8Array) {
  const pieces: string[] = [];
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    pieces.push(String.fromCharCode(...bytes.subarray(offset, offset + 0x8000)));
  }
  return {
    kind: 'INLINE',
    inline_content: btoa(pieces.join('')),
  };
}

function sha256Digest(bytes: Uint8Array): string {
  return `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
}

function blobBytes(bytes: Uint8Array) {
  return {
    kind: 'CANONICAL_BLOB',
    digest: sha256Digest(bytes),
    size: String(bytes.length),
    media_type: 'text/plain',
    codec: 'utf-8',
  };
}

function blobContent(value: string) {
  return blobBytes(new TextEncoder().encode(value));
}

function contentBytes(bytes: Uint8Array, digest = sha256Digest(bytes)) {
  return {
    content: {
      digest,
      complete_size: String(bytes.length),
      offset_bytes: '0',
      content: inlineBytes(bytes).inline_content,
      complete: true,
    },
  };
}

function contentChunk(value: string) {
  return contentBytes(new TextEncoder().encode(value));
}

function connectPayload(
  entries: Record<string, unknown>[] = [],
  control: Record<string, unknown> = {},
  liveEvents: Record<string, unknown>[] = [],
  liveControl: Record<string, unknown> = {},
) {
  return {
    workspace_availability: connectedWorkspaceAvailability,
    connection_id: 'connection-fidelity', connection_generation: 1,
    session_id: 'session-1', role: 'controller',
    live_hello: {
      live_owner_epoch: '1', live_revision: String(liveEvents.length),
      live_snapshot: { events: liveEvents },
    },
    snapshot: { snapshot: {
      session_id: 'session-1', writer_generation: '1', event_sequence_cut: String(entries.length),
      entries, control,
    } },
    live_control_snapshot: { snapshot: liveControl },
  };
}

it('carries the connected workspace observation without another workspace request', async () => {
  const payload = connectPayload();
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {status: 201}));
  stubRuntimeFetch(fetchMock);
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  expect(connection.availability).toEqual(payload.workspace_availability);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions/session-1/connections');
});

it('preserves PR03 exact control/query identities and typed query retirement states', async () => {
  vi.stubGlobal('crypto', { getRandomValues: webcrypto.getRandomValues.bind(webcrypto),
    ...globalThis.crypto,
    randomUUID: () => '66666666-6666-4666-8666-666666666666',
  });
  const responses = [
    connectPayload([], {}, [], {
      host_session_id: 'host:one',
      active_root_turn_id: 'turn:A',
      control_admission_deadline_ms: '9000',
    }),
    { command_outcome: {
      command_id: 'command:control:9000:66666666-6666-4666-8666-666666666666',
      status: 'PENDING',
      target_id: 'turn:A',
      public_code: 'CONTROL_ACCEPTED',
      user_control: {
        accepted: true,
        execution: 'USER_CONTROL_RUNNING',
        target: { kind: 'USER_CONTROL_ROOT_TURN', target_id: 'turn:A' },
      },
    } },
    { query_command: {
      found: false,
      control_query_status: 'CONTROL_QUERY_RESULT_UNAVAILABLE',
    } },
    { query_command: {
      found: false,
      control_query_status: 'CONTROL_QUERY_OWNER_UNAVAILABLE',
    } },
    { query_command: {
      found: true,
      control_query_status: 'CONTROL_QUERY_FOUND',
      outcome: {
        command_id: 'command:control:9000:66666666-6666-4666-8666-666666666666',
        status: 'SUCCEEDED',
        target_id: 'turn:A',
        user_control: {
          accepted: true,
          execution: 'USER_CONTROL_FINISHED',
        },
      },
    } },
  ];
  const requests: Array<{ operation: string; body: Record<string, unknown> }> = [];
  stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const operation = String(input).split('/').at(-1) ?? '';
    if (operation !== 'connections') {
      requests.push({
        operation,
        body: JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>,
      });
    }
    return new Response(JSON.stringify(responses.shift()), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }));

  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  const projection = connection.current();
  const reference = createUserControlCommandRef(
    projection, 'session-1', 'STOP_ACTIVE_TURN', 'ROOT_TURN', 'turn:A',
  );
  const accepted = await connection.stopActiveTurn(reference);
  expect(accepted).toMatchObject({
    status: 'pending',
    targetId: 'turn:A',
    userControl: { accepted: true, execution: 'RUNNING' },
  });
  expect(await connection.queryControlCommand(reference)).toEqual({
    status: 'RESULT_UNAVAILABLE',
  });
  expect(await connection.queryControlCommand(reference)).toEqual({
    status: 'OWNER_UNAVAILABLE',
  });
  expect(await connection.queryControlCommand(reference)).toMatchObject({
    status: 'FOUND',
    receipt: { status: 'succeeded', userControl: { execution: 'FINISHED' } },
  });
  expect(requests[0]).toEqual({
    operation: 'command',
    body: {
      command_id: reference.commandId,
      command_kind: 'STOP_ACTIVE_TURN',
      control_admission_deadline_ms: 9000,
      client_submission_id: '',
      expected_session_id: 'session-1',
      expected_host_session_id: 'host:one',
      target_turn_id: 'turn:A',
    },
  });
  expect(requests.slice(1).every((request) => (
    request.operation === 'query-command'
    && (request.body.expected_control as Record<string, unknown>).target_id === 'turn:A'
  ))).toBe(true);
});

it('binds PR03 background list and log reads to the exact Host and process', async () => {
  const responses = [
    connectPayload([], {}, [], { host_session_id: 'host:one' }),
    { background_processes: {
      session_id: 'session-1',
      host_session_id: 'host:one',
      processes: [{
        process_id: 'process:one', command: 'printf exact', cwd: '/tmp',
        status: 'running', physical_state: 'RUNNING', output_cursor: 'cursor:1',
        retained_from_cursor: 'cursor:0', origin: { turn_id: 'turn:A' },
      }],
      next_cursor: 'page:next',
    } },
    { background_process_log: {
      session_id: 'session-1', host_session_id: 'host:one',
      process: {
        process_id: 'process:one', command: 'printf exact', cwd: '/tmp',
        status: 'running', physical_state: 'RUNNING', output_cursor: 'cursor:2',
        retained_from_cursor: 'cursor:0', origin: { turn_id: 'turn:A' },
      },
      output: '逐字输出🙂', output_cursor: 'cursor:2', retained_from_cursor: 'cursor:0',
      gap_before_output: false, truncated_by_response_bound: false,
      source_coverage: 'COMPLETE',
    } },
  ];
  const calls: Array<{ operation: string; body: Record<string, unknown> }> = [];
  stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const operation = String(input).split('/').at(-1) ?? '';
    if (operation !== 'connections') calls.push({
      operation,
      body: JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>,
    });
    return new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }));

  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  expect(await connection.listBackgroundProcesses('page:old')).toMatchObject({
    processes: [{ processId: 'process:one', command: 'printf exact' }],
    nextCursor: 'page:next',
  });
  expect(await connection.readBackgroundProcessLog('process:one', 'cursor:1'))
    .toMatchObject({ output: '逐字输出🙂', outputCursor: 'cursor:2' });
  expect(calls).toEqual([
    {
      operation: 'list-background-processes',
      body: {
        expected_session_id: 'session-1', expected_host_session_id: 'host:one',
        cursor: 'page:old', maximum_items: 50,
      },
    },
    {
      operation: 'read-background-process-log',
      body: {
        expected_session_id: 'session-1', expected_host_session_id: 'host:one',
        process_id: 'process:one', output_cursor: 'cursor:1', max_output_chars: 32000,
      },
    },
  ]);
});

it('requests Plugin discovery using only the source directory', async () => {
  const result = {candidates: []};
  const fetcher = vi.fn(async () => new Response(JSON.stringify(result), {status: 200}));
  stubRuntimeFetch(fetcher);
  expect(await new LocalHttpRuntimeAdapter().previewPluginImport('/source')).toEqual(result);
  expect(fetcher).toHaveBeenCalledWith('/api/capabilities/plugins/preview-import', expect.objectContaining({
    method: 'POST', body: JSON.stringify({source_path: '/source'}),
  }));
});

it('reads the device capability overview without a selected conversation', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({
    skills: {}, mcp: {}, plugins: {},
  }), { status: 200 }));
  stubRuntimeFetch(fetcher);
  const adapter = new LocalHttpRuntimeAdapter();
  await adapter.inspectUserCapabilities();
  expect(fetcher).toHaveBeenLastCalledWith('/api/capabilities', expect.anything());
  await adapter.refreshUserCapabilities();
  expect(fetcher).toHaveBeenLastCalledWith('/api/capabilities/refresh', expect.objectContaining({
    method: 'POST', body: '{}',
  }));
});

it('passes user overview cancellation to fetch without a session connection header', async () => {
  const controller = new AbortController();
  const fetcher = vi.fn<typeof fetch>((_input, init) => new Promise((_resolve, reject) => {
    init!.signal!.addEventListener('abort', () => reject(init!.signal!.reason), {once: true});
  }));
  stubRuntimeFetch(fetcher);
  const read = new LocalHttpRuntimeAdapter().inspectUserCapabilities(controller.signal);
  expect(fetcher).toHaveBeenCalledWith('/api/capabilities', expect.objectContaining({signal: controller.signal}));
  expect(fetcher.mock.calls[0][1]?.headers).toBeUndefined();
  const aborted = expect(read).rejects.toThrow('cancel user overview');
  controller.abort(new DOMException('cancel user overview', 'AbortError'));
  await aborted;
});

it('preserves pending safe-point adoption from a user capability mutation', async () => {
  stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
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

describe('exact prompt projection', () => {
  it('sends one ordered typed prompt body without a text or attachment side channel', async () => {
    const requests: Array<Record<string, unknown>> = [];
    const responses = [
      connectPayload(),
      { command_outcome: {
        command_id: 'command:typed', status: 'PENDING', public_code: 'PROMPT_QUEUED',
        prompt_delivery: {
          queue_item_id: 'queue:typed', queue_status: 'PENDING', delivery_mode: 'NEW_TURN',
          execution: {status: 'REQUEST_EXECUTION_QUEUED'},
        },
      } },
    ];
    stubRuntimeFetch(vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.body) requests.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      return new Response(JSON.stringify(responses.shift()), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }));
    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    await connection.submitPrompt('command:typed', { parts: [
      { type: 'text', text: 'before\\n' },
      { type: 'image', source: 'local', bytes: Uint8Array.from([0, 255]), declaredMediaType: 'image/png' },
      { type: 'text', text: 'after' },
    ] }, 'read-only');

    expect(requests.at(-1)).toEqual({
      command_id: 'command:typed',
      command_kind: 'SUBMIT_PROMPT',
      client_submission_id: 'command:typed',
      prompt_content: { parts: [
        { type: 'text', text: 'before\\n' },
        { type: 'image', content_base64: 'AP8=', declared_media_type: 'image/png' },
        { type: 'text', text: 'after' },
      ] },
      requested_permission_mode: 'PERMISSION_MODE_READ_ONLY',
    });
    expect(requests.at(-1)).not.toHaveProperty('text');
    expect(requests.at(-1)).not.toHaveProperty('attachments');
  });

  it('reads one image by exact canonical owner and occurrence and verifies its bytes', async () => {
    const bytes = Uint8Array.from([0xff, 0x00, 0x89, 0x50, 0x4e, 0x47]);
    const digest = sha256Digest(bytes);
    const body = {
      ...inlineContent(JSON.stringify({ schema: 'pulsara.prompt/v2', parts: [{
        type: 'image', image_ref: 'image:aaaaaaaaaaaaaaaaaaaaaaaaaa', digest, encoded_bytes: bytes.length,
        media_type: 'image/png', width: 3, height: 2,
      }] })),
      media_type: 'application/vnd.pulsara.prompt+json', codec: 'utf-8',
    };
    const requests: Array<Record<string, unknown>> = [];
    const responses = [
      connectPayload([{
        entry_id: 'entry:image', turn_id: 'turn:image', entry_sequence: '1',
        entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content: body,
      }]),
      contentBytes(bytes),
    ];
    stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/read-content')) {
        requests.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      }
      return new Response(JSON.stringify(responses.shift()), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }));
    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    const image = connection.current().messages[0]?.promptContent?.parts[0];
    if (!image || image.type !== 'image') throw new Error('image projection missing');

    expect([...await connection.readPromptImage(image)]).toEqual([...bytes]);
    expect(requests).toEqual([{
      entry_id: 'entry:image', image_ref_ordinal: 0,
      offset_bytes: 0, limit_bytes: 1 << 20,
    }]);
  });

  it('preserves canonical ROOT order through inventory merges when an observation-only ROOT has no message', async () => {
    const entry = (id: string, turn: string, sequence: number, kind: string) => ({
      entry_id: id, turn_id: turn, entry_sequence: String(sequence), entry_kind: kind,
      scope_kind: 'ROOT', content: kind === 'USER_MESSAGE'
        ? inlinePrompt('text')
        : inlineContent('text'),
    });
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1, session_id: 'session-1', role: 'controller',
      live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
      snapshot: { snapshot: { session_id: 'session-1', writer_generation: '1', event_sequence_cut: '8', entries: [
        entry('prompt-a', 'A', 1, 'USER_MESSAGE'),
        entry('answer-a', 'A', 2, 'ASSISTANT_MESSAGE'),
        // B was started by a monitor, then interrupted before assistant output.
        entry('monitor-b', 'B', 3, 'TERMINAL_OBSERVATION'),
        entry('prompt-c', 'C', 4, 'USER_MESSAGE'),
        { ...entry('completion', 'C', 5, 'INTER_AGENT_MESSAGE'), source_subagent_task_id: 'reader' },
      ], control: { subagent_tasks: [{ task_id: 'reader', parent_turn_id: 'A', label: 'reader',
        status: 'COMPLETED', completion_accepted: true }] } } },
      live_control_snapshot: { snapshot: {} },
    }), { status: 200 })));
    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    await vi.waitFor(() => expect(connection.current().history?.windows.every(window=>window.contextState==='ready')).toBe(true));
    const projection = connection.current();
    expect(projection.messages.find(message => message.id === 'completion')?.sourceSubagentTaskId).toBe('reader');
    const merged = mergeRuntimeTaskInventory(projection, [inventoryTask('reader', 'reader', 'completed', 'A')]);
    expect(merged.messages.find(message => message.id === 'completion')?.sourceSubagentRelation).toBe('earlier');
    expect(mergeRuntimeTaskInventory(merged, merged.agentTasks).messages
      .find(message => message.id === 'completion')?.sourceSubagentRelation).toBe('earlier');
  });

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
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1, session_id: 'session-1', role: 'controller',
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

  it('hydrates canonical view_image content into its exact tool trace owner', async () => {
    const imageBody = JSON.stringify({
      schema: 'pulsara.prompt/v2',
      parts: [
        { type: 'text', text: 'Image loaded.' },
        {
          type: 'image', image_ref: 'image:aaaaaaaaaaaaaaaaaaaaaaaaaa', digest: `sha256:${'a'.repeat(64)}`, encoded_bytes: 3,
          media_type: 'image/png', width: 2, height: 1,
        },
      ],
    });
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(connectPayload([
      {
        entry_id: 'request', turn_id: 'turn', entry_sequence: '1',
        entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
        blocks: [{ block_id: 'block', block_kind: 'TOOL_CALL', tool_call_id: 'call', tool_name: 'view_image' }],
      },
      {
        entry_id: 'result', turn_id: 'turn', entry_sequence: '2',
        entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
        content: { ...inlineContent(imageBody), media_type: 'application/vnd.pulsara.prompt+json', codec: 'utf-8' },
        tool_result: { assistant_entry_id: 'request', tool_call_id: 'call', result_state: 'SUCCESS' },
      },
    ])), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    const trace = connection.current().messages.find((message) => message.id === 'request')?.traces?.[0];
    expect(trace?.resultText).toBe('Image loaded.');
    expect(trace?.resultContent?.parts[1]).toMatchObject({
      type: 'image', source: 'canonical', refOrdinal: 0,
      owner: { kind: 'entry', entryId: 'result' },
    });
  });

  it('keeps an in-flight user steer distinct from an ordinary user turn', async () => {
    const content = (value: string) => ({
      kind: 'INLINE',
      inline_content: btoa(String.fromCharCode(...new TextEncoder().encode(value))),
    });
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
      session_id: 'session-1', role: 'controller',
      live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '0',
          entries: [{
            entry_id: 'prompt-1', turn_id: 'turn-1', entry_sequence: '1',
            entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content: inlinePrompt('完成这项工作'),
          }, {
            entry_id: 'steer-1', turn_id: 'turn-1', entry_sequence: '2',
            entry_kind: 'USER_STEER', scope_kind: 'ROOT', content: inlinePrompt('先检查真实页面'),
          }, {
            entry_id: 'accepted-result-1', turn_id: 'turn-2', entry_sequence: '3',
            entry_kind: 'INTER_AGENT_MESSAGE', scope_kind: 'ROOT',
            source_subagent_task_id: 'task-1',
            content: content('{"status":"accepted"}'),
          }],
          control: { subagent_tasks: [{
            task_id: 'task-1', parent_turn_id: 'turn-1', label: 'reader',
            objective: '读取文件', status: 'COMPLETED', completion_accepted: true,
          }] },
        },
      },
      live_control_snapshot: { snapshot: {} },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    await vi.waitFor(() => expect(connection.current().history?.windows.every(window=>window.contextState==='ready')).toBe(true));
    expect(connection.current().messages.map((message) => ({
      body: message.body,
      userKind: message.userKind,
    }))).toEqual([
      { body: '完成这项工作', userKind: 'prompt' },
      { body: '先检查真实页面', userKind: 'steer' },
      { body: '', userKind: 'subagent-completion' },
    ]);
    expect(connection.current().messages[2]?.sourceSubagentTaskId).toBe('task-1');
    const withTask = mergeRuntimeTaskInventory(connection.current(), [inventoryTask('task-1', 'reader', 'completed', 'turn-1')]);
    expect(withTask.messages[2]).toMatchObject({
      sourceSubagentLabel: 'reader',
      sourceSubagentRelation: 'previous',
    });
  });

  it.each(['0', '9007199254740993'])('exposes only the tail then loads requested history pages preserving event cut %s', async (eventCut) => {
    const historyBodies: Array<Record<string, unknown>> = [];
    const entry = (sequence: number, body: string) => ({
      entry_id: `entry-${sequence}`,
      turn_id: `turn-${sequence}`,
      entry_sequence: String(sequence),
      entry_kind: 'USER_MESSAGE',
      content: inlinePrompt(body),
    });
    const responses = [
      {
        connection_id: 'connection-1',
        workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
        session_id: 'session-1',
        role: 'controller',
        live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
        snapshot: {
          snapshot: {
            session_id: 'session-1',
            writer_generation: '1',
            event_sequence_cut: eventCut,
            entries: [entry(3, 'latest')],
            older_history_cursor: {
              session_id: 'session-1',
              cut_sequence: '3',
              event_sequence_cut: eventCut,
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
            event_sequence_cut: eventCut,
            entry_sequence: '2',
          },
        },
      },
      { history_page: { entries: [entry(1, 'oldest')], has_more: false } },
    ];
    stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
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

    expect(connection.current().messages.map(message => message.body)).toEqual(['latest']);
    expect(historyBodies).toHaveLength(0);
    await vi.waitFor(() => expect(connection.current().history?.initialReady).toBe(true));
    await connection.readHistory!();
    expect(connection.current().messages.map(message => message.body)).toEqual(['middle', 'latest']);
    await vi.waitFor(() => expect(connection.current().history?.windows.every(window => window.contextState === 'ready')).toBe(true));
    await connection.readHistory!();
    expect(connection.current().messages.map((message) => message.body)).toEqual([
      'oldest',
      'middle',
      'latest',
    ]);
    expect(historyBodies).toHaveLength(2);
    expect(historyBodies[0]).toEqual({
      maximum_entries: 128,
      maximum_serialized_bytes: 2 << 20,
      cursor: {
        session_id: 'session-1', cut_sequence: '3',
        event_sequence_cut: eventCut, entry_sequence: '3',
      },
    });
    expect(historyBodies[1]?.cursor).toEqual({
      session_id: 'session-1', cut_sequence: '3',
      event_sequence_cut: eventCut, entry_sequence: '2',
    });
  });

  it('treats the manual context action as an explicit forced request', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const responses = [
      {
        connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
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
    vi.stubGlobal('crypto', { getRandomValues: webcrypto.getRandomValues.bind(webcrypto), randomUUID: () => 'test' });
    stubRuntimeFetch(vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
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
    const responses = [{
      connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
      session_id: 'session-1', role: 'controller',
      live_hello: { live_owner_epoch: '1', live_revision: '0', live_snapshot: {} },
      snapshot: {
        snapshot: {
          session_id: 'session-1', writer_generation: '1', event_sequence_cut: '2',
          entries: [{
            entry_id: 'entry-1', turn_id: 'turn-1', entry_sequence: '1',
            entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content: inlinePrompt('压缩前'),
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
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
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
      connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
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
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect((await connection.observe()).presentationNotices).toEqual([
      '模型已切换。由于上下文长度变化，接下来的回答可能不如之前连贯。',
    ]);
    expect((await connection.observe()).presentationNotices).toEqual([]);
  });

  it('projects provider reasoning and distinguishes full text from a summary', async () => {
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
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
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
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
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
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

  it('projects the root TODO from owner snapshots after live invalidation without attaching it to a message', async () => {
    const responses = [
      {
        connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
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
              ordered_items: [{ ordinal: 0, text: '任务步骤', status: 'in_progress' }],
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
    responses.splice(2, 0, {
      live_control_snapshot: { snapshot: {
        owner_epoch: '1', live_revision: '0', current_todos: [{
          todo_run_id: 'todo-root', todo_revision: '2', scope_kind: 'ROOT', disposition: 'ACTIVE',
          ordered_items: [
            { ordinal: 0, text: '检查契约', status: 'completed' },
            { ordinal: 1, text: '目视验证', status: 'in_progress' },
          ],
        }],
      } },
    });
    responses.push({ live_control_snapshot: { snapshot: {
      owner_epoch: '1', live_revision: '0', current_todos: [],
    } } });
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
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
    const digest = blobContent(reasoning).digest;
    const requests: Array<Record<string, unknown>> = [];
    const responses = [
      {
        connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
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
    stubRuntimeFetch(vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.body) requests.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      return new Response(JSON.stringify(responses.shift()), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().messages[0]).toMatchObject({contentState:'ready',reasoningContentState:'unloaded'});
    await connection.hydrateEntries!(['assistant-1']);
    expect(requests).toHaveLength(1); // Connect only; default body reads do not fetch thought.
    await connection.hydrateEntries!(['assistant-1'], undefined, true);
    expect(connection.current().messages[0].reasoning?.[0].body).toBe(reasoning);
    expect(requests.at(-1)).toMatchObject({
      entry_id: 'assistant-1', block_id: 'assistant-1:provider-reasoning:0', offset_bytes: 0,
    });
  });

  it('hydrates canonical blob entry, assistant block, and tool result bodies after reload', async () => {
    const queuedBody = '消费后的大正文🙂\n'.repeat(6_000);
    const assistantBody = '模型保真正文';
    const toolBody = JSON.stringify({ status: 'ok', content: '工具保真正文' });
    expect(new TextEncoder().encode(queuedBody).length).toBeGreaterThan(65_536);
    const entries = [{
      entry_id: 'consumed-prompt', turn_id: 'turn-blob', entry_sequence: '1',
      entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content: blobPrompt(queuedBody),
      input_source: {
        queue_item_id: 'queue-blob', command_id: 'command-blob', delivery_mode: 'NEW_TURN',
      },
    }, {
      entry_id: 'assistant-blob', turn_id: 'turn-blob', entry_sequence: '2',
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{
        block_id: 'assistant-text', block_kind: 'TEXT', content: blobContent(assistantBody),
      }, {
        block_id: 'assistant-call', block_kind: 'TOOL_CALL',
        tool_call_id: 'call-blob', tool_name: 'read_file',
      }],
    }, {
      entry_id: 'result-blob', turn_id: 'turn-blob', entry_sequence: '3',
      entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT', content: blobContent(toolBody),
      tool_result: {
        assistant_entry_id: 'assistant-blob', tool_call_id: 'call-blob', result_state: 'SUCCESS',
      },
    }];
    const reads: Array<Record<string, unknown>> = [];
    stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/read-content')) {
        reads.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      }
      const target = JSON.parse(String(init?.body ?? '{}'));
      const value = String(input).endsWith('/connections') ? connectPayload(entries)
        : contentChunk(target.entry_id === 'consumed-prompt' ? promptBody(queuedBody) : target.entry_id === 'assistant-blob' ? assistantBody : toolBody);
      return new Response(JSON.stringify(value), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect(reads).toEqual([]);
    await connection.hydrateEntries!(['consumed-prompt', 'assistant-blob', 'result-blob']);
    const projection = connection.current();

    expect(projection.messages.find((message) => message.id === 'consumed-prompt')?.body).toBe(queuedBody);
    expect(projection.messages.find((message) => message.id === 'assistant-blob')?.body).toBe(assistantBody);
    expect(projection.messages.find((message) => message.id === 'assistant-blob')?.traces?.[0].resultText)
      .toBe(toolBody);
    expect(reads).toHaveLength(3);
    expect(reads).toEqual(expect.arrayContaining([
      expect.objectContaining({ entry_id: 'consumed-prompt' }),
      expect.objectContaining({ entry_id: 'assistant-blob', block_id: 'assistant-text' }),
      expect.objectContaining({ entry_id: 'result-blob' }),
    ]));
  });

  it('refreshes an exact consumed entry when a blob queue item is consumed during hydration', async () => {
    const body = '读取中被消费🙂\n'.repeat(6_000);
    const pending = {
      prompt_queue_total_count: '1',
      prompt_queue: [{
        queue_item_id: 'queue-race', command_id: 'command-race', queue_sequence: '1',
        status: 'PENDING', delivery_mode: 'NEW_TURN', content: blobPrompt(body),
      }],
    };
    const consumed = {
      entry_id: 'entry-race', turn_id: 'turn-race', entry_sequence: '1',
      entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content: blobPrompt(body),
      input_source: {
        queue_item_id: 'queue-race', command_id: 'command-race', delivery_mode: 'NEW_TURN',
      },
    };
    const responses = [
      connectPayload([], pending),
      { error: { stable_code: 'CONTENT_QUEUE_NOT_PENDING', public_message: 'Queue item is no longer pending.' } },
      {query_command:{found:true,outcome:{command_id:'command-race',status:'SUCCEEDED',prompt_delivery:{queue_item_id:'queue-race',queue_status:'CONSUMED',delivery_mode:'NEW_TURN',consumed_entry_id:'entry-race',execution:{status:'REQUEST_EXECUTION_COMPLETED'}}}}},
      {session_id:'session-1',cut_sequence:'1',event_sequence_cut:'1',entries:[consumed]},
      contentChunk(promptBody(body)),
    ];
    const operations: string[] = [];
    stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL) => {
      operations.push(String(input).split('/').at(-1) ?? '');
      return new Response(JSON.stringify(responses.shift()), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    await vi.waitFor(() => expect(connection.current().messages[0]?.body).toBe(body));
    const projection = connection.current();

    expect(projection.queuedPrompts).toEqual([]);
    expect(projection.messages).toEqual([
      expect.objectContaining({
        id: 'entry-race', body,
        inputSource: expect.objectContaining({ queueItemId: 'queue-race', commandId: 'command-race' }),
      }),
    ]);
    expect(operations.slice(1)).toEqual(['read-content', 'query-command', 'window', 'read-content']);
  });

  it.each([
    ['CANCELLED', 'cancelled', 'USER_CANCELLED'],
    ['REJECTED', 'rejected', 'PROMPT_REJECTED'],
  ] as const)(
    'preserves an exact %s queue terminal when initial blob hydration loses pending authorization',
    async (queueStatus, expectedStatus, publicCode) => {
      const body = '尚未完成读取的大队列正文🙂\n'.repeat(6_000);
      const pending = {
        prompt_queue_total_count: '1',
        prompt_queue: [{
          queue_item_id: 'queue-terminal-race', command_id: 'command-terminal-race',
          queue_sequence: '7', status: 'PENDING', delivery_mode: 'STEER_ACTIVE_TURN',
          target_turn_id: 'turn-target',
          permission: { effective_mode: 'PERMISSION_MODE_READ_ONLY' },
          content: blobContent(body),
        }],
      };
      const detail = `Queue became ${queueStatus.toLowerCase()} before hydration completed.`;
      const responses = [
        connectPayload([], pending),
        { error: { stable_code: 'CONTENT_QUEUE_NOT_PENDING', public_message: 'Queue item is no longer pending.' } },
        { query_command: { found: true, outcome: {
          command_id: 'command-terminal-race', status: 'REJECTED',
          public_code: publicCode, public_message: detail,
          prompt_delivery: {
            queue_item_id: 'queue-terminal-race', queue_status: queueStatus,
            delivery_mode: 'STEER_ACTIVE_TURN',
          },
        } } },
      ];
      const operations: string[] = [];
      stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL) => {
        operations.push(String(input).split('/').at(-1) ?? '');
        return new Response(JSON.stringify(responses.shift()), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }));

      const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
      await vi.waitFor(() => expect(connection.current().promptTransitions[0]?.status).toBe(expectedStatus));
      const projection = connection.current();
      expect(projection.queuedPrompts).toEqual([]);
      expect(projection.promptTransitions).toEqual([expect.objectContaining({
        sessionId: 'session-1', connectionGeneration: 1,
        queueItemId: 'queue-terminal-race', commandId: 'command-terminal-race',
        deliveryMode: 'steer', targetTurnId: 'turn-target', permission: 'read-only',
        content: undefined, contentUnavailable: true, status: expectedStatus,
        outcomeCode: publicCode, detail,
      })]);
      expect(operations.slice(1)).toEqual(['read-content', 'query-command']);
    },
  );

  it('rejects same-length canonical blob corruption even when the server echoes the descriptor digest', async () => {
    const expected = new TextEncoder().encode('trusted bytes');
    const tampered = new TextEncoder().encode('altered bytes');
    expect(tampered.length).toBe(expected.length);
    const reference = blobBytes(expected);
    const responses = [
      connectPayload([{
        entry_id: 'entry-corrupt', turn_id: 'turn-corrupt', entry_sequence: '1',
        entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content: reference,
      }]),
      contentBytes(tampered, reference.digest),
    ];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect(connection.current().messages[0].contentState).toBe('unloaded');
    const result = await connection.hydrateEntries!(['entry-corrupt']);
    expect(result.messages[0]).toMatchObject({body:'',contentState:'error',contentError:expect.any(String)});
    expect(result.messages[0].contentError).toBe('这条会话内容暂时无法完整读取。');
    responses.push(contentBytes(tampered, reference.digest));
    await expect(connection.readCanonicalEntryContent('entry-corrupt',reference.digest,Number(reference.size)))
      .rejects.toMatchObject({code:'CONTENT_INTEGRITY_INVALID'});
  });

  it('rejects canonical blob bytes that are not valid UTF-8', async () => {
    const invalidUtf8 = new Uint8Array([0xc3, 0x28]);
    const reference = blobBytes(invalidUtf8);
    const responses = [
      connectPayload([{
        entry_id: 'entry-invalid-utf8', turn_id: 'turn-invalid-utf8', entry_sequence: '1',
        entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content: reference,
      }]),
      contentBytes(invalidUtf8),
    ];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect(connection.current().messages[0].contentState).toBe('unloaded');
    const result = await connection.hydrateEntries!(['entry-invalid-utf8']);
    expect(result.messages[0]).toMatchObject({body:'',contentState:'error',contentError:expect.any(String)});
    expect(result.messages[0].contentError).toBe('这条会话内容暂时无法完整读取。');
    responses.push(contentBytes(invalidUtf8));
    await expect(connection.readCanonicalEntryContent('entry-invalid-utf8',reference.digest,Number(reference.size)))
      .rejects.toMatchObject({code:'CONTENT_INTEGRITY_INVALID'});
  });

  it('keeps observed task navigation before an exact task page and joins details only for loaded canonical rows', async () => {
    historyOwnerFixture = {tasks:Array.from({length:144},(_,i)=>({task_id:`worker-${i}`,parent_turn_id:'root',parent_initial_entry_id:'create',batch_id:'workers'})),batches:[{batch_id:'workers',parent_turn_id:'root',creator_entry_id:'create',creator_tool_call_id:'spawn',task_count:144}]};
    const ids = Array.from({length: 144}, (_, i) => `worker-${i}`);
    const payload = connectPayload([
      {entry_id:'create',turn_id:'root',entry_sequence:'1',entry_kind:'ASSISTANT_TOOL_REQUEST',scope_kind:'ROOT',blocks:[{block_id:'spawn',block_kind:'TOOL_CALL',tool_call_id:'spawn',tool_name:'create_agent_tasks'}]},
      {entry_id:'created',turn_id:'root',entry_sequence:'2',entry_kind:'TOOL_RESULT',scope_kind:'ROOT',tool_result:{assistant_entry_id:'create',tool_call_id:'spawn',result_state:'SUCCESS'},content:inlineContent(JSON.stringify({tasks:ids.map(task_id=>({task_id}))}))},
      ...ids.map((id,i)=>({entry_id:`entry-${id}`,turn_id:`turn-${id}`,entry_sequence:String(i+3),entry_kind:'ASSISTANT_MESSAGE',scope_kind:'SUBAGENT_TASK',scope_subagent_task_id:id,blocks:[{block_id:`text-${id}`,block_kind:'TEXT',content:inlineContent('intermediate commentary')},{block_id:`call-${id}`,block_kind:'TOOL_CALL',tool_call_id:`call-${id}`,tool_name:'terminal'}]})),
    ], {active_turns:[{turn_id:'root',scope_kind:'ROOT',status:'RUNNING'}]}, [{
      live_revision:'1', event_type:'TEXT_DELTA', draft_identity:'worker-live', turn_id:'turn-worker-143', scope_kind:'SUBAGENT_TASK',scope_subagent_task_id:'worker-143',payload:{text_delta:{delta:'still working'}},
    }]);
    stubRuntimeFetch(vi.fn(async()=>new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}})));
    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    await vi.waitFor(() => expect(connection.current().history?.windows.every(window=>window.contextState==='ready')).toBe(true));
    const raw = connection.current();
    expect(raw.subagentRuns).toHaveLength(144);
    expect(raw.subagentRuns?.[0].status).toBe('unknown');
    expect(raw.subagentRuns?.[0].activities[0].traces?.[0].status).toBe('running');
    const task = inventoryTask('worker-143','visible worker','running','root','work');
    const loaded = mergeRuntimeTaskInventory(raw,[task]);
    const runs = loaded.messages.find(m=>m.id==='create')?.subagentRuns;
    expect(runs).toHaveLength(144);
    const selected = runs?.find(run => run.id === task.id);
    expect(selected?.activities.some(a=>a.body==='still working')).toBe(true);
    expect(selected?.activities[0].traces?.[0].status).toBe('running');
    expect(runs?.filter(run => run.id !== task.id).every(run => run.activities.length === 0)).toBe(true);
    const terminal = mergeRuntimeTaskInventory(raw,[{...task,status:'completed'}]);
    const done = terminal.messages.find(m=>m.id==='create')?.subagentRuns?.[0];
    expect(done?.activities.some(a=>a.id.startsWith('live:'))).toBe(false);
    expect(done?.activities[0].traces?.[0].status).toBe('cancelled');
    expect(raw.subagentRuns?.[143].activities[0].traces?.[0].status).toBe('running');
    const summaries = mergeRuntimeTaskInventory(raw, []).messages.find(m => m.id === 'create')?.subagentRuns;
    expect(summaries).toHaveLength(144);
    expect(summaries?.every(run => run.batchId === 'workers' && run.activities.length === 0 && run.status === raw.subagentRuns?.find(source => source.id === run.id)?.status)).toBe(true);
    expect(mergeRuntimeTaskInventory(raw, []).agentTasks).toEqual([]);
  });

  it.each(['start', 'delta', 'end'])('keeps the 144th worker result %s and progress outside bounded control', async phase => {
    historyOwnerFixture = {tasks:Array.from({length:144},(_,i)=>({task_id:`worker-${i}`,parent_turn_id:'root',parent_initial_entry_id:'create',batch_id:'workers'})),batches:[{batch_id:'workers',parent_turn_id:'root',creator_entry_id:'create',creator_tool_call_id:'spawn',task_count:144}]};
    const ids = Array.from({length: 144}, (_, i) => `worker-${i}`);
    const target = ids[143];
    const event = {turn_id: `turn-${target}`, scope_kind: 'SUBAGENT_TASK', scope_subagent_task_id: target,
      channel_kind: 'TOOL_RESULT', channel_attempt_id: 'attempt-143', channel_tool_call_id: 'same-call-id'};
    const events = [
      {...event, live_revision:'1', event_type:'TOOL_RESULT_START', payload:{tool_result_start:{assistant_entry_id:'entry-worker-143',tool_call_id:'same-call-id',attempt_id:'attempt-143'}}},
      {...event, live_revision:'2', event_type:'TOOL_RESULT_DELTA', payload:{tool_result_delta:{text:'partial result'}}},
      {...event, live_revision:'3', event_type:'TOOL_RESULT_END', payload:{tool_result_end:{result_state:'SUCCESS',final_text:'complete result'}}},
    ].slice(0, phase === 'start' ? 1 : phase === 'delta' ? 2 : 3);
    const payload = connectPayload([
      {entry_id:'create',turn_id:'root',entry_sequence:'1',entry_kind:'ASSISTANT_TOOL_REQUEST',scope_kind:'ROOT',blocks:[{block_id:'spawn',block_kind:'TOOL_CALL',tool_call_id:'spawn',tool_name:'create_agent_tasks'}]},
      ...ids.map((id,i)=>({entry_id:`entry-${id}`,turn_id:`turn-${id}`,entry_sequence:String(i+2),entry_kind:'ASSISTANT_TOOL_REQUEST',scope_kind:'SUBAGENT_TASK',scope_subagent_task_id:id,blocks:[{block_id:`call-${id}`,block_kind:'TOOL_CALL',tool_call_id:'same-call-id',tool_name:'terminal'}]})),
    ], {
      active_turns: ids.slice(0,128).map(id=>({turn_id:`turn-${id}`,scope_kind:'SUBAGENT_TASK',status:'RUNNING'})),
      tool_attempts: ids.slice(0,128).map((id,i)=>({attempt_id:`attempt-${i}`,assistant_entry_id:`entry-${id}`,tool_call_id:'same-call-id'})),
    }, [...events, {live_revision:'4',event_type:'SUBAGENT_PROGRESS',scope_kind:'SUBAGENT_TASK',scope_subagent_task_id:target,payload:{subagent_progress:{task_id:target,status:'ACTIVE',public_summary:'examining last worker'}}}]);
    stubRuntimeFetch(vi.fn(async()=>new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}})));
    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    await vi.waitFor(() => expect(connection.current().history?.windows.every(window=>window.contextState==='ready')).toBe(true));
    const raw = connection.current();
    const task = inventoryTask(target,'last worker','running','root');
    const merged = mergeRuntimeTaskInventory(raw,[task]);
    const trace = merged.messages[0].subagentRuns?.[0].activities[0].traces?.[0];
    expect(trace).toMatchObject({id:'same-call-id',status:phase === 'end' ? 'completed' : 'running'});
    expect(trace?.resultText).toBe(phase === 'start' ? undefined : phase === 'delta' ? 'partial result' : 'complete result');
    expect(raw.subagentRuns?.[0].activities[0].traces?.[0].resultText).toBeUndefined();
    expect(merged.agentTasks[0].progress).toBe('examining last worker');
    expect(mergeRuntimeTaskInventory(raw,[{...task,status:'completed'}]).agentTasks[0].progress).toBeUndefined();
    expect(mergeRuntimeTaskInventory(raw,[]).agentTasks).toEqual([]);
  });

  it('projects successful orchestration results and exposes child execution', async () => {
    historyOwnerFixture = {tasks:['task-1','task-2'].map(task_id=>({task_id,parent_turn_id:'turn-root',parent_initial_entry_id:'root-create',batch_id:'created'})),batches:[{batch_id:'created',parent_turn_id:'turn-root',creator_entry_id:'root-create',creator_tool_call_id:'call-create',task_count:2}]};
    const content = (value: string) => ({ kind: 'INLINE', inline_content: btoa(String.fromCharCode(...new TextEncoder().encode(value))) });
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
      toolRequest(1, 'root-create', 'create_agent_tasks', 'call-create'),
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
        content: content(JSON.stringify({ tasks: [
          { task_id: 'task-1', status: 'active' },
          { task_id: 'task-2', status: 'pending_start' },
        ] })),
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
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1',
      workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
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
    await vi.waitFor(() => expect(connection.current().history?.windows.every(window=>window.contextState==='ready')).toBe(true));
    const projected = mergeRuntimeTaskInventory(connection.current(), [
      { ...inventoryTask('task-1', '读取标题', 'completed', 'turn-root', 'Read README.md.'), summary: '# Pulsara' },
      inventoryTask('task-2', '等待槽位', 'pending', 'turn-root', 'Wait for capacity.'),
    ]);
    const create = projected.messages.find((message) => message.id === 'root-create');
    const wait = projected.messages.find((message) => message.id === 'root-wait');
    const denied = projected.messages.find((message) => message.id === 'root-denied');
    const userDenied = projected.messages.find((message) => message.id === 'root-user-denied');
    const artifactRead = projected.messages.find((message) => message.id === 'root-artifact-read');
    const orphanTool = projected.messages.find((message) => message.id === 'root-orphan-tool');

    expect(create?.assistantKind).toBe('tool-request');
    expect(create?.traces?.[0]).toMatchObject({ status: 'completed', meta: '操作完成' });
    expect(create?.traces?.[0].toolName).toBe('create_agent_tasks');
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
      status: 'cancelled', subtitle: '已结束', meta: '结果待核实',
      resultSummary: '原运行已结束，操作结果尚未记录；需要核实实际状态。',
    });
    expect(orphanTool?.traces?.[0].resultText).toBeUndefined();
    expect(orphanTool?.subagentRuns).toBeUndefined();
    expect(create?.subagentRuns?.[0]).toMatchObject({
      id: 'task-1', label: '读取标题', status: 'completed', objective: 'Read README.md.',
    });
    expect(create?.subagentRuns?.[0].activities.some(activity => activity.id === 'task-read')).toBe(true);
    expect(create?.subagentRuns?.find((run) => run.id === 'task-2')).toMatchObject({
      label: '等待槽位', status: 'pending', activities: [],
    });
    expect(projected.agentTasks).toHaveLength(2);
    expect(projected.agentTasks[1].status).toBe('pending');
    expect(JSON.stringify(create?.subagentRuns)).not.toContain('stale live text');
  });

  it('attaches child execution to the successful creator instead of an earlier failed call', async () => {
    historyOwnerFixture = {tasks:[{task_id:'task-success',parent_turn_id:'turn-root',parent_initial_entry_id:'create-invalid',batch_id:'batch-success'}],batches:[{batch_id:'batch-success',parent_turn_id:'turn-root',creator_entry_id:'create-success',creator_tool_call_id:'call-success',task_count:1}]};
    const entries = [{
      entry_id: 'create-invalid', turn_id: 'turn-root', entry_sequence: '1',
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{
        block_id: 'invalid-block', block_kind: 'TOOL_CALL',
        tool_call_id: 'call-invalid', tool_name: 'create_agent_tasks',
        tool_arguments_preview: btoa('{}'),
      }],
    }, {
      entry_id: 'result-invalid', turn_id: 'turn-root', entry_sequence: '2',
      entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
      tool_result: {
        assistant_entry_id: 'create-invalid', tool_call_id: 'call-invalid',
        result_state: 'INVALID_ARGUMENTS',
      },
      content: inlineContent(JSON.stringify({ error: 'dependency reference is unknown' })),
    }, {
      entry_id: 'create-success', turn_id: 'turn-root', entry_sequence: '3',
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{
        block_id: 'success-block', block_kind: 'TOOL_CALL',
        tool_call_id: 'call-success', tool_name: 'create_agent_tasks',
        tool_arguments_preview: btoa('{}'),
      }],
    }, {
      entry_id: 'result-success', turn_id: 'turn-root', entry_sequence: '4',
      entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
      tool_result: {
        assistant_entry_id: 'create-success', tool_call_id: 'call-success',
        result_state: 'SUCCESS',
      },
      content: inlineContent(JSON.stringify({
        batch_id: 'batch-success',
        tasks: [{ task_id: 'task-success', status: 'active' }],
      })),
    }];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(connectPayload(
      entries,
      { subagent_tasks: [{
        task_id: 'task-success', parent_turn_id: 'turn-root', batch_id: 'batch-success',
        status: 'COMPLETED', label: 'reader', display_role: '通用协作',
        objective: 'Read the target file.', result_summary: 'done',
      }] },
    )), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    await vi.waitFor(() => expect(connection.current().history?.windows.every(window=>window.contextState==='ready')).toBe(true));
    const projection = mergeRuntimeTaskInventory(connection.current(), [
      { ...inventoryTask('task-success', 'reader', 'completed', 'turn-root', 'Read the target file.'), batchId: 'batch-success' },
    ]);
    const invalid = projection.messages.find((message) => message.id === 'create-invalid');
    const success = projection.messages.find((message) => message.id === 'create-success');

    expect(invalid?.traces?.[0]).toMatchObject({
      resultState: 'INVALID_ARGUMENTS', status: 'failed',
    });
    expect(invalid?.subagentRuns).toBeUndefined();
    expect(success?.traces?.[0]).toMatchObject({ resultState: 'SUCCESS', status: 'completed' });
    expect(success?.subagentRuns).toEqual([
      expect.objectContaining({ id: 'task-success', label: 'reader', status: 'completed' }),
    ]);
  });

  it('keeps exact MCP meta-tool arguments and results for structured UI projection', async () => {
    const argumentsJson = JSON.stringify({ kind: 'MCP_TOOL', parent: { kind: 'MCP_SERVER', runtime_server_id: 'firecrawl' } });
    const resultText = JSON.stringify({
      total_count: 1, completeness: 'COMPLETE',
      items: [{ kind: 'MCP_SERVER', name: 'firecrawl', target: { kind: 'MCP_SERVER', runtime_server_id: 'firecrawl' }, source: null, status: '连接已就绪。' }],
    });
    const content = (value: string) => ({ kind: 'INLINE', inline_content: btoa(String.fromCharCode(...new TextEncoder().encode(value))) });
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
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
              tool_call_id: 'call-mcp-list', tool_name: 'list_capabilities',
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
      toolName: 'list_capabilities',
      title: '查看能力',
      argumentsJson,
      resultText,
      status: 'completed',
    });
  });

  it('keeps detached child result data without inferring active membership or showing unloaded tasks', async () => {
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
      connection_id: 'connection-1', workspace_availability: connectedWorkspaceAvailability, connection_generation: 1,
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

  it('associates interleaved live tool results by exact attempt and preserves an empty final result', async () => {
    const entries = [{
      entry_id: 'assistant-live', turn_id: 'turn-live', entry_sequence: '1',
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [
        { block_id: 'block-a', block_kind: 'TOOL_CALL', tool_call_id: 'call-a', tool_name: 'read_file' },
        { block_id: 'block-b', block_kind: 'TOOL_CALL', tool_call_id: 'call-b', tool_name: 'search_content' },
      ],
    }];
    const control = {
      active_turns: [{ turn_id: 'turn-live', status: 'RUNNING', scope_kind: 'ROOT' }],
      tool_attempts: [
        { attempt_id: 'attempt-a', assistant_entry_id: 'assistant-live', tool_call_id: 'call-a' },
        { attempt_id: 'attempt-b', assistant_entry_id: 'assistant-live', tool_call_id: 'call-b' },
      ],
    };
    const liveEvents = [
      {
        live_revision: '1', event_type: 'TOOL_RESULT_START', turn_id: 'turn-live', scope_kind: 'ROOT',
        channel_kind: 'TOOL_RESULT', channel_attempt_id: 'attempt-a', channel_tool_call_id: 'call-a',
        payload: { tool_result_start: { tool_call_id: 'call-a' } },
      },
      {
        live_revision: '2', event_type: 'TOOL_RESULT_START', turn_id: 'turn-live', scope_kind: 'ROOT',
        channel_kind: 'TOOL_RESULT', channel_attempt_id: 'attempt-b', channel_tool_call_id: 'call-b',
        payload: { tool_result_start: { tool_call_id: 'call-b' } },
      },
      {
        live_revision: '3', event_type: 'TOOL_RESULT_END', turn_id: 'turn-live', scope_kind: 'ROOT',
        channel_kind: 'TOOL_RESULT', channel_attempt_id: 'attempt-a', channel_tool_call_id: 'call-a',
        payload: { tool_result_end: { tool_call_id: 'call-a', result_state: 'SUCCESS', final_text: '' } },
      },
      {
        live_revision: '4', event_type: 'TOOL_RESULT_END', turn_id: 'turn-live', scope_kind: 'ROOT',
        channel_kind: 'TOOL_RESULT', channel_attempt_id: 'attempt-b', channel_tool_call_id: 'call-b',
        payload: { tool_result_end: { tool_call_id: 'call-b', result_state: 'APPLICATION_ERROR', final_text: 'b failed' } },
      },
    ];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(
      connectPayload(entries, control, liveEvents),
    ), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const traces = (await new LocalHttpRuntimeAdapter().connect('session-1'))
      .current().messages.find((message) => message.id === 'assistant-live')?.traces;
    expect(traces?.find((trace) => trace.id === 'call-a')).toMatchObject({
      status: 'completed', resultText: '',
    });
    expect(traces?.find((trace) => trace.id === 'call-b')).toMatchObject({
      status: 'failed', resultText: 'b failed',
    });
  });

  it('reconciles one pending live result when its exact attempt mapping arrives later', async () => {
    const request = {
      entry_id: 'assistant-late', turn_id: 'turn-late', entry_sequence: '1',
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{
        block_id: 'block-late', block_kind: 'TOOL_CALL',
        tool_call_id: 'call-late', tool_name: 'read_file',
      }],
    };
    const live = [{
      live_revision: '1', event_type: 'TOOL_RESULT_START', turn_id: 'turn-late', scope_kind: 'ROOT',
      channel_kind: 'TOOL_RESULT', channel_attempt_id: 'attempt-late',
      payload: { tool_result_start: { attempt_id: 'attempt-late' } },
    }, {
      live_revision: '2', event_type: 'TOOL_RESULT_END', turn_id: 'turn-late', scope_kind: 'ROOT',
      channel_kind: 'TOOL_RESULT', channel_attempt_id: 'attempt-late',
      payload: { tool_result_end: { result_state: 'SUCCESS', final_text: 'late exact result' } },
    }];
    const responses = [
      connectPayload([request], {
        active_turns: [{ turn_id: 'turn-late', status: 'RUNNING', scope_kind: 'ROOT' }],
      }, live),
      { observation: {
        through_event_sequence: '2', live_owner_epoch: '1', through_live_revision: '2',
        committed: [{
          projection_kind: 'CURRENT_CONTROL',
          current_control: {
            active_turns: [{ turn_id: 'turn-late', status: 'RUNNING', scope_kind: 'ROOT' }],
            tool_attempts: [{
              attempt_id: 'attempt-late', assistant_entry_id: 'assistant-late', tool_call_id: 'call-late',
            }],
          },
        }],
      } },
    ];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect(connection.current().messages).toHaveLength(2);

    const projection = await connection.observe();
    expect(projection.messages).toHaveLength(1);
    expect(projection.messages[0]).toMatchObject({ id: 'assistant-late' });
    expect(projection.messages[0].traces).toEqual([
      expect.objectContaining({ id: 'call-late', resultText: 'late exact result', status: 'completed' }),
    ]);
  });

  it('settles the pending live-result identity after an attempt mapping arrives in the same observation', async () => {
    const request = {
      entry_id: 'assistant-settle', turn_id: 'turn-settle', entry_sequence: '1',
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{
        block_id: 'block-settle', block_kind: 'TOOL_CALL',
        tool_call_id: 'call-settle', tool_name: 'read_file',
      }],
    };
    const responses = [
      connectPayload([request], {
        active_turns: [{ turn_id: 'turn-settle', status: 'RUNNING', scope_kind: 'ROOT' }],
      }, [{
        live_revision: '1', event_type: 'TOOL_RESULT_END', turn_id: 'turn-settle', scope_kind: 'ROOT',
        channel_kind: 'TOOL_RESULT', channel_attempt_id: 'attempt-settle',
        payload: { tool_result_end: { result_state: 'SUCCESS', final_text: 'settled preview' } },
      }]),
      { observation: {
        through_event_sequence: '2', live_owner_epoch: '1', through_live_revision: '2',
        committed: [{
          projection_kind: 'CURRENT_CONTROL',
          current_control: {
            active_turns: [{ turn_id: 'turn-settle', status: 'RUNNING', scope_kind: 'ROOT' }],
            tool_attempts: [{
              attempt_id: 'attempt-settle', assistant_entry_id: 'assistant-settle', tool_call_id: 'call-settle',
            }],
          },
        }],
        settlements: [{
          kind: 'COMMITTED', channel_kind: 'TOOL_RESULT',
          channel_attempt_id: 'attempt-settle', channel_tool_call_id: 'call-settle',
        }],
      } },
    ];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect(connection.current().messages).toHaveLength(2);

    const projection = await connection.observe();
    expect(projection.messages).toHaveLength(1);
    expect(projection.messages[0].traces?.[0]).not.toHaveProperty('resultText');
  });

  it('settles one interleaved result before mappings arrive and later binds only its sibling', async () => {
    const request = {
      entry_id: 'assistant-interleaved', turn_id: 'turn-interleaved', entry_sequence: '1',
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{
        block_id: 'block-a', block_kind: 'TOOL_CALL',
        tool_call_id: 'call-a', tool_name: 'read_file',
      }, {
        block_id: 'block-b', block_kind: 'TOOL_CALL',
        tool_call_id: 'call-b', tool_name: 'read_file',
      }],
    };
    const live = ['a', 'b'].map((suffix, index) => ({
      live_revision: String(index + 1), event_type: 'TOOL_RESULT_END',
      turn_id: 'turn-interleaved', scope_kind: 'ROOT', channel_kind: 'TOOL_RESULT',
      channel_attempt_id: `attempt-${suffix}`, channel_tool_call_id: `call-${suffix}`,
      proposed_entry_id: `result-${suffix}`, draft_identity: `result-${suffix}`,
      generation_id: `generation-${suffix}`, block_id: `result-block-${suffix}`,
      payload: { tool_result_end: { result_state: 'SUCCESS', final_text: `result ${suffix}` } },
    }));
    const responses = [
      connectPayload([request], {
        active_turns: [{ turn_id: 'turn-interleaved', status: 'RUNNING', scope_kind: 'ROOT' }],
      }, live),
      { observation: {
        through_event_sequence: '1', live_owner_epoch: '1', through_live_revision: '3',
        settlements: [{
          kind: 'COMMITTED', channel_kind: 'TOOL_RESULT',
          channel_attempt_id: 'attempt-a', channel_tool_call_id: 'call-a',
          proposed_entry_id: 'result-a', draft_identity: 'result-a',
          generation_id: 'generation-a',
        }],
      } },
      { observation: {
        through_event_sequence: '2', live_owner_epoch: '1', through_live_revision: '3',
        committed: [{
          projection_kind: 'CURRENT_CONTROL',
          current_control: {
            active_turns: [{ turn_id: 'turn-interleaved', status: 'RUNNING', scope_kind: 'ROOT' }],
            tool_attempts: [{
              attempt_id: 'attempt-a', assistant_entry_id: 'assistant-interleaved', tool_call_id: 'call-a',
            }, {
              attempt_id: 'attempt-b', assistant_entry_id: 'assistant-interleaved', tool_call_id: 'call-b',
            }],
          },
        }],
      } },
    ];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect(connection.current().messages).toHaveLength(3);
    expect((await connection.observe()).messages).toHaveLength(2);

    const projection = await connection.observe();
    expect(projection.messages).toHaveLength(1);
    expect(projection.messages[0].traces?.find((trace) => trace.id === 'call-a'))
      .not.toHaveProperty('resultText');
    expect(projection.messages[0].traces?.find((trace) => trace.id === 'call-b'))
      .toMatchObject({ resultText: 'result b', status: 'completed' });
  });

  it('projects identical queued prompt bodies by queue and command identity in queue order', async () => {
    const control = {
      prompt_queue_total_count: '2',
      prompt_queue: [
        {
          queue_item_id: 'queue-2', command_id: 'command-2', queue_sequence: '2',
          status: 'PENDING', delivery_mode: 'NEW_TURN', content: inlinePrompt('相同正文'),
        },
        {
          queue_item_id: 'queue-1', command_id: 'command-1', queue_sequence: '1',
          status: 'PENDING', delivery_mode: 'STEER_ACTIVE_TURN', target_turn_id: 'turn-1',
          content: inlinePrompt('相同正文'),
        },
      ],
    };
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(
      connectPayload([], control),
    ), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    expect((await new LocalHttpRuntimeAdapter().connect('session-1')).current().queuedPrompts)
      .toEqual([
        expect.objectContaining({
          queueItemId: 'queue-1', commandId: 'command-1', content: textPromptContent('相同正文'),
          deliveryMode: 'steer', targetTurnId: 'turn-1',
        }),
        expect.objectContaining({
          queueItemId: 'queue-2', commandId: 'command-2', content: textPromptContent('相同正文'),
          deliveryMode: 'new-turn',
        }),
      ]);
  });
});

describe('LocalHttpRuntimeAdapter connection ownership', () => {
  it('sends the stable page identity with an explicit takeover request', async () => {
    let requestBody: unknown;
    stubRuntimeFetch(vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      requestBody = init?.body ? JSON.parse(String(init.body)) : undefined;
      return new Response(JSON.stringify({
        connection_id: 'connection-takeover', workspace_availability: connectedWorkspaceAvailability, connection_generation: 2,
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
    stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
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
        connection_id: 'connection-result', workspace_availability: connectedWorkspaceAvailability, connection_generation: 3,
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
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
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
        terminal_public_detail: '前置任务未能完成。', completion_accepted: true,
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
    stubRuntimeFetch(fetchMock);

    const page = await new LocalHttpRuntimeAdapter().listSessionTasks('session-1');

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/sessions/session-1/tasks?limit=50');
    expect(page).toMatchObject({ totalCount: 51, remainingCount: 50, nextCursor: 'page-2' });
    expect(page.tasks[0]).toMatchObject({
      id: 'task-blocked', status: 'blocked', role: '验证',
      terminalPublicDetail: '前置任务未能完成。', completionAccepted: true,
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
      completionAccepted: false,
      summary: '中断前的最后结果',
    }];
    const projection: RuntimeProjection = {
      messages: [], isRunning: true, queuedCount: 0, queuedPrompts: [], promptTransitions: [], planMode: false,
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

  it('preserves owner-classified completion sources through inventory merges', () => {
    const baseTask = {
      role: '研究', objective: '检查结果', status: 'completed' as const,
      completionAccepted: true, dependencyIds: [], color: 'blue' as const,
    };
    const projection: RuntimeProjection = {
      canonicalRootTurnIds: ['turn-1', 'turn-2', 'turn-3'],
      messages: [
        { id: 'root-1', turnId: 'turn-1', entrySequence: 1, role: 'user', time: '1', body: '一' },
        { id: 'root-2', turnId: 'turn-2', entrySequence: 2, role: 'user', time: '2', body: '二' },
        { id: 'root-3', turnId: 'turn-3', entrySequence: 3, role: 'user', time: '3', body: '三' },
        { id: 'accepted-old', turnId: 'turn-3', entrySequence: 4, role: 'user', time: '3', body: '', userKind: 'subagent-completion', sourceSubagentTaskId: 'task-old', sourceSubagentRelation: 'earlier' },
        { id: 'accepted-previous', turnId: 'turn-3', entrySequence: 5, role: 'user', time: '3', body: '', userKind: 'subagent-completion', sourceSubagentTaskId: 'task-previous', sourceSubagentRelation: 'previous' },
        { id: 'accepted-current', turnId: 'turn-3', entrySequence: 6, role: 'user', time: '3', body: '', userKind: 'subagent-completion', sourceSubagentTaskId: 'task-current', sourceSubagentRelation: 'current' },
      ],
      isRunning: false, queuedCount: 0, queuedPrompts: [], promptTransitions: [], planMode: false,
      control: {}, liveControl: {}, agentTasks: [], eventSequence: 6,
      liveOwnerEpoch: 1, liveRevision: 1, liveControlOwnerEpoch: 1, liveControlRevision: 1,
    };
    const merged = mergeRuntimeTaskInventory(projection, [
      { ...baseTask, id: 'task-old', label: '旧任务', parentId: 'turn-1' },
      { ...baseTask, id: 'task-previous', label: '上一任务', parentId: 'turn-2' },
      { ...baseTask, id: 'task-current', label: '当前任务', parentId: 'turn-3' },
    ]);

    expect(merged.messages.slice(-3).map((message) => ({
      label: message.sourceSubagentLabel,
      relation: message.sourceSubagentRelation,
    }))).toEqual([
      { label: '旧任务', relation: 'earlier' },
      { label: '上一任务', relation: 'previous' },
      { label: '当前任务', relation: 'current' },
    ]);
    const withoutOrder = mergeRuntimeTaskInventory({ ...merged, canonicalRootTurnIds: undefined }, merged.agentTasks);
    expect(withoutOrder.messages.slice(-3).map(message => message.sourceSubagentRelation))
      .toEqual(['earlier', 'previous', 'current']);
  });
});

describe('capability catalog adapter', () => {
  it('projects Skill activation facts and the complete ready MCP tool detail', async () => {
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify({
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
    stubRuntimeFetch(fetchMock);

    const result = await new LocalHttpRuntimeAdapter().setUserSkillEnabled(
      '/Users/test/.agents/skills/review/SKILL.md',
      false,
    );

    expect(result.capabilities.skills).toMatchObject({
      configPath: '/Users/test/.pulsara/skills.yaml',
      items: [{ name: 'review', enabled: false }],
    });
    const [, init] = fetchMock.mock.calls[0]!;
    expect(JSON.parse(String(init?.body))).toEqual({
      path: '/Users/test/.agents/skills/review/SKILL.md',
      enabled: false,
    });
  });
});

describe('source text fidelity hard cut', () => {
  it('projects ordered canonical Text parts without exposing their storage JSON', async () => {
    const texts = ['', '第一段\n原换行', '', '最后一段🙂'];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(connectPayload([{
      entry_id: 'multipart-text', turn_id: 'turn-1', entry_sequence: '1',
      entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content: inlinePrompt(...texts),
    }])), { status: 200, headers: { 'Content-Type': 'application/json' } })));
    const projection = (await new LocalHttpRuntimeAdapter().connect('session-1')).current();
    expect(projection.messages[0].body).toBe(texts.join('\n'));
  });

  it.each(['history', 'queue'])('projects %s images as typed owner-scoped content', async (location) => {
    const content = {
      ...inlineContent(JSON.stringify({
        schema: 'pulsara.prompt/v2',
        parts: [{ type: 'text', text: '看图' }, {
          type: 'image', image_ref: 'image:aaaaaaaaaaaaaaaaaaaaaaaaaa', digest: `sha256:${'a'.repeat(64)}`, encoded_bytes: 4,
          media_type: 'image/png', width: 7, height: 5,
        }],
      })),
      media_type: 'application/vnd.pulsara.prompt+json', codec: 'utf-8',
    };
    const payload = location === 'history'
      ? connectPayload([{
        entry_id: 'image', turn_id: 'turn-1', entry_sequence: '1',
        entry_kind: 'USER_MESSAGE', scope_kind: 'ROOT', content,
      }])
      : connectPayload([], { prompt_queue: [{
        queue_item_id: 'queue-image', command_id: 'command-image', queue_sequence: '1',
        status: 'PENDING', delivery_mode: 'NEW_TURN', content,
      }] });
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(payload), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));
    const projection = (await new LocalHttpRuntimeAdapter().connect('session-1')).current();
    const projected = location === 'history'
      ? projection.messages[0]?.promptContent
      : projection.queuedPrompts[0]?.content;
    expect(projected).toEqual({ parts: [{ type: 'text', text: '看图' }, {
      type: 'image', source: 'canonical', imageRef: 'image:aaaaaaaaaaaaaaaaaaaaaaaaaa',
      digest: `sha256:${'a'.repeat(64)}`, encodedBytes: 4,
      mediaType: 'image/png', width: 7, height: 5, refOrdinal: 0,
      owner: location === 'history'
        ? { kind: 'entry', entryId: 'image' }
        : { kind: 'queue', queueItemId: 'queue-image' },
    }] });
    expect(location === 'history' ? projection.messages[0]?.body : undefined)
      .toBe(location === 'history' ? '看图' : undefined);
  });

  it('preserves accepted user prompts and steers byte-for-byte before rendering', async () => {
    const entries = ['USER_MESSAGE', 'USER_STEER'].map((entryKind, index) => ({
      entry_id: `user-${index}`, turn_id: 'turn-user', entry_sequence: String(index + 1),
      entry_kind: entryKind, scope_kind: 'ROOT', content: inlinePrompt(SOURCE_FIDELITY_TEXT),
    }));
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(connectPayload(entries)), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().messages.map((message) => ({
      body: message.body,
      userKind: message.userKind,
    }))).toEqual([
      { body: SOURCE_FIDELITY_TEXT, userKind: 'prompt' },
      { body: SOURCE_FIDELITY_TEXT, userKind: 'steer' },
    ]);
  });

  it('preserves canonical and imported assistant bodies byte-for-byte', async () => {
    const entries = ['CANONICAL', 'IMPORTED_HISTORY'].map((owner, index) => ({
      entry_id: `assistant-${index}`, turn_id: `turn-${index}`, entry_sequence: String(index + 1),
      entry_kind: 'ASSISTANT_MESSAGE', entry_owner_kind: owner, scope_kind: 'ROOT',
      root_final: true, fork_eligible: index === 0,
      blocks: [{
        block_id: `text-${index}`, block_kind: 'TEXT', content: inlineContent(SOURCE_FIDELITY_TEXT),
      }],
    }));
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(connectPayload(entries)), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(connection.current().messages.map((message) => message.body)).toEqual([
      SOURCE_FIDELITY_TEXT,
      SOURCE_FIDELITY_TEXT,
    ]);
    expect(connection.current().messages.map((message) => [message.rootFinal, message.forkEligible])).toEqual([
      [true, true],
      [true, false],
    ]);
  });

  it.each(['LIVE_GENERATION_ABORTED', 'LIVE_GENERATION_COMMITTED'])('does not retain a settled root preview without an exact committed entry: %s', async kind => {
    const responses = [connectPayload([], { active_turns: [{ turn_id: 'turn-1', scope_kind: 'ROOT', status: 'RUNNING' }] }, [{
      live_revision: '1', event_type: 'TEXT_DELTA', draft_identity: 'draft', turn_id: 'turn-1', scope_kind: 'ROOT',
      payload: { text_delta: { delta: 'advisory text' } },
    }]), { observation: { through_event_sequence: '1',
      committed: [{ projection_kind: 'CURRENT_CONTROL', current_control: { active_turns: [] } }],
      settlements: [{ kind, draft_identity: 'draft' }],
    } }];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), { status: 200 })));
    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    expect(connection.current().messages[0].body).toBe('advisory text');
    const settled = await connection.observe();
    expect(settled.messages).toHaveLength(0);
    expect(settled.isRunning).toBe(false);
  });

  it('keeps known tool card content while its attached canonical result body is being read', async () => {
    vi.stubGlobal('crypto', webcrypto);
    const output = 'canonical tool output';
    const reference = blobContent(output);
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const request = { entry_id: 'request', turn_id: 'turn-1', entry_sequence: '1',
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{ block_id: 'call-block', block_kind: 'TOOL_CALL', tool_call_id: 'call', tool_name: 'read_file' }] };
    const responses = [connectPayload([request], {
      active_turns: [{ turn_id: 'turn-1', scope_kind: 'ROOT', status: 'RUNNING' }],
      tool_attempts: [{ attempt_id: 'attempt', assistant_entry_id: 'request', tool_call_id: 'call' }],
    }, [{ live_revision: '1', event_type: 'TOOL_RESULT_DELTA', turn_id: 'turn-1', scope_kind: 'ROOT',
      channel_kind: 'TOOL_RESULT', channel_attempt_id: 'attempt', payload: { tool_result_delta: { text: 'visible streamed tool output' } } }]),
      { observation: { through_event_sequence: '2', settlements: [{ kind: 'LIVE_GENERATION_COMMITTED',
        channel_kind: 'LIVE_CHANNEL_TOOL_RESULT', channel_attempt_id: 'attempt', committed_entry_id: 'result' }],
        committed: [{ projection_kind: 'IMMUTABLE_ENTRY', entry: {
        entry_id: 'result', turn_id: 'turn-1', entry_sequence: '2', entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
        content: reference, tool_result: { assistant_entry_id: 'request', tool_call_id: 'call', result_state: 'SUCCESS' },
      } }] } }];
    stubRuntimeFetch(vi.fn(async input => {
      if (String(input).endsWith('/read-content')) {
        await gate;
        return new Response(JSON.stringify({ content: { digest: reference.digest, complete_size: reference.size,
          offset_bytes: '0', content: Buffer.from(output).toString('base64'), complete: true } }), { status: 200 });
      }
      return new Response(JSON.stringify(responses.shift()), { status: 200 });
    }));
    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    await vi.waitFor(() => expect(connection.current().messages[0].historyContextState).toBe('ready'));
    await connection.observe();
    expect(connection.current().messages[0]).toMatchObject({ contentState: 'unloaded',
      pendingAssistant: { traces: [{ id: 'call', status: 'completed', resultEntryId: 'result', resultText: 'visible streamed tool output' }] } });
    expect(connection.current().messages[0].traces?.[0].resultText).not.toBe('visible streamed tool output');
    const read = connection.hydrateEntries!(['result']);
    expect(connection.current().messages[0].contentState).toBe('loading');
    expect(connection.current().messages[0].pendingAssistant?.traces?.[0].id).toBe('call');
    release(); await read;
    expect(connection.current().messages[0].contentState).toBe('ready');
    expect(connection.current().messages[0]).not.toHaveProperty('pendingAssistant');
    expect(connection.current().messages[0].traces?.[0].resultText).toBe(output);
  });

  it('preserves split live deltas, final text, canonical snapshot, and reconnect content', async () => {
    const first = '\n中文 read_';
    const second = 'file /tmp/ROOT Kernel HostSession read-only bypass-permissions exit_code = 0\n';
    const source = first + second;
    const canonicalEntry = {
      entry_id: 'assistant-final', turn_id: 'turn-1', entry_sequence: '1',
      entry_kind: 'ASSISTANT_MESSAGE', scope_kind: 'ROOT',
      blocks: [{ block_id: 'text-final', block_kind: 'TEXT', content: inlineContent(source) }],
    };
    const responses = [
      connectPayload([], { active_turns: [{ turn_id: 'turn-1', status: 'RUNNING' }] }, [{
        live_revision: '1', event_type: 'TEXT_DELTA', draft_identity: 'draft-1',
        turn_id: 'turn-1', scope_kind: 'ROOT', payload: { text_delta: { delta: first } },
      }]),
      { observation: {
        through_event_sequence: '0', live_owner_epoch: '1', through_live_revision: '2',
        live: [{
          live_revision: '2', event_type: 'TEXT_DELTA', draft_identity: 'draft-1',
          turn_id: 'turn-1', scope_kind: 'ROOT', payload: { text_delta: { delta: second } },
        }],
      } },
      { observation: {
        through_event_sequence: '0', live_owner_epoch: '1', through_live_revision: '3',
        live: [{
          live_revision: '3', event_type: 'TEXT_END', draft_identity: 'draft-1',
          turn_id: 'turn-1', scope_kind: 'ROOT', payload: { text_end: { final_text: source } },
        }],
      } },
      { snapshot: { snapshot: {
        session_id: 'session-1', writer_generation: '1', event_sequence_cut: '1',
        entries: [canonicalEntry], control: {},
      } } },
      connectPayload([canonicalEntry]),
    ];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const adapter = new LocalHttpRuntimeAdapter();
    const connection = await adapter.connect('session-1');
    expect(connection.current().messages[0]?.body).toBe(first);
    expect((await connection.observe()).messages[0]?.body).toBe(source);
    expect((await connection.observe()).messages[0]?.body).toBe(source);
    expect((await connection.snapshot()).messages[0]?.body).toBe(source);
    expect((await adapter.connect('session-1')).current().messages[0]?.body).toBe(source);
  });

  it('preserves canonical and live reasoning while retaining presentation kinds', async () => {
    const entries = [{
      entry_id: 'assistant-canonical', turn_id: 'turn-canonical', entry_sequence: '1',
      entry_kind: 'ASSISTANT_MESSAGE', scope_kind: 'ROOT',
      blocks: [{ block_id: 'text', block_kind: 'TEXT', content: inlineContent('final') }],
      reasoning_blocks: [{
        block_id: 'reasoning-canonical', ordinal: '0',
        presentation_kind: 'REASONING_PRESENTATION_FULL',
        content: inlineContent(SOURCE_FIDELITY_TEXT),
      }],
    }];
    const liveEvents = [{
      live_revision: '1', event_type: 'THINKING_START', draft_identity: 'draft-live',
      turn_id: 'turn-live', scope_kind: 'ROOT', block_id: 'reasoning-live',
      payload: { thinking_start: {
        block_identity: 'reasoning-live',
        presentation_kind: 'REASONING_PRESENTATION_SUMMARY',
      } },
    }, {
      live_revision: '2', event_type: 'THINKING_DELTA', draft_identity: 'draft-live',
      turn_id: 'turn-live', scope_kind: 'ROOT', block_id: 'reasoning-live',
      payload: { thinking_delta: { block_identity: 'reasoning-live', delta: SOURCE_FIDELITY_TEXT } },
    }];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(connectPayload(
      entries,
      { active_turns: [{ turn_id: 'turn-live', status: 'RUNNING' }] },
      liveEvents,
    )), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const messages = (await new LocalHttpRuntimeAdapter().connect('session-1')).current().messages;

    expect(messages.find((message) => message.id === 'assistant-canonical')?.reasoning).toEqual([
      { id: 'reasoning-canonical', kind: 'full', body: SOURCE_FIDELITY_TEXT },
    ]);
    expect(messages.find((message) => message.id === 'live:draft-live')?.reasoning).toEqual([
      { id: 'reasoning-live', kind: 'summary', body: SOURCE_FIDELITY_TEXT, active: true },
    ]);
  });

  it('preserves plan questions and option text without changing option ordinals', async () => {
    const interaction = {
      id: 'plan-question-1', kind: 'plan-question' as const,
      workflowId: 'workflow-1', workflowRevision: 7,
    };
    const responses = [connectPayload([], {
      active_plan_workflow: { workflow_id: 'workflow-1', workflow_revision: '7' },
      open_plan_interaction: { interaction_id: interaction.id, workflow_id: 'workflow-1', kind: 'QUESTION' },
    }), {
      plan_question: {
        interaction_id: interaction.id,
        question: SOURCE_FIDELITY_TEXT,
        options: [{
          ordinal: '9', label: SOURCE_FIDELITY_TEXT,
          description: SOURCE_FIDELITY_TEXT, recommended: true,
        }],
        allow_free_text: true,
      },
    }];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(await connection.readInteraction(interaction)).toEqual({
      kind: 'plan-question',
      question: SOURCE_FIDELITY_TEXT,
      options: [{
        ordinal: 9, label: SOURCE_FIDELITY_TEXT,
        description: SOURCE_FIDELITY_TEXT, recommended: true,
      }],
      allowFreeText: true,
    });
  });

  it('joins a multi-chunk UTF-8 plan draft exactly while retaining digest and offset checks', async () => {
    const interaction = {
      id: 'plan-draft-1', kind: 'plan-draft' as const,
      workflowId: 'workflow-1', workflowRevision: 7,
    };
    const split = SOURCE_FIDELITY_TEXT.indexOf('中文') + 1;
    const first = SOURCE_FIDELITY_TEXT.slice(0, split);
    const second = SOURCE_FIDELITY_TEXT.slice(split);
    const firstBytes = new TextEncoder().encode(first).length;
    const requests: Record<string, unknown>[] = [];
    const responses = [connectPayload([], {
      active_plan_workflow: { workflow_id: 'workflow-1', workflow_revision: '7' },
      open_plan_interaction: { interaction_id: interaction.id, workflow_id: 'workflow-1', kind: 'DRAFT_REVIEW' },
    }), {
      plan_draft: {
        interaction_id: interaction.id, plan_utf8_digest: 'sha256:plan',
        offset_utf8_bytes: 0, body: first, next_offset_utf8_bytes: firstBytes, eof: false,
      },
    }, {
      plan_draft: {
        interaction_id: interaction.id, plan_utf8_digest: 'sha256:plan',
        offset_utf8_bytes: firstBytes, body: second,
        next_offset_utf8_bytes: new TextEncoder().encode(SOURCE_FIDELITY_TEXT).length, eof: true,
      },
    }];
    stubRuntimeFetch(vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.body) requests.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      return new Response(JSON.stringify(responses.shift()), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }));

    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    expect(await connection.readInteraction(interaction)).toEqual({
      kind: 'plan-draft', body: SOURCE_FIDELITY_TEXT,
    });
    expect(requests.at(-1)).toMatchObject({
      interaction_id: interaction.id,
      offset_utf8_bytes: firstBytes,
      expected_plan_utf8_digest: 'sha256:plan',
    });
  });

  it('still rejects a non-advancing plan draft chunk', async () => {
    const interaction = {
      id: 'plan-draft-invalid', kind: 'plan-draft' as const,
      workflowId: 'workflow-1', workflowRevision: 1,
    };
    const responses = [connectPayload(), {
      plan_draft: {
        interaction_id: interaction.id, plan_utf8_digest: 'sha256:plan',
        offset_utf8_bytes: 0, body: 'partial', next_offset_utf8_bytes: 0, eof: false,
      },
    }];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));
    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');

    await expect(connection.readInteraction(interaction)).rejects.toMatchObject({
      code: 'PLAN_DRAFT_INVALID',
    });
  });

  it('preserves task inventory, dependencies, progress, TODOs, results, and child activities', async () => {
    historyOwnerFixture = {tasks:[{task_id:'task-1',parent_turn_id:'turn-root',parent_initial_entry_id:'root-create',batch_id:'batch-1'}],batches:[{batch_id:'batch-1',parent_turn_id:'turn-root',creator_entry_id:'root-create',creator_tool_call_id:'create-call',task_count:1}]};
    const visualizations = [{ordinal:0,state:'READY',content_digest:'sha256:'+'a'.repeat(64),content_size:16,media_type:'image/png',codec:'binary',image_ref:'image:'+'a'.repeat(26),width:7,height:5}];
    const task = {
      id: 'task-1', parent_turn_id: 'turn-root', batch_id: 'batch-1', task_key: 'source-task',
      label: SOURCE_FIDELITY_TEXT, profile: 'research_worker', display_role: SOURCE_FIDELITY_TEXT,
      context: { mode: 'LAST_N', last_n_turns: 3 }, objective: SOURCE_FIDELITY_TEXT,
      status: 'ACTIVE', terminal_public_detail: SOURCE_FIDELITY_TEXT, completion_accepted: false,
      dependencies: [{
        task_id: 'task-dependency', task_key: 'dependency', label: SOURCE_FIDELITY_TEXT,
        status: 'COMPLETED', result_summary: SOURCE_FIDELITY_TEXT,
      }],
      result: {
        id: 'result-1', entry_id: 'entry-result', summary: SOURCE_FIDELITY_TEXT,
        output_preview: SOURCE_FIDELITY_TEXT, diagnostics: [],
      },
    };
    const rootToolRequest = {
      entry_id: 'root-create', turn_id: 'turn-root', entry_sequence: '1',
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{
        block_id: 'create-block', block_kind: 'TOOL_CALL', tool_call_id: 'create-call',
        tool_name: 'spawn_agent', tool_arguments_preview: btoa('{}'),
      }],
    };
    const childEntries = [{
      entry_id: 'child-guidance', turn_id: 'turn-child', entry_sequence: '2',
      entry_kind: 'INTER_AGENT_MESSAGE', scope_kind: 'SUBAGENT_TASK',
      scope_subagent_task_id: 'task-1', content: inlineContent(SOURCE_FIDELITY_TEXT),
    }, {
      visualizations, entry_id: 'child-answer', turn_id: 'turn-child', entry_sequence: '3',
      entry_kind: 'ASSISTANT_MESSAGE', scope_kind: 'SUBAGENT_TASK',
      scope_subagent_task_id: 'task-1',
      blocks: [{ block_id: 'child-text', block_kind: 'TEXT', content: inlineContent(SOURCE_FIDELITY_TEXT) }],
      reasoning_blocks: [{
        block_id: 'child-reasoning', ordinal: '0',
        presentation_kind: 'REASONING_PRESENTATION_SUMMARY', content: inlineContent(SOURCE_FIDELITY_TEXT),
      }],
    }];
    const connect = connectPayload(
      [rootToolRequest, ...childEntries],
      { subagent_tasks: [{
        task_id: 'task-1', parent_turn_id: 'turn-root', status: 'ACTIVE',
        label: SOURCE_FIDELITY_TEXT, display_role: SOURCE_FIDELITY_TEXT,
        profile: 'research_worker', objective: SOURCE_FIDELITY_TEXT,
        terminal_public_detail: SOURCE_FIDELITY_TEXT, result_summary: SOURCE_FIDELITY_TEXT,
      }, {
        task_id: 'task-default-role', parent_turn_id: 'turn-root', status: 'PENDING_START',
        label: 'default', profile: 'research_worker', objective: 'default',
      }] },
      [{
        live_revision: '1', event_type: 'SUBAGENT_PROGRESS', turn_id: 'turn-child',
        scope_kind: 'SUBAGENT_TASK', scope_subagent_task_id: 'task-1',
        payload: { subagent_progress: {
          task_id: 'task-1', status: 'ACTIVE', public_summary: SOURCE_FIDELITY_TEXT,
        } },
      }],
      { current_todos: [{
        todo_run_id: 'todo-root', scope_kind: 'ROOT', disposition: 'ACTIVE',
        ordered_items: [{ ordinal: 0, text: SOURCE_FIDELITY_TEXT, status: 'IN_PROGRESS' }],
      }] },
    );
    stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(
      String(input).includes('/activities?')
        ? { activities: [{ entry_id: 'child-guidance', turn_id: 'turn-child', entry_sequence: '2', entry_kind: 'INTER_AGENT_MESSAGE', accepted_at: '2026-09-01T00:00:00Z', objective: SOURCE_FIDELITY_TEXT, content: inlineContent(SOURCE_FIDELITY_TEXT) }, {...childEntries[1], content:inlineContent(SOURCE_FIDELITY_TEXT), accepted_at:'2026-09-01T00:00:00Z', inherited:true, turn_status:'COMPLETED'}] }
        : String(input).includes('/tasks?')
          ? { session_id: 'session-1', tasks: [task], total_count: 1, remaining_count: 0 }
          : connect,
    ), { status: 200, headers: { 'Content-Type': 'application/json' } })));

    const adapter = new LocalHttpRuntimeAdapter();
    const inventory = (await adapter.listSessionTasks('session-1')).tasks[0];
    const connection = await adapter.connect('session-1');
    await vi.waitFor(() => expect(connection.current().history?.windows.every(window=>window.contextState==='ready')).toBe(true));
    const projection = mergeRuntimeTaskInventory(connection.current(), [inventory]);
    const liveTask = projection.agentTasks.find((item) => item.id === 'task-1');
    const activityPage = await adapter.listSessionTaskActivities('session-1', 'task-1');
    const run = projection.messages.find((message) => message.id === 'root-create')?.subagentRuns
      ?.find((item) => item.id === 'task-1');

    expect(inventory).toMatchObject({
      label: SOURCE_FIDELITY_TEXT, role: SOURCE_FIDELITY_TEXT, objective: SOURCE_FIDELITY_TEXT,
      terminalPublicDetail: SOURCE_FIDELITY_TEXT, summary: SOURCE_FIDELITY_TEXT,
      dependencies: [{ label: SOURCE_FIDELITY_TEXT, resultSummary: SOURCE_FIDELITY_TEXT }],
      result: { summary: SOURCE_FIDELITY_TEXT, outputPreview: SOURCE_FIDELITY_TEXT },
    });
    expect(liveTask).toMatchObject({
      label: SOURCE_FIDELITY_TEXT, role: SOURCE_FIDELITY_TEXT, objective: SOURCE_FIDELITY_TEXT,
      terminalPublicDetail: SOURCE_FIDELITY_TEXT, summary: SOURCE_FIDELITY_TEXT,
    });
    expect(activityPage.activities[0]).toMatchObject({ body: SOURCE_FIDELITY_TEXT, entryKind: 'INTER_AGENT_MESSAGE' });
    expect(run?.activities.find(item => item.id === 'child-answer')?.visualizations).toMatchObject([{imageRef:visualizations[0].image_ref}]);
    const cold = projectAgentTaskConversation(activityPage.activities);
    expect(cold.find(item => item.id === 'child-answer')).toMatchObject({visualizations:[{imageRef:visualizations[0].image_ref,width:7,height:5}]});
    expect(projection.todo?.items).toEqual([{
      id: 'todo-root:0', label: SOURCE_FIDELITY_TEXT, status: 'in-progress',
    }]);
    expect(run).toMatchObject({
      label: SOURCE_FIDELITY_TEXT, role: SOURCE_FIDELITY_TEXT,
      objective: SOURCE_FIDELITY_TEXT, summary: SOURCE_FIDELITY_TEXT,
    });
    expect(liveTask?.progress).toBe(SOURCE_FIDELITY_TEXT);
    expect(run?.activities).toMatchObject([
      {id: 'child-guidance', turnId: 'turn-child', body: SOURCE_FIDELITY_TEXT},
      {id: 'child-answer', turnId: 'turn-child', body: SOURCE_FIDELITY_TEXT, reasoning: [{body: SOURCE_FIDELITY_TEXT}]},
    ]);
    expect(projectAgentTaskConversation([], run?.activities).find(message => message.id === 'child-answer'))
      .toMatchObject({ turnId: 'turn-child', body: SOURCE_FIDELITY_TEXT });
    expect(projectAgentTaskConversation([], run?.activities).find(message => message.id === 'child-guidance'))
      .toMatchObject({ role: 'user', userKind: 'steer', turnId: 'turn-child', body: SOURCE_FIDELITY_TEXT });
  });

  it('uses task API public content when root history still holds an unloaded blob', async () => {
    const full = '任务接口已读出的完整最终回复。';
    const entry = { entry_id: 'blob-child-answer', turn_id: 'turn-child', entry_sequence: '2',
      entry_kind: 'ASSISTANT_MESSAGE', scope_kind: 'SUBAGENT_TASK', scope_subagent_task_id: 'task-1',
      content: blobContent(full), accepted_at: '2026-10-21T00:00:00Z' };
    const connect = connectPayload([entry], { subagent_tasks: [{ task_id: 'task-1', parent_turn_id: 'turn-root',
      status: 'COMPLETED', label: '审阅', objective: '检查输出', profile: 'research_worker' }] });
    stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(
      String(input).includes('/activities?')
        ? { activities: [{ ...entry, content: { ...blobContent(full), ...inlineContent(full), kind: 'CANONICAL_BLOB' }, turn_status: 'COMPLETED' }] }
        : connect,
    ), { status: 200, headers: { 'Content-Type': 'application/json' } })));
    const adapter = new LocalHttpRuntimeAdapter();
    const connection = await adapter.connect('session-1');
    await vi.waitFor(() => expect(connection.current().history?.windows.every(window => window.contextState === 'ready')).toBe(true));
    const activities = connection.current().subagentRuns?.find(run => run.id === 'task-1')?.activities;
    expect(activities?.find(activity => activity.id === entry.entry_id)).toMatchObject({ body: '', contentState: 'unloaded' });
    const page = await adapter.listSessionTaskActivities('session-1', 'task-1');
    expect(page.activities[0].body).toBe(full);
    expect(projectAgentTaskConversation(page.activities, activities).find(message => message.id === entry.entry_id))
      .toMatchObject({ body: full, role: 'assistant', assistantKind: 'terminal' });
  });

  it('preserves plain and JSON message tool text while keeping typed tool labels', async () => {
    const request = (id: string, sequence: number, call: string, toolName = 'read_file') => ({
      entry_id: id, turn_id: 'turn-1', entry_sequence: String(sequence),
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{
        block_id: `${id}-block`, block_kind: 'TOOL_CALL', tool_call_id: call,
        tool_name: toolName, tool_arguments_preview: btoa('{}'),
      }],
    });
    const jsonMessage = JSON.stringify({ message: SOURCE_FIDELITY_TEXT });
    const editDiff = '@@ -1,2 +1,2 @@\n-old-001\n+new-001\n unchanged';
    const editResult = JSON.stringify({ status: 'success', diff: editDiff });
    const entries = [
      request('plain-request', 1, 'plain-call'),
      {
        entry_id: 'plain-result', turn_id: 'turn-1', entry_sequence: '2',
        entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT', content: inlineContent(SOURCE_FIDELITY_TEXT),
        tool_result: { assistant_entry_id: 'plain-request', tool_call_id: 'plain-call', result_state: 'SUCCESS' },
      },
      request('json-request', 3, 'json-call'),
      {
        entry_id: 'json-result', turn_id: 'turn-1', entry_sequence: '4',
        entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT', content: inlineContent(jsonMessage),
        tool_result: { assistant_entry_id: 'json-request', tool_call_id: 'json-call', result_state: 'SUCCESS' },
      },
      request('edit-request', 5, 'edit-call', 'edit_file'),
      {
        entry_id: 'edit-result', turn_id: 'turn-1', entry_sequence: '6',
        entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT', content: inlineContent(editResult),
        tool_result: { assistant_entry_id: 'edit-request', tool_call_id: 'edit-call', result_state: 'SUCCESS' },
      },
    ];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(connectPayload(entries)), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const messages = (await new LocalHttpRuntimeAdapter().connect('session-1')).current().messages;
    const plain = messages.find((message) => message.id === 'plain-request')?.traces?.[0];
    const json = messages.find((message) => message.id === 'json-request')?.traces?.[0];
    const edit = messages.find((message) => message.id === 'edit-request')?.traces?.[0];

    expect(plain).toMatchObject({ title: '读取文件', resultText: SOURCE_FIDELITY_TEXT });
    expect(json).toMatchObject({ title: '读取文件', resultText: jsonMessage });
    expect(edit).toMatchObject({ title: '更新文件', resultText: editResult, resultSummary: editDiff });
    expect(plain).not.toHaveProperty('output');
    expect(json).not.toHaveProperty('output');
  });

  it('does not project terminal_process pending-exit placeholders as real exit codes', async () => {
    const entries = [{
      entry_id: 'process-request', turn_id: 'turn-process', entry_sequence: '1',
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{
        block_id: 'process-block', block_kind: 'TOOL_CALL', tool_call_id: 'process-call',
        tool_name: 'terminal_process',
        tool_arguments_preview: btoa(JSON.stringify({ action: 'poll', process_id: 'process-a' })),
      }],
    }, {
      entry_id: 'process-result', turn_id: 'turn-process', entry_sequence: '2',
      entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT',
      content: inlineContent(JSON.stringify({ status: 'running', output: '', exit_code: -1 })),
      tool_result: {
        assistant_entry_id: 'process-request', tool_call_id: 'process-call', result_state: 'SUCCESS',
      },
    }];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(connectPayload(entries)), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const messages = (await new LocalHttpRuntimeAdapter().connect('session-1')).current().messages;
    const summary = messages.find((message) => message.id === 'process-request')?.traces?.[0].resultSummary;
    expect(summary).toBe('命令仍在运行。');
    expect(summary).not.toContain('-1');
  });

  it('applies filesystem result summaries only to exact builtin tool names', async () => {
    const request = (id: string, sequence: number, call: string, toolName: string) => ({
      entry_id: id, turn_id: 'turn-tools', entry_sequence: String(sequence),
      entry_kind: 'ASSISTANT_TOOL_REQUEST', scope_kind: 'ROOT',
      blocks: [{
        block_id: `${id}-block`, block_kind: 'TOOL_CALL', tool_call_id: call,
        tool_name: toolName, tool_arguments_preview: btoa('{}'),
      }],
    });
    const result = (
      id: string,
      sequence: number,
      assistantEntryId: string,
      toolCallId: string,
      value: Record<string, unknown>,
    ) => ({
      entry_id: id, turn_id: 'turn-tools', entry_sequence: String(sequence),
      entry_kind: 'TOOL_RESULT', scope_kind: 'ROOT', content: inlineContent(JSON.stringify(value)),
      tool_result: {
        assistant_entry_id: assistantEntryId, tool_call_id: toolCallId, result_state: 'SUCCESS',
      },
    });
    const entries = [
      request('read-request', 1, 'read-call', 'read_file'),
      result('read-result', 2, 'read-request', 'read-call', {
        status: 'ok', target: {kind:'PATH', path:'notes.txt'}, offset: 20, limit: 2, total_lines: 100,
        truncated: true, content: '20|甲\n21|乙',
      }),
      request('write-request', 3, 'write-call', 'write_file'),
      result('write-result', 4, 'write-request', 'write-call', {
        status: 'ok', target: {kind:'PATH', path:'created.txt'}, bytes_written: 9,
      }),
      request('mcp-request', 5, 'mcp-call', 'mcp__external__report'),
      result('mcp-result', 6, 'mcp-request', 'mcp-call', {
        status: 'success', path: 'remote.txt', bytes_written: 99,
        total_lines: 800, diff: '@@ remote data, not a filesystem edit @@',
      }),
    ];
    stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(connectPayload(entries)), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })));

    const messages = (await new LocalHttpRuntimeAdapter().connect('session-1')).current().messages;
    expect(messages.find((message) => message.id === 'read-request')?.traces?.[0].resultSummary)
      .toBe('notes.txt · 返回第 20–21 行 · 文件共 100 行 · 还有后续内容');
    expect(messages.find((message) => message.id === 'write-request')?.traces?.[0].resultSummary)
      .toBe('已创建 created.txt · 9 字节');
    expect(messages.find((message) => message.id === 'mcp-request')?.traces?.[0].resultSummary)
      .toBeUndefined();
  });
});

it('PR05 projects the original deadline/busy view and sends only the app command', async () => {
  const deadline = '2099-01-01T00:00:00+00:00';
  const responses = [connectPayload([], {}, [], {
    owner_epoch: '7', live_revision: '12', current_interaction: {
      interaction_id: 'interaction:original', interaction_kind: 'TOOL_CONFIRMATION',
      public_prompt: 'Allow terminal?', public_options: ['ALLOW', 'DENY'],
      expires_at_utc: deadline, decision_in_progress: true,
    },
  }), { command_outcome: { command_id: 'command:original', status: 'SUCCEEDED', public_code: 'INTERACTION_ALLOW' } }];
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(responses.shift()), { status: 200 }));
  stubRuntimeFetch(fetcher);
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  const interaction = connection.current().interaction!;
  expect(interaction).toEqual({ id: 'interaction:original', kind: 'tool-confirmation',
    prompt: 'Allow terminal?', options: ['ALLOW', 'DENY'], expiresAtUtc: deadline, decisionInProgress: true });
  await connection.resolveInteraction(interaction, { kind: 'tool', decision: 'allow', commandId: 'command:original' });
  const body = JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body));
  expect(body.command_id).toBe('command:original');
  expect(body.interaction_id).toBe('interaction:original');
  expect(body.expected_live_revision).toBe(12);
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it('PR05 preserves explicit confirmed non-acceptance from the protocol', async () => {
  const responses = [connectPayload([], {}, [], {
    owner_epoch: '7', live_revision: '12', current_interaction: {
      interaction_id: 'interaction:original', interaction_kind: 'TOOL_CONFIRMATION',
      public_prompt: 'Allow terminal?', public_options: ['ALLOW', 'DENY'],
      expires_at_utc: '2099-01-01T00:00:00Z', decision_in_progress: false,
    },
  }), {error: {stable_code: 'INTERACTION_NOT_ACCEPTED', public_message: 'confirmed not accepted'}}];
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(responses.shift()), {status: 200}));
  stubRuntimeFetch(fetcher);
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  await expect(connection.resolveInteraction(connection.current().interaction!, {
    kind: 'tool', decision: 'allow', commandId: 'command:original',
  })).rejects.toMatchObject({code: 'INTERACTION_NOT_ACCEPTED', retryable: false});
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it.each([undefined, 'interaction:expired', 'unrecognized-reason'])('PR05 removes the old slot and explains only the observed close reason: %s', async (reason) => {
  const responses = [connectPayload([], {}, [], {
    current_interaction: {
      interaction_id: 'interaction:original', interaction_kind: 'TOOL_CONFIRMATION',
      public_prompt: 'Allow terminal?', expires_at_utc: '2000-01-01T00:00:00Z',
    },
  }), { observation: {
    live: reason ? [{ event_type: 'INTERACTION_CLOSED', payload: {
      interaction_closed: { interaction_id: 'interaction:original', reason },
    } }] : [],
    live_control: [{ kind: 'LIVE_INTERACTION_CLOSED' }],
  } }, { observation: {} }];
  stubRuntimeFetch(vi.fn<typeof fetch>(async () => new Response(JSON.stringify(responses.shift()), { status: 200 })));
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  expect(connection.current().interaction?.id).toBe('interaction:original');
  const closed = await connection.observe();
  expect(closed.interaction).toBeUndefined();
  expect(closed.presentationNotices).toEqual([reason === 'interaction:expired'
    ? '确认已过期，本次操作未获授权。' : '这项确认已结束；原因暂不可确认。']);
  expect((await connection.observe()).presentationNotices).toEqual([]);
});

it.each([
  ['SUBMITTED', []],
  ['CANCELLED', ['已取消这次配置。']],
  ['unknown-form-reason', ['这项确认已结束；原因暂不可确认。']],
])('closes a capability form using its observed outcome: %s', async (reason, notices) => {
  const responses = [connectPayload([], {}, [], {
    current_interaction: {
      interaction_id: 'interaction:form', interaction_kind: 'CAPABILITY_FORM',
      public_prompt: 'Review Hook definitions', public_options: ['SUBMIT', 'CANCEL'],
    },
  }), { observation: {
    live: [{ event_type: 'INTERACTION_CLOSED', payload: {
      interaction_closed: { interaction_id: 'interaction:form', reason },
    } }],
    live_control: [{ kind: 'LIVE_INTERACTION_CLOSED' }],
  } }];
  stubRuntimeFetch(vi.fn<typeof fetch>(async () => new Response(JSON.stringify(responses.shift()), { status: 200 })));
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  expect(connection.current().interaction?.kind).toBe('capability-form');
  const closed = await connection.observe();
  expect(closed.interaction).toBeUndefined();
  expect(closed.presentationNotices).toEqual(notices);
  expect(closed.messages).toEqual([]);
});

it('imports raw file bytes outside JSON commands using current controller generation', async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  stubRuntimeFetch(vi.fn(async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(String(url).endsWith('/connections') ? connectPayload()
      : { path: '/tmp/imported/测试.pdf', name: '测试.pdf', bytes: 3 }), { status: 200 });
  }));
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  const file = new File(['pdf'], '测试.pdf', { type: 'application/pdf' });
  const signal = new AbortController().signal;
  await connection.importFiles([file], signal);
  const single = calls.at(-1)!;
  expect(single.url).toContain('/api/connections/connection-fidelity/import-file');
  expect(single.init?.body).toBe(file);
  expect(single.init?.signal).toBe(signal);
  const headers = single.init?.headers as Record<string, string>;
  expect(headers['Content-Type']).toBe('application/octet-stream');
  expect(headers['X-Pulsara-Connection-Generation']).toBe('1');
  expect(JSON.parse(headers['X-Pulsara-Filename'])).toBe('测试.pdf');
  expect(headers['X-Pulsara-Filename']).toMatch(/^[\x20-\x7e]+$/);

});

it('lists cwd path candidates for the selected session with paging and cancellation', async () => {
  const result = { directory: '/workspace/文档', items: [], next_cursor: 'report.pdf' };
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(result), { status: 200 }));
  stubRuntimeFetch(fetchMock);
  const controller = new AbortController();
  expect(await new LocalHttpRuntimeAdapter().completeWorkspacePaths('session/selected', '文档/', 'prev.pdf', controller.signal)).toEqual(result);
  expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session%2Fselected/path-candidates', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ prefix: '文档/', cursor: 'prev.pdf' }), signal: controller.signal, credentials: 'same-origin',
  }));
});

describe('annotation source history ownership', () => {
  const source = (id: string, body = 'source') => ({
    entry_id: id, entry_sequence: '2', entry_kind: 'ASSISTANT_MESSAGE', turn_id: 'turn-source',
    blocks: [{ block_id: `block-${id}`, block_kind: 'TEXT', block_ordinal: 0, content: inlineContent(body) }],
  });
  async function windowedConnection() {
    stubRuntimeFetch(vi.fn().mockResolvedValue(new Response(JSON.stringify(connectPayload()))));
    const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
    return connection;
  }
  it('reads the exact source window, reuses loaded content, and reports a missing anchor', async () => {
    const connection = await windowedConnection();
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({session_id:'session-1',cut_sequence:'3',event_sequence_cut:'3',entries:[source('wanted')]}))).mockResolvedValueOnce(new Response(JSON.stringify({session_id:'session-1',cut_sequence:'3',event_sequence_cut:'3',entries:[]})));
    stubRuntimeFetch(fetchMock);
    const signal = new AbortController().signal;
    expect((await connection.locateAnnotationSource!('wanted', signal)).messages.some(message => message.body === 'source')).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1/history/window',expect.objectContaining({body:JSON.stringify({anchor_entry_id:'wanted',maximum_entries:128,maximum_serialized_bytes:2<<20})}));
    await connection.locateAnnotationSource!('wanted', signal);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await expect(connection.locateAnnotationSource!('missing', signal)).rejects.toMatchObject({code:'HISTORY_ANCHOR_MISSING'});
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
  it('allows a replacement lookup to continue after its predecessor was canceled', async () => {
    const connection = await windowedConnection();
    const a = new AbortController(); const b = new AbortController();
    const fetchMock = vi.fn().mockImplementationOnce((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal!.addEventListener('abort', () => reject(init.signal!.reason), { once: true });
    })).mockResolvedValueOnce(new Response(JSON.stringify({session_id:'session-1',cut_sequence:'3',event_sequence_cut:'3',entries:[source('b')]})));
    stubRuntimeFetch(fetchMock);
    const canceled = connection.locateAnnotationSource!('a', a.signal).catch(error => error.name);
    a.abort();
    const replacement = connection.locateAnnotationSource!('b', b.signal);
    expect(await canceled).toBe('AbortError');
    expect((await replacement).messages.some(message => message.body === 'source')).toBe(true);
    expect(b.signal.aborted).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
  it('cancels a locator while shared multi-chunk hydration completes without installing partial bytes', async () => {
    const connection = await windowedConnection(); const controller = new AbortController();
    const entry = source('wanted'); const bytes = new TextEncoder().encode('完整来源');
    const blobEntry = { ...entry, blocks: [{ ...entry.blocks[0], content: blobBytes(bytes) }] };
    let release!: (response: Response) => void;
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({session_id:'session-1',cut_sequence:'3',event_sequence_cut:'3',entries:[blobEntry]})))
      .mockResolvedValueOnce(new Response(JSON.stringify({content:{digest:sha256Digest(bytes),complete_size:bytes.length,offset_bytes:0,content:Buffer.from(bytes.subarray(0,3)).toString('base64'),complete:false}})))
      .mockImplementationOnce(() => new Promise<Response>(resolve => {release=resolve;}));
    stubRuntimeFetch(fetchMock);
    const pending = connection.locateAnnotationSource!('wanted',controller.signal);
    await vi.waitFor(()=>expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(connection.current().messages[0].body).toBe('');
    controller.abort();
    await expect(pending).rejects.toMatchObject({name:'AbortError'});
    const retry = connection.locateAnnotationSource!('wanted',new AbortController().signal);
    release(new Response(JSON.stringify({content:{digest:sha256Digest(bytes),complete_size:bytes.length,offset_bytes:3,content:Buffer.from(bytes.subarray(3)).toString('base64'),complete:true}})));
    expect((await retry).messages[0]).toMatchObject({body:'完整来源',contentState:'ready'});
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

it('does not let a pre-rename session list response overwrite a confirmed title', async () => {
  let deliverOld!: (response: Response) => void;
  const fetchMock = vi.fn()
    .mockImplementationOnce(() => new Promise<Response>(resolve => { deliverOld = resolve; }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ session_id: 'session:one', title: 'new title' })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ sessions: [{ id: 'session:one', title: 'new title' }] })));
  stubRuntimeFetch(fetchMock);
  const adapter = new LocalHttpRuntimeAdapter();
  const pending = adapter.listSessions();
  await adapter.renameSession('session:one', 'new title');
  deliverOld(new Response(JSON.stringify({ sessions: [{ id: 'session:one', title: 'old title' }] })));
  expect((await pending)[0].title).toBe('new title');
  expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/sessions/session%3Aone/title', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({ title: 'new title' }),
  }));
});

it('searches sessions through the scoped paginated endpoint with cancellation', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [{
    session: { id: 'session:one', title: '自定义标题', lifecycle: 'ARCHIVED', workspace_id: 'workspace:one' },
    match_kind: 'assistant', snippet: '中间 commentary 命中',
  }], next_cursor: 'page-two' })));
  stubRuntimeFetch(fetchMock);
  const signal = new AbortController().signal;
  const result = await new LocalHttpRuntimeAdapter().searchSessions('中文', 'ARCHIVED', 'page-one', signal);
  expect(fetchMock).toHaveBeenCalledWith('/api/sessions/search', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ query: '中文', lifecycle: 'ARCHIVED', cursor: 'page-one' }), signal,
  }));
  expect(result.nextCursor).toBe('page-two');
  expect(result.items[0]).toMatchObject({ session: { id: 'session:one', title: '自定义标题', lifecycle: 'ARCHIVED' }, matchKind: 'assistant', snippet: '中间 commentary 命中' });
});


it.each(['ALL', 'OPEN', 'ARCHIVED'] as const)('requests five recent sessions within %s for an empty query', async lifecycle => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], next_cursor: null })));
  stubRuntimeFetch(fetchMock);
  await new LocalHttpRuntimeAdapter().searchSessions('  ', lifecycle);
  expect(fetchMock).toHaveBeenCalledWith('/api/sessions/search', expect.objectContaining({
    body: JSON.stringify({ query: '  ', lifecycle, limit: 5 }),
  }));
});

it.each(['live', 'history'])('keeps the current write owner when an earlier %s connect returns late', async (kind) => {
  let release!: (response: Response) => void;
  const delayed = new Promise<Response>(resolve => { release = resolve; });
  const live = (id: string) => {
    const payload = connectPayload();
    payload.session_id = id; payload.connection_id = `connection-${id}`;
    payload.snapshot.snapshot.session_id = id;
    return payload;
  };
  const writes: string[] = [];
  stubRuntimeFetch(vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === '/api/sessions/A/connections') return delayed;
    if (url === '/api/sessions/B/connections') return new Response(JSON.stringify(live('B')));
    if (url.endsWith('/model-call-binding')) {
      writes.push(new Headers(init?.headers).get('X-Pulsara-Connection-Id') ?? '');
      return new Response(JSON.stringify({model_call_binding:{connection_id:'model', reasoning:null}, reasoning_preference_reset:false}));
    }
    if (init?.method === 'DELETE') return new Response('{}');
    throw new Error(`Unexpected ${url}`);
  }));
  const adapter = new LocalHttpRuntimeAdapter();
  const pending = adapter.connect('A');
  await adapter.connect('B');
  release(new Response(JSON.stringify(kind === 'live' ? live('A') : {
    view:'history', session_id:'A', workspace_availability:{outcome:'MISSING', path:'/tmp/A'},
    snapshot:{snapshot:{snapshot:{session_id:'A', entries:[], control:{}, event_sequence_cut:'0'}}},
  })));
  await (await pending).close();
  await adapter.updateModelCallBinding('B', {connection_id:'model', reasoning:null});
  expect(writes).toEqual(['connection-B']);
});

it('retains deduplicated interruption history across continued turns and an older snapshot', async () => {
  const notice={owner_kind:'EXECUTED_TURN',owner_id:'old-turn',reason:'USER_STOPPED',terminal_at_utc:'2026-10-05T00:00:00Z',display_after_entry_sequence:'1'};
  const oldEntry={entry_id:'old-input',turn_id:'old-turn',entry_sequence:'1',entry_kind:'USER_MESSAGE',content:inlinePrompt('stopped question')};
  const snapshot={session_id:'session-1',event_sequence_cut:'2',entries:[oldEntry],control:{}};
  const responses=[{connection_id:'connection-1',workspace_availability: connectedWorkspaceAvailability, connection_generation:1,session_id:'session-1',role:'observer',
    live_hello:{live_owner_epoch:'1',live_revision:'0',live_snapshot:{}},snapshot:{snapshot},live_control_snapshot:{snapshot:{}}},
    {observation:{through_event_sequence:'3',committed:[{projection_kind:'CURRENT_CONTROL',interruption_notice:notice},{projection_kind:'CURRENT_CONTROL',interruption_notice:notice}]}},
    {snapshot:{snapshot}}];
  stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(responses.shift()),{status:200})));
  const connection=await new LocalHttpRuntimeAdapter().connect('session-1');
  expect(connection.current().interruptionNotices).toEqual([]);
  await connection.observe();
  expect(connection.current().interruptionNotices).toHaveLength(1);
  await connection.snapshot();
  expect(connection.current().interruptionNotices).toHaveLength(1);
  expect(connection.current().interruptionNotices![0].displayAfterMessageId).toBe('old-input');
});

it('projects a notice from a cold feedback-only page without requiring a visible message', async () => {
  const view=new CanonicalHistoryView('cold',{path:'/missing',outcome:'MISSING',reason:null},{session_id:'cold',event_sequence_cut:'4',
    entries:[{entry_id:'feedback',entry_sequence:'3',turn_id:'stopped',entry_kind:'USER_CONTROL_FEEDBACK'}],
    interruption_notices:[{owner_kind:'EXECUTED_TURN',owner_id:'stopped',reason:'USER_STOPPED',terminal_at_utc:'2026-10-05T00:00:00Z',display_after_entry_sequence:'3'}]});
  await view.initialize();
  expect(view.current().messages).toEqual([]);
  expect(view.current().interruptionNotices).toHaveLength(1);
  expect(view.current().interruptionNotices![0].displayAfterMessageId).toBeUndefined();
});

it('reads ROOT compaction progress on connect and every observation without inventing adoption', async () => {
  const responses = [connectPayload([], {}, [], { root_compaction_in_progress: true }),
    { observation: { root_compaction_in_progress: false } },
    { observation: { root_compaction_in_progress: true } },
    { observation: { root_compaction_in_progress: false } }];
  const fetch = vi.fn(async () => new Response(JSON.stringify(responses.shift()), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  stubRuntimeFetch(fetch);
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  expect(connection.current().liveControl.root_compaction_in_progress).toBe(true);
  for (const expected of [false, true, false]) {
    const projection = await connection.observe();
    expect(projection.liveControl.root_compaction_in_progress).toBe(expected);
    expect(projection.contextCompaction).toBeUndefined();
  }
  expect(fetch).toHaveBeenCalledTimes(4);
});

it('reads a cold image reference from first-chunk metadata and rejects changed facts', async () => {
  vi.stubGlobal('crypto',webcrypto);
  const imageRef='image:'+'a'.repeat(26);
  const bytes=Buffer.from('canonical image bytes');
  const digest='sha256:'+createHash('sha256').update(bytes).digest('hex');
  const metadata={digest,complete_size:bytes.length,media_type:'image/png',codec:'binary',image_ref:imageRef,width:7,height:5};
  const frames=[
    {content:{...metadata,offset_bytes:0,content:bytes.subarray(0,5).toString('base64'),complete:false}},
    {content:{...metadata,offset_bytes:5,content:bytes.subarray(5).toString('base64'),complete:true}},
  ];
  const fetcher=vi.fn(async()=>new Response(JSON.stringify(frames.shift()),{status:200}));stubRuntimeFetch(fetcher);
  const view=new CanonicalHistoryView('cold',{path:'/missing',outcome:'MISSING',reason:null},{session_id:'cold',event_sequence_cut:'0',entries:[],control:{}});
  const result=await view.readImageReference(imageRef);
  expect([...result.bytes]).toEqual([...bytes]);expect(result.mediaType).toBe('image/png');expect(result.width).toBe(7);
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({image_ref:imageRef,offset_bytes:0,limit_bytes:1<<20});
  frames.push({content:{...metadata,offset_bytes:0,content:bytes.subarray(0,5).toString('base64'),complete:false}},
    {content:{...metadata,width:8,offset_bytes:5,content:bytes.subarray(5).toString('base64'),complete:true}});
  await expect(view.readImageReference(imageRef)).rejects.toThrow('图片暂时无法读取');
});

it('refreshes only advisory live state after ring overflow with a fresh empty live cut', async () => {
  vi.stubGlobal('crypto', webcrypto);
  const entry = {
    entry_id: 'kept', turn_id: 'old-turn', entry_sequence: '1',
    entry_kind: 'ASSISTANT_MESSAGE', content: blobContent('already hydrated'),
  };
  const live = (id: string, text: string) => ({
    event_type: 'TEXT_DELTA', draft_identity: id, turn_id: 'running',
    scope_kind: 'ROOT', payload: { text_delta: { delta: text } },
  });
  const payload = connectPayload(
    [entry],
    { active_turns: [{ turn_id: 'running', status: 'RUNNING' }] },
    [
      live('old-draft', 'stale draft'),
      {
        event_type: 'SUBAGENT_PROGRESS',
        payload: { subagent_progress: { task_id: 'old-task', public_summary: 'stale progress' } },
      },
    ],
    { owner_epoch: '2', live_revision: '3' },
  );
  const frames = [
    payload,
    contentChunk('already hydrated'),
    { observation: {
      through_event_sequence: '2', live_owner_epoch: '1', through_live_revision: '900',
      gap: { kind: 'LIVE_GAP', reason: 'LIVE_RING_OVERFLOW' },
      committed: [{
        projection_kind: 'IMMUTABLE_ENTRY',
        entry: {
          entry_id: 'new', turn_id: 'new-turn', entry_sequence: '2',
          entry_kind: 'USER_MESSAGE', content: inlinePrompt('new committed input'),
        },
      }],
      live: [],
    } },
    { live_control_snapshot: { snapshot: {
      owner_epoch: '2', live_revision: '8', root_compaction_in_progress: true,
      current_todos: [],
    } } },
    { observation: {
      through_event_sequence: '2', live_owner_epoch: '1', through_live_revision: '901',
      live_control_owner_epoch: '2', through_live_control_revision: '8',
      live: [live('next-draft', 'next live')],
    } },
  ];
  const fetch = vi.fn(async () => new Response(JSON.stringify(frames.shift()), { status: 200 }));
  stubRuntimeFetch(fetch);
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  await connection.hydrateEntries!(['kept']);
  const identity = connection.chatWriteHeaders();
  expect(connection.current().messages.some(m => m.body === 'stale draft')).toBe(true);

  const restored = await connection.observe();
  expect(connection.chatWriteHeaders()).toEqual(identity);
  expect(connection.role).toBe('controller');
  expect(restored.messages.map(m => m.body)).toContain('already hydrated');
  expect(restored.messages.map(m => m.body)).toContain('new committed input');
  expect(restored.messages.map(m => m.body)).not.toContain('stale draft');
  expect(restored.subagentProgress).toEqual({});
  expect(restored.liveControl.current_todos).toEqual([]);
  expect(restored.liveControl.root_compaction_in_progress).toBe(true);
  expect(restored.presentationNotices[0]).toContain('临时进展已丢失');
  expect(restored.eventSequence).toBe(2);
  expect(restored.liveRevision).toBe(900);

  const next = await connection.observe();
  expect(next.messages.map(m => m.body)).toContain('next live');
  expect(next.presentationNotices).toEqual([]);
  const paths = fetch.mock.calls.map(call => String(call[0]));
  expect(paths.filter(path => path.endsWith('/connections'))).toHaveLength(1);
  expect(paths.filter(path => path.endsWith('/read-content'))).toHaveLength(1);
  expect(paths.some(path => path.endsWith('/history') || path.endsWith('/snapshot'))).toBe(false);
  expect(JSON.parse(fetch.mock.calls.at(-1)![1]!.body as string)).toMatchObject({
    after_event_sequence: '2', after_live_revision: 900,
    live_control_owner_epoch: 2, after_live_control_revision: 8,
  });
});

it.each(['COMMITTED_GAP', 'LIVE_CONTROL_GAP', 'LIVE_GAP'])('keeps other gap semantics: %s', async kind => {
  const frames = [connectPayload(), { observation: { gap: {
    kind, reason: kind === 'LIVE_GAP' ? 'LIVE_OWNER_EPOCH_CHANGED' : 'OTHER',
  } } }];
  const fetch = vi.fn(async () => new Response(JSON.stringify(frames.shift()), { status: 200 }));
  stubRuntimeFetch(fetch);
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  await expect(connection.observe()).rejects.toMatchObject({ code: 'PROTOCOL_GAP', gapKind: kind });
  expect(fetch).toHaveBeenCalledTimes(2);
});

const todoOwnerSnapshot = (run: string, revision: number, disposition = 'ACTIVE') => ({
  todo_run_id: run, todo_revision: String(revision), scope_kind: 'ROOT', disposition,
  ordered_items: disposition === 'ACTIVE'
    ? [{ ordinal: 0, text: `${run} revision ${revision}`, status: 'IN_PROGRESS' }] : [],
});
const todoInvalidation = (snapshot: Record<string, unknown>) => ({
  event_type: 'TODO_SNAPSHOT_UPDATED', scope_kind: snapshot.scope_kind,
  payload: { todo_snapshot_updated: snapshot },
});

it.each([
  ['same-run revision', [todoOwnerSnapshot('A', 2)], todoOwnerSnapshot('A', 1)],
  ['new ROOT run', [todoOwnerSnapshot('B', 0, 'CLEARED')], todoOwnerSnapshot('A', 99)],
  ['same-run CLEARED', [todoOwnerSnapshot('A', 2, 'CLEARED')], todoOwnerSnapshot('A', 1)],
  ['closed run absent from owner', [], todoOwnerSnapshot('A', 1)],
  ['old CLOSED after new ROOT', [todoOwnerSnapshot('B', 0, 'CLEARED')], todoOwnerSnapshot('A', 100, 'CLOSED')],
])('retains owner TODO truth after gap and a lagging live page: %s', async (_name, current, older) => {
  const baseline = { owner_epoch: '2', live_revision: '8', current_todos: current };
  const frames = [
    connectPayload([], {}, [], { owner_epoch: '2', live_revision: '1' }),
    { observation: {
      through_event_sequence: '0', live_owner_epoch: '1', through_live_revision: '900',
      gap: { kind: 'LIVE_GAP', reason: 'LIVE_RING_OVERFLOW' }, committed: [], live: [],
    } },
    { live_control_snapshot: { snapshot: baseline } },
    { observation: {
      through_event_sequence: '0', live_owner_epoch: '1', through_live_revision: '901',
      live_control_owner_epoch: '2', through_live_control_revision: '8',
      live: [todoInvalidation(older)],
    } },
    { live_control_snapshot: { snapshot: baseline } },
  ];
  const fetch = vi.fn(async () => new Response(JSON.stringify(frames.shift()), { status: 200 }));
  stubRuntimeFetch(fetch);
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  expect((await connection.observe()).liveControl.current_todos).toEqual(current);
  expect((await connection.observe()).liveControl.current_todos).toEqual(current);
  expect(fetch.mock.calls.filter(call => String(call[0]).endsWith('/live-control-snapshot'))).toHaveLength(2);
});

it('preserves the connection owner TODO baseline over retained live payloads', async () => {
  const current = [todoOwnerSnapshot('B', 0, 'CLEARED')];
  const fetch = vi.fn(async () => new Response(JSON.stringify(connectPayload([], {}, [
    todoInvalidation(todoOwnerSnapshot('A', 99)),
    todoInvalidation(todoOwnerSnapshot('A', 100, 'CLOSED')),
  ], { owner_epoch: '2', live_revision: '8', current_todos: current })), { status: 200 }));
  stubRuntimeFetch(fetch);
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  expect(connection.current().liveControl.current_todos).toEqual(current);
  expect(connection.current().todo).toBeUndefined();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('refreshes TODOs once per live batch without jumping interaction state or its cursor', async () => {
  const previousInteraction = { interaction_id: 'interaction-old', interaction_kind: 'TOOL_CONFIRMATION' };
  const nextInteraction = { interaction_id: 'interaction-new', interaction_kind: 'TOOL_CONFIRMATION' };
  const current = [todoOwnerSnapshot('B', 1)];
  const frames = [
    connectPayload([], {}, [], {
      owner_epoch: '2', live_revision: '3', current_interaction: previousInteraction,
      current_todos: [todoOwnerSnapshot('A', 1)],
    }),
    { observation: {
      through_event_sequence: '0', live_owner_epoch: '1', through_live_revision: '3',
      live_control_owner_epoch: '2', through_live_control_revision: '3',
      live: [todoInvalidation(todoOwnerSnapshot('A', 2)),
        todoInvalidation(todoOwnerSnapshot('A', 3, 'CLOSED')),
        todoInvalidation(todoOwnerSnapshot('B', 1))],
    } },
    { live_control_snapshot: { snapshot: {
      owner_epoch: '2', live_revision: '5', current_todos: current,
      current_interaction: nextInteraction,
    } } },
    { observation: {
      through_event_sequence: '0', live_owner_epoch: '1', through_live_revision: '3',
      live_control_owner_epoch: '2', through_live_control_revision: '5',
      live_control: [{ kind: 'LIVE_INTERACTION_CLOSED' }, { kind: 'LIVE_INTERACTION_OPENED', interaction: nextInteraction }],
    } },
  ];
  const fetch = vi.fn(async () => new Response(JSON.stringify(frames.shift()), { status: 200 }));
  stubRuntimeFetch(fetch);
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  const updated = await connection.observe();
  expect(updated.liveControl.current_todos).toEqual(current);
  expect(updated.liveControl.current_interaction).toEqual(previousInteraction);
  expect(updated.liveControl.live_revision).toBe('3');
  const next = await connection.observe();
  expect(next.liveControl.current_interaction).toEqual(nextInteraction);
  expect(next.presentationNotices).toContain('这项确认已结束；原因暂不可确认。');
  expect(JSON.parse(fetch.mock.calls.at(-1)![1]!.body as string)).toMatchObject({
    live_control_owner_epoch: 2, after_live_control_revision: 3,
  });
  expect(fetch.mock.calls.filter(call => String(call[0]).endsWith('/live-control-snapshot'))).toHaveLength(1);
  expect(fetch).toHaveBeenCalledTimes(4);
});

it('presents only fresh authenticated complete root results, without history replay or blocking observe', async () => {
  const assistant = {entry_id:'open-call',turn_id:'turn-open',entry_sequence:'1',entry_kind:'ASSISTANT',scope_kind:'ROOT',blocks:[{block_id:'b',block_kind:'TOOL_CALL',tool_call_id:'open-1',tool_name:'open_in_pulsara'}]};
  const result = {entry_id:'open-result',turn_id:'turn-open',entry_sequence:'2',entry_kind:'TOOL_RESULT',scope_kind:'ROOT',content:inlineContent(JSON.stringify({targets:[{target:{kind:'PANEL',name:'tasks'}}],errors:[{index:1,message:'invalid'}]})),tool_result:{assistant_entry_id:'open-call',tool_call_id:'open-1',result_state:'SUCCESS',source_coverage:'COMPLETE',display_kind:'COMPLETE'}};
  let committed: unknown[] = [];
  let cut = '9007199254740993';
  let releaseWindow!: () => void;
  const windowWait = new Promise<void>(resolve => {releaseWindow=resolve;});
  const windows: unknown[] = [];
  stubRuntimeFetch(vi.fn(async (input, init) => {
    const url=String(input);
    if(url.endsWith('/connections')) {const payload=connectPayload(); payload.snapshot.snapshot.event_sequence_cut=cut; return new Response(JSON.stringify(payload));}
    if(url.endsWith('/observe')) return new Response(JSON.stringify({observation:{committed,through_event_sequence:cut}}));
    if(url.endsWith('/history/window')) {windows.push(JSON.parse(String(init?.body))); await windowWait;return new Response(JSON.stringify({session_id:'session-1',entries:[assistant]}));}
    if(url.endsWith('/history-snapshot')) return new Response(JSON.stringify({snapshot:{snapshot:{entries:[assistant,result],event_sequence_cut:cut,control:{}}}}));
    return new Response('{}');
  }));
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  if(connection instanceof CanonicalHistoryView) throw new Error('expected controller');
  const seen: import('../../frontend/lib/presentation-targets').PresentationRequest[]=[];
  const reads: Promise<unknown>[]=[];
  connection.subscribePresentation!(request=>{seen.push(request);reads.push(request.read());});
  expect(seen).toHaveLength(0);
  cut='9007199254740994'; committed=[{projection_kind:'IMMUTABLE_ENTRY',event_sequence:cut,entry:result}];
  await connection.observe(); // The association read is still pending.
  expect(seen).toHaveLength(1); expect(windows).toEqual([expect.objectContaining({anchor_entry_id:'open-call',maximum_entries:1})]);
  await connection.observe(); expect(seen).toHaveLength(1);
  releaseWindow(); expect(await reads[0]).toEqual({targets:[{type:'panel',name:'tasks'}],errors:[{index:1,message:'invalid'}]});
  cut='9007199254740996'; await connection.snapshot(); expect(seen[0].signal.aborted).toBe(true);
  committed=[{projection_kind:'IMMUTABLE_ENTRY',event_sequence:'9007199254740995',entry:result},{projection_kind:'IMMUTABLE_ENTRY',event_sequence:'9007199254740997',entry:{...result,entry_id:'imported',entry_owner_kind:'IMPORTED_HISTORY'}}];cut='9007199254740997';
  await connection.observe(); expect(seen).toHaveLength(1);
  cut='9007199254740998';committed=[{projection_kind:'IMMUTABLE_ENTRY',event_sequence:cut,entry:{...result,entry_id:'truncated',tool_result:{...result.tool_result,display_kind:'HEAD_TAIL'}}}];
  await connection.observe();expect(seen).toHaveLength(1);
  cut='9007199254740999';committed=[{projection_kind:'IMMUTABLE_ENTRY',event_sequence:cut,entry:{...result,entry_id:'new'}}];
  await connection.observe();expect(seen).toHaveLength(2); await reads[1];
  await connection.close();expect(seen[1].signal.aborted).toBe(true);
});

it('announces only a fresh, complete, host-owned manage_capability OAuth success', async () => {
  const authorization = {owner:{kind:'local',scope_key:'user',server_id:'notion',plugin_id:null},config_identity:'config',display_identity:'config',package_install_id:null};
  const value = {action:'AUTHORIZE_MCP',status:'APPLIED',target:{kind:'MCP_CONNECTION',scope:'USER',source_kind:'LOCAL',server_id:'notion'},current:{authorized:true},authorization};
  const assistant = {entry_id:'auth-call',turn_id:'turn-auth',entry_sequence:'1',entry_kind:'ASSISTANT',scope_kind:'ROOT',blocks:[{block_id:'b',block_kind:'TOOL_CALL',tool_call_id:'auth-1',tool_name:'manage_capability'}]};
  const result = {entry_id:'auth-result',turn_id:'turn-auth',entry_sequence:'2',entry_kind:'TOOL_RESULT',scope_kind:'ROOT',content:inlineContent(JSON.stringify(value)),tool_result:{assistant_entry_id:'auth-call',tool_call_id:'auth-1',result_state:'SUCCESS',source_coverage:'COMPLETE',display_kind:'COMPLETE'}};
  let committed: unknown[] = [];
  let cut = '10';
  stubRuntimeFetch(vi.fn(async input => {
    const url = String(input);
    if (url.endsWith('/connections')) { const payload = connectPayload(); payload.snapshot.snapshot.event_sequence_cut = cut; return new Response(JSON.stringify(payload)); }
    if (url.endsWith('/observe')) return new Response(JSON.stringify({observation:{committed,through_event_sequence:cut}}));
    if (url.endsWith('/history/window')) return new Response(JSON.stringify({session_id:'session-1',entries:[assistant]}));
    return new Response('{}');
  }));
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  if (connection instanceof CanonicalHistoryView) throw new Error('expected controller');
  const reads: Promise<unknown>[] = [];
  connection.subscribeMcpAuthorization!(request => reads.push(request.read()));
  committed = [{projection_kind:'IMMUTABLE_ENTRY',event_sequence:cut,entry:result}];
  await connection.observe(); expect(reads).toHaveLength(0); // bootstrap/history boundary
  cut = '11'; committed = [{projection_kind:'IMMUTABLE_ENTRY',event_sequence:cut,entry:result}];
  await connection.observe(); expect(await reads[0]).toEqual({scope:'USER',authorization});
  await connection.observe(); expect(reads).toHaveLength(1); // no duplicate focus
  for (const changed of [
    {...result,entry_owner_kind:'IMPORTED_HISTORY'},
    {...result,scope_kind:'SUBAGENT'},
    {...result,tool_result:{...result.tool_result,display_kind:'HEAD_TAIL'}},
    {...result,tool_result:{...result.tool_result,result_state:'ERROR'}},
  ]) {
    cut = String(Number(cut) + 1); committed = [{projection_kind:'IMMUTABLE_ENTRY',event_sequence:cut,entry:changed}];
    await connection.observe();
  }
  expect(reads).toHaveLength(1);
  cut = '20'; committed = [{projection_kind:'IMMUTABLE_ENTRY',event_sequence:cut,entry:{...result,entry_id:'rejected-result',content:inlineContent(JSON.stringify({...value,status:'REJECTED'}))}}];
  await connection.observe(); expect(await reads[1]).toBeUndefined();
  await connection.close();
});

it.each([false, true])('sends one typed Plan revision source through the real adapter, queued=%s', async queued => {
  const responses = [connectPayload([], {
    active_plan_workflow:{workflow_id:'workflow-1', workflow_revision:'7'},
    open_plan_interaction:{interaction_id:'review:exact', workflow_id:'workflow-1', kind:'DRAFT_REVIEW'},
  }), {resolve_plan_interaction:{command_id:'command:revise', interaction_status:'REVISION_REQUESTED',
    continuation_turn_id:'turn:revision', draft_decision:'PLAN_DRAFT_REVISE'}}];
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(responses.shift()), {status:200}));
  stubRuntimeFetch(fetcher);
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  const receipt = await connection.resolveInteraction(connection.current().interaction!, queued
    ? {kind:'plan-draft', decision:'revise', sourceQueueItemId:'queue:original'}
    : {kind:'plan-draft', decision:'revise', revisionPrompt:{parts:[
      {type:'image', source:'local', bytes:Uint8Array.from([0, 255]), declaredMediaType:'image/png'},
    ]}}, 'command:revise');
  expect(receipt).toMatchObject({commandId:'command:revise', status:'succeeded', planDraftDecision:'revise', planContinuationTurnId:'turn:revision'});
  const body = JSON.parse(String(fetcher.mock.calls[1][1]?.body));
  expect(body).toMatchObject({command_id:'command:revise', interaction_id:'review:exact', workflow_id:'workflow-1',
    expected_workflow_revision:7, resolution_kind:'draft', draft_decision:'PLAN_DRAFT_REVISE'});
  expect(body).not.toHaveProperty('feedback');
  if (queued) {
    expect(body.source_queue_item_id).toBe('queue:original');
    expect(body).not.toHaveProperty('revision_prompt');
  } else {
    expect(body.revision_prompt).toEqual({parts:[{type:'image', content_base64:'AP8=', declared_media_type:'image/png'}]});
    expect(body).not.toHaveProperty('source_queue_item_id');
  }
});

it.each([{}, { plan: null }, { plan: '' }, { plan: ' \n\t\u3000' }])('projects no-proposal exit without a review card or synthetic user bubble (%j)', async (args) => {
  const resultText = JSON.stringify({status:'success', plan_control:'EXITED_PLAN', resume_permission_mode:'accept-edits'});
  const entries = [{
    entry_id:'exit-call', turn_id:'planning-turn', entry_sequence:'1', entry_kind:'ASSISTANT_TOOL_REQUEST', scope_kind:'ROOT',
    blocks:[{block_id:'exit-block', block_kind:'TOOL_CALL', tool_call_id:'exit-call-id', tool_name:'exit_plan', tool_arguments_preview:inlineContent(JSON.stringify(args)).inline_content}],
  }, {
    entry_id:'exit-result', turn_id:'planning-turn', entry_sequence:'2', entry_kind:'TOOL_RESULT', scope_kind:'ROOT',
    tool_result:{assistant_entry_id:'exit-call',tool_call_id:'exit-call-id',result_state:'SUCCESS'},
    content:inlineContent(resultText),
  }, {
    entry_id:'exit-continuation', turn_id:'execution-turn', entry_sequence:'3', entry_kind:'PLAN_CONTINUATION', scope_kind:'ROOT',
    content:inlineContent(JSON.stringify({transition:'EXITED_PLAN',workflow_id:'workflow-1',resume_permission_mode:'accept-edits'})),
  }];
  stubRuntimeFetch(vi.fn(async () => new Response(JSON.stringify(connectPayload(entries, {
    tool_attempts:[{assistant_entry_id:'exit-call',tool_call_id:'exit-call-id',result_entry_id:'exit-result',result_state:'SUCCESS'}],
  })))));
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  const projection = connection.current();
  expect(projection.planMode).toBe(false);
  expect(projection.interaction).toBeUndefined();
  expect(projection.messages.some(message => message.role === 'user')).toBe(false);
  expect(projection.messages[0]?.traces?.[0]).toMatchObject({
    title:'退出规划',subtitle:'已完成',status:'completed',resultText,
    resultSummary:'已退出规划，继续处理任务。',
  });
});

it('keeps settled live reasoning until the exact canonical reasoning bytes are hydrated', async () => {
  vi.stubGlobal('crypto', webcrypto);
  const text = 'still visible reasoning';
  const bytes = Buffer.from(text);
  const digest = `sha256:${createHash('sha256').update(bytes).digest('hex')}`;
  const entry = {entry_id:'entry-reason',turn_id:'turn-live',entry_sequence:'1',entry_kind:'ASSISTANT_MESSAGE',scope_kind:'ROOT',
    blocks:[{block_id:'text',block_kind:'TEXT',content:inlineContent('commentary')}],
    reasoning_blocks:[{block_id:'reason-canonical',ordinal:'0',presentation_kind:'REASONING_PRESENTATION_FULL',
      content:{kind:'CANONICAL_BLOB',digest,size:String(bytes.length),media_type:'text/plain',codec:'utf-8'}}]};
  const events = [
    {live_revision:'1',event_type:'THINKING_START',draft_identity:'draft-live',turn_id:'turn-live',scope_kind:'ROOT',
      payload:{thinking_start:{block_identity:'reason-live',presentation_kind:'REASONING_PRESENTATION_FULL'}}},
    {live_revision:'2',event_type:'THINKING_DELTA',draft_identity:'draft-live',turn_id:'turn-live',scope_kind:'ROOT',
      payload:{thinking_delta:{block_identity:'reason-live',delta:text}}},
  ];
  const responses = [connectPayload([], {active_turns:[{turn_id:'turn-live',scope_kind:'ROOT',status:'RUNNING'}]}, events),
    {observation:{through_event_sequence:'1',committed:[{projection_kind:'IMMUTABLE_ENTRY',entry}],
      settlements:[{kind:'LIVE_GENERATION_COMMITTED',draft_identity:'draft-live',committed_entry_id:'entry-reason'}]}}];
  stubRuntimeFetch(vi.fn(async input => new Response(JSON.stringify(String(input).endsWith('/read-content')
    ? {content:{digest,complete_size:String(bytes.length),offset_bytes:'0',content:bytes.toString('base64'),complete:true}}
    : responses.shift()), {status:200})));
  const connection = await new LocalHttpRuntimeAdapter().connect('session-1');
  expect(connection.current().messages[0].reasoning?.[0].body).toBe(text);
  await connection.observe();
  await vi.waitFor(() => expect(connection.current().messages[0].historyContextState).toBe('ready'));
  expect(connection.current().messages[0]).toMatchObject({contentState:'ready',reasoningContentState:'unloaded',
    pendingAssistant:{body:'commentary',reasoning:[{body:text}]}});
  await connection.hydrateEntries!(['entry-reason'], undefined, true);
  expect(connection.current().messages[0].reasoning?.[0].body).toBe(text);
  expect(connection.current().messages[0]).not.toHaveProperty('pendingAssistant');
});

it('discovers custom model IDs with a cancellable user-level request', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({model_ids: ['a', 'b']}), {status: 200}));
  vi.stubGlobal('fetch', fetchMock);
  const controller = new AbortController();
  const input = {base_url: 'https://gateway.example/v1', authentication: 'bearer_api_key' as const, api_key: null, connection_id: null};
  const adapter = new LocalHttpRuntimeAdapter();
  expect(await adapter.discoverModels(input, controller.signal)).toEqual({model_ids: ['a', 'b']});
  expect(String(fetchMock.mock.calls[0][0])).toContain('/api/model-configurations/discover');
  expect(fetchMock.mock.calls[0][1]).toMatchObject({method: 'POST', body: JSON.stringify(input), signal: controller.signal});
});
