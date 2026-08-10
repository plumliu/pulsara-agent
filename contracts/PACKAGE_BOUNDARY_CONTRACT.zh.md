# Package and Composition Boundary Contract

## 1. Production roots

Production Host唯一入口是 `conversation_kernel.host.KernelHostCore`，公共 facade
`pulsara_agent.host.HostCore`只是 exact alias。CLI `host run/repl/tui`只构造 Kernel与 Protocol
v3。

Canonical implementation位于：

- `conversation_kernel/`；
- `terminal_protocol/` v3；
- `storage/migrations/` clean universe；
- `storage/postgres_connection_provider.py` binding v2；
- neutral `terminal_process/`与`terminal_client/` supervision；
- current `llm/` normalized transport与 `capability/` catalog。

## 2. Forbidden production packages

以下 owner已物理删除，不得以 facade、lazy import或test helper恢复：

- `runtime/`、`event/`、`event_log/`、`replay/`；
- `projection_jobs/`与 runtime projection graph；
- Presentation Foundation与terminal Protocol v2；
- Oxigraph/graph/JSON-LD/ontology/SPARQL packages；
- legacy Host、MCP execution/recovery、subagent recovery；
- runtime-write admission epoch/guard graph；
- raw provider、draft adoption与durable stream segment。

## 3. Test support

`src/`不得 import `tests/support`。Test support默认只构造 canonical repository、clean migration
universe与Protocol v3；不得暴露 RuntimeSession、EventLog、projection或old schema factory。

Obsolete test只有在对应 production owner同一 slice删除且 canonical successor test存在时才能
删除。不得使用 skip/xfail或compat shim维持旧 contract。

## 4. CLI 与 settings

CLI只公开 Host run/repl/tui/inspect、skills、db status/migrate/verify与config-check。Settings只
接受 LLM、PostgreSQL与embedding配置，不接受 Oxigraph、SPARQL、projection worker或runtime-write
epoch配置。
