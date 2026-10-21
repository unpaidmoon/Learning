# AI 辅助学习项目

Windows 个人学习工作台：左侧课程切换与当前课程文件树，中间单资料标签页，右侧 AI。正式 MVP-A 使用 Codex 风格的浅深主题、自有业务数据和固定 DeepSeek Harness；先提供视频、图片、文本、用户主动截图与连续问答。

本轮重设计已获明确授权，最终实施与验收状态只在 [MVP 计划](docs/plans/MVP开发计划.md)维护。界面规范见 [前端开发规范](docs/architecture/前端开发规范.md)，接口与输入边界见 [应用契约](docs/architecture/应用契约.md)。历史 MVP-A/V3 证据保留，不能替代本轮验证或用户学习评价。

## 使用

项目环境与生产构建就绪后，双击根目录 `启动学习.cmd`。命令行可在仓库根目录运行以下本轮已成功验证的无浏览器入口，按输出地址打开：

```powershell
.\scripts\Start-Learning.ps1 -NoBrowser -Port 18872 -TimeoutSeconds 60
```

1. 点击“账号与模型”。默认官方账号；点击登录后在官方网页完成授权，应用接回状态并刷新实际模型目录及额度。已经在本项目登录过可直接使用保存状态。API Key 仅通过用户主动切换启用，来自规定私有配置；账号失败不会自动改用 API Key。
2. 打开课程文件夹，或为旧课程选择根目录。左侧顶部切换课程，下方浏览全部文件。外部资料导入默认移动，可选复制；冲突由你选择，未知结果先点核对，不重复导入。
3. 点击文件在中间标签页打开；同一时刻显示一个资料。视频／图片／文本内置支持，PDF／Office 等提供“使用系统默认程序打开”。切离视频自动暂停，切回恢复切走前的播放状态。
4. `Ctrl+Shift+S` 冻结软件当前可见画面，拖选松开把选区加入草稿；AI 自动展开并聚焦输入。补充问题后自行发送。原始快照留在当前浏览器本机，后台和模型只接收确认选区。
5. 回答中选中文字或可精确映射的公式范围，再点临时“添加到对话”，形成可移除引用卡片。复杂无法对应源码的范围会明确提示，不补引整式。
6. 右侧顶部可重开当前课程历史、新建对话或收起 AI。全屏时用 `Ctrl+Shift+A` 或鼠标右侧触边入口唤起当前 AI；关闭后回到纯视频全屏续播。

播放器：空格／单击播放暂停、双击全屏、ESC 退出全屏、左右短按两秒、长按右方向键临时两倍速、M 静音；全屏视频区域或普通音量控件上滚轮调音量。聊天区保留滚动与输入法操作。

关闭网页后后台继续回答；页面“安全退出”保存并停止所属实例，活动回答先确认。停止回答只取消当前生成。数据、已提交截图、聊天、旧来源及原始验证证据不自动删除。

如需独立重新登录，下面的正式辅助入口也已实际验证成功。它会输出当次真实官方授权链接并等待浏览器完成登录，凭据交给 Harness 保存；正式应用与该辅助入口不要同时使用同一 home：

```powershell
.\backend\.venv\Scripts\python.exe scripts/harness/account_login.py --data-dir .runtime/app/data --timeout 1200
```

本轮技术结果与未覆盖项见 [重设计验证报告](research/reports/redesign/MVP-A重设计验收报告-2026-10-21.md)。合成学习页可看 [浅色预览](output/playwright/redesign-browser-20261021-r4/browser/learning-light.png)／[深色预览](output/playwright/redesign-browser-20261021-r4/browser/learning-dark.png)；画面中的彩色区域为测试资料。

## 开发与验证

先读 [AGENTS.md](AGENTS.md)、[开发规范](docs/development/开发规范.md)及 [前端开发规范](docs/architecture/前端开发规范.md)。使用项目锁定环境，不修改全局 PATH、HOME 或 CODEX_HOME。以下命令已在项目环境实际成功执行；不同源码快照的验证范围仍须分别记录：

```powershell
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs check
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs test
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs build
```

本轮完整合成浏览器实际成功命令（使用现有固定 Chromium 与 ffmpeg，不调用互联网模型）：

```powershell
& .\backend\.venv\Scripts\python.exe tests/e2e/test_redesign.py --run-id redesign-browser-20261021-r4 --backend-port 18871 --ffmpeg-path "C:\Users\cyb\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
```

重复验证请使用新的 `--run-id`，保留旧证据；其他机器按现有环境传入 `--browser-path`／`--ffmpeg-path`。独立真实模型复测用 `scripts/validation/Run-RedesignReal.py`，参数与实际六次请求见本轮报告，不与上述 fixture 混称。

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
