# Harness 插件复用清单与依赖核查

核查日期：2026-10-18（Asia/Shanghai）  
状态：只读源码与文档研究；未安装插件或模型，未启动 Harness 原型，未发送付费模型请求，未从视频抽帧。

## 1. 当前架构决定与研究边界

按用户新的架构决定，**Windows 运行 DeepSeek Harness 及适用插件，作为执行层主选；安卓保留独立基础学习和直接调用 DeepSeek 的能力，不运行完整 Harness。** 界面完全定制，应用自己的课程、资料、聊天、附件、播放进度和学习记录 schema 是业务数据的统一来源；Harness 内部会话必须映射到应用会话，不把 Harness 的 JSONL、内部数据库和本机路径作为多端同步协议。

本报告替代此前《AI接入与Harness评估依据.md》中“Windows 首版不依赖 Harness”的选型建议，保留其历史比较。Python GPU ASR 仍可作为本项目工作进程；Tailscale 仅负责设备连接。截图与文字优先，必要时字幕，再必要时资料检索；**不提供让 AI 自动取得其他视频画面的能力。**

本次固定研究提交为 [`687f282e8625c6b8d11ff58211a90524be2e4317`](https://github.com/deepseek-ai/deepseek-harness/commit/687f282e8625c6b8d11ff58211a90524be2e4317)，GitHub 返回提交时间 2026-10-17。下列列举的官方包源码版本均为 `0.2.1-rc.2`（有特别注明者除外）。这证明研究时源码存在，不证明某制品已在本机安装或通过 Windows 验证。仓库自述仍为 developer preview，并提示兼容性变化。[固定提交 README](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/README.md)

## 2. “插件目录”具体指什么

需要区分三种来源，不能把“有插件管理器”理解为“已有全部学习插件”：

| 来源 | 可证事实 | 对本项目的意义 |
| --- | --- | --- |
| 仓库内官方包 | `packages/` 下的模型、会话、工具、UI、实验功能等组件 | 可以检查源码、组合或维护修改版；不必加载官方 UI |
| 官方可选／按需 bundle 名单 | 随安装可选名单与按需安装 admission 是源码显式清单 | 可以证明哪些功能真实存在及被官方发行组合纳入 |
| 第三方 Plugin Market | 官方明确不附带市场索引来源，内容属于第三方声明 | 未配置索引时不能假定有可用 ASR/RAG 学习插件；本轮不选择未核实的第三方包 |

市场实现为 `@deepseek-ai/dsh-plugin-market`，可选界面 bundle 为 `@deepseek-ai/dsh-experimental-plugin-market-bundle`。官方没有默认 index source，npm registry 是安装包来源，不能当作学习插件目录。[市场说明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/user/guide/plugin-market.md)、[市场组件](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/boot/plugin-market/README.md)

### 2.1 官方随安装可选的 12 个 bundle

以下为真实源码名称，**不是本项目全部要启用的清单**：[OPTIONAL_BUNDLES](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/boot/app-boot/src/profile.ts#L223-L236)

```text
@deepseek-ai/dsh-experimental-session-search
@deepseek-ai/dsh-experimental-ralph-bundle
@deepseek-ai/dsh-experimental-terminal-bundle
@deepseek-ai/dsh-experimental-badge-skill-bundle
@deepseek-ai/dsh-experimental-session-titles-bundle
@deepseek-ai/dsh-experimental-agent-team-profile
@deepseek-ai/dsh-experimental-voice-input-bundle
@deepseek-ai/dsh-experimental-cot-translation-bundle
@deepseek-ai/dsh-experimental-auto-review
@deepseek-ai/dsh-experimental-inspector-profile
@deepseek-ai/dsh-experimental-plugin-market-bundle
@deepseek-ai/dsh-experimental-tool-worktree
```

官方按需 admission 的另一个清单只有 `@deepseek-ai/dsh-subagent-claude-code` 与 `@deepseek-ai/dsh-subagent-codex`；它们不能作为现成资料问答或学习插件的证据。[ON_DEMAND_BUNDLES](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/boot/app-boot/src/official-bundle-packages.ts)

## 3. 官方 Host 能力复用矩阵

下表中的“可无 UI”指源码提供 Host／SDK 组合入口，无须挂载官方 Web UI；不是已经完成启动测试。“依赖”列列出已核查的关键服务注入与额外运行依赖，不是完整 npm 锁文件。`peerDependencies` 声明与 Cordis 的硬服务注入不是同一件事，不能只凭包清单删掉服务。整体依赖闭包仍须通过实际 profile 构建、配置导出和启动验证。

| 确切包名；现成 profile 行 ID（如适用） | 能力与关键依赖 | UI／无 UI | 首版复用判断 |
| --- | --- | --- | --- |
| `@deepseek-ai/dsh-sdk-client` | TypeScript 客户端启动同版本 `dsh` 子进程、发消息和订阅通知；依赖 `@deepseek-ai/dsh` 与 SDK protocol | Host 外库，可无 UI | 作为自有应用驱动 Harness 的候选；需解决取消与消息归因 |
| `@deepseek-ai/dsh-sdk-app`；`sdk-app-startup` | SDK profile 启动；包依赖 JSON-RPC server 与 CLI 组合 | 可无 UI | 复用官方受支持入口，不另造私有 runtime 启动方式 |
| `@deepseek-ai/dsh-sdk-jsonrpc-server`；`sdk-jsonrpc-server` | stdio JSON-RPC；源码硬注入 `agents`、`workingDirectory` | 可无 UI | 接入自有后台；非独立 HTTP Web 服务 |
| `@deepseek-ai/dsh-llm`；`llm` | 模型请求、流事件和适配器接口；基于 Cordis | 可无 UI | 必要基础能力 |
| `@deepseek-ai/dsh-llm-deepseek-api-key`；`llm-deepseek` | 注册 `deepseek-official`，硬注入 `llm`；凭据服务存在则解析凭据，否则从启动环境取引用 | 可无 UI | 复用官方 API route；固定只启用需要的模型 |
| `@deepseek-ai/dsh-llm-deepseek` | 共享 DeepSeek Messages transport 库，被 route 包使用；图片还需 attachment service | 可无 UI；不是另一套 UI | 随官方 route 复用，不额外再包一层 Python OpenAI SDK |
| `@deepseek-ai/dsh-session`；`session` | 事件日志和模型历史投影；没有持久化 backend 时只在内存 | 可无 UI | 执行记录复用，映射到业务聊天，不能直接当全应用数据模型 |
| `@deepseek-ai/dsh-session-persistence-jsonl`；`sessions` 或 `session-persistence-jsonl` | Harness 执行日志持久化；依赖 session/persistence 格式栈，含本地原生依赖 | 可无 UI | 用于 Windows 执行恢复；不作为跨端同步载荷 |
| `@deepseek-ai/dsh-agent`＋`@deepseek-ai/dsh-agent-loop`；`agent`／`agent-loop` | agent loop 硬注入 `agents`、`sessions`、`llm`、`tools`、`systemPrompt`、`sessionProjections` | 可无 UI | 首版只启用学习问答 agent；不默认多代理 |
| `@deepseek-ai/dsh-system-prompt`；`system-prompt` | 系统说明、工具 schema 组合 | 可无 UI | 用本项目学习说明替换软件工程 persona |
| `@deepseek-ai/dsh-tools`；`tools` | 工具注册、schema、校验与执行；硬注入 `systemPrompt` | 可无 UI | 复用注册与 guard；采用 native tools，首版无须 PTC `run_code` |
| `@deepseek-ai/dsh-session-projection`；`session-projection` | 提供执行状态等投影服务，agent loop 与 token meter 使用 | 可无 UI | 作为依赖保留，不把投影内部格式同步到安卓 |
| `@deepseek-ai/dsh-working-directory`；`working-directory` | 提供 SDK 与工具需要的 Session cwd 服务 | 可无 UI | 可保留服务，仍禁用模型的更改目录工具 |
| `@deepseek-ai/dsh-attachment-local`；`attachment-local` | 本地去重、图片验证、规范化与请求图缓存；依赖 attachment seam、DSH home、`sharp 0.35.5` | 可无 UI | 复用执行副本；用户原始裁剪与来源关系由应用自有附件管理 |
| `@deepseek-ai/dsh-token-meter`；`token-meter` | 用量与上下文压力投影；硬注入 `sessionProjections` | 可无 UI | 用于执行用量，转为应用自己的统计字段 |
| `@deepseek-ai/dsh-compaction-basic`；`compaction-basic` | 自动上下文摘要；硬注入 `llm`、`tokenMeter`、`sessions` | 可无 UI | 条件复用；摘要会额外调用模型，需避免丢当前题目条件 |
| `@deepseek-ai/dsh-compaction-image-offload`；`image-offload` | 图片预算超限后移除较早图片的模型投影，并保留日志 | 可无 UI | 条件复用；需验证当前题目图不被错误移除，不能视为业务附件删除 |
| `@deepseek-ai/dsh-llm-retry`；`llm-retry` | agent step 失败重试；策略在 provider 配置，非该插件自有 config | 可无 UI | 显式限制重试；不能默认无限重试或叠加多层重试 |

以上能力对应的固定源码与配置入口：[SDK 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/README.md)、[SDK server 依赖](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/server/src/index.ts)、[sdk-minimal 实际组合](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/bundle/sdk-minimal/cordis.patch.yml)、[agent-loop 依赖](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/index.ts)、[附件 package](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/attachment/attachment-local/package.json)。

### 3.1 模型与图片适配的具体差异

Windows 使用官方 Harness route 时，底层是 Anthropic-compatible Messages，默认根为 `https://api.deepseek.com/anthropic`；不能将 OpenAI Chat Completions 的参数和 URL 原样填进去。Harness 内部推理强度为 `off/low/high/max`，`off` 才映射关闭 thinking；官方适配器默认高强度，默认输出上限也较大，应在学习 profile 中显式设置。当前默认 catalog 已包含可看图的 `deepseek-flash`。[Messages 适配器](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm-deepseek/README.md)

**本项目建议：** 先用 `off` 建立速度基线，与 `low` 做同题比较再定默认；这不意味着数学效果已验证相同。单次用户提问正常情况下只产生一次模型请求；补字幕、资料时才增加执行 step，不预置规划、反思和多代理复核。

attachment-local 会规范化图片，模型适配器还可能生成请求版本并上传 Files API；这不能承诺与原裁剪图逐像素相同。应由应用保留用户选定的原始裁剪、来源时间点和哈希，把 Harness 的 attachment ID 作为执行映射。公式细节、缩放策略、重复上传和跨会话重用需要原型验证。[附件行为](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/attachment/attachment-local/README.md)

### 3.2 SDK 需要原型解决的两个限制

官方 SDK 文档明确：当前 wire 没有 mid-turn cancel；放弃一个 turn 的现成方式是关闭 runtime。SDK 的 `finalResponse` 是接收消息到下一次 idle 区间的最后一个根会话回复，不是严格按单条输入归因的结果。[SDK 限制](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/README.md#known-limitations-and-deferred-work)

**本项目建议：** 一条应用聊天的请求串行化，以应用 request ID 映射 Harness session、inbox message 和相关事件；自定义 Host 桥接插件实现所需的取消与事件归因，或验证受控关闭／恢复路径。不能仅转发 `finalResponse` 就视为完成聊天集成。该桥接是待写能力，不是假定存在的官方插件。

### 3.3 默认外发贡献需要裁剪

真实存在的 `@deepseek-ai/dsh-session-log-deepseek` 默认 `enabled:true`，会把 canonical Session 日志后缀作为 `dsh_session_log` 外发。该字段不进模型上下文、不会形成模型输入 token，但仍增加 HTTP 数据；与前端当前选择的模型上下文范围不是一回事。[日志贡献插件](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-log-deepseek/README.md)

另有 `@deepseek-ai/dsh-plugin-package-inventory-deepseek`，默认发送活动插件包清单。建议本项目 profile 对这两个贡献插件显式设 `enabled:false` 或不挂载贡献项，使模型请求外发内容符合本项目选定的范围；扩展注册基础库本身与两个数据贡献插件应区分。[包清单插件](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/plugin-package-inventory-deepseek/README.md)

## 4. 通用文件、shell、MCP 不等于学习插件

| 真实包／模型工具 | 实际能力、依赖及 UI | 首版处理 |
| --- | --- | --- |
| `@deepseek-ai/dsh-tool-fs`；`read`、`read_image`、`write`、`edit` | 无 UI 运行；硬注入 tools/fs/systemPrompt/workingDirectory，read_image 另需 attachment service 和图片模型。read 只读 UTF-8，不是 PDF／视频解析器 | 默认四项都关闭或不挂模型工具；提交图片通过 prompt attachment 完成，无须暴露任意路径 read_image |
| `@deepseek-ai/dsh-fs-local`／`@deepseek-ai/dsh-fs-sandbox` | Host 文件提供层；后者增加 sandbox 检查 | 服务可按启动依赖保留；不能把 sandbox 等同于“只读用户提交图片”的业务限制 |
| `@deepseek-ai/dsh-tool-fs-search`；`glob`、`grep` | ripgrep 文件发现／文本搜索；依赖 subprocess、tools 等 | 不作为课程 RAG；首版模型不暴露全目录搜索 |
| `@deepseek-ai/dsh-tool-pwsh`；或 `@deepseek-ai/dsh-tool-pwsh-persistent` | PowerShell 工具；分别依赖 shell／sandbox 或 persistent terminal 栈；可无 UI | 不向学习问答 agent 开放。固定程序调用 FFmpeg/ASR 属可信应用任务，不经模型任意 shell |
| `@deepseek-ai/dsh-mcp-client` | 连接外部 MCP server 并注册 `mcp__<server>__<tool>`；硬注入 tools，服务程序另行部署 | 首版只按需要接入自有上下文服务；MCP 本身不提供 ASR／RAG 数据能力 |
| `@deepseek-ai/dsh-mcp-resources` | 资源发现和读取；没有配置 server 时不贡献相应工具 | 可随 profile 存在，但不代表有课程资料索引 |

文件限制与图片 route 的官方依据：[tool-fs](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/fs/tool-fs/README.md)、[硬注入与开关源码](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/fs/tool-fs/src/index.ts)；MCP 的外部服务、注册和文本开销依据：[mcp-client](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/mcp/mcp-client/README.md)。

### 4.1 严格落实“不自动补图”

不能只在系统提示中写一句禁止。建议采用三层约束，均为本项目待实现／验证配置：

1. **不加载能力：** 学习 agent 的 profile 不注册 shell、通用 read_image、屏幕截图、视频抽帧或浏览器控制工具。用户主动截图由自有前端执行后提交，原视频不暴露为任意工具路径。
2. **按 agent 缩小工具面：** 使用 `agent.ctx.tools.restrict({allow:[...]})` 为继承工具建立白名单，仅允许字幕读取、资料检索等阶段需要的能力。官方 restrictions 不影响该 scope 自己注册的工具，也不直接约束保留的 PTC 传输，因此首版使用 native 模式并保持 scope 注册受控。
3. **执行时强制约束：** 用 `tools.guard()` 检查允许的工具名、业务上下文、附件 ID、课程／文件范围以及当前读取阶段。拒绝缺少用户提交依据的图片来源；工具内部把 ID 解析成路径，不接受模型任意路径。guard 能拒绝，后续 listener 不能强制放行。

这些接口真实存在，具体工具与 guard 由本项目编写。官方 registry schema 投影也有“allowlist”用语，但那是在过滤传给模型的定义字段，**不是学习工具权限白名单**，两者不可混淆。[工具接口](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/tools/README.md)、[restrict 与 guard 源码](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/tools/src/index.ts#L1031-L1081)

如后续确需允许重读用户已提交图片，也应新增 `submitted_attachment_id` 参数的专用包装工具，只返回本次／相关历史已经由用户提交的附件；不能直接开放 `read_image(file_path)`。这仍不允许从视频补取原图或相邻帧。

## 5. 已有 ASR、文档能力与本项目需求的差距

### 5.1 存在本地 ASR，但不等于长视频字幕方案已经具备

| 确切官方包 | 实际职责与依赖 | UI 耦合／首版建议 |
| --- | --- | --- |
| `@deepseek-ai/dsh-experimental-voice-input-bundle` | 组合语音 service、SenseVoice provider、Remote 和麦克风 UI，默认关闭 | 整个 bundle 含官方 UI；不直接照搬到定制学习界面 |
| `@deepseek-ai/dsh-experimental-speech-to-text-sensevoice` | SenseVoiceSmall ONNX＋Silero VAD；Host CPU only，Windows x64；依赖 speech service、subprocess、`sherpa-onnx-node 1.13.8` | provider 可作为无 UI 研究候选；速度／质量未测，不直接替代 GPU 长课转写 |
| `@deepseek-ai/dsh-experimental-api-speech-to-text` | 浏览器完整录音提交至 speech service；受大小时长限制，输入为 16 kHz 单声道 PCM16 WAV | 依赖 Typert/Web Remote；明确无文件上传、持久字幕历史和流式协议，不是课程处理 API |

上述事实来自 [SenseVoice provider](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/experimental/speech-to-text-sensevoice/README.md)、[provider package 依赖](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/experimental/speech-to-text-sensevoice/package.json)、[录音 API](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/experimental/api-speech-to-text/README.md)。

**本项目判断：** 先保留独立 Python GPU ASR 工作进程负责长视频分段、时间戳、当前片段优先、断点恢复；参考官方语音插件的准备状态、缓存校验、取消和队列设计。不能表述为“没有 ASR 插件”，也不能表述为“启用语音插件即完成三小时课程字幕”。官方 voice bundle 文档另指出，安装 `dsh` 即可能带入 sherpa-onnx 平台运行库，即使功能禁用也存在下载与磁盘成本；模型准备是另一件事。[Voice bundle](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/experimental/voice-input-bundle/README.md)

### 5.2 文档预览、转换与课程 RAG 分开

| 确切官方包 | 已有能力与依赖 | 复用边界 |
| --- | --- | --- |
| `@deepseek-ai/dsh-office-to-pdf` | Host 本地 DOC/DOCX/XLS/XLSX/PPT/PPTX 转 PDF；依赖独立 `@deepseek-ai/libreoffice-kit ^0.1.5` 及相应平台资产；直接 in-process 转换可不挂官方 UI | 可作为转换候选，已有缓存、取消和前台优先机制；不添加 OCR／RAG 或模型工具 |
| `@deepseek-ai/dsh-client-ui-sidebar-documentpreview` | 官方 sidebar 预览、PDF.js、文本层和 Office PDF；依赖官方 client slot、Remote、workspaceFiles | 有 UI 耦合；优先借鉴阅读器设计或抽取局部，不直接当自有阅读页面 |
| `@deepseek-ai/dsh-client-ui-attachment` | 官方附件栏、图片灯箱 | 有官方客户端耦合；不能当现成视频截图、裁剪、缩放和时间关联编辑器 |
| `@deepseek-ai/dsh-session-query-sqlite` | Session 历史 SQLite FTS5 查询 | 不等于课程文档、字幕和题目 RAG；不替代业务知识库 |

转换边界：[Office provider](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/document/office-to-pdf/README.md)；UI 依赖及图像缩放没有 drag-to-pan、扫描 PDF 没有可选择文本等限制：[预览组件](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/ui-sidebar-documentpreview/README.md)；附件栏能力：[附件 UI](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/ui-attachment/README.md)；会话检索范围：[session-query-sqlite](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session-query/session-query-sqlite/README.md)。

本次官方目录和相关源码未发现可直接覆盖完整“视频播放器＋用户截图编辑＋字幕纠错流水线＋课程资料 RAG”的学习插件。第三方是否存在未知，不把未核实市场内容作为已选依赖。

## 6. 本项目仍需开发的学习集成

以下是拟议内部模块／工具名称，不是官方现成 npm 插件：

| 待写能力 | 复用部分 | 项目自己负责 |
| --- | --- | --- |
| Harness 执行桥接 | SDK 或 Host 扩展、会话事件、agent lifecycle | 应用 request/session/attachment ID 映射、实时答案、取消、断流恢复、去重 |
| 学习上下文工具 | `dsh-tools` 注册与 guard，必要时 MCP bridge | `read_relevant_subtitles`、`search_course_materials` 的 schema、来源限制、按层读取和 token 预算 |
| 长视频任务插件／适配器 | Harness 插件服务入口，可参考录音 worker 生命周期 | Python GPU ASR、切段、时间戳、优先级、恢复、纠错与索引更新 |
| 学习资料模块 | 可选 Office conversion、成熟 PDF/OCR/检索组件 | 课程、题号、页码、字幕、图形、索引、自然语言定位、引用回看 |
| 截图聊天输入 | 可参考官方附件传递协议 | 内置播放器主动截图、缩放、拖动、裁剪、多图、补充文字与来源时间点 |
| 业务数据投影 | 监听 Harness 执行事件 | 把结果写到自有聊天和用量表；同步只处理业务 schema，不能复制内部 DB |

安卓后续采用相同业务协议与数据语义，并实现本端 DeepSeek 调用和所需本地上下文能力。电脑提供的 Harness plugin 不会自动变成安卓离线可用功能，也不需要把所有 Windows 执行会话复制到手机。

## 7. 版本、费用与最小验证

只依据 preview 标识、固定提交、源码 package 版本评估当前可核实状态，不据星数推断维护质量，不把 `0.2.1-rc.2` 声称为已验证稳定版。第一次集成通过后再固定同一套 Harness、Cordis、profile 与插件版本，避免混装内部 workspace 包。

官方 `llm-retry` 的默认 normal 策略是五次重试，always 策略可能无限；每次重试是新的模型请求。建议本项目显式收紧预算，尤其流式答案已出现后不能让 UI 自动重新提交，再叠 Harness 重试；应用幂等只能避免本地重复执行，不能保证服务方不对不确定请求计费。[重试行为](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm-retry/README.md)

**首轮原型需要证明：**

1. 定制 profile 无官方 UI 可启动，学习必需服务满足，模型工具清单没有 shell、任意 read_image、自动取帧或无关代理功能。
2. 用户提交的截图能经附件映射到 `deepseek-flash`，公式图形清晰；足够输入时只一次模型请求，不等待字幕。
3. 白名单和执行 guard 对未提交图片、错误范围及跳过上下文阶段的调用实际拒绝，不只依赖提示词。
4. 字幕、知识库按顺序补充；无资料时区分缺内容与未处理，关键图像缺失时请求用户补充。
5. 中途取消、连点发送、网页关闭、runtime 退出、恢复会话不会串回错误对话；保留实际执行请求和用量对应关系。
6. 当前题目截图／条件不会因 compaction 或 image offload 丢失；摘要、自动标题、重试等辅助调用可见且受控。
7. profile 外发内容与选定范围一致，session-log/package-inventory 默认贡献已按项目配置关闭。

以上均未执行。本轮只确立真实包清单、可复用接口和必须适配的边界，不宣称兼容、性能、数学效果或 token 收益已被验证。
