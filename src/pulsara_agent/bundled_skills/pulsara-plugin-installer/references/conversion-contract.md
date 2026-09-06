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

## Semantic Preservation Test

Conversion succeeds only when all of these statements are true:

1. Every active source component has one exact supported destination.
2. Skill instructions and invocation expectations retain their meaning.
3. MCP commands, arguments, working directories, URLs, headers, and environment
   declarations remain exact apart from a standard package-root placeholder
   representation with the same resolved value.
4. Every Hook has an exact supported event, matcher, handler, and control meaning.
   The full target is defined by `pulsara-hook-extension.md`, including stdin,
   environment, output, authority, and lifecycle differences.
5. Required host tools, Apps, permissions, dependency installation, PATH changes,
   background services, and UI behavior are either natively represented or absent.
6. License and compatibility statements remain truthful.
7. The sole Pulsara production validator returns `VALID` for the complete candidate.

Known presentation metadata may be omitted from the active standard manifest
when it never affects source behavior. Do not promote it to a Pulsara capability.
Unknown behavior-bearing fields block conversion rather than being silently
discarded. The importer owns the supported field set, not a model's judgment.

## Honest Stop

Stop as soon as preserving the whole Plugin would require guessing, substituting,
executing source code, dropping an active component, or changing user-visible
behavior. Report:

- the detected source format;
- the exact blocking files and components;
- what Pulsara representation is missing;
- why omission or substitution would change the Plugin's intent;
- that the original source was unchanged and no Plugin was installed.

Do not create a reduced fork unless the user separately asks for Plugin authoring.
