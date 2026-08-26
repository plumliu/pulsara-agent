# Capability, Tool and Policy Contract

## 1. Catalog

Builtin descriptor、availability、invocation owner、permission action与long-horizon classification
由 `capability.builtin_catalog` 单一拥有。Runtime不得维护第二套 tool-name/action set。

当前 direct tools为 filesystem、todo、terminal与terminal_process；subagent工具为
spawn/list/wait/stop；memory工具为 search/get/explain与五类 proposal。Tool schema在最终 provider
request中参与真实 token estimation。

## 2. Skills

Bundled/local skill属于 capability composition，不是 durable execution owner。Active skill内容被
Host显式lower到 system/capability input；CLI的 `--skill`不得静默忽略。

## 3. Policy

唯一 pre-dispatch authority是 typed `ToolDispatchAuthorizationPolicy`。Message-before-dispatch与
attempt-before-effect fence不能被 hook、tool implementation或UI绕过。

Read-only、allow-without-confirmation、confirm与deny均为 closed disposition。Tool effect不能从
metadata、自报 category或客户端状态推导 permission。

## 4. Extensions

Extension分为 live、post-commit与operational plane。Registration是 Host-scoped、bounded、具名且
有 timeout/lease；ordinary hook best-effort、无 durable catch-up。Hook不能获得 canonical
mutation port或 sealed event appender。

未获 first-party认证与 explicit capability的 registration只能收到 typed digest/size/redaction，
不能读取 tool arguments或 secret。Caller自报 `authenticated_first_party=true`不构成授权证据。

## 5. MCP

当前 Kernel只做 neutral MCP config detection。发现 enabled server时 composition fail closed；不静默
忽略，也不恢复 legacy MCP SDK/supervisor/continuation/recovery graph。未来 MCP实现必须单独通过
typed policy与process-local/durable边界审查。
