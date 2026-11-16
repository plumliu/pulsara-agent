---
name: pulsara-skill-installer
description: Install or replace a loose Agent Skill from a local source, and diagnose installation conflicts or ineffective copies. Use for these workflows; ordinary queries and known-copy enable/disable/removal use the capability tools directly.
---

# Pulsara Skill Installer

## Install

Prepare one local source directory, then pass its absolute path to
ROOT-native `manage_capability`. Reuse the user's scope: `WORKSPACE` is the GUI project,
`USER` is the Host home. Ask only when that choice is ambiguous.

```json
{"target":{"kind":"PATH","path":"/absolute/source/example-skill"},"action":"INSTALL_LOOSE_SKILL","scope":"WORKSPACE"}
```

The Host chooses the destination; terminal cwd does not. Use terminal/file tools
for downloading or editing the source, and the management tool for installation.
Name and description come from valid source frontmatter.

Reuse the result's `target` for subsequent management. Details are
queried with `query_capabilities`, using an observed path rather than this example:

```json
{"target":{"kind":"SKILL","skill_path":"/absolute/project/.pulsara/skills/example-skill/SKILL.md"},"kind":"SKILL"}
```

The Target's skill_path identifies SKILL.md; its parent contains relative
resources. List copies with kind SKILL and read their source/status text.
Plugin components belong to their whole Plugin; bundled Skills are read-only.
Discovery does not imply removability, and .agents/skills copies are not loose
removal targets. Management checks the actual copy when called.

## Replace from another source

For content edits to an installed loose Skill, use `pulsara-skill-creator` to
edit it in place. Installation never overwrites an existing destination.

When the user requests replacement from another source, prepare it first, identify
the exact old copy and reuse its scope. Call `REMOVE_LOOSE_SKILL`
with its `SKILL` target, then `INSTALL_LOOSE_SKILL` with the replacement
source. That request authorizes the sequence; do not ask again. It is not atomic:
if installation fails after deletion, report both outcomes and resolve the failure
without replaying removal.

## Verify or diagnose

A capability-page result notice can be acknowledged without repeating its action
or querying merely to confirm it. Diagnose concrete damage using the exact Target
and relevant files; missing one file does not prove removal.

An owned loose directory can be removed even if SKILL.md is missing or invalid;
use REMOVE_LOOSE_SKILL rather than creating a document to make removal work.
An absent directory can use ordinary installation from an available source.
Keep other copies and source directories distinct; reinstalling does not promise
enablement or remove all same-name Skills.

Read meta.status and actual content. ERROR may still describe a completed change
followed by cleanup or refresh failure. Address the failed step without replaying
the install. ROOT-native reload_capabilities is for out-of-band changes or
reported incomplete adoption, not a routine second step.

Installation does not require reading every supporting resource.
A requested toggle uses `SET_LOOSE_SKILL_ENABLED` with the same target and
`enabled`. Mutations retain ordinary permissions and any required user forms.
Use these owners for installation, removal and enablement; content editing uses
ordinary file tools.

Read `references/directory-contract.md` for directory admission, source-change
errors, copying rules or explicitly out-of-band administration.
