---
name: pulsara-plugin-installer
description: Install, configure, enable and remove native or portable Claude/Codex/Cursor plugins through manage_capability. Also inspect, trust, revoke trust or enable/disable local and Plugin Hook sources with user review.
---

# Pulsara Plugin Installer

The runtime consumes Agent Plugins 1.0 only. The official importer can convert a
selected Claude, Codex or Cursor distribution to a native candidate before the
sole native validator/publisher. Do not ask a model to rewrite supported formats.

## Workflow

1. Identify the exact local absolute source directory and selected distribution
   format. Reuse the user's USER/WORKSPACE scope; ask only if it is ambiguous.
   Acquiring a repository is separate from installing it;
   preserve licenses and do not run downloaded code while inspecting it.
2. Read the selected manifest to identify the format and components. The official
   importer owns exact conversion and native admission; use its result and
   diagnostics rather than auditing every script or reimplementing its checks.
   Empty declarations mean empty; never union neighboring host manifests.
   Unsupported host components are reported and not activated. Supported components must pass native validation; installation does not promise the original host behavior.
3. Use `manage_capability` INSTALL_PLUGIN with source_path and source_format
   (`native`, `claude`, `codex`, `cursor`). Use replace only for an
   explicitly requested replacement. The native parser and held-source
   revalidation still decide admission; never bypass source-race/secret/path errors.
4. The capability page provides a model-free import wizard: select a directory;
   its sole distribution is detected automatically. When multiple manifests exist,
   compare their component previews and select one, never union them. Classify
   fields and fill ordinary parameters, then install.
   Use this path for parameterized sources needing user input. Credential values
   never enter the candidate or INSTALL_PLUGIN arguments.
5. Installation creates a disabled instance. Configure its MCP connections with
   CONFIGURE_PLUGIN_MCP_CONNECTION or the shared connection editor; the user
   supplies private values there. OAuth uses AUTHORIZE_MCP for the exact instance.
6. Enable separately using SET_PLUGIN_ENABLED. The Host presents the current full
   component and credential-destination review; only user submission accepts launching
   external processes. A model cannot assert acceptance or treat missing keys
   as a failed package installation.
7. First-party changes automatically adopt at the existing safe point. Read
   mutation and adoption separately, then inspect the catalog and make a safe
   call when requested. Do not append an ordinary second `reload_capabilities`.
8. REMOVE_PLUGIN removes the exact instance and dedicated credentials, preserves
   Plugin data, and lets old consumers drain before package GC. It never edits
   the source or rewrites installed provider input.

## Minimal calls

Every `manage_capability` call includes `action` and explicit uppercase `scope`.
For a selected Claude distribution:

```json
{"action":"INSTALL_PLUGIN","scope":"WORKSPACE","source_path":"/absolute/source/plugin","source_format":"claude"}
```

Use the actual format (`native`, `claude`, `codex`, `cursor`). After a successful
install or replacement, reuse the returned `identity.plugin_id` and `scope` for
subsequent management; do not derive the target from the source directory name.
The successful result also gives `current.package_root`, the exact installed package.
For details pass `target={kind:"PLUGIN",scope:<returned scope>,plugin_id:<returned id>}`
to `inspect_capability`. For its capabilities copy that target to `list_capabilities.parent`.
Plugin is a source, not a list kind; complete package inventory stays in the GUI.
Installed means disabled, not enabled or trusted.

```json
{"action":"SET_PLUGIN_ENABLED","scope":"WORKSPACE","plugin_id":"example","enabled":true}
```

Enable requires the current user review. For disable set `enabled:false`; for
removal use `REMOVE_PLUGIN` with `plugin_id`. To edit a Plugin MCP connection use
`CONFIGURE_PLUGIN_MCP_CONNECTION` with `plugin_id` and exact local `server_id`;
omit `overlay` to open the current shared editor. `overlay:null` explicitly clears
it. OAuth uses `AUTHORIZE_MCP` / `CLEAR_MCP_AUTHORIZATION` with both IDs. Expected
guards may be omitted for fresh native inspection; never invent them or secrets.

## Hook source management

Local and Plugin Hooks use the same `manage_capability` actions. A local source
is bound by Host scope, not an arbitrary path:

```json
{"action":"TRUST_HOOK_SOURCE","scope":"WORKSPACE","source_kind":"LOCAL"}
```

For a Plugin source use `source_kind:"PLUGIN"` and the exact `plugin_id` instead.
Do not add `source_path`, a digest or a user-acceptance flag to Hook calls.
Trust presents the full current definitions in the GUI even under full access.
Hook enablement does not grant trust; Plugin enablement remains separate.

Use `list_capabilities` with `kind:"HOOK_SOURCE"` and copy the exact target into
`inspect_capability` to read current definitions and authorization. Use
`SET_HOOK_SOURCE_ENABLED` with `enabled` to switch one, and `REVOKE_HOOK_TRUST`
to revoke trust. Inspection is
allowed in READ_ONLY. Read `references/hook-source-management.md` for local
source authoring or exact mutation examples; it is enough for ordinary Hook work.

## Import diagnostics and advanced administration

The installed global `pulsara plugins` CLI remains an out-of-band administration
path (`validate`, `add`, `list`, `doctor`, `enable`, `disable`, `remove`,
`gc`). Check its actual help; foreign-format import belongs to the typed tool/UI
unless the CLI explicitly exposes it. Adopt CLI changes through GUI refresh or
the existing Host reload operation when permitted; CLI does not automatically
update live sessions.
Do not use a checkout, raw copy, private installer or direct managed-state edits.

Read `references/conversion-contract.md` when import diagnostics need explanation,
`references/codex-compatible.md` for Codex-specific incompatibility, and
`references/pulsara-hook-extension.md` for detailed Hook conversion or control
semantics. These are troubleshooting references, not mandatory pre-install reads.

Never guess credentials, endpoints, dependencies or compatibility. Use the official
importer for supported components and report its diagnostics. Do not manually rewrite
the package or claim unsupported host behavior is preserved. Ordinary permissions apply.

Use terminal/file tools to prepare sources, then the typed management tool to
install, configure and authorize them. Do not use terminal Hook trust/`--yes` to
bypass user review.
