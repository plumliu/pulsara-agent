# 本地 Agent 沙箱与权限系统调研

本文调研本地开源 agent 项目中“沙箱”的实际含义，并对照 Pulsara 现状。这里讨论的是本地执行权限，不是 E2B 之类的云端隔离环境。

## 结论

本地 agent 产品里的“沙箱”通常不是一个单一功能，而是五层机制的组合：

1. **权限模式**：agent 默认能不能改文件、跑命令、访问网络。
2. **审批策略**：哪些动作直接允许，哪些动作需要用户确认。
3. **工具策略**：哪些工具在当前 session/agent/入口下可见或可用。
4. **文件系统边界**：只能读写 workspace，还是允许访问宿主机任意路径。
5. **执行隔离**：命令是否真的跑在 OS sandbox、容器、SSH/远程环境里。

最重要的产品事实是：很多本地用户会主动选择 `danger-full-access` / `bypassPermissions`。这不是用户“不懂风险”，而是他们把本地 agent 当作自己的强力 shell 来用。一个有用的权限系统不应该假设用户永远想被保护；它应该让高权限模式可命名、可观察、可审计，并在少数灾难性动作上保留底线。

## Codex

Codex 的模型最清晰：**sandbox mode** 和 **approval policy** 是分开的。

- Sandbox mode: `read-only | workspace-write | danger-full-access`
- Approval policy: `never | on-request | on-failure | untrusted`
- 内置 preset 把两者组合，例如 read-only/workspace-write 默认需要 approval，full-access 默认不问。
- SDK 层明确暴露这些选项：`sdk/typescript/src/threadOptions.ts`。
- Rust 层有 permission profile 与 approval preset：`codex-rs/utils/approval-presets/src/lib.rs`。
- 执行隔离层是跨平台 OS sandbox：macOS Seatbelt、Linux Landlock、Windows restricted token。

Codex 给 Pulsara 的启发是：不要把“能否访问”与“是否询问”混成一个布尔值。`danger-full-access` 应该是一等 profile，而不是隐藏的异常状态。

## Claude Code

Claude Code 把用户体验层的 permission mode 与底层 sandbox settings 分开。

- Permission mode 包括 `default`、`plan`、`acceptEdits`、`bypassPermissions`、`dontAsk` 等。
- Sandbox settings 单独描述网络与文件系统：
  - 网络：allowed domains、Unix socket、local binding、HTTP/SOCKS proxy。
  - 文件系统：allow/deny read/write。
  - 运行时：sandbox 是否启用、不可用时是否 fail、是否允许 unsandboxed commands。
- 这些类型集中在 `src/entrypoints/sandboxTypes.ts` 与 `src/utils/permissions/PermissionMode.ts`。

Claude Code 的启发是：真正的 sandbox 配置会很快变成多维矩阵。Pulsara V1 不应急着承诺完整 sandbox，而应先建立稳定的权限词汇。

## OpenClaw

OpenClaw 是 host-first 的信任模型，但对远程入口、非主会话和多 agent 委托做了大量治理。

- 默认 `main` session 在宿主机运行，符合单用户可信操作员模型。
- 可配置 `agents.defaults.sandbox.mode: "non-main"` 或 `"all"`，让非主会话进入 sandbox。
- 默认 sandbox backend 是 Docker，也支持 SSH/OpenShell。
- Sandbox 配置包含 `mode: off | non-main | all`、backend、scope、workspaceAccess、tool allow/deny。
- Docker 默认偏严格：read-only root、network none、cap drop all、tmpfs/resource/seccomp/apparmor 等配置。
- 文档明确说 exec approvals 是操作员 guardrail，不是多租户安全边界。

OpenClaw 的启发是：本地本人使用可以 host-first；远程入口、群聊、子 agent、不可信会话才是必须收紧的边界。

## Hermes

Hermes 更偏“多执行环境 + dangerous command approval + 环境/路径硬化”。

- Terminal backend 包括 local、docker、ssh、modal、daytona、singularity。
- Docker backend 有 cap-drop、no-new-privileges、资源限制、bind mount、孤儿容器清理等硬化。
- `tools/approval.py` 是危险命令检测与 per-session approval 的中心。
- 它冻结 YOLO mode，避免运行时被 skill 或 prompt 注入修改环境变量绕过审批。
- 它区分 hardline blocklist 与普通 dangerous patterns：少数灾难动作永远阻断，其他高风险动作可经审批或 YOLO 放行。

Hermes 的启发是：即使接受高权限模式，也需要一条非常窄的灾难底线，例如整盘删除、块设备覆写、关机重启、修改 agent 自己的安全配置。

## Pulsara 现状

Pulsara 当前有 host-side guardrails，但还没有 OS/container sandbox。

已有能力：

- 文件工具继承 `WorkspaceTool`，路径会 resolve 并限制在 workspace root 内。
- `terminal` 的 `workdir` 被限制在 workspace 内，命令结束后如果 cwd 逃出 workspace，不更新 session cwd。
- `terminal` 有危险命令正则，命中时要求确认。
- shell 背景化 wrapper 会被引导到 terminal yield 模型。
- subprocess env 会做清洗，并支持 `.venv` overlay。
- terminal process 有 registry、yield、lifetime、shutdown/kill ownership 管理。

没有的能力：

- 没有 OS 级文件系统隔离。
- 没有容器/Seatbelt/Landlock/Windows restricted-token backend。
- 没有网络隔离或 domain allowlist。
- 没有正式的 permission profile。
- 没有把 approval policy 从 terminal dangerous-command 逻辑中抽象出来。
- 没有全局 inspect 输出告诉用户当前 session 的有效权限。
- terminal 在宿主机 shell 中运行，不能被描述成 workspace-bound sandbox。
- 当前代码没有 MCP 工具注册层；轻量权限 V1 若实施，应先覆盖 built-in tools。

## 对 Pulsara 的设计判断

V1 不应叫“真正沙箱”。更准确的名字是：

**Lightweight Permission System / Execution Policy**

它的目标不是阻止用户使用 full access，而是：

- 让 full access 成为可命名、可检查、可审计的一等模式。
- 让 read-only / guarded 模式能服务不可信 workspace、远程入口、自动任务、子 agent。
- 把 terminal 明确为宿主机能力，而不是假装能靠 Python 侧检查完成隔离。
- 保留少数灾难动作的硬底线。
- 为未来接入真实 sandbox backend 留出清晰接口。

## 推荐方向

Pulsara 先做三层抽象：

1. `PermissionProfile`
   - `trusted_host`
   - `workspace_guarded`
   - `read_only`

2. `ApprovalPolicy`
   - `never`
   - `risky_only`
   - `on_request`

3. `ExecutionBoundary`
   - V1: `host`
   - 未来可扩展：`macos_seatbelt`、`docker`、`landlock`

V1 的重点是 contract 和可观察性，不是先上重型隔离。等 contract 稳定后，再把 `ExecutionBoundary` 接到真正的 OS/container backend。
