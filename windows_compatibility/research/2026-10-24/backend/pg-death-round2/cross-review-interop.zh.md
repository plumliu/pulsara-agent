# 私有 interop byte relay 只读交叉审查

审查对象为 `output/windows-g0/interop_review/round2/relay_probe.py` 中的实际 guest `RELAY` 常量、`socket_probe.py`、`pinned_socket_probe.py`、`relay-ca30a7c3-summary.json`及对应A/B原始jsonl；没有名为`byte_relay.py`的文件。另核该目录sources中的实际WSL `util.cpp`、`config.cpp`、`binfmt.cpp`、`SocketChannel.h`、`socketshared.h`。只读代码/已有记录，没有重跑、修改候选、进入B或修改生产。

结论：**没有发现推翻本次已测边界的反例。** 私有socket保留、upstream O_PATH固定引用、A取消/B继续，均有具体代码及真实记录。它仍是有限实验候选，不能推成一般性物理完成/正常join/无未知结果保证。下面只列当前证据支持的边界与缺项。

## 已有证据支持的范围

1. `util.cpp:471`起的实际WSL选择逻辑仅在WSL_INTEROP未设置，或`access(path,F_OK)`失败且errno为ENOENT时，查找父链socket；选定后`UtilConnectUnix`连接失败不再尝试父链。因此“仍存在但无listener”的私有pathname，在该源码及本机版本上与“pathname被删除”有不同安全后果。
2. ca30a7c3中，relayA在anchorA死亡后仍有私有socket inode；后续尝试无PE记录、runner记录exit1，WSL报`UtilAcceptVsock ... 110`。relayB被SIGKILL后pathname仍为socket；后续尝试exit1，报`UtilConnectUnix ... 111`，同样无PE记录。它们证明本次两个死亡边界未回退到调用runner自身的其他anchor；没有依靠关闭全局interop或终止发行版。
3. `relay_probe.py:17-18`只open一次upstream O_PATH并保留fd，连接使用`/proc/<当前relayPID>/fd/<该fd>`。本RELAY没有close该fd或把同一变量换成新fd，进程存活期间未见FD复用窗口。`pin-36e45fe3-summary.json`另实证原路径被unlink、nlink0时，pin仍F_OK且指向原inode，连接失败而没有走父链。O_PATH保护的是已open的具体对象，不是旧pathname字符串。
4. WSL2初始创建路径有源码支持opaque bytes：`binfmt.cpp:167`发送创建请求后接受4条vsock（stdin/stdout/stderr/control）；`config.cpp:374`对CreateProcessUtilityVm只是把Message转发给原InteropChannel；`SocketChannel.h:127`、`socketshared.h:21`使用普通stream字节读写。所读这一WSL2路径没有SCM_RIGHTS传递要求，不能凭空指称当前bytes relay丢失了本次必需的ancillary fd。没有推广到其他WSL消息或未来版本。
5. A的PE parent34136提前退出、child22340仍在A Job；B的parent8444/child23864在B Job。终止A Job后A均消失，B仍以原creation identity存活；B两角色随后各有`pe-finished`。取消A不影响这次B普通工作，有直接证据。后续`facts`中的membership字段来自创建时记录，存活/creation则实时查询；不要称其为每次重新查询Job membership。

## 仍未验证/实现的边界

### 首次pin前的目标owner绑定

`relay_probe.py:17`直接open anchor提供的`/run/WSL/<pid>_interop`，只在open后记录inode；没有把首次open的对象与此前独立采集的目标socket/owner身份比较，也没有在ready前后故障门中覆盖“anchor死于首次open前，pathname已被另一个socket替换”的情况。O_PATH能防**open以后**原pathname被unlink/替换而漂到新对象，不能反过来证明**首次open**的对象一定属于预期anchor。

这不是本轮已观察到的误路由，不要求发明hash/registry或把inode当owner认证；只是当前候选不能宣称已完成这一启动绑定竞态验证。0700私有目录保护frontend pathname对其他uid的可达性，也不等于证明upstream属于目标Windows Job。

### pipe断开与已转发但未确认的unknown

ca30a7c3的relayB SIGKILL发生在两条PE链已经`child-ready`之后；对应relay原始日志只有已完成初始交换`counts=[498,0]`。但该次B仅运行6秒，前面的dead-anchor请求约等待10秒，不能据此声称杀relay时B仍live，或B是在relay死亡以后才完成。因此该旧例证明的是**relay死后阻止后续新建**，不是“任意中断点都没有已发生而未观察到的创建”。下述新增25秒实验才补齐既有B仍live的时序证据。

没有半个创建请求、请求完全转发但vsock/结果尚未收齐、结果pipe在真实生效边界断开、背压/短读短写与取消同时到达等故障矩阵。`connected`只记录error或字节数，没有Pulsara typed unknown/不可重放结算；`gather`结束、transport.close、carrier退出都不能被采用为Windows过程完成证据。本probe无需现在扩成生产状态机，但未来不能把error/EOF改报“未启动”。

另一个明确的harness细节：`socket_probe.py::launch`内部记录PE的`rc`却未`sys.exit(rc)`。已有pin死亡summary外层`returncode=0`同时内部`after-pe.code=1`；ca30a7c3也依赖linux runner-exit帧与facts判断失败。当前断言看的是内层实际code，结论没有因此失效，但不得抽取外层carrier exit0作为成功证据。

### 私有socket何时可以unlink

RELAY自身不unlink，故本次SIGKILL后保留tombstone有效；`relay_probe.py:111`由Windows测试finally在已知runners收齐后删除精确私有socket/directory。这是本次fixture的清理范围，未证明生产中的完整退休条件。

若仍有可执行的Linux调用者/待结算启动持有旧WSL_INTEROP字符串，删除pathname就重新产生ENOENT父链回退条件。已有missing-socket实验已经给出这种回退的实际证据。当前probe没有覆盖“清理后晚到旧调用者”、正常退休与accept并发、应用/relay异常退出后如何在保持未知结果时处理tombstone。该缺项不能用“正常finally会unlink”作为解决；也不应由本审查自行添加durable registry或新恢复框架。

### 正常join与Job存活范围

ca30a7c3测了取消A Job及B在仍保留anchor时正常完成。它没有测“Linux普通工作已join，但同Job中仍有无pipe Windows后代”这一正常收尾场景。anchor carrier/wslhost本身也属于Job，故Job是否还Active不能在关闭anchor前直接当成仅有工作后代的事实。

正常完成不能因为Linuxjoin/pipeEOF就TerminateJob并称工作成功；同样不能仅凭PE parent退出或relay连接完成宣布Job全部结算。候选仍需明确：封住relay新准入并保持tombstone、使anchor基础进程正常EOF退出、保留Job handle等真实工作集合完成；本轮尚无该完整顺序的实测。以上是缺少验证，不是宣称现有cancelA错误。

## 审查范围与建议采用措辞

### 时序更正与新增只读核验

已只读核 `relay-4e1c8baf-summary.json` 的 `BBeforeRelayKill`、`BAfterRelayKill` 与 `BFinished`，及其live-B原始jsonl：25秒B负载的父38588（creation134373311167827954）与子21452（creation134373311168968091）在relay428死亡前后均`exists=true`、`sameIdentity=true`；随后两角色在1792857541.9878328/1792857541.9972274记录自然finished。因此**relay自己死亡不自动结束已建立的B Windows工作**这一边界有新的直接证据；它不补齐半请求unknown或正常join矩阵。本段更正前版对ca30a7c3时序的过强表述，旧实验日志没有改写或重跑。

可以保留：在本机所测WSL2及普通PE后代条件下，私有残留socket避免死亡后的ENOENT回退，O_PATH使既定upstream在unlink后仍固定；A Job取消未伤B，真实子链得到对应Job归属。

不应升级为：已认证任意首次socket owner、任意FD/PID复用均安全、pipe断开无unknown、正常join/所有无pipe后代结算已验证、完整G0或PhaseB获批。没有修改受审候选文件或重跑；本报告仅在PG agent自己的round2目录新增。
