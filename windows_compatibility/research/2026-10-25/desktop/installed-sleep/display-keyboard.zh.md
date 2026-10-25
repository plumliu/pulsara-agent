# 活动显示器与键盘布局只读核验

2026-10-25 18:28:48.860785+08原生API结果：

| 当前活动屏幕 | 主屏 | 分辨率(px) | 有效DPI | 有效缩放 |
| --- | --- | --- | --- | --- |
| DISPLAY6 | 是 | 1920×1080 | 96×96 | 100% |

当前EnumDisplayMonitors仅返回1个活动屏幕；没有查询inactive面板身份/硬件序列号，不能据此说笔记本没有内屏。用户可自行启用原生内屏并选择扩展模式，再读新快照；两个活动屏幕有效DPI不同时，才具备本次混合DPI测试条件。没有替用户改显示模式、分辨率或缩放。

GetKeyboardLayoutList仅返回0x8040804。已核正式安装exe的Pulsara PID8324可见窗口thread36108当前HKL同0x8040804，LANGID0804。没有切换/安装键盘布局，没有读取其他app窗口内容或控制UI。右Alt键帽、HKL与预载布局本身均不能替代实际AltGr字符输入验证。

观察脚本仅对自己新建的短命Python进程调用SetProcessDpiAwareness(2)，GetProcessDpiAwareness确认为2；EnumDisplayMonitors/GetMonitorInfoW读取物理坐标，GetDpiForMonitor(MDT_EFFECTIVE_DPI)读取有效DPI并除96得到缩放百分比。它不改变Windows显示设置；脚本exit0，查询进程HANDLE已finally关闭，不持有长期观察服务。

用户手动跨屏的观察目标是：同一Pulsara窗口与原会话持续、最大化/还原和标题按钮命中、菜单/弹窗落点、中文draft的候选窗/光标/点击位置、HTML/PNG/PDF预览和原生dialog的布局与点击。记录两屏各自有效缩放和跨回结果，不能把单屏100%称为混合DPI通过；不要关闭app/PG或操作用户其他应用。

用户完成自己的显示配置操作后，由root通知再次执行（新文件，拒绝覆盖原始当前快照）：

```powershell
& .venv/Scripts/python.exe -X utf8 output/phase-b/desktop/installed-sleep/display_keyboard_observe.py --pulsara-pid 8324 --output output/phase-b/desktop/installed-sleep/displays-keyboard-expanded.json
```

API依据：[GetDpiForMonitor](https://learn.microsoft.com/en-us/windows/win32/api/shellscalingapi/nf-shellscalingapi-getdpiformonitor)与[EnumDisplayMonitors](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-enumdisplaymonitors)。当前仅事实核验与手动目标准备，不声称多DPI/物理AltGr已经通过。

## 用户启用扩展后的独立快照

18:32:26+08保存displays-keyboard-extended.json；current原文件保留。

| 屏幕 | 主屏 | 物理桌面矩形 | 分辨率 | 有效DPI/缩放 |
| --- | --- | --- | --- | --- |
| DISPLAY6 | 是 | [0,0,1920,1080] | 1920×1080 | 96/100% |
| DISPLAY1 | 否，位于右侧 | [1920,0,4480,1600] | 2560×1600 | 144/150% |

此时具备100%↔150%混合DPI条件。首个扩展快照中Pulsara窗口rect为[-32000,-32000,-31840,-31972]，不能将最小化坐标或MonitorFromWindow的nearest返回值冒称实际在屏窗口位置。增加只读IsIconic后另存18:32:47+08的displays-keyboard-extended-windowstate.json，捕获同Pulsara8324/thread36108已非最小化，窗口rect[366,51,1822,979]，位于DISPLAY6，GetDpiForWindow96/100%。两次读取之间的用户界面操作未由观察脚本控制。原始两份文件均保留。

这只是跨屏前基准；未声称100%→150%窗口变化、渲染或命中已通过。root协调用户手动向右跨屏并返回后，可再次另存快照验证同窗口thread、GetDpiForWindow96→144→96及实际预览/点击表现。HKL仍0x8040804。

18:35:10+08用户手动移到内屏后，displays-keyboard-internal.json确认同PID8324/thread36108、非最小化，窗口完整位于DISPLAY1的rect[2061,96,4245,1488]，GetDpiForWindow144/150%。相对外屏基准96/100%，已真实观察96→144，未由脚本移动窗口。布局/预览视觉和交互由root独立核验，不将本元数据断言代替视觉通过；尚未采移回外屏。
