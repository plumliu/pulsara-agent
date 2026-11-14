# Pulsara Runtime Observation 修订草案

状态：讨论草案，主体未实施。当前代码已修复同批用户输入的 Skill 引用收集，并删除旧 16 项激活限制；其余目录、提示与交接修订仍待实施。本文件确定 runtime observation 的公共展示形状、Skill 目录与通知，以及本轮运行信息、规划状态、已批准计划的发送与压缩交接。MCP、Hook、Plugin、记忆及其他来源的具体正文和发送规则后续逐类讨论，不在此推定。

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
| source | 本次说明的来源；已确定 SKILL_CATALOG_CHANGES、ACTIVE_SKILL，以及第 6 节的 TURN_CONTEXT、PLAN_MODE、APPROVED_PLAN |
| body | 集中呈现本次说明的对象数组 |
| body[].text | 自然语言内容，直接说明发生了什么、必要原因及下一步引导 |
| body[].target | 需要后续操作时附带的已有 Target；不需要时省略 |

这里是给模型阅读的说明，不是审计记录或程序 SDK 结果。不使用工具 content 的 type/value，不新增操作枚举、状态矩阵、变化 ID、版本标签或回执。Target 仍采用既定形状；Skill 使用 kind:SKILL、skill_path，不在对象旁再重复裸路径。body 的 text 直接表达空、失败或变化，不把正文包成另一层 JSON 字符串。

runtime observation 继续由原运行时来源投影为模型消息，不因 role=user 就成为新人类指令。内部必要的来源与控制事实仍由原 owner 保存，不从公开文字反推执行权限。

### 1.2 内部状态与公开文字

内部仍需分清“本次没有新变化”“已确认当前为空”“本次无法可靠读取”，以便正确去重、继承和报告失败。这不要求向模型公开 VALUE／CLEARED／UNAVAILABLE，也不要求为保留旧枚举而保留整套旧展示状态结构。

确有需要时直接说明，例如“当前没有可用技能。”或“当前无法获取技能目录。”；没有变化时不发送空通知。无法读取不能被描述为全部技能已删除，移除或禁用也不能靠一条空 body 让模型猜测。

修改公开投影时同步删除旧 presence 字段及其专属格式验证、测试和读取依赖。仍有独立用途的内部事实沿现有 typed owner 读取；随旧 Skill 正文恢复路径退出的反向解析器一起删除，不为自然语言再建设 decoder，不另存一份控制正文或新增状态注册表。

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

成功进入模型上下文的完整目录同时成为后续增量通知的比较基线；仅准备候选、未采用或失败时不推进。压缩后的新上下文若只收到“目录不可用”，不能继续依赖已退出上下文的旧目录基线。恢复后在下一次真实用户输入中补齐全部当前可用条目，说明“当前恢复可用”，不伪称它们都是新增；成功交付后再从该目录计算净增量。空闲压缩尚未开始实际模型交互时，不提前推进模型已知目录。

同一 epoch 内已安装的 SYSTEM、provider tools 保持字节一致，messages 仅追加。初始目录只在新 session／cold 上下文或明确采用的 compaction successor 中建立；平时变化追加通知，不回改上下文开头或历史消息。不为刷新目录增加新的前缀重建边界。

## 3. 平时只发送 Skill 增量变化

### 3.1 发送时机与比较范围

将原 SKILL_CATALOG 的自动全量变化快照收敛为 SKILL_CATALOG_CHANGES。在后续用户输入进入模型上下文时，集中报告相对上次成功告知状态的净变化；包括用户普通消息及用户主动发送的 steer，不由 runtime 为能力变化主动制造 steer，也不因目录变化唤醒空闲模型。

工具结果后的普通续跑不单独推送 Skill 目录变化。模型自己的管理调用仍直接收到实际工具结果；真实能力采用、启停和权限不等待变化通知才生效。

两次告知之间启停后恢复原状，可以没有净变化，不承诺逐次动作历史。沿现有来源和 session owner 比较实际状态，不新增持久变化日志、投递 ACK 或永久快照表。只有确知用户操作来源时才写“用户新增／停用”，不能把普通文件扫描或模型操作一律归因于用户。

### 3.2 变化正文

同一次的新增、启用、禁用、移除和更新集中在一个 body 数组中，以自然语言表达，不公开 action 字段或操作枚举：

| 变化 | 内容 |
| --- | --- |
| 新增／启用 | 名称、用途说明和当前 SKILL Target |
| 禁用 | 名称、SKILL Target，并说明不要继续读取或使用该技能 |
| 移除 | 名称和移除说明，不附已移除对象的 Target |
| 描述、路径或同名选中副本改变 | 说明更新，附当前名称、必要描述和 Target，不让模型继续依赖旧目录信息 |

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

待读技能属于压缩交接，与 TODO 和 PCA（process／cell／agent）并列放置，不再作为独立的 RETAINED_SKILL_CONTEXT runtime observation，也不复制技能正文。初始完整目录和交接待读清单职责不同：前者说明当前有哪些技能，后者要求继续前先重读此前读过的技能。

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
| TURN_CONTEXT | 工作区类型与根路径、时区与 UTC 偏移、本 turn 冻结的权限及操作说明、当前时间 | 每个 turn 首次准备模型输入时发送；该 turn 压缩并采用新上下文后，在 Runtime 交接处重新提供 |
| PLAN_MODE | 当前仍处于计划模式，应继续只读调查和规划；附“进入规划前的用户输入”完整原文 | 只在活动规划 turn 的压缩交接处提供，普通规划过程不单独发送 |
| APPROVED_PLAN | 本次用户批准的完整计划原文，以及结合已有进展继续、避免重复执行的提醒 | 用户批准并自动开启执行续接 turn 时发送一次；该执行 turn 压缩后在 Runtime 交接处承接 |

TURN_CONTEXT 的权限始终取本 turn 已冻结的实际值，不改成输入框当前选择，也不在压缩时重新决定权限。时间取本次输入准备／交接时的实际时间，环境取当前 owner 的实际信息。普通工具续跑不因时钟变化而单独追加旧 RUNTIME_CLOCK 或反复重发整块运行说明。

PLAN_MODE 引用进入规划动作之前、已经进入模型上下文的最后一条真实用户消息／steer 的完整正文，统一标为“进入规划前的用户输入”。沿既有输入顺序和进入动作取得原文，不用交接摘要，不猜哪句话导致规划，也不要求 enter_plan 新增 reason 参数。没有这样的用户输入时只说明状态，不编造引文。该标签不声称用户明确要求进入规划；后续用户补充仍按既有顺序生效，不以旧引文覆盖后来的要求。

APPROVED_PLAN 使用本次获批调用中保存的完整 plan，不读取 Markdown 或摘要来替代，不另存第二份方案正文。正文明确提醒：“部分工作可能已经完成，请结合已有进展继续，避免重复执行。”这不声称已有步骤完成，更不自动重做工具。执行 turn 压缩后承接同一份获批内容，不构成再次批准或新的执行任务。

上述发送与交接均不受旧“历史里已有原文就跳过”的分支影响。无论原始 exit_plan 调用是否仍在近期保留窗口，APPROVED_PLAN 都按本节时机提供完整原文；原始调用和结果仍按正常历史规则保留，不为避免文字重合而删除或改写历史交互。

完整计划、提示文字、JSON 转义和实际消息封装按真实发送形状计费，不只计算 plan 裸字符串。现行单 source variant 的 1 MiB 字节上限不能沿用为 APPROVED_PLAN 的封装上限；实施时删除这份计划投影上的旧载体限额检查及同用途拒绝路径，不设替代数值，也不因封装开销截断、摘要化或缩小 exit_plan 原有的 1 MiB UTF-8 计划输入范围。最终完整请求仍遵守实际 provider 输入准入和真实资源边界；真实失败明确报告，不伪装成已完整交付。本项当前只修订文档，生产代码待实施。

### 6.2 Turn 边界与触发来源

首次成功 enter_plan 结束原 turn，自动开启冻结只读权限的规划 turn；不在原 turn 内改写权限。模型已能从该工具调用与结果得知进入规划，不再另发进入提示或持续规划提示。重复 enter_plan 保持原规划 turn。

修订、取消一次审批及十分钟超时按 [Plan 工具草案](native_tools/plan.md)保持当前只读规划 turn；通过最终工具结果和真实用户反馈表达，不另造状态通知。在上述正常审批／退出分支中，只有批准或无方案退出才结束规划 turn，并自动开启恢复原权限的后继 turn。显式 Stop、强制退出和关闭会话仍按各自既有控制语义处理。批准时附 APPROVED_PLAN；无方案退出不附获批计划，也不把旧草案当成批准内容。

runtime 自动开启规划轮、批准后的执行续接、无方案退出后的续接及压缩交接，都不作为新用户输入触发 Skill 激活、Skill 增量或其他能力通知。TURN_CONTEXT 与实际需要的 Plan 说明可独立发送，不必夹带其他 observation。真实用户消息／steer 仍沿其自身规则处理。初始／压缩后的完整 Skill 目录仍按第 2 节建立，这不属于附带发送的能力变化通知。

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

再接独立的规划状态块；引文使用进入规划前最后一条已交付用户输入的完整原文：

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

### 6.4 压缩后的交接位置

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

环境、权限和时间不放到前面的能力目录区域。普通 turn 的 Runtime 交接只含 TURN_CONTEXT；规划 turn 再含 PLAN_MODE；批准后的执行 turn 再含 APPROVED_PLAN，不同时添加两种 Plan 说明。

运行说明独立于 TODO／PCA 是否非空：即使没有待办、进程、Cell、Agent 或待读 Skill，活动 turn 的压缩续接也必须提供 TURN_CONTEXT 和适用的 Plan 块，不能沿用旧“没有交接对象就不生成”的早退。

承接本 turn 需要的全部有效运行说明，不逐次重放已失效的状态、旧时钟和历史切换提示。TODO、PCA、待读 Skill 仍是并列交接内容，不各自新建 observation source。获批计划来自实际保存的原文，当前状态来自运行时事实，不依赖模型交接手册是否记全。

该排列仅在新 cold 上下文／明确采用的 compaction successor 建立；同一 epoch 内的后续输入只追加，不为调整说明位置重写已安装 SYSTEM、tools 或 messages 前缀。

已结束 turn 的空闲压缩只生成等待下一次输入的历史上下文，不提供任何 Runtime 交接，包括 TODO、PCA、待读 Skill 和这三类 observation 块，也不因此继续任务。后续由用户第一条真实输入开启新 turn 时，再携带该新 turn 的环境、时间、冻结权限及按实际触发规则需要的说明；不把已结束 turn 的规划状态或获批执行要求提前注入。完整 Skill 目录仍在新上下文建立时按第 2 节提供，与 Runtime 交接分开。

### 6.5 实施收敛边界

删除旧 PLAN_WORKFLOW／PLAN_HANDOFF 的独立状态、进入、修订、批准和退出提示生产消费规则，改为本节三类说明及既有工具结果／用户反馈。环境、权限、时间不再以 RUNTIME_ENVIRONMENT／RUN_PERMISSION／RUNTIME_CLOCK 三条公开 source 分散发送。

复用既有 Plan owner、turn 权限快照、续接关联和完整方案提取；APPROVED_PLAN 承载原文后，不在同一模型投影位置再并列另一套规划续接控制 JSON 或批准短提示。内部必要的模式切换、审批、原文关联与执行结算不因此删除，也不新增正文实体、永久 observation 日志、资格登记或自动重做机制。

旧 PIN_EXISTING_CANONICAL_BLOCK／MATERIALIZE_REFERENCED_BLOCK 对公开原文注入的选择不再决定是否产生 APPROVED_PLAN；保留从既有关联提取精确获批原文的能力。同步清理旧 source 字节限额对这份完整计划的限制和相关预算估算，使用完整实际封装计量，不因更换展示载体引入更小的合法计划范围。其他来源的独立边界不在本项机械删除范围内。

## 7. 实施接点与核对

相关合同：[能力查询](native_tools/capabilities.md)、[文件工具](native_tools/filesystem.md)、[Plan 工具](native_tools/plan.md)、[统一交付](PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)、[有序内容数据库](PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md)。Plan 工具按本文件的 turn 边界、三类说明和交接位置同步；其他文档的对应旧 Skill 叙述在后续同步时按本文件收敛，不据旧叙述恢复自动全文注入。

| 当前接点 | 修订边界 |
| --- | --- |
| [runtime observation 编解码与 source heads](src/pulsara_agent/model_input/continuity.py) | 保留 source／body 外壳，body 改 text＋可选 target 数组；删除公开 presence，内部必要事实不从自然语言反解析 |
| [来源采集](src/pulsara_agent/conversation_kernel/context_sources.py)及[编译器](src/pulsara_agent/model_input/compiler.py) | 初始目录与平时增量分开；按第 6 节合并本轮运行信息、限定 Plan 说明时机和位置；删除旧独立规划提示、Skill 的空 body 状态展示和全文激活注入，不重写已安装前缀 |
| [Skill 渲染](src/pulsara_agent/capability/render.py)及[激活解析](src/pulsara_agent/capability/resolver.py) | 目录采用 name／description／SKILL Target；激活只认 slash，输出 read_file 优先提示；移除旧 16 项数量限制与全文载体专用预算 |
| [原生与 JS 工具执行](src/pulsara_agent/conversation_kernel/tool_execution.py)及[文件读取](src/pulsara_agent/tools/builtins/filesystem.py) | 在共同读取事实边界识别合法 Skill；不将原生是否整页交付当纳入条件 |
| [旧压缩技能保留](src/pulsara_agent/conversation_kernel/compaction/retained_skill.py)及[压缩后输入准备](src/pulsara_agent/conversation_kernel/provider_dispatch.py) | 删除独立正文 source 和旧全文证明，改在 TODO／PCA 旁交接待读 Target，并核对当前来源状态 |
| [Plan 控制与续接](src/pulsara_agent/conversation_kernel/plan_runtime.py)、[Plan 事务](src/pulsara_agent/conversation_kernel/_repository/plans.py)及[Runtime 交接](src/pulsara_agent/conversation_kernel/compaction/runtime_handoff.py) | 保持首次进入与批准／退出的 turn 边界、冻结权限和唯一最终结果；按触发来源发送三类块，规划压缩保留进入前最后一条用户输入原文，执行压缩保留获批原文；空闲压缩不提供 Runtime 交接，自动续接不触发能力通知 |

实施验证应覆盖六类来源、原生与 JS、部分读取、同名不同副本、终端读取不追踪、slash 与自然语言名称区分、目录新增启停移除及更新、压缩后目录刷新与待读强制提示、删除禁用过滤、来源失败不伪装空值，以及同 epoch 前缀不改写。保留实际来源、权限、读取、顺序和压缩连续性的行为验证；不新增只证明字段已删的测试。

三类说明另核对：turn 首次输入的合并运行信息、规划过程无重复提示、规划压缩引用最后一条已交付用户输入、批准执行时的完整原文及进展提醒、连续活动压缩的承接、空 TODO／PCA 不省略运行说明、空闲压缩无 Runtime 交接、无方案退出不携带获批计划、修订／取消／超时不新建 turn、显式控制保持原语义、runtime 自动续接不夹带能力通知，以及交接位置与冻结权限保持一致。Skill 另核对完整目录基线的成功采用与失败恢复、同批各条用户输入的引用及未采用项不激活。

完整计划另覆盖原调用仍在／已不在近期窗口两种情况，以及合法输入接近 1 MiB、包含较多需 JSON 转义文字的情况；验证 APPROVED_PLAN 原文完整、实际封装计费正确，旧载体上限不导致截断或缩小输入范围，同时保留真实请求准入失败的行为验证。

其他 runtime observation 的正文、发送时机和交接职责尚未确定。本文件不因公共形状调整而改动 MCP 目录、Hook 执行、记忆召回或其他控制语义，后续逐类补齐。
