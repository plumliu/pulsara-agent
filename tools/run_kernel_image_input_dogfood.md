# K4 Kernel 图片真实验收

此脚本调用正式 `KernelHostCore`，图片经 Host 验证、canonical publication / hydration、compiler、final materialization 和正式 adapter 发出。模型调用、工具执行、压缩、恢复与 fork 均由生产 owner 执行。

```sh
.venv/bin/python -m tools.run_kernel_image_input_dogfood \
  --model openai/gpt-5.6-luna \
  --output scratch/k4-acceptance/luna-new-run

.venv/bin/python -m tools.run_kernel_image_input_dogfood \
  --model gpt-5.5 \
  --output scratch/k4-acceptance/responses-new-run
```

`--model` 必须精确匹配已保存的模型。若同一模型保存了多组 API 配置，额外传入 `--connection-id` 选择唯一连接；其模型 ID 必须与 `--model` 一致。输出目录必须是新目录，避免覆盖失败证据。默认 900 秒仅是本次有限验收运行的 deadline，不修改产品生命周期、重试或预算配置。模型名和连接 ID 只用于选择实验连接。

使用 `require_pulsara_home()` / `LocalSettingsStore` 读取保存配置；不写设置、不向环境变量导出密钥。复用既有 dogfood 的只读设置注入与临时数据库 helper：验证 admin/runtime 根 DSN 均为 loopback `pulsara`，创建独立随机数据库并应用 clean-v0，结束时仅删除本次数据库。工作区和 Pulsara home 也独立临时创建。

每组包含以下行为：

1. 纯文本历史，为随后真实压缩提供可回收内容。
2. 单条纯图提交，模型读取图片内数字。
3. Text/Image 交错、交换图片次序并重复同一图片，核对三个标签与数字的关联；文字 prompt 不包含答案。
4. 实际调用 `read_file`，核对文件结果、图片内容与多次请求的 SYSTEM/tools/messages 前缀连续性。
5. 手动压缩，确认 summary 请求包含原图，successor 仍保留规定窗口中的图片。
6. 关闭并重新打开 Host session，从数据库冷恢复图片；fork 后关闭父会话，子会话继续读取独立保留的原图。

脚本观察一次真实 adapter payload 构造的返回值，并在 `httpx.AsyncClient.send` 观察 SDK 生成的实际 JSON body；两处都原样委托生产函数。最终逐次核对 HTTP 的 context-bearing 字段与 frozen materialization 相等。图片 data URL 解码后的 bytes、MIME 和 occurrence 次数必须与完整 typed input 一致；最终 JSON 字节数与 quote 相等。不会修改请求或为 provider 加专用编码路径。

`report.json` 保存每次实际调用的目标、完整冻结投影、SDK payload、HTTP body、终止状态、usage、模型输出和检查结果。合成原图也保留在同目录。通过原凭据 scrubber 仅遮盖保存的真实密钥值，不遮盖正文和回复；证据目录沿已有 ignore 规则保留在本地。

隔离 wheel 验收使用相同脚本：`uv build --wheel` 后，在仓库外新建 uv 环境，按 `uv.lock` 导出的 runtime constraints 安装 wheel；只复制本脚本、`run_model_switch_handover_dogfood.py` 和 `probe_vision_tokens.py` 三份 harness 到该目录的 `tools/`，不复制 `src/` 或 tests。清除 `PYTHONPATH` 后在外部目录运行，报告的 `package_path` 必须位于新环境 `site-packages`。这同时验证已打包的 schema、Pillow worker 和 Kernel 运行资源。

真实服务结果只证明记录中的模型和请求。本脚本不能替代 I01–I44 的确定性资源、并发、取消、模态交接和数据库完整性测试，也不代表 U1/U2 浏览器图片产品已完成。
