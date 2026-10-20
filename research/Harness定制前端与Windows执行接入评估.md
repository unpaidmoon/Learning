# Harness 定制前端与 Windows 执行接入评估

日期：2026-10-18。状态：基于官方文档及源码的接入建议，未安装或运行 Harness，未调用付费模型，未处理视频。

用户最新决定优先于首轮选型：Windows 采用 **DeepSeek Harness＋适用插件作为执行层主选**；保留定制 React 界面、Python 资料／学习业务服务和独立本地 ASR。安卓后续独立完成基础学习和直接调用 DeepSeek，再通过应用协议同步。学习业务使用自有 schema，不把 Harness 会话日志作为全设备数据协议。

本次源码核对固定在官方提交 `687f282e8625c6b8d11ff58211a90524be2e4317`；该提交中的若干 package 标记 `0.2.1-rc.2`。这些标记不等于已经选定并验证的安装版本，发布包、runtime 和插件仍须匹配检查。[官方固定提交](https://github.com/deepseek-ai/deepseek-harness/tree/687f282e8625c6b8d11ff58211a90524be2e4317)

## 1. 当前建议

**优先验证「官方 Harness Web Host 作为内部执行宿主，FastAPI 通过其 API Peer 接入」**。让官方 session-controller 继续负责现有会话执行、实时输出和停止；用户只打开本项目 React 界面。保留 Python SDK＋项目自有桥接作为备选，不先为了使用 TS SDK再建设一套 Node 业务后台。

推荐的职责结构：

```text
用户定制 React 界面
        │ 本项目 HTTP／SSE／文件接口
FastAPI 学习业务后台 ─── 应用 SQLite、原文件映射、截图与学习数据
        │ 内部 ExecutionAdapter
        ├── Harness Web Host（Node 执行运行时、选定插件、独立 DSH_HOME）
        └── 持久处理队列 → 独立 Python ASR worker → 本地 GPU
```

**Web Host 不要求展示官方 UI。** `dsh web --no-open` 可运行宿主而不打开浏览器；机器调用可通过 `/connect` 建立 API Peer，不需要人工进入官方页面登录。宿主仍可能携带官方前端静态资源，这不意味着本项目必须使用其页面。是否能在后续 profile 中进一步移除前端资源，与当前定制界面是否可用是两件事。[CLI Web profile](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/apps/cli/reference/README.md#web-profile)、[Connection 官方说明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/connection/README.md)

默认 Web composition 仍会加载其他 Host 服务和 frontend-static，不能因此声称已经获得最小宿主或预设其内存占用。[Web app 装配](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/bundle/web-app/src/index.ts#L181)

该主线仍须通过 P0：API Peer 鉴权、WebSocket mux、实时帧、取消和断线恢复能否被薄适配；不能仅凭接口存在就宣称已完成无界面集成。如果此适配比固定版本 SDK 桥接更重，再在 Harness 主选不变的前提下切换接入方式。

## 2. 三条接入路线的实际差别

| 路线 | 已有能力 | 当前缺口与开发代价 | 本项目定位 |
| --- | --- | --- | --- |
| Python SDK → 官方打包 runtime | Python 可直接启动、发送文字／图片、订阅持久通知、恢复会话、关闭 runtime；同版本 runtime wheel 内含 JS 执行闭包 | 当前 SDK 协议没有实时 assistant chunk 或单会话 mid-turn cancel；正式聊天还要补项目桥接或协议扩展 | 轻量 SDK 路线备选及基础烟测 |
| TS SDK sidecar → Node runtime | 同一 stdio SDK 协议，异步类型接口，使用当前 Node 启动同版本 dsh | 和 Python SDK 一样缺少实时 chunk／单会话取消；另加 Python↔TS 适配和进程管理，不能因此自动解决体验缺口 | 有不可替代 TS 客户端复用收益时再选 |
| 官方 Web Host RPC → FastAPI 内部适配 | `session/prompt`、`follow` 的实时 assistant stream、`cancel`、历史页面、重连基线、队列请求标识 | 要正确实现 Connection 鉴权与 Remote／mux 契约，保持与固定版本同步；默认 Web profile 也需要裁剪为学习用途 | 首个 P0 主线 |

Python SDK 是子进程客户端，不是把 Harness 全部改成 Python。它安装同版本 `deepseek-harness-runtime-bin`，Windows x64 有 `.exe`；通常运行不要求额外安装系统 Node。TS SDK 则由 `process.execPath` 启动 Node CLI 模块；其 `dshBin` 不是可随意替换的任意 native exe 路径。[Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk/README.md)、[runtime wheel](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk-runtime/README.md)、[TS 启动源码](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/src/launch.ts)

因此最新架构中的 Node／JS 已经是执行运行时，不再只是前端构建工具；但**不需要另建负责课程、数据库、文件和学习记录的 Node 业务层**。可先复用官方已打包运行时；插件管理／修改版构建是否需要 pnpm 和系统 Node，单独纳入开发环境，不与日常启动依赖混淆。

## 3. 官方 SDK 能力与限制

### 3.1 确切请求和通知

官方 stdio SDK 协议当前包含：

- 请求：`initialize`、`session/prompt`、`session/resume`、`session/wait`、`session/working-directory/get`、`session/working-directory/set`、`shutdown`。
- 通知：`session.event`、`session.status`、`subagent.started`、`subagent.finished`。

Python 高层提供 `DeepSeekHarness.start()`、`run()`、`start_session()`、`resume_session()` 和 `close()`；`Session.run(..., on_notification=...)` 可收集通知。低层 `HarnessClient` 提供 `session_prompt()`、`request()` 和通知订阅。[协议类型](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/protocol/src/types.ts)、[Python API 源码](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk/src/deepseek_harness/api.py)、[Python Client](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk/src/deepseek_harness/client.py)

`session/prompt` 返回 `{messageId}`，表示进入队列。高层 `run()` 收集从该消息进入 durable inbox 到下一次 root agent `idle` 的活动区间；`final_response`／`finalResponse` 是区间最后一个已提交 root 文本，不是严格与某个 prompt 一一归因的响应。首版在一个业务会话里串行提交前台提问，不让多个重试／steering 混在同一个活动区间后再猜测归属。[TS SDK 语义及限制](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/README.md)

### 3.2 on_notification 不等于实时 token 流

SDK server 转发 `session/event` 和 `agent/status`；实时输出的 `agent/assistant-stream` 是瞬态事件，不在当前 SDK 通知表中。持久 `assistant/message` 即使内含最终完整 stream，也不能替代生成时的 chunk。不能把 SDK 回调写成已经满足流式打字体验。[SDK server 实际订阅](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/server/src/server.ts#L103)、[Agent 瞬态流事件](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent/src/runtime-types.ts#L353)

SDK 也没有 `cancelPrompt` 或 `session/cancel` 方法；本地等待超时并不代表模型停止。关闭整个 runtime 可以终止其工作，但会影响其中其他会话，不能作为最终产品单会话「停止回答」的正常实现。[SDK 限制](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/README.md#known-limitations-and-deferred-work)

### 3.3 如果走 SDK，需要新增什么

可复用的官方接缝包括 `ctx.on('agent/assistant-stream', ...)`、`ctx.agents.get(sessionId)`、`Agent.cancel(cause, options)` 和导出的 `HarnessSdkJsonRpcServer`。当前 SDK transport 没有作为可注入公共 service 暴露给普通插件，所以不能声称「安装一个现成插件即可给 SDK 加流与取消」。[Agent registry](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent/src/index.ts#L568)、[Agent cancel](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent/src/runtime-types.ts#L176)、[SDK transport 装配](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/server/src/index.ts)

有两种项目新增方案：独立 Host 插件提供本地流／取消 endpoint；或替换 SDK server row，复用官方 server 类，并扩展项目自己的 RPC／notification。后者要维护固定版本的协议扩展、序列化、生命周期和客户端过滤；前者要解决 SDK 请求与旁路流的顺序和身份关联。Python 低层客户端可接收任意 method 的 notification，但高层会话订阅未必把新增通知识别为本会话，需单独验证。[Python 通知分发源码](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk/src/deepseek_harness/client.py#L403)

这些桥接都属于本项目待开发能力，不能计为上游已有插件的现成收益。

## 4. Web Host 主线：现成流式与取消

### 4.1 内部连接与认证

官方 Web Host 有 API caller 入口：`GET /connect?token=...&interactions=none`，返回 302、API Cookie 和 Mux 地址。FastAPI 保持自己的 Cookie jar，并在 HTTP 和一个 API WebSocket 上使用同一凭据。无需让浏览器接收 Harness 启动 token，也无需让用户打开官方 UI。[Connection 认证与 API Peer](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/connection/README.md#browser-authentication-and-request-trust)

需要遵守的具体约束：

- 不是 `Authorization: Bearer` 接口；query token 仅用于根页面交换或 `/connect`，不能把 token 随意加到每个 RPC。
- API Cookie 绑定 Host authority。FastAPI 直接访问固定 loopback 地址，自己的 React 通过业务 API 使用，不把外侧 Origin／Cookie 原样误转发给 Harness。
- 一个 API socket 拥有 Peer；socket 关闭后该 Peer 被移除，以后 HTTP 请求需重新 `/connect` 获取新 Peer。不能只有一组 HTTP 请求而永远不建立对应连接。
- `interactions=none` 适合不承担官方转发交互的内部客户端。若保留会等待用户回答的插件，必须另行接管相应交互或在首版 profile 中排除；不能让后台默默等官方 UI。

这些都是接入契约，不要求新增登录系统。启动 token、API Cookie 和模型 API key 是不同凭据，不应互相替代。

原始 WS 建连不自动发送官方 Connection `ready`。`interactions=none` 不能打开 `$events`，首版以 `follow` 的首个 snapshot 判定该会话观察流就绪。只有 `interactions=answer` 的 `$events` 流才返回 `ready`，之后还可能有需回应的转发交互，不能只消费 ready。[Gateway events 准入](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/gateway/src/index.ts#L508)

### 4.2 确切业务方法

Web Remote namespace 为 `session`，Unary 通常通过 `POST /api/session/<method>`，流通过 `/api/remote.mux` WebSocket。Connection 使用自己的 request／response envelope，**不是 stdio SDK JSON-RPC**。[Connection envelope](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/connection/src/rpc.ts#L67)

已核到的能力：

| Web Remote | 用途与边界 |
| --- | --- |
| `session/create` | 创建／复用会话，响应中的 `sessionId` 可能与请求候选值不同，应保存实际映射 |
| `session/prompt` | 接收 `requestId`、`sessionId`、`mode: queue/steer`、`content` 和可选时区，返回 accepted；requestId 用于同一发送的身份及去重 |
| `session/follow` | 使用 address 跟随 snapshot、durable event，设置 `assistantStream:true` 可获取实时 start／chunk／end 和重连基线 |
| `session/page` | 读取历史页，配合游标补齐已提交结果 |
| `session/cancel` | 对当前 agent 请求取消，返回 accepted；现有实现保留待处理 inbox，不等于清空全部待发送任务 |

方法和形状以源码为准，不虚构 `runVideoQuestion`、`uploadSelectedImage` 等官方方法。[Session 请求类型](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/session-controller/src/types.ts)、[Remote 方法实现](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/session-controller/src/index.ts#L455)

`session/follow` 通过现有 `agent/assistant-stream` 事件生成实时帧；`session/cancel` 最终执行 `agent.cancel({kind:'user'},{keepInbox:true})`。关闭 follow、断开 HTTP 或浏览器停止接收流都不等价于这个业务取消调用。[实时 follow](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/session-controller/src/history.ts#L165)、[取消实现](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/session-controller/src/commands.ts#L493)

### 4.3 已核实的窄传输形状

`POST /api/session/prompt` 的 JSON 不是把业务 request 直接放在顶层，Typert 命名参数还有 `args.request` 两层：

```json
{
  "type": "client-request",
  "rpcId": "transport-request-1",
  "method": "session/prompt",
  "payload": {
    "args": {
      "request": {
        "requestId": "app-submission-1",
        "sessionId": "runtime-session-id",
        "mode": "queue",
        "content": [{"type": "text", "text": "请解答这道题"}]
      }
    }
  }
}
```

成功响应为 `type:server-response`、对应 rpcId、`result:{ok:true,value:{accepted:true}}`。URL endpoint 与 envelope.method 必须一致。`session/create`／`session/cancel` 使用同样 envelope，替换 endpoint 和 `args.request`。[客户端 envelope](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/connection/src/client/rpc.ts#L38)、[命名参数组装](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/gateway/src/client/index.ts#L444)

同一个 API Peer 的 WebSocket 打开 follow：

```json
{
  "type": "open",
  "streamId": "follow-1",
  "endpoint": "session/follow",
  "payload": {
    "args": {
      "request": {
        "address": {"kind": "session", "sessionId": "runtime-session-id"},
        "assistantStream": true
      }
    }
  }
}
```

服务端返回 `{type:item,streamId,value}`、`error` 或 `end` 帧。value 先为 snapshot，之后为持久事件或 assistant-stream。客户端 `{type:cancel,streamId}` 只取消观察流；模型停止仍走业务 `session/cancel`。先建立 follow 并取得 snapshot，再发送 prompt，避免漏掉前部实时帧。当前 `SessionFollowRequest` 没有 `afterCursor`／`sinceSeq` 参数，重连通过新 snapshot／page 和事件序号校准，不能自行编造增量游标字段。[Mux 帧](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/gateway/src/stream-protocol.ts#L234)、[follow 请求类型](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/session-controller/src/types.ts#L504)

### 4.4 自定义界面与薄适配

React 不必引入官方页面组件。FastAPI 的 ExecutionAdapter 消费内部 Web RPC，转成应用自己的 SSE／状态事件，同时把最终内容及来源写入业务数据库。官方 `@deepseek-ai/dsh-client-connection/client` 也支持 non-page carrier，但不是必须在 React 中引入的 UI 框架；先评估直接实现所需窄协议的工作量，再决定是否复用该客户端及其依赖。[Connection 客户端能力](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/connection/README.md)

P0 必须做不显示官方 UI 的集成样例，覆盖宿主启动就绪、follow snapshot、响应 envelope、WS mux 的 open／item／end／cancel、断线后重新连接和流基线。若这一适配需要重做大量 Cordis／Typert 客户端，则与 SDK＋窄桥接重新比较；不能以「Web RPC」四个字掩盖协议实现成本。

官方前端 Connection／Gateway／Session client 可以作为参考或复用来源，但不是独立 React SDK；部分 browser exports 由 Harness loader 装载，完整 Session client 还依赖 Cordis、Typert 和 fileUpload／commands 等服务。不能假设在普通 Vite 应用中 import 一个包即可工作。React 只连接 FastAPI 的主线可以不引入这些前端依赖。[Connection 安装入口](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/connection/src/client/index.ts#L222)、[Session client 依赖](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/session-controller/src/client/index.ts#L97)

## 5. 最小应用侧接口

以下是**本项目拟定接口，不是 Harness 已有 API 名称**。它隔离 Web RPC／SDK 接入细节：

```ts
interface ExecutionAdapter {
  startTurn(input: {
    conversationId: string;
    appTurnId: string;
    text: string;
    selectedImages: SelectedImage[];
    contextRevision: string;
  }): Promise<{ executionId: string }>;
  observe(executionId: string): AsyncIterable<AppExecutionEvent>;
  cancel(executionId: string): Promise<void>;
  recover(conversationId: string): Promise<void>;
  close(): Promise<void>;
}
```

`AppExecutionEvent` 可以包含 accepted、text delta、tool state、final、usage、cancelled 和 error，但内部映射必须根据实际 runtime 事件，不把业务状态名当成现成官方通知。应用的截图、课程、题目和资料引用不会依赖某个 runtime 的私有事件对象。

`SelectedImage` 只指用户本次确认的裁剪结果；source asset 和时间点作为结构化来源信息保存。聊天区保留的原图用于用户重新编辑，不因为可取得原图就自动交给模型。

## 6. 用户选择图片的实际传递

图片由浏览器主动截图／裁剪后进入应用附件库，后端取用户实际选择区域的 raster bytes，按接入协议传递：

- SDK：`{type:'image', data:<base64>, mimeType:'image/png'}`，与 `{type:'text',text:...}` 混排。
- Web RPC：`{type:'image', data:<base64>, mediaType:'image/png', name?:...}`。字段为 `mediaType`，不能套用 SDK 的 `mimeType`。

runtime 校验并接纳图片后，把它转换为自己的 durable attachment reference，再由模型适配器按路由构造图片请求。[SDK 图片类型与 admission](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/server/src/server.ts#L43)、[Web 图片类型](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/api/session-controller/src/types.ts#L85)

因此「应用附件库＋Harness 附件库」可能存在必要的规范化派生副本，不能宣称跨两个存储绝对只存一次。可用应用附件 hash 和 runtime attachment ID 映射减少重复提交；原视频仍然原地引用，不被附带上传或复制。默认图片归一化／路由变换可能改变像素或编码，必须用公式截图检查小字、上下标和符号清晰度。[Harness 本地附件存储](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/attachment/attachment-local/README.md)

学习 profile 只提供允许的资料读取工具；不提供自动视频抽帧工具，也不允许模型用通用 shell 绕过用户的「只看我提交的图片」规则。需要字幕时取时间片段；仍不足再查知识库。信息充分的题目不增加先规划／先检索的额外模型调用。

## 7. 数据所有权、任务所有权和插件边界

### 7.1 业务 SQLite 与 runtime 日志隔离

| 内容 | 权威所有者 |
| --- | --- |
| 课程、原始文件映射、播放位置、截图编辑关系、聊天可见消息、学习记录、同步版本 | 应用业务 schema／SQLite |
| GPU 片段任务、初稿和修正版字幕、解析与索引状态、重启检查点 | 应用处理模块及持久任务表 |
| Agent 执行历史、tool calls、运行上下文、当前执行恢复 | Harness 自己的 DSH_HOME／Session persistence |
| 设备同步与安卓独立使用 | 应用协议和本端数据库，不同步 Harness 运行目录代替业务同步 |

应用维护 `conversationId → runtimeSessionId／runtimeGeneration／lastImportedSeq` 映射。runtime 事件只投影需要的业务结果；不把所有临时 chunk、job 输出和内部 log 放进知识库。Harness 的 JSONL 仍由其官方持久化模块写入，不由 Python 直接改其文件。[JSONL persistence](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-persistence-jsonl/README.md)

`resume_session` 恢复的是已有 runtime 历史，不会自动吸收安卓新增的业务消息。后续跨端回来时，需要通过应用上下文版本判断映射是否仍适用，按可移植上下文建立／恢复执行；不能假设复制聊天记录或调用 resume 就完成了多端同步。这属于应用后续接入边界，不阻塞 Windows 首版。

### 7.2 Harness jobs 不接管本地 GPU 持久调度

官方 `dsh-jobs-local` 的记录在内存中，工作跟随 Harness／所属 agent 生命周期；并发达到上限会拒绝，不提供持久排队或抢占。`dsh-tool-jobs` 提供 `job_output`、`job_list`、`job_kill` 等模型工具，但不等同于持久语音任务队列。[jobs-local](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/jobs/jobs-local/README.md)

ASR、字幕纠错、文档解析和索引的作业身份及检查点仍由业务任务表管理。学习工具向应用服务请求特定片段／资料，并关联应用 job ID，不在 Harness 中再次启动一个相同的 FFmpeg／ASR 进程。必要的临时 tool job 可以保留为当前执行观察层，但不能成为第二个 GPU 调度器或恢复依据。

取消一个聊天不会删除整课转写任务；关闭网页不关闭后台；暂停应用处理队列在当前小片段提交后生效。GPU 转写仍是一条受控执行通道，优先级只决定下一个片段，不承诺抢占当前推理。

### 7.3 避免双重 Agent 编排和重复调用

- Harness 负责一次 turn 内的模型请求、工具执行、必要继续和其受控重试；FastAPI 不再围绕每道题建设第二套规划 Agent。
- 应用负责输入选择、当前视频关系、已确认的信息优先级和任务调度。学习插件调用已有应用服务，不复制资料解析或检索实现。
- 同一个问题保留稳定 `appTurnId`／requestId。网络结果不明时先查询会话状态和持久结果，不自动改走直接 API 再问一次。
- 运行上下文压缩确定一个主负责人。可以由 Harness 的 compaction 承担会话摘要，应用只控制首次提供和按需取回的资料；不要两边各自发模型请求总结同一段历史。
- 关闭或调整不需要的自动标题模型调用、通用编程工具及 subagent 默认入口；用实测请求列表核对，不宣称默认 profile 就已经 token 最少。

## 8. 凭据委派与日志

模型 key 由应用后台的凭据模块管理，Windows 采用 OS 凭据存储／DPAPI 等方式的具体实现由主方案确定。若用户通过配置输入框录入，只进行一次本地提交；不写前端 localStorage、前端构建变量或聊天消息，不在状态接口返回原值。

第一轮可由后台读取 key 并只在启动 child runtime 的进程环境中注入 `DEEPSEEK_API_KEY`；不放在命令行 argv、profile YAML 或 prompt 中。Python SDK 已有 `api_key`／`env` 支持，官方 `deepseek-official` API-key 插件在无 managed credential 覆盖时读取 launch environment。全 `sdk`／`web` profile 的 credentials-local 是「已存文件 > launch env > project .env > home .env」，因此需使用应用专属 DSH_HOME 并避免旧 key 遮蔽应用配置；更新环境来源 key 通常需要重启 runtime。[Python SDK 配置实现](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk/src/deepseek_harness/api.py)、[API key provider](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm-deepseek-api-key/README.md)、[凭据优先级](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/credentials/credentials-local/README.md)

应用诊断只保存请求 ID、模型、时延、用量和脱敏错误，捕获 runtime stderr 时也要过滤凭据及启动 URL token。官方启动失败报告会保留原始插件错误，不能假设上游已经替应用脱敏。[CLI 失败诊断说明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/apps/cli/reference/README.md)

官方 profile 默认可能启用 `session-log-deepseek`，将额外 Session log 元数据随官方请求发送；学习应用应显式 `enabled:false`，使发送内容符合「仅所需文字和选中图片」边界。官方说明这项元数据不计入模型输入 token，因此关闭它的主要收益是数据范围和传输量控制，不能宣传成同额 token 节省。OTel 是另一条独立通路，学习 profile 也应明确配置关闭，而不是仅关闭前者。[Session-log contributor](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-log-deepseek/README.md)

## 9. Windows 进程生命周期

首版只保留三个主要角色：FastAPI 业务进程、Harness 执行宿主、Python ASR worker。这里的「Node runtime」可以来自打包闭包或固定 Node＋dsh 安装，须验证插件装载和 Windows 子进程行为后确定交付形式；不启动额外的 TS 业务服务。

启动顺序是业务数据库／任务恢复 → 固定版本 Harness 启动及 API Peer 握手 → 应用界面可用；ASR 模型按处理需要延迟加载。前台题目不依赖字幕时，ASR 未就绪不能阻止提问。

显式退出时，先停止接受新执行，持久化应用任务状态，向 Harness 请求其正式关闭／结束流程并等待有限时间，再回收进程；ASR 当前已提交片段保留。浏览器标签关闭只影响观察连接，后台继续运行和记录；重新打开界面先读取应用状态，再从 runtime 已提交记录补齐，不把临时断线当作重新发问。

SDK 备选路径的 `close()` 已实现先 `shutdown` 后关闭／终止的清理阶梯；Web Host 路径的 Windows 优雅退出、相关原生子进程回收和重启后 Agent 恢复尚未实测。不能把 Unix 信号描述当作 Windows 已验证保证。[Python close 源码](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk/src/deepseek_harness/client.py#L94)

## 10. P0 门槛与决定方式

首个 P0 优先走 Web Host 内部接入，保持无官方 UI 的真实流程；SDK 备选只验证必要对照，不同时开发两套完整生产适配器。

1. 固定发布包／源码版本，并核对 Windows 可启动、profile 和适用插件能载入。当前环境未发现 `dsh` 命令，本次不自动安装。
2. 仅用本地模拟模型／协议样例先验证连接、image admission、chunk、cancel、恢复和事件投影，再经用户已有 API 配置做真实题目测试；本次未发模型请求。
3. Web Host：通过 `/connect` 建立 API Peer，验证 HTTP／WS 凭据一致、精确 envelope、Mux 流、多次断线和 Peer 清理。不能用浏览器打开官方 UI 的成功替代这项检查。
4. 截图＋文字只附用户选区，检查公式清晰度；没有字幕时可直接解答，不读取或补取其他视频画面。
5. 第一段文本可在最终 `assistant/message` 前显示；使用 revision／attemptId／index 防止重试和重连造成重复追加，最终以 durable 提交结果校正。
6. 点击停止后执行官方 cancel，验证当前模型／工具链停止、没有清空其他课程处理任务；同会话下一条排队问题的行为与界面一致。
7. 当前应用会话只允许一个归属明确的前台 turn，确认 accepted／inbox／final 的对应关系；断线和超时不重复提交模型请求。
8. 浏览器关闭、Host 重启、ASR 重启分别验证。应用业务数据不依赖某个 runtime 进程存活；恢复失败保留既有消息和截图，不偷偷重问。
9. 检查实际模型请求，确认没有多余 planner、自动标题、双重摘要和额外日志上传；记录工具 schema、系统提示、图片变换、调用次数、token 和时延。
10. 同时播放视频和处理短音频样例，记录 CPU／内存／GPU 与前台体验。额外运行时的实际占用必须测量，不能因 Node 或 Python 的语言名称推断性能。

主线通过以上 P0 后固定 Web Host 适配；若 Connection／mux 薄适配不能成立，再验证 SDK＋自有 Host 桥接。两者都继续使用 Harness 执行和原有学习业务服务，不退回绕过 Harness 的双重模型调用。

## 11. 本次结论的限制

已经核实：官方当前 SDK／Web Remote 的具体接口和缺口、Python runtime 打包形式、Web API Peer／无 UI 连接路径、jobs 与持久 GPU 队列的区别、附件 admission 原理。尚未核实：本机安装后的发布包兼容性、无 UI API 调用实跑、实时和取消的最终交互、模型回答效果及系统性能。未安装、运行或修改第三方项目。
