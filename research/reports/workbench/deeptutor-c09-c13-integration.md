# C09～C13 独立包迁入与实际接口取证

日期：2026-10-23。上游固定为 DeepTutor `a31d82624c27d75f4e771d4fcb78dce16470b383`。
本次完成独立包内纯模块迁入与确定性适配，未修改 backend/frontend/desktop、正式 runtime、依赖声明/锁、
source_digest 或用户数据；没有真实模型、用户材料、业务库或 Host 调用。REA 按完整源码路由核原函数，
实际 wheel、重启 fixture 与既有冻结浏览器证据分别记录，不将候选通过写成产品通过。

## 准确入口与当前制品

- 发行包：`learning-deeptutor-reuse==0.1.0`；import 包：`deeptutor_reuse`。
- 目录：`D:/Codex/Learning/third_party/deeptutor/src/deeptutor_reuse/`，不是 `src/learning_deeptutor_reuse/`。
- [最小可执行示例](../../../third_party/deeptutor/checks/integration-candidate/minimal_example.py)、
  [接口调用说明](../../../third_party/deeptutor/checks/integration-candidate/README.md)、
  [实际验证脚本](../../../third_party/deeptutor/checks/integration-candidate/verify_integration.py)。
- 实际 wheel：`.local/cache/workbench/deeptutor-c09-c13/dist-final/learning_deeptutor_reuse-0.1.0-py3-none-any.whl`。
- wheel SHA-256：`f7b0b7360eab9a277537128bc6d12706144f082a3241c4d0e3cbc6d363599265`。
- 真实安装位置：`.local/cache/workbench/deeptutor-c09-c13/wheel-env/Lib/site-packages/deeptutor_reuse/`，
  仅为独占验证环境；产品不能依赖该路径。

无新增依赖，仍只要求 Python 3.12、Pydantic 2.11.7；可选完整协议校验继续使用 jsonschema 4.25.1。
wheel 由固定 hatchling 构建，独立环境按现有 lock 离线安装依赖，再安装实际非 editable wheel。
集成方需执行 `uv sync --project backend --locked --reinstall-package learning-deeptutor-reuse`。
本分工没有刷新正式 backend 环境。

## 真正保留与新增的代码

| C项／目标 | 原文件／符号 | 实际取舍 |
| --- | --- | --- |
| C09 `learning_transactions.py` | `learning/models.py` 的 PendingOption、PendingQuestion、InteractionStatus、MasteryInteraction、MasteryEvent、MasteryPathLease | 既有类摘录保持；去除旧字符串选项解析器，保留原分组验证。没有自动判分或业务事务。 |
| C09 `book_progress.py` | `book/progress.py` 的 record_attempt、latest_attempts、recompute_score、recompute_weak_chapters、mark_visited、toggle_bookmark、completion_ratio | 本轮从已验离线候选迁入正式包；完整原文件仅改模型导入，原函数体保留。 |
| C10 `book_models.py` | `book/models.py` 全模型 | 原文件字节不变。Page/Block 被调用方称为 BookPage/LearningBlock，包内没有这两个额外类名。 |
| C10 `book_block_compiler.py` | `book/blocks/base.py` 的 BlockContext、BlockGenerator.generate、GenerationFailure、失败分类 | 保留默认 registry 之前的完整原前缀，只改模型 import；不导入整套 AI 生成器。 |
| C10 `book_compiler_state.py` | `book/compiler.py::_finalize_page_status` | 既有原方法＋终态前置 wrapper 不变，新增恢复适配实际调用。 |
| C10 `book_export.py` | `book/export.py::render_block/render_book_markdown/export_filename` | 完整原文件仅改模型 import，实际生成三章 Markdown 投影。 |
| C10 `book_resume.py` | 调用上述原模型与原状态聚合 | 项目新 DTO／纯状态迁移，不能称为原 BookEngine。一次目录确认、逐块结果、未知对账、完成章可读均有实际 fixture。 |
| C09 `learning_actions.py` | 调用原 Progress/MasteryInteraction/MasteryEvent、mark_visited | 项目新动作适配，跳过／解析只返回事件，不创建 QuizAttempt 或掌握。 |
| C11/C12 `offline_data.py` | 原 VisualizationEnvelope；既有离线候选的严格数据模型思路 | 项目新封闭校验；本轮补有限数值、display_math 校验、拒绝 percent 编码路径穿越、拒绝业务/模型事件。不是通用 HTML/JS sanitizer。 |
| C11 `visualization_models.py` | `visualizers/protocol.py` 的原 envelope DTO | 字节与现有摘录保持；原 DTO 接受 Any 与 entry_url，本身无安全保证。 |
| C12 既有离线包 | 原题型／答案键、阅读导航、Viewer 小解析器和上列模型/纯编译 | 保留已冻结成品及真实浏览器记录，逐资源重新核 hash；本轮不重建旧包或覆盖记录。 |
| C13 `research_similarity.py` | 原 normalize/token/similarity | 未改变；实际调用通过。它没有权威研究队列、请求重试、来源授权或恢复能力。 |

逐文件 Git blob、SHA-256、模式和修改通知已更新 `third_party/deeptutor/reuse-manifest.json` 与 `UPSTREAM.md`。
新增原文件的 hash：base `c21a9f3beb83002eb073282c0e525c907c6f183a586e57b6da4cf958cd3ad373`；
progress `93b9f95ca676b5db33b9e7be3ec723b7329b4bbcc075ffd6132c6357a043cbfc`；
export `b185f72d4dc166ae5e6b90a0033e98a4f7bba3b372e840c1b70bb06e2ddcc70b`。
上游 Apache-2.0 原许可及通知继续随 wheel 归档，没有引入供应商运行层或新许可资源。
`checks/integration-candidate/evidence/proof.json` 进一步实查 26 个保留函数体的允许差异、19 个实际
wheel 模块与当前源码/manifest 相等、根 LICENSE 与 UPSTREAM.md 在 wheel 中逐字节相等。

新增项目模块 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `book_resume.py` | `ab131cffacf009becac08503122d5ad7cdd37e78ae7d3aab7bc8917a411a367f` |
| `learning_actions.py` | `c14608083f64a3fcd57bd031bf498ddc2026342495ae6044297a9630c356a728` |
| `offline_data.py` | `d5dfe6c7245bade6312bfb2deee2f8e361db41b23f2febeeed7b079d2a3d7200` |

## 实际字段与调用约束

全部字段类型由真实 wheel 的 `model_fields` 导出至
`.local/cache/workbench/deeptutor-c09-c13/wheel-r3/actual-api-fields.json`，不依赖猜测类名。

| 原模型 | 集成必须识别的真实字段 |
| --- | --- |
| Book | id、revision、title、description、status、proposal、knowledge_bases、language、depth、page_count、chapter_count、metadata、kb_fingerprints、kb_document_fingerprints、stale_page_ids、stale_detected_at、created_at、updated_at |
| BookInputs | source_refs、source_context、user_intent、chat_session_id、chat_selections、chat_history、notebook_refs、knowledge_bases、question_categories、question_entries、language、captured_at |
| Spine | book_id、chapters、version、updated_at、concept_graph、exploration_summary |
| Chapter | id、title、learning_objectives、content_type、source_anchors、prerequisites、page_ids、summary、order |
| Page | id、book_id、chapter_id、title、learning_objectives、content_type、status、order、blocks、links、parent_page_id、error、created_at、updated_at |
| Block | id、type、status、title、params、payload、source_anchors、metadata、error、created_at、updated_at |
| Progress | book_id、current_page_id、visited_page_ids、bookmarked_page_ids、quiz_attempts、weak_chapters、score、updated_at |
| PendingQuestion | question_id、knowledge_point_id、module_id、prompt、question_type、expected_answer、options、explanation、difficulty、source_evidence、assistance_used、assistance_context、visual_context、accepted_answers、questions、created_at |
| MasteryInteraction | interaction_id、path_id、question、status、session_id、turn_id、user_answer、answers、result、created_at、updated_at |
| MasteryEvent | id、path_id、revision、event_type、payload、session_id、turn_id、created_at |
| MasteryPathLease | path_id、session_id、turn_id、acquired_at；只有 DTO，没有加锁/过期执行行为 |
| VisualizationEnvelope | schema_version、render_type、renderer、payload、presentation、interaction、fallback |
| RendererRef | id、version、target(native/iframe/artifact)、native_renderer、entry_url；目标权限须由宿主/受限适配另核 |
| book_models.QuizAttempt | block_id、page_id、question_id、user_answer、is_correct(bool或None)、submission_id、timestamp；不是 learning_transactions 中的类型 |

SourceAnchor 只有 kind/kb_name/ref/snippet，extra 字段会被忽略；不能在里面临时塞 asset_id/source_version 后以为已保留。
项目强来源链放已冻结的 BookInputs.source_refs、成果块/任务的正式 SourceRef 和应用版本记录。ChatSelection 的
空 message_ids 虽有上游“最近全部”注释，本包没有检索行为，不能把它解释成读取更多历史的许可。

Book/page/chapter/block 的 id 默认会生成，恢复时须传回稳定原 id。Book.page_count/chapter_count、revision 和
时间字段不会由本适配自动维护成业务真值，应用应根据自己的成果版本/目录事务填充；不要混作后端 revision。
原模型 extra=allow/ignore 和宽 params/payload 都不是工具授权或输入清洗边界。

### 一次目录确认与恢复

`BookCompileSnapshot` 字段为 schema_version=1、book、inputs、spine、pages、confirmed_outline_digest、
confirmation_count、operations。每个 BlockOperation 保存 operation_id/page_id/block_id、
state(in_flight/unknown/committed/not_started)、output_digest、reconciliation_ref。

`outline_digest` 冻结书/目录、块计划和输入，排除生成 payload/status；页在存储列表中的物理顺序不影响摘要。
`confirm_outline` 同摘要幂等，confirmation_count 最多 1；输入来源版本或目录改变会得到
`BOOK_OUTLINE_VERSION_CONFLICT`。新目录修订/源范围确认仍归产品，不靠重写此字段绕过。

`next_block` 仅派生下一个工作项；`begin_block` 返回 snapshot/dispatch，既有 operation 返回 dispatch=False。
它没有实际调度或模型权。项目须在出站前持久化开始状态。`make_block_context` 返回隔离对象，避免原
BlockGenerator.generate 原地写 Block 污染尚未提交的检查点；返回的生成 receipt 再交 `complete_block`。
同一 receipt 幂等，异内容的同一 operation 被拒绝。只在块均终态后调用原页聚合函数。

`interrupt_block` 保留未知 generating 单位；resume/next 不自动重放。已有真实结果时 complete 对账；能证明
未开始时才能显式 reconcile_not_started，并记录证据引用。新 attempt 必须新 operation_id。
错误块返回明确待决定状态，不因原 metadata.retryable=True 自动发第二次请求。

实际三章/六块 fixture 在第一章完成后立即读取该章，再在第二章第四块的“已有 receipt、尚未提交”窗口
保存 JSON，重新 model_validate_json 恢复。恢复被 unknown guard 拦住；导入原 receipt 后显式 resume，连续
完成后续块，总 generator 调用恰为 6、目录确认恰为 1、第一章字节 hash 未变。最终 render_book_markdown
实际包含第三章第二块，不是只有入口或空状态图。

### 自由跳过与解析

`apply_learning_action` 返回原 Progress/Interaction 的独立副本和原 MasteryEvent，参数含 action、event_id、
revision、now、可选 target_page_id。事件 idempotency/事务/目标读取权由应用拥有；包不自建事件存储。
合法后续页面可直接导航，不看 prerequisites 或掌握闯关。

活跃题跳过成为 ABANDONED；看解析只标 assistance_used（分组叶子也标），均不新增 QuizAttempt。
原 wrong、weak_chapters 和分数保持。`independent_grade_eligible` 只检查已 GRADED、应用提供的
adjudicated_correct is True 且无暴露；它不写掌握、不代替主观题判分。之前独立判分的题后来查看解析不会
反向抹掉既有证据。题目的 expected_answer/explanation/source_evidence 仍是服务器字段，不能把 DTO 整体发前端。

原 record_attempt(None) 会影响“最后一次判分”的推导，所以不能用“未判分作答”代替看解析事件。
Progress.score 仅当前正确题数；completion_ratio 仅访问比例，二者都不是掌握度或复习队列。

### 可视化与离线权限

`validate_offline_visualization` 实际先核原始 dict，再构造原 VisualizationEnvelope；限定版本、native
renderer id=`offline.function-plot`、native_renderer/render_type=`quadratic_tangent`、
format=`learning.parameter-plot/v1`。PlotData 只有 kind/a/x0，参数只有 label/min/max/step/initial，
数值有限且在 ±1000 内，每轴步数不超 10000。

额外业务字段、iframe、远程/本地 entry_url、交互模型事件、HTML fallback、代码表达式、原型字段、远程 schema、
无限/极端参数、路径穿越/percent 编码路径与危险 TeX 链接均实际被拒绝。该函数不执行表达式、不访问网络、
模型或文件。FigurePayload 只允许规范 assets/ 相对路径，实际文件和 hash 仍须与项目导出 manifest 对账。

浏览器隔离还须使用已验证的 CSP：connect-src none、frame/object none、禁止表单外发和 unsafe-eval；
DOM/SVG 由固定 renderer 创建，纯文本用 textContent，KaTeX trust=false。校验器不能替代 CSP、资源清单、
本地来源链接约束或桌面隔离。模型产出的任意 HTML/JS、原始 SVG 和通用函数表达式仍不支持。

既有离线 zip SHA-256 `73b6da21f85515befb07bbfdb0a3f0d15cd856a1980b85b2ec5f50d95a2e82fe`，
75 个文件及当前解包目录重新核 hash 一致，沿用已冻结的 Chrome155 file:// 真实断网验证：0 HTTP/WS、
0 业务存储写、0 自动模型请求，没有应用后台。本轮没有重新启动浏览器，不能把历史冻结证据说成新浏览器运行。
其完整来源、CSP、参数、自测、字体与来源回跳见 [原离线候选报告](../../../third_party/deeptutor/checks/offline-candidate/REPORT.md)。

## 实际验证与未覆盖

| 验证 | 结果／证据 |
| --- | --- |
| 新源码 fixture | 8 组 passed，`.local/cache/workbench/deeptutor-c09-c13/source-r2/summary.json` |
| 实际 wheel fixture | 8 组 passed，`wheel-r1/summary.json`；验证脚本清理和字段补全后 `wheel-r2/summary.json` 仍 8 passed；最终清单自身/全部75文件对账见 `wheel-r3/summary.json`，8组passed |
| 实际 wheel 原回归 | 16 passed、0 skipped，`installed-original-regression.stderr.txt` |
| 原环境回归 | 15 passed、1 optional jsonschema skipped，没有为此改 backend 依赖 |
| 两章最小例子 | ready；一次确认；逐步 readable 为 c1 → c1,c2；模型调用 0 |
| 安装与保护 hash | `wheel-verification.json`：包/后端声明和锁、source_digest 前后相同 |
| 代码检查 | 新适配/脚本 ruff passed；未格式化原上游方法或冻结离线包 |

实际命令在集成候选 README 中给出；所有 fixture 为本次自造文字/数学内容，不启动用户 Host。
初次 source-r1 有 3 组因原 generator 原地修改检查点而触发 BOOK_OPERATION_STATE_CONFLICT，保留失败 JSON；
增加 make_block_context 隔离后通过，没有放宽状态验证。首次验证脚本 lint 的导入位置/未用导入/紧凑语句
已修正，仅格式化验证脚本，未改变已构建包源字节。
冻结脚本最初按 LICENSE 文件名取首项，误选随包依赖许可；已改为精确 dist-info/licenses 路径后通过，
旧脚本在忽略缓存 `freeze-proof-first.py` 保留，没有更改实际许可文件。

这些仍不是正式业务来源 guard、任务暂停/恢复提交、过期 epoch 回调、桌面通道、成果编辑版本冲突、
真实 Harness、真实模型、正式导出 API/UI、任意图/动画或用户学习体验的验收。归档/删除权限、业务 revision、
范围重规划和持久队列继续由 Root 的正式库/执行层拥有；本包只提供可核验的确定性 DTO 与转换。
