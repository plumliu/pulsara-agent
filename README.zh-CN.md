<h1 align="center"><img src="frontend/public/assets/pulsara-icon.png" width="36" height="36" align="top" alt=""> Pulsara</h1>
<p align="center"><strong>你自己的 Agent 团队。🪐</strong></p>

<p align="center">
  <img src="assets/banner.png" alt="Pulsara — 持久会话、可审阅的工作记录、语义记忆" width="100%">
</p>
<p align="center">
  <a href="https://github.com/plumliu/pulsara-agent/releases/latest"><img src="https://img.shields.io/github/v/release/plumliu/pulsara-agent?include_prereleases&style=for-the-badge&color=b87b2d" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-111827?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/Electron-44-47848F?style=for-the-badge&logo=electron&logoColor=white" alt="Electron 44">
  <img src="https://img.shields.io/badge/React-19-149ECA?style=for-the-badge&logo=react&logoColor=white" alt="React 19">
  <img src="https://img.shields.io/badge/PostgreSQL-17-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL 17">
</p>
<p align="center">
  <a href="https://github.com/plumliu/pulsara-agent/releases/latest">下载桌面版</a> &nbsp; | &nbsp;
  <a href="#演示">观看演示</a> &nbsp; | &nbsp;
  <a href="INSTALL.md">安装说明</a> &nbsp; | &nbsp;
  <a href="README.md">English</a>
</p>

## Demo

https://github.com/user-attachments/assets/f78ec29d-f18f-4f9a-b98d-4ce3638ed35f

<table>
  <tr>
    <td width="50%" align="center"><strong>“比较三种方法在不同样本下的误差，给我 Python 绘图脚本。” 📊</strong></td>
    <td width="50%" align="center"><strong>“设计一个知识库系统，画个架构图并整理下组件职责给我。” 🏗️</strong></td>
  </tr>
  <tr>
    <td><a href="assets/demo-img/research-chart-with-source.png"><img src="assets/demo-img/research-chart-with-source.png" width="100%" alt="科研图表与 Python 源码并排展示"></a></td>
    <td><a href="assets/demo-img/architecture-diagram-and-notes.png"><img src="assets/demo-img/architecture-diagram-and-notes.png" width="100%" alt="架构图与组件职责文档并排展示"></a></td>
  </tr>
  <tr>
    <td width="50%" align="center"><strong>“画一张‘咖啡从产地到杯子’的流程信息图，包含种植、处理、烘焙、研磨和萃取。” ☕️</strong></td>
    <td width="50%" align="center"><strong>“帮我规划作品集网站，整理需求和站点地图。” 🗺️</strong></td>
  </tr>
  <tr>
    <td><a href="assets/demo-img/coffee-journey-svg.png"><img src="assets/demo-img/coffee-journey-svg.png" width="100%" alt="咖啡从种植到萃取的 SVG 流程图"></a></td>
    <td><a href="assets/demo-img/project-plan-and-sitemap.png"><img src="assets/demo-img/project-plan-and-sitemap.png" width="100%" alt="作品集网站需求与站点地图"></a></td>
  </tr>
</table>

## 别忘了你有一整支团队

让一个 Agent 追查问题，另一个比较方案，再安排独立审阅。Pulsara 可以按任务搭配模型，每个 Subagent 都有自己的上下文；你能打开任务图点进任何一个任务，看它做到了哪里。

一份扎实的调查还可以接着用。已完成子任务的对话和工具历史，能交给后续任务继续推进。实现与审阅从同一份发现出发，各自判断，省掉重新解释整个问题的功夫。

> 追一下这个 Bug，比较两种修法。选定后帮我改好，让另一个 Agent 独立审阅，再跑相关测试。

## 随时都能提需求

工具调用、后台命令和子代理进度都能展开看。临时想起一个新想法，就能把那条消息及时告诉 Pulsara。

你也能选中回复针对它做批注，比反复讲“刚才那个文件”省心多了。

## 工作再繁杂 Pulsara 也跟得上

新会话会尝试根据第一份用户输入生成标题。回答结束后，空输入框可能显示一句追问建议。建议只缓存在内存中：输入文字时隐藏，清空草稿后重新显示，发送下一条消息时清除。标题和建议都在后台生成，不延迟正式回答。

耗时命令可以放到后台，Agent 继续推进别的工作。对话变长后可以压缩上下文，保存的会话让你下次回来仍有记录可查。

Pulsara 也认真对待长上下文的开销。稳定的提示词前缀让支持缓存的模型服务复用已经处理过的输入，命中时能省下费用、缩短等待。

你的偏好、项目约束和已有决策可以存进记忆，留给后续任务检索。在记忆页里能直接查看、修改或删除。

## 装好就能开工

[下载最新桌面版](https://github.com/plumliu/pulsara-agent/releases/latest)

| 操作系统 | 快捷通道 |
| --- | --- |
| macOS 15+ (Apple Silicon) | [下载 DMG 🚀](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.0-alpha.6/Pulsara-0.1.0-alpha.6-macos-arm64.dmg) |
| Ubuntu 24.04 (amd64) | [下载 deb 📦](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.0-alpha.2/Pulsara-0.1.0-alpha.2-linux-amd64.deb) |
| Ubuntu 24.04 (arm64) | [下载 deb 📦](https://github.com/plumliu/pulsara-agent/releases/download/v0.1.0-alpha.2/Pulsara-0.1.0-alpha.2-linux-arm64.deb) |

> 💡 **Ubuntu 用户贴士**：Ubuntu 用户建议优先按[源码构建指南](DEVELOPMENT.md)构建当前版本。上方保留的 alpha.2 deb 不包含 alpha.6 的最新修复。

1. 安装并打开 Pulsara。macOS 把应用拖进“应用程序”；Ubuntu 从源码构建后安装生成的 deb。
2. 在设置中添加模型连接，选择提供方、模型和 API 协议，按界面提示填写认证信息或自定义配置。自定义服务填写 Base URL 后即可获取模型列表，有密钥时携带、没有时匿名查询；Model ID 支持搜索选择或手填，获取失败不影响手填和保存。
3. 开一个快速开始会话，拖进文件，或者直接打开项目目录。把任务交给它。

预设模型目录继续使用 models.dev 的能力信息，保留上下文至少 256,000 tokens 的选择门槛，不按 Claude、Gemini 等模型名称屏蔽。用户自行选择提供方和 API 协议；实际调用仍使用现有协议适配和能力校验。

当前是桌面预览版。macOS 首次打开的系统授权、更新方式与数据位置写在[安装说明](INSTALL.md)里。偏爱终端的话，应用也带了 `pulsara` 命令行入口。

## 30 秒上手，把脏活累活交出去

欢迎把作品和使用体验带到 [Issues](https://github.com/plumliu/pulsara-agent/issues)，也欢迎提需求、报 Bug。想一起做 Pulsara，可以看[开发指南](DEVELOPMENT.md)和 [Pull requests](https://github.com/plumliu/pulsara-agent/pulls)。

如果它对你有帮助，给 Pulsara 点个 Star吧。
