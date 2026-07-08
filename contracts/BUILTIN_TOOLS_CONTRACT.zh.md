# Built-in Tools Contract

_Created: 2026-07-04_

本文档冻结 Pulsara 内置工具层的长期契约。它覆盖工具协议、执行注册表、core tool registry、基础 filesystem 工具、todo 工具和 plan workflow 工具的边界。

相关代码：

- [src/pulsara_agent/tools/base.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/base.py)
- [src/pulsara_agent/tools/executor.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/executor.py)
- [src/pulsara_agent/tools/registry.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/registry.py)
- [src/pulsara_agent/tools/builtins/registry.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/registry.py)
- [src/pulsara_agent/tools/builtins/workspace.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/workspace.py)
- [src/pulsara_agent/tools/builtins/filesystem.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/filesystem.py)
- [src/pulsara_agent/tools/builtins/todo.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/todo.py)
- [src/pulsara_agent/tools/builtins/plan.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/plan.py)
- [tests/test_tools.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_tools.py)

相关契约：

- [CAPABILITY_SURFACE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/CAPABILITY_SURFACE_CONTRACT.zh.md)
- [PERMISSION_POLICY_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/PERMISSION_POLICY_CONTRACT.zh.md)
- [ARTIFACT_STORE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/ARTIFACT_STORE_CONTRACT.zh.md)
- [TERMINAL_ENV_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/TERMINAL_ENV_CONTRACT.zh.md)
- [TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/TERMINAL_OUTPUT_THREE_LAYER_CONTRACT.zh.md)
- [MEMORY_SURFACES_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MEMORY_SURFACES_CONTRACT.zh.md)

---

## 1. 分层边界

Pulsara 工具系统分为三层：

```text
CapabilityDescriptor / CapabilityExposurePlan
  -> ToolRegistry execution binding
  -> ToolExecutor emits tool-result events and artifacts
```

硬规则：

- `CapabilityDescriptor` 是模型可见工具声明真值。
- `ToolRegistry` 只负责 execution binding。
- `ToolExecutor` 只负责调用工具、规范化异常、发 tool result events、归档 artifact candidates。
- Permission gate / capability gate 在 `AgentRuntime` 中运行；普通工具实现不得自建一套全局权限判定。
- 少量工具可以有本地 fail-closed 防线，例如 `terminal=off`、hardline terminal command、workspace path escape；这些防线必须优先读取 `ToolRuntimeContext` 中的 run permission snapshot，不得读取 HostSession stored default 来改变已启动 run 的行为，也不能替代 runtime gate。

---

## 2. Tool / AsyncTool 协议

同步工具必须实现：

- `name: str`
- `description: str`
- `parameters: dict`
- `is_read_only: bool`
- `is_concurrency_safe: bool`
- `execute(call: ToolCall) -> ToolExecutionResult`

异步工具必须实现：

- 同样的声明字段；
- `execute_async(call, *, runtime_context: ToolRuntimeContext) -> ToolExecutionResult | ToolExecutionSuspended`

`ToolRuntimeContext` 至少包含：

- `runtime_session_id`
- typed `EventContext`

异步工具不得用 `asyncio.run()` 自建事件循环。它必须在 runtime 主 loop 中执行，由 `ToolExecutor.execute_async()` 注入 `ToolRuntimeContext`。

`is_read_only` 的语义是 permission-contract 语义：

- 对用户 workspace、外部系统、terminal process、durable memory 没有副作用，才可为 `True`。
- 只修改 agent-local ephemeral state 的工具可为 `True`，例如 `todo`。

`is_concurrency_safe` 只描述同批并发执行安全性，不等于 read-only。

---

## 3. ToolRegistry

`ToolRegistry` 契约：

- `register(tool)`：工具名唯一；重复注册必须 fail-closed。
- `get(name)`：未知工具抛 `KeyError("Unknown tool: ...")`。
- `names()`：返回稳定排序名称。
- `all()`：按 `names()` 排序返回工具实例。
- `tool_specs()`：从已注册工具生成 legacy `ToolSpec` 视图，仅用于兼容/测试；生产 descriptor 真值不得由这里反推。

`ToolRegistry` 不拥有：

- permission policy；
- capability exposure；
- skill catalog；
- model tool visibility；
- artifact archive ownership。

---

## 4. ToolExecutor

`ToolExecutor` 是工具执行边界。

执行规则：

- 每次工具调用先发 `ToolResultStartEvent`。
- 同步工具异常必须被捕获并转换为 `ToolExecutionResult(status=ERROR, output="[TOOL_ERROR] ...")`。
- 异步工具异常同样转换为 ERROR result。
- 若异步工具返回 `ToolExecutionSuspended`，该 suspended result 交给 runtime/host pending interaction 机制，不得被伪装成普通成功输出。
- 若工具实现 streaming 输出，streaming delta 由工具通过 emitter 写 `ToolResultTextDeltaEvent`。
- 非 streaming complete 的普通 result output 会写为 `ToolResultTextDeltaEvent`。
- 最后必须写 `ToolResultEndEvent`。

Artifact 规则：

- `ToolExecutionResult.artifact_candidates` 由 `ToolResultArtifactService` 处理。
- `ToolExecutor` 不自行决定 preview / archive / read-more 语义。
- artifact 详细契约见 `ARTIFACT_STORE_CONTRACT` 和 `TERMINAL_OUTPUT_THREE_LAYER_CONTRACT`。

---

## 5. Core tool registry

`build_core_tool_registry(runtime_session, ...)` 必须显式接收 `RuntimeSession`。传入非 `RuntimeSession` 必须报错。

基础 registry 在没有 memory dependencies 时必须包含：

- `artifact_read`
- `ask_plan_question`
- `edit_file`
- `enter_plan`
- `exit_plan`
- `read_file`
- `search_files`
- `terminal`
- `terminal_process`
- `todo`
- `write_file`

可选注册：

- `memory_search`：需要 `MemoryRecallService`。
- `memory_get` / `memory_explain`：需要 `MemoryQuery`。
- `remember_claim` / `remember_preference` / `remember_observation` / `remember_action_boundary` / `remember_decision`：需要 `MemoryProposalSink`。
- `extra_tools`：只作为 explicit execution binding extension；必须仍有 descriptor provider output 才能进入 model tools。

权限模式不得改变 core registry 的工具集合。即使 read-only 或 terminal off，mutating tools 和 terminal tools 仍注册；是否允许调用由 capability/permission gate 决定。

这条规则保持模型 tool array 稳定，避免 mode switch 导致 prompt prefix cache 大幅失效。

---

## 6. WorkspaceTool path resolver

`WorkspaceTool` 提供两个解析器：

### 6.1 `_resolve_workspace_path`

用于写入/修改类工具。

规则：

- 空 path 拒绝。
- 相对路径从 `workspace_root` 解析。
- 绝对路径也必须 resolve 后位于 `workspace_root` 内。
- `..` escape 必须拒绝。

### 6.2 `_resolve_read_path`

用于 read-only filesystem 工具。

规则：

- 空 path 拒绝。
- 原始输入以 `~` 开头：展开为 host-local path。
- 原始输入是绝对路径：作为 host-local path。
- 原始输入是相对路径：必须走 `_resolve_workspace_path`，因此不能通过 `..` 逃出 workspace。

这意味着“读 workspace 外文件”必须是显式 absolute 或 `~` 路径；相对路径始终保持 workspace-bound。

---

## 7. `read_file`

`read_file` 是 read-only 工具。

模型参数：

- `path`：必填；
- `offset`：1-indexed 起始行，默认 1；
- `limit`：读取行数，默认 500，最大 2000。

访问边界：

- 相对路径从 workspace root 解析，并禁止 `..` escape。
- 绝对路径和 `~` 可以读取 host-local ordinary UTF-8 text 文件。
- 设备路径、明显二进制扩展文件、目录、不存在路径必须拒绝。
- plain-text sensitive file（例如 `.env`、token 文本）若以显式 host-local path 请求，V1 允许读取；这不是 bug，而是 permission contract 中“read-only filesystem tools 可读本机普通文本”的明确能力。

输出契约：

- 成功输出 JSON，包含：
  - `status`
  - `path`
  - `access_scope`
  - `workspace_relative`
  - `offset`
  - `limit`
  - `total_lines`
  - `file_size`
  - `truncated`
  - line-numbered `content`
- `metadata` 必须包含：
  - absolute `path`
  - `access_scope`
  - `workspace_relative`
  - truncation / line count 等诊断字段。

`access_scope` 是互斥分类：

- `workspace`
- `home`
- `temp`
- `external_absolute`

重复读取防线：

- 同一 path / offset / limit 在文件未变时，第二次返回 `unchanged`，不重复正文。
- 第三次及之后返回 ERROR，阻止模型陷入重复读取循环。

---

## 8. `search_files`

`search_files` 是 read-only 工具。

模式：

- `target="content"`：搜索文本内容；
- `target="files"`：按名称/glob 查找文件。

参数：

- `pattern`
- `path`，默认 `.`
- `file_glob`
- `limit`，默认 50，最大 1000；
- `offset`
- `output_mode`: `content` / `files_only` / `count`
- `context`

访问边界：

- 相对 `path` 必须 workspace-bound，禁止 `..` escape。
- 显式 absolute / `~` 可以搜索 host-local path。
- workspace 外搜索必须是具体文件或具体子目录。
- workspace 外 broad root 必须拒绝，例如 `/`、用户 home 根、`/Users`、系统根、temp 根、部分 workspace parent。

实现边界：

- 优先使用 `rg`。
- 没有 `rg` 时使用 Python fallback。
- `limit` / `offset` 是结果分页，不是“允许扫描整个主机”的授权；因此 broad-root guard 是必须的。

输出契约：

- 成功输出 JSON，包含 `status`、`target`、`output_mode`、`total_count`、`truncated` 以及 `matches` / `files` / `counts`。
- payload 和 metadata 必须包含 `access_scope` 与 `workspace_relative`。
- 截断时输出 `_hint`，提示继续使用新的 offset。

重复搜索防线：

- 连续第三次同样 search 增加 `_warning`。
- 连续第四次同样 search 返回 ERROR。

---

## 9. `write_file`

`write_file` 是 mutating workspace 工具。

访问边界：

- 只能写 workspace 内路径。
- 绝对路径若不在 workspace 内，必须拒绝。
- 相对 `..` escape 必须拒绝。

行为：

- 写入完整 UTF-8 内容。
- 默认 `create_dirs=True`。
- 使用 atomic write。
- 若覆盖已有文件，应保持已有主要 line ending。
- 若已有文件含 UTF-8 BOM，新内容不带 BOM 时应保留 BOM。

并发/新鲜度：

- 同一 workspace 内按 path 加锁。
- 若文件在最近一次 `read_file` 后被外部修改，仍允许写入，但输出 `_warning`。

输出：

- 成功输出 JSON，至少包含 `status="ok"`、`path`、`bytes_written`、`files_modified`。

---

## 10. `edit_file`

`edit_file` 是 mutating workspace 工具。

访问边界与 `write_file` 相同：只能修改 workspace 内文件。

行为：

- 目标文件必须存在且是 file。
- `old_text` 必须必填。
- `new_text` 必须是 string，允许空字符串表示删除。
- 默认只替换唯一匹配。
- `replace_all=True` 时可替换所有 exact match。
- 支持 whitespace-normalized fuzzy matching。
- 多个匹配且未 `replace_all` 时必须拒绝，提示重新读取或搜索定位。
- 写入后必须做 post-write verification。

输出：

- 成功输出 JSON，至少包含 `status="ok"`、`path`、`replacements`、`strategy`、`diff`、`files_modified`。
- 失败输出必须提供可行动 hint，例如重新 `read_file` 或 `search_files`。

---

## 11. `todo`

`todo` 是 agent-local ephemeral plan tracker。

它在 permission 语义上是 read-only：

- 它只修改当前工具实例的 `_items`；
- 不写 workspace；
- 不写 durable memory；
- 不启动 terminal process；
- 不触达外部系统。

它不是 concurrency-safe，因为会修改共享 `_items`。

支持 action：

- `add`
- `update`
- `list`
- `clear`

合法状态：

- `pending`
- `in_progress`
- `completed`

`todo` 不参与 durable resume，不是 memory，不是 artifact，不是 event-log projection 真源。

---

## 12. Plan workflow tools

Plan workflow 工具：

- `enter_plan`
- `ask_plan_question`
- `exit_plan`

它们必须注册到 core registry，并必须有 capability descriptor，用于模型工具声明、exposure、inspector。

执行边界：

- 正常路径由 `AgentRuntime` workflow control plane 在 permission gate 前截获。
- 截获前仍必须通过 capability exposure access。
- 它们不应进入普通 `ToolExecutor` 执行路径。
- 若错误进入普通工具执行，fallback 必须返回 ERROR，并说明 “Plan workflow tools must be handled by the runtime control plane”。

具体 plan mode / structured question / approve / revise / cancel 语义见 `AGENT_RUNTIME_LOOP_CONTRACT` 与 `APP_SETTINGS_CLI_ENTRY_CONTRACT`。

---

## 13. Terminal / artifact / memory 工具

本文件不重复冻结以下工具的完整细节：

- `terminal`
- `terminal_process`
- `artifact_read`
- `memory_search`
- `memory_get`
- `memory_explain`
- `remember_*`

它们仍属于 built-in/core registry，但详细契约分别位于：

- terminal env / terminal process / terminal output：
  - `TERMINAL_ENV_CONTRACT`
  - `WORKSPACE_TERMINAL_LIFECYCLE_CONTRACT`
  - `TERMINAL_OUTPUT_THREE_LAYER_CONTRACT`
- artifact:
  - `ARTIFACT_STORE_CONTRACT`
- memory:
  - `MEMORY_SURFACES_CONTRACT`
  - `GOVERNANCE_WRITE_OUTBOX_CONTRACT`
  - `RETRIEVAL_RUNTIME_CONTRACT`

跨文档共同硬规则：

- 这些工具仍必须由 capability descriptor 声明。
- 这些工具仍必须由 `ToolRegistry` 提供 execution binding。
- 这些工具的可见性与调用许可仍由 capability exposure + permission gate 共同决定。

---

## 14. 测试守卫

以下测试类别是契约守卫：

- core registry 基础工具集合稳定；
- permission mode 不改变 registry 工具集合；
- read-only 模式下 mutating tools 仍可见但由 gate 拦截；
- `read_file` 可读 workspace 与显式 host-local text；
- `read_file("../...")` 必须被 workspace escape guard 拒绝；
- 显式 host-local sensitive text path 可读；
- `search_files` 可搜索具体 host-local 目录；
- `search_files` 必须拒绝 workspace 外 broad root；
- `search_files(path="..")` 必须拒绝；
- `write_file` / `edit_file` workspace escape 必须拒绝；
- repeated read/search guard；
- write stale warning；
- edit exact/ambiguous/fuzzy replacement；
- todo add/update/list/clear/status validation；
- ToolExecutor event sequence 与 replay；
- async tool runtime context 注入；
- workflow tool fallback fail-closed。

任何新增 built-in tool 必须同时更新：

- explicit capability descriptor；
- core registry 或 provider/binding installer；
- permission category；
- model-facing schema；
- 本文件或对应专门契约；
- 相关 tests。
