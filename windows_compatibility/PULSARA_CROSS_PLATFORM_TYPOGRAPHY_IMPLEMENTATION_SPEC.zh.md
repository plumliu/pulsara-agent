# 全平台统一字体实施说明

2026-10-26，`codex/windows-g0`，拉取基线`68afb244`。Mac、Windows Electron + WSL2、Linux统一使用允许再分发的开源字体，字体文件和代码一起提交推送。候选版本同步为alpha.11，避免Windows已部署alpha.10的同版本payload身份冲突；不打tag/发布。

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

## 实施与验证顺序

1. 确定统一字体角色与中英文排版样张。
2. 从固定上游提交生成九份完整WOFF2，保全部上游Unicode cmap和glyph order，带六份完整OFL/来源说明。
3. 接入唯一font-face、全平台变量与startup；修正自有UI显式字体，补原HTTP MIME与许可证静态输出。
4. 原生Electron测中英文、数字、混排、normal/bold/italic/bold-italic、代码及KaTeX；必须实际报告自带font glyphs和加载状态，不能只看CSS family。三种platform marker在Mac的相同测量仅证明CSS选择无平台分支，不代替真Windows/Linux渲染。
5. 验证全新frontend构建、原startup cover/CSP、相关frontend和HTTP回归、字体/许可证进入构建资源；原业务数据和日用安装版不改写。
6. 文档与实际截图记录统一字体结果。完成交叉审阅后按用户授权提交并推送当前分支，不合并main，不打tag/发布。Windows/Linux收到代码后重建安装包并用同一份样张核验；同文件/字号/行高不保证不同OS逐像素抗锯齿一致。

最终测量、资产尺寸、测试结果与审阅见本目录的typography材料，未执行的平台不得记为通过。
