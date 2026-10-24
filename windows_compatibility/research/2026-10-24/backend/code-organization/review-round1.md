# Windows规格代码组织修订：Backend全文交叉审阅

审阅日期：2026-10-24。对象：根 `PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md` 本轮最终525行候选（已通读原523行全文并复核唯一新增的构建缓存连带段），已完成全文审阅，包括新增§3.1–3.5、§11.1及原G0/认证/终端/前端/安装/WSL/激活条款。

**结论：可冻结，无剩余阻断。**

本结论批准的是后续代码组织和平台适配方案，保留原生Windows条件实施路线；没有将接口抽取、目录存在或本轮静态审阅算作G0/Windows支持通过。本轮未改生产或根规格，未重跑G0、provider或无关tests。

## 1. 文件安全接口与全caller范围已闭合

§3.1、§3.4，尤其162–166行，明确保留 `local_source_binding` / `exclusive_publish` 公共入口，同时把当前int fd契约改为owned `DirectoryBinding/FileBinding`，系统句柄只在平台leaf使用。已正确禁止Windows HANDLE整数冒充CPython CRT fd、共享flags和通用fileno逃逸；不是在旧接口上叠加兼容层。

迁移范围覆盖本轮14个直接import消费者，及额外间接raw-fd消费者 `plugins/source_import.py` / `desktop_app/file_preview_http.py`。Preview、HeldSkillRoot/Child、HeldPluginPackageObservation、PhysicalLifetimeAnchor等held槽与close/borrow责任一起更新，避免只修开目录函数而后续fstat/dir_fd/pread仍失效。read-at保留并发位置独立性、lock duplicate不能按路径reacquire、单个borrow close不得释放其他borrow，都已进入正文。

settings/Skill/plugin/import/preview/file tool的codec、授权、inventory、stale与unknown publication裁决仍归各owner；OS binding不变成storage/VFS authority。私有权限/冻结意图交平台实现，POSIX输出与canonical ID保持。§11.1 S2要求同批full-chain hard cut及真实平台验证，不存在新typed façade配旧fd消费者的交付阶段。

## 2. ProcessScope共用范围恰当

§3.2、§3.4和S3没有将terminal同步Popen+threads强行套成asyncio.Process，也没有让MCP/hooks使用terminal Registry/8槽。共享scope只承载创建前归属、精确root和managed range、terminate/wait/release；业务env、credential、frame bounds、deadline、reconnect、副作用结果和completion仍各自拥有。

重要细节已写清：MCP/hooks最后credential_guard跨真实spawn，lazy plan在guard外执行不允许；Job不全局注册，scope不发工具结果或容量释放；terminal physical对象可封装ConPTY特殊close/drain时序但Registry仍唯一组合终态、output仍唯一记账。与原§5的创建前Job、root/range/drain/join、半关闭未证明即G0失败无冲突。

共享`process_local_settlement.py`仍只是现有cancel-safe task join，没有被升格为进程恢复/全局scheduler。imports/factories集中且按owner lifetime固定平台，Windows依赖失败不fallback POSIX/WSL。不存在运行时backend协商或第二套业务state。

## 3. Node PG安全文件跨语言接缝与认证合同相容

§3.4，168–170行，已正确区分Node `BundledPostgres`语义owner与Python物理原语：先验证maintained Node/native binding，只有G0证实需要且接缝通过时才加入有限的 `desktop_postgres_files.py`。最终只保留选定的一个机制，不运行时native/helper双轨fallback。

helper被限定于同一 `require_pulsara_home`下专用DB目录/ACL/auth/pwfile安全操作，不接受renderer任意路径、不拥有PG startup/retry/rotation/角色DDL/reset授权、不常驻、不增durable状态。实际secret经私有管道返回，Node先纳scrub，错误/unknown不能降级为Node普通fs读写。对于删除，先由BundledPostgres验证并停精确实例，再执行绑定物理删除；与§7.2“data先删、auth后删、不确定保留材料”的顺序一致。

**实现时须按正文170行理解binding lifetime**：临时pwfile/目录若由helper持有，helper的有限operation必须覆盖外部initdb等实际使用窗口，而不是返回路径即退出并清理。可使用该次私有双向管道的有限使用/完成边界，具体wire由G0证明；不得为此新增长期服务、durable receipt或helper自己的PG manager。正文已明确“打开/校验/物理使用之间绑定”“helper返回路径不够”“完整退出与所需binding生命周期”以及G0-R/F/资源smoke，所以这属于已冻结验收要求的落实，**不是当前阻断**。

§7.2仍保留auth文件先原子发布后initdb、已有cluster材料缺失/损坏/mismatch拒绝、不自动ALTER/rotate/trust/reset、角色中断明确失败与现有schema重试边界；Node/Python DSN parser、client require_auth=scram-sha-256、roles/schema三方owner没有被新helper改变。

## 4. 三条IPC与composition保留原owner

`terminal_protocol/transport.py`及leaf只处理Unix或exclusive loopback stream/lifetime；gateway继续protobuf/auth/attachment/dispatch，client/Application同批迁移socket_path假设。既有launch capability与listener close/connect线性化要求未改变，endpoint不进入renderer。

`desktop_runtime_io.py`可以作为当前runtime局部共用接缝收纳JSONL字节I/O、diagnostic端点与kernel lock；这是按现有runtime资源归属的模块合并，而非把用途合为RPC。§3.4/§7.3仍区分三通道消息/authority，diagnostics保持local-only SID pipe/peer owner/first-instance与有限操作，host JSONL保留start/ready/shutdown/EOF原编排和UTF8匿名pipe join。锁复用binding原语，没有另建registry。

`runtime_resources.py` / Node `resource-paths.ts`只读定位资源，不拥有settings/home/credentials/process；同一Node Python路径供resolveHome、旧owner等待、runtime start和helper使用，避免不同路径进入另一套安装。ripgrep/desktop-launch既有入口仍唯一。

## 5. 跨专题与实施顺序

- S0先完成G0物理实证，再建立最小接口；S1的POSIX提取只是相同工作包分支内顺序，不能交付不安全双轨。S2/S3可按owner并行，S4依赖实际安全文件/进程接缝，S5才做完整用户闭环，S6/S7关闭构建/安装与激活；CI可早接，不必等末尾。
- 共用业务owner、平台叶子隔离、只在factory/compose选择，避免每tool判断OS或共享模块导入pty/fcntl/Win32对侧依赖。合法value级path/URL/快捷键分支无需制造对称薄文件，模块数量受真实职责约束。
- PG17.9/vector0.8.1/pg_search0.26.1、PowerShell7.6/ConPTY与stdin载荷、标准PS退出码、UTF8 wire、NSIS默认强杀替换、OAuth URI只focus等原冻结合同均未被新组织节绕过。
- 默认home、canonical路径、NTFS范围、用户页独立session、prefix连续性、最小durability、无fingerprint registry/新cap、真实provider+长程+三平台回归保持。
- 成本仍归原88–154人日及不确定性预算，额外native helper或长期fork若超出G0证明的窄缺口需重估；新组织节未暗示另加免费平台framework。

本轮审阅未发现需要再次修订的产品或组织矛盾。可以把本次代码组织修订状态更新为已冻结；G0未执行、Windows实现/原生验收/发布未完成的状态必须继续保留。

## 最终525行候选确认

已核对§11.1新增426行：拆分resource_build叶子、新NSIS include和CLI native source必须接入原mise/build输入，test_build_cache以真实leaf变更验证失效；不新增cache authority或源码hash。这关闭模块拆分后漏声明输入导致热缓存假命中的风险，与§10原有freshness owner及G0/激活合同一致。最终结论仍为：**可冻结，无剩余阻断**。
