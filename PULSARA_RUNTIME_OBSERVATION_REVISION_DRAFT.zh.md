# Pulsara Runtime Observation 修订草案

状态：讨论草案，主体未实施。当前代码已修复同批用户输入的 Skill 引用收集，并删除旧 16 项激活限制；其余目录、提示与交接修订仍待实施。本文件确定 runtime observation 的公共展示形状、Skill 与 MCP 变化通知，以及本轮运行信息、规划状态、已批准计划的发送与压缩交接。Hook 附加文字使用[独立消息草案](PULSARA_HOOK_CONTEXT_MESSAGE_REVISION_DRAFT.zh.md)，不再作为本外壳的来源；Plugin、记忆及其他来源的具体正文和发送规则后续逐类讨论，不在此推定。

## 1. 公共模型展示形状

### 1.1 保留 source／body，删除公开 presence

保留统一的 pulsara_runtime_observation 外壳，只公开 source 和 body。删除模型可见的 presence，不用新的状态字段替代。body 是对象数组，每项用自然语言 text 和可选 target 表达信息：

```json
{
  "pulsara_runtime_observation": {
    "source": "SKILL_CATALOG_CHANGES",
    "body": [
      {
        "text": "新增技能 review：检查代码中的问题。",
        "target": {
          "kind": "SKILL",
          "skill_path": "/project/.pulsara/skills/review/SKILL.md"
        }
      }
    ]
  }
}
```

| 字段 | 模型可见含义 |
| --- | --- |
| source | 本次说明的来源；已确定 SKILL_CATALOG_CHANGES、ACTIVE_SKILL，第 6 节的 TURN_CONTEXT、PLAN_MODE、APPROVED_PLAN，以及第 7 节的 MCP_CATALOG_CHANGES |
| body | 集中呈现本次说明的对象数组 |
| body[].text | 自然语言内容，直接说明发生了什么、必要原因及下一步引导 |
| body[].target | 需要后续操作时附带的已有 Target；不需要时省略 |

这里是给模型阅读的说明，不是审计记录或程序 SDK 结果。不使用工具 content 的 type/value，不新增操作枚举、状态矩阵、变化 ID、版本标签或回执。Target 仍采用既定形状；Skill 使用 kind:SKILL、skill_path，不在对象旁再重复裸路径。body 的 text 直接表达空、失败或变化，不把正文包成另一层 JSON 字符串。

runtime observation 继续由原运行时来源投影为模型消息，不因 role=user 就成为新人类指令。内部必要的来源与控制事实仍由原 owner 保存，不从公开文字反推执行权限。

### 1.2 内部状态与公开文字

内部仍需分清“本次没有新变化”“已确认当前为空”“本次无法可靠读取”，以便正确去重、继承和报告失败。这不要求向模型公开 VALUE／CLEARED／UNAVAILABLE，也不要求为保留旧枚举而保留整套旧展示状态结构。

确有需要时直接说明，例如“当前没有可用技能。”或“当前无法获取技能目录。”；没有变化时不发送空通知。无法读取不能被描述为全部技能已删除，移除或禁用也不能靠一条空 body 让模型猜测。

修改公开投影时同步删除旧 presence 字段及其专属格式验证、测试和读取依赖。仍有独立用途的内部事实沿现有 typed owner 读取；随旧 Skill 正文恢复路径退出的反向解析器一起删除，不为自然语言再建设 decoder，不另存一份控制正文或新增状态注册表。

### 1.3 发送入口与 Runtime 交接的边界

pulsara_runtime_observation 是运行时补充信息的统一载体，允许在以下三个位置发送：

| 入口 | 发送位置与范围 |
| --- | --- |
| 所有新 turn 首次模型输入 | 必须提供本 turn 的环境、时间和冻结权限；有筛选后的相关净变化时，也发送 Skill／MCP 通知。用户输入、定时任务、子代理启动及 Plan 自动续接等入口遵循同一规则 |
| 用户主动 steer／补充输入 | 随本次实际采用的真实用户输入，按各 source 自身规则附带通知；不因为历史消息或当前 turn 最初由用户开启而触发 |
| 活动 turn 压缩后继续 | 在压缩后的恢复位置，与 TODO／PCA 并列提供当前运行信息及适用的 Plan 说明 |

Runtime 交接专指压缩后的 TODO／PCA（process／cell／agent）状态交接。待读 Skill 清单和 runtime observation 在同一恢复位置与它并列；Plan 自动续接属于新 turn 的开始，不称为 Runtime 交接。

上述入口不要求重发全部 source；没有本次需要交付的内容时不发空块。普通工具续跑、后台事件或能力变化本身不单独触发 observation。完整初始 Skill 目录仍是第 2 节的上下文初始说明；Hook 执行反馈使用独立的 pulsara_hook_context。

Skill／MCP 变化通知由所有新 turn 的首次模型输入或本次新采用的用户主动 steer 触发。普通工具续跑、同一 turn 内的 Cell／Agent 等运行时补充本身不触发；它们如果实际开启了新 turn，则适用新 turn 规则。压缩恢复本身只提供运行说明，不补报能力变化历史；恢复后若同次还实际采用新的用户 steer，按该入口处理。

记忆来源保留独立触发规则：runtime 自动开启新 turn 或压缩本身不触发记忆观察；真实用户输入开启的 turn 和用户主动 steer，仍按既定用户输入记忆规则处理。扩大运行信息及能力通知的入口，不将运行时输入冒充为用户输入，也不因此重做召回。

能力通知侧只保存上次已告知的相关状态快照作为比较依据；当前真实状态继续由现有能力 owner 提供。最新净 diff 在准备本次汇报时临时计算，用于生成通知，用完即可丢弃，不另存持续更新的 diff，也不累计中间变化。

新上下文成功建立时，以当时成功观察的当前状态快照初始化比较依据；Skill 同时交付完整目录，MCP 不因此注入全量服务或工具目录。之后每个新 turn 或本次新采用的用户主动 steer 都按以下流程处理：

1. 准备汇报时，从现有 owner 取得最新已接纳的相关状态快照，与上次已告知快照比较，临时计算本次最新净 diff；尚未汇报过变化时，使用该上下文初始化的比较快照。
2. 有筛选后的相关净 diff，就据此生成本次通知，汇报当前事实，不逐次重放中间操作。
3. 输入安装成功后，用生成本次通知所依据的状态快照替换上次已告知快照，供下一次比较。
4. 没有净变化就不汇报；输入未采用、通知准备或输入安装失败时，不替换上次已告知快照。

每个新 turn 都检查，每次成功汇报后都替换比较快照；不能在比较或汇报前，先用当前状态替换上次已告知快照。真实能力状态仍随底层发现及时更新，不等待通知。

替换时使用本次通知候选已经取得、用于生成通知的那份快照，不重新读取安装时的最新状态。准备消息后又发生的变化留待下一次合格入口比较；本次只替换快照，不新增独立 diff 存储或变更队列。

本文件中的“成功进入模型上下文”以既有 provider 输入安装成功为准，并在该边界替换上次已告知快照。准备或选中候选、队列输入被采用，都不能提前视为通知已安装；也不等待模型回复后再替换，不新增送达回执。初始目录及新上下文比较快照的建立沿同一安装边界处理。

| 上次已告知状态 | 本次准备汇报时的最新状态 | 处理 |
| --- | --- | --- |
| 关闭 | 开启 | 汇报已开启，安装成功后使用本次“开启”快照替换旧快照 |
| 开启 | 开启 | 不汇报 |
| 开启 | 中间关闭又开启，最终开启 | 不汇报 |

A→B→A 没有最终净变化。不为比较增加动作日志或跨上下文变化历史；失败的上下文候选或无法可靠观察的状态不当作空集。

## 2. 初始与压缩后的完整 Skill 目录

新 session 建立上下文时，在上下文开头的能力目录区域提供当前完整有效 Skill 清单。压缩并采用新上下文时，重新取得当前目录，放在新上下文的初始能力说明中；不回放此前每次增量通知。

每项只需要：

| 内容 | 含义 |
| --- | --- |
| name | 技能名称 |
| description | 技能用途说明 |
| target | 对应 SKILL Target，指向实际说明文件 |

目录不包含全部技能正文，也不列所有同名副本；按现有来源解析、启用设置和同名选择，给出当前有效清单。条目用名字、描述及完整 Target 表达，不再单列裸 skill_path。初始目录的纯文本呈现示意：

```text
当前可用技能：

review：检查代码中的问题。
{"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"}
```

完整目录是新上下文的初始说明，SKILL_CATALOG_CHANGES 负责之后的变化，不在每轮或每次变化后重发全量目录。当前有效目录确认为空时可以给空清单及一句说明；观察失败则明确不可用，不以空清单冒充完整结果。

成功进入模型上下文的完整目录同时成为后续增量通知的比较基线；仅准备候选、未采用或失败时不推进。压缩后的新上下文若只收到“目录不可用”，不能继续依赖已退出上下文的旧目录基线。恢复后在下一个新 turn 首次输入或用户主动 steer 入口补齐全部当前可用条目，说明“当前恢复可用”，不伪称它们都是新增；成功交付后再从该目录计算净增量。空闲压缩尚未开始实际模型交互时，不提前推进模型已知目录。

同一 epoch 内已安装的 SYSTEM、provider tools 保持字节一致，messages 仅追加。初始目录只在新 session／cold 上下文或明确采用的 compaction successor 中建立；平时变化追加通知，不回改上下文开头或历史消息。不为刷新目录增加新的前缀重建边界。

## 3. 平时只发送 Skill 增量变化

### 3.1 发送时机与比较范围

将原 SKILL_CATALOG 的自动全量变化快照收敛为 SKILL_CATALOG_CHANGES。在所有新 turn 首次模型输入或本次新采用的用户主动 steer 中，按第 1.3 节在准备汇报时临时计算相对上次已告知快照的最新净 diff，集中报告相关变化，输入安装成功后用生成本次通知所依据的快照替换旧快照；不由 runtime 为能力变化主动制造 steer，也不因目录变化唤醒空闲模型。同一批输入按当前最终状态形成一次通知，不按每条排队输入重放变化过程。

工具结果后的普通续跑不单独推送 Skill 目录变化。模型自己的管理调用仍直接收到实际工具结果；真实能力采用、启停和权限不等待变化通知才生效。

两次告知之间启停后恢复原状时，不发送通知；只报告准备本次汇报时的最终净变化，不承诺逐次动作历史。沿现有来源和 session owner 比较实际状态，不新增持久变化日志、投递 ACK 或永久快照表。只有确知用户操作来源时才写“用户新增／停用”，不能把普通文件扫描或模型操作一律归因于用户。

### 3.2 变化正文

同一次的新增、启用、禁用、移除和更新集中在一个 body 数组中，以自然语言表达，不公开 action 字段或操作枚举：

| 变化 | 内容 |
| --- | --- |
| 新增／启用 | 名称、用途说明和当前 SKILL Target |
| 禁用 | 名称、SKILL Target，并说明不要继续读取或使用该技能 |
| 移除 | 名称和移除说明，不附已移除对象的 Target |
| 描述、路径或同名选中副本改变 | 说明更新，附当前名称、必要描述和 Target，不让模型继续依赖旧目录信息 |

名称、描述、路径、选中副本及可用性均未改变，只有说明正文变化时，不发送 Skill 变化通知，也不增加通知专用的正文追踪。正文由模型调用 read_file 时取得；压缩后的既定强制重读规则保持。

例如：

```json
{
  "pulsara_runtime_observation": {
    "source": "SKILL_CATALOG_CHANGES",
    "body": [
      {
        "text": "已启用技能 review：检查代码中的问题。",
        "target": {
          "kind": "SKILL",
          "skill_path": "/project/.pulsara/skills/review/SKILL.md"
        }
      },
      {
        "text": "技能 old-review 已移除。"
      },
      {
        "text": "技能 draft-helper 已禁用，请勿继续读取或使用该技能。",
        "target": {
          "kind": "SKILL",
          "skill_path": "/project/.pulsara/skills/draft-helper/SKILL.md"
        }
      }
    ]
  }
}
```

禁用说明是 Skill 使用引导，不为通用 PATH 读取或终端新增路径权限规则。配置或来源观察失败时写实际原因，不推断移除，也不据此重新启用技能。

## 4. Slash 激活只发送提示

ACTIVE_SKILL 保留为 runtime observation，但不再自动注入技能正文，也不公开 body 内的 reason 等激活枚举。显式激活只认用户输入中的 slash Skill 引用，例如 /review；普通名称和自然语言描述不算，skill:name 等额外文本语法也不作为显式激活。

Host 预配置的 active_skill_names 不能被当成用户通过 slash 选择的证据。前端 slash 引用和后端解析沿现有输入、技能来源和引用排除规则收敛，不新增激活资格登记或自然语言意图推断器。

提示必须明确优先使用 read_file 读取说明，然后再继续相关工作：

```json
{
  "pulsara_runtime_observation": {
    "source": "ACTIVE_SKILL",
    "body": [
      {
        "text": "用户通过 slash 指定使用 review 技能。优先使用 read_file 工具读取该技能说明；必须先完成阅读，再继续相关工作。",
        "target": {
          "kind": "SKILL",
          "skill_path": "/project/.pulsara/skills/review/SKILL.md"
        }
      }
    ]
  }
}
```

同次指定多个技能时放在同一 body 数组中。提示表达本次用户输入指定的技能，不成为永久用户偏好，不要求每次工具续跑重复发送。没有 slash 激活时不发空激活通知。

同批实际采用的各条用户消息／steer 均按原顺序收集引用，不能只解析最后一条；首次调用前还有原始用户输入时，也一并保留其引用。各正文和批注评论保持原有独立编辑面的 Markdown 排除边界，不拼成一段文字后重新判断；尚未采用的队列项、runtime 材料和历史消息不借本批重新激活。

删除旧“同时最多激活 16 个 Skill”的数量限制及对应生产、继承校验，不设替代数量限制。旧自动全文注入及其专用正文预算随该路径退出；短提示仍按真实完整输入进行实际准入，不用旧全文载体的配额拒绝合法引用。

SKILL Target 用于定位技能，模型取其中 skill_path 构造 PATH Target 调用 read_file；不要求为 read_file 增加 SKILL 参数分支。指定技能已删除、禁用或无法读取时，在 text 中说明具体事实，不假装说明已读或技能已可用；不返回虚构 Target。

## 5. 读过的技能纳入 TODO／PCA 旁的压缩交接

### 5.1 纳入条件

活动 turn 压缩续接时，凡经 read_file 成功读取过的合法 Skill 说明文件，都纳入压缩交接的待读技能集合，不限于 slash 激活或用户指定的技能。已结束 turn 的空闲压缩不生成 Runtime 交接，也不提前发送本节的强制重读清单。六类合法来源为：

| 来源 | 识别依据 |
| --- | --- |
| 项目 .pulsara/skills | 现有项目 Skill root owner |
| 项目 .agents/skills | 现有项目共享 Skill root owner |
| 有效 Pulsara home 的 skills | require_pulsara_home 等现有 home 解析结果，不硬编码默认家目录 |
| 用户 .agents/skills | 现有用户 home／Skill root owner |
| bundled | 当前安装包的 bundled Skill producer |
| Plugin | Plugin owner 提供的实际技能说明文件，不另行扫描包根目录 |

按 read_file 解析后的实际文件路径关联现有 Skill 来源与技能事实，不仅凭文件名为 SKILL.md 或同名技能判断，也不把实际读过的副本自动替换成另一个同名副本。

原生 read_file 和 JS tools.read_file 使用同一纳入规则，JS 中未显式 console.log／return 的成功读取也算。成功的部分读取同样纳入，不要求从第 1 行开始、读完全文或完成所有原生 NEXT；失败且没有读到说明时不假称已读。终端 cat 等读取方式不分析、不追踪。

去重并承接已有待读集合，跨多次压缩继续保留仍有效的技能引用；不沿用旧“只筛同 turn 的成功 FULL 原生读取”的限制。压缩时核对当前实际来源和启用状态，已确认删除、禁用的技能不再列入待读清单。来源无法观察不等于已删除，不能伪造当前有效目录或借交接恢复不可用能力。

### 5.2 交接位置与强制提示

待读技能在压缩后与 Runtime 交接（TODO／PCA）并列放置，不再作为独立的 RETAINED_SKILL_CONTEXT runtime observation，也不复制技能正文。初始完整目录和交接待读清单职责不同：前者说明当前有哪些技能，后者要求继续前先重读此前读过的技能。

示意中的小节位置与现有交接排列衔接，不新增另一份交接实体：

```text
TODO：
<当前待办>

PCA：
<当前进程、Cell 和 Agent 的交接信息>

必须先重新读取的技能：
以下技能此前已通过 read_file 读取过。继续任务前必须重新读取这些技能说明，读取时优先使用 read_file 工具。

review
{"kind":"SKILL","skill_path":"/project/.pulsara/skills/review/SKILL.md"}
```

这里的强制重读要求对所有纳入项成立，不把模型自主读取描述成“用户要求”。不靠“可能相关时再读”等弱化措辞代替要求；不新增运行时强制读完关卡、交付回执或已读资格。读取失败按正常工具错误处理，不声称已经完成重读。

### 5.3 删除旧正文保留机制

删除 RETAINED_SKILL_CONTEXT 的独立 source、旧正文复制和相关生产消费路径。原有 FULL 交付证明、历史 catalog 文本解析、offset=1／整文件匹配、旧文件 revision 核对及按正文体积挑选部分技能的机制不再适用；专门锁定这些旧规则的测试一并删除。不得继续用旧 8 项／40k 正文保留配额丢弃应交接的引用，也不另设替代数量或正文 cap。

待读事实沿现有 read_file 调用、Skill 来源 owner 与压缩交接承接，只需身份、路径和简短引导，不保留第二份技能正文，不新增永久阅读日志、正文实体、注册表或版本字段。

## 6. 本轮运行信息与 Plan observation

### 6.1 三类独立说明

每类使用独立的 pulsara_runtime_observation 块，沿第 1 节的 source／body 形状展示。body 仍为 text＋可选 target 的对象数组；不把运行信息散写在外壳外，也不为环境、权限、时间各保留一个 source。

| source | 正文内容 | 发送时机 |
| --- | --- | --- |
| TURN_CONTEXT | 工作区类型与根路径、时区与 UTC 偏移、本 turn 冻结的权限及操作说明、当前时间 | 每个新 turn 首次准备模型输入时发送，包含用户、定时任务、子代理及 Plan 自动续接等来源；该 turn 压缩并采用新上下文后，在 TODO／PCA 旁重新提供 |
| PLAN_MODE | 当前仍处于计划模式，应继续只读调查和规划；附进入规划前 turn A 最后一条已交付用户输入的完整原文 | 只在活动规划 turn 压缩后、TODO／PCA 旁提供，普通规划过程不单独发送 |
| APPROVED_PLAN | 本次用户批准的完整计划原文，以及结合已有进展继续、避免重复执行的提醒 | 用户批准并自动开启执行续接 turn 时发送一次；该执行 turn 压缩后在 TODO／PCA 旁承接 |

TURN_CONTEXT 的权限始终取本 turn 已冻结的实际值，不改成输入框当前选择，也不在压缩时重新决定权限。时间取本次输入准备／交接时的实际时间，环境取当前 owner 的实际信息。普通工具续跑不因时钟变化而单独追加旧 RUNTIME_CLOCK 或反复重发整块运行说明。

PLAN_MODE 引用进入规划动作之前、原 turn A 中已经进入模型上下文的最后一条真实用户消息／steer 的完整正文，统一标为“进入规划前的用户输入”。沿既有输入顺序和进入动作取得原文，不用交接摘要，不猜哪句话导致规划，也不要求 enter_plan 新增 reason 参数。没有这样的用户输入时只说明状态，不编造引文。规划 turn B 连续压缩时承接同一份进入前原文；B 内后续用户补充仍按正常顺序生效，不以旧引文覆盖后来的要求。

APPROVED_PLAN 使用本次获批调用中保存的完整 plan，不读取 Markdown 或摘要来替代，不另存第二份方案正文。正文明确提醒：“部分工作可能已经完成，请结合已有进展继续，避免重复执行。”这不声称已有步骤完成，更不自动重做工具。执行 turn 压缩后承接同一份获批内容，不构成再次批准或新的执行任务。

上述发送与交接均不受旧“历史里已有原文就跳过”的分支影响。无论原始 exit_plan 调用是否仍在近期保留窗口，APPROVED_PLAN 都按本节时机提供完整原文；原始调用和结果仍按正常历史规则保留，不为避免文字重合而删除或改写历史交互。

完整计划、提示文字、JSON 转义和实际消息封装按真实发送形状计费，不只计算 plan 裸字符串。现行单 source variant 的 1 MiB 字节上限不能沿用为 APPROVED_PLAN 的封装上限；实施时删除这份计划投影上的旧载体限额检查及同用途拒绝路径，不设替代数值，也不因封装开销截断、摘要化或缩小 exit_plan 原有的 1 MiB UTF-8 计划输入范围。最终完整请求仍遵守实际 provider 输入准入和真实资源边界；真实失败明确报告，不伪装成已完整交付。本项当前只修订文档，生产代码待实施。

### 6.2 Turn 边界与触发来源

首次成功 enter_plan 结束原 turn，自动开启冻结只读权限的规划 turn；不在原 turn 内改写权限。模型已能从该工具调用与结果得知进入规划，不再另发进入提示或持续规划提示。重复 enter_plan 保持原规划 turn。

修订、取消一次审批及十分钟超时按 [Plan 工具草案](native_tools/plan.md)保持当前只读规划 turn；通过最终工具结果和真实用户反馈表达，不另造状态通知。在上述正常审批／退出分支中，只有批准或无方案退出才结束规划 turn，并自动开启恢复原权限的后继 turn。显式 Stop、强制退出和关闭会话仍按各自既有控制语义处理。批准时附 APPROVED_PLAN；无方案退出不附获批计划，也不把旧草案当成批准内容。

runtime 自动开启规划轮、批准后的执行续接和无方案退出后的续接，都按新 turn 规则提供 TURN_CONTEXT，并可附筛选后的 Skill／MCP 净变化。它们不冒充用户输入触发 slash 激活或记忆观察。压缩恢复不重放激活和能力变化历史，也不额外触发记忆召回；真实用户消息／steer 仍沿自身规则处理。初始／压缩后的完整 Skill 目录按第 2 节建立，并据此初始化新上下文的 Skill 通知比较快照。

### 6.3 模型可见块示例

规划 turn 压缩后，本轮运行信息单独为一块；以下路径和时间仅为示意：

```json
{
  "pulsara_runtime_observation": {
    "source": "TURN_CONTEXT",
    "body": [
      {"text": "工作区类型：project；根路径：/project；时区：Asia/Shanghai；UTC 偏移：+08:00。"},
      {"text": "本 turn 权限：read-only。允许读取与调查；不允许文件修改、终端命令、进程变更及有外部效果的 MCP 调用。"},
      {"text": "当前当地时间：2026-11-14T15:00:00+08:00。"}
    ]
  }
}
```

再接独立的规划状态块；引文使用进入规划前 turn A 最后一条已交付用户输入的完整原文：

```json
{
  "pulsara_runtime_observation": {
    "source": "PLAN_MODE",
    "body": [
      {"text": "当前仍处于计划模式。请继续只读调查和规划，不执行修改。"},
      {"text": "进入规划前的用户输入：\n先进入计划模式，调查清楚后给我完整方案，先不要改代码。"}
    ]
  }
}
```

用户批准后的执行续接 turn 使用其实际恢复权限的 TURN_CONTEXT，并附以下独立块；plan 所在 text 项直接使用完整原文，不添加标题、不改写原有换行：

```json
{
  "pulsara_runtime_observation": {
    "source": "APPROVED_PLAN",
    "body": [
      {"text": "用户已批准以下计划。部分工作可能已经完成，请结合已有进展继续，避免重复执行。"},
      {"text": "# 文件工具改造方案\n1. 统一读取和写入的返回结构。\n2. 保留原始换行。\n3. 完成相关验证。"}
    ]
  }
}
```

执行 turn 内压缩后仍同时提供 TURN_CONTEXT 和上述完整 APPROVED_PLAN；规划 turn 内压缩后同时提供 TURN_CONTEXT 和 PLAN_MODE。两者均在 TODO／PCA 旁按独立 observation 块展示。

### 6.4 压缩后 Runtime 交接与 observation 的位置

活动 turn 压缩并继续同一 turn 时，采用后的新上下文概念排列如下；system 与 tools 是 provider 的独立输入部分：

```text
system：系统提示词
tools：当前原生工具定义

messages：
  当前完整 Skill 目录
  模型生成的压缩交接手册
  Runtime 交接：
    TODO
    PCA（process／cell／agent）
  必须优先用 read_file 重读的 Skill 清单
  TURN_CONTEXT observation 块
  PLAN_MODE 或 APPROVED_PLAN observation 块（仅相应 turn）
  按原顺序保留的近期原始消息与必要的完整工具交互
  尚待处理的新输入（如果有）
```

环境、权限和时间放在 TODO／PCA 旁的 observation 中，不放到前面的能力目录区域。普通 turn 压缩后提供 TURN_CONTEXT；规划 turn 再提供 PLAN_MODE；批准后的执行 turn 再提供完整 APPROVED_PLAN，不同时添加两种 Plan 说明。它们与 Runtime 交接并列，不改变 TODO／PCA 的内容形状。

运行说明独立于 TODO／PCA 是否非空：即使没有待办、进程、Cell、Agent 或待读 Skill，活动 turn 的压缩续接也必须提供 TURN_CONTEXT 和适用的 Plan 块，不能沿用旧“没有交接对象就不生成”的早退。

承接本 turn 需要的全部有效运行说明，不逐次重放已失效的状态、旧时钟和历史切换提示。TODO／PCA 的状态交接、待读 Skill 和 observation 各自保持职责，不为 TODO、PCA 或待读 Skill 新建 observation source。获批计划来自实际保存的原文，当前状态来自运行时事实，不依赖模型交接手册是否记全。

上述压缩恢复排列只在明确采用的活动 turn compaction successor 中建立；同一 epoch 内的后续输入只追加，不为调整说明位置重写已安装 SYSTEM、tools 或 messages 前缀。

已结束 turn 的空闲压缩只生成等待下一次输入的历史上下文，不提供 TODO／PCA Runtime 交接，也不提前附加待读 Skill 或这三类 observation 块，不因此继续任务。后续任何来源开启新 turn 时，首次模型输入都必须发送 TURN_CONTEXT，提供该新 turn 的环境、时间和冻结权限，并检查 Skill／MCP 相关净变化；记忆仍按真实用户输入的独立规则处理。不把已结束 turn 的规划状态或获批执行要求提前注入。完整 Skill 目录仍在新上下文建立时按第 2 节提供，与 Runtime 交接分开。

### 6.5 实施收敛边界

删除旧 PLAN_WORKFLOW／PLAN_HANDOFF 的独立状态、进入、修订、批准和退出提示生产消费规则，改为本节三类说明及既有工具结果／用户反馈。环境、权限、时间不再以 RUNTIME_ENVIRONMENT／RUN_PERMISSION／RUNTIME_CLOCK 三条公开 source 分散发送。

复用既有 Plan owner、turn 权限快照、续接关联和完整方案提取；APPROVED_PLAN 承载原文后，不在同一模型投影位置再并列另一套规划续接控制 JSON 或批准短提示。内部必要的模式切换、审批、原文关联与执行结算不因此删除，也不新增正文实体、永久 observation 日志、资格登记或自动重做机制。

旧 PIN_EXISTING_CANONICAL_BLOCK／MATERIALIZE_REFERENCED_BLOCK 对公开原文注入的选择不再决定是否产生 APPROVED_PLAN；保留从既有关联提取精确获批原文的能力。同步清理旧 source 字节限额对这份完整计划的限制和相关预算估算，使用完整实际封装计量，不因更换展示载体引入更小的合法计划范围。其他来源的独立边界不在本项机械删除范围内。

## 7. MCP 变化通知

### 7.1 发送时机与范围

将旧 MCP_CATALOG 全量目录摘要收敛为 MCP_CATALOG_CHANGES，沿第 1 节的 source／body 形状，只报告相对上次告知的实际净变化。通知使用现有 session 可观察、可使用范围内的服务与工具事实；独立及 Plugin 提供的 MCP 服务遵循同一规则，不借本章增加 Plugin 自身的通知。

在所有新 turn 的首次模型输入，或本次新采用的用户主动 steer 中附带。新 turn 入口沿现有 turn 接纳及首次派发事实识别，不受输入来源限制；同一 turn 内的用户 steer 则沿本次采用批次中的 HUMAN_MESSAGE／HUMAN_STEER 等真实用户来源识别，不能沿用“本 turn 最初由用户开启”的历史标记。用户将排队消息转为 steer 时，复用原队列重定向及后续采用事实，不根据界面文案或“已点击发送”提前推进通知基线。尚未采用、被取消或拒绝的输入不算发送完成。同一 turn 内的 Cell／Agent 等运行时补充不触发；定时任务、子代理等实际开启新 turn 时，按新 turn 规则处理。

能力实际采用、连接准入、权限及调用引用失效继续及时按当前事实生效，不等待通知。后台发现变化不主动制造 steer、唤醒模型或额外开启 turn；模型调用管理工具后的普通续跑也不单独追加变化通知。没有净变化时不发空块，不记录或重放每次配置操作。

存在按第 7.2 节筛选后的已确认相关净变化时，必须随本次合格发送入口附带，不将其视为可静默省略的目录摘要。最终请求仍按实际输入准入处理，真实失败不冒充已成功告知。

### 7.2 通知类型、内容粒度与查询指引

复用当前配置、连接、发现及执行策略 owner 的实际事实。底层能识别变化，不等于每个变化字段都要告诉模型；通知只选择影响服务可用性、工具发现与选择、使用条件或当前调用信息是否仍适用的净变化。底层继续比较完整已接纳的定义，模型只收到归类后的说明和查询入口。

下表是通知的内容分类，不新增公开 action、状态枚举或变化字段。text 可以说明受影响工具、变化大类、已确认的影响和下一步查询，不要求逐字使用固定模板：

text 使用建议性语气：已确认的新增、停用或更新直接陈述；对后续使用的影响可写“可能影响”“可能与此前不同”，查询建议可写“如有需要”“可以”“可考虑”。不把已确认事实改写成猜测，也不将通知写成必须先查询、刷新全部引用才能继续工作的指令。是否查询及如何继续由模型结合任务判断，真实权限和引用有效性仍由执行 owner 核对。

| 通知类型 | text 示例与内容要求 | 可选查询入口 |
| --- | --- | --- |
| 服务新增 | “MCP 服务 docs 已添加并启用。如需了解它提供哪些工具，可以查询当前工具目录。”新增时仍停用则如实说明，不把安装当作连接成功 | 可用连接 Target 查询工具目录 |
| 服务移除 | “MCP 服务 docs 已移除。如需寻找其他可用服务，可以查询当前服务列表。”只报告已确认移除，不将读取失败当成移除，不附已失效 Target | 可查询服务列表 |
| 服务启用 | “MCP 服务 docs 已启用。如有需要，可以查看该服务目前有哪些可用工具。”不保证连接和目录已经就绪 | 可查工具目录或服务现状 |
| 服务停用 | “MCP 服务 docs 已停用，后续使用可能受影响。如需了解当前情况，可以查看服务记录。”附仍可定位的连接 Target | 可查询当前服务记录 |
| 服务暂不可用 | “MCP 服务 docs 当前连接失败，后续调用可能受影响。如有需要，可以查看当前服务状态。”目录尚未就绪或其他已确认不可用情况，使用对应真实说明，不展示内部连接状态矩阵 | 可查询服务记录，不要求反复查询或自动重试 |
| 服务恢复可用 | “MCP 服务 docs 已恢复可用。如果任务仍需要该服务，可以考虑继续使用。”普通重连本身不要求取得所有工具的新引用 | 需要了解现状时可按需查询 |
| 服务名称或使用说明更新 | “MCP 服务 docs 的使用说明已更新。如果后续需要参考该服务的用法，可以查看当前服务记录。”可以说明实际名称变化，不展开完整配置或说明差异 | 可查询服务列表中的当前记录及实际 instructions |
| 工具目录更新 | “MCP 服务 docs 的工具目录已更新：新增 search，lookup 不再可见。后续如需使用该服务，可以查询当前目录和相关工具详情。”可点出已确认的工具增减，不要求列出全部变动 | 可用连接 Target 查询目录，再按需查询详情 |
| 工具使用说明更新 | “MCP 服务 docs 中 search 工具的使用说明已更新。后续如需参考其用法，可以查看当前工具详情。”不把每处措辞修改展开为通知 | 可用工具 Target 查询详情 |
| 工具输入接口更新 | “MCP 服务 docs 中 search 工具的输入参数定义已更新，参数要求可能与此前不同。如需使用，可考虑查询最新工具详情，确认当前接口和调用引用。”合并参数、约束、默认值等内部差异，不逐字段讲解 | 可用工具 Target 查询详情 |
| 工具返回定义更新 | “MCP 服务 docs 中 search 工具声明的返回结构已更新，相关返回信息可能与此前不同。如需了解，可以查看最新工具详情。”包括返回 schema 的增加、移除或内容变化，不推断未声明的实际返回结构 | 可用工具 Target 查询详情 |
| 工具使用条件或可见范围更新 | “MCP 服务 docs 的工具使用条件或可见范围已调整，可能影响部分工具的使用。如有需要，可以查看当前目录或相关工具详情。”已确认具体工具时可以点名，不展开内部策略字段，不将其说成远端 schema 更新 | 可按需查询目录或工具详情 |
| 连接配置或实际目标更新 | “MCP 服务 docs 的连接配置已更新，可能影响此前的连接或调用信息。如有需要，可以查看当前服务及工具信息。”只说明影响使用的实际变化，不公开凭据或内部身份，不根据通知断言旧调用信息仍适用 | 可按需查询服务记录、目录或工具详情 |

工具改名按旧名不再可见、新名出现处理，不建立别名。暴露设置、会话范围或定义接纳失败造成的工具不可见，也按已确认的目录／可用性变化说明，不一律称为远端删除。连接过程的中间状态不逐次推送，只在合格发送入口报告相对上次已汇报状态的当前净变化。同一服务同时发生几类变化时，合并成一项 text，避免重复通知。

查询指引沿[能力查询草案](native_tools/capabilities.md)的真实参数，不向 query_capabilities 增加 action：kind 始终填 MCP；不填 target 查询服务记录，填 MCP_CONNECTION Target 查询服务内工具目录，填 MCP_TOOL Target 查询工具详情。这些是供模型按需选择的查询入口；已经持有所需工具 Target 时可以直接查详情，不强制从服务列表重新走一遍。通知不指定模型使用哪种调用方式，也不要求模型根据字段差异自行修改旧调用参数。

输入／输出 schema 的任意层级增删或值变化仍由完整已接纳事实比较，不以本表代替检测或验证；对模型归并为“输入参数定义”“声明的返回结构”或更通用的“工具信息已更新”。通知不列 JSON Schema 关键字、完整差异或兼容性分析，不要求模型理解内部字段，也不在无法确定变化影响时猜测旧参数一定不能使用。可提示查询能够取得当前详情和调用引用，供模型自行判断是否需要；既定的引用失效规则保持。

内部代次、provider 工具别名、纯展示格式、资源／模板或 Prompt 信息，以及不影响模型使用的配置细节，不单独生成通知。SDK 返回但未纳入当前定义事实的元数据，不因本节新增追踪能力。观察失败或定义未通过接纳时，只报告可以确认的可用性／目录情况，不把未知当成空 schema 或删除，也不另存原始 MCP JSON、凭据或字段变更日志。

### 7.3 通知正文与示例

body 仍按服务聚合为 text＋可选 target 对象；text 可以较具体，但不变成接口说明全文或变化清单。服务在当前查询范围内仍可定位时，复用最小 MCP_CONNECTION Target；已移除或已超出当前可见范围、无法提供可用 Target 时，只保留名称及说明。工具名称和变化类别可以直接写在 text 中，不新增结构化通知字段。

```json
{
  "pulsara_runtime_observation": {
    "source": "MCP_CATALOG_CHANGES",
    "body": [
      {
        "text": "MCP 服务 docs 已添加并启用。如需了解它提供哪些工具，可以用所附连接 Target 调用 query_capabilities，设置 kind 为 MCP，查看当前工具目录。",
        "target": {
          "kind": "MCP_CONNECTION",
          "config_path": "/project/.pulsara/mcp.yaml",
          "server_id": "docs"
        }
      },
      {
        "text": "MCP 服务 archive 中 lookup 工具的输入参数定义已更新，参数要求可能与此前不同。如需使用，可考虑通过 query_capabilities 的 MCP 分支查看最新工具详情，确认当前接口和调用引用。",
        "target": {
          "kind": "MCP_CONNECTION",
          "config_path": "/project/.pulsara/mcp.yaml",
          "server_id": "archive"
        }
      },
      {"text": "MCP 服务 old-docs 已移除。"}
    ]
  }
}
```

只有确认发生了对应变化才采用相应文案；具体变化不明确时，可写“工具目录或定义已更新，可能影响此前掌握的信息；如有需要，可以查询当前详情”，不凭推测填上新增工具、参数变化或返回变化。不能确认是谁修改时，不把所有变化归因为“用户修改”。通知是否需要发送仍按第 7.1 节执行，建议性语气不改变实际通知义务或执行规则。

### 7.4 比较快照、发现边界与删除范围

在现有进程内 owner 中只保存上次已告知的相关状态快照。每个新 turn 的首次模型输入或本次新采用的用户主动 steer，都按第 1.3 节在准备汇报时，与最新已接纳的相关状态快照比较并临时计算净 diff，有相关净变化才通知。输入安装成功后，用生成本次通知所依据的快照替换上次已告知快照；diff 用完即可丢弃，不用安装时重新读取的最新状态替换。新上下文首次成功观察时，先用该状态快照初始化比较依据，不编造此前的新增历史，也不声称模型已收到整份 MCP 目录。输入未采用、通知准备或输入安装失败时不替换快照。观察失败如实说明暂时无法确认，不据此清空已知工具集合。复用现有上下文采用、turn 接纳与输入安装边界，不新增持久变更日志、版本字段、通知回执、资格登记或另一套刷新器。

只比较本次准备汇报时的最终净变化：例如已告知 docs 为关闭，期间反复打开、关闭，准备汇报时仍关闭，就不发送任何启停通知；最终开启则只说明当前已开启，安装成功后使用本次“开启”快照替换旧快照。工具定义 A→B→A、目录或其他相关状态恢复原值时同样不重放中间变化。若最终相关状态与上次已告知快照相同，无需通知，也不额外维护中间变更清单。

比较依据不能只是最终提示文字。例如前后两次都写“工具目录或定义已更新”，但准备本次汇报时，实际影响调用信息的定义相对上次已告知状态又有净变化，本次仍须通知。比较相关服务状态和当前范围内的工具定义／策略后，按第 7.2 节筛选及聚合；不把所有底层字段差异直接当作必须逐项告知的清单。资源、资源模板、Prompt 的数量或定义变化，不单独触发工具更新通知。聚合 runtime 代次、provider 工具别名和内部目录重建也不单独构成模型需要知道的变化。

继续复用 SDK 的 list_changed 通知、已有完整刷新、已配置周期刷新和重连发现。list_changed 只表示需要重新观察，并不携带具体 schema 差异；完整刷新并接纳事实后才判断实际变化。服务未通知、尚未刷新或变化发生在观察之后时，不承诺提前知道。单次参数拒绝、isError 或返回值不符合 outputSchema，也不能单独证明工具定义改变；保持已有错误及是否派发的真实说明，不自动重做业务。

压缩本身不发送 MCP 变化通知，也不为通知注入全量 MCP 目录或远端 schema。压缩并成功采用新上下文时，以当前成功观察的状态快照初始化新上下文的比较依据，不跨上下文补报此前尚未发出的变化。若重建时无法可靠观察，不用空集伪造快照；后续首次成功观察时再初始化。仅准备但未采用的压缩候选不改变现有快照。Plan 自动续接与其他新 turn 一样，先与上次已告知快照比较，再按结果通知和替换快照。新上下文初始化比较快照不改变连接、工具定义或调用引用的实际有效性。

通知不承担调用资格。引用仍按该工具实际定义、策略、范围及目标身份决定是否失效；未变工具不因同服务其他工具变化而失效。所有原生与 Cell 调用继续使用已确定的 meta 链路，规则沿[MCP 架构草案](PULSARA_MCP_META_ONLY_CALLING_ARCHITECTURE_DRAFT.zh.md)和[MCP 工具草案](native_tools/mcp.md)。

删除旧 MCP_CATALOG source 的全量自动摘要、presence 公开展示、FULL／COMPACT／REF_ONLY 三档目录投影和专用配额，以及 direct／meta 分类、降级例外、相关省略计数、资源数量和固定调用教学。不保留旧 source 别名，不将整份目录总摘要或总指纹代替本节的实际变化比较；此处不改变协议、发现、调用及 provider 的真实资源边界，也不新增通知总量 cap。

## 8. 实施接点与核对

相关合同：[能力查询](native_tools/capabilities.md)、[文件工具](native_tools/filesystem.md)、[Plan 工具](native_tools/plan.md)、[统一交付](PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)、[有序内容数据库](PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md)。Plan 工具按本文件的 turn 边界、三类说明和交接位置同步；其他文档的对应旧 Skill 叙述在后续同步时按本文件收敛，不据旧叙述恢复自动全文注入。

实施 hardcut 时必须删除旧的“每次模型请求或普通工具续跑都尝试推送 Skill／MCP 目录”规则，以及 MCP 按展示摘要文字判重的路径，改为第 1.3 节的发送入口和实际相关事实比较；不能只替换 source／body 外壳。底层发现、刷新、能力采用及实际调用准入仍及时运行，不因通知延后而冻结能力事实。

| 当前接点 | 修订边界 |
| --- | --- |
| [runtime observation 编解码与 source heads](src/pulsara_agent/model_input/continuity.py) | 保留 source／body 外壳，body 改 text＋可选 target 数组；删除公开 presence，内部必要事实不从自然语言反解析 |
| [来源采集](src/pulsara_agent/conversation_kernel/context_sources.py)及[编译器](src/pulsara_agent/model_input/compiler.py) | 初始目录与平时增量分开；按第 6 节合并本轮运行信息、限定 Plan 说明时机和位置；删除旧独立规划提示、Skill 的空 body 状态展示和全文激活注入，不重写已安装前缀 |
| [Skill 渲染](src/pulsara_agent/capability/render.py)及[激活解析](src/pulsara_agent/capability/resolver.py) | 目录采用 name／description／SKILL Target；激活只认 slash，输出 read_file 优先提示；移除旧 16 项数量限制与全文载体专用预算 |
| [原生与 JS 工具执行](src/pulsara_agent/conversation_kernel/tool_execution.py)及[文件读取](src/pulsara_agent/tools/builtins/filesystem.py) | 在共同读取事实边界识别合法 Skill；不将原生是否整页交付当纳入条件 |
| [旧压缩技能保留](src/pulsara_agent/conversation_kernel/compaction/retained_skill.py)及[压缩后输入准备](src/pulsara_agent/conversation_kernel/provider_dispatch.py) | 删除独立正文 source 和旧全文证明，改在 TODO／PCA 旁交接待读 Target，并核对当前来源状态 |
| [Plan 控制与续接](src/pulsara_agent/conversation_kernel/plan_runtime.py)、[Plan 事务](src/pulsara_agent/conversation_kernel/_repository/plans.py)及[Runtime 交接](src/pulsara_agent/conversation_kernel/compaction/runtime_handoff.py) | 每个新 turn 提供冻结运行信息，并可附相关能力净变化；新 turn 起始与压缩后的 TODO／PCA 状态交接分开；规划压缩引用进入前 turn A 最后一条已交付用户输入，执行压缩承接完整获批原文；自动续接不触发 slash 激活或记忆观察，空闲压缩不提供 Runtime 交接 |
| [MCP 发现与定义事实](src/pulsara_agent/conversation_kernel/mcp/discovery.py)、[schema 接纳](src/pulsara_agent/conversation_kernel/mcp/schema.py)、[目录与连接 owner](src/pulsara_agent/conversation_kernel/mcp/supervisor.py)及[执行策略](src/pulsara_agent/conversation_kernel/tool_surface.py) | 复用完整已接纳 schema、说明、配置、范围与策略事实；按第 7 节比较变化，不新增协议解析器或刷新路径；不把 SDK 未纳入定义事实的元数据宣称为已有追踪能力 |
| [MCP 旧展示](src/pulsara_agent/conversation_kernel/context_sources.py)、[摘要判重与 source heads](src/pulsara_agent/model_input/compiler.py)及[输入准备](src/pulsara_agent/conversation_kernel/provider_dispatch.py) | 用 MCP_CATALOG_CHANGES 替换旧目录摘要；只保存上次已告知快照，每个新 turn 首次输入或本次用户主动 steer 准备汇报时临时计算最新净 diff，有相关变化才通知，输入安装成功后用生成本次通知所依据的快照替换旧快照，ABA 不通知；采用新上下文时初始化比较快照，不保留跨上下文变化历史 |

实施验证应覆盖六类来源、原生与 JS、部分读取、同名不同副本、终端读取不追踪、slash 与自然语言名称区分、目录新增启停移除及更新、压缩后目录刷新与待读强制提示、删除禁用过滤、来源失败不伪装空值，以及同 epoch 前缀不改写。保留实际来源、权限、读取、顺序和压缩连续性的行为验证；不新增只证明字段已删的测试。

三类说明另核对：所有新 turn、用户主动 steer 及活动 turn 压缩三个入口，各 source 按自身规则发送；用户、定时、子代理、Plan 新 turn 均有冻结运行信息；规划压缩引用进入前 turn A 最后一条已交付用户输入且无输入时不伪造引文；执行续接及执行 turn 压缩均带完整获批原文；空 TODO／PCA 不省略运行说明；空闲压缩无 Runtime 交接；修订／取消／超时不新建 turn；自动新 turn 可发送能力净变化但不触发 slash 激活或记忆观察。Skill 另核对完整目录起点的成功采用与失败恢复、仅正文更新不通知、同批用户引用的收集及未采用项不激活。

完整计划另覆盖原调用仍在／已不在近期窗口两种情况，以及合法输入接近 1 MiB、包含较多需 JSON 转义文字的情况；验证 APPROVED_PLAN 原文完整、实际封装计费正确，旧载体上限不导致截断或缩小输入范围，同时保留真实请求准入失败的行为验证。

MCP 另核对：每个新 turn 首次输入与本次实际采用的用户主动 steer 都比较，同 turn 工具续跑及 Cell／Agent 补充不触发；服务增删启停及恢复、同名工具定义和范围变化按相关类别筛选；正文保留建议性查询指引；准备汇报时临时计算最新净 diff，启停或定义 ABA 不通知；相同提示文字对应不同最终净变化仍再次通知；在既有 provider 输入安装成功时，用生成本次通知所依据的状态快照替换上次已告知快照，准备后再发生的变化留待下一次比较，候选准备或队列采用不提前替换，也不等待模型回复，安装失败不替换；观察失败不误报删除；采用新上下文时初始化比较快照且不补报旧变化。无需为接入本节再覆盖一套依赖的 JSON Schema 一致性测试。

其他 runtime observation 的正文、发送时机和交接职责尚未确定。本文件不因通知调整而改动 MCP 查询、执行、记忆召回或其他控制语义，后续逐类补齐。Hook 的模型附加文字与事件交付沿[独立消息草案](PULSARA_HOOK_CONTEXT_MESSAGE_REVISION_DRAFT.zh.md)，删除旧 HOOK_CONTEXT source，不在 pulsara_runtime_observation 中发送 Hook 信息。Hook 安装、移除、信任、撤销信任和启停均不主动通知模型；能力查询与管理工具的正常结果保留，阻止、参数修改、权限裁决和续接控制保持现状。
