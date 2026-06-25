# Permission Policy Contract

_Created: 2026-06-25_

这份文档定义 Pulsara permission / approval 的长期契约。它不是 implementation plan，而是当前和后续实现必须遵守的硬协议：权限必须用少数几个有名字、可解释、可测试的预设来表达，默认采用最激进的 **bypass-permissions（不审批）**，同时把自由三轴组合保留为一个显式的高级 feature，而不是默认产品路径。

核心立场：

- **预设是主路径。** 四个命名预设覆盖真实产品意图，是 UI / CLI / 文档主推的入口。
- **默认是 bypass-permissions。** Pulsara 默认不审批，把自动化体验放在第一位。
- **自定义三轴是 feature，不是后向兼容。** 用户可以显式构造任意合法三轴组合，但这是高级能力，不是默认推荐路径。
- **hardline terminal 命令在任何组合下都必须 DENY。** 这是独立于审批策略的硬地板，不可协商、不可绕过、不可通过自定义关闭。

相关代码：

- [src/pulsara_agent/runtime/permission.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/permission.py)
- [src/pulsara_agent/runtime/terminal_risk.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal_risk.py)
- [src/pulsara_agent/runtime/approval.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/approval.py)
- [src/pulsara_agent/host/session.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py)
- [tests/test_permission.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_permission.py)

---

## 1. 核心原则

- **能力是一条有序阶梯，不是 3×3×3 矩阵。** 四个预设从最严到最宽排成一条线，每往下一级只放开一类审批。
- **默认 bypass-permissions。** 不显式选预设时，系统按 bypass 运行。
- **hardline DENY 是硬地板。** 它横切所有预设和所有自定义组合，永远生效，且与 approval / terminal 轴正交。
- **预设制收敛构造面。** 主路径只暴露四个预设名；自由三轴组合是显式 opt-in 的高级 feature。
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

- `read-only`：只能读。文件写 / terminal 工具直接拒绝。
- `ask-permissions`：在 read-only 基础上，放开“写 + terminal”，但每次都要用户确认。
- `accept-edits`：在 ask-permissions 基础上，文件写自动通过；terminal 仍要确认。
- `bypass-permissions`：在 accept-edits 基础上，terminal 也自动通过。除 hardline 外什么都不问。

阶梯模型是这份契约对外解释权限的首选心智模型，优先于“三轴矩阵”叙述。

---

## 4. 每个预设的精确 gate 行为

下表描述每个预设下，gate 对不同工具类别的判定（`ALLOW` / `WAIT`=等待用户确认 / `DENY`）。所有预设的 hardline terminal 命令一律 `DENY`，不在表内重复。

| 工具类别 | read-only | ask-permissions | accept-edits | bypass-permissions |
| --- | --- | --- | --- | --- |
| read 工具（read_file / search_files 等） | ALLOW | ALLOW | ALLOW | ALLOW |
| file write（edit_file / write_file） | DENY | WAIT | ALLOW | ALLOW |
| terminal（普通命令） | DENY | WAIT | WAIT | ALLOW |
| terminal_process 只读 action（list/log/poll/wait） | DENY | ALLOW | ALLOW | ALLOW |
| terminal_process 写 action（write/submit 等） | DENY | WAIT | WAIT | ALLOW |
| terminal hardline 命令 | DENY | DENY | DENY | DENY |

行为要点：

- `read-only` 的 approval 轴是 `n/a`：可变工具在 `is_tool_allowed_by_policy` 阶段就被拒，根本到不了 approval 判定。该预设不对 approval 轴做承诺，阶梯连续性由 profile 轴保证，而不是靠给它填一个无效的 approval 值。
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

移除的含义是“移出主推路径”，不是“从代码词表里删除”。它们仍是合法的自定义轴值，但不再有默认入口、不再出现在推荐文档里。

> 注意默认行为变化：此前 `project` workspace 默认走 `risky_only`（危险命令会问）。改为 bypass 默认后，project 不再对任何命令审批（hardline 除外）。这是真实行为变更，必须在 changelog 标明。

---

## 7. 自定义三轴 feature

自定义三轴是一个显式的高级 feature，让用户在四个预设之外构造自己的权限组合。

合法轴值词表：

- `profile`：`read_only` | `workspace_guarded` | `trusted_host`
- `approval`：`never` | `risky_only` | `on_request`
- `terminal`：`off` | `allow` | `ask`

构造期校验（硬约束，任何路径都适用）：

- `read_only` profile 必须 `terminal=off`，否则构造失败。
- 任何自定义组合产出的 `EffectivePermissionPolicy` 仍然受 §5 hardline 地板约束。

定位：

- 这是 feature，不是后向兼容垫片。它的存在理由是“让用户表达预设没覆盖的权限意图”，而不是“为了不破坏旧调用”。
- 主路径文档、CLI 帮助、默认值都应优先呈现四个预设；自定义三轴作为进阶选项呈现。
- 现有依赖裸三轴的测试可以继续通过这个 feature 入口存在，但它们守护的是“自定义 feature 仍然工作”，不是“预设可以被随意绕过”。

---

## 8. 构造边界

权限对象只能从两个入口产生：

1. **预设入口（主路径）**：预设名 → 四个 blessed 三元组之一。
2. **自定义入口（feature）**：显式三轴 → `resolve_permission_policy()` → 构造期校验。

边界规则：

- 任何 public / CLI / config / env 入口都应优先接受预设名。
- 自定义三轴入口必须是显式 opt-in，不能是默认或隐式路径。
- 不允许在预设入口之外再定义第二套“默认值推断”逻辑：默认就是 bypass-permissions，不再按 workspace_kind 隐式分叉出 risky_only 之类的旧默认。
- 不允许任何入口产出绕过 §5 hardline 地板或 §7 构造校验的 policy。

---

## 9. 禁止事项

- 不允许任何预设或自定义组合把 hardline terminal 命令降级成 WAIT 或 ALLOW。
- 不允许新增 terminal / terminal_process 执行入口绕过 hardline 判定。
- 不允许把自定义三轴 feature 描述成“默认推荐”或“主路径”。
- 不允许在预设之外再引入隐式默认值推断（如按 workspace_kind 自动选 approval）。
- 不允许构造 `read_only + terminal≠off` 的组合。
- 不允许把 bypass-permissions 解释成“无防护”——它只是“不审批”，hardline 地板仍在。
- 不允许为了新增权限组合而扩展预设数量，除非该组合对应一个真实、命名的产品意图。

---

## 10. 测试守护

这份契约由以下测试守住：

- 四个预设各自解析出 §2 表中的精确配置（read-only 断言可变工具被拒，不对 approval 值做断言）。
- 默认（不选预设）解析为 bypass-permissions。
- read-only 拒绝 file write 和 terminal，允许 read 工具，且该行为与底层 approval 值无关。
- ask-permissions 对 write 和 terminal 都返回 WAIT，对 read 和 terminal_process 只读 action 返回 ALLOW。
- accept-edits 对 write 返回 ALLOW、对 terminal 返回 WAIT。
- bypass-permissions 对 write 和普通 terminal 都返回 ALLOW。
- **hardline terminal 命令在四个预设下都返回 DENY。**
- **hardline terminal 命令在自定义 `bypass`-等价组合（never/allow）下仍返回 DENY。**
- **hardline 同时覆盖 `terminal` 命令和 `terminal_process` 写入数据两条入口。**
- terminal_process 只读 action 在 ask/accept/bypass 下不触发审批。
- 自定义三轴 feature 能构造 `risky_only` / `workspace_guarded` 组合，且这些组合仍受 hardline 地板约束。
- `read_only + terminal≠off` 构造期被拒。
- 自定义入口是显式 opt-in，不会被默认路径触发。

---

## 11. 完成标准

只要这份契约成立，permission 的行为就应该可以用一句话解释：

- **默认 bypass：** Pulsara 默认不审批，把自动化放在第一位。
- **四级阶梯：** 需要更严时，从 bypass 往上选 accept-edits / ask-permissions / read-only，每级只多问一类。
- **自定义可选：** 预设不够用时，用户可以显式构造三轴组合作为高级 feature。
- **hardline 不可破：** 无论哪种组合，hardline terminal 命令永远被拒——这是不依赖审批的安全地板。
