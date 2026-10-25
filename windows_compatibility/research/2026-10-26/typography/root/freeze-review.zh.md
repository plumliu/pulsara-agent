# 全平台统一字体最终四方审阅

2026-10-26。拉取基线68afb244，受测内容为本提交中的alpha.11统一字体源码、九份开源WOFF2、完整许可和构建资源；Git提交拥有源码身份。

| 审阅方 | 结论 | 记录 |
| --- | --- | --- |
| root | APPROVE | [实施与验证](report.zh.md)：已核生产差分、完整回归、实际glyph、字节一致、许可和清理 |
| backend，gpt-6.1-sol/high | APPROVE | [全文审阅](../backend/final-root-review.md) |
| terminal，gpt-6.1-sol/high | APPROVE | [全文审阅](../terminal/final-root-review.md) |
| desktop，gpt-6.1-sol/high | APPROVE | [全文审阅](../desktop/final-root-review.md) |

冻结范围为三端共用字体与既有加载/构建链，按用户授权提交并推送codex/windows-g0，不合并main、不打tag或公开发布。没有旧Mac字体基准或系统字体文件随本提交交付。

完整frontend1385项、定向组件37项、HTTP/WSL profile31项分别通过；lint、类型、版本、startup原smoke和真实file/ASAR字体加载通过。九字体和七notice已核static、实际wheel、ASAR fixture完整bytes一致。ASAR是字体/启动页fixture，不是完整新安装器。

三种DOM平台标记均在Mac测量，Windows/Linux真实OS字体渲染仍须拉取后重建alpha.11并在当地验收；不沿用alpha.10旧包资格。未执行平台不记为通过。原生菜单和独立内容文档保持自身排版owner，跨OS抗锯齿和未覆盖Unicode不承诺逐像素一致。
