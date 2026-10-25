# DeepTutor 独立复用闭包

固定来源、逐文件 hash、修改差异见 [UPSTREAM](UPSTREAM.md) 和
[复用 manifest](reuse-manifest.json)。[调查与验证记录](../../research/reports/workbench/deeptutor-reuse-audit.md)
区分代码身份、独立兼容和产品集成状态。

包名 `learning-deeptutor-reuse==0.1.0`，Python `>=3.12,<3.13`，核心只要求
`pydantic==2.11.7`。生产代码应经项目包装访问这些模块；包不拥有课程授权、任务或模型执行权。

| 模块 | 可用范围与集成边界 |
| --- | --- |
| `source_digest` | 完整字节 SHA-256；路径必须先由项目授权，摘要不能代替 asset 身份。 |
| `source_ref` | 上游来源 DTO；项目需另存 asset／source_version／跨页和字幕定位。 |
| `eval_dataset`、`eval_metrics` | 原样检索数据集及纯指标；precision 分母为实际返回项数，失败不能混成0分，空集合不代表评测通过。 |
| `eval_matching` | 新迁入的原样词面匹配器，为原 metrics 提供每排名 gold index 集合；不能判断数学等价或来源权限。 |
| `source_windows` | 项目无损 codepoint 窗口与重组核验；保留 unit/version/内容修订/原始ms，不擅自插入分隔符或推算词级时间。 |
| `learning_transactions` | typed 问题、作答事务／事件对象；无数据库事务、用户改判或调度，旧字符串选项显式拒绝。 |
| `book_models` | 原样书／目录／章节／页面／块 IR；SourceAnchor 会忽略未知来源字段，强来源保存在项目包装层。 |
| `book_compiler_state` | 只接受 READY／ERROR 块的终态页聚合；生成中页与隐藏块先由项目处理。 |
| `book_block_compiler`、`book_progress`、`book_export` | 已迁入的原生成状态包装、阅读进度纯函数和 Markdown 文字投影；原方法体保留，模型导入改成本包。 |
| `book_resume` | 项目新确定性检查点 DTO／迁移；一次确认后逐块接收结果，完成章可读，未知结果先对账；没有队列、IO 或模型调用。 |
| `learning_actions` | 跳过／看解析只返回原 MasteryEvent，不追加 QuizAttempt、不授予掌握；目标授权、判分与事务由宿主拥有。 |
| `offline_data` | 固定二次函数参数图和本地资源的项目数据白名单；只用原 VisualizationEnvelope，不接收 iframe／任意代码／自动模型事件。 |
| `visualization_models`、`visualization_refs` | Pydantic 协议对象及外部 schema ref 检查；结构正确不等于安全或数学正确。 |
| `visualization_protocol` | 完整原模块；需要 `[visualization]` extra，依赖固定 `jsonschema==4.25.1`。 |
| `research_similarity` | 主题标题的正规化／相似度；不能据标题合并不同来源或替代持久队列恢复。 |
| `research_records`、`research_recovery` | 原只读主题查询/统计、UTC记录和证据版本引用；新恢复分类只返回待对账/待决定，不派发任务或持久化。 |
| `file_io`、`parser_cache` | 原子文件发布／内容寻址解析缓存；项目需校验根目录、键、来源、可用内容与图片路径，不直接暴露为模型工具。 |

`web/visualizationParsers.ts` 是独立 TypeScript 小函数，无 React／Next／图表运行依赖。
SVG 拆分不会清洗内容；实际渲染与离线资源安全由项目承担。

以下命令已在仓库根目录执行成功，生成物放忽略的 `.local/cache/reuse/deeptutor/`：

```powershell
& backend/.venv/Scripts/python.exe third_party/deeptutor/tests/test_reuse.py
uv build third_party/deeptutor --wheel --out-dir .local/cache/reuse/deeptutor/dist --python backend/.venv/Scripts/python.exe
uv lock --project third_party/deeptutor
node --test third_party/deeptutor/tests/visualization_parsers.test.mjs
```

Python 最终项目环境结果为 16 项中 15 通过、1 跳过可选 jsonschema；锁定独立环境安装实际
wheel 与可选依赖后，`--installed` 验证 16 项全部通过。TypeScript fixture 3 项通过。
这些不代表来源授权、书生成、索引引擎、Harness、离线交互包或13场景已验收。

后续 C09～C13 的实际调用示例、恢复／自由跳过／离线边界见
[集成候选](checks/integration-candidate/README.md)；原 BookPage／LearningBlock 名称分别是
`deeptutor_reuse.book_models.Page`／`Block` 的调用方别名。模块名不是发行包名
`learning-deeptutor-reuse`，更新包后集成方需要显式重新安装本地 path dependency。

C06 实属 DeepStudent，C08 实属 DeepTutor；本轮原样 IR/Markdown、长字幕窗口、词面评测和研究恢复
证据见 [内容候选](checks/content-candidate/README.md)。DeepStudent 的 TS 原件／通知留在独立 checks，
未混入本 Apache-2.0 Python wheel；不引入其视频帧流程或另一套执行层。
