# PDF 阅读、选区与任务链独立审查

日期：2026-10-23（Asia/Shanghai）。范围：`workbench/models/reading/pdf_worker/sources/manifests/routes.py`、资料选区草稿／提交、`DesktopLearningTaskInput`、前端reading与原生桥接。只新增本文；未修改产品文件。复核只用独占合成PDF和临时SQLite，没有启动Host、服务端口、浏览器或模型，没有读取用户课程、私有数据库、视频或屏幕。

源码在审查期间由主agent并发修复。以下区分初读观察、补丁和实际复测；旧缺陷不被列为当前仍存在，静态修补不冒充运行验收。

## 已证实的接缝与当前状态

| 编号／影响 | 文件与定位 | 触发和后果 | 最小修复／当前证据 |
| --- | --- | --- | --- |
| P1：过时开读请求可接管活跃视图的位置epoch | 初读 `reading.py:187–220`（`DocumentReader.begin`先慢源校验、后换epoch）；新实现`reading.py:208–308`、`ReadingOpenInput`；`PdfReader.tsx:41–45,79–85` | 旧视图begin卡在源校验；关闭旧视图并重新打开，新begin已成功；旧请求随后完成，又换成它的epoch。新活跃视图下一次保存报`READING_STATE_CONFLICT`，前端把conflicted置true后不再保存。不是把旧位置写成新位置，而是过时请求剥夺了新视图的保存权。独占并发fixture已复现。 | 主agent已在慢校验前分配epoch，增加持久client／request／view_sequence fence。最新定向复测：旧begin晚返回后新save仍成功；同新request不换epoch；旧sequence当前及重开Repository后均`READING_VIEW_STALE`。局部fixture通过。 |
| P2：顶层选区正文可改变词内空白却保留original身份 | 初读`reading.py:144–146`的顶层`_compact`比较；新`DocumentReader.capture`约164–168行 | 页内选区真实为`Learning`，`pages[0].text="Learning"`、框与glyph均正确，但顶层`text="Learn ing"`曾被接受，保存为original。页内第一轮`_same_layout_text`尚不能保护顶层，该例已用实际PDFium页复现。 | 主agent已将顶层也改用布局边界核验。最新变体返回`PDF_SELECTION_MISMATCH`；合法旋转90度／非零cropBox／跨两页`Learning\nLearning`后台选区仍成功。局部fixture通过；不推广为真实浏览器全部版式通过。 |
| P2：完整清单中间页失败导致任务无法留缺口 | 初读 `manifests.py:47–73` | 三页PDF已知范围，第2页注入暂时`PDF_PROCESSING_TIMEOUT`，初版抛出整次请求：第1页快照保留，但manifest=0、task=0，无法进入needs_sources、排除或接受部分。这里是已知页范围中的处理故障，不是“超限可绕过”或要求静默截断。 | 主agent已加入DocumentGapRef。复测：3个单位完整保留，创建任务为needs_sources，失败页missing；未将失败页当已读或完整。局部fixture通过。 |
| P2：重叠文档范围生成重复unit并抛数据库错误 | 初读 `models.py:ManifestCreate.nonempty_unique`、`manifests.py:32–72`；任务单元落库 | `[0,1]`与`[1,2]`为不同请求tuple而被接收，清单4单位仅3个唯一unit_id；完整任务创建抛`IntegrityError: UNIQUE constraint failed: learning_task_units.task_id, learning_task_units.unit_id`。既没有有效任务，也没有稳定可处理错误。 | 主agent已在慢读取前增加重叠检查。相同fixture现返回`SOURCE_RANGE_OVERLAP`；也可规范化求并集，但不能靠截掉后半范围处理。局部拒绝fixture通过。 |
| P2：原PDF iframe令软件截图失败 | 初读 `pdfAdapter.ts:52–56,123–145`、`workspaceSnapshot.ts:227–231` | 阅读器为可见iframe，截图热键已转发，但原冻结管线遇到所有可见iframe直接抛`CAPTURE_CONTENT_UNAVAILABLE`。因此PDF公式／图形无法沿既有软件可见范围截图主流程加入草稿。 | 主agent已增加`captureFrame.ts`弱注册表及仅活跃绑定Viewer的可见DOM读取分支，保留其他iframe拒绝。这里只确认静态接入；实际裁剪像素、缩放、旋转、框内／框外与字体尚未由本审查重测，不写通过。 |
| P2：新gap的重试须更换派生快照 | 过渡版 `sources.py:require_current(DocumentGapRef)`、`tasks.py:decide_gap('retry')` | 首版gap读取路径一直抛保存的旧error，即使同版本页恢复也不会重新准备，重试无法补齐。不能通过吞异常将空text变成已完成。 | 主agent已改为refresh_gap。独占复测：恢复原页后retry→queued、scope_revision=1、逻辑unit_id不变、新snapshot含实际页文、旧gap仍在、同operation回放返回原回执。局部fixture通过。 |

## 丢回执与输入边界复查

- 旧翻译点击每次新建operation、错误后可重发的路径已在审查期间修正。最新`PdfReader.tsx:146–198`先持久化operation，未知不清除、禁新翻译，仅GET原操作回执；重开可恢复核对，已知4xx才解除。`learning-bridge.js:494–498`仍携带该operation进入原session的任务入口。本审查只做修复后的静态核对，没有用旧代码继续宣称存在重复出站。
- `DocumentReader.capture`按页序、实际glyph及原页面region核对，页内不连续或正文不符会拒绝；跨课由SourceStore校验。PDF.js使用`viewport.convertToPdfPoint`回原坐标，PDFium保存未旋转原页坐标。没有证据证明常规缩放／旋转／裁切可绕过此边界；解析／字符预算等明确拒绝也没有被当作绕过。
- 已有后台旋转／crop用例从PDFium glyph自行构造选区，已有浏览器用例在旋转功能检查后先归零再选词。它们分别有价值，但不能替代真正旋转／裁切／跨页DOM选区→后台核验的联合证据。本报告未把未覆盖项写成已发现坐标错误。
- 资料引用通过可信snapshot完整JSON冻结入当前提交的conditions；保存、清稿均校验同课程及实际提交内容，原生beforeSubmit显式携带`source_snapshot_ids`。本次未发现可凭这些ID跨课读取或扩大成整页／全文的已证实路径。

## 独占复核证据

目录：`.runtime/validation/reading-contract-review-20261023-baseline-contracts/`，由本审查创建，仅包含合成材料／临时DB／脚本与结果，不作为产品运行依赖。

| 记录 | 实际结果与界限 |
| --- | --- |
| `probe.py`／`result.json` | 初版中间页超时、重叠range主键错误、词内空白改变仍标original均复现；全部仅本地，业务outbound_attempts=0。首次脚本根路径错误在生成DB前失败，修正后运行退出0；旧空fixture目录保留，未删除资产。 |
| `epoch_probe.py`／`epoch-result.json` | 初版延迟旧begin后，新视图save返回READING_STATE_CONFLICT；outbound_attempts=0。只对独占DB注入校验等待，不操作真实窗口。 |
| `gap_retry_probe.py`／`gap-retry-result-r2.json` | 该批gap恢复／旧快照保留／回执幂等通过；重叠范围与页内空白伪改已稳定拒绝；当批顶层空白变体仍接受，后续epoch-r2记录已补拒绝复测。此结果是局部fixture，不是S01／S02／S04端到端或真实模型通过。 |
| `epoch_probe.py`更新后／`epoch-result-r2.json` | 新view fence下活跃save、同请求epoch、旧序列与重开去旧通过；顶层伪改稳定拒绝，合法旋转90度／cropBox／跨页后台引用通过；outbound_attempts=0。保留r1结果，不重标旧失败。 |

实际命令使用项目`backend/.venv/Scripts/python.exe`分别执行以上三个独占脚本，没有重跑全套。另一个准备中的DOM截图helper因主agent并发引入新captureFrame模块而在导入阶段退出，未进入截图过程；这不是产品截图验收失败或补丁通过的证据。

截至收尾，begin世代／顶层正文已有上述定向复测，未将这批观察到的问题继续称为未修复。受控PDF iframe的真实像素与确认裁剪、旋转／跨页DOM到后台的联合链仍未由本审查执行；不能由静态补丁或后台选区fixture代签。保留局部结果，不并入历史整轮通过数；工作包状态及修复由主agent集成并更新唯一MVP计划。
