# 原生业务skill／工具桥候选与固定SEA实证

日期：2026-10-23。状态：**独占候选与本地实证完成，未修改产品文件。** 17个场景、32次本地Messages，69项保留证据／授权检查通过；真实provider请求0。唯一成果是独占缓存中的合成staged候选，不是正式课程成果。

所有代码及原始证据位于 `.local/cache/workbench/native-business-candidate/`。没有修改backend、harness、locks或私有凭据，没有读取用户素材或重启用户Host。实际重建的是本probe自己创建的Host和Boundary。

## 交付内容与使用边界

| 文件／目录 | 责任 |
| --- | --- |
| `baseline/`、`baseline-manifest.json` | 开工时冻结的当前guard、profile、已落地continuation模块及执行依赖，共11个文件。 |
| `candidate/harness/plugins/learning-guard.mjs` | 可审阅的原生skill、固定两工具、按Session scope、source gate及私有bridge适配候选。 |
| `candidate.patch` | 只修改上述guard的LF diff；`git apply --check`通过，只检查未apply。 |
| `fake_bridge.py` | **纯测试假桥，不得迁入正式运行。** 只读合成source表，暂存合成stage JSON，验证元数据／对象／版本准入；不代替真实业务授权或数据库事务。 |
| `probe.py`、`schema-shadow-fixture.mjs` | 实际固定SEA runner及纯负例注入。没有新通用插件平台或供应商执行层。 |
| `native-business-20261023-r2/` | 最终实际运行、原始请求／SSE、Session page、guard audit、假桥事件与两次Host清理记录。 |
| `verification.json` | 69项检查和逐场景结果；区分真实Host链、直接HTTP授权负例及授权核心回放。 |

候选只调用锁定Harness的既有接口和Node内置模块，不新增依赖。正式集成必须把选定实现迁入正式模块，不能import缓存／probe。根代理负责真实业务bridge与Scope registry；本报告不把假桥成功写成产品功能或G07真实模型验收。

## 1. 固定API

原生注册一个静态项目skill `course-read`：既支持模型先读目录后调用 `skill({name:'course-read'})`，也支持用户 `/course-read`。正文只指引读取明确授权handle，不扫描目录、不自动联网、不自动读取课程全文。

两个业务工具的模型参数是封闭JSON Schema：

```text
read_source_handle({handle_id, source_version})
stage_result({task_id, object_id, expected_version, stage, body})
```

没有path、URL、course、shell、用户作答或任意方法参数。`stage_result`只暂存指定任务候选，不发布、不替用户作答或评分。候选body上限4096字符只是fixture固定参数，正式大小由根代理按真实产物契约冻结。

工具body从私有可信policy取身份，向唯一认证loopback业务路由发送：

```json
{
  "session_id": "<owned-session>",
  "run_id": "<frozen-run>",
  "request_id": "<frozen-native-request>",
  "epoch": 1,
  "call_id": "<exec.callId>",
  "tool_name": "read_source_handle | stage_result",
  "arguments": {}
}
```

`course_id`不从模型或该envelope决定；服务端按Session/run/request/epoch找冻结授权。`tool_name`只识别两个固定DTO，禁止反射任意函数／路径。原生 `skill` loader不走业务桥。

私有路由仅POST、固定path、精确Host、无Origin、严格Content-Length／JSON及独立随机token认证。token只在后台与所属Host内存／子进程环境，不入模型参数、响应、前端或公共报告。候选只把额外允许的本地业务端点加入现有网络guard，业务服务器再核路径与认证；没有打开任意网络。`fetch`传递 `exec.signal`、禁止redirect、没有HTTP隐式重试。

成功读取返回文本与 `{course_id,source_id,source_version,page}` SourceRef，并保留 `content_role:'original'`。成功stage返回receipt、任务／对象／预期版本／stage和 `content_role:'ai_generated'`；假桥保存其已授权实际读取来源。AI结果没有被反向标成原始教材。

## 2. Session policy与原生scope

在现有八字段routing／模型policy上固定增加四字段：

```text
course_id, purpose: qa | task | translation, task_id: string | null,
allowed_tools: exact fixed tool names
```

它们与 `session_id/run_id/request_id/epoch`共同绑定。数组排序后冻结并按值比较，相同恢复登记幂等；冲突拒绝，不默默改变活动Session。非task policy不能授予stage_result，translation必须空工具。模型／思考／输出／超时仍使用用户已固定的设置，fixture仅用Flash／off／1024／45秒，不测试自动选模型。

原生实现：

- 注册可信definition对象，`agent.ctx.tools.restrict({allow:allowed_tools})`控制继承可见性；执行guard另核agent对象、按agent allowlist、精确definition和参数。
- 系统组装及每次 `llm/stream`都核工具名集合与canonical schema digest；同名shadow在发HTTP前拒绝。
- translation在新的独立Session中通过已实证的 `await agent.ctx.inject(['skills'], scope=>...)`注册同名、model/user invocation均false的scoped skill；并限制tools=[]。
- filesystem、sandbox、pwsh-sandbox在probe composition中关闭；working-directory服务保留，由原有scoped空context抑制隔离任务cwd。普通问答不在应用层预检索。
- 保留现有native user marker/rpcId source gate及已落地的header/call-chain continuation；不加HMAC、不改消息正文、不删user／历史／runtime／catalog。

实际role schema：QA为 `read_source_handle + skill`，task为三工具，translation为[]。`allowed_tools`是可信后台输入，不是模型可请求的权限升级字段。旧六／八字段policy不自动获得业务工具；正式新业务请求必须先完整登记。

## 3. 实际模型协议链

所有“模型响应”均由确定性本地Messages服务脚本化，不表示真实模型自主决策或译文／学习质量通过。

| 场景 | 实际结果 |
| --- | --- |
| 普通QA | 1次Messages，业务桥调用0；没有source正文或完整skill正文。skill摘要可见，不存在应用层自动读。 |
| 模型按需skill | 原生skill加载→读取hA/v1→继续回答，共3次Messages；读取结果含真实合成文本与SourceRef。 |
| 用户 `/course-read` | 原生invocation在首个请求前注入；读取→回答共2次，无额外模型加载请求。 |
| 明确task | 同step读取hA、hA2→指定object_A/v1的stage_result→回答，共3次；两个工具结果合为下一次单个Messages。 |
| QA强行stage | schema不含stage_result，执行guard拒绝，业务HTTP调用0。 |
| 参数扩展path | Host执行前拒绝，业务HTTP调用0。 |
| translation选区含 `/course-read` | 1次Messages，tools=[]、skills/list=[]，没有catalog／skill-invocation／正文／业务HTTP；只保留选中文字及既有固定系统／路由信息。 |
| 同名schema shadow | 0次Messages，原生turn error为 `LEARNING_BUSINESS_SCHEMA_IDENTITY`；业务调用0。 |

task成功时bridge的call ID依次与原生调用一致：`read-task-good`、`read-task-second`、`stage-task-good`。读取及stage后分别追加required `BUSINESS_CONTEXT_REVISION_*`，真实wire user同时包含tool_result与附加文本；已落地continuation正确归原run，source gate继续核原始用户消息。

## 4. 后台拒绝与幂等实证

下列场景实际经原生工具body调用假桥，返回稳定错误并进入原生错误tool_result；下一次fixture请求继续，未取得被拒正文或写入：

| 负例 | 错误码 |
| --- | --- |
| 同课但未授权handle | `SOURCE_HANDLE_DENIED` |
| 请求版本不符 | `SOURCE_VERSION_MISMATCH` |
| 来源已换版 | `SOURCE_VERSION_CHANGED` |
| 跨课程handle | `SOURCE_COURSE_DENIED` |
| 未授权task | `TASK_SCOPE_DENIED` |
| 未授权目标object | `RESULT_OBJECT_DENIED` |
| 过期expected_version | `RESULT_VERSION_CONFLICT` |
| 未授权stage／publish | `RESULT_STAGE_DENIED` |
| 迟到旧epoch | `EXECUTION_EPOCH_STALE` |

另有直接认证HTTP负例：无token／带Origin拒绝，自填course元数据因envelope多字段拒绝，inactive回调拒绝。同 `(run_id,call_id)` 与相同payload返回原receipt；冲突payload返回 `TOOL_CALL_CONFLICT`。stage计数始终1→1，没有第二份候选。

直接授权核心回放还拒绝伪造session/run/request、布尔epoch与布尔版本；这部分不冒充真实模型链。唯一保存候选属于 `course_A/task_A/object_A/v1/chapter_draft`，role为ai_generated，来源是hA/v1及hA2/v1。跨课／未授权／已变更来源正文从未进入任何实际Messages。

切课本身不应让合法旧任务改写新课：bridge只认原冻结course，完全不读取当前UI课程。这里拒绝的是过期epoch、inactive任务和越权目标，不是把所有切课后的旧任务结果一概丢弃。

## 5. 实际重建后仅对账

先验证同一Host内相同policy重复登记200、scope未重复挂载、follow/page找回原rpcId，Messages保持32→32，新增prompt为0。

随后实际关闭本probe所属Host与Boundary，保留它自己的DSH_HOME／ledger；新Host和新Boundary在启动／follow前恢复17份冻结policy及owner映射，连接原Task Session，仅follow/page查询：

```text
original_rpc_found = true
completed_turn_found = true
messages_before = messages_after = 32
new_fixture_requests = new_bridge_requests = new_prompts = 0
policy_count_restored = 17
```

这是“模拟业务回执未知、Host已有完成事实”的真实本地恢复证据。没有为unknown建立新requestId，没有补发原问题。业务假桥在此期间保持自身授权状态；**不表示真实业务DB恢复、生成中断的全部故障窗口或所有冷scope元数据已验收。**

首个所属Host PID `[23988,24244]`、恢复Host `[22796,23652]`均退出。两组Host／control／Boundary端口关闭，最终business端口也关闭。所有child TEMP／TMP、DSH_HOME、native cache和数据均在独占目录，只管理所属Job。

## 6. 根代理必须实现的正式授权点

1. **Scope注册来源**：从正式提交／TaskRun／课程与冻结范围构造policy。Host私有policy DTO需透传四个业务字段，Boundary `allowed_tool_names`同步；真实源授权不以Host字符串policy代替。
2. **handle发行与读取**：由正式SourceStore／受限检索返回不可越界handle，核当前course、source_version、内容角色和允许页／区域／字幕范围。文档局部、附近字幕、全范围任务、视频图像许可不能混成“同课任意读”。候选只有固定文本，无视频／屏幕／图片能力。
3. **stage目标授权**：从持久task/unit查指定object、stage及预期revision，拒绝任意路径／文件／用户作答／复习评分。每次IO前重新核run、epoch、task状态及对象／来源有效性；Node检查不是最终权限。
4. **事务／未知**：真实任务单位、资产修订、receipt、来源链与索引事件应由正式事务／outbox衔接。相同call的receipt幂等还不能代替跨call的unit操作身份；不同call语义重试、已写未回执故障尚需根实现。unknown先查receipt／原生账本，不自动调用工具或模型重发。
5. **用户修改保护**：真实版本冲突需要保存双方候选、版本／撤销及可处理状态。假桥的版本拒绝不等于已经实现编辑冲突、保存／索引、产物发布或完整覆盖核验。
6. **原生scope完整性**：固定并审计全部项目skill roster，禁止未知provider补入未受限的/user invocation；await scoped Fiber ready，按实际agent对象去重并清理。translation用从未开放过资料skill的新Session；冷会话metadata读取也按policy处理，不盲用全局skills/list。本次重建对账目标为允许skill的task-good，未验证translation冷目录。
7. **生命周期**：本候选保持同sid policy不可静默替换。同一原生聊天sid跨新generation的显式活动绑定转换，需根代理在既有对话语义下实现；不能为绕过冲突而丢历史、重发unknown或把旧回调记入新run。

根代理可把候选中的有限工具注册／scope／transport拆入正式小模块；不要把假桥的合成表、脚本响应或JSON测试存储当正式权限实现。正式profile需显式开启native tool-skill并保留受审查provider组合；仅应用guard diff不能代替该组合与业务服务准备。候选启用前需由后台给所属Host设置私有 `LEARNING_HARNESS_BUSINESS_URL`／`LEARNING_HARNESS_BUSINESS_TOKEN`；缺失会拒绝启动，不可先把补丁当成兼容旧环境的无条件替换。

## 验证与限制

实际成功命令：

```powershell
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/native-business-candidate/probe.py' --run-id native-business-20261023-r2
& '.\backend\.venv\Scripts\python.exe' -B '.local/cache/workbench/native-business-candidate/verify.py'
node --check '.local/cache/workbench/native-business-candidate/candidate/harness/plugins/learning-guard.mjs'
git apply --check '.local/cache/workbench/native-business-candidate/candidate.patch'
```

EXE启动前实算并核固定SHA256 `f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365`。32份原始请求hash与ledger相等；所有实际出站模型参数为固定fixture值，无图片。上游源码、实际制品、纯回放、固定Host与真实模型结果分开记录。

r1业务矩阵已通过，r2新增实际owned Host重建。原始两批证据均保留。首轮verify缺少 `copy` import，在后续授权核心回放前失败；`verification-first-failure.json`留存，修复后只回放已有证据，未重跑／覆盖SEA结果。

未覆盖／not_run：真实provider、实际资料与学习质量、真实SourceStore／TaskStore／ArtifactStore、导出／索引发布、业务bridge重启授权恢复、活动停止、并发业务编辑、不同call语义去重、translation冷目录、同聊天sid跨生成转换及13场景端到端产品验收。69项通过仅支持这个固定API／原生执行候选，不能据此写13场景或G03／G04／G07产品验收完成。
