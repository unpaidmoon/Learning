# AI 辅助学习项目

最新桌面试用以 **DeepSeek Harness 的视觉与原生交互** 为基础，保留本项目 **左课程目录／中资料／右 AI** 布局。双击 [启动Harness桌面.cmd](启动Harness桌面.cmd)，默认端口 `8766`、独立试用数据目录。已迁入既有文件管理、图片／文本／Markdown 阅读、播放器与全屏、截图重裁、完整文档附件、回答引用和自动草稿；PDF／Office 阅读仍沿用系统程序。目录窗口前置，视频预览后台缓存邻近帧，空对话重复新建保留文字和附件，补充条件或纠错直接发普通消息。完整回归 49 项通过，真实问答、正文活动停止与同会话追问已验证；另保留一次长数学请求的思考截断，准确范围见 [本轮迁移报告](research/reports/harness-desktop/Harness桌面既有功能迁移验收-2026-10-22.md)。使用见 [桌面说明](desktop/README.md)；原浏览器入口和数据继续保留。若旧桌面仍在运行，请从托盘退出后重新双击 CMD 加载本轮模块。

Windows 个人学习工作台：左侧课程切换与当前课程文件树，中间文件资源管理器及资料标签页，右侧 AI。桌面使用 Harness 原生主题和交互，原浏览器试用保留上一轮 Codex 风格浅深主题；两者共用项目业务模块与固定 DeepSeek Harness 执行层。

本轮重设计已获明确授权，最终实施与验收状态只在 [MVP 计划](docs/plans/MVP开发计划.md)维护。界面规范见 [前端开发规范](docs/architecture/前端开发规范.md)，接口与输入边界见 [应用契约](docs/architecture/应用契约.md)。历史 MVP-A/V3 证据保留，不能替代本轮验证或用户学习评价。

## 使用

最新界面按精简原则收口，统一文字、按需显示操作并补齐键盘与轻动效；[界面与验证记录](research/reports/harness-desktop/桌面精简界面与交互验证-2026-10-22.md)。旧桌面从托盘正常退出后，再用原 CMD 打开加载。

再次双击桌面 CMD 会恢复同一窗口；隐藏启动失败现在会显示错误并保留退出码。旧后台残留导致的本次闪退已修复，最新桌面已实际重新打开。见 [启动补修记录](research/reports/harness-desktop/CMD后台残留与启动结果回报修复-2026-10-22.md)。

桌面 CMD 入口的 Windows PowerShell 编码错误已修复，原始 CMD 已实际启动并显示桌面窗口，入口与退出码回归共 10 项通过。见 [补修记录](research/reports/harness-desktop/CMD启动入口修复-2026-10-22.md)。

2026-10-22 已修复主按钮文字同色、浅深菜单选项不可见及 `det` 等命名算符的局部引用错误；正式前端制品已更新，已打开页面按 `Ctrl+F5` 加载。见[修复记录](research/reports/workspace-v2/UI颜色与命名算符局部引用修复-2026-10-22.md)。

2026-10-22 已修复创建课程 `PICKER_UNAVAILABLE`：目录选择改用 Windows 系统窗口，正式后台已重启。已打开页面请刷新后重新选择；[修复及实际窗口验证](research/reports/workspace-v2/原生课程目录选择修复-2026-10-22.md)。

项目环境与生产构建就绪后，双击根目录 `启动学习.cmd`。命令行可在仓库根目录运行以下本轮已成功验证的无浏览器入口，按输出地址打开：

```powershell
.\scripts\Start-Learning.ps1 -NoBrowser -Port 19175 -TimeoutSeconds 60
```

1. 桌面从左下“设置 → 账号与模型”打开配置；原浏览器界面直接点击“账号与模型”。默认官方账号，点击登录后在官方网页完成授权，应用接回状态并刷新实际模型目录及额度。已经在本项目登录过可直接使用保存状态。API Key 仅通过用户主动切换启用，来自规定私有配置；账号失败不会自动改用 API Key。
2. 点击创建课程，用原生对话框选择文件夹，再确认课程显示名。显示名独立于磁盘目录，桌面课程名旁悬停菜单可重命名或移除；移除保留原件和聊天。左上按钮可收起整个课程目录。
3. 中间固定“文件”页默认展示目录图标；支持前进／后退、上级、路径导航、图标／列表、当前目录搜索、排序、多选、右键及属性。目录内可复制／剪切／粘贴、重命名、新建目录；删除进入本机固定卷的 Windows 回收站。外部文件或目录可粘贴／拖入，只复制、同名自动保留两份。侧栏刷新发现磁盘变化；没有独立导入入口。
4. 打开资料后以标签页显示，最后一个视频也可关闭并返回“文件”页。视频／图片／文本内置支持，PDF／Office 阅读仍由系统程序打开；右侧“+”可单独添加整份 PDF／TXT／Markdown 问答附件，不导入课程。文件读取失败或超限会明确拒绝，不截断、不自动发送。
5. `Ctrl+Shift+S` 冻结软件当前可见画面，拖选松开把选区加入草稿；AI 自动展开并聚焦输入。补充问题后自行发送。原始快照留在当前浏览器本机，后台和模型只接收确认选区。
6. 回答中选中文字或公式范围，再点临时“添加到对话”，形成可移除引用卡片。整条分块矩阵含标点可引用，局部范围只引用实际选中部分；不能精确对应的范围仍明确拒绝。
7. 右侧直接显示输入区，草稿自动保存。左侧对话标题下拉可接续当前课程历史，也可新建对话或收起 AI。全屏时用 `Ctrl+Shift+A` 或鼠标右侧触边入口唤起当前 AI；关闭后回到纯视频全屏续播。

播放器：悬停进度条显示本地画面与时间；拖动时圆点和预览跟随，主视频继续原进度播放，松开才跳转一次并保留播放／暂停状态。空格／单击播放暂停、双击全屏、ESC 退出全屏、左右短按两秒、长按右方向键临时两倍速、M 静音；全屏视频区域或普通音量控件上滚轮调音量。聊天区保留滚动与输入法操作。

关闭网页后后台继续回答；页面“安全退出”保存并停止所属实例，活动回答先确认。停止回答只取消当前生成。数据、已提交截图、聊天、旧来源及原始验证证据不自动删除。

如需独立重新登录，下面的正式辅助入口也已实际验证成功。它会输出当次真实官方授权链接并等待浏览器完成登录，凭据交给 Harness 保存；正式应用与该辅助入口不要同时使用同一 home：

```powershell
.\backend\.venv\Scripts\python.exe scripts/harness/account_login.py --data-dir .runtime/app/data --timeout 1200
```

本轮技术结果与未覆盖项见 [交互修订验证报告](research/reports/workspace-v2/MVP-A交互修订验收报告-2026-10-22.md)。合成学习页可看 [浅色预览](output/playwright/workspace-v2-browser-20261022-r1/browser/learning-files-light.png)／[深色预览](output/playwright/workspace-v2-browser-20261022-r1/browser/learning-files-dark.png)；均为合成资料。当前 PDF 附件最多 20 MiB／32 页，TXT／Markdown 最多 1 MiB／20 万字符，超限整份拒绝。允许历史中的 PDF 页可能重复附带，增加实际输入；详细限制和未覆盖项见报告。

## 开发与验证

先读 [AGENTS.md](AGENTS.md)、[开发规范](docs/development/开发规范.md)及 [前端开发规范](docs/architecture/前端开发规范.md)。使用项目锁定环境，不修改全局 PATH、HOME 或 CODEX_HOME。以下命令已在项目环境实际成功执行；不同源码快照的验证范围仍须分别记录：

```powershell
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs check
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs test
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs build
```

本轮完整合成浏览器实际成功命令（使用现有固定 Chromium 与 ffmpeg，不调用互联网模型）：

```powershell
& .\backend\.venv\Scripts\python.exe tests/e2e/test_workspace_v2.py --mode full --run-id workspace-v2-browser-20261022-r1 --backend-port 19170 --ffmpeg-path "C:\Users\cyb\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
```

重复验证请使用新的 `--run-id`，保留旧证据；其他机器按现有环境传入 `--browser-path`／`--ffmpeg-path`。本轮前端 129／后台 206 通过、1 跳过，完整合成浏览器 67／67。独立整份附件真实模型复测用 `scripts/validation/Run-WorkspaceV2Real.py`，实际官方账号三次请求见本轮报告，不与上述 fixture 或上一轮两路六次请求混称。

启动与环境细则见 [开发环境与命令](docs/development/开发环境与命令.md)，固定制品、官方账号及执行 guard 见 [Harness 说明](harness/README.md)。按就绪输出的实例 ID 请求安全停止：

```powershell
& .\backend\.venv\Scripts\python.exe scripts/app.py stop --instance-id <就绪输出中的实例ID>
```

默认 `LEARNING_APP_HARNESS_ENABLED=false`；双击统一入口会显式启用 Harness。仅启动业务后台不代表真实模型可用。合成浏览器、真实模型、数学表现和实际学习评价分别记录；本轮不含完整 Office／ASR、知识库用途、Android／同步、自由多窗口或完整安装包。

## 仓库导航

| 路径 | 职责 |
| --- | --- |
| [frontend](frontend/) / [backend](backend/) / [harness](harness/) | 正式界面、业务后台、执行配置与插件 |
| [contracts](contracts/) | API schema、生成类型与契约样例 |
| [tests](tests/) / [scripts](scripts/) | 跨模块测试、公共 fixture；初始化、启动、生成与正式验证工具 |
| [docs](docs/README.md) | 需求、架构、开发规范、工作计划与验证方案 |
| [research](research/README.md) | 专题调研、验收报告与隔离实验 |

原视频、课件、聊天数据库、模型、凭据和内部执行 home 不入库。`.local/`、`.tools/`、`.runtime/` 与 `output/` 是本地目录；被忽略不代表可以删除，其中可能含业务资产和唯一原始证据。依赖锁正常入库，第三方许可保留原样。
