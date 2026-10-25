# 工具续答归属：原生 Session 头与消息来源门槛

日期：2026-10-23。调查范围为固定SEA的实际本地fixture和冻结源码。只新增本报告与独占 `.local/cache/workbench/harness-continuation-audit/` 证据；没有修改正式Boundary、guard、Host或profile，没有真实模型、用户材料或用户实例操作。

**最小安全接缝已经实证：原生 Session header＋冻结的sid→run/requestId绑定＋序列化前的user来源门槛＋窄tool-result链回溯。** 不需要新增HMAC工具envelope。仅有Session头或真实tool_result仍不够：原生steer可以把新的无marker用户输入与真实工具结果合并成一个wire user回合，必须在source尚未丢失时拒绝它。

本地核验结果为47项证据检查、12个Boundary归属负例通过，其中14项直接执行报告内的Python函数片段；五个批次均保留，包括错误接受新用户输入的r3失败方案。这里只证明接缝与负例，不代替正式权限、任务恢复、真实模型或桌面验收。

## 1. 原生接口确实存在

固定参考源码 `687f282e8625c6b8d11ff58211a90524be2e4317` 的：

- `packages/core/agent-loop/src/agent.ts:695` 构造并freeze请求，写入 `sessionId:this.session.id`；messages及每条message也已冻结。
- `packages/llm/llm/src/types.ts:528` 的 `GenerateOptions` 带 `sessionId` 和带source的原生messages，没有通用per-call headers字段。`agent/request`返回的是模型调用配置，也不是任意HTTP header接口。
- `packages/llm/llm-deepseek/src/adapter.ts:147` 原生发送 **`x-deepseek-harness-session-id`**，值来自该 `options.sessionId`。本次实际SEA的全部17个已到达Boundary的HTTP请求都带正确自有sid，且通过既有本地认证。
- 同文件`:122` 的request extensions能收到sessionId，但贡献的是请求body字段；这里已有正确header，不应为了本地路由把自定义字段加进provider body。
- `llm-deepseek/src/serialize.ts:124` 会合并相邻user／tool消息。native source信息不会跟着进入Messages JSON；工具结果和后加的runtime/catalog/user文本可能变成同一wire user message。

实际EXE每次启动前核SHA256 `f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365`。`adapter.ts`的header事实由实际请求头核实，而非仅从参考源码推定。所有probe沿用上一轮r4冻结的8份Python源码、相同profile seed；全部source/profile hash逐项相等。源码提交和运行制品仍不称为同一build。

Session header是内部归属字段，**不是独立认证或业务权限**。Boundary先执行现有loopback／Origin／本地capability校验；renderer没有该capability，模型没有任意HTTP工具。正式Boundary已有上游header白名单，继续让此内部sid在本地终止，不因本调查把它作为模型材料转发。

## 2. 失败与成功对照

独占plugin只有原生skill和无IO echo；关闭filesystem discovery及隐式Windows ACL skill provider，cwd贡献沿用已验证的scoped空provider。另注册一个required动态context：echo执行后变为 `REQUIRED_ROUTE_CONTEXT_SENTINEL_2/3`，由原生loop正常发布。本次没有手工删改或补写Messages正文。

脚本化的正向链为：原生skill加载→同step两个echo调用A/B→再一轮echo C→终答。两次动态runtime-context都与工具结果合并，覆盖连续多个混合回合。

| 批次 | 开关／输入 | 实际观察 |
| --- | --- | --- |
| `continuation-strict-20261023-r1` | 原有严格marker逻辑 | 前2个Messages已接受；第3个到达Boundary后因 `GENERATION_MARKER_REQUIRED` 拒绝，HTTP403，原生turn error为 `GENERATION_BINDING_REJECTED`。失败保留。 |
| `continuation-header-20261023-r2` | Session头＋活动run＋结构化工具链 | 4个Messages完成；两个混合runtime回合都归原run，原始附加context保留。 |
| `continuation-native-user-control-20261023-r3` | 仅上述header方案；echo A执行时通过原生 `agent.steer()` 注入新的无marker `source.kind:'user'` | **错误接受，方案失败。** 4个Messages完成，后续请求实际包含新用户sentinel与真实A/B工具结果。不能把该文本误称为required context。 |
| `continuation-native-user-gate-20261023-r4` | 相同注入，增加序列化前source gate | 只有前2个Messages到达Boundary；第3次请求在Host中以 `PROOF_NEW_UNMARKED_USER_DENIED` 阻止。原生turn error保留，模型未收到被拒的新输入。 |
| `continuation-source-gate-valid-20261023-r5` | header＋source gate；合法required context | 4个Messages完成；其messages与r2逐请求完全相等，证明gate没有删user、历史、runtime context或skill目录。 |

原生echo body的 `exec.callId` 与实际后续Messages的 `tool_result.tool_use_id` 完全一致，依序为 `multi-echo-A`、`multi-echo-B`、`echo-following-mixed-context`。同step A/B两个结果出现在同一个后续请求；不能按工具条数增加模型attempt。

合计16个本地Messages被fixture接受，另有1个到达Boundary后被拒；r4第三次调用在Host内阻止，未形成HTTP请求。真实provider出站均为0，synthetic SSE usage不作为真实用量。

## 3. 正式guard应采用的确切时序

绑定来自业务层持久run／generation及已准备的原生binding，不能来自模型参数、当前UI课程或动态猜测。需要共同冻结：

```text
session_id, run_id, request_id, epoch,
expected_marker = "[[learning-generation:<run_id>]]\n"
```

建议的接入顺序：

1. `prepare()/prepare_task()`得到真实sid、稳定requestId和native cut；业务CAS claim持久化上述绑定及输入／policy hash。
2. **在 `session/prompt`之前**，把同一绑定登记到Boundary和Host现有私有execution-policy控制通道。Host当前 `remember_execution_policy/configure_execution` 的记录需增加requestId／epoch；Boundary增加sid→活动绑定，不另建供应商协议。
3. Host执行 `llm/stream` hook时，以不可变 `options.sessionId` 找对应绑定，核最新真正user-origin消息。通过后才 `return next()`，进入原生serializer和fetch。此时无需也不得修改已冻结的options或messages。
4. Boundary先认证内部请求，再取原生Session header。正常带marker路径保持现有处理；只有原逻辑的错误**恰为** `GENERATION_MARKER_REQUIRED`，且存在有效Session绑定时，才尝试第4节的工具续答fallback。
5. 恢复时先恢复Host／Boundary的绑定，再进行可能唤醒续答的follow／恢复。未知请求仍先对账；不能以新requestId补发。相同sid切换到下一run只能在原run终态／准入控制下进行；迟到请求的旧marker与新活动run不相等，应拒绝，不能改记新run。

本次实际source gate位于无IO的同步 **`ctx.on('llm/stream', function(options,next){...})`**；其生产化片段如下，policy表来自私有注册而非文本：

```javascript
ctx.on('llm/stream', function(options, next) {
  const binding = executionBindings.get(String(options.sessionId || ''));
  if (!binding) throw Error('LEARNING_EXECUTION_BINDING_REQUIRED');
  const latestUser = options.messages
    .filter(message => message.role === 'user' && message.source?.kind === 'user')
    .at(-1);
  const marker = `[[learning-generation:${binding.run_id}]]\n`;
  const markerMatches = latestUser?.content.some(block =>
    block.type === 'text' && block.text === marker) === true;
  if (!markerMatches || latestUser.source.rpcId !== binding.request_id) {
    throw Error('LEARNING_CONTINUATION_UNMARKED_USER');
  }
  return next();
});
```

作用域应限定已注册的业务执行Session。普通聊天每次新提交也必须更新自己的可信活动绑定；不是要求所有历史user消息都匹配当前requestId。来源为 `runtime-context`、`skill-catalog`、`skill-invocation`、`tool` 的原生消息保持原样，它们不会被误当成新的user-origin输入。

`.source.kind`／`.source.rpcId` 来自原生Session准入与持久消息，用户或文档在text里拼出相同JSON不改变这些字段。probe的steer负例确实以原生user source加入，因此该门槛能在serializer丢失来源前拒绝。现产品已拒绝普通steer；此检查仍防止今后注入／工具新接缝绕过当前提交身份。

probe使用普通Error，原生错误code为UNKNOWN、message为上述稳定probe码；正式适配应把受控拒绝映射成项目稳定错误／可处理状态，不把它伪称为provider认证失败，也不隐式重试。

## 4. Boundary可迁入的窄函数

现有marker resolver应保留为独立严格函数。`_handle()`在已有认证后显式传入Session header即可，不必采用probe为截取header使用的thread-local技巧；sid绑定和下面的读取应处在现有Boundary lock中。

以下纯函数在独占 `routing.py:30` 提取，省略probe返回的method追踪字段，只返回owner；报告内原样片段已用实际r5请求回放和12个负例核验。`marker_resolve`必须指向原有严格marker处理，**不能递归进入fallback或启用fixture FIFO**。调用者只在确切的 `GENERATION_MARKER_REQUIRED` 后使用它；正常marker结果如与已知sid活动run冲突，也须拒绝。

```python
def resolve_tool_result_continuation(
    request, bound, session_header, session_runs, marker_resolve
):
    if not session_header:
        raise ValueError("ROUTE_SESSION_HEADER_REQUIRED")
    owner = session_runs.get(session_header)
    if owner is None or owner not in bound:
        raise ValueError("ROUTE_SESSION_NOT_BOUND")
    prefix = request.get("messages")
    if not isinstance(prefix, list) or any(not isinstance(m, dict) for m in prefix):
        raise ValueError("INVALID_MESSAGES")
    groups = 0
    while prefix:
        indexes = [i for i, m in enumerate(prefix) if m.get("role") == "user"]
        if not indexes:
            raise ValueError("ROUTE_NO_MARKED_ANCHOR")
        index = indexes[-1]
        if any(m.get("role") != "system" for m in prefix[index + 1:]):
            raise ValueError("ROUTE_NON_USER_REQUEST_TAIL")
        latest = prefix[index]
        try:
            # 仅查看这一条user，不能借此跳过一个新的无marker用户。
            anchored = marker_resolve({"messages": [latest]})
        except ValueError as error:
            if str(error) != "GENERATION_MARKER_REQUIRED":
                raise
        else:
            if groups == 0:
                raise ValueError("ROUTE_TOOL_RESULT_REQUIRED")
            if anchored != owner:
                raise ValueError("ROUTE_MARKER_OWNER_CONFLICT")
            return owner
        content = latest.get("content")
        if not isinstance(content, list):
            raise ValueError("ROUTE_TOOL_RESULT_REQUIRED")
        results = [b for b in content if isinstance(b, dict)
                   and b.get("type") == "tool_result"]
        if not results:
            raise ValueError("ROUTE_TOOL_RESULT_REQUIRED")
        if index < 1 or prefix[index - 1].get("role") != "assistant":
            raise ValueError("ROUTE_ADJACENT_TOOL_CALL_REQUIRED")
        calls = prefix[index - 1].get("content")
        if not isinstance(calls, list):
            raise ValueError("ROUTE_ADJACENT_TOOL_CALL_REQUIRED")
        call_ids = [b.get("id") for b in calls if isinstance(b, dict)
                    and b.get("type") == "tool_use"]
        result_ids = [b.get("tool_use_id") for b in results]
        if (not call_ids or any(not isinstance(x, str) or not x
                               for x in call_ids + result_ids)
                or len(set(call_ids)) != len(call_ids)
                or len(set(result_ids)) != len(result_ids)
                or set(call_ids) != set(result_ids)):
            raise ValueError("ROUTE_TOOL_CALL_ID_MISMATCH")
        groups += 1
        prefix = prefix[:index - 1]
    raise ValueError("ROUTE_NO_MARKED_ANCHOR")
```

它只越过与相邻assistant真实 `tool_use` 的ID集合完整且唯一配对的工具回合，直到本run的marker；不泛跳其他无marker user，不移除context，不改变来源／写入授权。**它必须与第3节source gate同时使用**；r3已证明单独使用它并不安全。

## 5. 负例、证据与未覆盖

Boundary纯函数拒绝了12类回放：缺header、未知sid、活动run已变更、普通无marker新用户、后追加无marker用户、只在text里拼tool_result JSON、错误call ID、缺少同step兄弟结果、重复result ID、没有相邻assistant调用、无已标记祖先、未绑定marker。缺失／冲突不会退回任意旧run或FIFO。

实际Host负例另证明了最关键的序列化歧义及source gate修复，不能拿上述纯函数负例代替它。HMAC工具envelope即使签名run/callId，也不能区分合并进同一回合的新用户文本；已存在原生header，故未实现签名key、回执bearer或其他权限面。

证据目录为 `.local/cache/workbench/harness-continuation-audit/`：

- 各run的 `result.json`、原始synthetic请求／SSE、Session page／frames、plugin audit和清理记录保留。
- `verification.json`记录47项核验、12个负例及五个run的明确结果；raw请求hash／实际字段继续在各run Boundary账本内。
- `routing.py`是可迁入纯函数及probe wrapper；正式运行不得import此缓存。r1～r5执行时快照与后续纯函数提取分别保留，不覆盖失败。

实际命令包括：

```powershell
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/harness-continuation-audit/probe.py' --run-id continuation-strict-20261023-r1 --hide-cwd
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/harness-continuation-audit/probe.py' --run-id continuation-header-20261023-r2 --hide-cwd --header-route
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/harness-continuation-audit/probe.py' --run-id continuation-native-user-control-20261023-r3 --hide-cwd --header-route --inject-unmarked
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/harness-continuation-audit/probe.py' --run-id continuation-native-user-gate-20261023-r4 --hide-cwd --header-route --source-gate --inject-unmarked
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/harness-continuation-audit/probe.py' --run-id continuation-source-gate-valid-20261023-r5 --hide-cwd --header-route --source-gate
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/harness-continuation-audit/verify.py'
```

五个批次Owned Job均 `remaining_pids:[]`，各自Host／control／Boundary三个端口均关闭。最终r5清理前所属PID为 `[14372,17052]`。child TEMP／TMP、DSH_HOME与native cache都位于独占cache；不按进程名查杀，不触碰既有实例。

未覆盖：真实模型、正式业务工具权限、任务重启／恢复、跨run并发竞争、图片tool-result、真实skill目录中途更新、provider切换及实际桌面。required runtime-context混合已实际通过；skill-catalog混合在wire上具有相同文本结构，但其动态更新本次 **not_run**。生产源门槛、绑定恢复与该函数须由集成负责人迁入后在正式fixture复验，再记录适用的真实模型证据。
