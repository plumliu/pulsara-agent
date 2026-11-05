# Responses 完成结果合并规格

## 目标与边界

同一通用适配器兼容当前支持的 message、reasoning、function_call 在不同 Responses 服务中的完成形状，不按供应商或模型名分支。OpenAI SDK 继续负责 HTTP、SSE 和 DTO 解码；Pulsara 只负责完成证据合并、可执行调用校验及 canonical 回放。

支持最终 response.output 独立提供完整结果、完整连续 item.done 序列独立提供结果，以及两者共同提供字段。未知工具类型或未知字段继续明确失败；字段合并不代表支持所有 Responses 产品能力。

## 单一路径

1. 非空 terminal output 按数组位置确定完整顺序；已经收到的 item.done 按 output_index 与对应项合并，允许 terminal 补齐尚无 item.done 的项，拒绝越界、重复或身份冲突。不按正文猜匹配项。
2. terminal output 为空、缺失或 null 时，必须有明确 completed 状态及非空、连续、全部结清的 item.done 序列。缺口、未完成 delta/part/item、不合法的 output 类型不能成为成功。
3. 同一项的合法字段双向取并集。只有一边提供的字段保留；两边相等只保留一份。可空身份/阶段/格式字段的 null 按未提供处理，非法类型不能覆盖有效值来隐藏错误。
4. message 的 content、reasoning 的 content/summary 按 part 位置合并；非空数组必须长度和顺序一致，空的内容数组可由另一份完整数组补齐。part 的正文与类型同时存在时必须一致。annotations/logprobs 的单边信息保留；两边非空且不同不拼接猜顺序。content_part.done 同样参与字段合并，保留原 item_id 关联及语义检查。
5. reasoning.encrypted_content 是不透明回放数据。两边提供合法非空字符串时选 item.done 的完整字符串，不要求两次加密字节相等；只有 terminal 提供时保留 terminal。null/空值不覆盖已有载荷，非法非字符串仍失败。公开 reasoning 文字与 summary 不适用这项冲突豁免。
6. 工具名、call_id、参数原字符串、消息角色、正文、非空身份、明确 phase/format 等真正冲突仍失败。参数不重新序列化，不修改已输出 delta，不执行未被最终接受的工具。

单边字段最终仍通过现有闭合 schema、类型、JSON 参数、来源顺序及投影校验。新增合并不能绕过原来的完成状态、取消、物理关闭、重试规则和资源边界。

## 回放、资源与实施

合并在新结果被接受前完成，生成唯一不可变 replay payload；之后的持久化、回读与后续请求使用同一份完整载荷。既有 epoch 的 SYSTEM、tools、历史消息及已保存载荷不得重写，不增加 rebase 边界。

使用现有真实 replay/source 兼容边界更新 source contract 为 v7，删除旧单向省略算法和其冗余常量，不保留新旧协商。无数据库形状、实体、ID、事件、后台任务或总历史上限变更。旧的内部 replay contract 不伪装为新 contract；不自动改写保存的数据或重置用户数据库。

content_part.done 保存合并所需的实际不可变 part 值，移除原来的整 part 指纹比较；沿用既有单次完成输出的字节边界约束该表示的总量。item.done、part.done 与最终合并表示分别受既有完成输出边界约束，不因两种完成事件重复承载同一正文而把可交付额度减半。合并后重新检查完整载荷，防止双边各自适配而并集超出边界。

## 验收

- 三种来源形式、output 省略/null/空、部分 item.done 与完整 terminal、双向补字段和空数组补齐；多项顺序与源索引不变。
- reasoning 加密字节不同、单边/双边缺失及非法类型；精确保留所选 opaque 字节。使用抓取的真实 OpenAI 流离线回放，并真实发送下一轮，验证载荷可被服务端接受。
- part 元信息单边补充不丢失，非空冲突、文本/参数/身份/phase/format 冲突继续失败；未知字段、未完成证据、重复/越界、合并后超额继续失败。
- canonical 保存/回读、原样 lowering、同 epoch 前缀连续、资源关闭与取消回归。测试迁移只改变已授权的字段缺失语义，保留或强化真实行为断言。

协议依据：[OpenAI Responses 流事件](https://developers.openai.com/api/reference/resources/responses/streaming-events#response.output_item.added)明确要求流式 reasoning 的后续回放使用 output_item.done 中的完整 encrypted_content。

## 验收结果

相关 290 项测试通过，覆盖适配器、字段合并、PostgreSQL 原子保存/原样回读、直接模型执行及 provider 前缀连续性；Ruff 与 diff 检查通过。旧测试中“缺少字段就拒绝”的三项断言按本规格迁移为保留完整字段的精确断言，正文、参数、身份和阶段冲突仍验证失败。

抓取的真实 gpt-6.1-terra 流中，两个 reasoning 加密载荷不同而其他字段相同；新路径离线接受该流，并精确保留 item.done 的字节。通过同一已保存配置，将所选原载荷原样用于真实下一轮，请求成功完成并按显式用户指令返回 REPLAY_OK。instructions/tools 保持一致，原输入保留为前缀，仅追加输出、诊断工具结果和用户测试指令；未执行视频工具。更早一次把 ACK 指令仅放进工具数据的测试得到“工具未执行，无法读取视频”的合理回复，该测试的原始失败断言与输出保留，没有修改证据。

证据位于 output/responses-terminal-mismatch/。验收未修改生产设置和用户数据库；检查时本地已有回放记录均为 Chat，不需要清库。
