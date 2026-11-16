# Local and Plugin Hooks

ROOT-native `manage_capability` changes trust and source enablement. WORKSPACE
means the current GUI project; quick-start sessions use USER.

## Find the source

```json
{"kind":"HOOK","scope":"WORKSPACE"}
```

Records contain a HOOK_SOURCE Target, status and definitions. To list a Plugin's
Hooks, use its PLUGIN Target as `parent`. An exact query uses:

```json
{"kind":"HOOK","target":{"kind":"HOOK_SOURCE","config_path":"/actual/project/.pulsara/hooks.json"}}
```

The returned `config_path` identifies the source. For a native OpenAI Plugin it
can identify a manifest; follow its referenced Hook files. Status distinguishes
enablement, trust and unavailable definitions.

## Create or edit a local Hook

Local definitions use user `<effective Pulsara home>/hooks.json` or project
`.pulsara/hooks.json`. Edit the requested source with file tools. For example,
this command records a diagnostic after a file read:

```json
{"hooks":{"PostToolUse":[{"matcher":"^read_file$","hooks":[{"type":"command","command":"printf 'file read\\n'","timeout":5}]}]}}
```

Commands run in the current workspace. Use case-sensitive RE2 matchers and
Pulsara tool names such as `read_file`. The [configuration reference](pulsara-hook-extension.md)
covers event inputs, model context, control outputs and Windows commands.
Plugin definitions are updated through Plugin source editing and replacement.

## Trust and enable

```json
{"target":{"kind":"HOOK_SOURCE","config_path":"/actual/project/.pulsara/hooks.json"},"action":"TRUST_HOOK_SOURCE"}
```

The user reviews the current commands and settings. Enabling a source and trusting
it are separate: trust preserves a disabled state, and disabling preserves trust.
Plugin sources also depend on their Plugin being enabled.

```json
{"target":{"kind":"HOOK_SOURCE","config_path":"/actual/project/.pulsara/hooks.json"},"action":"SET_HOOK_SOURCE_ENABLED","enabled":false}
```

```json
{"target":{"kind":"HOOK_SOURCE","config_path":"/actual/project/.pulsara/hooks.json"},"action":"REVOKE_HOOK_TRUST"}
```

All actions use the actual returned source Target. Changes apply to future events.
Read `meta.status` and `content` for the saved outcome and any refresh failure.

## Diagnose

Check the definition file, command script and source status for the reported
failure. A local source can be disabled or have trust revoked even when its file
is missing or invalid. Removing a local rule is a file edit; the management actions
preserve its definitions and scripts.

After restoring definitions, use the ordinary trust review for their changed
contents. Trust covers the definitions; a script failure can occur while those
definitions remain trusted. For damaged Plugin contents, recovery uses the Plugin
source and package workflow.
