# Pulsara Schema Hot-Path Hard Cut 实施规格

> 状态：Implementation-ready
> 日期：2026-07-22
> 代码基线：`main@dca11e75a150489a6fe167a39cd92189ca51b84d`
> 上游审计：`PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md` 第 12 节
> 实施阶段：`SH0` 到 `SH5`

## 1. 目标

本阶段将 PostgreSQL schema ownership 从 runtime hot path 中物理移除，建立唯一、显式、可审计的 schema migration authority。

完成后：

1. Host、EventLog、memory、governance、Inspector、worker 和 tool path 都不执行 DDL；
2. PostgreSQL runtime role 不需要 `CREATE TABLE`、`ALTER TABLE`、`CREATE INDEX` 或 `CREATE EXTENSION` 权限；
3. 只有 privileged `pulsara db migrate` 可以改变 Pulsara PostgreSQL schema；
4. production startup 只验证 exact migration head、checksum、required objects、extensions和runtime privileges；
5. schema 未初始化、过旧、过新、checksum漂移或权限不完整时，在创建 Host/session/resource 之前 fail closed；
6. pytest、benchmark 和本地 Docker 使用同一 migration registry，不再依赖 store constructor“顺便建表”；
7. 后续 Hook/outbox、typed event 或 memory schema变化只能增加 migration，不能重新引入 `ensure_schema()`。

本阶段只管理 PostgreSQL physical schema。它不自动迁移：

- EventLog JSONB payload 的 event schema/domain registry；
- checkpoint/artifact document contract；
- Oxigraph semantic graph；
- provider-input generation semantics；
- 用户 workspace 文件。

这些 domain contract 如发生 hard cut，仍需独立 offline migration 或明确 reset，不能因为 PostgreSQL migration head一致就假装兼容。

## 2. 为什么现在必须 hard cut

当前 schema 定义分散在六组生产 SQL：

| 当前定义 | 当前 owner |
|---|---|
| `RUNTIME_TRUTH_SCHEMA_SQL` | `storage/postgres_schema.py` |
| `MEMORY_SUBSTRATE_SCHEMA_SQL` | `storage/memory_schema.py` |
| `CANDIDATE_POOL_SCHEMA_SQL` | `memory/candidates/pool.py` |
| `CANDIDATE_PROJECTION_OUTBOX_SCHEMA_SQL` | `memory/candidates/projection_outbox.py` |
| `MEMORY_GOVERNANCE_CLAIM_SCHEMA_SQL` | `memory/governance/claims.py` |
| `GOVERNANCE_BATCH_PREPARATION_SCHEMA_SQL` | `memory/governance/preparation.py` |

它们又被以下 production paths 隐式执行：

- `PostgresEventLog.__post_init__()`；
- `PostgresGraphStore.__post_init__()` / `ensure_schema()`；
- Host resume、session manifest、working-context store；
- candidate pool、candidate projection outbox；
- search/vector index sync、mutation outbox、Oxigraph materializer、reconciler；
- governance claims、event outbox、batch preparation；
- recall trace；
- 最严重的 `MemoryWriteUnitOfWork.__enter__()`。

当前每次 governance UOW 都执行三组 schema SQL，其中包含 `CREATE TABLE IF NOT EXISTS`、`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`、index DDL、function replacement和backfill update。

最近出现的：

```text
psycopg.errors.InsufficientPrivilege: 创建扩展 "vector" 权限不够
```

只是最显眼的症状。将 `CREATE EXTENSION` 拆出 runtime SQL 后，普通 table/index/alter DDL仍位于构造和写入路径，根因并未消失。

## 3. 非目标

本阶段不做：

- Alembic/SQLAlchemy adoption；
- rolling schema compatibility；
- down migration；
- live backfill worker；
- 旧 EventLog payload decoder兼容；
- 自动接管没有 migration ledger 的旧 Pulsara database；
- runtime startup auto-migrate；
- runtime role自动创建database或role；
- PostgreSQL以外的通用 migration framework；
- Oxigraph ontology migration；
- destructive `pulsara db reset` public command。

Pulsara 当前使用直接 psycopg、PostgreSQL-specific DDL、advisory lock和pgvector。一个小型、线性、forward-only runner比引入 ORM migration stack更符合现有边界。

## 4. 全局 hard-cut 规则

### 4.1 单一真源

schema唯一真源是 packaged migration registry：

```text
src/pulsara_agent/storage/migrations/
├── __init__.py
├── contracts.py
├── errors.py
├── registry.py
├── runner.py
├── verifier.py
└── sql/
    ├── 0000_schema_migration_ledger.sql
    ├── 0001_pgvector_extension.sql
    ├── 0002_runtime_truth_baseline.sql
    ├── 0003_memory_substrate_baseline.sql
    └── 0004_memory_governance_baseline.sql
```

Hard cut完成后必须删除六组旧 `*_SCHEMA_SQL`、`MEMORY_SUBSTRATE_BOOTSTRAP_SQL` 和 `MEMORY_SUBSTRATE_EXTENSION_REQUIREMENT_SQL`。

不得让 migration SQL 与 runtime bootstrap SQL长期双写。

### 4.2 V1 不兼容旧数据库

本项目仍处于允许数据库重置的 hard-cut 阶段。V1规则：

- migration ledger不存在且检测到任意 Pulsara-owned object：`schema_unmanaged_database`；
- 不推断“这些表看起来像最新版本”；
- 不创建 ledger后把旧库直接标记为 latest；
- 不提供 `--adopt-existing`；
- 不在 Host startup执行一次性 backfill。

切换时重置 PostgreSQL。若 PostgreSQL reset导致 canonical memory/runtime authority丢失，关联的 Oxigraph projection也必须同步重置，避免旧 semantic projection冒充新 PostgreSQL world的派生结果。

本文中的“Pulsara-empty database”只表示：`PULSARA_RESERVED_OBJECT_NAMES`中没有已存在对象，且migration
ledger不存在。它不要求整个`public` schema没有第三方表；无关应用对象不参与Pulsara genesis、manifest或cleanup。

未来若需要保留生产数据，应另立 offline migration/export-import规格，而不是放宽本阶段 genesis规则。

### 4.3 Exact-head binary contract

每个 Pulsara binary只支持 registry中唯一的latest head：

- database behind：拒绝启动，要求先运行当前binary的`db migrate`；
- database ahead：拒绝启动，说明binary过旧；
- checksum/name/version不一致：拒绝启动；
- 不允许old/new binary在同一个database上滚动共存。

部署必须先停止所有Host/worker，再迁移，再启动新binary。

### 4.4 Forward-only且事务化

- 每个migration在独立PostgreSQL transaction中执行；
- DDL postcondition、runtime-role grants和ledger row在同一transaction中提交；
- migration failure必须零partial row；
- V1 migration不得使用`CREATE INDEX CONCURRENTLY`、`VACUUM`、`CREATE DATABASE`或其他不能处于普通transaction的语句；
- 不提供down migration；rollback使用备份恢复或database reset。

### 4.5 不解析或模板化 SQL

runner将migration resource的完整UTF-8 bytes交给psycopg执行：

- 不按分号手工split；
- 不使用字符串替换注入role/schema/table；
- 不允许用户提供migration filename或SQL；
- 动态role grant使用`psycopg.sql.Identifier`，不拼接文本。

## 5. V1 transaction domain

V1冻结为：

- 一个PostgreSQL database；
- schema为`public`；
- PostgreSQL server version `>= 15`且`< 18`；CI与bundled Docker主验证版本为16；
- admin DSN和runtime DSN必须连接同一database OID/name；
- 两个connection的`current_schemas(false)`必须规范化后精确等于`("public",)`；
- runtime code继续使用现有unqualified table names，因此不同`search_path`直接fail closed；
- Oxigraph不属于该physical transaction domain。

不在本阶段引入多schema配置。若未来需要`pulsara`专用schema，应作为新的migration和query qualification hard cut处理。

## 6. Migration ledger

### 6.1 唯一表

唯一migration ledger为：

```sql
CREATE TABLE public.pulsara_schema_migrations (
    version BIGINT PRIMARY KEY CHECK (version >= 0),
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    migration_contract_fingerprint TEXT NOT NULL
        CHECK (migration_contract_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    registry_prefix_fingerprint TEXT NOT NULL
        CHECK (registry_prefix_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    application_version TEXT NOT NULL
);
```

不增加平行的`schema_version` singleton或environment metadata表。当前版本由ordered rows的最后一项派生。

### 6.2 Canonical genesis

`0000_schema_migration_ledger.sql`是唯一genesis：

1. runner持有exclusive migration advisory lock；
2. `pulsara_schema_migrations`不存在；
3. database中不存在任何Pulsara-owned relation/function；
4. 允许`vector` extension由平台预装，因为它不是Pulsara-owned table；
5. 同一transaction创建ledger并插入包含version 0 contract/prefix fingerprint的row；
6. runtime role只获得该表的`SELECT`。

若ledger table存在但version 0 row缺失、字段不匹配或存在额外row，不能重新genesis。

### 6.3 Applied history invariant

读取rows后必须满足：

- versions精确为`0..head`，无gap、duplicate或negative；
- 每个version的name/checksum/migration contract fingerprint与当前registry同位置definition相等；
- 每行`registry_prefix_fingerprint`必须由previous prefix与本行migration contract fingerprint唯一重算；
- database不可包含binary registry之外的unknown version；
- `application_version`只用于审计，不参与兼容判断；
- `applied_at`只用于审计，不进入registry fingerprint。

### 6.4 Checksum

每个`MigrationDefinition`冻结`expected_sha256`。加载时：

```python
actual = sha256(resource_bytes).hexdigest()
if actual != definition.expected_sha256:
    raise MigrationResourceChecksumError(...)
```

checksum覆盖原始packaged bytes，不做strip、换行转换或SQL normalization。migration文件一旦进入registry就不可编辑，只能追加更高version。

### 6.5 Registry fingerprint

每个`migration_contract_fingerprint`覆盖：

```text
H(
  "pulsara:postgres-migration-contract:v1",
  {
    "version": version,
    "name": name,
    "expected_sha256": expected_sha256,
    "transaction_mode": transaction_mode,
    "postcondition_contract_fingerprint": postcondition_contract_fingerprint,
    "resulting_object_manifest_fingerprint": resulting_object_manifest_fingerprint,
    "runtime_grant_policy_fingerprint": runtime_grant_policy_fingerprint
  }
)
```

累计prefix使用唯一算法：

```text
prefix[-1] = H(
    "pulsara:postgres-migration-registry-genesis:v1",
    {"schema_version": "postgres_migration_registry_genesis.v1"},
)
prefix[v]  = H(
    "pulsara:postgres-migration-registry-prefix:v1",
    {
      "previous_registry_prefix_fingerprint": prefix[v - 1],
      "migration_contract_fingerprint": migration_contract_fingerprint[v]
    },
)
```

`migration_registry_fingerprint == prefix[latest]`。它不是只存在于当前代码中的派生值：每个ledger row都保存
其对应`registry_prefix_fingerprint`作为durable对端。

验证规则：

- behind database逐行比较到其自身head，并将durable head prefix与当前binary同version prefix比较；
- latest database比较完整registry prefix；
- ahead database在读取到第一个unknown version时稳定报告`schema_version_ahead`，不能忽略其prefix；
- migration SQL不变但postcondition/object manifest/grant policy改变时，migration contract fingerprint必然漂移；
  必须新增migration version，不能修改已应用definition；
- commit confirmation必须同时join version、name、checksum、migration contract fingerprint和registry prefix。

它进入：

- verifier result；
- CLI status/verify输出；
- benchmark environment fact；
- test template database contract；
- Host diagnostics。

它不写入每张业务表；ledger rows正是它的durable carrier。

## 7. Migration registry 与 baseline 分区

### 7.1 MigrationDefinition

建议process-local typed contract：

```python
@dataclass(frozen=True, slots=True)
class PostgresMigrationDefinition:
    version: int
    name: str
    resource_name: str
    expected_sha256: str
    transaction_mode: Literal["atomic"]
    postcondition_contract_fingerprint: str
    resulting_object_manifest_fingerprint: str
    runtime_grant_policy_fingerprint: str
```

registry factory必须验证：

- non-empty；
- 从0开始连续递增；
- name/resource唯一；
- resource文件名version与definition一致；
- checksum为lowercase SHA-256；
- 所有definition均为`atomic`；
- manifest/grant fingerprints均由对应typed registry entry重算，不能接受caller自报值。

### 7.2 Baseline migrations

#### `0000_schema_migration_ledger`

- 创建唯一ledger；
- 无业务表；
- 为runtime role授予`SELECT`。

#### `0001_pgvector_extension`

- `CREATE EXTENSION IF NOT EXISTS vector`；
- postcondition要求extension存在；
- V1要求pgvector支持HNSW，最低`0.5.0`；
- 不隐式运行`ALTER EXTENSION UPDATE`；版本过旧时fail closed，未来用显式migration升级。

这是唯一允许`IF NOT EXISTS`的baseline DDL，因为extension可能由managed PostgreSQL平台预装。

#### `0002_runtime_truth_baseline`

创建最终形态，不复制旧`ALTER ... IF NOT EXISTS`兼容链：

- `sessions`
- `runs`
- `turns`
- `agent_events`
- `ledger_materialization_accounts`
- `runtime_projection_checkpoints`
- `artifacts`
- `tool_result_artifacts`
- `tool_execution_records`
- `working_context_summaries`

所有current required columns、constraints和indexes直接出现在`CREATE TABLE/INDEX`中。

#### `0003_memory_substrate_baseline`

创建：

- `pulsara_jsonb_text_array(jsonb)` function；
- `graph_documents`
- `memory_nodes`
- `memory_vector_index`
- `memory_relations`
- `memory_write_outbox`
- `memory_governance_event_outbox`
- `memory_search_index`
- `recall_traces`
- `recall_usages`

`memory_vector_index.embedding`精确为`vector(1024)`；HNSW index是required postcondition。

#### `0004_memory_governance_baseline`

创建：

- `memory_candidates`
- `memory_governance_decisions`
- `memory_candidate_projection_outbox`
- `memory_governance_candidate_claims`
- `memory_candidate_evidence_rejections`
- `memory_governance_batch_inputs`

该migration依赖runtime truth中的session/run/turn foreign keys以及candidate table自身顺序。

### 7.3 Baseline SQL要求

- 除pgvector extension外，不使用`IF NOT EXISTS`；
- 不包含旧库backfill `UPDATE/DELETE`；
- table定义直接是当前最终schema；
- 所有对象使用`public.` qualification；
- index/constraint名称稳定；
- baseline application只允许Pulsara-empty database。

## 8. Canonical schema object manifest

### 8.1 目的

ledger证明migration history，但不能单独发现管理员在migration后手工drop table/index。为此新增唯一`PostgresSchemaObjectManifest`。

manifest按migration version声明：

- required extensions及最低版本；
- required user-defined types及schema；
- owned relations和relation kind；
- runtime-writable tables；
- read-only ledger tables；
- required functions/signatures；
- required indexes；
- 每个owned relation的全部ordered columns、type、typmod、nullability、collation、default、generated和identity
  contract；
- 全部PK/FK/unique/check constraints及deferrable/deferred/validated state；
- 全部required indexes及其keys/opclasses/predicate/include/valid/ready/live contract；
- 全部required functions的signature、return type、language、volatility、strict/security/leakproof/parallel/config和
  body contract。

对象名称不再分别保存在`RUNTIME_TRUTH_TABLES`和`MEMORY_SUBSTRATE_TABLES`。现有tests和Inspector从该manifest派生。

### 8.2 Fast 与 deep verification

#### Fast startup verification

每个process-owned verification key首次打开durable storage时执行一次：

- exact migration rows；
- durable registry prefix fingerprint；
- PostgreSQL server compatibility；
- `vector` extension及最低版本；
- required relations/functions存在；
- 每张owned relation的ordered executable column shape fingerprint：name、schema-qualified type identity、typmod、
  nullability、collation、default presence/default contract、generated和identity；
- 全部PK/FK/unique/check constraint semantic fingerprint，包括deferrable/deferred/validated；
- required index的存在性与valid/ready/live状态；
- required function minimum execution contract：signature、return type、language、volatility、strict、
  security-definer、leakproof、parallel和function-local config；
- runtime role具有manifest声明的最小权限；
- database/schema identity一致。

fast verifier必须足以证明所有runtime DML可执行，而不仅是column存在。比如`INSERT INTO sessions (id,
workspace_root)`依赖`created_at DEFAULT now()`和`metadata DEFAULT '{}'`，任一default缺失必须在Host resource创建前
失败。

fast verifier不比较完整non-constraint index expression、function body或unexpected historical object；这些保留给
deep verify。当前约二十多张表的column/default/constraint/function-minimum查询仍属于固定有界startup成本。

#### Deep CLI verification

`pulsara db verify --deep`额外比较：

- columns/type/nullability/default；
- constraint definitions；
- index definitions；
- `vector(1024)` typmod；
- required function identity；
- unexpectedPulsara-owned relation。

“Pulsara-owned”不是根据table内容或名称前缀猜测。manifest另行冻结
`PULSARA_RESERVED_OBJECT_NAMES`，它是所有current/historical migration曾拥有对象的并集；deep
verify只对该集合判定missing/unexpected，不把同一`public` schema中的无关应用表据为己有。

deep verification只读，不修复。

### 8.3 Manifest 不得自证

manifest来自packaged code，database只提供observed catalog。不能从当前database introspection生成“expected manifest”后再比较自己。

迁移集成测试必须在fresh database应用registry，再证明observed catalog等于packaged manifest。

### 8.4 Catalog canonicalization

deep verifier不得直接比较未经规范化的`pg_get_*`整段文本，也不得通过dump/restore文本推断schema identity。唯一
`PostgresCatalogCanonicalizer`从`pg_catalog`读取结构化字段，生成sorted canonical JSON：

- column：relation identity、`attnum`、name、schema-qualified type name、typmod、nullability、collation、
  generated、identity，以及经`pg_get_expr()`取得的default expression；
- constraint：constraint kind、ordered local column ordinals、referenced relation/columns、match/update/delete
  action、deferrable、initially deferred、validated，以及check expression；
- index：access method、unique flag、ordered key/expression、schema-qualified operator class、included columns和
  predicate、valid、ready和live state；
- function：schema/name、ordered schema-qualified argument types、schema-qualified return type、language、
  volatility、strict、security-definer、leakproof、parallel、function-local config，以及`prosrc`的SHA-256；
- extension/type：schema、name、version；对`vector`同时验证extension version和`vector(1024)` typmod。

OID只允许作为一次catalog query内部的join key。type、function argument/return type和operator class的semantic
identity一律使用`schema + name`（必要时加typmod）；extension安装时分配的type OID绝不进入canonical DTO或
fingerprint。

canonicalizer也不得把relation/index/type/owner OID、统计信息、filenode、创建时间或ACL文本顺序放进semantic
fingerprint。ACL由独立的privilege verifier按role/object/privilege三元组比较。

database OID仍可进入process-local connection target identity，用于证明两个connection指向同一database；它不进入
`observed_schema_fingerprint`，也不要求不同fresh database具有相同database OID。

每类对象使用独立domain separator，例如：

```text
pulsara:postgres-catalog-relation:v1
pulsara:postgres-catalog-index:v1
pulsara:postgres-catalog-function:v1
```

最终`observed_schema_fingerprint`由ordered object fingerprints组成。支持的PostgreSQL版本范围内，canonicalizer必须有
跨version fixture；新增支持版本前先证明相同logical schema得到相同fingerprint。超出支持范围时在canonicalization前
拒绝，不能用“best effort”继续。

### 8.5 Canonical DTO 与 fingerprint contract

本阶段统一复用Pulsara canonical JSON约定：

```text
canonical_json_bytes(value) = UTF-8 json.dumps(
    value,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
)

H(domain, value) =
    "sha256:" + hex(
        SHA-256(
            UTF8(domain) || 0x00 || canonical_json_bytes(value)
        )
    )
```

schema contract fingerprint payload只允许`null/bool/int/string/ordered tuple/typed string-key object`：

- `None`编码为JSON `null`；
- tuple按原顺序编码为JSON array；factory不接受set或unordered iterable；
- object keys按Unicode code point排序，key必须是string；
- string使用JSON escaping和原始UTF-8，不做隐式trim或Unicode normalization；
- float、bytes、datetime、Enum object和arbitrary Python object必须先由typed factory转换，否则拒绝；
- 所有fingerprint字段使用`sha256:<64 lowercase hex>`，只有migration resource `checksum`保持bare 64-hex。

以下是必须落地的中央DTO拓扑；字段可以拆到独立module，但不得让runner/verifier/benchmark各自定义平行dict：

```python
class PostgresMigrationLedgerRowFact(FrozenFactBase):
    schema_version: Literal["postgres_migration_ledger_row.v1"]
    version: int
    name: str
    resource_checksum: str
    migration_contract_fingerprint: str
    registry_prefix_fingerprint: str
    application_version: str
    applied_at_utc: str


class PostgresSchemaObjectManifest(FrozenFactBase):
    schema_version: Literal["postgres_schema_object_manifest.v1"]
    through_version: int
    required_extensions: tuple[PostgresExtensionContractFact, ...]
    required_types: tuple[PostgresTypeContractFact, ...]
    owned_relations: tuple[PostgresRelationContractFact, ...]
    required_functions: tuple[PostgresFunctionContractFact, ...]
    reserved_object_names: tuple[PostgresObjectIdentityFact, ...]
    manifest_fingerprint: str


class PostgresFastObservedCatalogFact(FrozenFactBase):
    schema_version: Literal["postgres_fast_observed_catalog.v1"]
    server_version_num: int
    extensions: tuple[PostgresObservedExtensionFact, ...]
    types: tuple[PostgresObservedTypeFact, ...]
    relation_execution_shapes: tuple[PostgresObservedRelationExecutionShapeFact, ...]
    function_execution_shapes: tuple[PostgresObservedFunctionExecutionShapeFact, ...]
    fast_executable_schema_fingerprint: str


class PostgresDeepObservedCatalogFact(FrozenFactBase):
    schema_version: Literal["postgres_deep_observed_catalog.v1"]
    fast_observed_catalog_fingerprint: str
    relations: tuple[PostgresObservedRelationFact, ...]
    functions: tuple[PostgresObservedFunctionFact, ...]
    deep_catalog_fingerprint: str
    observed_catalog_fingerprint: str
```

`PostgresObservedCatalogFact`是上述fast/deep discriminated union；fast path不能伪造或填充一个假的deep fingerprint。

manifest是cumulative through-version状态，不是仅描述该version新增对象。`resulting_object_manifest_fingerprint[v]`
必须等于该cumulative manifest fingerprint。

ledger row中的`applied_at_utc`由reader规范化为UTC RFC 3339；它和`application_version`只属于audit attribution，不进入
migration contract或registry prefix。

grant target使用唯一discriminated union：

```text
PostgresGrantTargetFact =
    PostgresSchemaGrantTargetFact(schema_name)
  | PostgresRelationGrantTargetFact(schema_name, relation_name, relation_kind)
  | PostgresSequenceGrantTargetFact(schema_name, sequence_name)
  | PostgresFunctionGrantTargetFact(schema_name, function_name, ordered_argument_types)
  | PostgresTypeGrantTargetFact(schema_name, type_name)

PostgresRuntimeGrantRequirementFact =
    target
    ordered_required_privileges
    requirement_fingerprint
```

privilege enum按target branch封闭；例如type branch不能接受`SELECT`，relation branch不能接受`EXECUTE`。

column default不保存任意SQL字符串。唯一`PostgresColumnDefaultContractFact` union覆盖当前schema所需的：

```text
no_default
canonical_constant(value, schema-qualified cast type)
current_timestamp
schema-qualified function_call(ordered canonical arguments)
```

canonicalizer将`pg_get_expr()`结果解析/匹配为该typed union；unknown expression fail closed。generated/identity是独立
column fields，不能伪装成default。

verification result使用discriminated union：

```text
PostgresFastSchemaVerificationResult
    binding
    ordered ledger rows
    expected/observed registry prefix
    expected/observed fast executable schema fingerprint
    effective privilege result
    result fingerprint

PostgresDeepSchemaVerificationResult
    nested fast result fingerprint
    expected manifest fingerprint
    observed deep catalog fingerprint
    unexpected/missing object facts
    result fingerprint
```

所有expected manifest、observed catalog、grant requirement、ledger row validation和verification result只能由中央factory
构造。factory从全部non-fingerprint字段重算fingerprint并逐字段比较；public constructor、caller-provided dict和数据库
自报fingerprint都不能绕过重算。

唯一factory集合为`PostgresMigrationContractFactory`、`PostgresSchemaManifestFactory`、
`PostgresGrantPolicyFactory`、`PostgresCatalogCanonicalizer`和`PostgresSchemaVerificationResultFactory`；runner、CLI、
Inspector、tests和benchmark只能消费这些factory产物，不得复制hash payload assembly。

## 9. Admin/runtime role contract

### 9.1 两个 DSN

新增migration-only环境变量：

```text
PULSARA_POSTGRES_ADMIN_DSN
```

现有：

```text
PULSARA_POSTGRES_DSN
```

保持runtime唯一DSN。

规则：

- admin DSN只由`pulsara db migrate`读取；
- Host/worker/Inspector不得读取或持有admin DSN；
- admin DSN和runtime DSN必须指向同一目标database，不是`postgres`maintenance database；
- CLI/report/log不输出DSN、password或userinfo；
- local development可以将两者设为同一DSN，但必须显式配置，不静默fallback。

`PULSARA_BENCHMARK_POSTGRES_ADMIN_DSN`继续属于benchmark database create/drop owner，不替代产品migration DSN。

### 9.2 Runtime role identity

migrator在任何DDL前用runtime DSN连接并读取：

```text
current_database
database_oid
current_user
current_schemas(false)
server_version_num
exact migration head + durable registry prefix
```

`current_schemas(false)`规范化为ordered schema-name tuple，并且必须精确等于`("public",)`。只检查
`current_schema()`或原始`SHOW search_path`都不充分，因为`public, extra`和暂时不存在的`"$user"` schema也可能
返回`current_schema() == public`。

admin connection读取同一database OID/name，并同样要求normalized effective search path精确为`("public",)`。
runtime role必须已经存在且具有database `CONNECT`。normalized search path进入verification result和binding。

### 9.3 Grants

唯一`PostgresRuntimeGrantExecutor`按typed object manifest和typed privilege enum应用grants：

- `USAGE` on schema `public`；
- `SELECT` on `pulsara_schema_migrations`；
- `SELECT, INSERT, UPDATE, DELETE` on runtime-writable owned tables；
- `USAGE ON TYPE public.vector`；
- required function `EXECUTE`；
- future required sequences的`USAGE, SELECT`。

runtime role不获得migration ledger写权限，也不需要schema/extension create权限。

`runtime_grant_policy_fingerprint`覆盖schema-qualified object identity、object kind和ordered required privilege set；
它不覆盖具体grantee role identity，因此同一durable policy可以显式rebind到replacement runtime role。实际role只进入
verification/attribution report。

该executor是admin-only ACL statement owner：

- role/object identifier只能通过`psycopg.sql.Identifier`渲染；
- caller只能提交registry中已冻结的manifest object identity和privilege enum；
- caller不能传SQL、object name string、privilege string或额外clause；
- executor只拥有固定的`GRANT <allowlisted privilege> ON <typed object kind> TO <role>` statement matrix；
- schema-object DDL仍只能位于packaged migration SQL resources。

V1不使用`ALTER DEFAULT PRIVILEGES`，每个migration只对其resulting manifest中的exact objects授予权限，避免
admin role未来创建的无关对象自动暴露给runtime role。

runtime role能够在`public` schema创建对象不作为startup fatal error，以兼容本地admin/runtime同用户；但`db verify`
必须报告`runtime_role_can_create_in_public_schema=true` warning。该字段只证明`rolsuper`或
`has_schema_privilege(role, 'public', 'CREATE')`，不泛化声称能够穷尽任意owned-object ALTER/DROP authority。
CI与production acceptance使用该字段为false的受限角色。

### 9.4 Runtime privilege reconciliation

grants不能只在pending migration中执行。`db migrate`在全部migration完成后、仍持有exclusive advisory lock时，对
当前runtime role执行一次幂等、单transaction reconciliation：

```text
BEGIN
  re-read exact migration head + durable registry prefix
  resolve latest typed grant policy
  read complete effective privilege set
  compute complete missing-grant set
  preflight admin grant authority for every missing grant
  apply the complete missing-grant set
  re-read and verify the complete effective privilege set
COMMIT
```

规则：

1. 使用`has_schema_privilege`、`has_table_privilege`、`has_function_privilege`、`has_type_privilege`等effective
   privilege查询；
2. 已通过owner、direct ACL、role membership或`PUBLIC`获得effective privilege时视为满足，不强制重写ACL；
3. 任一required grant缺少admin grant authority时，在执行第一条`GRANT`前失败；
4. 任一`GRANT`或final effective verification失败时整transaction rollback，不留下partial ACL；
5. reconciliation不新增migration row，也不改变registry prefix；
6. report中的`added_grants`只能在COMMIT FULL后发布；rollback/UNKNOWN时不得宣称已修复；
7. reconciliation COMMIT UNKNOWN时使用new admin connection重取exclusive lock并重查complete effective set；证明
   全部满足可adopt，证明仍是完整pre-state可重试；证明为既非pre-state也非complete set时返回conflict，无法取得
   完整authority时返回独立unresolved，不伪造partial列表。

这使up-to-date database可以绑定replacement runtime role、修复被撤销的grant，也兼容由平台角色拥有但通过
`PUBLIC`已可用的pgvector type。旧runtime role残留ACL只在bounded report中列出；V1不隐式`REVOKE`，清理由显式
运维完成。

## 10. Advisory lock 与并发

### 10.1 Lock identity

使用two-key PostgreSQL advisory lock：

```text
key1 = stable int32 namespace derived from
       sha256("pulsara:postgres-schema-migration:v1")
key2 = target database OID converted to signed int32
```

不得使用Python `hash()`、PID、DSN text或process-local UUID。

### 10.2 Migrator

`db migrate`持有exclusive session advisory lock覆盖：

- genesis判定；
- applied history validation；
- 全部pending migrations；
- per-migration grants；
- up-to-date runtime privilege reconciliation；
- final deep verification。

第二个migrator等待同一lock；取得后必须重新读取ledger，通常返回`up_to_date`。

### 10.3 Verifier

startup/CLI verifier在验证窗口持有shared advisory lock，保证不会读取到migration中间状态。验证完成后释放。

V1仍要求quiescent deployment。shared lock不在Host整个生命周期持有；operator不得在Host运行时启动migration。

### 10.4 Deadlines

- migration default absolute deadline：300秒；CLI允许1到3600秒；
- startup fast verify default：10秒；
- lock wait、connection、statement、postcondition和grant共享同一个absolute deadline；
- nested operation不得重新生成deadline；
- deadline到期关闭connection，PostgreSQL transaction rollback并释放session advisory lock。

## 11. Migration algorithm

唯一runner算法：

```text
load packaged registry
verify every resource checksum
resolve runtime database/role identity
connect admin to the exact same database
validate admin/runtime database OID + schema
acquire exclusive advisory lock

if migration ledger absent:
    inspect canonical Pulsara-owned object set
    if any owned object exists:
        fail schema_unmanaged_database
    atomically apply migration 0000 + grants + row

read and validate complete applied history

for each pending migration in order:
    BEGIN
    set local lock_timeout / statement_timeout from absolute deadline
    execute complete packaged SQL resource
    apply exact runtime grants for resulting manifest
    validate migration postconditions
    insert exact migration ledger row including contract/prefix fingerprints
    COMMIT

reconcile effective privileges for the current runtime role
run final deep verification
release advisory lock
return typed report
```

runner不得：

- 捕获`DuplicateTable`后继续；
- 忽略checksum drift；
- 在postcondition失败时仍写row；
- 跳过version；
- 根据当前tables猜测已应用migration；
- 自动删除unexpected objects；
- 在migration failure后启动Host。

每个resource通过`cursor.execute(resource_text, prepare=False)`作为完整simple-query payload执行；
这是支持function body和多statement migration的明确要求，不允许退回分号splitter。

up-to-date database也必须经过privilege reconciliation；`pending_migrations == ()`不能提前return。

## 12. Verify-only startup ownership

### 12.1 Secret-safe binding 与 connection capability

新增secret-safe、process-local proof：

```python
@dataclass(frozen=True, slots=True)
class VerifiedPostgresSchemaBinding:
    database_target_fingerprint: str
    database_name: str
    database_oid: int
    normalized_search_path: tuple[str, ...]
    runtime_role: str
    server_version_num: int
    pgvector_extension_version: str
    migration_head_version: int
    durable_registry_prefix_fingerprint: str
    fast_executable_schema_fingerprint: str
    verification_contract_fingerprint: str
    _construction_guard: object = field(repr=False, compare=False)
```

binding不保存DSN、password、userinfo、passfile内容或connection object。即使generic diagnostic错误地展开公开字段，也
只能得到secret-safe database contract identity。它不是durable fact，不能从event/artifact反序列化后恢复为有效
capability。

真实DSN只由`PostgresRuntimeConnectionFactory`持有。factory只从DSN提取allowlisted、secret-safe target fields：
transport kind、normalized host或Unix socket identity、port、database、`sslmode`和`target_session_attrs`；password、
userinfo、passfile、service-file内容、TLS key path、arbitrary options及unknown query parameters全部排除。随后用真实
connection取得database OID/name、effective role和normalized search path，共同形成
`database_target_fingerprint`。

binding只能由verifier module-private factory创建。真正打开runtime connection还需要opaque
`VerifiedPostgresAccessLease`；lease只保存service签发的capability token，并从service取得第12.4节唯一verified
connection provider，自身不持有DSN。该lease显式拒绝pickle/copy/JSON serialization。

### 12.2 Process-owned verification service

应用composition root创建唯一`PostgresSchemaVerificationService`。它按：

```text
(canonical database target fingerprint,
 observed database OID,
 runtime role,
 expected registry prefix fingerprint)
```

分区shared attempt，而不是让每个`HostCore`各自验证。每个key具有：

```text
UNVERIFIED
  -> VERIFYING(shared owned task + absolute deadline)
       -> VERIFIED(binding + connection capability)
       -> FAILED(stable error)
VERIFIED
  -> INVALIDATED(physical connection identity mismatch)
```

key resolution分两步：connection factory先以remaining deadline执行只读preflight，取得database OID/effective role/
search path；随后在service lock内计算resolved key并join或创建唯一full verification attempt。多个factory指向同一
resolved key时可以各自产生一次轻量preflight，但只能有一个catalog/ledger verification owner。preflight本身也必须
是service-owned physical operation，使用同一个caller absolute deadline、statement timeout和主动cancel/close；
不得使用未追踪的`asyncio.to_thread()`。同一endpoint重建database或failover到不同database OID时必须形成新key，
不能复用旧`VERIFIED` attempt。

并发与取消规则：

- 首个borrower启动一次`asyncio.to_thread` fast verification；同process中的Host、resume benchmark、Inspector等
  borrower共享该attempt；
- waiter通过`asyncio.shield()`等待；waiter cancellation只detach，不取消shared task或关闭其connection；
- blocking verifier一旦进入thread，由service持有到FULL failure/success；它使用自己的connection和唯一absolute
  deadline；connect timeout取remaining deadline，deadline到期主动cancel/close psycopg connection并形成stable
  failure；
- 完成resolved-key preflight之后，verification success和failure都在service lifetime内按key缓存；production schema
  cutover要求新process，不能在旧process中invalidate后重试。preflight在取得database OID之前失败时尚不存在可缓存
  的resolved key，后续borrower可以发起新的独立preflight generation；
- 不同database target、database OID、runtime role或registry prefix fingerprint绝不共享attempt；
- process shutdown先拒绝新borrow，等待所有VERIFYING owner在其原deadline内收口，再关闭connection factories；
- `HostCore.shutdown()`只release自己的lease，不取消process-owned verifier；
- fork后的child process不得继承parent capability，必须创建新service。

### 12.3 HostCore borrower

`HostCore`不再拥有独立verification state。以下入口必须先从process service取得access lease：

- `open_session`
- `resume_session`
- `resume_most_recent_session`
- `list_resumable_sessions`
- `repair_session_for_resume`
- Host inspect path

verification必须发生在workspace resolution、HostSession reservation、terminal lease、MCP supervisor、retrieval worker和
任何Postgres store构造之前。

### 12.4 Verified physical connection provider

access lease不是只供composition root检查的marker。lease向下唯一暴露：

```python
class PostgresConnectionLane(StrEnum):
    EVENT_LOG = "event_log"
    ARTIFACT = "artifact"
    HOST_CONTROL = "host_control"
    MEMORY_UOW = "memory_uow"
    MEMORY_QUERY = "memory_query"
    MEMORY_MAINTENANCE = "memory_maintenance"
    GOVERNANCE = "governance"
    INSPECTOR = "inspector"
    CHECKPOINT_MAINTENANCE = "checkpoint_maintenance"


class VerifiedPostgresConnectionProvider(Protocol):
    @property
    def schema_binding(self) -> VerifiedPostgresSchemaBinding: ...

    def connection(
        self,
        *,
        lane: PostgresConnectionLane,
        row_factory: object | None,
        deadline_monotonic: float,
    ) -> ContextManager[Connection]: ...

    def pool(
        self,
        *,
        lane: PostgresConnectionLane,
        deadline_monotonic: float,
    ) -> VerifiedPostgresPoolLease: ...
```

`PostgresPoolPolicyFact`冻结`lane/min_size/max_size/max_waiting/connect_timeout_seconds/checkout_timeout_seconds`和
policy fingerprint，并由provider-owned composition registry按lane解析；adapter不能传policy、临时放大pool或覆盖
physical configure callback。

规则：

- 所有production PostgreSQL adapters构造器required接收provider，不再接收DSN；
- `dsn: str | provider`兼容union、optional DSN fallback和constructor auto-connect全部删除；
- lane只控制bounded pool/metrics/statement policy，不改变schema authority；caller不能自定义conninfo或configure hook；
- direct connection与pool中的每个new physical connection都由provider创建；
- `ConnectionPool.configure`在连接可见于adapter前执行唯一`validate_physical_connection()`；
- pool rebuild、idle replacement、broken-connection replacement与首次connect使用同一validator；
- lease release后provider拒绝创建新connection；已借出的connection按lane deadline有界drain；
- production operation必须传递由最外层owner创建的absolute deadline，provider和adapter不得续期。

每个new physical connection必须重新查询并精确匹配binding：

```text
current_database()
database OID
current_user
current_schemas(false)
server_version_num
```

只有endpoint、identity和完整ledger probe成功读取后证明任一不一致时，provider才在把connection交给pool/adapter前
关闭它，并返回稳定`schema_database_identity_mismatch`。查询deadline、`QueryCanceled`、连接中断或其他未完成probe
只关闭当前physical connection，分别返回`schema_deadline_exceeded`或`schema_connection_failed`，不得invalidate共享
provider。不得通过`SET ROLE`、`SET search_path`或切换database把错误连接修成看似匹配。validator使用
read-only/autocommit probe或显式rollback，成功交付时connection必须处于IDLE transaction status，不能把catalog
probe transaction泄漏给业务adapter。

首次confirmed physical identity mismatch会把对应provider key转为`INVALIDATED`、关闭其pools并拒绝未来connection；
不能让pool后台无限重连。恢复需要新的process verification service/lease。

#### 12.4.1 V1 conninfo contract

为了让每次重连仍指向同一authority，V1 runtime connection factory和migration admin connector都只接受：

- 单一host或单一Unix socket；
- 单一port；
- 显式database；
- 显式user；
- allowlisted authentication/TLS fields；
- provider-owned `application_name`和`connect_timeout`。

V1拒绝：

- libpq `service`/`servicefile`；
- multi-host/multi-port failover；
- `options`或任何可注入role/search_path/session GUC的字段；
- `target_session_attrs`；
- unknown URI/query/keyword parameter。

password、passfile和TLS credential只由connection factory secret owner持有，不进入target fingerprint、binding或report。
factory解析后不得把原始DSN原样交回libpq；它使用allowlisted keyword args构造connection，并拒绝或显式neutralize
`PGSERVICE/PGSERVICEFILE/PGOPTIONS/PGTARGETSESSIONATTRS`等authority-bearing environment defaults。连接建立后还要
检查`connection.info.get_parameters()`的effective host/port/database/user与resolved target一致。

未来支持service或multi-host必须新增resolved endpoint-set authority，并证明每个endpoint属于同一database world；不能只
把这些字段从fingerprint排除。

#### 12.4.2 Production/test split

`psycopg.connect()`和`ConnectionPool()`在production package中的allowlist只有：

- verified connection provider；
- privileged migration runner/admin verifier preflight。

测试可在`tests/support`使用显式`UnverifiedTestPostgresConnectionProvider`。production module不得import它，production
adapter也不得为了测试保留raw DSN overload。需要直接catalog corruption的测试通过test provider的明确unsafe lane
执行。

### 12.5 Runtime wiring

`build_durable_runtime_wiring()`改为required接收`VerifiedPostgresAccessLease`。EventLog/store/UOW从lease取得
同一个`VerifiedPostgresConnectionProvider`，不再自行读取、保存或比较DSN，因此不存在“binding来自A、connection
B”的窗口。

它不再次验证、迁移或执行DDL。

### 12.6 其他 composition roots

以下root在构造store前执行一次fast verify：

- `pulsara inspect ...`
- `pulsara checkpoint doctor/gc ...`
- core dogfood runner；
- durable benchmark runner/template validator；
- 任何生产embedder公开factory。

这些root取得lease后必须把provider传给所有下游adapter；“先verify、随后仍按settings DSN自行连接”不符合contract。

`config-check`、skills和MCP config management不访问PostgreSQL，不需要schema verify。

### 12.7 Low-level adapters

low-level store可以假设provider已验证schema，但不能假设任意caller-provided connection可信。若component test通过
unverified test provider连接未迁移database，PostgreSQL `UndefinedTable`是合法错误；store本身不得偷偷修复。

## 13. 必须删除的 runtime DDL

SH3 hard cut必须逐项移除：

| 文件/对象 | 删除内容 |
|---|---|
| `event_log/postgres.py::PostgresEventLog` | `__post_init__`中的runtime truth schema执行 |
| `graph/postgres.py::PostgresGraphStore` | `initialize_schema`参数、`ensure_schema()`和constructor DDL |
| `host/resume.py` | `_ensure_schema()` |
| `host/session_manifest.py::SessionManifestStore` | `ensure_schema()`及每个operation调用 |
| `memory/candidates/pool.py::PostgresCandidatePool` | candidate schema bootstrap |
| `memory/candidates/projection_outbox.py` | projection outbox schema bootstrap |
| `memory/canonical/index_sync.py` | search index schema bootstrap |
| `memory/canonical/mutation_outbox.py` | mutation outbox schema bootstrap |
| `memory/canonical/oxigraph_materializer.py` | materializer schema bootstrap |
| `memory/canonical/reconcile.py` | reconciler schema bootstrap |
| `memory/canonical/vector_index_sync.py` | vector schema bootstrap |
| `memory/canonical/unit_of_work.py` | UOW入口三组DDL与`initialize_schema=False`兼容参数 |
| `memory/governance/claims.py` | claim schema bootstrap |
| `memory/governance/event_outbox.py` | event outbox schema bootstrap |
| `memory/governance/preparation.py` | batch input schema bootstrap |
| `memory/recall/trace.py` | trace schema bootstrap |
| `memory/working_context.py` | working-context schema bootstrap |

最终`src/pulsara_agent`中的schema-object DDL只能存在于`storage/migrations/sql/*.sql` packaged resources。
唯一例外是第9.3节admin-only typed ACL grant executor；Verifier catalog queries不属于DDL。

## 14. CLI contract

### 14.1 Commands

新增：

```bash
uv run pulsara db status  --env-file .env
uv run pulsara db migrate --env-file .env
uv run pulsara db verify  --env-file .env
uv run pulsara db verify  --env-file .env --deep
```

公共参数：

- `--env-file`
- `--override-env`
- `--prefix`，默认`PULSARA`
- `--timeout-seconds`

不提供接受明文DSN的CLI参数，避免shell history/process list泄露。

### 14.2 Config loading

`db`命令使用独立`PostgresDatabaseCommandConfig`，只读取：

- runtime DSN；
- admin DSN；
- timeout。

它不构造`PulsaraSettings`，因此不要求LLM API key、model或Oxigraph配置。

### 14.3 Command semantics

| Command | Connection | Mutation | Exit behavior |
|---|---|---|---|
| `status` | runtime DSN | 无 | connection成功即0；report可显示uninitialized/behind |
| `verify` | runtime DSN | 无 | exact valid为0；invalid为2 |
| `migrate` | admin + runtime DSN | migration/grants | latest为0；任何partial/invalid为2 |

`migrate`不启动Host，不执行Oxigraph reconcile，也不运行model API。

`status`在同一个runtime physical connection和同一个repeatable-read、read-only transaction中读取database identity与完整
migration ledger，再执行history classification；不得通过第二条preflight connection拼接可能来自不同failover/recreate
world的report。

### 14.4 Bounded JSON report

输出允许：

- database name/OID；
- normalized effective search path；
- runtime role；
- PostgreSQL/pgvector version；
- observed/expected head；
- durable observed/expected registry prefix fingerprint；
- applied/pending version tuple；
- privilege reconciliation status、bounded added grants和stale grantee warning count；
- verification status/failure code；
- `runtime_role_can_create_in_public_schema` warning；不把该有界probe泛化为任意object ownership/DDL authority。

禁止输出：

- DSN；
- password/userinfo；
- arbitrary SQL error detail中的连接字符串；
- migration SQL全文。

## 15. Failure taxonomy

新增稳定error codes：

| Code | 分类 | 行为 |
|---|---|---|
| `schema_connection_failed` | operational | fail，保留bounded cause |
| `schema_connection_target_unsupported` | config/authority | fail before preflight |
| `schema_physical_connection_identity_mismatch` | authority | close connection，fail closed |
| `schema_access_lease_expired` | lifecycle | 拒绝新connection |
| `schema_admin_dsn_missing` | config | migrate fail |
| `schema_database_identity_mismatch` | config/authority | fail closed |
| `schema_search_path_mismatch` | config | fail closed |
| `schema_migration_lock_timeout` | operational | fail，不改schema |
| `schema_uninitialized` | expected setup | Host/verify fail，提示migrate |
| `schema_unmanaged_database` | hard-cut boundary | migrate fail，要求reset |
| `schema_history_gap` | corruption | fail closed |
| `schema_migration_checksum_mismatch` | corruption/binary mismatch | fail closed |
| `schema_migration_contract_mismatch` | corruption/binary mismatch | fail closed |
| `schema_registry_prefix_mismatch` | corruption/binary mismatch | fail closed |
| `schema_version_behind` | deploy ordering | Host fail，先migrate |
| `schema_version_ahead` | deploy ordering | Host fail，升级binary |
| `schema_extension_missing` | prerequisite | migrate/verify fail |
| `schema_extension_version_unsupported` | prerequisite | fail |
| `schema_required_object_missing` | drift/corruption | fail closed |
| `schema_catalog_contract_mismatch` | drift/corruption | deep verify fail |
| `schema_runtime_privilege_missing` | deployment | fail before Host resources |
| `schema_runtime_grant_reconciliation_failed` | deployment/authority | transaction rollback，保留pre-state |
| `schema_runtime_grant_confirmation_conflict` | authority/confirmed conflict | fail closed，inspection |
| `schema_runtime_grant_confirmation_unresolved` | operational/unknown outcome | 停止mutation，下次先确认 |
| `schema_migration_postcondition_failed` | migration | rollback该version |
| `schema_migration_confirmation_conflict` | corruption/confirmed conflict | fail closed，inspection/reset |
| `schema_migration_confirmation_unresolved` | operational/unknown outcome | 停止mutation，可重试，不建议reset |

不能将checksum/catalog conflict降级为warning或自动运行baseline SQL。

## 16. Crash、cancel 与重试矩阵

| 窗口 | Durable结果 | 重试行为 |
|---|---|---|
| 取得lock前取消 | 无变化 | 可直接重试 |
| migration transaction SQL中失败 | transaction rollback，无row | 重试同version |
| grants失败 | DDL/grants/row同transaction rollback | 修权限后重试 |
| up-to-date ACL preflight失败 | 无ACL mutation | 修grant authority后重试 |
| up-to-date ACL第N条GRANT/final verify失败 | reconciliation transaction整组rollback | 修环境后重试 |
| up-to-date ACL COMMIT UNKNOWN | 不发布added-grants report | new connection重取lock并确认full/pre-state/unresolved |
| postcondition失败 | transaction rollback，无row | 修migration/环境后重试 |
| ledger row insert前进程崩溃 | transaction rollback | 重试 |
| commit确认UNKNOWN | 新connection重取exclusive lock，读取ledger prefix与catalog | FULL adopt；NONE安全重试；CONFLICT fail closed；UNRESOLVED停止本次mutation后可重试 |
| 一个version FULL，下一version失败 | 已完成version保留 | 重试从下一version继续 |
| final deep verify失败 | applied history保留，但Host仍拒绝启动 | offline repair/reset，不伪造row |
| 第二migrator等待 | 无变化 | 获锁后重新读取，返回up_to_date或继续 |

runner不得用process-local“最后尝试version”作为恢复真源。

### 16.1 Commit confirmation four-state

`COMMIT`发生connection loss时，原connection不再提供authority。runner必须：

1. 使用new admin connection；
2. 在原absolute deadline内重新取得同一exclusive advisory lock；
3. 读取完整ledger head/prefix和canonical catalog；
4. 将candidate分类为唯一`FULL | NONE | CONFLICT | UNRESOLVED`。

分类矩阵：

- `FULL`：candidate row存在，且version/name/checksum/migration contract/prefix全部精确相等；resulting object
  manifest与postcondition也精确成立。adopt该commit并继续；
- `NONE`：head仍是previous version，candidate row不存在，catalog精确等于previous resulting manifest，且不存在
  candidate新增对象。若deadline仍充足，可在同一新lock owner下安全重试；否则返回retryable NONE，由下一次
  `db migrate`继续；
- `CONFLICT`：已经取得完整ledger/catalog authority，并证明row只部分匹配、head异常或catalog既非previous也非
  resulting manifest。fail closed并要求inspection/reset；
- `UNRESOLVED`：无法在deadline内建立new connection、取得lock或完整读取authority。本次invocation不得继续任何
  schema/ACL mutation，但这不是corruption，不建议reset。下一次`db migrate`必须先对同一stable candidate重新执行
  confirmation，再决定FULL/NONE/CONFLICT。

不能把“没有证明FULL”直接等同于CONFLICT，也不能仅凭candidate table/index存在推断FULL。UNRESOLVED report保存
bounded candidate version/contract/prefix identity，不保存DSN或process-local connection state；若另一migrator已经推进
head，下一次先验证完整history，再按新的head继续。

## 17. Test infrastructure hard cut

### 17.1 PostgreSQL tests 显式分类

新增`postgres` pytest marker。需要真实PostgreSQL的测试必须显式请求统一fixture，不再在每个文件复制`_connect_or_skip()`并依赖constructor建表。

### 17.2 Per-worker migrated database

`tests/support/postgres_database.py`拥有：

```text
admin server connection
  -> create fresh per-xdist-worker database
  -> run exact migration registry once
  -> verify runtime role
  -> expose VerifiedTestPostgresDatabaseLease
  -> optionally expose explicit UnverifiedTestPostgresConnectionProvider
     only to catalog-corruption/negative tests
  -> terminate connections/drop database at worker teardown
```

规则：

- unit tests不请求fixture，因此无PostgreSQL也能运行；
- marked tests在server/admin DSN不可用时按现有policy skip；
- CI中PostgreSQL/pgvector不可用属于环境失败，不能静默让整组integration tests消失；
- database名由run/worker identity确定性sanitize，不含secret；
- 每个worker独立database，支持`pytest-xdist`；
- ordinary integration tests只接收verified test lease；raw DSN留在fixture owner内部；
- corruption tests必须在test name/marker中声明unsafe provider，不能让production adapter恢复DSN constructor；
- migration fixture setup不计入被测runtime hot-path timing。

### 17.3 Replace SQL-string tests

删除只对`*_SCHEMA_SQL`做substring assertion的测试，改为：

- registry continuity/checksum test；
- fresh database migration；
- catalog manifest equality；
- constraints/indexes/typmod查询；
- runtime role privilege test；
- no-DDL architecture guard。

### 17.4 Required integration cases

1. fresh Pulsara-empty DB迁移到latest；
2. migrate latest再次执行为no-op；
3. 两个并发migrator只有一个应用每个version；
4. synthetic failing migration transaction零partial object/row；
5. ledger checksum、migration contract和registry prefix任一tamper均被拒绝；
6. version gap/unknown future version被拒绝；
7. owned table存在但ledger缺失时拒绝adopt；
8. pgvector缺失/过旧时稳定失败；
9. runtime role可以完整Host DML但不能创建table/extension；
10. runtime constructor/UOW在无DDL权限角色下工作；
11. `current_schemas(false)`不是精确`("public",)`或database identity不一致时拒绝；
12. migration lock deadline有效；
13. wheel/sdist包含全部SQL resources且checksum一致；
14. 两个fresh database中pgvector OID不同但logical schema fingerprint相同；
15. fast verifier发现drop/retag column、删除default、generated/identity drift、constraint drift和minimum function
    contract drift；
16. deep verifier发现drop index、alter expression/function body和完整catalog state drift；
17. up-to-date database可rebind replacement runtime role并修复missing grant；
18. grant authority/preflight或第N条grant失败时整组ACL transaction rollback；
19. ACL COMMIT UNKNOWN分别覆盖complete set、exact pre-state、conflict和unresolved；
20. `PUBLIC`已提供effective type privilege时不要求extension owner重写ACL；
21. migration UNKNOWN confirmation分别覆盖FULL、NONE、CONFLICT和UNRESOLVED；
22. direct与pooled physical connection发生database/role/search-path/server drift时在checkout前拒绝；
23. pool首次connect、idle replacement和broken reconnect都重新执行physical validator；
24. service/multi-host/options/unknown conninfo parameter稳定拒绝；
25. architecture guard证明production adapters不调用`psycopg.connect`/`ConnectionPool`且不接受DSN；
26. 多个HostCore共享一个process verification attempt；waiter cancellation只detach；
27. multi-database/role verification keys不串用；
28. benchmark generator拒绝裸DSN和mismatched/released lease；
29. Host schema failure发生在session reservation、MCP和terminal resource之前。

## 18. Benchmark lifecycle

Durable runtime benchmark的template database必须：

- migration head exact latest；
- durable registry prefix fingerprint匹配binary；
- required extension/object/deep catalog全部通过；
- 所有业务表为空；
- 只有migration ledger rows和extension/catalog固定对象存在。

`PostgresTemplateDatabaseSandbox`每次clone后先fast verify，再进入计时。它返回
`VerifiedBenchmarkDatabaseLease`，而不是裸DSN。该lease包含secret-safe schema binding，并将connection creation委托给
sandbox-owned `VerifiedPostgresConnectionProvider`；clone verification和database setup都在measurement interval之外。

以下production-path generators改为required接收该lease，禁止自行用DSN直接构造`PostgresEventLog`：

- `benchmarks/durable-runtime/generators/model_semantic_batch.py`；
- `benchmarks/durable-runtime/generators/context_preparation.py`。

runner、generator和result writer必须join同一lease identity。generator收到裸DSN、binding/database OID不匹配或lease已
release时立即拒绝，不得自行重新verify或bootstrap schema。

benchmark environment result新增：

- migration head version；
- durable migration registry prefix fingerprint；
- schema deep-manifest fingerprint；
- Postgres connection/pool policy fingerprint；
- PostgreSQL/pgvector version；
- template business-empty status。

不同schema fingerprint的samples不可聚合为同一baseline。

## 19. Docker 与本地开发

### 19.1 Docker

删除`docker/postgres-init/001-pgvector.sql`作为Pulsara schema真源。pgvector image只提供extension binary，schema安装由migration 0001拥有。

bundled Docker固定演示两个role：

- `pulsara_admin`：container/database owner，只给migration DSN；
- `pulsara_runtime`：login/runtime application role，只给runtime DSN。

`POSTGRES_USER`切为`pulsara_admin`。新的Docker init artifact只负责创建
`pulsara_runtime` role并授予目标database `CONNECT`；它不得创建extension、function、table或index。
Pulsara migration负责extension、tables、types、functions、indexes和runtime grants。该init artifact是
container role genesis，不是第二个schema migration owner。

### 19.2 `.env.example`

增加：

```text
PULSARA_POSTGRES_ADMIN_DSN=postgresql://pulsara_admin:...@localhost:5432/pulsara
PULSARA_POSTGRES_DSN=postgresql://pulsara_runtime:...@localhost:5432/pulsara
```

admin DSN不进入`PulsaraSettings.redacted_dict()`的runtime storage内容，只由db command loader读取。

### 19.3 Quick Start

Quick Start改为：

```bash
docker compose up -d postgres oxigraph
uv run pulsara db migrate --env-file .env
uv run pulsara db verify --env-file .env
uv run pulsara host repl --env-file .env --workspace .
```

不得继续描述“runtime schema initialization会创建/升级表”。

## 20. Production cutover

SH3是不可滚动、不可拆分的deployment hard cut：

1. 停止所有Host、worker、Inspector long-running process和benchmark；
2. 备份需要保留的数据；V1实现不消费该备份；
3. 重置旧Pulsara PostgreSQL database；
4. 若旧PostgreSQL world被丢弃，同步重置Oxigraph projection；
5. 创建/确认admin与runtime role及database CONNECT；
6. 运行`pulsara db migrate`；
7. 使用runtime DSN运行`pulsara db verify --deep`；
8. 启动新Host；
9. 运行无DDL权限Host smoke与core dogfood。

禁止：

- 先部署新binary、期待第一次请求自动迁移；
- migration期间保留旧Host写入；
- 新schema上回滚到旧binary；
- 通过恢复旧`ensure_schema()`临时救场。

## 21. 实施阶段

### SH0：Contract、registry 与 canonical baseline

新增：

- long-term PostgreSQL schema migration contract；
- migration contracts/errors/registry；
- `0000..0004` packaged SQL；
- canonical object manifest；
- checksum、migration contract与durable registry prefix tests；
- fresh DB shadow migration/catalog tests。

此阶段production仍使用旧bootstrap；新migration仅测试使用。为了控制短暂dual truth，必须有测试证明：

- old runtime schema的最终catalog；
- fresh migration产生的catalog；
- packaged manifest；

三者在业务对象上完全相等。SH3会删除old truth。

**SH0 gate**

- Ruff；
- migration resource checksum；
- registry continuity；
- migration contract与registry prefix recurrence；
- canonical JSON/domain hash golden vectors与caller-self-reported fingerprint rejection；
- fresh database apply；
- catalog equality；
- two-fresh-database logical catalog fingerprint equality（排除OID）；
- wheel resource inclusion。

### SH1：Runner、role grants 与 CLI

实现：

- migration advisory lock；
- genesis/unmanaged DB判定；
- applied history validation；
- atomic per-version runner；
- exact grant executor与up-to-date privilege reconciliation；
- status/migrate/verify CLI；
- bounded JSON report与error taxonomy；
- concurrency/crash/deadline tests。

production runtime仍未切换。

**SH1 gate**

- migrate/no-op/concurrent/failure与UNKNOWN FULL/NONE/CONFLICT/UNRESOLVED确认测试；
- durable migration contract/registry prefix tamper test；
- up-to-date runtime role rebind、atomic missing-grant repair、ACL UNKNOWN confirmation和`PUBLIC` effective
  privilege test；
- admin/runtime database mismatch；
- no admin DSN fallback；
- CLI不加载LLM配置；
- report secret redaction。

### SH2：Verifier、Host binding、test 与 benchmark lifecycle

实现：

- fast/deep verifier；
- secret-safe binding与opaque access lease；
- process-owned verification service、HostCore borrower；
- verified physical connection provider、pool policy registry和conninfo fail-closed parser；
- Inspector/checkpoint/dogfood composition preflight；
- per-worker migrated Postgres fixture；
- benchmark template verify与result identity。

此阶段Host binding可以先以shadow/required-in-test方式接线，但旧stores仍暂时执行schema SQL，不能宣称hard cut完成。

**SH2 gate**

- Host pre-resource failure ordering；
- exact head/ahead/behind/checksum/contract/prefix/catalog/privilege矩阵；
- complete fast executable-schema drift matrix；
- multi-Host shared attempt、waiter cancellation、shutdown drain和multi-database partition；
- direct/pool/reconnect physical identity validation与forbidden conninfo matrix；
- xdist per-worker database；
- benchmark lease required且clone verify在measurement外；
- unit tests在无Postgres环境仍可运行。

### SH3：Production atomic hard cut

在一个不可拆分的production cut中：

- 所有production composition root required verified access lease；
- 第23节physical connection owner全集required接收verified provider，删除raw DSN constructor/field；
- `psycopg.connect`/`ConnectionPool`收敛到provider和migrator allowlist；
- 删除第13节全部runtime DDL/`ensure_schema()`；
- 删除六组旧SQL常量和bootstrap exports；
- 删除`PostgresGraphStore.initialize_schema`；
- UOW不再执行DDL；
- 更新contracts、README、env与Docker；
- 执行数据库/Oxigraph reset与migration；
- 不保留fallback。

**SH3 gate**

- architecture guard证明schema-object DDL只存在migration SQL resources，ACL只存在typed grant executor；
- architecture guard证明production adapters不再创建physical connection或持有raw DSN；
- runtime role无DDL权限；
- durable Host open/run/resume/close；
- governance、recall、vector、Inspector、checkpoint定向集成测试；
- migration前旧DB明确失败，不被silent adopt。

### SH4：Hardening 与旧测试真源删除

- SQL substring tests改为catalog tests；
- 所有Postgres tests统一fixture/marker；
- 删除重复`_connect_or_skip()` helper；
- deep-manifest drift probe；
- import/grep guards；
- Inspector health显示schema head/fingerprint和last verify，不显示admin信息；
- 将原债务项标记closed。

**SH4 gate**

- 全量offline pytest；
- 全量Postgres integration；
- `pytest -n auto`无database collision；
- package install后migration resources可读取；
- docs/contracts一致性grep。

### SH5：最终验证

无需为schema改造重跑所有历史real-LLM tests。最终验证：

1. fresh Docker/database bootstrap；
2. admin migrate + runtime deep verify；
3. runtime roleDDL denial probe；
4. Host REPL smoke；
5. 一个冻结core long dogfood；
6. writer/context benchmark各一个smoke case，确认schema setup不进入measurement；
7. shutdown/reopen/resume；
8. Definition of Done复核。

## 22. Architecture guards

新增静态guard：

1. AST扫描`src/pulsara_agent/**/*.py`的可执行string constants，不得包含schema-object DDL；注释和纯文档
   docstring可以出现SQL术语，但不能被runtime读取执行；
2. `CREATE|ALTER|DROP|TRUNCATE|REINDEX|COMMENT ON` schema-object statements只允许
   `storage/migrations/sql/*.sql`；
3. `CREATE EXTENSION`只允许migration 0001；
4. stores/UOW不得定义或调用`ensure_schema()`；
5. production不得import已删除`*_SCHEMA_SQL`；
6. Host/wiring/Inspector/checkpoint composition必须从process service取得verified access lease；
7. admin DSN symbol只允许db command/migration/test/benchmark admin owners；
8. migration versions连续且旧resource checksum immutable；
9. baseline migration除extension外不得包含`IF NOT EXISTS`；
10. migration SQL不得包含non-atomic forbidden operations；
11. tests不再直接依赖store constructor创建schema；
12. benchmark result必须包含durable registry prefix/schema fingerprint；
13. `src/pulsara_agent`内的`GRANT`只允许唯一`PostgresRuntimeGrantExecutor`；Docker role-genesis artifact中的
    exact database `CONNECT` grant是独立deployment allowlist；`REVOKE`在V1 production完全禁止；
14. grant executor的object kinds、privilege enums和statement templates必须与typed grant manifest精确相等；
15. production `psycopg.connect()`只允许verified connection provider和privileged migrator/preflight owner；
16. production `ConnectionPool()`只允许verified connection provider；
17. PostgreSQL adapter constructor不得声明`dsn/postgres_dsn/conninfo`参数或`dsn | provider` union；
18. `UnverifiedTestPostgresConnectionProvider`只能位于`tests/support`，`src/pulsara_agent`不得import；
19. conninfo parser必须拒绝service、multi-host、options、target-session-attrs和unknown fields。

guard扫描整个调用和import surface，不能只检查一个constructor函数字面量。

DDL detector使用token/AST规则识别statement-leading `CREATE|ALTER|DROP|TRUNCATE|REINDEX|COMMENT ON`，不得仅搜索
一个具体constructor调用字面量。catalog verifier的只读`SELECT`与migration resource loader属于明确allowlist；Python
module中不允许嵌入schema migration SQL。

ACL guard单独扫描`GRANT|REVOKE`：只允许admin-only grant executor中由`psycopg.sql.SQL`固定模板、typed enum和
`sql.Identifier`组成的allowlisted `GRANT`；caller-provided SQL/object/privilege立即失败。这样动态role binding可实施，
又不会把grant executor变成第二个通用migration入口。

connection guard同时扫描constructor signatures、imports和call graph；只禁止`psycopg.connect`字面量仍不够，因为
wrapper helper或pool factory同样可能重新引入raw conninfo owner。

## 23. 修改面

### 新增

- `src/pulsara_agent/storage/migrations/*`
- `src/pulsara_agent/storage/postgres_connection_provider.py`
- `src/pulsara_agent/storage/schema_contract.py`
- `src/pulsara_agent/storage/schema_verification_service.py`
- `contracts/POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md`
- `tests/support/postgres_database.py`
- migration/verifier/CLI/architecture integration tests

### 重点修改

- `src/pulsara_agent/cli.py`
- `src/pulsara_agent/settings.py`或独立db-command config module
- `src/pulsara_agent/host/core.py`
- `src/pulsara_agent/runtime/wiring.py`
- `src/pulsara_agent/event_log/postgres.py`
- `src/pulsara_agent/graph/postgres.py`
- `src/pulsara_agent/host/resume.py`
- `src/pulsara_agent/host/session_manifest.py`
- 第13节列出的全部memory/governance stores
- `benchmarks/durable-runtime/runners/postgres_sandbox.py`
- `benchmarks/durable-runtime/runners/result_contract.py`
- `benchmarks/durable-runtime/runners/run_dataset.py`
- `benchmarks/durable-runtime/generators/model_semantic_batch.py`
- `benchmarks/durable-runtime/generators/context_preparation.py`
- `README.md`
- `.env.example`
- `docker-compose.yml`
- `contracts/GRAPH_JSONLD_STORAGE_CONTRACT.zh.md`
- `contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md`
- `contracts/MEMORY_SURFACES_CONTRACT.zh.md`
- `PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md`

### Physical connection owner迁移全集

以下当前直接创建connection/pool或继续传播raw DSN的production modules必须全部改为required provider；本清单是
SH3 gate输入，不能以“composition root已经verified”为由跳过内部adapter：

- `src/pulsara_agent/event_log/postgres.py`
- `src/pulsara_agent/event_log/postgres_pool.py`
- `src/pulsara_agent/graph/postgres.py`
- `src/pulsara_agent/host/resume.py`
- `src/pulsara_agent/host/session_manifest.py`
- `src/pulsara_agent/inspector/store.py`
- `src/pulsara_agent/memory/artifacts/postgres_archive.py`
- `src/pulsara_agent/memory/candidates/pool.py`
- `src/pulsara_agent/memory/candidates/projection_outbox.py`
- `src/pulsara_agent/memory/canonical/index_sync.py`
- `src/pulsara_agent/memory/canonical/mutation_outbox.py`
- `src/pulsara_agent/memory/canonical/outbox_replay_hook.py`
- `src/pulsara_agent/memory/canonical/oxigraph_materializer.py`
- `src/pulsara_agent/memory/canonical/query.py`
- `src/pulsara_agent/memory/canonical/reconcile.py`
- `src/pulsara_agent/memory/canonical/unit_of_work.py`
- `src/pulsara_agent/memory/canonical/vector_index_sync.py`
- `src/pulsara_agent/memory/canonical/vector_query.py`
- `src/pulsara_agent/memory/governance/claims.py`
- `src/pulsara_agent/memory/governance/event_outbox.py`
- `src/pulsara_agent/memory/governance/preparation.py`
- `src/pulsara_agent/memory/recall/trace.py`
- `src/pulsara_agent/memory/working_context.py`
- `src/pulsara_agent/runtime/long_horizon/checkpoint_maintenance.py`
- `src/pulsara_agent/runtime/subagent/projection.py`
- `src/pulsara_agent/runtime/tool_artifacts.py`

`cli.py`、`host/core.py`、`runtime/wiring.py`和`settings.py`仍负责config/composition，但只能把DSN交给process-owned
connection factory/service；它们不得再把DSN传给上述adapter。

### 删除或重构

- `storage/postgres_schema.py`旧SQL真源；
- `storage/memory_schema.py`旧SQL真源；
- candidate/governance modules内的schema constants；
- `docker/postgres-init/001-pgvector.sql`；
- SQL substring contract tests；
- duplicated per-file PostgreSQL bootstrap helpers。

## 24. Definition of Done

以下全部成立才算完成：

1. migration registry是唯一physical PostgreSQL schema真源；
2. migration ledger canonical genesis不可从旧表自证；
3. 每行migration contract和累计registry prefix都有durable fingerprint对端；
4. unmanaged旧Pulsara database稳定拒绝并要求reset；
5. migration versions连续，SQL checksum、contract和prefix不可漂移；
6. 每个migration DDL、grants、postcondition和ledger row同transaction；
7. schema-object DDL只有packaged SQL owner，动态ACL只有typed grant executor owner；
8. up-to-date privilege reconciliation的preflight、全部GRANT和final verify处于一个transaction；
9. up-to-date database支持runtime role rebind，rollback/UNKNOWN不虚报`added_grants`；
10. 两个migrator被advisory lock正确串行化；
11. migration全程使用一个absolute deadline；
12. commit UNKNOWN可确定分类FULL、NONE、CONFLICT或UNRESOLVED，且后两者语义不混用；
13. canonical hash encoding、ledger/manifest/grant/catalog/verification DTO与中央factory唯一；
14. startup只做verify，不做DDL或grant repair；
15. Host schema failure发生在任何session/resource副作用前；
16. EventLog/store/UOW不再拥有`ensure_schema()`；
17. runtime role不需要CREATE/ALTER/EXTENSION权限；
18. runtime role可以执行完整Host、memory、governance、recall和checkpoint DML；
19. admin DSN不进入runtime settings/wiring/log/Inspector；binding、lease和adapter均不保存raw DSN；
20. database OID/name、runtime role、server version和`current_schemas(false) == ("public",)`精确验证；
21. 所有production PostgreSQL adapters required接收同一个verified connection provider；
22. direct/pool/reconnect创建的每个physical connection在可见前精确匹配binding；
23. service、multi-host、options、target-session-attrs和unknown conninfo均fail closed；
24. 除provider/migrator preflight外，production package不调用`psycopg.connect`或`ConnectionPool`；
25. database behind/ahead/checksum/contract/prefix drift均fail closed；
26. pgvector缺失/过旧在migration/verify阶段发现；
27. catalog semantic identity不包含database-local OID；
28. fast verifier有界并验证columns、defaults、collation、generated/identity、constraints、index state和minimum
    function contract；
29. process verification service按target/role/registry分区，一个key只有一个shared attempt；
30. waiter cancellation只detach，service shutdown有界drain verifier owner；
31. deep verifier能发现完整table/index/constraint/function drift；
32. SQL resources被wheel/sdist正确打包；
33. baseline SQL直接创建final schema，不包含旧backfill链；
34. `CREATE EXTENSION`只存在于migration 0001；
35. schema-DDL、ACL和connection architecture guards覆盖整个`src/pulsara_agent`；
36. Postgres tests使用显式migrated lease；unsafe provider只存在于negative tests；
37. unit tests不因migration fixture变成必须连接Postgres；
38. xdist workers不共享可变database；
39. benchmark template exact migrated且business-empty；
40. benchmark generators required接收`VerifiedBenchmarkDatabaseLease`，不直接构造裸DSN EventLog；
41. benchmark samples记录durable registry prefix和schema identity；
42. schema setup/verify不进入measurement interval；
43. README/Docker/env示例要求先migrate再启动Host；
44. Graph/EventLog/Memory长期contract同步；
45. 不提供runtime auto-migrate或旧库lazy adopt；
46. 债务文档明确记录adopt-existing目标已被reset-only V1 supersede；
47. 不声称migration ledger兼容旧event JSON payload；
48. old/new binary不在同一database滚动共存；
49. reset后的PostgreSQL/Oxigraph world一致；
50. fresh Docker、restricted-role Host smoke和core dogfood通过；
51. `PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md`中Schema hot-path状态更新为closed。

## 25. 最终形态

```text
Deployment/Admin
    PULSARA_POSTGRES_ADMIN_DSN
            |
            v
    pulsara db migrate
      exclusive advisory lock
      packaged immutable migrations
      atomic DDL + grants + ledger row
            |
            v
    public.pulsara_schema_migrations

Runtime
    PULSARA_POSTGRES_DSN
            |
            v
    fast verify-only startup
      shared verification lock
      exact head/checksum/object/privilege checks
            |
            v
    VerifiedPostgresSchemaBinding
            |
            v
    VerifiedPostgresConnectionProvider
      validate every new physical connection
      direct + pooled lanes
            |
            v
    Host / EventLog / Memory / Governance / Inspector
      DML only
      no raw DSN / psycopg.connect
      no ensure_schema
      no runtime DDL
```

该结构把“数据库是否可供当前binary安全使用”从每个repository的偶然副作用，提升为deployment与composition root之间唯一、显式、可验证的契约。
