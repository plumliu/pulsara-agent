# 全平台随包字体合同

macOS、Windows Electron + WSL2 与 Linux 使用同一组可再分发开源字体。本文只描述资源与排版边界；字体重建来源及完整许可证位于 `desktop/assets/fonts/licenses/`。

## 2026-10-26 用户确认的范围

仅会话页的模型回复正文（主会话、任务会话、继承会话）改用英文 Inter + 中文 MiSans，
固定 16px、400、0 字间距、1.70 行高；`base.css` 定义共用正文参数，
`workbench.css` 通过 `.conversation-transcript` 内消息正文选择器使用。
用户输入框、用户消息与 steer 恢复原字体和各自字号，额外字间距为 0。已发送用户消息及 steer 的正文使用 500 字重；输入框维持原字重。其他所有页面、品牌、发言人名称、Markdown 标题、过程信息保持原字体。共用 `.assistant-markdown` 不设置消息专用字体、字重或字距；思考气泡沿原宋体，工具名称和终端输出沿原等宽字体，新阅读参数仅由模型回复正文作用域施加。
Noto Sans SC、Noto Serif SC、Source Serif 4 因仍被这些区域使用，恢复随包保留。
该范围取代之前全局替换和删除旧字体的决定。

MiSans 以独立 `Pulsara Conversation CJK` family 注册，不能替换 `Pulsara UI CJK`。
官方原始 WOFF2 由 `desktop/scripts/prepare_fonts.py` 复制，不转换、不删减或修改字形；
CSS 字重映射沿用画廊（Regular=400）。完整 MiSans 许可随包，设置页注明使用并提供许可链接。
MiSans 自身许可不属于 OFL；其余原字体许可与资源边界仍遵循下文。
正文中的加粗保留语义，字重固定为 600（半粗），避免默认 700 过重；不影响标题及嵌入控件。代码/公式不继承新增字间距；标题显式使用原 `--serif`。
会话消息内 Markdown 标题保留原宋体，使用 600 半粗、1.45 行高，H1–H6 为 24/21/19/18/17/16px；上方留白 26px，下方 10px，首个标题不加顶部留白。此调整仅用于消息正文中的标题，不影响其他页面或发言人名称。
思考气泡保留原 Source Serif 4 + Noto Serif SC，并采用独立 Markdown 阅读样式：正文 14px / 400 / 0 字距 / 1.75 行高，文字使用 `--ink-soft`；标题 H1–H3 为 18/16/15px、600 字重，段落间距 10px，标题上下留白 18/7px。列表、首尾段落与公式块单独整理间距，避免外层会话紧凑样式覆盖。消息正文、工具气泡与其他页面不受影响。
本次不运行额外浏览器视觉自动化；完成必要构建与安装后由用户实测视觉效果。

## 字体与资源所有权

| 用途 | 统一字体 / CSS family |
| --- | --- |
| 英文界面 | Inter / Pulsara UI |
| 中文界面 | Noto Sans SC / Pulsara UI CJK |
| 英文会话正文、标题 | Source Serif 4 / Pulsara Body |
| 中文正文、标题 | Noto Serif SC / Pulsara Body CJK |
| 代码、终端 | JetBrains Mono / Pulsara Mono；中文用Pulsara UI CJK |
| 启动页品牌字样 | Gelasio / Pulsara Brand |
| 数学排版 | 保留KaTeX专用数学face；其CJK fallback使用Pulsara Body CJK |

六家字体来自Google Fonts固定Git revision `e7db6ca603fd4862010383ff17ffd6cc4ffac1c0`，完整OFL版权/许可与来源说明随包提供。FontTools负责完整TTF到WOFF2转换和格式读回，不按样张删减Unicode或字形。依赖负责字体解析、压缩、浏览器解码与塑形；Pulsara只选择家族、字重/斜体、资源地址和既有UI字号/行高。

FontTools 4.60.1仅做WOFF2压缩，原完整name table（含版权、许可和RFN）逐bytes读回一致，woff-specific metadata省略。Source Serif 4二进制仍含Source RFN，不能仅凭其独立OFL文件未列RFN推定整个字体没有限制；本轮依据[OFL FAQ 2.2/2.2.1](https://openfontlicense.org/ofl-faq/#22-can-i-make-and-use-woff-web-open-font-format-versions-of-ofl-fonts)所述完整数据/元数据保留的WOFF转换条件处理，不删除或重写字体作者、许可和名称。生成器保原字体版权和字形覆盖，不作设计变换。

唯一资源源头是`desktop/assets/fonts/`；唯一`@font-face`定义在`desktop/assets/fonts.css`。frontend的`base.css`导入该CSS，Vite将所引用WOFF2编译到原`static/assets/`；小型原Vite build plugin将完整OFL和来源说明也发到`assets/font-licenses/`。原Python wheel/static owner及WSL payload继续携带静态目录，不增加font HTTP服务或运行时manifest。启动页通过同一个`fonts.css`文件及相对路径加载，原CSP仅增加`font-src 'self'`。原HTTP FileResponse对woff2提供正确MIME，保原路径验证、Range与传输owner。

运行时加载随本机应用的字体资源，不从外网下载、不使用`local()`或CDN，不要求Windows、WSL或Linux安装系统字体。Windows+WSL2由**Windows Electron**塑形，字体随原WSL HTTPS静态响应交付；在WSL里安装字体不能替代Windows renderer加载字体文件。原pin TLS/BackendSession不变。

## 具体改动与范围

- base字体变量全平台切到自带families，删除原系统字体优先链。正常、粗体、Latin italic及可变字重均由所带实际face覆盖；Noto CJK没有真实italic，不伪造同名斜体文件。正文保原允许合成策略，启动页保原`font-synthesis:none`，不全局改变KaTeX合成。
- 启动页、目录选择器、tooltip、输入提示等Pulsara自有UI的显式系统字体改同一变量/品牌family。原字号、行高、布局、业务行为与交互保持；代码、pre、terminal及明确代码/path文字禁JetBrains operator连字，不修改字符串、复制或terminal cell语义。
- 系统原生菜单/文件对话框使用原生平台字体；用户网页、HTML/PDF/图像/SVG等独立文档保持其内容owner。Mermaid的Blob SVG图片不继承app字体，沿原独立SVG排版；外部浏览器的SDK OAuth完成页保其独立页面边界。本轮不借字体任务增加SVG字体嵌入、OAuth字体服务、全局important或预览CSP放宽，也不声称这些独立文档已被统一。
- KaTeX数学字体既有样式与资源保持；独立中文fallback接同一Noto Serif SC。自带UI字体不覆盖数学符号face。


## 维护与验证

字体由 `desktop/scripts/prepare_fonts.py` 从固定上游提交重建，保留完整 Unicode cmap、glyph order 及全部上游许可。字体调整后核对实际加载字形、混排、粗斜体、代码及数学排版；在一个系统切换平台 marker 不能替代其他原生平台验证。不同系统的抗锯齿不保证逐像素相同。
