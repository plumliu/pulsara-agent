# Conservative Plugin Conversion Contract

Conversion is a model-assisted authoring step before ordinary Pulsara validation
and installation. It is not a Runtime compatibility profile, fallback parser, or
permission boundary.

## Eligible Failures

Consider conversion only after the production validator has completed a safe,
read-only source observation and returned deterministic format or component
diagnostics. Typical eligible evidence includes a recognized vendor manifest path
with no Agent Plugins 1.0 root `plugin.json`, or a standard candidate whose exact
diagnostic identifies a representable declaration difference.

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

Create a new attempt-local directory and leave the source unchanged. Preserve all
safe ordinary resources unless a copied vendor control file would become active
under Agent Plugins 1.0 or Pulsara semantics. Original vendor manifests may remain
as inert resources; only the new root `plugin.json`, root `mcp.json`, fixed
`skills/`, and fixed `dev.pulsara/` extension paths carry Pulsara meaning.

Use the Published Agent Plugins 1.0 schema identifier in the new root manifest.
Map identity and metadata only from exact source facts. Preserve an authored name
and version. If the source host explicitly derives identity from a directory name,
that same normalized source basename may supply the missing name; otherwise do not
invent one.

Copy scripts, binaries, assets, and other ordinary resources byte-for-byte. Do not
modify an executable to make it portable. Skill frontmatter may be normalized only
when all nonportable information is preserved truthfully and the instructional
body retains the same meaning. Record every non-byte-identical text edit in the
call-local conversion report.

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

Presentation metadata can remain inert or be omitted from the active standard
manifest only when it never affects source behavior. Report it; do not promote it
to a Pulsara capability. If an optional-looking field is required for the Plugin's
stated purpose, it is active and blocks conversion when unsupported.

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
