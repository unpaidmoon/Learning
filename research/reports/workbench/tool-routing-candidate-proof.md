# 工具续答路由最小迁入候选与验证

日期：2026-10-23。状态：**独占候选通过本地验证，尚未应用正式工程。** 候选不启用业务skill／schema，guard仍拒绝全部工具；只增加具备原生source校验的continuation归属。

正式 `learning-guard.mjs`、Boundary、Host、Adapter、profile均未修改。所有输入是合成文字；实际模型出站0，没有用户资料、图片或既有用户实例操作。

## 交付位置

全部代码与运行证据位于 `.local/cache/workbench/tool-routing-candidate/`：

| 文件／目录 | 用途 |
| --- | --- |
| `baseline/`、`baseline-manifest.json` | 本次开始时复制的11份当前正式文件及SHA256，含guard、Boundary及其执行依赖／回归测试。 |
| `candidate/harness/plugins/learning-guard.mjs` | 原guard的最小改动；全拒绝工具及空schema断言保持。 |
| `candidate/backend/src/learning_app/execution/boundary.py` | 原Boundary的路由绑定、原生header接入与窄fallback。 |
| `candidate/backend/src/learning_app/execution/continuation_routing.py` | 已验证的纯工具链归属函数；不授予业务能力。 |
| `candidate.patch` | 只涉及上面3个文件的LF unified diff；`git apply --check`已通过，**没有执行apply**。 |
| `boundary-regression/result.json` | 29个复制的原Boundary测试，0失败／0错误。 |
| `candidate-proof.json` | 31项候选检查；包含实际Host、回放负例、绑定冲突、恢复登记与清理结果。 |
| `positive-r3/`、`unmarked-r4/` | 最终实际Host原始Messages／SSE、Session page／frames、guard audit、私有profile与清理记录。 |

正式运行不得import缓存目录。根代理应把审阅通过的3份候选内容迁入对应正式位置，再接Host／Adapter登记调用；已有变更继续保留。旧 `candidate-crlf-failed.patch` 只保留最初patch封装的换行错误；LF版本只读检查已通过，不应使用旧副本。

## 最小改动

### Guard

旧六字段模型policy继续兼容。只有额外同时登记 `request_id` 与正整数 `epoch` 的policy启用source gate；部分字段／格式不合法拒绝，同sid的不同完整policy拒绝。完整policy是：

```text
session_id, run_id, request_id, epoch,
model, thinking, max_output_tokens, timeout_seconds
```

`llm/stream` 在 `next()` 前核该sid最新 `source.kind:'user'` 消息：必须有精确独立marker块 `[[learning-generation:<run_id>]]\n`，且原生 `source.rpcId === request_id`。不匹配以 `LEARNING_CONTINUATION_UNMARKED_USER` 拒绝并记录布尔匹配结果／run／epoch；不记用户正文或token。不改messages，不删user、历史、runtime或catalog。

私有 `/state` 增加 `continuationRoutingVersion:1` 与 `continuationBoundSessionCount`，使集成层能核实新协议已加载。工具名单仍是空数组；原工具guard、空schema检查、网络与凭据边界未放宽。

### Boundary

`bind()` 新增可选参数 `session_id, request_id, epoch`，三者必须完整、合法；缺失时沿用原有语义。完整绑定在当前实例内不可改写，重复相同登记幂等。

`_handle()`在现有内部认证后读取原生 `x-deepseek-harness-session-id`；重复header拒绝。`_resolve_generation(request, session_id=...)`先调用保留的 `_resolve_marker_generation()`，仅在错误恰为 `GENERATION_MARKER_REQUIRED` 且该sid已完整登记时调用纯函数。

纯函数逐回合核相邻assistant `tool_use.id` 与user `tool_result.tool_use_id`：完整、唯一、集合一致才向前走，直到原marker；途中遇到普通无marker user立即拒绝，最终run必须与sid绑定相同。工具外附加的context原样保留。marked路径与已登记sid冲突也拒绝。新增账本事件 `continuation_routed/continuation_route_rejected` 记录归属，不增加provider attempt。

fixture中已登记sid也强制走严格marker规则，确保实际Host验证不会误用fixture FIFO；旧的未登记fixture行为及正式无marker负例保持。新函数只是归属，不替代来源范围或成果写入权限。

当前最小候选保留了原Host按sid不可变policy的约束，适合独立task Session。同一聊天sid切换到另一逻辑生成的显式生命周期不在本补丁中自动放宽；不能把新requestId静默覆盖旧绑定后继续认领迟到请求。

## 根代理需接的登记点

候选刻意没有修改正式Host／Adapter。应用时需在现有接口补两个字段透传：

1. `HarnessHost.remember_execution_policy()`／`configure_execution()`接受可选 `request_id, epoch`；完整新绑定保存这两项，旧六字段policy不变。
2. 在 `ExecutionAdapter.register_execution_policy()` 用 `binding.request_id`、`binding.epoch`登记。首次执行应先确认Host新协议／私有policy登记成功，再把同一sid/run/request/epoch交给Boundary，最后调用原 `session/prompt`。不要从模型参数或当前UI状态生成绑定。
3. 恢复路径从持久run读取同一绑定，先预置Host启动policy文件和Boundary映射，再启动／follow可能续答的Session。新guard对八字段policy会执行source gate；旧guard不支持该policy会拒绝启动，不能以Host ready代替版本核验。
4. `unknown`只允许恢复登记＋follow／page／已有原生事件对账，**不因此调用start／prompt、分配新requestId或自动重发**。登记冲突要失败并保留未知，不用新sid掩盖。

实际候选验证使用了不同的run ID与request ID，因此没有依靠“两者刚好相同”通过RPC来源检查。

## 实际Host验证方式与结果

为了保持工具schema为空，fixture没有注册echo或业务工具。它让本地Messages返回未注册的 `candidate_not_registered` 调用：原guard拒绝，原生loop仍生成真实错误 `tool_result`。一个不注册任何工具／skill的fixture plugin在结果后提供required动态context；因此后续wire user同时有真实tool_result与context。

| 场景 | 实际结果 |
| --- | --- |
| `positive-r3` | 同step被拒的A/B调用→第一次mixed续答→被拒的C调用→第二次mixed续答→终答；**3次本地Messages，3次工具拒绝，tools始终[]**。两个续答均由候选fallback正确归原run。 |
| `unmarked-r4` | 在下一pre-step明确加入新的native user-origin消息，无marker且rpcId不同；source gate以 `LEARNING_CONTINUATION_UNMARKED_USER` 拒绝；**只有第1次Messages出站到本地fixture**。新用户消息保留在原生历史，未进入下次模型请求。 |
| 模拟提交回执丢失后的对账 | 正向run完成后，重复同一完整Host／Boundary登记，再follow／page找原rpcId；登记200、冲突requestId登记409；Messages保持3→3，新增prompt为0。只是此故障窗口的本地证据，不冒充所有重启恢复已通过。 |
| 原有Boundary回归 | 29个原测试全部通过，包含官方模式最新无marker user不能借旧marker认领的负例。 |
| 候选focused检查 | 31项通过，包括缺header／未知sid保持严格拒绝、同sid重绑request／epoch拒绝、错误／重复call ID、缺兄弟结果、marked sid/run冲突、重复header HTTP拒绝及清理。 |

初版 `unmarked-r2` 的fixture在session/event内调用steer，但该次预期user没有实际进入step；不能算source gate负例通过。该批次保留。改为pre-step明确返回user-origin注入后，`unmarked-r4`才取得上述真实拒绝证据；没有用“请求没报错”冒充负例有效。

所有Host使用同一锁定SEA，启动时核EXE SHA256 `f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365`。Boundary处于纯本地fixture模式，Python的真实HTTPS构造在probe进程内直接报错；Host网络guard只允许本地fixture。所有真实provider attempt为0，synthetic usage不作为真实模型消耗。

最终正向Owned PID `[19024,22200]`、负向 `[16568,19764]`已全部清理，各自Host／control／Boundary端口均关闭。前两批所属进程同样清理；只操作自身Job，不按名称查杀。child TEMP／TMP、DSH_HOME、native cache及原始证据均在独占目录。

## 检查记录与未覆盖

实际成功的检查：

```powershell
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/tool-routing-candidate/verify_candidate.py'
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/tool-routing-candidate/check_proof.py'
& '.\backend\.venv\Scripts\python.exe' -B -m ruff check '.local/cache/workbench/tool-routing-candidate/candidate/backend/src/learning_app/execution/boundary.py' '.local/cache/workbench/tool-routing-candidate/candidate/backend/src/learning_app/execution/continuation_routing.py'
node --check '.local/cache/workbench/tool-routing-candidate/candidate/harness/plugins/learning-guard.mjs'
git apply --check '.local/cache/workbench/tool-routing-candidate/candidate.patch'
```

runner与focused脚本默认拒绝复用其run目录，保留历史。`check_proof.py`首次通过后仅把patch封装改为LF并重新只读检查；候选guard／Boundary逻辑未变。基础正式文件hash在patch检查时仍与冻结baseline相等；之后如主线程有新修改，需据新diff重新核对，不能强行覆盖。

未覆盖／not_run：正式Host／Adapter字段透传、正式任务启动恢复的全部故障窗口、业务skill／schema、来源读取与成果写入权限、真实模型、同sid跨生成切换、并发run竞争、实际桌面／用户学习流程。候选目前只是可审阅、可迁入的路由补丁，不能更新为业务工具或13场景已交付。
