# Phase B 最终交叉审阅：main / Runtime / PG / TLS / cover

2026-10-25；只读审阅当前工作树，未运行测试、未修改所审生产文件、未调用provider。范围为当前 Windows 正式接入及受影响的共用退出路径，参照规格§3.3、§9、§10.2–10.3、§13.3、§24。历史G0或fixture通过不作为当前安装版资格。

## 发现：TLS 退休失败的唯一责任引用提前丢失

审阅时 `main.showStartup` 在 `await previousSession.retire(...)` 前执行 `backendSession = undefined`。`BackendSession.retire` 自己正确缓存退休promise、传播原owner/连接/存储清理失败；但是main先移走唯一引用，失败重入的showStartup/后续Quit不再遇到同一失败屏障，存在继续停kernel/PG及exit0的路径。实际触发边界是原 `closeAllConnections`/`clearStorageData` 拒绝，或原窗口资源退休失败；无需假定另一个攻击者或新协议。

严重度P1：未结算的清理责任不能因为第一次退出失败而从下一次退出库存消失。已即时告知root；root随后完成最小修正，本人再次只读核对：先seal并清origin，只有同一retire成功后才清backendSession；失败重入仍等/重抛原promise。boot/Quit错误展示若也碰到退休错误，退到原生errorbox，不递归重试退休或产生未处理rejection。新增用例连续两次Quit均断言不PGstop、不exit0及两次错误，符合具体反例。

已只读核root原始日志 `output/phase-b/root/session-retirement-before.log`：原实现该定向用例1 fail，expected false/actual true；`windows-main-final.log`：修后Windows相关8/8通过。本人未重新执行测试，不把fixture当真实Electron网络清理故障实测。此项所指出的引用丢失在当前代码/该回归范围已闭合，无新增owner/registry。

## 其余已审边界

- 原BundledPostgres仍是唯一initialized/startedHere owner；每次start清initialized，只有本次确认initdb成功才true，Runtime在PG准备后取getter。已经存在但缺角色/库的cluster不能借重试再初始化。typed not-submitted/unknown不走启动失败认领读回；已结算非零保原exact-instance readback。未发现第二归属状态或自动重放。
- main准备阶段并行cover/PG但用allSettled结算两者；Quit取消当前PG准入、等原Runtime preparation/child关闭，再处理starting。unknown PG startup原错误保留，Runtime不clean则不进PG stop/exit0。重置先原kernel stop/wait-old，再原PG reset guard，不从Windows文件事实取得Linux删除权限。
- Runtime按本attempt startSent决定已提交失败；完整LF+fatal UTF8，已提交即使未ready也需正常关闭结果，WSL额外要求stopped。readline EOF残片不会被采纳。child.close不等于Linux范围joined；原guest stopped仍须在application.aclose和kernel锁结算后发送。没有把pipe错误升级为强杀。
- TLS每launch新内存Session，完整公证书精确相等，webRequest另限精确scheme/host/port；不热换旧pin，不借cache清理代替新Session。PDF内置extension只放既有资源例外，普通BrowserOwner使用另一隔离分区。seal在退出/替换前封新请求；下载原对象在native回调外cancel，原done参与退休。以上仅代码结论，真实SDK/安装图形链由root集成验收。
- windowTransition只串行物理窗口变更，不把cover加载塞入同一队列；Quit与replacement交错后boot检查quitting，不继续旧导航。StartupCover保确切WebContents和destroyed promise，show/retire请求身份防晚show，finish在隐藏/最小化/销毁时结算原fade；没有新增任意生命周期timeout。
- 额外只读看tasks的output.wait_for_revision窄修：离开原output condition后调用同一现有retention callback，再返回当前revision，遵循既有manager/output锁次序。其新真实子进程测试确定性阻塞reader callback，复现两个121字节在host160边界下提前可见。未发现这几行新增具体阻断；本人未重跑其129+27测试或冒领结果。

除上项已报root的问题外，本次阅读未再发现可具体证明的P0/P1。此结论不是全库安全证明，不覆盖尚未执行的真实provider、安装版全部GUI或原生macOS/Ubuntu资格。

## §13.3：terminal harness 后剩余真实模型矩阵

当前待运行harness仅包装原terminal runner；`--check-imports`不是provider验收，Linux命令case也不是Windows PE实证。以下按现入口复用，用户配置后再核准独立Unix socket PG/read-only snapshot/固定configure_interop，不另建runtime。

| 仍需的事实 | 可复用当前入口 | 必要限定 |
| --- | --- | --- |
| subagent scope、等待输入/消息、容量与compaction期间process跟进 | `run_round10_subagent_dogfood.py --connection-id ... --scenario all`，或其graph/context/wait-input/untargeted/capacity/enhanced子集 | capacity原场景含MCP/compaction；仍需同一真实interop配置。检查每case实际prompt/trace，不能仅把scenario名字当process跟进证据 |
| 普通文件/JSON/log/snapshot/只读与导出 | `run_artifact_export_dogfood.py --phase candidate --model ... --cases ...` | baseline会删artifact_export，禁止用于本次全tool面验收；文件导入、HTML/image/可见PDF和真实保存/取消仍须Electron原UI/HTTP链 |
| 图片输入/引用再读的真实provider载荷 | `run_kernel_image_input_dogfood.py --model ... --connection-id ... --output ...` | 要支持图像的实际保存连接；原runner已有calls/sdk_payloads/http_requests；不是只看生成文件存在 |
| 图像compaction、cold canonical history与再读 | `run_image_history_recovery_dogfood.py --model ... --scenario all --output ...` | deadline是有限实验观察，不可变成产品寿命cap；仍需实际模型能力与原prefix断言 |
| 正常关闭后的memory检索/真实Decision | `run_direct_memory_dogfood.py --model-id ... --wire-api ...` | 已有关闭并重建core的流程；默认模型不能猜用，需参数适配本次连接，输出路径先核避免覆盖 |
| BM25历史召回、混合Decision与prefix | `run_history_recall_dogfood.py` | 当前硬编码deepseek-flash/chat_completions及历史输出目录；先将测试选择/输出适配本次保存连接，不能为了跑它新配另一套凭据或覆盖旧证据 |
| 用户停止、canonical history、继续后suffix | `run_turn_interruption_history_dogfood.py` | 同样硬编码deepseek-flash及输出目录，需有限harness参数适配。它不能代替Electron异常退出后显式重开 |
| MCP/hook的能力变化与当前epoch不变 | `run_capability_change_observation_dogfood.py` 的mcp/hook子项；subagent capacity中的原MCP fixture | 现入口硬编码模型。stdio跨域Job、HTTP传输及hook正常/取消/失联仍分别核实，不把Linux-only或能力列表可见当Windows后代joined |
| 中文空格工程、Linuxvenv/Python/Git/Node、WindowsPE、图像/HTML导入导出、正常/异常Quit重开 | 已安装Electron→原desktop_runtime→原DesktopApplication/HTTP/session owners | 没有单一现runner覆盖全部。使用现生产界面/HTTP写有限验收步骤；原TS PG初始化/停止与TLS/session退休必须在同一真实链观察，不再复制Runtime |

公共前置缺口：上述多runner仍调用只接受loopback TCP/pulsara的旧测试DB helper，当前只给terminal harness接了Unix socket；不能据其存在宣称其它runner已可直接执行。各runner的module别名/注入点应单独核对，复用同一已核准目标而非开TCP。记录实际SYSTEM/tools/messages与provider回应，精确scrub全部实际credential；artifact baseline、硬编码默认模型、固定输出覆盖和仅model_api_keys过滤都不能原样冒用。这里只列复用路线，未改runner或调用外部服务。
