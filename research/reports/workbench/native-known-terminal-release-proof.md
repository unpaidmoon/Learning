# 已知取消／错误后复用原生 QA Session 的释放实证

日期：2026-10-23。状态：**独占增量候选完成，未改正式代码。** 同一 sid 在真实活动 stream 被 `session/cancel` 后，保留原生 user-aborted，经明确对账释放，下一 epoch/pro-high completed；已知 HTTP 错误也保留 error，随后同 sid 新 epoch completed。未知 provider 截断和未知业务效果仍不能释放。

代码及证据位于 [cancel-release 独占子目录](../../../.local/cache/workbench/native-product-bridge-candidate/cancel-release)。[known-terminal-release.patch](../../../.local/cache/workbench/native-product-bridge-candidate/cancel-release/known-terminal-release.patch) 约 248 行，相对**之前冻结的主 bridge 候选**生成，只对该 baseline 做 apply-check，未 apply。根代理已修改正式 images／exact_image_sequence 接缝，必须手工合并本文函数，不能覆盖当前 Guard/Host/Adapter/Boundary/types。

## 放行条件与未知状态

完成一次原生取消不等于 provider 用量结算，也不自动允许新 owner。候选要求同时具备：

1. 原生持久 `turn/end` 是 `aborted(reason.kind=user)` 或明确 error；sid/run/request/epoch 与原提交一致。
2. Repository 已保存**确切 turn/seq/reason**，明确 known_cancelled／known_error，且无未知业务效果、未收尾写操作或待对账回调。
3. Boundary 已观察其所属下行 socket 关闭；真实 `_forward` 还须原上行 socket 的 `fileno()==-1`，且 watcher 已 join。
4. 待分类 attempt 没有 provider tool-call 向量；存在未知工具向量则继续闭锁。已执行业务操作全部有已知回执。
5. Guard 收到私有桥返回的 exact attempt/dispatch/terminal 回执，与自己的真实原生 turn/seq/reason 核同，实际 `ctx.sessions.flush` 成功后才释放。

派生分类是 `closed_cancelled`／`closed_error`，**不是 completed**。原始 Boundary status、SSE、usage presence、native error／aborted 日志全部保留。没有缺失用量记零。

已知 error 的当前保守范围只有明确的本地 policy rejection 和受控 HTTP500 错误响应。未将截断、timeout、未知 transport error 或任意 provider 错误自动归为安全终态；生产若需更多已知错误类别，应逐项依据响应证据扩展。

## 可迁入的确切接口

[NativeCallRegistry](../../../.local/cache/workbench/native-product-bridge-candidate/cancel-release/candidate/backend/src/learning_app/execution/native_tools.py) 增加：

```python
registry.reconcile_terminal(lease, native_terminal, repository_tools)
# lease: session_id, run_id, request_id, epoch
# native_terminal: turn, seq, reason（来自已保存原生事件）
```

它先调用 `RepositoryTools.assert_releasable({...lease, native_terminal})`，要求 DB 核同实际终态，随后核网络关闭证据及业务回执，写入 fsync 的独立 reconciliation 事件。根代理可迁入已有 Scope／TaskRunStore 事务，不必引入新业务平台。

私有 `/bridge/release-check` 除 releasable 外返回已分类的 exact attempt/dispatch/terminal 列表。[NativeAttempts.reconcileKnownTerminal](../../../.local/cache/workbench/native-product-bridge-candidate/cancel-release/candidate/harness/plugins/native-attempt.mjs) 只核同本进程原始 attempt、真实 user 终态和空工具向量；有任何未覆盖 unknown 则拒绝。随后 [NativeLease.release](../../../.local/cache/workbench/native-product-bridge-candidate/cancel-release/candidate/harness/plugins/native-lease.mjs) 才 flush/release。

释放回执保留 `native_terminal_kind=aborted|error`。Host／Adapter 原有 release 方法不需要把 generation 写成 completed。新请求仍要另行登记新 run/request/epoch；旧请求不重发，旧 owner 不重新激活。

## 连接关闭事实如何取得

[Boundary](../../../.local/cache/workbench/native-product-bridge-candidate/cancel-release/candidate/backend/src/learning_app/execution/boundary.py) 将本次 socket 与自己的 request 行关联。在自有 `process_request_thread` 返回、服务器完成 shutdown/close 后，检查**这个原 socket**的 fd，再写 network_closed 证据；没有按端口消失或进程名猜测。

`_forward` 的 finally 在关闭 response/parser、原连接并 join watcher 后记录原上行 fd 和 watcher 状态。fixture 标明 `fixture_no_upstream`，不冒充 provider 网络。HTTP 错误响应也回传 Boundary 自己的 attempt/dispatch 关联头，防止错误响应丢失 exact attempt 身份。

正式 `_forward` 取消时原始 status 可能是 `upstream_unknown`。在本次额外本地转发实证中，该字段确实保留为 upstream_unknown；只有原生 user cancel、Repository 明确 known_cancelled、空工具向量和上下行均关闭全部满足，才另加 execution 分类 closed_cancelled。provider 最终 token／账单仍未知。没有 native user cancel 的未知断流仍拒绝，即使 DB 只记录了“原生发生 error”。

## 还修复了已知错误后的 marker 归属

r1/r2 取消分支已成功，但 HTTP500 后下一轮未通过。原生 [serialize.ts](../../../.runtime/desktop/upstream/packages/llm/llm-deepseek/src/serialize.ts) 第 127 行会合并相邻 user；错误 attempt 没有可见 assistant anchor，于是上一轮和新一轮 marker 同处 latest wire user。旧 Boundary 正序取第一个 marker，误选旧 owner。

增量修正仅用于已登记 native route：从该 latest wire user 中找**精确独立 marker text block**，取最后一个，再按现有 sid/current owner 校验。Host 仍先核真实最新 `source.kind=user` 的 RPC ID 和独立 marker；不跳过用户、不删除历史、不改正文、不把 header 当业务授权。

r3 实际请求保留两个独立 marker block，顺序为旧 run1、新 run2，新一轮正确归 run2。两条成功路径之后均再提交新无 marker 用户，HTTP 计数不增；原始旧 run 请求重放仍因 owner 冲突拒绝。

## 实际验证

[verification.json](../../../.local/cache/workbench/native-product-bridge-candidate/cancel-release/verification.json) 共 43 项通过；[既有 Boundary 测试](../../../.local/cache/workbench/native-product-bridge-candidate/cancel-release/boundary-tests.json) 29 项通过，Node syntax／Python compile 通过。单测 socket 替身不提供 fd 时记录 null／未知，不伪造关闭事实，也不让证据采集异常覆盖原错误。

| 运行 | 证据 |
| --- | --- |
| `native-terminal-20261023-r3` | 固定 SEA，7 次本地 Messages，4 组场景，provider 出站 0。 |
| user cancel | 活动 stream→原生 aborted(user)。闭合证据未落账时先拒绝；对账后回执仍 aborted，同 sid turn2/pro-high completed。 |
| known HTTP500 error | 原生 error／SERVER／500 原样保留，对账后回执仍 error，同 sid turn2/pro-high completed。 |
| unknown provider | 真实截断，原生 STREAM_CLOSED。即使 fixture DB 保存 native error，仍 `NATIVE_ERROR_ATTEMPT_UNKNOWN`；release／接管拒绝。 |
| unknown business | 业务调用仅执行一次并记 unknown；native 虽 completed，仍 `NATIVE_OPERATION_UNKNOWN`，新 owner 拒绝。 |
| `native-forward-terminal-20261023-r3` | 真正 SEA→Boundary `_forward`→纯本地 HTTP provider double；实际上下行关闭、watcher 退出，对账后同 sid下一轮 completed。 |

最后一组的 Boundary dispatch 账保留 **2 个模拟出站**，本地 provider double 确实收到 2 个请求；HTTPS 构造被限制为连接 `127.0.0.1`，没有 DNS／互联网／真实 provider 请求。不能把这组写成“Boundary 没出站”，也不能冒充真实供应商取消验收。

取消和错误的原始 usage `settled=false`、normalized 各字段仍为 null；没有改写为零或已结算。新的 epoch 只有自己的已完成 fixture usage，不替旧 attempt 补账。

## 历史失败、清理与限制

r1/r2 的 known-error 下一轮实际失败，原始结果保留；当时 runner 仅遍历完场景即写 status=completed，不能据此认定验收通过。[history-verdicts.json](../../../.local/cache/workbench/native-product-bridge-candidate/cancel-release/history-verdicts.json) 明确将两轮判为未通过。r3 已加入第二轮必须 completed 的断言，随后才通过全部正反例。

转发组合 r1/r2 也保留。r2 原生日志明确为独占提取路径过长，koffi.node 无法加载，未到模型出站。r3 仅将子进程 native cache 缩短到同独占目录下 nc 后成功；没有修改全局 PATH／HOME，也未修 MCP 配置。

最终两组各自拥有的 PID 均经 Windows Job 清理，remaining_pids 为空；Host/control/Boundary/业务桥／本地 provider 端口均实测关闭。真实 provider 请求始终 0，没有用户材料或用户 Host 重启。

未覆盖：真实供应商取消、进行中的业务写入取消、非空未知工具向量的安全释放、timeout 释放、重启后从持久原生事件重建 Guard 终态投影。上述情况保持 unknown／闭锁并先对账，不自动重发。根代理应继续沿用真实 DB 的 epoch/CAS/outbox 约束，手工迁入关键增量，保留已接入的图片派生白名单与 exact_image_sequence。
