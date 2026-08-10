# PostgreSQL Clean Migration Contract

## 1. 唯一 universe

```text
universe_id          = pulsara.conversation-kernel.v1
universe_generation  = 1
baseline_version     = 0
baseline_resource    = 0000_conversation_kernel_baseline.sql
catalog_resource     = 0000_conversation_kernel_expected_catalog_v1.json
grant_resource       = 0000_conversation_kernel_runtime_grants_v1.json
ledger               = public.pulsara_schema_migrations
```

Registry version从 0 contiguous增长。当前 registry只有 version 0。Baseline直接建立
`pulsara_v3` exact 24 product relations与最小 migration metadata，不先建 legacy tables
再删除。

## 2. Universe recognition

Runner在 advisory lock内先只读识别：

- 无 ledger且 Pulsara-owned world为空：允许安装 baseline；
- ledger/universe/resource/checksum/prefix完全匹配：验证或继续同一 universe；
- old v13、不同 universe、legacy product relation、unmanaged Pulsara world：返回
  non-retryable `schema_migration_universe_reset_required`，DDL count必须为 0。

没有 old importer、online translator、cold reader、reverse migration或 dual schema。
Reset只允许 operator明确授权的 exact endpoint/database/blob boundary；test使用 ephemeral
database。Reset默认不删除 database-scoped extension。

## 3. Extension

Clean required extension set exact为 `public.vector >= 0.5.0`。Compatible pre-existing
vector直接采用；缺失时只在 admin获授权时安装；wrong schema、too old或 required
type/operator shape不兼容时 fail closed。

`pgcrypto`不是 requirement。Unrelated pre-existing pgcrypto不参与 catalog drift，也不被
默认 reset删除。

## 4. Binding v2

`VerifiedPostgresSchemaBinding` 使用
`pulsara:verified-postgres-schema-binding:v2`，只绑定：

- database target/name/OID、exact public search path；
- runtime role/server version、public.vector version；
- universe ID/generation/fingerprint；
- migration head与registry prefix；
- verified combined catalog fingerprint；
- clean grant-policy与verification-contract fingerprint；
- binding fingerprint。

它不包含 runtime-write epoch、guard secret、maintenance mode或 admission lock。Connection
checkout只重验 database/role/search path和 ledger universe/head/prefix。

Host writer generation与job claim generation由各自 canonical row/CAS验证；binding不是第三
种 mutation guard。

## 5. Identity encoding

Migration identity只使用 `storage.migrations.contracts.canonical_json_bytes` 与该模块的
fingerprint helper。禁止第二套 JSON/hash encoder，禁止 fingerprint自引用。

固定 golden：

```text
baseline_contract = sha256:8390ab92c98ed167b03a3fd73943750bd23b148538c4eb5f75714b5398cbd240
universe           = sha256:9f3b3cc41831e3dd7ddff91ff9b0c4f35d421745c25a3d346331c95a2073ca19
genesis_prefix     = sha256:62c84b5c8e9dec93c3c76f1ba4da1892983dd431bc1be51d6d3d9cb12d7cdcc4
```

Baseline commit confirmation只有 `FULL | NONE | CONFLICT`。FULL采用；NONE只在 world仍为空时
有限重试；CONFLICT停止服务并要求 operator处置。

## 6. Privilege

Admin role拥有 migration DDL。Runtime role只能按 clean grant artifact访问 `pulsara_v3`
所需对象和只读 migration ledger；不能修改 ledger。Catalog/grant drift均 fail closed。
