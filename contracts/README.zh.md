# Pulsara 契约文档索引

_Created: 2026-07-04_

本目录是 Pulsara 的长期契约真源。根目录中的调研、审计、实施文档可以作为历史背景，但不作为最终代码契约。

阅读规则：

- 若代码与根目录设计文档冲突，以代码和本目录契约为准。
- 若代码与本目录契约冲突，需要修代码或修契约；不得默认“文档过期”。
- 新增 runtime 主线能力时，应优先在本目录新增/更新契约，再把根目录实施文档视为可丢弃草稿。

---

## 1. Runtime 主循环与会话

- [PACKAGE_FACADE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/PACKAGE_FACADE_CONTRACT.zh.md)
  - 覆盖 Python package facade、lazy import 边界、public `__all__`、CLI script import 边界。
- [APP_SETTINGS_CLI_ENTRY_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/APP_SETTINGS_CLI_ENTRY_CONTRACT.zh.md)
  - 覆盖 settings/env-file、CLI 命令面、Host run/repl/inspect、REPL plan/approval/resume/compaction UI、bundled skills 管理入口。
- [AGENT_RUNTIME_LOOP_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md)
  - 覆盖 `AgentRuntime` 的 run start、capability exposure、model/tool loop、pending state、plan workflow、MCP elicitation、mid-turn compact safe point、run finalization。
- [LLM_TRANSPORT_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/LLM_TRANSPORT_CONTRACT.zh.md)
  - 覆盖 LLM role/model selection、provider-neutral request、transport event translation、usage、retry、provider profiles。
- [EVENT_LOG_STORAGE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md)
  - 覆盖 typed event log、Postgres runtime truth schema、sequence、parent rows、run projection、artifact storage。
- [POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md)
  - 覆盖 PostgreSQL migration registry/ledger、admin/runtime role、verify-only startup、physical connection provider、部署与测试数据库边界。
- [RUNTIME_EVENT_PUBLISHING_HOOKS_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/RUNTIME_EVENT_PUBLISHING_HOOKS_CONTRACT.zh.md)
  - 覆盖 post-commit runtime event publisher、ordered subscriber delivery、RuntimeHookManager、observer hook isolation、tool-result error event helpers。
- [MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MESSAGE_TRANSCRIPT_CONTEXT_CONTRACT.zh.md)
  - 覆盖 message blocks、event replay、prior transcript reconstruction、LLM context render、tool-result aggregate budget。
- [INSPECTOR_PROJECTION_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md)
  - 覆盖 inspector read-only projections、runtime timeline、diagnostics、health checks。
- [WORKSPACE_TERMINAL_LIFECYCLE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/WORKSPACE_TERMINAL_LIFECYCLE_CONTRACT.zh.md)
  - 覆盖 `HostCore` / registry / supervisor / `HostSession` / `RuntimeSession` 的 ownership、close、workspace-scoped terminal supervisor。
- [HOST_RESUME_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/HOST_RESUME_CONTRACT.zh.md)
  - 覆盖 durable conversation manifest、resume、dangling run repair、transcript replay。
- [RECOVERY_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/RECOVERY_CONTRACT.zh.md)
  - 覆盖 failed / aborted recovery projection、abort kind、unfinished tool guidance。

---

## 2. Capability / permission / tools

- [CAPABILITY_SURFACE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/CAPABILITY_SURFACE_CONTRACT.zh.md)
  - 覆盖 unified capability surface、descriptor、provider、exposure plan、skill progressive disclosure、active skill attribution、capability gate。
- [BUILTIN_TOOLS_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/BUILTIN_TOOLS_CONTRACT.zh.md)
  - 覆盖 Tool/AsyncTool 协议、ToolRegistry、ToolExecutor、core registry、filesystem built-ins、todo、plan workflow tool fallback。
- [MCP_CAPABILITY_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MCP_CAPABILITY_CONTRACT.zh.md)
  - 覆盖 MCP manager、snapshot、binding bundle、tool name mangling、adapter、elicitation。
- [PERMISSION_POLICY_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/PERMISSION_POLICY_CONTRACT.zh.md)
  - 覆盖 permission presets、read-only host-local read、terminal hardline、action-level terminal_process observe、mode switching。
- [TERMINAL_ENV_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/TERMINAL_ENV_CONTRACT.zh.md)
  - 覆盖 terminal env builder、shell snapshot、PATH/env merge、diagnostics。
- [TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md)
  - 覆盖 terminal streaming preview、tool-result artifact、terminal completion event、adaptive preview/read-more。
- [ARTIFACT_STORE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/ARTIFACT_STORE_CONTRACT.zh.md)
  - 覆盖 artifact payload store、session/run ownership、tool-result artifact index、`artifact_read` read-side 边界。

---

## 3. Memory / compaction / continuity

- [GRAPH_JSONLD_STORAGE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/GRAPH_JSONLD_STORAGE_CONTRACT.zh.md)
  - 覆盖 JSON-LD entity、ontology registry、GraphStore named graph 语义、Postgres projection、Oxigraph materialization substrate。
- [MEMORY_SURFACES_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MEMORY_SURFACES_CONTRACT.zh.md)
  - 覆盖 governed canonical memory graph、runtime semantic graph、recall/reflection/run timeline、relatedness、Postgres substrate、outbox surface。
- [GOVERNANCE_WRITE_OUTBOX_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/GOVERNANCE_WRITE_OUTBOX_CONTRACT.zh.md)
  - 覆盖 memory governance executor、PostgreSQL UOW、relatedness destructive-action gate、mutation outbox、coordinator。
- [RETRIEVAL_RUNTIME_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/RETRIEVAL_RUNTIME_CONTRACT.zh.md)
  - 覆盖 embedding/rerank/tokenizer provider protocols、HostCore-owned retrieval resources、workers、bounded shutdown。
- [CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md)
  - 覆盖 typed compaction events、summary artifact、rehydration、mid-turn inline compact、manual/preflight safe point。
- [RUNTIME_SEMANTIC_GRAPH_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/RUNTIME_SEMANTIC_GRAPH_CONTRACT.zh.md)
  - 覆盖 run timeline persistence、execution evidence ledger、runtime semantic outbox lane、working_context 非记忆投影边界。
- [EVAL_DOGFOOD_GATE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/EVAL_DOGFOOD_GATE_CONTRACT.zh.md)
  - 覆盖 deterministic recall/relatedness eval gate、real-LLM dogfood opt-in、发布前证据与禁止事项。

---

## 4. 后续契约维护规则

截至 2026-07-04，本索引已覆盖当前主要 runtime / host / capability / permission / terminal / memory / compaction / eval 契约。

后续若新增以下类别能力，必须同步新增或更新契约：

- 新的 persisted runtime event 类型或 transcript reconstruction 规则；
- 新的 capability provider / MCP transport / tool execution binding；
- 新的 memory mutation lane / async surface / recall channel；
- 新的 permission mode 或 tool access scope；
- 新的 context compaction safe point 或 summary format；
- 新的 eval gate / dogfood 发布门槛。

不要把根目录实施文档中的“下一步计划”默认为契约；只有进入本目录并与代码/测试对齐后才算冻结。
