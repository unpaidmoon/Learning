# DeepStudent C01～C06 复用审计

日期：2026-10-23（Asia/Shanghai）。调查范围为[统一实施方案](../../../docs/plans/13场景统一交付实施方案-2026-10-23.md) C01～C06。按已授权的 REA 源码路由，复用[已有源码报告](../../topics/DeepStudent源码借鉴分析-2026-10-22.md)后核实际固定源码；未安装或启动 DeepStudent，未读取私有学习资料，未改动现有产品文件、产品依赖锁、运行实例或全局环境。

## 1. 已得到的结果与证据边界

固定来源为 [`helixnow/deep-student@635216503c0b34bdf812e17942e0f09f51872e6f`](https://github.com/helixnow/deep-student/tree/635216503c0b34bdf812e17942e0f09f51872e6f)。在 `.local/`、`.runtime/`、`research/` 的文件名搜索未发现既有目标源码，于已忽略的 `.local/cache/reuse/deepstudent` 浅取此提交，`rev-parse HEAD` 精确匹配，checkout 状态干净。缓存仅用于调查，正式候选不从缓存加载。

已经完成：8 个原文件无修改提取、3 份上游根许可／说明归档；实际候选的纯依赖核查；16 个 Node 特征 fixture 与 TypeScript 类型检查；原样 Rust 调度文件 19 个测试；独立 Windows 检查程序普通／静态 CRT 两种构建及本机运行。**这些是候选级证据，不是产品兼容、场景、真实流或学习效果通过。**

可复核入口：

- [原文件清单与完整 hash](../../../third_party/deepstudent/manifest.json)：8 个源码与 3 个原通知，含 SHA-256、Git blob、原路径、目标路径、修改为零。
- [23 个额外静态核读文件](../../../third_party/deepstudent/audit-sources.json)：C01～C06 的调用方、服务、数据类型、管线和原依赖锁；未冒称迁入。
- [调度依赖闭包](../../../third_party/deepstudent/dependency-manifest.json)：固定版本／checksum、实际 Windows normal/build 闭包、许可及原通知 hash。
- [命令与分层结果](../../../third_party/deepstudent/checks/validation.json)、[最终 locked 输出](../../../third_party/deepstudent/checks/fsrs-locked-output.txt)、[本地 fixture](../../../third_party/deepstudent/checks/candidates.test.mjs)。

## 2. C01～C06 逐项复用账

R 表示原样候选，A 表示需适配移植，I 表示项目独有规则。下表不把候选通过转换为产品状态；产品集成由主线程负责。

| 编号 | 核读文件／符号 | R／A／I 取舍及原因 | 已完成与目标落点 |
| --- | --- | --- | --- |
| C01 选区 | `selectionRef.ts` 的 `SelectionRefData`、`describeSelectionLocator`、`serializeSelectionRefData`、`selectionToChat`；PDF 入口及 selection context 定义 | A：保留正文与 locator 共同进入身份、快照序列化、来源展示及加入草稿流程。完整文件依赖上游会话、资源、通知、事件和窗口状态，不能 R 导入。I：课程、源版本、跨页区域、内容角色与授权 guard。 | 静态已核，未复制带应用依赖的文件。正式落点是现有资料选区／草稿模块，产品检查 `not_run`。 |
| C02 翻译 | `createNdjsonParser`、`parseAlignedFallback`；`buildCacheKey`、`readCache`、`writeCache`、`clearCache`；`TranslationPopover` 生命周期 | 解析器、LRU 为 R 候选；UI／执行为 A。I：严格完成判定、允许输入快照、模型／思考／提示及选区修订命名空间、attempt 对账。 | 解析器、缓存和类型原件已在 `third_party/deepstudent/upstream/src/features/chat/components/`。fixture 与独立类型检查 `passed`；实际翻译与停止 `not_run`。 |
| C03 题目／作答 | `qbank_generation/types.rs::validate_draft`、`parse_generation_output`；`QuestionBankService::submit_answer`、`apply_submission_verdict_in_tx`；`question_repo` 的版本更新及 submission 去重 | A：移植题型校验、作答身份和短事务结构。整个 Rust 服务依赖 VFS／rusqlite、同步、掌握度及复习计划，不能 R 接入项目数据层；生成 types 文件也含 `study_loop::media_source` 的提示规则引用，不能按“只有 serde import”判断为完整纯模块。I：题目版本、评价修订、任务权限和可靠复习联动。 | 已核具体纯函数及事务入口，原件 hash 已记录；不复制 monolith。建议适配到项目练习／评价服务，产品检查 `not_run`。 |
| C04 卡片／调度 | `chatanki_executor::execute_add_cards`、`card_content_is_valid`、`normalize_agent_extra_fields`、`validate_agent_card_template_ids`；`fsrs_scheduler`；`rate_with_mastery_bias_cas` | 调度原样 R 候选已经独立编译；卡片校验／入队回执和评分事务 A。I：统一今日总量、课程筛选、内容／调度修订、保留用户暂停、正式用户评分及改判。 | 原始 scheduler 位于 `third_party/deepstudent/upstream/src-tauri/src/fsrs_scheduler.rs`，窄检查 crate 和独立锁已保留。19 个原测试 `passed`，产品复习闭环 `not_run`。 |
| C05 作文 | `essay_grading_executor::execute_grade` 的非空／长度校验、上一轮原文及结果；`pipeline::parse_score_from_result`、属性／实体解析、结果保存；`types`、`text_stats` | A：保留轮次流、分项解析和原稿／原始结果双存。整个 pipeline 直接依赖 LLMManager／ProviderAdapter、VFS、事件及 tokio，不能 R 导入。I：稿件修订、批注原文锚定、用户改判、冲突与持久任务。 | 静态已核、来源 hash 已记录；未引入另一个模型执行层。建议移植为项目作文模块，实际两轮和批注检查 `not_run`。 |
| C06 讲义 | `ir.ts` 的 Block／解析和表格校验；`markdown.ts::handoutToMarkdown`；`windowing.ts` 字幕格式化／切块／范围函数；管线入口 | IR／Markdown 可保留 R 内核；字幕切块和管线 A。I：完整范围覆盖、毫秒／段ID／源修订映射、原文与 AI 补充身份、缺口暂停。上游帧流程不采用。 | IR／Markdown／windowing／type-only prompts 原件已归档，独立 fixture／类型检查 `passed`。完整讲义和来源覆盖 `not_run`。 |

### C01：确切保留点与差异

原类型只有单个 `sourceId`／`locator`，没有本项目所需的 `course_id`、`source_version` 或跨页区域数组。`serializeSelectionRefData` 在序列化前把正文超过 20,000 字符的部分截断并附省略号。`selectionToChat` 通过 `resourceStoreApi.createOrReuse(type='retrieval')` 和 `sessionManager` 写待发送引用，调用前可能创建上游会话。PDF 入口只取选区起始页。

直接依赖包括 i18n、UnifiedNotification、sessionManager、ensureActiveChatSession、resourceStoreApi／ContextRef、errorUtils、viewStore、APP_EVENTS、workbenchBus、windowStore、SELECTION_TYPE_ID；资源持久化与会话／窗口实现进一步依赖上游应用。此处没有为了保留几十行序列化代码而复制这些传递模块。

正式 A 应保留“文本＋位置共同形成不可变快照”的原实现结构，替换为项目草稿身份；拒绝或显式处理超限，不能静默截断。来源回跳不授予模型回读整份资料的权限。locator 展示算法可以小范围提取，但必须扩展为实际跨页来源，不把单页 locator 冒充跨页能力。

### C02：扫描器和 LRU 的真实闭包

`translationNdjsonParser.ts` 和 `translationCache.ts` 只 type-import `AlignedSegment`，编译后没有第三方运行依赖。缓存另使用平台 `Map`、`crypto.subtle`、`TextEncoder`，并在存在 `window` 时注册 `model_assignments_changed` 事件。类型文件包含上游 Tauri 事件／请求的结构描述，但无 Tauri import，不应直接变成项目协议。

16 个本地检查确认：字符串／转义跨 chunk 扫描、done 后不产新段、旧 segments 包装兜底、100 条 LRU 淘汰／命中提升、模型事件清空，以及下列局限：

- 花括号闭合但无效的 JSON 被静默忽略；`flush` 丢弃未闭合残片且不返回错误，前段成功并不能证明末段完整。外层必须分别记录已展示片段、解析问题、协议终止和实际模型流终止，不因 `segments.length > 0` 就发布完整成功。
- 缓存键不包含思考强度、提示版本、选区修订；无 secure crypto 时退化为 djb2。应在包装层校验可用 SHA-256 并加完整命名空间，不能用此弱键充当来源身份。局部请求强制空 `contextBefore`／`contextAfter`，仅发实际所选文字。
- 原浮层确有 `reqIdRef` 隔离、关闭／卸载取消、requestAnimationFrame 批处理；仍耦合 Tauri invoke/listen、React、i18n、图标及上游 UI。适配只保留生命周期代码，接现有 Harness；未运行其 UI，不宣称实际活动取消正确。

### C03：保留事务结构，补项目事实身份

`submit_answer` 在同一事务内取得题目、按 `(question_id, client_request_id)` 回放、写作答、更新统计及掌握事件；`None` 正确性保持待判定。需要人工判定的相同最近作答可转改判而不增加 attempt。`apply_submission_verdict_in_tx` 对同向改判短路并调整计数。`question_repo` 有预期更新时间的 OCC 路径；原作答结构绑定可变 question_id，没有本项目独立的题目修订字段。

必须适配：真实作答仅由用户操作创建；同一请求ID带不同答案应拒绝；旧作答固定旧题目／答案版本；评价原判与用户改判追加保存。上游改判直接更新既有 submission 判定，不能照搬成覆盖历史。错题计划在 commit 后 best-effort 调用，失败仅告警，不足以保证本项目“已判错必入复习”；应使用可恢复的明确副作用记录。上游 `correct_count >= 2` 即 mastered 的规则不迁入，错题 SM-2 不并成卡片 FSRS。

题型纯校验可抽出后 A：选择题选项数／key、答案key、判断题布尔值、非空题干等。原生成校验明确不支持 numeric／matching／ordering，而作答服务支持相关结构；不能把两个支持集合混为一个。`parse_generation_output` 的剔除／数量截断须返回明细，完整任务不能因过滤失败而静默减少题数。未移植或运行这些 Rust 服务，不作其全应用并发闭包保证。

### C04：纯 Rust 调度确实可保留

原 `fsrs_scheduler.rs` 只有 `chrono::{Duration, NaiveDate, TimeZone}` 和 `fsrs::{MemoryState, NextStates, FSRS}` 两组外部使用。独立 crate 直接以原文件作为 lib，不需要 Tauri、VFS、数据库或上游宿主。固定 `chrono=0.4.41`、`fsrs=6.6.2` 后，最终 Cargo.lock 含 89 个注册表包，所有版本／checksum 均与固定上游锁相同；实际 Windows normal/build 闭包为 40 包，详细树和许可见 dependency-manifest。其他目标平台尚未编译。

已装 `rustc/cargo 1.96.0` 与 MSVC，无安装工具链。19 个原测试通过，包括新卡步长、重学、间隔顺序／上限、确定性 fuzz、上海固定偏移日切、到期日逻辑天数及缺失记忆处理。没有把它们当作项目今日仲裁／评分事务测试。

固定新卡检查程序本机实际输出 `Again=1分钟、Hard=5.5分钟、Good=10分钟、Easy=8天`。普通 release 为 164,864 字节，SHA-256 `3f0e1ad6634f7911adc2d52820d70f89a2d57577667f16d8ab8e7aa9ffa5a5cc`，dumpbin 显示 `VCRUNTIME140.dll` 与系统 CRT API 依赖。以 `-C target-feature=+crt-static` 另构建后为 287,744 字节，SHA-256 `483610e13213cc9ce2f11845aa3f4dac28b26039bf6566054dcd215f29a34d6f`，相同固定输入运行成功，导入仅 `api-ms-win-core-synch-l1-2-0.dll`、`KERNEL32.dll`、`ntdll.dll`。这是实际本机制品观察，不是仅由 Cargo 声明推导。

**建议继续保留原调度层，再由项目增加有界 JSON／本地库包装。** 当前不存在因语言或工具链而必须移植 Python 的证据；不需要最终用户安装 Rust／VC 开发环境。此建议仍需正式 helper、构建打包、干净普通 Windows 环境和项目事务验证。没有把只输出固定 fixture 的检查程序当生产接口，也未将检查制品加入正式运行依赖。本次未安装 Python FSRS；不主张用未经等价验证的另一实现替换已可保留的原码。

外层必须校验 rating 和参数：原 `for_rating` 对越界值走 Good，`effective_parameters` 遇非法参数回退默认，不能成为正式业务的静默纠错。算法／参数版本、时区／日切明确保存；原 04:00 日切不是本项目已确认产品默认。`FsrsReviewService::rate_with_mastery_bias_cas` 的操作ID、before snapshot、暂停校验与评分 CAS 可 A，但不要复制其数据库或自动 mastery bias。

卡片创建已核非空／100张上限、front/back 或 text 内容校验、模板存在性、写入及默认入队回执。原 enqueue 出错仅告警并回 `enqueuedForReview=0`，不能直接满足“保存成功但入队失败可恢复”；项目须独立保存两项结果并可对账。统一今日总量、暂停不自动唤醒、撤销与改判仍由项目实现。

### C05：轮次与评分解析可 A，不能复制模型执行

执行器对作文正文 trim 后非空及 50,000 字符上限先拒绝，再解析评分标准；已有 session 读取上一轮原文／结果，下一轮为 latest+1。pipeline 保存 `request.input_text` 和完整原批改文本、精确总分、分项JSON；兼容整数列仅取 round，不应成为唯一分数。

解析器的可保留内容：按属性名读取、XML实体解码、最后一个 score 标签、finite／max 边界、按 rubric 的维度上限、分项及求和制校验。纯函数仍依赖 `GradingMode`／`ParsedScore`／`DimensionScore`、regex／OnceLock 和日志；应抽出后适配。原解析会 clamp 分数、跳过坏维度或以维度和修正 total，项目必须同时保留 raw、解析结果和诊断，不将这些修正冒充模型原判或评分可靠性。

不能搬入的部分有 Model2 默认配置／解密、ProviderAdapter 与直接流调用、VFS、Tauri 事件和 tokio spawn。`ESSAY_TASKS` 是进程内字典，不能替代重启恢复。`sanitize_user_input` 会截断并替换匹配到的“指令”文本，不宜用于静默改写用户原稿；本项目应保持实际原稿及明确输入边界。两稿并发、轮次 CAS、批注字符范围与原文hash、采纳修改及撤销、用户改判、迟到结果隔离均无产品证据，需主线程实现。

### C06：IR／Markdown 与字幕映射分开处理

`ir.ts` 零 import；`windowing.ts` 只依赖 `ir.ts`；`markdown.ts` 运行时只依赖 `ir.ts`，type-only 依赖 `prompts.ts::HandoutLang`。prompts 本身零 import，本次按原文件归档以闭合类型解析，未调用其帧／模型提示。七个 TS 候选不增加 npm 运行依赖。

本地 fixture 确认列表／表格结构、行列不齐拒绝、Markdown 单元格竖线转义与公式保留。同时确认这些必须适配的事实：

- 时间显示／IR 解析向下取整秒，Markdown 锚点只有 resourceId 和秒，无源版本。结构化来源须另存段ID、整数毫秒、源修订、原始／用户／AI身份；显示值不回写来源。
- 超长单行按字符硬切，拼接可恢复该行文本，但第二个硬块起不再携带时间戳。正式块必须携带原段及原文字符区间／毫秒映射，校验全文可重组且无丢失／重复；只有字符串数组不足以验收。
- `sectionTranscriptWindow` 默认前后加5秒，且只按 startMs 判断；会纳入附近段，也会漏掉起点在窗外但与窗重叠的长段。正式工具先核授权，再按明确重叠／裁切语义处理，不能将此默认扩窗用于局部翻译或精确任务。
- `parseOutline` 最多12节，把最后时间拉到媒体末尾并不保留被截断节的知识点，也不证明全内容处理覆盖。完整归纳先枚举全部源单位，不能以大纲终点代替覆盖账。
- `parseSectionBlocks` 静默丢弃越界／非法 figure、空文本；`salvageBlocks` 退成失败提示也继续；Markdown 缺图直接省略。正式完整任务要保留缺口并暂停，不能继承这些分支作为成功。

原 pipeline 会 extractFrames、图像模型说明、snapToFrame、高清重抽，并将失败降成无图讲义；`frames.ts`／`api.ts` 接媒体和上游执行。**不复制或调用这些流程。** 归档 windowing 中的帧纯函数不产生读取能力，也不作为正式导出接口。正式配图只能来自各自独立授权且可溯源的文档局部／已提交图像，不通过讲义补视频帧。

## 3. 原件 hash 与许可

原件均位于 `third_party/deepstudent/upstream/` 对应原路径。下列完整 SHA-256 与 manifest 一致；没有换行／格式化修改。

| 文件 | SHA-256 |
| --- | --- |
| translationNdjsonParser.ts | `86118e364aa9c87b8e285d0a8923e320e6e9f931437c85fa8e624ca774931b64` |
| translationCache.ts | `0bbc34b989ac18f6f9fe3bb41c75fc87dde522208da5aa450b043c413e68c210` |
| translationTypes.ts | `4c1c77e6b389c7562df35d187f57be04114e8cb7c98fc7d1c974a7b43da009d8` |
| ir.ts | `9f6e31a9c1d5d2b099eeb690d317d869bc8291f9d1fb910fb9928bfad6d7e381` |
| markdown.ts | `6360e692b6811fdca644b91e66cdbfe9c41897c9e34dce9405d6efd4f20a6431` |
| windowing.ts | `b84e36c4b3d9bba1c6ef1ac8d53641a87fb8ae9770c664fce130309ec97691ec` |
| prompts.ts | `1c93b3fc8214d6e54a62381ee615d978d5d847b66bf4640b9b9528c51dcf4f12` |
| fsrs_scheduler.rs | `62aad68b7b4a2fe1f0d43d5f2059acc35970c5a30ff6672a434be310f85c6686` |

根 LICENSE 是 GNU AGPL v3 原文，上游第三方说明自述 AGPL-3.0-or-later；两份均原样归档。`ir.ts` 有完整 BA7MLV MIT 通知，`prompts.ts` 有其 MIT 来源声明；不能因此把其他 DeepStudent 文件默认为 MIT。根许可、文件头和 `legal/THIRD_PARTY_NOTICES.txt` 继续保留各自身份。

实际 Rust 闭包：fsrs 6.6.2 为 BSD-3-Clause，chrono 0.4.41 为 MIT OR Apache-2.0；40 包中每包许可文件／通知及 hash 已归档。priority-queue 2.7.0 为 LGPL-3.0-or-later OR MPL-2.0，crate 未打包完整许可正文；由其 `.cargo_vcs_info.json` 精确提交 `0c76fb8fe75e4457f16f8e6c8e86508d1a89ba1d` 找回 [MPL 原文](https://github.com/garro95/priority-queue/blob/0c76fb8fe75e4457f16f8e6c8e86508d1a89ba1d/MPL-2.0.txt)、[LGPL 原文](https://github.com/garro95/priority-queue/blob/0c76fb8fe75e4457f16f8e6c8e86508d1a89ba1d/LGPL-3.0-or-later.txt)，按 Git blob 校验，并保留实际 crate `src/lib.rs` 的版权／许可头。原通知与检验值见 dependency-manifest。本报告记录许可事实，不把未确定的项目最终分发／链接方式判为法律兼容已验收。

项目新增代码只有独立检查脚本、窄 Cargo manifest／main、来源／依赖清单和报告；未改 8 个上游原文件。其后任何 A 修改应另保留修改差异和来源通知。

## 4. 验证结果及历史失败

| 层级／命令 | 结果 | 能证明的范围 |
| --- | --- | --- |
| 固定提交及原件 SHA-256／Git blob | `passed` | 固定源码身份、字节一致；不证明发布包对应。 |
| `node --test third_party/deepstudent/checks/candidates.test.mjs` | `passed`，16/16 | 独立解析／缓存／IR／字幕 fixture；部分检查明确刻画上游缺陷。 |
| validation.json 中独立 `tsc --noEmit --strict ...` | `passed` | 当前固定 TS 可类型检查候选及其 type-only 闭包。 |
| `cargo test --locked --manifest-path third_party/deepstudent/checks/fsrs/Cargo.toml --target-dir .local/cache/reuse/deepstudent-fsrs-target` | `passed`，19/19 | 原 scheduler 独立编译、固定本地 fixture；不是产品评分服务。 |
| `cargo metadata ... --locked --filter-platform x86_64-pc-windows-msvc` 及原锁比对 | `passed` | 89 个固定锁包版本／checksum相同、Windows normal/build 40 包。 |
| 普通 release 与 `+crt-static` 独立检查程序、dumpbin | `passed` | 本机固定输入运行、实际 PE 导入闭包；未验证普通干净机器。 |
| 产品／Harness／真实模型／活动取消／用户学习／离线包 | `not_run` | 本次未做；不由候选通过推导。 |

历史保留：首次手工裁剪锁的 `--locked` 测试拒绝更新；随后 `generate-lockfile --offline` 报 chacha20 0.10.1 yanked。没有换掉固定 fsrs 或绕过版本锁，正常 Cargo test 使用原锁的 chacha20 0.10.2 等版本成功、删除未使用项并规整引用，最终版本／checksum 比对无差异，最终 locked 重测19通过。另首次 Node 为15通过1失败，原因是新写 ragged-table fixture 的 JSON 字面量缺括号；改用 JSON.stringify 后16通过，上游原码未改。未抹去这些初始失败。

本次模型调用数为0，无出站attempt／usage记录可冒充；未测试真实模型成本、准确率、性能或取消。本次也没有运行 DeepStudent 发布包或产品，未使用 REA 二进制引擎诊断作为源码工作的前置。

## 5. 交给集成的边界与未覆盖

可立即接续的正式候选在 [third_party/deepstudent](../../../third_party/deepstudent/README.md)，不依赖忽略缓存；优先在外部包装保留 R 内核。对于 C01／C03／C05 以及 C02/C04/C06 的 A 部分，报告提供实际文件／符号和不迁入整文件的具体依赖理由，并未以“只借设计”替代逐项检查。

仍未覆盖：选区跨页交互和输入 guard、真实翻译流与过期缓存／活动停止、题目版本／真实作答／改判／复习事务、生产调度 helper 的有界协议和错误、统一今日总量／暂停／撤销、作文两轮与批注锚、完整字幕映射与缺口恢复、真实 Harness／用量、普通 Windows 安装包、离线实包和用户正常学习体验。完整应用依赖树／发布包源码一致性未审计；未采用的 monolith 仅沿所列入口做有界调查。

因此本交付结论为：**C01～C06 复用准备及候选级验证已收尾，产品集成与场景验收未完成。** 不据此更新唯一 MVP 计划为统一交付，不把检查程序或原样候选称为可用学习功能。
