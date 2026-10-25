# Windows Electron + WSL2

本目录维护当前实现合同及开发入口。生产代码位于 `desktop/`、`frontend/` 和 `src/pulsara_agent/`。

## 当前状态

Windows Electron + WSL2 的 B1–B4 核心接入已实现，本机所选矩阵已完成机制验证和安装版使用验证。**B5／Windows 发布资格仍未激活**。原生 Ubuntu x64/arm64 与其 GUI 验收继续延期，不由 WSL 结果替代。

当前代码版本为 `0.1.1-alpha.1`（Python `0.1.1a1`），设置页显示 `0.1.1`。版本号更新及合入 main 不代表已构建、发布或取得新增平台资格。

## 有效文档

- [Windows 实现合同](PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md)
- [开发与构建准备](PULSARA_WINDOWS_DEVELOPMENT_G0_CHECKLIST.zh.md)
- [共享代码修改后的定向重验范围](PULSARA_WINDOWS_SHARED_FIX_MINIMAL_REVALIDATION.zh.md)
- [随包字体合同](PULSARA_CROSS_PLATFORM_TYPOGRAPHY_IMPLEMENTATION_SPEC.zh.md)

## 历史材料

本轮新增的实验脚本、失败记录、截图、验收过程和演示页面保留在 [`codex/windows-g0`](https://github.com/plumliu/pulsara-agent/tree/codex/windows-g0/windows_compatibility/research) 分支，不作为生产依赖引入 main。main 原已存在的历史归档保持原样；历史文档不覆盖上述有效合同。
