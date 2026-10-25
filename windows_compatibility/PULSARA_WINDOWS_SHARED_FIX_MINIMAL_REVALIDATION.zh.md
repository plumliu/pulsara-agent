# Windows 共享修改的定向重验范围

此清单定义代码改变后如何选择必要回归，不表示每次提交或合并都已执行这些项目。完整平台资格以[实现合同第13节](PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md#13-验证矩阵与完成条件)为准。

| 范围 | 按受改边界验证 |
| --- | --- |
| 版本与资源 | Python/npm/锁文件一致、Windows 与 WSL 相同提交、payload 与外壳版本匹配、字体与许可证完整 |
| terminal 与跨域范围 | 独立命令、PIPE/PTY、首次绑定、取消、后代存活、物理 join、输出 drain、owner 关闭及 late outcome |
| PG 与 runtime | 双锁、未提交/unknown/failed 分类、EOF/失联、拒绝误认领、退出状态、reset 保数据及维护准入 |
| ROOT 与会话 | 接纳前后 head 变化、绑定/权限/context/replay 一致性、accepted-turn 唯一接纳、原 epoch 前缀和 append-only 输入 |
| Windows 宿主 | WSL 目标、TLS origin/pin、控制链、目录选择及原生路径入口、helper/安装器生命周期 |
| 工作台与安装版 | 设置、中文路径、附件、HTML/image/PDF、正常退出和重开；受改 UI 由实际环境验证 |
| 共享平台 | macOS 与 Linux 受影响的源码及应用行为；WSL 不能替代原生 Ubuntu GUI 资格 |

复用 `desktop/src/*.test.ts`、`tests/` 中的既有回归和 `desktop/scripts/` 中的资源验证入口，按实际环境选择对应测试；Windows 上的 POSIX 路径断言应在 WSL/POSIX 环境执行，不削弱断言或新增 skip/xfail。

真实 PE、OAuth、睡眠、多显示器和真实 provider 验证仅在相关边界改变且得到当前任务授权时进行。已经明确延期的项目仍记为延期，不能用较窄的测试结果改写为通过。实验脚本和历史结果保留在原分支，不进入生产导入或构建闭包。
