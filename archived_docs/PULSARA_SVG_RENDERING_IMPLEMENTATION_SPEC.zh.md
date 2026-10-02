# 对话 Markdown 中的 SVG

日期：2026-09-28。状态：已实施；本轮按用户要求由用户进行交互验收，代理只运行静态检查与构建。

## 展示合同

- `MarkdownBody` 中的 `svg` fenced code block 默认显示图片；未标注语言、`xml` 或 `html` 的代码块以 `<svg>`（可带 XML 声明）开头时也进入 SVG 阅读组件。非 SVG 的普通代码、行内代码、数学公式和 `MarkdownInline` 保持原语义。
- Markdown 解析器产出的完整独立 SVG HTML 块也可以展示：它必须位于段落之外，具有完整根闭合，转换为同一种 SVG 代码节点；其他 raw HTML 仍不执行。跨 Markdown HTML 块或混合普通正文的 SVG 应使用 `svg` 围栏明确范围。
- 流式输出期间显示源码及“图表生成中”，结束后解析。多个图表沿正文顺序排列；一张图失败保留其原始源码和重试入口，不影响其他图。
- 复用 `DiagramBlock`：Mermaid 和 SVG 共用源码切换、复制、错误/等待状态、对象 URL 生命周期及已有 Lightbox 放大查看器。Mermaid 继续通过原 owner 处理主题、布局与 strict 清洗。
- SVG 保留作者的颜色和几何，不按主题重绘。正文内预览最大高度为 `min(60vh, 560px)`，仅是布局约束；放大后可查看完整图像，不限制 SVG 字节数或图表总数。
- 原消息及复制的源码不变。无文件写入、render 注册、新后端接口、provider 输入修改或持久化结构变更。

## 依赖与所有权

- 浏览器 `DOMParser` 拥有 XML 语法验证；只接受根元素为 SVG 且无解析错误的文档。省略默认 SVG 命名空间时为展示副本补全；依据合法 width/height 或 viewBox 给图片提供尺寸。
- 直接声明仓库现有版本 `dompurify@3.4.16`，按需加载。由 DOMPurify 的 SVG/SVG filters profile 和 XML 解析模式负责清洗；使用独立实例，不与 Mermaid 共享清洗配置或 hooks。不实现另一套 XML 解析器或 SVG 清洗器。配置依据 [DOMPurify 官方说明](https://github.com/cure53/DOMPurify#can-i-configure-dompurify) 与安装包 API。
- Pulsara 只负责路由、显示状态、尺寸与清理。清洗后的 SVG 通过 Blob URL 作为原生 `<img>` 显示，放大也使用图片；不把模型 SVG/CSS 注入主 DOM，不启用 arbitrary raw HTML。
- 这是 SVG 图片展示，不提供脚本交互或外部依赖加载；浏览器图片上下文行为见 [MDN SVG as an image](https://developer.mozilla.org/en-US/docs/Web/SVG/Guides/SVG_as_an_image)。需要脚本交互的内容继续使用已有隔离 HTML 可视化入口。
- 解析、清洗或图片解码失败时显示原始源码。替换源码、重试和卸载时撤销旧 URL，迟到结果不能覆盖当前图片。

独立 `.svg` 文件链接及 `![说明](本地路径)` 沿《会话文件链接与预览实施规格》的用户点击入口；本篇处理消息中直接给出的 SVG 源码。
