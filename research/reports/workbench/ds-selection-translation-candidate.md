# C01／C02 选区与翻译最小候选接缝

日期：2026-10-23。固定 DeepStudent 提交 `635216503c0b34bdf812e17942e0f09f51872e6f`；延续[原审计](deepstudent-reuse-audit.md)。仅新增独立候选、报告和 `.local/cache/workbench/ds-selection-translation-candidate/`，未改产品／锁／构建，未调用真实模型或读取用户材料。

**结论：原 LRU 可用于普通翻译的后端准入缓存；原扫描器对当前普通译文没有有效协议接缝，不应挂空调用。selectionRef 的三个小函数可以保留原函数体，替换应用依赖，分别服务来源显示和有完整身份的本地缓存输入；不能替换已验证的 SourceSnapshot 权威存储。**

## 固定原码与闭包

| 原件／函数 | 本次保留及实际算法 |
| --- | --- |
| `translationCache.ts`，SHA-256 `0bbc34b989ac18f6f9fe3bb41c75fc87dde522208da5aa450b043c413e68c210` | 原文件不改。Map上限100；读命中delete/set刷新顺序；满时删除最早key。key包含mode/model/语言及context＋source的SHA-256，crypto失败会降成djb2。value只有正文或aligned segments，不管理任务、用量或来源权限。 |
| `translationNdjsonParser.ts`，`86118e364aa9c87b8e285d0a8923e320e6e9f931437c85fa8e624ca774931b64` | 原文件不改。跨push保持pos/depth/string/escape，提取顶层花括号对象；仅识别`src/tgt`字符串及`done:true`。对象外噪声、数组外壳／围栏被略过；坏JSON静默丢弃；flush丢弃残片；done后不再产段。 |
| `translationTypes.ts`，`4c1c77e6b389c7562df35d187f57be04114e8cb7c98fc7d1c974a7b43da009d8` | 前两者只作type-only依赖；编译后没有运行时import。 |
| `selectionRef.ts`，`56ddea18cf3ac459b348f5ed8edc8fc9c8e55f018114c7fd2779c58c88e35347` | 新归档完整原件；精确源码slice提取`describeSelectionLocator/buildSelectionDisplayName/serializeSelectionRefData`及类型／常量，函数体不改。前两者的`t`仅替换为默认中文标签插值依赖。slice hash与行号在build-manifest。 |

前三个原件仍读取已有正式 `third_party/deepstudent/upstream/`；新增selection原件在候选独占目录。编译仅用项目现有TypeScript5.9.3；运行只要Node24.14.0、内置模块和生成CJS。原根GNU AGPLv3及上游AGPL-3.0-or-later说明／通知原样进入闭包，适配标记见候选的ADAPTATION-NOTICE。

未迁入 selectionToChat 主入口：它静态依赖sessionManager、resourceStoreApi、通知、view/window store、workbenchBus和导航事件，不能通过导入整文件得到纯模块。`definitions/selection.ts`还会提示模型用source-id回读更多资料，并生成缺源版本的`[PDF@id:page]`标记；不接入当前selected-only边界。其retrieval createOrReuse和VFS体系不替换本项目快照／来源跳转。

## 当前项目的真实接点

1. `SourceStore._save_verified` 已按课程、完整reference、原文和content_role生成稳定`selection_<hash>`；重复同一精确选区确实可得到同一snapshot_id，不会因每次随机快照导致永远不命中。
2. LRU应位于 `TaskExecutor.execute_unit` 完成`require_current`和任务准入后、创建Host binding／注册执行策略／dispatch之前。普通翻译仍是同Host单次请求、所选文字only和原生body追加。前端只拦请求会漏掉当前任务成果保存；Host `llm/stream` guard缺业务课程／成果事务，也不应把cache响应伪装成实际LLM出站或body事件。
3. `_finish`／`ArtifactStore.publish_run_result` 当前要求真实completed run；缓存命中尚没有正式写入路径。根方需添加窄cache-completion／publish事务：当前course/task/unit/scope/source仍有效、无已有run／成果，独立创建当前课程AI成果并记cache来源。不能复用旧artifact身份、旧run/binding、旧usage或伪造Host事件绕过这个检查。

本候选只返回该事务的提案，不越权写业务DB。operation_id按当前task/unit/scope与cache key确定，重复提交稳定、不同任务不同；正文源只引用当前SourceSnapshot。命中明确返回`actual_outbound_attempts:0, attempts:[], usage:[]`；不把缺失token字段写0或复制seed usage。现有RunView的空attempts可得出站0，但需明确cache origin，不能把它误解为provider账已结算。

候选namespace包含：课程、完整来源reference／位置／解析版本、snapshot id/hash、content_role、未改字符、模型全部settings（含思考／输出／超时及未来额外设置）、用户指令、语言、格式及格式版本、冻结prompt／system prompt、引擎和provider作用域指纹。未知顶层参数拒绝；上下文只能空。model／provider指纹由后端提供，不包含凭据。

seed只允许同course/snapshot/frozen key、terminal完成且对账完成、未停／取消／截断、范围／来源仍当前、成果已保存且用量已知并settled的结果；usage未知也保守跳过。已有run或unknown先返回reconcile_required；暂停、旧范围和来源失效阻断。这样cache不成为重发／未知结果掩盖路径。

进程LRU不持久化，重启只丢优化，不影响已有成果。必须由后端持有同一helper进程；每请求重新启动会让上游内存LRU永远miss。上限100条、每正文64KiB，返回值不暴露可变缓存对象。弱hash退化被拒绝。超过上游20,000 UTF-16选区限制跳过缓存，**原文不截断，普通翻译仍走既有链**。

## 扫描器的有效与无效位置

当前 `_prompt` 明确“直接返回译文”，普通body可能没有任何`src/tgt`对象；扫描结果为空不是译文失败。Harness原生llm/bodyappend事件也不叫这些字段。将译文再JSON包装后送扫描器只是在演示，不能算真实复用；本候选不这样接。

最小替代位置是未来**明确的分段对照输出**：同一次Host请求改为显式`aligned_ndjson_v1`结果协议，扫描其body delta形成增量段，不增加模型步骤或供应商；普通翻译完全不调用。候选已准备collector并用fixture验证，但没有把它称为当前产品已接通。

原scanner的done不能代替Host终态；若只有合法首段、坏中段／残尾、done后垃圾，上游仍可能返回“有segments”。外包装保持raw，要求真实Host完成、严格NDJSON记录、最终done及`segments.src`逐字符拼接严格等于所选文字；失败保留raw并返回invalid/incomplete，不自动修复、补邻文、再调模型或降级成普通结果。原scanner仍承担实际增量扫描，严格终检承担完成性。旧数组／pretty／围栏容错有特征测试，但新显式协议不把它们当合格终态。

## selectionRef 的实际适配

- 原页码标签函数可用于现有来源chip：例如page_index2显示“教材 第3页”。返回的jump仍是项目完整reference、course和snapshot id，简化locator只是显示，不是读取权限或回跳真相源；原生页、跨页坐标、版本与角色不丢失。
- serializer原样会在20,000 UTF-16后截断并加`\n…`，甚至可能切开代理对；不能作为raw snapshot权威存储。候选先拒绝超限缓存，再调用原函数序列化包含course/snapshot/hash/role/完整reference的选区数据，实际用于缓存身份digest。普通超长选区继续保留，不拿预览文本替代原文。
- 本项目既有SourceSnapshot已有完整身份；不新增DS retrieval资源、不替换source_snapshot_id、不通过locator回读更多材料。AI生成或混合来源的角色按输入原样保留。

## 交付与验证

可迁入候选：[目录与运行说明](../../../third_party/deepstudent/checks/selection-translation/README.md)。重点文件是 `candidate.cjs`、有界持久 `helper.cjs`、固定源码构建 `build.cjs`、原selection归档及适配通知；根方可选择直接前端编译纯标签函数，但缓存准入／成果保存应留在后端业务边界。

实际已执行：

```powershell
node third_party/deepstudent/checks/selection-translation/verify.cjs
```

结果：14/14 fixture、原TS／提取TS严格类型检查、原件hash检查、仅复制JS／dist／notices后的独立helper连续remember→lookup→当前task publication均通过。覆盖24项身份／参数差异、失败／停止／unknown／截断／未结算／错误归属负例、100条LRU淘汰、无secure crypto与digest失败、超限不截断、当前成果身份／幂等提案、逐UTF-16分块扫描、选中文字精确覆盖与Host终态，以及2MiB输入拒绝。未调用真实模型，actual model calls=0。

缓存中的 `manifest.json/build-manifest.json/validation.json/standalone-fixture.json` 保存hash、依赖、完整命令输出和IPC示例。首次12/13通过、标签适配生成字符串丢regex反斜线导致`{{value}}`未插值；已用String.raw修正，仅改本地adapter，原函数slice未变，历史保留。

未覆盖：正式cache准入／CAS保存事务与安装锁、活动停止时真实Host/accounting、真实模型内容、真实用户材料、产品显式对照翻译模式、缓存跨重启持久化。候选通过不等于上述能力已交付。
