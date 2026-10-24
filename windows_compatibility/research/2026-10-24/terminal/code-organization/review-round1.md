# 代码组织修订：全文交叉审阅 Round 1

审阅对象：根 `PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`，本轮约523行唯一候选，新增3.1–3.5与11.1。已通读全文，按分段重新读取长输出被截断处；本次没有修改根规格/生产代码，没有重跑测试或G0。

## 结论

**可冻结，无剩余阻断。** 新章节把共用逻辑、平台文件、依赖方向、成组消费者和实施顺序写具体，没有改变上轮冻结的平台产品合同，也没有引入平台服务容器、第二套业务状态机或新的持久权威。此批准不意味着G0通过或Windows已经实现/激活。

## 重点审查

| 主题 | 核对结果 |
|---|---|
| 根层ProcessScope | `process_scope.py`与两个私有leaf仅封装创建前纳管、root/range观察、终止/等待/资源释放。terminal、MCP、hooks、截图、probe共享机制，各自持有确切physical execution；没有全局process registry、调度器、重试或统一结果框架。扁平文件规模适合当前复用量，允许合并无独立职责的薄文件，没有为了平台再增加目录框架 |
| terminal唯一状态 | manager保留Registry/_ProcessState、decision/reservation、scope/ID、容量和终态；physical对象替换Popen/stdin/master_fd。output/monitor不复制，physical_completion与completion subscriber唯一发布者仍是manager，与现行代码和5.2一致 |
| reader/watcher/join | 共同完成事实是root exit、range empty、输出drain和所有workers join；没有硬把POSIX内部等待次序固定给ConPTY。持续drain与source release/close配合，SDK自带worker由同一physical对象收尾；不会把SDKexitcallback/只杀根当完成 |
| shell与环境 | shell.py和叶子负责dialect/argv/PS载荷，environment保留defaultdeny、singleflight/TTL、snapshotadoption与失败policy。5.3仍固定PS7标准-Command退出语义、长短command同bootstrap、独立stdin和NoProfile；5.4 EOF仍为G0阻断，未借拆文件偷偷降级 |
| 同步/异步与凭据 | ProcessScope不强迫所有caller套Popen或改eventloop；MCP/hooks保留真实spawn、取消竞态收敛和安全publication都在credential_guard内，明确禁止lazyplan出guard后才启动。env/frame/deadline/reconnect/result仍属原caller，与真实生产代码的guard边界吻合 |
| 平台选择与imports | 领域factory/composition按真实平台只选择一次，具体OS imports/flags留leaf；共享模块不同时加载两个平台。测试注入在owner构造点；没有模型可控backend字符串、动态发现、运行时能力切换或silentfallback |
| 安全文件binding | 明确不能只把fd换成HANDLE整数，DirectoryBinding/FileBinding封闭raw操作；直接与间接caller、held DTO/PhysicalLifetimeAnchor、read-at、dup/borrow、锁和cleanup同批hardcut。read-at并发位置独立性、借用关闭不释放别人、无路径重acquire空窗写明确；这避免了抽取API后仍有消费方绕回os.*的半迁移 |
| Node/Python安全文件接缝 | G0先查维护中的Node binding，只有证据表明需要Python复用才新增短helper；最终唯一机制记录在leaf/recipe，无运行时fallback。BundledPostgres仍是secret/PG/reset authority，helper仅操作绑定文件，不常驻、不成任意路径服务；物理使用期间binding有效性也必须在G0证明，没有把返回路径假当保真handle |
| IPC/DB与资源 | Protocol-v3 transport不混宿主JSONL/diagnostics；listener/auth/attachment仍按7.3，未丢上轮独占socket/lifetime保护。postgres.ts仍唯一状态，Windowsleaf只做参数/实例/安全接缝；资源locator只读、同一解析Python供home/runtime/helper，不新增manifest或可编辑配置truth |
| Electron/frontend | 仍是main/runtime原boot/ready/quit状态机和原权限IPC，OS叶子不反向决定业务成功。用户级页面不依赖active session，路径/preview授权没有因适配前端而变宽；NSIS不强杀与OS强制结束的既有边界不变 |
| 实施顺序 | S0先G0；S1机械提取接口并带caller替换；S2安全binding全链路；S3进程/shell并逐caller替换；S4三IPC/PG/认证成组；S5同一应用；S6搬移/安装；S7整体验收。S2/S3可并行、S4依赖实际接缝、CI可提前，未出现“先界面可用、后补安全”的发布阶段 |
| 预算与授权 | 组织拆分属于原88–154人日预算，没增一套计费/阶段状态；额外nativehelper/长期fork按原重估规则。output仅研究/G0证据，生产不得import；没有执行G0/改生产/发布的虚假完成声明 |

## 与原冻结合同的对照

没有发现新旧条款冲突。原先的native Windows11 x64/NTFS、私有PS7.6 LTS、创建前Job、physical join、单一输出游标、无额外总寿命上限、provider prefix边界、SCRAM/auth失败规则、安全no-follow/发布、SDK复用和无silentfallback仍全部保留。WSL仍只是需另修订的备选，没有通过目录组织变成第二条生产路径。

最重要的实现review条件已经被3.4/3.5/11.1共同覆盖：physical内部partialspawn与依赖worker必须始终有同一owner接管；输入/取消/close结果不能因为包装成新类而丢失；文件借用和跨语言物理使用不能退回路径检查；删除被替代执行分支的同时保留POSIX有效能力。这些是后续代码review重点，不是本候选剩余歧义或新阻断。

## 批准边界

**同意将本轮代码组织修订冻结。** 保持“代码组织/平台合同已冻结，G0未执行，Windows实现和激活未完成”的状态。root可更新审阅状态与记录；若随后更改产品语义、owner边界、G0失败规则或选定未经本节允许的新框架，再复审受影响部分。
