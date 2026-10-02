# Public MCP Configuration and Advanced Administration

## Complete public candidates

Ordinary setup omits `config` so the shared editor collects connection inputs.
Supply an object only when the complete public settings are known. A connection
summary from inspection is not an UPDATE template; UPDATE replaces the entire
entry, including settings that were not in that summary. Preserve current truth
or use the prefilled editor.

For a known Streamable HTTP service requiring no authentication:

```json
{"display_name":"Docs","enabled":true,"transport":{"type":"streamable_http","endpoint":"https://example.org/mcp"},"auth":{"type":"none"}}
```

This is the value of `config`, not a whole management call. Supply the user's
actual endpoint. For explicit legacy SSE use transport type `sse`; do not probe
transport types by guesswork. Explicitly approved loopback HTTP requires
`allow_http_localhost:true` inside transport. It does not authorize non-loopback
plain HTTP or broaden network permission.

A stdio transport uses `type:"stdio"`, `command`, an `args` array, optional
workspace-relative `cwd`, and public-only `env`. Obtain the vendor's actual MCP
command instead of assuming its ordinary CLI is an MCP server. Authentication,
managed environment/header secrets and unknown configuration go through the
editor; never place secret values in candidates or read private settings to
construct them. Environment references are an advanced option, not a reason to
export credentials.

## User-only import

The capability page imports user-selected JSON/JSONC configurations without a
model: preview entries, classify header/environment values, fill inputs, save,
then test. File templates require an explicit user-selected file; do not read
arbitrary local files or execute `headersHelper` commands. Unsupported behavior
is not silently dropped.

## Explicitly out-of-band administration

The installed `pulsara mcp` CLI exposes `list`, `add`, `doctor`, `enable`, `disable`
and `remove`; check its actual `--help` for arguments. That process does not own a
running Host, so adopt its changes through GUI refresh or `reload_capabilities`
when permitted. A successful CLI mutation does not promise live adoption.

Ordinary conversation management uses `manage_capability`; do not use CLI,
repository checkouts, private scripts or raw YAML writes to bypass its review and
credential owners. If the installed launcher is missing, report the distribution
problem instead of substituting a checkout runtime. Host tool availability is
independent of the shell PATH.
