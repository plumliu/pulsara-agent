# Pulsara LaTeX 公式点击复制实施计划

## 1. 目标

让 Pulsara 中由 Markdown 渲染出的每一个数学公式都可以直接复制其 LaTeX 源码。

交互对象是**被渲染出来的公式整体**，不是额外出现的复制按钮：

1. 默认状态保持现有视觉，不增加常驻控件。
2. 鼠标移入公式时，公式整体出现中性灰、半透明的圆角包裹框。
3. 点击框内公式的任意位置，复制该公式对应的原始 LaTeX 源码。
4. 复制成功后，复用 Pulsara 右下角统一通知，显示“公式已复制”。
5. 键盘聚焦后可使用 `Enter` 或 `Space` 完成相同操作。

参考图只用于确定包裹范围和交互形态。Pulsara 不照搬其中的蓝色，而是继续使用当前前端的中性灰色设计语言。

## 2. 复制内容契约

复制的是交给 KaTeX 渲染的 LaTeX 源码，不是：

- 浏览器中可见的纯文本；
- MathML 的文本投影；
- KaTeX 生成的 HTML；
- 从最终视觉结果反向拼接出的近似公式。

例如下面的 Markdown：

```markdown
\[
\frac{d}{dt}\left(\frac{\partial L}{\partial \dot q}\right)
-
\frac{\partial L}{\partial q}
=0
\]
```

点击后复制：

```latex
\frac{d}{dt}\left(\frac{\partial L}{\partial \dot q}\right)
-
\frac{\partial L}{\partial q}
=0
```

外层 `$...$`、`$$...$$`、`\(...\)` 或 `\[...\]` 属于 Markdown 数学分隔符，不进入剪贴板。仅去掉解析器已经识别出的分隔符和结构性首尾空白；公式内部的反斜线、空格和换行保持不变。

## 3. 视觉与交互

### 3.1 行内公式

- 包裹范围只覆盖当前公式，不覆盖整行或相邻正文。
- hover/focus 时使用很小的内边距和约 `5px` 圆角。
- 背景使用 `color-mix` 从 `var(--paper-deep)` 与透明色生成，边框使用 `var(--line)` / `var(--line-strong)`，不引入新的品牌色。
- 包裹框不得改变静止状态下的行高、基线或文字间距。
- 光标使用符合普通可点击内容习惯的 `cursor: pointer`。

### 3.2 块级公式

- 包裹范围覆盖整个公式展示区域，形态与参考图中的整块公式框一致。
- hover/focus 时显示浅灰半透明背景和细边框。
- 保留现有居中、上下间距与横向滚动行为。
- 很长的公式允许继续横向拖动；拖动和文字选择不得误触发复制。

### 3.3 状态反馈

- 成功：复用应用现有的右下角 Toast，标题为“公式已复制”，使用 success tone。
- 失败：复用同一 Toast，标题为“无法复制公式”，正文说明浏览器没有授予剪贴板权限，使用 warning tone。
- 公式旁不再生成局部“已复制”标签、图标或第二套提示组件。
- Toast 的展示时长、动效、堆叠与关闭行为完全由现有统一通知 owner 管理。
- 公式自身只保留浏览器原生的 hover、focus-visible 与 active 视觉，不持有通知 timer。

### 3.4 触摸与键盘

- 触摸设备没有 hover 时，轻点公式即可复制。
- 包裹节点具有可理解的 `aria-label="复制 LaTeX 公式"`、焦点样式和键盘操作。
- 对滚动手势记录指针位移；超过小幅移动阈值时不执行复制。

## 4. 依赖与所有权边界

继续复用现有统一 Markdown 数学链路：

- `remark-math-extended` 负责识别数学语法与公式边界；
- 一个很小的本地 rehype 插件负责在 KaTeX 运行前保存“实际送入 KaTeX 的源码”并包裹交互节点；
- `rehype-katex` / KaTeX 只负责视觉渲染；
- 浏览器标准 `navigator.clipboard.writeText()` 负责写入剪贴板；
- `MarkdownBody` / `MarkdownInline` 负责点击与键盘交互，并把复制结果交给现有 `onNotify` 通知 owner。

不新增另一套 Markdown/LaTeX parser，不从 KaTeX DOM 反向恢复源码，也不引入只包装一次 `navigator.clipboard` 的复制依赖。若实现直接导入 unified 生态的树遍历工具，应将该工具声明为前端直接依赖，而不是依赖某个包的传递依赖。

## 5. 实现结构

### 5.1 源码捕获插件

在 `frontend/components/markdown-body.tsx` 附近增加一个局部 rehype 插件，并把插件顺序固定为：

```text
remark-math-extended
    -> Markdown math node
    -> copyable-math wrapper（保存 LaTeX source）
    -> rehype-katex（只替换 wrapper 内部的 math placeholder）
    -> React DOM
```

插件识别现有 `math-inline` 与 `math-display` 节点，在其外层分别生成行内或块级 wrapper。源码来自 KaTeX 渲染前的节点文本，存入 wrapper 的安全 data property；KaTeX 替换内部节点后，wrapper 与源码仍然存在。

插件不得：

- 重新解析 `$` 或反斜线分隔符；
- 修改公式内容；
- 包裹 fenced/inline code 中看起来像公式的文本；
- 读取 provider、模型或消息类型；
- 改变 KaTeX strict/error 契约。

### 5.2 React 交互

`MarkdownBody` 与 `MarkdownInline` 共用一个轻量的公式复制 owner：

- 点击事件只接受最近的公式 wrapper；
- 使用该 wrapper 保存的 LaTeX source 调用 `navigator.clipboard.writeText()`；
- 成功时调用现有统一通知：`onNotify('公式已复制', undefined, 'success')`；
- 失败时调用现有统一通知：`onNotify('无法复制公式', '浏览器没有授予剪贴板权限。', 'warning')`；
- 鼠标拖动、横向滚动或已有文本选择时不复制。

事件代理放在 Markdown 容器层，避免为长回复里的每个公式各自建立一套状态机或监听器。`PulsaraApp` 继续作为 Toast 的唯一状态 owner；`WorkbenchView`、`InspectorPanel` 及其 Markdown 子组件只透传现有 notifier，不创建通知 context、第二套 Toast stack 或公式专属通知状态。

### 5.3 CSS

在现有 Markdown 样式旁增加：

```text
.math-copy-target
.math-copy-target--inline
.math-copy-target--display
.math-copy-target:focus-visible
.math-copy-target:active
```

行内 wrapper 使用 `inline-block` 并维持当前 baseline；块级 wrapper 使用 `position: relative` 和 `max-width: 100%`。hover 视觉只改变背景、边框和 box-shadow，不改变公式尺寸。

## 6. 生效范围

能力属于统一 `MarkdownBody` / `MarkdownInline`，因此自然覆盖：

- assistant 正文；
- 思考摘要；
- subagent activity 与结果摘要；
- plan/interaction Markdown；
- inspector 中复用统一 Markdown 组件的内容。

不为不同页面复制实现，也不按消息来源增加特例。普通正文、代码块和工具输出保持原样。

所有生产调用点必须把 `PulsaraApp` 的现有 notifier 透传给统一 Markdown 组件，确保无论公式来自正文、思考摘要还是 inspector，反馈都进入同一个右下角 Toast stack。

## 7. 测试计划

### 7.1 组件测试

在 `frontend/components/markdown-body.test.tsx` 覆盖：

1. 行内 `$...$` 与 `\(...\)` 分别生成独立 wrapper。
2. 块级 `$$...$$` 与 `\[...\]` 分别生成独立 wrapper。
3. 同一消息中的多个公式各自保存并复制自己的源码。
4. 复制值不包含 Markdown delimiter，且保留内部 LaTeX 和换行。
5. 点击渲染公式的任意子节点仍定位到正确 wrapper。
6. `Enter` / `Space` 与鼠标点击等价。
7. 成功和失败反馈真实反映 Clipboard Promise 结果，并分别调用统一 notifier 的 success/warning tone。
8. inline code、fenced code 和普通美元文本不会变成复制目标。
9. 多行列表中的 display math 仍正常渲染，后续 Markdown 不受影响。
10. Markdown 组件不创建公式专属反馈 timer 或 Toast 状态。

### 7.2 浏览器视觉验证

使用真实会话页面检查：

- 行内公式包裹范围与参考图一致，不改变正文基线；
- 块级公式显示完整灰色半透明区域；
- `\boxed{}`、矩阵、分段函数、多行对齐公式不受包裹层影响；
- 超宽公式仍可横向滚动，拖动不复制；
- hover、focus、active 三种公式状态清晰但不抢眼；
- 成功后右下角显示统一的“公式已复制”Toast，失败时显示“无法复制公式”；
- 剪贴板中的内容是 LaTeX source，而非可见文字或 MathML。

### 7.3 回归验证

至少运行：

```bash
cd frontend
npm test -- components/markdown-body.test.tsx app/pulsara-app.test.tsx
npm exec eslint -- components/markdown-body.tsx components/markdown-body.test.tsx
npm run build:local
```

最后运行仓库根目录的 `git diff --check`。

## 8. 非目标

本轮不实现：

- 复制渲染图片、MathML、HTML 或纯文本的多格式菜单；
- 为公式增加常驻复制按钮；
- 公式编辑器、源码预览弹窗或右键菜单；
- 自动补充 Markdown/LaTeX delimiter；
- provider/model 特例；
- 后端事件、数据库字段、配置项或持久化复制状态；
- 公式旁的局部“已复制”标签或独立提示组件。

## 9. 完成标准

只有同时满足以下条件才算完成：

1. 所有统一 Markdown 组件中的有效公式均可通过点击整个渲染区域复制。
2. 剪贴板内容与该公式的 LaTeX source 一致，且不来自视觉反推。
3. 行内与块级公式都符合参考图的包裹范围，同时使用 Pulsara 中性灰视觉。
4. 长公式拖动、代码块、普通正文和既有 KaTeX 渲染没有回归。
5. 键盘与触摸路径可用，成功/失败均通过现有右下角统一 Toast 准确反馈。
6. 不引入新的 parser、后端状态或重复页面实现。
