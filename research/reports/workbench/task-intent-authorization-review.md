# 普通问答到学习成果任务的授权接缝

日期：2026-10-23（Asia/Shanghai）。本次仅设计审查，唯一新增正式文件为本文；未修改产品、锁或用户实例，未调用模型、启动Host或读取用户素材。方法遵循已应用的REA源码／制品证据区分，并用codebase-design的Module／Interface／Seam梳理责任。

**建议同时保留三条入口，共用一个TaskIntent Module：Task按钮提供结构化用户动作；原生`/name`提供skill加载入口；主回答模型通过窄`start_task`承接自然语言明确生成／修改。QA不直接获得成果写权限。只有服务端建立已知task、目标object及预期version后，独立TaskRun才能暂存并发布该任务成果。** 明确生成请求本身已允许默认保存，不再逐块审批。

自然语言是否“明确要求生成”属于语义判断。绑定真实typed_question能证明判断依据来自哪次用户输入，不能把该判断变成确定性ACL。服务器必须确定性限制可读来源、可写对象、修订、执行归属与幂等；语义误判则需要主模型规则、保守的歧义处理及实际模型验收，不能用关键词、精确引文或confidence冒充已解决。

## 1. 本次依据与已知实证

已重新读取本次目标附件、[13场景产品收口](../../../docs/product/13场景学习工作台需求收口-2026-10-23.md)、[统一实施方案](../../../docs/plans/13场景统一交付实施方案-2026-10-23.md)的来源／窄写／恢复与验收规则，以及[native候选报告](native-business-candidate.md)。本次实施授权覆盖旧文档“只写文档”，不覆盖用户素材、原件只读、模型选择或未知不重发约束。

主agent提供的OCR阅读14／21、字幕桌面组合12项保留各自范围，本报告未重跑。native候选报告的17场景、32次本地Messages、69项检查证明固定SEA中的有限工具、scope和本地恢复可行；真实provider为0，假桥不证明正式TaskStore／ArtifactStore授权或自然语言意图正确。

| 已核事实 | 设计含义 |
| --- | --- |
| 候选只有`read_source_handle`、`stage_result`两个业务工具；`purpose=task`及task/object/version来自probe预设。QA无stage，translation空工具。 | 候选已验证“给定授权后执行”，尚未解决普通QA如何合法取得任务授权。不能因为模型提出stage就补建task。 |
| 参考源码`tool-skill/src/index.ts:411–428`实际扫描直接user消息所有text block中任意空白分隔的`/name`；165行的“首行”注释与实现不一致。 | `/name`是确定性的加载规则，不是确定性的业务写意图。代码块、否定句或教材中出现token也可能命中。 |
| 同文件179–205行根据该扫描注入`source.kind='skill-invocation'`；skill工具加载和用户调用使用相同正文形态。 | 看到skill正文、加载成功或skill-invocation元数据，不等于已获成果创建／修改许可。必须查原始业务提交。 |
| 当前`execution/adapter.py:311–320`把marker、历史context和组合prompt送入同一`session/prompt`内容；`storage/repository.py:916–931`将引用／条件拼入prompt。 | 不能把Host里的user role或最新user消息全文当成原始typed_question。程序展开的资料／历史中的`/name`不能成为写授权。保留现有marker与原始输入，不用删正文或新增签名替代来源区分。 |
| 候选按sid冻结policy；报告明确未验证同聊天sid跨新generation转换、真实业务恢复、不同call的语义去重和translation冷目录。 | 正式集成要完成这些Seam；不能直接apply候选就称原生业务桥通过。 |
| `ArtifactStore.publish_run_result`已按独立run、来源、作者与唯一发布核验；目前建立新成果。现有`update`路径是用户编辑，修订author为user。 | 新建可复用已有发布事务；模型修改已有成果还需已授权target/base revision/block集合及author=model的内部Interface，不能借用户编辑路由冒充。 |

参考源码为`687f282e…`，锁定SEA客户端build为`332895e`。上表源码事实不冒充实际build全部行为。缓存中的候选和fake_bridge只供审阅，不是正式运行依赖。

## 2. 三种入口的允许边界

| 入口 | 能证明什么／不能证明什么 | 允许的产品流程 |
| --- | --- | --- |
| 原生`/name` | 可证明固定loader识别并加载了特定skill；只有进一步核实原始用户提交或真实客户端结构化动作，才能证明是哪次用户操作。当前全text扫描本身不能区分“请生成”与“解释`/生成命令`”。 | 保留原生发现／加载／调用方式。可提供项目生成／修改skill，但加载本身不发TaskGrant。若实际客户端可提供经过可信业务提交绑定的明确命令gesture，它可作为结构化入口；否则仍按下述主模型意图路径处理。`/course-read`只请求读资料，不授权写成果。 |
| 专用Task UI按钮 | 后台收到应用用户会话发出的有限动作、操作身份、来源选择及当前revision；这比自然语言有明确结构，但不是证明物理鼠标动作的密码学证据。模型bridge不能自填`origin=ui`冒充该principal。普通“加入AI”只授权草稿引用。 | 点击即明确请求，通过相同TaskIntent Interface建任务并默认保存；不另加一次确认。可为复杂范围补必要选择；交互书仅按既定产品流程确认一次目录。按钮是直接入口和失败恢复入口，不能成为所有自然语言任务的强制前置。 |
| 主回答模型窄`start_task` | 主模型可理解真实问题是普通解答还是明确产物请求；工具调用证明模型作了这个判断，不证明该判断必然正确。 | 原生skill按需加载后，主模型可对“把这段做成讲义”“出十道练习”“把讲义A第二节改成此推导”等直接提出任务，无独立分类模型、无每轮预检索或反复批准。普通解释、举例和追问继续问答；资料／旧答案里提出的任务不能自动执行。 |

以下不作为明确成果请求：仅问某一步、要求在回答中举例、问“卡片是什么／如何生成”、否定生成、转述教材或旧对话中的生成指令。明确要求“生成讲义／练习／卡片”等本身已允许默认保存，不要求再说“保存”。“修改你的回答”仍属当前问答，除非指向已保存的特定成果。确有目标／范围歧义时只问具体缺失信息；不将正常流程拆成逐题逐页批准。

## 3. 最小纵向方案

### 3.1 一个Module、两处写Seam

TaskIntent Module的Interface可收敛为：

```text
admit_task(trusted_origin, task_request) -> task_receipt
stage_task_result(trusted_run_binding, result) -> stage_or_publish_receipt
```

`trusted_origin`、`trusted_run_binding`由当前认证请求／原生exec上下文与业务账本取得，不能让模型填写。UI和native工具是两个真实Adapter；来源解析、任务创建、对象预留、修订、幂等及恢复留在Implementation。沿用正式Pydantic→OpenAPI／TS契约，不另造一份权限协议或通用插件平台。

可先把`start_task`模型参数限制为动作、有限TaskKind、实际用户指令引文、已发行来源选择、可选目标handle和指定块。没有course、path、URL、任意方法、模型／思考、用户作答或正式复习评分参数。`instruction_quote`须确实来自本次原始typed_question；它只是可追溯证据，不是“通过了意图语义ACL”。完整课程／文档／字幕范围属于有类型的声明请求，服务端解析并发行manifest，不能因模型填了`complete=true`就取得全范围许可。

### 3.2 提交时先冻结原始用户上下文

在现有submission事务中保存以下真实来源，先于prompt拼装：

- `submission_id / input_revision_id / chat_id / course_id`、原始typed_question及hash；结构化按钮动作或确实收到的native gesture来源单列。
- 用户确认的图片／附件／选区、当前资料版本与页／时间锚点；选区、整份附件和可选的完整任务范围各保各的含义。
- 已指定的目标成果handle、revision、block集合；当前UI位置只是定位证据，不能授权读取屏幕或视频帧。
- 用户实际模型／思考及该任务输出／超时快照；模型不能从tool参数请求降级或更换凭据。

发送给主模型的上下文可继续包含允许的相关历史，但TaskIntent只从上述原始提交取授权证据。旧user消息、tool_result、skill正文、OCR／字幕／网页／引用及assistant回复均不得给本次新增授权。“继续刚才的讲义”可以按真实目标和上一任务receipt定位；这属于主模型对当前指令的理解，不能自动复活已暂停任务。

### 3.3 主模型提出任务，服务端只在自己的Interface准入

普通QA的工具集合可按实际skill加载机制包含只读工具和窄`start_task`，**始终没有`stage_result`**。候选现有schema未包含start_task，正式加入后要重做schema pin与实际SEA组合验证。

服务端查当前原生call所属的真实submission／run／request／epoch，核指令证据、允许的TaskKind、来源与目标。语义判定记录为`origin=main_model_semantic`及相应规则版本，不伪填为UI gesture。原生或UI结构化动作则记录其实际来源。确定性拒绝伪造引文、旧提交、过期epoch、越课来源、错误对象和非法version。

准入在一个短事务中保存TaskIntent、Task、范围／输入快照、预留目标和操作receipt，提交后再排队。创建请求不包含成果正文；预留object无正文、不可索引，不冒充已生成成果。成功返回已知`task_id / object_id / expected_revision / scope_revision`和当前状态。QA给出原生任务入口即可，不在自己的会话中升级为可写角色，不用普通QA generation填TaskRun成果。

自然语言“整课”若已经明确当前课程，程序可枚举该课程当时版本的指定资料及有效字幕并冻结manifest；不必再让用户逐文件确认。模糊章节／重名资料／没有可确定目标时返回具体`needs_scope / needs_target`，不能偷偷取全课程来补。跨课程、外部研究及字幕全范围分别需要明确请求证据，不能由一次同课搜索未命中推导。

### 3.4 独立TaskRun执行与自动发布

复用现有TaskExecutor与`prepare_task`，在原业务聊天下关联独立执行Session，不新建业务聊天。TaskRun继承用户冻结设置和允许输入。translation仍是只含最终选中文字的独立Session，tools=[]，不载学习聊天历史或资料skill；选中文字里的`/name`也不触发工具。

后台创建仅该run可用的TaskGrant，至少绑定task／unit／object／阶段／预期revision／scope revision／source proof／模型参数与原生身份。模型写入只经`stage_task_result`，普通QA即使知道这些ID也不能使用此grant。task policy由服务端建立，模型不能通过参数把自己的purpose从qa改为task。

stage是同一明确任务的候选保存，不是新的“另存”按钮。结构、来源、完整性和执行终态可核后，服务端自动发布独立资料及索引；未完成／未知保存任务稿并明确状态。卡片保存与入队分别有回执，不能因stage成功宣称评价正确或已掌握。

### 3.5 修改成果必须先有目标和base revision

新建由服务端预留object及初始revision语义；修改从真实用户所指成果handle取得`artifact_id + expected_revision_id + allowed_block_ids`。模型不能把“当前课程”当成可改任意成果的范围，也不能改原始教材文件、用户作答、手改字幕或正式复习评分。

新增内部的模型修订落点，复用现有不可变revision／冲突／撤销事务，但修订author必须是model，内容身份及来源链保留。写指定部分，其他块沿用base；手动编辑改变同一目标时保存AI候选与用户版本，返回冲突，不能取最新head后暗中重试覆盖。后续本任务自己的块提交可沿可信receipt推进其预期revision；人工修改不是可自动追随的新base。

候选`expected_version`是fixture整数，正式应适配已有不可变revision身份；不能把测试中的v1或当前head字符串转换成一个未经核对的序号。

## 4. 必须由服务器执行的guard与来源proof

| 权限域 | 可信依据与执行检查 |
| --- | --- |
| 原生exec身份 | 认证私有bridge＋精确sid/run/request/epoch及真正agent／definition对象；业务DB再查归属、状态和冻结输入，不能仅信Node policy字符串或模型参数。保留现有marker/rpcId source gate及header/call-chain continuation。 |
| 资料选择／整份附件 | exact source snapshot／attachment回执、课程、source_version、hash、允许页区／全文范围、内容角色；只读handle不能反向扩大到父文档或祖先来源。相同文字不同来源不共用身份。 |
| 课程检索 | 主模型通过原生skill按需检索；服务器限制默认课程、排除状态、版本、结果量和等待。新读取重核源及排除状态；无命中／未就绪不联网，不恢复找题。 |
| 日常附近字幕 | 本次提交绑定的asset/version/time_ms，而非工具调用时的播放位置；服务器发行有界窗口、有效字幕revision与segment集合，核每次及本turn累计读取范围。不能反复翻页把附近权限穷举成整课。 |
| 完整字幕任务 | 单独的full-transcript task grant，绑定选定视频版本、有效字幕修订／手改head、时间范围、全部预期单位和缺口；原始转写及手改身份分开。任务许可不写入QA的长期allowlist，不开放视频帧。 |
| 成果写入 | 已存在TaskIntent／TaskRun与目标object／阶段／预期revision；服务端决定课程、kind、作者及允许块。每次IO核task状态、scope、来源和version，schema／提示词均不能代替。 |
| 来源身份 | 返回并保存original／user_note／ai_generated／mixed、实际SourceRef与引用快照。生成讲义可索引，但不能成为原教材证据，也不能授予读原始整课字幕的能力。 |
| 来源变化／缺口 | 旧引用和候选保留；新读取不指新版；完整任务缺口暂停并留稿。重试／排除／接受部分使用已明确的产品选择和范围修订，不自动降级为完整结果。 |
| 版本与并发 | 目标revision／block与读取proof在任务准入时绑定，发布时CAS；旧epoch、旧scope、跨课伪造或手改竞争拒绝或保留冲突。暂停不接收新的派发，已受理结果可按原归属安全落稿，不以丢弃结果替代停止。 |

skill目录、模型意图声明、源正文、call_id都不是独立权限。只读ASR编辑／准备的用户功能也不能因可用而变成模型改字幕工具。输出可视化／离线包始终没有上述能力或业务凭据。

候选的`read_source_handle`只能读已发行的handle，参数里没有检索词或字幕锚点，因而它还不是完整的课程检索／附近字幕Interface。正式按需skill需接有界的课程搜索／附近字幕定位，返回可读handle，再由现有窄读消费；必要时增加少量具体只读工具并重新固定schema／guard。不能为迁就候选两工具，在应用层每轮先查好资料，也不能把已知handle的读取通过称作知识库检索通过。

## 5. 幂等、原生生命周期及用量

TaskIntent必须有业务幂等身份，不能只靠`(run_id, call_id)`。同一提交的同一请求项在不同call语义重试时要取回原task；不同内容返回冲突。明确要求多个成果时，为实际请求项建立稳定slot或冻结的子项身份；不能为防重复把多项请求删减成只许一项，也不把每个句子都自动扩成一个任务。

首次创建／stage／发布均先持久receipt再返回；丢回执先GET原operation、查原TaskRun及原生事实，unknown不改requestId或自动POST。已有outbound ledger记录actual attempts、工具续答及所有额外调用；raw usage／来源／字段存在性／结算身份继续保留，缺失未知、不重复累计。模型语义判断由现有主回答模型承担，不偷偷再调用分类模型。

同一业务聊天继续原生交互，独立TaskRun另持其执行绑定。QA下一问必须显式切换当前sid的活动run policy，不能直接覆盖候选的sid map，让旧exec读到新grant。恢复先恢复持久owner、scope、receipt和policy再follow/page，不重发旧用户问题。当前候选已证实的是相同policy重登和一个允许skill的task会话恢复，不能外推跨generation转换及translation冷目录。

用户设置以原提交／明确按钮动作的快照为准。任务执行中切换UI模型不反改已运行任务，缺少对应能力要明确拒绝，不能丢题图或降级。普通停止只定位所显示生成／TaskRun，保留已有内容和原归属，不杀Host或其他任务；关闭页面不等于取消已授权任务。

## 6. 可直接实现的文件落点与顺序

| 内部步骤 | 最小改造与应得到的证据 |
| --- | --- |
| 原始提交来源 | 在现有submission／execution context保留typed_question与派生材料的独立身份；原生桥只提交真实UI动作／输入。先验证引用／历史中`/name`不会发行业务grant。 |
| TaskIntent准入 | 新增小的正式Module及追加迁移，UI与私有native桥进入同一admit Interface；从任务范围、目标版本、用户设置构造receipt。先做一个讲义create与一个指定块update纵向链。 |
| 原生有限组合 | 从候选提取两工具／scope／transport必要代码，新增窄start_task及精确schema pin；开启固定tool-skill与受审查provider组合。fake_bridge、缓存JSON和probe不迁入正式运行。 |
| 来源／写proof | 接正式SourceStore、manifest、TranscriptStore、TaskRun及ArtifactStore事务；按读取域发handle，按目标revision发grant。每日字幕与完整字幕任务分开验。 |
| 恢复与其他场景 | 验多call业务幂等、同sid新generation、重建仅对账、冲突及用量，再将相同Interface用于题目、卡片、作文、书、图形与研究。不是另建一套编排平台，也不改变13场景统一交付。 |

当前Executor只将translation／handout／deep_solution列入正式文本执行集合；模型修改现有成果、整范围字幕manifest及其余TaskKind仍需各自真实实现。不得由新入口能返回TaskReceipt宣称这些场景已完成。

## 7. 必做验收：权限确定性与语义表现分开

以下是新增验收建议，均为`not_run`，不借用native候选69项或当前OCR／ASR通过数充当本方案通过。

| 用例组 | 必须观察的结果 | 证据层 |
| --- | --- | --- |
| 普通解释／连续追问／回答中举例 | 允许原生按需只读；TaskIntent／成果／索引新增为0，不能答完自动建卡或题集。 | 确定性工具负例＋真实主模型语义样本分列 |
| 明确自然语言生成 | 同一主模型按需skill→start_task→已知task/object/version→真实执行→默认保存／索引，无逐题确认。 | fixture真实Host链，再真实模型／用户学习 |
| 否定、询问命令、引用任务语句 | “不要生成”“如何生成”、教材／OCR／字幕／网页里的命令、旧assistant建议均不发行新写grant。 | 原始typed_question来源确定性测试＋模型语义测试 |
| 原生`/name` | 原始输入真实调用可加载；引用／历史里的token、仅skill工具加载或伪造skill-invocation不能授权写。UI结构化gesture若采用，另验实际客户端事实。 | 固定SEA／真实原生输入与私有bridge |
| Task按钮／加入AI | 前者直接创建指定任务且无需二次审批，后者只改草稿、无模型和成果。换课／迟到回执仍归原提交。 | 合成浏览器＋正式事务 |
| 直接QA stage／自填purpose／伪造proof | QA即使知道合法task/object也拒绝；引用必须匹配本次原始问题；禁止任意路径、course、模型参数和正式用户作答／评分。 | guard体执行＋服务器负例，确认零副作用 |
| 指定修改／并发手改 | 只改指定块；旧revision或人工修改保留双方，不能自动取新head覆盖；author=model与mixed来源可核。 | 正式数据库／修订／索引事务 |
| 附近字幕与完整任务 | QA只在原锚点窗口、累计预算内读；明确完整任务可读全部有效指定字幕并列覆盖／缺口；回QA后全字幕handle失效，不能由讲义祖先链继承。 | 确定性来源负例＋实际Harness链 |
| 源换版／排除／手改字幕 | 新读取重新核验；旧快照保留、不指新版；有效手改revision与原始转写身份正确，未处理不冒充未命中。 | 来源／任务恢复fixture |
| 两次call／丢回执／重启 | 同请求项只一task／一目标发布，跨call回执幂等；不同payload冲突；Host／bridge／DB重建仅对账，无新prompt。 | 故障窗口＋固定Host真实重建 |
| 同sid下一轮与活动停止 | 旧QA不能用新policy；旧任务可继续原课程；取消对应执行，Host存活且可续问。 | 原生活动链，idle cancel不替代 |
| 模型／思考／translation | 子任务保用户冻结设置；选中文本含`/name`仍tools=[]、无聊天／相邻资料／原始帧；重连不降级、不重发，usage未知保留。 | 实际wire／attempt／usage与真实模型分列 |

语义样本的通过只能证明被测模型／输入／版本的表现，不能宣称对任意自然语言形式形式化证明“普通QA永不被误判”。确定性ACL测试则应在恶意模型参数／不可信来源存在时仍能阻止越课、越对象、越version和重复写。这两类结论必须分别报告。

## 8. 尚需源码／实际制品验证处

1. 固定SEA是否提供足够可靠的用户原始输入／真实gesture出处，尤其项目组合prompt的历史与引用是否被原生`/name`扫描；不能只读参考源码注释。
2. 新start_task的动态schema及同sid跨generation／模型选择转换，旧exec的grant捕获、scope Fiber清理与恢复顺序；候选目前不包含该工具。
3. 真正SourceStore／TranscriptStore的handle发行、完整字幕范围及改判／手改／排除后再读；当前字幕view分页不是普通QA附近读取的授权证明。
4. 正式stage／发布／模型修订与outbox／索引、跨call幂等、scope变更和丢回执恢复；假桥的单call去重不能替代这些。
5. translation从未开放资料skill的新Session与重建冷目录；其他任务虽允许skill，也不能把其既有完整范围带回QA。

以上是已授权目标内的实施接缝，不是新的产品审批清单。按证据实现、验证和修复即可；工作包状态仍只由集成负责人更新唯一MVP计划。
