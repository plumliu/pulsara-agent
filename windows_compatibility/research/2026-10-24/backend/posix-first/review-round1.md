# POSIX-first Phase A 第一轮全文交叉审阅

审阅对象：根 `PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md` 的 684 行候选（文首与 §15 仍明确本轮待审阅）。审阅者：backend，gpt-6.1-sol / high，2026-10-24。

**结论：本候选可冻结，无剩余阻断。** 此结论限于本轮实施规格；不声明实现完成、Phase A 验收通过或任何 Windows G0 通过。root 汇总其他专题修改后，仍须复核最终语义候选。

本轮已通读根规格全文，并交叉核对 `terminal/posix-first/proposal.md`、`desktop/posix-first/proposal.md` 和本专题 `proposal.md`；沿用此前已完成的生产调用闭包检查。没有改生产、根规格、保存配置或数据库，没有运行测试或 G0。

## 文件接口与消费者闭包

- §3.4、§11.2.2 和 A1 明确封闭 typed binding hard cut，全部直接/间接 caller、held DTO 和 cleanup 一起迁移；`plugins/source_import.py`、`desktop_app/file_preview_http.py` 的间接持有没有遗漏。清单作为导航而非完整性凭据，实际引用闭包决定分片大小；共享签名不能靠临时 fd unwrap 留下双轨。
- `DirectoryBinding` / `FileBinding` 仅承载已打开对象及物理操作；home、信任、权限、冻结值和结果归原业务 owner。候选方法足以表达当前 directory-relative no-follow、普通文件分类、bounded/text read、write/sync、独占发布和精确清理，不需要泛化 `os` wrapper 或平台服务注册表。
- metadata 明确只是观察，preview、plugin、Skill、settings 和 import 分别维持原谓词；没有把完整 dataclass 相等误当统一产品身份。POSIX mode/owner_uid 为真实观察，未提前以零值/NA 改 canonical bytes。A 不承诺 Windows 可满足该表示；G0 后保留全 caller 一次 hard cut 的空间。
- `read_at` 保持 `pread` 的独立位置，duplicate 不谎称独立 cursor；文本 reader 保留编码、换行、seek 和预算。borrow 在异步/to_thread 实际退出前不能关闭。parent close 与已经取得借用的关系继续受原 owner 的授权/revoke 合同约束。
- `PhysicalLifetimeAnchor` 保留同一 open-file-description，借用 close 不发 `LOCK_UN`；独占临界区不被一律替换成 lifetime anchor。真实 close 只尝试一次、撤销 wrapper 持有、继续释放其余资源，避免因 fd 复用误关新对象。
- settings replace、目录 rename-no-replace、artifact hard-link no-clobber 三种发布分别保留；commit-unknown、published/未发布/unknown 与最终 close 失败的物理事实不被万能 receipt 或重试掩盖。原 owner 的 fsync 错误政策也没有被通用 helper 放宽。
- §11.2.2 明确把 `desktop_runtime.acquire_kernel_lock()`、run/wait-owner/cleanup 纳入 mandatory owned-lock 迁移。§11.2.5 的条件片及末段禁止用“可选”绕开该闭包，因此没有把 kernel lifetime 锁漏到桌面后续阶段。hook 无锁 yield 分支也明确删除。

## 跨专题与阶段边界

- 原“G0 前不得抽接口”已改为 Phase A 明确例外；文首、§3.1/3.3/3.5、§4、§11.1/11.2 和 §13 对 A/B 的关系一致。A 可独立合并交付 POSIX；进入 B 则须 A 验收及 G0-D/T/F/R 全部通过。历史冻结记录没有自动批准新条款。
- ProcessScope 保留实际同步/异步创建与当前范围操作，Registry/工具 owner 持有完成、槽位、结果、deadline 和流任务。非终端 caller 不继承 terminal 的 group-empty 完成政策或 8 槽，MCP/hook 的 guard 覆盖真实 spawn 和取消后同一次创建的取回。
- 五段 launch 交接覆盖 OS 创建后、完整 handle 返回前的失败。先实际回收，否则交回原 owner 既有隔离/收尾路径；不能丢进程后释放 reservation，也没有新增 orphan registry。terminal physical 端点不吞业务状态或假造 PTY 半关闭。
- Node locator 是有实际复用点的必选片；Python locator、Unix IPC 与 recipe 拆分需要真实当前消费者。diagnostics、Protocol-v3 与宿主管道仍是三条独立合同；A 不提前切 Windows loopback/named pipe/认证文件或扩大 helper。
- Node/Python 的 Windows 安全文件接缝仍留 G0/B 选择唯一机制；Phase A 的 POSIX binding 抽取没有绕过 B 的 Node authority、秘密传输、initdb 实际消费及认证生命周期合同。
- 新 leaf 当阶段即更新构建输入并验证真实 cache 失效；A 没有把这一责任推迟到 B/S6。现有纯 POSIX PG、版本、local auth、Unix socket、schema 和 reset 顺序保持。

## 回归与完成判据

§11.2.7 已把聚焦故障、完整 native Python、wheel/cache/资源、真实安装 GUI/CLI、provider 与长程分层，且不允许相互替代。baseline/candidate 的独立 uv/wheel 来源检查避免 editable 环境串用；artifact runner 两份代码都用完整工具面的 `--phase candidate`，没有拿删工具版本当旧代码基线。配置、凭据及本地 disposable DB 的 preflight 遵循根 AGENTS；缺 runner/GUI/provider 环境只能记验证待补，不能推断 Phase A 通过。

当前 native Ubuntu x64/arm64、macOS arm64 和至少一个 Ubuntu GUI 的要求与桌面提案一致；另一 Ubuntu 架构如涉及专属变动或已有待解问题须补 GUI。§13 的 Windows 激活没有被 POSIX 验收替代。

## 供 root 去重的文件专题 A 净成本

建议 **10–16 净人日**，这是原 W3 和 W7 文件部分的阶段归属，**不额外累加到 88–154 总预算**：

| 文件工作项 | 净人日 |
| --- | --- |
| typed binding / metadata / 三类发布与 POSIX 实现 | 3–4 |
| settings/hooks/kernel/anchor 锁的持有与关闭接入 | 1–2 |
| Skill/plugin 全 caller、held 结构与 source_import | 3–5 |
| import/preview/HTTP、filesystem/export 实际消费者 | 1–2 |
| 文件专题真实竞态、unknown、offset 与 Linux/macOS 聚焦回归 | 2–3 |

该范围不含 terminal/异步进程 caller、不含桌面资源定位/打包/安装 GUI，不重复计完整共用 native/provider/长程验收。全量联验由 A6 统一记账；若生产引用闭包发现更多持有者或原有泄漏，A0 据实调整，不能以压缩数字为由省略 hard cut 或故障回归。本段数字供计划使用，不改变本轮冻结判据。

## 695 行最终冻结确认

已复核最终文件文首及 §15。相对于已全文批准的 684 行候选，只更新冻结状态和三专题/root 审阅事实，没有增加实施语义或 A 成本数字。文首与尾部均明确 A 未实施/验收、G0 未执行、Windows 未实现或激活；与正文阶段合同一致。**同意 695 行最终版本冻结，无新增阻断。** 本次确认没有重跑研究、测试或 G0。
