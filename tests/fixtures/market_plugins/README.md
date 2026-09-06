# Minimal market manifest fixtures

These are unmodified upstream manifests, not complete distributable Plugins.
Tests add only the local resources needed for the property under test. No source
code is executed, and these cases do not claim remote account authorization.

- `sentry-claude.json`: `getsentry/plugin-claude`, commit
  `73e53541d7af21672e27428c7067f4264b8a3d65`, `.claude-plugin/plugin.json`.
  Tests inline HTTP declaration and implicit default discovery.
- `slack-codex.json`: `slackapi/slack-mcp-plugin` (repository metadata names
  `slack-skills-plugin`), commit `1579a071323da95c9b4a59fb6d510f7336c021e3`,
  `.codex-plugin/plugin.json`. Tests explicit empty MCP versus neighboring
  Claude declarations and inert interface metadata.

Both are MIT; original copyright notices are retained in `LICENSE`.
