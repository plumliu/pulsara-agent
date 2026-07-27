# Package Facade / Public Import Contract

_Created: 2026-07-04_

_D4 hard cut: 2026-07-26_

本文档冻结 Pulsara Python package facade 的长期契约。Facade 是极小、无副作用的公开导入面，
不是 composition root，也不得被用来隐藏 package dependency cycle。

相关实现：

- `src/pulsara_agent/runtime/__init__.py`
- `src/pulsara_agent/tools/__init__.py`
- `src/pulsara_agent/ports/`
- `tests/test_package_facade.py`
- `tests/test_dependency_architecture.py`

---

## 1. 核心规则

- `pulsara_agent.__version__` 是根包唯一顶层 public symbol。
- 子包 `__all__` 是该 facade 的唯一公开面；未列入的内部 symbol 不承诺稳定。
- facade import 不得执行 I/O、读取 settings/env、连接数据库、启动 worker、构造 Host/runtime，
  或同步 workspace skills。
- facade 不得含 `_LAZY_EXPORTS`、`__getattr__`、dynamic import、local import router或
  `TYPE_CHECKING` compatibility branch。
- package 内部调用方必须从 symbol 的 owning module direct import。
- 新 facade export必须是 eager、无副作用，并由 dependency scanner证明不会形成 D4 forbidden edge。

---

## 2. Runtime Facade

`pulsara_agent.runtime` V1 是空 facade：

```python
__all__: list[str] = []
```

以下旧 convenience imports 已硬切且不提供兼容 shim：

- `AgentRuntime`、`RuntimeSession`；
- `ToolCall`、`ToolExecutor`；
- wiring builders；
- permission、plan、approval、recovery、terminal、context helpers；
- `build_in_memory_runtime_wiring`。

调用方必须直接导入，例如：

```python
from pulsara_agent.runtime.agent import AgentRuntime
from pulsara_agent.runtime.session import RuntimeSession
from pulsara_agent.runtime.tool_executor import ToolExecutor
from pulsara_agent.ports.tool_execution import ToolCall
```

恢复 runtime facade symbol需要单独契约修改；不能因为出现 cycle 就恢复 lazy routing。

---

## 3. Tools Facade

`pulsara_agent.tools` 只 eager export：

```python
from pulsara_agent.tools.registry import ToolRegistry

__all__ = ["ToolRegistry"]
```

以下旧 exports 已硬切：

- Tool/AsyncTool execution contracts；
- `ToolCall`、result/suspension DTO；
- `ToolExecutor`；
- `build_core_tool_registry`；
- built-in concrete classes。

Tool contracts从 `pulsara_agent.ports.*` 导入，concrete built-in从其具体 module导入，runtime
orchestration从 `pulsara_agent.runtime.*` owning module导入。

---

## 4. 其他 Facade

其他子包可以保留有限 eager re-export，但必须满足：

- `__all__` 与实际可导入名称逐字一致；
- import不触达外部资源；
- 不把 test support、production fake、composition selector或 concrete lower-layer owner暴露为
  产品 fallback；
- schema facade不得导入 replay/reducer；
- capability facade不得反向导入 concrete runtime/tools implementation。

出现 cycle时，应移动 contract ownership或注入 port，禁止把 eager facade改成 lazy router。

---

## 5. Test-Support Boundary

以下对象只允许位于 `tests/support`：

- whole in-memory runtime composition；
- component Host composition；
- `MockMcpClientManager`；
- fake governance UOW；
- in-memory artifact index等 component fake。

`src/`、production benchmarks和packaged exports不得 import `tests`。低层通用 deterministic
in-memory data structure可以保留在其 owning production module，但它不构成可选产品 composition。

---

## 6. CLI 与 Import Smoke

`pyproject.toml` 唯一 console script仍为：

```toml
pulsara = "pulsara_agent.cli:main"
```

import `pulsara_agent.cli` 不启动 Host；调用 `main()` 后才解析参数和构造 production composition。

最低 smoke：

- `import pulsara_agent.runtime` 得到空 `__all__`，无 lazy router；
- `import pulsara_agent.tools` 只得到 exact `ToolRegistry`；
- 不同 direct owning-module import顺序不触发 cycle；
- removed convenience symbol稳定不可见；
- production source没有 `from pulsara_agent.runtime import ...`；
- production source从 tools facade最多导入 exact `ToolRegistry`。

---

## 7. Architecture Gate

`tests/test_package_facade.py` 与 `tests/test_dependency_architecture.py` 是强制 gate。它们检查：

- runtime/tools facade形状；
- removed router/module物理不存在；
- production/test-support隔离；
- direct owning-module imports；
- canonical AST dependency observation；
- D4 target DAG forbidden edge为零。

全局 package SCC 尚未承诺在 D4 消失。剩余 SCC 以 canonical observation fingerprint冻结，交由
D5/D6继续收口；新增同一 package pair下的 module import仍会使 baseline失败。
