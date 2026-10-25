# 全平台统一字体

本轮交付：**Mac、Windows + WSL2、Linux统一使用随包开源字体，字体文件与代码一起提交。**新本地候选版本为alpha.11，避免已安装alpha.10的同版本payload冲突。

| 用途 | 新的同一套字体 |
| --- | --- |
| 界面英文 / 中文 | Inter / Noto Sans SC |
| 正文、标题英文 / 中文 | Source Serif 4 / Noto Serif SC |
| 代码、终端 | JetBrains Mono，中文回退Noto Sans SC |
| 启动页品牌 | Gelasio |
| 公式 | 原KaTeX数学字体；中文公式接Noto Serif SC |

- [实施说明与边界](../PULSARA_CROSS_PLATFORM_TYPOGRAPHY_IMPLEMENTATION_SPEC.zh.md)。
- [统一字体样张](open-fonts/reading.png)，包含中英文、列表、粗斜体、代码和公式。
- [实际结果与四方审阅](../research/2026-10-26/typography/root/report.zh.md)。
- [完整字体版权、OFL与固定上游来源](../../desktop/assets/fonts/licenses/SOURCES.md)。

## Windows / Linux怎么使用

1. 正常同步`codex/windows-g0`，Windows与WSL clone保持同一提交；字体WOFF2已经在Git内，不需要另传私有字体包、安装系统字体或访问字体CDN。
2. 沿现有构建链重建。WSL ext4 clone执行`mise run --jobs 1 desktop:verify`、`mise run --jobs 1 desktop:wsl-payload`，将新payload交给Windows clone，再执行`mise run --jobs 1 desktop:windows-package`。Linux和Mac沿原native构建链。
3. 确认新的alpha.11资源和exe版本一致，正常更新、保留用户数据。不能将新字体payload直接覆盖已部署的alpha.10版本目录；不需要reset数据库或重新配置模型。
4. 新包中检查中文/英文、标题、粗斜体、代码/terminal、品牌和含中文公式，确认实际renderer使用随包字体。Windows/Linux实际OS渲染须在当地新包验收，本轮Mac样张不替代它。

字体只用于Pulsara自有workbench和startup文字；原生菜单/文件面板与网页、PDF、独立SVG等文档保持各自owner。跨OS字体文件、CSS字号/行高相同，仍可能有少量抗锯齿差异。各平台需重建或更新新版本才显示这套字体。

## 字体维护

唯一源资产为`desktop/assets/fonts/`与`desktop/assets/fonts.css`。常规构建直接使用已提交文件，不下载字体。需要更新上游字体时，在仓库根运行：

```bash
uv run --no-project desktop/scripts/prepare_fonts.py
```

脚本用固定Google Fonts提交和FontTools完整压缩，保完整Unicode cmap、glyph order和name metadata。更新字体须同步完整OFL/来源材料，再做样张/加载/分发验证。字体仍按各自OFL授权，不随应用代码的其他许可重新授权。
