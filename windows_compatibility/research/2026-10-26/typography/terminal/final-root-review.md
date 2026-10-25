# 统一开源字体最终全文交叉审阅

2026-10-26，terminal / gpt-6.1-sol high。

**APPROVE：同意本候选统一开源字体、代码、许可与所列实证交付，无剩余源码或已执行证据阻断。** 可由root按用户授权统一commit/push `codex/windows-g0`。本批准不代替Windows/Linux当地新包验收，不授权tag、公开发布或合并main。本人本轮仅只读终审，未改生产、依赖或提交，未新增测试/build。

通读：`PULSARA_CROSS_PLATFORM_TYPOGRAPHY_IMPLEMENTATION_SPEC.zh.md`、`typography/README.zh.md`、`research/2026-10-26/typography/root/report.zh.md`，以及最终font-face/base/startup/mono scope、Vite notice输出、HTTP leaf、字体来源与四方真实材料。

## 实现与范围一致

- 六个开源家族及9个完整variable WOFF2由唯一canonical `desktop/assets/fonts/`持有，单一fonts.css同时供workbench和startup使用；Inter/NotoSansSC UI、SourceSerif4/NotoSerifSC正文、JetBrainsMono代码、Gelasio品牌一致。全平台无local()/CDN、系统安装要求或平台选择分支。Windows Electron经原WSL HTTPS静态owner取得资源，未把WSL安装系统字体当renderer加载方案。
- 字重范围与真实Latin italic对应实际face；Noto normal不伪造italic，正文保浏览器原合成策略，startup原synthesis none保持。KaTeX仍使用Math/Main/AMS/Size等自有face，仅cjk_fallback接BodyCJK。
- 分工内四文件只改family/shorthand与一条定向code/pre/terminal/path禁liga/calt规则，不改字号/行高/layout/原字符串、copy或terminal cell机制。保:not(.katex *)，无全局font-family important/shaping覆盖。
- 系统原生菜单/文件面板、HTML/PDF/外部网页、独立SVG/Mermaid Blob、外部OAuth完成页均明确保内容owner；没有为了字体增服务、SVG注入或放宽预览CSP。上述例外在三份最终文档中一致，未泛称所有独立文档已统一。

## 实证闭合且不扩大范围

- 先前已完整核CDP：80×3字形矩阵、43×3阅读样张的所有实际glyph记录均custom；Latin italic/可变bold、普通中文、mono中文fallback、Gelasio与中文公式实际face正确。已看reading.png，中英文、粗斜体、代码和公式无缺字/遮挡。
- 本次补核desktop `startup-fonts.json`、`startup-asar-fonts.json`：真实file URL、原startup/cover/preload及原CSP，UI/UI CJK400/500和Brand700加载成功，status/brand实际platform fonts均custom；16/32/15px及startup synthesis none准确。原smoke生命周期正/负例保留，独立ASAR字体fixture已如实标明，不冒称完整installer。
- 最终wheel alpha11九字体、七notice与canonical完整bytes一致；ASAR fixture完整bytes、原HTTP 200/font/woff2与完整内容、Range206/escape404回归、Vite构建notice输出均有实际材料。HTTP变更保原resolve/parents/isfile和FileResponse传输owner，没有新font服务或registry。
- 完整frontend83文件1385项、lint/type，以及本人3文件37项和backend31项分别记录，不叠加为不重叠总数。没有新增跳过/削弱断言。源码与locks同步alpha11避免既有alpha10资源身份冲突，不把metadata推进冒称当地新包已验。
- 三个context是同一Mac Electron中的DOM platform marker，三份文档均明确其局限；本机数据不证明Windows/Linux原生渲染，也不保证逐像素抗锯齿一致、全部Unicode/emoji均覆盖。当地按同一Markdown和新包验收的交接清楚，无需传私有字体包/reset数据或重配模型。

## 来源与最终文字

已读fixed Google/fonts revision来源、完整六份OFL/name metadata独立审阅、FontTools压缩脚本及readback；又核官方[OFL FAQ §2.2/2.2.1](https://openfontlicense.org/ofl-faq/#22-can-i-make-and-use-woff-web-open-font-format-versions-of-ofl-fonts)的原数据/元数据完整保留条件。最终文档准确保留Source RFN事实与WOFF压缩适用边界，不把CSS alias当法律改名，不删除原作者/许可，不为字体新增fingerprint authority。

最终可提交材料仅保统一字体中性样张、代码/许可及真实新资源验证；root已移除过时过程稿和失效材料引用，个人output历史保留而不作为本交付依据。提交不携app/wheel/asar/profile/node_modules或用户settings/数据库内容。字体fixture、HTTP/renderer与临时profile清理有原记录；没有provider/PG行为变化。

除四方最终批准元数据收口外，无需再改实施语义或重跑测试。
