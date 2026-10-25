# C10／C12 独立离线产物候选与实际复用证据

日期：2026-10-23。固定DeepTutor提交：
[`a31d82624c27d75f4e771d4fcb78dce16470b383`](https://github.com/HKUDS/DeepTutor/tree/a31d82624c27d75f4e771d4fcb78dce16470b383)。
完整源码按REA的源码路由核查；包身份、实际本地浏览器和源码结论分别记录，没有启动或安装整套DeepTutor。
本分工只写本目录，没有修改backend／frontend／desktop产品代码或既有resources锁。

## 入口与实际制品

- [阅读入口](package/index.html)：原包完整内容。
- [独立zip](artifacts/learning-book-candidate.zip)：988,614字节。
- zip SHA-256：`73b6da21f85515befb07bbfdb0a3f0d15cd856a1980b85b2ec5f50d95a2e82fe`。
- [资源清单](package/asset-manifest.json) SHA-256：`c8a1b9eeb0bbc1f62617f6de5e00b000b6fcb72ad822205bb158327c2db1c06a`。
- 实际测试入口是zip独立解包后的`verification/unpacked-r1/index.html`，全部75个文件核hash通过。
- [首次阅读截图](offline-reading.png)、[参数交互截图](evidence/offline-interaction.png)、[自测／参考答案截图](evidence/offline-selftest.png)。

浏览器为Windows Chrome155.0.8059.81，自动化CLI固定为Playwright0.1.22，复用本地已装工具，未用未固定latest。
新建独立浏览器profile，context的offline=true，HTTP(S)路由拒绝，service worker禁用，入口为本地file协议；
本包未启动或调用应用后台。没有关闭全系统网卡，也没有终止其他现用实例。
验证后已关闭本次独立浏览器session。测试profile与编译缓存由本目录gitignore排除。

## 确实迁入和调用的上游代码

[reuse-ledger.json](reuse-ledger.json)记录17个原文件的Git blob／完整SHA-256、实际目标文件hash与修改粒度。
所有原文件按固定提交的blob验原字节，并归档在`upstream/`。实际执行结果如下：

| 原代码／符号 | 实际保留与适配 | 依赖与本次证据 |
| --- | --- | --- |
| `book/models.py`：Page、Block、Book、Spine、Chapter | 完整原模型原样使用；Page／Block在fixture中以BookPage／LearningBlock别名导入。加载文件与固定原文件逐字节相等。 | 标准库＋Pydantic2.11.7。实际构造、编译并序列化6块／2页，重新model_validate_json通过。 |
| `book/blocks/base.py`：BlockContext、BlockGenerator.generate、失败分类／metadata | 提取默认registry之前的原前缀，只把模型导入改到隔离包；方法体保留。 | 标准库＋原模型。合成纯数据generator实际进入GENERATING，成功到READY，失败到ERROR，CancelledError传播。 |
| `book/compiler.py::_finalize_page_status` | 调用此前已迁入的原方法及终态前置wrapper。 | 六个块都完成后，两页状态实际聚合为ready；未把运行中块归为失败。 |
| `book/export.py::render_block/render_book_markdown` | 完整原文件只改一处模型导入。 | 实际生成随包`book.md`，涵盖文字、自测、卡片和视觉块文字投影。 |
| `book/progress.py::record_attempt/recompute_score/recompute_weak_chapters` | 完整原文件只改模型导入；本次用于组件验证。 | 原QuizAttempt模型的None判分实际保持score0、weak_chapters为空。该Python进度模块不在浏览器里维护正式记录。 |
| `web/lib/quiz-question-type.ts` | 完整原文件未修改，TypeScript5.9.3只去类型、编为classic-script exports包装。 | 无运行依赖。真实浏览器中multiple_choice正规化为choice；正确答案文字“4”由原resolveChoiceAnswerKey映射为B，核对结果正确。 |
| `web/lib/book-reader-navigation.ts` | 完整原文件未修改，仅转译与exports包装。 | 无运行依赖。实际“下一屏”动作令阅读区scrollTop=628，阅读位置显示90%，使用原screenful和percent函数。 |
| `visualizers/protocol.py`：RendererRef、VisualizationEnvelope等 | 使用已有原类摘录，实际构造／序列化原协议对象。 | Pydantic-only。renderer固定为本地quadratic_tangent；payload为有界数值数据。 |
| `VisualizationViewer.tsx::stripCodeFence`及现有严格JSON适配 | 去fence原函数通过此前候选适配模块保留；浏览器参数图解码实际使用parseChartConfigStrict，不保留jsonish修复。 | 无运行依赖。只JSON.parse，不执行表达式。splitSvgBlocks虽随原小模块保留，本包未调用，不把它当SVG安全边界。 |

原Book模型SHA-256为`6726eea4664514a36fda7936adbfef7ac5081603f37695cb860b25a97c194bc3`。
原BlockGenerator base文件为`c21a9f3beb83002eb073282c0e525c907c6f183a586e57b6da4cf958cd3ad373`；
原Markdown export为`b185f72d4dc166ae5e6b90a0033e98a4f7bba3b372e840c1b70bb06e2ddcc70b`。
浏览器保留模块bundle SHA-256为`2bbcbd4348c832a5f3d2a5a285a5cdf9c1e17d84e83fa7b1de7df6da9b0c6433`。
其余逐文件hash以机器账为准。

有界payload schema、合成数据generator、DOM／SVG阅读器、资源收集与ZIP打包为本项目新候选代码，
没有把这些新代码冒称上游完整renderer或原生成引擎。上游模型校验与原方法调用证据见
[python-model-compile.json](evidence/python-model-compile.json)；原函数转译来源见
[browser-module-build.json](evidence/browser-module-build.json)。

## 不能直接搬入的具体部分

| 原模块 | 无法作为本候选直接运行的原因／处理 |
| --- | --- |
| 完整BookCompiler／默认BlockGeneratorRegistry | 依赖BookStorage、SectionArchitect、生成器注册、流事件、并发与重试。默认registry会拉起各AI生成器。只保留纯状态／包装代码，实际模型、任务、来源与重试归应用。 |
| QuizGenerator | `_generate`懒加载QuestionPipeline、UnifiedContext和Book事件总线，不是离线纯数据编译。`_extract_questions`虽可单独提取，但映射的是该pipeline的特定summary结构，本次没有相应运行输入，未冒称迁入使用。 |
| InteractiveGenerator | 读取keypool、LLM配置、VisualizePipeline，产出HTML模式等任意代码；与本次有界纯数据／无自动模型要求不符。没有安装另一套模型执行层。 |
| BlockRenderer.tsx | 依赖React／i18n／MarkdownRenderer／Tooltip及多个块组件，带编辑、删除、移动、重生成等宿主回调。未引入Next应用和整套编辑底座。 |
| QuizBlock.tsx | UI依赖React、lucide、i18n和MarkdownRenderer，作答／补充练习走宿主回调。原纯题型／答案键函数可独立保留，UI另以本地DOM实现。 |
| quiz-judge.ts | 实际创建WebSocket访问`/ws/questions/judge`并处理AI判分流，不是纯判分工具；未打入离线包。 |
| FigureBlock／InteractiveBlock／VisualizationViewer | FigureBlock构造svg／chartjs／mermaid代码结果；InteractiveBlock构造text/html、prompt／resize事件；Viewer还有Next动态加载、Chart.js、Mermaid、Geogebra、MathAnimator与iframe bridge。只采用原协议／小函数，固定数值函数使用DOM/SVG。 |
| 上游SVG过滤与HTML iframe | 未被本次验证为通用安全边界。候选不接收任意HTML／JS／原始模型SVG字符串，未复制这些过滤器或桥。 |
| 原Markdown export单文件 | 会将交互／动画降为文字描述／指针，没有本地脚本字体打包能力，也不是HTML清洗器。保留其文字投影，完整离线功能由实际HTML包承载。 |

原SourceAnchor仍缺项目强来源版本与内容角色；本候选在块metadata及source_catalog保存合成来源版本和角色，
这没有替代正式后端的课程授权、asset/source_version、来源链与范围guard。

## 独立包的范围与安全方式

包含两章、6种块：文字／公式、合成SVG图、本地数值参数图、选择题和主观参考题、折叠卡片、提示。
所有题文、图片、来源和预期均为本次合成，未使用用户视频、屏幕、音频、私有文档或业务库。
来源快照SHA-256为`9f77be4dca78192717bba349ff6d1906d612c9cb0b2bba6a84e6fcd395764f69`，
包内原始合成材料实际能通过本地来源链接打开。

KaTeX0.16.22的min脚本、CSS及60个本地字体文件原样复制自项目固定node_modules，并保留原MIT许可。
DeepTutor Apache-2.0原文及修改通知也随包。资源清单逐一保存全部文件大小和SHA-256，没有CDN资源。
浏览器使用classic scripts和内嵌JSON快照，不需要file协议下的模块／JSON fetch，更不需要启动HTTP server。

新renderer只构造DOM／SVG，函数为固定`y=ax²`与其切线；数值有界，未包含eval、Function构造器、fetch、
WebSocket、postMessage、localStorage或IndexedDB调用。KaTeX显式trust=false并限扩展数量；
CSP限制connect-src none、无unsafe-eval、无iframe／object、无表单外发。图片路径限定包内assets，
JSON嵌入时转义`<`以保留数据边界。外链图片、路径遍历、危险TeX链接命令及额外代码表达式在构建fixture中被拒绝。
这些是已列固定输入与有界渲染策略的证据，不宣称审计了任意第三方脚本或任意SVG输入。

## 真实浏览器验证

实际以Playwright CLI操作Chrome、读取DOM和网络事件；观察／截图不是mock输出。
测试代码通过Playwright检查状态，不进入导出包。原始声明动作、快照、trace和汇总分别见
[browser-actions-final.json](evidence/browser-actions-final.json)、[browser-verification.json](evidence/browser-verification.json)
及`evidence/browser/traces/`。

| 检查 | 结果与实际观察 |
| --- | --- |
| 独立zip／解包 | passed，75文件hash一致；浏览器操作后再次核验全部文件未变化。 |
| 断网／无应用后台 | passed（本包范围）。navigator.onLine=false；所有观察请求为file协议；HTTP(S)／WebSocket为0，没有应用后台请求。 |
| 公式／图片／字体 | passed，5处KaTeX、0个math error，本地SVG naturalWidth=640；两种实际加载woff2均来自解包目录，document.fonts.status=loaded。 |
| 参数交互 | passed，真实range指针／键盘操作令a=2、x₀=2，得到切点(2,8)、斜率8；切换章节返回仍保留该参数。 |
| 选择题 | passed，选A得到incorrect和重试提示，改选B后correct；原答案键函数返回B。 |
| 主观参考 | passed，查看答案后仍ungraded，未记错或掌握；原Progress组件验证None不降低分数／不加入弱章节。 |
| 卡片／来源 | passed，展开卡片显示答案；来源弹窗显示版本和原文，本地原文链接在新tab断网可读。 |
| 无记录回传 | passed（已观察流程）。WebSocket、Storage写和IDB open观测为0；刷新后参数恢复初值，文件hash未变化。 |
| 浏览器错误 | 正常流程0 page error、0 console error、0 CSP violation、0失败资源请求。 |
| 静态／脚本 | 自写Python构建／打包／校验脚本ruff通过；应用JS接口禁用项静态检查通过。 |

汇总见 [final-verification.json](evidence/final-verification.json)。这些通过项不包含产品导出API、业务数据或用户学习效果。

## 限制与后续集成

- 当前只有一个固定参数图类型，没有任意函数、动画、Geogebra、Mermaid或任意HTML能力；相关S09／S12完整场景仍需主任务完成。
- 自测只供离线阅读核对，不产生正式作答／评价／FSRS／掌握记录，不提供回传。跨章保留参数和选项；选择题结果提示需重新核对，主观参考／卡片展开不跨章保存。
- 仅测Chrome155桌面；Firefox、Safari、移动端、无障碍完整审查、长书和大型媒体仍not_run。
- 尚未测正式成果编辑／修订／冲突／保存／索引、导出API、权限负例、真实Harness生成和用户实际学习体验。
- 原BlockGenerator取消会传播异常，但原Block仍留在generating；正式任务层必须持久化取消／未知状态，不能把该原包装当完整恢复引擎。
- 上游模型的payload宽字段不作安全边界；正式导出必须继续使用项目受限payload／来源／资源校验。全书完整覆盖与缺口暂停由主任务拥有。

历史过程保留：初次脚本lint发现未用导入／紧凑语句，修正自写脚本后通过，没有格式化原上游文件；
CLI初次默认拒绝file协议，按本次明确file测试任务仅在独立session配置所需开关，未改全局客户端；
自测快照解析器最初未处理`[active]`标记，修正解析并重新取当前refs后继续，原记录保留于browser-verification的harness_failures。
所有候选验证均不改变13场景统一交付状态。
