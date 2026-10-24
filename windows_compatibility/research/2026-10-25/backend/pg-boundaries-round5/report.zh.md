# G0-W-P 第五轮：协议与其余物理故障边界

受测基线 `b741dd92b06a2d167d882e312e18f503e23b8a89`，Windows/Linux clone一致。**本轮29个物理case通过既有结果的离线断言，PG候选的独立协议、生命周期、数据功能边界已取得正负实证；不能据此宣布整个Windows G0或生产激活通过。** 生产源码、规范、依赖、settings未改，Phase B未进入。

用户按物理Esc要求停止后，本agent没有启动新实验、OAuth或浏览器。所有本轮case在停止前已结束；29个精确home及临时搬移资源副本均已清理。当前进度与余项见[checkpoint](checkpoint.zh.md)及[P逐项清单](p-checklist.zh.md)。

## 实际边界与最小语法

沿用第四轮从当前真实`postgres.ts`机械生成的两行typed guard，实际编译后使用真实`BundledPostgres`和`PostgresOperations`；没有复制另一个lifecycle owner或第二startedHere。Windows固定Node22.23.3、绝对wsl.exe、显式Ubuntu/plumwsl/--cd/--exec、三pipe，helper使用Linux项目.venv Python3.12.15及真实随包PG17.9。固定私有home为`/home/plumwsl/.g0p5-*-中文 空`，UID1000、0700、ext4 `/dev/sdd` device2096，socket遵守原104字节边界。

每次物理调用重新取当前所需锁。start仅把独立PG EX传给常驻启动链；有限status/stop继承kernel，initdb继承两锁；remove重新取得两锁、验证准确私有data/uid/version和当前status3，持锁至删除结束。锁文件在PGDATA外，不unlink、不LOCK_UN。typed not-submitted/unknown立即原样抛错；已结算真实非零继续原readback，已有startedHere不被新错误清空。

本轮有限helper请求语法明确收窄为：**ready之后，恰一完整LF结束的operation JSONL，加stdinEOF，才接纳执行。** 旧single-readline虽不会执行第二条，却会忽略尾随重复/partial内容；若要在副作用前拒绝整份非法有限请求，这个EOF边界最小且不需要后台reader、队列或receipt。它仅适用于一次有限调用，不混入长生命周期desktop_runtime协议。Node在唯一frame后end(stdin)，所以正常操作及发帧后EOF保持可执行。

回复使用平台fatal UTF-8 TextDecoder与JSON.parse，验证唯一ready/result、方法、顺序、必要字段和消费值类型；完整result及正常carrier结束之前不采用成功。result之后再来frame、残片、非法UTF8、畸形JSON或错误schema都拒绝。发帧前非法reply关闭准入并等helper结束；发帧后非法reply为unknown，继续排空/等待物理调用，不重发。有限stdout/stderr沿既有PostgresOperations execFile的每流4MiB maxBuffer理由限制记录，不增加总任务/寿命cap。本轮没有另跑超限字节压力场景。

## 本轮证据

| 组 | 数量 | 核心实际结果 |
| --- | ---: | --- |
| functional | 1 | 中文空格路径、逐字节写UTF8、真实start/reuse、错实例/版本拒绝、ext4 clean-v0/deep verify/BM25/snippet/vector、PG资源搬移后重开检索、精确reset |
| framing | 9 | 畸形ready；重复ready；畸形/重复/partial/非法UTF8 result；重复/畸形/partial请求 |
| framing supplement | 2 | ready前非法提前帧执行前拒绝；有效JSON但错误result value schema在发帧后报unknown |
| mutators | 13 | initdb/stop/remove操作前EOF；initdb发帧后EOF；initdb/stop helper死亡；stop丢result；remove前死亡/删后丢result；方法间新start；真实readiness非零；真实启动失败；kernel锁隔离 |
| exported client smoke | 1 | 单一client抽出后复验kernel-lock路径 |
| UTF8 wire proof | 1 | 实际chunk以UTF8前导字节结束，由后chunk补齐，完整中文directory/socket逐值相等 |
| partial deletion | 1 | 真实第一次unlink完成后、下一次unlink前杀helper，部分目录与unknown事实保留，无重放 |
| known readonly error | 1 | 完整已捕获ENOENT按settled传播，不误标transport unknown、不初始化 |

以上29项及清理均由`verify_results.py`检查保留结果，见`verified-results.json`。没有未预期harness失败；协议拒绝和真实PG非零为有意负控，其原输出全部保留。没有弱化断言、skip/xfail或覆盖旧结果。

### ext4 数据功能与搬移

旧首轮backend检索用`/tmp/g0p-*`，本机该处实为tmpfs，不能冒称ext4数据资格。因此本轮只补一次完整ext4功能case。`queries.py`调用生产`prepare_database(initialize=True/False)`与deep `database_diagnostic`，实际status=verified；版本PG17.9、vector0.8.1、pg_search0.26.1，角色最小权限、Unix socket、空listen_addresses均核实。

生产lexical predicate/parameters/tokenizers查询`数据库`、`running`、`prepare_database`得到预期文档和原文位置。除脚本断言外，离线验证按原文UTF8字节逐一检查所有span的范围与字符边界，并检查对应原文跨度包含原query。vector同向量距离为0。没有伪造索引、snippet或schema结果。

随后真实stop，将同Linux已有PG资源复制到本轮`moved PG`目录（345MiB，路径带空格），从该路径的真实postgres重新启动同一data。`/proc/exe`实际指向该搬移路径；deep verifier、三类BM25/snippet、vector再次通过。最后原TS owner的running→stop→remove实际删除PGDATA，保留PG锁inode、socket与外部sentinel。

同case还核实：错误socket的reset拒绝且原实例查询不变；pidfile raw目录改为`/.`时start按resolved事实复用、reset按raw严格拒绝；停库后PG_VERSION=16拒绝启动且data sentinel保留，恢复本次fixture版本后才重开。未删除不明目录。

Python/rg/Chromium完整资源smoke、版本与整体搬移复用首轮root的`desktop:verify`归档；本轮`provenance.json`另记录项目/随包Python模块实际来源和PG版本。没有重复构建所有资源。

### framing 与准入

畸形ready使operation-written=0；重复ready使operation最多写一次，第二个ready令整个调用unknown，不发送第二次。post-operation畸形JSON、重复result、partial result、非法UTF8及错误value类型全部unknown，实际PG运行而本次startedHere=false。没有把第一条看似成功的result提前采用。

重复/畸形/partial请求和ready前提前帧，由当前helper在accepted/PG子命令之前拒绝，完整回复明确not-submitted；PG保持status3。这里not-submitted来自实际helper的执行前拒绝，不从最终status倒推。UTF8正控另观测到真实chunk末字节为多字节前导字节，随后完整decode和准确实例字段恢复，不仅依赖“有多个chunk”推断。

### mutator、锁和unknown

initdb/stop/remove在ready后操作前EOF均不执行相应mutator。initdb已提交后EOF仍真实完成并由owner正常起库。initdb真实bootstrap及stop真实pg_ctl停点杀helper时，有限后代仍占kernel/PG相应锁；恢复准确后代才结算，未以helper/carrier结束替代物理边界。

stop已停库但result丢失时，reset抛unknown、保持data/sentinel，不继续remove。remove取两锁但尚未delete时杀helper，data完整保留；remove已经删除后丢result时，data真实不存在、status4，但不继续initdb或重放reset。已有startedHere=true因缺成功确认而保留，不伪造为false。

另以Python标准audit hook在真实第一个PGDATA unlink成功后、第二次unlink之前暂停，再杀helper。诊断记录证明第一个文件已不存在、PGDATA仍为部分目录，两锁随helper死亡释放；原owner抛unknown、保留已有startedHere，没有自动重删/初始化。**unknown保证不继续擅自修改，不保证撤销已发生的删除或自动恢复部分目录。** 这个选择性故障没有替换OS返回值。

方法间竞争正控：原reset已成功stop后，独立同协议helper真实启动同库；remove重新取PG EX时busy，完整not-submitted，data/sentinel和新运行实例保留。旧stop结果不授权删除，也未自动第二次stop该新实例。

kernel隔离使用生产`acquire_kernel_lock`持锁owner：PG已运行时仍能取得原kernel锁，证明常驻PG不占原锁；该锁存活期间maintenance以PG_KERNEL_BUSY/not-submitted拒绝，PG保持可查询，持锁者实际退出后才允许维护。这不是完整随包kernel/UI关闭链的替代证据。

### 已结算真实非零

真实pg_ctl start使用诊断故障输入`-t 0`，产生真实code1及`server did not start in time`。只在该pg_ctl的exit(1)前暂停，等实际PG可查询后再恢复。完整错误code1/原stdout/stderr返回，helper与原owner随后真实readback准确实例，研究candidate保留startedHere=true及原code1，没有把它改成unknown。这个0秒是研究故障输入，不是产品任务时限。

另一项真实无效PG配置使start code1、当前status3，原owner保持startedHere=false且原错误保留。两项补上第四轮只有源码与原回归支持的真实非零路径。

## 研究修正、复用与范围

收尾发现旧probe把完整捕获的只读filesystem OSError也保守标为unknown。最终helper对这种已完成的filesystem失败返回settled；独立readonly-missing真实WSL调用确认原ENOENT保留、无unknown标记、没有初始化。此前组的原始错误帧不改写；实际PG失联和非零的分类没有改变。不宣称最终helper逐字重跑了所有早期组。

为root另派的真实kernel组合研究，协议实现机械抽成单一`client.cjs`：`createClient(home, onEvent).operations`可直接交给真实BundledPostgres。require没有PG/CLI副作用，默认无fault；本轮owner.cjs也只复用该实现。抽出后kernel-lock smoke通过；`owner.pre-extract.cjs`保留先前实际runner。它仍是output研究client，不是生产平台API。

原owner及两行候选的8项PG回归引用第四轮现有结果；本轮没有生产owner变更，实际新非零和协议case另有上述真实证据。未运行provider、不读saved settings/凭据、不sudo、不关闭interop或共享Ubuntu。

## 清理与P资格

`cleanup-audit.json`已核全部29个**本轮精确home**不存在、当前用户可观察范围无对应argv/私有环境标记进程。没有泛扫并回收其他agent的`.g0p5-kernel6-*`。`resource-cleanup.json`在确认无可观察进程执行搬移副本后，删除已核实本轮output内的345MiB副本。Windows预持carrier HANDLE最终均signaled。PermissionError会跳过，因此不声称跨权限全系统无残留。

独立PG协议/owner/数据功能条目可按[P清单](p-checklist.zh.md)局部验收。**整P仍需root把当前实际随包kernel/main的退出、旧kernel等待、PG stop顺序及宿主失联证据合入；本agent没有以持锁小进程代替那条完整链。** 字节超限压力、每个故障的所有交叉组合、真实Electron Quit/安装器及其他G0门不由本轮29项自动证明。生产WSL helper/typed接口仍未实现，Phase B与Windows支持未激活。native Ubuntu资格按本轮用户要求延期，不拿WSL结果替代。

归档清单只选小型源码、结构化结果、必要来源/清理；不收数据库、资源副本、compiled/node_modules/venv。研究调用的有限诊断观察超时不是产品生命周期cap。停止checkpoint之后没有开始OAuth或浏览器授权。
