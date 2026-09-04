# Pulsara 本地前端

这是 Pulsara 本地智能工作台的前端源码。正式页面由仓库根目录的本地应用服务提供，
不需要网站账号登录，也不使用演示数据。界面只保留已有后端契约支持的功能。

## 启动完整应用

在仓库根目录运行：

```sh
uv run pulsara app
```

服务只监听 `127.0.0.1`，终端打印的裸地址可在任意本机浏览器直接打开，不需要 token、
cookie 或账号。模型、DashScope 与本机 PostgreSQL 均在“设置”中配置，不读取 `.env`。
如不希望自动打开浏览器，可追加 `--no-open`。新建会话时只需选择“快速开始”或
“指定目录”；规划与权限在每轮输入旁选择。Agent 委派出的子任务会直接出现在对应主消息
下方，运行时展示各自的工具活动，完成后展示 Markdown 渲染的结果；任务页可集中查看同一
批任务事实。

## 前端验证

```sh
npm install
npm run lint
npm test -- --run
npm run build:local
```

`build:local` 会把静态资源写入本地应用服务的资源目录。开发前端时可使用
`npm run dev:local`，真实会话验证仍应通过完整本地应用完成。

产品与生命周期约束见仓库根目录的
[`PULSARA_FRONTEND_APPLICATION_SPEC.zh.md`](../PULSARA_FRONTEND_APPLICATION_SPEC.zh.md)
和
[`PULSARA_LOCAL_WEB_APPLICATION_LIFECYCLE_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](../PULSARA_LOCAL_WEB_APPLICATION_LIFECYCLE_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。
