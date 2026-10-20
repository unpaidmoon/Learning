# 跨端业务数据与 Harness 上下文接续设计

日期：2026-10-18（Asia/Shanghai）。

本文对应新的架构决定：**Windows 以 DeepSeek Harness 为执行层主选，并开发学习插件；安卓后续可独立调用 DeepSeek，电脑关机时继续学习，业务数据恢复连接后同步。** 这项决定取代先前报告中“不依赖 Harness 运行时”的建议，但保留业务数据自有、原文件原地引用和学习效率等既定原则。旧研究作为历史依据保留。

核查范围：只读官方文档及源码，固定提交 `687f282e8625c6b8d11ff58211a90524be2e4317`。未安装依赖、运行 SDK、发模型请求、上传资料或抽取视频画面。以下字段、状态机、插件名称和桥接接口是**本项目的设计建议，尚未实现**，不能当作 Harness 的现成功能。

## 1. 核心决定

1. **应用聊天与 Harness 执行会话分开。** 一个用户可见 `chat_id` 可以先后绑定多个 Harness 执行会话；后台换执行会话不要求用户新建聊天。
2. **同步只更新业务事实，不触发旧问题重跑。** 安卓已有的用户问题、模型回答、图片和学习记录到达 Windows 后，只做幂等保存和上下文失效判断，不调用 `run()`。
3. **跨端历史改变后，主选“下次提问时建立新的执行快照”。** 不把安卓普通聊天 JSON 强行转换为 Harness 原生 assistant/tool 执行日志。
4. **没有跨端分歧的 Windows 连续追问，复用原执行会话。** 只发送新的问题，不每轮全量注入历史。
5. **每个聊天的 Windows 执行串行化，并使用执行代次和 fencing。** 防止多个 prompt 混在一个 idle 区间、旧流覆盖新回答以及断线重试造成重复执行。

“自动采用较新修改”用于可变元数据或同一字段的修订选择；新增聊天消息、不同设备的回答和并发分支按 ID 合并，不能整条聊天 LWW 覆盖。

## 2. 官方实际支持哪些能力

### 2.1 SDK wire 与客户端

固定提交的 SDK wire 包含 `initialize`、`session/prompt`、`session/resume`、`session/wait`、工作目录 get/set 和 `shutdown` 七种请求；**没有任意历史 import、seed、fork、inject 或单会话 cancel 方法**。`session/prompt` 只接受 `sessionId` 与用户内容块，返回服务端生成的用户 `messageId`。[SDK 协议类型源码](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/protocol/src/types.ts)

`resume_session(saved_session_id)` 恢复的是指定 Harness home 内原有的持久化会话；不发送新输入，也不导入应用/安卓端的新消息。缺失、不兼容、被占用等情况失败，不自动创建替代会话。[Python SDK 说明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/sdk/README.md)

高层 `run()` 收集从 inbox receipt 到下一次整体 idle 的活动，返回该区间最后一条 root assistant 文本；不是天然归属于某一个 prompt 的独占结果。低层 `prompt()` 返回的是入队回执，不能直接作为回答 ID。[SDK run 实现](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/client/src/api.ts)

### 2.2 同进程插件可使用的能力

| 官方接口 | 语义 | 对本项目的限制 |
| --- | --- | --- |
| `ctx.agents.create({sessionId, seed?, setup?, ...})` | 创建 agent，支持原生 `SessionEvent[]` 初始记录和创建前配置 | `seed` 是原生事件，不是普通 Chat Completions messages 数组；SDK wire 不暴露此字段 |
| `ctx.agents.resume({resumeSessionId, setup?, ...})` | 从持久化恢复原生历史并配置新 agent scope | 不合并安卓业务历史；也不能用 setup 驱动未完成创建的 agent |
| `agent.inject(UserMessage)` | 排入下一步上下文，**不唤醒** agent | 仍是 user-role 上下文；不是插入原生 assistant 回答，可能被取消/销毁丢弃 |
| `agent.followup(UserMessage)` | 新普通问题入队并唤醒 | 用于真正的新提问，不能用来“导入”已完成旧问答 |
| `agent/created` | 串行等待监听器，监听完成前保持 queued input | 可以在新执行会话的第一步之前准备历史引用；失败应阻止带空历史执行 |
| `Session.append(type,data,{surfaceOp})` | 提交符合当前格式的事件，不自行调用模型 | assistant/tool 有严格事件结构，不能伪造执行来源 |
| `ctx.sessions.fork(source,boundary?,childId?)` | 从 live 原生会话复制稳定事件前缀 | 不是从应用聊天 DAG 任意创建原生工具历史 |
| `SessionMessageProjection.project(event,context)` | 对既有 surface 消息投影内容，保留原身份 | 不能新增任意消息节点，不作为聊天历史导入器 |

`create` 的 seed/setup 见[Agent 创建类型](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent/src/index.ts)；inject、created、pre-step 的确切时序见[Agent runtime types](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent/src/runtime-types.ts)；append/fork 见[Session 接口说明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/session/README.md)。

本文的应用 `ContextProjection` 是业务层选取上下文，不是 Harness 的 `SessionMessageProjection`；后者的投影不增加原生消息身份。[Surface 投影契约](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/session/src/surface.ts)

单独 `ctx.sessions.create()` 只创建内存 Session，不能认定已有 durable writer。接续沿 SDK/Agent 生命周期创建，并明确挂载持久化；插件不要裸创建 session 后再自行写 JSONL。[SessionStore 创建实现](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/session/src/index.ts)

### 2.3 不能把普通聊天包装成虚构执行日志

当前 `assistant/message` 事件要求 turn、step、完整 AssistantMessage 和准确的紧凑 provider stream；工具结果也与实际 call ID、请求头和声明有关。`seed` 存在不意味着任意外部聊天都能无损导入。[Session 事件类型](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/session/src/types.ts)

本项目不为安卓的已有答案捏造 Harness turn、provider stream、usage、tool-call 或 tool-result。安卓只保存可移植业务问答和必要来源事实，不承担生成 Harness 内部事件格式的义务。

插件若扩展消息来源，可以使用 producer-owned `MessageSourceMap` 和 `form:'recall'` 表示历史引用；模型不会自动得到引用边框，必须在内容里明确标注“历史问答，已经完成，仅供本次追问参考”。[消息与来源类型](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm/src/message.ts)

## 3. 应用业务身份与执行身份

以下是应用拥有的结构，不是 Harness schema。

### 3.1 业务层

| 实体 | 关键字段 | 用途 |
| --- | --- | --- |
| Chat | `chat_id`, `title_revision`, `course_hint` | 跨设备稳定的用户可见聊天 |
| Message | `message_id`, `chat_id`, `role`, `device_id`, `parent_ids`, `in_reply_to`, `generation_id` | 追加式消息事实与因果关系；assistant 指向实际所回答的用户消息 |
| MessageRevision | `message_id`, `revision_id`, `supersedes`, `content_hash`, `content_blocks` | 用户编辑或修正的版本链；旧消息正文不静默消失 |
| Submission | `submission_id`, `user_message_id`, `owner_device_id`, `dispatch_revision`, `state` | 明确由哪个设备执行哪一次请求，避免同步即重发 |
| Generation | `generation_id`, `submission_id`, `attempt_no`, `executor`, `provider/model`, `status`, `usage` | 一次实际生成尝试，区分已回答、部分回复、待发送、失败与结果未知 |
| Attachment | `attachment_id`, `content_hash`, `media_type`, `byte_length`, `local_availability` | 用户提交的实际图片字节与各端可用状态 |
| SourceLocator | `asset_id`, `source_version`, `video_time/page/bbox` | 回看视频或文档的逻辑定位，不等于已取得图片/文件字节 |

`parent_ids` 记录发送时已观察的上一条/多条消息；`in_reply_to` 表示明确回答关系。每设备同步游标只是传输进度，不等于聊天因果顺序。时间戳用于显示，不能仅靠设备墙钟判断上下文先后。

消息流式片段可先本地暂存，终态以已结算消息/修订保存；同步不会把每个 token 永久变成一个学习事件。生成用量未知时保留未知，不补成零，也不在接续时重新生成同一答案来补账。

### 3.2 ContextSnapshot

`snapshot_id` 指向不可变的模型上下文投影：

- 选定的业务 message/revision IDs，以及各端已观察的因果 frontier。
- 当前问题分支与明确相关的其他旧题，不将所有并发分支冒充连续对话。
- 需要保留的原文、用户修正、已提交题目图片和既有答案引用。
- 复用摘要的来源范围、版本、依赖哈希；摘要不是原消息的替代存储。
- 引用资料/字幕的来源版本与定位。
- 必需附件清单、投影策略版本、内容哈希与预计预算。

不能只用一个全局整数 watermark 表示多端 DAG 的全部状态。可以保存本地业务提交序号加 `included_revisions_hash` 和 `frontier`；本地序号用于高效找变更，哈希和 frontier 用于验证快照是否仍然适用。

### 3.3 ExecutionBinding

| 字段 | 含义 |
| --- | --- |
| `binding_id`, `chat_id`, `branch_frontier` | 用户聊天与当前上下文分支的关联 |
| `execution_generation` | 应用自有的执行快照代次，与 Harness 存储格式 generation 分开命名 |
| `harness_home_ref`, `harness_session_id` | Windows 本地执行位置与会话；不同设备不共享此身份作为同步协议 |
| `snapshot_id`, `context_hash`, `applied_frontier`, `applied_revisions_hash` | 本执行会话已经包含哪些业务事实 |
| `source_dependency_hash`, `profile_policy_hash` | 实际进入上下文的资料/字幕版本及插件/工具策略版本 |
| `execution_epoch`, `active_submission_id` | 当前串行执行所有权和旧回调隔离 |
| `last_event_watermark`, `event_ingestion_cursor` | 已持久摄入的 Harness 事件位置，不等于业务同步游标 |
| `state` | `unbound / ready / running / stale / reconciling / retired / failed` |

业务消息与 Harness 消息保持单独映射表：`binding_id + harness_session_id + event_seq/message_id -> app_message_id/generation_id`。回调摄入使用唯一键去重。业务 ID 不直接替代 Harness 原生 MessageId，避免绕过其不可变身份和恢复契约。

## 4. 主选接续路径：新快照，用户聊天不变

### 4.1 示例

1. Windows 已有 `chat C`，绑定 Harness 会话 H1。
2. 电脑关机；平板在 C 中新增问题 U2，使用本机已下载资料和截图，直接调用 DeepSeek 得到 A2。
3. 电脑上线后，同步 U2、A2 和附件。Windows 显示这些记录，将 H1 的 binding 标记为 stale；**不调用 H1.run(U2)**。
4. 用户在 Windows 继续问 U3。应用选取 U2/A2 等相关上下文，创建快照 S2，分配新 Harness 会话 H2 和新的 execution generation。
5. H2 在接收真正的新问题之前载入“已完成历史问答”引用，随后仅执行 U3。
6. UI 仍是聊天 C，U2/A2 的来源和已完成状态不变。仅 U3 的新生成产生新的问答费用；把历史作为 U3 输入仍会产生相应输入 token，并非免费保留上下文。

只有业务确有变化、依赖失效或上下文分支不同才重建；不能每个问题都无条件创建全新快照。旧执行会话按保留政策可归档，不随换代复制全部视频和图片字节。

### 4.2 学习插件的官方接口落点

推荐原型：应用在调用 SDK 前，发布 `harness_session_id -> ContextSnapshot` 的只读映射。学习插件监听官方 `agent/created`；对新建的学习执行会话读取并校验快照，构造少量有来源的历史引用 `UserMessage`，通过 `agent.inject()` 放入下一步上下文。监听完成后，SDK 的正常 `session/prompt` 只发送当前新问题。

快照仅包含需要接续的历史，不重复包含本次 SDK 即将提交的新用户消息及其新附件；新问题在 prompt 中准入一次。随后 binding 的 applied frontier 才随真实消息/答案提交推进，不能把初始 snapshot ID 当成永远不变的覆盖水位。

这条路径使用实际存在的 created/inject 时序，**没有假设 SDK 存在 `import_history()`**。快照 resolver 可以由本机 HTTP/IPC 或受限的不可变文件读取实现，属于我们需要开发的桥接；不能把任意模型给出的文件路径交给它。

建议声明 producer-owned 来源，例如 `kind:'learning-history', form:'recall'`，携带 snapshot ID 和业务引用；具体 TypeScript 扩展与持久化兼容性需在插件原型验证。内容显式标识原始 role、已完成状态、消息/修订来源，不把历史文本提升为 system/developer 指令。

快照按题目/消息组织，避免一次注入一个巨大、无法有效压缩的单元。当前题目的用户图片保持为实际图像内容，不默认只留下 OCR。所有图片仅来自用户已提交附件；原始完整视频帧与邻近画面不属于插件可补取内容。

监听初始化失败时，应令创建失败或阻止真实提问进入模型，不带空历史继续执行。resume 路径必须检查已存在的快照注入/持久 inbox，不再次注入相同 S2；不能仅用“进程刚启动”判断需要重放历史。

若该插件落点的持久化/时序验证不通过，最小后备是：新 session 的单次真实 prompt 中携带有界的历史引用及当前问题。它同样不是 assistant import，可能影响引用理解和长上下文压缩，必须验证效果；不要未经验证改用伪造原生日志。

### 4.3 为什么不主推把安卓消息增量追加到旧 H1

| 路径 | 适用条件 | 本项目选择 |
| --- | --- | --- |
| SDK 正常增量 prompt | Windows 持有原生完整前缀，只有下一次新用户问题 | 常规连续聊天使用，保留稳定前缀 |
| 对旧 session inject 历史增量 | 已严格确认前缀一致、无重排/修订、注入回执与 watermark 可靠 | 后续优化候选；不能每次同步都 wake/steer，也不能无证明全量重复注入 |
| 新执行 session＋业务上下文快照 | 安卓已有回答、出现分支、旧内容修订、原生会话缺失/不兼容 | **跨端接续主选**，较少依赖原生日志拼接 |
| 原生 seed/fork | 复制同一版本的真实 Harness 事件前缀或受支持原生恢复 | 保留官方能力；不用于伪装安卓普通聊天历史 |

新快照会影响 provider 前缀缓存，不能承诺比增量追加更省 token。代价换来历史边界和执行归属更清楚；原型记录总输入、压缩费用与接续准确性，再决定是否增加受控增量优化。

## 5. 并发、分支和 LWW 的边界

电脑关机期间，不要求手机和平板实时共同编辑草稿；它们仍可能各自产生问题和答案。同步后按照 ID、parent 和回答关系保留双方，不能按最新时间只留一个设备的消息列表。

UI 可保持连续时间线并自动展示来源，不强迫用户创建新对话。生成 U3 的上下文以 U3 声明的父消息/已观察 frontier 为准；需要跨分支引用时再引入相关内容，不能把两条并发答案排列成互相承接的推导。

LWW 适合聊天标题、草稿的同一字段、偏好设置或“当前选中的修订”等可变元数据。用户改题干产生新 revision，后台保留旧 revision 和来源；已有答案不偷偷改成曾基于新版题干生成。两次真实生成各有 generation ID，不使用 LWW 删除其中一份。

Windows 对同一 chat 只允许一个活动执行；输入可以先排队，发送前根据最新业务 frontier 重验 binding。同步期间收到新资料或新消息，不向正在生成的上下文半途塞入，先标记 dirty，当前生成仍绑定原快照完成，下一次新提问再重建。

## 6. 状态机、幂等和崩溃恢复

### 6.1 Binding 状态

```text
unbound --下一次真实提问--> preparing snapshot --> ready
ready --串行取得epoch--> running --结果持久化--> ready
ready/running --相关业务变更--> stale（运行中先完成原epoch）
stale --下一次真实提问--> 新execution_generation + 新session
running --断流/进程丢失--> reconciling
reconciling --找到可确认终态--> ready/stale
reconciling --无法确认远端结果--> outcome_unknown（不自动重发）
旧binding --新generation已就绪--> retired
```

上面的状态名称由本项目定义。真正的新提问、同步摄入和执行恢复是不同命令，不使用一条“新增 user 消息自动 run”的订阅同时承担三者。

### 6.2 Submission 所有权

安卓生成结果同步后，`completed` 保持 completed。未发送问题仍由创建时的 `owner_device_id` 执行；第一阶段不自动将远端 pending 问题转交电脑发送。未来转交需要带版本的明确 handoff，不能电脑和平板各自认为获得执行权。

各端完全断联时无法靠本地锁保证全局唯一云端调用；首阶段通过不自动跨端接管 pending、稳定 submission ID 和恢复对账控制重复请求。不能声称 LWW 会事后消除已经发生的重复计费。

### 6.3 Windows 执行提交

1. 本地事务持久化用户消息、submission、generation、快照和 planned binding。
2. 获取该 chat 的串行执行锁，增加 `execution_epoch`，记录 active submission。
3. 调用一次 SDK prompt，保存其入队 `messageId`；学习插件将真实提问与 active submission/epoch 关联。
4. 事件按 `(binding, session, seq)` 去重摄入；assistant 与 usage 绑定 generation，输出不能仅靠 `run().final_response` 猜归属。
5. 达到可确认终态后，在本地事务写 assistant 事实、用量和终态，并推进摄入 cursor。

SDK 的 JSON-RPC request ID 只是传输关联，不是模型计费幂等键；官方 prompt 服务端每次调用新建用户消息，再 followup。[SDK 服务端 prompt 源码](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/server/src/server.ts)

若要在“入队回执已丢失”的窗口自动恢复，需要额外开发插件 admission fence：在官方 pre-step 进入收费模型调用前，校验 active submission、epoch 和入队消息映射，并确保映射与进度有持久回执。此接口不是 stock SDK 已有的 exactly-once 功能，必须故障注入验证；无法确认时走结果未知，不盲目再次 prompt。

### 6.4 几个必须区分的崩溃窗口

| 窗口 | 恢复动作 |
| --- | --- |
| 应用保存 submission 后、确定尚未交给 Harness | 可以交付原 submission，先确认没有已有入队记录 |
| SDK 入队成功，但回执/业务映射丢失 | 查询原会话和 admission 映射，不重新调用 prompt |
| 请求已到模型，但回复或最终 usage 丢失 | 保存部分结果与结果未知；不声称重试免费或一定没有执行 |
| Harness 已持久化 assistant，应用尚未入库 | 从原执行记录补摄入，唯一键防止新增第二个答案 |
| 旧 epoch 的流晚到 | 关联原 generation，可归档结果；不能覆盖当前活动消息或推进新 binding |
| 新 session 已注入历史，真实提问尚未执行 | 恢复 pending/context 回执，验证只注入一次，不重跑历史 |

官方 `session/resume` 不会重播旧事件通知；恢复摄入需要本项目插件读取并返回相应记录。优先使用 `ctx.sessionQuery` 的受支持查询/观察能力，而非直接编辑 JSONL 或新增生产调用已弃用的同步事件读取方法。该查询服务需在 profile 中明确配置 concrete backend。[Session Query 官方接口](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session-query/session-query/README.md)

### 6.5 取消不能冒充 stock SDK 的现成能力

stock wire 无单会话 cancel/close；关闭 runtime 会影响该进程中的其他执行会话。因此学习插件如提供定向取消，需要自己的受限桥接并调用实际的 `agent.cancel()`，与 submission/epoch 联动验证。未实现之前，只能按官方的 runtime 关闭边界处理，不能宣称已有粒度更细的取消。[SDK 协议限制](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/protocol/README.md)

## 7. 附件缺失、资料 locator 与来源版本

消息元数据同步完成不代表图片字节已到达。截图附件应优先传输、断点恢复并核对哈希；视频和 PDF 仍按课程/文件按需下载。聊天可先显示附件占位与可用状态，不因此重做整个模型请求。

如果下一次问题依赖某张尚未同步的截图，先获取该附件或等待就绪，只阻塞相关请求。若当前截图/文字本身足够，可以不等无关资料。不能用视频 locator 自动截取同位置画面来代替用户已提交图片，也不能拿缩略图悄悄当原题。

来源 locator 只表达“哪份资料的哪个版本、哪个时间/页面”，不能假定电脑仍有该文件，不能当作 DeepSeek 可下载的外部 URL。新执行会话需将已选择的图片字节重新准入 Harness 附件存储，不能跨设备复用另一个 runtime 的私有 attachment ID。官方 SDK 图片准入会先转换为本地 durable references。[SDK 图片准入实现](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/sdk/server/src/server.ts)

### 7.1 哪些变化需要重建

需要重建或重选快照：选中历史消息的 revision 改变、父分支改变、当前题图片被用户换图/重新裁剪、上下文中实际使用的字幕/资料版本变化、插件策略或工具契约变更、旧执行会话不可恢复。

不必重建：无关课程新导入、与本题无关的字幕继续转写、播放进度更新、无关附件到达。依赖哈希只覆盖实际纳入上下文的来源，不对整个知识库每次变化都作废所有聊天。

旧答案继续绑定旧来源版本。新回答选用最新资料时记录新版本；不把旧引用的位置静默改成当前内容，也不在重建时伪造旧工具当时使用了新版字幕。

## 8. 工具历史与模型上下文压缩的双层所有权

### 8.1 业务层拥有的内容

应用数据库拥有完整聊天、来源、用户附件、修订和跨设备生成事实；ContextSnapshot 负责选择与当前问题相关的业务材料，保留当前题目的关键条件和已提交图片。摘要可缓存，但须记录其来源 revisions 与哈希，源内容变化后失效。

同步不自动调用模型摘要；只更新可追溯业务事实和快照有效性。必要摘要在下一次用户提问等已认可触发条件下生成并计费，不能每次导入或同步都重新总结整段历史。

### 8.2 Harness 拥有的内容

Harness 在当前 execution generation 内管理原生消息、真实工具往返、上下文 surface 和 compaction。官方 compaction 用额外模型请求总结较早的平衡区间，保留近期内容，并在原生日志中保存被遮蔽原文；它不替应用保存跨设备业务真相。[Compaction 引擎说明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/compaction/compaction/README.md)、[Basic compaction 策略](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/compaction/compaction-basic/README.md)

初期由学习插件统一预算：不同时让应用与 Harness 各自每轮压缩同一段历史。正常单 generation 的压缩交给 Harness；跨端重建时应用从业务源制作快照，可复用有明确来源范围的摘要。不能未经对应校验，把旧 Harness 摘要直接当成最新完整跨端历史。

Harness compaction 的费用同样归入实际 generation 的统计。不能因为普通对话 `run()` 只返回一个答案，就漏算内部工具追加请求和 compaction 请求。

### 8.3 工具结果只作为事实或原生历史存在

同一原生执行会话内，真实工具调用及结果由 Harness 配对保存；继续复用即可。跨端快照里，只引用用户需要的结果、出处和版本，作为已发生的资料事实，不伪造 `role:'tool'` 或旧 call ID，不制造“模型正等待的工具返回”。

如果安卓端执行过检索，其业务记录可保存查询、命中文段和来源版本；Windows 可以引用这些事实或重新按需检索，但不声称它们是 H2 原生工具步骤。历史文本中的工具命令也不因同步被重新执行。

## 9. 最小桥接接口草案

以下是我们拟开发的应用接口，**不是 Harness SDK 方法名**：

```text
SyncIngest.apply(mutations, attachments) -> accepted_cursor, changed_dependencies
ContextProjection.build(chat_id, parent_frontier, user_message_id) -> snapshot
ExecutionBinding.ensure(chat_id, snapshot, policy_version) -> binding
SnapshotResolver.resolve(harness_session_id) -> immutable_snapshot
SubmissionAdmission.begin(submission_id, epoch, harness_inbox_id) -> admitted_or_existing
ExecutionEvents.ingest(binding_id, epoch, harness_event) -> durable_cursor
ExecutionRecovery.reconcile(binding_id, submission_id) -> known_terminal_or_unknown
```

桥接层统一管理 profile/home、会话创建和恢复、按聊天串行、绑定校验与事件摄入；学习插件提供快照注入、受限上下文工具和必要的执行对账。Harness 内部数据库/JSONL、session ID 和附件 URI 不直接成为安卓同步协议。

## 10. 必须通过的原型验证

1. **零调用同步**：平板完成 U2/A2 后同步到 Windows；模型请求计数不增加。
2. **正确接续**：下一次 Windows U3 结合 U2/A2 解答；UI chat ID 不变，执行 session 可变；旧题不重跑。
3. **插件时序**：agent/created 中快照完成后才进入首个模型请求；resolver 失败时不得空历史调用。
4. **只注入一次**：新 session、持久恢复、进程崩溃后，快照及当前图片不重复加入。
5. **附件缺失**：消息先到、截图后到；只阻塞依赖截图的问题，不自动从视频补图。
6. **并发分支**：手机和平板离线各产生问答，恢复后双方保留；下一次问题选对因果上下文，无弹窗冲突选择。
7. **收费幂等边界**：分别在入队前、入队回执丢失、流中断、assistant 已保存但业务未保存时故障注入；不得将未知状态默认为未执行并重发。
8. **旧 epoch**：旧回复晚到不覆盖新的活动回答；仍能保留与原尝试对应的结果和用量。
9. **工具一致性**：安卓工具来源事实进入历史引用，不生成虚构 tool 对；Windows 真实工具轨迹可以恢复。
10. **版本失效**：修改当前题干/字幕导致下一次正确重建；无关转写进度不会持续作废会话。
11. **压缩与图片**：长对话阈值触发后仍保留本题条件、公式和已提交图片；两层不重复总结，用量统计包含辅助请求。
12. **生命周期**：验证插件引入的取消/恢复桥接；stock SDK 缺少的方法不能被 UI 当成已实现能力。

在上述验证完成前，只能确认“官方接口存在且可以支持该方向”，不能声称已完成任意历史导入、跨端透明恢复、exactly-once 计费或无损上下文接续。
