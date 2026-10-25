# PDF.js 5.4.624 持久 OCR 文字层与原生查找审计

日期：2026-10-23。独立范围为正式已部署 Generic Viewer 的小型适配候选与真实浏览器合成验证。仅新增本报告及 `.local/cache/workbench/pdf-ocr-layer-audit/`；未修改产品、资源锁、上游 Viewer 或运行实例。延续 [阅读与 OCR 引擎审计](reading-engine-audit.md)，按 REA 完整源码／实际制品路由检查。

## 结论与证据边界

**可以保持原 PDF 画布，把已有扫描页 OCR 接入官方 TextLayer、连续分页和原生查找。必须同时接 `streamTextContent` 与 `getTextContent`，并显式更新 FindController 的文本缓存。** 只添加 DOM 文字或只覆盖其中一个接口不足以闭环。

候选 `ocr-adapter.mjs` 已在实际 Chromium 中通过扫描页无字→已保存 OCR 到达→鼠标拖选／真实剪贴板复制→原生查找→原生页与 OCR 页跨页选取→缩放／四方向旋转／CropBox／分页重绘。初次审计和 div 补充使用合成 PDF 与先前已得到的本地 OCR 词框，没有新 OCR 推理。后续几何故障补充为获得真实单字符框，对同一自有合成 PNG 执行一次已部署本地 OCR，见末节；全过程没有模型 OCR、用户资料、屏幕或视频图像。本报告的独立候选验证不代表 S01／S02、实际 Harness 或用户学习体验通过。

重要边界：这个版本没有公开的单页 Find 文本失效方法。候选使用公共 `setDocument` 重建查找数据，但需要**只读**私有 `_extractTextPromises` 等待旧提取结束；它不是完全依赖公共 API 的无侵入方案。长文档成本仍需正式适配；后补实证表明重复修订可以通过替换文字层 div 解决监听累积，无需为此修改 vendor，见下文。

## 固定制品与输入

HTTP fixture 的 `/vendor/` 直接只读映射 `.runtime/workbench/resources/pdfjs/5.4.624/`，没有换成 audit 内的另一份引擎。浏览器返回 `version=5.4.624`、`build=384c6208b`。

| 实际文件 | SHA-256 |
| --- | --- |
| `build/pdf.mjs` | `e668546e4f396e7769e141b7b0d09aed91c84e1752c7642a2fe87f335dfab84f` |
| `build/pdf.worker.mjs` | `a6a52b63b21b901a70c28d662c72716df14ffbfc4b54436747df333add0649cb` |
| `web/viewer.mjs` | `596309263f46ae50c30d54467c90acf3d79876cc6ed9d00c834d8c4002913aa3` |
| 缓存候选 `ocr-adapter.mjs`，12,818 字节 | `04cb5bb3d065272d69dae200c3f8a947bd1d510cd656f3d5dd5514e2571f1aa5` |
| 原合成 `source.pdf`，47,893 字节 | `1b4c071278acc470264680dc6ad2f24a889c323da710ebc0661eeba99a5c3035` |
| 合成 `crop-source.pdf`，47,525 字节 | `924a10afc09020e9b05ece66159012addc0f045286046b5ce4a67b4d4e2b6d4a` |

PDF.js 及资源原许可沿用正式固定制品的 Apache-2.0 与其子资源通知；本次没有复制或删改它们。制品来源和通知完整性见前述引擎审计及既有正式资源锁。

`source.pdf` 原样复制自 reading-audit 三页 fixture：第 1、3 页为原生文字，第 2 页仅含扫描图。`ocr.json` 从 reading-audit 的 `node-ocr-results.json` 中 `scanned-page-render.png` 读取已测结果，15 个词框、bitmap 为 1488×2105，明确原 bitmap→PDF 仿射为 `[0.4,0,0,-0.4,0,842]`。原识别结果中的“工作 人 台”等错误和原有空格、空行、终止 LF 均保留，未改字以改善选区。

Crop fixture 仅对同一合成页设置 `CropBox=[20,430,575,710]`、原生 `Rotate=90`，保留原页绘制内容。其 OCR payload 明确绑定新 fixture hash，仅为已知几何关系的测试准备，**不授权生产系统跨 `source_version` 静默复用 OCR**。没有制造带全文文字的替代 PDF。

环境：Node 24.14.0；项目已固定的 Chromium 可执行文件；实际 UA `HeadlessChrome/151.0.0.0`；1440×1000、DPR=1。浏览器运行的是完整官方 Viewer iframe，测试壳只负责输入和测量。

## 真实 API 接缝

以下行号来自上述正式实际制品，不用源仓库声明代替本机观察。可对应 Mozilla 的 [TextLayer 源码](https://github.com/mozilla/pdf.js/blob/v5.4.624/src/display/text_layer.js)、[PDFPageProxy](https://github.com/mozilla/pdf.js/blob/v5.4.624/src/display/api.js)、[TextLayerBuilder](https://github.com/mozilla/pdf.js/blob/v5.4.624/web/text_layer_builder.js) 和 [PDFFindController](https://github.com/mozilla/pdf.js/blob/v5.4.624/web/pdf_find_controller.js)。

| 实际制品代码位置 | 发现与最小接法 |
| --- | --- |
| `build/pdf.mjs:13969` `TextLayer` | 接受 `TextContent` 或 `ReadableStream<TextContent>`，需要真实 `items/styles` 和当前原生 `PageViewport`；不必自制文字布局器。跨 iframe 必须使用目标 iframe realm 的 `ReadableStream`，否则其 `instanceof` 检查不匹配。 |
| `build/pdf.mjs:14950` `PDFPageProxy.streamTextContent` | 官方 `TextLayerBuilder` 的读取入口。扫描页有 OCR 时返回同一版本的 OCR 内容，未识别页继续原 native stream。 |
| `build/pdf.mjs:14967` `PDFPageProxy.getTextContent` | 聚合 `this.streamTextContent`；原生查找另走此入口。保存原 `getTextContent.bind(page)` 后再替换 stream，并不能取得“未替换原文”。候选先保存原 stream，再独立聚合它以判定是否原生空页，防止递归与 OCR 污染。 |
| `web/viewer.mjs:12359` `TextLayerBuilder` | `render` 用 stream 构建文字层并把 `textDivs/textContentItemsStr` 交给原生 highlighter。已有空文字层收到 OCR 时只取消／重建其文字层，保留 canvas；以后的分页重建仍从被包装 page proxy 读取。 |
| `web/viewer.mjs:6754/6834/7047` FindController | `setDocument` 复位缓存；首次 find 从 `getTextContent({disableNormalization:true})` 读取并保存每页文本、差异映射与 promises。相同查询事件、highlighter 更新或 DOM 重绘均不会重新提取已缓存扫描空页。 |
| `web/viewer.mjs:7210` 附近页面编辑处理 | 更新页面数组／匹配映射用途，不能充当 OCR 字符内容失效通知。 |
| `web/viewer.mjs:12427/12438` `cancel/#bindMouse` | `cancel` 移除全局选择监听登记，但没有解绑 div 自有 mousedown/copy 监听。反复在同一 div 上 cancel/render 会积累监听；替换 div 的补充实证见下文。 |
| `web/viewer.mjs:12448` 原 copy | 会对 `Selection.toString()` 做 Unicode 规范化并移除 NUL。要求原字符保真的 OCR 复制不能直接沿用这一输出。 |

候选只包装当前实际 `PDFDocumentProxy.getPage` 和返回的同一 `PDFPageProxy` 的两个文本方法；不替换渲染文档、page viewport、worker、画布、注释或 PDF 字节。只给 FindController 提供范围极窄的查找 facade。卸载时仅恢复仍由本 adapter 拥有的方法和自有监听。

## OCR→TextContent：坐标与字符

持久结果至少需要固定 `source_version/page_number/revision`、原始 `text`、按原文字序排列的 `words[{text,bbox}]`、bitmap 尺寸及**识别那次栅格化**的完整 `image_to_pdf[6]`。不能从当前显示缩放反猜。页面 CropBox 的原点、原生旋转、栅格旋转／裁剪、单位或像素比例都须由生产者明确记录。

候选按原 text 顺序寻找每个 word，之间只接受原 text 中的空白切片；不能对齐就失败。每个 `item.str` 都是原串切片；不做 trim、NFKC、纠错、自动空格或隐藏字符补齐，`items.map(i=>i.str).join('') === payload.text` 为硬断言。原 LF 直接放入空白 item，`hasEOL=false` 防止 Find 再追加第二份 LF。词框外空白只能标为推断几何，不能冒充识别出的字框。

每个词框四角先经保存的仿射转换到固定原 PDF user space，再生成 TextLayer 的 transform/width；实际显示继续交给每次原生 `PageViewport`。因此旋转／缩放只更新官方 viewport，不会重写持久原页框。扫描页以 `native.items.some(item=>item.str?.trim())` 拒绝覆盖已有原生文字；混合原生与 OCR 同页不在候选支持范围。非空词框、bitmap 边界、仿射正交性及转换后 page.view 边界均检查，越界直接拒绝，不静默裁剪词。

实证中有两处不能忽略：

- TextLayer 有 Canvas `fontBoundingBox*` 指标时优先采用它，单写 style.ascent 不控制实际文字框。候选按同一 font family、原生 `DEFAULT_FONT_SIZE=30` 在目标 document 测得 ascent ratio，并补偿 baseline。此 family 使用 `Arial, sans-serif`，避免与原生 generic family 的内部指标缓存混淆。
- 本版 TextLayer 对单字符通常跳过 width fitting；单个中文字的 span 可能比 OCR 框宽很多。候选仅对 UTF-16 长度为 1 的 item 将 transform 第一列置为双倍，触发原实现的 anisotropic width fitting，最终仍由 `geom.width` 约束。**str 完全不变**。这是版本相关的几何适配；正式可改为窄 vendor 开关强制 fit，而不能加不可见字符。

初版未处理这两点时框误差约 21.22 PDF 单位，现已修正。最终最多约 0.129 PDF 单位是合成样例的 DOM 框映回误差，**不是 OCR 识别准确率或任意教材字形精度**。词框不能给部分单字提供真实字符坐标。

## Find 缓存更新与竞争窗口

最小接法可从候选提取以下片段；省略了版本、来源、权限、释放和错误处理，不应整段当作完整产品服务：

```js
// Before replacing either page text method:
const nativeStream = page.streamTextContent.bind(page);
page.streamTextContent = options => ocrEntry
  ? new viewerWindow.ReadableStream({start(c) { c.enqueue(ocrEntry.content); c.close(); }})
  : nativeStream(options);
page.getTextContent = options => ocrEntry
  ? Promise.resolve(ocrEntry.content)
  : collectOriginalStream(nativeStream(options));

// After native-empty OCR contents and the text layer are installed:
await Promise.allSettled([...findController._extractTextPromises]);
assertCurrentDocument();
const previous = findController.state ? {...findController.state} : null;
findController.setDocument(makeFrozenOcrRevisionFacade(originalPdf));
eventBus.dispatch('updatetextlayermatches', {source: findController, pageIndex: -1});
if (previous?.query?.length) {
  eventBus.dispatch('find', {...previous, source: findController, type: 'again'});
}
```

这里 `ocrEntry` 在真正实现中按 page number 查找；facade 为 `{numPages, getPage(n) -> {getTextContent}}`，每次创建冻结 OCR map 快照，未识别页用保存的原 native stream 聚合。原 `app.pdfDocument`、LinkService 和 PDFViewer 仍持有原文档。query 的 case/whole-word/highlight/direction 等原状态一起重放。候选检查 PDF.js 精确版本及数组形状，串行刷新，不直接写 `_pageContents/_pageMatches/_pageDiffs` 等私有缓存。

等待旧提取是必需的：其异步 then 回调会写 `this` 上的缓存，新 `setDocument` 之后旧回调仍可能写进新数组。`setDocument` 自身不取消已启动的原 `getTextContent`。本次门控测试把旧 native stream 故意阻塞，OCR 已在文字层显示，但 apply/find 刷新保持 pending；放开后才重建，最终命中 `[0,1,0]`。

代价与正式建议：一次刷新会使所有页面重新进入查找提取，OCR 连续到达时应由正式桥接合并修订；不能每个词触发。大文档和永久挂起 stream 未评估，业务不能把此 pending 当 OCR 不可用或偷偷跳过竞争保护。可将文字层就绪和查找更新状态分开；若需要有界取消或真正单页更新，给固定 vendor 增加很窄的带 generation fence 的文本失效 API，再更新资源 hash／许可差异。不要在旧 promise 仍活动时只设空数组。

## 复制与选区来源

候选 `captureSelection()` 只遍历实际 Range 相交的 `.page .textLayer`，按真实 start/end 截取该页 fragment；只把原生真实 BR 表达为 LF，OCR 空白本来就是字面文本。不读取相邻 OCR 词、未选页、图片或原 PDF 全文作为材料。

原生 `Selection.toString()` 是可见布局字符串：本样例会省略末尾已选 LF，也会在跨页加布局换行；`Range` 中原始字符不同。候选对包含 OCR 的 copy 事件使用捕获阶段监听，按实际选中 fragment 写入剪贴板，避免原生 Unicode 规范化。跨页原文若没有页末 LF，候选不自行造字符分隔，页关系保留在 `fragments` 中。UI 若需要分段呈现，不能把展现分隔符悄悄变成发送原文。

快照结构含 `source_version` 和每页 `page_number/text/origin/ocr_revision`；OCR 部分附原串 UTF-16 start/end 与原 PDF quad，明确 `ocr_word_box` 或 `inferred_whitespace`。选择词中一部分时文字范围仍准确，框只是整个已命中词的包围范围，不能以该框扩大实际发送文字。原生页区域仍交由正式现有 selection guard 的 viewport 反变换处理，候选在其 regions 留空，未伪造原生字符框。

## 实际验证与证据

缓存 `results.json`、`advanced-results.json` 与逐条 CLI 原始输出 `browser-transcript.json`、`advanced-transcript.json` 是最终成功证据；`manifest.json` 记录输入、代码和证据文件大小／SHA-256、环境与历史失败。浏览器测试使用页面真实 DOM、文字层、find controller、鼠标和系统剪贴板，不是 mock PDF.js。

| 检查 | 结果 |
| --- | --- |
| 第 2 页到达前 | native items=0；查找 WORKBENCH 三页 `[0,0,0]`。 |
| 持久结果到达 | 15 词＋原空白共 30 items；查找自动重放后 `[0,1,0]`；canonical 原文严格相同。 |
| 原画布 | 到达前后同一 canvas 节点；RGBA SHA-256 均为 `d2d1e695adefee924ae9a27514bc4ef8000f5abdb56cadf1cb24393b38b881b2`。 |
| 真实指针与复制 | 鼠标拖选前两词，Ctrl+C 后实际 clipboard 为 `LEARNING WORKBENCH`。 |
| 全 OCR 复制 | 真实 Ctrl+C 完全等于持久原 text，含原终止 LF；未纠正“人”。 |
| 原生＋OCR 跨页 | Range 从原生页 1 末句到 OCR 页 2 前两词；实际复制为 `Cross-page selection starts here.LEARNING WORKBENCH`；快照页号 `[1,2]`，不含下一行 `Selected words only`。跨页范围用真实浏览器 DOM Range 建立，未声明测试了跨页鼠标拖动手感。 |
| 查找原生与 OCR | LEARNING 原生查找为 `[1,1,1]`，保持原生高亮映射；不是自制查找 UI。未声明跨页短语匹配。 |
| 缩放与四旋转 | 普通页用户缩放 1.4、viewport scale≈1.8667，rotation 0/90/180/270；15 词 DOM 框各自逆变换，最大误差 `0.06358642578129547` PDF 单位。各方向选中 WORKBENCH 的原 PDF quad 完全不变。 |
| CropBox 与原始 Rotate | view `[20,430,575,710]`、原始 Rotate90、用户缩放1.2及四旋转；viewport scale≈1.6，最大误差 `0.12866357848500343`；选区 quad 不变；WORKBENCH仍 `[0,1,0]`。 |
| 分页文字层重建 | 原 viewer queue 触发 reset/update/forceRendering，产生新的 TextLayerBuilder；原 text 与框恢复，原生查找仍 `[0,1,0]`。 |
| 字符不变 | 独立合成 `ﬁ`、NBSP、全角 `Ｘ`、LF 在 converter 拼接后原样相同；没有把该小例冒充整套 Unicode 实际复制覆盖。 |
| 负例 | 错版本来源→`OCR_SOURCE_MISMATCH`；原生非空页→`OCR_NATIVE_TEXT_EXISTS`；负 bitmap 框→`OCR_WORD_INVALID`；原页范围外→`OCR_WORD_OUTSIDE_PAGE_VIEW`；已释放旧文档回调→`OCR_STALE_DOCUMENT`。 |
| 旧提取竞争 | gate 未释放时 OCR 可见、apply未结束；释放后 apply结束、find `[0,1,0]`。 |

最终实际命令均 exit 0：

```powershell
& .\backend\.venv\Scripts\python.exe .local/cache/workbench/pdf-ocr-layer-audit/probe.py
& .\backend\.venv\Scripts\python.exe .local/cache/workbench/pdf-ocr-layer-audit/advanced.py
& .\backend\.venv\Scripts\python.exe .local/cache/workbench/pdf-ocr-layer-audit/build_manifest.py
```

文档检查 `backend/.venv/Scripts/python.exe scripts/check_docs.py` 实际通过：`Markdown navigation: 136 files, 0 errors; upstream notices excluded`。收尾仅关闭具名 `pdf-ocr-layer-audit` 浏览器；localhost服务按监听 PID 的完整 command line 确认属于本目录 `serve.py` 后关闭，未操作其他实例。

这些命令需先由 `serve.py` 启动独占 localhost 服务，并通过项目 Playwright CLI 打开对应 `fixture.html` 会话；port由 `port.txt` 记录，浏览器参数由 `cli.config.json` 固定。它们是临时审计程序，正式运行不得读取此目录。

保留的失败及修正见 manifest：初版字体框失配；直接 `reset()+draw()` 被原 queue 取消；最初 Selection 输出漏终止 LF；fixture 早期 JS 字符串错误；以及 Viewer ViewHistory 在 `firstPagePromise` 后异步恢复旧旋转，导致初次复跑拖选变成 `NING WORKBENCH`。最终 fixture 等待原生 `documentinit` 后再设初始视图，所有最终断言通过，未以剪裁／改字掩盖错误。

## 迁入建议及未覆盖

根集成方可把 converter、原 stream 聚合、page 双接口桥接、查找快照／刷新、选区快照分别迁入正式 reading bridge；保留完整 Generic Viewer。缓存里的诊断 `entry()`、fixture全局对象、临时服务与测试权限不进入产品。

正式迁入前或同时处理的具体点：

1. **生命周期**：初版候选重复使用同一 div 的风险保留；后补候选采用下面已经验证的 cancel／clone／替换 div 路径，每次仅一组原生监听且旧节点可回收，不必为此窄改 vendor。正式队列仍须避开活动选区和并发渲染窗口；五次合成修订不是长时间生产内存浸泡验收。不能仅改 `.replaceChildren()` 就称已释放监听。
2. **查找演进**：精确锁版本、只读 private seam，升级立即失败并重新验证。先控制刷新合并和文档切换；有需要再加 generation fence 的单页失效小补丁，不能隐式私改多个缓存数组。
3. **权限与任务归属**：候选检查当前原文档、source hash、COPY permission、原页界限；未测试禁止复制的 PDF，也没有替代正式 course/asset/source-version/expected OCR revision/document epoch、后台任务优先级和取消 guard。旧 OCR 到达后的源版本裁决由正式 API 完成。
4. **输入与精度**：仅原生空页、LTR 中英词框和正交仿射；UserUnit非1、DPR非1、RTL／竖排、任意斜拍／透视、多栏顺序、字符级框、边缘舍入、极大页、字体环境差异未覆盖。边界拒绝不能替代生产者正确保存栅格矩阵。扫描图原有错误不会因文字层可选而修正。
5. **体验与部署**：实际产品／Harness iframe路由、CSP、COPY权限禁用、离线启动、真实用户教材、长文档性能、真实材料下长时间持续OCR修订和跨页指针拖动体验均 `not_run`。本报告独立验证未调用真实模型或使用用户素材，也未改变任何正式安装闭包。

该候选证明关键接口路径可用，并留下可迁入代码与真实限制；正式任务和选择权限接入及最终验收由根集成方负责。

## 补充实证：替换 div 可避免重复监听，无需改 vendor

这是独立追加验证；原 `ocr-adapter.mjs`、初版 `manifest.json/results.json` 等证据保持原样，后补证据另存 `replacement-*.json`。**在固定 5.4.624 完整 Generic Viewer、原生空白扫描页、已经完成的文字层上，所提 cancel→cloneNode(false)→replaceWith→重新赋值 builder.div→render 路径成立。** 两个新 adapter 只改这一小段，不替换 builder、画布或原 PDF。

源码上的精确依据：

- `builder.div` 在构造函数中是普通可写属性。`cancel()` 首先用当前旧 div 从静态 `#textLayers` Map 移除登记，并取消旧 TextLayer、禁用旧 highlighter／accessibility mapping；因此必须在重新赋值前 cancel。
- `render()` 发现 `#textLayer=null` 后重新构造 TextLayer，读取当时的 `this.div`，更新 highlighter 与 accessibility 的 text mapping。旧 `#renderingDone=true` 不会阻止重建，因为快速 update 分支还要求 `#textLayer` 存在。
- `endOfContent` 是本次 render 的局部新节点。`#bindMouse` 将新 div→新 end 节点放入全局选择 Map；旧 div 不会重新登记。`cloneNode(false)` 不复制旧节点的事件监听或子节点。
- 现有私有 `#onAppend` 无需读写；render 自动调用它。`PDFPageView.draw` 设置的回调进入 `#addLayer`（`web/viewer.mjs:12663`），先把 `#layers[textLayer]` 槽更新为新 div，再对旧 div 调 replaceWith。旧 div 已由 adapter 脱离时这次 replaceWith 不再改变位置，先前插入的新 div 仍在正确层位；实测每页始终只有一个 `.textLayer`。
- 原生 global selection listeners 持有的是静态 Map；旧 div 删除后没有因为其他页仍存在就继续作为 Map key。对已选范围的私有 `prevRange` 也不能仅凭源码推断安全，本次通过实际选择、清除及重绘后的节点 GC 验证了测试状态下的回收。

建议迁入的是带两项属性清理的版本：

```js
builder.cancel();
const oldDiv = builder.div;
const nextDiv = oldDiv.cloneNode(false);
nextDiv.hidden = false;
nextDiv.classList.remove('selecting');
oldDiv.replaceWith(nextDiv);
builder.div = nextDiv;
await builder.render({viewport: view.viewport});
tagLayer(pageNumber);
```

两个属性清理都有实际依据。原 div 经 `builder.hide()` 后，未经清理的 clone 会继承 `hidden`；render 后虽然 highlighter.enabled=true，词 span 的 `getClientRects()` 数量仍为 0。带清理版本同样流程下 hidden=false、selecting=false、矩形数量=1、文字和查找正常。不要只在 render 后调用 `show()` 来替代这两行：本版 render 已启用 highlighter，show 的隐藏分支还会再次启用它。

| 后补候选与证据 | 结果 |
| --- | --- |
| `ocr-adapter-clone.mjs`，精确原提案 | SHA-256 `5186c06a9b1b17ac12e1f07bef7154487537249ecb73c0293e96134263d00e40`；13,100 字节。可见／已完成文字层上通过，hidden 继承负例已确认。 |
| `ocr-adapter-clone-visible.mjs`，加上述两行 | SHA-256 `9f91138203e6e84da63911e0d1ffd7149a8a19b27e929c95f8532ea1610a736b`；13,169 字节。相同全链和 hidden/selecting 边界通过。 |
| 每个版本连续 apply 5 次 | 相同原 OCR 字符和词框、五个显式测试 revision；原 builder 与 canvas 保留，每次只一个新 div／一个 endOfContent，查找 `[0,1,0]`，原字符严格不变。这不是五份新 OCR 推理。 |
| Chrome 真实监听诊断 | 每次新 div 的 mousedown=1、copy=1；document 的原生全局选择监听数量没有随修订增长。旧 div 暂时仍各有一份自身监听，这是可回收节点上的循环，不是已解绑的宣称。 |
| 单次真实鼠标拖选＋Ctrl+C | mousedown 回调执行1；adapter copy 执行1；原生 div copy执行0，因为 adapter 的捕获监听按设计截住事件。实际剪贴板仍为 `LEARNING WORKBENCH`。 |
| 原生 copy 诊断 | 仅在诊断期间临时绕开 adapter 捕获层，原 div copy 回调执行1，实际剪贴板相同；诊断后恢复所有原监听。没有用被截住的原生回调冒充“执行一次”。 |
| 旧 div 可回收性 | 只保存 WeakRef，不保存节点／handler远端句柄；CDP 两次显式 GC 后两版均为5/5旧 div 已回收。真实选取、四方向 zoom及 reset 后累计6/6回收，证明这些测试状态下没有全局强引用继续保留旧 div。 |
| zoom／rotation／reset | 用户缩放1.4/1.2/1.5/1、旋转90/180/270/0；canonical不变、查找 `[0,1,0]`、每个当前 div 两类原生监听各1；最大逆变换框误差 `0.11476076529220336` PDF单位。原 queue reset后仍单文字层和各1监听，旧div回收。 |

诊断用 CDP `Runtime.enable` 找到 **Viewer iframe 的 default executionContext**，在那里以 command-line API 调用 `getEventListeners`。在父页上下文调用会错误地得到空对象，不能据此宣布没有监听。事件计数临时包裹实际已注册函数、原参数调用并及时恢复；运行结果只序列化数字／字符串，回收检查不保留 listener/objectId 作为根。

保留的诊断修正：首次回收只得到4/5和4/6；定位到测试 `waitForFunction` 返回 `.endOfContent` DOM 节点而产生未 dispose 的 JSHandle。改成返回 boolean并显式 dispose，新的完整运行得到5/5和6/6。另有跨 iframe counter lexical binding错误及 CLI run-code console.log输出读取错误，都记录在 `replacement-manifest.json` 的 diagnostic_history；未把这些取证问题当成产品泄漏或静默省略失败。

**活动选区限制已直接测出**：选中 `WORKBENCH` 后替换其 div，DOM Selection 变空；词串、canvas 和查找仍正确。两行属性清理不会保留正在进行的拖选。正式拥有队列应延后此时的换层，或先按项目契约冻结最终选区快照；不能把这个方案描述为“活动选区无损更新”。并发 render/cancel、tagged PDF／表单／XFA 的辅助结构及长时间生产内存浸泡不在本补充覆盖范围。

最终命令实际 exit 0：

```powershell
& .\backend\.venv\Scripts\python.exe .local/cache/workbench/pdf-ocr-layer-audit/replacement.py
& .\backend\.venv\Scripts\python.exe .local/cache/workbench/pdf-ocr-layer-audit/replacement.py --visible
& .\backend\.venv\Scripts\python.exe .local/cache/workbench/pdf-ocr-layer-audit/replacement-edges.py
& .\backend\.venv\Scripts\python.exe .local/cache/workbench/pdf-ocr-layer-audit/build_replacement_manifest.py
```

完整数字、原始 CLI 输出和代码 hash 分别见同缓存内 `replacement-results.json`／`replacement-transcript.json`、`replacement-visible-results.json`／`replacement-visible-transcript.json`、`replacement-edges-results.json`／`replacement-edges-transcript.json` 与 `replacement-manifest.json`。这是可迁入候选证据，未修改或验收产品。

## 补充根因：整段 Range 把临时 endOfContent 算进选区

根集成方反馈正式 OCR 阅读的真实 Chromium 鼠标选取／复制已通过，但提交来源卡时 `DocumentReader.capture` 返回 `PDF_SELECTION_MISMATCH`。本补充只读产品源码和其合成运行证据，按 diagnosing-bugs 的红绿回放路径调查；未访问根方 19231 服务、修改产品或重建前端。独占复现端口为63400。

### 已定位的根因与时序

正式 r2 的 `output/playwright/workbench-ocr-reader-20261023-r2/selection-request.json` 中，文字为 `LEARNING`，regions 末尾却多出 `{x0:0,y0:0,x1:1,y1:1}`，实际响应422、`PDF_SELECTION_MISMATCH`。同目录 `selection-geometry.json` 记录复制后的 Range 起于 `L` offset0、止于空格 offset0；那时没有整页框。canvas 与 canvasWrapper 都是 left20.5/top32/719×931，完全相同。

这是两个时刻的真实几何差异，来源为固定 Viewer 原生选择辅助节点：

1. `web/viewer.mjs:12499` 附近的 global `selectionchange` 监听在非 Firefox 路径找到选区端点。末尾 offset0 时会退到前一个有内容的兄弟节点。
2. 同文件12543附近把 `endOfContent.style.width/height` 设成整个 textLayer 的尺寸，并把这个节点插到锚点旁。选区终点是下一个空格节点／span 的 offset0 时，空 `endOfContent` 恰好位于文字选区内部。
3. 该节点没有文字，因此 `Range.toString()` 仍只有所选词；但**整段 `Range.getClientRects()` 会包含它的整页 CSS 框**。产品原 `inspectSelection` 把所有非零框转换为 PageRegion，产生整页框。
4. global `pointerup`、`keyup` 或 `blur` 的 reset（12478附近）又把 end 节点 append 回 textLayer 尾，清空 width/height。它没有主动调用产品 `inspectSelection` 或派发选区刷新事件。因此旧回调保存的 regions 可仍含整页框，而后续 copy／诊断时读取的相同文字范围已经没有该框。

后台按 PageRegion 包含的 glyph 中心重新核对原文；一旦传入整页框，就正确地命中整页字符并拒绝“所选词”。**不能放松后台校验以掩盖该错误，也不需要为了这个错误修改单字符 fit2 transform。**

### 独立实际复现与后台红绿

缓存保留了正式 `ocrLayer.mjs` 的代码快照，以及 `pdfAdapter.ts` 经项目现有 TypeScript 仅擦除类型／imports 后的 JS 快照；调用其真实 `inspectSelection` 方法，不自行重写旧算法。原 PDF 和原画布不变。为取得实际字符框，对既有合成扫描 PNG 用已部署 Tesseract 本地闭包执行一次识别，返回相同原 OCR text、51个 symbols；引擎 `5.1.0-284-g9c08`。没有模型调用、外发材料或读取用户资料。

真实鼠标这份 fixture 选中了 `EARNING`；记录保留该实际结果，没有把它改写为 `LEARNING`。原 r2 是真实鼠标 `LEARNING` 的产品证据，独立回放证明的是同一故障模式。

| 时刻／方法 | 实际结果 |
| --- | --- |
| 拖选期间 | 原产品方法14个rect，其中末尾为整页；endOfContent位于G之后、空格之前，range相交，框793×1122；7个实际 TEXT_NODE 仅7个文字框。 |
| mouseup之后 | 同一 `EARNING` 原方法13个rect，整页框消失；end移回层尾，框793×0，range不再相交。 |
| copy之后 | 原文字与clipboard不变，仍无整页框。15个selectionchange记录中6次出现整页框，并非每次事件都出现。 |
| 原拖选regions→真实后台方法 | `PDF_SELECTION_MISMATCH`；hit text为整页 `LEARNINGWORKBENCHSelectedwordsonly12345学习工作人台中文文字选择`。 |
| 仅改为实际TEXT_NODE裁剪regions | 同一实际 `_with_ocr/capture` 接受；hit text严格为 `EARNING`，持久引用区间 start1/end8。 |
| mouseup之后旧算法重取regions | 接受，进一步证明是临时节点和回调时刻的问题。 |

回放调用真实 `DocumentReader._with_ocr/capture`，只对 SQL row 取得和 `_save_verified` 最终持久边界使用独立 fixture doubles；没有用自写比较函数冒充产品拒绝／接受，也没有声称本回放完成了实际数据库来源卡保存。

以下是已实际运行的红绿命令；红命令是预期失败、用于锁定旧行为，不应记成修复未通过：

```powershell
# exit1, AssertionError: PDF_SELECTION_MISMATCH reproduced by the unfiltered Range rectangles
& .\backend\.venv\Scripts\python.exe .local/cache/workbench/pdf-ocr-layer-audit/geometry-replay.py --legacy
# exit0, legacy拒绝且TEXT_NODE方案接受
& .\backend\.venv\Scripts\python.exe .local/cache/workbench/pdf-ocr-layer-audit/geometry-replay.py
```

冻结命令输出分别保存为 `geometry-replay-red-command.json`、`geometry-replay-green-command.json`；原始浏览器数据为 `geometry-selection-results.json/transcript.json`。`geometry-manifest.json` 记录所有输入、产品 JS 快照、根方 r2 输入与证据 hash。

### 最小修复的独立覆盖

保留原来每页实际 fragment 的文字提取，只改变几何来源：在该页裁过的 Range 内，用 TreeWalker 枚举 TEXT_NODE，精确取交集，跳过 collapsed／零字符范围，再读取该小 Range 的 rect。示意与独立验证执行代码一致：

```js
const walker = doc.createTreeWalker(layer, viewerWindow.NodeFilter.SHOW_TEXT);
while (walker.nextNode()) {
  const node = walker.currentNode;
  if (!part.intersectsNode(node)) continue;
  if (node.parentElement?.closest('.endOfContent,button,input,textarea,select')) continue;
  const leaf = doc.createRange();
  leaf.selectNodeContents(node);
  if (part.compareBoundaryPoints(viewerWindow.Range.START_TO_START, leaf) > 0)
    leaf.setStart(part.startContainer, part.startOffset);
  if (part.compareBoundaryPoints(viewerWindow.Range.END_TO_END, leaf) < 0)
    leaf.setEnd(part.endContainer, part.endOffset);
  if (leaf.collapsed || !leaf.toString()) continue;
  rects.push(...[...leaf.getClientRects()].filter(r => r.width > 0 && r.height > 0));
}
```

必须精确裁 start/end；仅 `intersectsNode` 不能保证选中了节点中的字符，尤其是 offset0 空格与空 TEXT_NODE。不要只按矩形面积过滤，因为用户明确选中整页文字仍须合法。几何遍历不要改成新的文字拼接器；原生 BR 的文本处理继续沿用已有真实 fragment 规则。

补充四种真实 DOM Range 边界均通过；它们是程序设置的实际浏览器 Range，不冒充四次鼠标拖选：

- L0→空格SPAN0：`LEARNING`，8个leaf框，后台接受。
- 词内部分 `ARN`：3个leaf框，后台接受，没有扩成整词。
- 末尾空TEXT_NODE offset0：`LEARNING`，8个leaf框，后台接受。
- display:contents 的 markedContent 嵌套：`LEARNING`，8个leaf框，后台接受；未把此合成结构称为完整 tagged PDF 辅助功能验收。

`geometry-variants-results.json` 保留上述完整框，`geometry-replay-results.json` 保留实际后台判定。`geometry-selection.py`、`geometry-variants.py`、绿色回放与 `build_geometry_manifest.py` 最终均 exit0。

替代假设核对：单字 span box 与 TEXT_NODE Range rect 确有约1～2px字体行框差异，但本例字符中心仍命中，leaf方案已由同一后端接受；canvasWrapper与实际canvas在根方r2和本独立fixture都一致。两者都不能解释额外整页框。一般 CropBox／旋转／DPR矩阵仍需既有验收，不因本故障定位而宣称全部覆盖。

正式 TreeWalker 修复、行合并、状态栏布局和真实来源卡创建由根集成方验证与交付；本补充未再次运行其产品或19231服务，不把独立时序／后台回放结果冒充正式产品通过。
