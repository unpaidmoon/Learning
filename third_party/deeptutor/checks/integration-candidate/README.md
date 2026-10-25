# C09～C13 实际包调用与合成验证

发行包是 `learning-deeptutor-reuse==0.1.0`，Python import 包是 `deeptutor_reuse`。
源码在 `third_party/deeptutor/src/deeptutor_reuse/`。没有名为 `learning_deeptutor_reuse` 的 import 目录。

本目录只提供自造 fixture 和集成说明，不是正式任务执行入口、权威队列或业务数据库。
完整审计见 [迁入报告](../../../../research/reports/workbench/deeptutor-c09-c13-integration.md)。

## 可直接使用的模块

| 模块 | 主要类／函数 | 边界 |
| --- | --- | --- |
| `book_models` | `Book, BookInputs, Spine, Chapter, Page, Block, SourceAnchor, Progress` | 原样 Pydantic IR，`BookPage=Page`、`LearningBlock=Block` 只是调用方别名；宽 payload/SourceAnchor 不做来源授权或渲染安全。 |
| `book_block_compiler` | `BlockContext, BlockGenerator, GenerationFailure` | 实际原 async `generate` 包装；子类提供 `_generate`。默认 AI registry 未迁入。原方法会修改 ctx.block。 |
| `book_compiler_state` | `finalize_terminal_page(page)` | 原终态聚合＋项目前置检查；非 READY/ERROR 块拒绝。 |
| `book_progress` | `record_attempt, mark_visited, recompute_score, recompute_weak_chapters, completion_ratio` | 原纯函数会修改传入 Progress；score 是正确题数，ratio 是访问比例。 |
| `book_export` | `render_book_markdown(book, spine, pages)` | 原文字投影，不是完整离线 HTML 打包器或 HTML 清洗器。 |
| `book_resume` | `BookCompileSnapshot` 及下列状态迁移 | 新项目纯 DTO/状态适配；原模型、生成包装与终态函数实际被调用，没有 IO/队列/模型调用。 |
| `learning_transactions` | `PendingOption, PendingQuestion, MasteryInteraction, MasteryEvent, MasteryPathLease, InteractionStatus` | 原 DTO/分组校验摘录；事务提交、答案公开投影、判分与掌握由项目负责。 |
| `learning_actions` | `apply_learning_action, independent_grade_eligible` | 跳过／看解析只返回事件，不追加作答、改判或创建掌握记录。 |
| `visualization_models` / `offline_data` | 原 `VisualizationEnvelope`；新 `validate_offline_visualization` | 原 envelope 宽字段不作安全边界；新适配只允许固定纯数据二次函数图。 |
| `research_similarity` | `normalize_topic, topic_tokens, topic_similarity` | 原标题相似度，未迁入权威研究队列。 |

## 生成／恢复的调用顺序

`BookCompileSnapshot(book, inputs, spine, pages)` 冻结原模型与已授权输入，目录和 page/block 规划必须已经存在。
`inputs.source_refs` 必须由项目冻结真实 asset/version/role；该宽字段不会替调用方授权。

```python
from deeptutor_reuse.book_resume import (
    outline_digest, confirm_outline, next_block, begin_block,
    make_block_context, complete_block, interrupt_block, resume_book,
)

# 只在项目已收到一次明确目录确认后调用：
state = confirm_outline(state, outline_digest(state))
item = next_block(state)
started = begin_block(state, item, operation_id)
state = started.snapshot
# 项目先原子持久化 state；dispatch=False 表示该 operation 已知，不能再次调用模型。
if started.dispatch:
    ctx = make_block_context(state, item)  # 独立对象，避免原 generator 原地污染检查点。
    receipt = await application_owned_generator.generate(ctx)
    state = complete_block(state, operation_id, receipt)
    # 项目把原始 receipt 和新 state 在自身事务内提交，再公开完成页面。
```

应用取消或重启后，`interrupt_block` 保留 generating 块并标记 operation unknown；`resume_book` 会返回
`BOOK_RESULT_UNKNOWN`。实际已有结果时，用原 operation_id 的真实 receipt 调用 `complete_block` 对账；
能证明未出站时才能显式调用 `reconcile_not_started(..., reconciliation_ref)`。之后显式 resume，使用新
operation_id。没有自动补发、自动 retry 或第二次目录确认。

`readable_pages` 返回 READY 页；`readable_chapters` 返回全部页面已 READY 的章。它们在整书未完成／
后续章 unknown 时仍可读早先完成章。不实现业务读取授权或自动锁定先修章。错误块需要应用明确处理；
当前适配不提供完整归纳的“接受部分”决定、重规划、隐藏块处理、源版本切换或归档权限。

## 跳过／解析和判分

`apply_learning_action(progress, interaction, action, event_id, revision, now, target_page_id="")` 返回
`LearningActionResult(progress, interaction, event)`，输入对象不被修改。action 为 `skip`、
`view_explanation` 或 `navigate`；目标页必须已由项目核权，可自由指向合法后续页，不检查先修闯关。

跳过活跃题把事务标记 ABANDONED；查看解析在尚未判分的题及分组叶子上标记 assistance_used。
两者都不追加 QuizAttempt、不清掉既有 wrong、不增加分数；后续独立判分资格会排除该暴露。
已独立判分的历史题后来查看解析，不反向抹掉其原证据。

原 `record_attempt(is_correct=None)` 可以记录未判分，但会成为该题“最新一次”记录。因此不能把“看解析”
直接当成 None 作答追加：它会影响旧 wrong 的 weak_chapters。项目改判应写真实判分历史并由正式事务处理。

## 可执行示例和验证

`minimal_example.py` 实际经过原 Book/Page/Block 和原生成包装，输出：
`ready`、`confirmation_count=1`、`readable_after_each_commit=[["c1"],["c1","c2"]]`、`model_calls=0`。
它不会启动 Host、业务库或真实模型。

已经成功执行：

```powershell
& backend/.venv/Scripts/python.exe third_party/deeptutor/checks/integration-candidate/verify_integration.py --output .local/cache/workbench/deeptutor-c09-c13/source-r2
uv build third_party/deeptutor --wheel --out-dir .local/cache/workbench/deeptutor-c09-c13/dist-final --python backend/.venv/Scripts/python.exe --offline
& backend/.venv/Scripts/python.exe third_party/deeptutor/checks/integration-candidate/verify_wheel.py
& .local/cache/workbench/deeptutor-c09-c13/wheel-env/Scripts/python.exe third_party/deeptutor/checks/integration-candidate/verify_integration.py --installed --output .local/cache/workbench/deeptutor-c09-c13/wheel-r2
& .local/cache/workbench/deeptutor-c09-c13/wheel-env/Scripts/python.exe third_party/deeptutor/checks/integration-candidate/verify_integration.py --installed --output .local/cache/workbench/deeptutor-c09-c13/wheel-r3
& .local/cache/workbench/deeptutor-c09-c13/wheel-env/Scripts/python.exe third_party/deeptutor/checks/integration-candidate/minimal_example.py
```

输出目录使用 `exist_ok=False`；再次验证应指定新目录。验证环境不进入产品依赖。
正式包由集成方刷新：`uv sync --project backend --locked --reinstall-package learning-deeptutor-reuse`。
本分工没有对 backend 环境执行 sync，包及 backend 的依赖声明／锁文件和 source_digest 均未改。

验证结果：源码 8 组 passed；实际 wheel 8 组 passed；实际 wheel 的既有回归 16 passed、0 skipped。
`file://` 部分是既有真实断网浏览器记录的冻结制品重新核 hash，本轮没有重新启动浏览器；
原 zip、75 文件、此前 HTTP/WS/业务存储写/模型请求均为 0 的记录保持一致。正式产品另行验收。
最终 `wheel-r3` 校验将资源清单自身也与冻结 identity 对账，核 74 个资源＋清单本身共 75 文件，
并补齐原事务／可视化子模型的实际字段导出。
`freeze_proof.py` 已验证 26 个原函数体只改模型 import／去除 registry 尾部，19 个 wheel 模块与
当前源码及迁入 manifest 一致，根许可和修改通知逐字节一致；结果在 [evidence/proof.json](evidence/proof.json)。
