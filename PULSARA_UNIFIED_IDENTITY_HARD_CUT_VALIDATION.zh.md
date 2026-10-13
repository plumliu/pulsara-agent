# 统一身份 hard-cut 实现与验证记录

日期：2026-10-14。实施依据：[冻结规格](PULSARA_UNIFIED_IDENTITY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。改造前基线 Git `5df79ba9`。

状态：代码实施与交叉复核完成；初轮后端及前端测试通过，后续五处修复的全量运行与唯一夹具失败修复结果见 §8。真实多模态与工具引用场景通过；真实纯文本 provider 验证有明确配置缺口。尚未切换用户生产 home；用户已授权暂存并提交本轮改动。图片与 HTML 混合展示继续延期。

## 1 实现与审阅顺序

按用户要求，先完成生产代码，再由此前审阅冻结规格的同一 GPT‑6 Astra high 子代理审阅；主代理核查并修复后，双方同意进入 pytest 更新阶段。此时没有提前修改 pytest 来获得绿色，进入门槛记录在规格 §14.2。

测试阶段暴露的真实生产问题再次交给同一审阅者复核，包括 fork 历史工具结果来源、explicit completion 事件来源、无 workflow 的 Plan 参数校验及 terminal PREPARING 取消结算。最终审阅结论：没有未解决的生产阻塞，可以收口代码审阅。测试通过和真实 provider 证据单独记录，不能由审阅意见替代。

## 2 已落地的单一路径

- Pulsara 业务身份统一为 `<kind>:<26位小写规范Base32>`，承载128 bit。Python、浏览器和 clean-v0 使用同一规范，随机与派生身份的分配仍由既有 owner 管理。
- 派生身份按冻结角色及完整来源生成；存活 owner 对不同完整事实的冲突拒绝并回滚。随机身份在既有分配边界发布前处理碰撞，冻结后不重新编号。没有别名目录、在线迁移、新旧双读或兼容层。
- 图片使用 workspace 绑定的 `image:` canonical blob 身份；在最终 body、hook、command 与 quote 冻结之前绑定。图片退出视觉上下文后保留中立可操作引用，显式历史读取可以找回。原 session/workspace 权限、完整 bytes 验证、fork owner 和 GC 契约保留。
- 历史、artifact、memory、任务、Plan、调度、terminal、配置及 UI 关联同步切换；fork 导入来源允许 `history:`，当前执行与 predecessor 仍要求 `turn:`。导入历史不获得执行资格。
- 文件 revision 使用完整256-bit `revision:` 加52位Base32。内容完整性、权限确认、provider/source/replay 契约和 cursor MAC 继续使用原完整摘要或令牌。外部 provider ID、用户正文、MCP payload、Git SHA 不做字符串改写。
- LocalSettings schema 升为 v5；terminal 握手 major 升为4，显式传递 command admission deadline；只接受新契约。已有 protobuf 文件/包的 v3 命名不代表保留旧 wire 支持。
- clean-v0 仍为31张应用表，加原有 schema migration 表；没有新增身份 registry、事件类别或任务恢复机制。前端打包静态资源与源代码同步重建。

## 3 自动化验证

| 验证 | 结果 | 本地证据 |
| --- | --- | --- |
| 全量 pytest | **3289 passed**，62条依赖弃用警告，245.62秒；无 skip/xfail 掩盖 | [完整日志](output/identity-hard-cut-20261014/pytest-final.log)、[JUnit](output/identity-hard-cut-20261014/pytest-final.xml) |
| 最后清理后的聚焦回归 | **57 passed**：身份、预算、浏览器控制权 | [日志](output/identity-hard-cut-20261014/final-cleanup-tests.log) |
| 前端 Vitest | **706 passed，46 files** | [日志](output/identity-hard-cut-20261014/frontend-final2.log) |
| TypeScript | `tsc --noEmit` 通过 | [日志](output/identity-hard-cut-20261014/frontend-types-final.log) |
| 前端生产构建 | `npm --prefix frontend run build:local` 通过；保留构建器已有 chunk-size 提示 | [日志](output/identity-hard-cut-20261014/frontend-build.log) |
| Ruff / diff 格式 | 全仓 `ruff check` 与 `git diff --check` 通过 | [Ruff 日志](output/identity-hard-cut-20261014/ruff-final.log) |
| clean-v0 与碰撞 | 新建隔离本地 DB、安装/重复安装/schema 核验、真实 blob 幂等和强制 D 碰撞回滚通过 | [日志](output/identity-hard-cut-20261014/schema-final.log) |

本机 Node 25 的实验性 Web Storage 影响测试环境；Vitest 使用 `NODE_OPTIONS=--no-experimental-webstorage`，没有为此修改产品代码。

新增身份测试覆盖规范 padding、kind 与角色隔离、UTF-8 前像、128/256-bit 向量、开放及完成 block 的派生碰撞、fork 来源和当前 turn 的区别、真实 PostgreSQL blob 碰撞回滚、真实 terminal 分配碰撞且不覆盖已有 sibling。其余原有 owner、长任务、prefix、计划/审批、分页、文件版本、删除/GC 和协议测试通过完整套件继续验证。

测试更新只替换已经改变的身份、schema 和序列化契约，并同步 fixture 关联；没有放宽原产品行为断言。第五次完整运行在3246条通过后因一个 fixture 的 session map key 未更新而等待，已修正关联，保留原 takeover 断言；上表是随后完整运行的成功结果。早期失败日志保留用于排查，不冒充成功证据。

## 4 真实 provider dogfood

使用用户已保存的生产设置，经 `LocalSettingsStore` 和有效 home resolver 只读取得。在旧 schema 仍可读取时取得配置值，经仅内存、受本机权限限制的通道交付本次验证；新身份由新版 owner 装配，测试注入只读配置并使用验证过的 disposable local DB。未覆盖 `~/.pulsara/local-settings.yaml`，未把凭据写入 shell 环境、fixture 或报告，交付代码没有旧 schema decoder。

通过场景使用保存的 OpenRouter 连接、`openai/gpt-6-luna`，记录实际 HTTP 请求、provider 回复和工具调用，核对与冻结 wire projection 相同。

| 场景 | 已观察行为 | 证据 |
| --- | --- | --- |
| 图片输入、压缩、引用恢复 | 真实摘要压缩采用 offload；中立后续问题未触发工具；明确要求时 `view_image(image_ref=...)` 成功并回答像素内容；同 epoch append-only；再次压缩后 cold canonical 读取成立；显式 `read_session_content` 找回引用后重读图片 | [报告](output/identity-hard-cut-20261014/dogfood-image-luna-2/report.json) |
| 实际工具引用往返 | `remember → memory_get`；超长 terminal 输出的 `artifact_read`；`list_agent_models → spawn_agent` 精确 connection，再 `wait_agent` 精确 task，得到42 | [报告](output/identity-hard-cut-20261014/dogfood-refs-2/report.json) |
| 旧/新格式复制对照 | 模型将7类各3组配对样例原样输出：旧21/21、新21/21。旧字符串仅作为用户测试数据，没有提交给资源解析入口 | [报告](output/identity-hard-cut-20261014/dogfood-copy/report.json) |

图片场景区分真实自然摘要和受控摘要丢引用：前者证明实际压缩采用 offload；后者故意让摘要不携带引用，再验证 canonical 历史恢复，不声称模型恰好自然遗忘了引用。复制对照是小样本逐字复制检查，不是旧系统端到端运行，也不能证明错误率改善；实际新引用的工具往返由独立场景证明。

保留的失败尝试包括 Qwen TLS 连接失败、首次诊断 wrapper 缺少 multiprocessing main guard、首轮 refs observer 未转发 `frozen_target_fact`。后两者修复的是验证 harness，成功报告来自修复后真实重跑。失败证据没有删除或改成通过。

**真实纯文本 provider 缺口：** 9个保存连接均声明多模态或能力未知；DeepSeek `deepseek-flash` 当前 catalog 声明 `text,image`。尝试在能力断言处明确失败，见[报告](output/identity-hard-cut-20261014/dogfood-text/report.json)。没有把它改成假纯文本，也没有把 unknown 当作 text-only。纯文本占位/不传视觉/历史恢复的自动化测试通过，但这不替代真实纯文本场景；按规格 §12 明确记录未配置时的验证缺口。

这些隔离场景未复制外置 MCP、Hook、用户 Skills、插件及 trust 资料，未声称完成生产 home 的等价启用验收。相应既有 owner 的自动化测试仍在完整套件中；正式启用应按规格 §10.2 逐 owner 准备资料，不自动重签 trust。

## 5 实际 tokenizer 测量

用 tiktoken 0.14.0 的 `cl100k_base` 与 `o200k_base`，对每类100组确定性样例的单字段紧凑 JSON 实际分词。旧样例按基线格式/配方构造，新样例调用当前生产生成函数；固定样例避免全零等特殊字符串偏差，但不代表生产分布。以下列出 o200k 均值；完整输入、双 tokenizer 结果和方法见[测量 JSON](output/identity-hard-cut-20261014/tokenizer-comparison.json)与[复现脚本](output/identity-hard-cut-20261014/compare_tokenizers.py)。没有给产品新增 tokenizer 依赖。

| 引用 | 字符数：旧 → 新 | JSON token 均值：旧 → 新 |
| --- | --- | --- |
| image | 71 → 32 | 44.72 → 22.61 |
| artifact | 85 → 35 | 46.32 → 22.54 |
| memory | 71 → 33 | 43.76 → 22.49 |
| task | 78 → 31 | 45.41 → 22.63 |
| connection | 49 → 37 | 27.00 → 22.32 |
| session | 40 → 34 | 25.45 → 22.73 |
| 完整 revision | 71 → 61 | 44.72 → 38.57 |

从基线与当前 AST 提取的实际 `view_image` 工具说明及 image_ref 参数说明，也分别测量：o200k 为170 → 168，cl100k 为171 → 169。说明保留“复制已给出的引用”语义，不要求模型理解派生算法或主动搜索旧图。

这两种 tokenizer 不被宣称为上述 provider/model 的实际分词器；表内 token 数不包含完整工具 schema、消息封装或图片费用，也不直接预测账单、缓存收益或模型复制准确率。

## 6 剩余身份候选复查

再次扫描生产 Python 与前端源码，旧 `subagent-task:`、`subagent-batch:`、`model-connection:`、`artifact:tool-result:`、`memory-relation:`、`steer-entry:` 和旧 SHA 图片引用说明没有命中。

保留 SHA/随机候选的 owner 分类如下，原始候选列表保留于本地[扫描结果](output/identity-hard-cut-20261014/residual-identity-scan.txt)：

- identity builder 内部派生；文件完整 revision 编码；不可变 bytes/HTML/blob/terminal 输出的完整性；provider compiled/source/replay/estimator/权限等完整契约确认。
- schema baseline/catalog/grant 的原有完整校验；memory embedding cache namespace；分页绑定/校验及 HMAC；MCP 的带密钥 opaque token。
- Hook/Skill/package/trust 所需原有内容或定义确认；用户/外部 provider 原始数据不被截断或重命名。
- UUID 仅余临时文件/目录、上传本地文件名、DOM/编辑草稿局部 key，以及 MCP SDK 所需整数 epoch。UI 动画/提示条局部随机值没有被升级成业务实体。

扫描文件是验证辅助，不是运行时身份注册表，不包含逐文件 code SHA 或工作树指纹。

## 7 启用边界

本轮未切换生产 home，也未就地转换旧数据库或旧配置。新版拒绝旧 LocalSettings schema 和旧 terminal major；正式启用必须准备新版配置并重建已核实可丢弃的本地 clean-v0，具体步骤及外置资料 owner 见规格 §10。用户原生产配置保留原样。

本次创建的验证数据库及仅内存凭据服务在验收后清理；原始测试与 dogfood 输出保留在 `output/identity-hard-cut-20261014/`，该目录不自动提交。未来仅检查本仓库提交时，应以本记录的覆盖范围为准，不能假定忽略目录中的大体积 provider 报告会随 Git 一并存在。

## 8 原三位审计者复查后的五处修复

2026-10-14，关闭原哈希复查发现的五处问题：terminal decision 入场碰撞后误清理旧 owner、旧 controller attachment 影响新连接、mailbox 派生 entry/event 碰撞污染健康批次、scheduled HTTP 身份入口校验遗漏、terminal monitor cancel 将错误格式当作 not-found。具体 owner 边界见实施规格 §14.4。

复核期间进一步补齐同类 scheduled update/switch/pause/resume/delete 及 run-once 入口；run-once 在查询既有 command 接受事实之前校验 command/session/schedule。没有增加身份注册表、兼容解析、持久事件或新生命周期上限。

原三位 GPT‑6.1 Sol high 审计者均完成只读复核并通过：

- `audit_model_file_hashes`：terminal 分配重试、显式冲突及 capacity 拒绝不会结算旧 slot；失败启动由 registry 负责结算；monitor 参数、授权和 owner 校验一致。独立相关测试9条通过，另以内存注入验证持续碰撞受原 deadline 约束。
- `audit_model_runtime_hashes`：attachment 保留至旧连接清理结束，检查与绑定之间无 await；scheduled HTTP 读写及 run-once 精确解析闭合。独立新增回归43条及既有协议/bridge/调度测试52条通过。
- `audit_model_resource_hashes`：mailbox 对全部保留 recipient 检查 entry/event，幂等检查及 ordinal 保持；在途批次直到接受/确认后才退役。独立回归4条通过，另以内存接受替身验证在途碰撞拒绝后健康批次仍能完成；该补充验证不冒充数据库事务测试。

主代理复核相同生产路径及失败/清理分支，并完成相关测试：

| 验证 | 结果 | 本地证据 |
| --- | --- | --- |
| terminal / protocol / 长任务既有回归 | 102 passed | [日志](output/identity-hard-cut-20261014/followup-terminal-protocol-tests.log) |
| mailbox / scheduled / 第一批边界回归 | 119 passed | [日志](output/identity-hard-cut-20261014/followup-mailbox-scheduled-tests.log) |
| 补齐入口后的边界 / scheduled host / PostgreSQL 回归 | 101 passed；含53条新增边界用例 | [日志](output/identity-hard-cut-20261014/followup-scheduled-final-tests.log) |
| 本轮全量 pytest | 3343 passed / 1 failed，62条依赖弃用警告，716.17秒；唯一失败为下述旧测试夹具 | [日志](output/identity-hard-cut-20261014/pytest-followup.log)、[JUnit](output/identity-hard-cut-20261014/pytest-followup.xml) |
| 唯一失败修复后的 HTTP 文件回归 | 19 passed；原成功路径断言保留 | [日志](output/identity-hard-cut-20261014/followup-http-final.log) |
| Ruff / diff 格式 | Ruff 及手写文件 diff 检查通过；暂存新增构建资源后，生成的 JS 中一处 Markdown 字符串尾随空格被 diff 检查报告，保留其原始字符串语义 | [Ruff 日志](output/identity-hard-cut-20261014/ruff-followup.log) |

新增边界测试覆盖真实本地 terminal 进程、真实 gateway 接受/断线清理、entry/event × 同/异 recipient 碰撞，以及非法 ID 在查询前被拒绝。现有 monitor 授权测试增加旧格式与错误 kind 两个场景。没有放宽原断言或增加 skip/xfail。

全量运行唯一失败为 `test_scheduled_http_uses_shared_run_once_and_exposes_atomic_switch`：成功路径的 service mock 原先掩盖了 `task-a` / `task-b` 非法夹具；入口补齐后 switch 正确返回400。只将该用例改为当前 `schedule:` ID，URL、请求和 mock 断言同步引用同一值，保留成功、拒绝额外字段及切换调用断言。用户要求其余用例不再复跑时，已启动的 HTTP 文件19条测试已完成通过；此后不再重复测试。保留上述全量失败日志，不将分开运行的修复验证表述为另一次全量绿色运行。

按用户要求，本轮没有重跑真实 provider dogfood；§4 保留的是上一轮已运行的结果与其明确缺口。本轮没有前端变更，未重复前端构建或测试。
