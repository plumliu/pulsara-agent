# 记忆一级页面与级联删除实施记录

日期：2026-09-05。实施基线：`291eee83`。未提交、未 push。

## 实际落地

- `memory/management.py`：完整管理 DTO、相对关系、反向 BASED_ON 闭包、恢复例外、完整确认记录与 keyset cursor；不引入 fingerprint 或预览 registry。
- `_repository/memory_management.py`：独立 catalog/detail/project 查询；用户确认后的 SERIALIZABLE 物理删除；candidate/ref 归属、幸存 candidate 归一、SUPERSEDED 恢复与响应偏好容量检查；精确 RETURNING 和事务内重新规划。
- `web_app/memory_controller.py`：服务端决定记忆域，逐条 NDJSON 传输；完整上传后才进入删除事务；大于 8 MiB 的确认不受普通 JSON body 总大小限制。临时文件单一 owner，执行物理退出后关闭。
- `frontend/components/memory-view.tsx`、`lib/memory-api.ts`：一级入口、跨对话/项目、四类筛选、搜索、历史、分页、页内详情、来源定位、完整影响预览、恢复例外选择、409 新预览与用户再次确认。
- 删除不改 canonical transcript、不重写当前 epoch；下一次记忆冻结读取新的事实集合。
- 收回 child surface 的 `remember`，恢复前置规范的 Main Agent-only candidate 来源。

没有新增表、列、索引、触发器、event、job、checkpoint、tombstone 或总图大小/深度上限。

数据库变更只涉及既有 pair FK 的延迟 NO ACTION、runtime 五表 DELETE 权限，以及 relations/两张 refs 表的 UPDATE 权限。后者是 PostgreSQL 执行 SELECT FOR UPDATE 的实际权限要求，不是新增产品编辑入口。同步更新现有 clean-v0 schema oracle；未新建 fingerprint 体系。

前端沿用现有 Sites 项目配色和组件风格，没有发布外部网站或修改 hosting 配置。

## 验证命令

以下 PostgreSQL 命令使用仓库 `.venv`，设置：

```sh
export PULSARA_POSTGRES_ADMIN_DSN=postgresql://plumliu@localhost:5432/pulsara
export PULSARA_POSTGRES_DSN=postgresql://pulsara:pulsara@localhost:5432/pulsara
```

测试基础设施创建并清理独立临时数据库，不重置上述用户数据库。

```sh
.venv/bin/python -m pytest -q tests/test_memory_management_contracts.py tests/test_memory_management_postgres.py tests/test_local_web_memory_management.py tests/test_memory_governance_semantics.py tests/test_round8_advisory_memory.py --tb=short --show-capture=no
# 131 passed，157.89s

.venv/bin/python -m pytest -q tests/test_stage5_clean_migration.py tests/test_stage2_conversation_kernel_postgres.py tests/test_memory_governance_semantics.py tests/test_memory_management_postgres.py tests/test_local_web_session_order_postgres.py --tb=short --show-capture=no
# 129 passed，133.10s

.venv/bin/python -m pytest -q tests/test_local_web_http_surface.py tests/test_local_web_memory_management.py --tb=short --show-capture=no
# 20 passed，1.72s

.venv/bin/python -m pytest -q tests/test_stage2_conversation_runner.py tests/test_round9_unified_capability_semantics.py tests/test_repository_modularization_architecture.py tests/test_fingerprint_subtraction_architecture.py --tb=short --show-capture=no
# 128 passed，45.61s

cd frontend
npx tsc --noEmit
npm test -- --run
# 89 passed / 5 files
npm run build
npm run build:local
# 两种构建均成功；本地 packaged static assets 已更新。
```

另运行 Ruff（新增生产文件、测试、inventory 工具）及 `git diff --check`，通过。

迭代验证还运行了：纯管理契约 12 passed；clean migration + round8 + governance 98 passed；architecture + HTTP + memory HTTP 24 passed；并发双删/锁后新增依赖 2 passed；已删除候选不能复活 1 passed；下一轮 preference freeze 1 passed。以上与最终套件重复，不累加成唯一测试数量。

## 失败与修正记录

1. 初次 PostgreSQL 删除测试出现 FOR UPDATE 权限不足。按 PostgreSQL 实际权限模型补充上述最小表权限，并同步规范/grants/oracle；随后全部通过。
2. 大分页 fixture 创建超过 100 条事实时原测试 writer lease 过期。fixture 每次 seed 前续租既有 lease；没有提高生产上限或放宽断言。
3. 组合测试两次暴露旧 round8 测试用全表 `count(BASED_ON)=1` 的隔离错误：实际为 107，包含新测试其他记忆域的 106 条关系。独立运行该旧测试通过。最终改为查询该测试的精确 target，并断言唯一 `(decision.fact_id, correct.fact_id)`；组合 131 项通过，未弱化关系语义校验。
4. architecture inventory 原先假设所有内部 frozen dataclass 都位于 contracts，现按类实际源码模块定位；仅把新增管理 owner/helper 纳入显式清单。
5. 前端旧断言不允许记忆导航，按本功能改为要求入口存在；修复原测试文件的 modelCatalog 返回类型及 DOM 类型断言，未删除行为断言。
6. browser streaming request 无法用于本地 HTTP/1.1 listener。上传改为逐记录 Blob parts，不构建单个大 JSON 字符串；服务端仍增量读取，响应仍使用流式 NDJSON。规范已注明公开协议限制。
7. 不完整删除响应不能证明“尚未删除”，前端改为要求刷新核对结果；未引入 durable receipt 或重试执行机制。

尚有既有 aiohttp shutdown_timeout 弃用警告、构建 chunk-size/构建工具警告；本轮未以忽略错误或扩大总量限制处理它们。

## 浏览器与真实数据库 dogfood

启动命令（不调用模型、不读取真实 API key）：

```sh
.venv/bin/python -m tests.support.memory_management_dogfood
```

使用真实 LocalWebApplication、通用 HTTP API 和 PostgreSQL；种子通过既有 candidate/governance repository 路径创建。

实测过程：

- 跨对话列表、关于你筛选、完整详情与双向“存在冲突”文案。
- 删除冲突一端后另一端保留且不再显示需要确认。
- 删除全局依据，预览含全局派生事实与项目派生事实，共三条事实、两条关系；确认成功。
- 查看第一/二版历史；删除第二版时显示 surviving-ancestry 恢复例外，确认按钮禁用；用户选择第一版一起删除，重新预览后成功，第三版保留。
- 双浏览器窗口：第一窗口预览删除新偏好；第二窗口删除旧偏好；第一窗口提交旧预览收到 409 和新预览，再次确认后成功。
- 最终 fixture 使用真实临时目录和正式 workspace identity；项目选择器只含“周末旅行”，排除快速开始，项目详情可看到跨对话依据。
- 点击“在对话中查看”，成功重开真实来源会话并显示原 user/assistant/remember 工具记录，无 provider 调用。

最初 fixture 的虚构目录只能验证管理页面，不能验证 resume；已改为真实临时目录后重测来源成功。closed-project 保留、104 条直接关系分页、105 条 catalog、上传超过 8 MiB、缺失 END、readiness、域隔离、并发删除/治理、偏好恢复碰撞等由 PostgreSQL/HTTP/前端测试覆盖。

数据库隔离实例：`pulsara_test_main_60577_804c4ba4ba`、`pulsara_test_main_90554_bc617212c7`、`pulsara_test_main_95417_0859798c58`。这些只是本轮可重建的测试数据，不是用户的 `pulsara` 数据库。

收尾已停止三个测试服务，查询 `pg_database` 确认这些实例均已删除；临时种子可用上述命令重新生成。停止时的 KeyboardInterrupt 是主动 Ctrl-C 的退出结果。

## 使用边界

这是 clean-v0 hard cut。旧 schema 需要按仓库现有流程重建，未提供在线迁移或双路径；本轮未清空用户数据库。删除后原始对话保留，未来对话若再次明确记住相同内容，advisory 记忆仍可重新形成；本功能不承诺永久遗忘。

## 用户后续明确要求：重置当前数据库

实施验证完成后，用户明确要求重置当前生产配置数据库。重新读取 `/Users/plumliu/.pulsara/local-settings.yaml`，验证 admin/runtime 均指向本机 `localhost:5432/pulsara` 且无其他连接后，删除 `pulsara_v3` schema 与 `public.pulsara_schema_migrations`，再执行 `.venv/bin/pulsara db migrate` 和 `.venv/bin/pulsara db verify --deep`。前者成功重建 clean-v0，后者通过。

重置前 7 个会话、129 条 transcript、0 条 memory fact；重建后三项均为 0。未制作备份，此次物理清空不可由本操作撤销。模型配置、API key、本地设置、工作目录及其他数据库未更改。上文“未清空用户数据库”描述的是后续授权前的实施验证阶段。
