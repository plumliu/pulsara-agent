---
name: pulsara-skill-installer
description: Install or replace a loose Agent Skill from a local source and resolve installation conflicts or unexpected same-name selection.
---

# Pulsara Skill Installer

## Install

Prepare a local directory containing `SKILL.md`, then use ROOT-native
`manage_capability`. `USER` installs under the effective Pulsara home;
`WORKSPACE` installs under the current GUI project's `.pulsara/skills`.
Quick-start sessions use USER because their temporary directory is not a project.
Reuse the user's intended scope; clarify it when the choice affects the task.

```json
{"target":{"kind":"PATH","path":"/absolute/source/example-skill"},"action":"INSTALL_LOOSE_SKILL","scope":"WORKSPACE"}
```

The document's valid frontmatter supplies the name and description. Its name
selects the installed directory, so a downloaded source directory may have a
different name. Supporting scripts and resources are copied with the Skill.

The result's SKILL Target identifies the installed `SKILL.md`. Use that Target
for management and its `skill_path` in a read_file PATH Target to read instructions.
ROOT can inspect the copy with:

```json
{"kind":"SKILL","target":{"kind":"SKILL","skill_path":"/absolute/project/.pulsara/skills/example-skill/SKILL.md"}}
```

Installation and enablement changes apply in existing sessions after refresh.
A fresh query's status explains whether this copy is enabled and selected;
a same-name copy can take precedence over it.

## Edit or replace

Edit an installed loose Skill in place for content changes; see
`pulsara-skill-creator`. Installation creates a new directory and reports a
conflict when that destination already exists.

To replace a copy from another source, prepare the new source first, then use
`REMOVE_LOOSE_SKILL` on the old SKILL Target and `INSTALL_LOOSE_SKILL` in the same
scope. If the second step fails, report the completed removal and the installation
error, then continue from that result.

`SET_LOOSE_SKILL_ENABLED` changes a copy's enabled setting using its SKILL Target
and `enabled:true|false`. Both `.pulsara/skills` and `.agents/skills` copies support
these settings. Removal through `REMOVE_LOOSE_SKILL` covers Pulsara home `skills`
and project `.pulsara/skills`; `.agents/skills` copies remain editable files.
Plugin components use Plugin management. Bundled Skills are packaged defaults;
a loose same-name copy provides customization.

## Read the result

`meta.status` and `content` describe what happened. ERROR can include an installed
copy followed by a refresh or cleanup failure; continue with that failed step
rather than repeating a completed installation. Normal installation refreshes
capabilities automatically. Use `reload_capabilities` for reported refresh problems
or changes made outside the running app.

For a damaged copy, inspect the named path and the concrete error. A managed loose
directory can be removed even when its document is missing or invalid. Preserve
the distinction between the source directory and installed copy during recovery.

Read [directory details](references/directory-contract.md) for source layout,
copying rules or standalone administration.
