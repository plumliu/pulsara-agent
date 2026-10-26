# Pulsara 可视化命名与提示词边界

日期：2026-10-27。范围：用户指定的两个模型可见名称，以及随后明确授权的单一发布入口与 SYSTEM/数据分析 Skill 职责划分。

## 当前合同

- 对话发布围栏名称为 `pulsara-viz`。每个显式闭合的顶层围栏代表一个图组，每行一个已有图片/HTML 路径或图片引用；分组、冻结发布和 ordinal 规则保持。
- HTML 显式内容根属性为 `pulsara-viz-root`，例如 `<main pulsara-viz-root>...</main>`。实时布局和截图共用同一根选择器，原有可见性、唯一根与自动布局规则保持。
- 按仓库开发期 hard-cut 规则移除旧名称识别，不保留别名。已保存消息与 HTML 不重写；旧围栏按普通代码块处理，旧根属性不再作为显式根，仍可适用原有自动布局。
- Markdown 依赖继续负责块结构；现有 Pulsara 适配负责闭合、顶层、图组绑定。浏览器继续负责布局，原有共享函数负责根选择。无需新解析器、状态或持久化字段。
- 内部环境/布局消息名称、`window.PulsaraVisualization` API、数据库与结果字段不属于此次改名。
- 更新后的 SYSTEM 仍只通过既有 cold epoch 或明确采纳的 compaction successor 安装；不修改运行中 epoch 的 SYSTEM/tools/messages。

## 实施范围

同步后端发布解析器、前端 Markdown 标记解析器、共享 HTML 根选择器、实时布局脚本、SYSTEM、数据分析 Skill 参考文档及例子。相关测试和 dogfood 输入使用新名称；归档历史文档保持原样。

共享 Markdown fixtures 覆盖前后端一致识别，并核对旧围栏不占用新图组 ordinal。浏览器用例覆盖显式新根、隐藏/动态根、自动布局与截图。前端构建同步已提交的静态资源。

验证记录保存在 `output/pulsara-viz-rename-20261027/`。

## 实施结果

已完成上述命名切换及静态资源重建。发布/冻结/HTML资源/预览/实际浏览器布局测试 73 项通过，历史分支和 native replay 测试 6 项通过；4 个前端测试文件共 233 项通过。桌面 build、受影响生产 Python 文件 Ruff 和 git diff --check 通过。构建保留原有的大 bundle 提示，没有新增 provider 调用或改写已有会话。


## 展示指引与按需参考（2026-10-27）

- SYSTEM 以 `pulsara-viz` 为唯一教授的图形发布入口，不再介绍 SVG/Mermaid 围栏或其示例；前端原有 SVG/Mermaid 渲染能力保留。
- 支持的图片直接发布；自绘插图、图解与交互内容使用 HTML，内部绘制技术与布局由模型选择。明确请求源码或文件时仍按用户要求交付。
- 常驻规则保留围栏格式、支持对象、图组切换、独立图组与共享控件、冻结发布、无网络访问、局部资源路径和 `pulsara-viz-root`。保留文字/表格、数学公式、当前文件链接的用途。
- 不新增 Skill。数据分析 Skill 的 visualization-render.md 聚焦图表选型、结论与证据、多视角和有效交互；按问题组织紧凑图组，避免把分析做成长期滚动的单一 HTML，也不把需要同时比较的证据藏在替代图页中。
- 现有数据分析 Skill 目录新增 references/html-environment.md，承载可选宿主 API、主题、动画与截图语义。SYSTEM 提供直接定位方式；该参考可由普通绘图任务直接读取，不要求采用数据分析流程。
- SYSTEM 除 Communication 段外逐字保持。工具定义、权限、前端运行行为与 epoch 安装边界均不变。

本轮复用 bundled Skill 实际同步/文件读取测试，并把新参考加入已有可读资源清单：13 项测试通过；Ruff 与 git diff --check 通过。未新增 provider 调用。

同一 V3 estimate_text 口径，SYSTEM 从 2833 降至 2240 tokens，减少 593；按需 Skill 正文从 971 到 1014，原展示参考从 1667 到 831，新宿主参考为 833。详细测量及修改前文本保存在 output/visualization-guidance-20261027/，按需文档不计入常驻 SYSTEM 节省。
