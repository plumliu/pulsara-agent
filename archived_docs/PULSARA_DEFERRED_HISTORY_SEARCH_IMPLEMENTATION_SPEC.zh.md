# 整轮历史搜索的延后维护与发布准入

日期：2026-10-22。用户在完成隔离事务、NKU和短时持续负载实验后，授权冻结并生产实施。
本规格取代会话载入规格第九轮对“暂停搜索实现”的阶段限制；其余载入、provider prefix和epoch合同不变。

## 产品语义

搜索继续按整轮QA/QAQ及tools/no-tools变体评分。允许搜索短暂落后；纯追加不得只因实时尾部变化，
使上次成功覆盖的真实前缀失去资格。不保证固定排名、TopK、固定追平秒数或任意负载下有限控制等待。
当前caller/target授权、workspace、range/excluded、可见ID及fork copy identity排除、损坏拒绝仍有效。
canonical正文是唯一真实来源，不以旧索引正文作增量真实性来源。

保留每题一组最新成功投影。候选持自身entry cut；新组成功后不必保留旧组物理行。
不新增任务表、coverage header、event、receipt、lease、generation、hash registry或持久扫描游标。
现有搜索文档增加covered_entry_sequence及expected_variants；Q1/Q2改用现有(session,id)唯一键的组合FK，
只强化同session归属，不替代ROOT、原始后继及正文资格验证。clean-v0一次硬切，无在线兼容或双读写。

## 数据库所有权与锁顺序（冻结）

PostgreSQL拥有事务、MVCC、advisory/row锁、FK及BM25。Pulsara复用lock_canonical_identities，
增加一个小型具名session-control准入helper；key按全局唯一session_id，固定namespace/scope，
不由caller传入domain选择另一把锁。此锁不授予权限，授权仍由原guard/parent谓词负责。

- publisher：昂贵RR构建完成并关闭读取连接后，独立RC事务取得session-control独占事务advisory锁S，
  再取得session KEY SHARE，核domain/Q1/cut及已发布覆盖，原子替换整组，提交释放全部锁。
- 接管、删除、归档/解归档、fork source和scheduled强控制：首次session父行锁之前先取得同一个S，
  后接原FOR UPDATE及workspace/fact/relation顺序。fork同事务重复S可重入。
- 普通writer：_require_writer(lock=True)用FOR NO KEY UPDATE，保留全部generation/owner/lifecycle/expiry谓词，
  不参加S。scheduled若用writer scope，必须在首次NKU之前取得S；禁止先持parent再申请S。
- 移除问题级额外advisory锁；S已串行同session全部发布并提供同题防倒退所需互斥。跨session仍可并行。
- 强控制已排队S后，后来的publisher等待S时不持父行锁，消除实验中的父KEY SHARE接力。
  不把局部准入顺序当无死锁/无饥饿的全局证明。普通旧generation在authority尚未转移时仍可能合法先写；
  接管提交后旧guard必须失败，删除提交后不得复活。

原canonical writer/fork中的同步projection、savepoint refresh和先全表DELETE rebuild在同改删除。
禁止留下writer(parent→S)与publisher(S→parent)反向路径；不同时启用旧同步与新后台维护。

## 固定cut、失败与发布

每题一次RR中捕获真实session entry head C，所有Q1、同owner A、原始第一条后继Q2读取受C约束。
executed/imported owner和entry cut覆盖fork(entry>0,event=0)。不按event head证明正文覆盖。
两种tools模式各构建一次，从完整QA派生QAQ，不再四遍重读同题。坏Q1/A使整组构建失败并保旧；
坏或scheduled Q2只使QAQ不适用，保QA。合法零文档与构建失败用typed进程内结果区分。
SQL/IO错误不得伪装合法零结果。session已不存在可返回合法不存在结果，不能伪造domain。

即使canonical已到120也允许发布100。S后重读已发布覆盖，拒绝100覆盖已成功110；同cut可修复缺行。
整组适用变体及合法缺席一次DELETE/INSERT/COMMIT，失败回滚保旧。旧合法零组不能删除较新正组。
expected variants识别应有分支缺行；max(cut)不是完整性证明。全组零行unknown，每遍扫描保守重评一次，
不自排无限循环；负覆盖表须另有实测必要性才讨论。

## Core级提示、维护与补齐

Core拥有独立维护器及一个既有KernelSessionIO物理lane，首版concurrency=1是进程内物理调度宽度，
不是逻辑任务/历史总量限制。底层S仍负责跨进程串行，不能用pending map冒充数据库互斥。

已提交ENTRY提示在过滤live Host route之前投给维护器；callback只合并小ID，不做数据库/正文读取。
后台解析真实owner：普通A/steer/tool result只触达所属问题；新ROOT USER_MESSAGE额外触达直接前题。
不跳过scheduled或坏的原始后继Q2。fork成功物理结算后提示补齐，即使调用方取消也先确认已提交child。

pending按canonical question合并；首due不因持续追加后移，running不取消；运行中新增hint保留并排尾。
C100完成后可先发布，再追C120，不要求正文停止。失败有间隔重试、不在队首自旋。
初始合并窗口250ms、失败重试1s、每遍扫描间隔30s只是首版进程内维护节奏，可由后续实测调整，
不构成固定追平SLA、操作/会话寿命上限或持久恢复要求。

启动立即使用已有索引，不等待全库扫描。后台用既有history page默认宽度keyset分页，只取身份、
canonical依赖序号和projection覆盖元数据，不取正文/blob。每题比较本owner尾部及原始raw Q2边界，
不逐题追session head。最多持一页扫描候选，与live pending公平交替；该页消费后再推进，
不先将整个corpus灌入pending。周期再次从头核对，修复丢提示/其它进程写入/游标后的历史插入。
fork hint可加速下一遍扫描但不反复重置当前游标；零文档每遍至多一次。无持久游标/领取状态。

## 两次canonical回读

真实HistoryCandidate持私有covered cut、variant及include_tools；provider projection字段白名单不变。
首次BM25候选、snippet positions及首次canonical回读共用一个短RR；不跨远端rerank等待保留连接。
rerank后新RR按候选自身cut重新构建并精确比body/messages/positions/identity/Q2，复核当前准入。
不读取最新索引cut替换候选cut，不要求旧物理投影行仍存在。无效候选不额外补位突破原候选预算。
显式session-content reader沿原RR与授权入口，自动检索另外保workspace和copy folding合同。

## 关闭与验收

Core冻结新open/fork并物理结算已接受写入/Host工作后，停止维护领取并唤醒loop，丢弃未领取提示，
不强制清空逻辑积压。当前同一真实IO完成commit/rollback并物理join，关闭维护lane，才释放provider/PG。
迟到callback不能复活关闭owner；单Host关闭不关闭用户级维护器。重复取消不使to_thread物理消失。

必须验收：cut/variants/失败保旧，100/110乱序、samecut、零组、两次RR与当前准入；新ROOT/工具/fork提示；
无Host route与丢提示周期补齐；公平推进、页内有界、退出物理join；同/异题跨进程publish等待S不持父锁；
真实接管/删除/scheduled/fork和发布交错、rollback释放、不同session并行；持续负载下控制等待及旧guard拒绝。
用新的隔离clean-v0库，不修改用户保存配置或原库；不以有限样本承诺任意负载无饥饿，不减少总历史。
