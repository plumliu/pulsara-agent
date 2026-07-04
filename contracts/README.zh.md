# Pulsara 契约文档索引

_Created: 2026-07-04_

本目录是 Pulsara 的长期契约真源。根目录中的调研、审计、实施文档可以作为历史背景，但不作为最终代码契约。

阅读规则：

- 若代码与根目录设计文档冲突，以代码和本目录契约为准。
- 若代码与本目录契约冲突，需要修代码或修契约；不得默认“文档过期”。
- 新增 runtime 主线能力时，应优先在本目录新增/更新契约，再把根目录实施文档视为可丢弃草稿。

---

## 1. Runtime 主循环与会话

- [AGENT_RUNTIME_LOOP_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md)
  - 覆盖 `AgentRuntime` 的 run start、capability exposure、model/tool loop、pending state、plan workflow、MCP elicitation、mid-turn compact safe point、run finalization。
- [LLM_TRANSPORT_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/LLM_TRANSPORT_CONTRACT.zh.md)
  - 覆盖 LLM role/model selection、provider-neutral request、transport event translation、usage、retry、provider profiles。
- [EVENT_LOG_STORAGE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md)
  - 覆盖 typed event log、Postgres runtime truth schema、sequence、parent rows、run projection、artifact storage。
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
- [MCP_CAPABILITY_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MCP_CAPABILITY_CONTRACT.zh.md)
  - 覆盖 MCP manager、snapshot、binding bundle、tool name mangling、adapter、elicitation。
- [PERMISSION_POLICY_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/PERMISSION_POLICY_CONTRACT.zh.md)
  - 覆盖 permission presets、read-only host-local read、terminal hardline、action-level terminal_process observe、mode switching。
- [TERMINAL_ENV_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/TERMINAL_ENV_CONTRACT.zh.md)
  - 覆盖 terminal env builder、shell snapshot、PATH/env merge、diagnostics。
- [TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md)
  - 覆盖 terminal streaming preview、tool-result artifact、terminal completion event、adaptive preview/read-more。

---

## 3. Memory / compaction / continuity

- [MEMORY_SURFACES_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MEMORY_SURFACES_CONTRACT.zh.md)
  - 覆盖 governed canonical memory graph、runtime semantic graph、recall/reflection/run timeline、relatedness、Postgres substrate、outbox surface。
- [GOVERNANCE_WRITE_OUTBOX_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/GOVERNANCE_WRITE_OUTBOX_CONTRACT.zh.md)
  - 覆盖 memory governance executor、PostgreSQL UOW、relatedness destructive-action gate、mutation outbox、coordinator。
- [RETRIEVAL_RUNTIME_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/RETRIEVAL_RUNTIME_CONTRACT.zh.md)
  - 覆盖 embedding/rerank/tokenizer provider protocols、HostCore-owned retrieval resources、workers、bounded shutdown。
- [CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/CONTEXT_COMPACTION_CONTINUITY_CONTRACT.zh.md)
  - 覆盖 typed compaction events、summary artifact、rehydration、mid-turn inline compact、manual/preflight safe point。

---

## 4. 仍需继续补齐的契约主题

以下模块已有代码与测试，但尚未完全独立成契约；后续收口时应优先补：

- Runtime semantic graph / run timeline persistence hook 的更细粒度写入契约。
- LLM/Memory eval gate 与 real-LLM dogfood 的发布门槛契约。

这些不是“可以忽略”的模块，而是下一批契约化候选。
