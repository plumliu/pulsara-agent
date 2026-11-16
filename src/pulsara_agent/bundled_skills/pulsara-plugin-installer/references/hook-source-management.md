# Local and Plugin Hook Source Management

Queries observe definitions; ROOT-native manage_capability changes trust or
source enablement. WORKSPACE means the current GUI project, not terminal cwd.
A transient session cannot manage a WORKSPACE source; do not silently switch scopes.

## Observe and identify the source

List current project sources with query_capabilities:

```json
{"kind":"HOOK","scope":"WORKSPACE"}
```

Records are object content items with a HOOK_SOURCE Target, status text and
actual definitions. The Target contains only kind and absolute config_path.
For a particular Plugin, list kind HOOK with its observed PLUGIN Target as parent.
For exact observation, copy the returned source Target:

```json
{"target":{"kind":"HOOK_SOURCE","config_path":"/actual/project/.pulsara/hooks.json"},"kind":"HOOK"}
```

Exact queries do not include scope or parent. Unknown enablement/trust is not
false, and an unavailable source is not empty. Querying does not grant trust,
execute commands or establish that the current runtime selected the definitions.

## Prepare local definitions

Only create/edit the resolved local source when requested. A simple command
after a file read uses this native JSON shape:

```json
{"hooks":{"PostToolUse":[{"matcher":"^read_file$","hooks":[{"type":"command","command":"printf 'file read\\n'","timeout":5,"async":false,"additionalContextLimit":300}]}]}}
```

The source parser and trust owner validate the current declaration. Editing does
not grant trust or execute it. Matchers use case-sensitive RE2; native names such
as read_file and manage_capability work. Read pulsara-hook-extension.md for other
events, inputs, output control, Windows commands or Plugin conversion. Plugin
definitions belong to the immutable package, not editable loose sources.

## Trust, enable, disable or revoke

Copy the actual Target unchanged into each requested management action:

```json
{"target":{"kind":"HOOK_SOURCE","config_path":"/actual/project/.pulsara/hooks.json"},"action":"TRUST_HOOK_SOURCE"}
```

```json
{"target":{"kind":"HOOK_SOURCE","config_path":"/actual/project/.pulsara/hooks.json"},"action":"SET_HOOK_SOURCE_ENABLED","enabled":false}
```

```json
{"target":{"kind":"HOOK_SOURCE","config_path":"/actual/project/.pulsara/hooks.json"},"action":"REVOKE_HOOK_TRUST"}
```

Plugin sources use the same Target shape with their actual returned config_path.
The original owner resolves source, scope and Plugin identity. Do not add a digest,
expected guard, acceptance flag or user confirmation claim. TRUST_HOOK_SOURCE
requires GUI review of all current commands, environment, events, matchers and
execution fields even under full access. Enable/disable and revoke use their normal
permission path without another full trust review. Trust does not enable a disabled
source or Plugin; disabling does not necessarily remove a trust record.

Read meta.status and text content. ERROR can still report a confirmed change
followed by cleanup or refresh failure; address the failure without replaying the
mutation. Adopted definitions apply to future events. The management call uses its
captured predecessor Hook view, so newly trusted Hooks cannot trigger that call's
own PostTool. Current provider prefixes remain unchanged.

## Missing or damaged sources

Distinguish definition file, enablement, trust state and command script. For a local
missing/invalid definition, requested disable or revoke still uses the same owner;
it does not delete definitions/scripts. Requested deletion edits only the exact
local rule/source and preserves other rules. Restore from a known source, then
follow parsing and any required GUI trust review; recovery does not grant trust.
Trust covers normalized definitions, not all script bytes, so script failure need
not change trust. Do not execute Hooks merely as a health test.

Plugin Hooks remain package-owned; damaged inventory can prevent component
management even when the instance itself can still be removed. An explicit
mutation may recreate unreadable trust state using owner defaults. Its settled
result does not establish previous enablement or another actor's actions.
A successful mutation need not be reloaded just to prove it; observe again for a
concrete contradiction or requested diagnosis.
