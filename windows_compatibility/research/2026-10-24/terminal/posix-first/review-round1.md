# POSIX-first 根规格全文交叉审阅 Round 1

审阅对象：`PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`，本轮约684行候选。已分段通读全文，并完整交叉核对backend/posix-first/proposal.md与desktop/posix-first/proposal.md；只读复核了现有Ubuntu native workflow、相关测试文件存在性及terminal runner的phase输入。未修改root/production，未跑测试、provider、数据库或G0。

## 结论

**本候选可冻结，无剩余阻断。** 同意用户本轮授权的顺序修订：Phase A为独立可交付的真实Linux/macOS接口抽取与回归，不以Windows G0全部通过为前置；全面Windows Phase B仍须A验收及G0-D/T/F/R全过。旧规格的G0限制已在相关章节同步改写，未发现相互否决的残留。

这是对当前候选的全文批准；root闭合其他专题并形成最终候选后，仍按本轮约定复核变化，不以此次批准自动覆盖后续语义修改。A未实施、G0未执行、Windows未实现/未激活的事实必须继续明确。

## 关键章节一致性

| 章节 | 复核结果 |
|---|---|
| 1 / 3.1 | 明确A可先做且独立交付，G0可并行；最终目录树标明A+B而非A预建清单，Windowsleaf/stub/PS/helper与unusedPythonlocator延后。没有目录空框架或假支持声明 |
| 3.3–3.5 | A只选择当前可用POSIX，unsupported发生在真正需要机制处；B完整Windows import才是对应门。每个已抽取领域的全部caller/held/cleanup同片hardcut，未触及领域由原owner继续拥有，不被静态扫描全绿驱动无关重写 |
| 4 | G0范围仍为Windows原机，A绿测、mock和userspace容器不能替代；G0不再限制A。B失败不可silentfallback或放宽产品语义 |
| 11 / 11.1 | A先验收、W0可并行、B以A唯一路径为基线。G0后可调私有接口并同批更新POSIXcaller，无V1/V2/aliases；未将A接口当稳定外部SDK或WindowsAPI能力证明 |
| 11.2 | 必选binding/terminal/shell/asynccaller/Node locator清楚；条件IPC/build拆分只按真实调用闭包加入，不能借可选绕过被触及资源责任；A0–A6每片真实可用且旧路径退出 |
| 13 / 15 | A独立退出与BWindows激活分开，已完成的A不因G0失败而失效；前两轮批准仍是历史，本轮需新审阅。没有把本次文档工作写成已跑验收 |

## 终端、scope、shell与异步消费者

1. ProcessRegistry/_ProcessState仍唯一拥有decision/reservation/ID/scope、业务状态、reader/watcher/Timer安装、组合join/capacity/completion。physical与scope只有root/range/byte/OS资源事实，不复制output/monitor，不靠rootpoll或SDKcallback提前完成。
2. 五段launch交接覆盖OS创建前、已创建未完整返回、返回未公开、公开后、组合物理完成；明确dup/fdopen/stream包装失败须回收或把实际physical对象交回原隔离，不能ordinaryraise后释放reservation丢活进程。必要修当前交接泄漏受到同一边界限制，没有引入orphanregistry/repairgraph。
3. A保留POSIX实际byte/drain等待顺序，但不把此顺序锁给未来ConPTY。EOT只作POSIXterminal行为，原close_stdin没有新增结果枚举；B半关闭仍过G0，未以接口名字冒充能力。
4. root退出/range存活/stop request/close资源分层准确；`pid==pgid`、selectablefd、start_new_session与TERM/KILL是POSIX实现事实，未变成全平台合同。没有任意树扫描、adopt(pid)、全局scope map或统一subprocess kwargs框架。
5. terminal/probe同步与MCP/hooks/screenshot异步保留各自流/调度；guard覆盖真实不可逆spawn及取消后取回同一attempt，禁止lazyplan出guard才启动。非terminal保留自己的deadline/frame/env/SDK/结果与完成policy，不强套terminalgroup-empty或8槽。
6. shell只提取现有候选/`-c`/loginprobe，不给POSIXcommand预造PS载荷文件；environment继续defaultdeny/singleflight/TTL/cacheadoption。probe保持owner锁下spawn/精确attemptpublication与closejoin，不变成无法取消的bytes黑盒。

上述均与本专题proposal一致，且没有把Windows驱动接口提前冻结。

## 与后端/桌面提案的交叉核对

- 文件metadata保留完整真实POSIX观察，而owner各用自己的原比较谓词；没有统一全字段相等或新hash。read-at、dup共享offset、borrow取消join、OFD锁、close不明不重试可复用fd，以及三类发布与commit-unknown都已明确，避免只包fd不迁生命周期。
- settings/Skill/plugin/hooks来源/preview HTTP/filetools及间接held对象成组迁移，没有`int|Binding`或fileno逃逸口；新增方法须有真实caller，不复制os/VFS。
- kernel锁若被typedbinding触及必须同片迁移，wait-owner不初始化settings/PG/app；现有UnixPG/diagnostics/V3 endpoint、auth与有限消息不变。Node/Python WindowsDB安全helper仍纯B，未混入A。
- Nodelocator覆盖实际main/runtime/CLI多入口，原Python/PG/Chromium布局、CLI marker/PATH/homeauthority不变；资源recipe可选拆分须立即追踪mise输入并测真实cache失效，而非等B再补。
- frontend默认不改但仍对真实受影响边界验证；A没有NSIS/WinCLI/win32UI、版本bump/tag/publish授权。source交付和release资格分开。

## 验收可实施性与证据边界

现有`ubuntu-tests.yml`确实有Ubuntu24.04 x64/arm64 native runner、完整Python suite、PG17扩展fixture和checkout外隔离wheel安装，可作为A现有证据入口；它不提供真实Electron GUI/保存provider配置，spec也未作这种错误等价。

macOS与两套nativeUbuntu的代码/资源要求、至少一套真实Ubuntu图形session的installedGUI要求及另一架构的条件升级规则已清楚。缺runner/GUI/保存连接时标“待验收”，不以macOS成功或mock替代。这些门有环境成本，但可通过具备相应环境后实施，不构成自相矛盾或要求新产品框架。

改前改后同锁/同生产tool surface，独立checkout不能共用指向candidate的editable环境，记录包实际来源，防止假baseline。真实provider与长程在macOS/Linux分别覆盖受改边界是合理的A核心退出条件；不用随机模型文本逐字相等，而看真实wire/tool/物理结果。artifact runner生产tool面的candidate模式、旧runner预检/测试harness修复及本地DB核验规则均避免为了runner改生产合同。

现有terminal物理/输出/monitor/环境/Host/termination测试覆盖本次最重要事实；新补的是partialconstructionhandoff的真实资源回收，不是转发方法镜像测试。MCP blockedstdin/TERMignore、hookcredentialrotation与screenshotcleanup回归按原caller语义执行。测试/GUI/provider三层不互相冒充，raw目录scan也不当正确性证明。

预算仍从原W1–W5/W7分配，Windows未知留B，额外跨阶段验证如实单列，未重复把专题人日叠加到88–154。A0据真实caller与环境拆账合理；分阶段降低定位风险，不虚构总工期减少。

## 批准语

**同意本轮POSIX-first候选按“Phase A独立实施与验收、G0不阻断A、Phase B仍G0全过前置”冻结；无剩余阻断。** 本轮只批准规格，没有开始A/G0或证明任何平台新增能力。等待root最终候选变化后复核。

## 695行最终文本确认

2026-10-24已复核最终文件文首与第15节：相对已批准684行候选，仅更新冻结状态、追加本轮三专题全文审阅表与事实记录；没有新增实施语义或预算数字。事实边界仍清楚为Phase A未实施/未验收、G0未执行、Windows未实现/未激活。**同意695行最终文本冻结，无剩余阻断。** 此次复核没有运行测试、G0、provider或数据库操作。
