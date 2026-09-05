# 记忆 dogfood 暴露的 ROOT 中断修复

日期：2026-09-05。基线 `291eee83`，与此前未提交的记忆管理实现共存。
本记录仅说明本轮 runtime 修复，不代替记忆删除的完整验收。

## 根因与修复

1. `KernelMemoryToolPort.invoke` 在已产生 tool attempt 后，把参数/引用拒绝返回为
   `INVALID_ARGUMENTS`，关闭 owner 返回 `TOOL_UNAVAILABLE`。两者都属于无 attempt
   的前置结果，违反 `PreparedToolResultAcceptance` 的既有判别联合。
   真实堆栈：`host._run_owned_root_task → runner.run_accepted_turn → tool_batches.execute
   → _await_tool_result_settlement → _settle_known_tool_result
   → build_prepared_tool_result_acceptance → PreparedToolResultAcceptance.__post_init__`，
   最终为 `ValueError: prepared tool result attempt/state union is invalid`。
   结果尚未提交就中断 ROOT，模型当然无法拿到拒绝原因并继续回复。
   现分别返回 `APPLICATION_ERROR`、`SYSTEM_ERROR`。同类 TODO scope 拒绝同步改为
   `APPLICATION_ERROR`，未改变未知副作用或其他异常处理边界。
2. 旧会话继续发送时，还触发 `canonical frontier item count exceeds its sequence cut`。
   canonical entry 数和 lowering 后 item 数不是同一计数；多 block 与 interrupted
   tool closure 都会破坏旧不等式。删除该错误比较，保留 sequence 非负、单调性、
   ordered prefix 精确相等及既有 fingerprint 格式检查，不重建已安装 epoch。
3. ROOT detached task 原先取出并丢弃 exception；现普通日志保留异常和堆栈，取消不报错。
4. UI 原先把非 running 一律显示为完成。协议现在投影既有最新 ROOT turn 行，
   中断显示“已中断”及可继续提示，不新增数据库状态或事件。
5. UI 原先 FIFO 配对工具结果，会把新结果挂到旧轮次未完成调用上，并从正文猜测成功。
   协议现在携带既有 `tool_results.tool_call_entry_id/tool_call_id/result_state`，
   ROOT 与 subagent 都精确匹配。删除 FIFO 与正文猜状态；分页中缺失调用时独立显示结果。

本轮未新增数据库表、列、约束、event、job 或 fingerprint 机制。只更新既有 wire schema
及其既有摘要。保留记忆来源语义：GLOBAL proposal 不能引用 CURRENT_CONTEXT_BOUND
tool result；`based_on_memory_ids` 可以引用合法全局记忆，无须同时填写工具引用。

## 真实 Chrome / provider 验证

使用用户 Chrome、正式本机服务 `http://127.0.0.1:56081/`，模型配置
OpenRouter / `openai/gpt-5.6-luna` / Chat / high。未打印 API key。

- 原会话 `session:d471e58ce2ca43b08f4315575b6fd0c0`：14:22、14:23 的 remember
  中断，14:25、14:35 后续请求也无回复。不是单纯 governance 未完成。
- 新测试会话 `e432a102`：14:36 仅使用 BASED_ON 引用成功并得到自然语言；14:37
  同时引用真实 `tool:2` 后复现上述 attempt/state 异常。
- 修复后，原会话 14:47 得到真实模型回复“会话恢复测试正常”。
- 原会话 14:48：memory_search 后，用真实引用触发相同 GLOBAL 拒绝；canonical
  工具结果为 `APPLICATION_ERROR`，模型正常回复：

  > 检索已找到蓝桉读书会背景记忆；但保存被拒绝：GLOBAL 记忆不能引用当前会话绑定的检索结果。已停止重试，未修改其他记忆。

- SQL 核验 14:47、14:48 两轮都是 `COMPLETED / COMPLETED`，14:35 仍保留
  `INTERRUPTED / FOREGROUND_EXECUTION_INTERRUPTED`。没有改写旧失败历史。
- 最终构建重启后刷新 Chrome：旧未完成 remember 没有被显示成成功；14:48 search
  完成、remember 执行失败，展开显示 `GLOBAL memory cannot cite a current-context-bound
  ToolResult`，随后自然语言完整保留。另一个未恢复会话显示“已中断”提示。

本轮未重置数据库。两条 `MEM-DOGFOOD-0905` 测试记忆及两个测试会话保留供核查。
没有把“工具拒绝后能回复”表述为全套真实模型级联删除验收通过。

## 验证命令与结果

均在仓库根 `.venv` 下运行；PostgreSQL pytest 使用独立临时数据库。

```sh
.venv/bin/pytest -q tests/test_memory_tool_result_rejection.py tests/test_round3_1_provider_input_prefix_continuity.py
# 早期 14 passed；随后增加日志回归，包含在最终 151 项中。

.venv/bin/pytest -q tests/test_stage2_conversation_runner.py -k memory_bad_citation
# 1 passed, 93 deselected；覆盖工具拒绝后继续回复及下一轮继续，真实 memory port + PG。

.venv/bin/pytest -q tests/test_stage2_conversation_runner.py tests/test_stage2_canonical_reader.py tests/test_stage2_protocol_v3.py tests/test_round3_1_provider_input_prefix_continuity.py tests/test_memory_tool_result_rejection.py tests/test_round7_model_visible_failure_and_tool_observation.py
# 首轮 150 passed；最终代码 151 passed in 39.54s。

.venv/bin/pytest -q tests/test_stage2_conversation_kernel_postgres.py -k snapshot
# 2 passed, 29 deselected。

.venv/bin/pytest -q tests/test_round8_advisory_memory.py tests/test_local_web_memory_management.py tests/test_memory_management_postgres.py
# 54 passed in 159.73s；13 条 aiohttp 弃用警告。

.venv/bin/pytest -q tests/test_memory_tool_result_rejection.py
# 5 passed。

.venv/bin/pytest -q tests/test_lightweight_todo_refinement.py tests/test_memory_tool_result_rejection.py
# 32 passed。

cd frontend
npm test -- --run app/pulsara-app.test.tsx
npm test
# 全前端首轮 91 passed；最终含精确工具关联回归 92 passed。
npx tsc --noEmit
npm run build:local
# 均通过，打包静态资源已更新；既有 bundle size 警告保留。
```

另：修改的 Python 源码/测试 Ruff、`git diff --check`、
`.venv/bin/python tools/generate_terminal_protocol_contract.py --check` 均通过。
未跑全仓 pytest；重复测试数不累加为独立覆盖数。

迭代失败：新 runner fixture 最初漏传 `context_source_collector`；新 UI fixture
最初赋值到不存在的 `adapter.projected` 而非 `adapter.connectionValue`。两者均修正测试
接线后通过，未放宽生产校验或行为断言。真实生产失败则是上文两条明确堆栈。

最终本机服务启动命令：`.venv/bin/pulsara app --port 56081 --no-open`。
未提交、未 push。

## 后续页面修复与用户要求清库提交

详情切换改为保留面板原位更新、重复选中不请求，删除自动 scrollIntoView；读取失败保留旧详情，
关闭/后发请求使旧响应失效。Chrome 坐标点击验证切换与重复选择后 scrollTop 均为 0，范围栏
位置始终为 183.5px。新增三项交互回归后，前端 95 passed，tsc/build:local/diff check 通过。

用户随后明确要求 reset 整个生产数据库并 stage/commit。重新核验目标为
`localhost:5432/pulsara`（服务端 `::1:5432`），无其他数据库连接且本机应用端口未监听。
删除整个 `pulsara_v3` schema 和 `public.pulsara_schema_migrations`，执行
`.venv/bin/pulsara db migrate`、`.venv/bin/pulsara db verify --deep`，分别返回 current、verified。
重建后逐表查询确认所有应用表均为零行。上述两个会话、两条测试记忆及全部关联数据已永久清除，
不再保留；本地 settings/API key 和工作目录文件未修改。未创建备份，未 push。
