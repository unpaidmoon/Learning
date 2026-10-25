# C05 作文轮次、解析与输入校验候选实证

2026-10-23。固定 DeepStudent `635216503c0b34bdf812e17942e0f09f51872e6f`。按已应用的 REA 技能对完整源码走常规分析，以真实 Node 子进程验证独立制品；未调用模型、未读用户素材、未改正式 backend/frontend/runtime。

结果是一个可迁入的单次解析 helper，保留原 TS 纯函数并增加边界 guard。它不执行批改模型，也不拥有稿件、轮次或评价数据库。主代理负责以 Artifact revisions 和正式任务接入，候选通过不等于产品验收。

## 原代码复用及边界

全部来源路径相对于固定 DeepStudent 仓库。原始文件逐字归档于 [essay-candidate/upstream](../../../third_party/deepstudent/checks/essay-candidate/upstream/)，[source-manifest.json](../../../third_party/deepstudent/checks/essay-candidate/source-manifest.json)记录 7 个文件的 SHA-256 与提交 Git blob。

| 来源文件与原函数 | 实际处理 | 取舍 |
| --- | --- | --- |
| `src/essay-grading/streamingMarkerParser.ts:71,87,515,673`：`decodeXmlEntities/extractAttributeValue/parseScoreFromText/parseStreamingContent` | score 与维度、实体、未闭合标记检测。原函数不改；流式解析器的清理文本不用于原稿或锚点。 | R 原码 + A 严格结果 guard。 |
| `src/essay-grading/markerParser.ts:99`：`parseMarkers` | 解析 del/ins/replace/note/good/err；其唯一运行时 import 是同目录 streaming parser。 | R。其容错及丢弃纯空白 text 的行为不能当成实际原稿。 |
| `src/essay-grading/suggestionAnchors.ts:98,142,162,181`：`findAnchoredIndex/applyAnchoredReplacement/markerOriginalText/buildSuggestionChange` | 原函数参与批注引用与上下文构造、查找；replacement 原函数用于独立修订 proposal fixture。 | R + A 重复目标唯一匹配、codepoint 边界、冻结稿 hash。 |
| `src/essay-grading/textStats.ts:38`：`calculateEssayTextStats` | 对实际未 trim 稿件统计；`for...of` 按 Unicode scalar 计数。 | R；不声称其具备 Rust 版本的句子统计。 |
| `src/essay-grading/types.ts/markerTypes.ts` | 原类型依赖；转译后类型 import 消失。 | R。 |
| `src/essay-grading/essayContentState.ts:28,51,74`：dirty/graded snapshot、`evaluateRoundSwitch` | 原 UI 切轮 guard 和完整配置 fingerprint fixture。当前 stdio 不把它们挂为空业务调用。 | R 候选；正式 Artifact 事务由项目实现。 |

运行时 import 闭包只有本地 CJS。构建用现有 Node `24.14.0`、TypeScript `5.9.3`，没有新增 npm 包、另一份 React、Tauri/VFS、LLMManager 或供应商 SDK。

`essay_grading_executor.rs:227` 对正文先 trim，`:302-330` 从 VFS 动态找 latest+1 及上一稿，随后进入独立评分 pipeline。这些不是可直接当本项目版本事务使用的纯函数。文件 SHA-256 `e707994e0a7c28733b8e774a37fd25160f21e3dd8d3307d4ffe43ec6e5ef6f04`。

`essay_grading/pipeline.rs:619-851` 的 Rust score 解析可提取，但会取最后 score、clamp、忽略无效维度并可能校正总分；同一固定仓库已有前端零第三方运行依赖的统一 score parser，故本次直接保留 TS 原函数，不额外引入 regex Rust helper。TS 取首 score 的差异通过严格只接受一个完整 score 消除。Rust pipeline SHA-256 `f3471b7bbc057f3b243b5c6b6016c7857a3b9387cd38b538906baead78bcd71f`。其 provider 重试、图片读取、VFS 保存及 Incomplete 含 score 结束标签即补救完成的语义均未迁入。

## 可迁入协议与制品

[README](../../../third_party/deepstudent/checks/essay-candidate/README.md)提供完整协议和迁入说明，[request.json](../../../third_party/deepstudent/checks/essay-candidate/examples/request.json)／[response.json](../../../third_party/deepstudent/checks/essay-candidate/examples/response.json)是真实 stdin/stdout fixture。

```text
<fixed-node.exe> <formal-root>/helper.cjs --compiled-root <formal-root>/dist
```

单次 UTF-8 JSON + EOF；单次 JSON stdout + LF；两边各限 2 MiB；exit `0` 为合法结果、`2` 为输入／协议限制、`3` 为部署／启动失败。只暴露 `validate_feedback`。参数由项目固定，不从 JSON 读取文件、URL、路径、模型或数据库。

输入 `payload.draft={draft_id,revision_id,content_hash,content_role,text}`，`feedback={draft_revision_id,draft_content_hash,body,annotated_text,host_terminal,cancel_requested,origin?}`，`rubric={max_total,aggregation,dimensions}`。稿件对象只接受列出的五字段；原始 UTF-8 SHA-256 必须吻合。正文非空且最多 50000 codepoint，拒绝孤立 surrogate，保留空白、CRLF、emoji 和组合字符；不 trim、NFC 或截取。角色保留 original/user_note/ai_generated/mixed，评价固定 ai_reference。

`host_terminal/cancel_requested` 必须由正式 Host/任务填入。只有 completed 且明确未取消、身份吻合、分数/rubric 与锚点检查无异常，才输出 `reference_ready` 和 typed `reference_evaluation`。停止、未知、半截标记、分数前缀／clamp／非有限值／多 score／缺维度／总分不符均输出 `pending`，原始反馈和 parser candidate 保留；旧稿身份不符输出 `stale_reference`，没有可信选区。参考评价始终不等同于用户自评或确认的客观正确性。

批注先用原 parser／anchor helper，再要求 actual 稿件中的唯一精确引用，重复目标须唯一上下文，最多考察 200 次出现；原 UTF-16 索引仅作诊断，输出 start/end 转为 codepoint 半开区间，检查不会切开 surrogate。XML 元素内容解码一次，原属性 extractor 已解码的值不再二次解码。原稿 `前🙂 bad 后 bad 末` 的第二个 bad 实测为 `[9,12)`，原 UTF-16 索引为 10。无法定位的批注单独诊断，不扩大或猜测选区。

独立包已准备于 `.local/cache/workbench/ds-essay-candidate/standalone/`。正式运行迁入全部 15 个固定文件并核 hash，不引用调查缓存，不需要构建工具。7 个源文件、7 个 dist JS、build manifest、3 份原许可／通知和 1 份适配通知的确切 hash 在 [deployment-manifest.json](../../../third_party/deepstudent/checks/essay-candidate/deployment-manifest.json)。Node 二进制复用项目已固定的 24.14 运行时，本清单记录本次实测 exe hash，没有另行分发 Node。

| 文件 | SHA-256 |
| --- | --- |
| `deployment-manifest.json` | `ff8c36ccb9b69d679f375b28db8fae3b098a49eacc90c62732d3b940c07fc57b` |
| `helper.cjs` | `55247e6335da6f0d5833ab113f9f7e24f67d4156dee932d51a92bd859a266021` |
| `candidate.cjs` | `38e2cde76bc4a90644fe4ef4e8456a2f2e44cf4a48b5780db817987b8af64d27` |

许可证原文保留在 `dist/notices`；上游根为 GNU AGPLv3，原第三方许可说明声明 AGPL-3.0-or-later。[适配通知](../../../third_party/deepstudent/checks/essay-candidate/ADAPTATION-NOTICE.txt)记录新增 wrapper 与原样源文件身份。完整上游 notice 的 2120 项组件清单是其应用分发范围，不能误记为本候选运行依赖。

## 轮次与事务实证

候选内存函数仅提供确定性负例，不是正式业务 DB，也不作为 stdio operation 暴露。两稿各自冻结 revision/hash；第二稿存在时，第一稿迟到参考仍归第一轮、不变更当前稿。AI 原判保留，用户自评与改判追加独立记录；重放同 operation 返回既有结果，换 payload 拒绝，CAS 冲突保留 proposal。修订 proposal 使用原替换函数，要求用户动作、预期版本和新 revision，原稿不变、新稿标 mixed。模型／思考变化进入配置 fingerprint。

正式实现必须继续由项目提供：任务冻结稿和 rubric/模型/思考配置；只读接收同一次 Harness 最终原串及真实终态；feedback Artifact 保存与稿件绑定；append-only 自评／改判、operation hash 和事务 CAS；停止／恢复、迟到回调及并发手动编辑 admission；采纳修改创建新 Artifact revision 与撤销。不把原 latest+1、切轮 UI guard 或候选内存数组当成这些事务的替代。

## 实际验证与未覆盖

仓库根执行：

```powershell
node third_party/deepstudent/checks/essay-candidate/verify.cjs
```

已通过严格 TS 检查、11 组测试及 12 个真实独立进程用例：原文件 hash/局部 import、raw 稿件 hash/Unicode/限长、重复与实体锚点、分数异常、实际终态、两稿两轮、自评／改判、幂等／CAS、未知／取消／旧稿、非法 UTF-8、双 JSON、超大 stdin、未知字段／operation、部署参数失败。standalone cwd 只含复制后的运行文件，NODE_PATH/NODE_OPTIONS 清空；运行前后包清单和文件 hash 不变。记录在 `.local/cache/workbench/ds-essay-candidate/{validation,standalone-fixture,manifest}.json`，实际模型调用 0。

新增文档后实际执行 `.\backend\.venv\Scripts\python.exe scripts/check_docs.py`，结果 `182 files, 0 errors`。该检查仅验证 Markdown 导航。

历史失败保留于 validation：早期 fixture 有一个多余括号，修正测试语法；另一测试误以为 Infinity 在 max60 时仍非有限，实测原 clamp 得到 60，现分别验证 raw-invalid 与 Infinity max 的 JSON-safe 诊断。没有为消除失败修改原算法。

未覆盖：正式 Artifact/Task 数据库与迁移、Host 活动取消／恢复、真实模型能否稳定遵循 score/marker 协议、产品批注交互与采纳／撤销、真实用户稿件和图片输入、S07 及全部 13 场景验收。该包为文本稿候选，不自动摄入图片、字幕、视频或屏幕，也不增加模型调用／补救重试。
