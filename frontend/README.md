# Pulsara 桌面工作台

这是 Pulsara Electron 桌面应用的 React 界面。正式工作台需要 Electron preload，浏览器地址不提供独立产品入口。本机文件、目录选择和系统打开操作通过窄原生桥接完成；内置 loopback 服务只负责既有 runtime 协议。

桌面安装、启动和打包请参阅仓库根目录 README。模型与记忆配置仍由应用设置管理；PostgreSQL 由桌面应用安装包提供并管理，界面不接受用户填写数据库 DSN。

## 前端验证

```sh
npm install
npm run lint
npm test
npm run build:desktop
```

`build:desktop` 将静态资源写入内部应用服务资源目录。`dev:desktop` 用于桌面开发的 Vite 资源服务；组件测试可注入原生桥接适配器。真实验收通过 Electron 应用完成。

三栏工作台、实例标签、文件浏览历史与冻结可视化的现行契约见
[`PULSARA_DESKTOP_MIGRATION_AND_BUNDLED_POSTGRESQL_PLAN.zh.md`](../PULSARA_DESKTOP_MIGRATION_AND_BUNDLED_POSTGRESQL_PLAN.zh.md)。
