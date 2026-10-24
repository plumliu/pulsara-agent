# POSIX-first Phase A：桌面/资源/IPC最小范围建议

日期：2026-10-24。本轮只读复核现有desktop TS/Python资源脚本、mise、Ubuntu workflows、native Electron smoke和dogfood runners。只在本目录写笔记，未改根spec/production，未运行测试、G0、provider或数据库操作。本建议给root汇总；跨专题A的terminal/file完整迁移由对应owner报告负责。

## 1. Phase A应该允许独立交付什么

赞成先交付**只改变内部接口/所有权位置、保持macOS/Linux现有产品行为**的Phase A，再进入Windows具体实现。A不应以Windows native G0已通过为前置；但它也不允许通过no-op Windows adapter、替换检索/权限、重写provider输入获得“跨平台完成”印象。Windows完整入口仍不支持，G0仍是Phase B的前置门。

A可以作为独立可review/merge的源码change，前提是其实际改动的全部POSIX caller、held对象、cleanup/借用和测试一起hard cut完成。它不是发布半迁移层：不能common返回typed binding、剩余caller仍当fd；不能POSIX facade只转发回旧实现而两套authority并存。但**不存在Windows实现**本身不是POSIX发布的兼容分支，不必虚构Windows stub或为了B先改现有产品合同。

A最小桌面结果是：同一个resource locator服务所有现有Node POSIX consumer；已有IPC的raw Unix机制可经窄接口抽离但endpoint/wire/权限/lifetime不变；继续一份boot/quit/reset/PG state。它不是先给Windows壳加按钮和exe。

## 2. 桌面/资源/IPC范围划分

| 范围 | A建议 | 实际POSIX复用/提取理由 | 保留的可观测值 |
| --- | --- | --- | --- |
| `desktop/src/resource-paths.ts` | **纳入A**，纯locator，调用一次注入main/runtime/CLI | `main.ts:16`、`runtime.ts:20,29,31`、`cli-install.ts:6`各拼同一Python/Playwright布局，多caller的真实重复。 | mac/linux Python仍`python/bin/python3`、PG bin与Playwright原目录，resolveHome仍调用同一Python resolver；不增manifest或Windows默认路径。 |
| `main.ts`/`runtime.ts` | **纳入A的接入**，不额外重写lifecycle | locator接入必须覆盖wait-owner、start与home；JSONL/ready/error/request map/EOF/close共用。 | 既有boot并行准备/start-frame gating、主frame/origin检查、正常close→PG stop顺序、未知错误展示；不更换encoding/事件集合/inspect时限。 |
| `cli-install.ts` | **纳入resource locator consumer**；无必要先造Windows leaf | POSIX shell script实际依赖相同Python位置，shellQuote/marker/status在A继续有效。 | `.local/bin`或deb `/usr/bin`、shell marker、同名冲突不覆盖、PATH不改、best-effort不阻塞DB。 |
| `postgres.ts` POSIX leaf | **非强制预拆**；若A整体目录方案要求，可仅机械提取当前socket/mode/uid/pgctl参数与核实操作 | 当前只有一个BundledPostgres owner，mac/Linux同一Unix socket合同。不能因未来Windows需要而在A预造TCP/auth abstraction。 | `PG_MAJOR`、`-k` socket、listen空、port5432、trust-local/reject-host、uid/0700、startedHere/initialized/失败保数据、reset次序完全不变。 |
| `desktop_runtime_io.py`/POSIX leaf | A的跨专题common接口需要时**成组抽取** | `desktop_runtime` server与CLI diagnostics client、kernel wait-owner/owner lock均有共用Unix lifetime机制。 | diagnostics.sock路径、有限JSON消息、AF_UNIX、chmod0600、fcntl owner/等待语义；不要先切namedpipe或改变Node控制帧。 |
| `terminal_protocol/transport.py` + POSIX leaf | A若抽内部transport，则**gateway/client/application一起迁移** | `v3_gateway`/`protocol_client`以及持有socket路径的application是实际多caller，不是仅terminal工具。 | 同一Unix socket路径/launch capability/frame/auth/attachment/revoke/close；loopback/SO_EXCLUSIVEADDRUSE仍是B。 |
| Python `runtime_resources.py` | **无POSIX真实consumer则延后B**；若A出现多处实际资源consumer才纳入 | 当前建议主要为Windows私有pwsh，A不需要为了目录对齐先建unused locator。 | rg仍归`ripgrep.py`，CLI→GUI仍归`desktop_launcher.py`/原desktop-launch.json；不新增副本。 |
| `prepare_resources.py` build leaf | **有真实Mac/Linux分离收益才抽**，不是A无条件重写builder | 当前已有Mach-O+dylibbundler与ELF+ldd/patchelf两类不同机制，适合按实际被动模块提取；download/export/install orchestration仍共用。 | 固定PG/vector/pg_search/Python/Playwright版本、native targets、许可证、内容校验、manifest值、native产物语义不变。 |
| frontend、preload、browser owner、startup cover | **默认不改**，只跑受影响路径验证 | resource/IPC refactor不需要改renderer contract、native控件、快捷键或path URL格式。 | same HTTP/preload API、user级服务owner、read_token/preview root、same literal tool surface。 |
| Win PG/auth、helper、NSIS、win CLI、win32 frontend、ConPTY、Win IPC | **全部延后B及对应G0** | 这些带独立Windows产品/安全语义，不是POSIX接口提取。 | A不生成postgres-auth.json，不扩managed DSN，不要求PS7/pywin32，不新增exe资产或Windows release项。 |

只抽实际接缝，不要求A一次创建根spec目录树里所有拟议文件。相关raw机制被抽出后旧执行路径须删除；未改领域继续在原生产owner，不把“没有预拆”误记成未迁移双轨。

## 3. 不能悄悄改变的POSIX产品合同

- **端点/secret**：POSIX的PG和diagnostics仍私有Unix socket，Protocol-v3仍原Unix endpoint；角色/DSN允许字段和auth/local socket隔离不变。A不创建新认证文件、不生成新密码，不切loopback、不把Win策略推广到POSIX。
- **资源与CLI**：仅内部locator注入，不能把默认home改AppData或把resources当user settings根；不能发现系统Python/rg/PATH fallback；CLI入口位置/脚本marker/argv/退出码保持不变。
- **provider**：A不修改SYSTEM、tool schema/描述、默认shell说明、工具名或binding字面值。已有epoch prefix不重建；baseline/candidate都采用完整生产tool surface，不用换provider说明来掩盖接口变化。
- **持久层**：无schema/clean-v0改动、无event/receipt/lease/恢复job、无总寿命caps；process/local object仅换实现位置，物理操作和副作用结算仍真实发生。
- **版本/发布**：A不自行bump版本、tag、draft/public release，不增加Windows artifact要求。保持当前相互一致的metadata、一个DMG+两个deb的release assembly集合、workflow_dispatch语义；源码独立交付不等于必须对外发布二进制。若用户另行发布，再由现有版本/release owner显式处理。

## 4. A验证顺序与必须/条件门

先在**baseline代码与candidate代码**使用同一锁/同一已声明环境跑对应层。baseline不是历史文档或旧evidence截图；记录确切Git revision、dirty状态、OS/arch、SDK/工具版本、命令、退出码和实际失败。没有native环境时标记未验证，不拿模拟/跨架构仿真作替代。

### A0：源码/单元与接口回归（A必需）

- 根uv环境执行所改owner现有tests；桌面最小关注`tests/test_desktop_runtime.py`、`tests/test_desktop_release.py`、`desktop/src/{runtime,postgres,main,cli-install}*.test.ts`及相关user-control、frontend runner边界；移除raw fd/IPC路径的caller由对应专题全量覆盖。
- `npm --prefix desktop test`；frontend若实际无改动，可使用既有workflow frontend regression/类型/lint作为共同基线，发生preload/type/组件改动则必须跑受影响前端测试与类型校验。
- macOS本机fixture绿仅证明macOS；Ubuntu按既有`.github/workflows/ubuntu-tests.yml`两架构native runner执行完整Python suite+wheel隔离安装（当前runner为Ubuntu24.04 x64/arm64）。`tests/linux/compose.yaml`可给本地Linux userspace/查询证据，但Docker/emulation不替native矩阵或GUI。

### A1：cache、wheel、native资源（按真实触及路径决定门，locator在A默认会触及随包启动）

- `desktop/scripts/test_build_cache.py`通过real mise freshness engine与real uv wheel验证。若抽`resource_build/*.py`，fixture创建真实leaf，修改内容并恢复旧mtime，证明受影响任务失效；改应用代码仍fresh application wheel且依赖资源能复用；missing output/failed task不能算命中。
- 只改Node locator也要验证已准备的资源/新shell位置的应用行为，不要求无理由清空全部cache。recipe/loader/layout/SDK输入变化时**必须native重建受影响资源**；可按mise正确边界热复用没变的资源。不能用`--force`把source依赖缺漏藏过去。
- 复用`mise run --jobs 1 desktop:verify`及`desktop/scripts/smoke_resources.py`。smoke当前：复制到带空格的`relocated resources`、净化PYTHON/DYLD/LD/PULSARA/PG环境、最小PATH、专有临时socket/cluster、扩展BM25+vector、Python import与Chromium启动，且从checkout外执行。它不会读保存设置，不是real-provider dogfood。
- 发行支持矩阵的**macOS arm64、Ubuntu24.04 x64、Ubuntu24.04 arm64**：触及共用recipe/layout的最终资源资格应逐target真实执行native version/architecture、relocation与wheel安装，不以Mac/lipo证明LinuxELF/loader。当前`tests/linux/installed_package_smoke.py`检验site-packages真实安装、launcher/rg/Skill/plugin、HTTP startup/shutdown；是wheel/backend门，不是Electron安装门。

### A2：原生Electron与已安装产物（desktop locator/lifecycle/资源接入时必需）

- 复用`npm --prefix desktop run test:startup`：真实Electron+生产preload/IPC、cover显示/淡出hide/minimize/destroy/replace与inert释放；只看Node mock不够。
- 若browser/cover/preload/layout接缝实际改变，`npm --prefix desktop run test:browser`也必需；无相关改动时可条件复用已有基线，不为纯resource locator额外改browser owner。该脚本当前输出硬编码`output/playwright/browser-owner-integration`，baseline每次完成须立刻原样归档到本轮平台目录，避免candidate覆写；可以给测试harness增加显式output参数，不改production API。
- 使用现有`mise run --jobs 1 desktop:package`或`package:dir`路径。最终提取改变真实安装定位时，应native生成对应DMG/deb并以**已安装**产物从非checkout cwd启动；不是`npm start`开发shell绿即完成。保持`--publish never`。
- 必需已安装行为：GUI cold/warm→真实bundle Python/PG ready→choose workspace/附件/preview与用户级设置→明确Quit→kernel close后PG stop→同home重启；旧owner慢退、启动失败、unknown退出/关闭race；CLI `--version/--help/app`、同名冲突、mac `.local/bin`与deb `/usr/bin`管理、关闭窗口继续后台/恢复/明确退出。无真实模型的fixture可证明生命周期；模型任务另见A3。
- macOS与**至少一套实际Ubuntu图形session**均必须有GUI证据，记录桌面环境/display/session bus。Ubuntu另一架构如共用OS级代码无差异，可明确以native build/relocation/CLI及同OS主架构GUI作为A的分层证据，不声称另一架构GUI实测；若触及其arch/native addon/图标/安装shell差异或该目标已有待解问题，则该架构installed GUI也变为必需。若根spec要对三target宣称全面installed GUI资格，则三target都需实测，不能在实施时临时降低门。
- 无Linux图形环境不是通过；A可写“源码完成、Linux GUI验证待补”，不能宣称Phase A全部验收完成或Windows已可开始全链路激活。

### A3：真实provider、前缀和长程（跨terminal/file/IPC核心迁移的A必需）

- baseline与candidate在同一真实平台fixture、相同保存的可用连接上执行。通过`LocalSettingsStore`/`require_pulsara_home`先读原生产配置，再临时home和已验证local disposable DB内read-only settings injection；不source .env、不导出APIkey、不复制生产secret，不修改保存设置。
- 至少一组覆盖所改OS边界的真实模型任务：terminal运行中继续工作、交互输入/关闭、artifact补读/导出、文件预览/变更检测、MCP/hook、queued subagent/compaction；完整长程由terminal/backend Phase A门统一跑，桌面不另计一套同样suite。若A只改纯桌面locator而kernel untouched，可选择代表bundle真实模型工作+quit/reopen；这是按影响裁剪场景，不是免掉provider可用证据。
- baseline/candidate不要追求随机模型回答逐字相同；比较工具实际行为、权限/副作用结果、wire SYSTEM/tools完整值及单epoch追加连续性，必要时固定prompt/fixture。记录原始prompt/provider request-response/tool trace和物理观察，精确清掉**实际secret值**，不生成code/doc/evidence SHA或fingerprint证明图。
- Linux未保存可用连接/缺local DB时报告具体缺项并标pending，不临时替换环境APIkey。Mac真模型任务不能证明Linux shell/pipe/native工具的实际provider交互；至少有Linux对应核心场景证据或A不得宣称该场景已验证。

## 5. 现有runner使用建议与陷阱

| runner | 可用于A的证据 | 必须避免 |
| --- | --- | --- |
| `tests/dogfood/run_terminal_cognitive_dogfood.py` | `--model`选择保存可用模型、`--phase`自由证据label、`--cases`、`--output`；先读saved settings、临时home+DB、只脱敏实际key。适合running/interactive/truncation/monitor等。 | 默认model不代表当前一定配置；先read-only preflight actual API与调用者，不添加新runtime cap。 |
| `tests/dogfood/run_round10_subagent_dogfood.py` | `--connection-id`明确保存连接，`--scenario`、`--output-dir`；支持完整subagent/context/capacity等。 | 不把provider连接ID当secret打印原配置；选择实际可用连接；scenario all的开销由共用A验证预算承担，不由桌面重复跑。 |
| `tests/dogfood/run_artifact_export_dogfood.py` | 读写/导出/no-clobber/readonly、长输出artifact补读。两份代码都用**`--phase candidate`**保持生产tool surface，用output目录区分before/after。 | 它的`--phase baseline`会主动omit artifact_export（源码顶部及报告baseline_method），会改变provider tools；禁止拿该模式当“接口提取前”基线。 |
| `tests/dogfood/run_workspace_recovery_dogfood.py` | workspace missing/recovery的具体产品证据，runner先读saved再隔离。 | 当前硬编码saved deepseek-flash Chat连接及旧输出目录，不是通用必跑命令；可参数化测试harness或用现有可选连接等价场景，不造credentials fallback，不把缺该特定model等同产品失败。 |
| `desktop/scripts/smoke_resources.py` | native bundle relocation与自建专有测试PG/extensions/Chromium。 | 它不读saved settings也不做模型工作；不能当real-provider或installed GUI通过。 |
| `tests/linux/installed_package_smoke.py` | 当前Ubuntu workflow真实wheel隔离安装，assets/CLI/rg/Skill/plugin/HTTP startup/shutdown。 | 不证实Electron tray/dialog/packaged private PG、DMG/deb安装或长程provider。 |

在执行前只读检查这些runner和生产API的当前兼容性。若runner已过时，修复测试harness到当前合同，不添加compat wrapper到production、不弱化assert或改evidence成绿色。现有脚本调用参考（仅计划，**本轮未执行**）：

```text
uv run --no-sync python -m tests.dogfood.run_terminal_cognitive_dogfood --model <实际保存可用模型> --phase before-A --cases <受影响场景> --output output/<A-evidence>/<platform>/before-A/terminal.json
uv run --no-sync python -m tests.dogfood.run_terminal_cognitive_dogfood --model <同一模型> --phase after-A --cases <同一场景> --output output/<A-evidence>/<platform>/after-A/terminal.json
uv run --no-sync python -m tests.dogfood.run_round10_subagent_dogfood --connection-id <实际保存连接ID> --scenario <受影响场景> --output-dir output/<A-evidence>/<platform>/after-A/subagent
uv run --no-sync python -m tests.dogfood.run_artifact_export_dogfood --model <同一模型> --phase candidate --cases <受影响场景> --output output/<A-evidence>/<platform>/after-A/export.json
```

每次报告记录baseline与candidate确切revision/dirty状态、OS/arch、完整实际命令、环境工具版本、结果/原错误、临时DB精确本机target与cleanup、saved settings只读使用说明。DSN可检查，密码只在实际出现处脱敏。输出按before/after及platform隔离；不覆写旧baseline，不靠历史绿结果替补本轮失败。

## 6. A成本拆账（不叠原总数）

桌面/资源专属A净工程估计（熟悉仓库、native Mac/Linux环境和必要runner可用）：resource locator多caller+测试 **1–2人日**；Unix宿主/内部IPC接缝成组提取中桌面接入 **2–4人日**（Pythontransport主体由backend统一记账）；build/cache真实改动的机械split与cache fixtures **1–3人日**（不拆则减）；native package/relocation/installed GUI+CLI证据及故障复核 **3–5人日**。合计约 **7–14人日**，是原W4–W7预算的一部分，不加到88–154上。terminal/file大迁移的A成本不在本桌面小计内，root须以唯一owner重新分配A/B，而非相加所有agent小计。

A/B阶段增加一次基线和交付review的真实额外劳动，如确需另一次完整多平台安装/长程运行，可以单列cross-phase验收开销并解释原因；不能对同一份证据在A与总W7各收一遍。基线重用须证明未受后续B修改影响；B仍需Windows全链激活和POSIX受影响回归，不能因为A绿就豁免。

## 7. 根spec必须显式修订的冲突条款

1. **§1/§3.1/§4/§11 W0先于全面生产改动**：当前文字“G0全部通过后Python正式实现”阻止A。改为A是行为保持POSIX提取，可先实施并独立验收；G0仍阻断Windows B，而非阻断A。不得顺手取消Windows G0。
2. **§3.1目录树**：标注A确有consumer的common/POSIX leaf、B Windows leaf、条件性helper/locator/build split；A不强建unused Win文件、PS7资源或platform服务框架。文件名是落点不是必须全创建的任务清单。
3. **§3.3/factory只选支持平台**：A只选择当前可用POSIX实现，Windows仍明确不支持，不返回POSIXfallback。没有Win stub不构成old/new兼容；抽取不能添加另一个user可选backend配置。
4. **§3.4/3.5与§11.1 S1/S2 hard-cut**：改成A切片内POSIX接口+所有caller/held/cleanup一次完成并删旧raw路径，B在同一接口补Windows。禁止“必须POSIX和Win同批才能合并”的歧义，也禁止A留raw兼容dual path。
5. **§7/§9/§10 Windows产品语义**：明确仍为B；A不更换PG endpoint/auth、三条IPCtransport、frontend/titlebar、NSIS/default Windows kill修复（当前无Win包）、Windows CLI/release assets；已有POSIX保持原行为。
6. **§11成本/顺序**：写A实交付/验收→B G0→Windows实现/激活；如果先执行独立G0研究也不阻止A并行，但A成功≠B G0通过。A/B预算是对原工作包重新归属，必要的额外重复验收单列。
7. **§13激活与§15冻结状态**：增加Phase A独立exit：POSIX抽取与相应native回归完成；不得用“Windows不可激活”否决已完成A，也不得用“A已合并”声称Windows支持完成。缺Linux必需证据要pending不是skip。
8. **cache sources/outputs条款**：A若已拆Mac/Linux recipe就立即追踪leaf并测试失效，不能等Windows S6才补；不得通过force build掩盖热cache错误。

建议根把A列为可落地的下一实施目标，同时保留B未知Windows机制的条件冻结；本proposal不授权现在执行A或G0，也不替用户自行开始实现。

追加runner preflight细节：terminal/export/subagent共用的`run_model_switch_handover_dogfood.py:354-391 _create_database`只接受保存admin/runtime DSN host为`localhost/127.0.0.1/::1`且root dbname=`pulsara`，再创建自己的临时DB。它不会自动接受桌面生产managed Unix socket。若当前保存配置不适用，应报告缺项/先在测试harness闭合已授权的本地隔离方式；不能为了runner给POSIX产品开TCP、改保存settings、导出实际密码或连未核实目标。本机检查仍需验证解析后的实际local endpoint，文本localhost本身不是远程目标安全证明。

追加baseline来源真实性：before/after若使用不同checkout/worktree，应各用对应uv管理的repo环境或隔离安装该版本wheel，不能共用一个editable `.venv`或继承指向candidate的`PYTHONPATH`。记录`pulsara_agent.__file__`、实际CLI/interpreter/resource路径及构建Git revision，确认baseline实跑baseline。`tests/linux/installed_package_smoke.py`已经有package位于sys.prefix/site-packages且无source PYTHONPATH的断言，可复用该证据边界；不得在baseline目录用candidate editable安装并称作旧行为。不生成逐文件hash。
