# C14 实际索引恢复、游标和滚动核对

日期：2026-10-23。固定 DeepTutor 提交 `a31d82624c27d75f4e771d4fcb78dce16470b383`。
本次只核原码、只读项目快照及独立合成 fixture，未改正式 backend/frontend/runtime，未改已冻结的
23 个迁入模块、`__init__`、包声明和锁，也未重建或替换正式 wheel。没有实际索引器、模型、用户素材、
业务数据库、浏览器或 Host 调用。Root 是正式集成人。

## 必须保留的产品缺口

1. **SourceJump 用户入口尚未接通。** 采样时全 frontend 的 SourceJump/sourceJump 只见
   `actions.ts`、`pdfAdapter.ts`、`PdfReader.tsx`、`ResourceViewer.tsx` 的声明、实现及透传；
   未发现 App／引用卡片构造并传入该意图。`App.openFile` 只开文件 tab，没有页／区域参数。
   因此 adapter 能接收对象并回跳，不等于用户点击引用回原文的产品链已经成立。
2. **request_id 尚未用于去重。** `pdfAdapter.jump` 每次都覆盖 pendingJump 并调用 applyJump；
   applyJump 成功后清空该槽，未记录已消费 request_id。再次传入同一 id 仍可重新跳。
   PdfReader 的 effect 依赖 sourceJump 对象变化；调用方需要区分一次明确回跳与普通重开恢复，
   防止旧跳转意图在新挂载／重建对象时重新覆盖阅读位置。本次没有修产品或验证调用层修复。

上述结论基于本次只读采样；Root 后续接线应另记实际用户点击／迟到／日常重开验收。
本次 Node fixture 是注入 SourceJump 的组件验证，没有把它记成用户入口闭环通过。

## 来源、候选与取舍

全部原件按已有固定提交归档和 hash 核对，具体记录在
`third_party/deeptutor/checks/c14-candidate/source-manifest.json`。Apache-2.0 原许可另存同目录 notices。

| 原文件／符号 | 核对与最小闭包 | 本次决定 |
| --- | --- | --- |
| `knowledge/indexing_run.py`：load_run、visible_run、IndexingRun.check/document/verify_sources/finish、request_cancel | 顶层为标准库、原 file_io、可选 psutil；真正 `_start` 还涉及文件路由、KB/raw 枚举、进程／文件租约。 | 完整文件只作探针，一处 file_io import 转到已验证独立包。没有调用 `_start`／上下文入口，没有迁入另一套索引任务权威。 |
| `services/parsing/engines/mineru/checkpoints.py::SliceCheckpoint` | 摘出完整原类、500MiB cap 和标准库导入；原方法体保留。 | 窄完整性／原子指针候选可独立保留，放 checks；当前本地 PDF/OCR 与业务版本机制没有必须新增它的证据。 |
| `services/rag/index_versioning.py` | 版本目录与 embedding signature；目录存在／有非 meta 文件只属较弱 ready 判断。 | 不搬上游 KB 目录和版本权威，不把目录标记当检索可用。 |
| `pipelines/llamaindex/pipeline.py::_publish_verified` | 真实顺序为重载验证持久 index → verify_sources → 写版本 meta → 更新 usable_version／完成文档。 | 保留控制点依据；不引入整套 LlamaIndex、embedding 配置、视觉资产与 KB 管理。 |
| `pipelines/llamaindex/storage.py::verify_persisted_index` | 验证向量、非空 docstore/index_struct，再用已有向量检索 probe；不靠一个 Host ready 标志。 | 已读实际实现，本次没有运行该索引引擎或证明索引恢复成功。 |
| `TurnRuntimeClient.ts::setResumeCursor` | 同 turn 不回退，换 turn 清缓存；方法可单独保留，无 socket 依赖。 | 在 probe 外壳中原样调用，暂不替换 Root 的 native turn/rpc/attempt/水位映射。 |
| `acceptStreamEvent/emitInOrder` | 原排序、重复抑制、gap buffer 和请求回放逻辑。 | 只作行为探针，不迁入完整 transport。不同 turn 的事件会自动切换本地 cursor，不能充当 Root 的课程／请求归属围栏。 |
| `useChatAutoScroll.ts::handleScroll` | 原箭头函数只读布局数值和 follow refs；完整 hook 还依赖 React、Resize/MutationObserver、RAF、load 与用户手势。 | 原判断体进入数字布局探针；不替换 Native Harness 聊天滚动，不套到 PDF 阅读位置恢复。 |

索引原文件 SHA-256：`e49b9489fcec696ed961456f388cf7041481ec21933fe9576c41df2ec8c039e1`；
SliceCheckpoint 原文件为 `8d9000853025b328743975a80dd4d86d69ade9a8d3c3ae724d4618d971004c08`；
TurnRuntimeClient 为 `1635d9006022e80a5701cf223b23f66cb1c02be838ae7e9734f5b82194d5cd25`；
scroll hook 为 `89163d909011361f38e095026b9918b504601160601e4e0b5eb03ef42df84f22`。
其余逐文件 hash 和项目快照 hash 以 source-manifest 为准。

## 真实恢复代码的边界

`visible_run` 并不是自动继续任务。它读取 journal／取消标志，结合进程创建时间或文件租约判断 owner。
owner 丢失时返回 interrupted 投影，把 pending/parsing/embedding 标为 unknown，保留 parsed、completed、
failed 等状态；原文件不被该投影改写。parsed 表示解析结果可以核验复用，不是 embedding／索引已经完成。
`finish` 则会将尚未完成的 parsed 也投为 unknown，解析缓存仍是另一个可复用工作存储。

`verify_sources` 对 parsed/embedding/completed 源重算完整 hash；`document` 和 `finish` 会忽略已失去 task_id
所有权的迟到更新；取消文件也绑定 task_id。损坏 journal／取消状态会报错并保留文件，而非猜成成功。
这些控制点适合 Root 的 DB/CAS/attempt 架构，但不是把上游 journal 直接复制成另一套业务真相源。

SliceCheckpoint 先发布不可变 hash 文件，再原子替换 ready 指针。实际故障注入证明 pointer replace 失败时旧指针
仍可读，归档字节被篡改则 load=None。但 hash 正确的任意非 ZIP 字节也能 load；它本身不验证归档内部、
来源版本、页范围、提取结果、向量或检索。job/root/page 参数也不是授权边界。上游 job_directory 的源 hash／
配置寻址与调用者的提取合并验证仍不可省略。本项目不使用其 MinerU cloud 栈，因此本次不新增正式依赖。

完整 DT transport 在新连接 generation 会 flush 未完成命令；是否可安全重发依赖其协议和服务端幂等。
不能据此替换本项目“结果未知先对账”，也不能将 turn_id 当业务 source_version。

## 对照本项目只读实现

实际路径为 `frontend/src/reading/readerSession.ts`（Windows 上大小写可访问，但仓库名为小写）。
该会话的 endpoint 固定 course/asset/source_version，激活前分配 intent sequence；异步 open 返回后核
disposed/visible/acquisition，迟到隐藏视图不挂载或抢准备优先级。隐藏 release 使用原 intent，不再发明更新序号。
保存使用 reader_epoch／expected_revision，旧 epoch 返回不会覆盖新会话 state；保存冲突后停止隐式重试。

`reading.py` 的 begin 按课程／资产／版本读取旧位置并建立 epoch；open/focus 还有 client/sequence/request 围栏。
save 先核来源和页，再按 course/asset/version/reader_epoch/revision 更新，rowcount!=1 报
READING_STATE_CONFLICT。位置保存与后台处理焦点分离：隐藏旧文档的合法保存不是重新抢当前文档优先级。
本次内存 SQLite 只执行 save 原方法体的 CAS 谓词，真实 source/page/Pydantic 接口为 fixture port，
没有验证实际文件权限、真实业务库或跨进程事务。

普通新会话恢复 page/scale/rotation/scroll_fraction；同一个挂载会话切换可见性时复用已经打开的 adapter。
明确 SourceJump 则检查 asset/source_version、选中目标页并居中高亮，成功后消费 pending 槽。
两者不是聊天跟随到底部；日常恢复不应反复应用旧引用跳转。跨页 selection 当前 applyJump 取 parts[0]，
本次也没有证明多页全部高亮或真实 PDF 坐标／布局准确性。

`workspace.py::_page/_result` 的 cursor 是 `updated_at|id`（历史版本用 created_at），按时间和 id 降序 keyset
分页。同时间用 id 解决边界重复；外层 statement 决定课程过滤，cursor 本身不授予范围。列表项在分页过程中
更新，可能移到 cursor 前面而需刷新重看；所以它适合工作区浏览，不是固定全课程枚举、索引覆盖证明或事件水位。

| 标识 | 范围与用途 | 不能代替 |
| --- | --- | --- |
| turn_id + seq | 传输有序回放／去重 | 课程、资产版本、native attempt 归属 |
| course/asset/source_version | 读取与来源身份 | 可变阅读位置 revision |
| reader_epoch/revision/client sequence | 阅读会话、位置CAS、焦点优先级 | 模型请求重发许可、来源新版本 |
| updated_at + id | 工作区列表分页 | 稳定范围枚举、流回放、旧引用回跳 |
| SourceJump.request_id | 明确跳转意图载体 | 当前实现尚无去重语义，需 Root 接通并消费 |

## 已完成的独立验证

| 层次 | 实际结果／证据 |
| --- | --- |
| Python | 6组 passed，`.local/cache/workbench/deeptutor-c14/python-r2/summary.json`；早先 python-r1 为5组，记录保留 |
| 原索引 journal | owner 丢失投影、task 绑定取消、坏状态保留、源改版拒绝、旧 task 迟到 document/finish 不改新记录 |
| 原 slice 类 | 原子指针失败保持旧版、hash 篡改拒绝，以及“hash有效不等于ZIP有效”的限制反例 |
| 项目 SQL 纯探针 | 同时间分页不丢／重；非法 cursor 拒绝；行更新跨 cursor 的特征；save 原谓词拒绝旧 epoch/revision并隔离course/version |
| Node | 6组 passed，`.local/cache/workbench/deeptutor-c14/node-r1/summary.json`；TypeScript5.9.3，仅源码体转译／端口模拟 |
| 原 cursor／scroll | 同 turn 不回退、gap／重复／大缺口回放请求；不同 turn 自动切换特征；用户离开底部后布局收缩／增长不重新跟随，主动向下近底可重启跟随 |
| 项目 ReaderSession | 原源码在 API 端口中运行：A 晚 open 不抢 B、release不加新序号、旧 epoch save忽略、待存位置合并、冲突不隐式重试 |
| 显式 jump 方法体 | 错版本不跳、合法对象可定位／高亮并消费pending；没有实际用户入口／浏览器／PDF几何验证 |
| 保护 | 23个迁入模块及__init__、包声明/锁 hash 与开始时相同；未修改冻结wheel源 |

所有模型、实际索引引擎、HTTP、WebSocket、Host 调用均为0。端口／内存数据库／故障注入不冒充真实进程崩溃、
实际索引重启、原生流或真实浏览器。没有启动用户实例，也没有新增产品实现。

## 交付与未覆盖

[候选入口](../../../third_party/deeptutor/checks/c14-candidate/README.md) 列出实际文件与命令。
可独立保留的是 slice 完整性、同 turn cursor 单调、scroll 判断的窄片段；当前没有必须再迁入正式包的证据。
Root 应保留现有 SourceRef／epoch／CAS／native 对账与布局所有权，不引入另一套文件任务、socket客户端或滚动作者。

仍未覆盖：SourceJump 用户点击接线及去重修复、实际 PDF/浏览器布局、完整 Native Harness 断线／回放／活动停止、
真实索引器和embedding持久版本恢复、进程崩溃／系统断电、跨进程CAS及用户阅读体验。来源版本、页区范围和
视频／截图许可继续由正式 guard 核验，本次未扩大素材摄入权限。
