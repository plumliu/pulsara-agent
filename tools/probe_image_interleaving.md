# 单条 USER 消息的图文交错实验

从仓库根目录运行：

```sh
# 只生成合成图片和 manifest，不调用模型
.venv/bin/python -m tools.probe_image_interleaving

# 五组已保存的外网 Chat 配置，两个素材轮次，各七种排列
.venv/bin/python -m tools.probe_image_interleaving --run

# 同时尝试此前不稳定的 gpt-5.5 Responses 配置
.venv/bin/python -m tools.probe_image_interleaving --include-responses --run

# 单独指定已经保存的模型 ID 或 connection ID
.venv/bin/python -m tools.probe_image_interleaving --connection glm-5.3-flash --run
```

默认包含此前的 Qwen、Muse、Luna、DeepSeek 四组配置及新增 GLM；始终排除用户指定不测的内网 Qwen3.8-27B。模型名称仅用于本次实验选择，不是生产 provider 能力表。`--include-responses` 增加已保存的 gpt-5.5 配置；若它首轮连纯文本基线都无法协议完成，仅再执行 text_first 和 interleave_before 两个诊断，不将失败解释为不支持交错。

每轮以固定随机种子生成三张不同的 512×384 PNG 数字卡，各轮使用不同数字。图片不含标签；文本只指定标签，不透露图片数字。七种场景均为单条 USER 消息，T 表示 text part，I 表示 image part：

| 场景 | 顺序 | 验证内容 |
| --- | --- | --- |
| text_baseline | T | 无图时应返回 null，不猜数字 |
| text_first | TII | 文字在前，按说明中的图序分配标签 |
| images_first | IIT | 附件在前、文字在后，与上一行使用相同图片和说明 |
| interleave_before | TITIT | 每段标签指向紧随其后的图片，末尾有输出要求 |
| interleave_after | ITIT | 每段标签指向紧邻其前的图片 |
| interleave_swap | TITIT | 标签文本与 interleave_before 完全相同，只交换两张图片 |
| interleave_four_repeat | TITITITIT | 四次图片 occurrence，第一与第三次为同一张图，四个标签分别保留 |

通过原 `LocalSettingsStore` / `require_pulsara_home()` 只读使用生产配置，复用 `probe_vision_tokens.request_once()` 及正式 SDK client、认证与凭据边界。SDK 自动重试关闭；单次实验 deadline 默认 90 秒，输出预算 2048 tokens，并发 4。每个配置按顺序发独立请求，Responses 使用 `store=false`；保持 `detail=auto` 和 provider 默认 reasoning。没有数据库写入、设置修改、kernel 激活或 D1/D2 调参。

结果写入新的 `scratch/image-interleaving-probe/<UTC 时间>/`，受已有 ignore 规则保护：

- `manifest.json`：配置身份、地址、实验参数、图片、完整抽象 parts、预期答案。
- `requests/`：送入 SDK 的完整请求参数、原始流式事件和调用结果；实际凭据值按现有规则精确遮盖。
- `results.jsonl`：每个调用的实际答复、终止原因、usage、预期值和评分。
- `report.zh.md`：按配置与场景汇总，逐请求刷新。

`protocol_complete` 表示 SDK 流出现了可观察的协议终止且没有被记录为调用错误；不要求 usage 存在，也不等于答案正确。`association_match` 允许可选 Markdown JSON 围栏，并把 JSON 整数转换为数字字符串，再与完整预期对象比较；`strict_match` 不作数字类型转换，但同样允许围栏。原始回复始终保留；这不是严格输出格式基准。交换图片与重复图片的独立对照必须答对，才能把读出数字与保持关联分开评估。

请求 JSON 字节数是 SDK 参数序列化值，不声称捕获了 HTTP 实际字节。图片识别与关联成功只证明这些配置对这些样例的可观察行为，不证明服务端内部从不重排，也不能替代未来正式 kernel 的 canonical/compaction/fork/continuity 验收。
