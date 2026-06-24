# Terminal Env Contract

_Created: 2026-06-25_

这份文档定义 Pulsara terminal subprocess environment 的长期契约。它不是 implementation plan，而是当前和后续实现必须遵守的硬协议：terminal 命令必须在可解释、可测试、默认安全的环境里运行，同时尽量接近用户本地 shell 能找到的工具链。

相关代码：

- [src/pulsara_agent/runtime/terminal/env.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal/env.py)
- [src/pulsara_agent/runtime/terminal/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal/session.py)
- [src/pulsara_agent/runtime/terminal/process.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal/process.py)
- [tests/test_terminal_env.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_terminal_env.py)
- [tests/test_terminal_runtime.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_terminal_runtime.py)

## 1. 核心原则

- **子进程必须显式传入 `env=`。** 不能让 terminal 隐式继承 Pulsara 进程的完整环境。
- **sanitize 永远先于 shell snapshot。** shell snapshot 只能在清理后的 parent env 上运行。
- **shell snapshot 是便利层，不是安全边界。** snapshot 失败时必须安全降级，而不是放开父环境。
- **PATH 是合并协议，不是普通变量覆盖。**
- **`.venv/bin` 是 per-command overlay。** 它根据 effective cwd 查找，不能固定在 workspace root。
- **diagnostics 只暴露摘要，不暴露完整 env/PATH。**

## 2. 正常执行路径

正常 terminal execution path 是：

```text
TerminalTool
  -> TerminalSession.execute()
  -> TerminalEnvBuilder.build()
  -> ProcessRegistry.exec_with_yield()
  -> spawn_local_process(env=env_result.env)
```

`spawn_local_process()` 中的 `build_default_subprocess_env()` 只是低层防御兜底：只有调用方没有显式传入 `env` 时才使用。正常 manager/session path 必须走 `TerminalEnvBuilder.build()`。

这是调用点级不变量：任何新的 terminal subprocess 入口、后台进程入口、test helper 或 runtime fallback，都不能直接继承 `os.environ`、自己拼 PATH，或绕过 `TerminalEnvBuilder.build()` 定义第二套 env 构造逻辑。低层 `build_default_subprocess_env()` 只能作为防御性 fallback，不是产品路径。

## 3. 唯一规则链

terminal env 必须按下面这条链构造：

```text
raw parent env
  -> sanitize by name allowlist + secret-value scan
  -> optional login+interactive shell snapshot using sanitized parent
  -> sanitize snapshot again
  -> merge non-PATH vars: snapshot overrides sanitized parent
  -> merge PATH:
       nearest .venv/bin
       extra_path_prepends
       shell snapshot PATH
       sanitized parent PATH
       SANE_FALLBACK_PATH
  -> pass explicit env= to subprocess
```

任何新入口、测试 helper、runtime path 都不能定义第二条 env 构造规则。

## 4. TerminalEnvConfig 开关分层

`TerminalEnvConfig` 的字段不是同一类产品开关。它们必须按稳定性和风险分层理解，而不是平铺成“用户想怎么组合都可以”的矩阵。

### 4.1 稳定行为

这些是 terminal env contract 的默认行为，不应作为普通产品开关暴露：

- sanitizer 先于一切 shell 逻辑。
- shell snapshot 使用 sanitized parent env。
- snapshot 输出再次经过 sanitizer。
- `.venv/bin` 是 per-command overlay。
- PATH 按契约顺序合并。
- diagnostics 不泄漏完整 env/PATH。

### 4.2 稳定调参项

这些可以作为稳定配置存在，但不能改变安全边界：

- `shell_snapshot_ttl_seconds`
- `shell_snapshot_timeout_seconds`
- `extra_path_prepends`

它们只能影响 snapshot 缓存/超时和 PATH 前置补充，不能绕过 sanitizer，也不能把完整 parent env 暴露给子进程。

### 4.3 高权限 escape hatch

这些字段是有意保留的逃生口，必须被文档和测试当作高风险能力：

- `passthrough_names`
- `inherit_allowlist`

`passthrough_names` 尤其强：它既加入变量名 allowlist，也跳过 secret-value scan。任何使用它的上层配置都应把它标成“显式承担泄漏风险”，不能把它描述成普通 allowlist。

### 4.4 迁移/调试开关

这些字段可以用于测试、诊断或迁移，但不应成为长期产品分叉：

- `enable_shell_snapshot=False`
- `enable_venv_overlay=False`

关闭 snapshot 或 `.venv/bin` overlay 只能降级便利性，不能改变 sanitizer、PATH fallback 或 explicit `env=` 的安全契约。

## 5. Sanitizer

第一步是 `sanitize_subprocess_env(parent_env, config)`。

允许的变量名：

```text
DEFAULT_ENV_ALLOWLIST | inherit_allowlist | passthrough_names
```

规则：

- `PWD` 永远移除。
- 不在 allowlist 的变量移除。
- 值看起来像 secret 的变量会移除，除非变量名在 `passthrough_names` 中，或变量名是 path/目录结构类变量。
- path/目录结构类变量包括 `PATH`、`HOME`、`SHELL`、`TMPDIR`、`SSH_AUTH_SOCK`、`XAUTHORITY`、`*_DIR`、`*_ROOT`、`*_HOME`、`XDG_*` 等。

`passthrough_names` 是高权限 escape hatch。它既把变量名加入 allowlist，也跳过 secret-value scan。使用它时，调用方是在明确承担把该变量传给 terminal 子进程的风险。

## 6. Shell Snapshot

如果 `enable_shell_snapshot=True`，builder 可以运行一次受控 probe：

```text
login + interactive shell -> printf sentinel -> env -0
```

snapshot 必须满足：

- probe 使用 sanitized parent env。
- probe stdin 是 `/dev/null`。
- probe stderr 丢弃。
- probe stdout 有 timeout 和最大字节数限制。
- probe 输出只解析 sentinel 之后的 `env -0`。
- raw snapshot env 必须再次经过 sanitizer。

snapshot 失败时返回 error snapshot，而不是抛出到 terminal command。失败只意味着 snapshot layer 被丢弃；sanitized parent env、`.venv/bin` overlay 和 fallback PATH 仍然继续生效。

失败 snapshot 可以被缓存到 TTL 过期或 cache key 改变。这个行为是契约的一部分：坏 shell 不应该让每条命令都重复卡住。

## 7. Snapshot Cache Key

snapshot cache key 必须覆盖会改变安全或工具链可见性的输入：

- shell path
- sanitized `HOME`
- workspace root
- shell startup files 的 mtime/size signature
- safe parent env signature
- `inherit_allowlist`
- `passthrough_names`

cache 命中还需要满足：

```text
now - cached.created_at <= shell_snapshot_ttl_seconds
```

## 8. 非 PATH 变量合并

最终 env 先从 sanitized parent env 开始：

```python
env = dict(sanitized_parent)
```

如果 snapshot 成功或至少提供了 env，那么 snapshot 中的非 `PATH` 变量覆盖 parent：

```text
snapshot non-PATH > sanitized parent non-PATH
```

如果 snapshot 失败且 env 为空，它不会覆盖 parent。

## 9. PATH 合并

`PATH` 必须按来源列表合并，而不是普通变量覆盖：

```text
nearest .venv/bin
  > extra_path_prepends
  > shell snapshot PATH
  > sanitized parent PATH
  > SANE_FALLBACK_PATH
```

合并规则：

- 按首次出现去重。
- 越靠前的来源优先级越高。
- `extra_path_prepends` 只有路径存在时加入。
- snapshot 失败或禁用时，snapshot PATH 为空。
- parent PATH 存在时仍追加 `SANE_FALLBACK_PATH` 中缺失的条目。
- 如果低层 fallback path 被迫使用 `build_default_subprocess_env()`，只做 parent sanitizer；只有 sanitized env 没有 PATH 时才填入完整 `SANE_FALLBACK_PATH`。

`.venv/bin` 查找规则：

- 从 effective cwd 向上查找最近 `.venv/bin`。
- 查找边界是 workspace root。
- package-local `.venv/bin` 优先于 workspace-root `.venv/bin`。
- 不允许越出 workspace 使用外部 `.venv/bin`。

## 10. Diagnostics

terminal result metadata 可以暴露 env diagnostics，但不能暴露完整 env 或完整 PATH。

允许的诊断字段包括：

- `sanitized_env_removed_count`
- `sanitized_env_secret_value_removed_count`
- `shell_snapshot_used`
- `shell_snapshot_error`
- `venv_overlay`
- `path_entries_count`

这些字段用于解释“为什么命令找不到工具”或“snapshot 是否生效”。它们不能变成 env dump。

## 11. 禁止事项

- 不允许每条命令默认改成 login/interactive shell。
- 不允许 shell snapshot 绕过 sanitizer。
- 不允许 snapshot 失败后继承完整 parent env。
- 不允许把完整 env/PATH 放进模型上下文。
- 不允许把 `.venv/bin` 固定成 session-start 或 workspace-root snapshot。
- 不允许把 `passthrough_names` / `inherit_allowlist` 当成低风险普通配置。
- 不允许新增 terminal 子进程路径绕过 `TerminalEnvBuilder.build()`。
- 不允许为了 env contract 引入 terminal output crash recovery 承诺。

## 12. 测试守护

这份契约由以下测试守住：

- sanitizer 移除 provider/internal secret env。
- sanitizer 保留 operational/toolchain allowlist。
- loader/hook vars 如 `PYTHONPATH`、`NODE_OPTIONS` 被移除。
- secret-value scan 不误伤 path structural vars。
- `passthrough_names` 是精确且高权限的 escape hatch。
- shell snapshot 过滤 profile noise 和 secrets。
- shell snapshot 使用 login+interactive probe。
- snapshot cache 受 TTL 和 startup file signature 控制。
- snapshot failure 在 TTL 内缓存，TTL 后才重试。
- snapshot 失败 fallback 到 sanitized parent + sane path。
- snapshot 可关闭。
- snapshot 非 PATH 变量覆盖 parent。
- PATH 总优先级为 `.venv/bin > extra_path_prepends > snapshot PATH > parent PATH > fallback PATH`。
- nearest `.venv/bin` 优先 package-local，再退到 workspace root，不越出 workspace。
- diagnostics 不泄漏完整 PATH。
- terminal runtime 中 pipe/PTY child env 都被 sanitize。
- normal terminal path 使用 `TerminalEnvBuilder.build()`，低层 fallback 不能成为产品路径。

## 13. 完成标准

只要这份契约成立，terminal env 的行为就应该可以用一条规则链解释：

- 默认安全：先 sanitize，再做便利层。
- 本地好用：shell snapshot 和 `.venv/bin` 让常见工具链可发现。
- 失败可恢复：snapshot timeout / failure 只降级，不扩大权限。
- 可审计：诊断足够解释行为，但不泄漏完整环境。
