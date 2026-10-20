# AI 接入与 DeepSeek Harness 评估依据

> **历史方案，架构建议已被用户后续决定覆盖。** 用户现已确定 Windows 使用 DeepSeek Harness 及适用插件作为执行层主选，安卓独立基础学习与直接 DeepSeek 调用。本文保留当时比较依据，不再代表“Windows 首版不依赖 Harness”的现行决策。新的实际插件、依赖及复用边界见 [Harness插件复用清单与依赖核查.md](./Harness插件复用清单与依赖核查.md)。本文有关 API 能力的事实仍应按所选 Harness 版本与最新官方文档复核。

核查日期：2026-10-18（Asia/Shanghai）  
状态：技术选型研究；未安装应用、未运行付费模型请求、未测试真实截图解题效果。

依据：《AI辅助学习项目技术原则与分阶段范围.md》及《AI辅助学习项目需求总结.md》。本文只提出本模块选型，不改变已确认的需求。

## 1. 选型结论

**建议首版直接使用 OpenAI-compatible Python SDK 的 `AsyncOpenAI`，连接 DeepSeek 官方 Chat Completions；在应用内实现受限的上下文补充流程。Harness 保留为参考和后续替代候选，暂不进入首版运行依赖。**

这是结合项目需求得出的工程判断：核心路径是一个问题的一次多模态解答，必要时按顺序补充字幕、资料；不需要通用编码代理、终端、插件管理或多代理调度。优先复用模型协议、异步网络与流式解析组件，把课程、题目、截图、字幕和聊天存储掌握在应用自己的业务模块中。

推荐初始配置（待真实素材测试）：

| 项目 | 建议 |
| --- | --- |
| 服务地址 | `https://api.deepseek.com` |
| 模型 | `deepseek-flash` |
| 协议 | Chat Completions，流式 SSE |
| 首轮推理配置 | 显式关闭 thinking，作为速度与 token 基线；再与 `low` 做效果对照 |
| 图片 | 用户提交的裁剪结果，通过 base64 `image_url` 发送；默认保留公式细节 |
| 额外模型调用 | 不默认增加 OCR 模型、问题分类模型、计划模型、校验模型 |
| 工具 | 只提供当前阶段需要的字幕读取或资料检索，不提供 shell、任意文件读取、自动视频取帧 |
| 版本 | 验证后固定 SDK 版本及应用配置，记录实际响应的模型和指纹；云端别名不等于冻结权重 |

DeepSeek 的官方入门示例直接使用 OpenAI SDK 与自定义 `base_url`，因此该方式属于官方展示的兼容接入路径。[首次调用 API](https://api-docs.deepseek.com/)

## 2. 已核实的模型与 API 事实

### 2.1 当前图片能力与模型选择

官方当前模型表中：`deepseek-flash` 对应 DeepSeek-V4.1-Flash-1017，支持图片；`deepseek-v4-pro` 当前不支持图片。旧的 `deepseek-v4-flash` 与 `deepseek-v4-flash-vision-exp` 名称仍可接受，但已转由新的 Flash 服务，不能通过使用旧名称固定旧模型。[模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)

所以首版不需要另外先调用视觉模型把截图转成文字，再交给 DeepSeek 解题。`deepseek-flash` 可直接接收文字和截图；实际公式、图形与数学解题效果仍需验证。

官方视觉说明支持 JPEG、PNG、GIF、WebP，以及 base64、可访问 URL、Files API 三种图片传递方式；Chat Completions 的图片放在 `user` 消息中。公式截图建议先用原始细节：`low` 会先缩小至 512×512，可能损害上下标与符号辨识。[Vision](https://api-docs.deepseek.com/guides/vision/)

**不把 localhost、Windows 文件路径或 Tailscale 私有 URL 当成 DeepSeek 可下载的图片地址。** 首版从本地后端读取用户实际提交的附件并发送 base64；不会发送视频，也不会自动补取视频画面。

### 2.2 Thinking 对速度、token 和工具协议的影响

官方当前 thinking 默认开启，可以通过 `thinking.type=disabled` 显式关闭；`reasoning_effort` 支持不同强度。开启思考且请求携带 `tools` 时，后续请求需要按官方规则回传先前的 `reasoning_content`，否则可能返回 400。[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)

**设计判断：** 首版速度优先，先明确关闭 thinking，避免意外采用服务默认的较重思考配置。数学题正确性是验收项，不能预先宣称关闭思考一定合适；用相同真实截图对比关闭、低强度思考的解答质量、首个答案时间、完整耗时和总用量，决定最终默认值。用户要求检查时可单独提高推理配置，不默认加第二个模型做评审。

### 2.3 工具调用与流式使用

DeepSeek 官方支持函数工具调用，工具执行由应用负责，模型返回函数名与参数；非思考模式官方例子使用 `extra_body={"thinking":{"type":"disabled"}}`。[Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)

Chat Completions 支持 SSE、工具参数增量和 token usage；当前文档说明最后一个结束块携带 usage，并非必须是一个空 `choices` 的独立 usage 块。适配器必须读取任意块的 usage，并依据 `finish_reason` 结算，不能照搬其他服务的流式假设。[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)

**设计判断：** 采用 SDK 普通流迭代，不绑定某个高级 agent helper。按 tool-call index 拼接参数，完整解析并校验后才执行本地工具；及时响应取消，保存已生成答案与结束原因。流中断时记录“用量未知／未完整返回”，不能把缺失 usage 当成零消费。

### 2.4 上下文缓存与 Files API 不能混为一谈

官方上下文缓存默认启用，匹配已持久化的完整前缀单元；请求边界、重复公共前缀及长输入的固定间隔可能形成缓存单元。只写“相同前缀必命中”不准确；官方明确是尽力而为。[Context Caching](https://api-docs.deepseek.com/guides/kv_cache/)

**设计判断：** 稳定的系统说明放在前面，避免每次在前缀插入时间、随机 ID 和无关课程状态；上下文压缩只在有必要时执行。缓存主要影响账单与延迟，不能承诺降低模型统计的 `prompt_tokens`，也不能为了缓存把整课字幕留在每次请求中。

Files API 可减少同一附件在连续追问中的重复上传，但不代表图片推理 token 消失。压缩 JPEG/PNG 主要改善带宽，也不能按字节比例推算 token 节省。首版先用 base64 简化生命周期，只有实测带宽成为问题时才加入远端 file_id 与本地附件映射。[Vision：Files 与 Token Usage](https://api-docs.deepseek.com/guides/vision/)

### 2.5 为什么先选 Chat Completions

官方也支持 Responses，但其兼容表明确不支持 `previous_response_id`、`conversation`、服务端 `store`、自动截断等能力，并且部分参数会被忽略；不能以为换用 Responses 就可以不发送所需历史。[Responses API 指南](https://api-docs.deepseek.com/guides/responses_api/)

**设计判断：** 先采用 DeepSeek 官方入门示例覆盖的 Chat Completions。应用自行持久化对话、筛选上下文，模型请求只提交选定的上下文。日后有实际需要再添加 Responses 适配器。

## 3. SDK 核查与纯 HTTP 的取舍

### 3.1 `openai-python` 能覆盖什么

官方 SDK 支持 `AsyncOpenAI`、自定义 `base_url`、流式迭代和 `extra_body`，并允许通过模型额外字段读取第三方服务扩展结果。[SDK README](https://github.com/openai/openai-python)

源码中，Chat Completion 参数支持图片消息、`stream_options`，模型名允许任意字符串；图片类型包括 base64 URL 及 `original` detail。可由此建立很薄的 DeepSeek 适配层。[请求参数源码](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/completion_create_params.py)、[图片参数源码](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_content_part_image_param.py)

注意使用 DeepSeek 参数语义，不能把 SDK 中所有 OpenAI 字段一并发送。`thinking` 用 `extra_body`；DeepSeek 扩展的 cache-hit/cache-miss 等 usage 字段在适配层显式读取，保留必要原始用量字段，以便 SDK 字段定义变化时检查。[usage 类型源码](https://github.com/openai/openai-python/blob/main/src/openai/types/completion_usage.py)、[SDK BaseModel 源码](https://github.com/openai/openai-python/blob/main/src/openai/_models.py)

### 3.2 为什么不是首版手写所有 HTTP

| 路径 | 优点 | 需要承担的工作 |
| --- | --- | --- |
| OpenAI-compatible SDK | 复用连接管理、异步请求、SSE 解析和类型对象；官方 DeepSeek 示例覆盖 | DeepSeek 参数、usage、thinking 的薄适配；合理设置超时、重试与取消 |
| `httpx`＋自行解析 | 完整控制第三方字段与协议，依赖少 | 处理跨网络块 SSE、错误响应、keepalive、工具参数碎片、连接生命周期及取消 |
| Harness SDK | 会话、事件、模型、工具循环等成套能力 | 多一层运行树和协议，需裁剪非学习能力并映射应用自己的数据模型 |

**结论为设计选择：** 首版选 SDK；将接口封装得足够小，在兼容性确有问题时替换成原生 HTTP。使用 SDK 不等于调用 OpenAI 服务，模型地址始终是 DeepSeek 官方地址。

### 3.3 本地核查范围

本机 Python 当前未安装 `openai`，已发现 `httpx 0.28.1`、`pydantic 2.13.4`。本次没有为了选型安装新依赖，也没有实际构造带凭据的模型请求。因此“SDK 源码提供所需能力”已经核对，“锁定某版本后在本机正常运行”仍待验证。

源码参考点：`openai/openai-python` 的 `main` 在本次查询得到提交 `553703e2d3db379a83e3ead9e40436b7a66f639e`。这只是研究依据，不是建议直接锁主分支；实现时选择经过兼容验证的已发布版本。

## 4. Harness 的复用价值与代价

### 4.1 已核实的组成

Harness 将模型、会话、工具、agent loop 都构造成 Cordis 插件，确实可以替换与重组；官方架构的 SDK profile 通过正常 `dsh` 启动流程运行，而不是一个仅负责 HTTP 请求的小库。[架构文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)

TypeScript SDK 会启动完整 runtime 子进程，通过 stdio JSON-RPC 发送问题和读取事件。Python SDK 同样驱动附带的 runtime；不是调用一次 SDK 就直接变成纯 Python 的 agent 内核。[TS SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/client/README.md)、[Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)

官方 `sdk-minimal` 省去了 Web、托管凭据等服务，但默认仍是最小编码代理：平台 shell、`read_image`、工作目录工具、JSONL 会话及自动上下文摘要。其配置树还包含附件、本地执行和其他内核服务。[sdk-minimal README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/README.md)、[实际配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/cordis.patch.yml)

官方仓库仍标注 developer preview，明确提示兼容性破坏。当前研究的 `master` 查询提交为 `687f282e8625c6b8d11ff58211a90524be2e4317`，SDK 客户端源码包版本为 `0.2.1-rc.2`。版本信息只表示研究时的源码状态，未核实对应制品安装或 Windows 启动。[仓库说明](https://github.com/deepseek-ai/deepseek-harness)、[package.json](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/client/package.json)

### 4.2 本项目的选择

| 路径 | 对本项目的判断 |
| --- | --- |
| 全量 Harness＋定制 UI | 需要绕过现有 UI 价值，剪掉大量非学习能力；首版收益不足 |
| 裁剪 Harness 运行底座 | 可以做到，适合需要丰富 agent 执行、插件生态时重新评估；当前还需维护 Cordis/profile 和会话映射 |
| 抽取个别源码模块 | 可能产生对内部包的传递依赖；除非独立边界清楚，不建议直接复制整组包 |
| 学习其会话、事件、取消和上下文设计，业务内薄编排 | **推荐首版**；复用 SDK 和成熟组件，保持学习业务统一 |

以上是按需求匹配度作出的判断，未测量 Harness 的 CPU、内存、延迟或 token 差值，**不能声称“不用 Harness 一定省多少 token”**。Harness 可以被裁剪，薄编排也可能写出冗长提示；实际用量由发给模型的内容、工具描述、推理配置和调用次数决定。

如果之后出现大量可复用工具、复杂任务恢复或多代理需求，再以同一组真实学习问题比较 SDK 路径与裁剪后的 Harness。当前不提前建设通用 agent／插件平台。

## 5. AI 模块的小接口与业务边界

建议拆成三个接口，下面为接口草案，不是可运行代码：

```text
ModelGateway.capabilities(model) -> ModelCapabilities
ModelGateway.stream(ModelRequest, cancellation) -> AsyncIterator<ModelEvent>

LearningAnswerService.ask(QuestionSubmission, cancellation)
    -> AsyncIterator<AnswerEvent>

LearningContext.read_subtitles(video_id, time_range, missing_fact)
    -> ContextResult
LearningContext.search_materials(query, course_hint, constraints)
    -> ContextResult
```

- `ModelRequest`：应用选定的消息、用户提交附件引用、当前允许的工具、推理和输出上限配置；不携带整库内容或数据库对象。
- `ModelEvent`：文字增量、完整工具调用、usage、结束原因、错误；屏蔽 SDK 专属对象。
- `AnswerEvent`：答案增量、补充资料状态、引用、完成与错误；UI 不依赖 DeepSeek 的消息结构。
- `ContextResult`：有来源 ID、时间／页码定位、内容修订号和覆盖状态，区分“未处理到”与“未检索到”。
- 聊天记录、附件、课程和字幕持久化由应用的数据模块负责；模型网关不成为第二套会话数据库。
- 本地上下文工具的参数由应用校验和绑定范围，不允许模型传任意文件路径；模型没有主动取帧接口。

Windows 首版由 Python 本地后台执行 `LearningAnswerService`。后续安卓需各端具备对应的本地模型调用与上下文能力；共享协议与数据结构即可，不要求所有平台直接执行同一份 Python 服务。

## 6. 按需上下文流程与模型轮数

### 6.1 充分输入的常用路径

```text
用户截图＋文字
  → 本地取得选定附件，整理必要历史
  → 第 1 次模型请求
  → 直接流式输出完整解答
```

不先用模型做独立 OCR，不先做分类或“是否需要字幕”的单独模型请求，不等待字幕任务完成。单轮正常路径为 **1 次模型请求**，并非所有场景都固定 1 次。

### 6.2 信息不足的补充路径

```text
第 1 次请求：可以解答，或请求当前视频相关字幕
  → 程序读取/优先处理字幕
第 2 次请求：结合字幕解答，或请求知识库
  → 程序检索资料
第 3 次请求：结合检索结果解答；仍缺关键条件时请用户补充
```

这是建议的受限状态机：每次只开放当前允许的补充阶段，避免模型同时调用字幕和知识库跳过顺序。无视频来源时不能假装可读字幕；字幕未就绪、无有效内容等状态显式进入后续处理。

目标为：输入足够时 1 次；使用字幕时通常 2 次；再使用知识库时通常 3 次。不默认反复规划、互相评审、自动“再检查一次”。预取范围过小而需要扩大、网络失败重试等会增加次数，应单独计量而非承诺绝对上限。

触发补充应记录具体的缺失信息，而非只有“信心低”：例如缺老师提到的结论、用户明确要求某资料方法、要求找前文。关键图形、题干或符号缺失时，字幕和资料不能成为猜题的借口；最终请求用户补充，禁止自动补图。

用户明确问“某讲义第几题”属于另一入口：按已知文件／页码／题号先由程序定位，再一次模型解答；需要理解模糊自然语言定位时可通过受限工具补充。**它不要求先尝试凭空解题。**

注意：上述机制能约束读取顺序，不能保证模型总能发现缺失条件。验收应覆盖“确实不完整却直接编答案”“已足够却频繁取字幕”两类问题。

### 6.3 连续聊天的上下文

- UI 与数据库保存全部历史，模型只看当前需要的投影。
- 保留当前题目原始截图、已确认条件、用户纠正、必要推导和近期追问。
- 对当前题目的图片不默认只保留 OCR 文本，避免图形与公式丢失。
- 旧问题先通过本地消息、附件引用和关键词定位取回；需要摘要时，在阈值触发后生成并复用，不每轮摘要。
- 摘要有额外调用费用，也可能丢条件；是否省总费用需计算摘要成本和之后复用次数。
- 用户自主新建对话；后台上下文组织不强制拆题，不删除可见历史。

## 7. 后续安卓独立问答的边界

Windows 本地后端代理 DeepSeek，仅解决首版调用与凭据存储，**不能视为已经实现“电脑关机，安卓仍可调用 API”**。

安卓后续建议保留两条候选：

1. 浏览器/PWA 直接调用：要验证 DeepSeek 实际 CORS、图片请求大小、流式响应和各来源；凭据在设备本地管理，不内置在前端构建中。
2. 原生容器或客户端提供 HTTP 与凭据存储桥接：例如后续评估 Android Keystore；可避免浏览器跨域限制，但有移动端集成与发布工作。

SDK 的浏览器开关并不能解决服务端 CORS，也不能替代凭据存储。官方 Node SDK 默认禁用浏览器运行，并说明显式启用时的凭据暴露问题；这是 SDK 行为依据，不等于本项目不能设计个人设备直连。[openai-node 运行环境说明](https://github.com/openai/openai-node#requirements)

### 7.1 本次无凭据 CORS 预检

只对 `https://api.deepseek.com/chat/completions` 发送一次 OPTIONS，无请求体、无密钥、无资料，未发送付费 POST。

```text
Origin: http://localhost:5173
Access-Control-Request-Method: POST
Access-Control-Request-Headers: authorization,content-type

HTTP 200
access-control-allow-origin: http://localhost:5173
access-control-allow-methods: POST
access-control-allow-headers: authorization,content-type
vary: origin, access-control-request-method, access-control-request-headers
```

这是本次请求的运行时观察，说明这一 origin 的这一组预检当时通过；**不验证带鉴权的实际 POST、不验证 Android WebView/PWA 其他 origin、不构成长期浏览器接入承诺**。Windows 首版仍从本地后台调用，移动独立问答在后续阶段做真实端到端验证。

## 8. 下一步最小验证与记录指标

| 验证 | 所需输入 | 判定与观测 |
| --- | --- | --- |
| SDK 契约 | 本地合成 SSE、无真实密钥 | `extra_body`、图片结构、工具碎片、usage 扩展、取消与断流解析 |
| 截图独立解题 | 用户主动选取的数学截图＋问题 | 不读字幕、不检索、1 次请求；公式、图形和结果可核对 |
| 推理配置 | 同一题集 | 关闭与低强度思考的首个答案时间、总时长、token 和解答质量 |
| 补充顺序 | 足够／缺字幕上下文／缺关键图片三类样例 | 顺序符合规则，必要时请用户补图，不取其他视频帧 |
| 连续追问 | 同题多轮、长历史、回到旧题 | 不串题、不丢条件，记录实际上下文与缓存量 |
| 工具异常 | 无字幕、未处理、检索为空、参数错误 | 显式覆盖状态，有限恢复，不无限循环 |
| 用量统计 | 每一次模型请求 | 输入、输出、缓存命中／未命中、reasoning token、请求轮数、状态与结束原因 |

本次没有读取任何 API 密钥，没有运行付费模型请求，没有从验收视频抽帧或上传音视频／图片。模型质量、SDK 锁定版本和 Windows 实际流式链路均尚未完成验证。
