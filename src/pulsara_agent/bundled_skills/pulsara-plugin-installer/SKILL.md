---
name: pulsara-plugin-installer
description: Import, install, configure, enable and delete native or portable Claude/Codex/Cursor plugins through Pulsara's existing capability owners.
---

# Pulsara Plugin Installer

The runtime consumes Agent Plugins 1.0 only. The official importer can convert a
selected Claude, Codex or Cursor distribution to a native candidate before the
sole native validator/publisher. Do not ask a model to rewrite supported formats.

## Workflow

1. Identify the exact local source directory, selected distribution format and
   USER/WORKSPACE scope. Acquiring a repository is separate from installing it;
   preserve licenses and do not run downloaded code while inspecting it.
2. Read the selected manifest and declarations. Empty component declarations
   mean empty; never union neighboring host manifests. Portable Skills must
   retain references/scripts/assets. Unsupported active Apps, agents, JS/TS
   host entrypoints or non-equivalent Hooks block whole-package import.
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
6. Enable separately using SET_PLUGIN_ENABLED. Show the current full component
   and credential-destination review; only user submission accepts launching
   external processes. A model cannot assert acceptance or treat missing keys
   as a failed package installation.
7. First-party changes automatically adopt at the existing safe point. Read
   mutation and adoption separately, then inspect the catalog and make a safe
   call when requested. Do not append an ordinary second `reload_capabilities`.
8. REMOVE_PLUGIN removes the exact instance and dedicated credentials, preserves
   Plugin data, and lets old consumers drain before package GC. It never edits
   the source or rewrites installed provider input.

The installed global `pulsara plugins` CLI remains an out-of-band administration
path (`validate`, `add`, `list`, `doctor`, `enable`, `disable`, `remove`,
`gc`). Check its actual help; foreign-format import belongs to the typed tool/UI
unless the CLI explicitly exposes it. CLI changes need explicit Host reload.
Do not use a checkout, raw copy, private installer or direct managed-state edits.

Read `references/conversion-contract.md` and `references/codex-compatible.md`
for importer semantics. Read `references/pulsara-hook-extension.md` before
claiming that a behavior-bearing Hook has an exact supported mapping.

Never guess credentials, endpoints, dependencies or compatibility. Do not create
a smaller Plugin by dropping active components; a separately requested authoring
task is different from importing an existing package. Ordinary permissions apply.
