# Windows 兼容实施目录

这里集中维护 Pulsara 的 Windows Electron + WSL2 路线、开发交接和实施过程。文中的 `src/`、`desktop/`、`frontend/` 及命令执行位置均以**仓库根目录**为基准；生产代码继续在原位置，不从本目录 import 实验代码。

## 当前入口

1. [当前实施 spec](PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md)：路线、所有权、G0-W、Phase A/B边界与验收矩阵；原冻结路线按用户授权修订为已有WSL接入，第16节保留Phase A历史实证。
2. [Windows 开发准备与 G0-W 操作清单](PULSARA_WINDOWS_DEVELOPMENT_G0_CHECKLIST.zh.md)：按第3节准备Windows工具、确认已有WSL2/默认非root用户，再准备双仓库与独立实验。
3. [Windows首轮环境/G0-W实测报告](research/2026-10-24/root/g0-w-initial/report.zh.md)：已有Ubuntu/plumwsl环境、双clone和Linux资源验证完成；记录interop普通Windows后代取消缺口，完整G0未通过，B未进入。
4. [第二轮取消/PG异常死亡实验](research/2026-10-24/root/g0-w-round2/report.zh.md)：无breakaway Job及每任务私有socket转发取得局部实证；裸WSL_INTEROP回退与PG helper晚启动反例已确认。
5. [第三轮PG替代准入/启动取消竞态/真实任务集成](research/2026-10-25/root/g0-w-round3/report.zh.md)：独立PG生命周期锁、有限子命令继承原锁、真实拒删/删除重建取得正证；SCM_RIGHTS首次绑定和Windows/Linux组合取消有机制证据；真实terminal内存候选14项通过，完整组合仍待验。
6. [第四轮真实任务合成/PG owner失联](research/2026-10-25/root/g0-w-round4/report.zh.md)：首次绑定、取消和真实terminal启动预约/回滚已合成；补齐存活/结果冻结接缝。真实TS PG owner完成双锁reset及EOF/失联实测，原owner误认领未提交启动的反例与两行研究修正均有证据。
7. [第五轮文件与真实HTTP owner](research/2026-10-25/root/g0-w-round5/report.zh.md)：真实Windows图形目录、用户文件选择/拖放、逐字节导入、HTML/image/PDF及下载；保留目录竞态、PDF白屏和研究启动器EPIPE等原失败及修正。
8. [MCP跨域回收](research/2026-10-25/terminal/mcp-round5/report.zh.md)、[hooks/environment跨域回收](research/2026-10-25/terminal/hooks-environment-round5/report.zh.md)、[PG其余失联边界](research/2026-10-25/backend/pg-boundaries-round5/report.zh.md)：真实产品owner研究集成、会话与调用范围区分、双锁与有限协议、ext4检索/搬移/reset实证。
9. [目标发现与离线payload](research/2026-10-25/desktop/discovery-payload-round6/report.zh.md)、[真实kernel控制链](research/2026-10-25/terminal/kernel-control-round6/report.zh.md)：用户原环境不变；有限安装失败保数据；正常EOF关闭顺序与异常退出误判反例。该阶段尚待的整发行版故障、OAuth及睡眠恢复已在下一项最终验收中补齐。
10. **[Windows WSL2 G0最终验收](research/2026-10-25/root/g0-w-final/report.zh.md)**：本机所选矩阵D/C/P/F/N机制可行性通过；补齐实际宿主SIGHUP/窗口退出、88.493秒睡眠恢复、Notion授权与callback rebind、原样PGDATA的真实dirty恢复。原失败、探针局限及生产待实施项均保留。
11. [Phase B正式接入与共享修正（进行中）](research/2026-10-25/root/phase-b/report.zh.md)：用户授权后的生产实现、原回归与真实适配验证，保留新发现的失败及修正；安装包、完整GUI与真实provider验收单独列状态。
12. [Windows共享修改后的原生macOS回归](research/2026-10-25/macos-regression/root/report.zh.md)：Mac arm64源码、真实provider、最终wheel/应用包及图形工作区验证；记录本轮EPERM与post-accept head窄修、四方审阅和未覆盖项。
13. [本轮共享修复的Windows最小重验清单](PULSARA_WINDOWS_SHARED_FIX_MINIMAL_REVALIDATION.zh.md)：同步/新payload、98项WSL定向测试、10项真实PE集成、Windows宿主与最终包context/GUI核验；不重做整个G0，不替代发布矩阵。
14. **[0ea98e63 / alpha.10 Windows新基线](research/2026-10-25/windows-shared-recheck/root/report.zh.md)**：R0–R5最小重验全部通过；新payload/实际安装来源、98项源码定向、10项真实PE、Windows宿主/startup、8次真实context调用、升级保数据及GUI/正常Quit/冷重开均有新证据。后续Phase B故障资格继续，用户无备用Windows环境，“WSL未安装／需重启”真实验收延期，不删除日常Ubuntu。
15. **[Phase B后半段本机验收收口](research/2026-10-25/phase-b-final/root/report.zh.md)**：alpha.10补齐正式PG五项失联、interop取消与未知边界、installed main活动Quit/宿主强退、setup/TLS/ENOSPC处理、真实活动任务睡眠与Explorer刷新。所选本机矩阵通过；B1–B4核心实现已落地，B5仍未激活。用户出差仅内屏，混合DPI往返延期；未装WSL/需重启、原生Ubuntu及完整发布集合依原矩阵单列，不能把延期算通过。
16. [全平台统一开源字体](PULSARA_CROSS_PLATFORM_TYPOGRAPHY_IMPLEMENTATION_SPEC.zh.md)：所有平台统一采用随包开源字体；alpha.11包含字体/许可和同一加载链。[字体与样张入口](typography/README.zh.md)，新Windows/Linux包的实际渲染按后续重建验收，不沿用alpha.10的旧制品资格。

当前状态：**2026-10-25本机Windows WSL2所选矩阵的G0-W机制可行性验收通过；B1–B4核心生产接入已落地，alpha.10本轮所选本机验收已收口；Windows支持/B5尚未激活。** 原生Ubuntu x64/arm64与GUI按用户要求延期，不作为本轮Windows G0验收的前置，也未被WSL结果替代。已执行与延期的真实环境、硬件和发布资格分别见第15项及spec第27节；下面历史阶段的“未实施”描述保留为当时状态。

Phase B已实际安装开发候选，并完成图形中文工作区、真实模型文件读写、Windows选择/拖放、HTML/image/PDF预览及图片下载字节比对。终端、记忆/历史和真实provider记录见各分工报告。进一步验收发现**共享ROOT准入缺陷**：候选输入的canonical head过期后，原owner反复重发同一旧候选而无法前进；这不是Windows跨域回收问题。窄修已通过6项真实PG定向回归、28项相关测试，进入用户批准的本地alpha.7安装包后，原capacity/context真实provider场景及跨压缩的真实Linux/Windows进程跟进也已严格通过。正常Quit/冷重开、alpha.6→alpha.7更新、普通卸载/重装保数据、正式安装版Notion OAuth与约80秒睡眠恢复均已实际通过；用户现场确认中文输入法、托盘恢复保留草稿及鼠标贴靠/还原正常。用户随后补充可使用笔记本内屏与HDMI外屏，混合DPI待核验；右Alt键本身不能证明有AltGr布局。受影响macOS原生回归、其他安装/故障环境及发布矩阵尚未齐全，不能把本机分项通过写成B5激活或全部平台完成。

G0发现的三类问题需要分别理解。“正式代码”指仓库中当前被产品调用的实现；其中的共享代码也供macOS使用，但不能把Windows实验中的所有反例都称为已在macOS复现的缺陷。

后续键盘检查又发现共享前端快捷键缺陷：Ctrl+Alt+K/N及IME组合期的Ctrl+K/N会被误作普通快捷键。两处原handler的窄修已通过254项相关测试、类型/lint与交叉审阅，见[快捷键报告](research/2026-10-25/terminal/phase-b/shortcut-review/report.zh.md)；修复已随alpha.9实际安装，Ctrl+Alt+K/N不误触、普通Ctrl+K/N可用及草稿保留已由真实GUI复验。已映射的AltGr特殊字符正例在原实现也通过，不能把该缺陷扩大成“所有AltGr输入错误”，也不将合成输入当作真实AltGr布局资格。

本轮Windows迁移及验收已获用户授权自主递增必要本地alpha候选版本并构建、安装，不再逐次询问版本号；Git tag和对外发布仍需另行明确授权，详见当前spec §8.3。

**macOS arm64本轮已执行矩阵通过**：基线a57ce3b1加本轮共享窄修，完整前端1372项、后端244项、desktop105项、terminal原167+PG4项及修后定向68项、真实Electron browser13项分别通过（各suite有重叠，不相加宣称总数）；最终随包context8/capacity15与terminal19次真实provider调用通过。新包实际完成原生中文目录/文件引用、⌘快捷键、隐藏恢复草稿、正常Quit及冷重开保留会话/工作区。修复Darwin zombie组EPERM和ROOT accept后仅全局head前进窗口，完整失败/修后证据及四方审阅见上项报告与spec §25。真实Mac睡眠、硬件IME、Mac x64/分发资格仍待验收。针对这次共享修复的Windows重验已在alpha.10完成，见第14项；没有沿用旧alpha.9安装结果，Windows支持仍未激活。

已修复的睡眠阻断：alpha.8构建后，在alpha.7正常Quit准备更新时，原kernel报告`StaleHostWriter`收尾失败，宿主因此保留PG并阻止更新。原会话writer lease在睡眠期间到期，旧SessionEnd读取仍要求有效writer。原GUI重试复用同PG恢复，随后空闲总览正常Quit和维护检查通过，未reset或强杀。此前alpha.7睡眠后的进程、健康检查和界面恢复证据仍成立，但没有覆盖有效writer、新消息与后续Quit，不能据此称完整通过；原失败记录保留。alpha.8未安装，修复进入alpha.9。用户已关闭内屏，重建窗口回到外屏不冒充原窗口混合DPI拖回验证。自动恢复与友好重试呈现的授权见spec §9.3。

上述修复已随alpha.9安装：续约异常封旧准入，原Core完成物理close后由原controller冷开，普通断连继续沿原前端重连；真实关闭失败保留quarantine且不可普通重试。原安装alpha.7的独立PG反例、候选真实Windows子进程回收及并发恢复、Host新增9项、相关315项（另1项原环境条件skip）、renderer29项、前端363项及真实Electron cover验证均已通过。

**alpha.9本机闲置睡眠验收已通过**：正常更新保留模型、原会话和数据库；用户实际睡眠108.491秒后，同一Windows主进程、kernel和PG保持，会话writer generation从1自动恢复到2，未发送草稿保留，无需手动重试。实际发送睡后消息得到“自动恢复后模型调用正常”，独立数据库核对两轮均只消费一次、无工具调用；随后原确认流程正常Quit，应用与kernel退出、PG停止，维护检查exit0。应用现保持关闭，interop与Hiddify TUN未关闭。详见[alpha.9睡眠与收尾报告](research/2026-10-25/desktop/alpha9-sleep/report.zh.md)及[Phase B汇总](research/2026-10-25/root/phase-b/report.zh.md)。本项覆盖闲置睡眠、自动恢复、真实新消息和正常退出，不扩为活动任务睡眠全矩阵或B5激活。

| 问题 | 性质与已有证据 | 修复边界 |
| --- | --- | --- |
| terminal及MCP/hooks/environment跨域存活与回收 | **Windows接入缺口。** 原POSIX进程组只能覆盖Linux/POSIX进程；WSL interop产生的Windows后代可能在Linux组退出后仍存活。G0已复现提前完成/释放资源，但这不是macOS原生任务的复现结果。 | 完整修复需要把Windows Job及WSL转发范围接入各原owner的存活、取消和物理结算；属于正式Windows接入，不能用只改Linux判断或研究探针冒充完成。 |
| PG启动失败后的`startedHere`归属 | **共享owner的错误分类缺口，跨域失联使问题显现。** G0向原TS owner注入“本次未提交/结果未知”后，实测其catch按当前同库状态误认领其他调用启动的实例。尚未证明原生macOS正常有限exec会产生同一反例。 | 区分未提交、执行结果未知与已结算非零；只有符合原读回条件的已结算失败才能走启动归属读回。WSL helper、双锁和跨域装配另属Phase B。 |
| Runtime在child清空后漏查异常退出 | **现有共享代码缺陷。** 原`stop()`在`child`已被close回调清空时直接返回，跳过已保存的异常退出结果，可能让Quit继续停PG。此逻辑也被macOS使用；G0实证来自Windows研究宿主，尚无本次macOS原生复现。 | 在现有Runtime owner检查已结束attempt的退出结果；无需先实现Windows adapter。 |

用户随后先授权独立修复Runtime与PG共享owner/POSIX错误分类，结果见[独立修正报告](research/2026-10-25/root/shared-owner-fixes/report.zh.md)：当时WSL隔离源码编译与desktop Node测试60/60通过，Windows定向回归通过。其后用户明确授权进入Phase B，先修复两个POSIX启动清理缺陷，再正式接入Windows跨域回收及其余规范功能；实施中发现的相关生产缺陷直接修复。两个POSIX缺陷已修复并通过62项定向回归，Windows接入正在实施及验证，尚不能称整套产品验收完成。该历史阶段尚无Mac原生重验，Phase A旧证据只作当时基线；后续本次原生Mac回归见第12项，G0候选通过仍不能替代正式接入验收。

2026-10-24 用户授权修订：Ubuntu发行版本号不限，接入已有发行版及其默认非root用户，取消`Pulsara`/`pulsara`固定命名；保留interop、Windows PATH和自动挂载，不管理或终止共享发行版。应用资源/PG/工程仍使用Linux ext4，依赖pins保留。当前本机Ubuntu26.04.1/plumwsl的版本准入及基本interop已观察，当时Windows后代生命周期等完整G0待验证，后续本机结论见最终报告。历史四方批准仅覆盖旧合同，不作为本修订通过证据。

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
