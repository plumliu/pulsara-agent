# Pulsara 应用入口与能力管理边界

日期：2026-09-30。状态：用户批准的 hard cut 已实施；非真实会话测试、GPT-6.1 Sol / xhigh critic 交叉审核与真实 Pulsara dogfood 均已通过。

本文是当前已实施的产品边界，以根目录 [AGENTS.md](AGENTS.md) 为约束。第 2 节记录实施前的边界问题，其余合同描述本次 hard cut 后的行为；实施与验收结果见第 12 节。

## 1. 决定

用户从任何目录运行 `pulsara app`，随后在 GUI 中创建、恢复会话并确定工作目录。用户可以直接使用 GUI 管理能力，也可以用自然语言让模型完成操作。普通使用不需要学习管理 CLI。

模型下载仓库、阅读文件、编辑 Skill 或 Hook 源码时使用 terminal 和文件工具；将来源安装为 Pulsara 能力、修改已安装实例、授予信任时，统一使用现有 `manage_capability`。它提交具体意图，由当前 Host 绑定的原生 owner 确定目标、权限、必要的用户输入和变更后的采用。补齐 loose Skill 与 Hook 的必要动作和公开观察，不增加第二个安装器或通用管理框架。

保留可工作的 `pulsara skills/plugins/mcp/hooks` CLI 作为独立操作者、维护脚本与诊断的高级接口，复用同一原生服务。它不承担运行中 Host 的控制或首方采用合同，也不是模型在正常对话中学习的管理路径。删除已确定失效的独立 `mcp reconnect`。Host CLI 已删除，不恢复。

现有 `db`、`config-check` 诊断入口继续保留，它们服务部署、数据库维护和故障诊断，不构成普通用户需要学习的能力管理流程。

这套边界减少模型需要记忆的内容：它只选择动作、USER/WORKSPACE 与来源或目标；无需重述项目目录、传播 Pulsara home、填写密钥、模拟用户同意或例行补一次 reload。

## 2. 为什么不能只看 cwd

用户提出的两层入口成立；模型运行 shell 时有合理默认 cwd，也成立。但 shell 的当前目录不能完整表示能力安装目标。

| 实施前的事实 | 对边界的影响 |
| --- | --- |
| `app` 启动 cwd 已与 GUI 会话目录分离。 | 不再改动这部分；应用启动不带项目身份。 |
| terminal 每次独立启动，省略 `workdir` 时从当前会话根启动；单次 `workdir` 或命令内 `cd` 可以改变该次 cwd，不能改变后续调用的默认根。 | 默认从根运行 CLI 时通常正确；进入下载目录后，CLI 的 cwd 不再等于安装的项目根。 |
| Skills/Plugins 的项目 CLI 可默认 cwd；MCP 不给 `--workspace` 则操作 USER；Hooks 另有源作用域。 | 模型必须记住各命令不同的缺省语义。 |
| terminal 的默认环境过滤不携带 `PULSARA_HOME`，创建 TerminalManager 时也未注入 Host 已解析的 home。 | custom-home Host 中的裸 CLI 存在操作默认 `~/.pulsara` 的风险；本轮由 typed 管理直接使用 Host 的 frozen home，保留 terminal 的原环境过滤规则。 |
| MCP/Plugin 已有原生 typed 管理、私密表单、精确当前值检查、Plugin 启用 review 与自动采用。 | 改成 terminal 主路径会让模型承担这些协调，或要求新增 Host IPC/表单桥接。 |
| standalone CLI 无运行中 Host supervisor；显式 reload 也有 ROOT/权限模式限制。 | CLI 与首方管理不是在所有角色和权限下等价的入口。 |

因此保留 terminal 的会话根默认值，同时把安装与配置目标交给 Host owner。源码位置、shell 执行位置和能力作用域各有用途，不要求模型把三者维持成同一个路径。

## 3. 目录、home 与作用域

### 3.1 人与 GUI

普通用户只需运行 `pulsara app` 启动本地应用服务和浏览器。指定目录、快速开始及恢复会话沿用现有 GUI owner；启动 cwd 不参与项目能力扫描、目录预填或会话身份。

当前仍有效的显式高级启动偏好，包括 Skill 选择、权限模式和 workspace MCP trust，沿用各自现有合同。本轮不删除这些偏好，也不重新设计 GUI 偏好持久化；它们不携带项目根、不安装能力、不替代单次 Plugin/Hook review。模型不得通过重启 app 或填启动 flags 来安装、配置、授权或采用能力。这里的“只启动应用”指 app 不负责决定会话目录及执行能力管理意图，不宣称 app 已无其他高级选项。

没有会话时，GUI 仍可管理 USER 能力。项目管理需要明确选中的会话或项目；不得用启动 cwd、用户主目录或 Pulsara home 伪造项目。

### 3.2 模型与 Host

`manage_capability` 的 `scope` 保持显式 `USER` / `WORKSPACE`。不新增 `AUTO`、持久安装偏好或隐含“全局”状态：

- `WORKSPACE` 指本次调用绑定的会话 canonical 根，由 Host 提供；工具不接受任意目标工作目录。
- `USER` 指该 Host 已解析并持有的有效 Pulsara home 及既有用户能力 owner，不从 terminal 环境重新猜测。
- 已明确“这个项目”“所有项目”或上下文已经约定作用域时，模型直接填对应值；只有真实含糊时才问一次。不得重复要求用户确认已有选择，也不得自行扩大到 USER。
- 来源用确切的本地绝对路径。来源可以位于下载目录、临时目录或其他获准位置；它不会决定安装作用域。
- 想管理另一个项目时，切换到那个项目的 GUI 会话或直接使用 GUI 管理面；不让模型通过任意路径参数给当前调用更换目标 owner。

用户安装范围与模型调用可见范围是不同概念。MCP 的 `ROOT_ONLY` / `ROOT_AND_SUBAGENTS` 继续表示谁能使用它，不表示写入 USER 或 WORKSPACE。

同一项已授权的安装、配置、启用或更新流程沿用已明确的作用域，不为每个动作再次询问。WORKSPACE 除物理根外，还须符合该能力现有 workspace kind 与来源规则；例如现有 quick/transient 会话不发现 WORKSPACE local Hook source，Plugin view 也不观察项目 slice。此范围不支持时明确失败；用户已选择 USER 时才操作 USER。不把 scratch 伪装成 project，不自动扩大 scope 或来源发现规则，不增设独立 scratch 能力类型。

### 3.3 独立 CLI

CLI 保留显式项目参数 `--workspace`：它在管理命令中是目标参数，和已经删除的 app 启动参数用途不同。本轮 hard cut 已去掉项目管理的 cwd fallback：

- 项目写操作必须提供目标 `--workspace`；缺少目标时在副作用发生前报用法错误。
- 无项目参数的列表与诊断只观察 USER/bundled 及相应用户 Plugin，不扫描 cwd 项目。带项目参数时才观察该项目与可见用户来源。
- USER 操作不需要伪项目；对已有接受 USER/WORKSPACE 选择的命令，USER 与 `--workspace` 不得同时出现。
- 相对**来源文件路径**仍可相对命令 cwd 解析；不得将这条路径规则用作隐式项目选择。
- MCP doctor 的相对 stdio 工作目录：有显式项目时用该项目；纯 USER 测试使用已解析 Pulsara home 的管理目录，沿用 GUI 无会话测试合同。返回实际测试目录；doctor 可能启动连接进程，不称为纯读取。
- 独立 CLI 不冒充活跃会话、不提供 reconnect，不自动推断 live Host 或发送采用通知。修改后通过 GUI 的刷新/现有 Host 控制路径或正常重启采用；不能承诺调用 CLI 后所有会话即时生效。

只为消除已确定的多义，将 MCP add 的 `--scope` 一次 hard cut 更名为 `--tool-visibility`，继续接受 `ROOT_ONLY` / `ROOT_AND_SUBAGENTS`，保持原缺省值与使用语义；删除旧 `--scope` 参数，不保留 alias。MCP 配置来源仍由显式 `--workspace` 决定，没有该参数时为 USER。Skills/Plugins 的 `--scope user|workspace` 继续表达安装来源，Hooks 的 `--scope` 继续筛选其来源作用域，帮助须说明各自含义。不重命名其他无关参数，不建立新的统一命令树。

独立 CLI 从自身进程的有效 home resolver 读取配置，操作者通过既有 `PULSARA_HOME` 选择非默认 home；它不继承一个未知 live Host 的 home。模型主路径补齐后，无需为能力管理向通用 terminal 增添 home 注入或重新放行被过滤的环境变量，也不要求模型传播 home。

## 4. capability 与 Plugin 的最小模型

管理面的“能力”是用户操作的归类，不修改运行时 `CapabilityKind`：leaf union 仍只有 `TOOL | SKILL`。Built-in 与 MCP tool 是 TOOL；Skill 是指导型 SKILL；MCP 的 resources/prompts 继续经既有标准工具访问。Hook 保持独立事件子系统，Plugin 保持包与实例，不新增 HOOK/PLUGIN leaf、generic invoke 或统一执行器。共同管理入口也不合并原有 permission、binding、transport、activation 与 canonical settlement owners。

| 概念 | 产品责任 | 现有 owner |
| --- | --- | --- |
| Skill | 可发现、按既有规则选择和读取的指令及附带文件。Loose Skill 可独立安装。 | LooseSkillDefinitionProducer、LocalSkillManagementService、既有配置/选择 owner。 |
| MCP | 连接配置、认证及远程 tools/resources/prompts 的使用。 | LocalMcpManagementService、managed credentials、MCP supervisor 与官方 SDK。 |
| Hook | 特定事件的命令声明；启用与精确定义信任共同决定可运行性。 | Hook source/parser、HookTrustStore、dispatcher/executor。 |
| Plugin | 原生包与安装实例，聚合上述组件并保有实例身份、参数、私有连接配置及 enable 状态。 | Plugin importer/validator/store/management 与组件适配器。 |

Plugin 不成为替代 capability 的权限系统。Skill/MCP/Hook 保留各自执行与信任合同；“安装 Plugin”“启用实例”“授权 MCP”“信任 Hook”不能互相代替。Plugin 内子项由该实例管理，不伪装成 loose 项独立删除，也不靠复制到其他能力目录脱离包的生命周期。

单独一个合法 Skill 仍按 loose Skill 安装，不为了统一表面强行包装 Plugin。已支持的外部 Plugin 格式交给现有 importer；模型不改写格式、不丢弃活跃组件，也不自己实现 validator。

Skill 正文与 frontmatter 仍是 untrusted instructional data，不授予工具、Hook、MCP、网络或文件执行权限，也不建立 Skill → Tool 依赖图。正文及 references/scripts/assets 按既有 catalog/activation 与 ordinary `read_file` 渐进读取，不增加 `load_skill`/`use_skill` 或安装时执行脚本的路径。新的管理观察只回答副本状态与操作资格，不成为第二套 Skill 激活或执行机制；优先级与完整性仍由唯一 central resolver/source owners 决定。

Plugin 的 immutable package、安装实例和 persistent data 沿用原生命周期：安装/replace 后实例为 disabled，enable review 针对确切当前包。Replace 产生新 package identity，即使命令文本相同，旧 Hook trust 也不能授权新包，需重新信任。Remove 清理既有专用凭据、保留实例 data；已借出的 consumer 持旧包完成结算，回收沿原 GC/anchor 合同，不引入新的自动清理任务。

## 5. 管理操作范围

复用现有闭合 `manage_capability` 动作联合和 call-local preparation/form/execution。只补下表缺口，保持 action-specific strict parser，不增加一个模型需要同时学习的通用 capability DSL。

| 操作 | 模型入口与范围 | 实现要求 |
| --- | --- | --- |
| 获取、检查、编辑仓库来源 | terminal/文件工具 | 沿用文件、网络、进程权限；下载成功不代表安装成功。 |
| MCP 增删改、OAuth、Plugin 安装/配置/启停/删除 | 已有 manage_capability 动作 | 保持现行首方路径和私密表单。 |
| Loose Skill 安装、启停、删除 | 同一个 manage_capability：`INSTALL_LOOSE_SKILL`、`SET_LOOSE_SKILL_ENABLED`、`REMOVE_LOOSE_SKILL` | 复用 LocalSkillManagementService、已有 Skill enable 配置与精确删除 owner。仅管理现有可管理来源；bundled 只读，Plugin 子项不可作为 loose 删除目标。 |
| Hook 文件创作/编辑 | terminal/文件工具 | 修改声明不授予 trust；请求既有刷新后在安全时点采用，不宣称任意文件写入会自动通知 Host。 |
| Hook 信任、撤销信任、启停 | 同一个 manage_capability：`TRUST_HOOK_SOURCE`、`REVOKE_HOOK_TRUST`、`SET_HOOK_SOURCE_ENABLED` | 按现有 source 级语义选择 local 或 exact Plugin source，复用 HookTrustStore 的当前定义重验证及执行 owner。 |
| 观察 Loose Skill 与 Hook 管理目标 | 同一个 manage_capability：`INSPECT_LOOSE_SKILLS`、`INSPECT_HOOK_SOURCES` | 补齐必要的模型只读接线，复用已有公开目录/来源观察与配置 owner；不建立第二套 inventory registry。 |
| 查询 MCP 状态与验证使用 | 现有 list/inspect、GUI 与管理结果的公开观察 | 保存、连接、发现和实际调用分别验证；管理结果不能代替一次真实使用。 |

所有新增动作仍必填 `scope`。`INSTALL_LOOSE_SKILL` 携带绝对 `source_path`，可携带现有安装 owner 支持的 `name` / `description` 候选修正；原始 source 不变。Skill 启停/删除携带公开观察所得的确切绝对 `skill_path`（安装副本的 `SKILL.md`），启停另带 boolean `enabled`。路径只选中现有副本，native owner 必须按 scope 验证其来源与操作资格，不能用它指定任意删除/写入目标；启停和删除的可管理根分别保持现有合同，不把“可发现”扩大为“可删除”。删除 observation 由 preparation 取得并直接传给现有删除 owner，不要求模型复制内部 identity 或新增 receipt。

Hook 写动作携带 `source_kind=LOCAL|PLUGIN`。LOCAL 的 exact source 由 scope 与 Host source provider 决定，不接收任意配置文件路径；PLUGIN 另带 `plugin_id`，由该 scope 的确切安装实例解析其既有固定 Hook 配置位置，不增加模型需要填写的配置路径。目标缺失、不完整或不再匹配时明确失败，不挑另一个 source。启停另带 boolean `enabled`。原生 preparation 持有 exact source identity、当前规范化 definitions、现有 trust subject 和必要的 package observation；模型不提供 definition digest、acceptance、执行 permit 或新的 opaque target token。

两个观察动作允许省略 target filter，并支持用上述 exact target 字段缩小观察范围。Loose Skill 观察要保留 disabled、shadowed、invalid 和不可管理项的真实状态/资格，不能把 effective winner catalog 当作完整副本列表；底层 producer 不完整时公开 unavailable/attention，不能把未观察到当作不存在。Hook 观察显示 source 的当前定义、trust、enabled、所属 Plugin 与其启用状态，复用现有 local/Plugin source inspection；不能为观察而启用 Plugin 或执行 Hook。仅公开用户决策所需的非秘密值，复用现有 pagination、单操作限制和完整性边界，不新增扫描数据库、registry、总量 cap。它们按真实只读效果接入现有权限分类，在 READ_ONLY 下可进行获准阅读；不得伪造非零 mutation effects 来套用现有写 preparation，也不能把整个 `manage_capability` 标成只读。

不增加通用包市场、自动 dependency 安装器、统一 capability 数据库、batch 工作流或后台安装 job。Loose Skill 当前安装目标已存在时继续报冲突；本次不引入 overwrite、自动 remove-then-install 或替换事务。仅要求获取/安装最新版不自动授权覆盖一个未指明的既有副本；用户已明确更新该副本或已授权精确删除再安装时，模型按当前 owner 合同继续该序列，不再重复问一次许可。删除和安装仍是分别结算的动作：删除成功后安装失败须如实报告，不承诺原子替换、自动回滚或重放。Plugin 的既有显式 replace 合同保留。

这些新增模型动作保持 ROOT-only。子 agent 可以在授权范围内准备来源并提供结果，由 ROOT 提交管理意图；不添加跨 agent 的管理身份或委托令牌。

## 6. HITL、权限与失败

模型提供公开意图和已知非秘密配置，runtime 判断物理 effects、必要输入和确认。普通可获准的安装不因“这是管理操作”再加统一确认关卡；已经授权的意图无需重复询问。工具调用挂起期间按现有 UI 收集所需输入，模型只等待真实结算。

- 密钥、OAuth 与 credential destination review 沿现有私密表单/owner；模型参数、terminal 命令、对话和诊断不得携带真实秘密。
- Plugin 启用继续要求用户提交完整组件及 credential destination review，即使权限模式允许自动写入。
- Hook 授予 trust 要展示该 source 的完整当前规范化声明，包括 event、matcher、command/commandWindows、timeout、async、status/context 设置与 source-declared environment；command 保持原字节，真实秘密继续走既有私密/脱敏边界。由用户确认后再由原 owner 重验证；模型不填写“用户已接受”，也不自行抄 digest 来替代 review。撤销与启停沿解析的 effects 和现有权限规则；enable 不自动授予 trust。
- Hook 既有 definition digest 属于跨重启语义确认边界，保留其用途；不新增文件/文档 SHA 或冗余 DTO fingerprint。Review 不宣称递归验证脚本、PATH、依赖或文件内容。
- READ_ONLY 下，模型仍无写许可；保留已有用户控制面可以接受并执行具体变更的路径，不把表单提交变成模型写许可。其他 DENY/ASK/ALLOW 继续由现有权限 owner 判定。
- 源/实例/定义在 review 期间改变则报冲突并重新观察；不悄悄替换准备目标、扩大作用域或自动重复 mutation。
- 取消与异常沿现有物理结算边界。已确定写入成功但采用不完整时，报告成功的 mutation 和 PARTIAL/PENDING 的采用；不要重新安装。副作用不明时保留该事实，先查询目标而不是自动重发。

保留 standalone CLI 并不意味着模型可以用 `--yes`、Hook trust 或 raw managed-state edits 绕过这条受支持的首方路径。Bundled installer 指令须明确这一点。这里定义支持的产品流程，不宣称为同一 OS 用户的任意 shell 构造了安全隔离；删除 CLI 也不能产生这样的隔离。不为此增加 shell 字符串拦截器、Host IPC 或新的鉴权体系。

## 7. 写入、采用与 provider prefix

沿用现有 owner 与状态区分：

1. 原生服务完成文件/配置/实例的 mutation，或明确未写入、冲突、结果不明。
2. 首方调用通知已有受影响 live session，当前 Host 在既有安全时点尝试采用；USER 影响既有用户范围，WORKSPACE 只标记同根会话。标记仍是进程内 best-effort，不新增 durable job、回执或跨重启恢复。
3. 模型按实际 catalog/route 发现并使用能力。保存配置、MCP 连通、发现工具和完成代表调用分别报告，不互相冒充成功。

新增 Skill/Hook 动作接入这个首方采用 owner，不让模型例行补 `reload_capabilities` 或 `reload_hooks`。保留显式 reload 用于独立管理变更、诊断或已报告的采用问题；不能在不满足现有权限的模式下要求模型补调用。

Hook 的现有采用缺口必须在本次接线中闭合：当前 `reload_capabilities` 只重新观察并发布 Plugin Hook slice，保留旧 local slice；`reload_hooks` 才重新观察 USER/WORKSPACE local sources 与 trust。因此 Hook mutation 成功后不能仅调用当前 capability adopter 就宣称已采用。复用既有 capability refresh owner，在合法安全时点刷新 local Hook slice 和 Plugin slice，并按原 Hook publication lane、exact predecessor 检查与 Host lifecycle 发布；不新增另一套 adopter、类型队列、registry 或 durable 状态。USER/WORKSPACE 的影响范围仍按原 source identity/owner 决定。

source mutation、local Hook 发布、Plugin/Skill 发布和 MCP reload 没有跨 owner 的原子事务。某部分已经发布而后续失败时不恢复旧配置或伪称完全成功：mutation 保留真实已结算结果，adoption 返回 PARTIAL/attention，并公开确切受影响部分；其他尚未到安全时点的会话保留 PENDING。重试仅重新观察和采用当前 source，不重放 mutation。现有 in-flight Hook view、MCP consumer 与物理执行继续按各自原合同结算；信任、启停和定义更新只影响未来获准采用的事件，不回放旧事件，也不因发布新 view 扩大当前调用的执行 authority。只读观察动作不标记变更或触发采用。

来源的完整观察得到 UNAVAILABLE，是应按原 owner 发布的真实负面观察，采用后不继续执行该 source 的 stale 旧贡献；它不同于 candidate 发布前被 deadline、cancel 或 stale owner 打断，后者没有发布 successor，该 writer 保持 predecessor。不把“保留旧 view 等物理排空”解释成允许新的调用继续借用失效来源，也不把完整 unavailable 结果伪装成一次未发生的观察。

触发 Hook refresh 的管理调用沿用既有 predecessor view 合同：在自身 PreTool 之前捕获原 view，其 Pre/Permission/Post 使用同一对象；新 view 不能在这个管理调用的 PostTool 自触发。只保持原生命周期所需的引用，不新增 view registry、lease DTO 或手工引用计数。Plugin Hook 的 WORKSPACE/USER 选择也沿现有来源规则：已启用包的高优先级项目 Hook 来源存在，但其声明 invalid/unavailable、untrusted 或 Hook-disabled 时，不偷偷回退执行同名 USER 来源。Plugin 实例 disabled 后是否重新显露 USER 则由原 enabled-package composition 决定；观察结果应区分实例启用、来源遮蔽与可运行性。

当前 epoch 的 SYSTEM、provider tools 保持字节不变，messages 只追加 suffix。Skill 的既有 source 观察/读取、MCP 的 `NEW_MCP_META_ONLY` 加 inspect/use 路由、Hook 对未来事件的采用继续走原路径；更新磁盘 Skill 不重写已经发送的文本。只有新 cold epoch 和明确采用的 compaction successor 可以重建 roots。能力管理不新增 rebase 边界，不为了“立刻生效”强制 compaction 或重启对话。

## 8. 最小落地与验收

本轮以一次覆盖上述新增动作与旧默认路径的 hard cut 实施：

1. 在现有管理 preparation/execution/form 内接入 loose Skill 和 Hook owners 及两项必要只读观察；沿用绑定的 canonical workspace kind/root 与 frozen home。GUI 与模型共享业务服务、确认/私密输入和采用 owner；模型动作仍经过现有 run permission，GUI 用户控制面沿原权限合同，不要求把 GUI HTTP 路由反向包装成模型工具。
2. 同步模型 schema、strict parser、权限/effect 分类、结果、bundled installers、GUI 所需的 Hook review 与文档；删除 loose Skill 模型安装的 CLI 主路径指令，保留高级独立管理说明。只读动作单独按真实阅读效果分类，不走写确认；新增写动作的 effects 由实际 owner/storage/执行行为确定，不由模型自报。模型 schema 的新入口仅在获准 cold/compaction 边界安装，不热改旧 epoch。
3. 在既有 safe-point refresh/publication 中补上 local Hook slice 观察与采用，覆盖 Hook trust/启停及来源编辑后的显式刷新；保留 Plugin slice 与 MCP 的各自结算，结果按第 7 节报告非原子的 partial，不添加跨 owner 回滚或修复任务。
4. 删除独立 mcp reconnect；去掉管理 CLI 的项目 cwd fallback，补齐纯 USER 观察和 MCP doctor 管理目录语义；将 MCP add `--scope` hard cut 为 `--tool-visibility`。不引入旧参数 alias、双路径协商或兼容 fallback。
5. 直接复用原生验证、源码 race 检查、精确当前值守卫、物理结算与 MCP SDK。保持 canonical schema/clean-v0 和 event/subject/guard/relation/job 分类数量；若实际发现独立必需的扩展，应先修订本稿的具体产品理由，不在编码中顺带添加。

必要验证按产品行为覆盖，不复制依赖库的全套 conformance：

- 从含项目能力的任意 cwd 启动 app，GUI 会话仍采用自己选定的根；该已实现合同不回退。
- 模型先在临时下载目录准备来源，再装到 WORKSPACE：结果目标仍是绑定会话根；USER 不受 terminal cwd 或环境过滤影响。用 injected custom-home 验证目标一致，无需读取或改写生产秘密。
- 同名 Skill shadowing、已存在冲突、精确删除、bundled/Plugin 子项边界沿原规则；删除高优先级项后的实际 winner 如实可观察。
- 两项新增只读观察来自 source owners，涵盖可观察的 disabled/shadowed/invalid 副本或 Hook source，并准确说明完整性与管理资格；READ_ONLY 可读而不获得写 authority。quick/transient 不扩大原 WORKSPACE Hook/Plugin 来源规则。
- READ_ONLY 用户控制面、ASK、ALLOW，以及 ROOT/child 分工符合合同；Plugin enable 和 Hook trust 的用户 review 不能由模型参数代替，review 后目标变化被拒绝。
- USER/WORKSPACE 首方采用只标记应受影响的 live session；分别覆盖 local Hook trust/enable 与 Plugin Hook slice 更新，并验证已有一部分发布后另一部分失败、取消时的 PARTIAL/attention，不回滚或重放 mutation。当前 epoch exact SYSTEM/tools 与前序 messages 保持原值，新 MCP 的 meta 路由和后续 Skill/Hook 使用有效；旧 Hook 事件不回放。
- 项目 CLI 缺目标在副作用前失败；无项目 list/doctor 不扫描 cwd；USER stdio doctor 使用公开的管理目录，独立 reconnect 与旧 MCP `--scope` 在 parser 中不存在，`--tool-visibility` 保持原调用可见范围语义。app 的有效高级启动偏好、db/config-check 诊断入口继续可用。

按 AGENTS.md 使用仓库 `.venv`/uv 完成聚焦检查，并进行一条真实 GUI dogfood：让模型准备来源、完成安装、处理实际需要的 HITL、验证一次能力使用。仅在用户授权实现后运行；使用保存的生产配置作为只读输入，保留必要失败详情，排除真实凭据。不要求额外来源证明哈希、安装收据或总量限制。

## 9. 与其他方案的取舍

| 方案 | 模型负担与实施代价 | 本稿决定 |
| --- | --- | --- |
| 所有管理都让模型调用 CLI | 表面只用 terminal，实际需掌握不同 scope/cwd、有效 home、秘密/HITL、live reload；要等价保留现有合同就需桥接 Host。 | 不采用为模型主路径。 |
| 在既有 typed 管理补最少缺口，terminal 准备来源 | 已有绑定、表单、业务 owner 和采用路径可直接复用；新增必要 Skill/Hook 动作。 | 采用。 |
| 删除所有管理 CLI，仅 GUI/typed | 可以减少独立适配器，但同时取消离线脚本与诊断管理；本次用户尚未要求放弃这些行为。 | 保留可工作的高级适配器，精简其失效项与隐式项目语义。 |
| 为 CLI 增加自动会话发现或 Host 管理桥 | 新增身份、请求路由、表单和失败生命周期，仅为重获现有 typed owner 的能力。 | 不采用。 |

## 10. 核对过的现行来源

这些是当前代码/现行合同的依据；archived 文档不作为恢复旧行为的授权：

- [GUI 启动目录 hard cut](PULSARA_GUI_WORKING_DIRECTORY_HARD_CUT_SPEC.zh.md) 与 [CLI](src/pulsara_agent/cli.py)。
- [terminal 认知减法现行合同](PULSARA_TERMINAL_MODEL_CALL_COGNITIVE_SUBTRACTION_SPEC.zh.md)、[TerminalManager](src/pulsara_agent/terminal_process/manager.py)、[environment owner](src/pulsara_agent/terminal_process/environment.py)。
- [闭合管理 intent](src/pulsara_agent/capability/management_intent.py)、[Host preparation](src/pulsara_agent/conversation_kernel/capability_management.py)、[call-local form/execution](src/pulsara_agent/conversation_kernel/capability_management_execution.py)、[tool runtime](src/pulsara_agent/conversation_kernel/tool_runtime.py) 与 [权限 owner](src/pulsara_agent/tool_permission.py)。
- [LocalSkillManagementService](src/pulsara_agent/capability/local_skill_management.py)、[Loose Skill source owner](src/pulsara_agent/capability/local_skills.py)、[GUI capability operations](src/pulsara_agent/web_app/session_controller.py)、[HookTrustStore](src/pulsara_agent/hooks/trust.py)、[Hook source owner](src/pulsara_agent/hooks/source.py)、[Hook dispatcher publication](src/pulsara_agent/hooks/dispatcher.py) 与 [Host 采用](src/pulsara_agent/conversation_kernel/host.py)。
- [Skill installer](src/pulsara_agent/bundled_skills/pulsara-skill-installer/SKILL.md)、[MCP installer](src/pulsara_agent/bundled_skills/pulsara-mcp-installer/SKILL.md)、[Plugin installer](src/pulsara_agent/bundled_skills/pulsara-plugin-installer/SKILL.md)。

## 11. 历史设计对照

补读了以下 ROUND 系列中与本次边界相关的定义、管理、权限、信任、发布和生命周期章节，并与当前 production owners 核对：

| 历史设计 | 本稿继续保留的取舍 |
| --- | --- |
| [Round 6 MCP](archived_docs/ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md) | Host 拥有连接与 request-scoped execution，发现不等于授权，reconnect 只服务未来调用；独立配置编辑不冒充 active Host reconnect。 |
| [Round 9 统一能力](archived_docs/ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md) | 统一语义发现与 exposure planning，不统一执行器和权限；closed TOOL/SKILL leaf、Hook/Plugin 的独立角色及 append-only prefix。 |
| [Round 9.1 Skill](archived_docs/ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md) | 一个 portable parser、来源 producer、central resolver 与渐进读取路径；Skill 内容不获得执行 authority，Plugin Skill 不物化到 loose roots。 |
| [Round 9.2 Hook](archived_docs/ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md) | 用户信任完整 normalized declarations；未来事件采用与 predecessor view 结算；trust 不递归证明脚本或依赖。 |
| [Round 9.3 Plugin](archived_docs/ROUND_9_3_AGENT_PLUGIN_BUNDLE_AND_HOOK_ADAPTER_IMPLEMENTATION_SPEC.zh.md) | 一个包生命周期，接入既有 Skill/MCP/Hook consumers；disabled 安装、exact enable review、replace 后重新信任、data 保留及旧 consumer 排空。 |

历史中的“模型主要调用 CLI、没有 provider-visible 管理工具”已被后续能力页 hard cut 与当前 `manage_capability` 取代，不能据此恢复旧入口。旧 tool-surface 自动 reset/rebase、冗余 fingerprint/activation SHA、固定旧 bundled 清单与数量 oracle、旧环境凭据路径等，也不作为本次实现要求。完整会话来源规则以当前代码/现行规格为准；新增无项目 USER 管理观察不恢复 cwd 伪项目。第 4、6、7 节已明确这些保留语义，本轮实现改变管理接线，不重写底层能力系统。

## 12. 实施与验收结果

现有闭合管理工具已接入第 5 节的八项 loose Skill / Hook 动作；模型、GUI 与高级 CLI 复用原生来源、配置、信任及公开观察 owners。Hook 信任使用完整定义审阅和私密表单布尔值，模型不能提供同意或摘要。查询按实际只读效果分类。首方采用补齐 local Hook slice，并分别报告各 owner 的结果；没有新增表、事件、registry 或 provider rebase 边界。

先完成全部非真实会话测试，再由 GPT-6.1 Sol / xhigh critic 交叉审核。四项首轮阻塞涉及已采用 MCP 的取消结算、Skill 发布前取消、Hook composition 不可用观察，以及 Plugin Hook 部分采用的失败定位；均已修复并补回归。复审发现的“先超时、join 时再取消”标记遗漏也已修复。最终复审无阻塞。

最终检查：Python `pytest -q -n 4 -m 'not retrieval_live'` 为 **2,395 passed**；前端为 **581 passed**。Ruff、编译检查、协议生成检查、TypeScript、前端构建和本地静态产物构建通过；ESLint 无错误，保留 5 项既有 warning。边界故障注入测试为 29 项。

真实测试复用了只读保存的生产配置：本机 PostgreSQL `localhost:5432/pulsara`，GPT-6 Luna / xhigh。全局 `pulsara app` 从 `/tmp` 启动，会话根为 `/Users/plumliu/Desktop/test1/pulsara-boundary-dogfood`。原生目录选择器的自动化连接超时，因此本次目录绑定通过现有 GUI 后端 create-session API 完成；对话、工具调用与审阅均在 GUI 中完成。

- 模型准备 `source/boundary-probe`，通过 `INSTALL_LOOSE_SKILL` 安装到绑定项目根。结果为 `APPLIED` / `RELOADED`，检查 enabled/effective 为 true，并从安装副本读到 `BOUNDARY_SKILL_OK`。
- 模型准备唯一的 PostToolUse 声明：matcher 为 `manage_capability|read_file`，命令为 `printf 'HOOK_POST\n' >> hook-events.log`，timeout 为 5，async 为 false。完整 GUI 审阅后信任成功；紧接着 terminal 检查日志为 ABSENT，随后 read_file 才产生 `HOOK_POST`，验证没有管理调用自身的 PostTool 自触发。
- Skill 禁用后 enabled/effective 为 false；Hook 禁用后保留信任记录、公开状态为 DISABLED。后续读取前后日志均为 4 行，没有新增执行。Skill 再启用后重新有效。
- dogfood 发现侧栏依赖旧缓存 `adoption.pending`，模型变更结束后仍显示旧状态。已沿原读取 owner 修复运行结束时的刷新条件并补界面回归；critic 复审通过，真实重新启用后侧栏无需刷新页面自动更新。
- 最后一轮 admitted permission 的 requested/effective 均为 READ_ONLY，两项 inspection 均 SUCCESS / OBSERVED，没有写入或信任表单。

测试中模型首次信任误传 `source_path`，被闭合 schema 拒绝后改用绑定来源成功；没有放宽 parser 或恢复任意路径目标。Skill installer 的参考文档也已同步为首方 inspection/read 路线。测试 Skill 保持启用、测试 Hook 保持禁用；会话和测试文件保留在隔离目录便于查看。

## 13. 内置能力 Skill 说明复核

四项内置说明（Skill installer、Skill creator、MCP installer、Plugin installer）已按当前闭合 parser 和原生 owner 对齐。主说明提供最小合法调用、来源与安装路径的区别、固定 USER/WORKSPACE 目标、私密表单及变更/采用结果；普通安装不再要求模型重复 CLI 验证或审计全部脚本。转换、原生校验和完整信任审阅分别由 importer、validator 和 Host 承担，较深参考文档只用于具体诊断。LOCAL Hook 已加入 Plugin installer 的发现描述与主说明，并新增简短来源管理参考；Hook 调用明确禁止 `source_path`、模型提供的 digest 和同意字段。

新增的 10 个管理示例通过工具 schema 与闭合 parser，2 个 Hook 配置示例通过原生解析；相关 79 项测试通过。GPT-6.1 Sol / xhigh critic 最终文案复审无阻塞。

使用新 GUI 冷会话 `session:84142103e9d246af8a0f741c0fdfe0c6`、GPT-6 Luna / xhigh，以自然语言要求检查项目 Hook、撤销信任、重新发起完整审阅并保持禁用，没有提示动作名或字段。模型仅读取 Plugin installer 主说明一次，随后依次调用 `INSPECT_HOOK_SOURCES`、`REVOKE_HOOK_TRUST`、`TRUST_HOOK_SOURCE`，全部 SUCCESS；完整 GUI 审阅后信任变更 APPLIED / RELOADED，来源仍为 DISABLED。没有 CLI、实现源码查询、重复采用或错误参数。这验证了普通 LOCAL Hook 路径的说明足够；不据此宣称全部复杂导入场景都已验证。

`INSTALL_PLUGIN` 成功安装或替换后，管理结果的 `identity.plugin_id` 直接取自原生 `SuccessfulPluginInstallOutcome.identity`，与顶层 `scope` 一同供后续配置、启用和删除使用。模型无需再从 manifest 推导操作目标；失败或冲突不伪称成功安装的身份。不新增查询、持久状态或权限，安装后 disabled、审阅与采用语义均沿用原合同。
