# POSIX-first Phase A：桌面专题全文交叉审阅（第一轮）

日期：2026-10-24。审阅对象：根 `PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md` 当前684行候选全文；交叉材料为 `terminal/posix-first/proposal.md`、`backend/posix-first/proposal.md` 与本专题 `proposal.md`。本轮只读源码/文档，没有修改生产或根规格，没有执行测试、G0、构建、真实provider或数据库重置。

**结论：当前语义候选可冻结，无剩余阻断。** 这是对本轮候选合同的明确批准，不是A实现/验收通过，不是Windows可行性或激活证据；root如再改实施语义，应对最终候选复核。

## 1. A独立交付与旧条款的闭合

- §1、§3.1/3.3/3.5、§4、§11/11.1/11.2及§13一致：A是现有Linux/macOS接口抽取与完整消费者hard cut，可独立交付；Windows全量接入B仍须A验收和G0-D/T/F/R全过。G0可以并行，不是A前置。Windows不可行不否定已验收A，也不授权自动降级WSL/外置DB。
- §3.1最终A+B目录不是A预建清单；当前无消费者的Python资源locator、Windows叶子、PS资源、安全DB helper及NSIS均推迟。§3.3只在实际机制入口拒绝尚未实现的平台，没有把A扩大成“所有入口提前Win import闭合”。
- §3.4的最终平台接口要求受§11.2明确分阶段：A保留真实POSIX mode及当前观察/身份合同；后续确需变更权限接口时按G0做所有caller hard cut。没有为了未来HANDLE放入虚假ACL、Windows元数据或generic fileno逃逸口。
- §7/§9/§10保留Windows目标，§11.2.5明确A不提前改PG endpoint/auth、JSONL、前端路径、安装/发布；§13开头明确该矩阵是B资格，避免将Windows验收反向成为A退出条件。

## 2. 桌面、资源与IPC：最小范围符合现有调用关系

1. **必选Node locator有真实重复消费者。** `main.ts`的home解析、`runtime.ts`的wait-owner/start以及`cli-install.ts`的Python入口都消费随包Python布局。§11.2.5要求同一解析值，且locator只返回路径；`main.ts`仍唯一boot/reset/requestQuit，`runtime.ts`仍唯一JSONL/ready/close owner。没有新增manifest/home resolver/进程或credential authority。
2. **PG机制不为整理目录而强拆。** `postgres.ts`原有单一状态留原处；只有当前真实需求才机械拆POSIX叶子。A的Unix socket、local认证、host拒绝、版本、DDL、reset/停止次序不变。Windows TCP/SCRAM/auth文件/安全helper是B，不能为让dogfood runner方便在A偷偷改TCP。
3. **锁一旦受typed binding影响就必须闭合。** §11.2.2纳入kernel lifetime锁、等待者及borrow/close；§11.2.5条件片不构成逃避该实际消费者的许可。若抽宿主I/O，server/client/cleanup一起迁；仍为同一Unix端点/权限/消息，正常EOF/慢退/失败不能变成提前强杀。
4. **Protocol-v3按实际需要抽。** 选中则gateway/client/application端点持有者一次接齐；frame/auth/launch capability/attachment/revoke与Unix路径不变。不为了未来Windows提前增加loopback或第二bridge。未选中时也没有空transport框架。
5. **纯locator与build recipe互不变成authority。** Python locator无当前消费者就推迟。可选recipe拆分若落A，mise sources/outputs与真实cache失效测试同批修改，不能留到S6；不新增资源registry/hash。改应用wheel必须新鲜，native SDK未改可复用热缓存。
6. **发布边界明确。** A默认不改renderer/preload/browser API；实际触及则跑相应回归。DMG+两个deb集合、workflow_dispatch、手动版本/发布权限均保留；验证构建不publish，不自行bump/tag。

## 3. 与终端、后端提案的跨专题一致性

- §11.2.2保留不同owner各自的metadata比较和同步强度；目录独占发布、settings replace/commit-unknown、artifact hardlink no-clobber三种操作分开，不把接口整洁误当POSIX多名称原子事务。HTTP read-at保持独立位置，借用/显式下载/关闭的既有期限不被统一。
- typed binding迁移覆盖直接调用者、间接source_import/preview HTTP与held对象/PhysicalLifetimeAnchor。每片按共享签名和持有闭包完成，不留 `int | Binding`、raw flags或路径重开兼容路径。
- §11.2.3把Popen创建后到wrapper/reader/watcher/Timer接管前的失败列为具体交接责任；范围、字节端点和产品完成组合仍分开。范围不发布completion、不代替8槽，也不扩大管理能力到任意逸出后代。
- §11.2.4异步MCP/hook/screenshot复用窄范围机制，但保留自己的流任务、deadline、env/credential guard和副作用结果。真正spawn仍在现有guard内；不全局切event loop，不把terminal的终态、槽和生命周期强加给其他caller。
- A保留独立shell/现行probe行为；没有新增PS临时脚本、命令schema变更、SYSTEM/tools重写、durable状态或总寿命cap。A0先确定真实调用闭包，A1/A2/A3可并行，A4按scope接口接入，A5按锁等依赖收口，A6共享矩阵只验一次，顺序可执行。

## 4. 原生验证与runner证据边界

§11.2.7已关闭此前容易产生假绿的几处缺口：

- macOS arm64与native Ubuntu x64/arm64的Python/wheel/资源/搬移是各自证据；容器userspace或本机Mac不能代替Linux原生目标。
- 至少macOS和一套真实Ubuntu图形session做已安装GUI/CLI全过程；另一Ubuntu架构至少native安装/CLI/资源，触及其专属安装/native行为或有待解问题时增加该架构GUI。须明确未测层级，不能把资源smoke或Electron mock当已安装GUI。
- 三目标资源搬移使用净化环境，验证Python/rg/PG BM25/vector/Chromium，且应用wheel新鲜、checkout外安装。真实GUI再验证图标、workspace/附件/preview/用户设置、正常/慢退/失败、同home重开、CLI冲突和tray恢复，证据层彼此不替代。
- 改前/改后不同checkout使用各自uv环境或对应独立wheel；记录import/CLI/resource实际来源，防止editable环境把baseline导向candidate。
- 真实provider与长程明确是核心A必需证据，Mac和Linux分别覆盖受改边界；少量echo不够。仅精确去除实际secret，保留prompt/response/tool trace和物理观察，不新增源码/证据SHA。
- runner先preflight当前API、保存连接和精确本机DB。现有DB helper的loopback/root dbname限制不等于managed Unix配置可直接跑；只修测试harness或报缺项，不能改生产endpoint/settings满足runner。
- artifact runner的`--phase baseline`会改变生产tool surface，规格已正确要求两端均`--phase candidate`仅目录分前后；terminal phase只是label。硬编码连接/output的旧runner要预检并归档baseline。
- 缺native Linux/GUI/provider证据只能“实现完成、验证待补”，不是A通过；不加skip/xfail、削弱断言或因为时间到了推断通过。

## 5. 成本与批准边界

§11.2.8按原W1–W5/W7归属A，不把三专题相互覆盖的估计叠到88–154人日总盘。A0根据真实闭包/native环境拆出A与B剩余劳动，避免在接口尚未按实际caller封闭前给伪精确数字；未知Windows Job/ConPTY/NTFS/pg_search成本仍留B。分段交付确实增加的安装/review劳动单列覆盖差异，已有同一证据不重复计费。与本专题7–14人日建议是归属关系，不是另加总额。

本轮没有需要修改后才能冻结的条款。最终冻结时只需root补全本轮审阅/状态记录；若其他审阅引起语义变更，再对唯一最终候选复核。此前冻结记录不会自动批准新条款，当前A也不会自动批准Windows。

## 6. 695行最终文件确认

已复核最终文件文首状态与§15新增审阅/冻结记录。变更仅记录本轮三位审阅者与root的明确批准，未新增A估算数字或改变前述684行候选的实施语义；保留“Phase A未实施、G0未执行、Windows未实现/激活”事实边界。**同意695行最终文件冻结，无剩余阻断。** 本次确认未重跑研究、测试、构建或G0。
