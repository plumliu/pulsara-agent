# 安装后快捷键展示与文档只读核对

父任务报告正式安装版工作台已 ready，用户已配置模型连接。本分支没有操作 GUI、读取配置或自行验证模型调用。

本轮仅修改 `frontend/components/session-sidebar.tsx`：原两个 kbd 硬编码 ⌘；现在读取既有可信 desktopHost().platform，darwin 显示 ⌘，Windows/Linux 显示 Ctrl。`pulsara-app.tsx` 与搜索框原 handler 已接受 metaKey/ctrlKey，不改变快捷键事件合同，不添加 platform API 或 navigator 猜测。

依父任务低影响文案要求，没有新增镜像测试。复用原 desktop-window-controls 与 pulsara-app 的搜索/新建/平台控件相关筛选共5项通过，189项为 -t 未选择，非完整前端套件；原断言和 skip 配置未改。frontend TypeScript noEmit 通过。原始输出为 sidebar-shortcuts-tests.log / sidebar-shortcuts-types.log。没有 build static、重打 wheel/NSIS 或改变正在验收的已安装 GUI；等父任务收齐其他 GUI 项后统一生成候选。

INSTALL.md Windows 部分与当前实际合同一致：已有 WSL2 Ubuntu/非 root 明确确认、Linux 资源/home、Windows host logs、同域 UNC 与字节导入、维护门拒绝 busy/unknown、卸载保 Linux data、同版材料不一致拒绝复用，未虚称发布激活。

DEVELOPMENT.md 两份 checkout、native Linux 产 payload.tar/manifest.json、Windows resources/wsl-payload 输入、desktop:windows-package 和 NSIS 输出名、构建时 .NET compiler/运行用户不依赖 Windows Python 的描述均与 recipe 一致。

唯一建议：该文的本地 Windows 指令只运行 desktop:wsl-payload，其真实依赖为 desktop:resources，包脚本只调用 validate_resources；不会执行 smoke_resources.py。正文通用“打包前始终临时数据库搬移检查”因此不涵盖这条本地路径。建议父任务在 Linux 指令先加 desktop:verify 再做 desktop:wsl-payload；CI 已在该 native job 单独跑 relocation smoke。本分支只读文档，没有替父任务改文档/任务图。
