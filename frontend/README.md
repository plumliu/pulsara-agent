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

## 会话内阻塞交互

能力配置、操作权限确认和规划交互统一使用会话内嵌卡片。会话内的添加 MCP、编辑 MCP 与插件连接配置复用共享 MCP 编辑器及原有凭据、默认值和提交逻辑；能力页继续使用其用户级编辑入口。

规划卡片不显示额外的 Plan Mode 标签。自然语言回答与修改意见使用通用 placeholder，沿用现有接口；授权使用通用服务图标，不依赖品牌信息或服务商专属说明。卡片小字限于作用范围、截止时间、凭据状态等决策所需的信息。

回归通过 PulsaraApp 的真实会话组件链路覆盖内嵌表单、秘密值分离、目的地确认、插件连接、权限结算与规划选择，不以预览页替代生产行为验证。
