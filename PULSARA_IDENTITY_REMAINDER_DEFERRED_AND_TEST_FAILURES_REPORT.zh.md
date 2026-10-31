# 身份收口：实施与验证报告

报告日期：2026-11-01。

本轮约定范围已完成实施与验证。worker 请求身份和固定 wire key／秘密重叠的后续收口均已按人类决议实现，验证范围分别列于下文。本轮没有 commit 或 push。完整命令、各批次结果及审阅记录见 [验收证据](benchmarks/suites/core/v1/identity_remainder_hard_cut_validation.json)，产品合同见 [实施规格](PULSARA_IDENTITY_REMAINDER_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。

## 本轮实现

本轮按各对象的原有生命周期使用共享身份生成和校验，覆盖浏览器、桌面请求、MCP、历史投影、草稿和 live 视图等路径。短 ID 不替代原有权限、owner 或完整验证事实；provider epoch 内 SYSTEM 和 tools 的稳定性以及 messages 只追加后缀的合同保持。

Context source 使用 typed source kind 作为唯一槽位，在语义摘要边界保留原有字节配方；人工确认直接使用 canonical decision ID。retain reservation 的回包严格校验 `request` kind，合法未知值仍为 no-op，非法值沿现有协议失败路径结算 pending。公开 package ID 投影保持真实身份和脱敏边界，动态环境变量、header 及工具 schema 按原数据处理。

## worker 请求身份收口

Code Mode prepare/call 的 request_id 统一由共享 `randomIdentity("request")` 生成；准备结果精确返回 `{request_id, peers}`，guest 的 batch 和 finally 只使用 prepared.request_id。正式 call 有自己的 request_id，通过 preparation 引用对应 prepare 请求；不保留旧计数 ID、prepared.id、别名或兼容读取。sequence、result/delivered/resume 握手、原 Cell pending/preparation 生命周期及已接纳副作用结算保持。

随机身份和校验复用一份 dependency-free JS identity core，renderer/Electron 直接引用，Code Mode 资源准备流程原样携带到 Python 包；worker 不依赖开发目录、额外 Node 包或旧算法回退。本次后续代码由 ROOT 自行修改，未调用 subagent。


本次后续验证：真实 Code Mode 76 passed；离线构建／隔离 uv 安装 wheel／随包 worker 6 passed；构建依赖失效 2 passed；renderer identity/browser 两文件 29 passed；Electron 独立回归 138 passed，renderer/Electron 类型检查及 renderer 构建通过。邻接 Code Mode 执行组合 24 passed、11 个 PostgreSQL 项因未配置 DSN 跳过，不宣称本次重跑了这些数据库测试。中间失败、重跑及范围限制见验收证据的 worker_request_followup；未改看门狗断言或放宽生产边界。

## 固定协议与动态数据的公开投影

先投影动态数据，再组装固定协议。CLI、模型查询／管理工具、交互表单、OAuth、用户库存、Hook 审阅及 provider 诊断均移除原整包重复脱敏。实际 package ID 仍由 PublicPackageId 表达可见／不可展示，raw ID、原 guard、CAS、权限和真实结算保持；没有整包重试、键名白名单或旧投影兜底。

能力重载只在 Host owner 投影一次，工具层直接序列化该结果；Hook stdin／外部输出仍沿原严格 JSON 边界处理。

任意外部 JSON 继续检查键和值，包括看似协议名称的 env、header、schema 属性名。键被改变或冲突时，只省略对应数据块，并以 `withheld_fields` 标明；APPLIED、ACK_UNKNOWN、last_known_cut 和其余数据照常返回。前端编辑器不会把这类省略当作空配置保存或测试。CLI 在原输出门禁中观察最新凭据后投影，Hook 准备保留旧凭据观察，轮换不丢保护。

该后续工作由 ROOT 自行实现，未调用 subagent；未修改用户保存的配置。主批次 400 passed、0 skipped；重载／Hook 补充批次 99 passed；最终碰撞／轮换批次 72 passed；前端 37 passed，类型检查与生产构建通过。这些批次有重叠，不相加作为唯一总数。详细命令、范围和结果见验收证据的 `public_projection_followup`。

## 验证与产物

以下是互相重叠的独立测试批次，**不能相加作为唯一测试总数**：

- PostgreSQL 综合回归：452 passed、0 skipped；更广 Python 回归：488 passed、0 skipped、1 warning。
- HTTP／协议 PostgreSQL 组合：212 passed、0 skipped、21 warnings。
- renderer 七文件回归：397 passed；插件相关九组回归：237 passed、1 skipped、1 warning。
- 前端两文件组合：380 passed；真实 compaction owner 相关组合：39 passed、0 skipped，独立模块复跑：35 passed。
- 上述 1 个 skipped 是 [大型 Hook 查询经 canonical PostgreSQL owner 往返测试](tests/test_capability_source_query.py#L913)，该无 DSN 批次未配置测试数据库；包含该文件的独立真实 PostgreSQL 批次已通过且无跳过。数据库测试使用已核实的本机隔离临时库。

frontend 类型检查与构建、正式资源准备及 macOS arm64 目录打包均已完成，包位于 `desktop/release/mac-arm64/Pulsara.app`。该批次打包时与当时源码对照的 422 个应用文件及 284 个 renderer 文件均无缺失、额外或差异；包内 sourcemap 的 app.tsx 和共享 identity 源与该批次当时源码完全一致，包内 Electron 的 Unicode 派生、随机 ID 和非法 padding 检查通过。desktop 测试为 138 passed，Electron 浏览器测试为 14 passed。各命令和日志见验收证据。

先前 macOS 应用包不代表后续 worker 或公开投影修改的应用打包验收；本次后续验证以各自记录为准。
