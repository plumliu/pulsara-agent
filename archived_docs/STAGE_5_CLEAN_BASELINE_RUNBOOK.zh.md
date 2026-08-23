# Pulsara Stage 5 clean-baseline migration-universe reset runbook

状态：**DRAFT FOR STAGE 5；未执行；不授权真实环境reset**

适用切换：Stage 5退役legacy migration 0000–0013并安装conversation-kernel clean baseline

实施规格：[STAGE_3_5_IMPLEMENTATION_SPEC.zh.md](STAGE_3_5_IMPLEMENTATION_SPEC.zh.md)

本手册不同于Stage 2的[DURABILITY_SUBTRACTION_CUTOVER_RUNBOOK.zh.md](DURABILITY_SUBTRACTION_CUTOVER_RUNBOOK.zh.md)。Stage 2手册处理authority activation；本手册处理**migration universe replacement**。二者不得混用。

## 1. 固定目标与禁止项

Stage 5只接受以下目标identity：

~~~text
universe_id          = "pulsara.conversation-kernel.v1"
universe_generation  = 1
baseline_version     = 0
baseline_resource    = "0000_conversation_kernel_baseline.sql"
catalog_resource     = "0000_conversation_kernel_expected_catalog_v1.json"
grant_resource       = "0000_conversation_kernel_runtime_grants_v1.json"
ledger               = public.pulsara_schema_migrations
~~~

clean baseline必须一次建立：new ledger genesis、exact required PostgreSQL capability、`pulsara_v3` exact 24 product relations、constraints/functions与closed runtime grants。它不得建立legacy `public` product relation、projection preparation、runtime-write guard、EventLog execution ledger或Oxigraph对象。migration identity只能使用实施规格8.4.2的无环公式与golden vector。

required extension set只有`vector`，必须位于`public`且版本`>= 0.5.0`；`pgcrypto`不是clean Kernel requirement。extension是database-scoped retained capability，不属于默认reset scope：compatible pre-existing vector直接采用，缺失时才由已授权admin安装；wrong schema、too old或type/operator shape不兼容时fail closed。共享数据库不得因本次reset删除、relocate或upgrade extension；只有dedicated database且operator针对exact extension另行授权时才能删除。unrelated pre-existing extension（包括`pgcrypto`）不参与clean catalog exactness。

本次切换明确禁止：

- old v13原地upgrade、converter、import、cold reader或reverse migration；
- old/new migration registry并存或按数据库形状动态选择runtime authority；
- 在partial reset或catalog/grant未确认时启动Host；
- 把reset进度、确认receipt或repair generation写成新的durable产品状态机；
- 未核验exact endpoint/database/schema/blob namespace就执行删除；
- 在本手册或回执中保存DSN、密码、provider key、MCP secret或用户正文。

## 2. Packaged identity与read-only预检

待部署binary必须公开并由deep verifier报告：

1. universe ID与generation；
2. baseline SQL SHA-256；
3. clean expected-catalog artifact SHA-256；
4. runtime-grant artifact SHA-256；
5. 由实施规格8.4.2唯一公式生成的baseline contract、universe fingerprint与genesis registry prefix，并通过固定Python golden；
6. `VerifiedPostgresSchemaBinding v2` contract identity；
7. binary/image identity与应用版本。

在任何reset授权前，operator先做read-only预检并保存：

- PostgreSQL endpoint identity、database name/OID、admin role与runtime role；
- 当前migration ledger列形状、head、registry prefix和识别结果；
- Pulsara-owned schema/relation/function/type/grant inventory，以及database-scoped extension的schema/version/owner与retained disposition；
- Pulsara-owned blob namespace与disposable derived state精确边界；
- active Host writer、job attempt、terminal/subagent/process与外部effect inventory；
- Stage 3/4 checkpoint evidence和当期重新生成的删除manifest摘要。

new runner遇old v13 ledger或不同universe时必须只返回typed non-retryable `MIGRATION_UNIVERSE_RESET_REQUIRED`（wire/CLI value：`schema_migration_universe_reset_required`），不得执行DDL。若资源边界、外部effect归属或expected identity任一不确定，结论为`NO-GO`。

## 3. Go/no-go条件

只有同时满足以下条件才可进入真实reset：

- operator对exact endpoint、database OID、Pulsara-owned schema与blob namespace给出本次明确授权；
- 所有Pulsara进程停止新session、prompt/steer、tool dispatch、job enqueue/claim和memory mutation admission；
- Host writer与job claim domain已fence；
- process-local turn/provider/tool/subagent/terminal/live worker已bounded cancel/join；
- 外部effect均已有operator disposition，不会由new Kernel自动重做；
- Stage 5 binary中old migration SQL、sealed migration-only leaf、projection preparation与legacy registry已不可达；
- clean baseline、catalog、grant artifact和universe fingerprint已冻结且静态gate通过；
- binding v2/verifier/provider已不读取runtime-write epoch或调用admission function，Host writer/job claim regression仍通过。

项目虽不在Stage间发布版本，但physical owner未退出仍然是hard safety fence；不得以“最终还会reset”为由跳过quiescence。

## 4. Reset与baseline安装顺序

### Step 1：停止admission并确认physical quiescence

停止所有Pulsara entrypoint，fence旧writer/worker，bounded join process-local owner。不能证明结果的已开始tool/remote effect保持`outcome_unknown`的operator记录；不向new universe导入attempt、receipt或execution state。

验收：没有task/thread/process能继续访问即将清理的数据库、blob或derived resource。

### Step 2：再次核验目标边界并取得最终授权

紧邻reset前重新读取endpoint/database OID、schema owner与blob namespace，并与预检证据逐项相等。真实删除必须由operator使用届时批准的scope-exact机制执行；本手册不内嵌可复制的destructive SQL或bucket命令。

验收：授权对象与实际连接对象完全一致，且不包含共享数据库/namespace中的其他owner。

### Step 3：清空Pulsara-owned universe

清除旧Pulsara-owned database schema/data、migration metadata、blob namespace与disposable derived state。database-scoped extension按前述retained disposition保留；不得把最初由Pulsara安装等同于当前拥有删除权。不得保留old ledger供new runner“识别后继续”，也不得保留legacy public table作为冷审计面。

若该步骤中断：保持全部Pulsara服务停止；重新执行read-only inventory，将目标归类为`OLD`、`PARTIAL_RESET`或`EMPTY`。只有operator重新确认边界后才能继续完成reset；不得在`PARTIAL_RESET`上运行baseline。

### Step 4：证明empty world

在运行baseline前确认：

- old/new migration ledger均不存在；
- legacy `public` product relation和`pulsara_v3` product relation均为0；
- Pulsara-owned function/type/trigger/sequence/protected-relation metadata为0；
- Pulsara-owned blob与derived namespace为空；
- compatible retained `public.vector >= 0.5.0`可以存在且不破坏empty-world判定；
- non-Pulsara共享对象未改变。

验收：runner的empty-world precondition成立。存在无ledger的Pulsara object时必须停止，不自动drop。

### Step 5：安装version-0 clean baseline

使用Stage 5 binary、privileged migration role和new registry，在migration advisory lock下执行单个atomic version-0 baseline。它采用compatible retained vector，或在缺失且已获安装授权时建立`vector WITH SCHEMA public`；不创建`pgcrypto`。该transaction同时写new ledger genesis并建立exact Pulsara catalog/grants；runtime role在commit前不能访问partial schema。

验收：ledger只有new universe的version 0 genesis；registry prefix、universe fingerprint、SQL/catalog/grant digests与packaged identity一致。

### Step 6：commit confirmation与deep verify

迁移调用正常返回时仍需执行deep verification：relation/function/type/constraint/index、required vector identity/shape与grant exact match，unrelated extension不参与exact set；runtime role不能解析legacy product relation，也不能写migration metadata。verifier随后签发binding v2，并用真实`ConversationKernelRepository` checkout证明不读取runtime-write epoch/guard function。

若baseline commit ACK unknown，只允许重新连接并读取：new ledger genesis、exact catalog与exact grants：

| 观察 | disposition |
|---|---|
| 三者全部匹配 | `FULL`：接受已提交，不重复baseline |
| ledger与new catalog均不存在，且仍是empty world | `NONE`：可在同一授权窗口重试baseline |
| 任一部分存在、identity不符或权限无法确认 | `CONFLICT`：保持服务停止，交给operator |

不增加migration receipt、repair owner或自动drop-and-retry。

### Step 7：Kernel dogfood后开放admission

先以restricted runtime role完成text、tool/multi-tool、rehydrate、Protocol v3、job、memory、blob和Host-close smoke；确认production import graph不加载EventLog/RuntimeSession/projection/Oxigraph/old migrations。随后只开放一个canary session，成功后才开放普通admission。

## 5. Abort与rollback

- reset前abort：保持旧数据不变，继续停机或回到已知binary；不宣称Stage 5 activated。
- reset中abort：保持服务停止；不得启动任何old/new binary读取partial universe。
- baseline前已完成reset：唯一前进路径是修复packaged clean baseline并从已确认empty world安装；没有数据恢复承诺。
- baseline后rollback：再次执行完整quiesce + operator-authorized complete reset，再从empty world安装选定binary的**单一**universe；不把new rows转换回old schema。

## 6. Activation evidence

Stage 5回执至少记录以下不含secret的字段：

~~~text
cutover_id:
operator:
binary_or_image_identity:
source_checkpoint_head:
deletion_manifest_fingerprint:
database_identity_fingerprint:
reset_scope_fingerprint:
old_universe_disposition: RESET_REQUIRED | ABSENT
physical_quiescence_result:
external_effect_disposition_summary:
empty_world_verification:
universe_id: pulsara.conversation-kernel.v1
universe_generation: 1
universe_fingerprint:
baseline_sql_sha256:
clean_catalog_sha256:
runtime_grant_sha256:
baseline_contract_fingerprint:
genesis_registry_prefix_fingerprint:
required_vector_disposition: adopted | installed
required_vector_schema_and_version:
verified_binding_v2_fingerprint:
baseline_commit_confirmation: FULL
deep_verify_result:
kernel_dogfood_result:
canary_result:
final_disposition: activated | aborted_before_reset | stopped_after_reset
~~~

`activated`只在baseline confirmation为`FULL`、deep verify与dogfood/canary全部通过时成立。

## 7. Ephemeral rehearsal gate

真实授权前，至少在ephemeral PostgreSQL/blob namespace验证：

- empty store一次安装version 0成功；
- old v13 fixture只返回`MIGRATION_UNIVERSE_RESET_REQUIRED`且DDL count为0；
- Stage 4 sealed leaf面对任一legacy row/coverage input只返回reset-required，projection handler/drain count为0；
- unmanaged object/no-ledger fail closed；
- baseline transaction失败不留下partial catalog；
- ACK unknown的`FULL/NONE/CONFLICT`三分支；
- migration identity exact通过实施规格8.4.2的三个固定golden；
- catalog或grant单点漂移被deep verifier拒绝；
- compatible retained vector可采用；wrong-schema/too-old/incompatible vector fail closed；pre-existing pgcrypto不影响clean verify且不会被reset删除；
- binding v2 repository checkout不读取epoch；runtime-write SQL object/trigger/callsite为0；stale Host writer/job claim仍分别拒绝；
- runtime role无法访问migration ledger写面或legacy public product name；
- reset中断分类不会启动Host；
- second migrate幂等验证new genesis，不重复DDL。

这些证据是migration safety gate，不是新的Runtime recovery protocol。
