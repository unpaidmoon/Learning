# Harness 学习工具与任务接入审计

日期：2026-10-23。范围：正式 `execution/`、桌面网关、profile／guard，以及固定 Harness 源码的 skill、工具执行和 Session 接缝。责任：本文件为独立只读调查报告；未修改其他文件，未启动／重启 Host 或用户实例，未读用户课程／凭据，未调用真实模型。下列文件路径均以 `D:\Codex\Learning` 为根。

## 结论与实施边界

1. **继续使用当前一个正式 `ExecutionAdapter`／Host／MessagesBoundary，在同 Host 内为翻译和长任务建立独立 Session，接口最小且有现成并发接缝。** 不应为每个任务直接 `ExecutionAdapter(...).open()`；其构造和 `open()` 会创建并启动另一套 Host／Boundary，`close()` 会停止它自己持有的宿主。任务门面应借用现有实例，只有应用执行层负责关闭。
2. **原生 skill 目录、模型 `skill` 调用和用户 `/name` 路径可复用；仅加入 SKILL.md 不能生效。** 现 profile 禁用 loader，guard 在执行及三处观测点拒绝一切工具，Host 就绪检查也要求工具集为空；桌面网关还未放行 `skills/list`。
3. **Boundary 已按每次 Messages 请求记账，并支持纯 `tool_result` 续答；不能把一次逻辑任务记成一次实际请求。** 原生 agent loop 自行执行工具并继续模型请求，无需应用重发 prompt 或再造供应商执行层。
4. **现账本的所有者仍是聊天 generation。** 独立翻译／任务不能通过伪造聊天消息接入。新增任务 execution run 及自己的 attempt／usage FK、统一只读查询，是保留旧聊天约束的可行小改动。
5. **新增工具必须把来源读取、成果写入和用户实际学习动作分开。** 只读来源工具与任务／对象／预期版本受限的写工具可以注册；任意文件／shell／视频帧、替用户作答／评分和隐式外部访问继续不可注册。

依据为用户本次实施授权及《13场景统一交付实施方案》4.3～4.5、5、G03／G04／G07。旧“P0 无工具”属于当前实现基线，不应作为新场景已经接通的证据。

## 证据身份与覆盖

| 证据 | 本次观察／来源 | 可支持的结论与限制 |
| --- | --- | --- |
| 固定源码 | `harness/runtime.lock.json`、`harness/desktop.lock.json`：参考提交 `687f282e8625c6b8d11ff58211a90524be2e4317`；缓存 ZIP 实算 SHA256 `828bbbebaf507d2b1fc335a608e1c8174e2dd5c5c80ebb64d5146ddc5ecb511f` | 本次从 `.runtime/desktop/upstream` 阅读源码；其 `.learning-source.json` 与锁一致。另将19个本次涉及的核心文件逐字节对照该 ZIP，19／19 相等。没有用缓存目录名称代替内容核验。 |
| 实际 EXE | `.runtime/harness/install/deepseek-harness-sdk-runtime-win-x64.exe` 实算 SHA256 `f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365`；长度 `273157120` | 与运行锁一致。未执行它；不能由源码推定所有新 API 在该包可用。 |
| 客户端 build | `harness/desktop.lock.json:12` 与 `research/reports/harness-desktop/桌面精简界面与交互验证-2026-10-22.md:17` | 既有实际 HTML／静态 REA 记录为 `332895e`，不是上述源码提交构建。历史 Evidence 为 `ev_331666df1a6c156f5accbbb28adbed3bc3834c2c556322f370d4e080efab4a1e`；这里只沿用其身份与限制，不把 UI 取证当成工具接入验收。 |
| 历史工具兼容 | `research/reports/v1/V1-2026-10-21.json:25`、`:2946`、`:3291`；对应 `research/probes/v1-harness/profile/v1-plugin.mjs:67` | 历史记录对同 hash EXE 注册了 `read_course_material` 等 raw `ctx.tools.register()` 工具，registry `skill` 激活，但 `tool-skill`／filesystem provider 禁用。可复用 raw 工具注册、对象身份和执行 guard 形式；不证明原生 skill loader 或现工作台完整链已通过。此探针代码不得成为正式运行依赖。 |
| 当前工作区 | 审计先核 `git status --short` 与 adapter／host diff | 两文件原有未提交改动是 `desktop_enabled` 参数透传；本报告保留它们。`desktop.py` 等尚未提交，均按工作区实际源码读取。 |
| 新任务存储 | 审计中读取 `backend/src/learning_app/workbench/tasks.py:326`、新迁移 `0007_learning_workbench.py` | 读到的 `bind_generation()` 暂时依赖同课程聊天 generation；下文是独立执行接入建议，未改该存储，也未宣称它已接入 Host。 |

本次遵循 REA skill 的路由边界：完整源码可以回答的问题直接分析源码，没有运行 REA doctor／setup 或安装引擎；需要制品身份的部分沿用上述既有取证并独立重算 EXE hash。新工具、原生 skill、任务取消的实际运行仍待单独验证。

## 原生 skill 的真实接缝

固定源码前缀为 `.runtime/desktop/upstream/`：

| 文件／符号 | 实际接口／行为 | 正式接入建议 |
| --- | --- | --- |
| `packages/skill/skill/src/index.ts:439` `SkillRegistry.register(skill)` | 接收 `{name, description, content, source, invocation?, provider?, resourceBase?}`；默认模型／用户均可调用。`registerProvider(factory)` 在`:390`注册 provider；`list/get/snapshot` 按调用 agent 的 scope 合并目录。 | 项目自有 plugin 可在 `apply()` 中注册受审查的静态学习 skill，`source:'runtime'`，可用 `resourceBase:{kind:'opaque',description:...}`。这无需开启项目／用户目录扫描，也无需另一套触发器。 |
| `packages/skill/tool-skill/src/index.ts:79` `apply()` | `inject=['agents','tools','skills','workingDirectory']`；`:84` 定义原生工具 `skill`，参数 `{name:string}`；`:163`注册；`execute()` 以 `exec.agent`、当前 cwd、signal 调 `ctx.skills.list/get()`。 | profile 重新启用 `tool-skill`，保留 registry `skill` 与 working-directory **服务**。不需要恢复 `tool-working-directory` 工具。 |
| 同文件`:179`、`:215` `agent/pre-step` | `/name` 直接注入 skill 正文；模型路径先获摘要目录，再调用原生 `skill`。目录可持久更新；未知／禁止调用的名字不被加载。 | 不加每轮应用预检索、分类模型或手动启用前置步骤。`skill` 仅加载项目指令，实际资料读写仍走独立业务工具与授权。 |
| 同文件`:411` `SKILL_GESTURE`、`:420` `invokedSkillNames()` | 正则 `/(^|\s)\/([a-z0-9]+(?:-[a-z0-9]+)*)(?=\s|$)/g`，扫描 `source.kind==='user'` 的全部 text block。不是只认整个 prompt 第一行。 | adapter 的 generation marker 独立 text block不会挡住 `/name`。但 `/name` 是指令加载手势，不是扩大资料、写入或外部访问授权的凭据。 |
| `packages/skill/skill-filesystem/src/index.ts:47` `Config` | 有 `includeDefaultRoots`（默认 true）、`customSkillDirs`、`bundledSkillDir`、watch 等；默认会含项目与用户目录。 | 若必须使用 SKILL.md provider，应 `includeDefaultRoots:false`，只设置正式受审查目录 `customSkillDirs`，按固定内容交付。不要简单取消 disabled 后开放用户／课程目录中的任意 skill。静态 registry 注册更窄，可继续禁用 filesystem provider。 |
| `packages/api/session-controller/src/skill-catalog.ts:19` `SessionSkillCatalog.list({sessionId})` | Remote namespace 是 **`skills/list`**，按 Session cwd／preset 查询用户可调用目录，可冷读。 | `desktop.py:_READ_METHODS` 增加这个精确方法；先核业务会话映射，转发只读结果，不开放 `settings`、插件管理或任意 Session。 |
| `packages/client/ui-skill/src/client/index.ts:113` `fetchCatalog(sessionId)` | 原生 UI 正是调用 `skills.list({sessionId},signal)`。 | 源码支持原生目录建议；实际 `332895e` bundle 的该 UI 能力仍需 fixture 浏览器核对，不能用源码存在替代。即使建议 UI 缺失，模型目录与用户直接 `/name` 也须分别实测。 |

`packages/bundle/base/cordis.patch.yml:308` 的三个实际 id 是 `skill`、`skill-filesystem`、`tool-skill`。`web-app/cordis.patch.yml:514` 默认禁用后两者；本项目 `preset-standard.plugins=[]`，所以必须明确挂载 loader，不会因保留空 preset 自动得到学习 skill。

## 当前硬拒绝与必须成组修改的位置

| 当前位置 | 拦截点 | 需要改成什么 |
| --- | --- | --- |
| `harness/profiles/learning.patch.json` 的 `tool-skill`／`skill-filesystem` 行 | 二者 disabled；没有项目学习工具 plugin | 开启原生 loader；选择上节的单一受控 provider 方案；新增正式项目工具 plugin。保留 bash／pwsh／fs／web／subagent／插件安装／goal 等禁用。 |
| `harness/plugins/learning-guard.mjs:111` | `ctx.tools.guard()` 无条件返回 `LEARNING_HARNESS_TOOL_NOT_AVAILABLE` | 改为有限名字、可信 definition 对象、agent 身份、活动 run grant 的交集。未知名字、同名 shadow、无 grant、过期版本／范围均拒绝；工具 body／后台业务层再次核范围。 |
| 同文件`:116` `agent/created` | 有任意工具即抛 `LEARNING_HARNESS_UNEXPECTED_TOOLS` | 核该 agent 的预期可见工具集合，登记精确 agent 对象；需要时以 `agent.ctx.tools.restrict({allow:[...]})` 缩小继承工具。翻译 Session 可使用 `allow:[]`。 |
| 同文件`:122` `llm/stream` | `options.tools` 非空即抛错，审计固定写 `tools:[]` | 校验实际 schema 名称／digest 与当前 run policy，记录真实列表；不能删掉检查后仍记录空列表。 |
| 同文件`:132` `system-prompt/assemble` | `assembled.tools.length` 非零即抛错，审计固定空工具 | 改为作用域内白名单与 schema 核验；保留系统段 hash。诊断用无 agent assemble 与真实 agent 视图须分列。 |
| 同文件`:180` 激活记录 | 固定 `mode:'p0-fail-closed', tools:[]` | 记录新 policy 版本、固定工具／skill manifest digest；未注册预期工具应失败，不用“已激活”掩盖缺失。 |
| `execution/host.py:211` `audit_state()` | `value.tools != []` 即 `HARNESS_UNEXPECTED_TOOLS` | 用正式 capability manifest 核预期集合／hash；缺少和额外工具均失败，不能只判断非空。控制 `/state` 不返回 bridge token 或来源正文。 |
| `desktop.py:42` `_READ_METHODS` 与`:523` `rpc()` | `skills/list` 不在允许集合，返回 `DESKTOP_METHOD_DENIED` | 精确放行目录读取并保留 Session 映射校验；任务 Session 不因知晓 ID 就成为 renderer 可遍历的聊天。 |
| `harness/tests/learning-guard.test.mjs:19`、`backend/tests/test_execution_host.py:22/:155` | 测试把空工具作为唯一正确结果 | 更新为预期清单和拒绝负例；增加真正模型工具调用→后台读写→工具结果→继续回答链，而不是仅断言 schema 存在。 |

**`boundary.py` 当前没有“tools 必须为空”的硬拒绝。** `_handle()` 只是把请求的 tool name 存进 row（`:748`）；官方转发主要校验模型／思考／max_tokens。因此应新增按 run 的工具 schema／输出来源核验，不能把放开上述 Host 检查等同于后台权限已落实。

## 最小工具实现与业务桥

固定源码 `packages/core/tools/src/index.ts` 的可用接缝：

- `ctx.tools.register(definition)`（`:1071`）：原始 definition 包含 `name,description,parameters,output:{schema,render},execute(args,exec)`。`parameters` 是 JSON Schema；不是 `defineTool()` 使用的简化 DSL。
- `execute(args,exec)` 的 `exec` 含可信 `agent`、`callId`、`rootCallId`、必需的 `signal`（`:327`～`:419`）。不要从模型参数取 course/session/run 身份。
- `ctx.tools.guard()`（`:1144`）是同步、单调拒绝；在可扩展的 pre-execute 后执行。HTTP 权限查询不能直接塞进同步 guard；可读本地不可变 grant，再在异步业务调用内核数据库事实。
- `agent.ctx.tools.restrict({allow,deny})`（`:1105`）仅作用域上下文可用，隐藏继承工具；definition 可被 scoped 同名 shadow，必须另核 `ctx.tools.get(exec.name,exec.agent)===expectedDefinition`。
- 注册 `timeoutMs` 是合作式超时；`execute()` 仍须把 `exec.signal` 传给 fetch／owned worker，并等待其停止。不能以杀 Host 实现停止。

可复用历史 `v1-plugin.mjs:74`～`:118` 的无包依赖 raw 注册、精确 definition Map、agent 对象身份和 guard 结构，迁入正式 `harness/plugins/` 后接真实业务接口。不要 import `research/probes`，也不要假设 SEA 外部 plugin 能解析任意 `@deepseek-ai/*` bare import；现有无依赖 `.mjs` 可直接使用 `ctx` 与 Node 内置模块。

建议工具按实际已实现业务接口注册，名称以下列语义为例，非宣称现已存在：

| 工具族 | 服务器可信绑定 | 不允许模型扩大 |
| --- | --- | --- |
| 课程搜索／文档片段／文档区域 | 当前提交 course、source snapshot/version、允许页／区域；返回 SourceRef、角色和覆盖 | 课程 ID／磁盘路径／新版来源／视频帧；文档区域许可不得变成补视频图片 |
| 附近字幕 | 提交时冻结的视频、source_version、问题毫秒时间点和允许窗口 | 浏览时不断变化的播放器时间、整课读取、全局字幕搜索 |
| 完整任务单位枚举／读取 | task scope revision、冻结 unit ID、有效字幕修订、覆盖清单 | 任意课程、未列单位、来源缺口时偷偷联网 |
| 成果创建／指定块修改 | 明确任务或明确修改动作、允许 artifact ID、expected revision、operation ID | 任意文件写入、覆盖用户新版本、普通聊天自动建成果 |
| 外部检索／读取 | 明确外部研究 grant、任务允许网络范围；后台受控外部客户端 | 课程未命中即联网、系统／局域网地址、直接启用通用 web／shell |

桥可以复用 Boundary 所有的 loopback HTTP server，新增精确私有业务路由及独立 token；或者使用一个独立的项目私有 loopback server。前者无需扩大 guard 的允许 origin，后者需精确增加 bridge origin。两者都要独立于 `/v1/messages` 计费路径：业务读取／写入不是 provider attempt。

最小请求由工具 body 构造 `{tool_name,session_id:exec.agent.id,call_id:exec.callId,grant_id,arguments}`。后台以 grant 查原提交／task／课程／冻结版本；模型不提交 token。写入 receipt 持久化，`(run_id,call_id)` 与 payload hash 共同去重；连接断开先查 receipt，未知时不得再写一次。返回只包含窄业务 DTO。不要把整个 FastAPI 的应用 cookie 或 provider key交给 Host 工具。

grant 通过已认证 Host control 新增的窄操作登记，在 prompt 前生效；当前 control 仅 `GET /state`（`learning-guard.mjs:153`），尚无此能力。它应固定 `{run_id,session_id,epoch,scope_revision,allowed_tools,model,thinking,max_output_tokens,timeout_seconds}`，不暴露给 renderer。任务取消／完成后封闭写入；迟到结果依旧绑定原课程并按预期版本判断，不追随当前 UI 课程。

文档局部图片不能仅返回路径或 `{type:'image',data:...}` 冒充原生 durable block。固定源码 `packages/attachment/attachment/src/index.ts:96/:116` 提供 `saveImages()`／`admitPromptContent()`；`llm/llm/src/types.ts:79` 的 block 是 `{type:'image',attachment:ImageAttachmentRef}`，`llm-deepseek/src/serialize.ts:65` 可将工具结果中的此 ref 投影为图像。若采用该链，plugin 需明确依赖 attachments、仅保存后台允许的文档区域 bytes，并 separately 实测模型能力、Host 投影及 renderer 回放；本次没有这条运行证据。

## 原生工具续答与路由的已知边缘

`packages/core/agent-loop/src/agent.ts:549` 从 assistant message 取 tool-call，调用 `executeToolCalls()`；`tool-calls.ts:281` 持久化 `tool/result`；`:311` 的 turn loop 在结果后进入下一 step，再次请求模型。一次 step 中多个工具结果可以合入后续一次 Messages 请求。**应用不应收到 tool-call 后调用 `start()` 补发同一用户 prompt。**

`ExecutionAdapter.observe()`（`:406`）接受同一 `native_turn` 的多个 assistant-stream start，事件身份含 native attempt 和 chunk index；只在 `turn/end`（`:447`）发布逻辑终态。工具调用／结果暂没有归一化业务事件，但原生 Session 有完整日志。任务进度与成果发布不能只靠拼接正文推测，应从受控业务工具 receipt／任务存储获得。

当前 `MessagesBoundary._resolve_generation()`（`:444`）倒查最新 user turn 的 routing marker，并跳过**只包含 tool_result** 的 user message。新 prompt 的 marker 与初始 skill 目录属于相邻 user 内容，固定 serializer 会合并（`llm-deepseek/src/serialize.ts:124`），正常首问仍可归属。

存在必须覆盖的边缘：tool-result turn 额外混入目录替换或 `deferContext` 文本后，不再是纯工具结果，也没有 marker，会返回 `GENERATION_MARKER_REQUIRED`。本次纯函数实际结果为：

```text
first_catalog_merged -> g-A
pure_tool_result -> g-A
tool_result_with_deferred_context -> ValueError GENERATION_MARKER_REQUIRED
```

最小首条纵向闭环可冻结 Session 的 skill／schema 集合，并不使用 deferred context；工具数据放在其 tool_result 内。若需要中途更新，应通过可信执行元数据或受审查的 native pre-step 路由补全建立归属，再补相关负例。**不要简单跳过所有无 marker 的 user message**，否则新未标记用户输入会被历史 generation 认领，破坏已有 `test_official_requires_current_user_marker_not_historical_binding`。

此外，本项目声明 `toolUpdate:'addition-only'`；不要以在同一 Session 中不断撤销／换名 schema 作为权限撤销方式。schema 可见性和当前实际授权分开，执行 guard／后台仍须重新核验。

## 独立任务执行的小接口

当前 adapter `prepare()`（`:149`）只按 `sha256(conversation_id:epoch)` 派生聊天 Session；`start()` 的 `history_context` 和已有 Session 历史都会进入模型。独立翻译绝不能复用当前聊天 sid 或 fork 当前聊天；即便只把所选字串写进 prompt，也会带上该 Session 的既有材料。

建议提供借用现有 Adapter 的任务门面，复用内部执行能力，不再套一层 provider：

```text
prepare_task(task_id, unit_id, run_id, request_id, epoch) -> binding
start(binding, explicit_payload, model, thinking, policy) -> receipt
observe(binding, after) / reconcile(binding, after) -> events
cancel(binding) -> cancellation_intent
attempts_for(run_id) -> transport facts
```

- 将现 `prepare()` 中 follow／create／native cut 抽成私有 `_prepare_session()`；原聊天入口语义保持。翻译 sid 用 `translation:run_id` 派生，每个未命中缓存的翻译新上下文，`attachments=[]`、`history_context=None`、不授予业务读取工具；只发送选中文字与翻译任务固定指令／必要路由信息。
- 长任务 sid 可用 `task:task_id:epoch` 或按单位派生；单位级新 Session 更容易控制输入范围／上下文体积。两种都在同 Host。长任务本地 checkpoint 独立于 Session，不把整个后台任务变成不可分的一次长 prompt。串行生成依赖由 task scheduler 控制。
- `session/create` 原生参数是 `{sessionId?,cwd?,workspaceId?,agentPreset?}`；`cwd`／workspace 二选一，客户端绝不能提供任意 cwd。应保存 RPC 实际返回的 sid；原生创建可能在请求身份已不可复用时另建 Session（`session-controller/src/types.ts:288`、`commands.ts:111`）。
- `session/selectModel` 只接受 provider／model／reasoningEffort 和 sid；`session/prompt` 为 `{requestId,sessionId,mode:'queue',content,clientTimeZone}`。稳定 requestId在 Host 中用于去重（`commands.ts:319/:584`）。恢复未知不自动生成新 requestId；先 follow／page／inbox 对账。
- 任务 binding 持久保存实际 `session_id,request_id,run_id,epoch,native_floor,native_turn,accepted,status,host_instance_id` 及 task／unit 归属、输入 hash、冻结 scope revision、固定模型／思考／输出／超时／policy digest。首次 claim另保存 worker owner／lease或等价的独占事实。所有 stream／event／tool receipt按 run／epoch核对，task取消不调用 adapter.close。
- 全局 run ID 与聊天 generation ID不得冲突。现私有 marker 可暂沿用 `[[learning-generation:<run_id>]]`，其 generation 字样只是内部传输标签；业务 owner必须从可信注册表区分，不能用该名字伪造聊天行。后续统一命名可在内部适配时完成。

同实例多 Session 的结构证据：adapter 的 `_streams/_sequences/_attempts/_chunk_floors` 全按 execution routing ID隔离；已有 `backend/tests/test_execution_host.py:158` `test_concurrent_conversations_keep_provider_and_stream_ownership` 覆盖两 Session 不同模型／思考。此测试本次只读未重跑；“前台问答＋后台写工具＋翻译”的新组合还未实际通过。需在 task scheduler中限制后台并发、给予前台优先；Session隔离本身不会自动实现资源优先级。

### 按任务固定输出与超时

当前 `.start()` 只有 model／thinking可选；max_tokens／timeout 在 Host profile、Boundary与 observe上仍是实例全局值。`MessagesBoundary.bind()`（`:428`）只固定 `(model,thinking)`，`_configuration_matches()`（`:955`）拿实例 `self.max_tokens`校验。仅把任务参数存表不会让实际请求生效。

原生 **`agent/request` waterfall** 是小而正确的参数接缝（`agent-loop/src/agent.ts:593`），返回新 config发生在 `request/header`保存之前。例如在项目 plugin为该 agent注册：

```javascript
agent.ctx.on('agent/request', async (_payload, next) => {
  const config = await next();
  const policy = requireBoundRunPolicy(agent.id);
  assertSelectedModelAndThinking(config, policy);
  return { ...config, maxTokens: policy.max_output_tokens };
});
```

这是源码支持的建议，仍须在实际 EXE验证。**不能在 `llm/stream` 直接修改 `options.maxTokens`**：loop 在`:695`已经 `Object.freeze(request)`，且那时改参数会绕过原生持久 request/header事实。

Boundary把 run policy扩为不可变 `{model,thinking,max_tokens,timeout,...}`，以该 run值校验 max_tokens和设置实际转发 deadline；observe以该 run的总观察期限工作。Host provider `streamIdleTimeoutMs` 目前仍全局，需显式核它与任务期限的关系，不能宣称已经有原生每 Session provider idle timeout API。长任务宜以有检查点的有限单位执行，任务总期限与每次 provider attempt期限分开记录。

桌面另需避免活动工具续答时的模型选择竞态：`DesktopGateway.rpc('session/selectModel')`（`:549`）当前立即改原生 selection；下一 step可能更换当次 generation路由，继而被 Boundary固定选择拒绝。可在活动生成时只保存下一次提交的 UI选择，当前 run仍固定；待下次 `start()`应用。至少需要真实工具多 step验证，不能只用单次无工具回答测试判断无问题。

## 独立 run 账本与现有多 attempt 限制

可直接复用的现有逻辑：

- `boundary.py:78` `OutboundLedger` 对 dispatch_started先 fsync；`:1039` 每次真正进入 `HTTPSConnection.request()` 前记一条实际 attempt，不重试、不跟随 redirect；fixture不算真实出站。
- Boundary每个收到的 Messages都生成独立 `boundary-<uuid>` request ID，保存请求 hash／原始请求、SSE和 raw usage。计费 request与 Host native assistant attempt是不同身份；不要假定二者字符串相同。
- `UsageObserver`（`:158`）逐字段记录存在性，累计值覆盖而非相加，收到 message_stop才结算；缺失为 null。`_usage()`（adapter`:470`）为所有匹配请求产生独立 `provider:<request_id>:usage` settlement ID，迟到观察与最终结算有不同事件身份。
- `Repository._usage()`（`:1276`）允许未结算观察升级为最终事实，拒绝已结算后的冲突；`GenerationService.generation_view()`（`:502`）只在每个 settlement同字段均已知时求和。新任务可提取复用这些归一化纯逻辑，不复制一套把未知当零的统计。

必须补齐／明确的限制：

| 限制 | 事实位置 | 小改动建议 |
| --- | --- | --- |
| 非聊天 owner缺失 | `storage/schema.py:121` generation要求 submission／assistant_message／snapshot非空且唯一；usage与outbound_attempts FK generation（`:183/:194`） | 在尚未部署的0007新增 `task_execution_runs`、任务attempt、任务usage／event表；task_units指向自身 run。保留旧约束。统一查询 union时增加 `owner_kind/owner_id`，不重复累计同一个边界请求。 |
| Boundary恢复只恢复传输事实 | `boundary.py:298`读 ledger，`:327`仍把 bound IDs与 selection建成空集 | 从聊天 generation和任务run恢复 policy，**在 Host恢复／follow可能触发续答前**登记；不以当前 UI选择覆盖旧 policy。未知 dispatch记录先对账，不能当未执行。 |
| 业务attempt字段比Boundary少 | adapter`:509`返回 status／hash／时间；service`:422`只同步actual_outbound／request/model/thinking/kind，现表没有状态／hash／rawRef | 任务attempt保留完整 transport事实与来源ref，usage引用该request；`kind:'model'`之外保存任务角色／单位，不把工具条数当模型条数。旧账本可通过统一查询补出处，按实际需要再增列。 |
| 用量到 turn结束才收集 | adapter`:447/:470` | 很长工具回合或未知中断期间，任务执行层需同步已知边界请求和usage观察／settlement；最终仍去重。不能等任务全部完成才给用户留下可对账事实。 |
| 聚合还需核请求覆盖 | 现 `generation_view()`只聚合已存在usage行 | 新任务汇总先对比应计的实际attempt集合与settlement集合；有一个已出站请求尚无最终usage，整体即未知。不能仅对已到达的若干usage求和后宣称完整总量已知。fixture与real来源仍分列。 |
| 当前归一化没有工具状态事件 | adapter`:423`只投影text／reasoning，terminal来自turn/end | 从业务工具receipt获得持久单位进度／已保存产物，若需原生工具卡片状态再加窄事件；不能从正文“已保存”推断数据库完成。 |
| 参数及来源白名单未在 Boundary注册 | `bind()`仅model／thinking | register_execution 接收可信sid／epoch／scope／schema digest／max_tokens／timeout；非聊天run不能利用fixture FIFO fallback。 |
| 源数据／工具图片实际出站集合缺少按run检查 | 当前Boundary `_image_summary`主要记hash | 比对当前run允许历史／文档区域hash集合；来源、选区、工具读取、视频帧禁区分开审计。不能把工具产生的新图像默认为用户截图许可。 |

建议 TaskExecutor只依赖 `TaskRunStore.claim/persist_binding/ingest/record_attempt/settle/get`、来源／资产业务Interface和借用的Adapter。`claim()`先以持久CAS决定是否允许首次派发，再调用 `.start()`；结果未知路径只 `.reconcile()`。块保存／索引／任务单位完成由事务与receipt衔接，不让会话正文成为唯一checkpoint。

Audit最小集合是：固定EXE hash／policy版本、run与task／unit归属、Session／native turn、每次真正模型dispatch的request hash与实际参数、可见工具及schema digest、工具call ID／原grant／允许源版本与范围hash、拒绝码、写入receipt／结果修订、取消intent／观察终态、usage来源和字段存在性。系统提示与素材可存hash和私有raw引用；不得把正文、磁盘路径、token或provider认证头放进公共诊断。`/state`分别报告全局已注册能力与按Session审计的实际可见能力；两者不同不应误判成未注册。

## 本次已执行的验证与后续门槛

本次实际执行均为只读或进程内纯函数，未启动Host：

| 验证 | 命令／方式 | 结果 |
| --- | --- | --- |
| 工作区／diff | `git status --short`；`git diff --numstat -- .../adapter.py .../host.py`；相关diff | passed；保留已有修改。 |
| ZIP／EXE身份 | PowerShell `Get-FileHash -LiteralPath <上述固定路径> -Algorithm SHA256` | passed；与锁相等，值见上表。 |
| 核心源码与锁定ZIP | PowerShell here-string管道至 `& '.\backend\.venv\Scripts\python.exe' -B -`；`zipfile.ZipFile`读取对应member，与当前源码 `read_bytes()`比较 | passed；5＋14个审计文件相等，其中含skill／tools／loop／Remote／serializer／UI skill与LICENSE。没有展开或改写ZIP。 |
| marker边缘 | 同一 `python -B -` 进程，`MessagesBoundary.__new__()`仅配置mode/bound集合，对三种合成messages调用 `_resolve_generation()` | passed（确认现有正常分支与明确失败分支）；混合tool-result上下文拒绝，结果见正文。 |
| usage累计与缺失 | 直接构造 `UsageObserver('proof-attempt')`，feed input=3、两次累计output=5及message_stop | passed；结算input=3、output=5，raw保留两次5；reasoning／cache字段均未出现且为null，settlement_count=1。不是模型实际用量。 |
| 新skill／任务真实Host链 | 未运行 | not_run。 |
| 真实模型／真实活动工具停止／Windows实窗／用户学习 | 未运行 | not_run。 |

接入后应先在独立fixture实例验收以下链，再做授权范围内真实模型验证：

1. **原生发现双路径**：普通课程问题使模型调用 `skill`再读资料；同一输入用 `/name`直接加载；检查实际请求、原生目录和工具结果，而不是只看plugin state。
2. **工具续答与记账**：skill→读取→窄写入→最终答复；同step多工具合成一次后续请求，每个实际HTTP各一attempt／settlement。加入目录更新／deferredContext边缘，拒绝新未标记用户输入借旧marker。
3. **同Host隔离**：活动聊天A＋仅选区翻译B＋课程长任务C；B实际messages不含A历史或选区外文本，C不会修改当前聊天；取消B/C仅影响各自Session，Host和A仍活动。
4. **权限负例**：跨课、旧source_version、额外path、未确认图片／视频帧、同名tool shadow、无grant、过期task scope、已手改artifact版本、普通问答写资产、模型代用户作答，均在body或事务前拒绝并有稳定错误码。
5. **故障恢复**：派发已接受未回执、工具写入已提交未回执、模型续答结果未知、块保存后索引失败、Boundary重建／run policy恢复、旧epoch迟到事件；恢复不新增未知模型请求、不重复写入／评分。
6. **参数与活动停止**：每个task实算出站model／effort／max_tokens；生成中和工具执行中停止均有实际信号与终态，raw usage未知如实保留。原生UI在活动turn改模型仅影响下一次提交。

未覆盖／unknown：`332895e` 实际包对新增 `ctx.skills.register`／tool-skill显式调用／`skills/list`及agent/request参数hook的完整兼容；工具结果图片的正式附件投影；新增组合的前台优先、性能、任务恢复与真实学习效果。已有注册工具的历史fixture只减少接口试错，不能替代这些门槛。

许可：固定源码顶层 `LICENSE` 为 MIT、Copyright (c) 2026 DeepSeek；若提取实质源码，保留版权及许可文本并更新正式来源清单。只调用已固定包的原生接口，不需要迁入其整套UI／VFS／provider；新增 `.mjs` 可沿用现正式部署闭包，无需用户额外安装上游开发工具链。
