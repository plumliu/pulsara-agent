# Pulsara Loose Local Skill Directory Contract

Official destinations are:

```text
<workspace>/.pulsara/skills/<skill-name>/SKILL.md
${PULSARA_HOME}/skills/<skill-name>/SKILL.md
```

The production Runtime discovers these roots together with workspace and user `.agents/skills` roots. Direct user copy, edit, rename, and deletion remain valid filesystem paths; no receipt or marker makes a Skill authoritative.

## Skill Folder Rules

- The folder name must match the `name` in `SKILL.md`.
- `SKILL.md` must begin with YAML frontmatter.
- The frontmatter must include string fields named `name` and `description`.
- Skill names use lowercase letters, digits, and hyphens only.
- Optional `license`, `compatibility`, and bounded string-to-string `metadata` remain portable data.
- Ordinary files and directories are copied, including useful `scripts/`, `references/`, and `assets/` resources.
- Symlinks, sockets, devices, and FIFOs are outside the official loose-copy domain. `__pycache__` directories and `.DS_Store` files are ignored. `.pulsara-skill-source.json`, if present, is ordinary inert resource data and is copied byte-for-byte; it has no ownership or provenance authority.

The official installer validates and copies one frozen source observation into a hidden sibling stage, verifies exact contents and portable modes, and publishes with the platform's exclusive no-replace directory rename. Existing destinations are never overwritten. The exclusive primitive guarantees final-name no-replace within the held root; it is not a sandbox or a power-loss durability promise, and same-UID replacement of the private stage or target-root namespace after the final binding cut is outside the product concurrency contract.

After installation, copy the returned `identity.skill_path` into
`inspect_capability.target={kind:"SKILL",skill_path:<returned path>}` to inspect the copy, its selection,
diagnostics and management eligibility. Read its instructions and resources
through the ordinary Skill/read-file route. First-party changes notify affected
live sessions at their existing safe points; inspect adoption separately from
the completed filesystem mutation. Current provider roots remain unchanged.

Pulsara package Skills are read-only defaults loaded directly from the installed package. They are not copied into these roots and have no sync, status, reset, manifest, backup, or opt-out state. A loose same-name Skill shadows the package definition until the user removes that loose directory.

## User import and advanced administration

The capability page offers directory import and management without a model.
Installing one loose directory does not execute neighboring host plugins.

Standalone `pulsara skills` commands are an explicitly out-of-band administration
path; inspect their actual help. They do not supply live-Host review/adoption.
Ordinary conversation management uses `manage_capability`, not CLI, raw copies
or private state edits. User filesystem edits remain supported; adopt external
changes through the GUI refresh or an allowed `reload_capabilities` call.
