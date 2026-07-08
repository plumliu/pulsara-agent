# Permission Policy Contract

_Created: 2026-06-25_

这份文档定义 Pulsara permission / approval 的长期契约。它不是 implementation plan，而是当前和后续实现必须遵守的硬协议：生产 runtime 权限必须用少数几个有名字、可解释、可测试的预设来表达，默认采用最激进的 **bypass-permissions（不审批）**。自由三轴组合只保留为组件级 / 显式低层测试能力，不得穿过 `AgentRuntime` / `HostSession` / `RunStartEvent` / inspector / resume / subagent snapshot 的生产 run contract 边界。

核心立场：

- **预设是主路径。** 四个命名预设覆盖真实产品意图，是 UI / CLI / 文档主推的入口。
- **默认是 bypass-permissions。** Pulsara 默认不审批，把自动化体验放在第一位。
- **自定义三轴不是生产 run feature。** `EffectivePermissionPolicy(custom)` 可以用于 `PolicyPermissionGate` 等组件测试，但 production run snapshot 必须是四个 preset 之一。
- **hardline terminal 命令在任何组合下都必须 DENY。** 这是独立于审批策略的硬地板，不可协商、不可绕过、不可通过自定义关闭。

相关代码：

- [src/pulsara_agent/runtime/permission.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/permission.py)
- [src/pulsara_agent/capability/exposure.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/exposure.py)
- [src/pulsara_agent/runtime/terminal_risk.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal_risk.py)
- [src/pulsara_agent/runtime/approval.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/approval.py)
- [src/pulsara_agent/host/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py)
- [tests/test_permission_policy.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_permission_policy.py)
- [tests/test_capability_surface.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_capability_surface.py)

---

## 1. 核心原则

- **能力是一条有序阶梯，不是 3×3×3 矩阵。** 四个预设从最严到最宽排成一条线，每往下一级只放开一类审批。
- **默认 bypass-permissions。** 不显式选预设时，系统按 bypass 运行。
- **hardline DENY 是硬地板。** 它横切所有预设和所有自定义组合，永远生效，且与 approval / terminal 轴正交。
- **预设制收敛构造面。** 主路径只暴露四个预设名；自由三轴组合只保留为组件级 / test-only 构造能力，不能进入生产 run/session contract。
- **`read_only` 与 terminal 互斥。** `read_only` profile 必须 `terminal=off`，这是构造期就要校验的硬约束。
- **审批是“是否打断”，不是“是否安全”。** approval 决定要不要等用户确认；它不能替代 hardline 地板提供的安全保证。

---

## 2. 四个预设

主路径只有四个命名预设：

| 预设 | profile | approval | terminal | 默认 |
| --- | --- | --- | --- | --- |
| `read-only` | `read_only` | `n/a` | `off` | |
| `ask-permissions` | `trusted_host` | `on_request` | `ask` | |
| `accept-edits` | `trusted_host` | `never` | `ask` | |
| `bypass-permissions` | `trusted_host` | `never` | `allow` | ✅ 默认 |

这四个预设里只用到两个 profile（`read_only` / `trusted_host`），两种**有效** approval（`on_request` / `never`），三种 terminal（`off` / `ask` / `allow`）。`workspace_guarded` 和 `risky_only` 不在任何预设里（见 §6）。

`read-only` 的 approval 标为 `n/a`：在 `read_only` profile 下，可变工具在 approval 判定之前就被拒，approval 轴没有任何可观察效果。该预设因此不对 approval 轴做承诺——底层构造对象可以携带任意 approval 值，但契约层面它是 inert 的。

---

## 3. 能力阶梯

四个预设构成一条单调放开的能力阶梯：

```text
read-only  ⊂  ask-permissions  ⊂  accept-edits  ⊂  bypass-permissions
```

- `read-only`：只能读 + 维护 agent 本地计划状态。**fail-closed allowlist**——只放行无写入、无终端执行、无 durable memory 写入副作用的工具（见 §3.1）。其中 `read_file` / `search_files` 是 host-local ordinary text read，不是 workspace-only read。其余（文件写 / terminal / durable memory 写 / 未来任何副作用工具）一律拒绝。
- `ask-permissions`：在 read-only 基础上，放开“写 + terminal”，但每次都要用户确认。
- `accept-edits`：在 ask-permissions 基础上，文件写自动通过；terminal 仍要确认。
- `bypass-permissions`：在 accept-edits 基础上，terminal 也自动通过。除 hardline 外什么都不问。

阶梯模型是这份契约对外解释权限的首选心智模型，优先于“三轴矩阵”叙述。

### 3.1 read-only 的含义与 `is_read_only` 字段

read-only 不是“完全只读、什么都不能动”，而是：**模型可以读取上下文、读取本机普通文本文件、检索记忆、整理/维护本轮或会话内的工作状态，但不能修改用户工作区、启动或控制终端执行、写 durable memory，或产生任何写入/执行类外部副作用。**

强制方式是 **fail-closed allowlist**：read-only profile 下，gate 只放行 `READ_ONLY_ALLOWED_TOOL_NAMES` 里的工具，其余一律 DENY。这与 denylist 相反——新增的副作用工具默认被 read-only 拦住，无需逐个登记到黑名单。动态 provider 工具（例如 MCP）即便 descriptor 声明 `is_read_only=True`，V1 也不会自动继承 read-only allowlist；必须作为单独产品决策显式加入。

内置工具是否有资格进入 allowlist 的依据是工具/descriptor 的 **`is_read_only` 字段**，其语义定义为：

> `is_read_only=True` 表示该工具**不修改用户工作区、外部系统、终端进程或 durable memory**；允许修改 agent-local ephemeral state（如内存里的 todo 列表）。

`read_file` / `search_files` 的 read-only 语义是 **host-local ordinary text read**：相对路径从 workspace root 解析，绝对路径与 `~` 可读取本机普通文本文件。它们不是 workspace-only 工具。`search_files` 在 workspace 外必须拒绝 broad recursive roots（如 `~`、`/`、`/Users`、`/tmp` 根），避免误扫大范围本机目录。

据此：

- **允许**（`is_read_only=True`）：`read_file` / `search_files` / `artifact_read` / memory 读工具（`memory_search` / `memory_get` / `memory_explain`）/ `todo`。
- **拒绝**（`is_read_only=False`）：`write_file` / `edit_file`（工作区写）、`terminal` / `terminal_process`（终端执行/控制）、`remember_*`（durable memory 写）、以及未来任何副作用工具。

`READ_ONLY_ALLOWED_TOOL_NAMES` 常量必须与内置工具中 `is_read_only=True` 且被产品明确纳入 read-only 的集合严格一致，由防漂移测试守护（见 §10）。它不是 dynamic provider capability 的自动并集。

> **已知后续精化**：`terminal_process` 的只读 action（`list` / `log` / `poll` / `wait`）本质是观察已有进程、无副作用。严格 read-only 语义下它们本应放行，但这需要 action-level classifier，且要改本节"终端类整体不可用"的表述。本版**不开此口**：read-only 下 `terminal_process` 整体被拦。该豁免将作为独立精化放进 read-only（plan mode 据此继承），不是 plan mode 专属 grant。

---

## 4. 每个预设的精确 gate 行为

下表描述每个预设下，gate 对不同工具类别的判定（`ALLOW` / `WAIT`=等待用户确认 / `DENY`）。所有预设的 hardline terminal 命令一律 `DENY`，不在表内重复。

| 工具类别 | read-only | ask-permissions | accept-edits | bypass-permissions |
| --- | --- | --- | --- | --- |
| read 工具（read_file / search_files / artifact_read） | ALLOW | ALLOW | ALLOW | ALLOW |
| memory 读工具（memory_search/get/explain） | ALLOW | ALLOW | ALLOW | ALLOW |
| todo（agent-local 计划状态） | ALLOW | ALLOW | ALLOW | ALLOW |
| file write（edit_file / write_file） | DENY | WAIT | ALLOW | ALLOW |
| memory 写工具（remember_*，durable memory） | DENY | ALLOW | ALLOW | ALLOW |
| terminal（普通命令） | DENY | WAIT | WAIT | ALLOW |
| terminal_process 只读 action（list/log/poll/wait） | DENY | ALLOW | ALLOW | ALLOW |
| terminal_process 写 action（write/submit 等） | DENY | WAIT | WAIT | ALLOW |
| terminal hardline 命令 | DENY | DENY | DENY | DENY |
| 未在 allowlist 的未来副作用工具 | DENY | ALLOW | ALLOW | ALLOW |

行为要点：

- `read-only` 的 approval 轴是 `n/a`：read-only 是 fail-closed allowlist（见 §3.1），可变工具在 `is_tool_allowed_by_policy` 阶段就被拒（DENY），根本到不了 approval 判定。该预设不对 approval 轴做承诺，阶梯连续性由 profile 轴保证，而不是靠给它填一个无效的 approval 值。表中 read-only 列的 DENY 包含 `remember_*`（durable memory 写）及任何不在 allowlist 的工具，不只是 file write / terminal。
- `terminal_process` 的只读 action 豁免（list/log/poll/wait）独立于 approval：只要 profile 允许 terminal 工具存在，它们就不触发审批。这是已实现且要长期保住的行为。
- `accept-edits` 与 `ask-permissions` 的唯一差别是 file write：前者 `never` 直接放行，后者 `on_request` 等待。
- `bypass-permissions` 与 `accept-edits` 的唯一差别是 terminal：前者 `allow` 直接放行，后者 `ask` 等待。

---

## 5. Hardline 硬地板

hardline 是这份契约里唯一不可协商的部分。

**适用范围（本版）**：hardline 地板**只锁定两个终端工具**——`terminal` 和 `terminal_process`。其他工具类（file write 等）是否需要类似的"无论如何都不执行"硬地板，留待后续单独商议，不在本版契约承诺内。

- `is_hardline_terminal_command()` 判定为 true 的 `terminal` 命令，**在任何预设、任何自定义组合下都返回 DENY**。
- `terminal_process` 的写入数据若命中 hardline，同样 DENY。
- hardline 判定**先于** profile / approval / terminal 任何一轴。它不是"高风险所以要审批"，而是"无论如何都不执行"。
- 没有任何配置开关、预设、env var 或自定义三轴组合可以把 hardline 降级成 WAIT 或 ALLOW。
- bypass-permissions 的语义是"不审批"，**不是"无防护"**。hardline 地板在 bypass 下依然全额生效。

这条规则是产品安全底线，必须由独立测试守住（见 §9），且任何新增 terminal 执行入口都要复用同一个 hardline 判定，不允许绕过。

---

## 6. 已从主路径移除的轴值

下列轴值不再属于任何预设，仅可通过 §7 的自定义 feature 显式构造：

- **`approval=risky_only`**：四个预设都不用它。它依赖 `is_risky_terminal_command` / `is_sensitive_terminal_command` 做命令级风险分级。这套分级在预设主路径里不再被触发；它在 gate 里只剩自定义路径会用到。
- **`profile=workspace_guarded`**：它在 gate 判定里与 `trusted_host` 行为相同（差异只在旧的 `_profile_default` 默认值）。预设制下，可变能力统一用 `trusted_host` 表达，`read_only` 表达只读。

移除的含义是“移出生产 runtime / HostSession / CLI 主路径”，不是“从组件级代码词表里删除”。它们仍可用于低层组件测试，但不再有用户可见生产入口、不再出现在推荐文档里。

> 注意默认行为变化：此前 `project` workspace 默认走 `risky_only`（危险命令会问）。改为 bypass 默认后，project 不再对任何命令审批（hardline 除外）。这是真实行为变更，必须在 changelog 标明。

---

## 7. 自定义三轴 component/test-only 构造能力

自定义三轴不是生产 run feature。它只保留为组件级 / test-only 构造能力，用于直接测试 `PolicyPermissionGate`、terminal/tool 低层行为、以及 `EffectivePermissionPolicy` parser/validator。

合法轴值词表：

- `profile`：`read_only` | `workspace_guarded` | `trusted_host`
- `approval`：`never` | `risky_only` | `on_request`
- `terminal`：`off` | `allow` | `ask`

构造期校验（硬约束，任何路径都适用）：

- `read_only` profile 必须 `terminal=off`，否则构造失败。
- 任何自定义组合产出的 `EffectivePermissionPolicy` 仍然受 §5 hardline 地板约束。

定位：

- 这是低层测试/组件构造能力，不是用户可见产品入口，也不是后向兼容垫片。
- 主路径文档、CLI 帮助、默认值只呈现四个预设；生产 host run / repl / inspect 入口必须拒绝 raw 三轴 flags/env。
- 现有依赖裸三轴的测试可以继续存在，但它们守护的是“组件级构造和 hardline 地板仍正确”，不是生产 run contract。

---

## 8. 构造边界

权限对象只能从两个入口产生：

1. **预设入口（主路径）**：预设名 → 四个 blessed 三元组之一。
2. **自定义入口（组件级 / test-only）**：显式三轴 → `resolve_permission_policy()` → 构造期校验。

边界规则：

- 任何 public / CLI / config / env 入口都必须接受并持久化预设名；生产 host run / repl / inspect 不接受 raw 三轴 flags/env。
- 自定义三轴入口只能用于组件级 / test-only 路径，不能进入 `AgentRuntime` / `HostSession` / durable event log / inspector / resume / subagent snapshot 的生产 run contract。
- 不允许在预设入口之外再定义第二套“默认值推断”逻辑：默认就是 bypass-permissions，不再按 workspace_kind 隐式分叉出 risky_only 之类的旧默认。
- 不允许任何入口产出绕过 §5 hardline 地板或 §7 构造校验的 policy。

---

## 8.5 强制模型：exposure 先行，permission gate 是权限唯一权威

工具调用有两层强制：

1. **Capability exposure access**：descriptor missing / hidden / unavailable / not-callable 的工具 fail-closed，且只拒绝对应 call。
2. **Permission gate**：对已经进入本 turn `CapabilityExposurePlan.callable_names` 的工具，根据本契约判定 ALLOW / WAIT / DENY。

权限契约只管理第二层。

- **CapabilityExposurePlan 是 tools array 的上游事实。** Hidden / unavailable / deferred / missing-binding capability 不进入模型 tools；模型幻觉调用时由 exposure access 拒绝。
- **Permission mode 不过滤 tools array。** 对于同一份 `CapabilityExposurePlan`，read-only / ask / accept-edits / bypass 看到的 tools 集合相同。
- **不可用权限 = 调用时被 permission gate DENY**（visible-but-blocked），不是“按 mode 隐藏工具”。read-only 下的 write/terminal 工具若在 exposure 中 direct callable，仍出现在工具清单里，但调用时被 DENY。
- **tools 数组跨所有 permission mode 恒定。** 这是契约的一部分：切换 mode 不改变请求前缀，prompt 前缀缓存保持稳定。

## 8.6 Session default + Run permission snapshot

permission mode 有两层身份：

- **HostSession stored default**：用户 / host 在 run 之间设置的默认 preset，用于未来 non-plan run。
- **Run permission snapshot**：每个 `RunStartEvent` 固化的不可变 run contract，是该 run 内 capability gate、permission gate、terminal、MCP resume、approval resume、subagent spawn 的唯一权限事实源。

- **谁能切**：仅用户 / host。Agent **没有**自切 mode 的工具，杜绝提权。
- **何时生效**：只影响之后的新 run。运行中（run lock 持有）、有 pending approval、active plan、或正在 stopping 时，切换被拒绝（抛 `HostSessionBusyError` / `HostSessionPendingApprovalError`），stored default 不变。
- **怎么生效**：切换只更新 HostSession stored default；已经启动或 suspended 的 run 继续读自己的 `RunStartEvent` snapshot。**不重建** gate / executor / registry / 终端会话。
- **不丢状态**：切换**不影响** live 终端进程、event log、artifact——它们挂在 `RuntimeSession`，不随切换重建。
- **不变量保持**：每个新 run 的 `permission_policy` 必须等于 `preset_to_policy(permission_mode).to_dict()`；§5 hardline 地板照旧全额生效。
- **入口**：`HostSession.set_permission_mode(mode)` / `HostCore.set_permission_mode(...)` / CLI REPL `:mode <preset>`；`:status` 与 `host inspect` 展示 stored default 与 effective next-run mode。

> **plan mode 不在本权限轴内。** plan mode 是独立 workflow 子系统：active plan 下的新 run 强制使用 `read-only` snapshot（source=`plan_mode`），但 HostSession stored default 保持不变，直到 exit_plan approve/cancel/force-exit 后恢复为 future run default。它**不**是 `PermissionMode` 的成员，权限轴始终只有四个预设。

---

## 9. 禁止事项

- 不允许任何预设或自定义组合把 hardline terminal 命令降级成 WAIT 或 ALLOW。
- 不允许新增 terminal / terminal_process 执行入口绕过 hardline 判定。
- 不允许把自定义三轴 component/test-only 能力描述成“用户可见生产 feature”“默认推荐”或“主路径”。
- 不允许在预设之外再引入隐式默认值推断（如按 workspace_kind 自动选 approval）。
- 不允许构造 `read_only + terminal≠off` 的组合。
- 不允许把 bypass-permissions 解释成“无防护”——它只是“不审批”，hardline 地板仍在。
- 不允许为了新增权限组合而扩展预设数量，除非该组合对应一个真实、命名的产品意图。
- 不允许按 mode 过滤工具注册表 / 向模型隐藏工具——强制必须走 gate（visible-but-blocked，见 §8.5）。
- 不允许给 Agent 任何自切 permission mode 的工具（仅用户/host 可切，见 §8.6）。
- 不允许在 mode 切换时重建 gate/executor/registry/终端会话而丢失 live 终端进程或 event log。
- 不允许把 read-only 退回 denylist（“只拦某几类、其余放行”）——read-only 必须是 fail-closed allowlist，未来副作用工具默认被拦（见 §3.1）。
- 不允许把有工作区写入 / 外部系统写入 / 终端执行或控制 / durable memory 写入副作用的工具加进 `READ_ONLY_ALLOWED_TOOL_NAMES`，或为绕过 allowlist 把这类工具标成 `is_read_only=True`。host-local ordinary text read 是本契约显式允许的 read-only 能力，不属于这条禁止项。

---

## 10. 测试守护

这份契约由以下测试守住：

- 四个预设各自解析出 §2 表中的精确配置（read-only 断言可变工具被拒，不对 approval 值做断言）。
- 默认（不选预设）解析为 bypass-permissions。
- read-only 拒绝 file write 和 terminal，允许 read 工具；其中 filesystem read 工具允许 host-local ordinary text read，且该行为与底层 approval 值无关。
- **read-only 是 fail-closed allowlist：拒绝 `remember_*`（durable memory 写）与任何不在 `READ_ONLY_ALLOWED_TOOL_NAMES` 的工具；放行 read 工具 / memory 读工具 / `todo`。**
- **`READ_ONLY_ALLOWED_TOOL_NAMES` 与内置工具中被产品明确纳入 read-only 的 `is_read_only=True` 集合严格一致（防漂移测试）；MCP 等动态 provider 工具不自动加入。**
- **`todo.is_read_only` 为 True，但 `is_concurrency_safe` 仍为 False（语义重定义不改并发行为）。**
- ask-permissions 对 write 和 terminal 都返回 WAIT，对 read 和 terminal_process 只读 action 返回 ALLOW。
- accept-edits 对 write 返回 ALLOW、对 terminal 返回 WAIT。
- bypass-permissions 对 write 和普通 terminal 都返回 ALLOW。
- **hardline terminal 命令在四个预设下都返回 DENY。**
- **hardline terminal 命令在自定义 `bypass`-等价组合（never/allow）下仍返回 DENY。**
- **hardline 同时覆盖 `terminal` 命令和 `terminal_process` 写入数据两条入口。**
- terminal_process 只读 action 在 ask/accept/bypass 下不触发审批。
- 组件级 / test-only 自定义三轴能构造 `risky_only` / `workspace_guarded` 组合，且这些组合仍受 hardline 地板约束。
- `read_only + terminal≠off` 构造期被拒。
- 自定义入口是组件级 / test-only，不会被默认路径、CLI、manifest、HostSession 或 AgentRuntime 触发。
- **所有工具跨所有 mode 全量注册、可见；read-only 下 write/terminal 仍在工具清单里，由 gate 在调用时 DENY（visible-but-blocked）。**
- **tools 数组在 read-only / ask / accept-edits / bypass 下集合完全相同（前缀缓存稳定）。**
- **`RunStartEvent` 必须记录 non-null preset `permission_snapshot_id` / `permission_mode` / `permission_policy` / `permission_snapshot_source`。**
- **`RunStartEvent.permission_policy == preset_to_policy(permission_mode).to_dict()`；缺失、自定义或 mode/policy 不一致均为 contract error。**
- **Plan workflow 的 previous/restored permission facts 也必须是 preset mode + preset policy 展开。**
- **`set_permission_mode` 在轮边界成功切换 stored default，切后新 run gate 行为随新 snapshot 变化。**
- **运行中 / pending approval / stopping 时 `set_permission_mode` 被拒，mode 不变。**
- **切换 stored default 后，已有 run / approval resume / MCP resume / child subagent 不改旧 snapshot；live 终端进程与 event log 不丢（零重建）。**
- **切到 bypass 后 hardline 仍 DENY。**

---

## 11. 完成标准

只要这份契约成立，permission 的行为就应该可以用一句话解释：

- **默认 bypass：** Pulsara 默认不审批，把自动化放在第一位。
- **四级阶梯：** 需要更严时，从 bypass 往上选 accept-edits / ask-permissions / read-only，每级只多问一类。
- **自定义仅限组件/测试：** 预设不够用不是 V1 生产 feature；用户可见入口仍只能选择四个 preset。
- **hardline 不可破：** 无论哪种组合，hardline terminal 命令永远被拒——这是不依赖审批的安全地板。
