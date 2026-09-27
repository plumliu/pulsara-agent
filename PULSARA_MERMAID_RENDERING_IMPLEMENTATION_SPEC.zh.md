# 对话 Markdown 中的 Mermaid

日期：2026-09-27。状态：已实施并完成下述验证。

## 产品与边界

- `MarkdownBody` 中语言为 `mermaid` 的 fenced code block 显示图表；普通代码、行内代码、数学公式和 MarkdownInline 保持原语义。
- 模型流式输出尚未结束时显示源码及轻量生成状态，结束后渲染。不对每个 token 尝试解析，不弹语法错误 toast。
- 图表使用当前纸色/深色主题，位于版心内；提供图表/源码切换、复制源码及放大查看。多个图表随正文顺序纵向展示，窄屏不撑开页面。
- 采用内置 Dagre 默认布局，流程图设置 `minNodeWidth: 0`、`padding: 8`、`nodeSpacing: 28`、`rankSpacing: 28`。Mermaid 12 默认最小标签宽度为 120px，菱形边长又由标签宽高与内边距相加确定，容易放大短条件。取消此最小宽度，让节点按文本测量；保持字号、形状与依赖的连线计算，不在 SVG 生成后单独缩放菱形。选用 Dagre 是因为已检查安装包 ELK adapter，其没有消费这组 flowchart 间距配置；不 fork 布局引擎。图表仍可以显式指定其他受支持布局。这些是单图布局尺寸，不是内容或总量限制。
- 无法解析或加载时显示简短原因和原始源码，可以重试。失败保留内容属于该格式的显示合同，不是旧实现兼容分支。
- 仅改变显示；消息存储、复制完整回复、provider 输入、render 工具、会话创建均不改变。

## 依赖所有权

使用精确版本 `mermaid@12.0.0`，由 Vite 本地构建，按需动态加载，不使用外部渲染服务或 CDN。Mermaid 拥有语法解析、图布局、SVG 生成与 strict 清洗；Pulsara 只接 Markdown 节点、当前主题、加载/错误状态、对象 URL 生命周期和已有图片查看器。

已核对官方 [使用文档](https://mermaid.js.org/config/usage.html) 及安装包的 `mermaid.core.mjs`、`config.type.d.ts`：`render` 有内部串行队列，但 `initialize` 作用于全局配置、不会加入该队列。为避免多个图表/主题切换互相污染，用一个最小 Promise 链串行包住 initialize + render；不再实现解析器、布局器或完整任务调度器。

- `startOnLoad: false`、`securityLevel: strict`、`htmlLabels: false`、`suppressErrorRendering: true`；保留依赖原有单图资源边界，禁止图内指令更改安全设置。不新增会话/历史/图表总数上限。
- 依赖需要 DOM 测量；使用独立、不可见但可测量的临时容器，结束及失败都清除。最终 SVG 通过 Blob URL 以图片显示，不将模型生成的 SVG/CSS 插入产品 DOM，不执行 `bindFunctions`。
- 不写新清洗器；SVG 图片的浏览器隔离与依赖 strict 模式共同构成显示边界。查看和放大复用现有 Lightbox，原源码始终可查看/复制。
- 每次渲染只接受仍属于当前 source/theme 的结果，卸载与替换时释放 URL；主题变化重新渲染，错误不能阻断其他图表。
- 无新持久表、事件、subject、guard、relation、job、fingerprint 或 provider rebase。懒创建会话规格保持待实施。

## 验证

- 针对性测试：识别范围、普通代码/公式不变、流式完成后渲染、多个图表、源码与复制、错误/重试、主题和异步迟到结果、资源释放。
- 实际浏览器使用真实 Mermaid 验证中文流程图、时序图、无效语法、多图、浅/深主题和窄屏；检查代码/HTML 不作为产品页面执行，并可放大。
- TypeScript、相关 lint、Markdown/Workbench 回归、本地生产构建与差异检查。只改变前端，无需 real-provider 调用证明图布局。

## 验收记录

- Markdown、Mermaid 与 Workbench 三组回归共 **69 项通过**；TypeScript、涉及生产文件及新增测试的 ESLint 通过。
- 本地生产构建通过，Mermaid 与各图类型按需拆包；保留 Vite 对大于 500 kB chunk 的构建提示，没有为隐藏提示修改阈值。
- Playwright 的真实 Chromium 使用生产 `MarkdownBody` 和安装的 Mermaid 核对中文流程图、中文时序图、多图、浅/深主题、390 px 窄屏、错误源码、放大查看。窄屏页面宽度未超过 viewport。
- 图内尝试把 securityLevel 改为 loose、开启 HTML、放入事件属性和 JavaScript 链接的样例，没有生成脚本或事件属性；最终预览只有 Blob 图片，测量容器在成功/失败后清除。此处是针对具体输入的集成验证，不声明穷尽所有 Mermaid 安全情况。
- 验证页、截图及构建记录保存在本地 `output/playwright/mermaid/`；不把合成对话样例描述为 real-provider dogfood。没有调用模型或修改用户会话记录。
- 懒创建会话文档继续保持待实施，本次没有改动它的实现。
- 紧凑布局复核：同一“天气怎么样？”中文样例的图像高度由 568px 降到 430px，条件菱形为 117 × 117px，字号保持 14px。浏览器核对桌面及 390px 窄屏文字完整、箭头连接正确；TypeScript、相关 lint 和生产构建通过。截图为 `compact-light.png` / `compact-mobile.png`。
