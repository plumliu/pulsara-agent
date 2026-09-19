# Kernel 图片输入 U1/U2 验收记录

日期：2026-09-15。权威：`AGENTS.md`、`PULSARA_KERNEL_IMAGE_INPUT_AND_OPENAI_WIRE_ADAPTER_DESIGN.zh.md` 第 14 节 U01–U11、`PULSARA_FRONTEND_APPLICATION_SPEC.zh.md` 与当前生产代码。

状态：**U1/U2 验收通过**。浏览器输入已 hard cut 为单一有序 Text/Image 内容，经 RuntimeConnection、browser bridge、Protocol-v3 与 Host 进入 K2/K3 的 canonical/compiler/wire 链路。旧字符串命令、attachment side channel、自动 Figure 正文和图片失败后的纯文本 fallback 均未保留。

最终 composer 不显示添加图片、撤销或重做按钮，也没有仅供图片添加使用的隐藏文件选择器。图片从实际 paste 事件或文件拖入进入；撤销/重做使用 Tiptap/ProseMirror 标准快捷键。最后一次 headed Chromium 检查实际拖入 PNG，观察到 `Text / Image / Text`，`Meta+Z` 删除尾部编辑、`Meta+Shift+Z` 恢复；composer 内工具栏、文件 input 和上述三个按钮的数量均为 0。截图保存在已忽略的 `output/playwright/u1-u2/composer-final-no-toolbar.png`。

## U01–U11 对照

| 项 | 验收证据 |
| --- | --- |
| U01 | `prompt-draft.test.tsx` 覆盖纯图、原始文字/换行、选区替换、删除、快捷键撤销/重做与 IME/Enter 边界；真实浏览器验证 24 行时编辑区由 24 px 增至 250 px 并滚动、缩短后回到 24 px，回到最新控件随 composer 高度变化。 |
| U02 | 单元覆盖 paste/drop、多图读取乱序、删除后迟到读取、session 切换、失败节点、内部复制、普通移动不复制及外部 HTML/Markdown URL 仅作文字；真实浏览器通过文件拖放插入原图。 |
| U03 | 单元覆盖纯图、交错、重复 occurrence 与唯一 serializer；真实浏览器的 Chat 和 Responses 各提交 `Text/Image/Text/Image/Text`，实际 HTTP 图片 bytes、MIME、顺序与输入文件一致，自动 Figure 标签未进入请求。 |
| U04 | `pulsara-app.test.tsx` 与草稿 store 覆盖发送期间编辑、跨 session 迟到结果、同 DraftSession 身份清理、明确拒绝、丢失 ACK 后查询原命令、Plan/权限与原排队/显式引导动作。 |
| U05 | 真实浏览器排队项只显示原位置 Figure 链接和图片总数；编辑先读取完整图片、确认取消，再恢复真实 `Text/Image/Text` 节点，选择位于尾部。前端测试覆盖两 occurrence 逐一按 queue owner/ordinal hydrate、未知取消与草稿冲突。 |
| U06 | `test_local_web_http_surface.py` 与 `test_local_web_browser_bridge.py` 分别在实际 JSON body 和 protobuf frame 的 8 MiB 两侧验证 base64/escaping/包装；确定 413/frame 拒绝保留 exact 草稿并显示容量原因。Host 的 D2 验证继续由 K2/K4 证据覆盖。 |
| U07 | 真实浏览器历史消息显示右对齐缩略图带、正文原位置 Figure 链接与原图弹窗；单元覆盖窄带 `+N`、失败/重读和迟到读取。Runtime/bridge 测试覆盖分页、刷新、observer/fork 的 owner/occurrence 读取与跨 session、仅 digest 错绑拒绝。 |
| U08 | 草稿、重复图片、undo/redo、提交快照、queue restore 与 object URL 单元覆盖；展示读取在 unmount 后完成时不创建或安装 URL，失败只在用户明确重试时重新读取。 |
| U09 | 保存配置只读加载后，headed Chromium 经真实 local HTTP/bridge/Host 调用 `openai/gpt-5.6-luna` Chat 和 `deepseek-flash` Responses。两次交错输入均不在文字中透露图片数字，模型分别回答 `756,691` 与 `691,756`；两次 HTTP 均为 200 且终止为 `COMPLETED`。 |
| U10 | 前端保留原 model picker 与下一 NEW_TURN 选择，不增加许可、模式或直接 steer 快捷键；typed 新图片拒绝保留草稿。Tier 1/2/3、P、cold、recent 与失败不推进复用已经完成的 K4 Kernel 验收。本轮依用户收窄要求，没有重复跑整套浏览器模型交接组合。 |
| U11 | Figure 只按每条完整 typed 内容中的 Image occurrence 编号；单元覆盖重复图片、窄带折叠、失败/重试及 owner 精确绑定。真实历史与队列的 Figure 链接打开同一原图；手打 `[Figure 1]` 在实际提交中仍是普通 Text。 |

## 正式请求证据

完整、已去除凭据值的浏览器运行记录保存在已忽略的 `scratch/u1-u2/browser-evidence/final-browser-provider-report.json`。`validation-final.json` 对其中两次正式调用重新执行以下检查：

- Chat：两张 PNG 为 11,534 / 11,644 bytes，SHA-256 分别为 `4abc9929…ca9166f8` / `e3f3b3e9…91dbc5e5`，模型回答 `756,691`；
- Responses：同两张图反向排列，模型回答 `691,756`；
- Runtime 观察到的 frozen projection 等于 SDK payload projection；最终 HTTP body 等于 SDK payload 加传输层 `stream: true`；
- 当前 user item 的类型严格为 Chat 的 `text/image_url/text/image_url/text` 与 Responses 的 `input_text/input_image/input_text/input_image/input_text`；data URL 解码字节逐一等于原文件；两套实际 wire 均无 `[Figure `。

最终浏览器截图另保存在 `output/playwright/u1-u2/`：已发送 Chat/Responses、历史原图弹窗、排队 Figure 链接、队列编辑恢复，以及无工具按钮的最终 composer。中途发现并保留了两项真实失败诊断：二进制图片读取曾被 UTF-8 校验错误拒绝；失败缩略图曾在 observer 刷新时自动重读。生产修复分别将 UTF-8 约束限于文本读取，并将图片重读限于用户明确动作。

## 自动化与构建

- `cd frontend && npm test`：17 files，263 tests passed；
- `cd frontend && npm run lint`：通过；
- `cd frontend && npx tsc --noEmit`：通过；
- `cd frontend && npm run build:local`：通过，生产静态资源已更新；
- `cd frontend && npm run build`：Vinext 五阶段构建通过；
- `.venv/bin/python -m pytest -q tests/test_local_web_browser_bridge.py tests/test_local_web_http_surface.py tests/test_stage2_protocol_v3.py`：38 passed，6 条既有 aiohttp deprecation warnings；
- 相关 Python Ruff 与 `git diff --check`：通过。

本轮没有重跑 K4 的完整 1979 项 Kernel/PostgreSQL/provider 矩阵。其冻结的模型交接、canonical、GC、并发与资源证据继续由 K4 验收记录承担；U1/U2 的新增 Python 只改 browser/protocol/content-read 边界。最后一次删除 composer 工具按钮后没有重跑 provider，因为该变更只删除展示控件及专用 file input；同一最终构建另以真实浏览器文件拖放、快捷键和截图复验，未改变 typed serializer 或 wire。

相机、屏幕采集、外部 URL 图片、裁剪/转码、OCR、分帧上传、草稿持久化和非图片媒体仍不在首版范围。
