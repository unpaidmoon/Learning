# 业务 epoch、原生 lease 与冷恢复终态实证

日期：2026-10-23。状态：**独占候选与固定 SEA 本地实证完成；本文未修改正式产品。** 同一聊天的业务 epoch 不必逐轮增加；原生 lease 需要独立递增。重建自有 Host 后，已知 completed、用户取消及正常 max-tokens 截断可仅凭真实持久日志与业务／attempt 账本重建释放投影，无须重发 prompt。未知 provider、未知业务效果及未结束 turn 仍闭锁。

增量位于 [epoch-recovery](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery)。[readonly-recovery.patch](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery/readonly-recovery.patch) 为 5 文件／255 行，只对冻结的 cancel-release 候选 baseline 做过 apply-check，没有 apply 到正式目录。根代理已调整正式 images、exact_image_sequence、业务 Port 和恢复 gate，应手工迁入函数，不能整文件覆盖。[review-delta](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery/review-delta) 已剔除全部 `/probe/*` 诊断接口；[delta-manifest.json](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery/delta-manifest.json) 保存前后哈希。

## 1. 正式 Repository／Service 的 epoch 事实

[service-proof.py](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery/service-proof.py) 实际导入当时正式 `Repository`、`GenerationService`、`AppConfig`、`ExecutionBinding`；只有执行对象是明确的无 provider fixture。两次真实 `service.submit`、服务消费、业务落盘均 completed，复用同一 sid 和 binding_id。

| 字段 | 第一轮 | 第二轮 |
| --- | --- | --- |
| `generation.epoch`／`ExecutionBinding.epoch` | 1 | 1 |
| sid | 同一 sid | 同一 sid |
| 私有 `native_lease_epoch` | 1 | 2 |
| request／generation | 各自独立 | 各自独立 |

第二轮完成后，经实际 `GenerationService._ingest` 投递第一轮的迟到 raw usage `{input_tokens:10, output_tokens:3}`，仍正确归第一轮，第二轮 usage 保持空。第一轮迟到正文被拒绝。不能为满足 NativeLease 的递增协议而提高业务 binding epoch，否则会破坏既有事件／用量归属。

推荐把映射写在现有 Scope／业务事务中：

```text
(sid, run_id, request_id, business_epoch) -> native_lease_epoch
```

同一 run 的映射不可替换，恢复只读回原映射；新 run 须前 owner 已知释放后才递增。Wire policy／NativeCall 的 `epoch` 继续表示私有原生 fence，业务事件的 epoch 保留原值。候选 [native_epoch_map.py](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery/native_epoch_map.py) 仅演示这一区分，不建议另建一套映射平台。根代理已选择在正式业务 Scope 内分开这两种 epoch。

[formal-service-epoch-r1](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery/formal-service-epoch-r1) 保存实际 DB、结果、9 项通过检查及当时三个正式源码的哈希／冻结副本。这是 Repository／Service 本地实证，不是原生 Host 或真实模型验收。

## 2. 冷恢复能用的真实公共 API

固定 SEA 实际可用以下接口：

```js
const handle = await ctx.sessionPersistence.open(sid, 'read');
try {
  const header = handle.header;
  const { events } = await handle.read(0, undefined);
  // 真实物理持久日志；核 header.id、事件连续 seq、原 user source 与终态。
} finally {
  await handle.close();
}
```

仅需为 Guard 增加 `sessionPersistence` 注入。`ctx.sessionQuery.observeSession(sid, {projectionMode:'none'})` 是查询接口，返回对象应调用 `[Symbol.dispose]()`；它不能单独作为已知终态证明。

源码 [cold-read.ts](../../../.runtime/desktop/upstream/packages/session-query/session-query/src/cold-read.ts) 第 47–54 行会在物理日志后追加 `interruptedTurnClosers`，仅供内存中平衡展示，不写回。因此查询里有 `turn/end` 不证明 Host 崩溃前实际写过该终态。实测 crash 用例在 follow 前：物理 12 条、真实 turn/end 为 0；查询 14 条、展示 turn/end 为 1。物理读取正确拒绝 `RECOVERY_RAW_TURN_OPEN`。

另一个实际差异：`peer.follow` 会恢复／发布 Agent，追加持久 `session/end-seed`。因此仅 follow/page 虽没有模型调用，native 文件字节仍会变化；不应写成“完全不变的磁盘只读”。本候选的恢复函数自身不创建／恢复 Agent；无 live Agent 时直接读物理句柄。已有 Agent 时，必须为 `idle`、Session 所有者相同，并实际 `ctx.sessions.flush(session)` 成功后再读取；不伪造 Session 对象。

## 3. 最小释放投影

可直接审查 [native-recovery.mjs](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery/review-delta/harness/plugins/native-recovery.mjs) 的三个函数：

```js
readRawNativeCut(ctx, policy)
recoverReadonlyLease(ctx, leases, attempts, policy)
verifyReadonlyCut(ctx, policy, recovery)
```

`recoverReadonlyLease` 只重建 release 所需的证明，不复活 token、tool handler、dispatch 或 provider 请求。它依次执行：

1. 核当前不可变 policy、sid、run、request 和私有 epoch；读取物理日志、核 header.id 与连续事件 seq，计算 canonical header＋events 的 SHA-256 和 cursor。
2. 折叠 inbox splice，`removedCount` 省略时按原生契约取 0；要求无 pending input、无 open turn。定位最后一轮真实 `source.kind=user` 的 rpcId 和独立精确 marker。runtime/catalog/history 保留原样。
3. 只核目标 run 所在轮的 call/result；较早历史轮不能被重新授予 owner。目标轮每个业务 native call 的 seq、turn、step、raw callId、name、args hash 必须与同 run Registry binding 对应；再核 exact attempt／dispatch／provider ordinal 的向量，拒绝缺项或多余 binding。
4. 通过 authenticated loopback release-check，要求 Repository 核同确切原生 `{turn,seq,reason}`，业务效果全部已知，所有所属 attempt 为 completed／closed_cancelled／closed_error，网络关闭与 watcher 收尾证据明确为 true。
5. 保存仅用于释放的 projection 及账本证据 SHA-256；NativeAttempts 使用单独的 `readonlyRecovery` 集合，不向 executable records／messages／tokens 回填旧内容。
6. release 再查 Repository／Registry，账本 digest 必须相同；再读物理 cut，cursor／hash 必须相同，才生成原生 kind 不变的 durable receipt。新 run 获准后只丢弃这份旧恢复投影，聊天历史不删除。

候选私有 API 为 `POST /leases/recover-readonly`，沿用现有 control 鉴权，JSON 仍只有四项：

```json
{"session_id":"...","run_id":"...","request_id":"...","epoch":2}
```

这里 epoch 是 native lease fence。Host 先恢复原 policy，follow/page 原 session，调用该接口，再走已有 `Host.release_execution`／Boundary release／业务 mark_released。fixture 曾直接 POST release 而漏掉 Python Host 的 released 状态，导致下一 owner 被正确拒绝；最终版本使用公共 `Host.release_execution`，没有绕过这道约束。

## 4. Repository 与 journal 的窄增量

正式 Port 的 `remember_terminal(run_id, terminal)` 继续只保存：

```json
{"turn":2,"seq":35,"reason":{"kind":"completed"}}
```

不要把内部 `runId`、恢复状态或 cut 混入这三个字段。来源必须是 Adapter 核实归属的实际 native `turn/end`；查询合成收尾不能直接入库。Scope／binding 已有 sid、run、request、business_epoch、native_lease_epoch 足够承接 owner 身份。

[NativeCallRegistry.release_evidence](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery/review-delta/backend/src/learning_app/execution/native_tools.py) 的增量很小：release-check 的 owner 四字段可附 `native_terminal`，先核原 owner，再要求 Port 核同该确切 terminal；响应补 `attempts[]`、`native_calls[]`，保留原 `terminal_attempts[]`。这些是本机私有证据，不发给模型，不当作业务授权。

cut 证明可随现有 release receipt／Scope JSON 持久保存：`recovered_from_persisted_cut`、`native_cursor`、`native_cut_sha256`，以及业务／原生 epoch 和 owner。候选内存中另冻 `evidenceSha256`、attemptCount、callCount。若需要跨进程审计完整复核，可同时保存 journal cut／证据 digest；无需把完整用户日志复制进新业务表，也不用另建重试队列。缺失 proof 时拒绝释放，不能从 usage 缺失推导“没有执行”。

Root 增加的私有 reconcile-only runs 清单是独立必需的前置条件：对已派发旧 run，在 Host 启动前载入；tool guard 和 llm/stream 均拒绝再次执行，只允许查询／对账。prepared 未派发不放入该清单，新 run ID 可正常执行。候选 cr7 使用同样独立清单；它不是 policy 正文字段或 marker，也不授予业务权限。本次没有发送恢复 prompt 来测试该 gate，因此 gate 的主动拒绝覆盖仍以根代理正式 fixture 为准。

## 5. max-tokens 的真实差异

固定源码 [translate.ts](../../../.runtime/desktop/upstream/packages/llm/llm-deepseek/src/translate.ts) 第 92–94 行：provider `end_turn`／`stop_sequence` → LLM `stop`，provider `max_tokens` → `max-tokens`。[Agent loop](../../../.runtime/desktop/upstream/packages/core/agent-loop/src/agent.ts) 第 547 行把后者直接转为原生 `turn/end.reason.kind=max-tokens`；普通无工具 stop 在第 550 行转为 completed。

实际固定 SEA 也如此：`stop_reason=max_tokens` 得到 `{kind:'max-tokens'}`，`stop_sequence` 得到 completed。正常截断不能误走 error reconciliation；NativeLease 的正常 settled 分支应接受 completed／max-tokens，仍要求已知业务结果与正常结束的 attempt。原生 kind 保持 max-tokens，业务应映射 truncated，raw usage 按原字段保存。正式 NativeBusiness 的终态状态映射也需支持 `max-tokens -> truncated`。

本次截断正例为完整正常 SSE＋文本截断，没有未知工具效果。未将半个 tool-call、未完整结束的 SSE、未知业务写入或 provider 超时放入同一放行条件。

## 6. 实际固定 SEA 证据

主证据 [cr7/result.json](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery/cr7/result.json)，独立 [91 项检查](../../../.local/cache/workbench/native-product-bridge-candidate/epoch-recovery/cr7/verification.json) 全通过。使用实际 SEA、原生 home、native persistence、loopback Messages、SQLite Port、fsync Registry journal；原文与工具来源全部合成，无真实 provider、密钥或用户材料。关闭主自有 Host／Boundary／transport 后，以同 home 和同账本创建新的自有 Host／端口。

| 冷恢复场景 | 实际结果 |
| --- | --- |
| 同 sid 第二轮 completed，历史第一轮含同名同 raw callId 工具 | 目标 turn2／native epoch2 正确恢复，release200；下一 owner 仅配置 native epoch3／business epoch1，未发 prompt。 |
| 用户活动 stream 取消，原生 aborted(user) | 原 terminal 与 closed_cancelled 账本一致，恢复／释放200；下一 owner 可登记。 |
| 正常 max_tokens 截断 | live release200，冷恢复 release200，kind 保持 max-tokens，无 error reconciliation。 |
| stop_sequence | 原生 completed，冷恢复／释放200。 |
| provider stream 未完整结束 | `NATIVE_ATTEMPT_UNKNOWN`，不能恢复、释放或被下一 owner 接管。 |
| native completed 但业务操作结果 unknown | `NATIVE_OPERATION_UNKNOWN`，同样闭锁。 |
| 真正 Host 中途退出、物理 turn 未结束 | follow 前 `RECOVERY_RAW_TURN_OPEN`；查询补收尾不能放行；follow 后仍拒绝。 |
| 四类 known 的 Repo terminal.seq 人为错置 | 均409 `REPOSITORY_TERMINAL_NOT_KNOWN`；恢复真实值后才允许。 |
| 先捕获物理 cut，再 follow 使持久日志改变 | 旧 projection release409 `RECOVERY_NATIVE_CUT_CHANGED`；重新读取核同后成功。 |

恢复前后 Messages ledger **11→11**、业务调用 **3→3**；冷 Host 的 dispatches=0、NativeAttempts executable records=[]、call claims=[]。真实 provider 请求 **0**。这里不能写成整个实验只有 0 次 Messages：主阶段实际执行了 11 次本地 Messages，恢复阶段才是新增 0 次。

两代 Host 所属 PID 均由 Windows Job 清理，最终 Host／control／Boundary／transport 四个端口关闭。未启动、停止或修改用户现有实例。

## 7. 历史失败与边界

所有 run 保留，没有覆盖失败：cr1 因过度限制任何 live Agent 而拒绝冷读取；cr2 因未处理省略的 `removedCount` 拒绝 inbox shape；cr3 已恢复／释放 completed，但 fixture 未调用公共 Host release 导致 Python policy 切换被拒绝。cr4、cr5 成功基本矩阵；cr6 补同 sid 历史、max-tokens、stop_sequence；cr7 再加 exact Repository terminal 负例。

补丁只有基线 apply-check、四个 JS 模块 syntax、Python compile 和以上实际 fixture 的覆盖；Service 9 项＋SEA 91 项共 100 项通过。`scripts/check_docs.py` 实际执行为 163 files／0 errors。没有对正式 images／新 Port 集成做替代验收；正式 source handle、任务权限、输出 CAS、事务 effect receipt、恢复 DB owner 和最新生产文件的测试均由根代理集成。重启后不能安全核实的旧 run 继续 unknown，不能用新 owner、合成终态或缺失 usage 接管。

固定 SEA 版本 `0.2.1-rc.2`，SHA-256 `f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365`；固定源码参考 `687f282e8625c6b8d11ff58211a90524be2e4317`，源码 ZIP SHA-256 `828bbbebaf507d2b1fc335a608e1c8174e2dd5c5c80ebb64d5146ddc5ecb511f`。源码解释与制品实证分别列明，不把它们称为同一 build。
