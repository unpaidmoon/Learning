# DeepStudent 源码借鉴分析

日期：2026-10-22。对象：[官网](https://deepstudent.cn/)链接的 [`helixnow/deep-student`](https://github.com/helixnow/deep-student)，固定源码提交 `635216503c0b34bdf812e17942e0f09f51872e6f`（2026-10-18）。本报告核读公开源码与随库文档，未安装、启动或调用其模型、OCR、ASR；官网演示不作为原生应用运行证据。

本项目适配依据是[最新需求收口](../../docs/product/PDF阅读与字幕及课程知识库需求收口-2026-10-22.md)。其中知识库 skill、课程资料默认索引、有限文档图文读取已确认；实际首批任务清单、整章归纳分期尚未决定。旧研究中“skill 尚待确认”不再是当前状态。本报告仅为研究建议，不修改业务代码、需求或工作包状态。

## 1. 判断

**主要借鉴它把阅读、选取、草稿、来源回看连起来的细节。** 不建议整体迁入 Rust/Tauri 运行层、聊天系统、自由桌面和统一个人知识检索。最有价值的源码同时显示了不能直接复用的差距：选区快照只有单个 locator，PDF 入口只定位起始页；没有显式来源版本字段；长选区会截断；媒体入口主动新开对话；其媒体转写工具走外部 ASR。

以上是下面逐项源码证据的归纳，并不代表 DeepStudent 在所有入口都有同样限制，也不证明本项目已经完成相应能力。

## 2. 最值得借鉴的设计

| 源码观察 | 对本项目的建议 | 证据与边界 |
| --- | --- | --- |
| PDF “添加到聊天”将选区文字、来源身份、页码 locator 形成结构化引用；注释明确修正了旧入口只把选中文字放 metadata、最终引用整本书的问题。 | 选择内容与源文件身份分开保存；手动选区作为不可变快照加入现有草稿，发送才出站。显示源文件名与定位，但不能因能回看原件而默认扩大模型输入。 | [PDF 选区入口 L216–244](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/pdf/components/PdfSelectionActions.tsx#L216-L244)。这是源码可达链与历史问题注释，未捕获真实出站。 |
| `selectionToChat` 将 `text` 与 `source` 序列化后存为引用资源，并调用会话 `addContextRef`；locator 必须进入数据 hash，避免不同位置的相同文本被错误合并。 | 同内容在不同页、不同版本应保留不同来源身份。引用去重至少要考虑来源版本、页码与区域；不要只按摘录文字去重。 | [快照与 hash 说明 L1–13](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/context/selectionRef.ts#L1-L13)、[加入现有草稿 L163–213](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/context/selectionRef.ts#L163-L213)。不必照搬其 `retrieval` 虚拟资源类型与 store。 |
| 技能加载模块提供 `load_skills`、技能清单发现和会话级已加载状态；知识检索技能组合对应工具 schema。 | 借鉴“短描述用于发现，加载后提供流程和工具”的分工；本项目继续用 Harness 原生机制，只实现课程受限读取能力，不另造管理器。 | [渐进加载 L1–8、L73–113](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/skills/progressiveDisclosure.ts#L73-L113)。加载提示、白名单不等于执行权限已经落实。 |
| 索引界面区分 `pending/indexing/failed/disabled/indexed`；同时引用按资源、按 unit 两类状态接口和诊断组件。 | 文件树展示简短状态，详情再显示覆盖、失败页及重试。把“未处理”“失败”“已处理但未命中”区别开，避免用户误以为资料不存在。 | [索引状态界面 L1–29、L102–108](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/learning-hub/views/IndexStatusView.tsx#L102-L108)。仅状态界面不是索引恢复正确性的证明。 |
| 转写保存稳定的 `[mm:ss] 文本` 行；仅把已完成且非空片段构建成文本。课程技能要求时间引用来自实际检索/读取结果。 | 底层保留精确毫秒、片段身份和版本，显示时间便于理解；引用能回到原视频。显示秒数不能替代本项目的原始时间精度与时间窗 guard。 | [转写序列化 L1–48](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src-tauri/src/media/transcript.rs#L1-L48)、[技能引用要求 L43–53](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/skills/builtin/course-study.ts#L43-L53)。引用要求是提示词，不能单独证明不会编造。 |
| 翻译组件把关闭、语言切换、卸载与流取消关联，使用请求序号隔离旧回调、帧批处理流式 UI；这些是其文件说明中的明确设计。 | 可参考局部翻译的生命周期和竞态处理，在本项目验证真实生成中取消与过期结果不覆盖新选区。 | [翻译组件 L1–35](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/components/TranslationPopover.tsx#L1-L35)。本轮未运行确认它全部满足说明；不搬其 Tauri IPC。 |

## 3. 直接复用前必须看清的差距

### 3.1 PDF 跨页引用并未由所读入口完整表达

`resolveSelectionPage` 只取 `window.getSelection().getRangeAt(0).startContainer` 最近的 `data-page-number`，返回单个页码。随后 `selectionToChat` 只构造 `page:N`；`SelectionRefData.source` 是单个 `locator`，没有分页面区域数组。

证据：[起始页解析 L153–172](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/pdf/components/PdfSelectionActions.tsx#L153-L172)、[引用字段 L36–54](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/context/selectionRef.ts#L36-L54)。

**推论：** 该入口不能直接满足我们“一次跨页拖选、保留各页来源与区域”的需求。不能据此断言其全应用不支持跨页选字；问题在于所查引用链的定位表达不完整。本项目应记录每页的文字范围／坐标，而非只记起始页。

### 3.2 已保存选区的来源版本与完整性仍需另外设计

所读 `SelectionRefData.source` 包含种类、`sourceId/locator/title/messageId`，没有显式 `source_version` 或源文件摘要。其 `serializeSelectionRefData` 在选区超过 `20,000` 字符时保留前段并附 `…`。这是有界存储策略，但会改变原选区内容。

证据：[字段 L36–54](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/context/selectionRef.ts#L36-L54)、[上限与序列化 L73–112](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/context/selectionRef.ts#L73-L112)。

**推论：** 可借快照模式，但本项目须增加稳定来源版本、跨页定位和实际保存内容说明。超过支持范围应明确告知，不应让用户以为完整选择已经加入。这里未追踪 `sourceId` 底层版本机制，不据局部类型缺字段断言 DeepStudent 全局没有版本管理。

### 3.3 翻译确实传入选区前后文参数

PDF 入口调用翻译时保存 `selection.contextBefore/contextAfter`；共享选区 hook 各侧提取最多 200 字符。翻译组件构造请求时传入 `context_before/context_after`。因此与本项目“仅发送选中文字”的规则不同。

证据：[PDF 参数 L144–151](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/pdf/components/PdfSelectionActions.tsx#L144-L151)、[上下文截取 L64–112](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/shared/selection/useTextSelection.ts#L64-L112)、[翻译请求参数 L540–552](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/components/TranslationPopover.tsx#L540-L552)。这证明前端请求字段，不是运行抓包。本项目最多复用入口与取消思路，必须删除额外上下文的数据路径。

### 3.4 媒体问答入口主动新建会话

`mediaChat.ts` 使用 `PREFILL_CHAT_INPUT` 的 `newSession: true` 或 `CHAT_NEW_SESSION`，等待会话切换，再启用 `course-study` 并引用媒体。它有处理“冷启动引用挂错会话”的细节，但整体行为与本项目“同次使用同课程沿用当前对话”不同。

证据：[媒体入口 L1–9](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/media-studio/mediaChat.ts#L1-L9)、[新会话请求 L61–90](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/media-studio/mediaChat.ts#L61-L90)。借鉴引用绑定到明确会话、预填与发送分开；不搬其新建会话策略和基于等待超时取当前会话的实现。

### 3.5 媒体 ASR 不能被称为本地转写实现

`media-tools` 技能明确把音频送外部 ASR 提供商，复用配置的语音输入 ASR；声明视频提取不可用时返回 `unsupported`，不能自行调用系统 ffmpeg。随库用户指南也列举云端 ASR 配置，并说明逐段收费。

证据：[工具声明 L15–25](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/skills/builtin-tools/media-tools.ts#L15-L25)、[用户指南转写部分](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/docs/user-guide/19-%E9%9F%B3%E8%A7%86%E9%A2%91.md#L88-L102)。可借能力探测、分段状态和明确不支持格式的做法；本项目本地 ASR 与受控调度仍须独立验证，不能拿其实现作已满足的证据。

### 3.6 检索规则、视频画面与复习功能需要取舍

- 知识检索技能合并资料、PDF 图片、用户记忆；其主动检索开关要求先检索，再回答；默认策略允许本地不足时联网。见[技能 L12–62](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/skills/builtin-tools/knowledge-retrieval.ts#L12-L62)。本项目的当前课程、明确跨课程和外部请求边界更窄，不能复制其默认策略。
- 课程技能要求先搜索再读取，并可制卡、出题；指南说明自动结合字幕和关键画面生成讲义、字幕进入一般检索。见[课程技能 L43–58](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src/features/chat/skills/builtin/course-study.ts#L43-L58)、[音视频指南](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/docs/user-guide/19-%E9%9F%B3%E8%A7%86%E9%A2%91.md)。本项目不自动补帧、不把整课字幕纳入知识库，不据此新增卡片、笔记和自测。
- 后台文本质量过滤把数学符号等不计入常用字符，常用字符比例低于 40% 时拒绝文本。见[过滤逻辑 L130–191](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/src-tauri/src/vfs/indexing.rs#L130-L191)。**推论：** 对公式密集数学材料应另验，不能直接继承该阈值。本轮没有数据集，不声称已经发生某种漏检率。

## 4. 代码候选与迁入成本

| 方式 | 候选 | 建议 |
| --- | --- | --- |
| 参考设计 | 选区快照与来源定位、索引状态、字幕回链、翻译生命周期 | 价值最高，可按现有业务身份、草稿和 Harness 接口重写小范围实现。 |
| 小模块候选 | locator 显示、时间格式化、选区工具条的交互测试思路 | 对照本项目需求重新验证；当前未完成逐文件依赖与许可证核查，不能宣称可直接复制。 |
| 整体迁入 | Chat V2、VFS/SQLite/LanceDB、自由桌面、Rust/Tauri 与技能管理器 | 耦合其状态、IPC和资源协议，迁入会与既有 Harness 与业务数据边界重叠，当前没有收益证据。 |

仓库[LICENSE](https://github.com/helixnow/deep-student/blob/635216503c0b34bdf812e17942e0f09f51872e6f/LICENSE#L1-L18)标注 GNU AGPL Version 3。这里仅记录许可身份；具体代码复用须在实施时核文件、依赖及分发方案，本报告不作兼容性法律结论。

## 5. 证据覆盖与未覆盖

已回答：选区到草稿的结构、PDF 页码定位、选区截断与字段、翻译前后文参数、技能发现设计、索引界面状态、字幕文本与引用、媒体新会话入口、ASR 工具对外部提供商的声明。

部分回答：索引恢复只核状态界面及配置、分段保留只核相关序列化与用户指南；未追完整崩溃恢复和落库事务，因此不宣称其暂停／重启保证可迁入。选区来源版本只核所读类型，没有覆盖整个 VFS 版本机制。

未验证：发布包与当前源码一致性、真实数学／英语 OCR效果、跨页拖选的实际交互、实际模型出站、取消完整性、召回质量、性能与费用、Windows/Android 真实学习体验。源码里的测试和官网演示均未运行，不转换为验收通过。
