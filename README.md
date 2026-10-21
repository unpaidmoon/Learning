# AI 辅助学习项目

面向个人学习的 AI 辅助工具，以考研数学一试点。Windows 使用定制界面、自有业务存储和 DeepSeek Harness 执行层；先交付用户主动视频截图、编辑、文字提问、保存与继续追问的试用闭环。

当前正式 P0-01～P0-04 与 V2 已按本轮范围验收，满足 P0 退出条件；**停在 P0，不自动进入 MVP-A**。现有页面是工程验证界面，尚不具备完整视频学习试用能力。工作包状态以 [MVP 开发计划](docs/plans/MVP开发计划.md)为准，验收依据见[报告导航](research/README.md)。

## 开发入口

先读 [AGENTS.md](AGENTS.md)和[开发规范](docs/development/开发规范.md)。固定工具、启动停止、配置与环境限制见[开发环境与命令](docs/development/开发环境与命令.md)，Harness 安装与执行策略见 [Harness 说明](harness/README.md)。

以下入口已实际执行过；目录整理后的复核范围见[迁移记录](docs/development/目录整理与迁移记录-2026-10-21.md)。在仓库根目录运行，工具来自项目环境，不修改全局 PATH：

```powershell
.\scripts\bootstrap.ps1 -SkipBrowsers
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs check
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs test
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs build
& .\.tools\node\node.exe .\.tools\pnpm\bin\pnpm.cjs test:e2e
& .\backend\.venv\Scripts\python.exe scripts/app.py dev
# 构建后同源运行：
& .\backend\.venv\Scripts\python.exe scripts/app.py start
```

以 `LEARNING_APP_READY` 输出的地址和实例 ID 为准。关闭网页后后台继续；Ctrl+C 或下面的实例命令停止并清理所属服务：

```powershell
& .\backend\.venv\Scripts\python.exe scripts/app.py stop --instance-id <就绪输出中的实例ID>
```

默认 `LEARNING_APP_HARNESS_ENABLED=false`，界面如实禁用发送。启用方式见 Harness 说明，模型密钥仅从后台 `.local/secrets/deepseek.env` 私有配置读取。

`test:e2e` 从 `tests/e2e/test_browser.py` 启动，使用已有 Chromium 和合成数据，不调用真实模型；浏览器缺失时明确失败。浏览器选项与权限验收入口见[验证说明](docs/validation/README.md)。

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
