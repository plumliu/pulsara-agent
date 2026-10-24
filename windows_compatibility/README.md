# Windows 兼容实施目录

这里集中维护 Pulsara 的 Windows Electron + WSL2 路线、开发交接和实施过程。文中的 `src/`、`desktop/`、`frontend/` 及命令执行位置均以**仓库根目录**为基准；生产代码继续在原位置，不从本目录 import 实验代码。

## 当前入口

1. [冻结实施 spec](PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md)：路线、所有权、G0-W、Phase A/B 边界与验收矩阵；第16节是本次 Phase A 实施记录。
2. [Windows 开发准备与 G0-W 操作清单](PULSARA_WINDOWS_DEVELOPMENT_G0_CHECKLIST.zh.md)：第一次接手时按第3节开始准备 Windows 本机工具、专用 WSL2、两份仓库与独立实验。

当前状态：Phase A 代码及 Mac 本机/应用包 GUI 实证已冻结；原生 Ubuntu x64/arm64 与 Ubuntu GUI 仍待验收；G0-W 待 Windows 实测，Phase B 未实施，Windows 支持未激活。整理目录不改变这些状态。

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
- `wsl-first/`：当前WSL主路线及收窄Phase A的提案、交叉审阅和路线冻结过程。root内的`previous-native-posix-spec.zh.md`/`candidate.zh.md`是历史快照，仍以本目录顶层spec为准。
- `phase-a-implementation/`：改前/改后验证、实际POSIX抽取、真实PG/内核/GUI实验及四方冻结记录。
- `windows-handoff/`：Windows环境准备/G0清单的建议、示例语法检查与全文交叉审阅。

Mac生成应用包`package/`、隔离应用副本`installed/`和`pytest-tmp/`测试临时数据已一并搬到相应本地目录保留，但由根`.gitignore`的本目录专属规则排除，不上传约3GB二进制或测试状态；Docker上下文也排除此文档/实验目录。它们不是Windows依赖，也不影响随Git保存的源码/文字证据可读性；原始日志中的临时PID/已删除home是历史观察，不表示仍有进程需要操作。

## 后续使用

新的跨平台实验仍按清单放`output/windows-g0/`隔离工作，成熟且不含凭据的报告/可复用小型探针再整理进本实施目录的对应日期/阶段。不要提交应用资源、venv/node_modules、数据库、raw settings或实际凭据，不用`git add -f`强行带入生成物。

另外两篇已结束阶段的根文档已按用户要求归档到[临时模型请求规格](../archived_docs/PULSARA_EPHEMERAL_REQUEST_TITLE_SUGGESTION_IMPLEMENTATION_SPEC.zh.md)和[自定义服务模型列表规格](../archived_docs/PULSARA_CUSTOM_MODEL_DISCOVERY_IMPLEMENTATION_SPEC.zh.md)，不混入Windows实施合同。
