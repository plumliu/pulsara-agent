# Hook configuration and command interface

For local source paths, trust and enablement, see
[Hook workflows](hook-source-management.md). This reference covers command
configuration and the input/output shapes needed to write or adapt a Hook.

Plugin definitions use `dev.pulsara/hooks/hooks.json` or a selected native OpenAI
manifest, as described in [Codex declarations](codex-compatible.md). External
import combines its selected declarations into one Pulsara Hook source.

## Configuration

The configuration is a JSON object:

```json
{
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

Root `description` is optional. Each event has an array of matcher groups, each
containing a `hooks` array of handlers.

| Handler field | Meaning |
| --- | --- |
| `type` | `command` |
| `command` | Shell command, up to 8 KiB UTF-8 |
| `commandWindows` | Optional Windows command; `command_windows` is also accepted |
| `timeout` | Seconds, 1–600; default 600. SessionEnd uses 1–3, default 1 |
| `statusMessage` | Optional progress label |
| `additionalContextLimit` | Context threshold, default 2500 tokens; 0 removes this per-handler threshold |
| `scope_policy` | ROOT, SUBAGENT or BOTH |
| `async` | Background execution, default false; SessionEnd runs synchronously |

Missing or null optional matcher/context-limit values use their defaults. A null
Windows command uses `command`. Scope defaults to BOTH for SessionStart,
SUBAGENT for SubagentStart/SubagentStop, and ROOT for the other events.

The active handler type is `command`. Other types and unsupported events are
reported as inactive; invalid groups or handlers leave independent entries usable.

## Events and matching

| Event | Matcher subject | Runs when |
| --- | --- | --- |
| SessionStart | startup, resume or compact | A session starts or resumes, or adopted compaction continues |
| SubagentStart | startup, resume or compact | The delegated session's start event follows SessionStart |
| SessionEnd | other | The session closes |
| UserPromptSubmit | All submissions | Input is submitted |
| PreToolUse | Tool name and aliases | Before a tool call |
| PermissionRequest | Tool name and aliases | The permission workflow requests a decision |
| PostToolUse | Tool name and aliases | After a tool result |
| PreCompact | manual or auto | Before compaction |
| PostCompact | manual or auto | After compaction is adopted |
| Stop | All final-reply attempts | ROOT or delegated Work attempts its final reply |
| SubagentStop | All final-reply attempts | The delegated Work's Stop processing is followed by SubagentStop |

Child session start runs SessionStart then SubagentStart, including compact
starts. A later Work in an already running session uses that session. Stop and
SubagentStop share the current turn's single continuation allowance.

Missing, null, empty or exact `*` matches all. Other patterns use case-sensitive
RE2 search; use `^` and `$` for exact matching. UserPromptSubmit, Stop and
SubagentStop run for all occurrences regardless of matcher text.

Useful aliases are Bash for `exec_process`, apply_patch/Edit for `edit_file`,
apply_patch/Write for `write_file`, Read for `read_file`, Grep for
`search_file_content`, and Glob for `find_files`. MCP uses its qualified remote
tool name. Aliases select operations; stdin still uses the native Pulsara tool
name and arguments.

## Command input

Commands receive one JSON object on stdin. Common fields are:

```text
session_id: string
session_kind: ROOT | SUBAGENT
input_origin: HUMAN_MESSAGE | AGENT_INSTRUCTION | SCHEDULED_TASK | MODEL_TOOL_CALL | CODE_MODE_CALL | null
transcript_path: null
cwd: string
hook_event_name: exact event name
model: string
```

The command's working directory and `cwd` are the current workspace root.
Event-specific fields are:

| Event | Additional input |
| --- | --- |
| SessionStart, SubagentStart | source, permission_mode |
| SessionEnd | reason: "other" |
| UserPromptSubmit | turn_id, prompt, permission_mode |
| PreToolUse | turn_id, tool_name, tool_use_id, tool_input, permission_mode |
| PermissionRequest | turn_id, tool_name, tool_input, permission_mode |
| PostToolUse | turn_id, tool_name, tool_use_id, tool_input, tool_response, permission_mode |
| PreCompact, PostCompact | turn_id, trigger |
| Stop, SubagentStop | turn_id, stop_hook_active, last_assistant_message, permission_mode |

Permission modes are default, acceptEdits, plan, dontAsk and bypassPermissions.
For file tools, the file path is `tool_input.target.path`. For process tools,
startup command/workdir are top-level fields, while existing process actions use
`tool_input.target`. Use the relevant tool's native input when adapting a script.

Plugin commands receive PLUGIN_ROOT and CLAUDE_PLUGIN_ROOT for their package,
and PLUGIN_DATA and CLAUDE_PLUGIN_DATA for their persistent data directory.
PULSARA_HOOK_SOURCE_DIR identifies the source directory; PULSARA_PROJECT_DIR is
present for project-scoped execution. Shell variable syntax applies. Windows
execution adapts braced Plugin variable references to cmd syntax.

## Command output

Plain stdout adds model context for SessionStart, SubagentStart and
UserPromptSubmit. JSON can supply event-specific context, for example:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": "Run the project's tests after changing this file."
  }
}
```

`hookEventName` matches the current event. The supported controls are:

| Event | Output effect |
| --- | --- |
| SessionStart | Plain or additional context; continue:false blocks |
| SubagentStart | Plain or additional context; boolean continue is diagnostic |
| SessionEnd | Diagnostic systemMessage |
| UserPromptSubmit | Plain/additional context; exit 2, continue:false or legacy block rejects input |
| PreToolUse | Additional context; exit 2, legacy block or permissionDecision:"deny" blocks the call |
| PermissionRequest | hookSpecificOutput.decision with behavior:"allow" or "deny" and optional message |
| PostToolUse | Additional context for subsequent work |
| PreCompact, PostCompact | continue:false blocks continuation; other stdout is diagnostic |
| Stop, SubagentStop | Exit 2 or legacy block requests continuation; continue:false ends the turn |

Legacy block uses top-level `decision:"block"` and a nonempty `reason`.
A stopping decision takes precedence over a continuation request. PreToolUse
keeps the original tool arguments; explicit permission approval uses
PermissionRequest. PostToolUse preserves the tool's settled result.

Use synchronous handlers for control decisions and asynchronous handlers for
informational output. Background output is delivered at a later normal safe
point. Stderr and systemMessage are diagnostics. Malformed output, command
failure and timeout are reported and leave the underlying operation to continue.

Context is delivered through `pulsara_hook_context`. A positive
additionalContextLimit omits a contribution that exceeds the threshold; it keeps
the contribution whole when accepted. Import preserves scripts and configuration
it can represent; a script's dependence on another host's inputs or transcript
can still require porting when the task calls for it.
