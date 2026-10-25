# C06／C08／C13 内容候选

[完整审计](../../../../research/reports/workbench/deeptutor-c06-c08-c13-content.md) 区分原样代码、项目适配、
已观察上游缺口与正式产品未覆盖项。全部输入为自造 fixture，没有真实模型、检索器、用户材料或 Host。

## 可迁入文件

| 文件／模块 | 调用与责任 |
| --- | --- |
| `src/deeptutor_reuse/source_windows.py` | 新项目 `TextUnit/TextWindow/split_text_unit/reassemble_text_unit`；24k codepoint、原偏移／文本hash／segment／ms／版本完整保留，无检索或执行权。 |
| `src/deeptutor_reuse/eval_matching.py` | 新增完整原样 DT matching；配已有 `eval_dataset/eval_metrics`，标准库闭包。 |
| `src/deeptutor_reuse/research_records.py` | 原主题 DTO／9个只读查询统计，UTC和证据版本引用适配；没有新业务队列。 |
| `src/deeptutor_reuse/research_recovery.py` | 只返回须对账／缺源待决定等分类，不改状态、不重发。 |
| `handout_adapter.ts` | 项目严格 text-only 包装，实际调用原 DS IR/Markdown；保持精确ms/source sidecar，缺图明确拒绝。 |
| `retained/{ir,markdown,handout_types}.ts` | 与包装一起构成可直接import的四文件闭包；原IR前缀去掉salvage／帧时间收集，Markdown只改type-only导入。 |
| `upstream/deepstudent/src/features/media-handout/{ir,markdown,windowing}.ts` | 固定DS原件；windowing只用于缺陷探针，不当生产完整性切块器。 |

Python import 前缀是 `deeptutor_reuse`；发行包是 `learning-deeptutor-reuse==0.1.0`。
C06 实属 DS，C08/C13 实属 DT。DS TS原件和 `notices/` 中的 AGPL／文件级MIT原文保持独立，未进入本Python wheel。
`extraction-manifest.json` 保存原文件blob/hash和具体方法，包侧迁入账在 `../../reuse-manifest.json`。

## 最小调用

```python
from deeptutor_reuse.source_windows import TextUnit, split_text_unit, reassemble_text_unit

unit = TextUnit("leaf", "asset", "source-v1", "subtitle-r3", "source_content", "完整的自造字幕😀",
                segment_id="segment-1", start_ms=1234, end_ms=9876)
windows = split_text_unit(unit, 24000, prefer_lines=False)
assert reassemble_text_unit(unit, windows) == unit.text
```

沿用 Root 已冻结24k leaf时使用 `prefer_lines=False`；不能重切旧plan后更换叶子来源。默认换行优化只用于新规划。
单段各片保留原时间，不从字符比例猜词级时间。空输入零窗口不等于来源缺失已解决。

```python
from deeptutor_reuse.eval_matching import RelevanceMatcher, source_text
from deeptutor_reuse.eval_metrics import evaluate_hits

matcher = RelevanceMatcher(case.gold)
hits = [set(matcher.match(source_text(row)).indices) for row in authorized_frozen_results]
scores = evaluate_hits(case.id, hits, matcher.gold_count, k=4)
```

实际检索、权限／来源过滤、失败状态和成功查询分母由 Root 提供。词面匹配不是数学等价、来源权或完整覆盖。
失败查询保留 `metrics=None`，空批次显式 not_run；不要把缺失变成零分成功。

研究恢复由 `TopicBlock.from_dict` 和只读 `ResearchTopicView` 投影已有业务记录；
`review_research_recovery(records, available_sources=..., available_evidence=...)` 只返回建议。
researching 必须由 Root 查真实 attempt ledger，缺来源不自动联网。Root 才拥有权威任务、CAS和提交事务。

## 实际执行

```powershell
& backend/.venv/Scripts/python.exe third_party/deeptutor/checks/content-candidate/verify_content.py --output .local/cache/workbench/deeptutor-c06-c08-c13/source-r1
node third_party/deeptutor/checks/content-candidate/verify_handout.mjs c06-node-r2
node third_party/deeptutor/checks/content-candidate/verify_handout.mjs c06-node-r4
uv build third_party/deeptutor --wheel --out-dir .local/cache/workbench/deeptutor-c06-c08-c13/dist --python backend/.venv/Scripts/python.exe --offline
& backend/.venv/Scripts/python.exe third_party/deeptutor/checks/content-candidate/verify_wheel.py
```

Python6组、Node4组、实际wheel6组均 passed；实际wheel原16项及书/动作8组回归通过。270013 codepoint的单行字幕
切成12块重组hash完全相同。输出目录不会覆盖，复测用新目录／run id。Node运行的是纯模块，未启动浏览器或后台。
最终 `c06-node-r4` 还实际 typecheck 四文件闭包，0错误；原始全量windowing只出现在legacy缺陷探针。
`freeze_proof.py` 的结果在 [evidence/proof.json](evidence/proof.json)，核9个原只读方法体、实际wheel模块、
原许可通知和 TS／Python 制品边界。

wheel SHA-256 为 `8a116a278f8873e0bac4b9fcce2cb7ad686344ffa95ec6899b78a01ab8f784cb`。
版本／依赖不变，Root正式环境仍需显式重新安装该path包；本分工没有对backend环境执行sync。

本候选不包含自动抽帧、图片吸附、缺图省略、salvage成成功、隐式重试、检索生产引擎或新的模型执行器。
