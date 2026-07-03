# Pulsara CLI / MCP Capability 下一步实施设计

本文是 `PULSARA_UNIFIED_CAPABILITY_SURFACE_RESEARCH.zh.md` 与 `PULSARA_UNIFIED_CAPABILITY_SURFACE_IMPLEMENTATION.zh.md` 的下钻文档，只覆盖接下来的 CLI / MCP 两条外部能力入口。

当前统一 capability surface 已经 hard cut 到：

- `CapabilityRuntime` 是唯一 capability 入口。
- `CapabilityExposurePlan` 是每个 turn 唯一 capability fact。
- `CapabilityDescriptor` 是 callable tool / skill / future MCP 的声明真值。
- `ToolRegistry` 只负责 execution binding，不再反推 descriptor。

因此 CLI / MCP 的下一步不能再绕回旧的 name allowlist 或 ToolRegistry-derived metadata。

## 0. 结论先行

CLI 和 MCP 必须分开设计：

- CLI v1：`skill/docs/prompt guidance + terminal/exec/shell tool`。不新增 `CapabilityProviderKind.CLI`，不把每个 CLI subcommand 封装成 typed tool。
- MCP v1：`server config + client lifecycle + tool discovery + descriptor snapshot + execution adapter`。MCP server 暴露的工具是 typed tools，但必须由 MCP provider 统一接入 capability runtime。

Firecrawl 是最好的对照：

- Firecrawl skill：`Bash(firecrawl *)` / `Bash(npx firecrawl *)`，属于 CLI skill-guided terminal usage。
- Firecrawl MCP server：如果用户配置了 MCP server，则属于 MCP provider route。
- 两者可以共存，但 provenance 必须区分清楚，不能把 skill-guided terminal 调用伪装成 MCP typed tool，也不能让 MCP tool 退化成模型手写 terminal JSON-RPC。

## 1. 当前代码落脚点

### 1.1 Capability runtime

主要文件：

- `src/pulsara_agent/capability/runtime.py`
- `src/pulsara_agent/capability/provider.py`
- `src/pulsara_agent/capability/exposure.py`
- `src/pulsara_agent/capability/descriptor.py`

当前结构：

```text
CapabilityRuntime.resolve_for_turn(...)
        ↓
CapabilityProvider.resolve(...)
        ↓
CapabilityProviderOutput
        ↓
CapabilityRegistry.snapshot()
        ↓
build_exposure_plan(...)
        ↓
CapabilityExposurePlan.direct_tool_specs / diagnostics / prompts
```

CLI / MCP 都必须进入这条链路：

- CLI route 通过 `LocalSkillCapabilityProvider` 输出 catalog / active skill injection / diagnostics，不输出 CLI callable descriptor。
- MCP route 通过未来 `McpCapabilityProvider` 输出 MCP tool descriptors，并确保每个 model-callable descriptor 都有 execution binding。

### 1.2 Skill / CLI 当前能力

主要文件：

- `src/pulsara_agent/capability/local_skills.py`
- `src/pulsara_agent/capability/local_skill_provider.py`
- `src/pulsara_agent/capability/types.py`

当前 `LocalSkillManifest` 已有：

- `name`
- `description`
- `when_to_use`
- `provides_tools`
- `disable_model_invocation`
- `user_invocable`
- `body_too_large`

当前 frontmatter parser 只认识：

```text
name
description
when_to_use
provides_tools
disable_model_invocation
user_invocable
```

CLI 下一步应该扩展这里，而不是新增 CLI provider。

### 1.3 Terminal execution 边界

主要文件：

- `src/pulsara_agent/tools/terminal.py`
- `src/pulsara_agent/runtime/permission.py`
- `src/pulsara_agent/runtime/tool_artifacts.py`
- `src/pulsara_agent/tools/executor.py`
- `src/pulsara_agent/event.py`
- `src/pulsara_agent/inspector/service.py`

CLI skill-guided terminal usage 的真实执行仍是 terminal tool：

```text
active skill / CLI recipe hints
        ↓
model writes shell command
        ↓
terminal / terminal_process tool
        ↓
permission gate + hardline + artifact + retained process
```

因此 CLI 的 capability 化目标不是创造新工具，而是增强：

- active skill 与 terminal call 的关联。
- CLI binary / auth / network / risk hints 的 diagnostics。
- inspector 对 terminal call 的解释能力。

### 1.4 MCP 未来执行边界

当前代码没有真实 MCP provider / adapter。未来落点建议：

- `src/pulsara_agent/capability/providers/mcp.py`
- `src/pulsara_agent/tools/adapters/mcp.py`
- `src/pulsara_agent/runtime/mcp/config.py`
- `src/pulsara_agent/runtime/mcp/manager.py`
- `src/pulsara_agent/runtime/mcp/client.py`
- `src/pulsara_agent/runtime/mcp/types.py`

MCP adapter 必须同时满足：

- capability provider 产出 descriptor。
- ToolRegistry 注册 execution binding。
- provider / manager 拥有 MCP client lifecycle。
- adapter 只借用 provider-owned client，不私自创建长期 async resource。

## 2. CLI 路线：skill-guided terminal capability

### 2.1 非目标

明确不做：

- 不新增 `CapabilityProviderKind.CLI`。
- 不新增 `CliCapabilityTool`。
- 不把 `hf upload`、`firecrawl search`、`gh pr create` 这类 subcommand 逐个变成 typed tool。
- 不让 skill frontmatter 放宽 terminal permission。
- 不基于 CLI 名称推断 read-only / destructive。

理由：

- CLI 是开放世界 shell。把 subcommand typed 化会复制 terminal 的 cwd/env/stdout/timeout/approval/hardline 风险面。
- 大多数成熟 agent 产品对 CLI 的主路径都是 skill/docs + shell/exec。
- terminal 已经是 Pulsara 的硬边界，绕开它会削弱现有安全模型。

### 2.2 新增 skill metadata

扩展 `LocalSkillManifest`，建议新增字段：

```python
suggested_tools: tuple[str, ...] = ()
required_binaries: tuple[str, ...] = ()
optional_binaries: tuple[str, ...] = ()
external_services: tuple[str, ...] = ()
network_required: bool = False
auth_required: Literal["none", "optional", "required"] = "none"
cli_usage_kind: Literal["none", "read", "write", "mixed"] = "none"
```

对应 frontmatter：

```yaml
---
name: firecrawl-search
description: Search the web through Firecrawl CLI.
when_to_use: User asks to search the web or retrieve recent web content.
provides_tools:
  - terminal
suggested_tools:
  - terminal
required_binaries:
  - firecrawl
optional_binaries:
  - npx
external_services:
  - firecrawl
network_required: true
auth_required: required
cli_usage_kind: read
---
```

字段语义：

- `suggested_tools`：prompt/inspector hint，不是 permission allowlist。
- `required_binaries`：可用于 cheap health diagnostics，例如 `command -v firecrawl`。
- `optional_binaries`：备用路径，例如 `npx firecrawl`。
- `external_services`：用于 provenance / inspector / future auth diagnostics。
- `network_required`：用于提示和 inspector，不直接改 permission。
- `auth_required`：用于提示用户可能需要登录/配置 token。
- `cli_usage_kind`：辅助 prompt 告知风险倾向，但 terminal permission 仍是最终 gate。

### 2.3 Parser 和 diagnostics

修改落点：

- `src/pulsara_agent/capability/types.py`
- `src/pulsara_agent/capability/local_skills.py`
- `tests/test_capability_skills.py`

实现要求：

- 新字段类型严格校验。
- unknown frontmatter 仍产生 warning。
- `suggested_tools/provides_tools` 只能引用已存在 tool names；未知 tool 产生 diagnostic，不进入 manifest。
- `required_binaries/optional_binaries/external_services` 去重并排序保持确定性。
- `auth_required` 只允许 `none/optional/required`。
- `cli_usage_kind` 只允许 `none/read/write/mixed`。

不要在 parser 中执行 `command -v`。解析 skill 必须是 cheap / deterministic / no side-effect。

### 2.4 CLI binary health

可选新增一个 cheap health resolver：

- `src/pulsara_agent/capability/skill_health.py`

输入：

- active skill manifests
- workspace root
- TTL cache

输出：

- diagnostics，例如：
  - `skill_required_binary_missing`
  - `skill_optional_binary_missing`
  - `skill_auth_required`
  - `skill_network_required`

边界：

- 默认只对 active skill 做 binary check，不对所有 catalog skill 做全量探测。
- `command -v` 应通过 local safe subprocess，设置短 timeout。
- health check 不执行 CLI 本身，不做登录，不访问网络。
- health diagnostics 不改变 terminal permission，只帮助 prompt/inspector 解释。

### 2.5 Terminal call 与 active skill 关联

目标：inspector 能回答「这次 terminal call 是否是在某个 active skill / CLI recipe 指导下发生的」。

实现方式建议：

1. `CapabilityExposurePlan.active_injections` 已有 active skill 列表。
2. 在执行 tool call 前，runtime 能看到当前 exposure。
3. 对 terminal / terminal_process call，写入一个 lightweight event metadata 或 tool call metadata：

```json
{
  "capability_context": {
    "active_skill_names": ["firecrawl-search"],
    "skill_suggested_tools": ["terminal"],
    "cli_required_binaries": ["firecrawl"],
    "cli_external_services": ["firecrawl"]
  }
}
```

落点候选：

- `CapabilityGateDecisionEvent` 中增加 per-call `active_skill_names`。
- 或新增 `ToolCallCapabilityContextEvent`。
- 或在既有 tool result / artifact metadata 中附带。

推荐：优先扩展 capability gate/exposure event，不要污染 terminal tool 参数。原因是这个关联是 runtime 解释事实，不是模型请求参数。

### 2.6 CLI route 验收标准

- Firecrawl-style skill 能声明 `required_binaries: [firecrawl]`、`external_services: [firecrawl]`。
- active skill prompt 能继续指导模型使用 terminal，而不是出现新的 typed CLI tool。
- 缺 binary 时，模型仍可选择 `npx` fallback，或向用户说明缺少依赖；runtime 不自动安装。
- terminal permission / hardline / artifact 行为完全复用现有 terminal tool。
- inspector 能显示：
  - active skill 名称
  - terminal call
  - skill CLI hints
  - permission decision
  - artifact refs

## 3. MCP 路线：server-configured typed tool surface

### 3.1 MCP server config shape

先定义 Pulsara 自己的 MCP config DTO，不急着支持所有 Codex / Claude Code 字段。

建议 v1 shape：

```python
class McpServerTransportKind(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"

@dataclass(frozen=True)
class McpStdioConfig:
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = MappingProxyType({})
    cwd: Path | None = None

@dataclass(frozen=True)
class McpStreamableHttpConfig:
    url: str
    bearer_token_env_var: str | None = None
    headers: Mapping[str, str] = MappingProxyType({})
    env_headers: Mapping[str, str] = MappingProxyType({})

@dataclass(frozen=True)
class McpServerConfig:
    server_id: str
    transport: McpStdioConfig | McpStreamableHttpConfig
    enabled: bool = True
    required: bool = False
    startup_timeout_ms: int = 10_000
    tool_timeout_ms: int = 30_000
    supports_parallel_tool_calls: bool = False
    enabled_tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    default_approval_mode: str | None = None
```

暂缓：

- OAuth browser login。
- SSE / websocket / IDE-specific transport。
- remote stdio placement。
- plugin-provided MCP config。

这些后续可以扩展，但 v1 不应一口吃下。

### 3.2 MCP provider output

未来 `McpCapabilityProvider.resolve(...)` 应输出：

- `CapabilityDescriptor` for each discovered tool。
- diagnostics for server state。
- optional catalog prompt? v1 不建议给 MCP 额外 prompt，避免和 tool schema 重复。

descriptor 建议：

```python
CapabilityDescriptor(
    id=f"mcp:{server_id}:{tool_name}",
    name=model_tool_name,
    namespace=f"mcp:{server_id}",
    provider_kind=CapabilityProviderKind.MCP,
    provider_id=server_id,
    is_model_callable=True,
    input_schema=mcp_tool.input_schema,
    is_read_only=False,  # unless annotation proves otherwise
    is_concurrency_safe=server_config.supports_parallel_tool_calls,
    is_destructive=annotation.destructive_hint or True if unknown,
    is_open_world=annotation.open_world_hint or True if unknown,
    permission_category="mcp",
    timeout_ms=server_config.tool_timeout_ms,
    availability=AVAILABLE / DEGRADED / UNAVAILABLE,
    metadata={
        "server_id": server_id,
        "original_tool_name": tool_name,
        "transport": "stdio" | "streamable_http",
        "annotations": ...,
    },
)
```

默认 fail-closed：

- MCP annotation 缺失时不要假定 read-only。
- unknown destructive/open-world 时按 risky 处理。
- unavailable / needs-auth / no binding 不 direct advertise。

### 3.3 MCP model tool name

MCP 原始 tool name 可能包含 server-private 信息，也可能与 builtin tool 重名。v1 必须 deterministic mangle：

```text
mcp__{safe_server_id}__{safe_tool_name}
```

要求：

- 只允许 `[a-zA-Z0-9_-]`，其他字符转 `_`。
- 冲突 fail-closed 或加 stable hash suffix；推荐 fail-closed，等 namespace/deferred 更成熟后再支持 hash。
- descriptor metadata 保留 `original_tool_name`。
- event/telemetry 默认可脱敏；inspector 本地可显示 original provenance。

### 3.4 MCP execution adapter

落点：

- `src/pulsara_agent/tools/adapters/mcp.py`

建议形状：

```python
@dataclass(slots=True)
class McpCapabilityTool:
    name: str
    description: str
    parameters: dict[str, object]
    server_id: str
    original_tool_name: str
    client_manager: McpClientManager

    async def execute_async(
        self,
        call: ToolCall,
        *,
        runtime_context: ToolRuntimeContext,
    ) -> ToolExecutionResult:
        ...
```

要求：

- adapter 不创建 client。
- adapter 不拥有 server process。
- adapter 通过 `McpClientManager.call_tool(server_id, original_tool_name, args, timeout)` 调用。
- result 统一转成 `ToolExecutionResult`。
- 大结果交给 `ToolResultArtifactService`，不要在 adapter 里私自截断。
- MCP elicitation 不得阻塞 stdin；必须转成 Pulsara pending interaction。

### 3.5 MCP lifecycle owner

落点：

- `HostSession` 或 `HostCore` 拥有 MCP manager。
- 如果 MCP server 与 workspace/session 强绑定，优先由 `HostSession` owner。
- 如果 server 是 workspace-scoped shared resource，需复用已有 workspace ownership / lease 经验，避免重复 terminal supervisor 的债。

v1 推荐：

- 先做 session-owned `McpClientManager`。
- mock provider 不启动真实进程。
- real stdio/HTTP 在后续 PR 接入，close 链必须幂等、有界、按顺序。

shutdown 顺序建议：

```text
HostSession.close
  → gate new runs
  → drain/cancel active runs
  → close MCP manager
  → close terminal/resources
  → finish session close
```

如果 MCP tool 正在执行：

- close 应发 cancel。
- 等待 bounded drain。
- 超时后 force close connection/process。

### 3.6 MCP elicitation

MCP server 可能请求：

- login / auth
- form input
- approval-like confirmation
- URL visit

Pulsara 已有 pending interaction / plan question 经验。MCP elicitation 应复用同类机制：

```text
MCP client receives elicitation
        ↓
McpClientManager creates PendingInteraction(kind="mcp_elicitation")
        ↓
CLI / future UI renders structured prompt
        ↓
user resolves
        ↓
McpClientManager sends response to server
```

不要：

- 在 MCP adapter 内 `input()`。
- 在 provider thread 私自阻塞。
- 把 elicitation 当普通 tool error。

### 3.7 MCP health / startup

server state：

- `disabled`
- `starting`
- `ready`
- `failed`
- `needs_auth`
- `degraded`
- `closed`

diagnostic examples：

- `mcp_server_disabled`
- `mcp_server_startup_failed`
- `mcp_server_needs_auth`
- `mcp_tool_schema_invalid`
- `mcp_tool_name_collision`
- `mcp_missing_execution_binding`

v1 mock provider 可先构造这些状态，不连真实 server。

## 4. 推荐 PR 顺序

### PR C0：CLI/MCP 边界守护

目标：

- 在代码层确认没有 CLI provider kind / adapter。
- 加测试防止 CLI-as-tool 回潮。
- 文档与 enum hard cut 保持一致。

落点：

- `src/pulsara_agent/capability/descriptor.py`
- `tests/test_capability_surface.py`
- docs

测试：

- `CapabilityProviderKind` 不包含 `CLI`。
- runtime provider stack 不包含 CLI provider。
- `ToolRegistry` 中不存在 `CliCapabilityTool`。
- skill with CLI hints 不生成 callable CLI descriptor。

### PR C1：扩展 skill manifest CLI hints

目标：

- 支持 `suggested_tools / required_binaries / optional_binaries / external_services / network_required / auth_required / cli_usage_kind`。
- 这些字段只作为 hints / diagnostics / inspector input，不影响 permission。

落点：

- `src/pulsara_agent/capability/types.py`
- `src/pulsara_agent/capability/local_skills.py`
- `src/pulsara_agent/capability/local_skill_provider.py`
- `tests/test_capability_skills.py`

测试：

- frontmatter 正常解析。
- unknown / invalid 类型产生 diagnostic。
- suggested tool 引用 unknown tool 被过滤并 warning。
- `auth_required` / `cli_usage_kind` enum 越界被拒。
- catalog prompt 不把 hints 夸大为 permission。

### PR C2：active skill ↔ terminal call observability

目标：

- terminal call 的 event/inspector 能显示 active skill / CLI hints。
- 不改 terminal permission。

落点：

- `src/pulsara_agent/runtime/agent.py`
- `src/pulsara_agent/runtime/permission.py`
- `src/pulsara_agent/event.py`
- `src/pulsara_agent/inspector/service.py`
- `tests/test_capability_surface.py`
- `tests/test_inspector.py`

测试：

- active Firecrawl-like skill 下调用 terminal，gate event 带 active skill context。
- 无 active skill 时不产生误关联。
- terminal denied / ask / allow 都能解释 active skill context。
- artifact 大输出仍按 terminal descriptor 归档。
- read-only / plan mode 不因 skill hints 放宽 terminal。

### PR C3：CLI binary health diagnostics（可选但建议）

目标：

- 对 active skill 的 required binaries 做 cheap health check。
- diagnostics 进入 exposure plan / inspector。

落点：

- `src/pulsara_agent/capability/skill_health.py`
- `src/pulsara_agent/capability/local_skill_provider.py`
- tests

测试：

- `command -v` 成功 → no missing diagnostic。
- binary missing → `skill_required_binary_missing`。
- 只检查 active skill，不扫全 catalog。
- TTL 生效，避免每轮重复探测。
- check 超时不会阻塞 run，产生 degraded diagnostic。

### PR M0：MCP config DTO + mock snapshot

目标：

- 定义 MCP server config / status / discovered tool DTO。
- 不连接真实 MCP server。
- 用 mock snapshot 验证 capability provider 输出。

落点：

- `src/pulsara_agent/runtime/mcp/types.py`
- `src/pulsara_agent/runtime/mcp/config.py`
- `src/pulsara_agent/capability/providers/mcp.py`
- tests

测试：

- stdio config shape。
- streamable HTTP config shape。
- enabled/disabled server。
- required server failed 的 diagnostic。
- enabled_tools / disabled_tools 过滤。
- unavailable server 不 direct advertise。

### PR M1：MCP provider + descriptor/exposure integration

目标：

- `McpCapabilityProvider` 从 mock server snapshot 产出 descriptors。
- model tool names deterministic mangle。
- direct/deferred/hidden 行为接入 `CapabilityExposurePlan`。

落点：

- `src/pulsara_agent/capability/providers/mcp.py`
- `src/pulsara_agent/capability/exposure.py` 如需 deferred 策略增强
- `tests/test_capability_mcp.py`

测试：

- MCP descriptor `provider_kind=MCP`。
- duplicate model name fail-closed。
- missing execution binding hidden + diagnostic。
- unavailable / needs-auth hidden + diagnostic。
- many tools 可 deferred。
- unknown annotations fail-closed：not read-only, open-world/destructive conservative。

### PR M2：MCP mock execution adapter

目标：

- 注册 mock `McpCapabilityTool` 到 ToolRegistry。
- 验证 descriptor + execution binding + ToolExecutor 调用链。

落点：

- `src/pulsara_agent/tools/adapters/mcp.py`
- `src/pulsara_agent/runtime/wiring.py`
- tests

测试：

- mock MCP tool 可执行。
- adapter 不拥有 client，只调用 manager。
- large result 进入 artifact service。
- execution error 规范化为 tool error。
- timeout metadata 传递。
- approved pending MCP tool resume 仍重新过 capability gate。

### PR M3：MCP manager lifecycle seam

目标：

- 引入 session-owned `McpClientManager` seam。
- 不一定连接真实 MCP，但 close/drain/cancel 语义先定。

落点：

- `src/pulsara_agent/runtime/mcp/manager.py`
- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/runtime/wiring.py`
- tests

测试：

- HostSession close 调用 manager close exactly once。
- close 有 bounded timeout。
- active MCP call close 时被 cancel。
- close 后新 MCP call 被拒。
- manager startup failure 对 required/non-required server 分别处理。

### PR M4：真实 streamable HTTP MCP spike

目标：

- 先接远端 HTTP MCP，因为它不涉及本地进程组。
- 验证 initialize / tools/list / tools/call / auth failure / timeout。

落点：

- `src/pulsara_agent/runtime/mcp/client.py`
- `src/pulsara_agent/runtime/mcp/manager.py`
- integration tests with fake HTTP MCP server

测试：

- tools/list 生成 descriptor snapshot。
- tools/call 返回 result。
- 429/500/timeout 结构化 failure。
- bearer token env var missing → needs-auth/degraded。
- tool timeout 生效。
- startup timeout 生效。

### PR M5：真实 stdio MCP spike

目标：

- 接本地 stdio MCP server。
- 明确 process owner、cwd/env、shutdown。

落点：

- `src/pulsara_agent/runtime/mcp/stdio.py`
- `src/pulsara_agent/runtime/mcp/manager.py`
- tests with tiny local MCP stdio fixture

测试：

- command/args/env/cwd 正确传递。
- process close 正常。
- server crash → failed diagnostic。
- startup timeout kill process。
- HostSession close kill process group / child process。

## 5. 测试矩阵

### 5.1 CLI

| 场景 | 期望 |
| --- | --- |
| Firecrawl-like skill 声明 CLI hints | catalog/manifest 保留 hints |
| skill 提供 unknown suggested tool | warning + filter |
| active skill 下 terminal call | event/inspector 显示 active skill context |
| read-only plan mode + CLI skill | terminal write 仍被拒或 ask，不因 skill 放宽 |
| required binary missing | diagnostic，不自动安装 |
| optional binary missing | warning/info，不阻断 |
| terminal large output | artifact policy 不变 |
| no active skill | terminal call 不被错误归因 |

### 5.2 MCP descriptor / exposure

| 场景 | 期望 |
| --- | --- |
| ready server + bound tool | direct or deferred exposure |
| needs-auth server | hidden + diagnostic |
| disabled server | hidden + diagnostic |
| descriptor no binding | hidden + `capability_missing_execution_binding` |
| binding no descriptor | `capability_missing_descriptor` |
| duplicate mangled name | fail-closed diagnostic |
| unknown annotations | fail-closed permission metadata |
| many tools | deferred exposure |

### 5.3 MCP execution

| 场景 | 期望 |
| --- | --- |
| mock tool success | ToolResult success |
| mock tool error | ToolResult error |
| large MCP result | artifact archived |
| tool timeout | structured timeout error |
| server unavailable at call time | error + health diagnostic |
| approval resume crafted stale MCP call | re-run capability gate, fail-closed if descriptor missing |

### 5.4 MCP lifecycle

| 场景 | 期望 |
| --- | --- |
| HostSession close | manager close exactly once |
| active MCP call during close | cancel + bounded drain |
| stdio startup timeout | process killed |
| stdio server crash | failed state, hidden tools |
| HTTP transient initialize error | retry within startup timeout |
| required server failed | configured fail-fast or explicit blocking diagnostic |
| non-required server failed | session continues with hidden/degraded MCP capability |

## 6. 编码坑

### 6.1 不要让 CLI hints 变成 permission

`suggested_tools: [terminal]` 只表示 skill 建议 terminal，不表示 terminal 在 read-only/plan mode 下可用。

### 6.2 不要让 MCP adapter 拥有 client

adapter 是 execution binding，不是 lifecycle owner。否则会重演 async provider 跨 event loop / shutdown 不清晰的问题。

### 6.3 不要把 unavailable MCP 只从广告中移除

如果模型幻觉调用 hidden/unavailable MCP tool，execution gate 必须 fail-closed。只是不 direct advertise 不够。

### 6.4 不要每轮启动或探测所有外部能力

CLI binary health 和 MCP server health 都需要 cache / TTL / generation。否则每个 turn 都会多出不可控 I/O。

### 6.5 MCP tool name 是隐私面

`mcp__company-jira__delete_issue` 可能泄露用户组织和服务配置。event log 本地可保留，遥测/外发摘要要脱敏。

### 6.6 MCP elicitation 不是普通 approval

MCP elicitation 可能来自 server 中途请求表单/URL/auth，不一定是 Pulsara 自己的 permission ask。它应复用 pending interaction 管线，但需要保留 server/request id 以便响应原 server。

## 7. 验收定义

CLI 侧完成时：

- 没有 CLI provider kind / adapter。
- Firecrawl/HF/GitHub-style skills 能声明 CLI hints。
- terminal 调用能被 inspector 解释为 skill-guided terminal usage。
- permission/hardline/artifact 完全继承 terminal tool。

MCP mock 侧完成时：

- MCP config / server state / discovered tool 有 typed DTO。
- MCP provider 输出 descriptors。
- MCP adapter 可通过 ToolExecutor 执行 mock tool。
- exposure/gate/artifact/inspector 都能解释 MCP capability。
- lifecycle owner seam 已经存在，即使真实 network/process client 尚未接入。

真实 MCP spike 完成时：

- streamable HTTP MCP 和 stdio MCP 至少各有一个 deterministic integration fixture。
- startup/tool timeout、close、failure、needs-auth 都有测试。
- real MCP tool 不绕过 capability gate。
- hidden/unavailable/not-callable MCP tool 调用 fail-closed。

## 8. 推荐默认选择

- CLI：先做 PR C1 + C2，C3 可跟随但不要阻塞。
- MCP：先做 M0–M2 mock provider/adapter，验证统一 surface；再做 M3 lifecycle；最后分别接 M4 HTTP 和 M5 stdio。
- 不要先接真实 MCP server。先把 config/status/descriptor/binding/lifecycle seam 固定，比一开始连 Firecrawl MCP 更重要。
