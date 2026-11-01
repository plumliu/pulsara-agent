# Pulsara {{version}}

本次仅发布 **macOS 15+、Apple Silicon DMG**。这是桌面预览版，包含自上个发行版以来的代理协作、Code Mode、会话输入和工作台更新。

## 本次更新

- **代理与 Cell 工作台：**代理使用独立会话，支持继续协作、继承历史和查询已知代理；侧栏集中查看代理对话、运行状态、代码 Cell 与调用图。
- **Code Mode：**模型可在 JavaScript Cell 中组合工具调用、等待结果和处理输出，复用现有权限、取消和结果交付机制。
- **随时补充任务：**统一排队消息与 steer 输入，完善引用批注、队列预览、发送反馈和重试状态展示；为可安全重试的模型请求提供重试处理。
- **MCP Prompt：**与 Skill 共用 slash 菜单，支持 `/服务器/提示词` 或直接搜索提示词名称；填写参数后自动预览并插入草稿，无参数模板直接转成文字。模型仅查询轻量元数据，不再通过 `get_mcp_prompt` 展开正文。
- **MCP 与扩展能力：**改进本机网络接入、可选资源模板发现与能力目录查询；精简连接状态，明确快速会话的配置作用域及 Target 使用方式。
- **桌面体验：**调整工作区宽度、空状态、文件预览、浏览器标签、设置与概览，并修复模型目录恢复等问题。
- **macOS 打包修复：**使用完整的 ad-hoc 应用签名与资源封装，修正早期包保留不完整链接器签名、可能被系统提示“应用已损坏”的问题。

## 下载

{{downloads}}

GitHub 的 Source code 压缩包是源码，不是安装包。本版不提供 Windows、Ubuntu 或 macOS Intel 安装包。

## 安装与签名

打开 DMG，将 Pulsara 拖入“应用程序”，推出 DMG，再从“应用程序”启动。

此预览版采用 ad-hoc 签名，**没有 Apple Developer ID 分发签名，也未经过 Apple 公证**。应用签名完整不代表 Gatekeeper 会自动信任；首次打开可能被拦截。确认下载来源后，先尝试打开应用，再到“系统设置 → 隐私与安全性 → 仍要打开”允许。[Apple 官方说明](https://support.apple.com/zh-cn/102445)

无需全局关闭 Gatekeeper。若仍提示应用已损坏，请重新下载当前 DMG，并在反馈中附上 `codesign --verify --deep --strict --verbose=2 /Applications/Pulsara.app` 的输出。

配置有效模型连接即可开始使用。应用自带 Python、PostgreSQL、Chromium 和搜索工具，不要求预先安装 uv、Docker 或数据库；具体任务需要的软件可在对话中按需准备。

## 跨版本升级

**0.1.3 更改了数据库基线、代理会话结构及身份格式，不兼容 0.1.1 及更早发行版的数据库，也不提供自动迁移。**

更新前请完全退出旧版 Pulsara，并备份整个数据目录：默认 `~/.pulsara`，设置了 `PULSARA_HOME` 时则为指定目录。保留旧应用和旧数据以便查看历史；将历史迁入新版需要另行迁移。请为新版使用新的数据目录，不要直接删除旧目录。

例如，安装后从终端启动独立数据目录：

```sh
PULSARA_HOME="$HOME/.pulsara-0.1.3" /Applications/Pulsara.app/Contents/MacOS/pulsara
```

再在设置中配置模型连接。开发版与发行版默认共用数据及 CLI，不要同时运行。更新方式仍为手动替换应用。详细安装、数据与源码构建说明见 [安装指南](https://github.com/plumliu/pulsara-agent/blob/v0.1.3-alpha.1/INSTALL.zh-CN.md)和[开发指南](https://github.com/plumliu/pulsara-agent/blob/v0.1.3-alpha.1/DEVELOPMENT.zh-CN.md)。

## 验证范围

- 本地 macOS ARM64 构建，检查前端、桌面端、版本一致性和随包依赖；搬移后的 PostgreSQL、pgvector、pg_search、Python、Chromium 与 QuickJS 通过冒烟检查。
- 对最终 DMG 进行镜像校验，并从只读挂载的镜像中复制应用后检查资源封装及严格递归签名。
- 独立新数据目录启动到“本地服务已连接”的工作台；正常退出后再次通过签名校验。
- 自动化检查不代替完整图形桌面验收；未声明 Windows 或 Ubuntu 的本版发行验收，也未声明通过 Apple 公证或 Gatekeeper 自动放行。

问题反馈：[GitHub Issues](https://github.com/plumliu/pulsara-agent/issues)。请注明版本、系统、架构和复现步骤，不要附带 API Key 或 OAuth 令牌。
