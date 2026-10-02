# Pulsara 内容搜索与文件查找拆分实施规范

状态：2026-09-30 用户已授权实施；搜索拆分、私有 ripgrep 与下游 Hook 契约已联合实现。同一 GPT-6.1 Sol / xhigh critic 完成代码复审，无剩余阻塞。macOS arm64 成品安装检查已通过；真实 GUI dogfood 已通过，完成本次激活验收。详细记录见第 12 节。

本文是 [Hook 有限 Codex 支持与用户审阅设计](PULSARA_HOOK_WEAK_CODEX_SUPPORT_AND_REVIEW_DESIGN.zh.md) 的搜索与依赖上游规格：本文拥有两项搜索工具的参数、执行、结果、ripgrep 打包和 terminal PATH；下游设计拥有整体 Hook 原生输入、闭合别名表、支持输出及用户审阅。本轮联合实施一次 hard cut，首次发布的 trust contract v2 同时覆盖两项搜索、最终别名与 Hook 输入/控制语义；不先独立发布其中一部分再复用同一个 v2。两份文档的联合审阅及实现验收均不由原版搜索文档的审阅结论代替。

## 1. 目标与范围

将模型可见的混合入口 `search_files` 一次性替换为 `search_content` 与 `find_files`。工具名直接表达查询对象；不再由 `target` 改变 `pattern` 的含义，也不接收在当前操作中被静默忽略的参数。

| 工具 | 职责 | 主要参数 |
| --- | --- | --- |
| `search_content` | 查找文本文件内容中的匹配 | `pattern`、`path`、可选 `file_glob`、分页及结果形式 |
| `find_files` | 按文件名称或相对路径 glob 查找文件 | `glob`、`path`、分页 |

两者保留 `permission_category="filesystem_read"`、`is_read_only=True` 与 `tool_family="filesystem"`。只读属性由现有权限策略消费；filesystem_read 和 filesystem family 当前只是分类标签，不决定读取路径或并行调度。共享已有路径解析、权限、搜索执行、结果封装与大结果 artifact 机制；不复制两套权限或状态系统。

本规范拥有两个闭合 schema、执行器接线、结果语义、私有 ripgrep 依赖打包、terminal 进程级 PATH 接线及 Python 搜索 fallback 删除、前端工具摘要、搜索 Hook 名称接线、现行说明及测试/dogfood 的同步。联合实施同时按下游规范完成 Hook 确认页与其他工具别名；供应商脚本 stdin 转换、搜索索引、持久缓存、通用查询 DSL、全文检索及内容编辑均不在本轮。

### 1.1 已实施的工具元数据减法

按用户后续决定，全局删除 BuiltinToolDescriptor 的 `is_concurrency_safe` 和 `long_horizon_policy`，删除调用分类中的 `effective_concurrency_safe`、catalog 中的 `long_horizon_policy_kind`，以及只为该闲置策略生成动作分类、阶段、成本和 classifier fingerprints 的代码。上述字段此前没有实际调度或 agent loop 执行消费点；不保留兼容字段、别名或旧策略生成器。观察 rollup renderer 的现有结果渲染合同仍保留。

权限判断、实际工具并行调度、物理资源边界和上下文压缩不因本次减法改变。descriptor/catalog 语义摘要自然随新字段集合重新计算；builtin 稳定 ID 仍按既有名字构造，不重建旧策略以保持旧摘要。现有 epoch 的 SYSTEM/tools 与历史消息不得重写；新代码只在既有冷 epoch 或明确采用的 compaction successor 边界建立新工具根，不对旧进程做热补丁，也不引入新 rebase 边界。搜索工具拆分的后续实施记录见第 12 节。

减法验证（2026-09-30）：两组 focused pytest 共 326 项通过，覆盖 descriptor/executor 闭合、权限、前缀连续性、图像工具、压缩、计划、artifact 与架构基线；新增序列化字段缺席断言后重跑对应闭合测试通过。受影响 Python 文件 Ruff 与 `git diff --check` 通过。本次未启动真实 Pulsara 会话或热更新现有进程。

## 2. 实施前基线与 ownership

以下为本次 hard cut 之前的生产基线，用于说明被替换的路径；不代表实施后的现状：

- `capability/builtin_catalog.py`：一个 `search_files` descriptor，`target=content|files`；`pattern` 同时代表内容正则和名称片段/glob。`file_glob`、`output_mode` 在 files 路径被忽略。
- `tools/builtins/filesystem.py`：`SearchFilesTool` 在执行时分支；优先调用安装在 PATH 的 `rg`，没有 `rg` 时走既有 Python 实现。两条路径的正则、文件发现和排序并不完全一致。名称搜索会隐式补 `*`。
- `conversation_kernel/tool_runtime.py` 和 `tools/builtins/__init__.py`：构造及导出旧类。
- `frontend/lib/builtin-tool-summary.ts`：统一显示“搜索文件”，结果靠是否有 files 数组区分。
- `hooks/matcher.py`：当前没有 `search_files` 的 `Grep` / `Glob` 别名。

本轮改为唯一搜索后端：随 Pulsara 安装包携带固定版本的 ripgrep，由它拥有内容正则、文件发现和正文搜索；删除 Python 搜索 fallback 与 PATH 探测分支。Python 只承担 Pulsara 的参数检查、subprocess 调用、结果解码、排序与分页，不再作为第二个搜索引擎。`wcmatch` 只拥有候选路径的 glob 匹配。Pulsara 拥有工具职责、获准路径、参数约束、结果页与可见失败，唯一小 matcher adapter 不执行第二次目录遍历。模型直接调用 built-in search_content/find_files，无需使用 terminal 拼接 rg 命令，也无需知道内部版本或可执行文件位置。

新增的 `wcmatch==10.1` 是 Python 应用依赖，由实施时更新 pyproject/uv.lock；ripgrep 是独立原生可执行文件，按第 2.1 节进入应用发行包，不把它伪装成 Python 库或要求用户另装系统工具。标准库 fnmatch 的 `*` 跨目录、`**` 无层级语义，不能满足选定的相对路径模式合同；保留 wcmatch 纯匹配 API，避免自己写 glob 解析器。取消双搜索后端后不再保留两套正则/ignore 语义；相同输入使用固定 ripgrep 版本与显式参数。不同系统的文件集合、文件系统大小写和权限仍可能不同，不承诺跨系统搜索结果完全一致。

### 2.1 私有 ripgrep 的版本、打包与调用

固定依赖为 [ripgrep 15.2.0 官方发布](https://github.com/BurntSushi/ripgrep/releases/tag/15.2.0)。该发布的资产与对应 `.sha256` 文件已核对；后续升级须显式更新构建输入并重新验证搜索与打包合同，不能在构建、安装或运行时解析 latest 自动升级。ripgrep 保持独立程序，不复制其遍历器、ignore 解析器或正则引擎到 Pulsara。

初始依赖资产映射如下；这是搜索依赖的打包目标，某一平台的 Pulsara 发行仍须通过整应用既有支持与安装检查，不因 upstream 提供二进制就宣称整应用已支持该平台。

| OS / 进程架构 | 官方资产（名称前缀均为 `ripgrep-15.2.0-`） |
| --- | --- |
| macOS arm64 | `aarch64-apple-darwin.tar.gz` |
| macOS x86_64 | `x86_64-apple-darwin.tar.gz` |
| Linux arm64 | `aarch64-unknown-linux-musl.tar.gz` |
| Linux x86_64 | `x86_64-unknown-linux-musl.tar.gz` |
| Windows arm64 | `aarch64-pc-windows-msvc.zip` |
| Windows x86_64 | `x86_64-pc-windows-msvc.zip` |

复用现有 Hatch 构建链，准备目标平台的官方资产并随 wheel/应用安装包分发。包内放置唯一当前目标的私有 `rg`（Windows 为 `rg.exe`），例如 `pulsara_agent/_vendor/ripgrep/rg`，保留 Unix 可执行权限。包含原生二进制的 wheel 必须使用正确的平台标签，不继续作为 `py3-none-any` 发布；OS 最低版本、Linux 兼容标签和动态依赖以实际资产及安装测试为准，不能仅凭 target 名字猜测。没有匹配且已验证的资产时，不发布该平台包、不切回系统 rg、不临时源码编译。普通发行用户安装预构建平台 wheel/应用安装包，不需要 Rust、brew 或手动安装 rg。源码开发、sdist 构建与 PEP 660 可编辑安装必须先显式准备目标平台资产，再进入离线构建/安装；没有准备资源就明确失败。对这些源码路径不承诺无需准备的一步安装，也不能在 pip/uv 安装触发的 Hatch hook 中隐式下载 rg。

联网下载只发生在显式的发行/开发依赖准备步骤；现有 Hatch hook 对普通 wheel、sdist 与 editable 构建只离线验证/打包已经准备的目标资源，不联网、不执行二进制安装器。发布成品以平台 wheel/应用包为普通用户入口；sdist 不携带某台构建机器的一份架构二进制冒充通用资源，使用 sdist 的构建者须先完成同一显式准备。准备时校验固定资产与上游 `.sha256`；资产名和固定校验值属于真实外部二进制的内容完整性边界，可以作为最小依赖锁定输入。下载和校验失败则停止构建，不重新解析 latest 或更换镜像/版本兜底。使用既有打包设施和标准解包机制，校验值不扩展成逐文件代码 hash、运行时 fingerprint registry、安装回执或持久任务。安装及应用运行不联网下载 rg，不在首次搜索时下载，不在 Pulsara home 创建版本缓存。依照上游 [COPYING](https://github.com/BurntSushi/ripgrep/blob/15.2.0/COPYING) 选择 MIT 分发，并随包保留该版本的 LICENSE-MIT、版权及适用的上游第三方 notices。

唯一小路径适配从已安装的 Pulsara 包资源定位可执行文件绝对路径；支持标准 wheel 安装后的真实包目录与 PEP 660 的源码目录，返回在调用及其子进程生命周期内稳定存在的路径。复用标准包资源/包根定位，不把 importlib.resources.as_file() 的临时路径在 context 退出后交给搜索或加入长期 PATH；不新增临时解包缓存。zip-import 等没有稳定可执行路径的布局明确报告不可用，不属于本次发行支持范围。绑定当前发行目标，不接受模型参数、工作区文件、用户设置或 PATH 替换。复用现有资源路径与 subprocess owner，启动/首次使用检查文件存在、可执行及版本是否为 15.2.0；该检查只是依赖可用性判断，不充当身份或权限证明。不修改系统或 Pulsara 主进程的 PATH，不创建全局 rg 命令。两个 built-in 搜索工具直接调用私有绝对路径；Pulsara 的 terminal 子进程则按第 2.2 节获得局部 PATH，默认可以直接输入 `rg`。用户在 Pulsara 外打开的终端不受影响。

调用使用 argv 数组和绝对 executable 路径，不经过 shell，显式 `--no-config` 禁用 RIPGREP_CONFIG_PATH 对命令选项的注入，颜色等机器输出选项由工具固定。内容正则使用 ripgrep 默认 Rust regex 引擎，不启用 PCRE2 或自动切换引擎。文件发现沿固定 rg 的默认 ignore、隐藏/二进制过滤和不跟随 symlink 的规则；项目 ignore 文件仍是查询输入的一部分，wcmatch 不扩大该集合。不增加第二套文件枚举、shell glob 展开或自定义 ignore 解析。

私有依赖缺失、损坏、架构不匹配、无法执行或版本不符，均沿已有工具错误结果与可见失败链路说明“Pulsara 内置搜索依赖不可用，请修复/重新安装 Pulsara”。保留具体可诊断错误；不能用 Python/系统 rg 降级，不能伪装成功空结果，不能指示模型自行下载或改装依赖。权限判断、取消、单次 subprocess 执行边界与 side-effect settlement 复用现有 owner，不因打包添加新事件、表、job 或恢复机制。


### 2.2 Pulsara terminal 的进程级 rg 可用性

随包携带的同一个 ripgrep 同时提供给 Pulsara 内置 terminal。正常安装下模型或用户可在该工具中直接执行 `rg --version` 或普通 rg 命令，无需知道私有文件位置，也无需先检测/安装系统 rg。用户在应用外打开的终端仍沿自己的 PATH；安装 Pulsara 不承诺那里自动出现全局 rg。

复用 `terminal_process/environment.py` 的 `TerminalEnvironmentOwner.build()` 与 `terminal_process/manager.py` 的现有 env 传递链。在已有 shell 快照、项目 venv 和用户 PATH 组合后，把第 2.1 节同一个资源路径适配得到的私有 executable 父目录放到最终子进程 PATH 最前面，按既有机制去重、用 os.pathsep 拼接。不能只依赖当前排在 venv 后面的 extra_path_prepends，使项目中的另一个 rg 抢先解析；不新增通用依赖注入框架、用户配置开关或第二份 rg。私有目录只提供随包 rg，其他命令继续沿原有 venv/用户路径顺序查找。路径含空格时仍通过环境字段传递，不把 PATH 拼进 shell command 文本，也不改写 shell 启动文件或注册 alias/function。

覆盖主 agent、subagent 及前台/后台 terminal 的既有进程创建路径；它们都从同一环境 owner 获取值，terminal 启动的普通子进程继续继承该 PATH。单次 workdir 或 cd 不取消私有目录注入，也不改变会话工作目录。只修改传给这些子进程的 env 副本，不改 os.environ、用户配置或系统环境，不把模型凭据加入 env。仅把 rg 提供给 terminal 不扩大其权限：命令仍经过 terminal 既有授权、进程生命周期与结果结算链路，不自动获得 built-in search_content 的只读判定。

terminal 仍是用户控制的通用 shell：显式重设 PATH、使用绝对 executable 路径或自行定义函数可以选择别的 rg；这里保证默认启动环境提供随包命令，不保证任意 shell 配置/用户命令都无法覆盖它。terminal 的 rg 参数、输出、ignore 和配置文件由调用方及 ripgrep 本身决定，不强制套用 built-in 搜索的 --no-config、分页或 JSON 输出合同；两个专用搜索工具始终使用私有绝对路径，不受 terminal 环境修改影响。

TerminalEnvironmentOwner.build() 在私有路径解析/依赖检查的窄边界捕获此类依赖错误，不把它抛给 manager.execute() 而变成整条 terminal preflight ERROR。失败时不注入无效目录，保留原有 shell/venv/用户 PATH，并在现有 TerminalEnvironment.diagnostic 与 terminal 结果的 shell_diagnostic 中记录具体依赖不可用原因。不能吞掉其他环境构建错误或更改它们的既有失败语义；两个专用搜索工具仍返回搜索依赖错误。terminal 本身可用于其他操作，不触发下载、修复安装、系统 rg fallback 或包装命令。此时普通 shell 仍可能按原有 PATH 找到系统 rg，那是 shell 行为，不是 Pulsara 的搜索降级，也不算随包依赖可用性通过，不能用它掩盖打包错误。验收在未主动覆盖 PATH 的正常安装环境中证明直接 rg 命中随包版本，并分别验证错误依赖的诊断；不在此新建命令拦截器或 shell 解析器。


## 3. `search_content` 输入契约

公开字段仅为：

| 字段 | 类型与默认 | 语义 |
| --- | --- | --- |
| `pattern` | 必填非空 string | 内容正则；使用固定 ripgrep 的默认 Rust regex 能力，不增加 PCRE 开关、自动字面量猜测或新的正则方言。 |
| `path` | 非空 string，默认 `.` | 获准的单个文件或搜索目录。相对路径从当前会话绑定的 workspace_root 解析；terminal 单次 workdir/cd 不改变它。 |
| `file_glob` | 可选非空 string | 只让匹配此文件 glob 的文件进入结果；使用第 4 节的同一个规则。不承诺搜索引擎从未读取未选文件。 |
| `limit` | integer，默认 50，1–1000 | 当前结果页的条目上限；继承已有 DEFAULT_SEARCH_LIMIT/MAX_SEARCH_LIMIT，不是总搜索数或会话限制。 |
| `offset` | integer，默认 0，最小 0 | 当前结果形式的零起始结果偏移。 |
| `output_mode` | `content|files_only|count`，默认 `content` | 匹配行、包含匹配的文件、各文件的匹配行计数。 |

schema 和运行时检查都拒绝 `target`、`glob` 与未声明字段；不增加旧调用翻译。无效正则按既有工具错误链路报告具体原因，不作为“无匹配”。`file_glob` 始终生效，包括 path 是单文件时；不匹配则成功返回空结果。

内容搜索的 `files_only` 仍然按内容决定哪些文件入选；它不会变成按名称查找。`count` 统计匹配行，一行含多个匹配仍计一行，保持 rg `-c` 的匹配行计数语义。

## 4. `find_files` 输入与 glob 契约

公开字段仅为 `glob`（必填非空 string）、`path`（默认 `.`）、`limit`（默认 50，1–1000）和 `offset`（默认 0，最小 0）。path 可以是单个文件或目录。只返回文件，不返回目录，也不读取文件正文来判断匹配。

拒绝旧 `pattern`、`target`、`file_glob`、`output_mode` 和其他未知字段。不将文件名片段自动补成通配符：`settings.py` 表示确切名称，`*settings*` 才表示片段。

共同规则固定为：

- 使用 `wcmatch.glob.compile(pattern, flags=GLOBSTAR|MATCHBASE|FORCEUNIX|CASE|DOTMATCH)` 得到现成 WcMatcher，在本次调用内复用其 `.match(relative_posix_path)`。输入只有一个模式字符串，不开放 flags、pattern 数组或 exclude 字段；不启用 REALPATH、BRACE、EXTGLOB、NEGATE、SPLIT、GLOBTILDE、FOLLOW 或 GLOBSTARLONG，不调用该库的 glob/iglob 遍历 API。SDK 默认的多模式展开限制不成为本产品的文件数/历史数上限，因为本合同没有多模式或展开。
- 没有 `/` 的模式匹配文件 basename，目录搜索时可选中任意深度的同名文件。例如 `*.py` 查找各级目录中的 Python 文件。
- 包含 `/` 的模式匹配引擎发现的候选路径相对于已解析查询 path 的 POSIX 相对路径，而不是工作区相对展示串。path 是单文件时，相对匹配基准为其父目录，但候选仍只有该文件。目录分隔符统一写 `/`，反斜杠沿 SDK 的转义语义，不按当前操作系统猜测另一种模式。
- `*`、`?`、字符类不跨 `/`；单独目录段 `**` 匹配零个或更多中间目录。`src/*.py` 只匹配 src 的直接子文件，`src/**/*.py` 也匹配更深子文件，`**/*.py` 包含查询根的 Python 文件。DOTMATCH 只使已发现的隐藏名称可匹配，不改变私有 rg 的发现、ignore 或 symlink 规则。启用的 SDK 基础语法保持其原义，不另造 shell/ripgrep/Codex 的完整兼容承诺。
- 模式是候选过滤器，不是新的文件读取根；不能用它扩大 path 的读取权限。

`wcmatch` 的 [官方 compile/WcMatcher API](https://facelessuser.github.io/wcmatch/glob/#globcompile) 与实际 10.1 实现已核对，并在隔离 uv 环境实测上述名称/单层/递归模式；依赖尚未加入生产代码。matcher 的生命周期只覆盖当前调用，无持久 compiled-matcher registry。

两个工具共用此 matcher 和唯一私有 rg 后端。find_files 使用 `rg --files` 发现候选，再统一过滤、排序和分页，不给 rg `-g` 或 Python fnmatch 再次解释公开模式。search_content 在获准查询 root 上使用 rg 内容搜索，再对结构化结果按候选相对路径做共同 glob 过滤，之后统计、排序、分页。Python 不再另行遍历或搜索正文。不得把所有候选塞入一个 argv、添加 batch 框架、建立第二套文件 inventory 或持久缓存。

内容 rg 适配必须用 `-e pattern` 和 `--` 分隔路径，避免内容模式被当作命令选项；内容行使用已有 rg 的 JSON 输出和标准 JSON 解码获得路径、行号及行正文，files_only/count 可沿原 `-l`/`-c` 路径并使用 rg 的 NUL 文件名分隔。find_files 使用 `rg --files --null -- <path>`；count 单文件查询也强制携带文件名。不能因文件名含冒号或换行而静默丢弃、拼错结果，不能用 stdout 文本截断推断 total_count。只适配依赖公开输出，不重写正则引擎或供应商解析器。

最小调用：

```json
{"glob":"settings.py","path":"src"}
```

```json
{"glob":"*.py","path":"src","limit":50,"offset":0}
```

```json
{"glob":"src/**/*.py","path":".","limit":50,"offset":0}
```

```json
{"pattern":"LocalSettingsStore","path":"src","file_glob":"*.py","output_mode":"content"}
```

## 5. 结果、排序和分页

成功结果保留 `status:"ok"`、`total_count`、`truncated`、`access_scope`、`workspace_relative`，以及已有 `_hint` 提示路线；不再返回 `target`。内容搜索保留 `output_mode`。路径继续沿已有 `_relpath` 规则输出，不能把路径展示变化引入此次切分。

| 操作/结果形式 | 条目载体 | 排序与分页单位 | `total_count` |
| --- | --- | --- | --- |
| find_files | `files` 数组 | 文件路径 | 所有匹配文件数 |
| search_content/content | `matches` 数组，每项保留 `path`、`line`、`content` | 文件路径、行号 | 所有匹配行数 |
| search_content/files_only | `files` 数组 | 文件路径 | 所有包含匹配的文件数 |
| search_content/count | `counts`：路径 → 匹配行数 | 文件路径；一项为一个文件的计数 | 所有文件的匹配行数之和 |

统一采用输出路径的大小写敏感字典序；content 在同一路径内按行号升序。不再依赖遍历顺序或修改时间，同一查询的候选使用该确定排序。排序后再取 `[offset:offset+limit]`。

count 也分页：`counts` 只装本页文件计数，`total_count` 是 glob 过滤后此次搜索的全量匹配行总数；`truncated` 由是否还有文件计数项决定。它的 offset 单位是文件，而不是匹配行。所有结果形式的 `truncated` 只表示页后仍有条目，不能表示本页包含整个结果集；前端摘要在 truncated 或输入 offset>0 时说明本次只返回部分结果，不增加响应 cursor 或分页状态。count 与 files_only 都不重复返回匹配行正文。

空匹配与越过末尾的 offset 都是成功空页；是否 truncated 依实际后续条目判断。截断时 `_hint` 给出精确下一页 offset（当前 offset 加本页条目数）；不存在持久 cursor、快照或跨调用一致性承诺。文件变化可能导致页间移动，模型必要时重新查询。不能在响应截断后声称目录或搜索结果完整。

保留当前正文展示长度及普通大结果投影/artifact owner。不增加总文件数、总匹配数、总扫描字节数或生命周期调用次数上限。当前 60 秒 rg 单次 subprocess timeout 属于保留的每操作执行边界；不扩展成任务总时长限制。

## 6. 权限、错误与读取观察

两者复用 WorkspaceTool 的只读路径解析和 tool permission owner，并分别注入同一会话已有的 frozen Pulsara home / user home resolution。相对 `../`、绝对路径、`~` 和字面的 `${PULSARA_HOME}` 路径前缀沿当前合同，可以读取获准的具体外部文件/子目录；工作区外 `/`、home、临时顶层等宽泛根沿 `_is_broad_search_root` 拒绝。不能把读工具错误地收紧成仅项目内，也不能扩大 home/external 权限。参数中的 path 经 owner 解析，glob 不另行寻址，也不从 terminal 环境重新解析 home。

保留 FILE_NOT_FOUND、SEARCH_ROOT_TOO_BROAD 等既有应用错误与修复提示。权限拒绝、错误模式、物理搜索错误、超时或取消均沿现有结果/续行机制表达，不伪装成功空结果。glob 委托 SDK，不另写语法 validator；SDK 报错沿普通工具错误链路，未启用语法不扩展为额外查询模式。内容正则由实际引擎验证，即使 glob 恰好排除全部结果，无效内容正则也不能作为成功空页。文件变化和无法读取等个别项沿 rg 的退出码与诊断表达；有部分输出但返回错误码时仍报告搜索失败，不把部分结果描述成完整成功；本轮不宣称搜索是磁盘的一致完整快照。

两项结果都不能登记为 `read_file` 的内容读取观察，不能赋予编辑所需的 content_revision / seen-line authority。模型仍须真正读取目标后编辑。

既有连续重复查询提示和阻断属于现存行为，本轮不增加次数或生命周期限制。沿原 `_track_lookup` 保存进程内状态，key 显式区分新工具、真实生效参数、路径和页；页变化、模式变化、两个工具的调用不得被混为同一个查询。没有新表、事件、subject、append guard、关系、job、nonce、fingerprint 或恢复机制。

## 7. Hook 与前端

在唯一 `hooks/matcher.py` 中增加 `search_content` 的 `Grep` 别名、`find_files` 的 `Glob` 别名。所有 search_content 结果形式都属于内容搜索；count/files_only 也可匹配 Grep。find_files 才匹配 Glob，不能再靠 target 推导别名。原始 matcher 保持 RE2 和原字节，不做正则文本替换或语义猜测。

本文单独拥有的搜索切分范围只为两项新工具提供原生名字/参数及 Grep/Glob 匹配候选；不在本文另定其他工具的 Hook 输入。联合实施时按下游 Hook 设计统一将所有工具事件的 public tool_name 改为原生实际名字、删除 pulsara_tool_name，并采用其唯一闭合别名表及支持输出合同。别名只参与 matcher candidates，不改写脚本 stdin；Grep/Glob 名称选择兼容不保证供应商脚本参数或输出兼容。Read 等其他别名、Hook 确认页和安装说明的改动由下游规格拥有，本文不保留与其冲突的旧 public name 路径。

增加别名会改变已有 Hook 的工具匹配语义。此次 hard cut 将 `hooks/trust.py` 已有 `TRUST_DIGEST_CONTRACT` 从 `pulsara.hook-definition-trust.v1` 改为 `pulsara.hook-definition-trust.v2`；该值已经属于 normalized_definition_digest 的跨重启语义确认边界，不新增持久字段、版本 token、registry 或 generation。联合首次发布的 v2 同时包含下游 Hook 的最终原生输入、别名和控制语义，统一使旧 Hook trust 失配，不能自动重新授信或兼容读取 v1 digest。具体激活及 disabled 语义见第 8 节。

前端工具摘要分别显示“搜索内容”和“查找文件”；副标题取 pattern 或 glob 与 path，结果显示匹配行/包含匹配的文件/找到的文件，按实际 output_mode 区分。本文不新增 Hook UI 框架；联合实施的确认页与匹配范围展示按下游 Hook 设计完成，复用生产 matcher，不复制第二套别名或语义解释器。

## 8. Hard cut 与激活

生产代码一次性删除 `search_files` descriptor、SearchFilesTool、target 分支、旧注册/导出、旧查询恢复分类、前端标签和隐藏转发；同时删除 which("rg")/系统 PATH 选择、Python 正文搜索与文件发现 fallback，以及仅服务旧 fallback 的 fnmatch/遍历代码。新工具复用并整理原帮助函数，不把旧混合执行器留在新 wrapper 下。旧输入不得作为新工具别名继续调用。

工具根只在既有 cold epoch 或明确采用的 compaction successor 重建。禁止在已有 epoch 中替换 SYSTEM/tools 或改写历史工具请求与结果。部署时停止旧 runtime；本轮开发激活从干净新会话/新冷 epoch 开始，不承诺把安装着旧搜索 descriptor 的 live Host 在线转成新执行器，也不新造自动 rebase 边界。若本地旧数据库的会话恢复妨碍 clean-v0 验收，按 AGENTS.md 验证本机 disposable PostgreSQL 目标后可重置；不为旧内部 descriptor 增加迁移链。

Hook 信任激活顺序：停止旧 runtime，再以 v2 digest 合同和两个新 descriptor 启动新冷会话；不热更新旧进程的 matcher 或 digest。原 trust 文件、trusted_at、enabled 和 Plugin 包状态均不作迁移或批量改写。任何 home/project/Plugin 来源以后由既有 owner 加载时，都只计算 v2 当前 digest：enabled 的旧已信任来源成为 MODIFIED，不能执行；disabled 来源仍显示 DISABLED，保留原 enabled=false，随后仅开启也不能把旧 digest 变成有效信任。用户请求重新信任时，沿既有完整 GUI 审阅和 exact-definition revalidation 写入 v2 digest，trust 不顺带开启来源或 Plugin。无需枚举所有旧项目、建立 trust inventory 或逐一 revoke；重置 PostgreSQL 也不代表 trust 文件已被清空。

当前联合发布只切换一次 v1 → v2 并进行一次完整重新审阅。若以后明确改为先独立发布搜索 v2、后发布其余 Hook 语义，第二次变更必须修订为新的固定 trust contract 并同步两份规格，不让同一个 v2 表示两组语义；不添加 profile 协商、兼容 digest 或新旧输入双轨。

替换活跃 specs 与 examples 中的旧调用，不改写 archived_docs 历史。涉及工具表面、返回值、接线与依赖打包，不修改 relational clean-v0 schema；现有架构 oracle 中新增一个 builtin 是明确的两个工具替换一个工具，durability categories 应保持不变。

## 9. 实施清单

1. builtin_catalog：两项 descriptor、闭合参数、filesystem family、只读 recovery contract；文案讲清正则、glob、路径和页单位。
2. filesystem.py、pyproject/uv.lock：SearchContentTool / FindFilesTool；唯一 wcmatch 纯 matcher 和私有 rg 路径适配，共享路径、候选过滤、排序、分页及错误封装。删除旧类、无效字段分支、Python 搜索 fallback 与系统 PATH 探测，不新增遍历引擎。
3. tools/hatch_build.py / 现有发行构建：固定 15.2.0 资产与完整性校验输入，目标平台 wheel/安装包、私有 executable 及许可文件；源码开发准备同一资源。安装包必须独立包含搜索依赖，应用运行与首次搜索不下载；Hatch hook 离线，源码/sdist/PEP660 安装前显式准备资源，成品采用稳定真实包路径。
4. tool_runtime.py / builtins/__init__.py：原生新类注册和导出；terminal_process/environment.py、manager.py 复用现有环境 owner，在最终 terminal 子进程 PATH 前置同一私有 rg 目录，覆盖主/子 agent 与前台/后台，保持凭据过滤和授权边界；仅私有 rg 依赖错误转为现有 shell_diagnostic，其他 terminal 命令继续可用。
5. hooks/matcher.py、hooks/trust.py 与 Hook 参考说明：两项有限别名、原生 stdin 合同和既有 digest contract 的一次 v2 hard cut；不增加信任迁移机制。
6. frontend/lib/builtin-tool-summary.ts 及相关测试：名称、参数摘要、结果单位与分页提示。
7. 当前根目录 PULSARA_FILE_PATH_IMPORT_IMPLEMENTATION_SPEC、PULSARA_ARTIFACT_EXPORT_IMPLEMENTATION_SPEC；内置 Skills、README 和其他 active references 按实际引用替换。
8. tests/test_content_revision_line_edit.py、test_stage2_live_contract.py、frontend runtime-adapter fixtures 的接线。没有旧入口的活动代码或 agent prompt。

## 10. 验收

联合实施时先完成本文及下游 Hook 设计要求的非真实会话检查，再由同一 GPT-6.1 Sol / xhigh critic 审阅完整代码与证据；修订后无阻塞再跑真实 Pulsara GUI dogfood。实施阶段与本文编写阶段分开。

- schema：两项正确 descriptor；错字段、错类型、负 offset、空 pattern/glob 和失效旧入口被明确拒绝，不静默忽略参数。
- 行为：内容正则、三种结果形式、glob 精确名称/片段/相对路径、`src/*.py` 单层与 `src/**/*.py` 零层/递归、`**/*.py` 根文件、单文件过滤、同名不同目录、空匹配、末尾页和真正下一页；count 行计数和文件分页单位必须单独证明。覆盖前导 `-` 的内容正则以及冒号/换行文件名，不能靠旧分隔解析丢弃条目；glob 排除全部结果也不掩盖无效正则。
- 私有 rg：PATH 中无 rg 或存在不同版本/伪造 rg 都不影响两个工具；RIPGREP_CONFIG_PATH 指向改变输出/搜索范围的配置时也不影响显式工具合同。验证默认正则、ignore、隐藏/二进制及 symlink 行为，所有结果形式复用同一后端；没有 Python 搜索 fallback。
- 打包：在每个实际发布平台从成品 wheel/安装包的干净安装运行 rg --version 与两项 built-in 搜索，覆盖路径带空格、Unix executable 权限、平台标签与许可文件；不依赖系统 rg/Rust，也不在安装或首次查询下载。缺失、损坏、错误架构/版本及依赖准备校验失败均明确失败；不发布未经验证的平台资产，不能以模拟测试替代相应目标的成品安装验证。另证显式准备前的源码/sdist/PEP660 构建离线失败、准备后的离线安装成功，无安装 hook 隐式下载；包资源路径在后续调用中有效。
- terminal rg：系统 PATH 无 rg、含不同版本 rg、项目 venv 含另一份 rg 时，默认命令解析均命中随包私有版本；覆盖路径带空格、环境快照启用/禁用、workdir/cd、主/子 agent、前台/后台及普通子进程继承。既有非 rg 命令顺序、凭据过滤、终端权限和生命周期保持；父进程/系统 PATH 及 shell 启动文件不变。依赖错误须进入现有 shell_diagnostic，并证明其他命令仍实际执行，不能让系统 rg 偶然可用代替私有依赖验收；显式 shell 环境覆盖属于调用方行为，不影响两个 built-in 搜索工具。
- 权限/观察：READ_ONLY 下两项可执行；获准外部具体路径与宽泛根拒绝；两者均不授权后续编辑；没有新的 scope 或写 effects。
- Hook：`^Grep$` 只匹配 search_content，`^Glob$` 只匹配 find_files；原生名仍可匹配；public tool_input 保持本工具原生参数。用既有 trust owner 的旧 v1 state 证明新进程拒绝执行旧信任；覆盖 local/Plugin、USER/WORKSPACE 及稍后才加载的来源，enabled/包状态保持不变。enabled 旧来源为 MODIFIED，disabled 旧来源保持 DISABLED，重新开启不等于重新信任；完整重审后记录 v2，不接受 v1 fallback。
- 前缀/持续运行：同一 epoch 的 SYSTEM/tools 不变，messages suffix-only；新冷会话仅提供两个新搜索工具。多页、多轮交替查询不受新总量上限影响，重复查询状态不跨工具串扰。
- 静态与集成：适量 Python focused tests、受影响的 PostgreSQL/架构 oracle、前端 summary/runtime-adapter tests、Ruff、TypeScript、现有协议/构建检查；不靠删断言或更新 oracle 隐藏旧入口残留。
- 真实 dogfood：读取保存生产设置，绑定隔离项目，GPT-6 Luna / xhigh；自然语言分别要求找到目标文件、查内容、找含匹配的文件及读取 count 下一页；观察模型直接选工具，没有 target，没有名字猜测或重复首屏。还须验证直接 terminal rg；使用已有 GUI 一次完成联合最终 Hook 契约的完整审阅，并覆盖下游要求的原生输入、Read/Grep/Glob 与控制行为，仅声明实际完成的验证。

## 11. 审阅记录

以下早期记录对应当时版本；当前联合复审结论见本节末尾，不将历史“未复审”状态作为当前状态。

同一 GPT-6.1 Sol / xhigh critic 已核对当前 builtin、执行器、WorkspaceTool、Hook matcher/trust owner 与前端摘要，无剩余实施前阻塞。此次审阅修订：以既有 trust digest 的一次 v2 hard cut 取代无法覆盖未来旧项目的批量 revoke；用已核对的 wcmatch 纯匹配 API 保留单层/递归相对路径 glob；明确内容 glob 的结果过滤边界、rg 参数/输出适配、分页单位及 frozen path/home 复用。没有修改运行时代码、应用依赖或其他文件。

主 agent 最终复审通过。四个 JSON 调用示例已验证格式；在临时目录实测 rg 的单文件发现、files_only、count、JSON 内容输出，覆盖前导 `-` 正则、带冒号/换行的文件名和按匹配行计数，均符合第 4 节约定。这是文档所依赖 API 的核对，不是新工具的实现测试。冻结意味着可以据此实施，不代表实现、用户重新信任或验收已完成；第 10 节仍是实施完成后的激活门槛。

2026-09-30 用户追加决定：Pulsara 随发行包携带固定 ripgrep 15.2.0，内置搜索采用单一私有后端；新增第 2.1 节并同步输入、执行、hard cut、实施清单及验收，删除原设计中的 Python fallback 保留要求。已核对该版本官方资产、校验文件与许可说明；本次仅修订本文，没有下载/安装二进制或修改构建及运行时代码，也未开展平台安装测试。本次修订未经过 critic 二次审阅，先前审阅结论只对应当时版本。

同日追加 terminal 边界：按用户认可的方案新增第 2.2 节，Pulsara terminal 的子进程 PATH 默认前置同一私有 rg 目录，应用外终端与系统全局 PATH 不变；同步实施与验收，明确 terminal 的 shell 语义和权限不等同于专用搜索工具。依据现有 TerminalEnvironmentOwner/manager 接线完成文档校对，未修改运行时代码，未运行 terminal 集成测试，也未进行 critic 二次审阅。

同日联合审阅准备：明确本文与 Hook 下游设计的 owner 分工、唯一别名表、全部原生 Hook 输入和一次完整 v2 信任切换，消除“保留其他工具 public name”与联合 hard cut 的冲突；同步联合验收与直接 terminal rg。此次只修改文档，联合 critic 审阅进行中。

2026-09-30 同一 GPT-6.1 Sol / xhigh critic 联合复审通过，无剩余实施前阻塞。修订闭合：上游/下游 owner 和唯一 Hook 输入/别名；一次完整 v2 信任切换；Hatch 离线构建与源码/sdist/PEP660 显式准备；稳定包资源路径；terminal 只对 rg 依赖失败记录现有诊断并继续其他命令；下游精确输出解析顺序、异步控制诊断、项目共享范围及 Host/MCP 派生匹配观察。主 agent 已按意见修订并完成 JSON 示例、两文档本地链接及 diff 空白校对。文档审阅不代表构建、运行代码、平台安装测试或真实 dogfood 已完成；除第 1.1 节既有元数据减法外，其余目标仍待用户决定实施。


## 12. 联合实施记录

2026-09-30，实施前工作与冻结规范已提交为 `ab16b002`。随后联合删除旧搜索入口、Python fallback 和系统 rg 探测，接入两个闭合工具、wcmatch 纯 matcher、固定私有 rg、terminal 子进程 PATH、原生 Hook 输入与最终别名表、一次 v2 信任契约及派生审阅清单；同一 critic 代码复审通过。没有新增 relational schema 或 durability 类别，没有热改已有 provider prefix。

打包目前只开放已实测的 macOS arm64，平台 wheel 标签为 `py3-none-macosx_11_0_arm64`。Hatch 验证真实架构、最低系统版本与固定 rg 版本；包内包含 MIT 与 PCRE2 notices。显式准备工具可选择其他上游资产，但这些目标尚未经过 Pulsara 成品安装验证，禁止发布。源码先 `uv venv`，显式准备资源，再 `uv sync --locked`；构建 hook 不下载。路径含空格的干净成品安装已运行实际 rg、两个内置工具与裸 terminal rg，均通过。

最终非真实会话检查：Python 全量 2444 passed（包含 PostgreSQL 与架构检查）；前端 35 个文件、587 passed；受影响 Python Ruff、TypeScript、terminal 协议生成检查、前端 production 构建、Hatch wheel/sdist 与 `git diff --check` 通过。依赖未准备的 wheel/sdist/editable 离线失败、准备后的构建成功，以及路径含空格的干净成品安装均有实际检查。

真实 GUI dogfood 通过：新 runtime 读取保存生产设置，隔离项目 `/Users/plumliu/Desktop/test1/search_split_20260930`，GPT-6 Luna / xhigh。用户完成原生目录选择后，在会话 `6395e367` 通过 GUI 一次审阅全部 3 条定义并提交信任；确认页显示项目共享范围、完整命令、实际适用操作和原生输入边界。随后在同项目新冷会话 `2736bf6e` 完成自然语言验收：

- `find_files` 使用 `glob=config.py,path=src`，返回 `src/config.py` 与 `src/deep/config.py`。
- `search_content` 使用 `pattern=needle,path=src,file_glob=*.py`；content 返回 4 行，files_only 返回 3 个文件，count 每页 1 文件，offset 依提示为 0 → 1 → 2，计数依次 2、1、1，全量 `total_count=4`；最后一页 `truncated=false`，前端仍显示部分页提示。
- 实际 `read_file` 读取 `src/config.py`；Read/Grep/Glob 代表 Hook 均触发，脚本记录的名字为 `read_file/search_content/find_files`，参数原生且没有 `target` 或 `pulsara_tool_name`。
- 同一新会话的无 stdin SessionStart 写出 `SESSION_START_NO_STDIN`；terminal 裸 `rg --version` 输出 15.2.0。
- `printf PULSARA_BLOCK_ME` 的 PreTool Hook 收到 `tool_name=terminal` 与原生 `command`，stderr 为 `dogfood native command blocked`、退出码 2；GUI 显示未获授权且未执行，模型没有绕过拒绝。

实际脚本输入与搜索结果保存在隔离项目 `.pulsara/events.jsonl`，启动输出在 `.pulsara/start.log`；GUI 工具详情与日志已交叉核对。此验收仅覆盖本机 macOS arm64，不扩张其他平台的发行声明。


真实 dogfood 发现并修复一处 P2 前端提示问题：后端 capability 表单正常关闭发送 `SUBMITTED`，adapter 曾仅将 `RESOLVED` 视为正常，因此误报未知结束原因。现在 `SUBMITTED` 静默收尾，`CANCELLED` 显示普通取消，未知原因继续提示；不把提交当成配置已应用，也没有新增协议、缓存或事件。三个回归通过，runtime-adapter 共 80 项通过，最终前端全量 587 项与 TypeScript/构建通过，同一 critic 对窄修订复审无阻塞。重新加载前端后，在会话 `2736bf6e` 对同一份已信任定义再次提交 GUI 审阅，实际只显示正常提交且配置已应用，错误提示未再出现；未撤销信任、修改定义或再次切换 v2。
