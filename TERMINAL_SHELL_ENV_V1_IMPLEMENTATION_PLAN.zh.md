# Pulsara Terminal Shell / Profile / Env v1 补强实施计划

_Created: 2026-06-20_

本文规划 Pulsara terminal runtime 的 Shell / Profile / Env 补强。它承接 `TERMINAL_RUNTIME_V1_IMPLEMENTATION_PLAN.zh.md` 和 `TERMINAL_YIELD_MODEL_V2_IMPLEMENTATION.zh.md`：v1/v2 已经解决了 managed process、yield 模型、PTY/stdin/poll/kill/streaming 的核心执行形态；本文只处理「子进程拿到什么环境、如何接近用户真实终端、如何避免泄漏 provider secret」。

本文同时沉淀对本地开源项目 Hermes / Codex / OpenClaw 的调研、Claude 对前序方案的批判，以及 Pulsara 的最终落地方案。目标是细致、可考、易于拆 PR 实施。

## 0. 总结

最终决策：

1. **先做 terminal subprocess env sanitizer**：所有 terminal 子进程必须显式传 `env=`。主防线是 **default-deny 的变量名 allowlist**，只允许已知安全/必要的操作变量进入子进程；secret pattern 的值扫描只作为 defense-in-depth。当前 `Popen` 继承完整父环境，这是 present-tense security issue。
2. **再做 base shell env / PATH snapshot**：用受控 login+interactive shell probe 捕获用户 shell 的安全 PATH / 工具链相关变量，解决 `uv`、`node`、`pnpm`、Homebrew、`mise`、`pyenv`、`nvm` 可发现性。snapshot 是便利层，必须建立在 sanitizer 之上。
3. **`.venv/bin` 不写入固定 snapshot**：每条命令按 `effective_cwd` 向上寻找最近 `.venv/bin` 并动态 prepend，避免 monorepo 中 workspace-root `.venv` 覆盖 package-local `.venv`。
4. **不做全局 login/interactive shell 默认**：不要把每条命令都改成 `$SHELL -li -c`。login/interactive shell 启动文件可能输出噪声、阻塞、修改 PATH、执行用户脚本；应只以 snapshot/probe 的形式受控使用，正常命令仍走 non-login/non-interactive shell。
5. **不做磁盘级 crash recovery**：在现有三层输出存储模型下，小输出只存在内存，artifact 只在超阈值后产生。为了 crash recovery 强行把所有 yielded process 从 spawn 开始 tee 到磁盘，会改变存储语义。因此本文把 Hermes-lite crash recovery 降级为 future optional，不纳入本轮 Shell / Env 补强。

推荐实施顺序：

```text
PR1: allowlist env sanitizer + explicit env= + secret/SSH regression tests
PR2: safe PATH/base env snapshot + timeout/cache/fallback + tests
PR3: per-command nearest .venv/bin overlay + monorepo tests
PR4: docs / real smoke / observability polishing
```

## 1. 已核实的 Pulsara 当前事实

### 1.1 子进程当前继承完整父环境

`spawn_local_process()` 在 PTY 和 pipe 两条路径都调用 `subprocess.Popen(...)`，但没有传 `env=`：

- `src/pulsara_agent/runtime/terminal/process.py:231`：PTY path。
- `src/pulsara_agent/runtime/terminal/process.py:243`：pipe path。

Python `subprocess.Popen` 不传 `env` 时会继承当前 Python 进程的完整环境。这意味着 terminal 命令可以看到：

- `PULSARA_API_KEY` / 其他 Pulsara 内部配置。
- `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`DEEPSEEK_API_KEY`、`GEMINI_API_KEY` 等 provider credentials。
- 任意 `*_TOKEN`、`*_SECRET`、`*_PASSWORD`、`*_CREDENTIAL`。
- Codex app / harness / proxy / internal runtime 注入的环境变量。

当前 output 层有 redaction：

- `src/pulsara_agent/runtime/terminal/output.py:13`：`KEY|TOKEN|SECRET|PASSWORD` 输出正则。

但 output redaction 只能保护「已经输出回 Pulsara 的文本」。它不能阻止子进程读取 secret、传给第三方命令、写入项目文件、上传到网络、或被工具自身日志吞掉。因此 secret stripping 必须发生在 subprocess env 入口，而不是只依赖 output redaction。

### 1.2 当前 shell detection 偏保守

`detect_terminal_shell()` 默认：

- `login=False`
- `interactive_init=False`

见 `src/pulsara_agent/runtime/terminal/shell.py:43`。`TerminalSessionManager.__post_init__()` 在未注入 shell 时直接调用 `detect_terminal_shell()`，见 `src/pulsara_agent/runtime/terminal/manager.py:30`。

这意味着 Pulsara 当前不是 login shell；它避免了 profile 噪声和卡顿，但可能缺少用户交互终端里的 PATH，例如：

- `/opt/homebrew/bin`
- `/usr/local/bin`
- `~/.local/bin`
- `~/.cargo/bin`
- `~/.bun/bin`
- `~/.nvm/.../bin`
- `~/.volta/bin`
- `mise` / `asdf` / `pyenv` / `rbenv` 动态 shims。

### 1.3 每条命令是 fresh shell，shell 内 export 不跨命令持久化

`_wrap_command()` 把每条 command 包成：

```sh
cd -- <cwd> || exit 126
eval '<command>'
__pulsara_ec=$?
pwd -P > <cwd_file> || true
exit $__pulsara_ec
```

见 `src/pulsara_agent/runtime/terminal/process.py:570`。

因此用户在一条 terminal 命令里 `export FOO=bar`，本来就不会自动影响下一条 terminal 命令。引入 shell env snapshot 不会破坏一个已经存在的「跨命令 shell 环境持久化」能力，因为当前并没有这项能力。跨命令持久化的是 terminal session 的 cwd，不是 shell process/env。

### 1.4 `.venv/bin` 当前没有 special handling

当前 spawn 只传 `cwd=str(cwd)`，没有根据 cwd 动态 prepend `.venv/bin`。如果父进程 PATH 里没有项目 `.venv/bin`，命令 `python`、`pytest`、`ruff` 可能不走仓库 uv 环境。

AGENTS.md 的全局偏好是「仓库内命令优先用 uv 管理的 `.venv/`」。因此 terminal runtime 应该帮助 agent 更容易走正确环境，但不能靠 session-start 固定一个 workspace-root `.venv/bin`，因为 monorepo 中可能存在 package-local `.venv`。

### 1.5 输出 artifact 的现有语义不支持 crash recovery 承诺

`OutputAccumulator._maybe_write_artifact_locked()` 只有在 `_total_chars > artifact_threshold_chars` 时才写 artifact，见 `src/pulsara_agent/runtime/terminal/output.py:107`。threshold 当前由 `max_output_chars` 驱动，默认 20k。

这是一开始定好的三层输出存储结构：

1. 小输出留内存。
2. 大输出超过阈值才落 artifact。
3. event/log 不永久保存所有 token/chunk。

因此本文不把 crash recovery 纳入当前补强范围。若未来要恢复 yielded process 日志，就必须改变 artifact 语义：yielded process 从 spawn 开始 tee 到磁盘。这不是 Shell / Env 补强的范围。

## 2. 本地开源项目调研

调研对象：

- Hermes: `/Users/plumliu/Desktop/python_workspace/hermes-agent`
- Codex CLI: `/Users/plumliu/Desktop/python_workspace/codex`
- OpenClaw: `/Users/plumliu/Desktop/python_workspace/openclaw`

### 2.1 Hermes：login shell 优先，配合 env sanitizer 和 sane PATH

Hermes 的 local process registry 在 PTY 和 pipe 路径都使用用户 shell 的 login/interactive 形式：

- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/process_registry.py:550`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/process_registry.py:586`

典型形态：

```python
[user_shell, "-lic", f"set +m; {command}"]
```

Hermes 的好处是 terminal 更像用户真实终端：profile/rc 中配置的 PATH、alias、toolchain 初始化更容易生效。它也额外做了环境构造：

- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/environments/local.py:303`：`_make_run_env()`。
- `/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/environments/local.py:296`：`_SANE_PATH`，包含 Homebrew 和系统路径。
- `_make_run_env()` 会剔除 provider env blocklist，除非被显式 passthrough。
- 它还支持 subprocess HOME isolation，把 git/ssh/gh/npm 等工具配置导向 profile HOME。

启示：

1. 用户 shell 复现是 terminal 可用性的关键。
2. 但 login shell 必须和 env sanitizer、PATH policy、secret blocklist 一起做。
3. Pulsara 不宜直接照搬「每条命令 login shell」，因为 profile 输出噪声和卡顿会污染 tool result；但 Hermes 证明了 sane PATH + secret stripping 是必要能力。

### 2.2 Codex：login shell 是受控策略，shell snapshot 是成熟路线

Codex unified exec 的 handler options 中有 `allow_login_shell`：

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs:50`

测试钉死了几个关键行为：

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/shell_tests.rs:122`：显式 login flag 会被尊重。
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/shell_tests.rs:150`：disallow 时默认 non-login。
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/handlers/shell_tests.rs:181`：disallow 时拒绝显式 login。

Codex 的 shell snapshot 机制更值得借鉴：

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/shell_snapshot.rs:69`：为 local environment 构建 shell snapshot。
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/shell_snapshot.rs:265`：用 login shell 运行 snapshot capture script。
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/shell_snapshot.rs:250`：用 non-login shell validate snapshot。
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/shell_snapshot_tests.rs:419`：清理 orphan/stale snapshot。

Codex 还明确区分 runtime-owned PATH prepends：

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tools/runtimes/mod.rs:118`

启示：

1. login shell 不应该是无条件默认，而是 policy/config 控制。
2. 更成熟的路线是「用 login shell 捕获状态，再用 non-login shell source snapshot」。
3. runtime 自己 prepend 的 PATH 要和用户 snapshot PATH 区分，避免 profile 覆盖 runtime 必需路径。

### 2.3 OpenClaw：shell env fallback + shell snapshot + secret filtering

OpenClaw 的 shell env fallback 会运行 login shell `env -0`，带 timeout 和 max buffer：

- `/Users/plumliu/Desktop/python_workspace/openclaw/src/infra/shell-env.ts:84`
- `/Users/plumliu/Desktop/python_workspace/openclaw/src/infra/shell-env.ts:171`
- `/Users/plumliu/Desktop/python_workspace/openclaw/src/infra/shell-env.ts:228`

它还提供 `getShellPathFromLoginShell()` 用 login shell probe 得到 PATH：

- `/Users/plumliu/Desktop/python_workspace/openclaw/src/infra/shell-env.ts:290`

OpenClaw 的 shell snapshot 更完整：

- `/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/shell-snapshot.ts:1`
- SAFE env names 包含 `PATH`、`NVM_DIR`、`PYENV_ROOT`、`BUN_INSTALL`、`CARGO_HOME`、`VOLTA_HOME` 等，见 `shell-snapshot.ts:24`。
- secret env / shell state patterns 明确过滤 `TOKEN`、`API_KEY`、`SECRET`、private key 等，见 `shell-snapshot.ts:53`。
- snapshot 有 refresh TTL、max age、cache key、startup signature、validate，见 `shell-snapshot.ts:15`、`shell-snapshot.ts:125`。

OpenClaw exec runtime 在执行前会：

- sanitize host base env，见 `/Users/plumliu/Desktop/python_workspace/openclaw/src/agents/bash-tools.exec-runtime.ts:92`。
- validate host env override，禁止危险 env 和 PATH override，见 `bash-tools.exec-runtime.ts:108`。
- apply shell PATH，见 `bash-tools.exec-runtime.ts:332`。
- wrap command with shell snapshot，见 `bash-tools.exec-runtime.ts:852`。

启示：

1. shell snapshot 是自然的安全 chokepoint。
2. 需要明确 safe env allowlist，并把 secret pattern 检查降级为 defense-in-depth。
3. PATH discovery 与 env safety 应在同一 env construction pipeline 中完成，而不是散落在 tool 层。

## 3. Claude 审查批判与采纳结论

Claude 对前序思路提出了四条关键批判，本文全部采纳。

### 3.1 子进程 env secret leak 比 PATH 便利性优先级更高

事实：Pulsara 当前 `Popen` 无 `env=`，继承完整父环境。结论：这不是未来优化，而是当前安全缺口。

采纳：

- PR1 必须先做 explicit sanitized env。
- env sanitizer 的主防线是 **变量名 allowlist**，不是变量名子串 denylist。allowlist 自动挡住下一个 provider 的新密钥名；denylist/default-allow 永远滞后。
- `SSH_AUTH_SOCK`、`XDG_SESSION_*`、`DBUS_SESSION_BUS_ADDRESS` 这类操作变量不能被 `AUTH` / `SESSION` 子串误伤。变量名不做宽泛子串 deny。
- PATH/snapshot 是 PR2，不能先于 PR1。
- output redaction 保留，但不作为 secret 安全主防线。

### 3.2 `.venv/bin` 必须 per-command，而不是 session-start 固定

问题：若在 session start 固定 workspace-root `.venv/bin`，用户之后 `cd packages/foo`，该 package 自己有 `.venv` 时仍会用 root `.venv`。

采纳：

- base shell env snapshot 可以按 session/workspace 缓存。
- `.venv/bin` overlay 必须按每条命令的 `decision.effective_cwd` 动态解析。
- 搜索策略：从 effective cwd 向上走到 workspace_root，找到最近的 `.venv/bin` 且可执行/存在就 prepend。

### 3.3 snapshot staleness 是可接受限制，但要写明

`nvm use 20`、用户在外部真实 shell 修改 profile、安装新工具等，不一定立即反映到 Pulsara terminal。Codex/OpenClaw 都通过 cache key / TTL / cleanup 接受这个限制。

采纳：

- v1 snapshot 设置 TTL，但不是唯一失效机制。
- cache key 应纳入 startup files mtime/size；用户改 `.zshrc` / `.bashrc` 时，下条命令可以因 cache key 变化而重新 probe，不必等 TTL。
- 允许 config/env 禁用 snapshot 或强制 refresh。
- 文档明确：snapshot 不是实时追踪用户 live shell；例如另一个 shell 里执行 `nvm use 20` 但不修改 startup files 时，可能要等 TTL 或显式 refresh。

### 3.4 crash recovery 不纳入本轮，避免破坏三层输出存储

Claude 指出：如果 crash recovery 承诺「读已有日志」，当前 artifact-on-overflow 不成立。要诚实支持，就必须 yielded process 从 spawn 开始 tee 到 artifact。

采纳：

- 本文把 crash recovery 降级为非目标。
- 不为 crash recovery 改 OutputAccumulator 的阈值语义。
- 未来若重新打开 crash recovery，必须同时设计：
  - yielded artifact tee from spawn。
  - checkpoint。
  - PID/PGID identity verification，避免 PID reuse 误杀无关进程。
  - recovered/detached 语义。

## 4. 最终目标状态

目标：所有 terminal 子进程都通过同一条 env construction pipeline：

```text
base parent env
  -> default-deny allowlist by env name
  -> value-level secret scan for allowed names
  -> merge safe shell snapshot env (PATH/toolchain only)
  -> apply runtime-owned PATH prepends
  -> apply per-command nearest .venv/bin overlay
  -> pass env= to subprocess.Popen
```

### 4.1 新模块建议

新增：

```text
src/pulsara_agent/runtime/terminal/env.py
```

职责：

1. 构造 sanitized subprocess env。
2. 识别和剔除 secret/dangerous env。
3. 捕获/缓存 safe shell env snapshot。
4. 合并 PATH。
5. 每条命令按 cwd 动态 prepend nearest `.venv/bin`。

建议接口：

```python
@dataclass(frozen=True, slots=True)
class TerminalEnvConfig:
    enable_shell_snapshot: bool = True
    shell_snapshot_ttl_seconds: float = 300.0
    shell_snapshot_timeout_seconds: float = 5.0
    inherit_allowlist: frozenset[str] = frozenset()
    passthrough_names: frozenset[str] = frozenset()
    extra_path_prepends: tuple[Path, ...] = ()
    enable_venv_overlay: bool = True


@dataclass(frozen=True, slots=True)
class TerminalEnvSnapshot:
    env: dict[str, str]
    created_at: float
    shell_path: Path
    source: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalEnvBuildResult:
    env: dict[str, str]
    diagnostics: dict[str, object]


class TerminalEnvBuilder:
    def build(
        self,
        *,
        cwd: Path,
        workspace_root: Path,
        shell: TerminalShellConfig,
    ) -> TerminalEnvBuildResult:
        ...
```

`inherit_allowlist` 只是对 §4.2 固定 base/toolchain allowlist 的用户扩展；固定 allowlist 始终生效，不能由空配置替代。`passthrough_names` 只接受精确变量名，用于未来显式透传 secret 或 dual-use env；不提供 prefix/wildcard 透传。

### 4.2 Sanitizer 规则

Sanitizer 的主防线是 **default-deny allowlist**：

1. 变量名必须命中固定精确 allowlist，或命中代码内置的极少数受控前缀，才有资格进入 terminal 子进程。用户配置只能追加精确变量名，不能追加 prefix/wildcard。
2. 对已通过 allowlist 的变量，再做 **shape-specific** value-level secret scan，例如 `Bearer ...`、`sk-...`、`ghp_...`、`github_pat_...`、`xoxb-...`、`AKIA...`、`-----BEGIN ... PRIVATE KEY-----`、以及明确的 provider token 格式。不要做泛化熵检测、长 hex 检测、长随机字符串检测；`DBUS_SESSION_BUS_ADDRESS` 的 `guid=...` 和 `XAUTHORITY` 的随机后缀是合法操作值，不能被 defense-in-depth 扫描剥掉。`PATH`、`HOME`、`SHELL`、`TMPDIR`、`SSH_AUTH_SOCK`、`XAUTHORITY`、`*_DIR`、`*_ROOT`、`*_HOME`、`XDG_*` 这类路径/结构变量不做 token value scan；否则一个目录名如 `.cache/sk-.../bin` 会把整条 PATH 静默剥掉。
3. 不在变量名上做宽泛子串 deny，例如 `AUTH` / `SESSION`。这会误伤 `SSH_AUTH_SOCK`、`XAUTHORITY`、`XDG_SESSION_TYPE`、`DBUS_SESSION_BUS_ADDRESS` 等常见操作变量。
4. snapshot 路径和 parent-env 路径使用同一套 allowlist，只是 value source 不同：PR1 从 sanitized parent env 取值，PR2 增加 safe shell snapshot 作为额外来源。

基础 allowlist 建议：

- `HOME`
- `USER`
- `LOGNAME`
- `SHELL`
- `TMPDIR`
- `TEMP`
- `TMP`
- `LANG`
- `LC_ALL`
- `LC_CTYPE`
- `TERM`
- `COLORTERM`
- `PATH`
- `SSH_AUTH_SOCK`
- `XAUTHORITY`
- `DISPLAY`
- `WAYLAND_DISPLAY`
- `DBUS_SESSION_BUS_ADDRESS`
- `XDG_RUNTIME_DIR`
- `XDG_SESSION_TYPE`
- `XDG_CURRENT_DESKTOP`
- `XDG_DATA_HOME`
- `XDG_CONFIG_HOME`
- `XDG_CACHE_HOME`
- `XDG_STATE_HOME`
- `PWD` 不从父环境继承，由 subprocess cwd 决定。

Toolchain-root allowlist 必须在 **PR1** 就定义完整，即使 shell snapshot 到 PR2 才实现。这样 PR1 合入后不会因为 default-deny 把父环境里的 `NVM_DIR` / `PYENV_ROOT` / `VOLTA_HOME` 剥掉，制造 PR1 -> PR2 的工具发现性回归窗口。

PR1 起就允许的 toolchain/path 变量建议：

- `NVM_DIR`
- `VOLTA_HOME`
- `PNPM_HOME`
- `BUN_INSTALL`
- `CARGO_HOME`
- `RUSTUP_HOME`
- `PYENV_ROOT`
- `RBENV_ROOT`
- `ASDF_DIR`
- `MISE_DATA_DIR`
- `MISE_CONFIG_DIR`
- `MISE_CACHE_DIR`
- `HOMEBREW_PREFIX`
- `HOMEBREW_CELLAR`
- `HOMEBREW_REPOSITORY`
- `GOPATH`
- `GOROOT`

仍然默认不允许：

- provider/vendor secrets，例如 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`DEEPSEEK_API_KEY`、`GEMINI_API_KEY`、`DASHSCOPE_API_KEY`、`MOONSHOT_API_KEY`、`ZHIPUAI_API_KEY`、`PULSARA_API_KEY`。
- Pulsara 内部运行配置，除非未来明确进入 allowlist。即使不含 secret，也应避免把 provider/base-url/profile 信息泄漏给任意 shell command。
- loader/hook 类高风险变量，例如 `LD_PRELOAD`、`DYLD_INSERT_LIBRARIES`、`PYTHONPATH`、`PYTHONHOME`、`NODE_OPTIONS`、`RUBYOPT`、`PERL5OPT`、`SSLKEYLOGFILE`。

`PYTHONPATH` / `NODE_OPTIONS` 是 dual-use 变量，不一定是 secret；剔除它们是有意的 security-over-convenience 取舍，因为它们能改变解释器/Node 运行时加载行为。若用户确实需要，应走未来显式 pass-through，而不是默认继承。

注意：

- `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` 是否保留需要产品决策。当前用户环境依赖本地 proxy helpers；但 provider/internal proxy 不应默认漏给用户命令。v1 建议默认保留普通 proxy 变量，因为 terminal 命令如 `git`/`curl` 可能需要网络；若未来区分 provider proxy 与 user proxy，应改成 host 显式传入。
- `PATH` 不接受 model/tool call 直接覆盖；只能由 env builder 的安全流程合并。

### 4.3 Shell snapshot 规则

v1 先做「env snapshot」而不是完整 shell state snapshot。也就是说，暂不 capture alias/function/setopt；只捕获安全 env，尤其 PATH 和工具链 root。

捕获方式：

```sh
$SHELL -li -c 'printf sentinel; env -0'
```

这里使用 login + interactive 是有意选择：zsh 的 `.zshrc` 只在 interactive shell 中读取，而 `nvm`、`pyenv`、`mise`、`conda` 等 macOS/zsh 工具链初始化经常写在 `.zshrc`。只用 `$SHELL -l -c` 会读取 `.zprofile` / `.zlogin`，但不会读取 `.zshrc`，导致 cache key 追踪 `.zshrc` 却 probe 不受其影响。Pulsara 不把用户命令改成 interactive shell；只让 snapshot probe 在 timeout、bounded output、stderr discard、stdin `/dev/null`、sentinel parsing、allowlist re-sanitize 这些护栏内运行一次。

约束：

1. 只对受信 shell 路径执行：
   - `$SHELL` 必须是绝对路径。
   - 文件存在且可执行。
   - 可选：必须出现在 `/etc/shells`。
2. timeout 必须短，建议 5s；失败就降级为 sanitized parent env + sane PATH。
3. max output buffer 必须有限，并在读取阶段 enforce，避免 profile 输出/异常膨胀。
4. probe 本身可以用 sanitized parent env 作为入口；即便 login shell 启动文件读取到外部环境，`env -0` 的解析结果也必须经过 §4.2 同一套 allowlist + value scan，不能直接透传。
5. 解析 `env -0` 后只保留 §4.2 的 safe names。PR2 不新增另一套 safe-name 体系，只新增一个 value source。
6. snapshot 结果按 shell path、HOME、startup files mtime/size、父环境安全签名、workspace_root 缓存。
7. v1 的 snapshot cache 假设 tool loop 是同步单线程；若未来启用并发 tool execution，需要给 cache map/entry 增加锁或改成 immutable copy-on-write。

cache key 包含 startup files mtime/size，因此 staleness 不是「只能等 TTL」。用户修改 `.zshrc` / `.bashrc` 后，下条命令应因 cache key 变化重新 probe。TTL 主要覆盖不会改变 startup files 的 live-shell 状态变化，例如用户在另一个终端里临时 `nvm use 20`。

### 4.4 PATH 合并顺序

建议最终 PATH 顺序：

```text
nearest .venv/bin for effective cwd
runtime extra_path_prepends
safe shell snapshot PATH
sanitized parent PATH
sane fallback PATH
```

其中 sane fallback PATH：

```text
/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin
```

合并规则：

1. 去重，保留靠前优先级。
2. 空 path entry 丢弃。
3. 不把不存在目录 prepend 到 PATH，除非是系统 fallback 常见路径；`.venv/bin` 必须存在才加入。
4. macOS/Linux 用 `:`；Windows 暂不作为 v1 主目标。

### 4.5 Per-command `.venv/bin` overlay

函数建议：

```python
def find_nearest_venv_bin(cwd: Path, workspace_root: Path) -> Path | None:
    current = cwd.resolve()
    root = workspace_root.resolve()
    while current == root or root in current.parents:
        candidate = current / ".venv" / "bin"
        if candidate.is_dir():
            return candidate
        if current == root:
            break
        current = current.parent
    return None
```

使用 `decision.effective_cwd`，不是 request 原始 `workdir`。这样 `workdir=""`、`workdir="."`、session cwd persistence、workspace escape guard 全部已经被 policy 层归一化。

行为：

- `cd packages/foo` 后，下一条命令 effective cwd 是 `packages/foo`，优先找 `packages/foo/.venv/bin`。
- 如果 package 没有 `.venv`，再向上找 workspace-root `.venv/bin`。
- 如果都没有，不改 PATH。

## 5. 实施切分

### PR1：Env Sanitizer 最小安全闭环

目标：所有 terminal subprocess 都显式传 `env=`，且 provider secrets 不进入子进程。

改动：

1. 新增 `src/pulsara_agent/runtime/terminal/env.py`。
2. 实现 `sanitize_subprocess_env(parent_env: Mapping[str, str]) -> dict[str, str]`，以 §4.2 的 allowlist 为主防线。
3. `TerminalSessionManager` 持有 `TerminalEnvBuilder` 或 `TerminalEnvConfig`。
4. `TerminalSession.execute()` 在拿到 `decision.effective_cwd` 后 build env。
5. `ProcessRegistry.exec_with_yield()` / `spawn_local_process()` 增加 `env: Mapping[str, str]` 参数。
6. PTY 和 pipe `subprocess.Popen` 都传 `env=dict(env)`。
7. `TerminalResult.metadata["env"]` 只允许放 diagnostics，不得放完整 env。可包含：
   - `sanitized_env_removed_count`
   - `shell_snapshot_used`
   - `venv_overlay`
   - `path_entries_count`

不做：

- 不做 shell snapshot。
- 不做 `.venv` overlay。
- 不改变 shell login 行为。
- 不改变 output artifact 策略。

PR1 必须同时定义完整 safe-name allowlist，包括 §4.2 的 toolchain-root 变量。PR2 只增加 shell snapshot 作为来源，不能把 `NVM_DIR` / `PYENV_ROOT` / `VOLTA_HOME` 等安全名字推迟到 PR2，否则 PR1 会制造工具发现性回归窗口。

验收测试：

1. 设置 `PULSARA_API_KEY=secret`、`OPENAI_API_KEY=secret`、`FOO_TOKEN=secret`，运行 `env`，输出不包含这些变量名和值。
2. 普通变量如 `HOME`、`LANG`、`PATH` 仍可见。
3. `SSH_AUTH_SOCK` 保留，且没有被 `AUTH` 子串误伤；`XDG_SESSION_TYPE` / `DBUS_SESSION_BUS_ADDRESS` 的处理按 allowlist 明确。
4. `NVM_DIR`、`PYENV_ROOT`、`VOLTA_HOME`、`PNPM_HOME` 等 toolchain-root 变量在 PR1 从 parent env 保留。
5. `PYTHONPATH`、`NODE_OPTIONS`、`LD_PRELOAD`、`DYLD_INSERT_LIBRARIES` 被剔除，并在测试名/注释中标明这是 security-over-convenience。
6. PTY 路径也不泄漏 secret。
7. pipe 路径也不泄漏 secret。
8. 子进程能正常运行 `echo hi`、`pwd`、`python -c ...`。
9. metadata 不包含完整 env。
10. output redaction 测试保留；但新增测试要证明即使命令 `env`，secret 也不存在，而不是存在后被 `[REDACTED]` 替换。

### PR2：Base Shell Env / PATH Snapshot

目标：在不无脑 login shell 的前提下，捕获用户 shell 的安全 PATH，提升工具发现率。

改动：

1. `TerminalEnvBuilder` 增加 shell snapshot cache。
2. 实现 `capture_shell_env_snapshot(shell, parent_env, timeout)`。
3. 支持 config/env：
   - `PULSARA_TERMINAL_SHELL_SNAPSHOT=0|1`，默认 `1`。
   - `PULSARA_TERMINAL_SHELL_SNAPSHOT_TTL_SECONDS`，默认 `300`。
   - `PULSARA_TERMINAL_SHELL_SNAPSHOT_TIMEOUT_SECONDS`，默认 `5`。
4. snapshot 失败时 fallback 到 sanitized parent PATH + sane fallback PATH，不 block terminal command。
5. 只合并 §4.2 allowlist 里的 safe env names，不引入 secret。

验收测试：

1. fake shell probe 返回 `PATH=/custom/bin:/usr/bin`，terminal env PATH 包含 `/custom/bin`。
2. fake shell probe 返回 `OPENAI_API_KEY=secret`，不会进入 child env。
3. fake shell probe timeout/fail，terminal 命令仍运行。
4. snapshot cache 在 TTL 内复用。
5. startup file mtime/size 变化时，即使 TTL 未过也重新 probe。
6. TTL 过期后重新 probe。
7. `PULSARA_TERMINAL_SHELL_SNAPSHOT=0` 时不 probe。
8. PATH 去重且优先级稳定。

### PR3：Per-command `.venv/bin` Overlay

目标：按 effective cwd 动态选择最近 `.venv/bin`，符合 uv / monorepo 工作流。

改动：

1. `TerminalEnvBuilder.build(cwd=decision.effective_cwd, workspace_root=...)` 中应用 nearest venv overlay。
2. `TerminalEnvBuildResult.diagnostics` 记录：
   - `venv_overlay_path`
   - `venv_overlay_applied`
3. 不在 snapshot 中固定 `.venv/bin`。

验收测试：

1. workspace root 有 `.venv/bin/python`，在 root 下运行 `python -c ...` 使用 root venv。
2. `packages/foo/.venv/bin/python` 存在，`cd packages/foo` 后优先使用 package venv。
3. `packages/bar` 无 `.venv`，向上 fallback root `.venv`。
4. `workdir="."` / auto-filled empty workdir 仍 honor session cwd，并基于 session cwd 找 `.venv`。
5. workspace 外 `.venv` 不会被使用。
6. `.venv/bin` 不存在时不修改 PATH。

### PR4：Docs、Real Smoke、Observability

目标：把行为写清楚，并用真实命令验证 daily usability。

改动：

1. 更新 terminal 文档：说明 env sanitizer、snapshot、`.venv` overlay、snapshot staleness。
2. 增加 debug metadata，但不泄露完整 env。
3. 增加 real smoke 测试或手动测试脚本：
   - `uv --version`
   - `which uv`
   - `python -c "import sys; print(sys.executable)"`
   - `env | grep -E "API_KEY|TOKEN|SECRET"` 应为空或无 secret。

验收：

1. `uv run pytest tests/test_terminal_runtime.py tests/test_tools.py` 通过。
2. real LLM terminal test 不因缺 PATH 失败。
3. real command `env` 不泄漏 provider secret。

## 6. 安全边界与失败模式

### 6.1 Secret 只允许显式 pass-through

默认 deny secret。若未来用户确实需要在 terminal 中使用某个 secret，比如跑部署命令，需要显式机制，例如：

```text
PULSARA_TERMINAL_ENV_PASSTHROUGH_NAMES=FOO_TOKEN,BAR_SECRET
```

v1 可以先不做 pass-through；如果做，必须：

1. 只允许精确变量名，不允许 wildcard。
2. tool result metadata 不回显变量值。
3. tests 覆盖默认 deny 与显式 allow。

### 6.2 Snapshot 不应 block 命令

shell profile 很容易慢、输出噪声、执行错误。snapshot 失败不应该让 terminal command 失败；应 fallback 并记录 diagnostics。

### 6.3 不捕获 alias/function 作为 v1

Codex/OpenClaw 的完整 shell snapshot 可以捕获 alias/functions/setopts。Pulsara v1 先只捕获安全 env/PATH。理由：

1. env/PATH 是当前痛点。
2. alias/function 更容易藏副作用和 secret。
3. Pulsara terminal commands 应尽量是可移植 shell command，不依赖用户 alias。

### 6.4 Snapshot staleness 是 accepted limitation

若用户编辑 `.zshrc` / `.bashrc` 等 startup files，cache key 的 mtime/size 签名应让下条命令重新 probe。真正的 staleness 主要来自不落盘的 live-shell 状态，例如用户在另一个终端里临时 `nvm use 20` 但不修改 startup files；这种变化需要等 TTL 或显式 refresh。

### 6.5 Proxy 变量需要单独产品判断

本机环境中用户通过 `proxy_on` / `proxy_off` 管理 `127.0.0.1:7890`。Terminal 命令如 `git`、`curl`、`npm` 有时需要 proxy。v1 建议暂时保留普通 proxy env，但不要把 provider/internal-only proxy 变量混进去。后续若发现泄漏风险，应拆出 host config 明确控制。

## 7. 非目标

本文不做：

1. 不做磁盘级 crash recovery。
2. 不改变 OutputAccumulator 的 artifact-on-overflow 语义。
3. 不让所有命令默认 login shell。
4. 不支持完整 alias/function snapshot。
5. 不引入 Docker/SSH/remote backend。
6. 不做 Windows PowerShell 完整环境复现。
7. 不让 LLM 通过 tool args 任意传 env 或 PATH。

## 8. Future Optional：Crash Recovery 的重新打开条件

若未来 production host/driver 需要 crash recovery，必须先满足：

1. yielded process 从 spawn 开始 tee redacted output 到 artifact，否则恢复后无日志可读。
2. checkpoint 存储 `process_id / pid / pgid / command / cwd / started_at / workspace_key / artifact_path`。
3. 恢复时验证 PID identity，至少包含 process start time 和 cmdline；不能只做 liveness probe。
4. 恢复后的状态是 `recovered/detached`，只承诺 list/status/kill/read artifact，不承诺恢复 live pipe/PTY/stdin。
5. checkpoint TTL 和 workspace scope gate 必须明确，避免跨 workspace 误 adopt。

在这些条件未满足前，Pulsara 的正式契约是：

- runtime/session 正常 close 时 best-effort cleanup。
- 同一 runtime session 内可管理 yielded process。
- host/app/Python crash 后不承诺恢复 terminal process。

## 9. 最小代码改动地图

预计触碰：

```text
src/pulsara_agent/runtime/terminal/env.py              # 新增
src/pulsara_agent/runtime/terminal/manager.py          # 注入 env builder/config
src/pulsara_agent/runtime/terminal/session.py          # build env at effective_cwd
src/pulsara_agent/runtime/terminal/process.py          # spawn_local_process(env=...)
src/pulsara_agent/runtime/terminal/models.py           # 如需 metadata/config 扩展
src/pulsara_agent/tools/builtins/terminal.py           # 仅 metadata/diagnostics 透传，不解析 env args
tests/test_terminal_runtime.py                         # sanitizer/snapshot/venv tests
tests/test_tools.py                                    # tool-level metadata/schema regressions
```

不应触碰：

- LLM adapter。
- memory system。
- terminal yield model schema。
- OutputAccumulator artifact threshold。
- crash recovery checkpoint。

## 10. 一句话收束

Terminal Shell / Env 补强的主线不是「让命令更像我的 zsh」这么简单，而是先用 default-deny allowlist 收住 subprocess env 的安全边界，再用受控 snapshot 恢复用户工具链可发现性，最后用 per-command `.venv/bin` overlay 对齐真实项目目录。Hermes 证明 login shell + sane env 很重要；Codex/OpenClaw 证明 snapshot 和 policy 更成熟；Claude 的 critique 把优先级钉死：**先 allowlist env sanitizer，再 PATH snapshot，再 cwd-relative `.venv` overlay**。Crash recovery 暂不做，因为它会倒逼改变三层输出存储语义。
