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

Reuse the result's `identity.skill_path` for subsequent management. Details are
queried with `inspect_capability`, using an observed path rather than this example:

```json
{"target":{"kind":"SKILL","skill_path":"/absolute/project/.pulsara/skills/example-skill/SKILL.md"}}
```

`current.skill_root` is the installed directory; `current.skill_path` is its
`SKILL.md`. List copies with `list_capabilities`, `kind:"SKILL"`; inspect selection,
issues and management eligibility. Plugin components belong to their whole
Plugin, and bundled Skills are read-only. Discovery does not imply removability;
workspace `.agents` copies are not removal targets.

## Replace an existing copy

Installation never overwrites an existing destination. For an explicitly
requested update:

1. Prepare the replacement source before removing anything.
2. Inspect the exact old copy and its removal eligibility; reuse its scope.
3. Call `REMOVE_LOOSE_SKILL` with its installed `skill_path`, then install the
   replacement with `INSTALL_LOOSE_SKILL`.

The user's update request authorizes this sequence; do not ask again. Replacement
is not atomic. If installation fails after deletion, report both outcomes and
resolve the failure without replaying a completed removal.

## Verify or diagnose

Read mutation status and `adoption` separately. `APPLIED` means the source change
settled, not that every consumer adopted it or that a user form appeared.
First-party changes notify live sessions at their existing safe points; current
provider roots stay unchanged. Inspect reported adoption failures; explicit
`reload_capabilities` is for out-of-band changes or partial adoption, not a routine
second step. Retry observation/adoption rather than the completed install.

Read the installed Skill body only when its use or instruction verification is
requested; installation does not require reading every supporting resource.
A requested toggle uses `SET_LOOSE_SKILL_ENABLED` with the same `skill_path` and
`enabled`. Mutations retain ordinary permissions and any required user forms.
Do not bypass these owners with CLI, raw copies or managed-state edits.

Read `references/directory-contract.md` for directory admission, source-change
errors, copying rules or explicitly out-of-band administration.
