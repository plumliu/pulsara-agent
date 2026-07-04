# Package Facade / Public Import Contract

_Created: 2026-07-04_

本文档冻结 Pulsara Python package facade 的长期契约。它不把所有内部模块声明为稳定 public API；它只冻结当前 `__init__.py` 暴露面、lazy import 边界和 import-cycle 防线。

相关代码：

- [src/pulsara_agent/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/__init__.py)
- [src/pulsara_agent/runtime/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/__init__.py)
- [src/pulsara_agent/tools/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/__init__.py)
- [src/pulsara_agent/capability/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/__init__.py)
- [src/pulsara_agent/host/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/__init__.py)
- [src/pulsara_agent/llm/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/llm/__init__.py)
- [src/pulsara_agent/memory/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/__init__.py)
- [src/pulsara_agent/event/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event/__init__.py)
- [src/pulsara_agent/message/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/message/__init__.py)
- [src/pulsara_agent/graph/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/graph/__init__.py)
- [src/pulsara_agent/jsonld/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/jsonld/__init__.py)
- [src/pulsara_agent/entities/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/entities/__init__.py)
- [src/pulsara_agent/ontology/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/ontology/__init__.py)
- [src/pulsara_agent/storage/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/__init__.py)
- [src/pulsara_agent/retrieval/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/retrieval/__init__.py)
- [src/pulsara_agent/inspector/__init__.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/inspector/__init__.py)

---

## 1. 核心立场

Pulsara package facade 是开发者便利层，不是第二套 runtime composition root。

硬规则：

- `pulsara_agent.__version__` 是根包唯一顶层 public symbol。
- 子包 `__all__` 中列出的名称是该子包 facade 的 public import surface。
- 未列入 `__all__` 的内部模块/名称不承诺稳定 facade import。
- facade 不得做 I/O、读取配置、初始化 provider、打开数据库连接、启动 terminal supervisor 或 sync bundled skills。
- facade 不得绕过 capability/runtime/host composition root 构造生产对象。

---

## 2. Lazy facade 边界

以下两个子包必须保持 lazy facade：

- `pulsara_agent.runtime`
- `pulsara_agent.tools`

原因：

- runtime submodules 依赖 tools、memory、permission；
- tool built-ins 又依赖 runtime session、permission、terminal primitives；
- eager re-export 会重新引入 import cycle。

lazy facade 契约：

- 用 `_LAZY_EXPORTS: dict[str, tuple[module, attr]]` 维护 public names。
- `__all__ = list(_LAZY_EXPORTS)`。
- `__getattr__(name)` 只在访问时 import target module。
- 成功解析后可以 cache 到 `globals()`。
- unknown name 必须抛 `AttributeError(name)`。

不得把 `runtime` 或 `tools` 改回 eager import，除非先证明所有 import-cycle tests 和 CLI startup smoke tests 仍通过，并更新本契约。

---

## 3. Eager facade 边界

以下子包当前是 eager facade：

- `capability`
- `host`
- `llm`
- `memory`
- `event`
- `event_log`
- `message`
- `graph`
- `jsonld`
- `entities`
- `ontology`
- `storage`
- `retrieval`
- `inspector`

这些 facade 可以从内部模块 re-export 常用类型/函数，但必须遵守：

- import 过程不得触达外部服务；
- import 过程不得要求 Postgres/Oxigraph/LLM provider 可用；
- import 过程不得读取 `.env` 或用户 home 配置；
- import 过程不得产生 durable side effect；
- `__all__` 必须与实际可导入名称保持一致。

若某 eager facade 新增 re-export 引入 import cycle，应优先改该子包为 lazy facade，而不是在内部模块里增加局部 hack。

---

## 4. Runtime facade 特别规则

`pulsara_agent.runtime` 是最容易发生 import cycle 的 facade。其 public surface 包括但不限于：

- `AgentRuntime`
- `RuntimeSession`
- `build_agent_runtime_wiring`
- `build_durable_runtime_wiring`
- `build_in_memory_runtime_wiring`
- permission policy types；
- plan / approval / recovery types；
- terminal runtime types；
- context / transcript helpers；
- publisher / hook / timeline helpers。

规则：

- 新增 runtime public symbol 时，必须加入 `_LAZY_EXPORTS`。
- 不得在 `runtime/__init__.py` 顶层 import runtime submodule object。
- 不得从 facade 初始化 `CapabilityRuntime`、`HostCore` 或 `RuntimeSession`。
- facade 只做 symbol routing。

---

## 5. Tools facade 特别规则

`pulsara_agent.tools` 是工具协议和 built-in binding 的 developer convenience facade。

public surface 包括：

- `Tool` / `AsyncTool`
- `ToolCall`
- `ToolExecutionResult`
- `ToolExecutionSuspended`
- `ToolRuntimeContext`
- `ToolResultArtifactCandidate`
- `ToolExecutor`
- `ToolRegistry`
- built-in tool classes；
- `build_core_tool_registry`

规则：

- 新增 built-in tool 若要成为 facade public import，必须加入 `_LAZY_EXPORTS` 与 `__all__`。
- facade import 不得构造 `RuntimeSession` 或 `ToolRegistry`。
- facade import 不得读取 workspace、terminal state 或 memory state。
- built-in tool 行为契约见 `BUILTIN_TOOLS_CONTRACT`。

---

## 6. Capability facade 特别规则

`pulsara_agent.capability` 是 unified capability surface 的 developer convenience facade。

它可以 re-export：

- bundled skill management API；
- `BuiltinToolCapabilityProvider`；
- `LocalSkillCapabilityProvider`；
- descriptor / provider / exposure types；
- render helpers；
- skill health resolver；
- call classifier。

它不得重新引入旧 `CapabilityResolver` / `ResolvedCapabilitySet` / `NoopCapabilityResolver` API。`LocalSkillCapabilityProvider` 是 unified provider，不是旧 resolver fallback。

---

## 7. InMemory / test-only re-export 边界

部分 facade 仍 re-export `InMemory*` 类型，用于测试与兼容。

规则：

- InMemory 类型出现在 facade 不代表 production fallback 合法。
- 生产 runtime storage authority 仍是 Postgres/Oxigraph/real UOW。
- 新 production integration tests 不应依赖 InMemory substrate。
- 若移除某个 InMemory facade export，必须先迁移旧测试或提供明确 deprecation commit。

相关生产边界见：

- `APP_SETTINGS_CLI_ENTRY_CONTRACT`
- `GRAPH_JSONLD_STORAGE_CONTRACT`
- `GOVERNANCE_WRITE_OUTBOX_CONTRACT`
- `ARTIFACT_STORE_CONTRACT`

---

## 8. CLI script entry

`pyproject.toml` 中唯一 console script：

```toml
pulsara = "pulsara_agent.cli:main"
```

CLI entry 的运行语义由 `APP_SETTINGS_CLI_ENTRY_CONTRACT` 冻结。本文件只冻结 import surface：

- import `pulsara_agent.cli` 不应启动 HostCore；
- 调用 `main()` 才开始解析 argv / env-file / settings。

---

## 9. 测试守卫

以下 smoke checks 是 package facade 的最低守卫：

- `import pulsara_agent; pulsara_agent.__version__` 可用；
- `from pulsara_agent.runtime import AgentRuntime, RuntimeSession, build_durable_runtime_wiring` 不触发 import cycle；
- `from pulsara_agent.tools import ToolCall, ToolExecutor, build_core_tool_registry` 不触发 import cycle；
- `from pulsara_agent.capability import CapabilityDescriptor, BuiltinToolCapabilityProvider, LocalSkillCapabilityProvider` 可用；
- `from pulsara_agent.host import HostCore, HostWorkspaceInput` 可用；
- `from pulsara_agent.llm import LLMRuntime, LLMConfig, ModelRole` 可用；
- `from pulsara_agent.memory import MemoryGovernanceEngine, MemoryRecallService, MemoryWriteUnitOfWork` 可用；
- `from pulsara_agent.event import RunStartEvent, ToolResultEndEvent` 可用；
- `from pulsara_agent.message import ToolResultArtifactRef, ToolResultBlock` 可用。

这些 checks 不证明业务行为正确；它们只证明 public facade 没有被 import-cycle 或 missing export 破坏。
