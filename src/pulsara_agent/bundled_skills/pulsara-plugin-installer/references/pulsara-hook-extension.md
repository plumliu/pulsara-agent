# Pulsara Hook Extension Target

Read this file only when a source Plugin contains behavior-bearing Hooks. It
defines the exact Pulsara conversion target; it is not a second Hook parser or a
claim that arbitrary vendor Hooks are compatible.

Agent Plugins 1.0 has no portable Hook component. Pulsara Hooks are a client
extension stored at exactly:

```text
dev.pulsara/hooks/hooks.json
```

The installed Plugin must still have a valid root `plugin.json`. Plugin enablement
makes this definition source eligible, but command execution also requires the
ordinary Pulsara Hook trust decision. Never execute a Hook while converting it.

## Configuration Shape

The target is strict UTF-8 JSON with no duplicate keys, comments, aliases,
`NaN`, or `Infinity`:

```json
{
  "description": "Optional source description",
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^(Bash|apply_patch)$",
        "hooks": [
          {
            "type": "command",
            "command": "\"${PLUGIN_ROOT}/scripts/check.sh\"",
            "commandWindows": "\"%PLUGIN_ROOT%\\scripts\\check.cmd\"",
            "timeout": 30,
            "statusMessage": "Checking tool request",
            "additionalContextLimit": 2500,
            "async": false
          }
        ]
      }
    ]
  }
}
```

Emit only the root fields `description` and `hooks`, only the group fields
`matcher` and `hooks`, and only these command-handler fields:

- `type`: must be exact `command`;
- `command`: nonempty shell command, at most 8 KiB UTF-8;
- `commandWindows`: optional nonempty Windows command, at most 8 KiB UTF-8;
- `timeout`: optional integer seconds; default 600 and range 1..600, except
  `SessionEnd`, whose default is 1 and range is 1..3;
- `statusMessage`: optional diagnostic label, at most 4 KiB UTF-8;
- `additionalContextLimit`: optional nonnegative integer, default 2500; use it
  only for events that accept context. A positive value omits the whole context
  contribution when its provider-specific estimate exceeds the threshold; zero
  disables this per-handler threshold but not capture or compiler bounds;
- `async`: optional boolean, default false. `SessionEnd` is always synchronous.

The whole config is bounded to 1 MiB, 16,384 JSON nodes, depth 64, ordinary
scalars 64 KiB, and matchers 1 KiB. These are per-carrier parser bounds, not a
Plugin lifetime or total-Hook cap. Do not split one behavior across files to evade
them: one Plugin has exactly one Hook config source.

`prompt`, `agent`, `http`, `mcp_tool`, or any other handler type has no target.
Unknown behavior-bearing fields are not inert merely because the parser can
diagnose or skip them. If they are required, conversion is not exact.

## Events and Matchers

The only event names are:

```text
SessionStart
SessionEnd
UserPromptSubmit
PreToolUse
PermissionRequest
PostToolUse
PreCompact
PostCompact
SubagentStart
SubagentStop
Stop
```

Missing, empty, or exact `*` matches all. Other matchers use case-sensitive RE2
search semantics, not Python backtracking regular expressions. Lookaround,
backreferences, recursion, and other non-RE2 constructs have no target. Use `^`
and `$` when exact matching is required.

Matcher subjects are fixed:

| Event | Subject |
|---|---|
| `SessionStart` | `startup`, `resume`, or `compact`; Pulsara has no `clear` producer |
| `SessionEnd` | exact `other` reason |
| `UserPromptSubmit` | matcher ignored; always selected |
| tool lifecycle events | frozen tool identity and closed aliases below |
| `PreCompact`, `PostCompact` | `manual` or `auto` |
| `SubagentStart`, `SubagentStop` | exact existing worker-profile selector |
| `Stop` | matcher ignored; always selected |

The existing worker-profile matcher values are exactly. They select an existing
Pulsara lifecycle event; this conversion target does not define or install a
worker, agent, preset, or child runtime:

```text
general_worker
research_worker
review_worker
verification_worker
synthesizer
```

Important tool aliases and public names are:

| Pulsara tool | Matcher candidates | Public `tool_name` |
|---|---|---|
| `terminal` | `terminal`, `Bash` | `Bash` |
| `terminal_process` | `terminal_process` | `terminal_process` |
| `terminal_monitor` | `terminal_monitor` | `terminal_monitor` |
| `edit_file` | `edit_file`, `apply_patch`, `Edit` | `apply_patch` |
| `write_file` | `write_file`, `apply_patch`, `Write` | `apply_patch` |
| `spawn_agent` | `spawn_agent`, `Agent` | `spawn_agent` |
| `create_agent_tasks` | `create_agent_tasks` | `create_agent_tasks` |
| MCP tool | exact provider-qualified remote identity | same identity |

Other builtins use their exact Pulsara descriptor name. `edit_file` and
`write_file` keep their real Pulsara input schemas; they do not acquire a fake
Codex `command` argument. `terminal_process` and `terminal_monitor` are independent
tool calls, not transparent `write_stdin` continuations of `Bash`. A Hook that
depends on those vendor transport semantics is not convertible.

## Command Input

Each command receives one JSON object on stdin. Common fields are:

```text
session_id: string
transcript_path: null
cwd: string
hook_event_name: exact event name
model: string
```

Event-specific fields are:

| Event | Additional required input |
|---|---|
| `SessionStart` | `source`, `permission_mode` |
| `SessionEnd` | `reason: "other"` |
| `UserPromptSubmit` | `turn_id`, `prompt`, `permission_mode` |
| `PreToolUse` | `turn_id`, `tool_name`, `tool_use_id`, `tool_input`, `permission_mode` |
| `PermissionRequest` | `turn_id`, `tool_name`, `tool_input`, `permission_mode` |
| `PostToolUse` | `turn_id`, `tool_name`, `tool_use_id`, `tool_input`, public `tool_response`, `permission_mode` |
| `PreCompact`, `PostCompact` | `turn_id`, `trigger` |
| `SubagentStart` | `turn_id`, `agent_id`, `agent_type`, `permission_mode` |
| `SubagentStop` | `turn_id`, `agent_id`, `agent_type`, `agent_transcript_path: null`, `stop_hook_active`, `last_assistant_message`, `permission_mode` |
| `Stop` | `turn_id`, `stop_hook_active`, `last_assistant_message`, `permission_mode` |

`permission_mode` is one of `default`, `acceptEdits`, `plan`, `dontAsk`, or
`bypassPermissions`. A conditional `pulsara_tool_name` appears only when the
Pulsara exposed or outer name differs from the compatibility primary name.

`transcript_path` and `agent_transcript_path` are deliberately null. Hook stdin
does not contain Plugin identity, install paths, private replay, raw artifacts, or
any model or retrieval credential. A vendor Hook that requires a transcript file, Plugin
provenance in stdin, hidden reasoning, or a different tool-input schema has no
exact mapping.

## Environment and Commands

Plugin Hook commands receive these public declaration values:

```text
PLUGIN_ROOT=<exact immutable managed package root>
PLUGIN_DATA=<exact persistent Plugin data root>
```

Pulsara does not interpolate either placeholder into the reviewed command.
Reference them using the command shell's own syntax and preserve the resulting
behavior exactly. The generic executor also supplies `PULSARA_HOOK_SOURCE_DIR`
and, when applicable, `PULSARA_PROJECT_DIR`. It inherits the ordinary sanitized
host environment; inherited variables containing a caller-bound credential are
omitted at the process boundary.

Do not rewrite a reviewed command, guess a PATH executable, install a dependency,
or add a launcher merely to make conversion pass. If relocation into the managed
package changes command, `cwd`, quoting, executable, or dependency behavior in a
way the candidate cannot express exactly, stop.

## Output and Control

Transport, spawn, timeout, malformed output, and unsupported-control failures are
diagnostic and fail open. Only a successfully parsed explicit control changes the
owner operation. `stderr` and `systemMessage` are diagnostics, not model context.

The supported behavior is:

| Event | Exact accepted behavior |
|---|---|
| `SessionStart` | plain stdout or `hookSpecificOutput.additionalContext` adds advisory context; `continue:false` blocks |
| `SessionEnd` | observe only; JSON may contain only `systemMessage` |
| `UserPromptSubmit` | plain/additional context; exit 2, `continue:false`, or legacy `decision:"block"` blocks |
| `PreToolUse` | exit 2, legacy block, or hook-specific `permissionDecision:"deny"` blocks; hook-specific context is allowed |
| `PermissionRequest` | hook-specific decision object with `behavior:"allow"|"deny"` and optional message |
| `PostToolUse` | hook-specific additional context only; result rewrite or suppression is forbidden |
| `PreCompact`, `PostCompact` | `continue:false` blocks; other stdout is diagnostic |
| `SubagentStart` | plain or hook-specific additional context; root `continue` is ignored |
| `SubagentStop`, `Stop` | exit 2 or legacy block requests at most one continuation; `continue:false` terminalizes |

JSON `hookSpecificOutput.hookEventName` must equal the current event. PreTool does
not support `allow` or `ask`; only `PermissionRequest` can explicitly allow.
Argument rewrite, ToolResult rewrite, updated permissions, interrupt/rewrite
fields, and output suppression have no target. `suppressOutput:true` is invalid.
PostTool exit 2 does not replace or hide its already-settled result.

An asynchronous handler cannot emit any control-vocabulary field. Plain async
stdout is context only for `SessionStart`, `UserPromptSubmit`, and
`SubagentStart`; otherwise it is diagnostic. Do not convert a vendor background
Hook whose correctness depends on blocking, permission, continuation, ordering,
or durable completion.

Context is untrusted, one-shot, advisory user-role input. It never rewrites
SYSTEM or tools. `additionalContextLimit` omits a whole oversized contribution;
it does not truncate it. A Hook that requires developer/system authority or
guaranteed context delivery is not equivalent.

## Conversion Decision

For every source Hook, record an explicit event, matcher, stdin, environment,
execution, output, and control mapping. Preserve command and script bytes. A
renamed event is valid only when the owner timing and control effect are the same,
not merely because the names sound similar.

Refuse whole-Plugin conversion when any required Hook depends on an unsupported
event or handler, matcher dialect, transcript path, automatic dependency setup,
different tool schema, argument/result mutation, output suppression, implicit
fail-closed behavior, more than one continuation, durable/background completion,
or stronger context authority. Leaving such a Hook file in the candidate as an
inert resource does not preserve the Plugin.
