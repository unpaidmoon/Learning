# Harness 界面复用、适配收益与开销验证计划

> 审查后执行入口（2026-10-20）：本篇保留历史A/B与复用核算；实际先[V1](/D:/Codex/Learning/docs/验证/V1-Harness最小路线执行方案.md)，SDK桥接和直连诊断仅在有明确问题/授权时对照，不并建两套完整执行层。首字/首增量只诊断，学习评价按[V3](/D:/Codex/Learning/docs/验证/V3-数学与试用价值执行方案.md)分有用内容、完整答、完整流程与返工。次数按所有实际出站内部/辅助attempt计，未知错误先对账；当前只准备文档，未开展新对照。

日期：2026-10-18（Asia/Shanghai）。状态：官方文档与源码只读研究、待执行的原型计划。未安装 Harness 或插件、未抽取视频画面、未上传视频、未发送付费模型请求，也没有运行下面的性能实验。

本轮新决定优先于旧选型记录：**Windows 采用 DeepSeek Harness 与插件作为执行层主选；安卓不运行完整 Harness，各端业务数据结构由本项目统一定义。** Windows 完全定制 React 界面，工具插件通过统一接口在 Windows 执行；UI 插件逐项评估宿主依赖与移植成本。安卓后续有网时可在电脑关机后独立调用 DeepSeek，先保存本地记录，再同步。Tailscale 仅负责连接。

首版主流程不变：用户提交截图与文字 → 必要时补充字幕 → 仍不足才检索知识库。禁止模型自动补取原图、相邻帧或其他视频画面。业务文件与原图保存在本机，不代表允许模型读取它们。

## 1. 源码基线与结论

官方仓库本次核对的 `master` 提交为 **`687f282e8625c6b8d11ff58211a90524be2e4317`**，提交时间 2026-10-17。下列链接尽可能固定到该提交，不能将以后 `master` 的变化当成本次已核实能力。[官方提交](https://github.com/deepseek-ai/deepseek-harness/commit/687f282e8625c6b8d11ff58211a90524be2e4317)

建议先验证三个不同问题，不混在一次演示里：

1. **执行层能否复用**：模型路由、会话运行、工具调用、日志恢复是否减少本项目重复开发。
2. **自有界面能否正确接入**：真实流式回答、停止生成、事件与消息的关联是否成立。
3. **某个 UI 插件是否值得适配**：它需要多少官方客户端服务、样式和事件投影，适配后是否仍保持本项目的数据和操作流程。

官方 SDK 能启动执行层，不等于它同时提供完整 Web UI、实时 token 通道和任意界面插件宿主。Windows Harness 学习 profile 是主选抽象，具体入口先比较 WebHost 内部机器 Peer 与 SDK＋桥接。**P0 优先验证无需显示官方 UI 的 WebHost follow/cancel 接入，再扩大插件复用范围。** Harness 执行层主选不随这两个入口的比较而改变。

## 2. 已核实的界面扩展机制

### 2.1 Slots 是宿主协议，不是独立 React 组件库

官方客户端本身是浏览器端 Cordis 插件应用，数据经过 Host → Remote → Client model → UI adapter → Conversation → Slots → React。插件依赖注入、会话作用域、React props 与远端回调都由这套宿主提供。[Web Client 架构](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/subsystems/web-client.md)

客户端插件在 `package.json` 声明 `dsh.client.platform/inject` 并提供 `./client` 入口，由 Host 生成启动图并激活，不是执行层插件加载后自然出现在任意 React 页面。以 `ui-chat.apply()` 为例，实际注入 `slots/sessions/uiWorkspace/uiSession/uiConversation/locale/configForms/remote/remote.session/sidebarRight`；仅运行 SDK profile 不会提供这套浏览器宿主。[ui-chat 声明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/ui-chat/package.json)、[ui-chat 服务依赖](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/ui-chat/src/client/apply.ts)

实际注册入口是 `ctx.slots.register()`，常用 `ctx.slots.inject(slot, callback)` 等待某个父槽存在。`root` 是唯一内建槽，父组件声明并渲染其 child 槽；不能脱离原父槽生命周期，孤立启用下游插件。组件得到 `PropsRuntime`、子槽渲染函数、store、注入回调与 locale 等派生 props，通常不直接接收 `ctx`。[Slots 协议](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/subsystems/slots.md)

核对到的相关位置：

| 功能 | 官方位置 | 对自有界面的含义 |
| --- | --- | --- |
| 聊天主视图 | `main[key=conversation]`、`main.conversation`、`conversation.view[id=chat]` | 替换整体布局需要保留或替换这些槽的父声明 |
| 消息节点 | `conversation.chat.node[key=...]` | 用户、回答、工具调用等按节点种类分发 |
| 工具结果细节 | `tool.call.toolview[key=toolName]` | 适合定向复用某个工具展示，但依赖工具调用数据和宿主传参 |
| 输入与附件 | `conversation.composer.bar`、`conversation.input.attachments/left/right/overlay/model` | 是输入扩展位置，不自带本项目的视频截图和非破坏式裁剪 |
| 会话绑定 | `useSession`、`useConversation`、`useInput`、`inputActions` | 由 scope adapter 和 Conversation 提供，不能仅导入 UI 包就出现 |

以上名称来自官方实际槽层级，验证时仍要检查固定版本的当前注册、scope 和 props，不能只依靠名称相似。[槽层级](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/subsystems/slots.md#conversation-hierarchy)

### 2.2 新消息卡片还需要事件定义

新增一种题目卡片，并非只注册 React renderer。官方机制先注册 `ConversationNodeDefinition`，通过 `match/start/update/buildViewNode` 把确定事件折叠为目标节点，再注册 keyed renderer。`uiConversation.events` 与 `uiConversation.views` 是分开的注册表；真实流增量与持久结算事件需要能重建同一结果。[Conversation 定义及示例](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/subsystems/conversation.md#definition-and-typed-chat-payload)、[Conversation 包](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/ui-conversation/README.md)

设计建议：本项目先定义自己的题目、附件、引用、请求和消息 schema；Windows adapter 将 Harness 事件转换为这些结构，或把同一业务事件投影给官方节点。不要为复用一张卡片，把 Harness 内部 Context/Node 数据结构直接变成安卓同步协议。

### 2.3 React 版本与共享模块

核对到官方 UI 包中 React `^18.2.0` 出现在开发依赖，不能据此断言 React 19 不兼容。实际集成还依赖客户端对 React、React DOM、JSX runtime 等平台模块的共享注入。需要保证 hooks 与 renderer 使用同一套 React 实例，核对实际包入口、CSS、编译产物和类型，不把宽泛 peer 或旧开发依赖当作交互验收。[ui-chat 包](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/ui-chat/package.json)、[客户端装载机制](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/subsystems/client-modules.md)

具体共享入口为 `client/web/src/platform.ts` 的 `PLATFORM_MODULES`。保留原客户端宿主时，React 19 是该宿主与全部实际插件的联合升级验证；独立 React 19 UI 只移植局部展示时，则验证移植代码，不必虚构原宿主已支持该组合。[平台模块源码](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/web/src/platform.ts)

## 3. UI 复用的具体分级

| 类型 | 可以争取复用的内容 | 仍需适配或重写 | 安卓边界 |
| --- | --- | --- | --- |
| 纯展示与通用算法 | 与宿主无关的 Markdown、图像布局、格式化、局部样式 | 注入自有数据和动作、统一样式、核对静态依赖与许可证 | 浏览器安全且无 Node/宿主依赖的部分才可复用 |
| 消息、工具卡片 | 视觉结构、局部交互、独立展示逻辑 | 事件到本项目消息的映射、引用与附件读取、命令回调、重连与取消状态 | UI 可迁移，工具调用改走统一接口；离线时不能假装 Windows 工具可用 |
| 输入区、会话导航 | 某些控件和快捷键设计 | 原组件可能依赖 `sessions/uiSession/uiConversation/remote/workspace`，需要宿主桥或重写控制层 | 改接安卓本地会话、资料、相机和模型通道 |
| 插件设置、工具权限、运行轨迹 | Windows 插件元数据与具体配置展示 | 宿主注入、服务端设置、权限与运行事实、事件投影 | 不承诺直接迁移，默认只保留 Windows 所需能力 |
| 视频与截图主流程 | 可参考操作设计和底层通用组件 | Range 播放、视频 ID 与时间、用户选区、草稿附件、只发送确认图片 | 后续单独接安卓文件 URI 与本地媒体能力 |

这些是适配分级，不是声称每一类已有可直接使用的具体插件。最终插件清单需要逐个填写：入口、注入服务、槽、会话事件、静态依赖、Host 方法、平台 API、许可证、预计修改范围及验收案例。

Windows 有两种可验证的集成分支：

- **有限宿主适配＋选择性移植**：保留完全自有 React shell，复用独立展示代码；对选中的消息/工具插件提供很小的适配接口。默认不实现全部官方 UI 宿主 API。
- **保留官方客户端宿主，替换 root/布局**：可让更多 UI 插件沿原槽和 Conversation 工作，但要承担 Client models、Remote、加载器、单实例 React 与插件样式等完整依赖。需要证明复用收益超过新增宿主维护量。

建议先验证第一条，挑一个最有价值的工具结果卡片实做迁移样本；如果实际目标插件高度依赖官方客户端，才以第二条对照。完全定制界面与保留部分宿主机制不矛盾，但不能把“可替换 root”说成“所有旧插件无改动兼容”。

这两个界面复用分支与执行层连接方式分开决定：自有 React UI 可以通过内部 Peer 接入 WebHost，不需要显示官方页面，也不因此自动具备官方浏览器 UI 插件的 Slots/Conversation 宿主。

## 4. 接入分支与 SDK 的实际限制

主方案补充的 WebHost 机器接入路径为 `GET /connect?token=...&interactions=none`、Cookie 和 `remote.mux`。原型优先评估以内部 Peer 访问现有 follow/cancel/control 的适配方式，保持前台完全自有；这属于对固定版本内部协议的适配，不能宣称是稳定的公开 REST API。认证、Cookie 生命周期、mux、流基线与取消需要一起验证，不把 token 查询参数记录到普通日志。相关 Host、Remote 和 Session 能力以官方接口及主技术方案的逐项核查为准。[API Gateway](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/api-gateway.md)

事实：SDK 的 `DeepSeekHarness/HarnessClient` 会启动独立运行时，通过 stdio JSON-RPC 驱动会话。`session/prompt` 返回入队消息 ID；高层 `run()` 收集到下一次 idle 的最后一条根回答，不保证在并发注入或 steering 下与某次提问形成一对一因果结果。[SDK 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/README.md)

事实：当前 SDK 协议只有 `session.event`、`session.status` 和子代理开始/结束四类通知，没有逐 token assistant 流，也没有 prompt-cancel 或单 session-close 方法。服务端实际监听 `session/event` 转发；`assistant/message` 的结算到达与真实流式首字不是一回事。[协议方法](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/protocol/README.md)、[服务端监听代码](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/server/src/server.ts)

事实：Web Session Controller 的 follow 路径具有当前 assistant 流基线、增量帧与重连结算配对能力；这部分能力不能从 SDK 的普通通知推断得到。[Session Controller](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/session-controller/README.md)

`ctx.remote.$on()` 也不是任意 Host 事件的浏览器总线；其转发采用明确 allowlist，不能把 Host 内部监听 `session/event` 或 `agent/assistant-stream` 的代码原样写成浏览器 SDK 调用。自有 bridge 必须明确新增通道及生命周期。[Remote 事件 allowlist](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/remotes/src/remote-events.ts)

因此必须把这些开发量列入收益账：

1. 优先适配 WebHost 内部 Peer 及现有 follow/cancel/control；SDK 分支若使用现有 stdio 协议，则另补真实文本增量与目标会话取消。不能把 SDK 的限制外推为整个 Harness/WebHost 的限制。
2. 绑定项目 `requestId/messageId/sessionId` 与 Harness 会话、turn/step 和结算 ID，避免重复、串题或使用“最新未完成消息”猜归属。
3. 把瞬态流和最终消息分开，重连时去重并恢复当前状态；不能等到结束后重放 timed stream 冒充实时回答。
4. “停止生成”只影响目标会话，不以关闭整个 Harness 运行时作为普通交互动作；不能连带停止其他会话或字幕处理。
5. 本项目业务记录为可导出、可同步的规范数据；Harness 执行日志作为运行事实和恢复依据，通过明确映射归入业务，避免双向任意改写两份聊天真相源。

官方 JSON-RPC 帧本身不会进入模型上下文，但 profile 中的 prompt、工具 schema、标题生成、压缩及重试可能改变调用与用量。运行时成本与模型 token 成本要分开测。[SDK serving 的模型影响](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/server/README.md#model-experience)

## 5. 长视频仍使用项目自己的媒体接口

官方 `GET|HEAD /api/file` 是附件/文件引用接口：默认受约 20 MiB 限额约束，完整返回并忽略 Range，不能直接承担当前约 510 MB、3 小时视频的按需播放。[Session media references](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/session-controller/README.md#session-media-references)

保留本项目 `/media/{assetId}` Range 路径和播放器；截图只在用户操作时采集。确认后的选区小图通过附件 adapter 交给 Harness。不能为接入旧 UI 的图片工具而暴露任意原视频抽帧、通用 `read_image` 或整个源目录给模型。

## 6. 复用收益账

| 能力 | 可能省去的重复开发 | 新增适配 | 仍需本项目开发 |
| --- | --- | --- | --- |
| 模型执行与工具循环 | 模型路由、工具登记、受控执行、事件生命周期的部分基础设施 | 学习 preset、参数映射、工具白名单、业务结果转换 | 截图优先策略、字幕到 KB 的阶段约束、课程语义 |
| 会话运行与恢复 | Harness 的会话日志、运行中状态、部分恢复框架 | 项目消息与执行日志身份、版本及重放映射 | 可迁移业务 schema、附件来源、安卓独立会话与同步 |
| 插件体系 | 可复用 Windows 工具插件及其扩展点 | Host 依赖、接口、输出上限、进度与取消、版本隔离 | ASR 调度、长任务持久恢复、文件管理和资源让行 |
| 界面插件 | 部分卡片、控件、轨迹显示与局部交互 | Slots、Conversation、Remote 或纯展示代码移植 | 完全定制布局、视频截图裁剪、课程资料、移动端交互 |
| 日志与用量 | 已有运行事实和模型使用信息 | 统一指标、实际请求抓取、缓存命中归因 | 项目的费用视图、失败重试策略、低打扰错误体验 |

不能把「省去部分基础设施」换算成未经测量的工期或 token 百分比；也不能因为使用插件就认为 ASR、索引、同步、截图和问题路由已经存在。

## 7. 受控 A/B 的实验结构

### 7.1 对照对象

| 组 | 内容 | 用途 |
| --- | --- | --- |
| A：直连诊断基线 | 原模型 SDK 直连 DeepSeek，使用相同学习 prompt、截图、工具和业务存储 | 仅测执行与协议增量成本，不作为主选或默认回退 |
| B：主选抽象 | Windows Harness 学习 profile＋项目 preset/patch＋所需插件白名单 | 执行层固定主选，接入方式在下面两个分支比较 |
| B-host：优先 P0 | 学习 profile 的 WebHost＋内部机器 Peer，复用现有 follow/cancel/control | 在完全自有 React UI 中验证真实增量、取消与重连；不要求显示官方 UI |
| B-sdk：对照 | 学习 profile 的 SDK 接口＋学习插件桥接 | 测新增流式/取消桥接与维护成本；不将官方 `sdk` profile 固定为最终主选 |
| B-min：可选裁剪对照 | `sdk-minimal` 加明确 patch，达到相同输入和可用工具 | 仅判断更小执行树是否值得，不能因名字 minimal 就默认取代 B-host 或最终学习 profile |

对 B-host/B-sdk，工具白名单、模型、thinking、图片、完整上下文及工具结果必须相同；若承载入口所需内部服务不同，将其列为资源与维护差异，不能借机改变学习行为后再比较“效率”。profile/preset 的最终组合在这些验证后固定。

官方 `sdk-minimal` 不叠加 base，但仍包含软件工程 persona、图片文件工具、工作目录和 Windows PowerShell 等组件。不要仅凭 minimal 名称就认为它没有额外工具和上下文；实验前核对展开配置和实际出站请求。[minimal 配置](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/bundle/sdk-minimal/cordis.patch.yml)

学习会话中只开放当前阶段需要的字幕与 KB 工具。保留运行时服务和对模型开放工具是不同操作：附件服务可以存在，通用文件/终端工具不因此进入学习问题的 tools。被 profile 依赖的内部服务能否裁剪，需要实际展开和激活验证。

### 7.2 两层实验，避免混淆模型波动

**第一层：本地确定性 provider/传输回放。** 同一组模拟流、工具调用、错误、断流和 usage；记录请求内容并比较两条路径。输出可以逐字相同，用于精确校验事件、取消、去重和本地桥接开销，不连接付费端点，也不抽视频帧。

**第二层：真实 DeepSeek 配对测试。** 原型完成、凭据配置后，使用用户主动提供并确认的截图。固定模型、thinking、输出上限、temperature、工具描述、工具顺序、返回结果、上下文版本、图片字节与尺寸。真实模型输出不保证字节相同；预先规定同一题目条件、答案和解释要求，再比较完成情况、调用与用量。这个阶段尚未执行，不能用第一层回放的结果代替模型数学效果。

在真实测试前，必须抓到 A、B-host、B-sdk 最终出站请求的语义差异；只给 SDK 或 Host 相同输入参数不够。运行时自动注入的身份、目录、时间、插件说明和附加 schema 都是实际 token 来源。若无法等价，先记录差异，再拆分「输入变化」与「执行层」影响。

### 7.3 固定用例

| 用例 | 模拟输入/预期路径 | 核对重点 |
| --- | --- | --- |
| C0 | 完整题目截图＋文字，直接回答 | 一次主解题模型请求；无字幕、KB、OCR、标题或复核的隐藏调用 |
| C1 | 问题明确需要当前讲解，补一次字幕后回答 | 相同字幕工具参数、结果、下一轮上下文；没有自动补图 |
| C2 | 字幕不足再查 KB | 阶段顺序、结果大小、引用与最大自动轮次 |
| C3 | 连续追问＋较长旧历史 | 当前题目确认图保留，旧内容压缩阈值和摘要成本单独记录 |
| C4 | 生成中取消、刷新、网络中断、重复点击 | 请求身份一致、停止只作用目标会话、历史不重复、不无声重发计费 |
| C5 | 两个会话并行，另有后台纠错 | 流和最终答案归属，不把下一次 idle 的最后回答错误关联给另一问题 |
| C6 | 关闭网页再打开，重启后台 | 本地消息和执行结算恢复、未结算状态可解释、后台任务不依赖网页 |

这组用例不要求首版加入多代理、浏览器工具或自动扫描视频。C0/C1/C2 的具体图文与字幕 fixture 在用户主动截取后登记，不在本次研究中产生。

### 7.4 资源与时间测量

统一记录 `buildId/HarnessCommit/profileHash/pluginVersions/promptHash/contextHash/imageHash/toolsetHash/device/powerMode/model/thinking/runId`。不在测量日志保存密钥。

| 指标 | 测量方法与注意点 |
| --- | --- |
| 冷/暖启动 | 区分电脑重启后的首次、进程重启后的首次、已有常驻运行时；从启动命令到协议 ready、学习插件 ready、UI 可操作分别计时 |
| 进程与内存 | 记录整个进程树、每进程 RSS/Working Set 和 Private Bytes；汇总时说明共享页可能重复计算，不能只看父 Python 进程 |
| GPU/CPU/磁盘 | 记录 GPU 显存、利用率、CPU、磁盘读写与队列；纯模型网络请求不应无故加载本地大模型 |
| 首字与总时 | 分开记录 provider 首个内容增量、Host 收到、UI 首次可见回答、最终答案、工具与整轮完成；thinking 增量不冒充答案首字 |
| 调用与 usage | 记录全部模型请求、工具轮次、自动摘要/标题/重试，以及输入输出/推理 token、缓存命中/未命中；缺失为未知，不记零 |
| 吞吐 | 记录同一时段输出 token/s、模型请求吞吐与可用前台交互；只有业务需要时再测小并发，不以批量压测牺牲前台 |
| 前台影响 | 视频掉帧、seek、截图到附件可编辑、输入响应、公式渲染长任务；回答流不触发播放器重挂载 |

先用本地回放多次确认稳定性，再用小量真实配对样本建立初步基线。交替 A/B 与 B/A，记录网络和缓存状态。不要用缓存后的 B 对比冷缓存 A；不能保证缓存隔离时，明确分层展示。样本较少时报告每次值、中位数和范围，不包装成可靠的高分位结论。

## 8. 优先级与 pass/rework 门槛

以下是建议的工程门槛，不是已经通过的结果，也不擅自替用户承诺秒数、内存上限或准确率。

### P0：先确认接得正确

**Pass：** 自有 UI 真正实时显示回答；停止只取消目标；消息与执行事件可精确关联；只发送用户确认图；出站 prompt/tools/thinking 可检查；字幕未生成时 C0 正常执行；无无关模型调用。

**Rework：** 只能等结算后重放、取消必须杀整运行时、靠 idle 最后一条回答猜归属、默认带终端/任意读图工具、默认读取字幕/KB、背景隐式调用模型。先调整 Host Peer 适配或 SDK bridge、学习 preset 与事件映射，不扩大插件数量，也不以此默认回退为直连执行层。

### P1：最小运行与单个 UI 插件适配

**Pass：** 固定版本可启动与退出，冷/暖路径和整个进程成本已测；一个代表性卡片在完全定制界面、重连和历史重放中正确工作；没有第二套可写业务 schema；通过 React 实例、样式和 mount/unmount 检查。

**Rework：** 为一张卡片被迫接入大部分官方 shell，修改散落到业务界面，或需模拟大量宿主服务。比较选择性移植与保留官方客户端宿主分支，明确改造文件和长期跟进范围，不用插件数量代表复用收益。

### P2：前台与后台同时运行

在后台空闲、转写运行、纠错运行、转写＋纠错四种条件下重复同一视频播放、截图、输入和问答操作。转写片段、模型和并发固定；每组记录后台完成量，不能靠让 B 的后台完全不工作得到漂亮数据。

**Pass：** 截图、输入、播放不等待整课任务，当前问答能让低优先级工作让行；已有结果复用、队列和重启恢复成立；资源无持续失控增长。

**Rework：** 播放/截图明显被后台阻塞，前台需等待模型加载、整课字幕或索引；退出网页导致重任务丢失；添加 Harness 后调用增长无法归因。先减并发、缩短片段、调整调度和插件树，再复测相同用例。

性能预算在 A 的真实基线形成后，按用户可感知等待与整机余量填写。若 Harness 有可解释、可接受的固定常驻成本且减少维护，实现仍可通过；若存在可复现前台阻塞或无必要的模型开销，则必须先返工，不能以“以后优化”通过首版验收。

### P3：以后再扩到安卓与更多插件

Windows 核心闭环通过后，才验证安卓独立本地会话、电脑关机时直接 DeepSeek、离线资料、后续同步和 UI 组件移植。工具插件留在 Windows，前端统一展示可用性；Tailscale 只提供在线连接，不能代替同步或关机后的服务。

## 9. 原型阶段的可审查产物

- 固定版本、展开 profile/preset 和插件白名单，附最小启动与退出方法。
- 自有 UI ↔ 学习 API ↔ Harness 的流式、取消、附件和请求映射契约。
- 每个被采用 UI 插件的依赖/改造清单及回放样例。
- A/B 请求差异报告、各指标原始数据、用量缺失说明与异常案例。
- 视频截图主流程、背景干扰、关闭网页和重启恢复的实际验收记录。

本次只形成上述计划与源码依据，不把它们写成已开发、已安装或已验证的功能。
