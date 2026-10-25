# DeepTutor 源码借鉴分析（2026-10-22）

本报告只做公开源码与一方文档分析，不启动开发或改变分期。结合本项目最新 [PDF／字幕／知识库需求收口](../../docs/product/PDF阅读与字幕及课程知识库需求收口-2026-10-22.md)，其确认状态优先于旧研究。已有知识库专题见[课程知识库定位与设计建议](课程知识库定位与设计建议-2026-10-22.md)。

## 1. 证据与版本

- 目标为 [HKUDS/DeepTutor](https://github.com/HKUDS/DeepTutor)，通过 GitHub connector 读取仓库树和选定文件。
- 本次核实默认分支 main 提交为 [a31d82624c27d75f4e771d4fcb78dce16470b383](https://github.com/HKUDS/DeepTutor/commit/a31d82624c27d75f4e771d4fcb78dce16470b383)，提交时间 2026-10-18T20:12:56Z、提交说明为 release v1.6.15。以下源码链接固定于该提交。
- 这是源码静态核查，不是安装、运行、浏览器实测或真实模型调用。按 reverse-engineer-anything 技能的路由规则，完整源码分析无需调用 REA 引擎，也未安装或修复 REA。
- 表内“源码事实”只说明代码存在此分支；性能、跨端可靠性和学习效果没有运行证据。恢复文档属于一方设计说明，不冒充已独立验证。
- 仓库根 [LICENSE](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/LICENSE#L1-L4) 是 Apache License 2.0。本轮仅参考设计；没有复制业务实现或引入依赖。

## 2. 首先纠正对项目形态的理解

当前 DeepTutor 已不能简单概括成“多个解题 agent 串联”。普通 Chat 使用一个 exploring agent loop；deep_solve 复用同一个 Chat loop，再增加计划、步骤完成与重规划工具。源码明确说“不再有独立 pipeline”，其 SolveSession 是内存状态，不能因此宣称具有跨进程的解题步骤恢复。见 [Chat 绑定](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/agents/chat/agentic_pipeline.py#L1-L12)和 [DeepSolveCapability](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/capabilities/solve/capability.py#L1-L16)。

但“单代理”也不代表和本项目的按需检索相同：其 loop 在模型开始前调用知识库 seed；对已附加的传统 RAG 库，用当前问题并行检索，最多三个库、每库结果最多 4000 字符；PageIndex、专属能力知识库和特定 skip hook 另行处理。见 [loop 启动](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/agents/loop/agent_loop.py#L335-L358)和 [seed 实现](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/agents/loop/pipeline.py#L1532-L1607)。不能据此建议本项目也每轮先搜资料。

## 3. 七项值得借鉴的具体做法

| 做法 | 一手证据与类型 | 对本项目的价值及适用边界 |
| --- | --- | --- |
| 单次引用与长期会话上下文分开 | README 将 capability、课程、模型等称为 sticky context，把文件、阅读片段等区分为 one-time references；这是[产品说明](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/README.md#L701-L711)，本次未逐条追踪全部实现。 | 值得作为输入区信息层次的参照：当前课程与模型留在会话，所选 PDF、截图与精确引用在草稿里可检查。实际历史保留仍按本项目快照和上下文规则，不将“一次引用”误解为历史资产可删除。 |
| 引用有源身份、版本、页序及局部区域 | SourceRef 包含 kb_ref、document_key、source_digest、page_index、page_label、parser_signature、node_id、crop；解析来源时核访问与 SHA-256，来源改变会报错。属于[源码事实](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/services/rag/source_pages.py#L24-L54)。 | 非常贴合本项目：引用不能只有文件名与摘录；文档检索和用户选区共用稳定定位，并保留已发送内容快照。本模块未证明本项目所需的全部旧版保留与文件移动行为，仍需自有资源身份管理。 |
| 解析产物可复用，索引候选验证后发布 | 一方[索引恢复设计](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/docs/INDEXING_RECOVERY.md#L16-L37)说明缓存绑定来源字节与解析签名，embedding 改动可复用解析；新版本加载、向量核验和检索探针通过后才原子发布，失败保留旧版。此次为文档依据。 | 可借鉴到 PDF 阅读 OCR 与知识库复用、持久检查点、源变化拒绝旧结果发布。不要照搬同步引擎的长时间 pending cancel；前台优先与取消延迟需要在项目固定运行组合另测。 |
| 用真实检索路径评测原段召回 | eval runner 走与聊天相同的 rag_search → RAGService → 实际 pipeline，使用原文 gold passages，检索失败单列且不计入均值。属于[源码事实](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/services/rag/eval/runner.py#L1-L12)。 | 建一小组真实学习查询与正确原段，比比较向量数、凭“装好了某框架”验收更有价值。召回评价与答案正确性分开；词面匹配不能证明数学等价、图表理解或整章覆盖。 |
| 断线后按 turn + event seq 接回原生成 | 前端连接恢复后发 resume_turn(turnId, afterSeq)，丢弃旧 seq、缓冲缺口并请求回放；后端从持久事件补齐。属于[前端源码](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/web/features/chat/transport/TurnRuntimeClient.ts#L295-L386)与[后端源码](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/services/session/turns/lifecycle.py#L386-L457)事实。 | 用来审查本项目已有对账机制：关闭/重开界面、换课程、丢事件后继续查看原请求，不能重发模型问题来冒充恢复。这里只证明回放设计，不证明所有工具与模型调用具有 exactly-once 语义。 |
| 完成、取消与已显示内容一起收口 | 正常路径在 DONE 可见前提交事件和终态，并带持久消息 id；取消针对当前 task，等待 finally，尽力保留已生成正文及附件。见[完成路径](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/services/session/turns/executor.py#L1188-L1221)、[取消入口](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/services/session/turns/lifecycle.py#L305-L323)、[部分结果保存](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/services/session/turns/executor.py#L1255-L1347)，属于源码事实。 | 适合作为 Harness 适配与自有业务存储的检查项：停止不清空已看到的解答，完成事件能对应正式消息。其 asyncio task.cancel 不能直接证明 Harness 生成已经停止；本项目仍需自己的取消确认和用量未知规则。 |
| 滚动跟随服从阅读意图，处理晚到的布局变化 | [useChatAutoScroll](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/web/hooks/useChatAutoScroll.ts#L14-L110) 观察内容 ResizeObserver／MutationObserver／图片 load；用户向上滚动、触控或翻页键释放贴底，避免靠固定“流结束后延迟”处理。属于源码事实。 | 对长公式、截图与折叠内容很实用，可作为实际阅读体验的验证场景。现有 Harness 原生滚动已有实现时先对照缺口，不为借鉴而整体替换原生消息组件。 |

上述七项中，来源身份、可恢复索引、检索评测最适合未来 PDF／知识库工程验证；事件回放、取消与滚动则优先作为已有实现的检查清单，不自动视为缺失。

## 4. 明确不宜照搬的部分

1. **默认强制计划与逐步 gate。** deep_solve 中文提示要求任何求解前先 solve_plan、每步 solve_finish_step、卡住再 solve_replan；见[原提示](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/capabilities/solve/prompts/zh/system.md#L1-L13)。这是可选重模式，不适合本项目默认完整解答、速度优先的日常提问。不能从静态源码断言它一定慢多少，性能未测。
2. **每轮附加 RAG seed。** 前述启动路径会在常规知识库附加时做预检索；本项目已确认使用 Harness skill 原生按需触发，不增加独立规划、分类或每轮预检索。
3. **按题号自动找原题。** LlamaIndex search 先检查 requested_exercises，命中时进入结构化找题，再回退普通检索；见[源码](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/services/rag/pipelines/llamaindex/pipeline.py#L278-L294)。这是与本项目明确取消项冲突的行为。
4. **图像统一先由另一视觉模型转文字。** Reading 的 view_figure 解析一个已存文档图像，再调用视觉模型返回文字；见[工具说明](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/capabilities/reading/figure_view.py#L31-L50)及[调用](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/deeptutor/capabilities/reading/figure_view.py#L132-L169)。可借鉴“按定位取单图”，不自动引入额外串行视觉模型；本项目已确认图文检索应围绕授权文档区域，并完全隔离视频补帧。
5. **整个学习平台和运行底座。** README 展示 Book、Mastery、Quiz、Research、Partners、多检索引擎等大量入口；见[能力说明](https://github.com/HKUDS/DeepTutor/blob/a31d82624c27d75f4e771d4fcb78dce16470b383/README.md#L701-L728)。这些是其产品范围，不是本项目新增需求。本项目继续 Harness 主线与课程／资料／AI 三栏，不因此建立自有通用 agent 平台、题库、笔记或多代理默认流程。

## 5. 本轮已回答和未覆盖

| 调查问题 | 状态 | 边界 |
| --- | --- | --- |
| 当前 solve 是否仍是独立多代理流水线 | 已回答 | 当前固定源码表明是同一 Chat loop 加工具化步骤状态；没有推断其他能力完全不使用多代理。 |
| 资料来源、版本、图文读取有哪些可借鉴点 | 已回答 | 已核 SourceRef 与 view_figure；未审计每个解析器的权限、裁剪精度和所有引用 UI。 |
| 聊天断线、取消、持久化设计 | 部分回答 | 已核核心前后端路径；未全仓审计幂等命令、租约、工具进程停止、外部供应商取消与 usage 汇总。 |
| 前端阅读体验 | 部分回答 | 核了滚动实现与产品上下文层次；未运行页面或验证可访问性、性能、触屏和复杂公式。 |
| 许可证 | 已核根文件 | 未做依赖与逐文件许可审计；当前没有代码移植。 |
| 本项目学习效果、响应时间、可部署性 | 未验证 | 未安装 DeepTutor、未调用模型、未跑其测试或真实学习流程。 |

结论是选择模块边界与验证方法借鉴：优先来源回链、可恢复处理和真实检索评测；保持本项目既定输入边界、默认回答路径和 Harness 运行层。具体实施仍待对应阶段授权。

