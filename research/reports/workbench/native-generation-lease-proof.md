# 同一原生 QA Session 跨 generation 的调用归属实证

日期：2026-10-23。状态：**独占候选与固定 SEA 本地验证完成，未改产品文件。** 同一个原生 Session 连续两轮 completed；最终 r3 共 5 次本地 Messages，65 项保留证据检查通过，另有 9 项合成事件形状检查。真实 provider 请求 0。

结论：可以复用普通 QA 的原生 Session，但必须将 **Session 上的当前 lease** 与 **每次工具派发时冻结的 owner** 分开。`exec.callId` 实际等于 provider `tool_use.id`，然而同一 Session 的两轮可以出现完全相同的 ID、工具名和参数。`(sid, callId)` 不能作为调用唯一键；本次原生 call 事件序号 15、34 将它们明确区分。原生 `exec.token` 是同进程 Symbol，可将 guard 已验证的调用 owner 传到 handler。迟到 handler 不应再次按 sid 查找可变 policy。

所有代码及原始证据位于 [独占缓存](../../../.local/cache/workbench/native-generation-lease-candidate)。仅创建自己的 Host、Boundary、假业务桥、端口和运行目录；未读取用户素材或私有 provider 凭据，未重启用户 Host。假桥只读取合成 source，未产生任何 staged 成果。

## 证据与候选入口

| 路径 | 内容 |
| --- | --- |
| [native-lease.mjs](../../../.local/cache/workbench/native-generation-lease-candidate/candidate/harness/plugins/native-lease.mjs) | 有限的 QA lease、同步原生事件投影、每 Symbol 调用冻结 owner、终态 flush/release。 |
| [learning-guard.mjs](../../../.local/cache/workbench/native-generation-lease-candidate/candidate/harness/plugins/learning-guard.mjs) | 上一份业务候选上的最小接入；原工具定义、精确身份／schema guard、source gate 仍保留。 |
| [probe.py](../../../.local/cache/workbench/native-generation-lease-candidate/probe.py) | 真正固定 SEA runner、Boundary 局部子类、只读假桥；这里的 HTTP 调度／业务库均不是产品实现。 |
| [provider_tool_calls.py](../../../.local/cache/workbench/native-generation-lease-candidate/provider_tool_calls.py) | 从实际 Messages SSE 的 start/delta/stop 被动记录原始工具 ID、参数 hash、ordinal。 |
| [candidate.patch](../../../.local/cache/workbench/native-generation-lease-candidate/candidate.patch) | 相对本目录 `baseline/` 的 guard 修改及新增 helper；不是当前正式 guard 的直接覆盖补丁。 |
| [verification.json](../../../.local/cache/workbench/native-generation-lease-candidate/verification.json) | r3 的 65 项证据核查；含本次 owned PID 清理结果。 |
| [provider-calls-observed.json](../../../.local/cache/workbench/native-generation-lease-candidate/provider-calls-observed.json) | 实际 SSE 工具调用账；没有认证令牌。 |
| [unit-verification.json](../../../.local/cache/workbench/native-generation-lease-candidate/unit-verification.json) | 9 项合成事件形状检查，含 TOOL_OUTCOME_UNKNOWN；不冒充实际 SEA 崩溃恢复。 |

`baseline/` 冻结前一份 native-business-candidate 的 11 个源码／配置文件；其中正式业务桥原本尚未集成。`candidate.patch` 已对这份冻结 baseline 执行 `git apply --check --directory=.../baseline`，只检查未 apply。正式集成应迁入选定的小接口，不得 import 缓存或把整份旧 guard 覆盖当前产品修改。

固定 SEA SHA256：`f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365`。参考源码：`687f282e8625c6b8d11ff58211a90524be2e4317`，ZIP SHA256：`828bbbebaf507d2b1fc335a608e1c8174e2dd5c5c80ebb64d5146ddc5ecb511f`。下述 tools、tool-calls、session、model-selection、repair 源文件均重新与锁定 ZIP 逐字节核同。**发行 SEA 与参考源码不当作同一 build；兼容结论来自本次实际制品。**

## 原生工具执行实际能得到什么

固定源码 [ToolExecutionInput](../../../.runtime/desktop/upstream/packages/core/tools/src/index.ts)、[ToolRunContext](../../../.runtime/desktop/upstream/packages/core/tools/src/index.ts) 与实际 r3 guard 观察一致：

```text
agent, arguments, callId, concludeTurn, deferContext,
name, rootCallId, signal, token
```

`agent.id === agent.session.id`；可以访问 `agent.session` 和 `agent.ctx`。`arguments` 是 registry 截取、冻结的 JSON；`signal` 是取消信号，没有公开 controller。实际 exec **没有** `source`、`rpcId`、turn、step、provider attempt ID 或业务 run。`rootCallId` 在本次原生直接工具中等于 `callId`。PTC 没有启用；本报告不外推 PTC／嵌套调用。

[createExecutionToken](../../../.runtime/desktop/upstream/packages/core/tools/src/index.ts) 为每次执行创建新的 Symbol。实际 guard 的 4 次已允许调用拿到 4 个不同 token；3 次业务 read 的 handler 均通过同一个 token 找到原冻结 owner。handler 内新查 `executionPolicies.get(exec.agent.id)` 的旧写法已被移除。

`token` 只作同进程关联，不序列化、不作为 bearer 权限、不承诺重启后保留。持久调用身份使用 `(native_sid, native_call_seq)`；工具输出归属还携带冻结 `run_id/request_id/epoch/turn/step`。

## 持久调用事件先于 guard：可迁入的小接口

[executeToolCalls](../../../.runtime/desktop/upstream/packages/core/agent-loop/src/tool-calls.ts) 直接设置 `callId: block.id`。其 [startCall](../../../.runtime/desktop/upstream/packages/core/agent-loop/src/tool-calls.ts) 的顺序是：

```text
assistant/message 已提交（包含本 step 原生 tool-call blocks）
session.append('tool/call', {turn, step, callId, name, arguments})
tools.prepare → tools/pre-execute → monotonic tools.guard
tools.dispatch → tools/execute → tool.execute(args, exec)
tool/result（sourceEventSeqs 引用 call 事件序号）
```

[session/event](../../../.runtime/desktop/upstream/packages/core/session/src/index.ts) 是提交后同步通知，监听器异常被原生容纳，**不能用它 throw 来撤销已提交事件**。候选只在该事件钩子内建立只读归属投影；投影失败置 invalid，后续工具 guard 拒绝。没有扫描 `snapshotEvents()`，没有修改／删除日志。

本次实际投影链：

```text
turn/start(turn)
  user/message source.kind=user + rpcId + 精确独立 marker block
    assistant/message(turn,step,id,tool-call[])
      tool/call(turn,step,seq,callId,name,raw arguments)
        tools.guard(exec.token) 冻结 owner
          handler 只读 token owner，核 active lease 后调用业务桥
```

`user/message` 自身不带 turn；必须用事件位置和 `turn/start` 边界关联，不能假设该字段存在。skill invocation、runtime-context、model-selection 等 user-role 消息均不授予业务权限。候选只认该轮真实 `source.kind=user`，RPC ID 和 marker 同时匹配已登记 immutable policy。

[claim](../../../.local/cache/workbench/native-generation-lease-candidate/candidate/harness/plugins/native-lease.mjs) 核相同 agent/session 对象、当前 turn、唯一未消费 call 事件、call ID、工具名、参数 hash、前置 assistant block 和当前 lease，随后以 `exec.token` 保存 `Object.freeze(owner)`。原定义身份／封闭参数校验仍先于 claim。

handler 可迁入的关键变化在 [invoke](../../../.local/cache/workbench/native-generation-lease-candidate/candidate/harness/plugins/learning-guard.mjs)：

```js
const owner = leases.owner(exec); // 校验同 token、agent/session、id/name/args
leases.assertActive(owner);      // 旧 owner 已释放，立即拒绝；不重命名为新 run
const policy = owner.policy;     // 不查当前 sid policy 来决定调用归属
// 用冻结 policy 及 native turn/step/callSeq 构造私有业务 envelope。
```

`/probe/late`、`/probe/dispatch` 和保存旧 callback 是本地负例注入，**不得迁入正式控制面**。真实 tool body 必须 await 其工作到收尾。原生 [runGroup](../../../.runtime/desktop/upstream/packages/core/agent-loop/src/tool-calls.ts) 会排空已启动调用；[dispatchToolBody](../../../.runtime/desktop/upstream/packages/core/tools/src/index.ts) 也不会因取消放弃仍在执行的 body。因此“第一轮已 completed、第一轮原生 handler 仍未返回”的情况不能当成正常原生生命周期。本报告的迟到 callback 是有意保存的外部回调模拟；它实测保留旧 token owner 并拒绝，不冒充一次仍运行中的原生工具。

## 同 sid 的 lease 追加与释放

[accept](../../../.local/cache/workbench/native-generation-lease-candidate/candidate/harness/plugins/native-lease.mjs) 不再把 sid 永久绑定一个 run，而保留 immutable run history 和当前 sid 指针。当前 run 未 released 则任何新 run 冲突；同 run 同 policy 重新登记只对账。released 旧 run 的重新登记不激活它，也不把当前指针改回旧 run。

本候选仅支持同课程 QA 的同用途、同 exact allowed_tools 续轮；新 epoch 必须增大。model、thinking、max output、request、run 可以逐轮改变。它没有扩展为同一 Session 切任务／课程／权限等级。

[release](../../../.local/cache/workbench/native-generation-lease-candidate/candidate/harness/plugins/native-lease.mjs) 接收确切 sid/run/request/epoch，核该 owner 的原生 `turn/end completed`、所有 tool/call 已有 result、无 TOOL_OUTCOME_UNKNOWN，再实际调用 [ctx.sessions.flush(session)](../../../.runtime/desktop/upstream/packages/core/session/src/index.ts)。没有 durability listener、flush 失败、终态变化或身份不符均不释放。r3 实际 flush 返回参与者，产生 `native_turn=1, terminal_seq=22, durable=true` 的释放回执。

Boundary 的局部候选 [release_native/bind](../../../.local/cache/workbench/native-generation-lease-candidate/probe.py) 只在匹配释放回执后允许追加新 route。旧 generation 的模型 policy、raw usage、attempt 仍保留；release 到新 bind 的间隙拒绝请求。切换后，旧 tool_result continuation 的祖先 marker 与当前 owner 冲突，实际被 `ROUTE_MARKER_OWNER_CONFLICT` 拒绝。

**Boundary 回执不是业务授权。** 假桥还单独保留业务 unknown 标记：即使原生 turn 已 completed，假业务账仍 unknown 时，`rotate` 拒绝 `BUSINESS_OUTCOME_UNKNOWN`，不注册新 owner、不新增 prompt。此项是本地业务状态模拟。另以固定 repair 的 TOOL_OUTCOME_UNKNOWN 事件形状重放，核 native release 与新 policy 都拒绝；它不是实际杀 Host 产生的崩溃证据。

正式实现必须在已有业务 execution/scope 事务内串行化同 sid 的 handover，核 DB 终态及所有业务 operation 是否已知，再请求原生 flush/release、更新 Boundary/guard 和提交新 request。跨进程部分完成要保留待对账状态；不能因 Host ready、断连、页面关闭或旧 HTTP 已结束就接管 unknown。候选只放行 completed，尚未定义 error/cancel 的安全释放条件。

## 跨轮模型切换的确切 hook 顺序

r1 暴露旧 guard 的真实兼容错误：原生 `session/selectModel` 第二轮成功选 pro/high，guard 的 `agent/request` 却先读到上一轮 flash seed，触发 `LEARNING_EXECUTION_SELECTION_MISMATCH`，第二轮未出站。

参考 [installModelSelection](../../../.runtime/desktop/upstream/packages/core/agent/src/model-selection.ts) 在其 `agent/request` waterfall 的 `await next()` 之后才覆盖 route。后注册的项目 guard 默认处于它内部，过早校验 seed。r2/r3 使用以下最小修正，实际 pro/high 出站通过：

```js
agent.ctx.on('agent/request', async (_payload, next) => {
  const config = await next(); // 已经过原生 model-selection 覆盖
  const policy = activeLeaseFor(agent.id);
  assertModelMatches(config, policy);
  return { ...config, reasoningEffort: policy.thinking,
    maxTokens: policy.max_output_tokens };
}, { prepend: true });
```

位置见 [候选 guard](../../../.local/cache/workbench/native-generation-lease-candidate/candidate/harness/plugins/learning-guard.mjs)。它保留每轮用户的原生选择，不切新 Session，也不把旧模型 mismatch 放宽为无条件通过。实际第二轮还保留原生 `source.kind=model-selection` notice。

## 实际两轮结果

原生 sid 始终为 `learning-qa-lease-proof`；`session/create` 只执行一次。

| 项目 | 第一轮 | 第二轮 |
| --- | --- | --- |
| run／request／epoch | lease-run-1／lease-request-1／1 | lease-run-2／lease-request-2／2 |
| 实际模型与思考 | deepseek-flash；thinking disabled | deepseek-v4-pro；thinking enabled + effort high |
| 实际 max_tokens | 1024 | 1536 |
| 本地 Messages 次数 | 2 | 3 |
| 工具 | 原生 skill + hA 只读 | hA、hA2 只读；强行 stage 请求被 guard 拒绝 |
| 共有 provider ID | shared-provider-call | shared-provider-call |
| hA 原生归属 | turn1／step1／callSeq15 | turn2／step1／callSeq34 |
| 终态 | completed | completed |

第二轮使用用户 `/course-read`；目录、原生 invocation body、第一轮已提交 user 与 source 结果、模型切换 notice、runtime context 都保留。所有 5 次实际请求只公开 `read_source_handle` 和 `skill` 两个 schema，没有 stage_result。强制返回 stage 调用仍由 Host 执行 guard 拒绝，且假桥没有收到任何写操作；skill 正文／用户命令没有提升权限。

负例保留在 r3 result 和 guard audit：

| 注入 | 实际结果 |
| --- | --- |
| 第一轮最终 stream 尚未闭合，登记第二 owner | Host `LEASE_PREVIOUS_NOT_RELEASED`；Boundary `BOUNDARY_PREVIOUS_NOT_RELEASED`。 |
| 第一轮尚无 completed native 终态就 release | `LEASE_TERMINAL_NOT_KNOWN`。 |
| 假业务 outcome unknown 尝试 handover | `BUSINESS_OUTCOME_UNKNOWN`，新 prompt 0。 |
| 第二轮 active 时执行已保存的第一轮 callback | 同旧 Symbol 查得 run1/callSeq15，active=false，`LEASE_NOT_ACTIVE`；不向桥发送请求。 |
| 同 sid 手动 tools.execute，复用当前 call ID 和参数、但没有新 native call 事件 | 原生 materialized error `LEASE_CALL_SOURCE_AMBIGUOUS`；没有业务调用。 |
| 用私有桥认证重放旧 owner envelope | `EXECUTION_BINDING_DENIED`，没有借当前 run2 执行。 |
| 当前 route 上重放旧混合 tool_result 请求 | `ROUTE_MARKER_OWNER_CONFLICT`。 |
| 重复登记 active policy 与已释放旧 policy，然后 follow/page 对账 | 当前 owner 不变，Messages 5→5，业务事件不增，新 prompt 0。 |
| 同 sid 新无 marker 用户，历史仍含两轮 marker 和 skill body | Host source gate 拒绝 `LEARNING_CONTINUATION_UNMARKED_USER`，Messages 5→5。 |

## Provider 调用与原生调用账的实际边界

r3 Boundary 不是用 fixture 脚本预填一张“期望调用表”。它在实际 `_observe` 收到 SSE 字节时，解析 `content_block_start(tool_use)`、`input_json_delta`、`content_block_stop`，记下 `run_id + Boundary attempt_id + ordinal + original id/name/args hash`。业务 read 到桥前必须与当前 run 唯一原始调用匹配。保留 SSE 离线用 1、17、8192 字节分块重放，所得调用表完全一致，并逐项核同原生 guard 的 ID、名称和参数 hash。

但原生 assistant ID 是 Harness 自己的 UUID；本次 actual `source.replayState.response` 只有 kind/version/model，没有 provider `message_start.id` 或 Boundary request ID。**不能只凭 exec.callId 宣称已取得唯一 provider attempt。** 本次不同 run 的相同 ID/参数可用冻结 run 和 native callSeq 区分。若同一 run 的多个 attempts 重复相同 ID、名称和参数，本候选的桥会因多于一个匹配而拒绝；尚未证明该情形下 exact attempt 的一对一恢复。

正式 DB 的调用身份建议以 native `(sid, call_seq)` 为主，并保留 run/request/epoch/turn/step 和 provider `(attempt_id, ordinal, id, name, args_hash)`。不要给 `(sid, callId)` 加唯一约束，也不要用 provider ID 当业务幂等操作 ID。涉及 stage_result 时仍需 unit/operation 级稳定幂等键、对象版本 CAS 和真实来源权限，不能因 tool ID 相同／不同就覆盖或重发。

## 失败历史、清理与未覆盖项

r1 保留原 guard hook 顺序失败：1 轮 completed、2 次本地 Messages，第二轮 mismatch 后未到 Boundary。r2 保留 `{prepend:true}` 修正后的 2 轮成功证据。r3 在此基础上改为从真正 SSE 观察建立 provider 调用表，并增加 guard→body Symbol 对照，最终验证通过。r2 与 r3 每个 run 都保存了当次 guard/helper 源码；r3 另冻结 runner、SSE observer 和假桥源码。r1/r2/r3 均未覆盖原始运行证据。

三次 owned Host PID 分别为 r1 `[21296,26556]`、r2 `[7896,15212]`、r3 `[18412,26544]`；各次 Windows Job 清理后 remaining_pids 均为空，Host/control/Boundary/假桥端口均已关闭。最终验证是证据离线核查，不新增 Host 或模型调用。三次运行合计 12 次本地 Messages，真实 provider 0。

尚需根代理落实：真实业务 DB 的 lease／unknown 事务、source handle 与课程／版本授权、业务 operation 幂等、错误／取消终态释放、同一 run 重复 provider ID 的 exact attempt 绑定、冷恢复后映射重建。当前是 warm follow/page 只对账实证，没有再次做冷 Host 重建。进程内 token 丢失后应对照原始事件与 DB receipts，不恢复执行、不自动重发；历史 user／runtime／catalog 均不删除。

本候选不启用真实业务 skill，不修改正式 profile／guard／Boundary／数据库／锁，不是产品权限验收或真实模型学习体验验收。
