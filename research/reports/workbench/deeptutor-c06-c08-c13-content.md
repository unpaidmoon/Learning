# C06／C08／C13 内容切块、检索评测和研究恢复候选

日期：2026-10-23。范围为独立源码和合成 fixture；没有修改正式 backend/frontend/desktop/runtime、
source_digest、依赖声明或锁，没有真实检索器、模型、用户材料、数据库或 Host 调用。
完整源码按 REA 的源码路由核读，实际 wheel 与上游缺陷探针分别记录，不把本候选当成产品检索闭环。

## 归属、来源和交付入口

实施方案已经明确：C06 是 **DeepStudent**，C08 与 C13 是 **DeepTutor**。

| C项 | 固定来源 | 本次可迁入部分 |
| --- | --- | --- |
| C06 | DS `635216503c0b34bdf812e17942e0f09f51872e6f`，`src/features/media-handout/{ir.ts,markdown.ts,windowing.ts}` | 原 IR／Markdown 函数实际调用；新严格 text-only 包装、原始切块缺陷证据；项目独立无损 codepoint 窗口 |
| C08 | DT `a31d82624c27d75f4e771d4fcb78dce16470b383`，`deeptutor/services/rag/eval/{dataset.py,metrics.py,matching.py}` | 前两个原样文件不变，新增原样 matching.py，补齐文本→gold index hits→原指标链 |
| C13 | 同 DT 提交，`deeptutor/agents/research/data_structures.py` | 原 TopicStatus、TopicBlock 方法及 9 个只读查询／统计；UTC／证据版本引用适配；纯恢复建议，不建业务队列 |

准确 import 前缀为 `deeptutor_reuse`，源码位于 `third_party/deeptutor/src/deeptutor_reuse/`。
新增 Python 文件：`eval_matching.py`、`source_windows.py`、`research_records.py`、`research_recovery.py`。
发行包仍为 `learning-deeptutor-reuse==0.1.0`，无新增依赖。

TS 原件、严格包装和验证在 [content-candidate](../../../third_party/deeptutor/checks/content-candidate/README.md)。
DS 原件／通知留在独立 checks，未并入 Apache-2.0 Python wheel；原 `ir.ts` 的 BA7MLV / wangke-agent
文件级 MIT 全文和 DS 根 AGPL-3.0-or-later 原声明均保留，不把所有文件声称为同一种许可。
可直接 import 的 C06 闭包是 handout_adapter.ts 加 retained/ir.ts、markdown.ts、handout_types.ts 四个文件；
IR 保留原前缀并排除 salvage/帧时间收集，Markdown 只将 type-only prompts 导入改到本地类型。

本次实际 wheel 位于 `.local/cache/workbench/deeptutor-c06-c08-c13/dist/learning_deeptutor_reuse-0.1.0-py3-none-any.whl`，
SHA-256 `8a116a278f8873e0bac4b9fcce2cb7ad686344ffa95ec6899b78a01ab8f784cb`。
独占环境按已有锁离线安装依赖，再安装真实非 editable wheel；正式运行不能依赖这些缓存路径。
集成方需要 `uv sync --project backend --locked --reinstall-package learning-deeptutor-reuse`。

## C06：原代码保留和无损窗口

原 `ir.ts` SHA-256 `9f6e31a9c1d5d2b099eeb690d317d869bc8291f9d1fb910fb9928bfad6d7e381`；
`markdown.ts` 为 `6360e692b6811fdca644b91e66cdbfe9c41897c9e34dce9405d6efd4f20a6431`；
`windowing.ts` 为 `b84e36c4b3d9bba1c6ef1ac8d53641a87fb8ae9770c664fce130309ec97691ec`。
全部重算 Git blob/SHA-256，与已有 DS 固定原件清单一致。

Node fixture 使用项目已固定 TypeScript 5.9.3 转译，实际调用原 `parseSectionBlocks`、`handoutToMarkdown`、
`formatClock` 等函数。保留列表／表格结构与列数检查、编号清理、表格竖线转义和 Markdown 输出。
`markdown.ts` 的 prompts 导入是 type-only，转译后没有供应商、React、VFS 或提示词运行依赖。
最终还对这四个闭包文件做 strict TypeScript 类型检查，0错误；不冒称整个 DS 类型检查或产品浏览器通过。

原函数的具体限制已实际复现：

- `chunkTranscript` 按 JS UTF-16 length/slice 切长行，后续片段没有原时间前缀；6001-unit fixture 切出
  孤立代理项。前导空行会丢失，通用 join 空串／换行不能还原所有原文本边界。
- `segmentsToTranscript` 会 trim 文本；`usableSegments` 只取完成且非空的段，不适合当完整任务的范围枚举。
- `parseClock` / `formatClock` 向下取整秒，不能替代原始毫秒定位。
- IR parser 会丢无效／越界 figure，Markdown renderer 会省略未提供图片映射的 figure；原 salvage 分支
  还会用提示文字替代破损结果。这些行为不能作为完整成果成功发布或自动重试许可。

公开候选 `handout_adapter.ts::renderBoundHandout` 实际调用原 IR／Markdown，只接受已由项目绑定的
source asset/version/hash/content_revision/role 和整数 ms。输出 Markdown 及完整 ms／版本 sidecar，
展示用 `[媒体@id:mm:ss]` 是较粗的文字投影，实际跳转应使用 sidecar。空文本、被 parser 丢块、坏表格、
截断 JSON 明确失败；figure 返回 `HANDOUT_CONFIRMED_IMAGE_MAPPING_REQUIRED`，不静默省图，不抽帧、不吸附。
支持用户确认图片还需要 Root 扩展显式资产映射；本候选没有该能力。

原 IR 会清理 Markdown 标记／编号和空白，适合生成内容的显示结构，不能当原材料无损归档。
本项目新 `source_windows.py` 因而独立处理原始文本，并未伪称原 DS chunkTranscript 已可原样解决问题：

| 类型／函数 | 实际契约 |
| --- | --- |
| TextUnit | unit_id、asset_id、source_version、content_revision、content_role、text；可选 page_index、segment_id、start_ms、end_ms |
| TextWindow | 上述身份／定位，加 start_codepoint/end_codepoint、原 text SHA、窗口 SHA、原样 text |
| split_text_unit | 默认最大 24000 codepoint，UTF-8 严格有效；不 trim、不正规化、不插分隔符、不重叠；可优先在后半窗换行后切 |
| reassemble_text_unit | 按原序核连续半开偏移、源身份／角色／修订、原片段和全部 hash；拒绝丢块、重复、乱序、改版或篡改 |

超长单行 fixture 有 270013 codepoint、750029 UTF-8 字节，切成 12 块，每块最多 24000，重组 SHA-256
与原文均为 `4d8697d15bfac1dde685f62cc33392523701d0b69e8b48067106ef24f8bd3974`。
CRLF／前后空行／尾空白、中文、emoji、组合字符和 ZWJ 均保留；31 种小 budget 也通过。偏移单位明确为
codepoint，不是 UTF-16、token 或 grapheme。视觉字素可以跨窗，但不会生成孤立代理项或丢字符。
长段子块全部保存同一原 segment_id 和完整原时间，不以字符比例伪造词级毫秒。

已有 Root 24k 冻结 leaf 必须保留旧映射；匹配旧硬边界时传 `prefer_lines=False`。默认换行优化仅用于新规划，
不能重新切旧 plan 后把新 leaf 悄悄当作旧来源。空文本返回零窗口，只表示输入为空；缺页／未转写／失败段仍须
由正式范围枚举和读取状态判定，不能把输入没出现的单位当成已覆盖。

## C08：真正补齐的纯匹配器

新增原文件 `deeptutor/services/rag/eval/matching.py` 按固定提交从 GitHub connector 取原 blob，Git SHA-1
`cab037804947fbd7b9eb82b197951818e920d3ec`，SHA-256
`19ae5bde484b6b76f594aa0cac1c6574f82b00d8374c013da94159637bf28c76`，完整原样进入 `eval_matching.py`。
它只用标准库，提供 MatchPolicy、MatchResult、GoldPassage、RelevanceMatcher、source_text、tokenize、normalize_text。
已有 eval_dataset/eval_metrics 原字节不变，不需要 DS 或额外模型执行器。

最小链为 `EvalDataset/load_dataset → RelevanceMatcher(case.gold) → match(source_text(record)).indices →
evaluate_hits → aggregate_query_metrics`。每个排名是 gold 索引集合，重复命中不重复增加 recall/new-gold nDCG/AP；
precision 分母是 cutoff 内实际返回项数，重复相关项仍属于相关返回项。QueryMetrics.retrieved 是总 hits 数，
不要误当为 cutoff 后数量。

合成排名 `[无关, gold0, gold0, gold1]`、k=4，实际 recall=1、precision=.75、MRR=.5、AP=.5、nDCG=.6509；
只返回一个正确项的短结果 precision=1。失败查询单列 `metrics=None`，不掺入成功均值；空评测显式 not_run、
queries=0，不将原聚合返回零块称为评测通过。原函数会 `int(k)`，调用方仍应在契约层校验正整数预算。

原 matching 是词面／token 重叠。实际 fixture 将 `x>0 / positive` 与 `x<0 / negative` 的相反条件句也匹配为真，
已作为已知反例记录，不修改原函数来掩盖定义。它不验证数学等价、引用源身份、内容角色、读取许可、检索生产质量
或整范围覆盖。实际检索器调用数为 0；本次仍没有建立正式 RAG 闭环。

## C13：保留查询、移除队列权威

原 `data_structures.py` SHA-256 为 `980e21a90e9c33b32d822b296ec4b4da6db6a7b2e07978dd3b32cb03cf64b46b`。
运行了完整原类的独立受控探针，没有安装整套 DT：max_length=2 的队列 save/load 后上限变为 None；
两个 researching 单位加载后仍 researching，pending=0，且并未自动恢复。原 raw_answer 的“50KB”限制
实际使用 len(str)；60000 个汉字为 180000 UTF-8 字节，字段 original_size 却为 60000，结果已被截断。
所以不能拿原 raw_answer 或有 save 方法就声称完整证据／自动恢复通过。

`research_records.py` 保留原 TopicStatus、TopicBlock DTO 方法，查询投影 ResearchTopicView 保留 9 个原方法体：
has_topic、find_similar、list_topics、get_pending_block、get_block_by_id、get_all_completed_blocks、
get_all_pending_blocks、is_all_completed、get_statistics。继承已保留的原 TopicSimilarity。它没有 add/append/mark、
save/load/auto-save 或模型权；只读投影不是新业务队列。标题相似只能给候选，不可据此跨来源合并或删除单位。

适配明确改变了三处语义：默认时间及 add trace 改为 UTC；TraceReference 代替截断 raw_answer；完整证据由
Root 资产／版本持有，记录 tool_id/citation_id/tool_type/query/summary、evidence_asset_id/version/hash、operation_id。
旧 raw_answer 字典会显式要求迁移，不静默截断或当新引用。原 latest trace／summary／状态序列化仍实际运行。

`review_research_recovery` 只接受项目提供的授权 source 三元组(asset,version,role)及 evidence 版本/hash 映射，
返回 completed、pending、reconciliation_required、failed_decision_required、blocked，不改变任何记录或调度工作。
researching 一律先核 Root 的真实 attempt ledger；缺来源／版本、缺证据或旧 naive 时间显式待决定／迁移，
不联网补齐、不回 pending 重发。完成状态没有证据时也会阻断。上游 get_statistics 只是声明状态统计，不是
经过来源和覆盖核验的完成数。

实际 JSON 保存并重读的七个自造单位中，只有一个进入 verified completed；一个 researching 待对账、一个 failed
待决定、一个 pending、另三个因缺来源／缺证据／naive 时间阻断。所有原状态保存前后未改，完整 180000 字节
证据通过引用 hash 保留。这些检查不验证纸面结论真假，也不替代 Root 的课程／页区／字幕范围 guard、CAS、
epoch、实际出站记录或数据库原子提交。

## 实际验证与未覆盖

| 验证层 | 结果／证据 |
| --- | --- |
| Python 新源码 | 6组 passed，`.local/cache/workbench/deeptutor-c06-c08-c13/source-r1/summary.json` |
| Node 原 IR/Markdown 与包装 | 4组 passed，初版 `c06-node-r2/summary.json`；最终可import闭包及strict类型检查见 `c06-node-r4/summary.json`，0类型错误；TypeScript5.9.3，Node24.14.0，非产品浏览器 |
| 真实 wheel | 6组 passed，`wheel-r1/summary.json` |
| 真实 wheel 旧回归 | 原16项全部 passed；上轮书/动作8组全部 passed，`installed-original-regression.stderr.txt`、`book-regression-r1/summary.json` |
| 锁／源码保护 | `wheel-verification.json`：包和后端声明/锁、source_digest 前后 hash 相同 |
| 本次模型/检索/Host | 均0；GitHub 只用于固定公共源文件取证，未发送用户材料 |

逐字节 wheel/源码、9个 C13 原查询方法AST、TS边界和原许可通知核验见
`third_party/deeptutor/checks/content-candidate/evidence/proof.json`。正式同步后仍需 Root 的产品接口验收。

首轮 Node 工具入口误指仓库根 node_modules，已改读现有 frontend 固定 TypeScript，无安装；首个“普通尾换行”
断言被原函数正确保留而失败，历史 c06-node-r1 留存，改为真正丢失的前导空行反例后通过，没有改原函数。
原完整队列探针的可选 json-repair 缺失提示保留；正式新模块没有该依赖或 JSON 修复流程。

正式 Markdown IR／生产检索 API／索引更新／SourceRef角色和范围 guard／有效字幕原始与手改修订选择／
全范围缺口决策／真实 Harness／真实课程学习体验仍未在本次通过。本分工没有改动这些正式产品部分，Root
应按自己的接口、事务和验收接入，不把本候选纯函数与 fixture 当作检索或研究产品已交付。
