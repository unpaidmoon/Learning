# DeepTutor C07～C14 实际复用准备与核查

日期：2026-10-23。集成负责人为主 agent；本分工只写新建 `third_party/deeptutor/`、本报告及
同名 evidence JSON，读取现有代码和研究，不改产品文件、共享实例、全局环境或用户资料。

## 结论与证据范围

已获得固定提交 `a31d82624c27d75f4e771d4fcb78dce16470b383` 的 22 个原始文件，
经 GitHub connector 提供的 Git blob SHA-1 与完整树条目核对后，原字节存入
[`upstream/`](../../../third_party/deeptutor/upstream/)。没有安装整套 DeepTutor。
固定提交的完整 Git 树为 4,407 条，`truncated=false`；公开源读取不需要 REA 引擎诊断。
这按 REA 的完整源码路由实施，不将源码事实写成应用运行观察。

已准备 14 项文件／符号迁入记录，形成 `learning-deeptutor-reuse==0.1.0` 独立包：
5 个完整原文件原样复制、5 项原函数／类摘录、4 项小范围适配（其中1项是 TypeScript）。
每项 hash、Git blob、符号、原样／修改说明见
[`upstream-lock.json`](../../../third_party/deeptutor/upstream-lock.json) 和
[`reuse-manifest.json`](../../../third_party/deeptutor/reuse-manifest.json)。
`prepare_excerpts.py` 会先验证锁定的原 blob，不能通过重新生成来接受修改过的上游文件。

最终独立验证：项目 Python 3.12.5／Pydantic 2.11.7 环境中 16 项，15 passed、1 skipped
（项目环境未装可选 jsonschema）；隔离环境安装实际 wheel 和固定可选依赖后，16 passed、0 skipped。
TypeScript fixture 3 passed。构建、脚本 lint、原通知入 wheel 检查 passed。
完整 stdout／stderr、命令、实际 wheel hash 与本项目对照源码 hash 存在
[`deeptutor-reuse-evidence.json`](deeptutor-reuse-evidence.json)。

这些结果只证明迁入小模块能独立导入、序列化并满足列出的 fixture。产品来源授权、持久事务、
模型生成、索引引擎、Harness、Windows 实窗、离线成果包和 S01～S13 学习流程均不由本分工验收。
未运行项均为 **not_run**；不能把本表直接写成已集成、已恢复或13场景通过。

## C07：来源与完整字节摘要

- 来源：`deeptutor/services/rag/source_pages.py`，SHA-256
  `51d41e4581de132137c521f86abcef5d789a8c05ef0e79845944dc1b685ee205`。
- 符号：`SourcePageError`、`SourceRef`、`source_digest`。原 DTO 顶层仅 Pydantic；摘要函数仅
  `hashlib`／`pathlib`，每次读取1 MiB，返回完整64位 SHA-256。已提取 `source_digest.py`、
  `source_ref.py`；函数及类内容保留原样，增加独立导入与说明，未引入页解析／渲染。
- 原 `resolve_source` 要求 `deeptutor.multi_user.knowledge_access`，页渲染还需要
  `source_visuals`、PyMuPDF；同原文件的构建过程依赖 LlamaIndex。这些不在运行闭包中。
- 具体适配原因：原 SourceRef 使用 `kb_ref/document_key`，没有项目课程／asset／source_version、
  字幕段／毫秒、成果修订／内容角色；crop只是浮点四元组，没有坐标语义、范围、顺序校验。
  `source_digest`相同只能说明字节相同，不能把不同来源身份合并。
- 建议落点：项目来源模块包装 `deeptutor_reuse.source_digest.source_digest`；权限、原地路径、
  旧版状态、跨页和字幕定位由项目强 SourceRef 核验。不能将模型传入的任意路径交给摘要函数。
- fixture：超过1 MiB的中文／字节内容、同内容改名、源内容改版摘要变化、坏digest／负页序／额外字段拒绝通过。
  裁剪准确性、PDF真实权限／渲染、视频图片边界未由上游 DTO 或这些 fixture 证明。

## C08：检索数据集与纯指标

- 来源：`deeptutor/services/rag/eval/dataset.py`，SHA-256
  `1d2dd5c25f946e78df2bc2cbd286cc72e9dc6f12384d33dc04c08e62565e893f`；
  `metrics.py`，SHA-256
  `4082684a67eb9a3c43cbc818f2aa93c77d1a1b330e96f5f96e7aa6b2303932de`。
- 符号：`EvalCase/EvalDataset/load_dataset`、`recall_at_k/precision_at_k/ndcg_at_k/reciprocal_rank/average_precision/evaluate_hits/aggregate_query_metrics`。
  两个完整原文件原样移为 `eval_dataset.py`、`eval_metrics.py`，仅改变包／文件路径。
  直接及传递依赖只涉及标准库和项目已固定的 Pydantic 2.11.7，没有模型或检索引擎。
- 保留语义：precision分母为实际返回项数；重复gold段不会增加recall或DCG；AP按新gold首次出现累加。
  `aggregate_query_metrics([])`会返回0均值和`queries=0`，项目不得把它显示成评测通过。
- 已核限制：文件加载拒绝重复ID，`from_cases()`没有同等去重调用；`limited(0)`通过未重校验的
  `model_copy()`产生空集合。输入hits中的gold索引必须由项目验证，不能用这些纯函数判断来源合法。
- 建议落点：检索评测模块直接调用这两个原模块；项目错误码、失败单列、受限检索入口及带版本gold fixture
  由外层提供。词面命中不证明数学等价、回答正确或完整内容覆盖。
- fixture：重复gold排名、手算nDCG／MRR／AP、短返回precision、非法k、JSONL／重复ID／坏JSON通过；真实课程检索指标 not_run。

## C09：学习事务对象

- 来源：`deeptutor/learning/models.py`，SHA-256
  `da990512532bc9bbc14ada6b80b87c44ef4408efcc3b91c1a2a6688695906f55`。
- 符号：`PendingOption/PendingQuestion/InteractionStatus/MasteryInteraction/MasteryEvent/MasteryPathLease`。
  `learning_transactions.py` 保留所需类闭包、group验证、选项label/body分离及choice_map，使用标准库和Pydantic。
- 必要适配：删除 `_read_legacy_option_strings`。它会懒导入 `learning.pending.parse_options`，
  再传入 `utils.text_display`；新项目没有这些旧数据，无需引入此链。typed对象可序列化，旧字符串options现在拒绝。
  没有迁入最近五次掌握率、门槛评分、调度或服务层。
- 项目仍需课程／任务／题目身份、作答和评分事务、用户改判、待判断、评价修订、CAS／幂等和统一复习。
  本模块的extra="ignore"也不是项目HTTP严格DTO；不能直接暴露上游宽对象成为边界。
- 建议落点：练习／评价模块的内部typed对象映射。模型有status不代表数据库事务或跨进程恢复存在。
- fixture：数学选项body保留、typed JSON往返、group重复ID拒绝、有效group、旧字符串拒绝通过；产品提交／改判／持久恢复 not_run。

## C10：书模型与终态聚合

- 来源：`deeptutor/book/models.py`，SHA-256
  `6726eea4664514a36fda7936adbfef7ac5081603f37695cb860b25a97c194bc3`；
  `compiler.py`，SHA-256
  `5c3dfe108f51b717f4b73d69a6f1f36cf959bfc29a4978493d6c4f1d03717899`。
- `book_models.py` 是完整原文件的逐字节复制：Book／Spine／Chapter／Page／Block、枚举、概念图及查找方法。
  直接依赖 enum/time/typing/uuid、Pydantic；没有上游应用运行层。
- 状态聚合原 `BookCompiler._finalize_page_status` 方法体原样保留在小owner类，外部新增
  `finalize_terminal_page(page)`，只接受READY/ERROR块，遇到pending/generating/hidden抛`PAGE_NOT_TERMINAL`。
  原方法只统计READY和ERROR，直接对中途／隐藏块调用会误报失败，故不能不加前置条件使用。
- 完整compiler依赖BookStorage、StreamBus、BlockGeneratorRegistry、SectionArchitect、直接模型工厂及重试／并发流程，未移入。
  同步依赖模型不是安装完整compiler的理由。
- 弱来源边界：SourceAnchor(extra="ignore")会丢弃source_version／区域等项目字段；BookInputs的空聊天filter
  具有上游“取全部近期消息”语义，不能沿用为资料授权。项目必须独立保存确认范围和强来源链。
- 建议落点：学习书内部IR／章节块编译；项目包装确认目录修订、完整覆盖缺口、块落盘、部分内容可读、持久检查点和离线资源。
- fixture：Book JSON往返、章节／概念查找、成功／部分／空页终态以及生成中拒绝通过；真实书生成和离线编译 not_run。

## C11：可视化协议及有限schema校验

- 来源：`deeptutor/visualizers/protocol.py`，SHA-256
  `3992d7d372577c778c0d7880e26a7cda6e64db3cb5f94d57d1a4194d1b3b9621`。
- 原 `RendererRef/VisualizationPayload/VisualizationPresentation/VisualizationInteraction/VisualizationEnvelope`
  摘为 `visualization_models.py`，Pydantic-only；`_find_remote_schema_ref`函数体原样摘为
  `visualization_refs.py`并增加公开alias。
- 完整原文件逐字节保存在 `visualization_protocol.py`，其唯一新增外部依赖为jsonschema。
  基础包不要求此库；可选`[visualization]`固定`jsonschema==4.25.1`。
  [该固定PyPI版本](https://pypi.org/project/jsonschema/4.25.1/)为MIT、提供Python3.12兼容轮子；
  实际验证版本／依赖hash以本目录`uv.lock`和依赖许可归档为准，不使用未固定latest。
- 原校验包含32,000字符schema上限、200,000字符payload上限、远端`$ref/$dynamicRef`拒绝和jsonschema结构校验。
  但原`json.loads`会接受NaN／Infinity，`renderer_entry`并不拒绝所有URL／Windows盘符，
  RendererRef／Envelope也不是项目来源或资产权限校验。不能把结构校验当作渲染安全或数学正确。
- 建议落点：项目可视化协议外层的固定renderer、严格JSON、来源／修订和离线资源校验。
  没有移入插件注册／远端renderer或自动模型调用权。
- fixture：协议往返、嵌套远端ref拒绝、schema错误类型、坏JSON语法、payload超限通过；宿主隔离／恶意SVG／iframe真实消息流 not_run。

## C12：Viewer小函数

- 来源：`web/components/visualize/VisualizationViewer.tsx`，SHA-256
  `db039deb92ff468912d818ae1640acc40efc30131655ed78b0614be6ef24686f`。
- `web/visualizationParsers.ts`原样保留`stripCodeFence`与`splitSvgBlocks`函数体，只新增export；
  `parseChartConfigStrict`保留去fence后JSON解析，删除上游jsonish宽松修复分支。
  无React／Next／Chart.js等直接或传递运行依赖；fixture使用本项目锁定TypeScript5.9.3转译。
- 完整Viewer依赖Next dynamic、原生React、主题/i18n、Chart.js、Mermaid、iframe bridge、MathAnimator和Geogebra，未迁入。
  自写SVG过滤器和触发问答的window事件不是经验证的安全闭包，不能原样作为隔离边界；不为小函数安装整个Next应用。
- 建议落点：前端可视化输入解析，具体本地renderer由项目维护。SVG正则拆分不是内容清洗，也不能证明嵌套SVG等任意XML正确。
- fixture：Unicode去fence、多SVG根、拒绝JS对象literal／尾逗号／NaN共3项通过；实际Viewer和离线交互 not_run。

## C13：研究队列纯逻辑及恢复缺口

- 来源：`deeptutor/agents/research/data_structures.py`，SHA-256
  `980e21a90e9c33b32d822b296ec4b4da6db6a7b2e07978dd3b32cb03cf64b46b`。
- `research_similarity.py`保留`DynamicTopicQueue._normalize_topic/_topic_tokens/_topic_similarity`及常量值／方法体，
  放入无状态owner类并增加alias；依赖只有标准库difflib/re。
- 不能原样部署整个队列的具体原因：使用naive `datetime.now()`；ToolTrace会截断raw_answer，且依赖
  宽松`utils.json_parser`／可选json_repair；`save_to_json`原地覆盖、`_auto_save`吞持久化错误；
  `from_dict/load_from_json`不保存恢复max_length/state_file，没有RESEARCHING恢复决策，也没有CAS或提交事务。
  有save函数不足以证明重启自动恢复。
- 建议落点：研究任务内的标题相似度参考；项目DB拥有队列、来源缺失待决定、UTC时间、独立attempt及检查点。
  标题相近不能合并来源身份，不同资料的同文也不能误去重。
- fixture：正规化、英语轻量词干、相似／空标题通过；中文主题召回质量、研究任务／重启恢复 not_run。

## C14：实际索引恢复、流恢复与现有实现

上游恢复文档之外，已核实际代码，原文件身份如下（详细Git blob见manifest）：

| 上游文件 | SHA-256 | 已核符号／事实 |
| --- | --- | --- |
| `services/parsing/cache.py` | `c1724bbe027a2c68f38c350fb11fc33df2088990c06ed3c5bb22bbb687a53c93` | `lookup/reserve/write_manifest/load_ir`；源hash与解析signature寻址，未完成目录移旁保留。 |
| `services/file_io.py` | `942e4ed3197ccc1e3417f72befcc6236a15eb049304f44b17a9fa4a59adad21f` | 同目录临时文件、flush/fsync、replace；Windows PermissionError有界退避，fsync失败仅警告。 |
| `knowledge/indexing_run.py` | `e49b9489fcec696ed961456f388cf7041481ec21933fe9576c41df2ec8c039e1` | `load_run/visible_run/IndexingRun.verify_sources/request_cancel`；租约丢失将未完源投为unknown。 |
| `services/rag/index_versioning.py` | `5d5ecd07716b7ea81f5483a3fa62f1024aed3f01fd707f7d0b99d40aff829eeb` | 新版本目录、ready判定与原子写metadata。 |
| `services/rag/pipelines/llamaindex/pipeline.py` | `9e08ec97aec324f7c2cfc7da652537b75d11d28c8d3779d6436fee459c9a435c` | `_publish_verified`先核持久index、再verify_sources、再publish。 |
| `services/rag/pipelines/llamaindex/storage.py` | `5b391d1b43ee5800cf318ab1c2f1f22db332c8ae46285532d514477eda37bf74` | `verify_persisted_index`重载、校验vectors，以已存vector检索probe。 |
| `web/features/chat/transport/TurnRuntimeClient.ts` | `1635d9006022e80a5701cf223b23f66cb1c02be838ae7e9734f5b82194d5cd25` | turn+lastSeq恢复、丢弃旧seq、gap缓冲并请求回放。 |
| `web/hooks/useChatAutoScroll.ts` | `89163d909011361f38e095026b9918b504601160601e4e0b5eb03ef42df84f22` | 用户滚动意图与Resize／Mutation／图片load跟随。 |

`file_io.py`为完整原文件原样复制；`parser_cache.py`仅把一次DT绝对导入改成相对`file_io`，形成纯标准库闭包。
其原 `source_hash_from_path`只取16位digest，不用作本项目来源版本；项目可向cache键传入已核的完整SHA-256。
`is_ready`只检查manifest的created_at，`load_ir`及图片路径也不是授权边界，需项目外层校验根目录、hex键、源／signature、
可用IR和图片路径。没有在这里重建业务任务真相源或引入LlamaIndex／模型供应商。

上游索引恢复并非只有文档设计，但完整pipeline仍耦合上游runtime根、embedding配置、KB目录、视觉资产、LlamaIndex和知识库管理。
本项目应在选定索引实现及自己的DB任务／版本事务上移植“候选验证后发布、源变化拒绝、未知不算完成”的控制点。
不能因为本次抽出cache／文件函数就宣称已证明索引中断恢复。

对现有本项目的静态对照：`service.py::reconcile`读取event_watermark后调用execution.reconcile；
`adapter.py::reconcile`通过Peer follow/page定位原native turn、rpcId和当前attempt，读已持久消息／终态；
`storage/repository.py`保存事件幂等身份与水位。故不搬DT的WebSocket client重建另一套聊天传输。
这次没有运行现有Harness断线／活动停止，也没有声称所有工具已经exactly-once。
Native Harness消息滚动仍由其正式制品管理，原scroll hook只归档对照，未替换宿主消息组件。

fixture验证了未完cache保留后重建、完成markdown复用，以及注入replace失败时上一版本文件完整保留。
这是确定性故障注入，不是系统断电、实际GPU索引、进程崩溃或Harness活动流恢复实测。

## 许可、依赖、最终文件与集成交接

全部本轮DT摘取／复制代码为Apache-2.0，原LICENSE原文保留，见
[`UPSTREAM.md`](../../../third_party/deeptutor/UPSTREAM.md)。没有根NOTICE；未采用树内其他带独立许可的图标／third-party动画资源。
已选源文件未发现额外逐文件版权通知。小包保留原注释，适配差异逐项记录，没有把上游代码冒称自主原创。

直接运行依赖为Pydantic2.11.7；完整协议可选jsonschema4.25.1。隔离验证的10个传递／直接运行发行包均已冻结在
`third_party/deeptutor/uv.lock`；它们的原许可原字节和SHA-256见
[`dependency-licenses/dependencies.json`](../../../third_party/deeptutor/dependency-licenses/dependencies.json)。
最终wheel实查包含根LICENSE／UPSTREAM、来源锁／manifest及11份依赖通知文件（10许可＋1清单）。
这不替代正式产品打包器对其所有依赖的通知归档。

核心源码文件（`src/deeptutor_reuse/`）最终为：

```text
__init__.py
source_digest.py
source_ref.py
eval_dataset.py
eval_metrics.py
learning_transactions.py
book_models.py
book_compiler_state.py
visualization_models.py
visualization_refs.py
research_similarity.py
file_io.py
parser_cache.py
visualization_protocol.py   # 可选jsonschema extra；基础包不主动导入
```

另有`web/visualizationParsers.ts`、测试／再提取脚本、原文件归档、根许可／通知、两个manifest和独立uv.lock。
实际wheel和sdist位于忽略缓存，正式运行通过项目path依赖构建包，禁止依赖`.local/cache`或`research`路径。

集成方已说明先前一次backend path依赖lock/sync成功；此后本分工增补了迁入模块、可选协议文件、
来源锁和随包通知。**需要集成方重新sync／重装该本地包**，以免仍运行前一刻build快照；
可按既有工程流程对`learning-deeptutor-reuse`执行reinstall并核对真实安装路径。固定包版本仍为0.1.0，
功能／输入权限未扩大，不要求重启或改动现有共享实例。

历史失败保留：首次独立脚本lint因测试有意在导入前选择源码／wheel路径而报E402；增加附原因的
单文件E402例外后复测passed。未对原样上游文件做整库格式化，也没有把skip覆盖成pass。
最终wheel SHA-256为`64fefa7e672a1fe152d40049bbb9d7cdef234c8e58c877daf7693305f8f793d6`；
若集成方重新构建，制品以其实际hash另记，不能用此独立包验证替代正式产品制品验收。
