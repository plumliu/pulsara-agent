# Pulsara Loose Local Skill Directory Contract

Choose `WORKSPACE` to install for the current project or `USER` to install for
the current user. The installer selects the destination and returns the absolute
`target.skill_path`. Use that path to read or edit the installed Skill; resolve
its scripts, references and assets relative to the directory containing `SKILL.md`.

The Runtime also discovers workspace and user `.agents/skills` roots. Loose Skills are ordinary files, with no receipt or ownership marker. Models may edit their installed contents in place under ordinary file permissions; see `pulsara-skill-creator`. Direct user copy, edit, rename and deletion remain supported.

## Skill Folder Rules

- The folder name must match the `name` in `SKILL.md`.
- `SKILL.md` must begin with YAML frontmatter.
- The frontmatter must include string fields named `name` and `description`.
- Skill names use lowercase letters, digits, and hyphens only.
- Optional `license`, `compatibility`, and bounded string-to-string `metadata` remain portable data.
- Ordinary files and directories are copied, including useful `scripts/`, `references/`, and `assets/` resources.
- Symlinks, sockets, devices, and FIFOs are outside the official loose-copy domain. `__pycache__` directories and `.DS_Store` files are ignored. `.pulsara-skill-source.json`, if present, is ordinary inert resource data and is copied byte-for-byte; it has no ownership or provenance authority.

The installer copies one frozen source observation and publishes without
overwriting an existing destination. Source races, unsafe entries or validation
failures must be resolved through that owner, not bypassed by a manual package copy.

After installation, query with kind SKILL and the confirmed SKILL Target to
observe source/status. Read its instructions with a read_file PATH Target for
skill_path; resolve resources beside SKILL.md. Read management meta.status and
content for actual filesystem effects and cleanup/refresh failures.

Pulsara package Skills are read-only defaults loaded directly from the installed package. They are not copied into these roots and have no sync, status, reset, manifest, backup, or opt-out state. A loose same-name Skill shadows the package definition until the user removes that loose directory.

## User import and advanced administration

The capability page offers directory import and management without a model.
Installing one loose directory does not execute neighboring host plugins.

Standalone `pulsara skills` commands are an explicitly out-of-band administration
path; inspect their actual help. They do not supply live-Host review/adoption.
Conversation installation, removal and enablement use ROOT-native
`manage_capability`.
Content edits are observed by subsequent Skill snapshots without reinstalling or
routinely reloading; already prepared snapshots and provider prefixes stay unchanged.
