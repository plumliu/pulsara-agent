# 正式首启 schema drift：资源换行边界修复

2026-10-25。真实NSIS安装完成后的首启失败为生产缺陷，本报告不将其改写成测试fixture问题。当前正式cluster没有由本人迁移/修改/启停，已安装payload没有由本人修改；用户尚未配置provider，未发模型调用。

## 实际根因与只读现场

`installed-first-start-failure.jsonl` 来自已安装Python及原verifier，只读SQL事务：PostgreSQL17.9，runtime/admin角色和pulsara数据库已存在，pg_search0.26.1/vector0.8.1及pulsara_v3齐全。canonical sessions行数为0。system_identifier=7700516472209506840，data=`/home/plumwsl/.pulsara/desktop/postgres`，socket=`/home/plumwsl/.pulsara/desktop/socket`，无TCP监听，postmaster start=2026-10-25 16:16:55.063366+08:00。

原RuntimeConnectionFactory.verify的真实code为`schema_catalog_drift`，不是初始化许可或缺库。Windows checkout→wheel→已装baseline SQL保留2793个CRLF；原registry对raw bytes算身份并decode执行，五个PL/pgSQL函数体保存CRLF（735/79/18/21/4）。原catalog观察为`sha256:3f4309ea8c76bb3c58e4da50b6d8f6c09af78e74427236e03c3dc53d95152d4c`；固定期望为`sha256:812aaf3157354f2ce2b0b2765ae07128c31a01579cadac3cf3ae4e3d08f80f42`。仅在诊断内存中将原观察payload的CRLF转换LF后恰与固定期望一致，未写数据库、未改变生产verifier。

## 正式最小修复

- `storage/migrations/registry.py` 单一私有资源输入函数将仓库文本CRLF规范为LF。原checksum、SQL execution bytes、辅助JSON完整性检查全部消费相同canonical bytes。相对原LF资源，其既有migration universe身份不变；Windows原raw-CRLF身份不另建兼容/迁移分支。
- `.gitattributes` 对migration SQL/JSON声明LF，减少源包污染；运行时边界仍处理外部wheel/checkout换行，不只依赖Git属性。
- 固定expected catalog、数据库observed verifier、baseline SQL语义/表/事件/oracle均不修改。已装坏cluster仍应报drift，由root按精确身份/零canonical记录及原owner明确reset/重建，不ALTER函数或修ledger冒充成功。

## 验证与原失败

`tests/test_migration_resource_portability.py` 用LF/CRLF两种真实资源输入验证同一canonical身份和实际执行SQL；变更SQL正文或辅助manifest仍必须checksum拒绝。`lf-resource-tests.json`：私有源码中Git HEAD原registry两项失败；当前registry两项通过。Ruff通过。Windows项目venv的pytest conftest缺psycopg，原exit4保留为`windows-resource-tests-missing-dependency.log`；功能回归使用可用WSL Python3.12.15，不记作Windows原生Python通过。

`prepare_lf_fresh.py` 从当前Windows源码构建真实wheel，经uv --target安装到独立ext4目录，复用已装standalone Python的依赖；不覆盖正式payload或Linux主clone。`lf_fresh_owner.cjs` 使用锁定Windows Node22.23.3、原BundledPostgres/WSL client/host/Runtime；仅测试模块lookup指向本次已安装wheel，无复制owner或跳过数据库准备。真正新home首启后原DesktopApplication.ready、实际Windows HTTPS bootstrap、原deep verifier通过；正常stop后同home重开再次通过，initialized依次true/false，五个函数正文CR均0，canonical sessions仍0。`lf-fresh-result.json` 为最终完整事实，application代码实际来自新wheel的installed路径。

三个额外测试失败均原样保留：first将origin与api直接拼接导致InvalidURL（已实际ready）；second即时Windows连接ECONNREFUSED（已ready）；third误用不存在的`/api/bootstrap`返回404（deep verifier已过但当时未单存该SQL结果，不能倒补原记录）。最终从真实router核对`/api/app/bootstrap`。仅修harness地址，未削弱HTTP200/TLS或SQL断言。

## 新发现的Windows转发时点边界

最终case没有取消网络原断言，而在同一ready实例上记录有限只读GET观察，绝不重发PG启动/SQL写入、未在生产加入重试：

| attempt | 原Runtime ready后首次Windows请求 | 后续同一实例 | 首次HTTPS200完成 |
| --- | --- | --- | --- |
| fresh initialized=true，port36375 | +0ms发起，+5ms ECONNREFUSED | +261ms仍拒；+524ms发起成功请求 | +531ms |
| reopen initialized=false，port44147 | +0ms发起，+2ms ECONNREFUSED | 后续逐次事实见networkReadiness数组 | +1051ms |

TLS CA使用本attempt交付的完整公证书。10秒只约束此只读实验观察，不是产品总寿命cap。Linux site.start/ready不能单独证明Windows localhost转发已可达；这会影响正式main立即loadURL，已即时报root。root/races负责在原Session/pin owner中提供Windows端只读连接就绪、Quit取消的正式最小接缝；本报告不冒称此网络缺口已由schema修复解决。旧refused/404原记录不删除。

## 精确清理与范围

四个测试根 `.pg-lf-9kr8swmd`、`.pg-lf-50s3ck1m`、`.pg-lf-_35snsew`、`.pg-lf-axbhvys_` 都先由原Runtime/PG owner关闭，再真实pg_ctl status3和当前kernel/PG两锁可得后删除整个自有目录。对应first/second/third及最终cleanup JSON留存。没有接管正式`.pulsara`、不动共享Ubuntu生命周期、interop/TUN不变。最后wheel/目标目录已删除，重现须用prepare创建新唯一home，不复用已删config。

生产源码与测试现已freeze交root/races重建；这只关闭本次schema可移植性缺陷，不表示完整NSIS图形/网络/provider或Mac/native Ubuntu资格。

## 补跑受影响的既有迁移套件

按root追加要求，在新的私有ext4源码快照/独立PG17.9中执行原 `tests/test_stage5_clean_migration.py` 全部case及新增portability两项。复用原Python依赖和原测试DB factory，只向本次测试进程注入确切Unix socket DSN；临时cwd无.env，不读取保存settings，不改Linux主clone。结果见 `existing-migration-regression.json`：**14 passed / 1 failed**，6.75秒。原resource_bytes身份/golden、clean-v0重复migrate、binding、catalog/function/trigger/grant drift、commit确认及新LF/CRLF用例均通过。

唯一失败是原 `test_compatible_vector_and_unrelated_pgcrypto_are_adopted_not_removed` 在其准备阶段 `CREATE EXTENSION pgcrypto` 即失败：随包资源没有 `share/extension/pgcrypto.control`，还没有执行该case的migrate或本次资源验证。只读核原17.9构建源确认pgcrypto源码存在，但当前PG没有配置OpenSSL，环境也未发现系统OpenSSL开发头，因此不是现成可直接复用的extension输入。

root明确接受此项作为原套件**测试资源前置限制**：保留原失败，不skip、不换成别的extension、不改断言；不为单条测试下载/编译SSL或扩充正式PG依赖。没有因此新增pgcrypto/OpenSSL产品支持声明。先前fresh installed首启/重开deep验证与本轮原resource身份通过仍各自单列，不把它们相加冒称原套件15/15。

用户随后明确澄清：pgcrypto是此前性能测试安装的扩展，生产不需要。本轮维持不加入正式资源的决定；原“保留无关扩展”测试不删除，其前置缺失与失败记录继续保留，不再为此扩大调查或支持范围。

这次 `.pg-reg-fjhn8n_i` 的test数据库由原fixture逐个清理，集群正常stop后真实status3、kernel/PG两锁可得，再删除整个自有源码/PG树；`existing-migration-regression.json` 的removed=true。未重复此前两轮fresh应用验证，provider继续等待用户GUI配置。生产代码保持此前freeze，无为回归绿灯的新修改。

## 最后Supervisor只读交叉审查

按root要求核了tasks新增Host MCP清理接缝，未运行或修改其文件：_queue_connect在锁内持有connection task；await SDK import取消时原helper先等worker再重抛，不晚构建slot。同步factory→slot构造→all_slots登记到client.open首次await之间没有事件循环窗口。Host close snapshot要么已经持slot，要么持connection task并取消/join其cleanup；open后的closed检查拒发布，cleanup失败保留slot/anchor，唯一close_task保存并向HostSession传原异常。已读新增未发布slot/重复close的两项回归，未发现当前支持API下的late-registration遗漏；不冒领tasks的131/28项测试结果。
