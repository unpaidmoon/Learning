# C05 作文解析候选

固定 DeepStudent 提交 `635216503c0b34bdf812e17942e0f09f51872e6f` 的 7 个原始 TypeScript 文件，逐字保留于 [upstream](upstream/)。[来源清单](source-manifest.json)记录文件 SHA-256、Git blob 与修改状态。[调查报告](../../../../research/reports/workbench/ds-essay-candidate.md)说明实际复用和未迁入部分。

运行只需要固定 Node `24.14.0`、`helper.cjs`、`candidate.cjs`、`dist/` 的 7 个 JS、`package.json`、`build-manifest.json` 和 4 份通知，共 15 个文件。没有运行时 npm 包。TypeScript `5.9.3` 仅构建使用；正式使用不要求用户安装开发工具。

## 构建与验证

在仓库根目录，使用已固定的项目 Node：

```powershell
node third_party/deepstudent/checks/essay-candidate/verify.cjs
```

实际验证通过：严格 TS 检查、11 组测试、12 个真实独立 Node 进程用例。证据在 `.local/cache/workbench/ds-essay-candidate/{validation,standalone-fixture}.json`。没有模型调用或用户素材。

`prepare.py` 用于核对固定提交与归档源码，日常构建使用已归档源码，不依赖原仓库缓存。`verify.cjs` 写入候选独立包 `.local/cache/workbench/ds-essay-candidate/standalone/`，保留既有目录，不自动删除。

## 单次协议

由正式部署器校验 [deployment-manifest.json](deployment-manifest.json) 的 `runtime.required_files`，复制全部文件到版本化正式运行目录。部署后传入该目录的 `dist`，不得在正式运行时指向调查缓存。

```text
<fixed-node.exe> <formal-root>/helper.cjs --compiled-root <formal-root>/dist
```

stdin 输入一个 UTF-8 JSON 值并关闭 EOF；stdout 输出一个 JSON 值加 LF。输入、输出分别最多 `2097152` 字节。调用方通过参数列表、隐藏窗口、有界超时管理所属进程。这里不启动服务或持久进程。

只支持 `operation: "validate_feedback"`。完整请求见 [request.json](examples/request.json)，实际回复见 [response.json](examples/response.json)。

| 字段 | 契约 |
| --- | --- |
| `protocol_version`, `request_id`, `operation`, `payload` | 协议 1；请求 ID 为 1～160 位英文字母、数字、下划线或短横线；不接受其他 envelope 字段。 |
| `payload.draft` | 仅 `draft_id/revision_id/content_hash/content_role/text`。hash 为实际原稿原始 UTF-8 的 SHA-256；正文不 trim、不规范化、不截取。最多 50000 Unicode codepoint，拒绝孤立 surrogate。 |
| `payload.feedback` | `draft_revision_id/draft_content_hash/body/annotated_text/host_terminal/cancel_requested`；可选 `origin: "ai_reference"`。正文分别最多 512 KiB 和 256 KiB。 |
| `payload.rubric` | 服务端冻结的 `max_total/aggregation/dimensions[{name,max_score}]`；aggregation 为 `sum` 或 `independent`，最多 32 个唯一维度。 |

`host_terminal`、`cancel_requested`、稿件身份和 rubric 必须由项目任务与 Host 状态填入，不能采信模型自行声称的终态。只有 `completed` 且 `cancel_requested === false` 才可能输出 `reference_ready`。

输出固定为 `origin: "ai_reference"`。`status` 为 `reference_ready/pending/stale_reference`；原文字符串留在 `raw_feedback/raw_annotated_text`。可靠格式下的 `reference_evaluation` 提供总分、满分、维度与原解析器计算的等级；其含义始终是 AI 参考评价。`parsed_candidate` 仅用于诊断，不能绕过 `status/diagnostics` 当成正式判分。

批注为 `annotations[]`，每项带实际稿 `revision/hash`、`start/end` 半开区间、`offset_unit: "unicode_codepoint"`、原稿中的 `quote`、类型、理由与候选 replacement。无法唯一定位的内容留在 `unmapped_annotations`，不画成可信选区。`utf16_index` 是排查用的原函数索引，不是业务坐标。

例如原稿 `前🙂 bad 后 bad 末`，第二处 `bad` 对应 codepoint `[9,12)`，原函数 UTF-16 索引为 `10`。XML 传输中的元素内容与属性各解码一次；实际原稿、CRLF、空白和组合字符均保留。

exit `0` 表示返回合法结果，包含待判断和旧稿评价；exit `2` 表示输入、协议或大小限制错误；exit `3` 表示启动参数、版本或部署失败。正常及稳定错误回复不写 stderr。

## 原代码与项目责任

原 `parseScoreFromText/parseMarkers/parseStreamingContent/findAnchoredIndex/markerOriginalText/buildSuggestionChange/calculateEssayTextStats` 参与实际解析、未完成检测、锚定和统计。外层只增加冻结身份、严格分数与 rubric 检查、原稿精确比对、重复目标与 codepoint 边界限制；不改原函数。

原解析器接受数值前缀并会 clamp，维度 Infinity 也可能被夹成有限值。候选把这些原始输出保留为诊断，将原始数值异常、多 score、维度缺失、总分不符、半截标记、停止或未知终态标为 `pending`，不自动修复成成功。嵌套实体和重复目标有独立负例。

库内 `revisionProposal/appendAssessment` 仅是确定性事务 fixture；`helper.cjs` 不暴露这些 operation。原 `essayContentState` 切轮和快照函数也只是 UI/纯函数候选。正式业务由项目 Artifact revisions、冻结的轮次任务、独立 feedback Artifact、append-only 自评／改判、operation hash/CAS 与迟到回调 guard 负责。这里没有业务 DB、队列、供应商执行层或自动采纳修改。

原 Rust executor 的 trim、动态 latest 轮次、VFS 保存和 provider pipeline 未迁入；模型重试、图片读取、把含 `</score>` 的 Incomplete 响应当完成也未迁入。该候选只接项目同一 Harness 的最终原始输出。

原许可和通知在包内 `dist/notices/`；[ADAPTATION-NOTICE.txt](ADAPTATION-NOTICE.txt)记录原文件身份和适配。完整上游 notice 清单描述上游应用，不代表候选引入其全部依赖。

本次仅候选验证。正式 Host、Artifact 事务、产品 UI 采纳／撤销、真实模型格式、真实素材及 S07／13 场景验收均未在此候选中执行。
