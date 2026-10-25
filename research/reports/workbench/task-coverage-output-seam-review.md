# Task Module：来源覆盖、生成工作块与成果发布接缝

日期：2026-10-23（Asia/Shanghai）。独立只读设计审查；已读实际`tasks.py / runs.py / executor.py / artifacts.py / models.py / schema.py`及[统一实施方案](../../../docs/plans/13场景统一交付实施方案-2026-10-23.md)。只新增本文，不改产品、锁、迁移、测试或用户实例；未运行Host、模型或素材处理。下述改造与新增验收均未执行，不维护第二套工作包状态。

**建议保留现有TaskExecutor、CoreRun、Harness接入和用量账，只拆开三个身份：SourceLeaf负责范围与覆盖，WorkUnit负责一次生成工作，TaskTarget负责同任务的一份主成果。保留`task_units`作为WorkUnit并追加独立来源账；translation映射为一work、一份精确选区、一个target。完整讲义、章节与比较研究按内容及输出预算组合多个来源，不能按页强制调用模型或每run创建新资料。**

## 1. 当前耦合的确切位置

| 位置 | 当前行为 | 对完整任务的限制 |
| --- | --- | --- |
| `models.py:313–316,381–409`；`schema.py:97–109` | TaskUnit必须有一个source_snapshot_id，并直接持run_id、result_artifact_id。 | 来源叶、生成工作和成果三者被做成一对一。 |
| `tasks.py:97–175` | 完整manifest.units逐一变成task_units。 | PDF页数直接决定生成单位数；source准备失败与输出失败容易共用needs_sources。 |
| `executor.py:125–136,157–205,207–226` | 每次取下一unit，prompt只收一SourceSnapshot，再claim该unit的run。 | 一个比较问题无法同时用A/B，整课整理自然退化为逐页／逐字幕片段回答。 |
| `runs.py:116–191`；`common.py:24–52` | input_hash和scope核验只绑定单个snapshot；claim即把unit置read。 | 无法证明多来源输入集合／长页片段覆盖，prepared不能冒充实际请求已包含该内容。 |
| `artifacts.py:124–199` | 一个run只能引用该unit的单snapshot；完成后`_create`新成果并存回unit。 | 多work会制造多个独立资料，不能连续填同一本书／讲义，也没有目标块的模型CAS。 |
| `executor.py:298–350` | 完成run才组装ArtifactBody；默认part编号每run重来，title附unit label。 | 直接改成“合并到同artifact”会发生block_id碰撞或整份body覆盖；需明确patch槽位。 |
| `tasks.py:236–262,348–402` | outline是JSON及确认gate；完成取决于所有active unit有成果。 | 没有章节工作、输入覆盖、块发布或冲突是否解决的独立判据。 |

这些是初始单选闭环的适配边界，不否认其translation、独立run、活动取消、unknown对账、raw usage和事务故障证据。不能仅修改prompt文字来解决数据身份耦合。

## 2. 先固定四个不变量

1. **SourceLeaf不是一次LLM调用。** 一页／一个有效字幕时间片／一个成果block可作为覆盖叶；一个run可读多个叶，一个很长的叶也可分成多个有连续偏移的输入片段。空白页／静音片段若有可信本地完整处理证据，可确定性处理，不为凑覆盖调用LLM。
2. **WorkUnit不是一页或一个引用。** 它是讲义一节、书的一章或一批正文块、研究的一项比较等输出工作。可以依赖多来源，并按实际上下文／输出／超时预算有限切块；不默认增加规划模型、多代理或额外校验。
3. **一个Task预留一个主TaskTarget。** 多work向同一主成果提交完整块批次，先完成的章节可读／可编辑；未完成状态独立显示。书中练习／卡片引用各自正式业务对象，不另造题库，也不把每页产物变成另一份学习资料。
4. **覆盖、执行与发布分别有账。** 已解析不等于已送入成功执行，模型完成不等于正文已发布，已发布一章不等于完整范围完成；也不因来源曾被引用就证明全文都已处理。

## 3. 最小表改造：四张新表，复用其余账本

以下是建议结构，不是第二份正式schema。字段用Pydantic／SQLAlchemy源码落地，再生成公开契约。追加下一版迁移，0001～当前0014等已存在迁移均不改。

| 表／改造 | 最小字段或关系 | 责任 |
| --- | --- | --- |
| 新`learning_task_sources` | task_id、source_id；原始asset/version/digest或artifact/revision；用户声明范围；有效字幕track／参数／手改head；enumeration_state、expected_leaf_count可空、错误／检查点／参数版本 | 在慢解析前持久化用户已授权来源。坏PDF无法取页数时也有可恢复源级缺口，不能用0页代替未知。 |
| 新`learning_task_source_leaves` | task/source/leaf_id；typed locator；required representations；prepared_snapshot_id可空、准备状态／错误；派生revision、排除所处scope revision | 页、有效字幕窗口、asset block的稳定覆盖身份；准备结果可替换指针，旧snapshot与变更事件保留。leaf_id不能依赖临时OCR结果hash或run。 |
| 新`learning_run_inputs` | run_id＋input ordinal；leaf_id、实际read_snapshot_id／hash、原叶片段范围、representation、读取／实际出站proof | 冻结某run真正使用的多个来源及子范围。作为引用白名单、输入hash和覆盖并集依据；不能只存模型自报covered=true。 |
| 新`learning_task_targets` | task_id唯一；reserved_artifact_id；已materialize的artifact FK可空；mode=create/update；原目标／base revision；结构／目录revision；允许block槽位、availability／最新已提交revision | 模型开始前已有目标身份和写范围；第一批内容完成才materialize非空Artifact，之后只提交patch。update绑定现有目标及base。 |
| 扩展`learning_tasks` | task_layout_version、plan_revision；准备／枚举阻塞原因；scope声明／已冻结manifest关联；原状态与事件继续使用 | pending枚举、needs_sources、awaiting_outline、生成、冲突与完整／部分可处理地呈现；不再仅由run状态推导覆盖。 |
| 保留并扩展`learning_task_units`作为work | source_snapshot_id改nullable且仅作v1兼容；work_kind、chapter_id、plan_revision、输入叶／片段计划、目标block槽位；run_id及工作状态保留 | physical unit_id可继续作work_id，避免复制CoreRun。新工作用多来源计划，不能继续以单snapshot字段作权限真相。 |
| 扩展`learning_task_runs`／input_hash | 使用版本化v2输入manifest hash，绑定work/plan/target footprint及多read snapshot；原binding、状态、增量、event、attempt、usage结构保留 | 一套真实执行与unknown对账；实际一个provider请求仍只记一个attempt，不按输入叶数计费。 |
| 复用artifacts／revisions／conflicts／operations／search | 成果块patch发布新增operation kind／Receipt；以run＋batch slot派生业务身份；revision author支持model | 同事务保存块修订、conflict或published、任务工作回执及索引更新；不用新增第二套候选文件存储或回执数据库。 |

WorkUnit的输入计划可先用有界typed JSON保存在该表；run_inputs是不可变的实际读取proof。没有当前查询需求就不另加通用依赖图／插件表。目录可继续存现有outline JSON加revision，不必为了本次拆分另起Book数据库。

`ArtifactBody.blocks`当前至少一块，不能塞假正文来“预留”空成果。reserved_artifact_id先属于TaskTarget，第一批完整块发布才建立真实Artifact及head；之前在任务入口展示进度／留稿，不伪造可搜索的已完成成果。已有明确多项产物请求可由TaskIntent的稳定请求项分别建Task；不将“每页一份”当成多成果需求。

## 4. DTO与两个小Interface

保持既有create／control／reconcile入口，扩展以下业务类型即可：

| DTO | 关键语义 |
| --- | --- |
| `TaskSourceSpec` | 有限document／transcript／artifact范围，业务ID／版本及授权proof；complete请求可以先处于待枚举，不能要求调用方先读成功才能建Task。 |
| `TaskSourceLeafView` | 稳定locator、准备状态／representation／snapshot／错误；不含run或独立成果指针。 |
| `WorkUnitView` | 章节／输出块工作、依赖叶范围、当前run、生成／发布／冲突状态；不把source gap与结构化输出失败混成一类。 |
| `RunInputManifest` | 实际有序read snapshot／片段／角色／hash集合、scope／plan revision和目标写footprint；只由服务器构建。 |
| `TaskTargetView` | 同一预留／已发布artifact身份、内容可读状态、缺口／冲突及当前revision；不宣称partial内容完整。 |
| `TaskOutputPatch`／`CommitReceipt` | 指定block槽位、expected block/base proof、完整内容块及归因；回执区分published／conflict／already_applied。不接受整份任意替换或自报course／作者。 |
| `TaskCoverageView` | 源枚举是否完成；总叶数可未知；prepared、已实际输入／已处理、缺失、排除分开。百分比不能将未知写0或只数有run的叶。 |

两个实际Seam可收敛为：

```text
Scope.prepare(task_id) -> ReadyScope | AwaitLocal | SourceGap
Work.claim_next(task_id) -> PreparedWorkRun | AwaitScope | AwaitOutline | Paused | Done
Work.commit(run_id, output_patch) -> CommitReceipt
```

Scope隐藏PDF／OCR／ASR／有效字幕和枚举；Work隐藏内容打包、CoreRun、目标块校验及发布。Executor仍是现有一条受控队列，只消费这些结果，不循环“每页一次模型”。这是两个小Interface，不是可配置任意动作的工作流引擎。若claim与CoreRun创建分开，ReadyWork必须有CAS token并在claim事务再核scope／plan／target，避免准备后过期派发。

### translation的兼容映射

一份用户最终选区→一SourceLeaf（可含跨页parts）→一WorkUnit→现有隔离CoreRun→同Task一个translation target。保持tools=[]、不补前后文／聊天历史、选择不调用、模型／思考固定、丢回执只对账。无需为翻译改用新的执行宿主或全任务上下文。

## 5. 来源准备、初始损坏与完整覆盖

1. 先在准入事务保存Task、TaskSourceSpec、TaskTarget及操作receipt，再慢枚举／准备。先核真实课程关联及授权；跨课／伪造来源仍拒绝，不能以“持久留稿”为由接收任意路径。
2. 对合法源枚举页／有效字幕范围／asset blocks，形成稳定叶清单。整份坏PDF连页数也未知时，保存source级`enumeration=blocked`和真实错误；不假造一片空页、不报整份0页成功，也不删除其他源及已完成稿。修复后按同源版本恢复；换版需要新的明确范围修订。
3. 缺本地文字层或字幕先接现有Preparations／OCR／ASR，复用其租约／检查点／参数版本、前台优先和用户暂停。不得把本地准备变成付费模型OCR／字幕纠错，也不得因补当前片段复活用户暂停的批量队列。
4. 一个叶的可用representation要足以处理其内容。含图表／公式的一页不能仅因有几行提取文本就算全部内容覆盖；需要已授权的文档图像／局部representation或明确内容缺口。文档读取许可不能补视频／屏幕原帧。所选模型不支持必要representation时说明问题，不换模型或静默省掉图片。
5. 准备失败保留待决定叶；无法补齐暂停相关完整工作，用户可重试、排除或接受部分。重试保存新派生snapshot并保留旧gap，稳定leaf_id不变；排除形成scope修订；接受部分仍标partial。来源单位可重做已知失败的本地准备，未知模型run不能据此重发。

**完整判据**必须同时满足：所有声明源枚举完成、当前范围所有叶／必要内容representation有可核处理覆盖、全部必需work输出提交或已按用户决定处理、无未结执行／未解决发布冲突。不能由“全部run completed”“有若干引用”“OCR完成”或“生成了几章”推导完整。

叶覆盖由成功处理的run输入片段并集计算；准备、实际请求包含、处理成功及输出提交各分开。超长单页／字幕段可按内容预算切片，但记录原leaf、连续字符范围、源段ID、整数毫秒、版本及切块映射；不能仅取前N字符。一个字幕段切多文本块不能凭字符比例虚构更精确的词时间。范围重叠去重按locator和版本，不把同文不同源误合并。

空白页／静音时间片需要可信本地完整处理证据后可无模型完成；仅text为空并不足以区分空白与OCR／ASR失败。统计展示原声明范围、排除／缺口与修订，排除后不能悄悄把原范围说成100%；空有效范围不发布“完整讲义”。

## 6. 内容打包、章节与一份可编辑成果

### 工作划分

短讲义／比较任务在预算内一次run可包含全部已授权来源；长任务按章节／内容窗口及输出预算形成work。优先使用已有标题、PDF目录、字幕结构和用户明确目录，常规pack为本地确定性处理，不默认先调用规划模型。完整来源清单与work计划分别存：改pack大小不改变来源分母，不能通过删work让缺页消失。

book没有明确目录才执行既定的一次目录提议／确认；已有用户明确目录直接记confirmed。确认记录绑定outline/plan revision，后续按章节连续生成。一个章节可有多个work，一个work也可引用多个文档／字幕／资产块。已完成章先读，其余继续，不逐章请求批准。目录修订改变plan revision，旧work不得写新章节槽位；保留已完成章节、历史revision和手改内容，不自动从头重发。

研究work以子问题／比较项／报告部分为单位，允许A/B同时进入同run；不按每页拆成互不知情的报告。课程内来源不自动变外部研究，研究的明确外部许可仍独立。普通相关检索范围不冒充“完整阅读全部资料”。

### 多来源输入与引用proof

run在派发前冻结有序InputManifest，hash覆盖全部read snapshots／片段、content_role、scope/plan revision、用户设置、prompt及目标写footprint。部分叶只送一段时，应发行精确read snapshot或等价的片段proof；不能给模型整页snapshot_id后只送局部，再让它引用未读取段。输出允许引用**本run实际读取集合**，而不是任务全范围所有ID。全文覆盖不以最终引文数计。

CoreRun的prepared只表示输入／目标已冻结；没有真实出站事实时不能当作模型已处理。unknown保留原InputManifest／binding，恢复先对账；不能因另一个源准备好了就重pack原未知run。已发布章节可以按需作为带AI／mixed身份的任务上下文，不必每批重复整本书；它们不能替代原始叶覆盖，也不授予读取全部祖先原件的能力。

### 块批次发布与CAS

最小闭环可以每个work/run完整结束后发布一批完整blocks；这已支持Task未完成时阅读已成章节，无需先做token级发布。流式部分正文仍保存在现有run稿。将来若使用native stage分多批，仍必须复用相同run及稳定batch slot，不能另起发布平台。

每批只提交`TaskOutputPatch`到同TaskTarget；server根据work分配全局稳定block槽位，避免每run重复`part_1`覆盖前文。发布事务保留其他块，核来源、作者、target、scope／plan／run归属、幂等receipt、revision与索引；不能拿一批body直接替换整份成果。**本run来源白名单只核本次新增／改写的块，未改的既有块沿原来源链原样保留**；否则章2只读来源B时，会错误拒绝原有章1引用来源A的正文。重复回调／不同call语义重试按run＋batch槽位去重，不多建artifact。

并发手动编辑使用以下最小规则：

- 对将覆盖／删除的块，冻结base revision中的block hash／版本；新块冻结“该槽尚不存在”及目录结构revision。发布时比较这些proof，不能将expected改成最新head后重试。
- 同一块变化则保存AI候选和用户当前revision，返回conflict，不覆盖、不计为已提交完成。沿用现有conflicts／revision历史，模型修订author为model，混合身份保留。
- 不相交的人工编辑可原样保留，再将通过各自block CAS的新章节追加到授权槽位；这是有proof的无重叠组合，不能把“读了当前head”当成允许覆盖。若暂只实现整artifact CAS，也必须保留候选，不能自动覆盖；但会阻碍“编辑已成章、后续章继续”体验，应以块CAS补齐。
- 当前ArtifactStore的用户update／restore不可直接当模型修改入口。内部发布复用其事务／历史／索引逻辑，显式传可信author、task与目标footprint。原始教材、用户作答、手改字幕和正式复习评分不在该写域。

## 7. scope变更、错误分类与故障窗口

`run_still_in_scope`应从单snapshot升级为读取集合／片段及输出块proof：排除无关叶不必丢弃已读工作；排除本run读取过的叶、改变原版本或改目录目标时，旧结果留稿而不冒充新范围成果。不能仅以全局scope_revision不同就删除所有历史，也不能忽略真正相关的变化。

本地来源失败、已知模型失败、输出结构错误、目标编辑冲突、派发unknown分开。坏JSON不是教材缺页，不能引导用户通过排除教材或refresh_gap恢复；已完成provider正文先留run稿，已知输出失败只允许明确恢复策略，unknown仍只对账。用户暂停／取消不新派发，已受理内容按原归属安全落稿；普通停止不杀Host。

| 故障窗口 | 必须持久的事实与恢复行为 |
| --- | --- |
| Task准入后，尚未枚举／初始PDF打开失败 | TaskSource声明、错误、target与operation存在；expected叶数未知，恢复不新建第二Task。 |
| 枚举半途／OCR或ASR单元提交前后 | 叶身份／检查点、版本与准备回执可核；已完成准备复用，手改字幕不覆写。 |
| pack／claim后、模型派发标记前 | InputManifest、target footprint及prepared CoreRun完整；仅可确认未派发的原work继续。 |
| 模型受理、未收到回执／宿主重建 | 原run/request/epoch、binding与实际attempt保留；follow/page／原receipt对账，无新prompt。 |
| 一批块落盘／DB提交／索引更新／回执丢失 | patch、revision、commit receipt与work状态原子；异步索引用幂等outbox或保持现事务；不产生第二成果／重复块。 |
| 已成章被用户编辑，后续批到达 | 同块保双方冲突；无重叠块通过各自CAS组合，保留人工内容；不追最新head覆盖。 |
| 已完成一批后暂停／源失联／目录修订 | 成果仍可读、状态未完整；待处理work被fence，旧回调不写新槽位；恢复只继续未执行部分。 |

## 8. 迁移与既有证据复用

追加迁移扩展当前表和新表，旧迁移文件、历史失败、原始attempt／usage及已保存资料不改。SQLite的nullable/字段调整若需batch rebuild，在项目迁移恢复副本和事务保护下独占执行并验证，不能对用户运行库直接试错。

新增task_layout/input_contract版本：旧单选task映射为一source leaf、一work、一个target，继续使用同一Executor/CoreRun。旧input_hash按v1解析与恢复，不用v2公式重算后宣称旧事实冲突。活动unknown保留原binding及prompt，不为转换重发；无法可靠重建的旧来源proof保留legacy／unknown，不倒推伪造。

历史多unit已经产生的多份Artifact保留，不删除／合并，也不追溯改成同一资料或宣称原先已完整归纳。可投影legacy列表；新Task使用一target新语义。翻译单选实证可保持相同输入／调用次数和输出行为，必要字段变化通过一个正式兼容Adapter处理，不复制第二个worker／队列／Harness层。

## 9. 测试哪些调整、哪些必须保留

本次未执行测试。以下按行为修改fixture，不能只让断言追随新字段名。

| 现有测试／行为 | 应调整或必须保持 |
| --- | --- |
| `test_workbench_manifests.py`完整来源枚举；`test_workbench_reading.py`34页／页失败 | **保持**全范围与错误叶、版本／跨课规则；改断言到source_leaves。新增同34页在可验证预算内可打包为少量work，主artifact始终一份；不把34个LLM请求写成要求。 |
| `test_workbench_runs.py`输入不可变、attempt归属、unknown／事件／usage | **全部保持**；改claim输入为多read snapshot／片段manifest，并加调换来源、遗漏片段、改角色、改target proof均拒绝。一个run多叶仍按真实模型请求计attempt。 |
| 同文件single atomic product测试 | 更新为“单run/batch唯一提交到TaskTarget”，多run累积一成果；重复operation／不同call不重复块。既有role与普通聊天不能发布的负例保持。 |
| `test_workbench_executor_host.py`translation、活动cancel、保存／恢复 | **保持**单选隔离、模型参数、部分正文、Host存活、known invalid output与unknown不重发；handout新增同run至少两个来源、同Task两批一target，原request count不当新全文策略。 |
| `test_workbench_tasks.py`暂停、缺口、目录确认／已有目录直跑 | **保持**这些行为；SourceGapDecision改指source／leaf，不再等同重跑某模型unit。目录确认后需实际产生章节work，已成章可读且剩余连续执行。 |
| `test_workbench_artifacts.py`版本／撤销／冲突／索引原子性 | **保持**；新增同块手改冲突双方保留、无重叠章节安全追加、块ID碰撞拒绝、author=model／mixed及只依据资料多来源白名单。 |
| `test_workbench_runtime.py`退出确认、凭据切换、unknown取消留稿 | **保持**；Task有准备中source／预留target但尚无run时也纳入活动任务判断；关页面不退出后台，所有清理只管理所属进程。 |
| 迁移恢复fixture | 从旧schema升级，聊天／截图／手改／Task／Run／usage／legacy成果和恢复副本保留；中断前后无自动模型调用；旧迁移不可变。 |

新增必要组合：初始坏PDF持久缺口→修复同版本→本地OCR→多来源pack；含有效字幕及手改的整课→一讲义→交互书；超长单页／单行字幕跨pack无丢失重复；章节1手改期间章节2提交；模型已接受未回执时来源／目录变化；部分成果恢复后断网导出仍保留已发布内容和来源。覆盖／模型准确性／实际学习／离线能力分别下结论，不能由本次结构设计直接判passed。

上述拆分是现有Task Module的增量演进。先改来源账、work输入集合和TaskTarget，再让当前CoreRun执行与发布消费它们；不先堆规划模型、代理或另一个任务平台。实现和唯一计划状态由主agent负责，不再询问用户已经确认的场景、保存或目录规则。
