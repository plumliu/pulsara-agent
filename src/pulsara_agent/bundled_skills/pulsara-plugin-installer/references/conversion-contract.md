# Conservative Plugin Conversion Contract

Conversion uses the official selected-format importer before native validation
and installation, not model rewriting. It is not a runtime compatibility profile,
fallback parser or permission boundary.

## Selected Import Format

Select native, Claude, Codex or Cursor explicitly. The importer holds a safe
source observation, follows that distribution's declarations and creates a
temporary native candidate. Native packages go directly to the sole validator.
Do not first fail native validation to discover an already-selected format.

Do not convert a source that failed because of:

- a final or internal symlink;
- a socket, device, FIFO, path escape, or other special-file condition;
- the exact active API key appearing anywhere in the admitted source domain;
- unreadable, changing, replaced, or incompletely observed membership;
- cancellation or deadline settlement;
- ambiguous identity, license, or required behavior that the source itself does
  not resolve.

The model cannot repair or override these admission outcomes.

## Candidate Construction

The importer owns the attempt-local candidate and leaves the source unchanged.
It copies ordinary resources and the complete selected Skill directories. Vendor
control manifests are not activated or unioned; the importer writes the native
root `plugin.json`, root `mcp.json`, fixed `skills/`, and supported fixed
`dev.pulsara/` extension paths. Do not construct a second candidate with shell
commands or model-generated edits.

Use the Published Agent Plugins 1.0 schema identifier in the new root manifest.
Map identity and metadata only from exact source facts. Preserve the authored
name and version; missing required native metadata is an import error, not a
reason to invent identity.

Scripts, binaries, assets and selected Skill files are copied byte-for-byte. Do
not modify an executable or rewrite Skill instructions to make a Plugin portable.
The GUI preview shows selected components, ordinary connection parameters and
import notices; there is no separate conversion-report artifact or registry.

## Declaration Conversion

The official importer/native validator owns these checks. Use its supported
conversion and diagnostics; this list explains admission rather than requiring
the model to build a second validator or audit every script. The importer checks supported declarations, not arbitrary script semantics:

1. Install supported selected-format Skills, MCP connections and command Hooks.
2. Copy scripts and Skill resources unchanged; do not execute them during import.
3. Preserve supported MCP declarations and credential boundaries. Invalid supported
   configuration still blocks installation; unsupported settings are not guessed.
4. Report unsupported host components and Hook events/handler types in preview
   notices and installation diagnostics. They are not registered or executed.
5. Unreferenced directories are resources; do not infer another host's components.
6. Preserve licensing and truthful compatibility statements.
7. Validate the native candidate through the existing production owner.

Installation means the supported subset was installed, not that the original
harness behavior is preserved. Unknown source-host extensions are reported and
not activated. Invalid supported components, unsafe paths, source races and actual
credential literals still block installation. The model cannot override them.

For compatibility questions, explain the exact unsupported host dependency.
Do not manually construct a reduced fork or repair a failed supported declaration.
