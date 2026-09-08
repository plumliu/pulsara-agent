# Pulsara 浏览器修复 PR01：原文保真 Hard Cut 实施规格

状态：**READY_FOR_IMPLEMENTATION，尚未实施**。

日期：2026-09-08。

索引：[浏览器 dogfood 复盘与修复方案](PULSARA_BROWSER_DOGFOOD_REVIEW_AND_FIX_PLAN_2026-09-08.zh.md)。对应问题：**F07**。代码调查基线：`7e2ec332`；实施时须重新核对工作树，不以行号或旧测试结果代替当前代码。

本文件确定第一个 PR 的完整范围，不代表授权执行代码修改。本轮交付为规格文档；未来实施完成前不得标记 ACTIVATED。

## 1. PR 目标和拆分决定

PR01 只完成一个产品边界：**前端可以翻译自己生成的界面标签，不得翻译、替换或改写承载原文的内容字段。**

目标结果：模型生成的代码、命令、自然语言、思考展示、方案、子任务正文和工具原文，不再被 `productVisibleText` 全局正则替换；回复复制保留同一原文，审批视图不再展示被中文化改写的计划。

这是索引 Batch A 的第一项独立 hard cut。Batch A 是优先级批次，不再要求全部问题挤进同一个 PR。先修最高优先级的 F07，为后续结果详情提供可信的内容输入；本 PR 不依赖 live 关联、队列或停止协议的改造。

这不是“先保留旧实现、以后再切换”：PR01 必须同时删除全部原文中文化入口。按产品边界拆 PR，不保留同一边界的新旧双路径。

建议 PR 标题：`fix: preserve source text across conversation and plan views`。

## 2. 范围锁定

### 2.1 必须纳入

1. 删除通用正文术语替换器及其所有生产调用。
2. 主会话 assistant 正文、已有可展示 reasoning、子任务正文/结果/进展、TODO 文本、规划问题/选项/方案正文保真。
3. 工具已有 `resultText` 和被展示的原文片段不再做术语翻译；明确区分它们与产品生成的摘要。
4. 复制回复使用未经语义改写的源正文；现有 Markdown 渲染与复制数据分离。
5. 保留已有 typed UI label 中文映射，不把整个界面变成底层枚举或英文。
6. 更新测试、必要的说明及实际随 Python 包发布的前端构建产物。

### 2.2 明确不纳入

| 索引项/能力 | PR01 边界 |
| --- | --- |
| F01 工具详情 | 不新增完整 diff/JSON/artifact 浏览器；只保证已有原文片段不被词汇替换 |
| F02 live 结果关联 | 不重写 result draft/trace 关联，不改 settlement |
| F03 队列及去重 | 不新增 pending 区，不修改 optimistic 去重与队列身份 |
| F04 历史计划 | 不增加历史查询接口、已批准计划入口或 fork 计划权限 |
| F05 取消 toast | 不顺手修取消规划文案；不是本 PR 的完成条件 |
| F06/M01 停止 | 不修改 STOP 协议、turn 目标或后台进程/自动续轮策略 |
| M02 detach 确认 | 不修改确认寿命、接管或重连策略 |
| M03/M04 文件工具 | 不改 descriptor、revision、seen observation、replace_file 例外 |
| 通用错误信息治理 | 不重做全部 toast 错误详情、全站 i18n、凭据脱敏系统 |

上述问题继续由索引跟踪。PR01 不得宣布整个 Batch A 完成，也不得为了通过本规格顺带实现上述机制。

## 3. 当前代码真源与根因

主要文件均从仓库根目录起算。

| 位置 | 当前事实 | 实施动作 |
| --- | --- | --- |
| `frontend/lib/runtime-adapter.ts:3189` | PRODUCT_TEXT_REPLACEMENTS 包含 read_file、ROOT、read-only、exit_code 等替换 | 整体删除，不迁移为另一个通用正文过滤器 |
| 同文件3229、3236 | productVisibleText 与 productVisibleMessage 对整段自由文本运行正则 | 删除函数与导出；移除 `.map(productVisibleMessage)` |
| 同文件1380–1425 | plan question、选项、description、完整 draft 被改写 | 读取解码后的原文直接传入内容模型 |
| 同文件1699、2906、2970–3045 | 子任务 live progress、TODO、任务清单/依赖/结果再次翻译 | 所有来源文本保真；只对枚举使用 label mapping |
| 同文件2741、2750 | 工具 message 和非 JSON 原文也经过翻译器 | 原文分支直接返回源字符串，不改 F01 的摘要选择结构 |
| `frontend/components/workbench-view.tsx:769–774` | Markdown 与 clipboard 都取 message.body | 保持单份正确源正文；补渲染/复制回归 |
| 同文件974 | plan draft 用 MarkdownBody 渲染 | 输入必须等于本次读取的 draft 原文，不改变批准对象 |
| `frontend/components/markdown-body.tsx` | ReactMarkdown/GFM/math/KaTeX 负责渲染，已有 normalizeMathMarkdown | 复用；不自建 Markdown parser，不把数学排版归一化写回源正文 |

已运行过的最小反例：

````text
模型原文：
```python
from api import read_file
ROOT = "read-only"
exit_code = 0
```

当前前端变成：
```python
from api import 读取文件
主任务 = "只读"
退出码为 0
```
````

这会使代码失真。复制回复取的是改写后的 body，因此不是纯视觉问题。plan 也经过同一变换，而决策仍绑定原始 draft，可能出现“看见的文本与批准的文本不一致”。canonical 和 provider 历史本身没有因此改变，不做数据修复或历史回填。

## 4. 权威与数据边界

### 4.1 “原文”的精确定义

本规格中的原文，是现有授权读取/协议投影交给前端的对应内容字段，经现有合法字节解码后的字符串。

- 不要求前端读取未授权数据、provider 隐藏思考或超出现有 preview/coverage 的内容。
- reasoning 仅保留当前生产链路已经允许展示的块及其 FULL/SUMMARY 语义，不另抓取更多 provider 字段。
- 工具原文可能是 canonical preview，不承诺自动取得完整 artifact。
- 不改变既有凭据保护边界；只禁止把术语中文化伪装成脱敏。不得新增宽泛技术词屏蔽规则。
- 不把用户输入框现有提交时 trim 的行为纳入本次修改；本次边界从已接受的内容字段开始。

可验证的不变量：对于一个已经投影给前端的源文本字段 `S`，其展示数据字段 `P` 满足 `P === S`。复制回复 `C === message.body === S`。用直接字符串/字节相等测试，不新增 hash。

Markdown 解析后的 DOM 不必逐字包含 Markdown 标记；例如粗体和公式可以被渲染。但渲染过程不得替换词义，代码内容不得被中文化，复制仍使用源 Markdown，而不是 DOM textContent 或排版归一化后的文本。

### 4.2 按字段分类，不按文本形状猜测

| 字段类别 | 例子 | 允许操作 |
| --- | --- | --- |
| 原文内容 | assistant body、reasoning body、plan draft/question、工具输出 | 合法解码、按现有规则渲染；不翻译、不改写 |
| 来源提供的名称/说明 | task label、display_role、objective、依赖摘要、TODO text | 保留来源文本；不能因字段叫 label 就认定是产品标签 |
| 产品自有枚举标签 | task status、permission mode、profile 默认名、tool 固定用途说明 | 复用已有有限映射，生成中文 UI label |
| 产品合成摘要 | “已读取 path · N 行”、按钮标题、等待提示 | 可以组合中文标签与原值；原 path/标识符不能被改写 |
| 来源错误正文 | 工具 message、terminal_public_detail | 原文保留；旁边可有按 typed 状态生成的说明，不扫描错误句子翻译 |

缺失值可以沿用现有默认值；不得对真实非空值做 trim、大小写转换、Unicode normalization、工具名替换或“去技术化”后再存回原文字段。使用 trim 判断是否显示一个空卡片，不等于允许改写该字段。

### 4.3 禁止增加的机制

- 不新增 `raw_body/translated_body` 双写、正文副本存储或 DTO fingerprint。
- 不增加事件、job、relation、subject、guard、checkpoint、原文注册表或 replay 修复。
- 不添加翻译开关、旧版 fallback、identity-wrapper 形式的废弃函数。
- 不新增 regex/AST“跳过代码块再翻译正文”的框架。行内代码、路径和精确引用也可能出现在普通段落。
- 不修改 SYSTEM、provider tools、messages、replay fragment 或 compaction 根。
- 不新增依赖；已有 TypeScript、React、ReactMarkdown 和测试工具足够。

## 5. 实施步骤

### Phase A：先补能失败的回归

从当前生产 adapter 构造真实协议形状的 deterministic fixtures，不只测试一个字符串工具函数。

1. 主会话 canonical assistant body 含最小反例，断言完整字符串相等。
2. TEXT_DELTA 分段跨过敏感词，再 TEXT_END，逐阶段断言只追加/采用实际源内容；不能因为分块边界改变翻译结果。
3. canonical reasoning 与 live reasoning 使用相同危险词样例，确认内容不被替换。
4. plan question、选项 label/description、分块 draft 均含这些词；断言 content 返回源字符串。
5. 覆盖 projectAgentTasks、projectTaskInventoryRecord、依赖结果、live progress、TODO 和子任务 activities，防止只修主回复而遗漏第二层投影。
6. 工具非 JSON 结果和 JSON message 字段断言文本相等，`resultText` 不变；不把“显示所有 JSON”加入本 PR。

保留当前回归中不相干的正确断言。原来要求全文被翻译的 productVisibleText 测试，须替换为上述保真测试和真正的 UI 标签测试；记录这是语义 hard cut，不是删断言求绿。

### Phase B：一次切断全部原文翻译路径

1. 删除 PRODUCT_TEXT_REPLACEMENTS、productVisibleText、productVisibleMessage，包括导出和测试 import。
2. project() 返回已构造的 messages，不再整树“产品化”映射。
3. 替换所有调用点：原文直接传递；枚举沿用 taskStatus、profileLabel、toolDisplayName、toolFailureLabel、permission 现有映射。
4. `display_role` 有来源值就保留；只有缺失时才由 profileLabel 产生默认中文。不得把来源自由文本强转成某个枚举。
5. formatToolResult 中来源 message/plain text 分支直接返回源值，保留原有产品摘要分支。本 PR 不拆分 summary/details 数据模型。
6. Plan 内容在完整分块接收后直接返回 body，不改 chunk identity/offset/digest 校验、interaction revision、owner gate 或 resolve 请求。

预期主生产改动集中于 runtime-adapter.ts；只有测试证明 component 仍有同类原文替换时才修改对应 component。不要为了让 diff 好看机械重构整个 workbench。

### Phase C：复制、审批与标签回归

- 复制按钮继续复用现有 clipboard 路径。修复源 body 后若无需生产代码调整，只增加测试，不另建 rawCopyText。
- MarkdownBody 保留现有数学渲染兼容行为；该渲染临时字符串不能进入 RuntimeProjection、clipboard 或 plan resolution。
- 检查当前计划问题的选项 ordinal 和 draft approve/cancel/revise 请求仍引用原 interaction，不因展示修复重新提交 plan content。
- 验证工具中文用途、权限选项、任务状态、默认 profile label 仍可读。删除全文替换不能导致产品固定按钮变成底层代码。

`pulsara-app.tsx:122` 的 productMessage 是 toast 的信息筛选函数，不是本次正文变换器。它的“技术词/非中文错误退回默认提示”问题留给通用错误可观测性后续工作；本 PR 不改其现有调用，但禁止将任何正文/计划/复制路径接入该函数。

### Phase D：打包验证、真实 dogfood、交接

- 构建实际 Python 应用所用 static 资源，确认用户启动的不是旧 bundle。
- 使用 root uv/.venv 做本地验证；wheel/isolated launcher 仅使用临时安装环境，不覆盖用户全局安装。
- 完成第8节真实浏览器测试、记录证据后，逐项检查第10节。
- 只有全部验收满足才将本规格标记 ACTIVATED，并在索引中更新 PR01 状态；不改 F01/F02/F03/F05/F06 的完成状态。

## 6. 文件范围与删除清单

### 6.1 预期允许的变更

| 文件 | 目的 |
| --- | --- |
| `frontend/lib/runtime-adapter.ts` | 删除全文变换、修正所有内容投影 |
| `frontend/lib/runtime-adapter.test.ts` | adapter 保真与 typed label 回归 |
| `frontend/app/pulsara-app.test.tsx` | 复制、审批、任务展示和恢复路径 |
| `frontend/components/markdown-body.test.tsx` | 代码/行内文本不失真，数学现有能力不回退 |
| 对应 component/新专用 component test | 仅在必要时验证已有展示/复制路径，不重做 UI |
| `src/pulsara_agent/web_app/static/` | `build:local` 正常生成的发布资源，不手改压缩 JS |
| 本规格与索引 | 验收结果、状态、剩余已知问题 |

原则上不需要 backend/protobuf/数据库/descriptor 变更。若实施发现必须修改这些边界，先给出具体证据并修订范围，不自行搭建新协议。

### 6.2 必须删除

- 三个旧符号：PRODUCT_TEXT_REPLACEMENTS、productVisibleText、productVisibleMessage。
- 所有依赖上述函数改变原文词汇的生产调用。
- 验证“任意模型正文应被中文化”的旧测试语义，同时以更强的内容保真断言替代。
- 发布构建中被正常新产物替代的旧 JS/map 引用；由现有构建流程处理，不递归清理宽泛目录。

不留空壳包装器、不移动到别的文件继续运行、不新增 feature flag。现有 bundler 的文件名散列属于正常产物命名，不另建手工文件 SHA 或验收证据 hash。

## 7. 自动化验收矩阵

每个保真样例至少包含：`read_file`、`edit_file`、`ROOT`、`Kernel`、`HostSession`、`read-only`、`bypass-permissions`、`exit_code = 0`；组合中文、路径、URL、JSON、反引号和首尾换行。

| 编号 | 场景 | 必须断言 |
| --- | --- | --- |
| T01 | canonical assistant / imported assistant | body 与输入原文严格相等，不能只匹配某个关键词 |
| T02 | live TEXT_DELTA/END → canonical → snapshot/reconnect | 各时点可用原文不被替换；最终内容不重写/不重复 |
| T03 | reasoning FULL/SUMMARY、live/canonical | 仅已有可展示内容；字符串与 presentation kind 保留 |
| T04 | plan question + options | question/label/description 相等，选项决策 ordinal 不变 |
| T05 | plan draft 多 chunk、中文跨块 | 拼接原文相等；现有 offset/identity 错误仍被拒绝 |
| T06 | plan draft 渲染 → approve/revise/cancel | 视图代码原词保留，提交仍绑定原 interaction/revision；不额外写入 draft |
| T07 | 子任务两类 inventory、依赖、result、progress、activity | 所有来源正文和自定义名称保真，枚举状态映射仍正确 |
| T08 | TODO 文本 | 原词不变，勾选/状态投影不受影响 |
| T09 | 工具 plain text/JSON message、resultText | 原文分支严格相等；摘要可以存在但不改原字段 |
| T10 | 整条回复复制 | clipboard 调用参数等于源 Markdown，保留缩进/换行；失败提示仍有效 |
| T11 | Markdown fenced/inline code、math、HTML 类文本 | 代码不翻译，数学回归通过；保留 ReactMarkdown 安全渲染，不启用 raw HTML |
| T12 | 中文 UI 标签与未知来源文本 | 固定工具用途、permission、profile、status 中文可读；陌生自由文本不猜译 |

fixture 必须经过实际 adapter/component 接口。禁止把一个已经保真的 fake Message 直接塞给 UI 后，就宣称验证了 adapter。React 层可以用 fake adapter 测复制/交互，但必须与 T01–T09 的真实 adapter 测试互补。

## 8. 验证命令与真实浏览器流程

以下命令是实施时的要求，本规格编写阶段尚未执行这些验收。此前索引中的83项绿灯不是本 PR 的验收结果。

### 8.1 本地测试

在 `frontend/`：

```sh
npm test -- lib/runtime-adapter.test.ts app/pulsara-app.test.tsx components/markdown-body.test.tsx
npm test
npm run lint
npm run build:local
```

在仓库根目录：

```sh
.venv/bin/pytest tests/test_frontend_reasoning_projection.py tests/test_round3_1_provider_input_prefix_continuity.py -q
git diff --check
```

先确认根目录 uv 环境可用，不使用系统 Python 替代。若新增专用测试文件，须包含在 focused 命令及最终记录中。

本 PR 不要求无差别重跑全部后端数据库套件；后端投影/continuity focused checks 加前端全回归与实际发布验证是当前范围的门槛。不得为本 PR 修改后端语义后仍沿用这个较小门槛。

任何失败都要记录。任务相关失败必须修复；较大的无关基线失败单列并报告，不能添加 skip/xfail、弱化断言或假称该命令通过。未满足必需门槛时保持未激活。

### 8.2 发布资源与 isolated wheel

当前 `frontend/vite.local.config.ts` 将 build:local 输出到 `src/pulsara_agent/web_app/static`；`pyproject.toml` 将 `src/pulsara_agent` 打进 wheel。`tools/hatch_build.py` 负责 bundled Skill inventory 检查，不会替你重新构建前端。

实施顺序：

1. 先 build:local，再用 `uv build --wheel --out-dir <已创建的独立临时输出目录>` 构建 wheel。尖括号是需替换的实际路径，不直接照抄。
2. 临时目录由 mktemp 等创建；参考现有 `tools/run_round9_3_plugin_installer_discovery_dogfood.py` 的 isolated launcher 安装模式，只复用相关打包步骤，不运行整个插件安装 dogfood。
3. 将此 wheel 安装到隔离环境，从非源码 cwd 启动 `pulsara app --no-open`，核对包导入位置、应用静态资源和入口均来自该安装。
4. 浏览器实际打开安装版应用，复测原文反例与复制；仅 `pulsara --version` 成功不算 UI 修复已经发布。
5. 不覆盖全局 launcher，不改用户保存的 model/DB 配置，不通过额外文件哈希证明“包里是新代码”；用安装路径、构建产物引用和实际行为证明。

避免让源码版与安装版服务同时占用同一个 host owner/测试会话。如需停当前服务，识别确切本次服务再操作；不 reset 当前真实数据库，不清除历史作为正常前置步骤。

### 8.3 真实 provider/browser dogfood

使用 `LocalSettingsStore` 与 `require_pulsara_home()` 读取用户保存的生产配置；优先已有 OpenRouter 可用模型，不依赖 bobapi 恰好慢或失败来制造样例。不导出实际密钥，不改配置，不上传本地私密内容。

至少覆盖这些实际交互路径：

- 要求模型给出含反例标识符的 Python/JSON/shell 文本，检查 streaming、最终回复、复制和 reload。
- 让模型进入规划并提出含 `read-only` 等字面值的选项，再提交含原词代码片段的 draft；对照现有授权 plan 读取内容检查视图。只制定无副作用的测试计划；验证完明确取消，不留下待审批项。
- 创建一个只读子任务，其目标、结果或进展包含这些词，核对主对话和任务详情；不要求该模型泄露 reasoning，未返回该块就由 deterministic T03 提供覆盖。
- 在安装版应用确认同一修复行为，不能只看 Vite dev server。

模型不一定逐字服从预设提示。**比较对象是该次实际 provider-visible/canonical 原文与 UI/clipboard，不是把模型必须生成某句文本当成前端正确性断言。** 如关键反例词没有出现，可补一个明确探针；记录实际输入/输出，不改证据冒充通过。

截图与真实操作必需，不能只读取 DOM；自动化也必须核对精确文本/剪贴板。二者互补：截图判断代码排版、中文标签及窄/宽布局，精确断言判断标识符和空白是否保持。

浏览器本次遇到 F01/F02/F03 等索引中尚未修的问题应按原编号记录；既不能顺手扩展 PR，也不能因此伪造该路径通过。F07 必须有可观测的 source→projection→render/copy 证据。

## 9. 失败、回退与兼容性

- 不可读取计划、内容 chunk 校验失败、controller 失效时，保留当前错误路径，不展示拼接不完整的正文为完整方案。
- 空文本与缺失字段按现有类型区分；不得拿“操作已完成”填进原文槽来满足非空检查。
- clipboard 拒绝只提示复制失败，不自动写文件、修改浏览器权限或发送到外部服务。
- 本 PR 是前端表示层 hard cut，不做数据库迁移、历史 replay 或双版本消息存储。刷新读取原 canonical 数据即可得到新展示。
- 部署回退由 Git/正常构建发布流程处理，不在生产代码里保留旧正则路径作为回退开关。
- 浏览器自身需要的连接/鉴权、artifact 边界、plan owner 校验与 provider-prefix continuity 全部保持。

## 10. Activation acceptance criteria

全部满足才将本规格状态改为 **ACTIVATED**：

- [ ] F07 全部调用面完成切换，不仅主回复；T01–T12 有明确断言和结果。
- [ ] PRODUCT_TEXT_REPLACEMENTS、productVisibleText、productVisibleMessage 在生产源码中零保留，无别名、包装器或复制实现。
- [ ] 回复/思考/计划/子任务/TODO/工具原文不再被词汇翻译；复制与对应源 body 相等。
- [ ] 计划显示与原 draft 内容一致，既有读取权限、chunk 校验、批准对象和决策语义不变。
- [ ] 产品固定标签仍由现有 typed 映射中文化，来源自定义名称/说明不被误翻译。
- [ ] 前端 focused、完整回归、lint、build:local 及指定 Python focused checks 全部通过。
- [ ] 实际 source build 和 isolated wheel/launcher 的浏览器行为已验证，静态资源不是旧 bundle。
- [ ] 已完成真实 provider + 截图 + 精确文本/复制核验，记录实际证据和模型未提供的不可测块。
- [ ] 无 backend/protobuf/DB/descriptor/provider-input 语义变更，无新增 fingerprint/durable 状态/兼容路径。
- [ ] 根目录索引标记 PR01 完成，但其他 F/M 项继续保持各自未完成状态。
- [ ] 最终报告说明代码与产物范围、删除项、测试命令结果、剩余风险和 Git 状态；不自动 stage/commit。

实现者必须逐项给出事实，不得把“83项旧测试本来就通过”或“文件最终正确”作为以上条件的替代品。
