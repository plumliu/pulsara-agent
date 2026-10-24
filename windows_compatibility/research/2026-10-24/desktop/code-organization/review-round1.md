# 代码组织修订全文交叉审阅（Desktop Round 1）

日期：2026-10-24。对象：根`PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`本轮**525行最终候选**。已通读第1–15节，重点复核新增§3.1–3.5、§11.1与既有产品合同的关系；在先通读523行候选后，另读最终新增S6 cache输入条款及其上下文。没有修改根规格/生产代码，没有重跑测试、G0或真实provider；本轮文件只在本目录。

## 明确结论

**可冻结，无剩余阻断。** 此结论覆盖本次代码组织与平台适配修订，认可既定原生Windows条件实施路线、具体模块落点和依赖顺序。它不表示G0通过、上述生产文件已创建、Windows支持已实现或激活。root仅更新状态/审阅记录可直接完成本轮冻结；若继续改变执行、安全、认证或更新合同，应再审受影响部分。

## 具体审阅结论

| 审阅面 | 最终文本所给约束 | 判断 |
| --- | --- | --- |
| common / platform依赖方向 | §3.1/3.2把产品owner、领域内平台leaf与少量共享OS机制分开；允许合并无独立职责的薄文件，禁止统一platform runtime、VFS、通用RPC/registry、另一套Windows应用 | 具体且有边界；不是目录清单替代所有权。 |
| Electron lifecycle | `main.ts`唯一boot/reset/requestQuit；`runtime.ts`唯一child/JSONL/ready/close；`postgres.ts`唯一实例状态、认证/lifecycle；POSIX/Windows leaf只给参数和物理证明 | 不复制Windows boot/quit，不把helper抬成PG owner，不让frontend接管后端业务。 |
| resource locator | §3.3 Node resource-paths与Python runtime_resources只返回真实随包路径；同一个Python path供应resolver/runtime/helper；home/settings/认证不进入locator；rg/GUI launch沿既有唯一owner | 没有两语言可编辑manifestregistry或新资源authority；同一recipe布局靠重定位smoke验证，符合实际打包边界。 |
| Node/Python安全文件接缝 | §3.4先验证维护中native binding；仅实证缺口才用有限短命令helper。helper不拥有PG启动、reset授权、角色/轮换；目标限定home、真实handle持有、UTF8 secret传输与cleanup/结果不明须G0验证 | 没有凭空造daemon/FS服务，也没有把Node chmod/readFile当ACL等价证明。外部initdb/删除的对象绑定不能因helper返回路径就假定安全，已明确闭合。 |
| 文件typed binding全caller | §3.4禁止把HANDLE整数当fd，列明DirectoryBinding/FileBinding、read-at独立位置、borrow/duplicate锁持有、精确cleanup；包括间接source_import、preview_http和held DTO | 覆盖前后端预览/导入/设置/包的真实调用链，避免只移核心函数留下raw fd消费。无裸fileno逃逸口不影响leaf内部复用标准库/绑定。 |
| 进程scope与guard | 共用ProcessScope仅物理创建/范围/终止/释放；terminal同步与MCP/hooks异步I/O不被强制揉成同一Popen；credential_guard覆盖真正spawn而非lazy计划；业务slots/output/monitor仍共用owner | 窄机制与业务线性化分离正确。ConPTY source close与持续drain的先后允许平台所需配合，最终完成条件仍不变。 |
| 三条IPC | Protocol-v3 transport单独在terminal_protocol，desktop_runtime_io仅JSONL/诊断/lock；帧/auth/attachment业务不迁入transport；Node/Python端和CLI/server一起改 | 保留已冻结的loopback capability、named-pipe local SID DACL、UTF8匿名pipe合同，无新RPC层。 |
| frontend与IPC | §3.2/9保持preload sender验证、HTTP业务、preview token/root、用户级页面，平台操作收口宿主/后端；只改显示和受限调用，不暴露任意FS/Node | 不需要强制新建desktop-contract.ts才能闭合。现有renderer-safe type契约若复用仍须无Node运行依赖；最终文本已有来源门足够约束此点。 |
| imports与选择点 | factory/composition仅按真实OS选择一次，leaf imports隔离，Windows缺能力不得fallback；小value分支可以保留 | 兼顾跨平台import可用性与不过度抽象；不会为每个OS if建wrapper。 |
| G0与hard cut | §3.5/11.1要求consumer/DTO/close成组迁移，POSIX抽取只在同分支的顺序，不发布新旧双轨/no-op Windows stub；S1不宣称应用已能启动 | 保留原三平台有效路径，不借拆分降低权限/结算验收。 |
| build/install/cache | S6保留prepare_resources唯一入口，recipe按需拆；WindowsNSIS宏放tracked scripts而非ignored build；新增leaf/include/CLI source加入既有sources/outputs或构建输入，test_build_cache验证真实leaf失效 | 已关闭模块拆分后热缓存假命中的具体风险；继续由mise/uv/builder拥有新鲜度，无新hash/registry。 |
| 阶段依赖 | S0实证→S1接缝→S2文件/S3进程并行→S4服务/DB→S5用户闭环→S6安装→S7激活；recipes/样式/CI允许在正确切片提前 | 顺序不是新durable流程，stage退出条件有真实可执行证据；不把“UI可开”当Windows完成。 |
| 成本 | 原88–154人日已含接口提取/consumer迁移/安装验收，helper或上游fork超出G0机制时另估；没有把本轮代码组织再计一份重构项目 | 预算与前轮去重一致；未利用模块数量/搜索命中估工期。 |

## 既有冻结合同回归核对

本轮组织文本未削弱下列条款：当前epoch prefix连续性；无新增总寿命cap/durable执行恢复；PS7独立命令与TTY/PIPE差别；Job创建前纳管与root/range/drain/join；安全文件no-follow/no-clobber/真实unknown；PG TCP/SCRAM双端认证与auth失败顺序；内核lifetime锁；OAuth customURI仅focus；NSIS必须替换默认强杀宏；用户级页不依赖active session；WSL仅另行授权的备选；Win11桌面证据与Server runner/macOS绿测区分。

实施仍须按G0结果把具体binding API、有限helper seam和recipe填回规定位置。如果任何一个安全/生命周期机制不能满足合同，原规格要求回到规格决策而非fallback，这仍是冻结后有效的前置门。没有新的修订请求。
