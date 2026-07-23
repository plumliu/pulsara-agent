# PostgreSQL Schema Migration Contract

_Created: 2026-07-22_

本文档定义 Pulsara PostgreSQL physical schema 的唯一长期 ownership 契约。它约束 migration、runtime startup、physical connection、测试数据库与部署切换；不替代 event payload、checkpoint、artifact 或 Oxigraph domain migration。

相关实现：

- [storage/migrations/](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/migrations)
- [postgres_connection_provider.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/postgres_connection_provider.py)
- [schema_verification_service.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/schema_verification_service.py)
- [schema_contract.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/schema_contract.py)
- [cli.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/cli.py)
- [test_schema_migrations.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_schema_migrations.py)
- [test_schema_hot_path_architecture.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_schema_hot_path_architecture.py)

## 1. 核心规则

1. `storage/migrations/sql/0000..NNNN` 是 Pulsara-owned PostgreSQL schema 的唯一真源。
2. 只有显式 `pulsara db migrate` 可以执行 schema DDL 或 runtime grant reconciliation。
3. Host、EventLog、Graph、Memory、Governance、Inspector、checkpoint、worker 与 benchmark measurement path 只做 verify/DML，不执行 DDL。
4. production PostgreSQL adapter 必须接收 verifier-issued `VerifiedPostgresConnectionProvider`，不得接收 raw DSN 或自行创建 connection/pool。
5. runtime role 不拥有 `CREATE TABLE`、`ALTER TABLE`、`CREATE EXTENSION` 等 schema authority。
6. V1 是 reset-only hard cut：存在 Pulsara-owned objects 但没有 migration ledger 的数据库是 unmanaged database，不推断、不接管、不 lazy migrate。
7. binary 只支持 registry 的 exact latest head。behind、ahead、checksum、contract、prefix 或 catalog drift 均 fail closed。

## 2. Migration Registry

Registry 是连续、不可变、forward-only 的 ordered definitions。每一项冻结：

- version 与 name；
- packaged SQL resource 与 SHA-256 checksum；
- transaction mode；
- postcondition contract；
- cumulative object manifest；
- runtime grant policy；
- migration contract fingerprint；
- cumulative registry prefix fingerprint。

Fingerprint 使用 domain-separated canonical JSON：

```text
sha256(domain UTF-8 || NUL || canonical JSON UTF-8)
```

Canonical JSON 的 map key 排序，tuple 保序；禁止 float、bytes、mutable list、set、未转换 enum 或 caller 自报 nested fingerprint。数据库 OID 只可用于一次 catalog join，不进入 schema semantic identity。PostgreSQL type/function/operator identity使用 schema-qualified logical name与 typmod。

Migration SQL 作为完整 UTF-8 resource 交给 psycopg，不按分号拆分、不模板化、不接受 caller SQL。Schema-object DDL 只能存在于 migration resources。动态 role ACL 只有 `PostgresRuntimeGrantExecutor` 可以通过固定 typed allowlist 与 `psycopg.sql.Identifier` 生成。

## 3. Durable Ledger

唯一 ledger 是 `public.pulsara_schema_migrations`。每行必须持久化：

- version/name/resource checksum；
- migration contract fingerprint；
- cumulative registry prefix fingerprint；
- applied timestamp与application version审计字段。

Applied history 必须精确为 `0..head`。每行 contract 与 prefix 都要从当前 binary 的 immutable registry重算；不能只核对 SQL checksum。Latest database 的 durable prefix 必须等于 binary registry fingerprint。

`0000` 是 canonical genesis。只有 ledger 与所有 Pulsara-reserved objects 都不存在时才能应用。已有 owned relation/function但缺少 ledger时返回 `schema_unmanaged_database`。

## 4. Migration Transaction 与确认

每个 version 在独立 transaction 中原子执行：

```text
DDL
+ postcondition
+ complete runtime grants
+ ledger row
```

Up-to-date privilege reconciliation也必须在一个 transaction 中完成 authority preflight、完整 missing-grant set、最终 effective verification与commit；任一失败整组rollback。

Migration/ACL commit 丢失连接后，原 connection不再有确认 authority。runner在同一 absolute deadline内使用新admin connection、重新取得同一 advisory lock并分类：

- `FULL`：完整 row/prefix/catalog或完整 grant set已提交；
- `NONE`：完整 pre-state仍成立，可在deadline内安全重试；
- `CONFLICT`：取得完整 authority且证明结构冲突，fail closed；
- `UNRESOLVED`：deadline内无法建立完整 authority，本次停止 mutation，之后可重新确认，不宣称 corruption，也不建议 reset。

## 5. Admin 与 Runtime Role

- `PULSARA_POSTGRES_ADMIN_DSN` 只由 db migration command读取；不得进入 `PulsaraSettings` runtime storage、wiring、日志、Inspector、event 或 benchmark sample。
- `PULSARA_POSTGRES_DSN` 绑定受限 runtime role；Host、Inspector、checkpoint 与 runtime benchmark 使用它。
- admin/runtime 必须连接同一 database name/OID。
- PostgreSQL server version必须在受支持区间内；V1 search path 的 `current_schemas(false)` 必须精确为 `("public",)`。
- runtime role需要 ledger SELECT、owned relations/functions/types 的精确 DML/USAGE/EXECUTE privileges，但不得拥有 schema DDL capability。

`PUBLIC` 已提供某项 effective privilege时不强制重写 extension-owned ACL。替换runtime role可以由显式 migrate命令做幂等 privilege reconciliation；旧role残留权限只报告，不隐式 revoke。

## 6. Verify-only Startup

Process-owned `PostgresSchemaVerificationService` 先用service-owned、deadline-bounded preflight取得database OID，再按
canonical database target、database OID、runtime role和registry fingerprint分区。一个resolved key只有一个shared
attempt；waiter cancellation只detach，不能取消共享physical verification。resolved-key verification的成功和失败均
缓存到service lifetime；尚未取得OID的preflight failure不形成key，后续borrower可重新preflight。Shutdown有界drain
全部preflight与verification owner。

Fast verifier必须在任何 Host session、terminal、MCP、workspace resource分配之前证明：

- exact ledger history/head/prefix；
- database/role/server/search-path identity；
- pgvector prerequisite；
- owned relation全部 columns、logical type/typmod、nullability、default、collation、generated/identity；
- runtime DML依赖的 constraints与index valid/ready/live state；
- minimum function execution contract；
- runtime effective privileges。

Deep verifier用于migration postcondition、offline doctor、template validation和drift审计；它进一步验证完整 index/constraint/function catalog与unexpected/missing objects。

## 7. Physical Connection Authority

`VerifiedPostgresAccessLease` 是 process-local、不可pickle/copy的借用 owner。Secret-safe binding不保存DSN。DSN只由 verifier-owned connection factory持有。

每个新physical connection必须在交付前完成endpoint、database identity与完整migration-ledger probe。只有完整probe证明
与binding不一致才永久invalidate provider；probe timeout、`QueryCanceled`或connection failure只淘汰当前connection，
不得将暂时无法证明误报为identity mismatch。

每个production direct connection、pool首次连接、idle replacement和broken reconnect在对adapter可见前重新验证：

- effective host/port/database/user/sslmode/application name；
- database name/OID；
- runtime role；
- exact search path；
- server version；
- migration head与durable registry prefix。

V1 conninfo拒绝 service/servicefile、多host、hostaddr、options、target-session-attrs、passfile、sslkey、caller-owned application name与unknown parameters。Production adapter不得提供 `dsn | provider` 双入口。显式 unverified provider只允许 tests/support 的 catalog-corruption negative tests。

## 8. CLI 与部署

CLI schema surface：

- `pulsara db status`：只读、bounded、secret-redacted状态；
- `pulsara db migrate`：admin-only mutation与runtime grant reconciliation；
- `pulsara db verify [--deep]`：使用runtime DSN验证，不修改schema或ACL。

`db status`必须在同一个physical connection和同一个repeatable-read、read-only transaction中取得database identity与
完整ledger history，不能用第二条preflight connection拼接report。

`db verify`公开的额外权限warning固定为`runtime_role_can_create_in_public_schema`。它只证明superuser或对`public`
schema的effective `CREATE` privilege，不声称穷尽任意owned-object ALTER/DROP authority。

这些命令只加载 storage env，不加载LLM/API key配置。标准部署顺序：

```text
stop Host/worker/Inspector/benchmark
-> backup if needed
-> V1 hard cut reset PostgreSQL and corresponding Oxigraph projection
-> create/confirm admin database owner and restricted runtime role
-> db migrate
-> runtime db verify --deep
-> start Host
```

Old/new binary不得在同一database滚动共存；不能先启动新binary再期待第一次请求自动迁移。

Bundled Docker init只创建runtime role并授予database CONNECT。pgvector、functions、tables、indexes与runtime object grants全部由 migration registry拥有。

## 9. Test 与 Benchmark

- Unit tests默认不连接PostgreSQL。
- PostgreSQL integration tests使用统一marker和每个xdist worker独立的fresh migrated database。
- Ordinary adapter tests使用verified provider；raw connection只属于fixture或显式negative test。
- Wheel与sdist必须包含全部migration SQL与expected catalog resources。
- Durable benchmark template必须exact latest、deep verified、business-empty。
- Benchmark generator required接收`VerifiedBenchmarkDatabaseLease`；clone migration/verification在measurement interval之外。
- Benchmark sample必须记录durable registry prefix、schema fingerprint、PostgreSQL/pgvector版本与connection policy identity；不同schema identity不得聚合。

## 10. Domain Migration Boundary

Migration head只证明PostgreSQL physical schema。它不证明以下内容兼容：

- historical AgentEvent JSON schema/domain registry；
- checkpoint/artifact document contracts；
- provider-input generation semantics；
- Oxigraph ontology/materialization world；
- workspace files。

这些 contract发生hard cut时仍需独立migration或reset。PostgreSQL reset丢弃canonical world时，旧Oxigraph projection必须同步reset。

## 11. 禁止事项

- constructor、`__post_init__`、UOW、request或worker hot path执行schema SQL；
- `ensure_schema()`/`initialize_schema()` production fallback；
- runtime startup auto-migrate、grant repair或旧库adopt；
- production adapter持有raw DSN、调用`psycopg.connect()`或构造`ConnectionPool`；
- admin DSN进入runtime diagnostics；
- migration SQL中使用`IF NOT EXISTS`掩盖baseline drift（canonical genesis判定除外）；
- checksum/catalog conflict降级为warning；
- 将UNRESOLVED误报为confirmed structural conflict。
