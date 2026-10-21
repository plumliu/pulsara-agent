# Pulsara MCP 工具演进与增量能力目录实施规格

日期：2026-10-21。

状态：用户已于 2026-10-21 确认全文并授权完整实施。生产实现已完成，最终树 664 项回归通过，Ruff 与差异格式检查通过；同一个 GPT-6 Astra high 子代理已完成代码交叉复查，无剩余阻塞发现。保留生产配置与会话；未进行真实模型实测，未触发 Actions。

## 1. 目标、裁决与适用范围

同一 provider continuity epoch 内，MCP 服务端更新不应因为描述文字或明确兼容的参数变化而中断原有能力；新增工具和不兼容的新版本应通过既有 inspect → meta 路径使用。全过程保持 `SYSTEM`、provider `tools` 字节不变，`messages` 只能追加后缀。

本规格已由用户确认，替代 Round 9 中“同一个原生 identity 一旦发生 schema/description replacement，整个 epoch 内禁止新版 meta 调用”的条款。其余权限、SDK、调用结算、完整 inspect、前缀连续性和物理资源边界继续有效。

### 1.1 已达成的产品共识

1. 工具级路由，不给整个 MCP server 永久贴上 native/meta 标签；同一 server 可以同时存在两种工具。
2. 原生定义冻结，不把新参数、新工具或新说明写回既有 `tools` 数组。
3. 纯描述变化、可确认的兼容变化继续使用旧原生契约；不通过 prompt diff 提前开放新参数或新取值。
4. 不兼容更新关闭旧原生入口，最新版通过 inspect → meta 调用。禁止自动把旧 native 参数转换并转发到 meta。
5. 已经是 meta 的工具，定义再次变化不主动推送 schema/description diff。模型先 inspect 获得完整定义及引用，引用有效时可重复调用；失效时在执行前拒绝并要求重新 inspect。
6. 新增、移除、可用性和调用路径变化需要可发现；正常原生工具不在 catalog 中重复枚举。
7. catalog 展示“当前原生定义之外的能力及原生例外”，并按实际模型可见内容变化追加；不是每轮全量重复，也不是逐事件的变更日志。
8. 不在此轮改动 MCP 原生准入阈值，不增加任意历史、更新次数、调用次数或 epoch 生命周期上限。

### 1.2 为实施补齐的裁决

以下补充决定已随用户对全文的确认一并冻结：

| 补充决定 | 目的 |
|---|---|
| 一个已安装的 native 工具一旦因不兼容或无法证明兼容而转 meta，本 epoch 不自动恢复 native | 不因服务回滚、反复部署而来回切换模型调用路径 |
| 普通断连、授权失效和临时目录不可读不触发上述永久降级 | 避免把可恢复的连接问题误当接口升级 |
| 兼容性只支持明确列出的有限变换；复杂且无法确认的变化转 meta，而不是整项永久停用 | 不自建通用 JSON Schema 包含关系求解器 |
| 第一版对 output schema 的结构变化转 meta；纯文案变化可继续 native | 让新版返回契约经 inspect 可见，不凭输入兼容推断输出兼容 |
| meta ref 保留现有 exact-version、scope、epoch 和 FULL-result 采用规则 | 减少本轮所有权变化；不为了少一次 inspect 放宽引用真实性 |
| 回收已失效且不再被在途操作使用的 meta ref，不把历次版本累积当成 live 容量 | 防止长程任务因反复 inspect 耗尽历史配额 |

### 1.3 不做的事

- 不增加自动冷重启、自动压缩、动态重写工具数组、隐藏的 provider prefix rebase。
- 不增加同工具的 version 后缀别名、旧/新两套有效执行入口或通用 schema diff 调用协议。
- 不根据自然语言描述自动推断语义兼容、计费变化或权限变化。
- 不把 catalog 更新当用户请求，不唤醒空闲模型，不自动执行新增工具。
- 不重做 MCP transport、OAuth、JSON Schema validator、分页协议或 scheduler。
- 不增加表、持久事件、live event kind、subject slot、append guard、durable job、投递回执或独立变更账本。

## 2. 当前实现核对与复用边界

### 2.1 实施前代码事实与改动边界

| 所在位置 | 当前行为 | 本轮变化 |
|---|---|---|
| `capability/planner.py` | 已安装 native 按完整 semantic fingerprint 比较；不相等就 `SCHEMA_REPLACED`，排除同 identity meta | 分离冻结原生定义、兼容判断与当前执行路由 |
| `capability/contracts.py`、`conversation_kernel/mcp/supervisor.py` | description、input/output schema、scope 等共同进入工具语义身份 | 不把完整版本相等当成 native 兼容的唯一判据；保留真实边界所需身份 |
| `conversation_kernel/tool_runtime.py` | native prepared binding 要求 descriptor/version exact join | 增加唯一显式的兼容绑定适配；不得直接删除 exact join 后接任意 executor |
| `conversation_kernel/context_sources.py` | catalog 列出 direct/new/unavailable 工具；当前版本身份也参与 source identity | 变为有界的 meta 发现目录与原生例外；普通定义变化不触发相同目录重复追加 |
| `model_input/compiler.py` | `MCP_CATALOG` 已有 `SNAPSHOT_ON_CHANGE` 及 FULL/COMPACT/REF_ONLY | 复用 source、预算和 suffix 安装，补明确的清空语义 |
| `conversation_kernel/capability_query.py` | 已有按 server 的工具枚举和 server inspect | 作为完整分页发现入口，不再复制一套工具清单服务 |
| `conversation_kernel/mcp/meta.py` | HMAC ref，PREPARED → DORMANT → CALLABLE；绑定 scope/epoch/version/policy/route | 保留完整采用流程，补失效 ref 退休及回收 |
| `conversation_kernel/mcp/schema.py` | 使用 jsonschema/referencing，禁止外部 schema 引用检索 | 继续负责参数验证及引用解析 |
| `conversation_kernel/mcp/sdk_facade.py`、`conversation_kernel/mcp/supervisor.py` | 已有 list_changed、发现、连接 generation、dispatch permit、权限与结算 | 继续拥有刷新和物理调用，不新增并行协议实现 |

上表中的路径均相对于 `src/pulsara_agent/`。下文的 DIRECT/META/BLOCKED 是产品概念；实施时统一调整现有类型和消费者，不能再维护一套影子路由。

### 2.2 依赖与 Pulsara 的职责

- MCP SDK 拥有协议请求、通知、`tools/list`/`tools/call`、输出校验及 transport 生命周期；Pulsara facade 仅承接现有产品边界。
- jsonschema/referencing 拥有 dialect、引用、schema admission 和实例校验。兼容分类只做有限、可验证的结构变换识别；不复制 schema 解释器。
- Pulsara planner 拥有冻结原生集合和工具路由，runtime 拥有当前 executor 与权限绑定，compiler/continuity 拥有模型输入采用，meta owner 拥有引用真实性与生命周期。
- 已检查本仓库 `.venv` 中 SDK 的 `ClientSession.call_tool`、`validate_tool_result`、工具清单缓存更新入口。SDK 不提供“Pulsara 冻结工具数组下的兼容路由”产品策略，这一小段适配属于 Pulsara。
- SDK 能解释的扩展也可能有执行意义。例如本地 SDK 支持 `x-mcp-header`；不能把所有非标准 schema 字段都当可忽略文案。

## 3. 术语与状态模型

### 3.1 四个互不替代的事实

| 事实 | 含义 | Owner |
|---|---|---|
| 原生基线 B | 当前 epoch 已安装的原生工具 identity、完整 spec 和 provider wire projection | continuity / exposure plan |
| 当前定义 C | 已成功观察、校验和采用的服务端工具定义 | MCP supervisor/runtime |
| 调用路由 R | 当前工具应由 DIRECT、META 或 BLOCKED 路径处理 | planner / runtime |
| 调用许可 P | 本次 scope、权限、用户批准、连接和资源条件是否允许执行 | 现有 permission / MCP admission owners |

`R=META` 不意味着 `P=ALLOW`。catalog 描述 R 和可发现状态，不签发 P。工具在 B 中可见，也不意味着当前可执行。

### 3.2 工具身份

- 使用现有已解析 runtime server identity + remote tool identity；模型复制枚举给出的完整 target，不猜 provider 名称编码。
- 不根据相同显示名称合并不同安装来源、不同 server、不同会话或 Root/子代理作用域。
- 工具改名按“旧 identity 消失、新 identity 出现”处理，不制造自动别名。
- server ID 不变但 endpoint、凭据、来源或 scope 变化，先经过现有配置/权限 owner 的采用，不因工具名相同就继承旧授权。

### 3.3 同 epoch 的路由基线

1. B 中不存在的工具，在本 epoch 一律不晋升 native；可用时走 META。
2. B 中存在且未降级的工具，C 与 B 明确兼容时走 DIRECT；实际调用仍按当前执行约束校验。
3. B 中存在、已观察到不兼容或无法确认兼容的有效 C，关闭旧原生入口，采用 META；同 epoch 锁定这一选择。
4. C 缺失、未完整观察、未通过 schema admission 或超出当前 scope 时，按现有不可用规则处理；需要 META 而不可完整 inspect 时 BLOCKED。兼容的 DIRECT 调用本身不要求重新 inspect，不能因新版说明太长而额外停用。
5. 暂时 BLOCKED 不抹去原来的路由选择。恢复后，未降级工具可回 DIRECT，已降级工具只回 META。
6. 降级事实只由既有 epoch 的 typed plan/owner 持有，不建持久账本；新 cold epoch 或已采用 compaction successor 重新规划。
7. 未采用的候选发现结果不算已完成降级；已采用的降级不能因后续 refresh、provider request 失败或源回滚丢失。

这里的 epoch 路由/ref 采用以 continuity 成功安装 typed tool-exposure plan 为界。MCP safe point 可以先安装新的物理 runtime，供候选计划冻结一致的 executor/source 视图；这不等于该 epoch 已采用降级。若编译、preflight 或 continuity 安装前失败，该候选不改变已安装的降级事实；此后服务回滚 B 可以继续 DIRECT。若计划已安装而 provider 发送失败，META 锁定仍保留。物理 lease/fence/admission 始终独立检查，不因为候选未发布就允许旧 ref 穿透当前 executor 的版本/权限边界。

## 4. 原生准入与预算矩阵

保留当前规则：原生兼容的 MCP 候选数量 `<= 128`，这些工具的 provider wire 启发式 token 合计 `< 64,000`；内置工具不计入这两个 MCP 阈值。候选集合仍须满足现有原生 canonical/wire 字节边界。原生候选整体采用或整体转 meta，不新增“前 128 个”或按热度挑选策略。

| ID | 场景 | 决策 |
|---|---|---|
| B01 | cold 时 128 个 MCP、63,999 token，其他准入通过 | 候选整体 native |
| B02 | cold 时 129 个 MCP，或 token 恰为 64,000 | 候选整体 meta；提供初始发现目录 |
| B03 | 少量工具但单项 schema 不适合 provider 原生格式 | 该项按既有完整 inspect 条件走 meta；其他项按当前候选规则处理 |
| B04 | 内置工具自身超出既有物理边界 | 返回现有规划错误；不能把内置工具偷偷移入 MCP meta |
| B05 | epoch 中新加一个工具，令总数或总 token 超阈值 | 旧 native 集合不变；新项 meta，不能把旧集合整体挪走 |
| B06 | epoch 中删除很多工具，令总量降回阈值以下 | 原有 meta 不晋升 native，等合法 cold 边界 |
| B07 | native 描述变长，使当前服务定义总量超阈值 | 冻结 wire 未变，不重新做 native 容量选择；执行兼容另判 |
| B08 | catalog 很大或 inspect schema 很大 | 走各自既有目录预算/完整 inspect 边界；不是增加全局工具总数上限 |
| B09 | 一个 server 全 native，后来新项/更新项转 meta | 允许同 server 混合路由 |

## 5. Native 定义演进判定矩阵

### 5.1 判定原则

兼容判断始终比较 B 与 C，不仅比较相邻两次 refresh。说明文字相等不能证明权限或身份相同；完整 semantic fingerprint 不同也不必然意味着旧调用不可执行。

对可继续 native 的结构变化，要求支持的变换能确认旧输入契约不会被收紧；还必须保留调用时对冻结调用契约与当前输入 schema 的验证。验证失败在物理发送前返回准确错误，不能删字段、补默认值、转换类型或自动改走 meta。

第一版的兼容分类器仅识别下面列出的有明确前提的变化。无法确认时转 META，不把未知声称为“已证明破坏式”。说明文案要区分“不兼容”与“兼容性未确认”。

有限变换只能作用于已明确支持的普通对象/属性/数组 items 等正向约束位置。第一版结构放宽白名单只处理不含引用、组合/条件、unevaluated 和未知执行关键词的明确简单 schema；不要新增引用图或极性分析器来扩大支持面。完全未改的复杂 schema 可以继续 exact-match；纯文案及已确认无关的顺序变化仍可识别，遍历真正 schema 节点必须复用依赖，不用通用 JSON 深度删 key。

反例：`$defs.t.enum` 从 `["a"]` 扩为 `["a","b"]`，如果根通过 `not: {"$ref":"#/$defs/t"}` 引用它，根输入集合反而收紧。不能只检查被修改节点是否直接位于 `not` 子树内。

原生参数验证复用现有 provider lowering/argument admission 规则；不得另写一套 strict/nullability 转换。模型主动传入只存在于新版的字段或 enum 值，不因为 C 接受就放行；冻结的调用契约仍然约束该原生入口。

### 5.2 输入与描述

| ID | 变化 | DIRECT 是否保留 | 说明及限制 |
|---|---|---|---|
| N01 | 完全相同的定义，单纯 reconnect 或重新枚举 | 是 | 复用当前有效执行绑定，连接实例依旧重新 admission |
| N02 | 工具 description、title 变化 | 是 | 不修改 provider 文案，不发 diff；前提是没有同时变化的权限/执行策略 |
| N03 | schema 节点的 description/title/$comment/examples 变化 | 是 | 仅忽略真正 schema 节点的注解；不能递归删除用户数据中的同名 key |
| N04 | 对象键顺序、required/enum 的排列变化 | 是 | 只按对应关键词的语义处理；不把所有数组排序 |
| N05 | 新增可选属性，旧对象明确禁止该属性，且所有旧约束原样保留 | 是 | 新字段不在 required，无新条件约束；旧调用集合仍被接受，新字段不提前开放 |
| N06 | 新增可选属性，但旧 additionalProperties 已允许任意同名值，新字段开始限制它 | 未必；无法确认则 META | “可选”不等于兼容：过去合法的额外字段可能现在被拒绝 |
| N07 | 新增可选属性，同时修改 dependentRequired/if/then/else、propertyNames 等 | 无法证明则 META | 不能只检查 required 列表 |
| N08 | 简单 enum 扩大、const 放宽为包含旧值的 enum | 是，限已实现的直接位置变换 | 不在 not、oneOf 等非单调位置泛化；新取值不提前开放 |
| N09 | 简单上下界放宽、移除 required 项 | 是，限相同 dialect/相同已支持结构 | number/integer、exclusive 规则不能混淆；参数仍按冻结契约发送 |
| N10 | enum 缩小、const 改值、最小值升高、最大值降低、格式/长度收紧 | 否，META | 即便某次旧参数碰巧有效，也不保留整个 native 契约为正常可用 |
| N11 | 增加 required 参数，或可选改必填 | 否，META | 不自动填默认值；default 不是保证服务端接受省略的授权 |
| N12 | 删除/重命名旧属性，改变字段类型或含义 | 否，META | 不推断重命名映射；纯自然语言中的含义变化不由分类器理解 |
| N13 | additionalProperties true → false，或 schema 型额外属性约束收紧 | 否，META | 即使已声明属性未变，旧合法输入域也可能缩小 |
| N14 | additionalProperties false → true，其他约束不变 | 是，限明确支持的简单对象结构 | 仍不让模型借此使用冻结契约外的新字段 |
| N15 | default、deprecated、readOnly/writeOnly、x-* 等字段变化 | 非纯文案；按已知执行/契约语义判断，否则 META | 尤其不能忽略 SDK 消费的 x-mcp-header；不把“注解”泛化为无影响 |
| N16 | $schema/dialect、$id、$ref、动态引用或组合结构改变 | 无法证明则 META | 复用依赖的 admission/解析；不自建等价证明或外部引用下载 |
| N17 | 不认识的关键词发生变化 | META，原因是兼容性未确认 | 未变的未知关键词不单独造成降级；与已知变换的交互不能擅自忽略 |
| N18 | 服务声明 schema 非法、外部引用不被允许、元数据越界 | BLOCKED 或按现有 invalid-tool policy 处理 | meta 不绕过 schema admission；其他工具的隔离语义沿现有 policy |
| N19 | 仅 provider 原生格式不再支持，但 MCP schema 合法且可完整 inspect | META | meta 参数容器不受叶子原生 lowering 的同一限制 |
| N20 | 同时出现文案变化和破坏式结构变化 | META | 不让文案忽略规则掩盖实际结构变化 |
| N21 | B → 兼容 C1 → C2 | 每次与 B 比较 | 不通过逐步放宽/收紧链条偷换原始契约 |
| N22 | 已降级后 C 回滚成 B | 本 epoch 仍 META | 下一个合法 cold 边界才可重新选 native |

这里的兼容是可执行的输入契约判断，不是对远端业务语义永远不变的保证。服务端在完全相同 schema 下改变行为，也不能靠比较 schema 自动识别。

### 5.3 输出、执行策略和身份

| ID | 变化 | 决策 |
|---|---|---|
| O01 | output schema 仅描述注解变化 | 可继续 DIRECT；不向模型重复 diff |
| O02 | output schema 新增、删除或结构变化 | 第一版转 META，inspect 展示完整新版输出契约；不实现第二套输出兼容推理 |
| O03 | 实际返回不满足采用的 output schema | 复用 SDK/facade 的输出错误结算；不能称为“本地未执行”，不能自动重跑 |
| O04 | 仅超时/连接物理参数调整 | 由现有执行策略 owner 采用；不因其变化重写 native schema |
| O05 | read-only/effect、授权范围或确认要求变化 | 当前权限/确认规则立即约束下一次 admission；不能用旧 descriptor 或 meta ref 沿用旧批准 |
| O06 | Root 可见改为对子代理不可见 | 子代理停止新 admission；Root 按其自身路由继续，不把两者目录混用 |
| O07 | 相同名称换了 endpoint、安装来源或凭据主体 | 按当前配置身份及授权采用；拒绝旧 permit/ref 的错误复用，不推断等价 |
| O08 | 工具改名 | 旧名不可用，新名作为新工具 META；下一个 cold 边界再选择 |
| O09 | 服务端只有未公开的行为变化，无新元数据/通知 | 无法提前识别；诚实处理真实调用结果，不能声称端到端零竞态 |

结构性 output 变化转 meta 是此轮的保守产品决定，不等同于断言它必定破坏输入调用，也不应向模型错误报告“输入参数不兼容”。

### 5.4 唯一判定顺序

1. 先使用当前身份、scope、schema admission 和连接事实决定本次是否存在合法执行候选；权限与具体调用批准由原 owner 处理。
2. 不在 B 中的候选走 META，不运行“晋升 native”判断；已锁定降级的 identity 同样保持 META。
3. 对未降级的 B 内工具，完整冻结/当前定义相同则继续 DIRECT；仅允许的文案/语义无关顺序变化同样继续 DIRECT。
4. output 结构发生变化，按 O02 转 META；input 结构变化只有落入明确有限兼容集时才保留 DIRECT，其余转 META，分别标明已确认不兼容或无法确认。
5. META 必须满足完整 inspect 的物理边界；不满足时不可用。DIRECT 不受新版完整 inspect 体积单独限制，但仍遵守当前工具 metadata admission 和原生冻结契约。
6. 调用时执行既有参数、当前权限及 MCP admission 检查。一个兼容分类结果不签发永久许可，也不能跳过 SDK/current owner 的实际约束。

比较 JSON 值必须保持 JSON 类型语义，不能因为 Python 中 `True == 1` 就把 enum/const 视为相同；也不能规范化后把原 provider wire 替换掉。规范化只用于局部判定，B 与已安装 wire 保持原值。

## 6. 工具路由与恢复完整矩阵

| ID | 原状态 | 新观察 | 目标状态 | 模型如何获知 |
|---|---|---|---|---|
| R01 | 不存在于 B | 新增且可采用 | META | catalog 新增发现项；inspect 获取定义 |
| R02 | 正常 DIRECT | N02–N09/N14 中已证明兼容的变化，输出/策略也允许 | DIRECT | 不主动 diff，保持原生定义 |
| R03 | 正常 DIRECT | 已确认不兼容或无法确认兼容 | META，旧 native 入口关闭 | catalog 明确 native 已停用；直接误调用也返回 inspect 指引 |
| R04 | DIRECT | 连接暂失、认证待完成、完整目录暂不可读 | BLOCKED，保留未降级身份 | catalog 报原因；不错误建议 meta 能绕过 |
| R05 | R04 | 同身份有效定义恢复且与 B 兼容 | DIRECT | 更新/清除原生例外；不永久停用 |
| R06 | R04 | 恢复后的有效定义与 B 不兼容 | META | 给出转 meta 指引 |
| R07 | META | schema/description 再次变化，仍能完整 inspect | META | 不主动 diff；旧 ref 如失效则调用时要求重新 inspect |
| R08 | META | 工具被移除或 scope 不再允许 | BLOCKED/不在当前可见集 | 更新目录；旧 ref 执行前拒绝 |
| R09 | R08 | 工具同名恢复、重新允许 | META | 目录恢复；回滚不自动激活已退休引用，重新 inspect 并 FULL 采用后才可调用 |
| R10 | DIRECT | 完整权威枚举确认工具删除 | BLOCKED | 原生例外说明已移除；其他 native 不受影响 |
| R11 | R10 | 同 identity 返回，未发生过有效不兼容降级 | 与 B 比较后 DIRECT 或 META | 不能仅凭名字相同决定恢复 |
| R12 | 需要 META 的工具 | schema 非法/完整 inspect 超物理边界 | BLOCKED | 明确不能完整读取契约，不返回看似可用的 ref；兼容 DIRECT 不因 inspect 太长被额外停用 |
| R13 | 降级 META | 服务回滚/预算减少/再次 reconnect | META | 若目录状态无实质变化，不再通知 |
| R14 | 任意 | 全新 cold epoch/显式采用 compaction successor | 重新规划 DIRECT/META/BLOCKED | 从当前已采用完整事实重建初始目录，不继承旧 epoch ref |
| R15 | 不存在于 B 的 META | 同名工具加入了另一个会话的原生集合 | 本会话仍 META | 会话间不互相改变冻结集合 |

路由选择与网络可达性是两个维度。文档中的 BLOCKED 不要求新增一个可持久恢复的状态机；复用现有不可用原因及 typed runtime facts。

## 7. Meta inspect、引用与调用矩阵

### 7.1 正常协议

`list_capabilities(kind=MCP_TOOL, parent=MCP_SERVER)` 负责发现。`inspect_capability(target=MCP_TOOL)` 按当前路由返回：DIRECT 返回本 epoch 可调用的冻结 description/input schema 与原生工具名，不发 meta ref；META 返回当前完整 description、input/output schema、调用方式及 ref；BLOCKED 返回原因与适用恢复方式。`use_new_mcp_tool` 使用有效 ref 与 arguments 调用。保留现有公开 meta 工具名，不为了名称中的 new 再增加一套别名。

DIRECT 的工具枚举摘要同样对应冻结可调用契约，不能用最新 description 宣称原生可使用新增字段。此轮不额外增加“同时展示冻结和最新两份 schema”的查询模式；远端最新管理视图不等于当前会话原生调用契约。

meta ref 不是权限票据。沿现有 owner 检查签发来源、scope、子任务、epoch、定义版本、路由、execution policy，以及实际执行时的连接/admission 条件。

| ID | 场景 | 行为 |
|---|---|---|
| M01 | 仅枚举到工具名，未 inspect | 可发现但没有可调用 ref；不能凭名字构造 token |
| M02 | inspect 结果尚未提交 | PREPARED，不可调用；失败释放准备状态 |
| M03 | inspect 结果已提交但未以 FULL 进入 provider 输入 | DORMANT，不可调用；不得用截断 schema 激活 ref |
| M04 | 完整结果被当前 scope/epoch 采用 | CALLABLE，可调用；不宣称证明模型主观已读懂 |
| M05 | 同 ref 有效，后续连续调用 | 可重复使用，无需每次重新 inspect；每次仍做权限/admission |
| M06 | meta 定义任意次数更新 | 路由仍 META 时不主动推送 diff；旧 ref 按当前 exact-version 规则失效 |
| M07 | 仅 meta 描述变化导致 exact-version ref 失效 | 本轮允许要求重新 inspect；不为了避免此成本扩建“宽松 ref”第二套规则 |
| M08 | inspect 后、调用前定义被采用为新版 | 物理发送前拒绝旧 ref，告知重新 inspect，不自动重试 |
| M09 | 参数不符合已 inspect 的当前 schema | INVALID_ARGUMENTS，指出可公开的具体问题；不把参数错误伪装成版本变化 |
| M10 | ref 跨会话、Root/子代理、epoch，或伪造/已退休且未重新完整 inspect 采用 | 拒绝；在当前作用域重新 inspect，不能复制旧引用当授权 |
| M11 | policy/身份变化使旧 ref 不再绑定当前 executor | 拒绝并说明当前状态；不能用“schema 没变”绕过 |
| M12 | 同 schema reconnect | 不因物理 generation 变更就必然要求新 schema；是否保留 ref 按现有语义/policy 身份判断，permit 必须绑定当前连接 |
| M13 | inspect 被截断、descriptor 超界或 schema 无法安全解析 | 不签发可调用引用；按既有物理边界返回明确诊断 |
| M14 | 原生降级后模型仍发旧 native 调用 | 本地拒绝，给 exact inspect target 和 meta 指引；不替模型迁移参数 |
| M15 | 工具实际已执行但返回错误/超时/输出不匹配 | 沿既有 effect-aware 结算；不声称未执行，不通过重新 inspect 自动重放 |
| M16 | 其他工具新增/改版，当前 META 工具的定义、policy、route 均未变 | 当前合法 ref 继续可用；不因整个 server/目录聚合版本变化令所有引用失效 |

### 7.2 长程引用生命周期

当前 `conversation_kernel/mcp/meta.py` 的既有边界是每 scope/epoch 最多 1,024 个 unique live refs，当前实现直到 epoch 退休才集中清除。反复更新同一工具会产生多个版本引用，不能让早已不可调用的历史记录永久占用这项 live 预算。

实施要求：

1. 复用现有 meta owner，在新路由事实被采用后识别已经失去当前合法调用路径的 ref。
2. 失效 ref 不得仅因回滚重新自动变为 CALLABLE；需要重新 inspect 并完整采用。若同 scope/epoch 的完整定义、policy、route 恢复相同，新的 inspect 经 FULL 采用后，允许既有确定性算法签发相同 token 字节；它不是承诺永久撤销的能力票据，不新增 nonce 或永久 tombstone。
3. 未决 inspect settlement、已获得 admission 的调用和其他合法在途消费者必须正确结算，再释放其必要持有；不能清理过程中破坏在途调用的所有权。
4. 没有在途持有、确定失效的 ref 退休并释放；不得保留无限 tombstone 或另建历史 registry。
5. 不驱逐仍有效的当前引用来假装无限容量，不引入 LRU 或任意 TTL；真正同时达到既有 live 物理边界时仍返回 typed capacity failure。重复 inspect 同一仍有效版本复用当前引用，不消耗一个新版本配额。
6. 反复更新、inspect、调用、结算超过 1,024 个历史版本，且同时 live/在途占用始终很小，必须持续可用。测试可缩小已存在的物理阈值验证生命周期，不能增加总版本次数上限。
7. host 关闭/新 epoch 按现有 owner 退休；不把历史 transcript 中的 ref 恢复成执行授权。
8. A → B → A 后的新引用记录只接受本次新 inspect 对应的 settlement/result entry。退休前迟到的 settlement、旧 FULL 安装或旧 transcript 重放，不能激活新记录；复用已有 owner-issued 对象身份和 result/settlement IDs 隔离生命周期，不用 token 字符串相同作为激活依据。

## 8. Catalog：稀疏当前快照，不是 schema diff 日志

### 8.1 目录的内容

catalog 只补充原生定义没有表达清楚的当前事实：

- 当前 META 工具：无论来自冷启动预算降级、原生 wire 不兼容、服务端新增，还是 native 契约降级。
- 仍出现在 B、但当前不能使用原生入口的工具及原因；若可走 meta，明确指出。
- 与发现有关的 server 状态、必要标识、数量、完整性/省略信息及枚举入口。
- 保留 MCP resources/prompts/templates 的现有发现能力及原 read/get 入口，不因为精简工具清单而静默删除这些能力。

工具或整个 server 已被权威确认移除时，原生停用例外由 B 中该 scope 已安装的 identity 与当前缺席事实构造，不要求当前 C 或当前 server 行仍存在。不复制旧定义当“当前工具”，也不引入持久 tombstone；scope 撤销只纠正模型已经见过的 B 中入口，不暴露该 scope 从未有权看到的其他工具、配置或凭据。

不重复列出正常 native 工具；不包含完整 schema、逐字段 diff、每个 meta 版本号、ref token、凭据或高频连接 generation。server 概览优先稳定的已配置名称、身份与必要能力信息；不把任意长的远端新描述自动塞进提示词。

把 provider-facing 的 `new_tool_names/new_tool_count/new_tool_usage` 改成表达 META 路由的字段；它们不再暗示“刚新增”。hard cut 更新生成端、schema/文案和消费者，不同时输出新旧字段。内部既有公开 `use_new_mcp_tool` 名称不在此轮重命名。

### 8.2 模型可见更新矩阵

| ID | 变化 | 是否追加 catalog | 内容 |
|---|---|---|---|
| C01 | cold 时全部工具 native，且无例外 | 不重复工具名单 | 必要 server/resource 概览可以保留 |
| C02 | cold 时大量工具因阈值 META | 是，初始发现目录 | server、META 数量、预算内名称、分页入口 |
| C03 | 服务新增工具 | 是 | 更新当前 META 集合；不塞完整定义 |
| C04 | native → meta | 是 | 原生停用 + 当前 meta 发现入口 |
| C05 | native 纯文字/已确认兼容更新 | 否 | 原生定义未改、目录未变 |
| C06 | 已有 META 定义/描述反复更新 | 否 | 调用时发现 ref 失效后 inspect |
| C07 | META 新增/删除、可用/不可用变化 | 是 | 更新当前目录状态 |
| C08 | 普通 native 暂不可用/恢复 | 是 | 增加/修正/明确清除原生例外 |
| C09 | 定义、物理 generation 改变，但所有目录事实相同 | 否 | 底层大 fingerprint 变化不能单独触发重复消息 |
| C10 | epoch、scope 或冷启动采用集合改变 | 新上下文重新产生适用目录 | 不能复用旧 epoch 的降级表或 token |
| C11 | 多次变化发生在下一次正常 dispatch 之前 | 合并成当前快照 | 不重放所有中间 diff；已采用的 native 降级仍保留 |
| C12 | 上次非空目录现在真正为空 | 必须显式清空 | 使用 typed empty/CLEARED，不以缺席表示恢复 |
| C13 | 源暂不可读、完整性未知 | 必须表达 unavailable/unknown | 不能把失败视为“所有工具已删除” |
| C14 | 本次预算未选中 catalog | 不是目录为空 | 沿现有 source 选择语义；不能消费为已送达或删除旧前缀 |
| C15 | 用户刚点击能力页操作，但工具目录没变 | catalog 不重复 | 已结算 UI 因果事实仍走既有 CAPABILITY_CHANGE，不重复操作 |

### 8.3 快照、去重与清除

“增量”指相对 B 的稀疏当前快照，而不是必须从第 1 条开始累积计算的补丁序列。后续同 source 的 VALUE 取代该 source 先前的当前目录含义，历史消息仍保留。MCP_CATALOG 继续使用既有非可信观察来源，不把远端名称/描述升级成高优先级指令或授权。

目录 equality 必须基于当前稳定可公开的发现事实、路由及完整性，不包含隐藏的 schema/description version 或随机生成值。旧 fingerprint 可继续用于真正的源/路由 exact join，但不能使不变的模型可见目录每次重新发出；不新增冗余 DTO hash registry。

最终安装判重还必须考虑本次选中的实际 variant 与 lifecycle：FULL 有变化，但本次安装的 COMPACT/REF_ONLY 内容与上一有效观察完全相同，不应再追加同一摘要。反过来，预算允许首次呈现更多目录内容或生命周期从 UNAVAILABLE 恢复 VALUE，就不能因为总集合“没变”漏掉这一更新。复用 compiler/continuity 的 installed observation，不引入第二套已读回执；只针对 MCP_CATALOG 收口，不顺手改变其他 source 的语义。

空目录的语义必须在现有 source lifecycle 中实现：cold 时原本就无附加事实可不产生内容；已经安装过非空目录后，必须用明确的空值或 CLEARED 清除。`NOT_APPLICABLE`、预算未选中和 `UNAVAILABLE` 不能互相冒充。

### 8.4 分页、有界渲染与摘要优先级

1. 复用现有 32 KiB / 8 KiB / 2 KiB 渲染档位及 compiler 总预算，不新增全局工具数/历史数上限。
2. 优先保留“旧 native 入口已经停用”的准确纠正、server 可发现性、工具计数和完整性说明；再在剩余预算内列 META 名称。
3. 名称放不下时，不切断 target/工具名。报告省略数量；提供 `list_capabilities` 的 server 列表与按 server 工具分页入口。
4. server 本身也放不下时报告省略 server 数，保留查询所有 server 的入口，不能让排序靠后的 server 永远不可发现。
5. 短目录也不能承诺包括所有停用例外；有省略时模型应以 inspect/调用返回为准，调用 gate 必须独立于通知是否送达。
6. 分页结果、server inspect 和 catalog 共用同一当前采用视图；分页发生 refresh 时沿现有 cursor/offset 规则明确一致性边界，不能把跨版本拼接说成完整权威快照。
7. server inspect 返回当前 server 状态、计数、发现入口；工具枚举负责名称/摘要/路由；单项 inspect 提供完整契约。不要在三处维护三份工具 schema。
8. 新增工具恰好落在未展示区域时，可通过数量/目录变化和分页发现；一进一出而可见摘要完全相同时，不保证主动告知具体变化。完整枚举仍返回当前事实，不新增不可见历史通知队列。

### 8.5 目录示例（语义示例，不是第二套公开协议）

```text
firecrawl:
  meta:
    - firecrawl_example：不在当前原生定义中，inspect 后通过 meta 调用。
    - firecrawl_search：当前版本通过 meta 调用。
  native_unavailable:
    - firecrawl_search：旧原生入口已停用，使用上述 meta 路径。
  omitted_meta_tools: 0
```

模型不得把上述名字自行拼成真实 target；实际字段必须使用现有 query 返回的完整目标。示例中的重复说明表达路由关系，实现可合并成一条有明确 native 状态的记录。

### 8.6 实施时的唯一目录载荷

沿现有 `MCP_CATALOG` source 及 runtime-observation envelope，更新其版本化载荷契约；不新增 context source kind，不做旧/新格式双读。既有 runtime server/resource/prompt 字段继续保留其含义，工具发现部分按下表定义。字段可以复用当前已有同义 typed 字段，但不得再出现两份含义相同的集合。

| 内容 | 完整语义 |
|---|---|
| 原生例外集合 | 只针对 B 中已知入口；含 exact target、原生不可用原因、当前可走 META 或当前 BLOCKED 的状态及行动指引 |
| META 目录集合 | 当前可见 META identity、exact target；可选短名称，不含完整定义或 schema 版本；与原生例外同项时允许合并展示，但计数不重复 |
| Server 可发现摘要 | exact runtime identity、稳定显示名、已知连接/目录状态、工具数与现有资源/prompt 数；实际被移除的 server 只承载 B 来源例外，不假装仍连接 |
| 完整性 | 区分事实观察不完整/不可用与渲染预算省略；只有完整观察时才报告精确总数；未知不能填 0 |
| 省略计数 | 已知集合中由于预算未展示的 server/tool/例外数量；不代表远端实际缺失 |
| 枚举指引 | 复用现有 list_capabilities 的 MCP_SERVER 列表、MCP_TOOL parent 分页及单项 inspect；不另造 schema 或执行 token |

逻辑集合按工具 identity 去重，渲染保持确定顺序。可用性文案使用稳定原因类别，不把连接重试次数、随机错误串或时间戳作为目录更新内容。调用失败的细节保留在实际工具结果和 diagnostics，不能通过清空目录抹掉它们。

上述是载荷必须携带的语义，不另设模型需要手工合并的新增/删除 patch API。对完整、相同渲染预算下的新 VALUE，模型使用新的当前目录；对省略/未知，模型不得据缺席推断删除。

## 9. 发现、刷新与作用域矩阵

| ID | 触发/范围 | 要求 |
|---|---|---|
| D01 | SDK 收到 tools/list_changed | 复用既有回调/刷新 owner；通知是失效提示，不是完整新目录 |
| D02 | server 不发通知 | 依赖既有配置周期刷新（启用时，`catalog_refresh_interval_ms`）、reconnect/reload/发现机会，不保证即时得知；不新增高频轮询，也不删除现有周期刷新 |
| D03 | 单纯 list/inspect | 读取当前已采用事实；不能谎称一定触发了全服务器远端重连 |
| D04 | 会话侧栏 reconnect | 更新该 owner 的物理连接及采用事实，保持 B 不变 |
| D05 | 能力页配置保存/重载 | 用户级 owner 负责数据和受影响 scope 的刷新；不能依赖某个当前会话来定义全局可用性 |
| D06 | reload 返回配置 changed IDs 为空 | 不等于远端 schema 没变；配置身份变化和工具定义变化分开描述 |
| D07 | 普通下一条 user message | 仍在原 epoch；允许采用新目录，不允许重建原生数组 |
| D08 | ROOT 与 SUBAGENT_TASK | 各自的 B、目录、ref 与权限独立；子代理只看获准范围，不泄露 Root-only 工具 |
| D09 | 创建新子代理 | 按自身冷启动及继承权限规则规划，不复用父 ref；不因父工具 meta 就强制子工具 meta |
| D10 | fork/重开 host/进程重启 | 新执行 owner 按现有合法 cold 路径采用；历史聊天不复活 ref/permit；不改 fork 的任务继承语义 |
| D11 | 调用发出后远端无通知热更 | 无法原子钉住远端部署；诚实处理服务返回，不承诺实际远端版本被 ref 锁定 |

## 10. 竞态与执行结算矩阵

本节的“发送前”以既有 MCP physical attempt/admission owner 定义的边界为准，不能用“模型还没收到结果”替代。通知到达、目录采用、参数检查、授权、物理发送各有不同时间点。

复用 supervisor 当前的 dirty fence → drain pre-fence admissions → SDK catalog listing → 采用新 lease/视图顺序：list_changed 先阻止新 admission，已经获得有效 permit 的排队/执行操作按原 owner 结算，待其 drain 后才刷新 SDK 的输入/输出元数据缓存。收到通知不等于新版已经采用，不追溯撤销合法旧 permit。用户取消、连接关闭和授权撤销仍按各自原有合同处理，本轮不重新定义它们的在途结算。

| ID | 时序/竞态 | 必须发生的结果 |
|---|---|---|
| X01 | provider 请求已经安装旧 B，生成调用期间收到目录更新 | 不修改该请求；runtime 在 admission 核对当前合法路由，拒绝已失效入口；下一次正常请求追加目录 |
| X02 | native 参数验证后、admission 前发生不兼容采用 | 旧请求不得靠已过一次验证穿越；本地拒绝并指向 inspect |
| X03 | 已获有效 permit，排队等待 lane 期间收到 list_changed | dirty fence 拒绝新 admission，已有 permit 按原 owner 继续/取消并 drain；不能静默换 executor，也不能把通知误当已采用新版 |
| X04 | 物理调用已开始后才观察更新 | 按已开始的 attempt 结算；不伪装成“确定未执行”的 schema stale |
| X05 | meta inspect 完成但工具结果提交失败 | ref 不激活；重试采用正常 inspect 路径 |
| X06 | inspect 已提交，FULL provider 安装失败或只渲染截断体 | ref 不可调用；再次安装须走现有完整采用边界 |
| X07 | inspect FULL 安装后、新版本采用前模型复用 ref | 当前合法则执行；不能为了未来可能变更强制每次 inspect |
| X08 | inspect FULL 安装后、新版本已采用，再调用旧 ref | 发送前拒绝，明确重新 inspect；不自动转换参数 |
| X09 | schema 与 policy 同时更新 | 兼容不豁免权限；新的 admission 基于一致采用视图，不混搭旧 schema 与新 policy 的批准；既有 permit 按原 owner 结算 |
| X10 | 用户审批等待期间发生定义/参数/身份变化 | 原批准不转授给新调用；复用现有 stale-confirmation 路径重新准备，必要时重新审批 |
| X11 | 用户 stop/steer 与目录更新同时发生 | 沿现有取消/steer 合同；未发出的调用不执行，已发出结算真实结果；不为 schema 恢复覆盖用户取消 |
| X12 | 同一批多个工具，其中一个失效 | 仅拦截受影响项，其余按既有并行/权限规则执行；不能先执行再统一报未发送 |
| X13 | 同 server 多个更新通知乱序/合并 | 采用完整当前观察，不按通知顺序重放 schema；旧连接回调不能覆盖新连接 owner |
| X14 | 部分 tools/list 页成功，后续失败 | 不将缺失页工具宣布删除；保留明确不可用/不完整观察，执行安全性由现有 owner 决定 |
| X15 | 全量刷新完成，但新计划尚未安全采用 | 不向模型发布与 executor 不一致的新目录；沿既有安全点安装一致 plan/runtime/source |
| X16 | compiler 已准备目录，随后又发生变化 | 已安装内容不重写；下一次观察追加更新；未安装候选不消费为已通知 |
| X17 | provider 请求失败重试 | 复用已安装前缀，不能重复追加同一目录或偷换 B |
| X18 | catalog 变为空/不可读时恰好预算紧张 | 正确表达生命周期，不以静默缺席恢复旧工具；gate 始终有效，不依赖模型先收到通知 |
| X19 | compaction 准备期间收到更新 | 由显式 successor 采用边界决定新 B；旧请求继续受旧 epoch gate，不能混用两套 ref |
| X20 | compaction 失败或未采用 | 保留原 epoch、降级事实和合法 ref；不提前 cold-adopt |
| X21 | host 关闭/进程崩溃后恢复聊天 | 丢弃 process-local ref、permit 与通知；新 cold 上下文从当前事实建立，不回放历史执行 |
| X22 | ref 退休与在途操作/inspect settlement 并发 | 拒绝新的失效调用，已有合法持有完成结算；不泄漏、不复活、不错误占用历史配额 |
| X23 | 输出 schema 刷新与旧调用返回并发 | SDK listing 必须等待 pre-fence admissions drain，避免本地缓存改写在途验证契约；远端自身热更仍可能使旧调用返回不匹配，沿真实结果结算，不私建验证缓存、不称为未执行 |
| X24 | 远端在本地最后检查后无通知改变定义 | 返回实际远端协议/应用错误；ref 不代表服务端版本锁，不自动重放副作用 |
| X25 | server 目录一进一出，总数不变且均在省略区 | 不制造虚假的全量变化保证；分页可读当前事实，旧引用调用仍有独立 gate |
| X26 | B → 不兼容 C → B，期间尚无模型调用 | 若 C 已被采用，仍锁定 META；若 C 仅候选且未采用，不凭未发布候选制造持久降级 |
| X27 | A inspect 已提交未 FULL → B 采用 → 回滚 A → 新 inspect 尚未 FULL，此时旧 settlement/FULL 迟到 | 不激活新的 A 记录；只有新 inspect 的 FULL 采用可使当前 ref CALLABLE；允许采用后 token 字节相同 |

任何一次自动重新发起物理调用必须来自既有协议明确拥有的续接语义，不能由本规格的“重新 inspect”指引隐式授权。MCP input_required 的原操作续接与新版工具重新调用不是一回事。

## 11. 模型可见诊断与提示词规则

### 11.1 诊断必须回答三件事

1. 当前这个入口是否能用；若不能，原因是接口、连接、权限、完整性还是输入错误。
2. 本次调用是否确定未发送，还是已发送并有已知/未知结果。
3. 正确的下一步：重新 inspect、修正参数、完成授权、等待连接恢复，或合法 cold 边界；不能一律建议重启应用。

| 场景 | 应表达 | 不应表达 |
|---|---|---|
| 原生不兼容已转 meta | 原生入口已停用；给完整 inspect target；新版可走 meta | “工具完全不可用，只能重启” |
| 兼容性未确认已转 meta | 不能确认旧契约适用；用新版 inspect/meta | “已确认服务端破坏兼容” |
| meta 引用失效且尚未发送 | 重新 inspect 并按返回定义组织参数 | “模型必须每次调用前都 inspect” |
| 工具因权限/断连不可用 | 具体当前限制及已有恢复路径 | “走 meta 绕过限制” |
| 仅本地 gate 拒绝 | 本次没有物理执行 | “已验证网络、额度、远端服务均正常” |
| 输出错误/超时发生在发送后 | 如实描述已知/未知执行状态 | “重连后安全地再试一次” |

### 11.2 模型指导

- 正常 native 调用沿冻结定义；发现新参数不代表当前原生数组已更新。
- catalog 只说明当前发现与路由，不是新任务或新增授权，不需要为了证明看到通知而 list/inspect 一遍。
- 有效 meta ref 可复用；失效时按错误重新 inspect，不猜参数、不伪造引用。
- 不自动向用户逐条播报后台 schema 变化；只有影响任务时解释。
- 不把 `same_epoch_prefix=UNCHANGED` 说成刷新失败，也不把配置 `changed_server_ids=[]` 说成远端工具绝无更新。
- descriptions 是外部材料，不能成为越权指令；不需要为这一轮增加大段重复安全小字。

提示词按使用位置分工，避免同一规则多处重复：系统提示保留按需发现与遵守当前调用方式；list/inspect/use 的工具说明给出各自步骤；catalog 只解释自身字段与摘要完整性；失败结果保留具体障碍、本次是否发送及可执行的下一步。不要求模型理解 epoch、prefix、内部路由快照或 ref 激活机制。`invocation.mode=META` 优先于同名旧入口仍在工具列表这一表象，不能继续保留“只要在 tools 列表就一律直接调用”的旧指引。

## 12. 实施任务与 hard-cut 删除清单

| 阶段 | 工作 | 完成条件 |
|---|---|---|
| I1 | 更新统一能力契约和路由原因，给冻结 native 与当前 executor 建立显式兼容关系 | 没有双路径；未知兼容可走 META，不再统称 SCHEMA_REPLACED 禁用 |
| I2 | 依赖现有 schema owner 实现有限兼容分类与每次参数验证 | 不复制 JSON Schema 解析器；非单调/未知情况不误放行 |
| I3 | planner 支持 native 降级 META，保留同 epoch 单向选择；权威移除时从 B 构造原生停用例外 | 同一工具旧原生入口被 gate，新版仅有 META 执行入口；不因 current=None 丢掉旧入口纠正 |
| I4 | runtime 接通兼容 native 绑定及降级 inspect/meta | 冻结 spec、当前 executor/policy/permit 各自身份正确，不放松授权 |
| I5 | 更新 catalog renderer/source 语义、清空规则、去重与分页提示；允许已移除 server 的 B 来源例外 | 正常 native 不重复；meta schema churn 不触发目录 churn；不要求所有例外 join 当前 server 行 |
| I6 | 统一 list、server inspect、tool inspect、错误指导 | 所有入口报告同一采用视图，没有“目录说 META、inspect 说 UNAVAILABLE”矛盾 |
| I7 | 在既有 ref owner 中回收失效引用并覆盖并发结算 | 无历史配额耗尽、无在途泄漏、无 ref 自动复活 |
| I8 | 更新活动规范、提示词、测试和必要文档 | 只描述新规则；既有归档文档明确受本规格替代的边界 |

必须删除/替换：

- “任何 description/schema fingerprint 不同就禁用”的唯一判定。
- “已在原生数组中的 identity 永远禁止 meta”分支。
- 原生升级只允许 cold 恢复的统一错误文案。
- 用 `new_tool_names` 混指所有 META 的 provider catalog 字段与说明。
- 用完整 MCP schema 版本变化触发相同模型可见目录重复追加的依赖。
- 将已退休引用永久计入 live 容量的历史累积路径。

不删除：原生前缀冻结、合法 cold 边界、current owner/executor exact join、用户权限与确认、完整 inspect 激活、连接/attempt 结算、schema admission、资源预算。

## 13. 回归验收矩阵

不得靠生产 Firecrawl 实时变化证明兼容分类。使用可控 MCP server，经过真实 SDK transport、supervisor、planner、runtime、compiler 和 provider wire 记录入口；provider 可以使用可控测试端，不以真实模型推理替代可重复断言。真实模型实测须另获用户安排。

| 组 | 覆盖 | 核心断言 |
|---|---|---|
| T01 原生基线 | B01–B07 | 128/129、63,999/64,000；内置不计入；同 epoch 不重选 native |
| T02 文案 | N02/N03/N04/N20 | 工具/嵌套描述变化可用；const/default/examples 内业务数据不被误删；enum/const 的 boolean 与 number 不混同；结构变化不能被掩盖 |
| T03 可选属性 | N05/N06/N07 | 简单安全增量继续；旧 additionalProperties 开放、条件约束反例转 meta |
| T04 约束 | N08–N17 | enum/边界放宽正确；收紧/必填/类型变化降级；覆盖 `$defs` 经根 not/$ref 引用的非单调反例，不只检查节点祖先 |
| T05 输出与策略 | O01–O09 | 输出结构变化路径准确；SDK 输出失败保留执行事实；权限变化不被兼容性绕过 |
| T06 原生降级链路 | R03/M14 | 旧 native 本地拒绝；inspect 完整新版；meta 成功；同一工具仅一个有效新版入口 |
| T07 Meta 重复调用 | M01–M08 | FULL 后 ref 可复用；版本更新后旧 ref 未发送；新 inspect 后继续；catalog 不追加 schema diff |
| T08 Ref 所有权 | M09–M13 | forged/foreign/跨 epoch/子任务/dormant/截断结果均拒绝；不泄露凭据 |
| T09 发现 | C01–C04、D01/D02 | 新工具从通知/重载发现；未通知前不虚构；预算 meta 初始目录可发现 |
| T10 目录稳定性 | C05–C15 | 未变不追加；底层版本变但目录不变不追加；FULL 变而选中 COMPACT 不变时不重复；预算允许更多信息时不遗漏；明确清空与 unavailable 分开 |
| T11 目录预算 | 第 8.4 节、X25 | 有界摘要、完整 target、计数与省略准确；所有 server/工具可分页发现；resources/prompts 不丢 |
| T12 生命周期 | R04–R15、D07–D10 | 断连同 schema 可恢复；已降级回滚仍 META；整个 server 移除仍有 B 来源例外；新 cold 可重新 native；普通 user turn 不冷启动 |
| T13 Admission 竞态 | X01–X04/X09–X12 | dirty 后不发新 permit，已有合法 permit 正常 drain；新采用后旧参数/批准不用于新 admission；其他工具不被整批错误拦截 |
| T14 观察竞态 | X13–X18/X26 | 不完整列表不推断删除；旧连接不覆盖新 owner；候选/采用/消息安装边界一致 |
| T15 压缩与恢复 | X19–X21 | 未采用压缩不换 B；已采用后 ref 重检；历史消息不复活授权 |
| T16 结算 | X04/X15/X23/X24 | 已发送结果保真；无自动 effectful retry；不能把输出错当没执行 |
| T17 长程 | 第 7.2 节、X22/X27 | 历史版本超过现有 live 阈值仍可继续；同时有效引用真实达到边界则明确失败；不驱逐有效引用；ABA 旧 FULL 不得激活新记录 |
| T18 Scope/UI | D05/D08、O06 | user-level 能力页独立，Root/child 正确过滤；会话级路由可不同 |

跨 T01–T18 必须检查实际 adapter-final provider payload：同 scope、同 epoch 的相邻请求 `SYSTEM/tools` 字节相同，旧 messages 是新 messages 的前缀；无新增内容的失败重试允许完全相等，有新增已采用内容时要求严格扩展。合法 cold/compaction successor 边界单独验证，不要求跨 epoch 保持同一前缀。不能只断言 Python 内部 DTO 未变。Chat Completions 与 Responses 两条已支持路径均需覆盖 native→meta 和目录追加，尤其包含 strict 原生 schema 拒绝新增字段的情况。

优先扩展现有 `tests/test_round9_unified_capability_semantics.py`、`test_mcp_native_threshold.py`、`test_mcp_unavailable_guidance.py`、`test_mcp_schema_validation.py`、`test_mcp_catalog_subscriptions.py`、`test_capability_source_query.py` 及 prefix continuity 测试，复用 `tests/fixtures/round9_mcp_server.py` 等真实协议夹具。不为每条矩阵复制一套 harness；不可删断言、添加 skip/xfail 换绿。

## 14. 明确限制与冻结检查

### 14.1 已明确接受的限制

- 本地无法把远端服务锁在某个工具版本；只能保证当前已观察契约、发送前 gate 和真实调用结算。
- “兼容”不证明业务含义或默认行为没有改变；纯自然语言费用/行为调整不自动分类或主动全文 diff。
- 新可选参数在当前 native 入口仍不可用；若没有发生降级，不额外提供同 identity 的 meta 捷径。需要新功能时采用合法 cold 边界。
- 目录按当前预算摘要，不保证主动点名每个省略区域中的变化；完整按需枚举和调用 gate 始终可用。
- 同 epoch 降级不自动回升；输出结构变化和无法证明的复杂输入变化可能走 meta，但不因此丢掉可调用能力。
- 同时 live 引用及单次完整 inspect 等既有物理边界仍存在；不声称支持无限并发或无限单个 schema。

### 14.2 冻结前检查

- [x] 用户已确认全文及第 1.2 节补充裁决，并授权完整实施。
- [x] GPT-6 Astra high 独立审阅，主代理独立检查后交流；文档中发现的高优先级遗漏和冲突已闭合。
- [x] 原生/Meta/不可用、发现/执行/授权、候选/采用/发送/结算没有混成一个状态。
- [x] 目录快照的清空、预算省略、schema-only 去重和分页未知边界均有验收项。
- [x] 不增加自动 rebase、持久事件、重复 hash/registry、投递 ACK 或隐藏总次数上限。
- [x] 规格确认与生产实现状态分开记录；未进行真实模型实测或触发 Actions。

### 14.3 交叉审阅记录

2026-10-21：主代理先完成文档并独立检查当前代码，再派 GPT-6 Astra high 子代理进行只读独立审阅；双方交流后由主代理修订，同一个子代理完成第二轮复查。未委派生产实现。

| 审阅发现 | 最终收口 |
|---|---|
| 确定性 ref 的 A→B→A 与“不复活”表述冲突 | 允许新 inspect FULL 采用后复用 token 字节；旧 settlement/FULL 不能激活新记录，加入 X27 |
| `$defs` 的局部 enum 放宽可能通过 not/$ref 收紧根输入 | 结构变换限定简单 schema，复杂情况转 META，不建引用极性分析器 |
| 通知到达时强行撤销已获准排队调用，会改变原有结算 | 明确 dirty fence、pre-fence drain、SDK listing 和新版采用的先后 |
| DIRECT inspect 与 META inspect 混写 | DIRECT 返回冻结可调用契约；META 返回当前完整定义及引用 |
| FULL 变化但实际 COMPACT 相同仍可能重复追加 | 按最终安装 variant 和 lifecycle 判重，复用 installed observation |
| 当前 server 已删除，renderer 无法 join 当前行 | 从 B 与权威缺席构造原生例外，不建持久 tombstone |
| 忽略现有周期刷新 | 明确保留已配置的 catalog_refresh_interval_ms 路径 |
| 完整 inspect 越界错误地影响兼容 native | 仅在需要 META 的调用路径要求完整 inspect 可交付 |
| 无关工具变化可能误伤已有 meta ref | 明确 M16；单项定义/policy/route 不变就不因聚合版本强制失效 |
| 前缀验收把失败重试和跨 epoch 情况混在一起 | 同 epoch 前缀允许重试相等；cold/successor 单独验证 |

第二轮结论：上述问题已闭合，可作为供用户最终确认的冻结候选，无新增必须单独选择的产品分歧。此后用户已确认全文并授权实施；这条文档审阅结论本身不代表生产实现通过验收。

## 15. 实施与代码审阅记录

### 15.1 已落地的唯一生产路径

| 边界 | 实现与验证 |
|---|---|
| B/C 兼容分类 | `capability/mcp_compatibility.py` 复用 referencing 的 schema 节点遍历，只证明本规格列出的有限简单放宽；输出结构改变、输入不兼容及兼容性未确认分别报告。契约 default 等字段不被当成描述删除 |
| epoch 路由 | planner 始终与冻结 B 比较；当前 executor/policy 与旧调用定义通过显式 binding 连接，调用时验证旧/新两个输入契约；已安装降级在本 epoch 保持 META，未安装候选不改变降级事实 |
| 目录与查询 | 稀疏目录省略正常 native 名称，展示 META 与原生例外，保持资源/提示词发现；按全局原生例外优先分配既有摘要预算。source disposition 决定目录完整性，连接 READY 不代表 tools/list 已完成 |
| 编译与前缀 | 按实际选中并安装的目录文本去重，明确 EXPLICIT_EMPTY 清除；保留 SYSTEM/tools 字节不变与 messages 仅追加；移除旧 schema replacement 禁止 meta 的路径 |
| 引用生命周期 | 复用现有 exact scope/epoch/version/policy/route 与 FULL 激活路径，回收不再合法的引用；旧 settlement/FULL 不激活 ABA 中的新记录，不累积历史版本占用 live 容量 |
| 所有权与持久化 | 不新增表、事件、持久任务、nonce、tombstone 或降级账本；复用 supervisor dirty fence/drain、SDK transport/schema admission、连续性安装与现有 ref owner |

### 15.2 本轮新增回归

- `tests/test_mcp_tool_evolution.py`：有限变换、复杂引用反例、default/执行字段的精确诊断、输出契约、降级/缺席/回滚/冷启动、完整 inspect 越界隔离、跨 server 的原生例外预算优先、目录选中 variant 去重/清除、超过 1,024 次版本变化的 ref 回收及迟到 ABA settlement/FULL。
- `tests/test_mcp_tool_evolution_integration.py`：官方 SDK 本地 HTTP 服务、通知刷新、真实 PostgreSQL canonical history、kernel/compiler/continuity 与 Chat Completions/Responses 最终 adapter wire。覆盖兼容 native、冻结入口拒绝新参数、native→meta、重复使用合法 ref、旧 ref 本地拒绝后重新 inspect、新工具发现、工具移除及每个相邻实际 wire payload 的前缀断言。
- 同一真实链路覆盖 preflight 安装前失败→物理 runtime C→回滚 B 仍 DIRECT→再次采用 C 后 META；独立 SDK 测试暂停 tools/list，验证 READY 期间 source 不完整时 catalog 与 server inspect 均报告未知计数。
- 既有 MCP transport/schema/权限/取消/结算/阈值、能力分页、目录最终 wire 预算和前缀连续性回归继续运行；未删除或放宽这些边界断言。

### 15.3 同一 Astra high 的代码交叉审阅

由原文档审阅者 `/root/mcp_evolution_review_astra` 进行实现后的独立只读审阅，主代理检查并修订后，同一个子代理再次复查。审阅发现与修订：

1. 去掉空 schema/boolean 的过宽快捷判定，执行字段变化不能被误放行为兼容；未证明的 default/dialect/pattern 等变化使用 UNCONFIRMED，包含新增属性接管旧额外属性 schema 的路径。
2. 目录完整性读取计划中的 source disposition，修正刷新在途时 READY 被误当完整空目录；server inspect 同步该视图。
3. 全局先填各 server 的原生例外，再填 META 名称，防止先处理的 server 挤掉后续原生停用说明。
4. 明确物理 runtime 安装与 epoch typed-plan 安装的区别，并用失败安装回归固定 X26；无需新增状态所有者。

最终复查结论：上述发现均闭合，当前审阅范围没有剩余阻塞 finding。完整最终树回归结果由主代理另行核验；审阅不替代测试。未进行真实模型或外部 MCP 业务实测，未修改保存的生产配置/会话，未触发 Actions。

### 15.4 最终验证结果

2026-10-21，在仓库根 `.venv` 中执行：

```sh
.venv/bin/pytest -q tests/test_mcp_*.py tests/test_capability_source_query.py tests/test_catalog_final_wire_resources.py tests/test_round9_unified_capability_semantics.py tests/test_round6_mcp_production.py tests/test_round3_structured_model_input_compiler.py tests/test_round3_1_provider_input_prefix_continuity.py --disable-warnings --maxfail=1
.venv/bin/ruff check src tests --output-format concise
git diff --check
```

结果：**664 passed**（112.19 秒）；Ruff 全部通过，差异格式检查通过。集成测试使用本地独立创建并清理的测试数据库和官方 SDK 本地服务，provider 推理由可控测试端提供；没有调用真实模型或 Firecrawl 业务接口。

## 16. MCP 提示词减负修订

2026-10-21，用户要求减少认知负担，对系统 MCP 段、list/inspect/use 三个工具说明、catalog 字段说明、Skill 重复提示及不可用/引用失效文案做删改；不变更参数结构、执行路由、授权或引用生命周期。

- 删掉旧“同名工具在列表里就直接调用”的冲突。inspect 的返回方式决定调用方法，META 使用新参数及引用，DIRECT 使用列出的参数。
- 说明有效引用可连续复用，被拒绝后重新 inspect；不要求每次 inspect 或无目的地先遍历全目录。
- 删除 provider prefix、epoch、same-schema、快照采用等执行内部细节；保留未发送事实、具体 inspect target、可用性理由与不重复未知结果操作的要求。
- 系统 MCP 段、三项工具主说明与目录说明这五处文本，以 Pulsara 启发式算法合计约 **908 → 492 token**，减少约 **46%**。这不是整个 system/tools 或完整请求的 token 数，也不替代真实模型行为验证。

沿用同一个 `/root/mcp_evolution_review_astra` 只审本节提示词改动。审阅发现并修正了“调用拒绝→inspect→再次要求 inspect 自己”的恢复循环：调用拒绝给具体工具 target，inspect 的不可用诊断仅在阻塞当前任务时指向具体 MCP_SERVER target。旧入口不可用的文案不再断言输入参数发生变化，可覆盖输出变化及兼容性未确认。最终提示词复查无剩余阻塞项。上一节的 664 项是生产实现阶段的验证记录；本轮相关回归另行记录。

本轮验证：MCP 失败指引、演进单测及真实链路、能力查询、Round 9/6、编译器与目录 wire 预算共 **343 passed**（48.93 秒）；按用户指定的最终中性文案更新后，真实 SDK 指引回归再次 **2 passed**。Ruff 和 `git diff --check` 通过，未做真实模型实测或运行 Actions。
