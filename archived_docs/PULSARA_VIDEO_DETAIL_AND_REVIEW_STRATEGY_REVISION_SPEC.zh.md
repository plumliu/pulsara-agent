# Pulsara 视频 detail 与按问题审看策略修订规范

日期：2026-11-05。状态：已实施。生产行为、工具说明、Skill 与活动基础规格已同步；本次验收见第 10 节。

本文规定 `read_video_frames` 的新 detail 合同，以及 `pulsara-video-review` 的模式选择策略。实施时，本文优先于[视频理解实现规格](PULSARA_VIDEO_UNDERSTANDING_IMPLEMENTATION_SPEC.zh.md)及[工具输出与分页合同](PULSARA_TOOL_OUTPUT_AND_PAGINATION_REFACTOR_PLAN.zh.md)中与本修订冲突的 detail 条款；未涉及的能力和边界继续沿用原合同。

## 1. 目标与范围

detail 用于核对已经定位的画面内容：文字、数字、人物、物体或局部状态。一次调用提供一个短范围内的首、中、尾三个观察位置，优先保留空间细节，给模型少量前后上下文。

本修订明确以下产品决定：

1. 保留 `target/from/to/mode/crop/continuation` 六个顶层参数，不新增时间点、帧数、FPS、多范围或第四种 mode。
2. detail 必须显式提供 `from` 和 `to`，满足 `0 < to - from <= 2` 秒。支持小数秒，不要求对齐整数秒或固定时间网格。
3. detail 的选择计划只有首、中、尾三个目标；同一实际源帧可以满足多个目标，实际观察和图片因此可以少于三张。
4. 超限或未显式提供范围时，返回模型可读的工具错误与使用提醒；不继续抽帧，不自动截断、拆段或切换 mode。
5. Skill 按当前问题选择 overview、detail、motion，不把全片审看组织成连续 detail 小窗口。

两秒是用户明确选择的单次局部核对边界，用于维持 detail 的产品职责；三个目标是该动作的观察质量配置。它们不是视频总时长、任务寿命、工具总调用数或历史总图片数上限。overview、motion 仍可请求较长范围，并沿原分页机制继续。需要多处细节时允许独立读取，不设置累计 detail 次数限制。

本次不修改 overview 的选帧与排版、motion 的连续观察与拼图、语音转写、批注、播放器或数据库结构。detail 保留现有原片渲染、crop 和空间质量配置；不在此次引入空间分块分页、超分辨率、自动 OCR、自动跟踪或新的高清档位。

## 2. 三种模式的产品分工

| mode | 回答的问题 | 主要保留的信息 | 范围选择 |
| --- | --- | --- | --- |
| overview | 这段里有什么，应该去哪里看？ | 范围内的代表状态与时间位置 | 可以是全片、中间片段或继续缩小的局部范围 |
| detail | 这里写了什么，是什么物体或状态？ | 少量原帧的空间细节 | 显式指定、不超过两秒的局部范围 |
| motion | 这里怎样变化，先后顺序是什么？ | 连续观察中的时间顺序与中间状态 | 围绕过程问题选择范围，沿原合同分页 |

overview 可以用于视频中间任意合法范围，不是仅供首次调用的全片入口。detail 的三个位置不构成完整过程证据；motion 不仅用于 A→B→A，也用于出现、消失、先后顺序、重复动作和状态转变。

缩小 detail 范围会让三个目标在时间上更加集中，不增加目标数，也不自动增加空间分辨率。空间辨认依赖源画面、现有清晰图片配置和适当 crop。三个目标之间仍可能存在未观察到的短暂状态。

## 3. 参数校验与超限返回

### 3.1 模式相关的参数要求

| 参数 | overview / motion | detail |
| --- | --- | --- |
| target | 必填 PATH Target | 同左 |
| mode | 省略为 overview | 必须选择 detail 才进入本合同 |
| from | 沿原默认值与范围校验 | 显式提供、有限非负 number |
| to | 沿原默认值与范围校验 | 显式提供、有限 number，且大于 from |
| crop | 沿原归一化展示坐标合同 | 同一矩形应用于本次所有选定帧 |
| continuation | 首次省略，续读复制完整 next | 同左；续读参数仍含显式 from/to |

六字段 schema 保持关闭。字段说明与主工具说明必须写清 detail 的条件要求；不能把 from/to 改成所有 mode 无条件必填，也不能仅依靠提示词执行约束。

runtime 在应用默认时间范围之前检查 detail 的显式字段存在性。不得从视频时长、continuation、上一调用或 UI 阅读位置补齐缺失参数；null 不算显式合法时间。其他非法类型、布尔值、NaN、无穷、负起点、空范围和反向范围沿现有参数错误合同处理。

detail 的两秒检查使用原始请求范围，在与有效视频轨求交之前执行。不得通过视频提前结束、裁剪有效范围或预算缩页，把一个超限请求变成合法请求。恰好两秒合法，任何严格大于两秒的请求均超限。

时间比较复用已有精确时间转换 owner，以参数的十进制数值语义进行差值与比较，例如 `12.32–14.32` 应作为恰好两秒接受。不得通过取整、固定毫秒量化或任意 epsilon 放宽上限。中点与 strict-before 解析继续使用精确源时间，不以二进制浮点相减的误差拒绝合法边界。

生产参数入口负责显式范围校验；直接调用准备/便利函数时也必须执行时长约束，避免不同执行路径接受不同语义。媒体时长、未知时长、无视频轨和权限校验继续使用既有 owner。

### 3.2 模型可见错误

缺少显式范围或超过两秒属于调用参数错误。生产准入沿既有 `INVALID_ARGUMENTS` 结果交付，正文包含 `INVALID_RANGE` 和完整使用提醒；直接执行路径沿 `APPLICATION_ERROR` 返回 `error/message`。复用原错误与结算 owner，不为统一外层状态另建执行通道。不得让未捕获异常替代工具结果，也不得返回成功图片后仅附警告。

缺少任一端点时，提醒必须明确：

```json
{
  "error": "INVALID_RANGE",
  "message": "detail requires explicit from and to with 0 < to - from <= 2 seconds. Choose a local range for a specific visual question; use overview to locate content or motion to inspect a sequence."
}
```

例如请求 `from=10, to=15, mode=detail` 时，结果应包含原请求及两秒约束，推荐措辞为：

```json
{
  "error": "INVALID_RANGE",
  "message": "detail accepts at most 2 seconds per request; requested [10, 15) spans 5 seconds. Choose a local range for a specific visual question. Use overview to locate content in a longer range, or motion to inspect changes and order."
}
```

错误结果不返回图片、成功帧索引、完整覆盖声明或自动构造的 next。参数错误应在媒体探测/解码前识别，不启动 ASR、不更新模型阅读范围、不触发首次媒体自动展示。授权与调用入口检查仍由既有 owner 执行。

## 4. 首、中、尾选择合同

### 4.1 时间目标

通过参数校验后，使用源 owner 确认的有效视频轨与请求 `[from,to)` 求交，得到非空 `[a,b)`。没有视频交集时沿原合同报错；原请求超出已知媒体时长时仍沿原范围校验报错，不通过本节绕过。

对有效视频交集仅建立以下三个目标，按顺序解析：

| 目标 | 时间 | 源帧解析 |
| --- | --- | --- |
| 首 | a | 在 a 时刻实际显示的帧 |
| 中 | (a+b)/2 | 在中点实际显示的帧 |
| 尾 | before(b) | 严格位于半开终点 b 之前实际显示的帧 |

`before(b)` 不是 `b - 固定毫秒`，也不是 `b - 1/名义FPS`。复用已有 strict-before 目标与真实 PTS / 展示区间解析。不得选取从 b 才开始显示的下一帧。

正常完整视频范围的示例：

| 请求 | 三个目标 |
| --- | --- |
| `[0,2)` | `0`、`1`、`before(2)` |
| `[12.32,12.56)` | `12.32`、`12.44`、`before(12.56)` |
| `[42,43)` | `42`、`42.5`、`before(43)` |

不使用半秒格点；长度大于一秒与小于等于一秒使用同一条首中尾规则。源视频本身没有记录的中间帧不插值、不生成；不以邻域锐度、变化检测、切点或语义判断替换目标对应的源帧。

### 4.2 实际帧与重复图片

1. 同一源帧的已验证展示区间满足多个目标时，只形成一次实际观察。源位置与展示区间由解码 owner 确认，不凭图像相似性判断是同一源帧。
2. 不同源位置即使像素相同，仍是不同时间的实际观察，保留各自索引。
3. 继续允许页内精确图片共享：展示变换、crop、既有目标缩放后的未烧号 RGB 尺寸与全部像素完全相同，多个观察才可指向同一张图片。不引入近似去重或跨调用图片状态。
4. 不为凑足三张图片复制同一源帧，不为填满预算追加第四个目标。完整计划有一至三个不同源观察；完整交付可以是一至三张实际图片。

三个目标只是时间锚点，不自动命名为“操作前、操作中、操作后”。相同首尾或三张相同图片都不能证明区间内没有变化。

### 4.3 源边界与清晰度

保留原始 `requested_range`；视频轨延迟开始或早于媒体尾部结束时，明确披露未被视频覆盖的前后部分。有效交集上的首中尾不能被描述为观察了整个原请求。

返回真实源帧时间 `t`，以及必要的 `visible_range`；一帧在 from 之前已经开始显示且覆盖 from 时，保留真实 t，不伪造为 from。解码预滚不构成额外交付的观察。

图片继续从原片取得并执行展示方向/像素宽高比校正，再 crop 和按原空间质量目标渲染。保持一致的 crop 和尺寸，不以 overview 缩略图放大替代原片，不因页面预算降为低清拼图。源画面模糊、遮挡或文字不存在时如实保留，工具不承诺自动修复。

此处限制的是选定观察数，不是为获得目标源帧所需的依赖解码帧数。继续复用现有解码、取消、资源和截止时间机制，不新增任意解码帧总量上限。

## 5. 有限计划、分页与结果语义

### 5.1 准备与交付

detail 在本次调用内建立完整首中尾计划，解析并按源位置去重后得到按真实时间有序的 N 个观察，`1 <= N <= 3`。不准备 256 个 detail 观察，不建立下一时间窗口，也不扩展原请求。

继续复用准备、原片渲染、图片绑定、图文联合 quote 和接受的既有生产路径。页面预算只决定交付这个有限计划的哪个连续前缀；不能重新选择目标或删除中间观察来假装完整。

预算足够时一次交付全部观察。预算不足时，可以交付非空前缀并返回完整 next；最小清晰图片、索引与控制信息仍放不下时沿原 `RESOURCE_BOUNDARY` 返回。既有 preaccept 重新预算、合法 compaction、当前页原图保护和已接受页不可修改的合同继续成立。

一次调用内的缩页继续复用未绑定编码候选，移除尾部观察后重新绑定索引、完成情况和 next，不为每次 quote 重复解码。同一有限计划的剩余观察在下次调用中可确定性重建，不建立持久计划。

### 5.2 continuation 与策略版本

detail 复用现有有限计划位置：continuation 的 `position` 只含 `next_observation`，表示去重后的有序源观察序列中下一未交付位置。成功续读位置满足 `0 < next_observation < N`，完成后 next 为 null。

不接受 detail 的 `window_start`、全局格点 ordinal 或额外位置字段。完整 next 保持原 target、显式 from/to、mode 和 crop；位置由 runtime 生成，模型只复制，不手算。

续读仍绑定既有规范路径、源版本事实、视频流、原始查询与选择策略；每次重新校验访问与源事实。取消、页面未接受或最小页失败不消费位置。任何成功非空页必须推进。

沿用现有唯一选择策略版本字段，将版本硬切为 `frames-v3`。不建立按旧版本执行的分支，也不把旧格点位置转换成新的观察位置。沿该共享字段生成的旧 `frames-v2` continuation 均按原版本失效机制明确返回 `FRAME_CONTINUATION_UNAVAILABLE`；这不修改 overview/motion 的选帧算法，也不改写已接受历史图片或结果。

### 5.3 模型可见结果

保留 target、源版本事实、requested_range、processed_range、实际帧索引、必要显示尺寸/crop、complete、next 和限制说明。detail 结果应满足：

- `sampling` 为 `sparse`；帧索引按时间排序，不用 ordered 一词暗示密集过程覆盖。
- `scanned_ranges=[]`，`observed_cut_times=[]`。首中尾解码、准备包络或三张相同图片都不形成连续扫描证据。
- 每条观察保留现有 `n/image/t/cell_rect/visible_range` 及适用的实际 `dt`；`observation_position` 使用有限计划中的源观察序号。
- 用既有 `plan_delivery` 表达 `position/next_position/observations/window_complete`，其中 observations 是 N，而非强制为 3；window_complete 仅表示本次有限计划已交付完，不表示出现新时间窗口或逐帧覆盖。
- 移除 detail 的 `grid_delivery`。实际 probe 数与间隔继续如实表达，不再暗示半秒固定采样。
- `processed_range` 只表达已有 owner 的准备范围；实际交付依帧索引与计划位置判断，模型不得拿准备尾部重建续读。

正常完整视频范围内，全部选定观察已交付时 `complete=true, next=null`。尚有选定观察时 `complete=false` 并保留 next。原请求存在前后无视频部分时必须披露；即使有效计划耗尽、next 为 null，也不得把原请求标为完整成功。

模型侧文案使用直接的动作指导：说明首中尾采样，提示小字使用 crop、过程使用 motion。将具体缺口或错误随实际结果说明，不反复加入“不能证明/不意味着”式通用警告。存在 next 时使用条件式提示，例如：

> More selected images from this same short detail range are available. Copy next only if they are needed to answer the current question; otherwise stop.

不输出命令式的“仍有格点，继续复制 next”，不把清空有限计划当成回答用户前必须完成的步骤。已经取得足够证据时，模型可以停止，仍须准确描述实际使用的证据。

## 6. 工具说明

主工具描述必须同时传达默认入口、三种用途、detail 硬约束、三个目标与条件式续读。建议英文正文如下，可直接作为实现基准：

> ROOT direct only. Read local video as attached images with time indices. target alone gives a sparse full-video overview. Use overview on any range to locate content; detail to inspect text, objects or local states; motion to examine changes, order or brief events. detail requires explicit from/to seconds with 0 < to - from <= 2 and samples the start, midpoint and just before the end; targets on the same source frame are returned once. Decimal seconds and normalized crop are supported. If remaining evidence is needed, copy the complete next call; stop when the question is answered.

from/to 的字段说明应标明 overview/motion 的原默认行为与 detail 的显式范围要求；mode 说明应标明两秒是 detail 单次请求边界。超限说明必须同时出现在工具描述和第 3 节的实际 tool result 中，不能仅藏在 Skill。

## 7. 视频 Skill 修订要求

修改对象是 bundled `pulsara-video-review/SKILL.md` 及其 `references/worked-examples.md`。以下策略由这两个文件共同承载，正文讲选择与操作，示例展示完整决策过程。

### 7.1 按当前问题选择动作

Skill 应用简洁、面向模型决策的语言说明：

1. 全片总结或位置未知时，可以先做全片 overview，建立候选内容地图。用户已提供局部范围或位置时可以直接开始，不强制全片概览。
2. 地图不足以定位时，对相关片段做 overview，并按需要进一步缩小范围。不把视频机械切成等长片段，也不要求逐段调用所有 mode。
3. 存在明确的辨认问题时使用 detail，例如读金额、提示、标签，或辨认局部状态。显式选择不超过两秒的范围，可以使用小数秒，并在小字或局部内容需要放大时使用 crop。
4. 答案依赖变化过程时使用 motion，包括先后顺序、短暂出现/消失、重复动作和 A→B→A。不要求先在 overview 里发现异常才允许 motion。
5. 已取得的图像足以回答时直接使用；需要重新查看已经交付的图片可沿已有 IMAGE_REF/view_image 路径。原图细节不足或需要另一源时刻时才重新读取相应媒体证据。
6. 每次返回后判断未解问题，再选择同请求续页、另一范围、换 mode 或作答。next 是可用证据，不是必须执行的清单。

“全片 overview → 分段 overview → 局部 detail / motion”是可选阅读示例，不是固定流水线。detail 调用应有具体的内容辨认目标，无需给工具新增 question 参数或在运行时建立问题登记表。

### 7.2 用户给出时间点

| 用户意图 | 策略 |
| --- | --- |
| “30 秒这个金额是多少？” | 直接选择附近不超过两秒的 detail，必要时 crop |
| “30 秒附近到了哪个界面？” | 局部 overview；内容不清楚再 detail |
| “30 秒这里做了什么操作？” | motion；只有文字/物体细节不足时再 detail |
| “提示什么时候出现，之后消失了吗？” | motion；必要时用 detail 核对提示内容 |
| “对比 30 秒与 90 秒的状态。” | 两处独立的局部 detail，保持可比较的 crop；无需扫描中间全段 |

用户给出时间点不会自动触发 motion+detail。选择来自问题需要，而不是时间点这一输入形式。

### 7.3 完整性与停止条件

稀疏 overview 可能漏掉没有留下可见线索的短暂事件；detail 首中尾同样不能排除间隙事件。用户要求检查整段是否出现过某个短暂状态时，需要按该范围开展连续过程检查，并依据 motion 实际交付情况说明覆盖，不能只检查 overview 看起来可疑的地方。

时间上的连续证据不自动保证小字可辨，画面清晰也不证明时间间隙内无变化。Skill 不承诺模型已理解每个像素、每次动作或全部文字；源模糊、未交付页面或未检查范围均应如实说明。

证据满足用户要求时停止，不为清空 next 或使工具 complete 变真继续读取。需要读出大量不同画面的文字时，多次 detail 可以是必要工作；不用总调用数硬限制代替正确的停止判断。

语音/字幕、纯音频、批注、steer 与播放器独立预览的现有指导继续保留。纯视觉问题不要求 read_transcript，也不能用字幕或旁白证明未观察到的视觉结果。

长视频中的 `annotate_media` 用于与用户共享审看发现。模型按当前问题组合 overview 定位、detail 辨认、motion 检查过程，在形成有用结论时即添加带原媒体范围的简洁批注，供用户边看边定位、提问和调整关注点。批注区间按发现本身选择，可以跨越多次读取；不要求每条批注先调用全部三种模式，也不按固定频率写阅读日志。用户显式追问时回看对应片段，处理后按原目标继续；普通拖动播放头仍由用户独立控制。

Skill 开头明确：直接调用 `read_video_frames` 开始审看，首次实际读取会自动在侧栏打开视频，已满足向用户展示该视频的目的，无需在审看前或结束时重复调用 `open_in_pulsara`。另行生成报告或其他需要展示的新产物时，仍按通用展示指引调用 `open_in_pulsara`。沿既有首次展示合同，后续读取尊重用户关闭或切换预览的操作。

### 7.4 Worked examples 的内容

示例至少覆盖以下决策，不要求固定调用次数：

- **全片总结后核对一个数字。** 全片 overview 建立地图，对相关中间片段再 overview；例如用 `[245.2,246.2)` 的 detail/crop 读取筛选条件，得到答案后回到原主任务位置，不把其他部分切成两秒 detail。
- **只有时间点的辨认问题。** 直接用 `[12.32,12.56)` 的 detail 读取目标，解释实际时间来自源帧；不加无关的全片 overview 或 motion。
- **保存过程与结果。** 对点击、弹窗出现及后续状态使用 motion；提示已清楚时直接作答，否则在已定位提示附近选择不超过两秒的 detail/crop。
- **短暂往返。** 即使 overview 的前后画面相同，过程问题仍使用 motion；不把 detail 首中尾没有发现 B 当成 B 不存在。
- **超限恢复。** detail 请求五秒收到提醒后，依据问题改用该范围的 overview/motion，或选择已定位的局部范围；不默认生成三个连续 detail 请求。
- **detail 被分页。** 已读到目标数字时允许停止；若需要的另一张选定画面尚未交付，复制完整 next。next 只完成同一个两秒内的计划。
- **远距离比较。** 分别核对两处局部状态，复用已有图片；只有需要解释中间变化过程时才追加 motion。
- **长视频共同审看。** 首次读帧自动展示视频；按需组合三种模式，边推进边留下有用批注，用户点击批注回看并提问后，模型局部核对、补充发现再回到主任务。

删除 Skill 与示例中“detail 通常读取 2–5 秒”“长 detail 合法并沿半秒格点持续续读”“五秒十一目标”及长静态讲解全范围 detail 示例。保留文字页跨句、读取来源和局部回看后回到主阅读位置等仍有效指导。

## 8. 实现归属与硬切要求

| 现有 owner / 文件 | 实施责任 |
| --- | --- |
| `tools/builtins/media.py` | 六参数 schema 的条件说明与显式 detail 范围校验 |
| `capability/builtin_catalog.py` | 主工具描述中的用途、两秒边界和条件式续读 |
| `media/frames.py`、detail 目标选择模块 | 有限首中尾计划、源观察去重；便利函数与生产路径使用同一选择规则 |
| `media/frames_decode.py`、`media/source.py` | 复用精确时间、实际展示帧、strict-before 和源事实校验，不复制解码器 |
| `media/frames_detail_render.py` | 保留清晰原帧与页内 exact RGB 共享，不承担时间选帧 |
| `media/frames_paging.py` | 复用有限计划 next_observation、源绑定和完整 next，删除 detail 格点游标 |
| `media/execution.py` | 参数错误正常交付、有限计划装页、原图联合准入和取消结算 |
| bundled Skill 与 worked examples | 第 7 节的按问题选择策略 |

PyAV/现有解码 owner 继续负责源帧与展示区间，Pillow/现有渲染 owner 负责图片变换和编码；Pulsara 只拥有三目标选择、查询校验与交付语义。不增加依赖，不复制 SDK 机制。

实施时删除 `DetailGrid`、半秒步长、detail 全局目标跳格、`detail_grid_*` / `detail_next_grid_ordinal`、`next_global_grid_ordinal`、`grid_delivery` 及无独立用途的 detail 大批准备路径。可以重用原模块承载有限目标计算，但不能保留失去用途的网格结构或旧策略回退。motion 仍需要的 256 观察物理配置保留，不能随 detail 清理误删。

已有成功历史结果与图片不回写，旧 continuation 失效不代表允许重放历史工具。新工具说明与 Skill 仅在新 cold epoch 或明确采用的 compaction successor 安装；既有 epoch 的 SYSTEM/provider tools 保持字节相同，messages 只追加后缀，不为修订新增 rebase 边界。

不新增数据库表、durable/live event 类别、subject、guard、relation、job、receipt、持久计划、指纹注册表或帧实体 ID。沿既有调用与结果提交边界结算。无需 clean-v0 schema 变化或数据迁移。

实施同一变更时，将基础视频规格中的参数表、detail 模式定义、第 5.3 节、第 6.2.1/6.2.4 节、选择版本与相关验收条款，以及分页合同中的 detail 段落同步为本文合同。不得让两份活动文档继续同时要求不同的 detail 行为；既有历史验收记录不能作为新策略已通过的证据。

## 9. 验收要求

按用户要求，以替换原视频浏览链路中的相关测试为主，保留有意义的参数、选帧、分页和 Host 回归；真实模型复用现有 dogfood 入口做少量自然任务检查，不新增庞大的独立测试框架。

### 9.1 参数与边界

| 场景 | 预期 |
| --- | --- |
| 仅 target、未指定 mode | 默认 overview，保持原行为 |
| detail 缺少 from、to 中任一个或两个 | 工具错误，说明显式范围与两秒要求；不探测/解码或更新阅读范围 |
| `[0,2)`、`[12.32,14.32)` detail | 接受恰好两秒 |
| `[12.32,14.320001)`、五秒 detail | INVALID_RANGE，包含超限提醒；无图片、无自动拆段 next |
| 小数秒极短合法范围 | 按精确首中尾解析，允许合并到同一源帧 |
| 非法类型、布尔值、非有限数、负值、零长度或反向范围 | 明确参数错误 |
| 超过媒体时长、无视频交集、无可用视频轨 | 沿原源/范围错误合同 |
| 原请求超过两秒，但有效视频交集不足两秒 | 仍拒绝原超限请求 |
| overview/motion 超过两秒 | 不受 detail 边界限制 |

### 9.2 实际图片与分页

1. 使用带时间真值的固定帧率、VFR、低 FPS、非零起点和延迟视频起点样例，验证首中尾对应真实展示区间；strict-before 不取终点处新开始的帧，不使用固定 epsilon。
2. 三个目标分别命中三帧、两帧和一帧时，验证实际观察数、真实 t、visible_range 和图片数。不同源时间的同像素观察保留索引；同源帧不凑数复制。
3. crop、旋转、像素宽高比与模糊帧验证沿原空间合同；微小文字变化不被近似合并，清晰图片来自原片。
4. 验证 N=3 的整页、每页仅一帧、共享图片和最小页仍失败。预算变化不重选目标；复制完整 next 后全部独立观察无重漏，位置严格推进，无越界/原地 next。
5. 验证首中尾整个有限计划的准备与页内缩小，无 256 个 detail 观察准备，也无把省下的预算用于追加目标。
6. 验证改 target、from/to、crop、源版本或策略的 continuation 被拒绝；旧 frames-v2 明确失效，没有转换、兼容读取或从头重放。
7. 验证请求存在无视频前后部分时，限制、complete 与 next 不误导；未交付部分和解码预滚不标为连续覆盖。
8. 生产 Host 验证图片、索引和调用归属实际进入 provider；强制预算缩页/合法 compaction 后，当前页与完整 next 保留，取消与最小页失败不消费位置。

### 9.3 Skill 与自然任务行为

用真实视觉模型在生产 Host 的新合法安装边界验收。自然任务不指定工具名、mode、固定时间窗或“必须停在第几次调用”，避免把预写路线当成模型自主选择能力。

自然任务优先覆盖局部辨认、指定小数时间点及分段概览的选择。motion/ABA 和远距离比较沿已有行为回归与 Skill 示例检查；超限提醒与有限计划续读替换原生产工具测试。其余场景用于后续扩展评估，不扩大本次新增测试量。

记录实际问题、工具参数、返回帧/图片、完整 next、模型回答、正确性、未支持的覆盖声明、调用数、图像数量、token 和延迟。重点检查：

- 明确的辨认问题可直接 detail，不固定触发全片 overview 或 motion+detail。
- 全片/分段 overview 用于定位，不机械遍历连续 detail 小窗口。
- 过程问题使用过程证据；不能由首中尾未发现某状态推断它从未出现。
- 得到所需证据后可停止，不为清空 next 继续调用；多次必要核对不按任意总次数判失败。
- 超限错误后按问题修正，不将长范围默认拆成连续两秒 detail。
- 原本需要大量不同画面文字的任务不能靠少读、漏读换取较少图片；质量与覆盖要求优先于数量指标。

沿 `LocalSettingsStore` 和有效 Pulsara home 读取用户保存的生产配置，遵守仓库现有只读设置、凭据排除与可观察证据要求。最终是否完成，以实现合同、针对性回归和真实自主使用证据共同判断，不能只凭测试通过或图片数量下降宣布完成。

## 10. 本次实施与验收记录

实现采用 `frames-v3`：显式两秒范围校验、精确首中尾目标、同源帧合并与有限计划分页均进入生产路径。原半秒格点、格点游标及结果字段已删除。原片渲染、精确图片共享、物理预算、压缩和工具结算复用既有 owner。

bundled Skill 正文与 worked examples 已按问题选择动作重写，包含任意片段 overview、小数秒 detail、crop 的两个角坐标、过程问题 motion、同请求续页和停止判断。工具结果使用直接的后续动作提示，实际缺帧或源范围问题保留具体说明。

### 10.1 针对性回归

最终集中运行以下既有测试链路及替换后的 detail 用例，**144 项通过**：

- `test_media_detail_selection.py`、`test_media_detail_paging.py` 替换原格点选择/全局格点分页测试。
- `test_media_detail_render.py`、`test_media_frames.py`、`test_media_frames_paging.py`、`test_media_frame_continuation.py`。
- `test_media_tool_surface.py`、`test_media_frame_compaction_host.py`、`test_media_overview_policy.py`、`test_bundled_skills.py`。

Host 测试使用独立临时 PostgreSQL，验证接受后的图片/索引归属、两秒有限计划的完整 next、合法压缩后续读及物理预算缩页。此次未扩大为全仓测试；改动文件 Ruff 与 `git diff --check` 通过。

### 10.2 自然视觉任务

复用 `tests/dogfood/run_media_natural_benchmark.py --native-only`，使用保存的 `qwen3.8-flash` 配置、生产 Host 和独立数据库。三个问题均未规定 mode 或调用路线。原问题、实际参数、工具结果、provider 请求、回答、usage 与耗时保存在 [natural-host.json](../output/detail-three-targets/natural-host.json)。

| 任务 | 模型实际选择 | 结果 | 工具调用 / 图片 |
| --- | --- | --- | --- |
| 50.2 秒附近报告标题和实付金额 | detail 49.2–51；局部 detail 50–50.6 加 crop | 读出“卖得更多，赚得更少”、49.3 万 | 3 / 6，其中一次 crop 参数错误后自行纠正 |
| 前 100 秒幻灯片主题及自我介绍标题 | overview 0–100；detail 98–100、11.5–13.5 | 识别主要页面及 `whoami (Ivan)` | 3 / 7 |
| 4.963 秒的人数、上衣颜色与项链 | detail 4.9–5.1 | 1 人、黄色上衣、有项链 | 1 / 3 |

三个任务共 9 次模型调用，均未使用转写或连续 detail 分段遍历。各次 detail 的所有选定帧均在一页交付；有限计划缩页与压缩续读由上述 Host 回归验证。录屏任务耗时约 237 秒，含服务连接重试；讲课和人物任务约 53 秒、20 秒。

录屏首次把 crop 写成宽高后自行修正；工具字段和 Skill 已补充明确的左右/上下坐标及数值示例。讲课回答给出的约略页面区间使用了 overview 代表范围，因此这里只验主题和标题；Skill 已补充“用实际帧时刻定位主题，需要精确转变时间时使用 motion”的正向指引。

额外的 Skill 加载检查显式点名 `pulsara-video-review`，其余仍为自然问题。模型实际读取 bundled `SKILL.md`、定位素材，再使用 detail 50–51 和带合法 crop 的 detail 50.1–50.3，正确读出标题及 49.3 万，并在隔离会话内留下时间线批注。共 5 次模型调用，约 222 秒，含一次服务断连重试；完整记录见 [skill-host-recheck.json](../output/detail-three-targets/skill-host-recheck.json)。首次临时脚本从 stdin 启动造成多进程入口失败，已终止并清理对应临时数据库；失败记录保留为 `output/detail-three-targets/skill-host-launch-error.json`，随后使用带 main guard 的文件入口完成上述检查。

已按用户要求重新构建 macOS ARM64 桌面应用并安装到 `/Applications/Pulsara.app`。标准 `desktop:verify` 中 149 项桌面测试通过，随包环境的重定位、数据库、媒体解码和浏览器 smoke 通过；应用包与安装后的严格递归签名校验通过。安装包内本次修改的 9 个 Python/Skill 文件与工作目录一致，随包 Python 实测 `frames-v3`、恰好两秒接受、超限拒绝及新版 Skill 内容通过。
