# Public MCP Configuration and Advanced Administration

## Public candidates

Ordinary setup omits config so the shared editor collects inputs. ADD candidates
need an explicit transport and its required fields. For a known Streamable HTTP
service requiring no authentication, config can be:

```json
{"transport":{"type":"streamable_http","endpoint":"https://example.org/mcp"},"auth":{"type":"none"}}
```

Use the actual endpoint; explicit legacy SSE uses type:sse. Stdio uses type:stdio,
command, optional args, workspace-relative cwd and public-only env. HTTP uses
endpoint and optional top-level public_headers, not process fields. A vendor CLI
is not necessarily an MCP server.

ADD defaults to enabled, auth:none and ROOT_AND_SUBAGENTS. UPDATE patches only
provided fields, including nested transport fields; omitted settings and private
configuration remain unchanged. env/public_headers mappings replace their editable
maps as a whole: {} clears one; omission preserves it. args:[] means no arguments.
Changing an independent transport requires the new branch's required fields.

Plugin updates cannot alter package command/args/cwd/transport or independently
toggle its connection. config:null removes its whole overlay, restoring package
settings and the default subagent scope; config:{} preserves it.

Auth selects only none/oauth/bearer/static_headers; selecting the same type keeps
its private settings. Credentials, unknown inputs and advanced policy—including
subagent restriction, local-HTTP permission and special OAuth settings—belong to
the user editor. Never put secrets in public env, headers, endpoint/query or argv,
or read private settings to construct config.

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

Missing config and malformed config are different. A confirmed missing file/entry
can use ordinary ADD_LOCAL_MCP when recovery is requested; do not fabricate an old
entry just to REMOVE it. If existing YAML/entry cannot be parsed, explain the
specific boundary and discuss restoring valid config; do not raw-edit managed
config to bypass review or credential owners. A missing entry does not prove
private credentials or local OAuth grants were cleared. Read meta.status and content for actual
settlement, cleanup and refresh effects; never promise revocation of remote tokens.
