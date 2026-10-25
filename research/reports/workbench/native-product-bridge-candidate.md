# 当前正式 Harness 的原生业务桥集成候选

日期：2026-10-23。状态：**独占候选完成，未写正式产品。** 基于当前正式 guard、runtime.lock、profile 和 execution 代码冻结 15 个文件，合并原生业务工具、generation lease、精确 provider attempt 关联。最终 r4 用实际 `ExecutionAdapter.open/prepare/start`、同一固定 SEA、真实私有 Python HTTP handler 和独占 SQLite 业务 Port 完成 11 场景、22 次本地 Messages；167 项保留证据检查与既有 29 项 Boundary 测试通过，真实 provider 请求 0。

完整代码在 [独占候选](../../../.local/cache/workbench/native-product-bridge-candidate)。[integration.patch](../../../.local/cache/workbench/native-product-bridge-candidate/integration.patch) 已分别对冻结 baseline 和当前正式工作区执行 `git apply --check`，均通过，**没有 apply**。冻结时至生成补丁时这 15 个正式文件 hash 无漂移，记录见 `baseline-current-drift.json`。补丁只含正式目标路径，不含 runner、SQLite fixture Port、缓存路径或测试控制端点。

没有修改／替换 TaskRunStore、Repository、数据库迁移、依赖或正式锁；仅候选 runtime.lock 更新自有源码 hash 和能力描述，SEA／源码锁定身份未变。没有用户材料、provider key、真实模型或用户 Host。

## 可审查文件

| 候选正式路径 | 作用 |
| --- | --- |
| [learning-guard.mjs](../../../.local/cache/workbench/native-product-bridge-candidate/candidate/harness/plugins/learning-guard.mjs) | 静态 `course-read` 原生 skill，固定 read/stage 两工具，精确 definition／schema、角色白名单、source gate、按 agent 隐藏及执行校验。 |
| [native-lease.mjs](../../../.local/cache/workbench/native-product-bridge-candidate/candidate/harness/plugins/native-lease.mjs) | user RPC／marker、原生 turn/step/callSeq 与 Symbol token owner；immutable run history、已知终态释放、unknown 闭锁。 |
| [native-attempt.mjs](../../../.local/cache/workbench/native-product-bridge-candidate/candidate/harness/plugins/native-attempt.mjs) | 一次原生流的 AsyncLocalStorage、私有 dispatch nonce、Boundary attempt 回执、原生 assistant 提交向量关联。 |
| [native_tools.py](../../../.local/cache/workbench/native-product-bridge-candidate/candidate/backend/src/learning_app/execution/native_tools.py) | 真正的私有 loopback handler、归属／回执 journal，以及接 Repository 的窄 Protocol。不是旧 FakeBridge。 |
| [provider_tool_calls.py](../../../.local/cache/workbench/native-product-bridge-candidate/candidate/backend/src/learning_app/execution/provider_tool_calls.py) | 从实际原始 Messages SSE 解析工具 ID、ordinal、名称、参数 hash。 |
| [Boundary](../../../.local/cache/workbench/native-product-bridge-candidate/candidate/backend/src/learning_app/execution/boundary.py) | 在真实入站行登记 nonce→attempt，观察 provider SSE，返回自己的 attempt header；原始 usage 路径保留。 |
| [Host](../../../.local/cache/workbench/native-product-bridge-candidate/candidate/backend/src/learning_app/execution/host.py)／[Adapter](../../../.local/cache/workbench/native-product-bridge-candidate/candidate/backend/src/learning_app/execution/adapter.py) | 私有 bridge 参数、scope 登记、release 回执与复用已保存 native sid 的明确签名。 |
| [profile](../../../.local/cache/workbench/native-product-bridge-candidate/candidate/harness/profiles/learning.patch.json)／[runtime.lock](../../../.local/cache/workbench/native-product-bridge-candidate/candidate/harness/runtime.lock.json) | 恢复 own `tool-skill`，关闭 sandbox、pwsh-sandbox、skill-filesystem；更新自有源码 hash。 |

补丁约 1297 行，包含新增模块及原代码上下文。它不增加插件扫描、通用调度平台、工具包或第二个供应商执行层。正式运行只能迁入正式路径，不能引用本缓存。

## 精确 run、attempt 与 tool-call 关联

前一份 lease 报告的“同一 run 内重复 ID/参数仍只能歧义拒绝”已在本候选补通。固定源码原生 exec 不提供 provider attempt ID，不能从 `callId` 反推；本候选也没有改写消息正文／删除历史来补。

真实链路为：

```text
agent/request（prepend:true）冻结本次原生 turn/step 与用户选择
llm/stream source gate 核真实 user rpcId + 独立 marker
  AsyncLocalStorage 为这一次流建立 dispatch nonce
    既有 fetch guard 只在该原生流的 /v1/messages 添加 nonce
      Boundary 经本地认证及 generation/Session 绑定，创建真实 attempt 行
      Boundary 返回自己的 attempt_id + nonce 回显；不转发这些头给 provider
    Host 保存 response header，按原生 block-end 收集工具向量
  assistant/message 提交时核 turn/step、唯一未决流、完整工具 ID/name/args 向量
    tool/call 事件 seq → tools.guard 的 Symbol token → 冻结 owner
      Python bridge 核 run/request/epoch/dispatch/attempt/ordinal/id/name/argsHash
```

nonce／attempt header **只作关联，不授权读写**。原有 Boundary token、独立业务 token、source gate、固定工具定义、scope 和 Repository 检查都保留。header 由 Host／Boundary 私有流程生成；模型输入不能声明 owner。两端均拒绝重复 dispatch、无匹配原生流、缺少 attempt 回执或向量不符。

r4 故意让同一 run 的 step1 和 step2 都返回 `reused-provider-id`、同一个工具名、完全相同参数。实际原生 callSeq 为 15／21，工具 ordinal 为 1／0，分别绑定两个不同 Boundary attempt 和 nonce。另有同 Session 下一 generation 的同 ID。所有允许调用都核同真实 SSE，而不是用 runner 的“期望调用表”代替。

Python registry 建立双向约束：`(sid,native_call_seq)` 只绑定一个精确 provider 调用；`(attempt_id,tool_ordinal)` 也不能绑定第二个原生身份。实际替换同 run attempt 得到 `NATIVE_CALL_BINDING_CONFLICT`，伪造第二个 native seq 得到 `NATIVE_PROVIDER_CALL_ALREADY_BOUND`，跨 run 借旧 attempt 得到 `NATIVE_EXACT_ATTEMPT_MISMATCH`。

## Repository 的窄接口

真实 transport 位于 `native_tools.py`，只绑定自有 IPv4 loopback 端口，使用独立私有 token。只开放 POST `/bridge/invoke` 和只读 `/bridge/release-check`，拒绝 Origin、Host 不符、非精确 Content-Length、Transfer-Encoding、额外 envelope 字段和未登记调用。

接入点是三个方法：

```python
class RepositoryTools(Protocol):
    def authorize(self, call: NativeCall, arguments: dict) -> None: ...
    def invoke(self, call: NativeCall, arguments: dict) -> dict: ...
    def assert_releasable(self, lease: dict) -> None: ...
```

`NativeCall` 是冻结 dataclass，含 sid/run/request/epoch、attempt/dispatch/ordinal、native turn/step/callSeq、tool ID/name/args hash。模型参数只有：

```text
read_source_handle({handle_id, source_version})
stage_result({task_id, object_id, expected_version, stage, body})
```

`authorize` 必须重新查询真实 DB 的当前 run、epoch、课程、exact tools、handle 与 source version／范围；**缓存回执也执行此检查**。`invoke` 在真实业务事务内再次核授权及版本，使用 TaskRunStore／SourceStore／ArtifactStore 实施受限读取、稳定业务 operation ID、对象版本 CAS、SourceRefs 与成果状态；不能把 native ID 当业务 operation ID。`assert_releasable` 拒绝任何未对账业务效果、待提交回调或 unknown。

已知且未产生不明效果的业务拒绝应由 Repository adapter 转为 `ToolRejected(code,status)`。未分类异常按 unknown 处理。transport 在调用业务实现前 fsync 一条 unknown 回执，成功后才记录 completed。已有 completed 回执可以对账返回，但不重新执行业务操作；unknown 回执不重发。实际撤销 fixture DB 里 run 权限后，同一个缓存回执也被 `REPOSITORY_RUN_NOT_ACTIVE` 拒绝。

本地 runner 的 `FixtureRepositoryPort` 使用**独占合成 SQLite**，只为真实 HTTP transport 验证授权拒绝、CAS、幂等及 unknown。它不进入补丁，也不等于生产 Repository 授权验收。真实 SourceRef 范围、单位成果关系和持久 outbox 仍由根代理接入已有业务表；TaskRunStore 保留原实现。

## Adapter 调用顺序及 scope

新增参数是 `ExecutionAdapter(..., native_tools=transport)`、`start(..., policy=..., business_scope=...)`、`register_execution_policy(..., scope=...)`、`restore_execution_policy(..., scope=...)` 和 `release_execution(binding)`。Scope 仅含 course_id、purpose、task_id、allowed_tools，由业务 DB 创建；每一条模型请求都必须先登记其冻结 policy/scope。缺省不能推断为获得课程读取或写入能力。

示意接线：

```python
transport = NativeToolTransport(registry, repository_tools).start()
adapter = ExecutionAdapter(repo_root, runtime_dir, native_tools=transport, ...)
await adapter.open()
binding = await adapter.prepare(..., session_id=stored_native_sid)
await adapter.start(binding, submitted_text, policy=frozen_policy,
                    business_scope=authorized_scope, ...)
```

transport 的启动／关闭由所属 backend runtime 管理，Adapter 不关闭共享业务服务。普通 QA 的新生成要显式沿用 DB 保存的 native sid；候选 `prepare` 的默认值也固定为历史 epoch1 身份，不再按本轮 epoch 新建聊天。已有其他历史 sid 必须明确传入，候选不迁移／合并旧日志。任务／翻译仍用 fresh 隔离 Session。

实际 Adapter 会先 prepare/create agent，start 才配置 policy。候选私有 `/executions` 因此会 await 同 agent 的 `configureAgentSurface`：先安装空 allow barrier，再 dispose 旧 skill Fiber／restriction，安装 exact allow 与 skill 可见性，最后移除 barrier。相同 scope 重复登记共用 ready，不叠加 restriction。r4 实际先产生 `tools=[]/skillSuppressed=true` 的 agent，随后同 agent 配置为三个授权工具，实际 read→stage→completed。

`agent/request` 的 `{prepend:true}` 必须保留，否则原生 model-selection 外层在项目校验之后才覆盖模型。r4 同 sid 第二轮实际出站 pro/high/1536，第一轮 flash/off/1024；不是只看 `selectModel` 返回值。

## 配置与关闭边界

开启 `tool-skill`，保留原生 skill registry 与 working-directory 服务；关闭 `skill-filesystem`、`sandbox`、`pwsh-sandbox`。其余文件、Shell、PTC、subagent、Web、MCP 等禁用项保留。禁用 sandbox 同时避免其隐式 dsh-windows-acl skill provider；不提供假的 runnerCommand。

仅静态注册 own `course-read`，按需模型 `skill` 或用户 `/course-read` 调用。普通 QA 不自动执行工具、不读材料。translation 用 fresh Session、`tools.restrict({allow:[]})`、注入作用域内禁止 model/user invocation 的同名 skill。它保留 working-directory 服务，但在 learning-task-* agent 的同名 required context 提供空文本。普通 QA history／runtime／catalog 均不删除。

新 guard 必须与真实 transport、scope 登记及相应 Host/profile 同时接线；未配置业务 endpoint 的旧 Host 构造不是启用后配置。本补丁不提供“登记失败则放宽工具”的 fallback。根代理应先接 RepositoryTools，再启用这组变更。

## 释放、取消与恢复

`release_execution` 必须先取得原生 completed、完整 tool results、无原生 attempt unknown、业务 release-check 通过和实际 `ctx.sessions.flush` 的回执。Boundary／registry 核匹配 sid/run/request/epoch 后标记旧 lease released；新 epoch 才能追加。release 到新 bind 的间隙拒绝请求。

原生 completed 不代表业务效果已知。实际 unknown 场景让真实 Port 调用在记录 unknown 后异常；原生最终仍 completed，但 release 返回 `NATIVE_OPERATION_UNKNOWN`，新 owner 登记返回 `LEASE_PREVIOUS_NOT_RELEASED`。重开 journal 后重放该调用仍拒绝，Port 调用数不增。

实际生成中 cancel 使用原生 `session/cancel`，得到 aborted；Host 随后继续完成 translation。没有用杀 Host 代替停止。此场景取消的是模型 stream，**不声称已验证进行中 stage／业务 IO 的取消**；业务迟到提交仍须 Repository 的 epoch／version CAS。当前候选保守保留 cancel/error 的 lease，不能直接标成可重新提交；后续已知终态对账由业务层实现。

暖恢复只登记同 policy 并 follow/page：Messages 22→22、Port invoke 11→11。随后实际关闭本探针所属 Host／Boundary／transport，复用原生 home 和 fsync journal，创建新的私有 token、Host 与服务，读取 QA／unknown／cancel 原事件；仍 22→22、11→11，新 prompt 0，新 Messages 0，归属／operation 账逐项相同。没有把恢复登记变成提交。

## 验证矩阵与保留范围

| 场景 | 最终 r4 证据 |
| --- | --- |
| 普通 QA | 1 次 Messages，0 业务读／工具调用。 |
| 同 sid QA 两轮 | 3+3 次；模型／思考不同，重复工具 ID 被 exact attempt 区分。 |
| 真实 Adapter task | prepare 后登记 scope，read→stage→completed，合成 AI 成果仅 1 条。 |
| cross-course、wrong-object、wrong-version、wrong-stage | 各 2 次；真实 transport 到 Port 后分别拒绝，未增加成果。 |
| unknown | 2 次；实际业务调用 unknown，释放／接管／恢复重放均拒绝。 |
| 进行中 cancel | 1 次；原生 aborted，Host 存活，lease 不被接管。 |
| translation | 1 次；选区文字保留，tools/list/catalog/invocation/cwd 均隐藏，业务读 0。 |
| 新无 marker 用户 | 在已有两轮 marker／skill 历史的同 sid 内拒绝，22→22。 |
| 缺少业务认证、浏览器 Origin、额外 course 元数据 | handler 拒绝；不进入业务实现。 |

原始证据在 `native-product-20261023-r4/`，每次 run 都冻结当次 guard、helper、Python执行代码和 runner。r1 基础精确关联、r2 双向唯一／撤权／无 marker、r3 late scope surface、r4 实际 Adapter 路径均保留，没有覆盖前次证据。此前 skill／continuation／lease 报告中的历史失败也未删除。

最终 [verification.json](../../../.local/cache/workbench/native-product-bridge-candidate/verification.json) 167 项通过；[boundary-test-results.json](../../../.local/cache/workbench/native-product-bridge-candidate/boundary-test-results.json) 29 项通过，Node syntax／Python compile 通过。真实 SSE 按 1、31、8192 字节分块重放，调用向量一致。主 Host 与重建 Host 所属 PID 均经各自 Windows Job 清理，无 remaining PID；最终 Host/control/Boundary/transport 端口均实测关闭。

固定 SEA SHA256 `f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365`；参考源码 `687f282e8625c6b8d11ff58211a90524be2e4317`，ZIP SHA256 `828bbbebaf507d2b1fc335a608e1c8174e2dd5c5c80ebb64d5146ddc5ecb511f`。关键源码重新与锁定 ZIP 逐字节核同，发行制品不冒充同 build 源码。

未覆盖：真实 provider／用户资料、生产 Repository 授权与事务、account route 的模型调用、PTC、模型隐式 retries、进行中业务写入取消、取消／错误 lease 的业务对账释放。图片转换 allowlist 不在本主候选中，未放宽 `allowed_image_sha256`；另按根代理要求独立调查文档页原生派生证明。
