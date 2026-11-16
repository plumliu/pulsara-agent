# Plugin formats and imports

## Select the distribution

| Format | Manifest |
| --- | --- |
| native | `plugin.json` at the Plugin root |
| claude | `.claude-plugin/plugin.json` |
| codex | `.codex-plugin/plugin.json` |
| cursor | `.cursor-plugin/plugin.json` |

Use the format matching the prepared distribution. The importer follows that
manifest and its defaults. Explicit empty component declarations remain empty;
neighboring host distributions remain resources.

Native packages follow Agent Plugins 1.0.0, with root `plugin.json`, optional
`skills/` and `mcp.json`. Their required manifest fields use the published schema.
External formats are converted by the installer; representable optional metadata
is retained and other metadata is explained in the result.

## Understand the available subset

A bad or unsupported Skill, MCP service, Hook file/group/handler or host feature
is reported separately while independent valid components install. A valid
package with no currently usable components can still install disabled.

External Skills use their valid frontmatter `name` for the standard discovery
entry. Original directories, bodies and resources are preserved; a package-relative
alias connects the entry to them when needed. Preview shows the accepted names.
Invalid documents and name conflicts are reported by their source paths.

An external MCP service needing public input or field classification can be
configured through the import wizard. A service still missing those choices
is left out of this installation; other components remain available. Private
values are supplied in the installed connection editor. Descriptive `note` text
is ignored with a notice; an unsupported authorization setting such as
`oauth_resource` leaves the affected service inactive.

Hooks accept a single file path, paths array, inline object or objects array.
Selected declarations are processed in order. Supported events and command
handlers use the existing [Hook format](pulsara-hook-extension.md).

## Resources and updates

The importer keeps scripts, assets and vendor manifests as resources. Contained
links preserve relative resource relationships. A path or resource problem is
reported at the affected component; core manifest or publication failure can
prevent installation.

Generated native manifests, MCP config and the single Pulsara Hook source describe
the selected subset. That Hook source is also written for an empty selection.
Links to generated configuration refer to its installed normalized contents.
Native OpenAI Hook declarations retain their actual manifest source path instead.

Installation and replacement leave the instance disabled. The result explains
imported and inactive features; runtime behavior can still depend on the original
host's inputs or APIs. Use the ordinary enablement and Hook reviews to activate
available components.

The capability-page wizard supports format selection and connection inputs.
For requested standalone administration, inspect `pulsara plugins --help`;
refresh changes made outside the app with the capability page or
`reload_capabilities`.
