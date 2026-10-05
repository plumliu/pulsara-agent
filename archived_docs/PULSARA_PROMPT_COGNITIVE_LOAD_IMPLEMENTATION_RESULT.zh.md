# Pulsara 提示词实施与验收记录

日期：2026-10-03。依据 [冻结实施规范](/Users/plumliu/Desktop/python_workspace/pulsara_agent/archived_docs/PULSARA_PROMPT_COGNITIVE_LOAD_HARD_CUT_IMPLEMENTATION_SPEC.zh.md) 完成 AUDIT 优先结论全部 P1/P2 的实施。生产改动限于五个现有 owner；新增测试与真实模型诊断脚本用于验证这些改动。没有修改数据库 schema、权限 gate、Plan 状态机、provider role、source/event/subject/guard/relation/job 集合、prefix 安装路径或回合完成语义。

## 1. 实施结果

| 交付 | 最终位置与行为 |
| --- | --- |
| 消息来源与生命周期 | [collector](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/context_sources.py) 集中解释 role、来源用途、三种 trust、遗漏、ONE_SHOT；复制标签不能获得权限。协作 provenance 与 EXPLICIT/INFERRED 原意保持。 |
| Visualization metadata | [reader](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/reader.py) 只在已有 assistant refs 的构造点加入规定的 `handling`；refs 顺序、USER origin、UTF-8 计量保持。human 同形 JSON、annotation 和图像保持原文及原投影。 |
| Plan、TODO、授权持续 | [DEFAULT_SYSTEM_PROMPT](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/ports/system_prompt.py) 解释 Plan 的审阅触发、只调查阶段和批准续接；TODO 不改变审批与权限。accepted-run 快照、exact-call 批准、拒绝、过期及未知效果各有下一步规则。 |
| AGENTS.md | 同一 DEFAULT 段规定根目录确切路径首读、目标祖先路径、适用范围，以及缺失/不可读的区别；全文已在当前任务可用时避免重复读取。 |
| 工具结果与时效 | collector 区分 invocation 与 body 业务结果；旧观察可继续使用，影响下一步的变化值才需重查；未知效果先走授权查询。中断事实仍保留。 |
| 编排前置条件 | [builtin_catalog](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/builtin_catalog.py) 给七个 root 编排工具加入完全相同的 scope + effective bypass 前置句；[pulsara-subagent Skill](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/bundled_skills/pulsara-subagent/SKILL.md) 集中说明 listing/wait 也受该 gate 约束。 |
| Skill 与 handoff | collector 统一 ACTIVE_SKILL 正文、catalog、相关时的缺失查询、精确 returned target、分页/artifact 路由和 supporting reference；handoff 保留完整 TODO、已有 ID 与缺项未知，查询服从当前 gate。 |
| UI 交付 | DEFAULT 增加两句 Markdown 表格、LaTeX、Mermaid、真实绝对文件路径与有需要才调用交互可视化的路由。 |

替换或合并了原有重复来源说明、Skill 双义读取规则和 unknown-effect 路由，没有增加通用 wrapper、prompt registry 或另一套执行机制。Memory、Delegation 以及协作 provenance 的原有文字保持。用户此前要求的工具示例 `Hello` 保持。

## 2. 契约与边界测试

均使用仓库 `.venv`。下列批次全部通过；不同批次有重复用例，数量是执行结果，不累计为独立测试数。

| 批次 | 结果与主要覆盖 |
| --- | --- |
| system prompt、provider prefix continuity、Plan workflow、provider output termination | 166 passed；安装根不漂移、Plan 与当前 readonly/完成语义。 |
| structured input compiler、durable provider replay、direct model、Plan PostgreSQL | 187 passed；实际编译与原生 replay、持久 Plan owners。 |
| confirmation retention、model-visible failure、provider-visible tool result、Plan host | 82 passed；exact interaction、expiry/unknown、业务结果投影、Plan 续接与非批准分支。 |
| 本次新增投影/descriptor/Skill 与既有 compaction 子集 | 47 passed；所有非 bypass mode 的七工具 gate 在 owner I/O 前拒绝、effective root 包含 collector/delegated guidance、human 同形 JSON 与 metadata 区别。 |
| 完整 visualization subscription | 7 passed；真实 PostgreSQL reader 和两种已保存 publication 来源。 |
| 最后 AGENTS 首读句修订后的 prompt/effective-root 子集 | 2 passed。 |

精确命令及测试范围见 [主代理记录](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/scripted-test-main.md) 和 [测试代理记录](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/scripted-test-agent.md)。既有 synthetic metadata replay 测试未改写，没有新增 skip/xfail 或弱化产品断言。生产与改动测试/诊断脚本完成 Ruff、编译检查和 `git diff --check`；没有为格式化重写整份既有测试文件。

## 3. 真实模型场景

通过 `LocalSettingsStore` 和 `require_pulsara_home()` 只读加载 `/Users/plumliu/.pulsara` 保存的连接，使用其 `openai/gpt-6-luna`、OpenRouter 路由。使用正常 KernelHostCore、模型调用与工具/Plan/interaction owners；新进程与 cold epoch 安装新提示。隔离数据库由现有 helper 验证本地 target 后建立、使用并清理；保存的生产设置未改写。报告保留实际 provider-visible 输入、归一化回复、工具请求与结果，仅屏蔽真实凭据值。

| 场景 | 实际行为与证据 |
| --- | --- |
| 根与嵌套 AGENTS、普通实现、变化值重查 | 按适用目录读取后写入目标；普通实现没有进入 Plan；外部 revision 变化后重读再只改目标行。[最终首读句场景](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/agents-ordinary-changed-model.json) |
| 根 AGENTS 缺失、无关子树、缺失 Skill | 根文件不存在时继续目标祖先，未扫无关子树；通过 `list_capabilities(kind=SKILL)` 查询，未猜路径/安装，完成独立计算。[补充场景](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/supplementary-model.json) |
| 非 bypass 编排、ACTIVE_SKILL、业务失败、稳定观察、UI | READ_ONLY 未尝试 root 编排；已有 Skill 正文没有为激活重读，创建两项工作并等待确切 ID；成功 poll 中的 exit 7 被认作业务失败；稳定旧值直接使用；静态表格与实际文件链接直接交付。[基础场景](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/real-model-dogfood-base.json) |
| exact ALLOW/DENY、过期与继续同一任务 | 精确批准第一写入、拒绝第二写入，第二文件不存在且没有替代写入；过期没有冒称成功/拒绝，续接使用新的 exact gate，未扩大审批或重问整个任务。[interaction 场景](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/interaction-model.json) |
| partial handoff 与 unknown effect | READ_ONLY 下保留已有 TODO/ID，做独立读取，没有调用受禁编排或盲重启；未知 append 通过只读终端检查确认 marker 已有一行，没有重跑 append。[原始记录](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/handoff-unknown-model.json)、[unknown 人工复核](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/unknown-effect-human-adjudication.json) |
| 已有 visualization metadata | 先真实发布 HTML，再在同一 session 的下一回合使用 reader 实际 refs/handling；说明已有显示，没有把 metadata 当新绘图请求或 screenshot。[metadata 场景](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/metadata-plan-model.json) |
| 明确先审方案 | 完整可发现事实下调查后提交 draft，批准前没有改文件或提出多余问题。[明确契约场景](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/plan-review-explicit-contract-model.json) |
| Plan 批准后实施 | 本地诊断 reviewer 检查实际完整 draft 后，使用 public owner 精确批准一次；runtime 自动续接一次，模型实现 app/test 并运行测试，2 passed；续接没有重复 enter/ask/exit 审批。[批准续接场景](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/plan-review-approval-model-corrected.json) |

以上八份选定原始报告共保存 99 次 provider 调用，包含场景准备、重测和下面说明的被中断 Plan 夹具；此数字不是通过场景数。脚本入口为 [主场景](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/dogfood/run_prompt_cognitive_dogfood.py) 与 [补充场景](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/dogfood/run_prompt_cognitive_supplementary.py)。

handoff 用现有生产构造函数投影受控快照，未声称完成一次真实 compaction；实际采用/前缀契约由 scripted owner tests 覆盖。unknown append 的一次物理写入是隔离夹具，canonical attempt/interruption 与模型后续合法查询走现有 owners。interaction/Plan 的人类决策由标明用途的本地诊断 controller 提供，不涉及生产项目审批。

## 4. 实测修订与未隐藏的夹具问题

首轮普通工作在空目录递归查找 AGENTS。依据该动作，把已有首读句收紧为根文件的确切路径；没有增加另一层提醒或发现机制。最终句在根/嵌套、根缺失和普通实施场景重测通过。其他 P1/P2 规则未因该句修改而扩大范围。

保留原始报告中的 false/unset 值，没有把历史文件改成全绿：

- 早期单行输出判据额外要求 LF，但请求并未要求；原失败文件保留，明确内容要求后重跑。
- 稳定/变化观察的准备回合遗漏 `decision_passed`，已按实际 read、回复和 trace 单独复核；[稳定观察复核](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/stable-observation-human-adjudication.json) 与 [变化观察复核](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/changed-observation-human-adjudication.json) 保留原记录。
- unknown-effect 初始判据禁止任何 terminal，错误排除了合法只读检查；原 false 与独立复核保留。脚本现在检查确切只读动作、实际 matching count、物理 marker 和未重复副作用。
- 首个 Plan 空目录夹具缺少实现文件和输入格式，模型提出真正 OPEN QUESTION，诊断控制没有回答而中断；CancelledError 不是 provider 失败。原报告与 [说明](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/plan-review-empty-workspace-open-question-note.json) 保留，补齐可发现事实后重测。
- Plan 批准探针首次误把 owner enum 写为 `DRAFT`，实际为 `DRAFT_REVIEW`；保留旧报告，修正夹具后通过真实批准与实施。
- 最早 harness 调用了不存在的 session.close；保留初始错误，改用 public close_session，再捕获实际 wire 输入。

这些代表性场景证明列出的具体行为，未构成所有模型/所有运行的理解保证。例如空目录的 exact-interaction 场景未先读缺失 AGENTS；专门的 AGENTS 场景验证了发现路径。此次没有建立独立 A/B 认知负担评测。

## 5. 输入体积、前缀与安装

[前后提示快照和体积记录](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/prompt-size.json) 通过现有 collector 与 worker profile 构造入口提取。effective root 从 12,994 增至 17,374 UTF-8 bytes（约 33.7%）；general worker 从 13,504 增至 17,884。全局 builtin catalog 说明从 29,675 增至 30,414 bytes；它包含 37 个 descriptor，首个真实 root 请求的可见 surface 是 36 个，worker 是 21 个。没有可用的本地 tokenizer；记录的 provider final-wire estimates 也不等于实际 tokenizer 计数。

文本增加填补了决策入口、作用域与结果解释。实测观察到合法续接和避免若干冗余动作；尚不能据此量化认知负担降低。

[捕获输入的前缀比较](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/prompt-implementation-20261003/captured-prefix-checks.json) 在六份报告中比较了同一首个 canonical request 下的 SYSTEM/tools 和 messages 前缀：59 次 exact JSON-value 比较，无失败。分组未使用 SYSTEM，因而不会把 SYSTEM 漂移分成新组；这是 capture 层补充证据，原始 HTTP 字节与安装边界仍由既有 prefix/replay 测试负责。

新文案已在 dogfood 的 cold epoch 验证。现有 prefix owner 未改动，已有运行 epoch 保留已安装根和历史消息；新的 cold epoch 或明确采用的 compaction successor 才安装新根。本次没有热改当前桌面进程或触发用户会话 rebase。
