# artifact_export 实施验证

日期：2026-09-30。对应 [冻结实施合同](../../PULSARA_ARTIFACT_EXPORT_IMPLEMENTATION_SPEC.zh.md)。

## 实现与边界

新增 `artifact_export(artifact_id, path)`，按 canonical session/workspace artifact owner 验证来源与 UTF-8 字节，原样创建新文件。保留字符分页 `artifact_read`。复杂处理由已有文件工具与 terminal 完成；没有添加自动 JSON 解析、查询 DSL 或内嵌脚本引擎。

导出受 filesystem_write 权限与路径 scope 约束，不覆盖目标；共享原子新文件创建使用绑定父目录 FD。成功响应按 FULL 交付且不递归归档，不标记源记忆已经被模型阅读。导出文件为普通本地副本，权限与生命周期由文件系统管理。coverage 表示保存范围，不能把保留快照当完整原始输出。

## 自动验证

较大范围回归：**444 passed**（194.69 秒，既有 aiohttp DeprecationWarning 1 条）。覆盖 artifact、文件编辑、projection、model input compiler、长时 PostgreSQL/compaction、capability、terminal、fork。该批次发生在最后补充清理失败与取消测试之前，不能把不同批次数量简单相加。

最终修订后的聚焦验证：**190 passed**（7.96 秒）；所有改动 Python 文件通过 Ruff，`git diff --check` 通过。该批次覆盖最终代码及审查修复。命令：

```sh
.venv/bin/pytest tests/test_artifact_export.py tests/test_content_revision_line_edit.py tests/test_round1_tool_output_artifact.py tests/test_round7_1_provider_visible_tool_result_projection.py tests/test_stage2_conversation_runner.py::test_round1_provider_rematerialization_uses_preview_and_scoped_artifact tests/test_stage2_terminal_host_lifetime.py -q
```

新增测试覆盖：

- 空正文、BOM、CRLF、NUL、多字节、JSON 重复键、任意扩展名、大正文逐字节保存。
- canonical source edge、跨 session/workspace、缺失与损坏内容。
- 完整/保留快照、精确 FULL、非递归与 memory IDs。
- 权限 preset、显式路径、已有文件/目录/符号链接、并发创建、父目录替换。
- 发布前失败、发布成功但调用回执异常、发布后 fsync/最终 close 失败。
- ROOT/child 真实调用在发布后取消：保留 exact success，只创建一次，inline 标记保留。
- 同 epoch 前缀连续性，以及首次安装工具面时 terminal 不可用的导出/文件读取链路。

## 真实模型方法

使用保存的 `openai/gpt-6-luna` 模型配置，通过正常 LocalSettingsStore/有效 Pulsara home 只读加载。每例独立 workspace/session，使用已验证的本地临时 PostgreSQL 数据库；完成后关闭资源并删除临时数据库。原始保存设置未更改。

可重跑脚本：[run_artifact_export_dogfood.py](../../tools/run_artifact_export_dogfood.py)。示例：

```sh
.venv/bin/python tools/run_artifact_export_dogfood.py --phase candidate --output /tmp/artifact-export-candidate.json
.venv/bin/python tools/run_artifact_export_dogfood.py --phase baseline --cases small,log,json,multi --output /tmp/artifact-export-baseline.json
```

每例先让真实模型执行一次不透明数据源命令保存结果，下一轮提出自然任务。除已有文件场景明确要求本地副本外，不强迫导出、不提供脚本。baseline 在新工具面安装前移除 export，保留现有分页，未修改任何已安装 epoch；它是工具可用性的消融实验，不是旧 Git 版本的完整复刻。初版 small 的答案已在 preview 中，不能验证补读，因此扩大源正文，并用 baseline_small/clarified 补跑。

原始记录：[baseline.json](baseline.json)、[candidate.json](candidate.json)、[baseline_small.json](baseline_small.json)、[clarified.json](clarified.json)。保留实际提示、回复、工具参数/结果、模型 usage 与错误，去掉实际凭据值。

## 对照结果

工具调用数包含初次源命令；字节是 canonical tool result JSON 的序列化量，**不是精确 provider wire 正文量**。耗时为该例两轮之和。输入 token 为 recorder 报告的总量，不区分缓存命中，不能用于声称计费节省。

| 任务 | 分页：调用/结果字节/秒 | 导出可用：调用/结果字节/秒 | 正确性与观察 |
| --- | --- | --- | --- |
| 小段补读（补跑） | 2 / 19,813 / 19.64 | 2 / 14,812 / 17.31 | 都用 artifact_read，得到 R74X，无需导出 |
| 长日志定位与邻行 | 6 / 143,428 / 42.74 | 3 / 11,319 / 22.36 | 两者正确；候选自主 export + terminal |
| JSON 分组统计 | 4 / 93,179 / 65.73 | 5 / 16,866 / 35.26 | 分页 beta 错为 7306；候选为正确 8285；alpha=8097、gamma=7194 |
| 多份结果关联 | 6 / 151,584 / 38.90 | 9 / 85,502 / 53.86 | 两者正确；候选调用更多且更慢，不能宣称普遍提速 |

JSON 首轮候选脚本误把终端保存正文当 `{output: ...}` 外壳，KeyError 后检查并修正。因此工具描述明确导出的是终端正文，解析前检查内容；补跑得到正确结果（5 次、15,983 字节、33.12 秒），外壳误解未复现，但模型自己的 Python `int.__iadd__` 用法导致 AttributeError 后重试。API 没有消除模型编程错误。

## 其他场景

- 普通日志与无效 JSON：4 次调用，正确提取 ERROR 和末尾 `status={unfinished`，没有按扩展名强制解析。
- RETAINED_SNAPSHOT：4 次调用，零命中结论明确限定在保留字节 `[64676,130212)`；未声称丢失前缀没有目标。为产生该夹具，只对该源命令的 TerminalOutputOwner 注入 65,536 字节保留窗口，不修改产品默认上限。
- 已有目标：6 次调用；模型先检查存在性，保留 `occupied.txt`，显式选 `occupied_copy.txt` 后正确统计。并非模型实际触发了覆盖拒绝；拒绝由自动测试证明。
- READ_ONLY：首个失败记录已经可见，模型直接回答 alpha，没有发起导出；真实权限拒绝路径由自动权限矩阵证明。
- no_terminal：后续指令要求不用 terminal，模型使用 artifact_read 正确回答。这里是**指令约束**，不是运行时移除能力；实际 terminal 缺席由首次 seal 前移除该工具族的集成测试证明。
- 初版 small：baseline/candidate 均仅 1 次源命令、答案可见；只作夹具记录，不算补读成功证据。

结论：本次样本验证了两入口可以自然共存，导出支持现有脚本处理并减少部分任务的正文搬运。没有证据承诺所有任务减少调用、降低成本或提速。没有因此扩大查询 API。

## 最终代码审查与收尾

由原 GPT-6 Astra xhigh design reviewer 独立检查代码。发现一个 P2：最终 close 抛错可能将已发布文件误报为未创建，并跳过另一 FD 的关闭。已改为两个 FD 均尝试关闭，错误携带发布事实，并新增故障注入回归。另按审阅建议补充 ROOT/child 发布后取消的真实调用验证。

最终复审结论：**无剩余阻塞问题**。reviewer 独立重跑最终 export 测试 **39 passed**（2.28 秒），确认关闭失败修复、ROOT/child 取消以及 terminal 缺席链路。此前 reviewer 独立三模块 137 passed；这些批次不相加。主线程最终聚焦集为上文记录的 190 passed。实施合同状态更新为已实施并通过本轮验证；收益结论仅限上述样本。

## 后续提示词调整（2026-09-30）

按用户反馈，工具说明、artifact 预览、provider lowering 与 terminal 指引统一为：默认优先 artifact_read 分页，信息足够即停止；反复分页繁琐或需要复杂提取、聚合、脚本时再选择导出。不强制先读一页，不改变执行权限、API 或已安装 epoch 的前缀。README 与实施合同同步。

本次说明调整的回归：`test_round1_tool_output_artifact.py`、`test_round3_structured_model_input_compiler.py`、`test_terminal_cognitive_subtraction.py` 共 **128 passed**（10.02 秒），Ruff 与 diff 检查通过。未重新运行真实 provider；上文原始样本对应调整前提示词，不能用来断言本次优先级调整改善了模型行为。
