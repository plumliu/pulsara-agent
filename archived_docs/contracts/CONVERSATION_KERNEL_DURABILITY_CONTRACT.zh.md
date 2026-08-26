# Canonical Conversation Kernel Durability Contract

## 1. Truth ownership

`pulsara_v3` 中的 canonical relational rows 拥有当前产品语义真值。Conversation、
turn、entry、assistant block、tool attempt/result、interaction decision、prompt queue、
subagent task/result、context binding、job、memory 与 blob reference 均由 closed
repository transaction 写入。

`agent_events` 只拥有 accepted occurrence truth：

- event vocabulary exact 26；
- subject slot exact 13；
- append guard exact 2：`HostWriterGuard` 与 `JobAttemptClaimGuard`；
- append 只能发生在 canonical transaction 内部；
- query 不能证明 corresponding canonical row 仍存在；
- reopen 不读取 event journal 来恢复 execution。

不存在 universal EventLog、151-type grammar、generic publisher、historical execution
decoder、replay reducer、checkpoint、repair receipt 或 derived head authority。

## 2. Foreground transaction boundary

一个普通 text turn 的 durable 核心是：

1. 接受 user entry并创建 running turn；
2. 在 dispatch provider 前冻结 immutable context binding revision 与 exact
   `provider_input_through_sequence`；
3. 将完整 assistant message和 ordered blocks原子提交；
4. 将 turn terminal state提交。

Tool path必须满足：

1. 完整 assistant tool-request message先提交；
2. policy decision完成；
3. physical dispatch前提交唯一 logical-call attempt；
4. physical outcome返回后提交唯一 model-visible tool result；
5. unknown physical outcome不得自动 retry。

Assistant block以 provider Start ordinal排序，不以 End顺序排序。Canonical content
超过 inline bound时先写 immutable content-addressed blob，再由 canonical transaction
验证 digest/size并安装受 FK 保护的 reference。无引用 blob 只能在 fixed grace 后由
best-effort bounded GC 删除。

## 3. Context 与 compaction

Context input从 canonical rows和 immutable context snapshot构造，不从 execution event
replay。每次 accepted provider-generated assistant entry保存 exact binding revision及
本次 pre-dispatch input cut。

Mid-turn safe point可以切换到新的 immutable binding revision。已经被后续 provider
input采用的 model-generated compaction snapshot属于 replay-significant semantic
artifact：创建失败不得回滚已完成 reply，但 adopted snapshot的正文、source cut与
compiler/model contract必须保留。

## 4. Crash 与 reopen

Reopen只执行 conversation rehydrate：

- acquire新的 Host writer generation；
- 将上一个 Host遗留的 running turn标记为 `INTERRUPTED`；
- 读取 canonical conversation/context facts；
- 启动新的 process-local execution owners。

不支持 execution replay。Provider stream、coroutine、pending interaction、terminal
process与subagent task不跨 Host恢复。存在 attempt但没有 result的 interrupted effect被
视为 outcome unknown，除非具名 remote-queryable contract能查询同一个 physical attempt。

## 5. Writer 与 job fence

每 session至多一个 active Host writer。所有 foreground mutation在事务内 exact验证
session ID、writer generation与writer owner ID。

Durable jobs由 `durable_jobs` 保存 intent/aggregate state，由
`durable_job_attempts` 保存每次 claim、finite retry lineage与 physical outcome。Claim
mutation必须 exact验证 job/attempt/generation/owner。Job worker不能写 transcript或
subagent conversation rows。

Handler catalog exact四类：background compaction、post-compaction memory extraction、
memory governance、memory-index refresh。不得增加 generic handler或第五类 work。

## 6. Derived isolation

Presentation、search index、notifications、ordinary hooks、audit sampling与live frames是
derived或process-local plane。它们失败时不能反向否决 canonical commit、把 completed
turn改为 failed，或阻塞 Host close。跨进程必须完成的工作只能进入 exact-four durable
job catalog。
