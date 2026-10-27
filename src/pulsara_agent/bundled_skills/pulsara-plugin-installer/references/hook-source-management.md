# Local and Plugin Hook Source Management

Observation uses `list_capabilities` / `inspect_capability`; mutation examples are
objects for `manage_capability`, not terminal commands. Reuse
the chosen uppercase scope. `WORKSPACE` is the current GUI project;
`USER` is the Host's frozen Pulsara home. A quick/transient session cannot manage
a WORKSPACE Hook source; do not silently switch to USER.

## Observe and identify the source

```json
{"target":{"kind":"HOOK_SOURCE","scope":"WORKSPACE","source_kind":"LOCAL"}}
```

Pass this to `inspect_capability`. It returns `config_path`, diagnostics,
authorization for the current observed definitions and their execution content.
It does not return trust digests or internal review data. An unavailable observation is unknown, not empty; unknown adoption does not erase declared definitions. Local paths are the project's
`.pulsara/hooks.json` or the effective user home `hooks.json`; use the observed
absolute path for file editing rather than guessing cwd or a custom home.
Call `list_capabilities` with `kind="HOOK_SOURCE"` and the chosen `scope`, omitting `source_kind` to observe both local and Plugin sources; copy a returned target into `inspect_capability.target`.
To select a Plugin source use `source_kind:"PLUGIN"` and its `plugin_id`.

## Prepare local definitions

Only create/edit the resolved local source when requested. For a simple command
after a file read, the native JSON shape is:

```json
{"hooks":{"PostToolUse":[{"matcher":"^read_file$","hooks":[{"type":"command","command":"printf 'file read\\n'","timeout":5,"async":false,"additionalContextLimit":300}]}]}}
```

The source parser and trust owner validate the complete current declaration.
Creating or editing this file does not grant trust or execute it. Matchers use
case-sensitive RE2; native names such as `read_file` and `manage_capability` work.
Read `pulsara-hook-extension.md` only for other events, inputs, output control,
Windows commands or Plugin conversion. Plugin definitions belong to the immutable
package and are not edited as loose sources.

## Trust, enable, disable or revoke

```json
{"action":"TRUST_HOOK_SOURCE","target":{"kind":"HOOK_SOURCE","scope":"WORKSPACE","source_kind":"LOCAL"}}
```

```json
{"action":"SET_HOOK_SOURCE_ENABLED","target":{"kind":"HOOK_SOURCE","scope":"WORKSPACE","source_kind":"LOCAL"},"enabled":false}
```

```json
{"action":"REVOKE_HOOK_TRUST","target":{"kind":"HOOK_SOURCE","scope":"WORKSPACE","source_kind":"LOCAL"}}
```

For a Plugin source, set target.source_kind to PLUGIN and include target.plugin_id.
Never send `source_path`, `skill_path`, a digest, `--yes` or an acceptance boolean.
`TRUST_HOOK_SOURCE` requires GUI review of all current commands, environment,
events, matchers and execution fields, even in full-access mode. Enable/disable
and revoke use their normal permission path, without another full trust review.
Hook trust does not enable a
disabled Plugin. Hook enablement does not establish trust. DISABLED may retain
an existing trust record; do not interpret it as an untrusted definition.

Read mutation and adoption separately. APPLIED / PARTIAL means the change settled
but some live adoption needs attention; inspect reported parts without replaying
the mutation. Adopted definitions apply to future events. The management call's
own Pre/Permission/Post use its captured predecessor view, so newly trusted Hooks
cannot trigger their own trust call's PostTool. Inspection is read-only and never
grants trust or activates an observed source merely because it was inspected.

## Missing or damaged sources

Distinguish the definition file, source enablement, trust state and command script.
For a local missing/invalid definition, requested disable or revoke still uses the
same source owner; it does not delete definitions or scripts. Requested deletion
edits only the exact local rule/source and preserves other rules. Restore local
definitions or scripts from a known source, then follow normal parsing/adoption
and any required GUI trust review; recovery never grants trust automatically.
Trust covers normalized definitions, not every script's bytes, so script failure
need not change trust. Diagnose without executing Hooks merely as a health test.
Plugin Hooks remain package-owned; if package inventory is unavailable, component
management may be unavailable even when the instance can still be removed.

When definitions or trust state are unavailable, enablement in the observed view may
be unknown; use the exact disable/revoke settlement for that completed action.
An explicit mutation may recreate unreadable trust state using the owner's
defaults. Its resulting enablement cannot prove an earlier state or external re-enable.
An unavailable view does not undo a successful mutation or require reloading to
prove it. Observe again only for a concrete contradiction or requested diagnosis.
