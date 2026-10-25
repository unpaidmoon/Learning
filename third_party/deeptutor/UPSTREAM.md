# DeepTutor 来源与修改通知

本目录归档并提取 [HKUDS/DeepTutor](https://github.com/HKUDS/DeepTutor) 的固定提交
`a31d82624c27d75f4e771d4fcb78dce16470b383`，采用 Apache-2.0，原许可全文保存在
`LICENSE`。2026-10-23 从 GitHub connector 读取固定提交的原始 blob，按 Git blob SHA-1
复核后归档；内容 SHA-256、符号和修改清单见 `reuse-manifest.json`。

`upstream/` 中各文件保留原始字节、注释和通知；这些归档文件没有作为完整 DeepTutor
应用安装。固定提交的完整 Git 树返回 4,407 个条目且 `truncated=false`，不存在根 `NOTICE`，
根路径请求也返回 404。树内其他许可位于未提取的 provider-icons 和 thinking-orbs 资源目录；
未把这些资源并入本包。已选源文件未发现额外逐文件版权声明。

`src/deeptutor_reuse/` 为本项目独立分发包，不提供上游模型供应商、知识库授权、任意工具、
运行引擎或插件注册能力。逐文件取舍记录见 manifest：

- `book_models.py`、`eval_dataset.py`、`eval_metrics.py` 原样复制原模块，只有所在包名／文件名改变。
- `source_digest.py` 原样摘出 `source_pages.py::source_digest` 函数及其标准库导入，移除同文件
  的上游来源解析、路径授权和页渲染函数。本项目必须在调用摘要前核验路径权限。
- `source_ref.py`、`visualization_models.py`、`visualization_refs.py` 是未修改类／函数的摘录；
  `visualization_protocol.py`、`file_io.py` 原样复制完整原文件。
- `learning_transactions.py` 摘取事务对象，删除旧字符串选项的兼容解析器；
  `book_compiler_state.py` 保留原状态聚合方法，新增仅允许终态块的外部调用前置检查。
- `research_similarity.py` 保留正规化／分词／相似度方法及常量，删除队列和轨迹持久化依赖；
  `parser_cache.py` 仅将一次上游 `file_io` 导入改为本包导入。
- `web/visualizationParsers.ts` 保留两个无依赖解析函数，新增 export；Chart 解析去掉
  宽松修复分支，强制 `JSON.parse`。没有移入上游 SVG 过滤器或 iframe bridge。
- 2026-10-23 将已在离线候选验证的 `book_block_compiler.py`、`book_progress.py`、
  `book_export.py` 迁入实际包：原方法体保留，仅改模型导入；base 截取默认 registry 之前
  的完整前缀，不引入默认 AI 生成器。对应原文件另按 Git blob／SHA-256 核验后归档。
- `book_resume.py`、`learning_actions.py`、`offline_data.py` 是本项目新适配器，调用已保留
  的原模型与纯函数，不能标作上游原始调度或安全引擎。它们提供确定性确认／检查点迁移、
  跳过和解析事件、封闭的纯数据可视化校验，不提供权威队列、业务持久化或自动模型权。
  生成上下文复制原 Block，防止上游 generator 的原地修改污染未提交检查点；未知结果
  必须由应用对账。明细与实际字段见 `checks/integration-candidate/` 及其审计报告。
- `eval_matching.py` 完整原样保留固定提交 `services/rag/eval/matching.py`，只依赖标准库，
  供已有 dataset/metrics 使用；词面匹配不作数学等价、来源许可或全范围覆盖判断。
- `research_records.py` 保留原 TopicStatus、TopicBlock 及 9 个只读查询／统计方法；原方法
  的证据 DTO 改为 TraceReference，时间改为 UTC。不迁入 DynamicTopicQueue 的状态修改、
  append/auto-save/文件读写，不把截断 raw_answer 当完整证据。
- `source_windows.py`、`research_recovery.py` 是本项目新纯适配，提供有原偏移与身份的
  无损窗口、恢复待决定分类，不提供模型、检索器或新业务队列。
- C06 的 DeepStudent IR/Markdown/windowing 原件和相应 AGPL／文件级 MIT 通知另存于
  `checks/content-candidate/`。这些 TS 文件没有并入本 Python wheel，不改变原声明。
- 新增项目包的 `__init__.py`、构建配置、兼容验证和这些来源文件；这些不是上游文件。

模型通过校验不表示来源已授权；Book 模型的弱 `SourceAnchor` 不保存本项目所需的来源版本。
所有项目来源、课程／任务归属、修订、范围授权、作答与恢复必须由项目包装层处理。

核心运行依赖沿用项目已固定的 `pydantic==2.11.7`；可选完整协议校验固定
`jsonschema==4.25.1`，没有安装其格式校验 extras。独立验证环境的完整版本与制品 hash 由
`uv.lock` 冻结，对应运行依赖原许可归档在 `dependency-licenses/`。正式后端依赖锁由集成方维护。
