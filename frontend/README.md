# Pulsara 桌面工作台

这是 Pulsara Electron 桌面应用的 React 界面。正式工作台需要 Electron preload，浏览器地址不提供独立产品入口。本机文件、目录选择和系统打开操作通过窄原生桥接完成；内置 loopback 服务只负责既有 runtime 协议。

桌面安装、启动和打包请参阅仓库根目录 README。模型与记忆配置仍由应用设置管理；PostgreSQL 由桌面应用安装包提供并管理，界面不接受用户填写数据库 DSN。

目录选择器默认从系统桌面目录开始浏览；桌面目录无法获取或不存在时使用用户主目录。起始位置由桌面宿主决定，不跟随当前会话；最终工作目录仍由用户选择。

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

排队输入随内容向上展开，不使用固定像素或视口比例的常规高度上限。仅当队列即将占满工作台可用空间时内部滚动：复用现有布局观察器，扣除输入框、TODO dock 等实际高度，并沿用顶部栏与“回到最新”的 105px 预留。窗口或输入框高度变化时重新计算，不限制排队条数。每条消息显示为独立实底气泡，通过间距、阴影和操作分区建立层次；滚动条空间左右对称预留，气泡相对输入框居中，气泡与输入框共用常态阴影，气泡悬停不改变阴影或位置；仅正文实际溢出两行时显示“展开”，展开与收起使用可中途反向的高度动画。TODO 展开时先为其浮层的实际高度预留空间，队列高度动画通过现有布局观察器同步更新“回到最新”，不会将 TODO 清单挤成零高度。气泡沿用 toast 的原生 Web Animations 方式入场与退场，移除时平滑收拢占位；仅对既有队列状态变化做短暂视觉过渡，不增加自动过期、拖动删除或新的删除逻辑，退场项立即退出焦点和操作，减少动态效果偏好下直接完成。此规则替代历史 PR04 文档中的 `min(190px, 28vh)` 建议。

能力配置、操作权限确认和规划交互统一使用会话内嵌卡片。会话内的添加 MCP、编辑 MCP 与插件连接配置复用共享 MCP 编辑器及原有凭据、默认值和提交逻辑；能力页继续使用其用户级编辑入口。

规划卡片不显示额外的 Plan Mode 标签。自然语言回答与修改意见使用通用 placeholder，沿用现有接口；授权使用通用服务图标，不依赖品牌信息或服务商专属说明。卡片小字限于作用范围、截止时间、凭据状态等决策所需的信息。

回归通过 PulsaraApp 的真实会话组件链路覆盖内嵌表单、秘密值分离、目的地确认、插件连接、权限结算与规划选择，不以预览页替代生产行为验证。


### 固定跨平台字体与阅读参数

仅主会话、任务会话及继承会话的模型回复正文使用 Inter（英文）与 MiSans（中文），
固定 16px / 400 / 0 / 1.70。用户输入、用户消息和 steer 使用原字体，额外字间距归零。其他页面、界面控件、品牌字样、发言人名称和标题
恢复原字体：Inter + Noto Sans SC 或 Source Serif 4 + Noto Serif SC。
所有字体继续随包跨平台提供。代码和数学公式保持原专用字体。
完整合同见 `windows_compatibility/PULSARA_CROSS_PLATFORM_TYPOGRAPHY_IMPLEMENTATION_SPEC.zh.md`。


## 跟随最新的隔离体验

生产滚动行为由 `lib/thread-follow-controller.ts` 统一协调，契约见仓库根目录的 `PULSARA_LATEST_FOLLOW_REDESIGN_PLAN.zh.md`。跟随意图、真实尾部可见性与临时查看/选区保护分别表示；向下返回吸附为 48 CSS 像素，按钮可见性的独立测量容差为 1.5 CSS 像素。

运行 `npm --prefix frontend run demo:follow -- --port 5175 --strictPort`，打开 `http://127.0.0.1:5175/follow-demo.html`。体验页引用生产 Workbench，提供长思考、流式输出、工具结果、历史区间、长队列和失败读取，不连接生产运行时；它只用于人工体验，不进入桌面默认构建入口。
