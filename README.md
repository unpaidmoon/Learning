# AI 辅助学习项目

面向个人学习的 AI 辅助工具，以考研数学一试点。Windows 使用定制界面、自有业务存储和 DeepSeek Harness 执行层；先交付用户主动视频截图、编辑、文字提问、保存与继续追问的试用闭环。

2026-10-21 后续反馈：用户不认可现 UI 及部分交互，已重新收口官方账号默认登录／可选 API Key、真实课程根目录／单文件标签页，以及全屏问答、软件内快捷键截图、选区引用卡片和播放控制。**设计收口不代表新行为已经实现**；本页下述命令与旧技术结果不构成新设计验收，实际工作包状态见 [MVP 计划](docs/plans/MVP开发计划.md)，完整规则见[需求总结](docs/product/AI辅助学习项目需求总结.md)。当前仍是讨论与文档同步，未在本轮改动应用或用户文件。

当前已按明确授权实施 MVP-A：Windows 原地视频播放、主动截图裁剪、文字与图片问答、公式阅读、保存恢复和安全退出。自动技术验证已完成，用户实际学习评价待参与；不能称完整 V3 价值验收或完整首版完成。见[本轮报告](research/reports/v3/MVP-A与V3验收报告-2026-10-21.md)和[人工试用记录](docs/validation/MVP-A试用记录.md)。工作包状态以 [MVP 开发计划](docs/plans/MVP开发计划.md)为准。本轮不含 Office／ASR、知识库、Android、同步或完整安装包。

## 开始试用

现有项目环境及构建就绪后，双击根目录 `启动学习.cmd`。下面是已实际验证过的命令行入口，在仓库根目录执行后，按就绪输出地址手动打开学习页：

```powershell
.\scripts\Start-Learning.ps1 -NoBrowser -Port 18798 -TimeoutSeconds 60
```

双击入口启用正式 Harness 并打开学习页；上述 `-NoBrowser` 命令启动同一后台，由你手动打开输出地址。密钥仅由后台读取既有私有配置。选择或新建课程后，主动关联本地视频（原地引用），点击具体视频恢复位置并播放。点击“截图提问”暂停，在编辑器确认选区后直接加入草稿，补充文字并发送；也可粘贴图片、继续追问或重开历史。模型与思考选择作用于后续请求，未支持的配置明确拒绝。

关闭聊天可返回视频；关闭网页后后台继续。用页面“安全退出”保存并停止所属服务，活动回答会先要求确认。原截图仅在当前浏览器本机保存供重裁，后台和模型只接收确认选区；更换浏览器或清除浏览器数据后，原图重裁缓存可能不可用，已确认附件仍保留。

入口的实际无浏览器 smoke 使用 `Start-Learning.ps1 -NoBrowser -NoHarness -Port 18769 -TimeoutSeconds 30`，随后核实例安全停止；它证明启动与清理，不代替真实模型验收。初次安装与构建仍需要下述项目开发环境，这不是无开发环境安装包。

## 开发入口

先读 [AGENTS.md](AGENTS.md)和[开发规范](docs/development/开发规范.md)。固定工具、启动停止、配置与环境限制见[开发环境与命令](docs/development/开发环境与命令.md)，Harness 安装与执行策略见 [Harness 说明](harness/README.md)。

以下入口已实际执行过；目录整理后的复核范围见[迁移记录](docs/development/目录整理与迁移记录-2026-10-21.md)。在仓库根目录运行，工具来自项目环境，不修改全局 PATH：

```powershell
.\scripts\bootstrap.ps1 -SkipBrowsers
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs check
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs test
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs build
& .\backend\.venv\Scripts\python.exe scripts/app.py dev
# 构建后同源运行：
& .\backend\.venv\Scripts\python.exe scripts/app.py start
```

以 `LEARNING_APP_READY` 输出的地址和实例 ID 为准。关闭网页后后台继续；Ctrl+C 或下面的实例命令停止并清理所属服务：

```powershell
& .\backend\.venv\Scripts\python.exe scripts/app.py stop --instance-id <就绪输出中的实例ID>
```

默认 `LEARNING_APP_HARNESS_ENABLED=false`，界面如实禁用发送。启用方式见 Harness 说明，模型密钥仅从后台 `.local/secrets/deepseek.env` 私有配置读取。

`test:e2e` 已映射到 `tests/e2e/test_mvp_a.py`；`test:e2e:security` 映射到旧 runner 的安全分组。两个根别名的 `--help` 转发已实际通过。完整浏览器验收使用下面的实际命令；这些 run ID 已保留原证据，复跑请更换 ID：

```powershell
& .\backend\.venv\Scripts\python.exe tests/e2e/test_mvp_a.py --run-id mvp-a-20261021-r4
& .\backend\.venv\Scripts\python.exe tests/e2e/test_browser.py --mode security --run-id mvp-a-security-20261021-r2 --backend-port 18850
& .\backend\.venv\Scripts\python.exe tests/e2e/test_native_video_picker.py --run-id mvp-a-native-picker-20261021-r1
```

它们使用已有固定 Chromium、合成数据及本地 provider，真实模型调用为 0；浏览器缺失时明确失败。真实模型分组使用独立的 `scripts/validation/Run-V3.py`，有实际出站及 raw usage 证据，不与上述 fixture 混称。浏览器选项与权限验收入口见[验证说明](docs/validation/README.md)。

文档导航可通过 `pnpm docs:check` 单独检查，也已纳入根 `check`；适用范围见[开发规范](docs/development/开发规范.md)。

## 仓库导航

| 路径 | 职责 |
| --- | --- |
| [frontend](frontend/) / [backend](backend/) / [harness](harness/) | 正式界面、业务后台、执行配置与插件 |
| [contracts](contracts/) | API schema、生成类型与契约样例 |
| [tests](tests/) / [scripts](scripts/) | 跨模块测试、公共 fixture；初始化、启动、生成与正式验证工具 |
| [docs](docs/README.md) | 需求、架构、开发规范、工作计划与验证方案 |
| [research](research/README.md) | 专题调研、验收报告与隔离实验 |

原视频、课件、聊天数据库、模型、凭据和内部执行 home 不入库。`.local/`、`.tools/`、`.runtime/` 与 `output/` 是本地目录；被忽略不代表可以删除，其中可能含业务资产和唯一原始证据。依赖锁正常入库，第三方许可保留原样。
