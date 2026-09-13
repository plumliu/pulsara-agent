# 图片输入 token 实验

`probe_vision_tokens.py` 为 D1 提供测量材料，不启用 kernel 图片输入，也不把测得的 provider 系数写入生产 estimator。

## D2 多比例本地素材

只生成 D2 的比例、像素面积、编码和内容对照素材：

```sh
.venv/bin/python -m tools.generate_image_resource_fixtures
```

生成器复用本文件对应 probe 的合成画面与 Pillow 编码器，不读取生产配置、不调用 provider，也不改变旧 smoke/full/d1 矩阵。输出到新建的 `scratch/image-resource-fixtures/<UTC 时间>/`，包含原始图片、`manifest.json`、`fixtures.csv`、中文说明和四张预览图。

覆盖 16:10、16:9、3:2、4:3、5:4、21:9、1.91:1 的精确比例，横竖配对；另有近似等面积对照、非常见比例、极端窄边、不整齐尺寸，以及 28/32 网格边缘附近的组合。A4、6/5/7 寸照片均覆盖 150/300 DPI 和横竖方向；6 寸分别记录 6×4 英寸与用户指定的 15×10 cm。A4 的理想长短边比为 √2，物理尺寸和整数像素的取整误差保留在说明及清单中，不声称整数宽高能精确表示 √2。

所有文件都会重新读取并完整解码检查；7 对 PNG 压缩控制额外验证像素相同。清单中的 RGB 像素面字节、base64 payload 字节各自保留单位与范围，不能作为进程峰值内存或整个 wire 的实测值。本轮素材生成不代表已经完成 D2 接纳、并发解码和 compaction 的资源实验；透明、动画、损坏及其他元数据场景另行补充。

生成素材后，可运行独立的 D2 本地资源实验：

```sh
.venv/bin/python -m tools.probe_image_resources \
  --fixtures scratch/image-resource-fixtures/20260913T051948892348Z/manifest.json
```

每个测量启动新的子进程并预热 Pillow，记录操作系统 RSS 高水位增量和耗时；默认重复两次、单进程实验 deadline 30 秒。除了原素材，它还生成大像素小文件、透明/灰度/16 位模式、渐进式 JPEG、动画、损坏文件和 PNG 大文本元数据。批次对照覆盖 1/2/4/8 并发、及时释放/全部保留，以及共享/独立物化的重复图片。所有工作都在本地，不访问模型、设置或数据库。

输出位于新建的 `scratch/image-resource-probe/<UTC 时间>/`，保存计划、素材、逐次 JSONL 和 CSV。它测量的是独立解码路径与合成 wire，不是生产 canonical hydration 或真实图片 compaction。2026-09-13 基础实验及两次按发现追加的补测共 666 次，完整结果见 [D2 资源实验报告](../scratch/image-resource-probe/20260913T053759102238Z/report.zh.md)。当前生产代码尚未支持图片内容，不能用本实验代替正式 kernel 接纳/压缩验收。

## D1 Provider 实验

从仓库根目录运行，依赖由 `uv sync --group dev` 安装，其中 Pillow 仅为开发依赖。

```sh
# 只生成素材和实验 manifest，不调用模型
.venv/bin/python tools/probe_vision_tokens.py

# 调用已保存配置；默认排除目前不可达的内网 Qwen3.8-27B
.venv/bin/python tools/probe_vision_tokens.py --run

# 扩充尺寸、长宽比、格式和边界样本
.venv/bin/python tools/probe_vision_tokens.py --suite full --run

# D1 定向补测：只选择四组稳定配置，每组 19 个案例、重复两次
.venv/bin/python tools/probe_vision_tokens.py --run --suite d1 --repeat 2 \
  --concurrency 4 --output-tokens 1024 \
  --connection qwen3.8-flash --connection meta/muse-spark-1.3-contributor \
  --connection openai/gpt-5.6-luna --connection deepseek-flash --exclude-model gpt-5.5

# 只测指定模型和图片，仍自动添加无图基线
.venv/bin/python tools/probe_vision_tokens.py --run \
  --connection qwen3.8-flash --case scene_1024 --case scene_1024_png_raw

# 重复测量，或显式比较 detail（默认只有 auto）
.venv/bin/python tools/probe_vision_tokens.py --run --repeat 2 \
  --detail auto --detail high --exclude-model gpt-5.5

# 仅当回到局域网后，显式选中内网模型
.venv/bin/python tools/probe_vision_tokens.py --run --include-lan --connection Qwen3.8-27B

# 本地实验控制检查，不访问 provider
.venv/bin/python -m pytest tests/test_vision_token_probe.py -q
```

默认 smoke 每组 8 个请求：同文本无图基线，512/1024/2048 方形仪表盘图，同像素未压缩 PNG，JPEG 85，1024 随机噪声图，同图出现两次。PNG 压缩级别 0 和 9 使用完全相同像素；JPEG 是有损控制，不能宣称像素完全相同。噪声图用于改变内容熵，不能单独隔离编码大小的影响。Full 额外覆盖 32/384/512/768 附近尺寸、4K、相同像素面积但不同长宽比、长条图、JPEG 30 和 WebP 80。微小图的 OCR 失败不等于模型不支持图片。

D1 suite 每组 19 个案例：单 USER 消息基线、三 USER 消息基线、17 个图片案例。包含 64/128/256 小图、399/400/401 与 1008/1009 边界、1365×768/1920×1080/1080×1920/2560×1440、1024 方图重复 3/4/8 次、128 方图重复 8 次，以及三张 1024 方图分布于三条 USER 消息。跨消息时每轮重复同一提示词，中间加入固定 assistant 文本 `Acknowledged.`；基线只移除图片，完整保留文本与消息结构。它是静态多消息输入的计量实验，不是有真实历史回复的 kernel dogfood。

图片均用 Python/Pillow 生成，不包含用户数据或外部下载内容。仪表盘左上方单词为 ORBIT，提示词不透露答案；无图/噪声预期 NONE。这只是基础视觉通路检查，不是视觉质量基准。

配置只经 `LocalSettingsStore` 和 `require_pulsara_home()` 读取。复用既有 SDK client、认证和进程凭据边界；不修改设置、不接触 PostgreSQL、不读取环境 API key。当前实验构建独立 multipart 请求，因为正式 kernel 仍未支持图片。实验使用保存的 model、base URL、wire API 和认证；reasoning 保持 provider default。每次请求为独立上下文，Responses 设置 `store=false`。

`--timeout` 默认 90 秒，`--output-tokens` 默认 256，`--concurrency` 默认 2；这些是可调的单次实验参数，不是新增生产限制。SDK 自动重试关闭。每组、每轮、每个 detail 先跑基线：基线失败或缺少 input usage 时，显式记录跳过后续图像样本。其他组继续。增加 output budget 可排查推理耗尽输出预算导致的空答案。

输出在 `scratch/vision-token-probe/<UTC 时间>/`，默认不覆盖旧目录：

- `manifest.json`：所用配置身份/地址（无密钥）、图片及编码参数、依赖版本、实验参数。
- `images/`：实际发送的素材。
- `requests/`：每次完整 `request.json`、SDK 返回的完整 `events.jsonl`、`result.json`；不保存认证头。
- `results.jsonl`：每个样本结束立即追加，失败时已完成结果仍保留。
- `results.csv`、`report.md`：完成后汇总。

所有保存的文本按实际配置凭据值精确遮盖；不删掉错误正文、回答、推理或 usage。`request_json_utf8_bytes` 是实验完整 JSON 参数体按紧凑 JSON 序列化的字节数，不声称是捕获的 HTTP 实际字节，也不等同于 Pulsara `final_wire_utf8_bytes` 的 context-bearing projection。

`input_delta_vs_text_baseline = 图片请求 input_tokens - 同轮、同 detail、同消息布局的无图基线 input_tokens`。它包含图片相关 framing，是 provider 报告的输入计量增量，不证明模型内部 visual sequence 的长度。缓存 token 仍包含在总输入中，不扣除；缺失 usage 用空值，不当作零。返回 model/provider 与基线不同的行不计算增量。上游未暴露内部路由时，无法证明两次请求落在同一后端；可用重复实验观察波动。

负差值保留在 `raw_input_difference`，同时将 `input_delta_vs_text_baseline` 置空，并标记 `comparison_issue`。它意味着当前基线不能解释该请求，不能当作负的视觉成本。即使差值为正，上游动态附加提示词或隐式路由也可能使其失去校准意义；不稳定网关应单独排除，不能靠挑选正常行拟合算法。

`answer_match` 独立于 usage 成功状态：有效计数、空回答、长度截断和 OCR 正确与否分别记录。生产 D1 的参数选择应同时考虑误差范围、可接纳尺寸和压缩频率，不能从一次测量导出跨 provider 的准确计数或安全上界。
