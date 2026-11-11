# 内部版本标签 Hard-Cut：基线语义逐项复核

日期：2026-11-11。文档与代码对照基线：`5307bdb0bf7d17ed38f82ed0a26bb90fcc299fd3`。

范围：[内部版本标签删除规格](PULSARA_INTERNAL_VERSION_LABEL_SUBTRACTION_HARD_CUT_DRAFT.zh.md)。另外两份根目录产品草案只随基线提交，未实施统一分页、Code Mode 图文 steer 或 motion 修订。

本记录对照实际业务约束、调用链和保留测试，不把字段消失、行数减少或测试总数作为语义等价证明。接口和持久正文移除内部标签是本次明确授权的 hardcut；不承诺读取旧开发会话与旧数据库结构。

## 1. 逐项对照

| 规格项 | 基线的实际产品行为 | 当前实现与复核证据 |
| --- | --- | --- |
| Cell 输出 | 关联 Cell／turn／attempt／sequence，严格解析 record 并保持顺序 | 仅删 `cell_output.v1`；真实字段闭集、record 形状、身份及 canonical bytes 校验保留。前后端同时更新；Cell fixture 使用当前真实字段。 |
| Prompt 图文正文 | 有序 parts、严格类型与 canonical 编码、真实图片引用归属 | 顶层改为 parts；重复键、未知字段、非 canonical JSON 仍拒绝。`prompt_storage.py` 的图片 owner／workspace／refs 验证没有业务变更。 |
| Annotation 正文 | quote／comment／来源与 provider 投影保持关联 | 仅去 codec 的旧 schema 处理；round-trip、UTF-16 范围、图文与原文投影断言保留。 |
| Closure／late outcome | tool call 与 disposition／真实结果状态和正文精确对应 | 仅去 JSON 的固定 schema 成员；call、状态、正文及 reader 顺序保留，上界估算使用实际新正文。 |
| 本机设置 | 当前配置字段、凭据保护、原子保存与提交不确定性保护 | 只删格式标签／专属异常；保留实际闭集校验及既有 postgres 字段过滤。真实保存文件未改，不在用户 home 触发 repair；测试使用临时设置。 |
| 定时规则 | once／interval／daily／weekly／monthly 的真实字段、时区与时间约束 | 前端、`validate_rule`、`localOnce`、工具 request schema 同步去 contract。仍严格拒绝多余字段，并验证日期、时钟、间隔、weekday／monthday；定向五文件 213 项通过。 |
| 命令幂等 | command kind、身份、实际参数摘要、原 target 与事务确认 | 仅去 request version；repository 的真实等值与冲突检查保留。ACK 未知继续由原事务 owner 确认，不重做已生效业务。 |
| Plan 工具准入 | 原广告 surface、descriptor、工具归属及 JSON Schema 决定是否可执行 | `plan_runtime.py` 只去固定 binding 构造和参数；实际执行 binding／advertised spec／catalog descriptor 比较与真实参数解析保留。 |
| Plan 原内容归属 | 原 ROOT turn、assistant entry、tool call、完整参数与 exact plan 内容关联 | 删除固定 id／version／fingerprint 三元组，不删除原 block 查询与完整 arguments 比较、semantic digest、interaction kind／workflow revision；PostgreSQL Plan 流程与漂移拒绝测试通过。 |
| 权限 preset | requested／effective mode、来源、overlay、继承、workflow/revision 不可被替换 | 固定 preset id／hash 删除。Plan overlay 仍强制只读，无 overlay 仍要求 requested=effective；SQL 与编译器继续验证实际权限事实及精确归属。 |
| 模型 target／transport | 实际 endpoint、connection、model、wire API、reasoning 与冻结执行 owner 一致 | 去 v8／v9／v2 与失效透传；冻结真实值、实现绑定与调用身份验收保留。Chat／Responses 的实际处理方法 AST 与基线一致。 |
| 错误脱敏／重试 | 保留真实失败信息，仅隐藏实际秘密；重试与取消按原规则 | DTO 标签删除，sanitizer 实际函数及 retry 算法无行为 diff；真实 SDK loopback 测试继续覆盖状态、等待、取消、关闭与脱敏。 |
| 公开错误身份 | 同一语义错误得到既有公开稳定 fingerprint | 唯一 identity builder 局部保留必要旧派生常量，不传回 DTO。主代理与核心代理分别比较三类错误，基线／当前公开错误身份相同。 |
| Native 工具复用 | 已安装 cohort 不扩张，Chat／Responses wire 不混用 | 去固定版本 hash，使用现有 quote wire 摘要／字节数与 predecessor 完整 wire 判断复用；不一致则走原物化路径。未新增 API 字段；retained schema、late META 与 scope 约束保留。 |
| Native 物化 | 选定工具必须精确符合实际 schema、wire 值、字节数与 token quote | 只去合同标签；物化后真实 wire 摘要、字节数、估计 tokens 三项检查保留，原 deadline 与物理准入边界未改。 |
| Replay target | 端点、模型、binding、API 与实际 codec 隔离 replay | 去 compatibility 标签与无用复制；保留真实 target／codec 与 manifest 选择和匹配。 |
| Replay 内容 | 公开投影、opaque reasoning、实际字段合并、顺序与 payload 完整性 | 去实现宣言，实际 replay 处理函数、Responses 完成事件合并与 payload 验证未改；不删除或改写 provider 正文里的版本键。 |
| 工具结果语义／rollup | 真实 variants、状态、denial 与正文渲染分支决定模型观察 | 删除只描述 builder／renderer 的对象与标签；真实 variant 集合、状态约束、denial 选择及渲染行为保留。 |
| Context source | channel、trust、budget、placement、degradation、modes、lifecycle 决定编译行为 | 去版本元数据；compiler 仍逐项比较真实 policy，并保留 physical bounds、absence 与生命周期规则。 |
| Compiler 预算 | MUST_KEEP、降级顺序、实际计费与最终 wire admission | 去报告标签；公式、数值边界、降级和 admission 未改。实际预算与图片上界测试保留，不保留普通过程内摘要的旧 golden。 |
| Prefix 连续性 | epoch 内 SYSTEM／tools 相同、messages 仅追加；合法 compaction successor 才采用 | 仅去 context-base hash 中固定 lowering 宣言；实际 base 内容／snapshot／来源范围与 prefix 比较保留，没有新增重建边界。 |
| Compaction 存储 | 保存完整实际 summary、source range／digest、owner、codec／size／内容摘要 | 删除三列固定标签及 DTO／fork 复制；正文、lineage、目标／cut 与采用条件保留，真实 PostgreSQL snapshot／fork 测试通过。 |
| Embedding | 不同模型／维度／文本处理语义与旧 fact 内容不可混用 | 删除固定 version 列；继续使用实际 semantic contract ID、fact digest、domain/context 与覆盖状态筛选，旧 embedding 的真实隔离测试保留。 |
| 查询诊断 | 查询种类、实际计数、限制与 watchdog 数据可读 | 去固定版本标签与无消费者的 protocol major；真实数据和查询分类保留，错误原因不因名称清理而无谓改名。 |
| 会话 cursor | 续读位置、作用域、查询参数与 operation 必须匹配 | 仅去 operation 版本后缀；编码／解码使用同一实际 operation。去版本升级拒绝断言，保留完整 Unicode 长正文续读、跨 operation／scope 篡改和图片不可拆分验证。 |
| Skills／memory hints | frontmatter、放置、正文与 opt-out matcher 控制行为 | 删除固定 core／placement／policy 标签；真实规则与信号未改。 |
| Plugin 本地状态 | enabled、scope、当前安装包、连接 overlay 与路径身份必须一致 | 去内部 v1／contract_id；原子读写、布尔类型、插件／scope／package／overlay／路径验证保留，外部插件 schema 未改。 |
| Hook trust | 用户已确认且实际定义未变时仍 TRUSTED，内容或来源变化后 MODIFIED | `hooks/trust.py` 生产实现没有改动；唯一摘要构造点的局部 v3 派生常量保留。只删除旧版本升级失效用例，实际定义／来源／环境／授权测试保留。 |
| Windows helper | 在原准备阶段发现不可加载／异常退出，不把故障延后到第一次工具调用 | 只去固定输出与 stdout 比较；既有无副作用调用、成功退出、构建 check=True 和启动 execute 保留。新增真实 missing-helper 回归用例验证原检查点抛出 ENOENT。 |
| Terminal 格式身份 | 当前应用创建的 client/server 使用同一包与生成定义 | 整体去 ProtocolIdentity 与版本拒绝，保留 hello 的 launch capability、session／client 身份、role、attachment、cleanup 与 live subscription。 |
| Terminal 业务定义 | 其余 wire 字段编号、类型、enum、options 与 snapshot 内容完整性 | 两名代理分别比较基线生成 descriptor：只移除指定 message／字段后，与当前 descriptor 完全相同；protoc 一致性和 snapshot 内容摘要检查通过。 |
| 数据库安装 | 空库原子安装、重复安装、lost ACK 的 FULL／NONE／CONFLICT | ledger 使用已有 name／SQL checksum／applied_at；只去 universe／head／prefix／应用版本副本与重复证明，原锁、事务及确认分支保留。 |
| 数据库结构／权限 | 当前关系、列、约束、索引、函数、触发器及 grants 必须精确合格 | 保留实际采集和 observed catalog 比对、扩展与 runtime grants 校验。资源由隔离数据库实际结构重新产生，不降低约束以迁就旧库。 |
| 数据库缓存／连接 | 按实际目标、数据库 OID、runtime role 隔离，checkout 核实连接事实 | 只去全进程固定 registry cache 成员；target、OID、role、search_path、server 与 ledger checksum 检查保留。 |
| 数据库诊断 | 显示实际安装／验证状态、数据库和角色，错误仍能指明真实失败 | 去固定 head／applied／universe／prefix／结果重复摘要；CLI、HTTP、desktop 同步更新，实际 reset-required 原错误 code 保留。 |
| 独立真实边界 | 外部协议、实际内容修订／并发代次、MAC、稳定实体 ID 和发布资源定位仍有效 | 未改 MCP 协商、外部插件 schema、shared ID 域、内容 revision、workflow revision、实际 PostgreSQL／扩展／WSL／包版本；没有新增 fallback、迁移链、注册表、字段、cap 或恢复机制。 |

## 2. 四代理交叉复核与主代理复核

四位实施代理交叉审查他人范围：核心代理审桌面／协议／正文与 Plan runtime；数据库代理审核心权限／source／预算／compaction；桌面代理审模型／native／replay；集成代理审数据库／repository／reader／诊断。均以基线逐项比较代码与调用链，未用整体通过数替代业务复核。

交审发现的定时规则前后端断链已由主代理同步修正后台 parser、service、request schema 和 fixture；核心代理再次只读复核并实际执行五文件 213 项测试，确认关闭。health 的固定 protocol major 残留也已删除。主代理另以当前 fixture 验证长正文 cursor 编解码和 annotation 正文链路。

主代理在独立基线 checkout 和当前 checkout 使用同一环境比较业务输出：10 个文本样例、3 个 JSON、4 个图片尺寸、3 个工具 schema lowering、3 个脱敏错误（含公开身份），结果相同。没有将该探针变成版本删除测试框架。

## 3. 验证状态

最终验证使用整合后的当前代码；早期仍含旧 fixture 的中间运行不作为最终通过结果。

| 检查 | 实际结果 |
| --- | --- |
| Python 完整集合，包含本机隔离 PostgreSQL，排除显式 retrieval_live | hardcut 整合运行记录为 **6212 passed、2 failed、20 skipped，另 15 subtests passed**；两项失败的当前处置及后续定向通过结果见下文，未将定向结果改写成一次新的全套通过。命令为 `.venv/bin/python -m pytest -q -n 4 -m 'not retrieval_live' --tb=short`，向测试进程提供已核实的本机 DSN 和 loopback NO_PROXY。 |
| 前端完整集合 | **99 个文件、1667 项通过**，命令 `npm test -- --maxWorkers=2`；`npm run build:desktop` 通过。 |
| Desktop | TypeScript 编译与 Windows／runtime／maintenance 相关 **51 项通过**；全套 **147 passed、3 failed**，其中两个 POSIX 原生进程子用例及其父级聚合为基线同样失败，见下文。未修改该生产模块或测试。 |
| 当前图文／compiler／预算补跑 | 183 passed、2 skipped；保留真实图片计数、来源漂移、Unicode、工具结果资源和编译行为断言。 |
| Cursor／annotation／历史 recall／CLI 等补跑 | 153 passed；提供实际 PostgreSQL 测试连接，长正文续读与图片不可拆分路径通过。 |
| Reset／canonical reader／image history 补跑 | 26 passed；用已有实际 SQL checksum 构造不合格安装 fixture，不再篡改已删除版本列。原子 reset、失败回滚及取消等断言保留。 |
| 体系与资源测试 | 21 passed，包括唯一真实 baseline 资源、实际 authority／架构约束及资源完整性。 |
| 基线失败修正后的 Code Mode／协作验证 | `tests/test_agent_collaboration_api.py` 与 `tests/test_code_mode_runtime.py`：**184 passed，另 15 subtests passed**。删除整句错误文案锁定，保留异常类型、单项／batch 的捕获顺序及零准入副作用。 |
| 完整依赖环境中的 migration 验证 | 本机已有完整 Homebrew PostgreSQL 17.9 的临时私有实例，扩展为 pg_search 0.26.1、vector 0.8.1、intagg 1.1；`tests/test_stage5_clean_migration.py`：**15 passed**，原扩展保留用例未改。 |
| 生成产物与代码检查 | protoc／snapshot 检查、生产改动文件 ruff、`compileall`、`git diff --check` 通过。改动 Python 文件相对同组基线的新增 lint 诊断为 0，未借机清理无关既有告警。 |

各组存在重叠，不将这些通过数相加为唯一测试总数。规格要求的纯标签用例和断言直接删除；没有新增 absence 测试、skip／xfail，也没有放宽实际行为断言。未修改的实际 reset 错误 code 保留；没有为了名称整齐改变可观察错误原因。

环境与范围：测试使用经确认的本机私有 PostgreSQL socket，由原 helper 创建并清理隔离库；未重置正在运行的 `pulsara`，未改保存设置，未部署或重启用户应用。真实 SDK 本地 HTTP 测试在测试进程设置 loopback `NO_PROXY`，不改生产代理行为。未调用远端模型。

## 4. 基线失败的处置与结论边界

1. `test_compatible_vector_and_unrelated_native_extension_are_preserved` 有真实意义：必须保留已有扩展的 OID、版本和实际功能，不能因环境缺包删除用例。打包应用提供的 PostgreSQL 未包含可选 contrib `intagg`；完整本机 PostgreSQL 17 已提供它及全部产品必需扩展。使用现有 `initdb`／`pg_ctl` 创建独立临时实例，仅监听私有 Unix socket，通过原 helper 创建测试数据库，完整 migration 文件 15 项通过。测试及生产代码未为此改变，未往用户应用目录补文件，临时服务已停止、目录已清理；以后运行此用例仍应按 `tests/linux/README.md` 准备完整测试依赖。
2. `test_code_mode_single_and_batch_reject_removed_profile_before_admission` 的整句文案期待没有独立业务必要。现在记录和检查 `TypeError`，继续确认两种输入的 single／batch 错误均可捕获，且 admissions、rows、wakes 全部不变；不将生产错误文案改回旧字符串。原有拼写建议测试仍验证提示功能。相关两文件 184 项及 15 个 subtests 通过。
3. Desktop 的 `real numeric nonzero preserves command output` 与 `output overflow after a real side effect remains unknown`：完整运行时 native fixture 得到的 child code 为 null，与预期非零退出／MAXBUFFER 不同；独立基线完整运行复现相同两项失败，独立基线单文件运行 5 项通过。`desktop/src/postgres-posix.ts` 与对应测试相对基线没有 diff。本次不通过修改进程语义、延长限制或跳过测试解决无关问题。父级聚合使当前完整集合记录为 3 条失败。

在本次明确允许删除的接口／持久格式字段之外，逐项复核、四方交审、基线业务输出对照和整合测试未发现本轮引入的产品语义回归。这个结论不等于全部仓库测试为绿，也不承担旧内部载荷兼容或尚未实施产品草案的行为承诺。
