---
name: pulsara-skill-installer
description: Install and inspect portable loose Agent Skills through the running Host's manage_capability tool. Use when the user asks to install a local skill, choose workspace or user scope, list effective skills, or diagnose why a local skill is not effective.
---

# Pulsara Skill Installer

Use `manage_capability` for loose Skill management. Use terminal and file tools
only to obtain or edit a local source directory; the current Host owns the
installation target and its frozen Pulsara home.

## Workflow

1. Identify the exact local absolute source directory. Reuse the user's scope
   choice: `WORKSPACE` means the current GUI project, `USER` means all projects.
   Ask once only if the intended scope is genuinely ambiguous.
2. Call `manage_capability` with `INSTALL_LOOSE_SKILL`, the selected `scope` and
   absolute `source_path`. Optional `name` and `description` use the native
   candidate normalization; source files and references/scripts/assets remain
   intact. Never specify a destination workspace or propagate home environment.
3. Verify with `INSPECT_LOOSE_SKILLS` using the same scope, optionally filtering
   by the installed absolute `skill_path` (the copy's `SKILL.md`). Inspect
   disabled, shadowed, invalid, unavailable and operation eligibility separately.
4. Use `SET_LOOSE_SKILL_ENABLED` with the exact `skill_path` and boolean `enabled`
   when requested. Use `REMOVE_LOOSE_SKILL` only for an authorized exact copy;
   discoverable workspace `.agents` copies are not deletion targets.
5. When using the Skill or verifying its instructions is requested, read it through
   the ordinary Skill/read-file route. Installation alone does not require loading
   all resources. It does not rewrite the current provider prefix/running request.

## Minimal calls

Pass these objects to `manage_capability`; substitute the observed absolute
paths and reuse the user's scope. `source_path` is a directory; `skill_path` is
the installed copy's `SKILL.md`, not its directory.

```json
{"action":"INSTALL_LOOSE_SKILL","scope":"WORKSPACE","source_path":"/absolute/source/example-skill"}
```

```json
{"action":"INSPECT_LOOSE_SKILLS","scope":"WORKSPACE","skill_path":"/absolute/project/.pulsara/skills/example-skill/SKILL.md"}
```

For enable/disable use `SET_LOOSE_SKILL_ENABLED` with the same `skill_path` and
`enabled:true` or `false`. For removal use `REMOVE_LOOSE_SKILL` with `skill_path`.
`INSPECT_LOOSE_SKILLS` is allowed in READ_ONLY; mutations follow current
permissions. The installed path comes from the result; do not reconstruct it
from a terminal cwd or guessed home.

An existing destination is a conflict. An explicit request to update an exact
installed copy authorizes its separate remove/install sequence; reuse that scope
and do not ask again. Report a completed deletion if the later installation fails.
Never promise an atomic replacement or automatically retry a settled mutation.

`APPLIED` describes the source change; inspect adoption separately. Successful
first-party changes notify affected live sessions and use their existing safe
points. Do not routinely call reload. An adoption `PARTIAL` warrants inspecting
its reported parts; retry observation/adoption, never the completed install.

The capability page also offers direct directory import and management without a
model. Bundled Skills are read-only defaults and Plugin children belong to their
Plugin owner. Installing a loose directory does not execute neighboring host
plugins or create a receipt, ownership marker or managed provenance.

Standalone `pulsara skills` commands remain advanced offline administration.
They do not supply live-Host review/adoption. Do not use CLI, raw copies or private
managed-state writes to bypass the supported conversation flow.

Read `references/directory-contract.md` only when directory admission, copying
or source-change diagnostics need explanation; ordinary installation needs no
filesystem implementation audit.
