---
name: pulsara-skill-installer
description: Install or replace a loose Agent Skill from a local source, and diagnose installation conflicts or ineffective copies. Use for these workflows; ordinary queries and known-copy enable/disable/removal use the capability tools directly.
---

# Pulsara Skill Installer

## Install

Prepare one local source directory, then pass its absolute path to
`manage_capability`. Reuse the user's scope: `WORKSPACE` is the GUI project,
`USER` is the Host home. Ask only when that choice is ambiguous.

```json
{"action":"INSTALL_LOOSE_SKILL","scope":"WORKSPACE","source_path":"/absolute/source/example-skill"}
```

The Host chooses the destination; terminal cwd does not. Use terminal/file tools
for downloading or editing the source, and the management tool for installation.
Optional `name` and `description` normalize metadata without rewriting the source.

Reuse the result's `target` for subsequent management. Details are
queried with `query_capabilities`, using an observed path rather than this example:

```json
{"target":{"kind":"SKILL","skill_path":"/absolute/project/.pulsara/skills/example-skill/SKILL.md"}}
```

`current.skill_root` is the installed directory; `current.skill_path` is its
`SKILL.md`. List copies with `query_capabilities`, `kind:"SKILL"`; inspect selection,
issues and management eligibility. Plugin components belong to their whole
Plugin, and bundled Skills are read-only. Discovery does not imply removability;
workspace `.agents` copies are not removal targets.

## Replace from another source

For content edits to an installed loose Skill, use `pulsara-skill-creator` to
edit it in place. Installation never overwrites an existing destination.

When the user requests replacement from another source, prepare it first, inspect
the old copy's removal eligibility and reuse its scope. Call `REMOVE_LOOSE_SKILL`
with its `SKILL` target, then `INSTALL_LOOSE_SKILL` with the replacement
source. That request authorizes the sequence; do not ask again. It is not atomic:
if installation fails after deletion, report both outcomes and resolve the failure
without replaying removal.

## Verify or diagnose

Capability-page actions may produce a brief Runtime result notice. Use its
reported origin and outcome without repeating the action or inventing leftover
installation records. Missing files alone do not prove uninstallation. For
troubleshooting inspect the exact target; known-target management can proceed
directly. Acknowledge a settled notice from its result; do not query or reload
merely to confirm it. Diagnose reported damage using the exact public target
and relevant files, without auditing Runtime implementation as a routine step.

An owned loose directory can be removed even if SKILL.md is missing or invalid;
use REMOVE_LOOSE_SKILL rather than creating a document to make removal work.
An absent directory can use ordinary installation from an available source.
Keep other copies and source directories distinct; reinstalling does not promise
enablement or remove all same-name Skills.

Read mutation status and `adoption` separately. `APPLIED` means the source change
settled, not that every consumer adopted it or that a user form appeared.
First-party changes notify live sessions at their existing safe points; current
provider roots stay unchanged. Inspect reported adoption failures; explicit
`reload_capabilities` is for out-of-band changes or partial adoption, not a routine
second step. Retry observation/adoption rather than the completed install.

Installation does not require reading every supporting resource.
A requested toggle uses `SET_LOOSE_SKILL_ENABLED` with the same target and
`enabled`. Mutations retain ordinary permissions and any required user forms.
Use these owners for installation, removal and enablement; content editing uses
ordinary file tools.

Read `references/directory-contract.md` for directory admission, source-change
errors, copying rules or explicitly out-of-band administration.
