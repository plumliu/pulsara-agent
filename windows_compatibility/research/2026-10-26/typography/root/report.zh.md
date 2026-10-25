# 全平台字体统一：实施与验证

2026-10-26，基线68afb244。交付Mac、Windows + WSL2、Linux共用的开源字体、完整许可和接入代码。候选版本alpha.11，避免已部署alpha.10的同版本payload冲突；没有tag或公开发布。完整源码身份以包含本报告的Git提交为准。

## 最终实现

界面Inter/Noto Sans SC，正文Source Serif4/Noto Serif SC，代码JetBrains Mono、中文Noto Sans SC，品牌Gelasio；采用用户批准的全平台统一方案。原字号/行高/布局/行为不改。KaTeX数学face保留，仅其中文fallback使用Noto Serif SC；代码和明确literal/path文字禁operator连字，原字符与复制内容不变。

九份完整可变WOFF2共20,545,940bytes，字体和六份原OFL及来源说明都随Git提供。原Google Fonts固定提交、FontTools4.60.1完整压缩、不删glyph/Unicode、name/copyright/license/RFN元数据读回见`root/open-font-build.jsonl`和backend许可审阅。Source Serif4二进制的Source RFN仍保留；保名依据是[OFL FAQ 2.2/2.2.1](https://openfontlicense.org/ofl-faq/)的完整WOFF压缩条件，不谎称字体没有RFN。

单一`desktop/assets/fonts.css`和`fonts/`被startup直接使用、被frontend base导入；原Vite生成字体assets及完整notice，原wheel/static/WSL payload继续携带。运行时仅加载随本机应用的字体资源，无CDN、local()或系统字体安装。startup CSP只增font-src self；原HTTP FileResponse修woff2 MIME，保持原target校验/Range/传输owner。没有新font服务、runtime registry、fingerprint、数据库或provider输入变动。

自有UI额外显式字体（提示、tooltip、typewriter cursor、目录选择品牌、缺host页面）接同变量。系统原生菜单/文件面板、用户网页/HTML/PDF、独立SVG图形和外部SDK OAuth完成页仍由其原owner排版；Mermaid Blob SVG不继承app webfont，不把该独立文档误记为统一UI证据。完整范围见顶层字体实施说明。

## 实际检查

| 检查 | 结果 | 记录 |
| --- | --- | --- |
| 新字形矩阵 | 80样本×3 platform marker，全部实际glyph来自custom webfonts，无系统fallback | `typography/open-fonts/rendered-fonts.json` |
| 阅读样张 | 43样本×3 marker，中英文/标题/列表/粗斜体/代码/中文公式全部custom，实图人工检查无缺字 | `typography/open-fonts/reading-fonts.json`、`reading.png` |
| startup字体 | 真实file://原startup+preload/CSP，UI/CJK400/500与Brand700实际加载；原字号16/32/15、synthesis none保持 | desktop `startup-fonts.json`及log |
| ASAR startup字体 | 真file://font-app.asar内startup、原cover/preload/CSP，字形Inter+Noto/Gelasio均custom | desktop `startup-asar-fonts.json`及log |
| 原startup smoke | 全部lifecycle、visible fade、hidden直接结算、几何/交互门/trust/error controls通过 | desktop `startup-smoke.log` |
| 完整frontend | 83文件、1385项通过，无skip；lint和TypeScript检查通过 | root `frontend-final.log`、`frontend-lint.log` |
| 定向组件 | terminal分工37项通过，code/copy/terminal/picker合同不改 | terminal `focused-frontend.log` |
| HTTP/WSL profile | 两模块31项通过，包含新MIME/完整bytes/Range206/逃逸404；改前MIME失败保留；Ruff通过 | backend `mime-implementation.md`及logs |
| 真实字体HTTP | 原LocalHttpServer的9份真实WOFF2全200/font/woff2/完整bytes相等；7notice完整相等，原server自然close | root `http-assets.json` |
| frontend生产构建 | 原构建命令成功，更新已跟踪static；9字体/7notice随产物，未手改生成代码 | root `frontend-production-build.log` |
| wheel | 新alpha.11 native wheel中9字体/7notice与canonical逐bytes相同，MIME修复source在内 | root `wheel-font-assets.json` |
| ASAR资源 | 以原Electron asar SDK生成font/startup+现dist fixture，9字体/7notice及font/startup资产逐bytes相同 | root `asar-font-assets.json` |
| 版本 | 原release_artifacts.py check通过，desktop/frontend/Python/locks同步alpha.11 | 根版本文件/最终收口 |

这里只把三种**DOM platform marker**在同Mac Electron中比较；实际OS仍darwin，不能写成Windows/Linux真机字体已验收。ASAR是本次字体/启动页fixture，不冒称完整新安装器；wheel是真实构建。Windows/Linux需在新alpha.11包使用同一份Markdown再核实际glyph和GUI，不需另传字体包。不同OS抗锯齿的少量差异不等于字体文件不一致；也不声称这些字体覆盖全部Unicode/emoji。

## 探针检查

startup个人探针初次误认为file URL一定产生ResourceTiming项，字体加载断言已通过但该harness假设失败；改用真实CSSOM/FontFace/CDP，保旧log、原产品及smoke断言不变。其余原源码功能断言未削弱、未加skip/xfail。

## 清理、审阅与交接

所有字体fixture只渲染本地CSS/中性样张，不读取模型/用户settings、不调用provider或PG。日用应用与其kernel/PG保持运行，临时HTTP server/字体probe全部结算；临时Electron profiles精确删除。提交图片仅使用中性样张。字体raw TTF临时下载目录由生成器退出删除；Git仅携所需可再分发WOFF2/完整notice与代码/文字证据，不提交app、wheel、asar、profile或node_modules。

root与三位gpt-6.1-sol/high各在output个人目录工作，已相互审阅实际源码、许可证、产物和glyph。四方最后全文均APPROVE，结论见[最终四方审阅](freeze-review.zh.md)；用户已明确授权推送当前分支，不合并main。Windows/Linux原生字体与新安装器资格仍待当地重建，不沿用alpha.10旧包资格。
