# Windows 兼容实施目录

这里集中维护 Pulsara 的 Windows Electron + WSL2 路线、开发交接和实施过程。文中的 `src/`、`desktop/`、`frontend/` 及命令执行位置均以**仓库根目录**为基准；生产代码继续在原位置，不从本目录 import 实验代码。

## 当前入口

1. [当前实施 spec](PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md)：路线、所有权、G0-W、Phase A/B边界与验收矩阵；原冻结路线按用户授权修订为已有WSL接入，第16节保留Phase A历史实证。
2. [Windows 开发准备与 G0-W 操作清单](PULSARA_WINDOWS_DEVELOPMENT_G0_CHECKLIST.zh.md)：按第3节准备Windows工具、确认已有WSL2/默认非root用户，再准备双仓库与独立实验。
3. [Windows首轮环境/G0-W实测报告](research/2026-10-24/root/g0-w-initial/report.zh.md)：已有Ubuntu/plumwsl环境、双clone和Linux资源验证完成；记录interop普通Windows后代取消缺口，完整G0未通过，B未进入。
4. [第二轮取消/PG异常死亡实验](research/2026-10-24/root/g0-w-round2/report.zh.md)：无breakaway Job及每任务私有socket转发取得局部实证；裸WSL_INTEROP回退与PG helper晚启动反例已确认。
5. [第三轮PG替代准入/启动取消竞态/真实任务集成](research/2026-10-25/root/g0-w-round3/report.zh.md)：独立PG生命周期锁、有限子命令继承原锁、真实拒删/删除重建取得正证；SCM_RIGHTS首次绑定和Windows/Linux组合取消有机制证据；真实terminal内存候选14项通过，完整组合仍待验。

当前状态：Phase A代码及Mac本机/应用包GUI实证冻结范围保持；原生Ubuntu x64/arm64与GUI仍待验收；Windows回收和PG替代维护准入已有G0候选实证，尚未接入生产或通过完整C/P门。当前PG候选已替换被否定的单helper持锁方案；Windows真实owner仍有提前完成反例，内存修正不代表生产已修复。完整G0未过，Phase B未实施，Windows支持未激活。

2026-10-24 用户授权修订：Ubuntu发行版本号不限，接入已有发行版及其默认非root用户，取消`Pulsara`/`pulsara`固定命名；保留interop、Windows PATH和自动挂载，不管理或终止共享发行版。应用资源/PG/工程仍使用Linux ext4，依赖pins保留。当前本机Ubuntu26.04.1/plumwsl的版本准入及基本interop已观察，Windows后代生命周期等完整G0仍待验证。历史四方批准仅覆盖旧合同，不作为本修订通过证据。

## 四方过程材料

`research/2026-10-24/`来自原`output/windows-compat-20261024/`，完整保留root及三位`gpt-6.1-sol high` subagents的个人目录。文本材料、日志及小型探针随Git同步；原始内容中的旧命令/路径保留为当时记录，不是新的运行入口。

| 子目录 | 内容与阅读顺序 |
| --- | --- |
| [root](research/2026-10-24/root/) | 汇总、验证与冻结；[Phase A最终审阅](research/2026-10-24/root/phase-a-implementation/final-review.md)、[Mac GUI证据](research/2026-10-24/root/phase-a-implementation/macos-gui-review.md)、[最终验证日志](research/2026-10-24/root/phase-a-implementation/verify-final.log) |
| [terminal](research/2026-10-24/terminal/) | 终端/控制链路研究与实证；[Phase A终审](research/2026-10-24/terminal/phase-a-implementation/freeze-review.md)、[交接清单审阅](research/2026-10-24/terminal/windows-handoff/review.md) |
| [backend](research/2026-10-24/backend/) | PG/文件/执行域研究与实证；[Phase A终审](research/2026-10-24/backend/phase-a-implementation/freeze-review.md)、[真实PG失败实验](research/2026-10-24/backend/phase-a-implementation/real-pg-failures.log)、[交接清单审阅](research/2026-10-24/backend/windows-handoff/review.md) |
| [desktop](research/2026-10-24/desktop/) | 桌面/资源/安装/GUI研究；[Phase A终审](research/2026-10-24/desktop/phase-a-implementation/freeze-review.md)、[包与CLI来源证据](research/2026-10-24/desktop/phase-a-implementation/package-cli-evidence.json)、[交接清单审阅](research/2026-10-24/desktop/windows-handoff/review.md) |

各个人目录的阶段含义：

- 顶层`report.md`、`code-organization/`、`posix-first/`：早期原生Windows/较宽POSIX抽象方案的历史研究，已被当前路线取代，不作为实施authority。
- `wsl-first/`：原专用WSL路线及收窄Phase A的提案、交叉审阅和冻结过程；接入已有发行版/保留interop的后续用户修订以顶层当前spec为准。root内的`previous-native-posix-spec.zh.md`/`candidate.zh.md`均为历史快照。
- `phase-a-implementation/`：改前/改后验证、实际POSIX抽取、真实PG/内核/GUI实验及四方冻结记录。
- `windows-handoff/`：Windows环境准备/G0清单的建议、示例语法检查与全文交叉审阅。

Mac生成应用包`package/`、隔离应用副本`installed/`和`pytest-tmp/`测试临时数据已一并搬到相应本地目录保留，但由根`.gitignore`的本目录专属规则排除，不上传约3GB二进制或测试状态；Docker上下文也排除此文档/实验目录。它们不是Windows依赖，也不影响随Git保存的源码/文字证据可读性；原始日志中的临时PID/已删除home是历史观察，不表示仍有进程需要操作。

## 后续使用

新的跨平台实验仍按清单放`output/windows-g0/`隔离工作，成熟且不含凭据的报告/可复用小型探针再整理进本实施目录的对应日期/阶段。不要提交应用资源、venv/node_modules、数据库、raw settings或实际凭据，不用`git add -f`强行带入生成物。

另外两篇已结束阶段的根文档已按用户要求归档到[临时模型请求规格](../archived_docs/PULSARA_EPHEMERAL_REQUEST_TITLE_SUGGESTION_IMPLEMENTATION_SPEC.zh.md)和[自定义服务模型列表规格](../archived_docs/PULSARA_CUSTOM_MODEL_DISCOVERY_IMPLEMENTATION_SPEC.zh.md)，不混入Windows实施合同。
