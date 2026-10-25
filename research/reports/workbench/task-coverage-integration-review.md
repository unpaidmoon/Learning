# TaskCoverage v2 集成契约复核

日期：2026-10-23。责任：`baseline_contracts` 独立审查，仅新增本报告与独占、忽略入库的合成 fixture；产品代码、迁移、锁、计划与运行实例均由 root 负责。

本次重新读取目标附件及当前 13 场景需求／实施方案，审查 0015、TaskCoverage／TaskPlanner／TaskRecovery、RunStore、ArtifactStore、Executor 与路由的实际集成。原始失败发生时 root 同步修复中；以下行号是**触发快照**，以函数名定位，不能将中间态当作最终发布状态。root 提供的 r4 21 项及 SEA 本地 Messages 结果没有由本代理重跑；本报告不宣布场景验收通过。

## 可证实发现

### I01 · P1：嵌套 SQLite 连接使正常来源准备自锁

触发快照：`task_coverage.py:664–678` 的 `_prepare_transcript_leaf` 在第一个 `engine.connect()` 尚未退出时，缺少转写 unit 就调用 `_preparation_pending`；该函数 `:525` 再开连接。`storage/repository.py:133–137` 对所有事务执行 `BEGIN IMMEDIATE`，因此第二个连接等待同线程尚未释放的写预留锁。不是用户实例争用，也不是等待 ASR 的正常状态。

独占 `partial_track_probe.py` 初次实际退出 1，栈为 `_prepare_transcript_leaf → _preparation_pending → BEGIN IMMEDIATE → OperationalError: database is locked`。此异常不在 `refresh` 的 AppError／OSError 或 `Executor._work` 的捕获集合内，能结束唯一任务 worker，阻断后续任务。

同一问题还发生在整课程 admission：触发快照 `admit:89/171 → _course_members:294 → CourseFileStore._root:330 → folder`，在已开启 admission 事务内再次读取 DB。`course_admission_probe.py` 对 `include_generated=false` 已复现；该 fixture 仅把自己的 SQLite busy_timeout 缩为 100ms，未改变产品配置。包含生成成果时，事务内 `ArtifactStore.list` 也不能自开连接。

最小修复：等待／外部 store 读取放在事务外，或者复用显式传入的连接；admission 的幂等查询、冻结成员与最终落盘仍须正确对账。**本报告收尾时已读到 root 将 course 元数据采集移至事务外、缺 unit 分支移到原连接退出后。`partial-track-result-r2.json` 已验证该触发不再自锁，随后暴露 I03；course 修复本代理未复跑。**保留原失败，不把修复源码等同组合验收。

### I02 · P1：来源“重试”未恢复已知失败的本地 ASR job

触发快照：`TaskRecovery.source_gap` 原 `task_controls.py:84–140` 仅把来源／叶改回 declared／pending；`TaskCoverage._preparation_pending:533–537` 仍读到原 `PreparationStore` 的 failed，马上再次阻塞。

`asr_retry_probe.py` 用同一合成 WAV、同一冻结 track、一个通过正式 `PreparationStore.claim/finish` 产生的已知失败记录复现：重试回执是 task queued、scope_revision=1，但刷新后又是 needs_sources／TASK_PREPARATION_FAILED；preparation 仍 failed、revision=2，新的本地 claim 为 null。没有运行解码器、ASR 引擎或 provider。

最小修复：明确用户 retry 应持久化对**同一 job／track／参数**的恢复意图，再以稳定 operation_id 和原 revision 调已有 PreparationStore.control；中途崩溃重放同一控制回执。不能因后台刷新清除独立的手动 pause／cancel。**收尾已读到 root 的 durable preparation_retry 及 `_apply_preparation_retry`，本代理尚未执行其恢复、故障窗口和并发 CAS 复验。**

### I03 · P1：完整任务指定现有部分字幕轨时，不申请同轨补齐

最新修复后的独占 `partial_track_probe.py r2` 仍复现：已存在一个 current/unit 字幕准备，probe 已完成、有效时间已知，目标 unit 尚无 raw 转写；用户以该 `track_id` 创建完整任务。`_enumerate_transcript` 仅在 `track_id is None` 时创建 full preparation；有 track 时只查找已存在的 `scope=full` job，找不到便保存 `preparation_job_id=None`。后续叶永久停在 TASK_SUBTITLE_NOT_READY。

`partial-track-result-r2.json`：task needs_sources、source blocked、job_id=null、叶 blocked；DB 中仅有 unit/completed 的原准备，没有 full job。该 fixture 使用合成的 engine 身份及持久化 probe 事件，验证的是准备编排，不是实际 ASR 质量。正常“已看过一小段，再整理整课”也具备同样的存储形态。

最小修复：完整任务缺原始转写时，为**指定轨的原 engine／语言／参数及 source_version**申请受控补齐；复用 full job 或登记同轨 full 准备，不能另选默认轨、丢手改，也不能后台唤醒已手动暂停的 full job。验收：原已有 raw 单元不重做，新增单元与手改均保留、完整覆盖可核；full job 已 pause／cancel 时仍需明确恢复。尚待 root 实施／验证。

### I04 · P1：全部排除后误进入 knowledge-only 空输入分支

触发快照：`TaskPlanner.plan:149–150` 只用过滤后的 active sources 判定空来源，而没有检查原始 admission。唯一坏 PDF 先持久 blocked／unknown count，用户明确 exclude 后，planner 创建 `inputs=[]` 的 content work；complete／sources_only 仍为 true，RunStore.claim 能返回 prepared。`exclude-all-result.json` 已实际复现。

后果：没有材料可处理的“只依据指定资料”任务仍能请求模型；随后因 source-only 写入约束失败。对允许补充的原任务则可能隐式转成纯知识生成。排除来源不应增加这种许可。

最小修复：空输入仅用于**原始 scope_spec.sources 就为空**、selection、非 sources_only 的明确知识任务；planner 与 claim 同时守门。全部排除应显示可处理的空范围状态，保留旧稿，不继续模型。**已静态读到 root 收紧 fallback、claim 二次 guard 和全部排除的状态处理；本代理未复跑修复。**

### I05 · P2：全空白范围没有成果却标为完成

触发快照：`TaskPlanner.state:265–270` 将本地 empty proof 当成最终成果完成。`probe.py` 的 empty_document 为合法空白 UTF-8 文档：task completed、units=0、target.materialized=false、artifact_count=0。

空页覆盖事实正确，但不能据此宣称“生成学习成果”完成。最小修复是在零生成工作且无成果时给明确“无可生成内容／需处理来源”的状态与稳定原因，既不调用模型填充，也不产生假空成果。**已静态读到 root 的全空、零工作 needs_sources guard；修复复验 not_run。**空页夹在有内容的全文中仍应按本地 empty proof 正常计覆盖，不能一概变成缺页。

### I06 · P2：已完成流缺 attempt 证据时留下不可执行的 queued 状态

`probe.py` 的 missing_dispatch_proof 复用现有 finish_work helper，仅不喂 attempt 事实：run completed、成果已落盘、unit completed；输入 proof 仍 prepared，所以 processed=0。reconcile 却给 task queued，而 planner 返回 false、也没有 pending/read work。`Executor._work:159–162` 对此重复排队。

目前**没有发生隐式重发**；拒绝将 prepared proof 计完整覆盖是正确的。但若原请求账本真实缺失或迟到，任务应明确待对账／unknown，仅刷新原 run 的证据，而不能长期显示 queued 并空转。最小验收：迟到 attempt 更新能完成原任务；账本永久缺失则保持可说明的未知，已有成果不删、不新建 run、不自动重发。此项仅本地事件 fixture 可证，未声称真实 provider 曾丢失账本。

## Book、停止与写入边界

- 最初 `book outline_supplied=false` admission 是 queued；可计划 content，confirm_outline 却报 TASK_OUTLINE_STATE_INVALID，底层 claim 可 prepared。`probe.py` 的 book_gate 保存原始结果。当前 executor 的 SUPPORTED_TEXT_TASKS 本来就不含 book，因此没有证据表明正文已真实派发；收尾又读到 planner 的 unconfirmed-book 拒绝及 claim 的 phase guard。正式目录 proposal → 一次确认 → 连续章节仍须实现，尤其 supplied outline 直跑、目录 revision、章节与来源叶分离、target 复用；这不是本报告要求再搭一套平台或再次询问产品。
- 本次读取的 mark_dispatching 在最后派发前检查 task running、冻结 work/hash、所属 unit 未排除、binding 与 cancel_requested。源缺口决定先拒绝 dispatching／unknown；排除未完成 work 会留下旧计划并关闭原 unit 出站。root 已补来源准备的 active_source／叶回写 fence。未发现可据此认定的跨课或迟到越权写入；本轮没有活动 Host 停止验收，不能记 passed。
- `_publish_target` 使用 run 冻结 target／允许块与本次 run_inputs，模型不能写 user_authored、不能引用本批范围外 snapshot；同块差异保留冲突 revision，非目标块与无关手改保留。该路径没有从普通 QA 借 generation 的入口。来源身份由存储的 snapshot role 传递，不能把“提示模型只依据资料”当作来源 ACL 或语义忠实性证明。
- 24k 字符切片、页级文档图像和原始／手改字幕身份分别保留。正常超限、来源变化、字幕边界不精确的明确拒绝不是可绕过漏洞。本次未对原生 PNG→JPEG 派生证明或真实图像出站作结论，root 已另行调查。

## 证据与复验范围

独占目录：`.runtime/validation/task-coverage-integration-review-20261023-baseline-contracts/`。实际命令均为 `backend/.venv/Scripts/python.exe <该目录脚本>`：

| 脚本／证据 | 观察结果 |
| --- | --- |
| `probe.py` / `result.json` | 四个初始状态边界已观察；book/blank 是修复前快照；early_partial 只记录现象，未据此单独判违规。 |
| `asr_retry_probe.py` / `asr-retry-result.json` | 原来源 retry 未恢复原失败 preparation，failed fixture。 |
| `exclude_all_probe.py` / `exclude-all-result.json` | 全排除仍建立空 content run，failed fixture。 |
| `partial_track_probe.py` 初次 | 实际退出 1，自锁栈见调用记录；原独占 DB 保留。 |
| `partial_track_probe.py r2` / `partial-track-result-r2.json` | 同类触发已不自锁；仍未创建 full 补齐准备，I03 未解决。 |
| `course_admission_probe.py` / `course-admission-result.json` | 捕获实际 OperationalError 及完整栈；仅 fixture busy_timeout=100ms。脚本捕获异常后退出 0 不代表用例通过。 |

没有真实 provider、Host、用户素材、ASR/OCR 模型或现存运行实例操作。全部课程、WAV、坏 PDF、空白文本和 DB 是本代理新建的独占 fixture。没有运行全套测试、改依赖或更新任何工作包状态。修复中的原失败均保留；root 的后续复验结果应追加相应正式证据，不回写成原始通过。

## 第二轮窄审查：partial 对账、实际 envelope、分批编辑

root 已提供旧问题修复后的 r8 31 passed、r10 65 passed，以及自造文字＋扫描 PDF 的真实 r1 截断／r2 同模型和思考完成证据。本代理未重跑这些组合，也没有覆盖它们的原始结果。本轮只核下列三个现存接缝，不检查未接功能；继续不改产品代码。

### I07 · P1：accept_partial 可把缺出站 proof 的 unknown 任务结单

`TaskRecovery.source_gap` 当前 `task_controls.py:25–50` 仅拒绝 FINAL task 或 state 为 dispatching／unknown 的 unit，没有拒绝 task 自身 unknown、unit 已 completed 但输入 proof 尚未结清的组合。

独占 `narrow-r11-probe.py` 的 unknown_accept_partial 实测：修复后的 reconcile 先正确给 task=unknown、unit=completed、processed=0；随后 source_gap(accept_partial) 成功返回 partial，scope_revision=1，之后 reconcile 保持 partial，run_inputs.proof 仍为 prepared、processed 仍为 0。这不是已知来源缺口的“接受部分”，而是把待对账的执行事实变成了任务终态。

最小修复：来源决定入口复用执行对账判定，拒绝尚待结清的原 run／已完成输入 proof；不能只看 unit 状态。只检查 task.state==unknown 也不足以覆盖先 pause 再做缺口决定的路径，暂停不能洗掉未结清事实。先补原请求证据，不能新建 run 或重发。验收：直接 unknown 和 pause 后两个入口均拒绝；补回同一 run 的 attempt 后能按实际来源缺口处理，重复 operation 仍查原回执。

### I08 · P1：字符装箱不约束实际请求体，合法元数据可超过正式出站硬上限

`task_planning.py:13–15/128–145` 只计算来源 text 的字符、图像数量与图像原字节；`TaskExecutor._work_prompt` 的 `executor.py:213–268` 又把完整 SourceRef（含每页所有 regions）和编辑目标正文 JSON 全部放进提示。当前没有实际序列化体积预算。

`narrow-r11-envelope-probe.py` 生成自有单页 PDF，通过实际 DocumentReader.page／capture 验证 1,000 个单字符选区；每份 256 个范围框均只命中同一个真实 L 字符。它是合法但极端的协议输入，未伪造快照或跳过选区校验。TaskAdmission、来源准备和 planner 均接受，并形成 **1 work／1,000 inputs／1,000 正文字符**。实际 `_work_prompt` 达 **28,876,309 UTF-8 字节**。

正式 `execution/boundary.py:43/805–814` 的 MAX_REQUEST_BYTES 为 **25,165,824（24 MiB）**。上述仅 prompt 就已超过限制，尚未加 native 请求外壳与 JSON 转义。没有发送 Host/provider 请求；“会超过正式长度 guard”是直接比较已测字节与当前源码上限，不是模型上下文容量或真实拒绝的冒充实测。

最小修复：对模型真正需要的材料、指令、编辑基线和请求外壳作有余量的字节／上下文装箱，并限制每批输入条目，给结构化输出及来源引用留空间。仅用于程序回跳的 dense regions 可继续完整保存在服务端 proof 中，提示只传模型需要的身份和定位字段。准备后的最终请求仍须检查实际 envelope；超限已知失败不能被记作派发 unknown，也不能隐式重发。不能截断授权正文或自动调模型／思考／输出量。单个不可再分输入应返回可处理状态。小字幕正文同样不能免除条目元数据和输出引用预算。

### I09 · P1：多 work 编辑同一块可累计覆盖、最终却只剩最后一批

`ArtifactStore._publish_target` 当前 `artifacts.py:451–466` 对每批提供的 block_id 都作整块替换；成功后 `:491–495` 更新 target.expected_revision_id。后一个 work 因而取得前一批结果作为新基线，CAS 成功，但不会自动保留前一批贡献。完成判定仍依据所有 run_inputs 的消费记录，未核最终 target 是否仍承载前批成果。

独占 `narrow-r11-probe.py` 的 multi_work_edit：30,000 字符来源装成两个 work，目标只允许 paragraph_1，用户要求累积整理。依次发表 BATCH_A_IMPORTANT_FACT 和 BATCH_B_IMPORTANT_FACT，两个 receipt 都 applied，最终 task=completed、processed_leaf_count=1；**当前块和来源链只剩 B，A 只在历史修订里**。保留历史满足撤销，不代表当前完整成果仍覆盖已处理批次。

不能只让模型“记得保留旧内容”解决：第二批输入只有本批来源，当前 `:385–398` 的 allowed_sources 也只有本 run。若模型累积 A+B 并带回 A 的旧 snapshot_id，将触发 TASK_SOURCE_DENIED；若去掉旧 ID，则前批内容的来源链丢失。编辑基线虽已进入 prompt，这两种发布权限目前仍没有对应的累积契约。

最小修复：给同一目标块的分批贡献保留稳定工作身份及来源，再安全归并；或者显式按授权块分配工作，使同一块只接受一次最终替换。沿用当前任务／target／run 与块 CAS，不另建平台。累积路径必须同时保护原目标的冻结版本、无关手改及前批来源，不能仅追最新 head 覆盖；在支持前应提前明确拒绝不可安全执行的同块多批编辑，不能出站后声称完整成功。验收至少包括 A+B 当前可读且来源均可回溯、重放不重复、两批之间手改同块保留双方冲突、无关块手改正常保留。最终 target 的完整性不能只用历史 run 已读计数代替。

### 本轮独占证据

目录沿用 `.runtime/validation/task-coverage-integration-review-20261023-baseline-contracts/`：

- `backend/.venv/Scripts/python.exe <目录>/narrow-r11-probe.py`，实际退出 0，`narrow-r11-result.json` 保存 I07／I09 的失败状态及修订／来源身份；程序成功运行不表示契约通过。
- `backend/.venv/Scripts/python.exe <目录>/narrow-r11-envelope-probe.py`，实际退出 0，`narrow-r11-envelope-result.json` 保存 I08 体积、输入数及正式边界上限；实际本地 PDF 解析与选区验证通过，模型调用 0。

本轮未运行 Host、provider、OCR／ASR 模型、全套测试或用户实例，未读取用户素材，未改锁或产品源码。三项均为修复前独立观察，必要修复和正式复验由 root 集成；后续结果应追加，不能抹去本轮失败。

## 第三轮：正式 Native Business 接入的冻结快照复核

只审查已接入的业务 scope、原生调用归属、来源句柄、暂存／发布及恢复。root 正在补冷恢复，以下是 **2026-10-23 11:28:52 UTC** 的按文件冻结快照，不代表最终合并状态。源码副本、20 项完整哈希与原文件时间／长度保存在 `.runtime/validation/native-business-integration-review-20261023-baseline-contracts/snapshot/` 和 `source-hashes.json`。后续 `live-hashes-after-probe.json` 已记录 native_tools／adapter／service／schema／Host 测试文件在审查期间改变；未覆盖或改写 root 的代码。

| 冻结模块 | SHA-256 |
| --- | --- |
| workbench/native_business.py | `7059f1b3c00eb93080746ccb6cb664b514f34caeae3577e66304b9f03dd2dedb` |
| execution/native_tools.py | `50c4ebadb255091c9fecb7da39cd000acd98db856530065406d739762fb0a923` |
| execution/adapter.py | `e1d4b1706e4304e1008a58ef9d40a74b9253c2affb9ed96123b908ac6f7fe6f7` |
| service.py | `cbef2efde99f15ac0804c229aeee28ea534564071a964fc3a70eef25caf830a3` |
| workbench/executor.py | `98c7abfe7e9184aa1e97d56eae34fcd219219c122cce689441e28a59bd61ae1d` |
| harness/plugins/learning-guard.mjs | `50988fdb3e0d2e3ca79708a029b7dcbe253f9441c6a0d29807e912d3b4bd0a5b` |
| 0016_native_business.py | `61362b655389495feca671c922e2500757cd918c8121794f85a07c3c19e02998` |

fixture 通过独立模块名加载冻结 NativeBusiness／NativeCallRegistry／Adapter 源码；Repository、迁移和成果存储使用当前项目模块、独占新数据库。没有启动 Host 或 provider，没有用户资料。冻结模块的异常类型与正式 port 共用同一 ToolRejected，以保持真实异常分类，未改其执行逻辑。

### I10 · P1：只读工具丢回执无法恢复，QA 却已显示 completed

冻结 `native_business.py:604–616` 的 read_source_handle 在返回实际快照后，没有保存可对账的读取回执；`:717–732` 的 reconcile_effect 只查 native_effects，该表记录的是 stage 效果。`native_tools.py:468` 的 reconcile_effects 因此无法恢复一个已经完成读取、但尚未写入传输 journal 完成回执的调用。

独占 lost_read_receipt 复现步骤：真实 NativeBusiness 读出当前授权 snapshot，返回 ID 与正文均匹配；在 port 返回后、Registry 写 completed 前，由 fixture 抛异常模拟进程中断。Registry 保持 NATIVE_OPERATION_UNKNOWN；重新加载该 journal 再 reconcile，恢复项为空、业务读取回执为 null、output_settled=false。没有重新调用读取工具或模型。

随后仅用两个 completed 的合成 provider attempt 与同一 run 的 completed native 终态，调用冻结 `Adapter._terminal_status:640–661`，结果仍为 completed；按正式 Repository 事件接口入库后，QA generation 也实际是 completed，而 Registry.assert_releasable 仍拒绝 NATIVE_OPERATION_UNKNOWN。TaskExecutor 的 `_finish:488–494` 已有 output_settled 守门，本发现**不声称**该 task 路径会把未知草稿发布为成果。

影响：QA 完成状态与工具结算相矛盾，且这个纯读取故障没有现有恢复回执。根据冻结 `service.py:315–335`，同一 chat 的后续提交会先尝试释放原租约；无法释放则以 NATIVE_PREVIOUS_NOT_RELEASED 结束新提交。后半段是已读调用链结论，本代理未启动 Host 重现整个 UI 阻塞。

最小修复：为只读结果保存同 NativeCall／args hash／scope 绑定的可恢复审计回执，或提供有证明的“无写效果、已知失败”恢复分类；不能把无回执的写操作照此转成功，也不重发模型。QA 增加工具结算的业务状态门槛；保留原生 turn/end 的原始事实，不能重写同一 event_id 的旧载荷来伪造结算。验收：读取完成后丢 journal 回执、读取前中断、源回执迟到、重启及下一轮同 chat 分别核对；写阶段未提交仍 unknown，已提交仅通过原效果回执恢复。已向 root 发送触发事实；最新修复复验 not_run。

### I11 · P2：未派发就本地失败的 task scope 没有可用的收尾证明

冻结 `executor.py:381–390` 先持久化 scope，再等待 validate_choice，之后 `:419–428` 才记 dispatching 并发 prompt。若该本地预检或图片准备失败，`:449–451` 会正确 fail_prepared，但 native scope 仍 active。

独占 prepared_scope_local_failure 复现：实际 prepared run／binding → ensure_task_scope → 模拟本地模型选择预检失败并调用正式 fail_prepared；task=failed、run=failed、该 run 的持久事件为 0，scope 却仍 active。NativeBusiness.assert_releasable 给 NATIVE_TERMINAL_NOT_SETTLED，因为从未产生原生 turn，不能取得原生 turn/end。这里不是 provider 结果未知，也没有工具副作用。

最小修复：为已证明没有进入 prompt 派发的执行定义独立、可审计的关闭分支；须核原 binding、DB 的未派发状态及实际 native／attempt 事实，避免把“账本为空”单独当作没发过的证明。不能捏造 turn/end，也不让它在后续启动时永久作为待恢复租约出现。取消落在 scope 建立与 dispatch 标记之间也应覆盖。Task run 的 session 本来隔离，因此本报告不声称这个孤立旧租约会直接阻止所有新 task；本项是 root 正在补的冷恢复／已知未发送收尾接缝，尚待集成复验。

### 本轮已核而未发现绕过的边界

- `_authorize` 同时核当前业务活性、cancel_requested、run／unit 输入 hash、固定 binding、native turn 和 floor；实际传输还核 provider attempt／dispatch／工具序号／参数 hash 及原生 exec token。句柄按 run 和课程查询，不接受路径或隐式课程读取。QA 没有 stage_result，translation 没有工具权限。本轮未找到可证实的跨 run／跨课程读取或写入放行，未把只读源码结论记成 Host 端到端通过。
- stage 是持久草稿；正常任务发布前先处理未知效果并要求 output_settled。before-commit 无效果记录不能重试成成功，after-commit 只读取精确 native_effect 回执，不重新执行 stage。本轮未发现可绕过这条 task 发布门槛的正式入口。
- 新增 stage_manual_conflict 合成组合：对真实 edit target 冻结原版本，先手动修改同块，再经冻结 NativeBusiness 暂存模型正文、完成原 run、走正式 ArtifactStore 发布。实际结果为 conflict，当前正文仍为 USER_MANUAL_EDIT，模型 MODEL_PROPOSAL 以 author=model 的冲突修订保留。该项本地 fixture passed，不代表真实活动停止或冷 Host 恢复通过。

实际命令与证据：`backend/.venv/Scripts/python.exe .runtime/validation/native-business-integration-review-20261023-baseline-contracts/probe.py r2`，退出 0，`result-r2.json` 保存 I10／I11 失败观察及手改冲突的通过观察。首次代码与结果另存 `probe-initial.py`／`result.json`，没有覆盖原失败。模型、Host、真实素材调用均为 0；无产品、锁、用户实例或工作包状态修改。

root 已反馈新增不可变 0017 read_receipts、只读响应同事务回执、未知工具效果的 QA 终态门槛及独立 unknown 事件 ID，并继续实现 not_dispatched 收尾。以上是 root 正在集成的修正，不改变 I10／I11 冻结快照的原始失败；本代理尚未独立复验新迁移及新 Host 组合。

### I12 · P1：同 run 可换新工具调用 ID 越过原效果 unknown

继续检查同一冻结快照：`NativeCallRegistry.invoke`（`native_tools.py:391–447`）只阻止**同一个** `session_id:native_call_seq` 的 unknown 操作重放。对同 run 的另一个 provider attempt／native step／call_id，它不先核对原 unknown，NativeBusiness 的活性与目标 guard 也不会自行查询传输 journal 的原未知效果。

独占 `pending-effect-probe.py` 复现：第一次有效 stage 在 before_commit 中断，Registry 留下 unknown，原效果查不到；不重放原 call，给同一 run 的第二个原生调用分配全新的 attempt／dispatch／step／seq／call_id，内容相同，向量经正式 ProviderToolCalls 的合成 SSE 校验。第二次 invoke 成功创建 stage_revision=1；结果同时留着第一项 unknown 和第二项 completed。任务最终发布门槛会阻止它完成，但在此之前新效果已经被执行，未满足“未知先对账，不换调用身份自动重发”。

项目原生侧也没有挡住该接缝：冻结 `native-lease.mjs:observe/claim` 只把 `error.code === TOOL_OUTCOME_UNKNOWN` 标成 unknown；业务桥收到 NATIVE_OPERATION_UNKNOWN 时抛普通 Error。独占 Node 合成事件给它更多信息（明确的 error.code=NATIVE_OPERATION_UNKNOWN），仍得到 firstRecordedUnknownFlag=false、下一步 claim accepted=true。`llm/stream` 只核 active lease，也未因待对账的业务效果收口。本项不是实际 SEA／模型重放实测，证据是冻结项目模块的两个独立本地组合。

最小修复：在同 run 新效果／新续答前先对账既有 unknown；已取得精确提交回执则恢复，仍缺回执则加 run 级执行 fence，保留原调用，不由模型换 ID 重试。只读回执、stage 回执以及错误投影应共用该判定，不能仅修补“相同 call_id 再发”。明确的 known rejection 可正常继续，不能把所有已知失败都变 unknown。相同 body 的业务去重仍需保留，但不能替代未知效果的执行门槛。验收包括 unknown-before-commit＋同内容新 ID、不同内容新 ID、after-commit 回执恢复后正常继续、跨 run 无关任务仍可运行。

新增实际命令与证据：

- `backend/.venv/Scripts/python.exe .runtime/validation/native-business-integration-review-20261023-baseline-contracts/pending-effect-probe.py`：退出 0，`pending-effect-result.json` 保存两个操作状态及实际新 stage，契约失败。
- `node .runtime/validation/native-business-integration-review-20261023-baseline-contracts/lease-unknown-probe.mjs`：Node v24.14.0，仅冻结项目 NativeLease 的合成事件；退出 0，`lease-unknown-result.json` 保存下一步被接受的投影，未启动 Harness 或网络。

本项已先发给 root，后续实现与复验仍由 root 负责；全部模型调用 0，未改正式产品代码或已有锁。
